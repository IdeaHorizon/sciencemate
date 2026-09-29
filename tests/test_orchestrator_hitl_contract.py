from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.llm import LLMMessage, LLMResponse
from core.loader import load_harness
from core.pause import clear_all, get_paused_run
from core.state import State

bootstrap(force=True)


class _OneShotLLM:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls = 0

    async def chat(self, _messages, **_kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("The orchestrator unexpectedly requested another LLM turn")
        return self.response


@pytest.fixture(autouse=True)
def _clear_pause_registry():
    clear_all()
    yield
    clear_all()


def test_orchestrator_requires_structured_pause_for_blocking_questions() -> None:
    source = (Path(__file__).parents[1] / "nodes" / "_orchestrator" / "harness.yaml").read_text(
        encoding="utf-8"
    )

    assert "阻塞性追问必须结构化" in source
    assert "同一轮必须调用" in source
    assert "普通正文问号不会创建 pause" in source
    assert "禁止只在普通正文末尾追问后结束" in source
    assert "request_human_input" in source


def test_orchestrator_keeps_internal_work_out_of_user_visible_content() -> None:
    source = (Path(__file__).parents[1] / "nodes" / "_orchestrator" / "harness.yaml").read_text(
        encoding="utf-8"
    )

    assert "用户可见输出合同" in source
    assert "不是工作日志" in source
    assert "简单 QA 只回答问题本身" in source
    assert "不要在正文里写内部计划" in source
    assert "禁止用 `---`" in source


@pytest.mark.asyncio
async def test_real_orchestrator_loop_persists_blocking_question_as_pause(tmp_path: Path) -> None:
    harness = load_harness("_orchestrator")
    assert "request_human_input" in harness.tools
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    llm = _OneShotLLM(
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "call_blocking_scope",
                    "type": "function",
                    "function": {
                        "name": "request_human_input",
                        "arguments": json.dumps(
                            {
                                "question": "请选择本轮要覆盖的研究范围。",
                                "context": "两个范围需要不同的时间和证据预算。",
                                "options": ["窄范围", "宽范围"],
                                # 新契约：给 options 必须给推荐项
                                "recommended_option_index": 0,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
            finish_reason="tool_calls",
            usage={"total_tokens": 12},
        )
    )

    result = await run_loop(
        harness,
        state,
        [LLMMessage(role="user", content="开始，但范围不明确时必须先问我。")],
        llm,
    )

    assert result.status == "paused"
    assert result.pause_event is not None
    assert result.pause_event.pending_tool_call_id == "call_blocking_scope"
    assert result.pause_event.question == "请选择本轮要覆盖的研究范围。"
    assert result.pause_event.options == ["窄范围", "宽范围"]
    assert get_paused_run(state.run_id) is not None
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "human_input_requested" in transcript
    assert "call_blocking_scope" in transcript


@pytest.mark.asyncio
async def test_real_orchestrator_plain_answer_does_not_create_pause(tmp_path: Path) -> None:
    harness = load_harness("_orchestrator")
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    llm = _OneShotLLM(
        LLMResponse(
            content="光合作用把光能转成化学能；这个普通问答不需要中断等待。",
            tool_calls=[],
            finish_reason="stop",
            usage={"total_tokens": 9},
        )
    )

    result = await run_loop(
        harness,
        state,
        [LLMMessage(role="user", content="光合作用是什么？")],
        llm,
    )

    assert result.status == "completed"
    assert result.pause_event is None
    assert get_paused_run(state.run_id) is None
    assert "光合作用" in result.final_text
    assert "human_input_requested" not in state.transcript_path.read_text(encoding="utf-8")
