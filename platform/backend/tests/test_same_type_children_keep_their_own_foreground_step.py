"""同类型的两个子节点，前台记账各是各的（#1052 框架侧）。

## 现场

翼型会话：Experiment child 起了头没走到结束，随后的 fresh Experiment 派发被摄取层
拒收 —— `record.rejected: overlapping foreground subagent starts cannot be associated
safely`（seq 5158/5166）。新 child 的 pause 还会链到**旧那次**的 step 上。

## 机制

`foregroundSteps` 以 `child_node_type` 为键。「同类型」不等于「同一次派发」：

* 一次派发起了头却没走到结束（run_node 在 `subagent_call_start` 之后提前返回，例如
  `resume_task_identity_mismatch`），同类型下一次派发被判成重叠；
* 后台跑完的同类型子节点按类型结掉了前台那一次的记账，前台那次随后 pause，
  `run.paused` 链不到自己那一步，只能退回 child_run_id；
* 从头重读（重启 / 补事件）时 `subagent_call_paused` 落的是 `run.paused`，重放路径
  不认它，于是 pause 不出账 —— 首读时没有的 `record.rejected` 在重读时凭空出现。

## 修法

harness 的 start / paused / end 三条都带 `dispatch_id`（run_node 每次调用现发）；
平台按它记账，没有它的老 transcript 退回按类型记。实时与重放共用一个键定义。
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
    _ingest,
    _runtime_transcript,
    execution_db,
    ingest_transcript_like_production,
)


def _start(dispatch_id: str | None, *, background: bool = False, note: str = "") -> dict:
    raw = {"event": "subagent_call_start", "at": AT, "child_node_type": "experiment",
           "child_depth": 1, "background": background, "node_inputs_preview": note}
    if dispatch_id:
        raw["dispatch_id"] = dispatch_id
    return raw


def _paused(dispatch_id: str | None, child_run_id: str) -> dict:
    raw = {"event": "subagent_call_paused", "at": AT, "child_node_type": "experiment",
           "child_run_id": child_run_id, "pause_question": "批准真实提交吗？"}
    if dispatch_id:
        raw["dispatch_id"] = dispatch_id
    return raw


def _end(dispatch_id: str | None, child_run_id: str) -> dict:
    raw = {"event": "subagent_call_end", "at": AT, "child_node_type": "experiment",
           "child_run_id": child_run_id, "child_status": "completed"}
    if dispatch_id:
        raw["dispatch_id"] = dispatch_id
    return raw


class _Parent:
    """逐条喂父 transcript；记下每条前台 start 登记的那一步，供后面核对 pause 链到谁。"""

    def __init__(self, db: AsyncSession, session: str) -> None:
        self.db = db
        self.service = ExecutionIngestService()
        self.context = _context(session=session, run=f"run-{session}")
        self.state: dict = {}
        self.offset = 0
        self.started: dict[str, str] = {}

    async def feed(self, raw: dict, *, name: str | None = None) -> None:
        before = set((self.state.get("foregroundSteps") or {}).values())
        await _ingest(self.service, self.db, self.context, raw,
                      offset=self.offset, state=self.state, file_identity="parent")
        self.offset += 100
        if name is not None:
            (step,) = set((self.state.get("foregroundSteps") or {}).values()) - before
            self.started[name] = step

    async def events(self, kind: str) -> list[ExecutionEvent]:
        return list((await self.db.scalars(
            select(ExecutionEvent)
            .where(ExecutionEvent.session_id == self.context.session_id,
                   ExecutionEvent.kind == kind)
            .order_by(ExecutionEvent.sequence))).all())

    async def pause_steps(self) -> list[str]:
        return [str((e.payload or {}).get("stepId")) for e in await self.events("run.paused")]


@pytest.mark.asyncio
async def test_a_fresh_dispatch_is_not_refused_while_an_earlier_one_never_ended(
    execution_db: AsyncSession,
) -> None:
    """翼型现场的形状：前一次没走到结束，fresh 派发照常记账，两次的 pause 各链各的。

    pause 故意倒序到达 —— 按类型配对的话，后到的那次会拿走先到那次的 step。
    """
    parent = _Parent(execution_db, "s-overlap")
    await parent.feed({"event": "run_start", "at": AT, "node_type": "project_chat"})
    await parent.feed(_start("disp_old", note="旧任务"), name="old")
    await parent.feed(_start("disp_fresh", note="fresh 新任务"), name="fresh")
    await parent.feed(_paused("disp_fresh", "exp-fresh"))
    await parent.feed(_paused("disp_old", "exp-old"))
    await execution_db.flush()

    rejected = await parent.events("record.rejected")
    assert not rejected, (
        "fresh 派发被当成与旧那次「重叠」拒收了（#1052 seq 5158/5166）："
        f"{[(e.payload or {}).get('reason') for e in rejected]}")
    assert await parent.pause_steps() == [parent.started["fresh"], parent.started["old"]], (
        "pause 没链回它自己那一次派发的 step")


@pytest.mark.asyncio
async def test_a_background_same_type_end_does_not_close_the_foreground_dispatch(
    execution_db: AsyncSession,
) -> None:
    """后台那一次的结束只结它自己；前台那次之后的 pause 仍链得到自己那一步。"""
    parent = _Parent(execution_db, "s-background")
    await parent.feed({"event": "run_start", "at": AT, "node_type": "project_chat"})
    await parent.feed(_start("disp_bg", background=True))
    await parent.feed(_start("disp_fg"), name="fg")
    await parent.feed(_end("disp_bg", "exp-bg"))
    await parent.feed(_paused("disp_fg", "exp-fg"))
    await execution_db.flush()

    assert await parent.pause_steps() == [parent.started["fg"]], (
        "后台同类型子节点结束时结掉了前台那次的记账 —— 前台的 pause 只能退回 child_run_id")


@pytest.mark.asyncio
async def test_old_transcripts_without_a_dispatch_id_still_pair_by_type(
    execution_db: AsyncSession,
) -> None:
    """老 transcript 的 start / paused / end 都不带 dispatch_id：照旧按类型配对。"""
    parent = _Parent(execution_db, "s-legacy")
    await parent.feed({"event": "run_start", "at": AT, "node_type": "project_chat"})
    await parent.feed(_start(None, note="第一次"), name="first")
    await parent.feed(_end(None, "exp-1"))
    await parent.feed(_start(None, note="第二次"), name="second")
    await parent.feed(_paused(None, "exp-2"))
    await execution_db.flush()

    assert not await parent.events("record.rejected")
    assert await parent.pause_steps() == [parent.started["second"]]


@pytest.mark.parametrize("with_dispatch_id", [True, False], ids=["new-shape", "old-shape"])
@pytest.mark.asyncio
async def test_rereading_the_parent_transcript_adds_nothing(
    execution_db: AsyncSession, with_dispatch_id: bool,
) -> None:
    """重启 / 补事件会从头重读：重读一遍不许多出首读时没有的事件（走生产摄取入口）。

    两次派发各自 pause 过一次。重放路径此前不认 `subagent_call_paused`（它落的是
    `run.paused`），pause 不出账，第二次派发在重读时被判成「重叠」。
    """
    service = ExecutionIngestService()
    tag = "new" if with_dispatch_id else "old"
    context = _context(session=f"session-reread-{tag}", run=f"run-reread-{tag}")
    first, second = ("disp_1", "disp_2") if with_dispatch_id else (None, None)
    transcript = _runtime_transcript(
        f"runs/orchestrator__p__session__reread_{tag}/transcript.jsonl",
        [
            {"event": "run_start", "at": AT, "node_type": "project_chat"},
            _start(first, note="第一次"),
            _paused(first, "exp-1"),
            {"event": "loop_resume", "at": AT},
            _start(second, note="第二次"),
            _paused(second, "exp-2"),
        ],
    )

    async def _all() -> list[tuple[str, str]]:
        rows = (await execution_db.scalars(
            select(ExecutionEvent)
            .where(ExecutionEvent.session_id == context.session_id)
            .order_by(ExecutionEvent.sequence))).all()
        return [(row.id, row.kind) for row in rows]

    first_pass_states: dict[str, dict] = {}
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript,
        harness_states=first_pass_states)
    first_pass = await _all()
    assert "record.rejected" not in {kind for _id, kind in first_pass}, first_pass

    reread_states: dict[str, dict] = {}
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript,
        harness_states=reread_states)
    assert await _all() == first_pass, (
        "从头重读多出了首读时没有的事件 —— 重放与实时的记账不是同一个答案")
    assert [s.get("foregroundSteps") for s in reread_states.values()] == [
        s.get("foregroundSteps") for s in first_pass_states.values()]
