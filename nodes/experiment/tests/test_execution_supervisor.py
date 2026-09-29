"""Regression tests for the experiment advisory supervisor."""
from __future__ import annotations

import json

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks as H


def _ctx(state: State, turn: int) -> HookContext:
    ctx = HookContext.__new__(HookContext)
    ctx.state = state
    ctx.turn = turn
    ctx.messages = []
    ctx.tool_call_records = []
    return ctx


def _events(state: State) -> list[dict]:
    return [json.loads(line) for line in state.transcript_path.read_text(encoding="utf-8").splitlines()]


def test_supervisor_selects_one_highest_priority_candidate(tmp_path):
    state = State.new("experiment", tmp_path)
    ctx = _ctx(state, 3)
    H._supervisor_offer(ctx, source="health", priority=50, content="health advice")
    H._supervisor_offer(ctx, source="failure", priority=20, content="failure advice")
    H._supervisor_offer(ctx, source="plan", priority=10, content="plan advice")

    injected = H.execution_supervisor_on_turn_start(ctx)

    assert injected is not None
    assert len(injected) == 1
    assert injected[0].content == "plan advice"
    event = _events(state)[-1]
    assert event["event"] == "execution_supervisor_injected"
    assert event["source"] == "plan"
    assert event["candidates"] == 3


def test_supervisor_deduplicates_and_releases_after_cooldown(tmp_path):
    state = State.new("experiment", tmp_path)
    first = _ctx(state, 1)
    H._supervisor_offer(first, source="repeat", priority=30, content="change direction", dedupe_key="same")
    assert H.execution_supervisor_on_turn_start(first)

    suppressed = _ctx(state, 2)
    H._supervisor_offer(suppressed, source="repeat", priority=30, content="change direction", dedupe_key="same")
    assert H.execution_supervisor_on_turn_start(suppressed) is None
    assert any(event["event"] == "execution_supervisor_suppressed"
               and event["reason"] == "cooldown" for event in _events(state))

    released = _ctx(state, 6)
    H._supervisor_offer(released, source="repeat", priority=30, content="change direction", dedupe_key="same")
    assert H.execution_supervisor_on_turn_start(released)


def test_operation_suppresses_scientific_advisory_but_keeps_operational_safety(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(H, "_is_operational_run", lambda _state: True)
    ctx = _ctx(state, 2)
    H._supervisor_offer(ctx, source="scientific", priority=1, content="scientific verdict", scope="scientific")
    H._supervisor_offer(ctx, source="failure", priority=20, content="inspect build log")

    injected = H.execution_supervisor_on_turn_start(ctx)

    assert injected is not None
    assert injected[0].content == "inspect build log"
    assert any(event["event"] == "execution_supervisor_suppressed"
               and event["reason"] == "operation_scope" for event in _events(state))


def test_supervisor_recovers_from_malformed_bookkeeping(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state[H._SUPERVISOR_LAST_KEY] = "corrupt"
    state.hook_state[H._SUPERVISOR_CANDIDATES_KEY] = {
        "broken": {"source": "broken", "priority": "not-a-number",
                   "content": "fallback advice", "dedupe_key": "broken:key"},
        "invalid": {"source": "invalid", "priority": 1, "content": "", "dedupe_key": ""},
    }

    injected = H.execution_supervisor_on_turn_start(_ctx(state, 4))

    assert injected is not None
    assert injected[0].content == "fallback advice"
    assert state.hook_state[H._SUPERVISOR_LAST_KEY]["broken:key"] == 4
