"""连续档的代答要自己说出「不是人选的」（#1053 §3a）。

连续（无人值守）档下 `request_human_input` 不停人，按调用方的推荐项代答 —— 这是有意的
（wangd 2026-08-19：连续模式下不应出现任何需要人操作的行为），本测试不动它。

动的是**代答结果长什么样**。此前它是 `{status: success, response, bypassed: True,
asked_by}`，与真人答复回填的 `{status: success, response, asked_by}` 只差一个布尔。
梅雨项目（#1053 §3）里模型据此在终稿写「用户选择了改纯文献机制综述」，而会话消息里
没有任何一条这样的用户消息。

判据落在**模型实际收到的那条 tool 消息**上，走真 agent loop：代答路径带
`answered_by`，真人答复路径没有。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent_loop import resume_loop, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import clear_all, get_paused_run
from core.state import State
from shared.lib import dangerous_commands as dc

bootstrap(force=True)

_ARGS = {
    "question": "数据拿不到，下一步怎么走？",
    "options": [
        {"label": "改纯文献综述", "description": "放弃本研究数据，只做文献层面的机制综述"},
        {"label": "继续找数据源", "description": "换一条获取路径再试"},
    ],
    "recommended_option_index": 0,
}


def _harness() -> NodeHarness:
    return NodeHarness(
        node_type="test_node", version="0.1", system_prompt="test", rules=[],
        guidelines=[], skills=[], expected_outputs={},
        tools=["request_human_input"], max_turns=5, kb_query="_disable")


def _llm() -> MagicMock:
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[{
            "id": "call_ask", "type": "function",
            "function": {"name": "request_human_input",
                         "arguments": json.dumps(_ARGS, ensure_ascii=False)}}],
            finish_reason="tool_calls", usage={"total_tokens": 10}),
        LLMResponse(content="好。", tool_calls=[], finish_reason="stop",
                    usage={"total_tokens": 10}),
    ])
    return llm


def _seen_by_model(messages: list[LLMMessage]) -> dict:
    (msg,) = [m for m in messages if m.role == "tool" and m.tool_call_id == "call_ask"]
    return json.loads(msg.content)


@pytest.mark.asyncio
async def test_a_continuous_mode_answer_carries_who_answered_it() -> None:
    clear_all()
    state = State.new(node_type="test_node", base_dir=Path(tempfile.mkdtemp()))
    messages = [LLMMessage(role="user", content="go")]
    dc.set_bypass_mode(True)          # 连续档每轮由 session_driver.apply_autonomy 施加的就是它
    try:
        result = await run_loop(_harness(), state, messages, _llm())
    finally:
        dc.set_bypass_mode(False)

    assert result.status == "completed", "代答行为不变：连续档不停人"
    seen = _seen_by_model(messages)
    assert seen["response"] == "改纯文献综述"
    assert seen["answered_by"] == "unattended_default"
    assert "不是用户的选择" in seen["answer_note"]

    events = [json.loads(line) for line in
              state.transcript_path.read_text(encoding="utf-8").splitlines()]
    (audit,) = [e for e in events if e.get("event") == "human_input_bypassed"]
    assert audit["answered_by"] == "unattended_default"


@pytest.mark.asyncio
async def test_a_real_user_answer_carries_no_default_provenance() -> None:
    clear_all()
    state = State.new(node_type="test_node", base_dir=Path(tempfile.mkdtemp()))
    messages = [LLMMessage(role="user", content="go")]
    dc.set_bypass_mode(False)

    paused = await run_loop(_harness(), state, messages, _llm())
    assert paused.status == "paused"
    final = await resume_loop(get_paused_run(state.run_id), "继续找数据源")

    assert final.status == "completed"
    seen = _seen_by_model(messages)
    assert seen["response"] == "继续找数据源"
    assert "answered_by" not in seen and "answer_note" not in seen and "bypassed" not in seen
