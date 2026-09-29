"""活 pause 是最强活性证据 —— 账面与它矛盾时一律和解，永不拒答。

这条不变量的两代事故：

2026-08-09（台账 #1/#8）：turn 以 paused 结束 → run waiting_human，同一秒
attempt 被标 failed 关闭 → answer 被拒、turn 被拒，两条路同时堵死。当时的修法
是"抛错时把 run 标成可恢复"（_mark_run_stale + raise）—— 让死锁可见，但仍然
拒答。

2026-08-24（run_7aba18be…c1259）：清扫在 attempt 出生 100ms 时把它误判
stale_unknown（注册表还没登记的竞态），run 本体正常跑完并停在 pause 等人。
用户答复时，老逻辑发现 attempt 非 RUNNING → 盖 `resumable=false` 并拒答；
而 run 还是 waiting_human，UI 照常呈卡片 —— **卡片说"答我"，答复说"你不可
答"**，用户无限撞墙。"标成可恢复"的出口和"继续呈卡片"的出口互指。

根因是把账本当权威、把活着的现场当嫌疑人。`_validate_resume_binding` 只在
`paused_binding` 存在（session.alive 且 session.paused，都是现场事实）时被调
—— 一个活体进程此刻停在 pause 上等人，账面说什么都推翻不了这一点。所以：
按账面拒答的三道闸（状态白名单 / resumable 标志 / attempt 必须 RUNNING）全部
删除，矛盾一律和解回现场。唯一例外是人的明确意志：CANCELLED / COMPLETED。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.execution import AttemptStatus, Run, RunAttempt, RunStatus
from app.services.harness_sessions import AppRunBinding
from app.services.local_execution import (
    HarnessSessionStaleError,
    _validate_resume_binding,
)
from app.services.run_liveness import runtime_lost


async def _seed(
    db,
    *,
    attempt_status: str,
    run_status: str = RunStatus.WAITING_HUMAN.value,
    summary: dict | None = None,
    attempt_ended: bool = False,
    attempt_exit_reason: str | None = None,
):
    run = Run(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="sess",
        id="run-1", status=run_status, summary={} if summary is None else summary,
    )
    db.add(run)
    db.add(RunAttempt(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="sess",
        run_id="run-1", attempt_no=1, status=attempt_status,
        exit_reason=attempt_exit_reason,
        ended_at=datetime.now(UTC) if attempt_ended else None,
    ))
    await db.flush()
    return run


def _binding():
    return AppRunBinding(
        user_id="u1", conversation_id="sess", session_id="sess", run_id="run-1",
    )


async def _validate(db):
    return await _validate_resume_binding(
        db, binding=_binding(), user_id="u1",
        project_id="proj", conversation_id="sess")


@pytest.mark.asyncio
async def test_live_attempt_resumes_normally(db_session):
    await _seed(db_session, attempt_status=AttemptStatus.RUNNING.value)
    run = await _validate(db_session)
    assert run.status == RunStatus.WAITING_HUMAN.value, "活着的 attempt 不该被动"
    assert "reconciledFromLivePause" not in (run.summary or {}), \
        "没有矛盾就没有和解 —— 正常答复路径不许留下和解痕迹"


@pytest.mark.asyncio
async def test_the_born_dead_race_shape_reconciles_and_answers(db_session):
    """2026-08-24 真实事故形态的回放 —— 这个输入曾把用户卡死。

    attempt 被清扫误判（stale_unknown / app_server_restart / ended_at 已盖）、
    summary 被答复闸盖过毒（staleReason=pause_outlived_attempt）、
    run 还是 waiting_human、pause 活着。旧代码对它抛
    "The durable Run is not safely resumable"；新不变量：和解并放行。
    """
    await _seed(
        db_session,
        attempt_status=AttemptStatus.STALE_UNKNOWN.value,
        attempt_ended=True,
        attempt_exit_reason="app_server_restart",
        summary={"staleReason": "pause_outlived_attempt",
                 "staleDetectedAt": "2026-08-24T08:08:04+00:00"},
    )

    run = await _validate(db_session)

    assert run.status == RunStatus.WAITING_HUMAN.value
    # `resumable` 不再落盘（关于未来的判决）。和解成功的判据是**事实**回到
    # 现场：状态是 waiting_human、见证被清掉、attempt 重新开着。
    assert "resumable" not in run.summary
    assert "staleReason" not in run.summary
    assert "staleDetectedAt" not in run.summary
    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "run-1"))
    assert attempt.status == AttemptStatus.RUNNING.value
    assert attempt.ended_at is None, \
        "ended_at 是 observed_status 的'这一趟结束了'正面证据 —— 留着它账面就还在说两种话"
    assert attempt.exit_reason is None


@pytest.mark.asyncio
async def test_a_closed_attempt_under_a_live_pause_reconciles_not_rejects(db_session):
    """attempt 关了而 pause 活着 —— 和解，不是"标成可恢复然后拒答"。

    旧行为（_mark_run_stale + raise "start a new Session from it"）自己就是
    两出口互指的一半：它叫用户另起 Session，而 run 停在 waiting_human 让 UI
    继续呈可答卡片。
    """
    await _seed(db_session, attempt_status=AttemptStatus.FAILED.value,
                attempt_ended=True, attempt_exit_reason="answer_turn_failed")

    run = await _validate(db_session)

    assert run.status == RunStatus.WAITING_HUMAN.value
    assert "resumable" not in run.summary
    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "run-1"))
    assert attempt.status == AttemptStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_failed_ledger_reconciles_to_the_live_pause(db_session):
    """账面 failed + 活体进程停在 pause 上 → 听现场的（2026-08-17 事故形态）。"""
    await _seed(
        db_session,
        attempt_status=AttemptStatus.FAILED.value,
        run_status=RunStatus.FAILED.value,
    )

    run = await _validate(db_session)

    assert run.status == RunStatus.WAITING_HUMAN.value
    assert "resumable" not in run.summary
    assert run.summary["reconciledFromLivePause"]["previousStatus"] == RunStatus.FAILED.value
    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "run-1"))
    assert attempt.status == AttemptStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_missing_attempt_row_does_not_block_the_answer(db_session):
    """attempt 行整个不存在 —— 放行，别造假行，别拒答。后续 ingest 会如实补。"""
    run = Run(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="sess",
        id="run-1", status=RunStatus.WAITING_HUMAN.value, summary={"resumable": True},
    )
    db_session.add(run)
    await db_session.flush()

    result = await _validate(db_session)

    assert result.id == "run-1"
    attempts = (await db_session.scalars(
        select(RunAttempt).where(RunAttempt.run_id == "run-1"))).all()
    assert attempts == [], "不许凭空造 attempt 行 —— 那是 ingest 的事"


@pytest.mark.asyncio
async def test_a_cancelled_run_is_not_resurrected(db_session):
    """CANCELLED 是人的明确意志 —— 现场再活也不许把它翻回来。"""
    await _seed(
        db_session,
        attempt_status=AttemptStatus.FAILED.value,
        run_status=RunStatus.CANCELLED.value,
    )

    with pytest.raises(HarnessSessionStaleError):
        await _validate(db_session)

    run = await db_session.scalar(select(Run).where(Run.id == "run-1"))
    assert run.status == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_a_completed_run_is_not_reopened(db_session):
    """COMPLETED 与"还在等答复"真矛盾 —— 坏的是 pause 注册表，不许倒写结论。"""
    await _seed(
        db_session,
        attempt_status=AttemptStatus.COMPLETED.value,
        run_status=RunStatus.COMPLETED.value,
    )

    with pytest.raises(HarnessSessionStaleError):
        await _validate(db_session)

    run = await db_session.scalar(select(Run).where(Run.id == "run-1"))
    assert run.status == RunStatus.COMPLETED.value


# ── 清扫侧：注册表空缺不是死亡证明 ────────────────────────────────────────────


def _orphan(i: int, status: str, *, created_ago_seconds: int = 3600, summary=None):
    return Run(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id=f"s{i}",
        id=f"orphan-{i}", status=status, summary={"executionKernel": "formal_harness"} if summary is None else summary,
        created_at=datetime.now(UTC) - timedelta(seconds=created_ago_seconds),
    )


@pytest.mark.asyncio
async def test_reaper_covers_running_not_just_waiting_human(db_session):
    """回收器必须扫**所有需要活进程**的状态，不只是等人的那种（2026-08-09）。

    种子必须过了出生宽限（created_at 后退）：宽限内"注册表没有它"不构成证据。
    """
    from app.services.harness_sessions import mark_orphaned_harness_runs

    for i, status in enumerate((
        RunStatus.RUNNING.value,
        RunStatus.WAITING_HUMAN.value,
        RunStatus.WAITING_PERMISSION.value,
        RunStatus.RETRYING.value,
    )):
        db_session.add(_orphan(i, status))
    db_session.add(Run(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="s-done",
        id="done-1", status=RunStatus.COMPLETED.value, summary={"executionKernel": "formal_harness"},
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    await db_session.flush()

    changed = await mark_orphaned_harness_runs(db_session)

    assert changed == 4, "四个需要活进程的状态都要回收，running 不能漏"
    running = await db_session.scalar(select(Run).where(Run.id == "orphan-0"))
    assert runtime_lost(running, has_live_binding=False) is True
    assert running.summary["staleFromStatus"] == RunStatus.RUNNING.value, \
        "要如实记下死前状态 —— running 时失联可能有半截产物"
    assert "resumable" not in running.summary, \
        "清扫不许写 resumable —— 那是关于未来的判决，能不能续由现场现算"
    done = await db_session.scalar(select(Run).where(Run.id == "done-1"))
    assert done.status == RunStatus.COMPLETED.value, "终态不许被动"


@pytest.mark.asyncio
async def test_reaper_does_not_require_resumable(db_session):
    """`resumable` 回答"暂停能不能续"，拿它当回收门方向正好反了（2026-08-09）。"""
    from app.services.harness_sessions import mark_orphaned_harness_runs

    db_session.add(_orphan(10, RunStatus.RUNNING.value, summary={}))
    db_session.add(_orphan(11, RunStatus.RUNNING.value,
                           summary={"executionKernel": "formal_harness",
                                    "resumable": False}))
    await db_session.flush()

    changed = await mark_orphaned_harness_runs(db_session)

    assert changed == 2, "summary 空的、resumable=False 的，都得回收"
    for rid in ("orphan-10", "orphan-11"):
        run = await db_session.scalar(select(Run).where(Run.id == rid))
        assert runtime_lost(run, has_live_binding=False) is True


@pytest.mark.asyncio
async def test_a_newborn_attempt_with_a_fresh_lease_is_not_swept(db_session):
    """2026-08-24 竞态的回放：attempt 出生 100ms、租约新鲜、binding 还没登记。

    清扫此刻撞进来（会话 GET 的 `_reap_orphaned_runs_of` 就挂在高频路径上），
    不许盖章 —— 租约没过期连"不知道"都算不上。
    """
    from app.services.harness_sessions import mark_orphaned_harness_runs

    now = datetime.now(UTC)
    db_session.add(_orphan(20, RunStatus.RUNNING.value, created_ago_seconds=0))
    db_session.add(RunAttempt(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="s20",
        run_id="orphan-20", attempt_no=1, status=AttemptStatus.RUNNING.value,
        heartbeat_at=now, lease_until=now + timedelta(seconds=180),
    ))
    await db_session.flush()

    changed = await mark_orphaned_harness_runs(db_session)

    assert changed == 0, "租约新鲜的 attempt 被扫死了 —— 出生竞态回来了"
    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "orphan-20"))
    assert attempt.status == AttemptStatus.RUNNING.value
    assert attempt.ended_at is None
    run = await db_session.scalar(select(Run).where(Run.id == "orphan-20"))
    assert "staleReason" not in (run.summary or {})


@pytest.mark.asyncio
async def test_a_newborn_run_without_any_attempt_is_not_swept(db_session):
    """run 行已建、第一条事件还没到 —— 出生宽限内不许按"注册表没有它"定罪。"""
    from app.services.harness_sessions import mark_orphaned_harness_runs

    db_session.add(_orphan(21, RunStatus.RUNNING.value, created_ago_seconds=0))
    await db_session.flush()

    changed = await mark_orphaned_harness_runs(db_session)

    assert changed == 0
    run = await db_session.scalar(select(Run).where(Run.id == "orphan-21"))
    assert "staleReason" not in (run.summary or {})


@pytest.mark.asyncio
async def test_an_expired_lease_is_swept(db_session):
    """租约过期 + 无 binding = 真无主 —— 该扫还得扫，宽限不是豁免。"""
    from app.services.harness_sessions import mark_orphaned_harness_runs

    stale_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(_orphan(22, RunStatus.RUNNING.value))
    db_session.add(RunAttempt(
        tenant_id=settings.runtime_tenant_id,
        workspace_id="ws", project_id="proj", session_id="s22",
        run_id="orphan-22", attempt_no=1, status=AttemptStatus.RUNNING.value,
        heartbeat_at=stale_at, lease_until=stale_at + timedelta(seconds=180),
    ))
    await db_session.flush()

    changed = await mark_orphaned_harness_runs(db_session)

    assert changed == 1
    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "orphan-22"))
    assert attempt.status == AttemptStatus.STALE_UNKNOWN.value
    assert attempt.ended_at is not None, \
        "ended_at 必须盖上 —— observed_status 拿它当'这一趟结束了'的正面证据"
