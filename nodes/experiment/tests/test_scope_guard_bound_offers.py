"""Scope cards must publish the actual Core Offer consumed by the answer path."""
from __future__ import annotations

import asyncio
import json
import shlex

import pytest

from core import execution_target, loop_hooks_builtin, pause, pause_driver  # noqa: F401
from core.decision_offer import Offer
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse
from core.pause_dialogue import exact_pause_answer, answer_from_dialogue
from core.state import State
from nodes.experiment.tools import safe_bash
from shared.lib import dangerous_commands as danger


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    pause.clear_all()
    monkeypatch.setattr(danger, "BYPASS_ENABLED", False)
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", set())
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", False)
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "org"))
    yield
    pause.clear_all()


def _request(tmp_path, **changes):
    state = State.new("experiment", tmp_path / "runs", session_id="bound-offer-test")
    state.hook_state["_current_tool_call_id_being_executed"] = "fixture-call"
    request = dict(kind="bash", cmd="touch result.txt", cwd=str(state.root),
                   target="<unresolved>", scope="unresolved_target", reason="fixture", op="touch")
    request.update(changes)
    _, result = safe_bash._scope_guard_authorization(state, **request)
    event = pause.PauseEvent.from_payload(result["pause_event"], pending_tool_call_id="fixture-call")
    return state, request, result, event


@pytest.mark.parametrize("text,choice", [("批准执行", "approve"), ("拒绝", "deny"), ("1", "approve"), ("2", "deny"), ("[1]", "approve"), ("approve", "approve")])
def test_displayed_label_and_number_reach_the_current_offer(tmp_path, text, choice):
    state, _, result, event = _request(tmp_path)
    offer = Offer.from_pause_payload(result["pause_event"])
    assert event.offer_id == offer.offer_id and event.offer_id
    assert exact_pause_answer(event, text)["choice_id"] == choice
    state.hook_state["_pause_dialogue"] = {"pause": event, "text": text, "answer": None}
    answer = answer_from_dialogue(state, child_run_id=event.asking_run_id,
                                  offer_id=event.offer_id, choice_id=choice, content=text)
    assert answer["status"] == "success"
    assert state.hook_state["_pause_dialogue"]["answer"]["choice_id"] == choice


def test_offer_identity_binds_full_command_not_just_truncated_preview(tmp_path):
    state, request, _, first = _request(tmp_path, cmd="printf '" + "x" * 600 + "' > a.txt")
    _, repeated = safe_bash._scope_guard_authorization(state, **request)
    assert repeated["pause_event"]["offer_id"] == first.offer_id
    request["cmd"] += "; touch another.txt"
    _, changed = safe_bash._scope_guard_authorization(state, **request)
    assert changed["pause_event"]["offer_id"] != first.offer_id


def test_later_identical_call_does_not_accept_a_previous_offer(tmp_path):
    state, request, _, first = _request(tmp_path)
    state.hook_state["_current_tool_call_id_being_executed"] = "later-call"
    _, result = safe_bash._scope_guard_authorization(state, **request)
    current = pause.PauseEvent.from_payload(result["pause_event"], pending_tool_call_id="later-call")
    assert first.offer_id != current.offer_id
    state.hook_state["_pause_dialogue"] = {"pause": current, "text": "批准执行", "answer": None}
    answer = answer_from_dialogue(state, child_run_id=state.run_id, offer_id=first.offer_id,
                                  choice_id="approve", content="批准执行")
    assert answer["status"] == "error"
    assert state.hook_state["_pause_dialogue"]["answer"] is None


def test_direct_call_without_loop_identity_gets_a_fresh_presentation(tmp_path):
    state, request, _, _ = _request(tmp_path)
    state.hook_state.pop("_current_tool_call_id_being_executed")
    _, first = safe_bash._scope_guard_authorization(state, **request)
    _, second = safe_bash._scope_guard_authorization(state, **request)
    assert first["pause_event"]["offer_id"] != second["pause_event"]["offer_id"]


@pytest.mark.parametrize("field,new_value", [("cwd", "other-cwd"), ("target", "other-target"), ("scope", "unknown_absolute"), ("op", "redirect")])
def test_changed_authorization_binding_does_not_reuse_the_offer(tmp_path, field, new_value):
    state, request, _, event = _request(tmp_path)
    request[field] = new_value
    _, changed = safe_bash._scope_guard_authorization(state, **request)
    assert changed["pause_event"]["offer_id"] != event.offer_id


@pytest.mark.parametrize("before,after", [("local", "wsl:Ubuntu"), ("wsl:Ubuntu", "wsl:Debian")])
def test_switching_execution_target_invalidates_scope_approval(tmp_path, monkeypatch, before, after):
    monkeypatch.setattr(execution_target, "current", lambda: execution_target.parse(before))
    state, request, result, event = _request(tmp_path)
    key = result["pause_event"]["metadata"]["authorization_key"]
    danger.mark_confirmed(state, key)

    monkeypatch.setattr(execution_target, "current", lambda: execution_target.parse(after))
    approved, changed = safe_bash._scope_guard_authorization(state, **request)
    assert not approved and changed["pause_event"]["offer_id"] != event.offer_id
    assert changed["pause_event"]["metadata"]["authorization_key"] != key
    assert changed["pause_event"]["metadata"]["execution_target"] == after


def test_approval_is_registered_for_the_run_without_a_second_permission_cache(tmp_path):
    directory = tmp_path / "approved-environment"
    directory.mkdir()
    state, request, result, _ = _request(tmp_path, target=str(directory / "a.txt"), scope="unknown_absolute")
    danger.mark_confirmed(state, result["pause_event"]["metadata"]["authorization_key"])
    assert safe_bash._scope_guard_authorization(state, **request) == (True, None)
    command = "printf ready > " + shlex.quote(str(directory / "b.txt").replace("\\", "/"))
    assert safe_bash._bash_path_effects_guard(state, command, cwd=str(state.root)) is None
    # The grant must not escape to the parent or a sibling.
    sibling = tmp_path / "not-approved" / "b.txt"
    assert not any(
        role.role == "approved_write_root"
        for role in safe_bash.matching_path_roles(str(sibling), state)
    ), "This grant must not authorize siblings independently of Core's temp policy"
    roles = state.hook_state["path_roles"]["approved_write_root"]
    assert len(roles) == 1 and roles[0]["path"] == str(directory)


def test_wrong_offer_or_run_and_discussion_do_not_authorize(tmp_path):
    state, request, result, event = _request(tmp_path)
    state.hook_state["_pause_dialogue"] = {"pause": event, "text": "为什么要授权？", "answer": None}
    for rid, oid, text in [(state.run_id, event.offer_id, "批准执行"),
                           ("other-run", event.offer_id, "批准执行"),
                           (state.run_id, "old-offer", "批准执行")]:
        answer = answer_from_dialogue(state, child_run_id=rid, offer_id=oid,
                                      choice_id="approve", content=text)
        assert answer["status"] == "error"
    assert state.hook_state["_pause_dialogue"]["answer"] is None
    assert not danger.is_confirmed(state, result["pause_event"]["metadata"]["authorization_key"])


class _DoneLLM:
    async def chat(self, messages, **kwargs):
        return LLMResponse(content="fixture complete", tool_calls=[], finish_reason="stop", usage={})


@pytest.mark.parametrize("text,approved", [("批准执行", True), ("拒绝", False)])
def test_real_driver_consumes_the_nodes_own_offer_without_test_repair(tmp_path, monkeypatch, text, approved):
    state, request, result, event = _request(tmp_path)
    selection = exact_pause_answer(event, text)
    assert selection is not None
    messages = [LLMMessage(role="system", content="bounded protocol test"),
                LLMMessage(role="assistant", content="", tool_calls=[{"id": "fixture-call", "type": "function", "function": {"name": "safe_run_bash", "arguments": "{}"}}]),
                LLMMessage(role="tool", name="safe_run_bash", tool_call_id="fixture-call", content=json.dumps(result))]
    harness = NodeHarness(node_type="experiment", tools=[], max_turns=2, loop_hooks=["highrisk_confirm"],
                          loop_hooks_disable=["memory_delta", "kb_delta", "turn_budget", "memory_onboarding", "law_review_gate"],
                          summarizer=SummarizerConfig(enabled=False))
    pause.register_pause(pause.PausedRunContext(run_id=state.run_id, state=state, messages=messages,
                         harness=harness, llm=_DoneLLM(), pending_tool_call_id="fixture-call", pause_event=event))
    async def ask(_event):
        return selection
    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))
    allowed, block = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is approved
    assert (block is None) is approved
    assert not any(r.get("event") == "subprocess_spawn" for r in (
        json.loads(l) for l in (state.root / "transcript.jsonl").read_text("utf8").splitlines()))
