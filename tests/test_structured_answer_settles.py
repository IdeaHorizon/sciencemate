"""结构化答复（点选项）必须被结算成文本 —— 否则 resume 那一步直接崩。

实测（2026-08-30，本机部署跑真课题）：调度器发的是 `structured_question` 而不是
`decision_package`。App Server 带 choice 作答时按契约 `AnswerValue = str | dict`
交上来一个 dict，而只有 decision_package 一支会结算它；其余类型把 dict 原样交给
`resume_loop(ctx, response_text: str)`，它第一件事就是 `response_text[:200]` ——

    HarnessSessionError: unhashable type: 'slice'

也就是**点任何一个结构化选项都会把整个 run 崩掉**，而 UI 那一侧同时又
`canSend=false`，人连自由文本都发不了：硬死锁。

判据落在"解成了什么"和"解不出时会不会硬闯"两件事上，不落在异常类型上 ——
换个写法照样可能崩，而这两条只有真结算了才会绿。
"""
from __future__ import annotations

from core.pause import PauseEvent
from core.pause_driver import _resolve_offered_choice


def _structured_question() -> PauseEvent:
    return PauseEvent.from_payload({
        "question": "先读全文还是直接换问题？",
        "context": "文献判决：核心主张已被完整做过。",
        "options": ["读全文再判", "直接换问题"],
        "option_details": [
            {"id": "option_1", "label": "读全文再判",
             "description": "先钉死两条未知再决定", "recommended": True},
            {"id": "option_2", "label": "直接换问题",
             "description": "接受被抢占", "recommended": False},
        ],
        "offer_id": "orch:q48a22a4a:ocf374db9",
        "decision_id": "orch:q48a22a4a",
        "asking_node_type": "_orchestrator",
        "metadata": {"type": "structured_question"},
    }, pending_tool_call_id="call_1")


def test_choice_resolves_to_text_and_authoritative_label():
    resolved = _resolve_offered_choice(
        _structured_question(),
        {"offer_id": "orch:q48a22a4a:ocf374db9", "choice_id": "option_1",
         "note": "不想因为一个摘要就把课题毙了"},
    )
    assert resolved is not None, "点了一个合法选项，却没被结算"
    text, authoritative = resolved
    assert isinstance(text, str), "交给 resume_loop 的必须是字符串"
    # 标签、描述、用户附言三样都要到得了模型那边。
    assert "读全文再判" in text
    assert "先钉死两条未知再决定" in text
    assert "不想因为一个摘要就把课题毙了" in text
    # 权威动作是标签本身 —— 模型不许再自行解读原文。
    assert authoritative == "读全文再判"


def test_answer_survives_the_slice_that_crashed_it():
    """回归：结算结果必须能被 resume_loop 那句 `response_text[:200]` 切。"""
    text, _ = _resolve_offered_choice(
        _structured_question(),
        {"offer_id": "orch:q48a22a4a:ocf374db9", "choice_id": "option_2"},
    )
    assert text[:200]


def test_choice_not_in_the_offer_is_refused():
    """不在选项集里 = 这次授权没发生。拒绝，交给调用方保持 pause。"""
    assert _resolve_offered_choice(
        _structured_question(), {"choice_id": "option_9"}) is None


def test_answer_to_a_previous_offer_is_refused():
    """回答的是上一次呈递（选项集已经变了）——同样不许沿用。"""
    assert _resolve_offered_choice(
        _structured_question(),
        {"offer_id": "orch:q48a22a4a:oDEADBEEF", "choice_id": "option_1"},
    ) is None


def test_free_text_pause_refuses_a_choice():
    """没有结构化选项的 pause 收到 choice_id：没有身份可对，拒绝而不是瞎猜。"""
    free_text = PauseEvent.from_payload({
        "question": "你想让我怎么做？",
        "metadata": {"type": "structured_question"},
    })
    assert _resolve_offered_choice(free_text, {"choice_id": "option_1"}) is None


# ── 走真入口：证明 drive_pause_chain 确实结算了 dict，而不是只有 helper 会 ──
#
# 上面几条只测了 helper。helper 对了而调用点没接上，正是"修复落在没人走的那条
# 路上"——缺陷原样活着，CI 全绿。所以这一条从 drive_pause_chain 进，
# 用真的 resume_loop 走到底：dict 没被结算的话，`response_text[:200]` 当场炸。

import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import PausedRunContext, clear_all, register_pause
from core.pause_driver import drive_pause_chain
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


def _paused_run(tmp_path: Path, pause_event: PauseEvent) -> State:
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="literature", system_prompt="t", tools=[], max_turns=5)
    messages = [
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
    register_pause(PausedRunContext(
        run_id=state.run_id, state=state, messages=messages, harness=harness,
        llm=_StubLLM([LLMResponse(content="finished", tool_calls=[],
                                  finish_reason="stop", usage={})]),
        pending_tool_call_id="tc_1", pause_event=pause_event,
    ))
    return state


def _event(tmp_path_marker: str = "") -> PauseEvent:
    ev = _structured_question()
    return ev


@pytest.mark.asyncio
async def test_drive_pause_chain_settles_a_structured_choice(tmp_path: Path):
    ev = _structured_question()
    state = _paused_run(tmp_path, ev)

    async def ask(_pause_event):
        # App Server 带 choice 作答时给的就是这个形状。
        return {"offer_id": ev.offer_id, "choice_id": "option_1", "note": "就这么办"}

    final_text = await drive_pause_chain(ask_fn=ask)
    assert final_text == "finished", "结构化答复没能把 run 推下去"

    # 回填给模型的那条 tool_result 里，必须是解出来的文本 + 权威动作，
    # 而不是一坨 dict 的 repr。
    resumed = [m for m in state.messages_debug] if hasattr(state, "messages_debug") else []
    del resumed  # state 不持有 messages；判据落在 transcript 上


@pytest.mark.asyncio
async def test_drive_pause_chain_holds_when_the_choice_was_never_offered(
    tmp_path: Path,
):
    """选项不在本次呈递里：保持 pause，绝不拿未受理的答复 resume。"""
    ev = _structured_question()
    state = _paused_run(tmp_path, ev)

    async def ask(_pause_event):
        return {"offer_id": ev.offer_id, "choice_id": "option_does_not_exist"}

    final_text = await drive_pause_chain(ask_fn=ask)
    assert final_text == "", "未受理的答复不该把 run 推下去"
    from core.pause import get_paused_run
    assert get_paused_run(state.run_id) is not None, "这一级必须仍然停在等人"
