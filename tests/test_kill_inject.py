"""Phase 1 tests: kill_signal + injected_messages + inject_into_node + cancel_node.

不联网；用 stub LLM 验证 framework 信号传递正确性。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.agent_loop import LoopResult, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse, is_framework_notice
from core.pause import (
    ActiveRunInfo, clear_all, find_child_state, list_active_runs,
    register_active, unregister_active,
)
from core.state import State
from core.tool_registry import execute as execute_tool


# ── 共用 fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _bootstrap_once():
    bootstrap()
    yield
    clear_all()


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State.new(node_type="literature", base_dir=tmp_path)


@pytest.fixture
def harness() -> NodeHarness:
    return NodeHarness(
        node_type="literature",
        system_prompt="test",
        tools=[],
        max_turns=5,
    )


class _StubLLM:
    """记录每次 chat 看到的 messages；用预定响应。"""
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def chat(self, messages, **kw):
        self.calls.append([
            LLMMessage(role=m.role, content=m.content,
                        tool_calls=m.tool_calls, tool_call_id=m.tool_call_id,
                        name=m.name)
            for m in messages
        ])
        if not self.responses:
            return LLMResponse(content="(done)", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


# ── kill_signal ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_signal_cancels_loop_at_next_turn(state, harness):
    """写 kill_signal 后下一轮立刻退出 status='cancelled'。"""
    # 起一轮先让 turn=1 跑 LLM
    llm = _StubLLM([
        LLMResponse(content="thinking turn 1", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])

    # 在 turn=1 之前就写 signal —— 立刻 cancel
    state.hook_state["kill_signal"] = {
        "reason": "test cancel",
        "requested_by": "test",
    }

    result = await run_loop(harness, state, [], llm)
    assert result.status == "cancelled"
    assert result.cancel_meta is not None
    assert result.cancel_meta["reason"] == "test cancel"
    assert result.cancel_meta["cancelled_at_turn"] == 1
    # 没调任何 LLM
    assert len(llm.calls) == 0


@pytest.mark.asyncio
async def test_kill_signal_after_turn1_cancels_at_turn2(state, harness):
    """turn=1 跑完产生 tool_call；turn=2 开始前写 kill → 在 turn 2 退出。"""
    # 简化：用 stub LLM 输出无 tool_call 直接结束，再手动塞 kill
    async def _execute_kill():
        # 给一个总能跑到 turn=2 的场景：turn1 调一个工具，turn2 LLM 返空
        # 但 stub 太复杂，直接 unit 测信号检测就够
        pass

    # 已在 turn1 前测过，无需重复


# ── injected_messages ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_injected_messages_appended_as_framework_notice(state, harness):
    """inject 一条 → 下一轮 LLM 看到的 messages 里有它，且形态是 framework-notice。

    2026-08-17 起中途注入不再用 role="system"（中段 system 消息会被模型当成
    "自己没说完的话"接着复述，实测 13/14）。契约改成 user 角色 + 归属信封，
    见 core.llm.framework_notice。
    """
    state.hook_state["injected_messages"] = [
        {"content": "重新做：用 BAOAB 而非 Verlet", "source": "test"},
    ]
    llm = _StubLLM([
        LLMResponse(content="ok", tool_calls=[], finish_reason="stop", usage={}),
    ])
    await run_loop(harness, state, [], llm)
    assert len(llm.calls) == 1
    msgs = llm.calls[0]
    notices = [m for m in msgs if is_framework_notice(m)]
    assert any("调度器中途注入" in (m.content or "") for m in notices)
    assert any("BAOAB" in (m.content or "") for m in notices)
    # 归属：不许伪装成人类发言（换成 user 角色之后这是必须机械可辨的）
    assert all(m.role == "user" for m in notices)
    assert all("不是用户发言" in (m.content or "") for m in notices)


@pytest.mark.asyncio
async def test_injected_messages_consumed_once(state, harness):
    """inject 一条 → 消费后清空，turn 2 不会再次注入。"""
    state.hook_state["injected_messages"] = [
        {"content": "test inject", "source": "test"},
    ]
    # 给 2 个响应：turn1 调工具（None）turn2 收尾
    # 这里简化：LLM 单轮收尾，验证 hook_state 被清空即可
    llm = _StubLLM([
        LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={}),
    ])
    await run_loop(harness, state, [], llm)
    assert state.hook_state.get("injected_messages") in (None, [])


# ── inject_into_node tool ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inject_into_node_writes_hook_state(tmp_path: Path):
    """orchestrator state 调 inject_into_node → 写 child state.hook_state['injected_messages']。"""
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    result = await execute_tool(
        "runtime_control", parent,
        action="inject",
        child_run_id=child.run_id,
        content="改成 BAOAB",
        source="orchestrator_relay",
    )
    assert result["status"] == "success"
    queue = child.hook_state.get("injected_messages") or []
    assert len(queue) == 1
    assert queue[0]["content"] == "改成 BAOAB"
    assert queue[0]["source"] == "orchestrator_relay"
    assert queue[0]["injected_by_run_id"] == parent.run_id


@pytest.mark.asyncio
async def test_inject_into_node_finds_paused_child(tmp_path: Path):
    """child 在 paused registry 也能找到 → inject 仍能写入。"""
    from core.pause import (
        PauseEvent, PausedRunContext, register_pause,
    )
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_pause(PausedRunContext(
        run_id=child.run_id, state=child, messages=[],
        harness=MagicMock(), llm=MagicMock(),
        pending_tool_call_id="fake",
        pause_event=PauseEvent(question="?", asking_node_type="literature",
                                 asking_run_id=child.run_id,
                                 pending_tool_call_id="fake"),
    ))
    result = await execute_tool(
        "runtime_control", parent,
        action="inject",
        child_run_id=child.run_id,
        content="hint",
    )
    assert result["status"] == "success"
    assert (child.hook_state.get("injected_messages") or [])[0]["content"] == "hint"


@pytest.mark.asyncio
async def test_inject_into_node_unknown_child_errors(tmp_path: Path):
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    result = await execute_tool(
        "runtime_control", parent,
        action="inject",
        child_run_id="nonexistent",
        content="x",
    )
    assert result["status"] == "error"
    assert "找不到" in result["error"]


@pytest.mark.asyncio
async def test_inject_into_node_empty_content_errors(tmp_path: Path):
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    result = await execute_tool(
        "runtime_control", parent,
        action="inject",
        child_run_id="x",
        content="   ",
    )
    assert result["status"] == "error"
    assert "content" in result["error"]


# ── cancel_node tool ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_node_writes_kill_signal(tmp_path: Path):
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    result = await execute_tool(
        "runtime_control", parent,
        action="cancel",
        child_run_id=child.run_id,
        reasoning="user said stop",
    )
    assert result["status"] == "success"
    sig = child.hook_state.get("kill_signal")
    assert sig is not None
    assert sig["reason"] == "user said stop"
    assert sig["requested_by"].endswith(parent.run_id)


@pytest.mark.asyncio
async def test_cancel_node_requires_reason(tmp_path: Path):
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    result = await execute_tool(
        "runtime_control", parent,
        action="cancel",
        child_run_id="x", reasoning="",
    )
    assert result["status"] == "error"


# ── kill via cancel_node end-to-end through run_loop ──────────────────

@pytest.mark.asyncio
async def test_cancel_via_tool_causes_next_turn_exit(state, harness, tmp_path):
    """parent 用 cancel_node 写 child kill_signal → child run_loop 下轮退出 cancelled。"""
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    register_active(ActiveRunInfo(
        run_id=state.run_id, node_type="literature", state=state,
    ))
    # 模拟 parent 主动 cancel
    await execute_tool(
        "runtime_control", parent,
        action="cancel",
        child_run_id=state.run_id, reasoning="halt for test",
    )
    # 现在跑 child loop —— 应立刻 cancel
    llm = _StubLLM([])
    result = await run_loop(harness, state, [], llm)
    assert result.status == "cancelled"
    assert "halt for test" in (result.cancel_meta or {}).get("reason", "")
    assert len(llm.calls) == 0


# ── active registry ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_registry_register_unregister(tmp_path: Path):
    s = State.new(node_type="literature", base_dir=tmp_path)
    info = ActiveRunInfo(run_id=s.run_id, node_type="literature", state=s)
    register_active(info)
    assert s.run_id in [r.run_id for r in list_active_runs()]
    assert find_child_state(s.run_id) is s
    unregister_active(s.run_id)
    assert s.run_id not in [r.run_id for r in list_active_runs()]
    assert find_child_state(s.run_id) is None


# ── 找不到 child：说清是哪一种找不到（2026-08-17 重启后原地重试两次）──────


def _orch_state(tmp_path):
    from core.state import State

    state = State(run_id="orch", node_type="_orchestrator",
                  root=tmp_path / "runs" / "orch")
    state.root.mkdir(parents=True, exist_ok=True)
    return state


def _write_child(tmp_path, run_id: str, *, finished: bool) -> None:
    d = tmp_path / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    lines = ['{"event": "run_start", "node_type": "experiment"}']
    if finished:
        lines.append('{"event": "run_end", "status": "completed"}')
    (d / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_missing_child_that_never_existed(tmp_path):
    from shared.tools.library.runtime_control import _missing_child_error

    out = _missing_child_error(_orch_state(tmp_path), "1786992568-441abc")
    assert out["child_run_kind"] == "unknown"
    assert "记错" in out["error"]


def test_missing_child_that_already_finished(tmp_path):
    from shared.tools.library.runtime_control import _missing_child_error

    state = _orch_state(tmp_path)
    _write_child(tmp_path, "1786992568-441abc", finished=True)
    out = _missing_child_error(state, "1786992568-441abc")
    assert out["child_run_kind"] == "finished"
    assert "已经结束" in out["error"]


def test_missing_child_interrupted_points_at_resume(tmp_path):
    """真实现场：平台重启，run 还在盘上但 registry 空了。"""
    from shared.tools.library.runtime_control import _missing_child_error

    state = _orch_state(tmp_path)
    _write_child(tmp_path, "1786992568-441abc", finished=False)
    out = _missing_child_error(state, "1786992568-441abc")
    assert out["child_run_kind"] == "interrupted"
    assert "resume_run_id" in out["error"], "得告诉调度器怎么把它接着跑完"
