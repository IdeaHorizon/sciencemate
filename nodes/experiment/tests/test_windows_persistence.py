"""Native I/O regressions for the explicit POSIX/Windows persistence contract.

These tests use temporary files at the I/O boundary. The existing delivery
transaction tests exercise unmodified Core State saves, freezes, and replay.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodes.experiment.tools import capture, delivery_transaction as tx, persistence


@pytest.fixture
def io_state(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return SimpleNamespace(root=root)


@pytest.mark.skipif(os.name != "nt", reason="Windows CRT directory-open regression")
def test_windows_directory_validation_avoids_unsupported_crt_open(tmp_path, monkeypatch):
    with pytest.raises(PermissionError):
        os.open(tmp_path, os.O_RDONLY)

    def directory_open_must_not_run(*args, **kwargs):
        raise AssertionError("Windows directory persistence must not use CRT open")

    monkeypatch.setattr(persistence.os, "open", directory_open_must_not_run)
    persistence.fsync_directory(tmp_path)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_directory_validation_rejects_invalid_target(tmp_path, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_bytes(b"ordinary file")
    with pytest.raises(OSError):
        persistence.fsync_directory(path)


def test_windows_directory_validation_does_not_ignore_access_error(tmp_path, monkeypatch):
    real_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == tmp_path:
            raise PermissionError(errno.EACCES, "directory metadata denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(persistence, "_WINDOWS", True)
    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError, match="directory metadata denied"):
        persistence.fsync_directory(tmp_path)


def test_posix_directory_fsync_error_does_not_switch_to_windows_contract(tmp_path, monkeypatch):
    closed = []
    opened = []

    def open_directory(path, flags):
        opened.append((path, flags))
        return 707

    def fail_sync(fd):
        assert fd == 707
        raise OSError(errno.EIO, "directory sync failed")

    real_open = persistence.os.open
    # os is shared with tempfile cleanup; restore it before fixture teardown.
    with monkeypatch.context() as patch:
        patch.setattr(persistence, "_WINDOWS", False)
        patch.setattr(persistence.os, "open", open_directory)
        patch.setattr(persistence.os, "fsync", fail_sync)
        patch.setattr(persistence.os, "close", closed.append)
        with pytest.raises(OSError, match="directory sync failed") as caught:
            persistence.fsync_directory(tmp_path)
    assert caught.value.errno == errno.EIO
    assert persistence.os.open is real_open
    assert opened == [(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))]
    assert closed == [707]


def test_existing_file_reflush_is_repeatable_without_truncation(tmp_path):
    path = tmp_path / "events.jsonl"
    payload = b'{"seq":1,"text":"native flush"}\n'
    path.write_bytes(payload)
    for _ in range(2):
        persistence.fsync_file(path)
        assert path.read_bytes() == payload


def test_file_sync_failure_propagates_without_changing_bytes(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"preserve these bytes\n")

    def fail_sync(fd):
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        raise OSError(errno.EIO, "file sync failed")

    monkeypatch.setattr(persistence.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="file sync failed"):
        persistence.fsync_file(path)
    assert path.read_bytes() == b"preserve these bytes\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows writable-handle requirement")
def test_readonly_existing_file_is_a_real_error_not_success(tmp_path):
    path = tmp_path / "readonly.jsonl"
    path.write_bytes(b"immutable bytes\n")
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PermissionError):
            persistence.fsync_file(path)
        assert path.read_bytes() == b"immutable bytes\n"
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_large_capture_first_write_and_reuse_keep_digest_bound_bytes(io_state):
    observation = {"created": [f"output-{index}" for index in range(250)],
                   "modified": [], "deleted": [], "unknown": [],
                   "identities": {}, "identity_basis": {}, "overlapping_actions": {}}
    first = capture.persist_observation(io_state, "large-output", observation)
    replay = capture.persist_observation(io_state, "large-output", observation)
    assert first == replay
    assert first["summary_truncated"] is True
    directory = capture.allocate_record_dir(io_state, "large-output")
    reference = first["observation_ref"]
    payload = (directory / reference["file"]).read_bytes()
    assert len(list(directory.glob("observation-*.json"))) == 1
    assert hashlib.sha256(payload).hexdigest() == reference["sha256"]
    assert len(payload) == reference["size_bytes"]
    assert capture.read_observation(directory, reference, "large-output") == observation
    (directory / reference["file"]).write_bytes(b"damaged")
    with pytest.raises(ValueError):
        capture.persist_observation(io_state, "large-output", observation)


def test_manifest_file_sync_failure_cannot_replace_previous_claim(io_state, monkeypatch):
    tx._atomic_write_manifest(io_state, {"closure_id": "previous"})
    original = tx.manifest_path(io_state).read_bytes()
    real_sync = tx.os.fsync

    def fail_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ENOSPC, "injected disk full on flush")
        return real_sync(fd)

    monkeypatch.setattr(tx.os, "fsync", fail_file_sync)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx._atomic_write_manifest(io_state, {"closure_id": "new"})
    result = caught.value.as_result()
    assert result["checkpoint"] == "tmp_fsync"
    assert result["replace_completed"] is False
    assert result["retryable"] is False
    assert tx.manifest_path(io_state).read_bytes() == original


def test_native_replace_error_preserves_existing_files(io_state):
    tx._atomic_write_manifest(io_state, {"closure_id": "previous"})
    original = tx.manifest_path(io_state).read_bytes()
    blocked = tx.manifest_path(io_state).parent / "nonempty-directory"
    blocked.mkdir()
    sentinel = blocked / "sentinel"
    sentinel.write_bytes(b"must survive")
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx._atomic_write_manifest(io_state, {"closure_id": "new"}, path=blocked)
    assert caught.value.facts["checkpoint"] == "replace"
    assert caught.value.facts["replace_completed"] is False
    assert caught.value.facts["retryable"] is False
    assert sentinel.read_bytes() == b"must survive"
    assert tx.manifest_path(io_state).read_bytes() == original


def test_manifest_readback_rejects_changed_replacement(io_state, monkeypatch):
    real_replace = tx.os.replace

    def replace_then_corrupt(source, target):
        real_replace(source, target)
        Path(target).write_text(json.dumps({"closure_id": "changed"}), encoding="utf-8")

    monkeypatch.setattr(tx.os, "replace", replace_then_corrupt)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx._atomic_write_manifest(io_state, {"closure_id": "planned"})
    result = caught.value.as_result()
    assert result["error_code"] == "closure_manifest_readback_mismatch"
    assert result["checkpoint"] == "manifest_readback"
    assert result["replace_completed"] is True
    assert result["canonical_visible"] is True
    assert result["retryable"] is False


@pytest.mark.parametrize("number,retryable", [
    (errno.EACCES, False), (errno.ENOSPC, False), (errno.EIO, False),
    (errno.EINTR, True), (errno.EAGAIN, True), (errno.EBUSY, True),
])
def test_only_transient_io_errors_are_immediately_retryable(number, retryable):
    assert persistence.retryable_io_error(OSError(number, "failure")) is retryable
