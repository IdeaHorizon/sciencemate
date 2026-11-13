from __future__ import annotations

import json

import pytest

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.decision_offer import Choice, Offer
from core.harness import NodeHarness
from core.llm import LLMResponse
from core.pause import PauseEvent, clear_all
from core.pause_dialogue import discuss_pause, exact_pause_answer
from core.state import State
from shared.tools.library.decision_package import _present_decision_package


@pytest.fixture(autouse=True)
def registry():
    bootstrap()
    clear_all()
    yield
    clear_all()


def state_with_review(tmp_path):
    state = State.new("_orchestrator", tmp_path / "runs")
    cid = state.save_artifact(
        "review_critique",
        "current",
        json.dumps(
            {
                "verdict": "approve",
                "summary": "研究设计可以执行。",
                "recommended_action": {"action": "proceed"},
                "concerns": [],
            }
        ),
        metadata={"produced_by_node_type": "_reviewer"},
        provenance=forwarded(
            produced("_reviewer", "review"), via_node_type=state.node_type, via_run_id=state.run_id
        ),
    )["id"]
    entry = {
        "producing_node": "analysis",
        "producing_run_id": "producer",
        "review_state": "done",
        "review_critique_artifact_id": cid,
        "decision_state": "pending",
        "artifact_ids": [],
        "producing_node_inputs": {"research_question": "比较自我改进速率"},
    }
    state.hook_state["pending_post_node_flow"] = [entry]
    state.hook_state["autonomy_mode"] = "assisted"
    return state, entry


@pytest.mark.asyncio
async def test_routine_decision_returns_choices_to_scheduler_without_asking_user(tmp_path):
    state, entry = state_with_review(tmp_path)
    result = await _present_decision_package(state, "analysis", "producer")
    assert result["status"] == "success" and result["decision_required"] is True
    assert "pause_event" not in result
    assert entry["decision_state"] == "pending"
    assert not entry.get("accepted_action")  # recommended != selected
    events = [json.loads(line)["event"] for line in state.transcript_path.read_text().splitlines()]
    assert "decision_package_prepared" in events
    assert "decision_package_presented" not in events  # no phantom human decision in the platform


@pytest.mark.asyncio
async def test_scheduler_can_proceed_within_authority_in_assisted_mode(tmp_path):
    state, entry = state_with_review(tmp_path)
    result = await _present_decision_package(
        state,
        "analysis",
        "producer",
        action="proceed",
        reasoning="定义明确，审查通过。",
    )
    assert result["status"] == "success" and result["decided_by"] == "orchestrator"
    assert state.hook_state["pending_post_node_flow"] == []
    assert entry["decision_history"][-1]["decided_by"] == "orchestrator"
    assert entry["human_note"] == ""
    assert entry["orchestrator_reasoning"] == "定义明确，审查通过。"


@pytest.mark.asyncio
async def test_real_question_keeps_the_human_boundary(tmp_path):
    state, entry = state_with_review(tmp_path)
    result = await _present_decision_package(
        state,
        "analysis",
        "producer",
        human_decision_reason="扩大样本需要增加预算，尚未获得授权。",
    )
    assert result["status"] == "pause"
    assert "预算" in result["pause_event"]["metadata"]["needs_a_person"]
    assert entry["decision_state"] == "awaiting_human" and not entry.get("accepted_action")
    # Re-presenting after recovery cannot silently erase the missing authorization.
    restored = await _present_decision_package(
        state,
        "analysis",
        "producer",
        action="proceed",
    )
    assert restored["status"] == "pause"
    assert "预算" in restored["pause_event"]["metadata"]["needs_a_person"]
    assert not entry.get("accepted_action")


@pytest.mark.asyncio
async def test_missing_user_information_is_not_fabricated_in_unattended_mode(tmp_path, monkeypatch):
    from shared.lib import dangerous_commands as dc
    from shared.tools.builtin import _request_human_input

    monkeypatch.setattr(dc, "BYPASS_ENABLED", True)
    state = State.new("_orchestrator", tmp_path)
    result = await _request_human_input(
        state,
        "可用预算是多少？",
        human_decision_reason="现有信息没有预算上限，不能代填。",
    )
    assert result["status"] == "pause"
    assert "预算上限" in result["pause_event"]["metadata"]["needs_a_person"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "pause", "error"])
async def test_scheduler_revision_uses_existing_dispatch_and_keeps_actual_result(
    tmp_path, monkeypatch, status
):
    state, entry = state_with_review(tmp_path)
    calls = []

    async def dispatch(parent, target, **kwargs):
        calls.append((parent, target, kwargs))
        return {"status": status, "pause_event": {"question": "需要新授权"}, "error": "不可执行"}

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", dispatch)
    result = await _present_decision_package(
        state,
        "analysis",
        "producer",
        action="revise",
        reasoning="先补足可比性定义。",
    )
    assert len(calls) == 1 and calls[0][1] == "analysis"
    assert calls[0][2]["node_inputs"]["research_question"] == "比较自我改进速率"
    assert calls[0][2]["node_inputs"]["human_directive"] == ""
    assert "调度器决定" in calls[0][2]["user_note"]
    assert result["status"] == status
    assert entry["decision_state"] == "action_authorized"  # dispatch isn't completion


def question(state, kind="decision_package"):
    offer = Offer(
        decision_id="decision",
        kind="post_node",
        question="是否修改？",
        choices=(Choice("proceed", "继续"), Choice("revise", "修改")),
        recommended_id="revise",
    )
    return PauseEvent.from_payload(
        {
            **offer.to_pause_payload(),
            "offer": offer.to_pause_payload(),
            "asking_node_type": "_orchestrator",
            "asking_run_id": state.run_id,
            "metadata": {"type": kind},
        },
        pending_tool_call_id="pending-decision",
    )


class Scripted:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def chat(self, messages, **kwargs):
        return next(self.responses)


def response(text, calls=None):
    return LLMResponse(
        content=text,
        tool_calls=calls or [],
        finish_reason="tool_calls" if calls else "stop",
        usage={},
    )


@pytest.mark.asyncio
async def test_question_is_conversation_not_an_invalid_choice(tmp_path):
    state = State.new("_orchestrator", tmp_path)
    pause = question(state)
    selected, reply = await discuss_pause(
        state,
        NodeHarness(node_type="_orchestrator", system_prompt="scheduler", tools=[]),
        Scripted([response("建议修改，因为两组的速率定义不同。")]),
        pause,
        "为什么建议修改？",
    )
    assert selected is None and "定义不同" in reply
    assert pause.pending_tool_call_id == "pending-decision"
    assert "_pause_dialogue" not in state.hook_state
    assert not any(
        json.loads(line)["event"] == "decision_answer_recorded"
        for line in state.transcript_path.read_text().splitlines()
    )


@pytest.mark.asyncio
async def test_natural_decision_keeps_the_user_directive_and_current_identity(tmp_path):
    state = State.new("_orchestrator", tmp_path)
    pause = question(state)
    text = "按刚才的建议改，但先统一计量单位"
    call = {
        "id": "choose",
        "type": "function",
        "function": {
            "name": "runtime_control",
            "arguments": json.dumps(
                {
                    "action": "answer",
                    "child_run_id": state.run_id,
                    "offer_id": pause.offer_id,
                    "choice_id": "revise",
                }
            ),
        },
    }
    selected, reply = await discuss_pause(
        state,
        NodeHarness(
            node_type="_orchestrator", system_prompt="scheduler", tools=["runtime_control"]
        ),
        Scripted([response("好，先统一单位再修改。", [call])]),
        pause,
        text,
    )
    assert selected == {"offer_id": pause.offer_id, "choice_id": "revise", "note": text}
    assert "统一单位" in reply


@pytest.mark.parametrize("text", ["为什么要批准？", "1 是什么意思？", "可以解释一下吗？"])
def test_questions_cannot_be_parsed_as_approval(tmp_path, text):
    assert (
        exact_pause_answer(question(State.new("_orchestrator", tmp_path), "permission"), text)
        is None
    )


@pytest.mark.asyncio
async def test_model_cannot_turn_permission_discussion_into_a_grant(tmp_path):
    state = State.new("_orchestrator", tmp_path)
    pause = question(state, "permission")
    call = {
        "id": "wrong",
        "type": "function",
        "function": {
            "name": "runtime_control",
            "arguments": json.dumps(
                {
                    "action": "answer",
                    "child_run_id": state.run_id,
                    "offer_id": pause.offer_id,
                    "choice_id": "proceed",
                }
            ),
        },
    }
    selected, _ = await discuss_pause(
        state,
        NodeHarness(
            node_type="_orchestrator", system_prompt="scheduler", tools=["runtime_control"]
        ),
        Scripted([response(None, [call]), response("尚未授权，可以先讨论用途。")]),
        pause,
        "为什么需要这个权限？",
    )
    assert selected is None
