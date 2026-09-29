"""hypothesis runtime_trace hook 单元测试。"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.harness import NodeHarness
from core.llm import LLMResponse
from core.loop_hooks import HookContext, get_loop_hook
from core.state import State

# 触发 register_loop_hook
import nodes.hypothesis.hooks  # noqa: F401


def _ctx(turn: int = 1) -> HookContext:
    state = MagicMock(spec=State)
    state.run_id = "test-run"
    state.hook_state = {}
    harness = NodeHarness(node_type="hypothesis", max_turns=0)
    return HookContext(harness=harness, state=state, messages=[], turn=turn)


def test_runtime_trace_hook_registered() -> None:
    hook = get_loop_hook("runtime_trace")
    assert hook is not None
    assert hook.on_turn_start is not None
    assert hook.on_llm_response is not None
    assert hook.on_turn_end is not None
    assert hook.on_end is not None


def test_runtime_trace_counts_tool_calls() -> None:
    hook = get_loop_hook("runtime_trace")
    assert hook is not None
    ctx = _ctx(turn=2)
    ctx.tool_call_records = [
        {"name": "search_kb", "args": {"q": "x"}, "result": {"status": "ok", "hits": 1}},
        {"name": "save_artifact", "args": {}, "result": {"status": "error", "error": "boom"}},
    ]
    hook.on_turn_end(ctx)
    assert ctx.state.hook_state["runtime_trace_tool_count"] == 2


def test_runtime_trace_on_llm_response_no_tools() -> None:
    hook = get_loop_hook("runtime_trace")
    assert hook is not None
    ctx = _ctx()
    resp = LLMResponse(content="done", finish_reason="stop", tool_calls=[], usage={})
    hook.on_llm_response(ctx, resp)  # should not raise
