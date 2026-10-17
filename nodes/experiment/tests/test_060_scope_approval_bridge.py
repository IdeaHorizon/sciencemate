"""Scope approval uses the shared pause/answer/confirmation protocol.

The real pause driver, resume loop and high-risk hook run here.  A local fake
LLM only ends that loop; the final action is a harmless in-memory spy after the
real node authorization gate.  No shell, model API, UI instance or dangerous
payload is executed.  Structured-choice cases use a Core Offer fixture: they
test Core choice resolution, not HTTP/UI construction of that offer.

These tests do not establish cross-run identity, TTL or concurrent exactly-once
guarantees for the shared confirmation store.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest

from core import pause, pause_driver, session_driver
from core import loop_hooks_builtin  # noqa: F401 -- register the real hook
from core.decision_offer import Choice, Offer
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse
from core.state import State
from nodes.experiment.tools import safe_bash
from shared.lib import dangerous_commands as danger


@pytest.fixture(autouse=True)
def _isolate_approval_runtime(monkeypatch, tmp_path):
    pause.clear_all()
    monkeypatch.setattr(danger, "BYPASS_ENABLED", False)
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", set())
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", False)
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "org"))
    yield
    pause.clear_all()


class _DoneLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return LLMResponse(
            content="Approval protocol replay finished; no payload was run.",
            tool_calls=[], finish_reason="stop", usage={},
        )


def _pending(tmp_path):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="approval-bridge", session_id="approval-bridge-session",
    )
    # An unresolved target is the real SGD-card shape.  This text is only a
    # confirmation identity; no executor receives it.
    request = dict(
        kind="bash", cmd="printf 'approval-probe\\n'",
        cwd=str(state.root), target="<unresolved>",
        scope="unresolved_target", reason="Test-only unresolved write target",
        op="tee",
    )
    allowed, payload = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is False
    assert payload["status"] == "pause", payload
    return state, request, payload


def _register(state, payload, *, structured=False):
    event_payload = dict(payload["pause_event"])
    if structured:
        # Use actual offered labels, not corrected labels supplied by the test:
        # a mismatch with HIGHRISK_OPTIONS must remain observable.
        offer = Offer(
            decision_id=f"{state.run_id}:scope-approval", kind="scope-approval",
            question=event_payload["question"], context=event_payload["context"],
            choices=tuple(
                Choice(id=choice_id, label=label)
                for choice_id, label in zip(
                    ("approve", "deny"), event_payload["options"], strict=True,
                )
            ),
        )
        event_payload.update(offer.to_pause_payload())
    event = pause.PauseEvent.from_payload(
        event_payload, pending_tool_call_id="scope-approval-call",
    )
    messages = [
        LLMMessage(role="system", content="Only replay approval state."),
        LLMMessage(role="user", content="Wait for the explicit choice."),
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": event.pending_tool_call_id, "type": "function",
            "function": {"name": "safe_run_bash", "arguments": "{}"},
        }]),
        LLMMessage(
            role="tool", name="safe_run_bash",
            tool_call_id=event.pending_tool_call_id,
            content=json.dumps(payload, ensure_ascii=False),
        ),
    ]
    llm = _DoneLLM()
    harness = NodeHarness(
        node_type="experiment", tools=[], max_turns=2,
        loop_hooks=["highrisk_confirm"],
        loop_hooks_disable=[
            "memory_delta", "kb_delta", "turn_budget", "memory_onboarding",
            "law_review_gate",
        ],
        summarizer=SummarizerConfig(enabled=False),
    )
    pause.register_pause(pause.PausedRunContext(
        run_id=state.run_id, state=state, messages=messages, harness=harness,
        llm=llm, pending_tool_call_id=event.pending_tool_call_id,
        pause_event=event,
    ))
    return event, llm


def _events(state):
    return [
        json.loads(line)
        for line in (state.root / "transcript.jsonl").read_text().splitlines()
    ]


def test_scope_card_uses_shared_risk_type(tmp_path):
    _, _, payload = _pending(tmp_path)
    event = payload["pause_event"]
    assert event["metadata"]["type"] == "highrisk_confirm"


def test_scope_card_uses_shared_options(tmp_path):
    _, _, payload = _pending(tmp_path)
    event = payload["pause_event"]
    assert event["options"] == danger.HIGHRISK_OPTIONS


def test_directory_permission_context_keeps_executor_and_writer_boundaries(tmp_path):
    state, request, _ = _pending(tmp_path)
    directory = tmp_path / "operator-approved-output"
    directory.mkdir()
    request.update(target=str(directory / "result.txt"), scope="unknown_absolute")
    allowed, payload = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is False
    event = payload["pause_event"]
    assert event["options"] == danger.HIGHRISK_OPTIONS
    assert event["metadata"]["registers_root"] == str(directory)
    context = event["context"]
    # This grants subprocess permission; it is neither a universal writer
    # permission nor a promise to bypass Core ownership/readonly capability.
    assert "子进程" in context
    assert "Core" in context
    assert "ownership" in context
    assert "文件工具" in context
    assert "后续不再逐条询问" not in context


def test_raw_highrisk_key_context_does_not_promise_cwd_binding(tmp_path):
    state, request, _ = _pending(tmp_path)
    # Parsed only.  No shell or executor receives this illustrative command.
    request["cmd"] = "rm -rf ./approval-probe-scratch"
    allowed, payload = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is False
    event = payload["pause_event"]
    assert event["metadata"]["authorization_key"] == request["cmd"]
    context = event["context"]
    assert "框架共享确认机制" in context
    assert "不额外承诺 cwd/目标" in context
    assert "当前 cwd 和该目标路径" not in context
    assert "完整内容（前 500 字符）" not in context


@pytest.mark.parametrize("answer_kind", ["number", "label", "structured"])
def test_human_approval_reaches_real_driver_hook_and_retry(tmp_path, monkeypatch, answer_kind):
    state, request, payload = _pending(tmp_path)
    event, llm = _register(state, payload, structured=answer_kind == "structured")
    record_answer = Mock(wraps=danger.record_highrisk_answer)
    monkeypatch.setattr(danger, "record_highrisk_answer", record_answer)
    answer = {
        "number": "1",
        "label": event.options[0],
        "structured": {"offer_id": event.offer_id, "choice_id": "approve"},
    }[answer_kind]

    async def ask(_event):
        return answer

    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))
    assert llm.calls == 1
    assert record_answer.call_count == 1, "Do not rely on the hook's legacy text fallback"
    assert any(row["event"] == "highrisk_confirm_approved" for row in _events(state))
    # Re-enter the real gate as the tool retry would; never run its payload.
    effect = Mock()
    allowed, block = safe_bash._scope_guard_authorization(state, **request)
    if allowed:
        effect()
    assert (allowed, block) == (True, None)
    effect.assert_called_once_with()
    allowed_again, next_pause = safe_bash._scope_guard_authorization(state, **request)
    assert allowed_again is False
    assert next_pause["status"] == "pause"


@pytest.mark.parametrize("answer_kind", ["number", "structured"])
def test_human_denial_does_not_authorize_retry(tmp_path, monkeypatch, answer_kind):
    state, request, payload = _pending(tmp_path)
    event, llm = _register(state, payload, structured=answer_kind == "structured")
    record_answer = Mock(wraps=danger.record_highrisk_answer)
    monkeypatch.setattr(danger, "record_highrisk_answer", record_answer)
    answer = "2" if answer_kind == "number" else {
        "offer_id": event.offer_id, "choice_id": "deny",
    }

    async def ask(_event):
        return answer

    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))
    assert llm.calls == 1
    assert record_answer.call_count == 1
    assert any(row["event"] == "highrisk_confirm_denied_or_unclear" for row in _events(state))
    allowed, block = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is False
    assert block["status"] == "pause"


def test_autonomous_scope_permission_stays_pending(tmp_path, monkeypatch):
    state, request, payload = _pending(tmp_path)
    _, llm = _register(state, payload)
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    # Autonomous is not continuous: no bypass / blanket preauthorization.
    result = asyncio.run(session_driver.auto_resume_pauses())
    assert result is None
    assert pause.get_paused_run(state.run_id) is not None
    assert llm.calls == 0
    assert not any(row["event"] == "loop_resume" for row in _events(state))
    allowed, block = safe_bash._scope_guard_authorization(state, **request)
    assert allowed is False
    assert block["status"] == "pause"
