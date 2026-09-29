"""Fail-closed legacy runs must explain the authority failure before its fallout."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import run_contract as run_contract_module
from nodes.experiment.tools.contract_audit import TERMINAL_CLOSURE_REGISTRY
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    _run_acceptance_receipt_digest,
    audit_execution_intent_binding,
    load_run_contract,
    resolve_run_acceptance,
)


_INTENT_CHECK = "experiment_execution_intent_audit"


def _events(state: State, name: str) -> list[dict]:
    return [
        event
        for event in (
            json.loads(line)
            for line in state.transcript_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        )
        if event.get("event") == name
    ]


def _legacy_classified_state(tmp_path: Path) -> State:
    """Reproduce an accepted pre-receipt scope without inventing new authority."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build and verify the requested solver.",
    }
    scope = {
        "mode": "operational",
        "category": "toolchain_build",
        "reason": "classified before run acceptance receipts existed",
        "invocation": {},
    }
    state.hook_state["experiment_execution_scope"] = dict(scope)
    state.append_transcript("experiment_scope_classified", **scope)
    return state


def _classify_scientific(state: State) -> None:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Measure the declared scientific target.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This closure fixture is exploratory and has no preregistration.",
        },
    }
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="The result will be used to evaluate a scientific claim.",
    ))
    assert result["status"] == "success", result


def _second_conflicting_receipt(state: State) -> None:
    receipt_event = _events(state, "run_acceptance_receipt_recorded")[0]
    payload = {
        key: value
        for key, value in receipt_event.items()
        if key not in {
            "event", "at", "tenant_id", "session_id", "submission_id",
        }
    }
    payload["intent_input_keys"] = [
        *payload["intent_input_keys"],
        "unexpected_conflicting_input",
    ]
    payload["intent_input_keys"] = sorted(set(payload["intent_input_keys"]))
    payload["receipt_digest"] = _run_acceptance_receipt_digest(payload)
    state.append_transcript("run_acceptance_receipt_recorded", **payload)


def test_legacy_classified_run_names_authority_failure_first_at_end(
    tmp_path: Path,
) -> None:
    state = _legacy_classified_state(tmp_path)

    contract = load_run_contract(state)
    intent = audit_execution_intent_binding(state, require=True)

    # P0a fail-closed semantics and conservative obligations stay unchanged.
    assert contract["execution_contract_valid"] is False
    assert contract["requires_hypothesis_verdict"] is True
    assert contract["review_eligible"] is False
    assert intent["passed"] is False
    assert intent["status"] == (
        "run_authority_receipt_missing_for_existing_scope"
    )
    reason = intent["reason"]
    assert "already classified" in reason
    assert "does not reconstruct" in reason
    assert "start a fresh run" in reason
    assert "declare_inconclusive_verdict" in reason
    assert "do not fabricate" in reason

    loop_result = SimpleNamespace(status="completed", final_text="done")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )

    assert loop_result.status == "blocked"
    blocked = _events(state, "experiment_downstream_blocked")[-1]
    assert blocked["failed_checks"][0] == _INTENT_CHECK
    assert blocked["primary_failed_check"] == _INTENT_CHECK
    assert blocked["consequential_failed_checks"] == blocked["failed_checks"][1:]
    assert "fail-closed consequences" in blocked["causal_note"]
    assert loop_result.final_text.index(_INTENT_CHECK) < (
        loop_result.final_text.index("fail-closed consequences")
    )
    for consequence in blocked["consequential_failed_checks"]:
        assert loop_result.final_text.index("fail-closed consequences") < (
            loop_result.final_text.index(consequence)
        )


def test_receipt_conflict_does_not_claim_the_run_predates_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    _classify_scientific(state)
    _second_conflicting_receipt(state)

    # Mutation probe: even an accidentally over-broad helper must not relabel
    # a concrete receipt conflict as a pre-receipt legacy run.
    monkeypatch.setattr(
        run_contract_module,
        "_run_acceptance_missing_for_existing_scope",
        lambda _state, _acceptance: True,
    )

    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    intent = audit_execution_intent_binding(state, require=True)

    assert acceptance["status"] == "run_authority_receipt_conflict"
    assert intent["status"] == "run_authority_receipt_conflict"
    assert "start a fresh run" not in intent["reason"]
    assert "before run acceptance receipts" not in intent["reason"]

    loop_result = SimpleNamespace(status="completed", final_text="done")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )
    blocked = _events(state, "experiment_downstream_blocked")[-1]
    assert "primary_failed_check" not in blocked
    assert "consequential_failed_checks" not in blocked
    assert "causal_note" not in blocked


def test_valid_receipt_keeps_the_existing_blocked_event_shape_and_order(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _classify_scientific(state)
    assert resolve_run_acceptance(state, bind_if_absent=False)["passed"] is True

    before = hooks.audit_experiment_contract(state)
    closure_checks = {
        event_key: before[audit_key]
        for audit_key, event_key in TERMINAL_CLOSURE_REGISTRY.items()
    }
    expected = sorted(
        name
        for name, check in closure_checks.items()
        if not check.get("passed", False)
    )

    loop_result = SimpleNamespace(status="completed", final_text="done")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )
    blocked = _events(state, "experiment_downstream_blocked")[-1]

    assert blocked["failed_checks"] == expected
    assert set(blocked) - {
        "event", "at", "tenant_id", "session_id", "submission_id",
    } == {"failed_checks", "review_eligibility"}
    assert state.hook_state["experiment_downstream_blocked"] == {
        "reason": "experiment_closure_incomplete",
        "failed_checks": expected,
        "review_eligibility": False,
    }
