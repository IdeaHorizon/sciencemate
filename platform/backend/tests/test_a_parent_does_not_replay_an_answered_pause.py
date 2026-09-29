"""子运行恢复之后，父运行那张权限卡就不该还在（#887）。

## 现场

父 orchestrator 等 Experiment 子运行。子运行两次进入高危确认，**两次都批准了并
继续**，任务最终执行完成。父运行却又把**第一张**权限卡摆回人面前，还要再 resume
一次才走；服务端日志里同时有

    ledger contradicted a live pause … reconciled from scene
    Run … was already completed and then went waiting_permission
        — a terminal was stamped too early

顶层会话最终 `completed/ok`，而 Experiment 子 Run 仍被投影成
`waiting_human / alive / endedAt=null`。

## 三条机械判据

1. **哪条最新，按平台自己的单调序算**，不按 worker 的墙钟。`sequence` 在摄取那
   一刻由 `UPDATE … RETURNING` 原子分配、库里有唯一约束；`occurred_at` 是两个
   进程写 transcript 时的墙钟，应用重启后还会从字节偏移重读。拿墙钟当主序，
   "哪条最新"就成了一个依赖运行环境的答案。
2. **那一步结束了，父这边的卡就失效。** 父 run 的 `run.paused` 有一支是从
   `subagent_call_paused` 投影来的，带着 `stepId`；而那一步的结束由子 transcript
   自己声明（`step.completed` / `step.failed`，同一个 stepId）。
3. **终态不回退。** 一条已经 completed 的 run 不该再被投影成 waiting_permission。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.execution import EXECUTION_TABLES, ExecutionEvent, Run, RunStatus
from app.services.execution_ingest import ExecutionIngestService, IngestContext
from app.services.sessions import _pending_approval

AT = "2026-09-08T04:50:00+00:00"


@pytest_asyncio.fixture
async def execution_db() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=EXECUTION_TABLES
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.drop_all(
                sync_connection, tables=reversed(EXECUTION_TABLES)
            )
        )
    await engine.dispose()


def _context(run: str = "run-parent") -> IngestContext:
    return IngestContext(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        project_id="project-a",
        session_id="session-a",
        run_id=run,
        actor_user_id=None,
        decision_authority=None,
    )


async def _ingest(service, db, context, raw, *, offset, state, file_identity):
    return await service.ingest_raw_record(
        db,
        context=context,
        file_identity=file_identity,
        byte_offset=offset,
        raw_line=json.dumps(raw, ensure_ascii=False, sort_keys=True).encode(),
        raw=raw,
        adapter_state=state,
    )


async def _run_row(db: AsyncSession, run_id: str) -> Run | None:
    return await db.scalar(select(Run).where(Run.id == run_id))


@pytest.mark.asyncio
async def test_a_parent_pause_dies_when_its_child_step_finishes(
    execution_db: AsyncSession,
) -> None:
    """子步骤跑完之后，父这边那张卡消失 —— 不需要父自己收到 resume。

    #887 现场里父那条 resume 因为应用重启丢了，于是卡一直挂着；而子运行的两次
    批准**都已经生效**。人再点一次，批准的是一个早就过去的暂停。
    """
    service = ExecutionIngestService()
    context = _context()
    state: dict = {}
    offset = 0

    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": AT, "node_type": "project_chat"},
                  offset=offset, state=state, file_identity="parent")
    offset += 1
    # 父侧：派子节点 → 子节点停下问人（投影成父 run 的 run.paused，带 stepId）
    await _ingest(service, execution_db, context,
                  {"event": "subagent_call_start", "at": AT,
                   "child_node_type": "experiment", "child_depth": 1},
                  offset=offset, state=state, file_identity="parent")
    offset += 1
    await _ingest(service, execution_db, context,
                  {"event": "subagent_call_paused", "at": AT,
                   "child_node_type": "experiment",
                   "child_run_id": "1788839891-3ac143"},
                  offset=offset, state=state, file_identity="parent")
    offset += 1
    await execution_db.flush()

    parent = await _run_row(execution_db, "run-parent")
    assert parent is not None and parent.status == RunStatus.WAITING_HUMAN.value
    card = await _pending_approval(execution_db, run=parent)
    assert card is not None, "子节点停下问人，父这边本来就该有一张卡"
    step_id = str((card.get("raw") or {}).get("stepId") or "") or _step_id_of(
        await _pauses(execution_db))

    # 子 transcript 自己声明这一步结束了（同一个 stepId）
    await _ingest(service, execution_db, context,
                  {"event": "root_step_end", "at": AT, "status": "completed"},
                  offset=offset, state={"rootStepId": step_id},
                  file_identity="child")
    await execution_db.flush()

    assert await _pending_approval(execution_db, run=parent) is None, (
        "子步骤已经结束，父这边还在摆那张权限卡 —— 人会去批准一个过去的暂停")


async def _pauses(db: AsyncSession) -> list[ExecutionEvent]:
    return list((await db.scalars(
        select(ExecutionEvent).where(ExecutionEvent.kind == "run.paused")
        .order_by(ExecutionEvent.sequence))).all())


def _step_id_of(events: list[ExecutionEvent]) -> str:
    for event in reversed(events):
        step = str(dict(event.payload or {}).get("stepId") or "")
        if step:
            return step
    raise AssertionError("父 run 的 run.paused 里没有 stepId —— 这条链接不上了")


@pytest.mark.asyncio
async def test_the_newest_pause_is_decided_by_sequence_not_wall_clock(
    execution_db: AsyncSession,
) -> None:
    """两张卡先后呈递、第二张的墙钟**更早**时，仍然认第二张。

    父子两份 transcript 各由一个进程写；应用重启后还会从字节偏移重读。拿墙钟当
    "哪条最新"的判据，答案就依赖运行环境 —— 而它答错的样子就是重放第一张卡。
    """
    service = ExecutionIngestService()
    context = _context("run-clock")
    state: dict = {}

    later = datetime(2026, 9, 8, 4, 50, 0, tzinfo=UTC)
    earlier = later - timedelta(seconds=30)

    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": later.isoformat(), "node_type": "project_chat"},
                  offset=0, state=state, file_identity="p")
    await _ingest(service, execution_db, context,
                  {"event": "run_paused", "at": later.isoformat(),
                   "reason": "waiting_permission", "question": "第一张：批准 rm -rf?"},
                  offset=1, state=state, file_identity="p")
    await _ingest(service, execution_db, context,
                  {"event": "loop_resume", "at": later.isoformat()},
                  offset=2, state=state, file_identity="p")
    # 第二张卡的墙钟**比第一张早**（时钟回拨 / 另一份 transcript 的时间）
    await _ingest(service, execution_db, context,
                  {"event": "run_paused", "at": earlier.isoformat(),
                   "reason": "waiting_permission", "question": "第二张：批准 submit_job?"},
                  offset=3, state=state, file_identity="p")
    await execution_db.flush()

    run = await _run_row(execution_db, "run-clock")
    card = await _pending_approval(execution_db, run=run)
    assert card is not None
    assert "第二张" in str(card.get("prompt")), (
        f"按墙钟挑，挑回了已经批准过的第一张：{card.get('prompt')!r}")


@pytest.mark.asyncio
async def test_a_completed_run_is_not_projected_back_to_waiting(
    execution_db: AsyncSession,
) -> None:
    """终态不回退：迟到/重放的 pause 不能把一条已完成的 run 打回等人。"""
    service = ExecutionIngestService()
    context = _context("run-terminal")
    state: dict = {}

    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": AT, "node_type": "project_chat"},
                  offset=0, state=state, file_identity="p")
    await _ingest(service, execution_db, context,
                  {"event": "run_end", "at": AT, "status": "completed"},
                  offset=1, state=state, file_identity="p")
    await execution_db.flush()
    run = await _run_row(execution_db, "run-terminal")
    assert run.status == RunStatus.COMPLETED.value

    await _ingest(service, execution_db, context,
                  {"event": "run_paused", "at": AT,
                   "reason": "waiting_permission", "question": "迟到的旧卡"},
                  offset=2, state=state, file_identity="p")
    await execution_db.flush()
    await execution_db.refresh(run)

    assert run.status == RunStatus.COMPLETED.value, (
        "一条已经 completed 的 run 被打回 waiting_permission —— "
        "服务端日志里那句「a terminal was stamped too early」就是这一刻")
    refused = (run.summary or {}).get("refusedProjections") or []
    assert refused and refused[-1]["refused"] == RunStatus.WAITING_PERMISSION.value, (
        "挡下来了却不留痕 —— 那就分不清「挡住了」和「根本没发生」")
    assert await _pending_approval(execution_db, run=run) is None


@pytest.mark.asyncio
async def test_a_terminal_can_still_be_corrected_to_another_terminal(
    execution_db: AsyncSession,
) -> None:
    """对照：只挡「终态 → 非终态」。终态之间的改写照旧放行并留痕。

    把两件事压在一起会把其中一件一起藏掉。
    """
    service = ExecutionIngestService()
    context = _context("run-two-terminals")
    state: dict = {}

    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": AT, "node_type": "project_chat"},
                  offset=0, state=state, file_identity="p")
    await _ingest(service, execution_db, context,
                  {"event": "run_end", "at": AT, "status": "completed"},
                  offset=1, state=state, file_identity="p")
    await _ingest(service, execution_db, context,
                  {"event": "run_end", "at": AT, "status": "failed"},
                  offset=2, state=state, file_identity="p")
    await execution_db.flush()

    run = await _run_row(execution_db, "run-two-terminals")
    assert run.status == RunStatus.FAILED.value
