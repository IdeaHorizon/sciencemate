"""run 走到终态，它那张卡片也必须合上。

## 现场（yuankk，2026-09-17，node20）

「这个显示没能完成，但又显示正在 writing」—— 研究早就停了，界面还在转圈。
查库：

    step.started    833 次
    step.completed    5 次
    root_step_end     0 次（harness 全仓不发这个事件）

## 根因：懒开的一端有，懒关的一端没有

root step 是 `_ensure_root_step` 在第一条 `tool_call` 到达时**懒开**的 —— 因为
harness 从不发 `root_step_start`。而关闭那一端**只**认 `root_step_end` 原始事件，
它同样没人发（只有 local_execution 的异常路径发）。于是正常跑完的 run，卡片永远
停在"进行中"；`subagent_call_end` 那个分支甚至注释写着"它的 root_step_end 会发
step.completed"——一个从不发生的期待。

## 修法：不给它第二个真相源

补一个"记得发 root_step_end"的调用点是打补丁 —— 下一条终态路径照样会忘（现在
已经忘了至少三条）。这一步的终态**本来就没有独立事实**：run 结束了，它就结束
了。所以从 run 的终态推出来，和开那一端一样懒、一样由到达的事件触发。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService

from .test_execution_foundation import (  # noqa: F401
    AT,
    _context,
    _raw_line,
    _runtime_transcript,
    execution_db,
    ingest_transcript_like_production,
)


async def _ingest(db: AsyncSession, records: list[dict]) -> list[ExecutionEvent]:
    service = ExecutionIngestService()
    path = _runtime_transcript("runs/1786331823-0d8bfc/transcript.jsonl", records)
    await ingest_transcript_like_production(
        db, service=service, context=_context(), path=path, harness_states={})
    return list((await db.execute(select(ExecutionEvent))).scalars().all())


def _by_kind(events: list[ExecutionEvent], kind: str) -> list[dict]:
    return [e.payload for e in events if e.kind == kind]


@pytest.mark.asyncio
async def test_a_completed_run_closes_its_step(execution_db: AsyncSession) -> None:
    events = await _ingest(execution_db, [
        {"event": "run_start", "at": AT, "node_type": "writing"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "save_artifact", "args": {}},
        {"event": "run_end", "at": AT, "status": "completed"},
    ])
    started = _by_kind(events, "step.started")
    completed = _by_kind(events, "step.completed")
    assert len(started) == 1, "根 step 没开出来 —— 本条的前提塌了"
    assert len(completed) == 1, (
        "run 已经完成，卡片还开着 —— 界面会一直显示「正在 writing」"
    )
    assert completed[0]["stepId"] == started[0]["stepId"], "关的不是开的那张卡"


@pytest.mark.asyncio
async def test_a_failed_run_closes_its_step_as_failed(execution_db: AsyncSession) -> None:
    events = await _ingest(execution_db, [
        {"event": "run_start", "at": AT, "node_type": "writing"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "save_artifact", "args": {}},
        {"event": "run_end", "at": AT, "status": "incomplete"},
    ])
    failed = _by_kind(events, "step.failed")
    assert not _by_kind(events, "step.completed"), "没跑完不许说完成"
    assert len(failed) == 1 and failed[0]["errorCode"] == "incomplete", (
        "run 的结论是这一步唯一的结论 —— 不许自己另判一个"
    )


@pytest.mark.asyncio
async def test_a_run_that_opened_no_step_closes_nothing(execution_db: AsyncSession) -> None:
    """一次工具都没调 → 压根没开过卡 → 不许凭空造一张已完成的卡出来。"""
    events = await _ingest(execution_db, [
        {"event": "run_start", "at": AT, "node_type": "writing"},
        {"event": "run_end", "at": AT, "status": "completed"},
    ])
    assert not _by_kind(events, "step.started")
    assert not _by_kind(events, "step.completed")
    assert not _by_kind(events, "step.failed")


@pytest.mark.asyncio
async def test_closing_is_idempotent_across_a_reingest(execution_db: AsyncSession) -> None:
    """同一份 transcript 被重读（重启/续跑常态）不能出第二个结束事件。"""
    records = [
        {"event": "run_start", "at": AT, "node_type": "writing"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "save_artifact", "args": {}},
        {"event": "run_end", "at": AT, "status": "completed"},
    ]
    await _ingest(execution_db, records)
    events = await _ingest(execution_db, records)
    assert len(_by_kind(events, "step.completed")) == 1
