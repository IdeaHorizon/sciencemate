"""Bounded workspace records, with lossless content-addressed detail pages.

The transcript wrapper's limit includes the final UTF-8 JSON and newline.
Large path indexes remain in the same run, rather than becoming a misleading
partial checkpoint. Readers verify every page and restore the original fields.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_RECORD_BYTES = 5_000_000
MAX_WORKSPACE_PAGE_BYTES = 64_000
_CHUNK_BYTES = 45_000
_FIELDS = ("paths", "file_stats", "patch")
_EVENTS = {"workspace_changed", "workspace_checkpoint_requested"}
_REFERENCE = "workspace_details"


def _json_line(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _list_preview(values: list) -> list:
    preview = []
    size = 0
    for value in values[:50]:
        size += len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) + 2
        if size > MAX_WORKSPACE_PAGE_BYTES:
            break
        preview.append(value)
    return preview


def encode_workspace_record(record: dict[str, Any], transcript_path: Path) -> bytes:
    """Keep ordinary records unchanged; externalize only oversized workspace facts.

    Called after secret redaction and metadata attachment, at the actual writer.
    The pages are atomically installed before their reference is appended.
    """
    line = _json_line(record)
    newline_expansion = len(os.linesep.encode()) - 1
    if (record.get("event") not in _EVENTS
            or len(line) + newline_expansion <= MAX_TRANSCRIPT_RECORD_BYTES):
        return line
    detail = {key: record[key] for key in _FIELDS if key in record}
    raw = json.dumps(detail, ensure_ascii=False, default=str).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    directory = transcript_path.parent / "workspace-records"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(transcript_path.parent.resolve()):
        raise ValueError("Workspace detail directory escaped its run")
    target = directory / f"{digest}.jsonl"
    storage_hash = hashlib.sha256()
    storage_bytes = 0
    pages = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            for start in range(0, len(raw), _CHUNK_BYTES):
                page = _json_line({
                    "index": pages,
                    "content": base64.b64encode(raw[start:start + _CHUNK_BYTES]).decode("ascii"),
                })
                if len(page) > MAX_WORKSPACE_PAGE_BYTES:
                    raise ValueError("Workspace detail page exceeds its byte budget")
                output.write(page)
                storage_hash.update(page)
                storage_bytes += len(page)
                pages += 1
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    compact = dict(record)
    for key, value in detail.items():
        compact[key] = (
            _list_preview(value) if isinstance(value, list)
            else str(value).encode("utf-8")[:MAX_WORKSPACE_PAGE_BYTES].decode("utf-8", "ignore")
        )
    compact[_REFERENCE] = {
        "schema_version": 1,
        "encoding": "json-utf8-base64-pages",
        "path": f"workspace-records/{digest}.jsonl",
        "sha256": digest,
        "bytes": len(raw),
        "pages": pages,
        "storage_bytes": storage_bytes,
        "storage_sha256": storage_hash.hexdigest(),
        "fields": list(detail),
        "path_count": len(detail.get("paths", [])),
    }
    compact["workspace_details_external"] = True
    line = _json_line(compact)
    if len(line) + newline_expansion > MAX_TRANSCRIPT_RECORD_BYTES:
        raise ValueError("Workspace record metadata exceeds the transcript byte budget")
    return line


def restore_workspace_record(record: dict[str, Any], transcript_path: Path) -> dict[str, Any]:
    """Verify a same-run manifest and recover complete paths before checkpointing.

    Each physical JSONL page has its own fixed byte bound. The wrapper's read
    limit is unchanged; neither arbitrary paths nor unchecked file contents are
    accepted as replacement fields.
    """
    ref = record.get(_REFERENCE)
    if ref is None:
        if record.get("workspace_details_external"):
            raise ValueError("External workspace record manifest is missing")
        return record
    if record.get("event") not in _EVENTS or not isinstance(ref, dict):
        raise ValueError("Invalid external workspace record reference")
    digest = ref.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or ref.get("schema_version") != 1
            or ref.get("encoding") != "json-utf8-base64-pages"
            or ref.get("path") != f"workspace-records/{digest}.jsonl"):
        raise ValueError("Invalid external workspace record identity")
    root = transcript_path.parent.resolve()
    target = (root / ref["path"]).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("External workspace record is missing or escaped its run")
    if target.stat().st_size != ref.get("storage_bytes"):
        raise ValueError("External workspace record storage length mismatch")
    chunks = []
    storage_hash = hashlib.sha256()
    content_hash = hashlib.sha256()
    content_bytes = 0
    with target.open("rb") as source:
        index = 0
        while page := source.readline(MAX_WORKSPACE_PAGE_BYTES + 1):
            if len(page) > MAX_WORKSPACE_PAGE_BYTES or not page.endswith(b"\n"):
                raise ValueError("External workspace record page exceeds its byte budget")
            storage_hash.update(page)
            decoded = json.loads(page)
            if not isinstance(decoded, dict) or decoded.get("index") != index:
                raise ValueError("External workspace record page order mismatch")
            content = base64.b64decode(decoded["content"], validate=True)
            if len(content) > _CHUNK_BYTES:
                raise ValueError("External workspace record content page exceeds its byte budget")
            content_hash.update(content)
            content_bytes += len(content)
            chunks.append(content)
            index += 1
    if (index != ref.get("pages") or content_bytes != ref.get("bytes")
            or content_hash.hexdigest() != digest
            or storage_hash.hexdigest() != ref.get("storage_sha256")):
        raise ValueError("External workspace record content identity mismatch")
    detail = json.loads(b"".join(chunks))
    if (not isinstance(detail, dict) or set(detail) - set(_FIELDS)
            or set(detail) != set(ref.get("fields") or [])
            or not isinstance(detail.get("paths", []), list)
            or len(detail.get("paths", [])) != ref.get("path_count")):
        raise ValueError("External workspace record field or path count mismatch")
    return {**record, **detail}
