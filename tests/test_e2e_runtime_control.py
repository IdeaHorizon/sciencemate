"""End-to-end integration tests for Phase 1 runtime control.

实际跑 `core.executor.execute_node`（含 register_active / unregister / 写 summary.json）
用一个能在指定 turn 做 side-effect 的 stub LLM 模拟"父 harness 在 child 跑中途
inject/cancel"全链路。

覆盖：
  - execute_node 起 child → register_active → run → unregister + write summary
  - 中途 cancel：parent 写 kill_signal → child 下轮 cancelled → summary.cancelled
  - 中途 inject：parent 写 inject → child 下轮 LLM 看到 system inject message
  - 多条 inject 按顺序消费、消费后 hook_state 清空
  - cancelled child unregister 干净（不泄漏）
  - paused child 仍在 active registry（可被 inject/cancel）
  - inject_into_node 通过工具调用 → child execute_node 端到端响应
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

import pytest

from core.bootstrap import bootstrap
from core.executor import execute_node
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse, is_framework_notice
from core.pause import (
    ActiveRunInfo, clear_all, find_child_state,
    list_active_runs, register_active,
)
from core.state import State
from core.tool_registry import execute as execute_tool


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


class CallbackLLM:
    """Stub LLM：按顺序返预设 responses；可在指定 turn 之**前** LLM call 触发回调
    （让测试代码 emulate 父 inject/cancel）。

    on_turn[N] 在 turn N 的 LLM 调用前执行（已经过 kill/inject 检查）。
    要在 turn N 检查时看到新写入的 hook_state，需要在 on_turn[N-1] 写入。
    """
    def __init__(self, responses: list[LLMResponse],
                  on_turn: dict[int, Callable] | None = None) -> None:
        self.responses = list(responses)
        self.on_turn = on_turn or {}
        self._turn = 0
        self.seen_messages: list[list[LLMMessage]] = []

    async def chat(self, messages, **kw):
        self._turn += 1
        self.seen_messages.append(list(messages))
        cb = self.on_turn.get(self._turn)
        if cb:
            await cb(messages) if asyncio.iscoroutinefunction(cb) else cb(messages)
        if not self.responses:
            return LLMResponse(content="(done)", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


def _node_harness_no_required_outputs() -> NodeHarness:
    """简单 harness：没 required output → run 完成不会 incomplete。"""
    return NodeHarness(
        node_type="literature",
        system_prompt="test harness",
        tools=[],
        max_turns=10,
        required_outputs=[],
        required_output_artifact_types=[],
    )


# ── e2e 1: 完整 lifecycle —— register → run → unregister + summary ─────────

@pytest.mark.asyncio
async def test_e2e_normal_run_registers_and_unregisters(tmp_path: Path,
                                                          monkeypatch):
    """跑一个正常完成的 child：active registry 在 run 中含本 run，结束后清。"""
    harness = _node_harness_no_required_outputs()

    # 拦截 load_harness 让 executor 用我们的 harness
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    # 在 turn 1 检查：本 run 应该在 active registry 里
    seen_active_during_run: list[bool] = []

    def turn1_check(msgs):
        ids = [r.run_id for r in list_active_runs()]
        seen_active_during_run.append(len(ids) == 1)

    llm = CallbackLLM(
        responses=[
            LLMResponse(content="all done", tool_calls=[],
                         finish_reason="stop", usage={}),
        ],
        on_turn={1: turn1_check},
    )

    summary = await execute_node(
        node_type="literature",
        state_dir=tmp_path,
        llm=llm,
    )
    assert summary["status"] == "completed"
    # 跑中确实在 registry
    assert seen_active_during_run == [True]
    # 跑完不在 registry
    assert summary["run_id"] not in [r.run_id for r in list_active_runs()]
    # summary.json 写好了
    sum_path = Path(summary["state_dir"]) / "summary.json"
    assert sum_path.exists()


# ── e2e 2: 中途 cancel 完整链路 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_external_cancel_during_run(tmp_path: Path, monkeypatch):
    """child 跑 turn 1 时父用 cancel_node 写 kill_signal → turn 2 立刻退 cancelled。"""
    harness = _node_harness_no_required_outputs()
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    # 起一个 "parent" state 模拟 orchestrator
    parent_state = State.new(node_type="_orchestrator", base_dir=tmp_path / "parent")

    # turn 1 LLM call 时，用 cancel_node 工具写 kill_signal（child 必须已 active）
    async def turn1_cancel(msgs):
        active = list_active_runs()
        assert len(active) == 1, "child 应该在 active registry"
        child_run_id = active[0].run_id
        result = await execute_tool(
            "runtime_control", parent_state,
            action="cancel",
            child_run_id=child_run_id,
            reasoning="abort for test",
        )
        assert result["status"] == "success"

    # 给 turn 1 一个调工具的 response，让它能进 turn 2
    llm = CallbackLLM(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=[{
                    "id": "tc_1", "type": "function",
                    "function": {"name": "list_artifacts", "arguments": "{}"},
                }],
                finish_reason="tool_calls", usage={},
            ),
        ],
        on_turn={1: turn1_cancel},
    )

    # 启用 list_artifacts 工具（builtin）
    harness.tools = ["list_artifacts"]

    summary = await execute_node(
        node_type="literature",
        state_dir=tmp_path / "child",
        llm=llm,
    )
    assert summary["status"] == "cancelled", f"got {summary}"
    assert summary["cancel_meta"]["reason"] == "abort for test"
    # turn 1 的 LLM 调用期间发的取消 → **本轮工具派发时**就被拦（turn 1）。
    # 这里以前断言 2：旧实现只在轮初查一次 kill_signal，于是取消之后仍会把
    # 本轮已经决定的 list_artifacts 跑完，到下一轮才停。#284 现场是同一条：
    # /stop 之后 data 的恢复链又调了两轮模型。取消检查移进工具/模型派发的咽喉
    # 之后，"取消后不得再发起新调用"变成机械保证。
    assert summary["cancel_meta"]["cancelled_at_turn"] == 1
    assert summary["cancel_meta"]["cancelled_at"] == "tool:list_artifacts"
    # summary.json 写好 + cancel_meta 在里头
    sm = json.loads((Path(summary["state_dir"]) / "summary.json").read_text(encoding="utf-8"))
    assert sm["status"] == "cancelled"
    assert sm["cancel_meta"]["reason"] == "abort for test"
    # active 清干净
    assert summary["run_id"] not in [r.run_id for r in list_active_runs()]


# ── e2e 3: 中途 inject 完整链路 + LLM 看到 ──────────────────────────────

@pytest.mark.asyncio
async def test_e2e_external_inject_during_run(tmp_path: Path, monkeypatch):
    """父中途 inject_into_node → child turn 2 LLM 调用时 messages 含 system inject。"""
    harness = _node_harness_no_required_outputs()
    harness.tools = ["list_artifacts"]
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    parent_state = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")

    async def turn1_inject(msgs):
        child_run_id = list_active_runs()[0].run_id
        await execute_tool(
            "runtime_control", parent_state,
            action="inject",
            child_run_id=child_run_id,
            content="重新做：用 BAOAB 不用 Verlet",
            source="orchestrator_relay",
        )

    llm = CallbackLLM(
        responses=[
            # turn 1: 调工具，让 loop 进 turn 2
            LLMResponse(
                content=None,
                tool_calls=[{
                    "id": "tc_1", "type": "function",
                    "function": {"name": "list_artifacts", "arguments": "{}"},
                }],
                finish_reason="tool_calls", usage={},
            ),
            # turn 2: 收尾
            LLMResponse(content="adjusted", tool_calls=[],
                         finish_reason="stop", usage={}),
        ],
        on_turn={1: turn1_inject},
    )

    summary = await execute_node(
        node_type="literature",
        state_dir=tmp_path / "c",
        llm=llm,
    )
    assert summary["status"] == "completed"
    # turn 2 LLM 看到的 messages 应该含 inject 注入
    turn2_msgs = llm.seen_messages[1]
    sys_msgs = [m for m in turn2_msgs if is_framework_notice(m) or m.role == "system"]
    assert any("BAOAB" in (m.content or "") for m in sys_msgs), \
        f"turn 2 messages 应含 inject: {[m.content[:80] for m in sys_msgs]}"
    assert any("调度器中途注入" in (m.content or "") for m in sys_msgs)


# ── e2e 4: 多条 inject 按序消费 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_multiple_injects_consumed_in_order(tmp_path: Path,
                                                        monkeypatch):
    """同一轮塞多条 inject → child 下轮全看到，按顺序 append 进 messages。"""
    harness = _node_harness_no_required_outputs()
    harness.tools = ["list_artifacts"]
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    parent_state = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")

    async def turn1_inject(_):
        child_id = list_active_runs()[0].run_id
        for content in ["指令 A", "指令 B", "指令 C"]:
            await execute_tool(
                "runtime_control", parent_state,
                action="inject",
                child_run_id=child_id, content=content,
            )

    llm = CallbackLLM(
        responses=[
            LLMResponse(content=None, tool_calls=[
                {"id": "tc_1", "type": "function",
                 "function": {"name": "list_artifacts", "arguments": "{}"}},
            ], finish_reason="tool_calls", usage={}),
            LLMResponse(content="done", tool_calls=[],
                         finish_reason="stop", usage={}),
        ],
        on_turn={1: turn1_inject},
    )

    summary = await execute_node(
        node_type="literature", state_dir=tmp_path / "c", llm=llm,
    )
    assert summary["status"] == "completed"
    turn2_msgs = llm.seen_messages[1]
    inject_contents = [
        m.content for m in turn2_msgs
        if is_framework_notice(m) and "调度器中途注入" in (m.content or "")
    ]
    assert len(inject_contents) == 3
    # 顺序保留
    assert "指令 A" in inject_contents[0]
    assert "指令 B" in inject_contents[1]
    assert "指令 C" in inject_contents[2]


# ── e2e 5: paused child 仍在 active registry（可被 inject）──────────────

@pytest.mark.asyncio
async def test_e2e_paused_child_still_inject_addressable(tmp_path: Path,
                                                           monkeypatch):
    """child 调 request_human_input pause 后，inject_into_node 仍能找到它。"""
    harness = _node_harness_no_required_outputs()
    harness.tools = ["request_human_input"]
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    llm = CallbackLLM(
        responses=[
            LLMResponse(content=None, tool_calls=[
                {"id": "tc_1", "type": "function",
                 "function": {"name": "request_human_input",
                                "arguments": '{"question":"?"}'}},
            ], finish_reason="tool_calls", usage={}),
        ],
    )

    summary = await execute_node(
        node_type="literature", state_dir=tmp_path, llm=llm,
    )
    assert summary["status"] == "paused"
    child_run_id = summary["paused_run_id"]
    # 此时 child 应该既在 active registry 也在 pause registry
    # （find_child_state 会双查）
    assert find_child_state(child_run_id) is not None
    # inject 仍能写入
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    result = await execute_tool(
            "runtime_control", parent,
            action="inject",
            child_run_id=child_run_id,
            content="redirect mid-pause",
    )
    assert result["status"] == "success"
    found = find_child_state(child_run_id)
    assert (found.hook_state.get("injected_messages") or [])[0]["content"] == "redirect mid-pause"


# ── e2e 6: cancel + active registry leak free ────────────────────────────

@pytest.mark.asyncio
async def test_e2e_cancel_does_not_leak_active_registry(tmp_path: Path,
                                                          monkeypatch):
    """连续 10 次 child run 中途 cancel —— registry 不留 stale 项。"""
    harness = _node_harness_no_required_outputs()
    harness.tools = ["list_artifacts"]
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")

    for i in range(10):
        async def turn1_cancel(_):
            cid = [r.run_id for r in list_active_runs()
                    if r.node_type == "literature"][0]
            await execute_tool(
                "runtime_control", parent,
                action="cancel",
                child_run_id=cid, reasoning=f"iter {i} cancel for test",
            )
        llm = CallbackLLM(
            responses=[
                LLMResponse(content=None, tool_calls=[
                    {"id": "tc_1", "type": "function",
                     "function": {"name": "list_artifacts", "arguments": "{}"}},
                ], finish_reason="tool_calls", usage={}),
            ],
            on_turn={1: turn1_cancel},
        )
        s = await execute_node(
            node_type="literature",
            state_dir=tmp_path / f"r{i}",
            llm=llm,
        )
        assert s["status"] == "cancelled"

    # 没有 literature 类型的残留 active
    leaks = [r for r in list_active_runs() if r.node_type == "literature"]
    assert leaks == [], f"leaked: {leaks}"


# ─────────────────────────────────────────────────────────────────────────────
# action='progress' —— 偷看正在跑 child 最近的 transcript 事件（不用等它完成）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runtime_control_progress_returns_recent_events(tmp_path: Path):
    """child 有 experiment_progress 事件时，progress action 能读到最近几条，
    格式化成 stage: detail 摘要（不是让 LLM 自己啃转录 dump）。"""
    child = State.new(node_type="experiment", base_dir=tmp_path / "child")
    child.append_transcript("experiment_progress", stage="turn_start", detail="turn 5/50")
    child.append_transcript("experiment_progress", stage="tool_plan",
                             detail='safe_execute_python [code="import numpy..."]')
    child.append_transcript("experiment_progress", stage="tool_done",
                             detail="safe_execute_python", status="success", returncode=0)
    register_active(ActiveRunInfo(run_id=child.run_id, node_type="experiment",
                                    state=child, started_at="2026-07-14T05:00:00Z"))

    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "parent")
    result = await execute_tool(
        "runtime_control", parent,
        action="progress", child_run_id=child.run_id, n=5,
    )

    assert result["status"] == "success"
    assert result["node_type"] == "experiment"
    assert result["n_events_returned"] == 3
    lines = result["recent_progress"]
    assert any("turn_start" in l and "turn 5/50" in l for l in lines)
    assert any("tool_done" in l for l in lines)


@pytest.mark.asyncio
async def test_runtime_control_progress_unknown_child_errors(tmp_path: Path):
    """不存在的 child_run_id → error，附带当前 active 列表帮 LLM 纠错。"""
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "parent")
    result = await execute_tool(
        "runtime_control", parent,
        action="progress", child_run_id="does-not-exist",
    )
    assert result["status"] == "error"
    assert "active_run_ids" in result


@pytest.mark.asyncio
async def test_runtime_control_progress_requires_child_run_id(tmp_path: Path):
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "parent")
    result = await execute_tool("runtime_control", parent, action="progress")
    assert result["status"] == "error"
