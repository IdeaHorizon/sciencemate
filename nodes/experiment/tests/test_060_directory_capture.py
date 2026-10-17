"""Real filesystem coverage for opt-in directory observation, not a second ledger.

The small adapter exercises the old implicit-root behavior on the bad baseline,
so missing directory evidence fails an assertion rather than only a new keyword.
No process, model, or producer result is mocked.
"""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import os
from pathlib import Path
import signal
import socket

import pytest

from nodes.experiment.tools import capture


def _snapshot(candidates=(), roots=(), **kwargs):
    if "include_directories" in inspect.signature(capture.snapshot).parameters:
        kwargs["include_directories"] = True
    return capture.snapshot(candidates, roots, **kwargs)


@contextmanager
def _bounded_identity_read():
    def expired(_number, _frame):
        raise AssertionError("capture attempted a blocking read of a non-regular object")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("with_file", [False, True])
def test_new_directory_is_observed_even_when_empty(tmp_path, with_file):
    root = tmp_path / "workspace"
    root.mkdir()
    before = _snapshot(roots=[root])
    output = root / "result"
    output.mkdir()
    if with_file:
        (output / "part.txt").write_text("result\n")
    after = _snapshot(roots=[root])
    observed = capture.diff(before, after)
    assert after["entries"].get(str(output), {}).get("kind") == "dir"
    assert str(output) in observed["created"]
    assert after["entries"][str(root)]["kind"] == "dir"
    assert not after["truncated"]
    if with_file:
        assert str(output / "part.txt") in observed["created"]


def test_root_created_after_missing_baseline_has_directory_identity(tmp_path):
    root = tmp_path / "new-output-root"
    before = _snapshot(roots=[root])
    assert before["root_coverage"][0]["status"] == "missing"
    root.mkdir()
    after = _snapshot(roots=[root])
    assert after["entries"].get(str(root), {}).get("kind") == "dir"
    assert str(root) in capture.diff(before, after)["created"]


def test_unchanged_old_directory_is_not_a_new_producer(tmp_path):
    root = tmp_path / "workspace"
    old = root / "old"
    old.mkdir(parents=True)
    before = _snapshot(roots=[root])
    after = _snapshot(roots=[root])
    assert before["entries"].get(str(old), {}).get("kind") == "dir"
    assert capture.same_identity(before["entries"][str(old)], after["entries"][str(old)])[0]
    observation = capture.diff(before, after)
    assert str(old) not in observation["created"] + observation["modified"]


def test_file_only_default_retains_the_existing_snapshot_shape(tmp_path):
    root = tmp_path / "workspace"
    output = root / "nested" / "output.txt"
    output.parent.mkdir(parents=True)
    output.write_text("ok")
    actual = capture.snapshot([], [root])
    assert set(actual["entries"]) == {str(output)}
    assert actual["scanned"] == 1


def test_directory_capture_excludes_private_and_git_subtrees(tmp_path):
    root = tmp_path / "workspace"
    business = root / "business"
    private = root / ".python-scratch"
    git = root / ".git"
    for directory in (business, private / "secret", git / "objects"):
        directory.mkdir(parents=True)
        (directory / "data.txt").write_text("data")
    actual = _snapshot(roots=[root], excluded_roots=[private])
    assert actual["entries"].get(str(business), {}).get("kind") == "dir"
    assert not any(Path(path).is_relative_to(private) or Path(path).is_relative_to(git)
                   for path in actual["entries"])
    excluded = {row["root"] for row in actual["root_coverage"] if row["status"] == "excluded"}
    assert {str(private), str(git)} <= excluded


def test_directories_and_files_share_one_budget(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    candidate = tmp_path / "explicit.txt"
    candidate.write_text("explicit")
    for index in range(10):
        (root / f"dir-{index}").mkdir()
        (root / f"file-{index}.txt").write_text("data")
    actual = _snapshot([candidate], [root], max_files=3)
    assert str(root) in actual["entries"], "the root itself consumes the common budget"
    assert actual["scanned"] == len(actual["entries"]) == 3
    assert actual["truncated"] is True
    assert actual["root_coverage"][0]["status"] == "truncated"


def test_unreadable_directory_reports_incomplete_coverage(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can bypass a real directory read denial")
    root = tmp_path / "denied"
    root.mkdir()
    (root / "hidden.txt").write_text("private")
    root.chmod(0)
    try:
        actual = _snapshot(roots=[root])
    finally:
        root.chmod(0o700)
    assert actual["entries"].get(str(root), {}).get("kind") == "dir"
    assert actual["truncated"] is True
    assert actual["root_coverage"][0]["status"] == "error"
    assert str(root / "hidden.txt") not in actual["entries"]


@pytest.mark.parametrize("as_root", [False, True])
def test_directory_symlinks_do_not_read_external_contents(tmp_path, monkeypatch, as_root):
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "not-an-output.txt").write_text("external bytes")
    link = root / "linked-dir"
    link.symlink_to(external, target_is_directory=True)
    hashed = []
    original = capture._sha256

    def observe_hash(path):
        assert not path.resolve().is_relative_to(external), "capture followed a directory symlink"
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(capture, "_sha256", observe_hash)
    actual = _snapshot(roots=[link if as_root else root])
    assert not hashed
    assert not any(path.endswith("not-an-output.txt") for path in actual["entries"])
    if as_root:
        assert actual["entries"][str(link)]["kind"] == "symlink"


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_special_files_are_not_hashed_or_misreported_as_regular(tmp_path, kind):
    path = tmp_path / "special"
    server = None
    if kind == "fifo":
        os.mkfifo(path)
    else:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
    try:
        with _bounded_identity_read():
            row = capture.identity(path)
        assert row["kind"] == "other", row
        assert "sha256" not in row and "hash_error" not in row
        assert row["inode"] == path.lstat().st_ino
        with _bounded_identity_read():
            actual = _snapshot(roots=[tmp_path])
        assert actual["entries"][str(path)]["kind"] == "other"
    finally:
        if server is not None:
            server.close()


def test_symlink_file_identity_does_not_hash_its_target(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("not requested")
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(capture, "_sha256", lambda path: pytest.fail("symlink target was opened"))
    assert capture.identity(link) == {"kind": "symlink", "target": str(target)}


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_hash_open_does_not_block_or_follow_a_substituted_object(tmp_path, monkeypatch, replacement):
    path = tmp_path / "candidate"
    path.write_text("regular at lstat")
    target = tmp_path / "other"
    target.write_text("must not be hashed through a link")
    original = capture._sha256

    def change_before_open(candidate):
        candidate.unlink()
        if replacement == "fifo":
            os.mkfifo(candidate)
        else:
            candidate.symlink_to(target)
        return original(candidate)

    monkeypatch.setattr(capture, "_sha256", change_before_open)
    with _bounded_identity_read():
        observed = capture.identity(path)
    assert "hash_error" in observed and "sha256" not in observed
