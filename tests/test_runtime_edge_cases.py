"""边界 case：kill/inject 跟 pause/summarizer/特殊内容 的交互。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse, is_framework_notice
from core.pause import (
    PauseEvent, PausedRunContext, clear_all, register_pause,
)
from core.pause_driver import drive_pause_chain, make_scripted_ask
from core.state import State
from core.tool_registry import execute as execute_tool


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


class _StubLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append(list(messages))
        if not self.responses:
            return LLMResponse(content="(done)", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


# ── 1. paused 中被 cancel → resume 立刻 cancelled ────────────────────

@pytest.mark.asyncio
async def test_kill_signal_written_during_pause_then_resume(tmp_path: Path):
    """child paused → 外部写 kill_signal → drive_pause_chain ask 返答复 →
    resume_loop 进入 run_loop turn 1 检查到 kill → 立刻 cancelled。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
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
    llm = _StubLLM([])  # LLM 不应被调（kill 拦截）
    register_pause(PausedRunContext(
        run_id=state.run_id, state=state, messages=messages,
        harness=harness, llm=llm,
        pending_tool_call_id="tc_1",
        pause_event=PauseEvent(question="?", asking_node_type="literature",
                                 asking_run_id=state.run_id,
                                 pending_tool_call_id="tc_1"),
    ))
    # 模拟父在 ask 之前写 kill
    state.hook_state["kill_signal"] = {"reason": "kill in pause",
                                          "requested_by": "test"}

    captured_status = []

    async def stub_finalize(s, h, lr, _llm, **kw):
        captured_status.append(lr.status)
        return {"status": lr.status}

    ask = await make_scripted_ask(["whatever"])
    await drive_pause_chain(ask_fn=ask, finalize_fn=stub_finalize)
    # finalize 看到 cancelled status
    assert "cancelled" in captured_status
    # LLM 没被调
    assert llm.seen == []


# ── 2. inject + summarizer 不会把 inject 整体压走 ─────────────────────

@pytest.mark.asyncio
async def test_inject_survives_summarizer(tmp_path: Path):
    """messages 已经塞满（触发 summarizer）；inject 之后下一轮 LLM 仍能看到。

    用 truncate 策略简单验证：keep_last_n_turns=2 保留末尾，inject append 后
    必在末尾，必保留。
    """
    state = State.new(node_type="literature", base_dir=tmp_path)
    # 配 summarizer 强制触发
    summarizer = SummarizerConfig(
        enabled=True,
        trigger_type="token_threshold",
        trigger_threshold=0.001,  # 极低 → 几乎一定触发
        strategy="truncate",
        keep_last_n_turns=2,
    )
    harness = NodeHarness(
        node_type="literature",
        system_prompt="x",
        tools=[],
        max_turns=3,
        max_context_tokens=1000,
        summarizer=summarizer,
    )
    state.hook_state["injected_messages"] = [
        {"content": "重要 inject 必须看到", "source": "test"},
    ]
    # 制造大量历史 messages 触发压缩
    big_messages = [
        LLMMessage(role="system", content="x" * 50),
        LLMMessage(role="user", content="y" * 50),
        LLMMessage(role="assistant", content="z" * 200),
        LLMMessage(role="user", content="w" * 200),
        LLMMessage(role="assistant", content="v" * 200),
    ]
    llm = _StubLLM([
        LLMResponse(content="done", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    result = await run_loop(harness, state, big_messages, llm)
    assert result.status == "completed"
    seen = llm.seen[0]
    assert any("重要 inject 必须看到" in (m.content or "") for m in seen), \
        f"inject 被压缩丢了: {[m.content[:60] for m in seen]}"


# ── 3. inject 含 JSON / 多行 / 特殊字符不破坏 message ────────────────

@pytest.mark.asyncio
async def test_inject_complex_content(tmp_path: Path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    complex_text = (
        'multi\nline\nwith "quotes" and {json: "obj"} and 中文 emoji 🚀'
        '\n```python\nx=1\n```'
    )
    state.hook_state["injected_messages"] = [
        {"content": complex_text, "source": "test"},
    ]
    harness = NodeHarness(node_type="literature", system_prompt="t",
                            tools=[], max_turns=2)
    llm = _StubLLM([
        LLMResponse(content="done", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    await run_loop(harness, state, [], llm)
    sys_inj = [m for m in llm.seen[0]
               if is_framework_notice(m) and "调度器中途注入" in (m.content or "")]
    assert len(sys_inj) == 1
    assert complex_text in sys_inj[0].content


# ── 4. cancel 跟 inject 同时 → cancel 优先 ───────────────────────────

@pytest.mark.asyncio
async def test_cancel_takes_priority_over_inject(tmp_path: Path):
    """同一 turn 既有 kill_signal 也有 injected_messages → kill 优先，不消费 inject。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    state.hook_state["kill_signal"] = {"reason": "priority", "requested_by": "t"}
    state.hook_state["injected_messages"] = [
        {"content": "shouldn't be seen", "source": "test"},
    ]
    harness = NodeHarness(node_type="literature", system_prompt="t",
                            tools=[], max_turns=2)
    llm = _StubLLM([])
    result = await run_loop(harness, state, [], llm)
    assert result.status == "cancelled"
    # inject 未被消费（仍留在 hook_state；下次 run 还能看到，但本次 cancel）
    assert state.hook_state.get("injected_messages")  # 未被消费


# ── 5. inject 通过 inject_into_node 在 1 个轮之内连续两次都被消费 ────

@pytest.mark.asyncio
async def test_two_injects_then_loop_consumes_both(tmp_path: Path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    from core.pause import ActiveRunInfo, register_active
    register_active(ActiveRunInfo(
        run_id=state.run_id, node_type="literature", state=state,
    ))
    await execute_tool("runtime_control", parent,
                        action="inject",
                        child_run_id=state.run_id, content="A")
    await execute_tool("runtime_control", parent,
                        action="inject",
                        child_run_id=state.run_id, content="B")
    harness = NodeHarness(node_type="literature", system_prompt="t",
                            tools=[], max_turns=2)
    llm = _StubLLM([
        LLMResponse(content="ok", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    await run_loop(harness, state, [], llm)
    seen_sys = [m for m in llm.seen[0]
                if is_framework_notice(m) and "调度器中途注入" in (m.content or "")]
    assert len(seen_sys) == 2
    assert "A" in seen_sys[0].content
    assert "B" in seen_sys[1].content


# ── 6. cancel 在 run_loop 多 turn 跑中后期才到达 ──────────────────────

@pytest.mark.asyncio
async def test_cancel_arriving_at_turn_3_exits_at_turn_4(tmp_path: Path):
    """turn 1/2/3 正常跑 → turn 3 的 LLM call 时写 kill → turn 4 立刻 cancel。"""
    from typing import Callable

    class _CB(_StubLLM):
        def __init__(self, responses, on_turn=None):
            super().__init__(responses)
            self.on_turn = on_turn or {}
            self._turn = 0

        async def chat(self, messages, **kw):
            self._turn += 1
            self.seen.append(list(messages))
            cb = self.on_turn.get(self._turn)
            if cb:
                cb()
            if not self.responses:
                return LLMResponse(content="(done)", tool_calls=[],
                                    finish_reason="stop", usage={})
            return self.responses.pop(0)

    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="literature", system_prompt="t",
        tools=["list_artifacts"], max_turns=10,
    )

    def turn3_kill():
        state.hook_state["kill_signal"] = {
            "reason": "late kill", "requested_by": "test",
        }
    tc_resp = lambda i: LLMResponse(
        content=None, tool_calls=[{
            "id": f"tc_{i}", "type": "function",
            "function": {"name": "list_artifacts", "arguments": "{}"},
        }], finish_reason="tool_calls", usage={},
    )
    llm = _CB(
        responses=[tc_resp(1), tc_resp(2), tc_resp(3)],
        on_turn={3: turn3_kill},
    )
    result = await run_loop(harness, state, [], llm)
    assert result.status == "cancelled"
    assert result.cancel_meta["cancelled_at_turn"] == 4
    # turn 1/2/3 都跑了 LLM；turn 4 没跑
    assert len(llm.seen) == 3


# ── 7. inject 在每次 run_loop 入口都消费一遍（不是只第一次）─────────

@pytest.mark.asyncio
async def test_inject_at_turn_2_seen_at_turn_3(tmp_path: Path):
    """第一轮跑工具 → 第二轮 LLM 完成 → 写 inject 等下次 → 但这次 loop 已完成所以
    inject 等下次 run_loop 调用。验证 hook_state 持久。"""
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(node_type="literature", system_prompt="t",
                            tools=["list_artifacts"], max_turns=5)
    llm = _StubLLM([
        LLMResponse(content=None, tool_calls=[{
            "id": "tc_1", "type": "function",
            "function": {"name": "list_artifacts", "arguments": "{}"},
        }], finish_reason="tool_calls", usage={}),
        LLMResponse(content="done", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    # 在 turn 1 完成前写 inject（手动触发模拟）
    state.hook_state["injected_messages"] = [
        {"content": "late inject", "source": "test"},
    ]
    await run_loop(harness, state, [], llm)
    # 第一轮 LLM call 之前就消费了；turn 2 LLM 仍能看到 inject 因为 messages 持久
    inj_msgs_t1 = [m for m in llm.seen[0]
                    if is_framework_notice(m) and "late inject" in (m.content or "")]
    assert len(inj_msgs_t1) == 1
    # turn 2 LLM 也看到（在 messages 流里）
    inj_msgs_t2 = [m for m in llm.seen[1]
                    if is_framework_notice(m) and "late inject" in (m.content or "")]
    assert len(inj_msgs_t2) == 1


# ── 8. depth tracking 正确（嵌套 child）─────────────────────────────

@pytest.mark.asyncio
async def test_active_registry_tracks_parent_run_id(tmp_path: Path):
    """通过 executor 起 child 时，active info 的 parent_run_id 正确填。"""
    from core.executor import execute_node
    from core import executor as exec_mod

    harness = NodeHarness(node_type="literature", system_prompt="t",
                            tools=[], max_turns=2,
                            required_outputs=[], required_output_artifact_types=[])
    monkey_loaded = lambda nt, nodes_dir=None: harness

    seen_info = []

    class _ChkLLM(_StubLLM):
        async def chat(self, messages, **kw):
            from core.pause import list_active_runs
            seen_info.extend(list_active_runs())
            return await super().chat(messages, **kw)

    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    llm = _ChkLLM([
        LLMResponse(content="done", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    import unittest.mock as _m
    with _m.patch.object(exec_mod, "load_harness", monkey_loaded):
        await execute_node(
            node_type="literature",
            state_dir=tmp_path / "c",
            parent_state=parent,
            depth=1,
            sub_run_id="x->y",
            llm=llm,
        )
    # 跑中至少看到 1 个 active info，含正确 parent_run_id
    assert any(i.parent_run_id == parent.run_id for i in seen_info)
    assert any(i.sub_run_id == "x->y" for i in seen_info)
