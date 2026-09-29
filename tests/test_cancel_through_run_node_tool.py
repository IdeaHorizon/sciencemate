"""child run 通过 run_node 工具起 + 中途 cancel → 父 LLM 看到正确状态。

验证 run_node tool 不把 cancelled child 当 paused 处理（pre-existing 只 case 了 paused）；
cancelled child status 透传给父，父 LLM 能在 tool_result 看到 child_status=cancelled。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.executor import execute_node
from core.harness import NodeHarness
from core.llm import LLMResponse
from core.pause import clear_all, list_active_runs
from core.state import State
from core.tool_registry import execute as execute_tool


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


class _CallbackLLM:
    def __init__(self, responses, on_turn=None):
        self.responses = list(responses)
        self.on_turn = on_turn or {}
        self._turn = 0
        self.seen = []

    async def chat(self, messages, **kw):
        self._turn += 1
        self.seen.append(list(messages))
        cb = self.on_turn.get(self._turn)
        if cb:
            await cb() if callable(cb) else None
        if not self.responses:
            return LLMResponse(content="(done)", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_run_node_tool_propagates_cancelled_status(tmp_path: Path,
                                                          monkeypatch):
    """父用 run_node 工具起子；子中途被 cancel；父拿到 tool_result child_status=cancelled。"""
    # 子 harness：简单，没 required outputs
    child_harness = NodeHarness(
        node_type="literature", system_prompt="t",
        tools=["list_artifacts"], max_turns=5,
        required_outputs=[], required_output_artifact_types=[],
    )
    # 父 harness：能调 run_node + cancel_node
    parent_harness = NodeHarness(
        node_type="_orchestrator", system_prompt="p",
        tools=["run_node", "runtime_control"], max_turns=10,
        callable_nodes=["literature"],
    )

    # 让 load_harness 按 node_type 给对应 harness
    from core import executor as exec_mod
    def fake_loader(nt, nodes_dir=None):
        return parent_harness if nt == "_orchestrator" else child_harness
    monkeypatch.setattr(exec_mod, "load_harness", fake_loader)

    # 子在 turn 1 时被父 cancel —— 但父跟子在同一 run_loop 跑（父 await 子）。
    # 测试策略：直接跑子（不通过 run_node tool）来验 cancelled status；同时验
    # run_node tool 在子完成后能拿到 cancel propagation 逻辑。
    # 这里改用另一策略：起子的同时模拟父调用 cancel（通过 cancel_node tool 写 kill）。

    async def turn1_self_cancel():
        # 子的 turn 1 LLM 调用时，自己（父代理）写 kill_signal 给自己
        # —— 模拟父 cancel_node 调用结果
        active = list_active_runs()
        if active:
            c = active[0]
            c.state.hook_state["kill_signal"] = {
                "reason": "parent cancelled mid-run", "requested_by": "parent",
            }

    child_llm = _CallbackLLM(
        responses=[
            LLMResponse(content=None, tool_calls=[
                {"id": "tc_1", "type": "function",
                 "function": {"name": "list_artifacts", "arguments": "{}"}},
            ], finish_reason="tool_calls", usage={}),
        ],
        on_turn={1: turn1_self_cancel},
    )
    summary = await execute_node(
        node_type="literature",
        state_dir=tmp_path,
        llm=child_llm,
    )
    assert summary["status"] == "cancelled"
    assert summary["cancel_meta"]["reason"] == "parent cancelled mid-run"

    # 现在用 run_node 工具的 _resolve_forward_artifacts 路径不太好直接测；
    # 验证 run_node tool 返的 dict 含 status='cancelled'（透传）—— 通过单独构造
    parent_state = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    # 直接调 run_node tool；让 child 立刻 cancel
    # （load_harness 拦截已生效 → 子用 child_harness）
    cancel_first_child_llm = _CallbackLLM(
        responses=[
            LLMResponse(content=None, tool_calls=[
                {"id": "tc_1", "type": "function",
                 "function": {"name": "list_artifacts", "arguments": "{}"}},
            ], finish_reason="tool_calls", usage={}),
        ],
        on_turn={1: turn1_self_cancel},
    )
    # patch LLMClient 让 run_node tool 内部启的子用我们的 stub
    from core import llm as llm_mod
    orig_llm = llm_mod.LLMClient
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **kw: cancel_first_child_llm)
    parent_state.hook_state["_callable_nodes"] = ["literature"]

    # 直接调 run_node 工具
    result = await execute_tool(
        "run_node", parent_state,
        node_type="literature", user_note="测试派发",
        node_inputs={"research_question": "do it"},
    )
    # tool 应返 child_status='cancelled'，status='cancelled'
    assert result["status"] == "cancelled" or result.get("child_status") == "cancelled"


@pytest.mark.asyncio
async def test_pending_curator_not_registered_for_cancelled_child(tmp_path: Path,
                                                                    monkeypatch):
    """child cancelled 时 run_node 工具不应注册 pending_curator_integrations。"""
    child_harness = NodeHarness(
        node_type="literature", system_prompt="t",
        tools=["list_artifacts"], max_turns=5,
        required_outputs=[], required_output_artifact_types=[],
    )
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda nt, nodes_dir=None: child_harness)

    parent_state = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    parent_state.hook_state["_callable_nodes"] = ["literature"]

    async def cancel_self():
        for r in list_active_runs():
            if r.node_type == "literature":
                r.state.hook_state["kill_signal"] = {
                    "reason": "x", "requested_by": "test",
                }

    cb_llm = _CallbackLLM(
        responses=[
            LLMResponse(content=None, tool_calls=[
                {"id": "tc_1", "type": "function",
                 "function": {"name": "list_artifacts", "arguments": "{}"}},
            ], finish_reason="tool_calls", usage={}),
        ],
        on_turn={1: cancel_self},
    )
    from core import llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **kw: cb_llm)

    await execute_tool(
        "run_node", parent_state,
        node_type="literature", user_note="测试派发", node_inputs={"research_question": "x"},
    )
    pending = parent_state.hook_state.get("pending_curator_integrations") or []
    # cancelled child 不该被注册到 pending
    assert pending == []
