"""输入框上方那个「当前上下文 xx%」读的是 harness 自报的占用，且只认这个会话自己的。

harness 每次 LLM 响应后写一行 `context_window`（见 core/summarizer.context_window_report）。
这条链要守住两件事：

  1. 那一行原样翻成 `context.updated` 事件：百分比与压缩线所需的字段一个不少，
     窗口不是正数的整条不要（按 0 画出来的"100%"比不画更糟）。
  2. 会话读模型取的是**顶层 run 最新**的一条：子节点各有各的 context，它们的
     占用说的不是"这个会话"；旧的一条也不算数。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService
from app.services.sessions import latest_context_window
from tests.test_execution_foundation import (  # noqa: F401  (execution_db fixture)
    _context,
    _runtime_transcript,
    execution_db,
    ingest_transcript_like_production,
)

AT = "2026-09-19T08:00:00+00:00"


def _report(*, turn: int, effective: int, window: int = 100_000, prompt: int | None = 4321,
            at: str = AT, last: dict | None = None) -> dict:
    return {
        "event": "context_window",
        "at": at,
        "turn": turn,
        "prompt_tokens": prompt,
        "est_tokens": int(effective / 1.3),
        "effective_tokens": effective,
        "calibration": 1.3,
        "window": window,
        "configured_window": 256_000,
        "compress_at": 0.7,
        "emergency_at": 0.9,
        "breakdown": {"system": 1200, "tools": 800, "toolResults": effective - 3000,
                      "summary": 0, "framework": 500, "conversation": 500},
        "n_messages": 7 + turn,
        "last_compaction": last,
    }


@pytest.mark.asyncio
async def test_the_report_line_becomes_a_context_updated_event(
    execution_db: AsyncSession,
) -> None:
    service = ExecutionIngestService()
    context = _context(session="session-ctx", run="run-ctx")
    records = [
        {"event": "run_start", "at": AT, "node_type": "_orchestrator"},
        {"event": "llm_response", "at": AT, "usage": {"prompt_tokens": 4321,
                                                       "completion_tokens": 10,
                                                       "total_tokens": 4331}},
        _report(turn=1, effective=52_000,
                last={"turn": 0, "tokens_before": 90_000, "tokens_after": 40_000,
                      "saved_ratio": 0.55}),
        # 窗口为 0：整条丢，别画一条按 0 算出来的线。
        _report(turn=2, effective=53_000, window=0),
        {"event": "run_end", "at": AT, "status": "completed"},
    ]
    transcript = _runtime_transcript("runs/orch__ctx/transcript.jsonl", records)
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript, harness_states={}
    )

    events = list(
        (
            await execution_db.execute(
                select(ExecutionEvent)
                .where(ExecutionEvent.kind == "context.updated")
                .order_by(ExecutionEvent.sequence)
            )
        ).scalars().all()
    )
    assert len(events) == 1, [event.payload for event in events]
    event = events[0]
    assert event.visibility == "summary"
    assert event.source["rawEvent"] == "context_window"
    assert event.payload == {
        "turn": 1,
        "promptTokens": 4321,
        "estimatedTokens": 40_000,
        "effectiveTokens": 52_000,
        "window": 100_000,
        "configuredWindow": 256_000,
        "compressAt": 0.7,
        "emergencyAt": 0.9,
        "breakdown": {"system": 1200, "tools": 800, "toolResults": 49_000,
                      "summary": 0, "framework": 500, "conversation": 500},
        "messageCount": 8,
        "lastCompaction": {"turn": 0, "tokensBefore": 90_000, "tokensAfter": 40_000},
    }
    # 记花费的那条照旧，没有被这条挤掉（一条 raw 一条事件，两行两条）。
    usage = (await execution_db.execute(
        select(ExecutionEvent).where(ExecutionEvent.kind == "usage.updated")
    )).scalars().all()
    assert len(usage) == 1


@pytest.mark.asyncio
async def test_the_session_reads_the_newest_top_level_report_only(
    execution_db: AsyncSession,
) -> None:
    service = ExecutionIngestService()
    earlier = (datetime.fromisoformat(AT) - timedelta(minutes=5)).isoformat()
    later = (datetime.fromisoformat(AT) + timedelta(minutes=5)).isoformat()

    # 第一轮（顶层）：较早的一条。
    turn1 = _context(session="session-latest", run="run-turn-1")
    await ingest_transcript_like_production(
        execution_db, service=service, context=turn1, harness_states={},
        path=_runtime_transcript("runs/orch__t1/transcript.jsonl", [
            {"event": "run_start", "at": earlier, "node_type": "_orchestrator"},
            _report(turn=1, effective=30_000, at=earlier),
            {"event": "run_end", "at": earlier, "status": "completed"},
        ]),
    )
    # 第二轮（顶层）：最新的一条 —— 会话该读它。
    turn2 = _context(session="session-latest", run="run-turn-2")
    await ingest_transcript_like_production(
        execution_db, service=service, context=turn2, harness_states={},
        path=_runtime_transcript("runs/orch__t2/transcript.jsonl", [
            {"event": "run_start", "at": AT, "node_type": "_orchestrator"},
            _report(turn=3, effective=61_000, at=AT),
        ]),
    )
    # 第二轮派出的子节点：时间更晚、数字更大，但它说的不是这个会话的 context。
    child = replace(_context(session="session-latest", run="run-turn-2__child"),
                    parent_run_id="run-turn-2")
    await ingest_transcript_like_production(
        execution_db, service=service, context=child, harness_states={},
        path=_runtime_transcript("runs/orch__t2/child/transcript.jsonl", [
            {"event": "run_start", "at": later, "node_type": "experiment"},
            _report(turn=1, effective=95_000, at=later),
        ]),
    )
    # 别的会话：不串。
    other = _context(session="session-other", run="run-other")
    await ingest_transcript_like_production(
        execution_db, service=service, context=other, harness_states={},
        path=_runtime_transcript("runs/orch__other/transcript.jsonl", [
            {"event": "run_start", "at": later, "node_type": "_orchestrator"},
            _report(turn=9, effective=99_000, at=later),
        ]),
    )

    got = await latest_context_window(execution_db, session_id="session-latest")
    assert got is not None
    assert got["effectiveTokens"] == 61_000, got
    assert got["turn"] == 3
    assert got["runId"] == "run-turn-2"
    assert got["at"].replace(tzinfo=None) == datetime.fromisoformat(AT).replace(tzinfo=None)

    assert await latest_context_window(execution_db, session_id="session-none") is None
