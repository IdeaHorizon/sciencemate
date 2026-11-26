"""Capture/restore must carry old Windows policy before measuring an upgrade.

The OS security adapter is replaced here; native DACL behavior is exercised by
the Windows roundtrip. No backend, model, or real user fixture is started.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "contract_upgrade_windows_dacl", REPO / "scripts/acceptance/upgrade_rehearsal.py"
)
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)
SID = "S-1-5-21-111-222-333-1001"
LITERATURE = "harness-state/literature-access"


def entry(path: Path, *, protected=True):
    flags = "OICI" if path.is_dir() else ""
    sddl = f"D:{'PAI' if protected else 'AI'}(A;{flags};FA;;;SY)(A;{flags};FA;;;{SID})"
    fingerprint = hashlib.sha256(sddl.encode()).hexdigest()
    return {
        "sddl": sddl,
        "protected": protected,
        "dacl_present": True,
        "null_dacl": False,
        "dacl_sha256": fingerprint,
        "explicit_ace_sha256": [fingerprint],
        "kind": "directory" if path.is_dir() else "file",
    }


class Security:
    def __init__(self, source: Path):
        self.source = source.resolve()
        self.reads = []
        self.applied = []
        self.events = []
        self.validated = []

    def current_sid(self):
        return SID

    def read(self, path):
        path = Path(path)
        assert path.resolve().is_relative_to(self.source), "capture read the copied policy"
        self.reads.append(path)
        relative = path.relative_to(self.source).as_posix()
        return entry(
            path, protected=relative == LITERATURE or relative.startswith(LITERATURE + "/")
        )

    def validate(self, value):
        self.validated.append(copy.deepcopy(value))
        if value.get("sddl") == "CONTROLLED INVALID DESCRIPTOR":
            raise ValueError("controlled invalid security descriptor")

    def apply(self, path, value):
        path = Path(path)
        assert path.exists(), "restore applied policy before copying the object"
        self.applied.append((path, copy.deepcopy(value)))
        self.events.append("apply")


@pytest.fixture
def data(tmp_path):
    source = tmp_path / "previous-data"
    (source / LITERATURE).mkdir(parents=True)
    (source / LITERATURE / "grant.json").write_text('{"controlled_fixture_only": true}\n')
    (source / "logs").mkdir()
    (source / "logs/permissions-snapshot.json").write_text('{"controlled_fixture_only": true}')
    (source / "logs/backend.log").write_text("not carried")
    (source / "credential.key").write_text("CONTROLLED secret outside carried inputs")
    (source / "project-worktrees/w").mkdir(parents=True)
    with (source / "project-worktrees/w/large.bin").open("wb") as handle:
        handle.truncate(rehearsal.FILE_CAP + 1)
    with sqlite3.connect(source / "db.sqlite") as connection:
        connection.executescript("""
            create table projects(id text primary key, name text);
            create table sessions(session_id text primary key, project_id text, title text);
            create table runs(id text primary key, session_id text, project_id text, status text);
            create table model_backend_configs(
                id text primary key, encrypted_api_key text, context_window_tokens integer);
            insert into projects values ('p1', 'controlled fixture');
            insert into sessions values ('s1', 'p1', 'controlled old session');
            insert into runs values ('r1', 's1', 'p1', 'completed');
            insert into model_backend_configs values ('b1', 'CONTROLLED-key-to-scrub', 128000);
        """)
    return source


@pytest.fixture
def windows(data, monkeypatch):
    security = Security(data)
    monkeypatch.setattr(rehearsal, "_windows_mode", lambda: True)
    monkeypatch.setattr(rehearsal, "_windows_security", lambda: security)
    return security


def capture(data, tmp_path):
    fixture = tmp_path / "captured-fixture"
    manifest = rehearsal.capture(data, fixture, version="0.5.16")
    return fixture, manifest


def write_manifest(fixture, manifest):
    (fixture / rehearsal.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def assert_refused_before_copy(fixture, target, security):
    with pytest.raises((SystemExit, ValueError, OSError)):
        rehearsal.restore(fixture, target)
    assert not target.exists(), "unsafe fixture was copied before validation"
    assert not security.applied, "unsafe fixture changed a destination DACL"


def test_capture_uses_original_protected_policy_and_keeps_scrubbing_and_copy_limits(
    data, tmp_path, windows
):
    original = (data / LITERATURE / "grant.json").read_bytes()
    fixture, manifest = capture(data, tmp_path)
    policy = manifest["windows_dacl"]
    assert policy["schema_version"] == 1 and policy["user_sid"] == SID
    assert policy["entries"][LITERATURE] == entry(data / LITERATURE)
    assert policy["entries"][LITERATURE]["protected"] is True
    paths = {"."} | {
        path.relative_to(fixture).as_posix()
        for path in fixture.rglob("*")
        if path.name != rehearsal.MANIFEST
    }
    assert set(policy["entries"]) == paths
    assert windows.reads and all(path.is_relative_to(data) for path in windows.reads)
    assert not (fixture / "credential.key").exists()
    assert not (fixture / "logs/backend.log").exists()
    assert not (fixture / "project-worktrees/w/large.bin").exists()
    assert "project-worktrees/w/large.bin" in manifest["skipped_large"]
    with sqlite3.connect(fixture / "db.sqlite") as connection:
        assert connection.execute(
            "select encrypted_api_key, context_window_tokens from model_backend_configs"
        ).fetchone() == (None, 128000)
    with sqlite3.connect(data / "db.sqlite") as connection:
        assert (
            connection.execute("select encrypted_api_key from model_backend_configs").fetchone()[0]
            == "CONTROLLED-key-to-scrub"
        )
    assert (data / LITERATURE / "grant.json").read_bytes() == original


@pytest.mark.parametrize("archived", [False, True], ids=["directory", "tgz"])
def test_restore_replays_original_policy_parent_first_between_relocation_and_baseline(
    data, tmp_path, windows, monkeypatch, archived
):
    fixture, manifest = capture(data, tmp_path)
    input_path = fixture
    if archived:
        input_path = tmp_path / "controlled-fixture.tgz"
        with tarfile.open(input_path, "w:gz") as archive:
            archive.add(fixture, arcname="fixture")
    target = tmp_path / "new-data"

    def relocate(command, **_kwargs):
        assert command[0] == "controlled-installed-python"
        assert "--copied" in command and "--apply" in command
        assert not windows.applied
        windows.events.append("relocate")
        return SimpleNamespace(returncode=0, stdout="controlled relocation", stderr="")

    def baseline(to, previous, tree):
        assert to == target and previous == "0.5.16"
        assert len(windows.applied) == len(manifest["windows_dacl"]["entries"])
        windows.events.append("baseline")
        return {"entries": {".": {}}}

    monkeypatch.setattr(subprocess, "run", relocate)
    monkeypatch.setattr(rehearsal, "_write_baseline", baseline)
    rehearsal.restore(
        input_path, target, python="controlled-installed-python", tree=tmp_path / "tree"
    )
    expected = manifest["windows_dacl"]["entries"]
    actual = {path.relative_to(target).as_posix(): value for path, value in windows.applied}
    assert actual == expected and actual[LITERATURE]["protected"] is True
    order = {
        path.relative_to(target).as_posix(): index
        for index, (path, _) in enumerate(windows.applied)
    }
    assert order["."] == 0
    for relative in expected:
        parent = Path(relative).parent.as_posix()
        if relative != "." and parent in order:
            assert order[parent] < order[relative]
    assert windows.events[0] == "relocate" and windows.events[-1] == "baseline"
    assert (target / LITERATURE / "grant.json").read_bytes() == (
        data / LITERATURE / "grant.json"
    ).read_bytes()


def test_restore_without_python_still_restores_policy(data, tmp_path, windows):
    fixture, manifest = capture(data, tmp_path)
    target = tmp_path / "restored-without-relocation"
    rehearsal.restore(fixture, target)
    assert len(windows.applied) == len(manifest["windows_dacl"]["entries"])
    assert (
        next(value for path, value in windows.applied if path == target / LITERATURE)["protected"]
        is True
    )


@pytest.mark.parametrize(
    "corruption", ["legacy", "foreign-sid", "missing", "extra", "case-alias", "invalid-entry"]
)
def test_restore_rejects_incomplete_or_foreign_policy_before_creating_target(
    data, tmp_path, windows, corruption
):
    fixture, manifest = capture(data, tmp_path)
    policy = manifest["windows_dacl"]
    if corruption == "legacy":
        manifest.pop("windows_dacl")
    elif corruption == "foreign-sid":
        policy["user_sid"] = "S-1-5-21-111-222-333-1002"
    elif corruption == "missing":
        policy["entries"].pop(LITERATURE)
    elif corruption == "extra":
        policy["entries"]["harness-state/not-carried"] = entry(data / "db.sqlite")
    elif corruption == "case-alias":
        policy["entries"]["HARNESS-STATE"] = copy.deepcopy(policy["entries"]["harness-state"])
    else:
        policy["entries"][LITERATURE]["sddl"] = "CONTROLLED INVALID DESCRIPTOR"
    write_manifest(fixture, manifest)
    assert_refused_before_copy(fixture, tmp_path / "refused-target", windows)


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "harness-state:stream",
        "harness-state\\..\\escape",
        "harness-state//literature-access",
        "logs/.",
    ],
)
def test_manifest_paths_cannot_escape_or_alias_the_fixture(data, tmp_path, windows, unsafe):
    fixture, manifest = capture(data, tmp_path)
    manifest["windows_dacl"]["entries"][unsafe] = entry(data / "db.sqlite")
    write_manifest(fixture, manifest)
    assert_refused_before_copy(fixture, tmp_path / "refused-target", windows)


def test_fixture_symlink_cannot_redirect_policy_to_an_external_directory(data, tmp_path, windows):
    fixture, manifest = capture(data, tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "untouched.txt"
    sentinel.write_text("untouched")
    linked = fixture / "harness-state/linked"
    linked.symlink_to(external, target_is_directory=True)
    manifest["windows_dacl"]["entries"]["harness-state/linked"] = entry(external)
    write_manifest(fixture, manifest)
    assert_refused_before_copy(fixture, tmp_path / "refused-target", windows)
    assert sentinel.read_text() == "untouched"


def test_fixture_junction_is_rejected_before_policy_application(
    data, tmp_path, windows, monkeypatch
):
    fixture, manifest = capture(data, tmp_path)
    linked = fixture / "harness-state/junction"
    linked.mkdir()
    manifest["windows_dacl"]["entries"]["harness-state/junction"] = entry(linked)
    write_manifest(fixture, manifest)
    is_link = rehearsal._is_link
    monkeypatch.setattr(rehearsal, "_is_link", lambda path: path == linked or is_link(path))
    assert_refused_before_copy(fixture, tmp_path / "refused-target", windows)


def test_destination_symlink_cannot_redirect_copy_or_acl_replay(data, tmp_path, windows):
    fixture, _manifest = capture(data, tmp_path)
    external = tmp_path / "external-empty-directory"
    external.mkdir()
    target = tmp_path / "restore-target-link"
    target.symlink_to(external, target_is_directory=True)
    with pytest.raises((SystemExit, ValueError, OSError)):
        rehearsal.restore(fixture, target)
    assert target.is_symlink() and not list(external.iterdir())
    assert not windows.applied


def test_destination_parent_link_is_rejected_before_any_external_copy(data, tmp_path, windows):
    fixture, _manifest = capture(data, tmp_path)
    external = tmp_path / "external-parent"
    external.mkdir()
    parent = tmp_path / "restore-parent-link"
    parent.symlink_to(external, target_is_directory=True)
    target = parent / "new-data"
    with pytest.raises((SystemExit, ValueError, OSError)):
        rehearsal.restore(fixture, target)
    assert not list(external.iterdir()), "restore copied outside before rejecting the parent link"
    assert not windows.applied


def test_old_posix_fixture_remains_usable_without_windows_policy(data, tmp_path, monkeypatch):
    monkeypatch.setattr(rehearsal, "_windows_mode", lambda: False)
    monkeypatch.setattr(
        rehearsal,
        "_windows_security",
        lambda: pytest.fail("POSIX must not invoke Windows security"),
    )
    (data / LITERATURE).chmod(0o700)
    fixture, manifest = capture(data, tmp_path)
    assert "windows_dacl" not in manifest
    target = tmp_path / "posix-restored"
    rehearsal.restore(fixture, target)
    assert stat.S_IMODE((target / LITERATURE).stat().st_mode) == 0o700
    assert (target / LITERATURE / "grant.json").read_bytes() == (
        data / LITERATURE / "grant.json"
    ).read_bytes()
