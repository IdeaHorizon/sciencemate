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

## 记录长成生产那样

`tool_result` 的结果字段叫 `result_preview`（core/agent_loop 写，ingest 读），旁边
还有一份完整序列化 `content`。此前这里的夹具写的是 `result` —— ingest 不读这个键，
于是 pause / success 两个结局在库里都是空摘要，「批准前那次是 pause、批准后那次
是 success」这句话**从来没被检验过**。现在两次调用的结局都要以结构化的
`resultStatus` 落在 child 上。
"""
from __future__ import annotations

import json

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


#: 权限 pause 的 envelope（形状同 core/capability_grants 的出网授权）。问题和
#: 选项一带上就超过 500 字符 —— harness 的 `_brief` 会把它压成截断字符串，
#: 这正是「只能从 resultSummary 里猜」的那个局面。
PAUSE_RESULT = {
    "status": "pause",
    "pause_event": {
        "question": "submit_job 将在本机真实提交作业（dry_run=false），批准吗？",
        "context": "高危操作：真实提交会占用计算资源并写入运行目录。" * 20,
        "header": "高危操作确认",
        "asking_node_type": "experiment",
        "options": ["批准本次", "拒绝"],
        "recommended_option_index": 0,
    },
}
SUCCESS_RESULT = {"status": "success", "job_id": "local-7", "dry_run": False}


def _brief_like_harness(result: dict):
    """`tool_result.result_preview` 的生产形状（core/agent_loop `_brief`）：
    不超 500 字符原样，超了的成功/pause 压成截断字符串。"""
    text = json.dumps(result, ensure_ascii=False, default=str)
    return result if len(text) <= 500 else text[:500] + "...[truncated]"


def _child_records(*, with_start: bool, result: dict, call_id: str) -> list[dict]:
    start = [{
        "event": "run_start", "at": AT, "node_type": "experiment",
        "sub_run_id": CHILD_SUB_RUN,
        "parent_run_id": "orchestrator__p__session__resume",
        "depth": 1,
    }]
    # 字段逐个对 core/agent_loop.py 的两处 `append_transcript`。
    call = [{
        "event": "tool_call", "at": AT, "turn": 1, "name": "submit_job",
        "args": {"scheduler": "local", "dry_run": False},
    }, {
        "event": "tool_result", "at": AT, "turn": 1, "name": "submit_job",
        "result_preview": _brief_like_harness(result),
        "tool_call_id": call_id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
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
    first_turn = _child_records(with_start=True, result=PAUSE_RESULT, call_id="call_1")
    assert isinstance(first_turn[-1]["result_preview"], str), (
        "夹具自检：pause envelope 应被 `_brief` 压成截断字符串，否则测不到难的那种")
    child = _runtime_transcript(child_path, first_turn)
    offset = await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child,
        harness_states={},
    )

    # ── 人批准 → **新的一轮**：states 清零，从字节偏移接着读 ────────────────
    #
    # 生产里就是这个形状。共用同一个 states 的话这条用例测不出任何东西。
    child = _runtime_transcript(
        child_path,
        first_turn
        + _child_records(with_start=False, result=SUCCESS_RESULT, call_id="call_2"),
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child,
        harness_states={}, from_offset=offset,
    )

    rows = (await execution_db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.kind.startswith("tool."))
        .order_by(ExecutionEvent.sequence)
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
    # 两次调用的**结局**也都在 child 上，且按发生顺序：先 pause（不算执行），
    # 批准后那次才是 success。结局从结构化字段读，不从可能截断的摘要里猜。
    outcomes = [
        (row.payload or {}).get("resultStatus")
        for row in rows
        if row.run_id == child_run and row.kind == "tool.completed"
    ]
    assert outcomes == ["pause", "success"], (
        f"child 上两次 submit_job 的结局应依次是 pause → success，实际 {outcomes}"
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
        _child_records(with_start=True, result=SUCCESS_RESULT, call_id="call_1"),
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child, harness_states=states
    )
    rows = (await execution_db.scalars(
        select(ExecutionEvent).where(ExecutionEvent.kind == "tool.started")
    )).all()
    assert {row.run_id for row in rows} == {f"{context.run_id}::{CHILD_SUB_RUN}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "tool_result", "expected"),
    [
        # 老 transcript（#734 之前）没有 `content`：只剩截断预览，从它开头认 envelope。
        ("老记录只有截断预览",
         {"result_preview": _brief_like_harness(PAUSE_RESULT)}, "pause"),
        # 预览是截断串、`content` 是完整的：以完整那份为准（status 不在开头也认得）。
        ("完整序列化里 status 不在开头",
         {"result_preview": "…[truncated]",
          "content": json.dumps({"job_id": "j", "status": "success", "pad": "x" * 600})},
         "success"),
        # 结果不是 envelope（列表/纯文本）：判不出就不写 —— 没有 ≠ 成功。
        ("结果不是 envelope", {"result_preview": ["a", "b"], "content": '["a", "b"]'}, None),
    ],
)
async def test_the_outcome_is_read_from_the_envelope_not_guessed(
    execution_db: AsyncSession, label: str, tool_result: dict, expected: str | None,
) -> None:
    """`resultStatus` 取自工具 envelope；截断与老形状都不让它退回去猜摘要。"""
    service = ExecutionIngestService()
    context = _context(session=f"session-outcome-{abs(hash(label))}", run="run-outcome")
    transcript = _runtime_transcript(
        f"runs/outcome-{abs(hash(label))}/transcript.jsonl",
        [
            {"event": "run_start", "at": AT, "node_type": "project_chat"},
            {"event": "tool_call", "at": AT, "turn": 1, "name": "submit_job",
             "args": {"dry_run": False}},
            {"event": "tool_result", "at": AT, "turn": 1, "name": "submit_job",
             "tool_call_id": "call_1", **tool_result},
        ],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript,
        harness_states={},
    )
    completed = (await execution_db.scalars(
        select(ExecutionEvent).where(
            ExecutionEvent.session_id == context.session_id,
            ExecutionEvent.kind == "tool.completed",
        )
    )).all()
    assert len(completed) == 1, f"{label}: 应有一条 tool.completed，实际 {len(completed)}"
    payload = completed[0].payload or {}
    if expected is None:
        assert "resultStatus" not in payload, f"{label}: 判不出时不该编一个 {payload}"
    else:
        assert payload.get("resultStatus") == expected, f"{label}: {payload}"
