"""执行轨迹：子节点的工具调用必须记在**它自己**那一步下。

## 现场

E2E v23 实测，UI 的 Trace 视图里：

    literature   ✓   （有工具活动）
    hypothesis   ✓   No tool activity was emitted for this step.
    experiment   ✓   No tool activity was emitted for this step.

而 transcript 里 hypothesis 明明有 19 次工具调用。查库：

    step                    tool_calls
    project_chat activity   42          ← literature 18 + hypothesis 19 + 调度器 5
    (其它步)                 0

**归属在投影时就丢了**，不是显示层的问题。

## 根因

根 step 的 id 原来只由 `context.run_id` 派生：

    step_id = f"step_{_hash_parts(context.run_id, attempt_no, 'root')[:24]}"

而**父子 transcript 共用同一个 platform run** —— 子节点算出来的"根 step"和
调度器逐字相同，于是所有工具调用都落到调度器名下。

## 为什么归属只能由子 run 声明

父节点在 `subagent_call_start` 那一刻**不知道**子 run 的 id：`child_run_id`
来自子节点跑完后的 summary（`run_node.py:1484`）。所以父节点没法给出正确的
step id，只有子 run 自己能 —— 而它的 transcript 目录名就是它的 run id
（实测：父记录的 `child_run_id` 与目录名逐字相同）。
"""
from __future__ import annotations

import json
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


def _write(relative: str, records: list[dict]) -> Path:
    return _runtime_transcript(relative, records)


def _steps_and_tools(events: list[ExecutionEvent]) -> tuple[dict[str, str], dict[str, int]]:
    """→ ({stepId: title}, {stepId: 工具调用数})"""
    titles: dict[str, str] = {}
    counts: dict[str, int] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        step_id = str(payload.get("stepId") or "")
        if not step_id:
            continue
        if event.kind == "step.started":
            titles[step_id] = str(payload.get("title") or "")
        if event.kind == "tool.started":
            counts[step_id] = counts.get(step_id, 0) + 1
    return titles, counts


@pytest.mark.asyncio
async def test_child_run_tools_do_not_land_on_the_orchestrator_step(
    execution_db: AsyncSession, tmp_path: Path
) -> None:
    """本次回归的核心：两份 transcript、同一个 platform run，步骤必须分得开。"""
    service = ExecutionIngestService()
    context = _context()
    # 一个 turn 一份 states —— 生产就是这么传的（`execute_local_turn` 的局部
    # 变量）。归属本身不靠它，靠 `_owning_transcript` 查库。
    states: dict[str, dict] = {}

    # 调度器自己的 transcript（第一份 = owning）
    parent = _write(
        "runs/orchestrator__p__session__s/transcript.jsonl",
        [
            {"event": "run_start", "at": AT, "node_type": "project_chat"},
            {"event": "root_step_start", "at": AT, "title": "project_chat activity"},
            {"event": "tool_call", "at": AT, "turn": 1, "name": "run_node", "args": {}},
            {"event": "subagent_call_start", "at": AT, "child_node_type": "hypothesis",
             "child_depth": 1, "background": False},
        ],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=parent, harness_states=states
    )

    # 子节点 hypothesis 的 transcript：目录名 = child_run_id
    child = _write(
        "runs/1786331823-0d8bfc/transcript.jsonl",
        [
            {"event": "run_start", "at": AT, "node_type": "hypothesis"},
            {"event": "root_step_start", "at": AT, "title": "hypothesis"},
            {"event": "tool_call", "at": AT, "turn": 1, "name": "get_research_goal", "args": {}},
            {"event": "tool_call", "at": AT, "turn": 2, "name": "save_artifact", "args": {}},
        ],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child, harness_states=states
    )

    events = list((await execution_db.execute(select(ExecutionEvent))).scalars().all())
    titles, counts = _steps_and_tools(events)

    parent_steps = [sid for sid, t in titles.items() if t == "project_chat activity"]
    child_steps = [sid for sid, t in titles.items() if t == "hypothesis"]
    assert len(parent_steps) == 1 and len(child_steps) == 1

    # 关键：两个 step 的 id 必须不同。相同就是本 bug 的原貌。
    assert parent_steps[0] != child_steps[0], (
        "父子 transcript 共用一个 platform run，step id 不能只由 run_id 派生"
    )
    assert counts.get(child_steps[0]) == 2, "子节点的 2 次工具调用必须记在它自己那一步"
    assert counts.get(parent_steps[0]) == 1, "调度器只调了 1 次工具，不该背子节点的账"


@pytest.mark.asyncio
async def test_two_different_children_get_two_different_steps(
    execution_db: AsyncSession, tmp_path: Path
) -> None:
    """两个子节点不能互相串台 —— 归属键必须是各自的 run id，不是节点名。"""
    service = ExecutionIngestService()
    context = _context()
    states: dict[str, dict] = {}
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, harness_states=states,
        path=_write("runs/orch/transcript.jsonl",
                    [{"event": "run_start", "at": AT, "node_type": "project_chat"},
                     {"event": "root_step_start", "at": AT, "title": "project_chat activity"}]),
    )
    for run_dir, node in (("1786330713-5fb582", "literature"), ("1786331823-0d8bfc", "hypothesis")):
        await ingest_transcript_like_production(
            execution_db, service=service, context=context, harness_states=states,
            path=_write(f"runs/{run_dir}/transcript.jsonl",
                        [{"event": "run_start", "at": AT, "node_type": node},
                         {"event": "root_step_start", "at": AT, "title": node},
                         {"event": "tool_call", "at": AT, "turn": 1, "name": "read_file", "args": {}}]),
        )

    events = list((await execution_db.execute(select(ExecutionEvent))).scalars().all())
    titles, counts = _steps_and_tools(events)
    assert len({sid for sid in titles}) == 3, f"应有 3 个互不相同的 step，实际 {titles}"
    for node in ("literature", "hypothesis"):
        sid = next(s for s, t in titles.items() if t == node)
        assert counts.get(sid) == 1, f"{node} 的工具调用没记在自己名下"


@pytest.mark.asyncio
async def test_orchestrator_only_session_is_unchanged(
    execution_db: AsyncSession, tmp_path: Path
) -> None:
    """没有子节点时行为不变 —— 改动不许把简单场景弄复杂。"""
    service = ExecutionIngestService()
    context = _context()
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, harness_states={},
        path=_write("runs/orch/transcript.jsonl",
                    [{"event": "run_start", "at": AT, "node_type": "project_chat"},
                     {"event": "root_step_start", "at": AT, "title": "project_chat activity"},
                     {"event": "tool_call", "at": AT, "turn": 1, "name": "list_files", "args": {}},
                     {"event": "root_step_end", "at": AT, "status": "completed"}]),
    )
    events = list((await execution_db.execute(select(ExecutionEvent))).scalars().all())
    titles, counts = _steps_and_tools(events)
    assert len(titles) == 1
    step_id = next(iter(titles))
    assert counts.get(step_id) == 1
    # start 与 end 必须配得上同一个 id，否则 UI 上这一步永远不结束
    ends = [e for e in events if e.kind == "step.completed"]
    assert ends and str(ends[0].payload.get("stepId")) == step_id
