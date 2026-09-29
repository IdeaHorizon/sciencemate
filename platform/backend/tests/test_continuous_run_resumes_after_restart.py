"""被重启打断的 **continuous** 研究会被自动续跑；别的一律不碰（Layer B 选择器）。

误驱动的代价是烧钱（[[project_hypothesis_node_token_runaway]]），所以选择器的每一
条判据都在这里钉死。变异任一条都该有一个负例转红：
  · 不是 continuous 档（assisted）          → test_assisted_run_is_not_resumed
  · 不是重启打断的（别的 staleReason）      → test_non_restart_stale_is_not_resumed
  · 运行时没真丢（attempt 还开着）          → test_run_with_open_attempt_is_not_resumed
  · 太久以前打断的（窗口外）                → test_old_interruption_is_not_resumed
  · 正在续的（进程内幂等）                  → test_a_run_already_resuming_is_skipped
  · 顶层才驱动（子节点跟父走）              → 顺带：子 run 不单独出现
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.config import settings
from app.models.execution import Run, RunAttempt, RunStatus, SessionProjection
from app.models.project import OperationMode, Project, ProjectConfig
from app.models.user import User
from app.services import restart_resume
from app.services.restart_resume import find_resumable_continuous_runs

RECENT = datetime.now(UTC) - timedelta(minutes=5)
OLD = datetime.now(UTC) - timedelta(hours=6)


@pytest.fixture(autouse=True)
def _clear_resuming():
    restart_resume._resuming.clear()
    yield
    restart_resume._resuming.clear()


async def _seed(
    db,
    *,
    continuous: bool = True,
    stale_reason: str = "app_server_restart",
    attempt_ended: bool = True,
    updated_at: datetime = RECENT,
    heartbeat_at: datetime | None = RECENT,
    status: str = RunStatus.RUNNING.value,
    lease_offset_minutes: int = -60,
) -> str:
    pid, sid, uid = str(uuid4()), str(uuid4()), str(uuid4())
    rid = f"run_{uuid4().hex}"
    db.add(User(id=uid, email=f"{uid}@e.local", hashed_password="x", display_name="D"))
    db.add(Project(id=pid, name="P", owner_id=uid))
    # 档位真相源：autonomous + 预授权全部高危 "*" = continuous。
    db.add(ProjectConfig(
        project_id=pid,
        operation_mode=OperationMode.AUTONOMOUS if continuous else OperationMode.ASSISTED,
        autonomous_authorized_risk_classes=["*"] if continuous else [],
    ))
    db.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=pid, session_id=sid, title="S", created_by_user_id=uid,
    ))
    db.add(Run(
        id=rid, tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=pid, session_id=sid, parent_run_id=None,
        status=status, summary={"staleReason": stale_reason} if stale_reason else {},
    ))
    db.add(RunAttempt(
        id=f"a_{uuid4().hex}", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=pid, session_id=sid, run_id=rid, attempt_no=1,
        status="stale_unknown" if attempt_ended else "running",
        lease_until=datetime.now(UTC) + timedelta(minutes=lease_offset_minutes),
        heartbeat_at=heartbeat_at,
        ended_at=datetime.now(UTC) - timedelta(minutes=30) if attempt_ended else None,
    ))
    await db.flush()
    # updated_at 有服务器默认/onupdate，插入后显式压成想要的值再提交。
    from sqlalchemy import update as _update
    await db.execute(
        _update(Run).where(Run.id == rid).values(updated_at=updated_at)
    )
    await db.commit()
    return rid


async def test_interrupted_continuous_run_is_resumable(db_session):
    rid = await _seed(db_session)
    targets = await find_resumable_continuous_runs(db_session)
    assert [t.run_id for t in targets] == [rid]


async def test_assisted_run_is_not_resumed(db_session):
    # assisted 档停下等人是**对的**，平台不能替它做主。
    await _seed(db_session, continuous=False)
    assert await find_resumable_continuous_runs(db_session) == []


async def test_non_restart_stale_is_not_resumed(db_session):
    # 别的原因（真崩、上游拒绝）各有出口，不在这一层兜。
    await _seed(db_session, stale_reason="provider_unavailable")
    assert await find_resumable_continuous_runs(db_session) == []


async def test_a_worker_killed_without_closing_its_attempt_is_resumed(db_session):
    """app server 被杀时来不及关 attempt —— 这正是自愈要接住的那一幕。

    这条原来断言的是相反的事（"attempt 还开着就不驱动"），理由写的是"可能刚
    重启还没登记"。但 `_seed` 造出来的租约**一小时前就过期了**：那不是"刚重启
    还没登记"，那是"worker 死了一小时、没人替它收尾"。刚重启还没登记的那一幕
    由租约挡住（派发时写 lease = now+180s），见
    test_a_freshly_dispatched_run_is_hands_off_even_with_an_empty_registry。

    "那一趟结束了"要 worker 主动写一笔，而 worker 被杀时恰恰写不了 —— 拿它当
    活性判据，等于让每个被 kill -9 的 run 永远显示「运行中」。
    """
    await _seed(db_session, attempt_ended=False)
    assert len(await find_resumable_continuous_runs(db_session)) == 1


async def test_a_fresh_lease_is_never_resumed_out_from_under_a_live_worker(db_session):
    """租约还没过期 = 它刚刚还在动 —— 绝不能在它底下再起一个。

    双开 worker 的代价（两个进程写同一个 worktree）远大于晚续一会儿。
    """
    await _seed(db_session, attempt_ended=False, lease_offset_minutes=+5)
    assert await find_resumable_continuous_runs(db_session) == []


async def test_old_interruption_is_not_resumed(db_session):
    # 上一次真实进展是很久以前 = 老僵尸，不主动复活。
    await _seed(db_session, heartbeat_at=OLD)
    assert await find_resumable_continuous_runs(db_session) == []


async def test_witness_bump_to_updated_at_does_not_defeat_recency(db_session):
    """回归钉子：2026-08-24 本地实测的那条缝。

    `mark_orphaned_harness_runs` 写见证时把 `updated_at` 顶到 now —— 一个几小时前
    就停了的老 run，重启后 updated_at 看起来"刚刚更新"。当时的 recency 判据看
    updated_at，于是误续了 3 条旧测试 run。正解看 `heartbeat_at`（真实进展、见证
    不碰）。这里就造那个形状：updated_at=now，但 heartbeat=6h 前 → 必须不续。
    单测里 updated_at 是手设的，所以最初那版单测照不出这条缝——这条专门补上。
    """
    await _seed(db_session, updated_at=RECENT, heartbeat_at=OLD)
    assert await find_resumable_continuous_runs(db_session) == []


async def test_run_without_heartbeat_is_not_resumed(db_session):
    # 从没产出过真实进展 = 没有可续的断点。
    await _seed(db_session, heartbeat_at=None)
    assert await find_resumable_continuous_runs(db_session) == []


async def test_a_run_already_resuming_is_skipped(db_session):
    rid = await _seed(db_session)
    restart_resume._resuming.add(rid)  # 已在续 → 幂等跳过
    assert await find_resumable_continuous_runs(db_session) == []
