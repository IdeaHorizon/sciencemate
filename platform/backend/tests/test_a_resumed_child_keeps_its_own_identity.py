"""人答复 pause 之后，子节点的工具调用还是它自己的（#947）。

## 现场

同一次 `submit_job`：

* 第一次调用在 Experiment child 上产生 `tool.started(dry_run=false)` 与
  `tool.completed(resultSummary.status=pause)`；
* 人批准之后**真正提交**的第二次调用，产生在**父 project_chat root** 上，
  `resultSummary.status=success`。

于是公开审计无法把真实的受管提交绑定到产出三件套的那条 Experiment run；按
"child 是不是真的产出了它"来验的 benchmark 会正确地失败。

## 机制

子节点身份（`sub_run_id` / `parent_run_id` / `depth`）**只出现在它那份 transcript
的第一条 `run_start` 里**，之后每一行都不带。平台把它记进 `adapter_state`
—— 而 `harness_states` 是 `execute_local_turn` 的局部变量，**每个 turn 清零**。

人答复 pause 是新的一轮：新 turn、空 states、从上次的字节偏移接着读 ——
**那条 `run_start` 不会被重读**。身份没了，事件落回父 run。

同一份 state 里 `dispatchKey` 没事，因为它是从文件身份现算的；身份这一半却随
turn 一起消失。**同一个事实两个半边，一个持久一个易失** —— 分叉时不报错。

## 修法

身份早就落库了：这份文件此前的每一条事件都记着它归谁（`run_id` +
`file_identity`）。新建 adapter_state 时从**那张表**取回来
（`harness_transcript_ingest._child_run_of`），判据与 `_owning_transcript` 同源。

## 判据

这条用例的关键是**跨 turn**：第二次摄取换一个空的 `harness_states`、从字节偏移
接着读 —— 生产里人答复 pause 时就是这个形状。共用一个 states 的话它测不出东西。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService

from tests.test_execution_foundation import (  # noqa: F401  execution_db 是 fixture
    AT,
    _context,
    _runtime_transcript,
    execution_db,
    ingest_transcript_like_production,
)

CHILD_SUB_RUN = "_orchestrator->experiment@d1"


def _child_records(*, with_start: bool, tool_result: str) -> list[dict]:
    start = [{
        "event": "run_start", "at": AT, "node_type": "experiment",
        "sub_run_id": CHILD_SUB_RUN,
        "parent_run_id": "orchestrator__p__session__resume",
        "depth": 1,
    }]
    call = [{
        "event": "tool_call", "at": AT, "name": "submit_job",
        "args": {"scheduler": "local", "dry_run": False},
    }, {
        "event": "tool_result", "at": AT, "name": "submit_job",
        "result": {"status": tool_result},
    }]
    return (start if with_start else []) + call


@pytest.mark.asyncio
async def test_the_approved_retry_stays_on_the_child(execution_db: AsyncSession) -> None:
    """批准前后两次调用都记在 Experiment child 上，父 root 不出现它们。"""
    service = ExecutionIngestService()
    context = _context(session="session-resume", run="run-resume")

    parent = _runtime_transcript(
        "runs/orchestrator__p__session__resume/transcript.jsonl",
        [{"event": "run_start", "at": AT, "node_type": "project_chat"}],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=parent,
        harness_states={},
    )

    # ── 第一轮：子节点起来 + 第一次调用（撞上权限 pause）──────────────────
    child_path = "runs/1789002004-experiment/transcript.jsonl"
    child = _runtime_transcript(child_path, _child_records(with_start=True, tool_result="pause"))
    offset = await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child,
        harness_states={},
    )

    # ── 人批准 → **新的一轮**：states 清零，从字节偏移接着读 ────────────────
    #
    # 生产里就是这个形状。共用同一个 states 的话这条用例测不出任何东西。
    child = _runtime_transcript(
        child_path,
        _child_records(with_start=True, tool_result="pause")
        + _child_records(with_start=False, tool_result="success"),
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child,
        harness_states={}, from_offset=offset,
    )

    rows = (await execution_db.scalars(
        select(ExecutionEvent).where(ExecutionEvent.kind.startswith("tool."))
    )).all()
    by_run: dict[str, list[str]] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append(row.kind)

    child_run = f"{context.run_id}::{CHILD_SUB_RUN}"
    assert child_run in by_run, (
        f"子节点的工具事件一条都没记在它自己名下；实际落在：{sorted(by_run)}"
    )
    assert context.run_id not in by_run, (
        "批准后的重试被记到了父 project_chat root 上 —— 公开审计就没法把真实提交"
        f"绑定到产出三件套的那条 run（#947）。父 root 上的：{by_run.get(context.run_id)}"
    )
    assert by_run[child_run].count("tool.started") == 2, (
        f"两次调用应各留一条 tool.started，实际 {by_run[child_run]}"
    )


@pytest.mark.asyncio
async def test_a_first_turn_child_still_works(execution_db: AsyncSession) -> None:
    """判别力自检：同一轮内读到 `run_start` 的老路径没被改坏。

    没有这一条，上一条可以靠"把所有事件都塞给某条子 run"作弊通过。
    """
    service = ExecutionIngestService()
    context = _context(session="session-first", run="run-first")
    parent = _runtime_transcript(
        "runs/orchestrator__p__session__first/transcript.jsonl",
        [{"event": "run_start", "at": AT, "node_type": "project_chat"}],
    )
    states: dict[str, dict] = {}
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=parent, harness_states=states
    )
    child = _runtime_transcript(
        "runs/1789002005-experiment/transcript.jsonl",
        _child_records(with_start=True, tool_result="success"),
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child, harness_states=states
    )
    rows = (await execution_db.scalars(
        select(ExecutionEvent).where(ExecutionEvent.kind == "tool.started")
    )).all()
    assert {row.run_id for row in rows} == {f"{context.run_id}::{CHILD_SUB_RUN}"}
