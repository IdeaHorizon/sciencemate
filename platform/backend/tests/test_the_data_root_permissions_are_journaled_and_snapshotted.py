"""数据根权限：启动记快照、版本变了就比、差异进条幅、worker 被告知账本在哪
（RFC_WORKER_START_IS_NOT_A_GATE §3.9，窄单 G）。

判据（RFC 13）：手工改一个目录的权限、换版本号再启动 → 差异事件里有它、账本里没有 → 事件说
「不是本应用改的」；账本里有的 → 列进 changed_by_us。同版本不比；第一次只记。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app.services import permissions_snapshot as snap


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "afs"
    for name in snap.WATCHED:
        (root / name).mkdir(parents=True)
    (root / "harness-state" / "users").mkdir()
    (root / "project-worktrees" / "abc").mkdir()
    return root


def test_the_first_start_only_records(tmp_path):
    root = _root(tmp_path)
    assert snap.record_startup(root, app_version="0.5.6") is None
    saved = json.loads((root / "logs" / snap.SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert saved["app_version"] == "0.5.6"
    assert set(saved["entries"]) >= {".", "harness-state", "harness-state/users", "project-worktrees/abc", "payload"}
    assert snap.latest_change() is None


@pytest.fixture(autouse=True)
def _forget_the_latest_change():
    yield
    snap._latest_change = None   # 进程内一份，别漏给下一条测试


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode 才是这里的权限事实；Windows 走 icacls 哈希")
def test_a_changed_mode_across_versions_is_reported_and_attributed(tmp_path):
    root = _root(tmp_path)
    snap.record_startup(root, app_version="0.5.6")
    # 升级之间发生了两件事：一处是别人改的（没有账），一处是我们改的（账上有）。
    (root / "harness-state" / "users").chmod(0o500)
    (root / "project-worktrees" / "abc").chmod(0o500)
    journal = root / "logs" / snap.JOURNAL_NAME
    journal.write_text(json.dumps({
        "at": "2999-01-01T00:00:00+00:00", "op": "setintegritylevel",
        "path": str(root / "project-worktrees" / "abc"), "level": "L", "returncode": 0,
    }) + "\n", encoding="utf-8")
    try:
        change = snap.record_startup(root, app_version="0.5.7")
        assert change is not None and change["code"] == "data_root_permissions_changed"
        changed = {d["path"] for d in change["diffs"]}
        assert {"harness-state/users", "project-worktrees/abc"} <= changed
        assert change["previous_version"] == "0.5.6"
        assert change["changed_by_us"] == ["project-worktrees/abc"], "账本里有的才算我们改的"
        assert change["our_changes_in_between"][0]["level"] == "L"
        assert snap.latest_change() is change
        # 同版本再启动：权限又变了也不比 —— 那是运行中的事，不是升级的事。
        (root / "payload").chmod(0o500)
        assert snap.record_startup(root, app_version="0.5.7") is None
        assert snap.latest_change() is None
    finally:
        for name in ("harness-state/users", "project-worktrees/abc", "payload"):
            (root / name).chmod(0o700)


def test_compare_only_looks_at_paths_both_snapshots_know(tmp_path):
    before = {"entries": {"a": {"mode": "0o700", "owner": "1"}, "gone": {"mode": "0o700", "owner": "1"}}}
    after = {"entries": {"a": {"mode": "0o500", "owner": "1"}, "new": {"mode": "0o700", "owner": "1"}}}
    assert snap.compare(before, after) == [{"path": "a", "field": "mode", "before": "0o700", "after": "0o500"}]


def test_describe_entry_names_a_missing_or_unreadable_path(tmp_path):
    assert snap.describe_entry(tmp_path / "nope")["error"] == "missing"
    entry = snap.describe_entry(tmp_path)
    assert entry["error"] is None and (entry["mode"] or entry["acl_sha256"])


def test_the_worker_is_told_where_the_journal_is(monkeypatch, tmp_path):
    from app.config import the_data_root
    from app.services import harness_sessions as hs
    from tests.test_a_self_hosted_endpoint_dispatches_without_a_key import REASONING_ROLE, _backend

    monkeypatch.setattr(hs, "harness_subprocess_env", lambda root, passthrough=(): {})
    env = hs._child_environment(Path(tmp_path), {REASONING_ROLE: _backend()})
    assert env["HARNESS_PERMISSIONS_JOURNAL"] == str(the_data_root() / "logs" / "permissions.jsonl")


@pytest.mark.asyncio
async def test_a_machine_level_change_shows_up_in_every_session_banner(monkeypatch):
    from app.services import session_environment as env

    change = {"type": "environment_warning", "code": "data_root_permissions_changed", "severity": "degraded",
              "path": "/data/afs", "error": None, "consequences": ["3 处权限变了"]}
    monkeypatch.setattr(snap, "_latest_change", change)
    monkeypatch.setattr(env, "live_report", lambda p, s: {"capabilities": {}, "warnings": []})

    class _DB:
        async def execute(self, *_a, **_k):  # pragma: no cover - live_report 命中就不查库
            raise AssertionError("not needed")

    got = await env.current_environment(_DB(), project_id="p", session_id="s")
    assert got["warnings"][0]["code"] == "data_root_permissions_changed"
    monkeypatch.setattr(snap, "_latest_change", None)
    got = await env.current_environment(_DB(), project_id="p", session_id="s")
    assert got["warnings"] == []


ICACLS = (
    "{path} DESKTOP-X\\\\CodexSandboxUsers:(I)(M,DC)\n"
    "{pad}NT AUTHORITY\\\\SYSTEM:(I)(F)\n"
    "{pad}DESKTOP-X\\\\fcbay:(I)(F)\n"
    "\n"
    "{summary}"
)


def _icacls(path: str, summary: str = "Successfully processed 1 files; Failed processing 0 files") -> str:
    return ICACLS.format(path=path, pad=" " * len(path), summary=summary)


def test_the_acl_fingerprint_is_the_aces_not_the_path_or_the_summary():
    here = snap.acl_fingerprint(_icacls(r"C:\Users\x\afs\harness-state"), r"C:\Users\x\afs\harness-state")
    moved = snap.acl_fingerprint(_icacls(r"D:\elsewhere\harness-state"), r"D:\elsewhere\harness-state")
    localised = snap.acl_fingerprint(_icacls(r"C:\Users\x\afs\harness-state", "已成功处理 1 个文件; 处理 0 个文件时失败"),
                                     r"C:\Users\x\afs\harness-state")
    assert here == moved == localised
    denied = _icacls(r"C:\a").replace("NT AUTHORITY", "DESKTOP-X\\\\fcbay:(DENY)(R)\n   NT AUTHORITY", 1)
    assert snap.acl_fingerprint(denied, r"C:\a") != snap.acl_fingerprint(_icacls(r"C:\a"), r"C:\a")


def test_snapshots_of_another_format_or_another_place_are_not_compared(tmp_path):
    root = tmp_path / "afs"
    (root / "harness-state").mkdir(parents=True)
    logs = root / "logs"
    logs.mkdir()
    old_format = snap.take_snapshot(root, app_version="0.5.7") | {"schema_version": 1}
    old_format["entries"] = {k: v | {"acl_sha256": "different"} for k, v in old_format["entries"].items()}
    (logs / snap.SNAPSHOT_NAME).write_text(json.dumps(old_format), encoding="utf-8")
    assert snap.record_startup(root, app_version="0.5.8") is None, "1 版快照的指纹带着路径，和 2 版逐条比必然全红"

    elsewhere = snap.take_snapshot(root, app_version="0.5.8") | {"data_root": str(tmp_path / "old-root")}
    elsewhere["entries"] = {k: v | {"mode": "0o0"} for k, v in elsewhere["entries"].items()}
    (logs / snap.SNAPSHOT_NAME).write_text(json.dumps(elsewhere), encoding="utf-8")
    assert snap.record_startup(root, app_version="0.5.9") is None, "数据根挪过地方：继承来的 ACL 本来就不同"
    assert snap.not_comparable(elsewhere, snap.take_snapshot(root, app_version="x")) is not None
