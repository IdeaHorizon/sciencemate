"""窄单 D #1219 判据 1/3/4 + B #1217 判据 3：回收孤儿 worker 先认亲。

认亲凭据是 spawn token：后端派活时落账（只存哈希，`spawn_ledger`），worker 把 token 写进锁记录。
对得上才杀；对不上不杀、把原因记进那条 run；锁是账本启用前拿的（上一版派的）按从前的判据收，
升级不倒退；账本读不出按从前的判据收并记 warning，不造新的硬失败。
"""
from __future__ import annotations

# ruff: noqa: F811
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.model_backend import ModelBackendConfig
from app.models.project import Project
from app.models.user import User
from app.services import environment_facts, harness_sessions as hs, spawn_ledger
from tests.test_local_runtime_api import runtime_client  # noqa: F401
from tests.test_one_unreadable_run_does_not_stop_the_orphan_sweep import _orphan

TOKEN = "a" * 32


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "spawn-ledger"
    monkeypatch.setattr(spawn_ledger, "ledger_dir", lambda: directory)
    return directory


def _row(token: str = TOKEN, acquired: datetime | None = None) -> dict:
    return {"pid": 4242, "spawn_token": token,
            "acquired_at": (acquired or datetime.now(UTC) + timedelta(seconds=1)).isoformat()}


def test_a_recorded_spawn_is_ours_and_only_its_hash_is_on_disk(ledger) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    assert spawn_ledger.recognise("p", "s", _row())[0] == "ours"
    on_disk = "".join(f.read_text(encoding="utf-8") for f in ledger.iterdir())
    assert TOKEN not in on_disk, "token 是控制通道握手的凭证，账本里只许有哈希"


def test_another_token_or_no_token_is_not_ours(ledger) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    assert spawn_ledger.recognise("p", "s", _row("b" * 32))[0] == "not_ours"
    assert spawn_ledger.recognise("p", "s", _row(""))[0] == "not_ours"
    assert spawn_ledger.recognise("p", "other-session", _row())[0] == "not_ours"


def test_a_lock_taken_before_the_ledger_existed_is_legacy(ledger) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    before = datetime.now(UTC) - timedelta(days=3)
    assert spawn_ledger.recognise("p", "never-recorded", _row("c" * 32, before))[0] == "legacy"


def test_no_ledger_at_all_is_legacy_and_an_unreadable_one_is_unknown(ledger) -> None:
    assert spawn_ledger.recognise("p", "s", _row())[0] == "legacy"
    ledger.mkdir()
    (ledger / spawn_ledger.SINCE_FILENAME).mkdir()          # 读它会 IsADirectoryError —— 「读不出来」
    assert spawn_ledger.recognise("p", "s", _row())[0] == "unknown"


def test_an_unwritable_ledger_never_stops_a_spawn(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("这是文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(spawn_ledger, "ledger_dir", lambda: blocker / "spawn-ledger")
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)        # 不抛


def _lock_holder(tmp_path, monkeypatch, token: str) -> list[int]:
    lock = tmp_path / "session.lock"
    lock.write_text(json.dumps(_row(token)), encoding="utf-8")
    monkeypatch.setattr(hs, "_session_lock_path", lambda p, s: lock)
    monkeypatch.setattr(hs, "_process_is_a_runtime_worker", lambda pid: pid in alive)
    alive = {4242}
    terminated: list[int] = []

    class _Control:
        @staticmethod
        def terminate(pid):
            terminated.append(pid)
            alive.discard(pid)
            return True

        @staticmethod
        def kill(pid):
            terminated.append(pid)
            return True

    real = hs.harness_module
    monkeypatch.setattr(hs, "harness_module",
                        lambda name: _Control if name == "shared.lib.process_control" else real(name))
    return terminated


def test_the_reaper_kills_our_orphan(ledger, tmp_path, monkeypatch) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    terminated = _lock_holder(tmp_path, monkeypatch, TOKEN)
    assert hs.reap_orphaned_session_worker("p", "s") == "pid 4242 terminated"
    assert terminated == [4242]


def test_the_reaper_leaves_a_worker_it_did_not_spawn(ledger, tmp_path, monkeypatch) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    terminated = _lock_holder(tmp_path, monkeypatch, "d" * 32)
    with pytest.raises(hs.NotOurWorker, match="不是本应用派出的"):
        hs.reap_orphaned_session_worker("p", "s")
    assert terminated == [], "不是自己派的进程不许杀"


def test_the_holders_fact_says_whether_it_is_ours(ledger, tmp_path, monkeypatch) -> None:
    spawn_ledger.record_spawn("p", "s", TOKEN, 4242)
    _lock_holder(tmp_path, monkeypatch, TOKEN)
    monkeypatch.setattr(hs, "_session_activity_path", lambda p, s: None)
    holders = environment_facts.own_holders("p", "s")
    assert [(h["pid"], h["spawn_token"], h["alive"], h["ours"]) for h in holders] == [(4242, TOKEN[:8], True, "ours")]


@pytest.mark.asyncio
async def test_the_sweep_reaps_ours_and_records_why_it_left_the_other(runtime_client, ledger, monkeypatch) -> None:
    _client, factory = runtime_client
    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        backend = await db.scalar(select(ModelBackendConfig).where(
            ModelBackendConfig.scope_kind == "institution", ModelBackendConfig.provider == "demo"))
        ours = await _orphan(db, project, user, backend, run_id="run_ledger_ours")
        foreign = await _orphan(db, project, user, backend, run_id="run_ledger_foreign")
        await db.commit()

        async def no_adoption(*args, **kwargs):
            return False

        monkeypatch.setattr(hs.harness_session_manager, "adopt_live_worker", no_adoption)
        spawn_ledger.record_spawn(ours.project_id, ours.session_id, TOKEN, 4242)
        spawn_ledger.record_spawn(foreign.project_id, foreign.session_id, "e" * 32, 4343)
        locks = {ours.session_id: _row(TOKEN) | {"pid": 4242}, foreign.session_id: _row("f" * 32) | {"pid": 4343}}
        tmp = Path(spawn_ledger.ledger_dir()).parent
        for sid, row in locks.items():
            (tmp / f"{sid}.lock").write_text(json.dumps(row), encoding="utf-8")
        monkeypatch.setattr(hs, "_session_lock_path", lambda p, s: tmp / f"{s}.lock")
        alive = {4242, 4343}
        monkeypatch.setattr(hs, "_process_is_a_runtime_worker", lambda pid: pid in alive)
        terminated: list[int] = []

        class _Control:
            @staticmethod
            def terminate(pid):
                terminated.append(pid)
                alive.discard(pid)
                return True

            kill = terminate

        real = hs.harness_module
        monkeypatch.setattr(hs, "harness_module",
                            lambda name: _Control if name == "shared.lib.process_control" else real(name))

        changed = await hs.mark_orphaned_harness_runs(db, reap_workers=True)
        await db.commit()
        await db.refresh(ours)
        await db.refresh(foreign)

    assert changed == 2
    assert terminated == [4242], "只收自己派的那个"
    assert ours.summary["staleReapedWorker"] == "pid 4242 terminated"
    assert foreign.summary["staleReapError"].startswith("NotOurWorker:")
    assert "不是本应用派出的" in foreign.summary["staleReapError"]
