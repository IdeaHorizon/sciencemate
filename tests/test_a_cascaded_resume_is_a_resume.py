"""子 run 暂停、恢复、跑完之后，父 run 那一侧发生了什么（#1083）。

## 两件事此前都没发生

**一、没人记下父 run 恢复了。** `loop_resume` 在 core/ 里只有 `resume_loop` 一个
写点，级联这条路不经过它。而平台把 `run.paused` 一律投成 `waiting_human`，能把它
改回 running 的只有 `run.started` / `run.resumed` / `run.retrying`。于是连续档自动
作答之后（那条路全程在 worker 进程里，平台侧补 `loop_resume` 的那一处不参与），
父 run 永远停在「Needs your answer」——**而 worker 里早已没有待答的 pause**。
活体：benchmark pipeline_stop_point，根 run 从 04:52 停到发 stop 也没动。

**二、交给父 run 的是 loop 状态，不是子 run 定稿后的终态。**
`LoopResult.status` 只有 completed/failed/cancelled/void/paused 五种，
`blocked` 和 `incomplete` 的子 run 到父 LLM 眼里一律是 `success`；blockers 丢失、
绑定的 task 不置 blocked、`subagent_call_end` 不写。非暂停路径这些全都做
（`_finish_child`）。**同一个问题两条路答不一样，就一定会分叉** ——
而分叉的方向恰好是「暂停过一次」这件事本身把失败洗成了成功。

判据落在父 run 那一侧看到了什么，不落在子 run 收没收尾（子 run 一直是对的）。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import PauseEvent, PausedRunContext, register_pause
from core.pause_driver import drive_pause_chain, make_scripted_ask
from core.state import State


def _ctx(nt: str, call_id: str, seen: dict, parent: State | None = None) -> State:
    st = State.new(node_type=nt, base_dir=Path(tempfile.mkdtemp()), project_id=None)

    async def chat(messages, **kw):
        for m in messages:
            if m.role == "tool" and m.tool_call_id == "p1" and nt == "project_chat":
                seen.update(json.loads(m.content))
        return LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={})

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=chat)
    msgs = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": call_id, "type": "function",
            "function": {"name": "run_node", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id=call_id, name="run_node",
                   content='{"status":"pause"}'),
    ]
    h = NodeHarness(node_type=nt, version="0.1", system_prompt="", rules=[], guidelines=[],
                    skills=[], expected_outputs={}, tools=[], max_turns=3, kb_query="_disable")
    register_pause(PausedRunContext(
        run_id=st.run_id, state=st, messages=msgs, harness=h, llm=llm,
        pending_tool_call_id=call_id,
        pause_event=PauseEvent(question="?", asking_node_type=nt),
        parent_run_id=parent.run_id if parent else None,
        parent_tool_call_id="p1" if parent else None))
    return st


def _events(st: State) -> list[str]:
    return [json.loads(line)["event"]
            for line in st.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _drive(child_summary_status: str) -> tuple[State, State, dict]:
    bootstrap()
    seen: dict = {}
    root = _ctx("project_chat", "p1", seen)
    child = _ctx("experiment", "c1", seen, parent=root)

    async def finalize(state, h, result, llm, *, depth, sub_run_id):
        return {
            "status": child_summary_status,
            "run_id": state.run_id,
            "blockers": [{"blocker_id": "b1", "category": "missing_input"}],
            "artifacts": [],
            "missing_required_outputs": [],
        }

    async def main():
        await drive_pause_chain(ask_fn=await make_scripted_ask(["2"]), finalize_fn=finalize)

    asyncio.run(main())
    return root, child, seen


def test_the_parent_records_that_it_resumed() -> None:
    """父 run 自己的 transcript 里要有 `loop_resume` —— 平台只认得这个。"""
    root, _child, _seen = _drive("blocked")
    assert "loop_resume" in _events(root), (
        "父 run 恢复了却没留下恢复事件 —— 平台那一侧它会永远停在 waiting_human，"
        f"而 worker 里已经没有待答的 pause。父 run 事件：{_events(root)}"
    )


def test_the_parent_is_told_the_child_was_blocked_not_successful() -> None:
    """子 run 定稿成 blocked，父 LLM 拿到的就不能是 success。"""
    _root, _child, seen = _drive("blocked")
    assert seen, "父 LLM 根本没拿到那条 tool_result"
    assert seen.get("child_status") == "blocked", (
        f"子 run 是 blocked，父 run 看到的却是 {seen.get('child_status')!r}：{seen}")
    assert seen.get("status") != "success", (
        f"blocked 的子 run 在父 LLM 眼里成了 success：{seen}")
    assert seen.get("blockers"), "blockers 在级联路上丢了 —— 父 run 不知道它卡在什么上"


def test_the_cascade_uses_the_same_bookkeeping_as_the_normal_path() -> None:
    """父 transcript 里要有带 child_status 的 `subagent_call_end`。

    那是非暂停路径的收尾记账（`_finish_child`）。级联路以前完全不经过它。
    """
    root, _child, _seen = _drive("blocked")
    lines = [json.loads(line)
             for line in root.transcript_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    ends = [e for e in lines if e.get("event") == "subagent_call_end"]
    assert ends, f"级联收尾没走 _finish_child：{[e.get('event') for e in lines]}"
    assert ends[0].get("child_status") == "blocked", ends[0]


def test_a_child_that_really_completed_is_still_reported_as_success() -> None:
    """对照：真跑完的子 run 不能被这条修复连坐成失败。"""
    _root, _child, seen = _drive("completed")
    assert seen.get("status") == "success", seen
    assert seen.get("child_status") == "completed", seen


def test_without_a_summary_the_degraded_answer_says_so() -> None:
    """拿不到子 run summary 时退回 loop 状态，但必须**说明它是降级的**。

    否则"按 loop 状态编出来的 success"和"子 run 真的成功了"又长得一模一样。
    """
    bootstrap()
    seen: dict = {}
    root = _ctx("project_chat", "p1", seen)
    _child = _ctx("experiment", "c1", seen, parent=root)

    async def main():
        await drive_pause_chain(ask_fn=await make_scripted_ask(["2"]), finalize_fn=None)

    asyncio.run(main())
    assert seen.get("child_summary_unavailable"), seen
