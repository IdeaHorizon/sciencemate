"""升级彩排（RFC_WORKER_START_IS_NOT_A_GATE §3.10，窄单 H）：夹具怎么抄、收据怎么认、发布闸怎么拦。

- 夹具不带秘密：模型 key 抹掉、凭据主密钥不抄、像密钥的字节一个都不许进。
- 夹具记得住要核的东西：项目 / 会话 / run 的 id、KB 条数、有没有上一版的权限快照。
- 发布闸：没收据不发；收据里有 False 不发；sha256 对不上不发；全对才放。
- 装完自检里接了这一步，而且没给夹具时只告警不跳过发布闸。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rehearsal = _load("afs_upgrade_rehearsal", REPO / "scripts" / "acceptance" / "upgrade_rehearsal.py")


def _fake_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "afs"
    (root / "logs").mkdir(parents=True)
    (root / "payload" / "big").mkdir(parents=True)
    (root / "payload" / "big" / "app.zip").write_bytes(b"x" * 1024)
    (root / "credential.key").write_bytes(b"super-secret-master-key")
    con = sqlite3.connect(root / "db.sqlite")
    con.executescript("""
        create table projects(id text primary key, name text);
        create table sessions(session_id text primary key, project_id text, title text);
        create table runs(id text primary key, session_id text, project_id text, status text);
        create table model_backend_configs(id text primary key, provider text, encrypted_api_key text, api_token text, context_window_tokens integer, prompt_tokens integer);
        create table users(id text primary key, hashed_password text not null, must_change_password boolean not null);
        insert into users values ('u1', '$argon2$real-hash', 0);
        insert into projects values ('p1', '测试项目');
        insert into sessions values ('s1', 'p1', '第一个会话');
        insert into runs values ('run_a', 's1', 'p1', 'completed');
        insert into runs values ('run_b', 's1', 'p1', 'failed');
        insert into model_backend_configs values ('b1', 'openai_compatible', 'gAAAA-encrypted', 'tok', 128000, 42);
    """)
    con.commit(); con.close()
    kb = root / "harness-state" / "projects" / "p1"
    kb.mkdir(parents=True)
    kb.joinpath("kb_claims.jsonl").write_text('{"id":"c1"}\n{"id":"c2"}\n{"id":"c3"}\n', encoding="utf-8")
    (root / "harness-state" / "users" / "u1").mkdir(parents=True)
    (root / "harness-state" / "users" / "u1" / ".platform-tenant-identity.json").write_text(
        '{"schema_version": 1, "tenant_id": "local"}\n', encoding="utf-8")
    (root / "project-worktrees" / "w" / ".research").mkdir(parents=True)
    (root / "project-worktrees" / "w" / "huge.bin").write_bytes(b"\0" * (rehearsal.FILE_CAP + 1))
    (root / "logs" / "permissions-snapshot.json").write_text(
        json.dumps({"app_version": "0.5.6", "entries": {".": {"mode": "0o700"}}}), encoding="utf-8")
    (root / "logs" / "backend.log").write_text("not carried\n", encoding="utf-8")
    return root


def test_capture_scrubs_secrets_and_remembers_what_to_check(tmp_path):
    root = _fake_data_root(tmp_path)
    out = tmp_path / "fixture"
    manifest = rehearsal.capture(root, out, version="0.5.6")
    assert manifest["previous_version"] == "0.5.6"
    assert [p["id"] for p in manifest["projects"]] == ["p1"]
    assert [s["id"] for s in manifest["sessions"]] == ["s1"]
    assert {r["id"] for r in manifest["runs"]} == {"run_a", "run_b"}
    assert manifest["kb_claims"] == {"p1": 3}
    assert manifest["has_permissions_snapshot"] is True
    assert "project-worktrees/w/huge.bin" in manifest["skipped_large"]
    assert "model_backend_configs.encrypted_api_key" in manifest["scrubbed"]
    assert "model_backend_configs.api_token" in manifest["scrubbed"]
    # 秘密一个都没过去
    assert not (out / "credential.key").exists()
    assert not (out / "payload").exists()
    assert not (out / "logs" / "backend.log").exists()
    con = sqlite3.connect(out / "db.sqlite")
    assert con.execute("select encrypted_api_key, api_token from model_backend_configs").fetchone() == (None, None)
    # 计数列不是秘密：抹了它们夹具就不再是「上一版真跑过」的样子
    assert con.execute("select context_window_tokens, prompt_tokens from model_backend_configs").fetchone() == (128000, 42)
    con.close()
    assert not [c for c in manifest["scrubbed"] if "tokens" in c]
    con = sqlite3.connect(out / "db.sqlite")
    assert con.execute("select hashed_password, must_change_password from users").fetchone() == ("scrubbed-for-fixture", 0), \
        "NOT NULL 的文本秘密列写占位串；布尔列不碰"
    con.close()
    assert "users.hashed_password (placeholder)" in manifest["scrubbed"]
    # 要核的东西都在
    assert (out / "harness-state" / "users" / "u1" / ".platform-tenant-identity.json").is_file()
    assert (out / "logs" / "permissions-snapshot.json").is_file()
    assert json.loads((out / rehearsal.MANIFEST).read_text(encoding="utf-8"))["kb_claims"] == {"p1": 3}


def test_capture_refuses_a_fixture_that_still_carries_a_key(tmp_path):
    root = _fake_data_root(tmp_path)
    (root / "user").mkdir()
    (root / "user" / "notes.md").write_text("my key is sk-abcdef0123456789\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="像密钥"):
        rehearsal.capture(root, tmp_path / "fixture2", version="0.5.6")


def test_restore_copies_everything_but_the_manifest_into_an_empty_dir(tmp_path):
    root = _fake_data_root(tmp_path)
    out = tmp_path / "fixture"
    rehearsal.capture(root, out, version="0.5.6")
    target = tmp_path / "fresh"
    manifest = rehearsal.restore(out, target)
    assert manifest["previous_version"] == "0.5.6"
    assert (target / "db.sqlite").is_file() and not (target / rehearsal.MANIFEST).exists()
    (tmp_path / "dirty").mkdir(); (tmp_path / "dirty" / "x").write_text("x")
    with pytest.raises(SystemExit, match="不是空的"):
        rehearsal.restore(out, tmp_path / "dirty")


def test_a_packed_fixture_restores_like_a_directory(tmp_path):
    import tarfile

    root = _fake_data_root(tmp_path)
    out = tmp_path / "fixture"
    rehearsal.capture(root, out, version="0.5.6")
    packed = tmp_path / "upgrade-from-0.5.6.tgz"
    with tarfile.open(packed, "w:gz") as archive:
        archive.add(out, arcname="upgrade-from-0.5.6")
    target = tmp_path / "fresh"
    manifest = rehearsal.restore(packed, target)
    assert manifest["previous_version"] == "0.5.6" and (target / "db.sqlite").is_file()


def test_the_committed_fixture_is_small_and_carries_no_secret():
    """入库的那份夹具（0.5.6，PC 上真跑过一条消息的数据根）：有 manifest、有 run、没有秘密、没有载荷。"""
    import tarfile

    packed = REPO / "scripts" / "acceptance" / "fixtures" / "upgrade-from-0.5.6.tgz"
    assert packed.is_file() and packed.stat().st_size < 2 * 1024 * 1024
    with tarfile.open(packed, "r:gz") as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile([n for n in names if n.endswith(rehearsal.MANIFEST)][0]))
    assert manifest["previous_version"] == "0.5.6"
    assert manifest["runs"] and manifest["sessions"]
    assert manifest["skipped_large"] == []
    assert not [n for n in names if "/payload/" in n or n.endswith("credential.key") or "/webview2/" in n]


def test_long_path_prefix_only_on_windows(tmp_path, monkeypatch):
    target = tmp_path / "x"
    monkeypatch.setattr(rehearsal.sys, "platform", "linux")
    assert rehearsal.long_path(target) == str(target.resolve())
    monkeypatch.setattr(rehearsal.sys, "platform", "win32")
    assert rehearsal.long_path(target).startswith("\\\\?\\")
    assert rehearsal.long_path(rehearsal.long_path(target)) == rehearsal.long_path(target), "不重复加前缀"


def test_restore_relocates_through_the_one_legal_exit(tmp_path, monkeypatch):
    """搬根只有一个合法出口（relocate_project_data.py）：restore 调它、带 --copied --apply、数据根指到 to。"""
    root = _fake_data_root(tmp_path)
    out = tmp_path / "fixture"
    rehearsal.capture(root, out, version="0.5.6")
    seen = {}

    class _Done:
        returncode = 0
        stdout = "完成"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        return _Done()

    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    target = tmp_path / "fresh"
    rehearsal.restore(out, target, python="/opt/app/python", tree=REPO)
    assert seen["argv"][0] == "/opt/app/python"
    assert seen["argv"][1].endswith("platform/backend/scripts/relocate_project_data.py")
    assert {"--to", str(target), "--apply", "--copied"} <= set(seen["argv"])
    assert seen["env"]["PLATFORM_DATA_ROOT"] == str(target)
    source = (REPO / "platform" / "backend" / "scripts" / "relocate_project_data.py").read_text(encoding="utf-8")
    assert '"--copied"' in source and "不搬" in source


def test_api_payloads_may_be_lists_or_items_envelopes():
    assert rehearsal._items([{"id": 1}]) == [{"id": 1}]
    assert rehearsal._items({"items": [{"id": 2}], "total": 1}) == [{"id": 2}]
    assert rehearsal._items({"detail": "x"}) == [] and rehearsal._items("text") == []


def test_snapshot_compare_matches_the_backend_rule():
    before = {"entries": {"a": {"mode": "0o700", "owner": "1"}, "gone": {"mode": "0o700"}}}
    after = {"entries": {"a": {"mode": "0o500", "owner": "1"}, "new": {"mode": "0o700"}}}
    assert rehearsal._compare_snapshots(before, after) == [
        {"path": "a", "field": "mode", "before": "0o700", "after": "0o500"}]


# ── 发布闸 ───────────────────────────────────────────────────────────────────

publish = _load("afs_publish_release", REPO / "scripts" / "package" / "publish_release.py")


def _release_dir(tmp_path: Path, *, receipt: dict | None, sha: str = "a" * 64) -> Path:
    release = tmp_path / "release"
    release.mkdir(parents=True)
    (release / "SHA256SUMS").write_text(f"{sha}  ScienceMate-Setup.exe\n", encoding="utf-8")
    if receipt is not None:
        (release / publish.WINDOWS_REHEARSAL).write_text(json.dumps(receipt), encoding="utf-8")
    return release


def _good_receipt(sha: str = "a" * 64) -> dict:
    return {"result": "REHEARSAL OK", "against": "http://127.0.0.1:1", "previous_version": "0.5.6",
            "checks": {"model_called": True, "environment_clean": True, "capabilities_all_true": True,
                       "previous_runs_visible": True, "kb_readable": True, "permissions_unchanged": None},
            "host": "pc", "ran_at": "2026-10-16T00:00:00+00:00", "installer": "ScienceMate-Setup.exe", "sha256": sha}


def test_the_gate_refuses_without_a_rehearsal_receipt(tmp_path):
    with pytest.raises(SystemExit, match="没有人在上一版的数据根上彩排过升级"):
        publish.refuse_a_windows_build_that_never_rehearsed_an_upgrade(_release_dir(tmp_path, receipt=None))


def test_the_gate_refuses_a_receipt_with_a_failed_check(tmp_path):
    bad = _good_receipt(); bad["checks"]["previous_runs_visible"] = False
    with pytest.raises(SystemExit, match="previous_runs_visible"):
        publish.refuse_a_windows_build_that_never_rehearsed_an_upgrade(_release_dir(tmp_path, receipt=bad))


def test_the_gate_refuses_other_bytes_and_passes_the_same_bytes(tmp_path, capsys):
    with pytest.raises(SystemExit, match="不是同一份字节"):
        publish.refuse_a_windows_build_that_never_rehearsed_an_upgrade(_release_dir(tmp_path, receipt=_good_receipt("b" * 64)))
    publish.refuse_a_windows_build_that_never_rehearsed_an_upgrade(_release_dir(tmp_path / "ok", receipt=_good_receipt()))
    assert "升级彩排" in capsys.readouterr().out


def test_a_release_without_windows_files_needs_no_rehearsal(tmp_path):
    release = tmp_path / "release"; release.mkdir()
    (release / "SHA256SUMS").write_text("c" * 64 + "  ScienceMate-0.5.7-mac.zip\n", encoding="utf-8")
    publish.refuse_a_windows_build_that_never_rehearsed_an_upgrade(release)


def test_publish_runs_the_rehearsal_gate_right_after_the_acceptance_gate():
    source = (REPO / "scripts" / "package" / "publish_release.py").read_text(encoding="utf-8")
    body = source[source.index("def publish("):]
    assert body.index("refuse_a_windows_build_nobody_ran(release_dir)") < body.index(
        "refuse_a_windows_build_that_never_rehearsed_an_upgrade(release_dir)") < body.index("refuse_if_the_wire_broke(")


def test_the_installer_self_check_rehearses_the_upgrade_when_given_a_fixture():
    source = (REPO / "scripts" / "package" / "build_windows_app.py").read_text(encoding="utf-8")
    assert "prove_an_upgrade_from_the_previous_release_works(install_dir, exe, setup, upgrade_fixture)" in source
    assert '"--upgrade-fixture"' in source
    assert "prove_the_installer_works(setup, Path(args.upgrade_fixture) if args.upgrade_fixture else None)" in source
