import hashlib
import io
import json
import os
import time

from PIL import Image
import pytest

from app.services.feed import journal_cover_bundle as bundle
from app.services.feed import journal_cover_browser as browser
from app.services.feed import journal_cover_inventory as inventory
from app.services.feed import journal_covers as covers


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    cache, assets, runtime = (tmp_path / n for n in ("export-cache", "assets", "empty-runtime"))
    cache.mkdir(); runtime.mkdir()
    monkeypatch.setattr(bundle, "bundle_dir", lambda: assets)
    monkeypatch.setattr(covers, "cache_dir", lambda: runtime)
    journal = covers.Journal("Portable Journal", ("1234-5678",), "Publisher")
    stream = io.BytesIO()
    Image.new("RGB", (720, 1024), "navy").save(stream, format="PNG")
    body = stream.getvalue()
    (cache / f"{journal.key}.image").write_bytes(body)
    covers._write(cache / f"{journal.key}.json", {
        "status": "available", "journal": journal.name, "issns": journal.issns,
        "publisher": journal.publisher, "content_type": "image/png",
        "checked_at": time.time(), "sha256": hashlib.sha256(body).hexdigest(),
        "image_url": "https://publisher.test/cover.png?token=PRIVATE_TOKEN",
        "source": "https://username:PRIVATE_PASSWORD@publisher.test/journal?session=PRIVATE_SESSION",
        "cookie": "PRIVATE_COOKIE", "local_path": "/private/cache",
    })
    covers._write(cache / "browser-config.json", {"credential": "PRIVATE_BROWSER"})
    result = bundle.export(cache, assets)
    return journal, body, result


def test_export_contains_only_public_metadata_and_small_images(shipped):
    journal, _, result = shipped
    assert result["journals"] == result["images"] == 1
    assert result["skipped"] == 0
    manifest = (bundle.bundle_dir() / "manifest.json").read_text()
    assert "PRIVATE_" not in manifest
    assert "/private/cache" not in manifest
    assert not (bundle.bundle_dir() / "browser-config.json").exists()
    entry = bundle.metadata(journal.key)
    assert entry["source"] == "https://publisher.test/journal"
    assert entry["width"] <= 360 and entry["height"] <= 512
    with Image.open(io.BytesIO(bundle.read(journal.key)[0])) as image:
        assert image.format == "WEBP"


def test_fresh_install_can_serve_cover_without_network_or_browser(shipped, monkeypatch):
    journal, _, _ = shipped
    monkeypatch.setattr(covers, "identify", lambda item: journal)
    monkeypatch.setattr(covers, "_schedule", lambda item: pytest.fail("fresh install tried network"))
    asset, pending = covers.get_or_schedule(object())
    assert asset and asset[1] == "image/webp" and not pending
    assert list(covers.cache_dir().iterdir()) == []
    assert not covers.needs_refresh(journal)
    assert not browser.due(journal)
    assert inventory.write_report([journal], running=False)["available"] == 1


def test_newer_runtime_cover_takes_priority_over_shipped_cover(shipped, monkeypatch):
    journal, body, _ = shipped
    monkeypatch.setattr(covers, "identify", lambda item: journal)
    covers.save_cover(journal, (body, {"sha256": hashlib.sha256(body).hexdigest(),
                                     "content_type": "image/png"}), "https://publisher.test", method="test")
    assert covers.get_or_schedule(object()) == ((body, "image/png"), False)


def test_corrupt_or_unsafe_bundle_asset_is_not_served(shipped):
    journal, _, _ = shipped
    entry = bundle.metadata(journal.key)
    (bundle.bundle_dir() / entry["file"]).write_bytes(b"corrupt")
    assert bundle.read(journal.key) is None
    path = bundle.bundle_dir() / "manifest.json"
    value = json.loads(path.read_text())
    value["journals"][journal.key]["file"] = "../../private-file"
    covers._write(path, value)
    assert not bundle.metadata(journal.key)


def test_atomic_manifest_replacement_with_the_same_mtime_is_reloaded(shipped):
    journal, _, _ = shipped
    path = bundle.bundle_dir() / "manifest.json"
    assert bundle.metadata(journal.key)
    before = path.stat()
    value = json.loads(path.read_text())
    filename = value["journals"][journal.key]["file"]
    value["journals"][journal.key]["file"] = "../" + "x" * (len(filename) - 3)
    covers._write(path, value)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert not bundle.metadata(journal.key)
