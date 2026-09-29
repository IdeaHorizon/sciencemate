"""熔断 / 有界重发在 **run_loop 里真的接上了**（issue #184 接线层）。

agent 交回的 `tool_call_recovery` 单元测试证明了判定逻辑对，但判定器接没接进
循环是另一回事 —— #184 的实际损失（~5300 次失败调用、40 分钟空转、conversation
灌到 138K 无法 resume）恰恰是"机制存在但没接到路径上"造成的，跟本周 QC 三连
同源。所以这里用 mock LLM 驱动**真实 run_loop**，锁死接线本身。

关键：`HARNESS_DEFAULT_MAX_TURNS=0`（无限轮）下也必须停 —— 熔断按连续失败
次数计，不看轮数上限。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.agent_loop import run_loop
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _state() -> State:
    return State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()))


@pytest.fixture(autouse=True)
def _register_echo_tool():
    async def _echo(state, **kw):
        return {"status": "success", "echo": kw}
    try:
        register_tool(ToolDefinition(
            name="echo_tool", description="test tool",
            parameters_schema={"type": "object", "properties": {}},
            handler=_echo, node_types=["experiment"],
        ))
    except Exception:
        pass       # 已注册（多次运行）


class _MalformedArgsLLM:
    """每一轮都返回 args JSON 非法的 tool_call —— 复刻 glm 的 7.7KB critique 崩坏。

    这正是 #184 里烧掉 5300 次调用的形态：tool_calls 非空（所以旧代码不当它
    是协议故障），但 args 反序列化必挂，模型也不自我纠正。
    """

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        return LLMResponse(
            content=None,
            tool_calls=[{
                "id": f"c{self.calls}", "type": "function",
                "function": {"name": "echo_tool",
                             "arguments": '{"content": "unterminated'},   # 非法 JSON
            }],
            finish_reason="tool_calls",
            usage={"completion_tokens": 50},
        )


def _harness(max_turns: int = 0) -> NodeHarness:
    return NodeHarness(
        node_type="experiment", system_prompt="sys",
        tools=["echo_tool"], max_turns=max_turns,
    )


def test_breaker_stops_infinite_malformed_args_burn(monkeypatch):
    """核心回归：MAX_TURNS=0（无限）下，连续 args 崩坏必须被熔断停下，
    而不是烧到天荒地老。"""
    monkeypatch.setenv("HARNESS_DEFAULT_MAX_TURNS", "0")   # 无限轮
    state = _state()
    llm = _MalformedArgsLLM()

    result = asyncio.run(run_loop(
        _harness(max_turns=0), state, [LLMMessage(role="user", content="go")], llm))

    # 停了，而且是熔断停的（不是跑完轮数）
    assert result.status == "failed"
    assert llm.calls < 30, f"熔断没生效，调用了 {llm.calls} 次"
    assert "provider" in result.final_text.lower() or "协议" in result.final_text

    events = [json.loads(x) for x in
              state.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    breaks = [e for e in events if e.get("event") == "protocol_circuit_break"]
    assert breaks, "应写 protocol_circuit_break 审计事件"
    assert breaks[-1]["streak"] >= 5


def test_malformed_args_feedback_reaches_model(monkeypatch):
    """有界重发接线：回喂给模型的 tool result 必须带断点摘录 + hint，
    而不是裸 'column 7711'（模型据此无从定位，实测直接放弃）。"""
    monkeypatch.setenv("HARNESS_DEFAULT_MAX_TURNS", "0")
    state = _state()
    llm = _MalformedArgsLLM()
    msgs = [LLMMessage(role="user", content="go")]

    asyncio.run(run_loop(_harness(max_turns=0), state, msgs, llm))

    tool_msgs = [m for m in msgs if m.role == "tool" and m.content]
    assert tool_msgs
    payload = json.loads(tool_msgs[0].content)
    assert payload["status"] == "error"
    # 结构化反馈的三样东西
    assert "excerpt" in payload or "hint" in payload, payload
    assert payload.get("malformed_args") is True or "参数 JSON 解析失败" in str(payload)


class _HealthyLLM:
    """正常干活：先调一次工具，再收尾。绝不能被熔断误伤。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "echo_tool", "arguments": '{"x": 1}'}}],
                finish_reason="tool_calls", usage={"completion_tokens": 20})
        return LLMResponse(content="干完了。", tool_calls=[],
                           finish_reason="stop", usage={"completion_tokens": 10})


def test_healthy_run_not_broken(monkeypatch):
    """正常 run 不受任何影响 —— 熔断/重发都不该误伤（验收硬要求）。"""
    monkeypatch.setenv("HARNESS_DEFAULT_MAX_TURNS", "0")
    state = _state()
    llm = _HealthyLLM()

    result = asyncio.run(run_loop(
        _harness(max_turns=0), state, [LLMMessage(role="user", content="go")], llm))

    assert result.status == "completed"
    assert result.final_text == "干完了。"
    events = [json.loads(x) for x in
              state.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert not [e for e in events if e.get("event") == "protocol_circuit_break"]


class _RecoveringLLM:
    """第一轮 args 崩坏、收到反馈后改好 —— 自我纠正的 run 必须能跑完。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "echo_tool", "arguments": '{"bad": '}}],
                finish_reason="tool_calls", usage={"completion_tokens": 20})
        if self.calls == 2:
            return LLMResponse(
                content=None,
                tool_calls=[{"id": "c2", "type": "function",
                             "function": {"name": "echo_tool", "arguments": '{"ok": 1}'}}],
                finish_reason="tool_calls", usage={"completion_tokens": 20})
        return LLMResponse(content="纠正后完成。", tool_calls=[],
                           finish_reason="stop", usage={"completion_tokens": 10})


def test_self_correcting_run_completes(monkeypatch):
    """一次成功即清零：中途崩一次、改好后继续 → 正常完成，不被熔断。"""
    monkeypatch.setenv("HARNESS_DEFAULT_MAX_TURNS", "0")
    state = _state()
    llm = _RecoveringLLM()

    result = asyncio.run(run_loop(
        _harness(max_turns=0), state, [LLMMessage(role="user", content="go")], llm))

    assert result.status == "completed"
    assert "纠正后完成" in result.final_text
