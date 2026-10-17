"""The shared producer projection must not promote unknown content to a write."""
from __future__ import annotations

import os

from nodes.experiment.tools import action_boundary, capture, finish


def _record(path, before, after):
    changes = capture.diff(before, after)
    return [{"kind": "observation", "action_id": "measured-action", **changes,
             "identities": action_boundary._identity_rows(before, after, changes)}]


def test_large_file_timestamp_change_is_not_a_new_content_producer(tmp_path):
    path = tmp_path / "field.dat"
    with path.open("wb") as handle:
        handle.truncate(capture.SMALL_FILE_BYTES + 1)
    before = capture.snapshot([path])
    previous = path.stat()
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    records = _record(path, before, capture.snapshot([path]))
    assert str(path) in records[0]["modified"]
    assert finish._producer_rows(records, path) == []


def test_created_file_with_failed_hash_is_not_content_evidence(tmp_path, monkeypatch):
    path = tmp_path / "field.dat"
    before = capture.snapshot([path])
    path.write_text("new result")

    def unreadable(_path):
        raise PermissionError("injected content read failure")

    monkeypatch.setattr(capture, "_sha256", unreadable)
    after = capture.snapshot([path])
    assert after["entries"][str(path)]["hash_error"]
    assert finish._producer_rows(_record(path, before, after), path) == []


def test_new_large_file_does_not_require_full_content_hash(tmp_path):
    path = tmp_path / "field.dat"
    before = capture.snapshot([path])
    with path.open("wb") as handle:
        handle.truncate(capture.SMALL_FILE_BYTES + 1)
    after = capture.snapshot([path])
    assert "sha256" not in after["entries"][str(path)]
    rows = finish._producer_rows(_record(path, before, after), path)
    assert len(rows) == 1 and rows[0]["action_id"] == "measured-action"
