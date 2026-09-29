"""进展熔断必须在**真的 agent loop 里**生效，不只是类自己好使。

两次教训都在同一处：机制写对了但没接到路径上，单元测试全绿、生产照旧
（委派登记埋错位置、回声剥离按位置取证据）。所以这条跑真 run_loop。

回放 2026-08-13 v26：模型每轮返回一字不差的同一句话 + 同一个工具调用，
协议全程正常、不算空轮、参数缓存也对不上 —— 现有三道防线全穿过去，
最终 1482 轮 / 117M tokens。
"""
from __future__ import annotations

import json

import pytest

from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


class _BrokenRecordLLM:
    """永远返回同一个回复 —— 温度为零 + 输入不变的真实形态。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        return LLMResponse(
            content="I need to break the scratchpad loop.",
            tool_calls=[{
                "id": "call_same",
                "type": "function",
                "function": {"name": "list_artifacts", "arguments": "{}"},
            }],
            finish_reason="tool_calls",
            usage={},
        )


@pytest.mark.asyncio
async def test_a_repeating_loop_is_cut_by_the_real_agent_loop(tmp_path):
    harness = NodeHarness(
        node_type="literature",
        system_prompt="t",
        tools=["list_artifacts"],
        max_turns=60,          # 兜底：熔断没接上时测试会撞到这里，而不是跑到天荒地老
    )
    state = State.new("literature", tmp_path / "rt", project_id="p")
    llm = _BrokenRecordLLM()

    result = await run_loop(harness, state, [LLMMessage(role="user", content="go")], llm)

    assert result.status == "failed"
    assert result.turns < 15, f"熔断没接上：跑了 {result.turns} 轮才停"
    assert "逐字节相同" in result.final_text

    lines = [json.loads(line) for line in
             state.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [line for line in lines if line.get("event") == "progress_circuit_break"]
    assert events, "停机必须留痕，否则没人知道为什么停"
    assert events[0]["repeat_streak"] >= 2


@pytest.mark.asyncio
async def test_a_healthy_run_still_finishes_normally(tmp_path):
    """反向不变量：正常干活的 run 不能被这道门误伤。"""

    class _WorkingLLM:
        def __init__(self):
            self.turn = 0

        async def chat(self, messages, **kw):
            self.turn += 1
            if self.turn > 4:
                return LLMResponse(content="做完了。", tool_calls=[],
                                   finish_reason="stop", usage={})
            return LLMResponse(
                content=f"第 {self.turn} 步",
                tool_calls=[{
                    "id": f"c{self.turn}",
                    "type": "function",
                    "function": {"name": "list_artifacts",
                                 "arguments": f'{{"artifact_type": "t{self.turn}"}}'},
                }],
                finish_reason="tool_calls",
                usage={},
            )

    harness = NodeHarness(node_type="literature", system_prompt="t",
                          tools=["list_artifacts"], max_turns=20)
    state = State.new("literature", tmp_path / "rt2", project_id="p")

    result = await run_loop(harness, state, [LLMMessage(role="user", content="go")], _WorkingLLM())

    assert result.status == "completed"
    assert result.final_text == "做完了。"
