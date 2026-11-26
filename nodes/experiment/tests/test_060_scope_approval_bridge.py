"""Scope gates resume their exact original invocation through Core.

A deterministic model ends the loop after the in-memory effect. Real command
execution, persistence, TTL and concurrency have separate integration coverage.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest

from core import pause, pause_driver, session_driver
from core import loop_hooks_builtin  # noqa: F401 -- register owner hooks
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
    from core import approvals
    from tests._approval_support import prepare_result
    meta = payload["pause_event"]["metadata"]
    request = {"kind": "bash", "cmd": "printf 'approval-probe\\n'", "cwd": str(state.root),
               "target": meta["target"], "scope": meta["scope"], "reason": "Test-only unresolved write target", "op": "tee"}
    async def gate(state, **values):
        allowed, blocked = safe_bash._scope_guard_authorization(state, **values)
        if allowed:
            state.hook_state["approval_effects"] = state.hook_state.get("approval_effects", 0) + 1
            return {"status": "success"}
        return blocked
    offered = asyncio.run(prepare_result(state, payload, gate, request))
    record = approvals.load(state, offered)
    operation = record["operation"]
    event = pause.PauseEvent.from_payload(offered, pending_tool_call_id=operation["tool_call_id"])
    messages = [LLMMessage(role="system", content="bounded approval integration"),
        LLMMessage(role="assistant", tool_calls=[{"id": operation["tool_call_id"], "type": "function",
            "function": {"name": operation["tool"], "arguments": json.dumps(request)}}]),
        LLMMessage(role="tool", tool_call_id=operation["tool_call_id"], name=operation["tool"],
            content=json.dumps({"status": "pause", "pause_event": offered}))]
    llm = _DoneLLM()
    harness = NodeHarness(node_type="experiment", tools=[], max_turns=2,
        loop_hooks_disable=["memory_delta", "kb_delta", "turn_budget", "memory_onboarding", "law_review_gate"],
        summarizer=SummarizerConfig(enabled=False))
    pause.register_pause(pause.PausedRunContext(run_id=state.run_id, state=state, messages=messages,
        harness=harness, llm=llm, pending_tool_call_id=operation["tool_call_id"], pause_event=event))
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
    assert "框架续执行原调用" in context
    assert "绑定完整参数、工作目录和执行目标" in context
    assert "当前 cwd 和该目标路径" not in context
    assert "完整内容（前 500 字符）" not in context


@pytest.mark.parametrize("answer_kind", ["number", "label", "structured"])
def test_human_approval_reaches_real_driver_hook_and_retry(tmp_path, monkeypatch, answer_kind):
    state, request, payload = _pending(tmp_path)
    event, llm = _register(state, payload, structured=answer_kind == "structured")
    answer = {"number": "1", "label": event.options[0], "structured": {
        "offer_id": event.offer_id, "choice_id": "approve"}}[answer_kind]
    async def ask(_event):
        return answer
    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))
    assert llm.calls == 1 and state.hook_state["approval_effects"] == 1
    assert any(row["event"] == "approval_decided" for row in _events(state))
    assert safe_bash._scope_guard_authorization(state, **request)[0] is False


@pytest.mark.parametrize("answer_kind", ["number", "structured"])
def test_human_denial_does_not_authorize_retry(tmp_path, monkeypatch, answer_kind):
    state, request, payload = _pending(tmp_path)
    event, llm = _register(state, payload, structured=answer_kind == "structured")
    answer = "2" if answer_kind == "number" else {"offer_id": event.offer_id, "choice_id": "deny"}
    async def ask(_event):
        return answer
    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))
    assert llm.calls == 1 and not state.hook_state.get("approval_effects")
    assert safe_bash._scope_guard_authorization(state, **request)[0] is False


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
