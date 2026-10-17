"""父 transcript 里一次派发的三条事件带同一个 `dispatch_id`（#1052）。

`subagent_call_start` 早就带着 `dispatch_id`（#1080 第 4 条），而 `subagent_call_paused`
和 `subagent_call_end` 不带。平台只好按 `child_node_type` 把「结束」配回「开始」——
同类型不等于同一次派发：

* 后台跑完的同类型子节点按类型结掉了**正在前台跑**的那一次的记账，前台那次随后
  pause，就链不到自己那一步；
* 一次派发起了头却没走到结束（`subagent_call_start` 之后提前返回），同类型的下一次
  fresh 派发被当成「重叠」拒收，新 child 的 pause 还链到旧那次的 step 上。

判据：四条会写这两个事件的路（前台结束 / 前台 pause / 后台结束 / 级联恢复后结束）
写出来的 `dispatch_id` 都等于这次派发 `subagent_call_start` 上的那一个。

只桩「子 run 内部怎么跑」（`_execute_with_infra_retry`），其余走真代码。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.state import State


def _state(tmp_path: Path) -> State:
    from core.bootstrap import bootstrap
    from core.loader import load_harness

    bootstrap()
    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="p_local")
    st.project_root = tmp_path / "proj"
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    st.hook_state["_callable_nodes"] = list(load_harness("_orchestrator").callable_nodes or [])
    return st


def _parent_events(st: State, *names: str) -> list[dict]:
    lines = st.transcript_path.read_text(encoding="utf-8").splitlines()
    return [e for e in (json.loads(x) for x in lines if x.strip()) if e.get("event") in names]


def _summary(run_id: str, state_dir: Path, status: str = "completed") -> dict:
    """与真实子 run 同形的 summary（键取自 `_finish_child` 实际读的集合）。"""
    return {
        "run_id": run_id, "node_type": "literature", "status": status, "turns": 2,
        "state_dir": str(state_dir), "artifacts": [], "final_text_preview": "done",
        "started_at": run_id, "missing_required_outputs": [],
        "missing_input_artifact_types": [], "unverified_artifacts": [],
        "memory_candidates": [], "blockers": [], "project_workspace": {"paths": []},
    }


@pytest.fixture
def child_exec(tmp_path, monkeypatch):
    """桩掉子 run 内部；记下每次拿到的 `parent_dispatch_id`，按队列给结局。"""
    from shared.tools import run_node as rn

    seen: list[str | None] = []
    outcomes: list[str] = []

    async def fake_exec(state, node_type, exec_kwargs):
        seen.append(exec_kwargs.get("parent_dispatch_id"))
        rid = f"r_{node_type}_{len(seen)}"
        d = tmp_path / rid
        d.mkdir(parents=True, exist_ok=True)
        if (outcomes.pop(0) if outcomes else "completed") == "paused":
            return {**_summary(rid, d, "paused"), "paused_run_id": rid,
                    "pause_event": {"question": "批准真实提交吗？"}}
        return _summary(rid, d)

    monkeypatch.setattr(rn, "_execute_with_infra_retry", fake_exec)
    return seen, outcomes


@pytest.mark.asyncio
async def test_a_foreground_end_names_the_dispatch_that_started_it(tmp_path, child_exec):
    from shared.tools.run_node import _run_node_tool

    seen, _ = child_exec
    st = _state(tmp_path)
    await _run_node_tool(st, "literature", node_inputs={"research_question": "a"},
                         user_note="先查文献。")
    await _run_node_tool(st, "literature", node_inputs={"research_question": "b"},
                         user_note="再查一轮。")

    starts = _parent_events(st, "subagent_call_start")
    ends = _parent_events(st, "subagent_call_end")
    assert len(starts) == len(ends) == 2
    assert [e.get("dispatch_id") for e in ends] == [s["dispatch_id"] for s in starts], (
        f"subagent_call_end 没带派发身份或带错了：{[e.get('dispatch_id') for e in ends]}")
    assert starts[0]["dispatch_id"] != starts[1]["dispatch_id"], "每次派发一个身份，不复用"
    assert seen == [s["dispatch_id"] for s in starts], "子 run 拿到的不是同一个身份"


@pytest.mark.asyncio
async def test_a_foreground_pause_names_the_dispatch_that_started_it(tmp_path, child_exec):
    from shared.tools.run_node import _run_node_tool

    _, outcomes = child_exec
    outcomes.append("paused")
    st = _state(tmp_path)
    out = await _run_node_tool(st, "literature", node_inputs={"research_question": "a"},
                               user_note="查文献。")
    assert out.get("status") == "pause", out

    (start,) = _parent_events(st, "subagent_call_start")
    (paused,) = _parent_events(st, "subagent_call_paused")
    assert paused.get("dispatch_id") == start["dispatch_id"], (
        f"pause 链不回它那一次派发：start={start['dispatch_id']} paused={paused}")


@pytest.mark.asyncio
async def test_a_background_end_names_its_own_dispatch(tmp_path, child_exec):
    """后台那一次的结束只结它自己 —— 按类型配对正是它把前台那次结掉的原因。"""
    from shared.tools import run_node as rn

    st = _state(tmp_path)
    out = await rn._run_node_tool(
        st, "literature", node_inputs={"research_question": "bg"}, user_note="后台查着。",
        background=True)
    assert out.get("status") == "started_background", out
    await asyncio.gather(*list(rn._BACKGROUND_TASKS))

    (start,) = _parent_events(st, "subagent_call_start")
    (end,) = _parent_events(st, "subagent_call_end")
    assert start.get("background") is True
    assert end.get("dispatch_id") == start["dispatch_id"], end


def test_a_cascaded_end_names_the_dispatch_of_the_paused_child() -> None:
    """子 run pause → 人答复 → 级联恢复跑完：父这边补写的 `subagent_call_end` 也要带
    当初那次派发的身份（它在子 State 的 `parent_dispatch_id` 上）。"""
    from core.bootstrap import bootstrap
    from core.harness import NodeHarness
    from core.llm import LLMMessage, LLMResponse
    from core.pause import PausedRunContext, PauseEvent, register_pause
    from core.pause_driver import drive_pause_chain, make_scripted_ask

    bootstrap()

    def _paused(nt: str, call_id: str, parent: State | None) -> State:
        st = State.new(node_type=nt, base_dir=Path(tempfile.mkdtemp()), project_id=None)
        if parent is not None:
            st.parent_dispatch_id = "disp_the_one_that_paused"
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(
            content="done", tool_calls=[], finish_reason="stop", usage={}))
        h = NodeHarness(node_type=nt, version="0.1", system_prompt="", rules=[],
                        guidelines=[], skills=[], expected_outputs={}, tools=[],
                        max_turns=3, kb_query="_disable")
        register_pause(PausedRunContext(
            run_id=st.run_id, state=st, harness=h, llm=llm, pending_tool_call_id=call_id,
            messages=[
                LLMMessage(role="user", content="go"),
                LLMMessage(role="assistant", content="", tool_calls=[{
                    "id": call_id, "type": "function",
                    "function": {"name": "run_node", "arguments": "{}"}}]),
                LLMMessage(role="tool", tool_call_id=call_id, name="run_node",
                           content='{"status":"pause"}'),
            ],
            pause_event=PauseEvent(question="?", asking_node_type=nt),
            parent_run_id=parent.run_id if parent else None,
            parent_tool_call_id="p1" if parent else None))
        return st

    root = _paused("project_chat", "p1", None)
    _paused("literature", "c1", root)

    async def finalize(state, h, result, llm, *, depth, sub_run_id):
        return _summary(state.run_id, Path(state.root))

    async def main():
        await drive_pause_chain(ask_fn=await make_scripted_ask(["2"]), finalize_fn=finalize)

    asyncio.run(main())
    (end,) = _parent_events(root, "subagent_call_end")
    assert end.get("dispatch_id") == "disp_the_one_that_paused", end
