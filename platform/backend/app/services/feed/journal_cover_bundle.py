"""Portable public journal covers shipped with the application."""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit

from PIL import Image


def bundle_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "journal-covers"


@lru_cache(maxsize=2)
def _manifest(root: Path, identity: tuple[int, ...]) -> dict:
    try:
        value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        return value.get("journals", {}) if value.get("version") == 1 else {}
    except (OSError, ValueError, AttributeError):
        return {}


def metadata(key: str) -> dict:
    root = bundle_dir()
    try:
        stamp = (root / "manifest.json").stat()
        # Atomic replacement can preserve mtime on coarse clocks/filesystems.
        # Keep the file identity and size in the cache key as well.
        identity = (stamp.st_dev, stamp.st_ino, stamp.st_size,
                    stamp.st_mtime_ns, stamp.st_ctime_ns)
        records = _manifest(root, identity)
        entry = records.get(key, {})
        filename = entry.get("file", "")
        if not re.fullmatch(r"images/[a-f0-9]{64}\.webp", filename):
            return {}
        if not (root / filename).is_file():
            return {}
        return {**entry, "status": "available", "kind": "journal_cover", "method": "bundled",
                "content_type": "image/webp"}
    except (OSError, AttributeError):
        return {}


def read(key: str) -> tuple[bytes, str] | None:
    entry = metadata(key)
    if not entry:
        return None
    try:
        body = (bundle_dir() / entry["file"]).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
        return None
    return body, "image/webp"


def public_url(value: str) -> str:
    """Keep public provenance, never query tokens, credentials or local paths."""
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", ""))


def export(cache: Path, output: Path) -> dict:
    from app.services.feed import journal_covers as covers
    output.mkdir(parents=True, exist_ok=True)
    (output / "images").mkdir(exist_ok=True)
    manifest_path = output / "manifest.json"
    previous = covers._read(manifest_path).get("journals", {})
    records = dict(previous)
    skipped = 0
    for path in sorted(cache.glob("*.json")):
        if not re.fullmatch(r"[a-f0-9]{24}\.json", path.name):
            continue
        entry = covers._read(path)
        if entry.get("status") != "available":
            continue
        try:
            body = path.with_suffix(".image").read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if digest != entry.get("sha256"):
                raise ValueError("cache_hash_mismatch")
            old = records.get(path.stem, {})
            if old.get("source_sha256") == digest and (output / old.get("file", "missing")).is_file():
                continue
            validated = covers.validate_image(body, entry.get("content_type", ""), entry.get("image_url", ""))
            if validated is None:
                raise ValueError("invalid_cover")
            with Image.open(io.BytesIO(body)) as source:
                picture = source.convert("RGB")
                picture.thumbnail((360, 512), Image.Resampling.LANCZOS)
                encoded = io.BytesIO()
                picture.save(encoded, format="WEBP", quality=84, method=4)
                asset = encoded.getvalue()
                width, height = picture.size
            asset_hash = hashlib.sha256(asset).hexdigest()
            filename = f"images/{asset_hash}.webp"
            target = output / filename
            if not target.exists():
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    handle.write(asset)
                Path(handle.name).replace(target)
            records[path.stem] = {
                "journal": entry["journal"], "issns": covers._issns(entry.get("issns", [])),
                "publisher": entry.get("publisher", ""), "file": filename,
                "sha256": asset_hash, "source_sha256": digest, "width": width, "height": height,
                "checked_at": entry.get("checked_at", 0),
                "homepage": public_url(entry.get("homepage", "")),
                "source": public_url(entry.get("source", "")),
                "image_url": public_url(entry.get("image_url", "")),
            }
        except (OSError, ValueError, KeyError):
            skipped += 1
    manifest = {"version": 1, "journals": dict(sorted(records.items()))}
    covers._write(manifest_path, manifest)
    used = {entry["file"] for entry in records.values()}
    # Remove superseded generated assets, never unrelated files.
    for path in (output / "images").glob("*.webp"):
        if re.fullmatch(r"[a-f0-9]{64}\.webp", path.name) and f"images/{path.name}" not in used:
            path.unlink()
    return {"journals": len(records), "images": len(used), "skipped": skipped,
            "bytes": sum((output / name).stat().st_size for name in used)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=bundle_dir())
    args = parser.parse_args()
    print(json.dumps(export(args.cache, args.output)))
