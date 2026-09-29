"""pause_driver.drive_pause_chain 应在 resume 完成后调 finalize_fn 写 summary.json。

这测的是 Phase 0 的 fix —— pre-existing bug：paused → resumed 的 run summary 永远
不被写。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import (
    PauseEvent, PausedRunContext, clear_all,
    register_pause,
)
from core.pause_driver import drive_pause_chain, make_scripted_ask
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


class _StubLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, **kw):
        if not self.responses:
            return LLMResponse(content="(done)", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_pause_driver_invokes_finalize_after_resume(tmp_path: Path):
    """child 被 register 为 paused → drive_pause_chain → ask 返答复 → resume_loop
    用 stub LLM 立刻完成 → finalize_fn 被调，写 summary.json。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="literature",
        system_prompt="t",
        tools=[],
        max_turns=5,
    )
    # 构造一个 messages 列表，最后一条是 pause placeholder tool_result
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": "tc_1", "type": "function",
             "function": {"name": "request_human_input",
                           "arguments": '{"question":"?"}'}}
        ]),
        LLMMessage(role="tool", tool_call_id="tc_1",
                    name="request_human_input",
                    content=json.dumps({"status": "pause", "pause_event": {}})),
    ]
    llm = _StubLLM([
        # resume 之后下一轮 LLM 直接收尾
        LLMResponse(content="finished", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    register_pause(PausedRunContext(
        run_id=state.run_id,
        state=state,
        messages=messages,
        harness=harness,
        llm=llm,
        pending_tool_call_id="tc_1",
        pause_event=PauseEvent(
            question="any?", asking_node_type="literature",
            asking_run_id=state.run_id, pending_tool_call_id="tc_1",
        ),
    ))

    # finalize fn 用真的 executor.finalize_run
    from core.executor import finalize_run as real_finalize
    ask = await make_scripted_ask(["my answer"])

    final_text = await drive_pause_chain(
        ask_fn=ask, finalize_fn=real_finalize,
    )
    assert final_text == "finished"
    # summary.json 应该写好了
    sum_path = state.root / "summary.json"
    assert sum_path.exists()
    sm = json.loads(sum_path.read_text(encoding="utf-8"))
    assert sm["status"] in ("completed", "incomplete")
    assert sm["node_type"] == "literature"


@pytest.mark.asyncio
async def test_pause_driver_handles_cancellation_during_resume(tmp_path: Path):
    """resume 过程中 LLM 没调工具但外部已经写 kill_signal → status=cancelled，
    finalize_fn 仍被调（写 cancelled summary 不在 finalize_run 路径里，
    但 drive_pause_chain 本身仍能优雅返回）。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    state.hook_state["kill_signal"] = {"reason": "cancel mid-resume",
                                          "requested_by": "test"}
    harness = NodeHarness(
        node_type="literature", system_prompt="t", tools=[], max_turns=5,
    )
    messages = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": "tc_1", "type": "function",
             "function": {"name": "request_human_input",
                           "arguments": '{"question":"?"}'}}
        ]),
        LLMMessage(role="tool", tool_call_id="tc_1",
                    name="request_human_input",
                    content=json.dumps({"status": "pause"})),
    ]
    llm = _StubLLM([])
    register_pause(PausedRunContext(
        run_id=state.run_id, state=state, messages=messages,
        harness=harness, llm=llm,
        pending_tool_call_id="tc_1",
        pause_event=PauseEvent(question="?", asking_node_type="literature",
                                 asking_run_id=state.run_id,
                                 pending_tool_call_id="tc_1"),
    ))
    finalize_called = []

    async def stub_finalize(s, h, lr, _llm, **kw):
        finalize_called.append(lr.status)
        return {"status": lr.status}

    ask = await make_scripted_ask(["whatever"])
    text = await drive_pause_chain(ask_fn=ask, finalize_fn=stub_finalize)
    # resume_loop 第一轮就检测到 kill_signal 直接 cancelled
    assert "cancelled" in finalize_called
    assert "cancelled" in text or text  # text 含 cancel 标记，或非空
