"""Fixed remote helper against a real POSIX filesystem, without SSH or Slurm."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="remote helper runs on the Linux submit host")
HELPER = Path(__file__).resolve().parents[1] / "tools" / "slurm_files_remote.py"


def run(action, data, content=b""):
    return subprocess.run([sys.executable, str(HELPER), action, json.dumps(data)], input=content,
                          capture_output=True, timeout=10)


def test_binary_upload_fetch_and_frozen_identity(tmp_path):
    boot = tmp_path / ".bootstrap"
    assert run("prepare", {"bootstrap": str(boot)}).returncode == 0
    target = boot / "inputs" / "0"
    content = bytes(range(256)) * 100
    assert run("upload", {"path": str(target), "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}, content).returncode == 0
    ident = json.loads(run("identity", {"path": str(target)}).stdout)
    fetched = run("fetch", {"path": str(target), "identity": ident})
    assert fetched.returncode == 0 and fetched.stdout == content
    target.write_bytes(b"changed")
    assert run("fetch", {"path": str(target), "identity": ident}).returncode != 0


def test_upload_never_follows_final_or_parent_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"untouched")
    parent = tmp_path / "link"
    parent.symlink_to(outside, target_is_directory=True)
    final = tmp_path / "final"
    final.symlink_to(sentinel)
    for path in (parent / "sentinel", final):
        result = run("upload", {"path": str(path), "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}, b"x")
        assert result.returncode != 0
    assert sentinel.read_bytes() == b"untouched"


def test_fetch_refuses_symlinks_even_with_target_metadata(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    ident = json.loads(run("identity", {"path": str(target)}).stdout)
    link = tmp_path / "link"
    link.symlink_to(target)
    result = run("fetch", {"path": str(link), "identity": ident})
    assert result.returncode != 0 and not result.stdout


def test_bad_upload_cleans_only_its_new_file(tmp_path):
    target = tmp_path / "new"
    assert run("upload", {"path": str(target), "size": 3, "sha256": "0" * 64}, b"abc").returncode != 0
    assert not target.exists()
    target.write_bytes(b"old")
    assert run("upload", {"path": str(target), "size": 3, "sha256": "0" * 64}, b"abc").returncode != 0
    assert target.read_bytes() == b"old"


def test_snapshot_preserves_case_and_excludes_symlink_descendants(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    (root / "Result").write_bytes(b"upper")
    (root / "result").write_bytes(b"lower")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"secret")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    snap = json.loads(run("snapshot", {"roots": [str(root)]}).stdout)
    assert str(root / "Result") in snap["entries"] and str(root / "result") in snap["entries"]
    assert str(root / "linked" / "private") not in snap["entries"]
    assert snap["entries"][str(root / "linked")]["kind"] == "symlink"


def test_large_file_hash_limit_is_explicit(tmp_path):
    path = tmp_path / "large"
    path.write_bytes(b"0123456789")
    ident = json.loads(run("identity", {"path": str(path), "hash_cap": 2}).stdout)
    assert ident["sha256_skipped"] == "size_over_cap" and "sha256" not in ident


def test_tail_is_bounded_and_missing_is_distinct_from_error(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"abcdef")
    assert json.loads(run("tail", {"path": str(path), "limit": 3}).stdout)["text"] == "def"
    missing = json.loads(run("identity", {"path": str(tmp_path / "missing")}).stdout)
    assert missing["kind"] == "missing"
    assert run("identity", {"path": "C:\\not-remote"}).returncode != 0


def test_identity_is_checked_in_same_helper_before_remote_writes(tmp_path, monkeypatch):
    import pwd
    binary = tmp_path / "bin"
    binary.mkdir()
    command = binary / "scontrol"
    command.write_text("#!/bin/sh\nprintf 'ClusterName = actual-cluster\\n'\n")
    command.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    actual_user = pwd.getpwuid(os.geteuid()).pw_name
    target = tmp_path / "new-bootstrap"
    for expected in ({"user": "other-user", "cluster": "actual-cluster"},
                     {"user": actual_user, "cluster": "other-cluster"}):
        result = run("prepare", {"bootstrap": str(target), "expected_identity": expected})
        assert result.returncode != 0 and not target.exists()
    assert run("prepare", {"bootstrap": str(target), "expected_identity": {
        "user": actual_user, "cluster": "actual-cluster"}}).returncode == 0
