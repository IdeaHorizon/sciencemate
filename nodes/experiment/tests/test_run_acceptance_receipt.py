"""P0a regressions for the same-run governing input acceptance receipt."""
from __future__ import annotations

import asyncio
import hashlib
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    _run_acceptance_receipt_digest,
    audit_execution_intent_binding,
    create_run_manifest,
    load_run_contract,
    resolve_run_acceptance,
)


_RECEIPT_EVENT = "run_acceptance_receipt_recorded"
_PENDING_ASSIGNMENT_EVENT = "experiment_prereg_assignment_pending"


def _save_frozen_prereg(
    state: State,
    name: str,
    *,
    run_role: str | None = None,
    execution_mode: str | None = None,
) -> str:
    metadata: dict[str, object] = {"stage": "simulation"}
    if run_role is not None:
        metadata["run_role"] = run_role
    if execution_mode is not None:
        metadata["execution_mode"] = execution_mode
    artifact_id = state.save_artifact(
        "pre_registration",
        name,
        f"# frozen preregistration: {name}\n",
        metadata=metadata,
    )["id"]
    state.mark_frozen(artifact_id)
    return artifact_id


def _classify(
    state: State,
    *,
    scope: str = "operation",
) -> dict:
    return asyncio.run(_classify_experiment_scope(
        state,
        scope=scope,
        operation_category="toolchain_build",
        reason="Bind the run input before any managed side effect.",
    ))


def _receipt_events(state: State) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    return [event for event in events if event.get("event") == _RECEIPT_EVENT]


def _pending_assignment_events(state: State) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    return [
        event
        for event in events
        if event.get("event") == _PENDING_ASSIGNMENT_EVENT
    ]


def _event_payload(event: dict) -> dict:
    return {
        key: value
        for key, value in event.items()
        if key not in {"event", "at", "tenant_id", "session_id", "submission_id"}
    }


def test_explicit_prereg_forces_scientific_and_binds_exact_receipt(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "explicit_node_input",
        run_role="secondary",
        execution_mode="operational",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Build the preregistered solver before measurement.",
    }

    classified = _classify(state, scope="operation")
    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert classified["classification"]["requested_mode"] == "operational"
    assert resolved["passed"] is True, resolved
    assert resolved["status"] == "bound"
    receipt = resolved["receipt"]
    assert receipt["schema_version"] == 2
    assert receipt["prereg_assignment"] == {
        "kind": "bound",
        "artifact_id": prereg_id,
        "version": 1,
        "content_hash": load_run_contract(state)["prereg_content_hash"],
        "source": "explicit_node_input",
    }
    assert receipt["binding_source"] == "explicit_node_input"
    assert receipt["governing_task_input_binding"] == {
        "artifact_id": prereg_id,
        "version": 1,
        "content_hash": load_run_contract(state)["prereg_content_hash"],
    }
    assert len(receipt["receipt_digest"]) == 64
    assert len(_receipt_events(state)) == 1


def test_explicit_none_receipt_is_stable_when_unrelated_catalog_grows(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    assignment = {
        "kind": "none",
        "reason": "This operation consumes no project preregistration.",
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build and mechanically verify a local package.",
        "prereg_assignment": assignment,
    }

    classified = _classify(state, scope="operation")
    before = resolve_run_acceptance(state, bind_if_absent=False)
    assert classified["status"] == "success", classified
    assert before["receipt"]["schema_version"] == 2
    assert before["receipt"]["prereg_assignment"] == assignment
    assert before["receipt"]["governing_task_input_binding"] is None
    assert before["receipt"]["binding_source"] == "none"

    _save_frozen_prereg(state, "late", run_role="primary", execution_mode="scientific")
    after = resolve_run_acceptance(state, bind_if_absent=False)
    contract = load_run_contract(state)
    audit = audit_execution_intent_binding(state, require=False)
    effect_audit = audit_execution_intent_binding(state, require=True)

    assert after["receipt"] == before["receipt"]
    # A typed explicit-none receipt means this run accepted no governing prereg.
    # Later project activity belongs to another task unless a formal action
    # explicitly tries to consume it; it must neither retrofit authority nor
    # prevent this operation from reaching an honest terminal state.
    assert contract["prereg_artifact_id"] is None
    assert contract["prereg_binding_source"] == "none"
    assert contract["execution_mode"] == "operational"
    assert contract["requires_hypothesis_verdict"] is False
    assert audit["passed"] is True, audit
    assert audit["status"] == "bound"
    assert effect_audit["passed"] is True, effect_audit
    assert effect_audit["status"] == "bound"


def test_operation_with_explicit_none_ignores_unrelated_project_ambiguity(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "unselected_a", run_role="primary")
    _save_frozen_prereg(state, "unselected_b", run_role="primary")
    assignment = {
        "kind": "none",
        "reason": "The operation is unrelated to either project study.",
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Build an unrelated local tool without consuming project preregistrations."
        ),
        "prereg_assignment": assignment,
    }

    classified = _classify(state, scope="operation")

    assert classified["status"] == "success", classified
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert receipt["prereg_assignment"] == assignment
    assert receipt["governing_task_input_binding"] is None
    assert receipt["binding_source"] == "none"
    contract = load_run_contract(state)
    assert contract["prereg_artifact_id"] is None
    assert contract["execution_mode"] == "operational"
    audit = audit_execution_intent_binding(state, require=True)
    assert audit["passed"] is True, audit


def test_pending_assignment_cannot_authorize_scientific_reclassification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "candidate_a", run_role="primary")
    _save_frozen_prereg(state, "candidate_b", run_role="primary")
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Build an unrelated tool, then attempt to reinterpret this run as "
            "one of two preregistered scientific studies."
        ),
    }

    first = _classify(state, scope="operation")
    receipt_before = resolve_run_acceptance(
        state, bind_if_absent=False
    )["receipt"]
    second = _classify(state, scope="scientific")
    receipt_after = resolve_run_acceptance(
        state, bind_if_absent=False
    )["receipt"]

    assert first["status"] == "success", first
    assert first["classification"]["mode"] == "operational"
    assert receipt_before["prereg_assignment"] == {"kind": "pending"}
    assert second["status"] == "error", second
    assert second["error_code"] == "prereg_assignment_required"
    assert second["assignment_status"] == "pending"
    assert second["prereg_assignment"] == {"kind": "pending"}
    assert second["next_action"]["owner"] == "dispatching_parent"
    assert state.hook_state["experiment_execution_scope"]["mode"] == "operational"
    assert receipt_after == receipt_before


def test_unpreregistered_operation_can_be_corrected_to_unpreregistered_science(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Explore a new scientific question without a preregistration.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This is an explicitly unpreregistered exploratory run.",
        },
    }

    first = _classify(state, scope="operation")
    second = _classify(state, scope="scientific")
    audit = audit_execution_intent_binding(state, require=True)

    assert first["status"] == "success", first
    assert second["status"] == "success", second
    assert second["classification"]["mode"] == "scientific"
    assert audit["passed"] is True, audit
    assert audit["prereg_binding"] == {
        "artifact_id": None,
        "version": None,
        "content_hash": None,
    }


def test_unpreregistered_science_can_be_corrected_to_operation_before_conflict(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Mechanically validate a workload with no preregistration.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This run does not consume a project preregistration.",
        },
    }

    first = _classify(state, scope="scientific")
    second = _classify(state, scope="operation")
    audit = audit_execution_intent_binding(state, require=True)

    assert first["status"] == "success", first
    assert second["status"] == "success", second
    assert second["classification"]["mode"] == "operational"
    assert audit["passed"] is True, audit
    assert audit["scope_mode"] == "operational"
    assert audit["prereg_binding"] == {
        "artifact_id": None,
        "version": None,
        "content_hash": None,
    }


def test_pending_assignment_rejects_scientific_scope_with_candidate_witness(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "candidate_a", run_role="primary")
    _save_frozen_prereg(state, "candidate_b", run_role="primary")
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Attempt scientific execution before the caller resolves prereg assignment."
        ),
    }

    classified = _classify(state, scope="scientific")

    assert classified["status"] == "error", classified
    assert classified["error_code"] == "prereg_assignment_required"
    assert classified["assignment_status"] == "pending"
    receipts = _receipt_events(state)
    assert len(receipts) == 1
    assert receipts[0]["schema_version"] == 2
    assert receipts[0]["prereg_assignment"] == {"kind": "pending"}
    pending = _pending_assignment_events(state)
    assert len(pending) == 1
    assert pending[0]["authorizing"] is False
    assert pending[0]["next_action_owner"] == "dispatching_parent"
    assert {
        item["artifact_id"] for item in pending[0]["candidate_bindings"]
    } == {
        item["artifact_id"] for item in classified["candidate_bindings"]
    }


def test_operation_rejects_multiple_caller_forwarded_preregs(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    selected_a = _save_frozen_prereg(state, "forwarded_a", run_role="primary")
    selected_b = _save_frozen_prereg(state, "forwarded_b", run_role="primary")
    state.hook_state["forwarded_input_ids"] = [selected_a, selected_b]
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Build the input selected by the caller, but the caller forwarded two."
        ),
    }

    classified = _classify(state, scope="operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_ambiguous"
    assert _receipt_events(state) == []


@pytest.mark.parametrize("scope", ["operation", "scientific"])
def test_caller_forwarded_frozen_and_pending_preregs_are_ambiguous(
    tmp_path: Path,
    scope: str,
) -> None:
    state = State.new("experiment", tmp_path)
    frozen_id = _save_frozen_prereg(state, "forwarded_frozen", run_role="primary")
    pending_id = _save_frozen_prereg(state, "forwarded_pending", run_role="primary")
    state.save_artifact(
        "pre_registration",
        "forwarded_pending",
        "# pending forwarded amendment\n",
        amendment_reason="The caller forwarded this still-pending revision.",
    )
    state.hook_state["forwarded_input_ids"] = [frozen_id, pending_id]
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Use the caller-selected preregistration, but one forwarded choice is pending."
        ),
    }

    classified = _classify(state, scope=scope)

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_ambiguous"
    assert _receipt_events(state) == []


@pytest.mark.parametrize("scope", ["operation", "scientific"])
def test_sole_caller_forwarded_pure_draft_prereg_is_unavailable(
    tmp_path: Path,
    scope: str,
) -> None:
    state = State.new("experiment", tmp_path)
    draft_id = state.save_artifact(
        "pre_registration",
        "forwarded_pure_draft",
        "# caller-forwarded preregistration that was never frozen\n",
        metadata={"run_role": "primary"},
    )["id"]
    state.hook_state["forwarded_input_ids"] = [draft_id]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the preregistration selected by the caller.",
    }

    classified = _classify(state, scope=scope)

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_forwarded_prereg_unavailable"
    assert classified["unavailable_forwarded_preregs"] == [draft_id]
    assert _receipt_events(state) == []


@pytest.mark.parametrize("scope", ["operation", "scientific"])
def test_caller_forwarded_frozen_and_pure_draft_preregs_are_ambiguous(
    tmp_path: Path,
    scope: str,
) -> None:
    state = State.new("experiment", tmp_path)
    frozen_id = _save_frozen_prereg(state, "forwarded_frozen", run_role="primary")
    draft_id = state.save_artifact(
        "pre_registration",
        "forwarded_pure_draft",
        "# second caller-forwarded preregistration that was never frozen\n",
        metadata={"run_role": "primary"},
    )["id"]
    state.hook_state["forwarded_input_ids"] = [frozen_id, draft_id]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Use the preregistration selected by the caller.",
    }

    classified = _classify(state, scope=scope)

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_ambiguous"
    assert set(classified["ambiguous_preregs"]) == {frozen_id, draft_id}
    assert _receipt_events(state) == []


def test_unforwarded_project_pure_draft_leaves_omitted_assignment_pending(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.save_artifact(
        "pre_registration",
        "unrelated_pure_draft",
        "# unrelated project preregistration that was never frozen\n",
        metadata={"run_role": "primary"},
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build an unrelated local tool.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert receipt["prereg_assignment"] == {"kind": "pending"}
    assert receipt["governing_task_input_binding"] is None
    assert receipt["binding_source"] == "none"


def test_operation_omission_remains_pending_amid_unselected_amendment(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "unselected_amendment", run_role="primary")
    state.save_artifact(
        "pre_registration",
        "unselected_amendment",
        "# pending amendment\n",
        amendment_reason="A different task is preparing a later protocol.",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": (
            "Build an unrelated local tool without consuming the pending protocol."
        ),
    }

    classified = _classify(state, scope="operation")

    assert classified["status"] == "success", classified
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert receipt["prereg_assignment"] == {"kind": "pending"}
    assert receipt["governing_task_input_binding"] is None
    assert receipt["binding_source"] == "none"
    contract = load_run_contract(state)
    assert contract["prereg_artifact_id"] is None
    assert contract["execution_mode"] == "operational"
    audit = audit_execution_intent_binding(state, require=True)
    assert audit["passed"] is True, audit


def test_forwarded_selection_beats_project_ambiguity_and_missing_role_is_scientific(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "other", run_role="primary", execution_mode="scientific")
    selected = _save_frozen_prereg(state, "selected")
    state.hook_state["forwarded_input_ids"] = [selected]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the selected preregistered task.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert receipt["binding_source"] == "selected_input"
    assert receipt["governing_task_input_binding"]["artifact_id"] == selected


def test_explicit_node_input_wins_over_different_forwarded_prereg(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    explicit = _save_frozen_prereg(state, "explicit")
    forwarded = _save_frozen_prereg(state, "forwarded")
    state.hook_state["forwarded_input_ids"] = [forwarded]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": explicit,
        "experiment_focus": "Run the explicitly named preregistration.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert receipt["binding_source"] == "explicit_node_input"
    assert receipt["governing_task_input_binding"]["artifact_id"] == explicit


def test_explicit_node_input_wins_over_forwarded_pure_draft(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    explicit = _save_frozen_prereg(state, "explicit")
    forwarded_draft = state.save_artifact(
        "pre_registration",
        "forwarded_pure_draft_but_not_governing",
        "# caller-forwarded draft that the explicit binding supersedes\n",
        metadata={"run_role": "primary"},
    )["id"]
    state.hook_state["forwarded_input_ids"] = [forwarded_draft]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": explicit,
        "experiment_focus": "Run the explicitly named frozen preregistration.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert receipt["binding_source"] == "explicit_node_input"
    assert receipt["governing_task_input_binding"]["artifact_id"] == explicit


def test_load_contract_forces_explicit_secondary_prereg_scientific_before_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "secondary_before_classification",
        run_role="secondary",
        execution_mode="operational",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Read the exact frozen prereg contract before classification.",
    }

    contract = load_run_contract(state)

    assert contract["prereg_artifact_id"] == prereg_id
    assert contract["prereg_binding_source"] == "explicit_node_input"
    assert contract["execution_mode"] == "scientific"


def test_load_contract_forces_missing_role_prereg_scientific_before_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "missing_role_before_classification",
        execution_mode="operational",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Read a frozen prereg that has no legacy run_role.",
    }

    contract = load_run_contract(state)

    assert contract["prereg_artifact_id"] == prereg_id
    assert contract["run_role"] == "secondary"
    assert contract["execution_mode"] == "scientific"


@pytest.mark.parametrize(
    "invalid_version",
    [True, 1.9, 0, -1, 0.0, -1.0, "0", "-1", "1.0", "not-a-version"],
)
def test_declared_prereg_version_requires_positive_plain_integer(
    tmp_path: Path, invalid_version: object,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "invalid_version")
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "prereg_version": invalid_version,
        "experiment_focus": "Run one exact preregistration version.",
    }

    classified = _classify(state, scope="operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_version_invalid"
    assert _receipt_events(state) == []


@pytest.mark.parametrize(
    ("declared_version", "expected_version"),
    [("1", 1), (" 1 ", 1), (1.0, 1), (2.0, 2), ("", 2), ("   ", 2), (None, 2)],
)
def test_declared_prereg_version_normalizes_equivalent_selectors(
    tmp_path: Path, declared_version: object, expected_version: int,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "serialized_version")
    state.save_artifact(
        "pre_registration",
        "serialized_version",
        "# frozen preregistration: serialized_version v2\n",
        amendment_reason="Create a newer head so exact v1 selection is observable.",
    )
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "prereg_version": declared_version,
        "experiment_focus": "Run one exact preregistration version.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)

    assert classified["status"] == "success", classified
    assert receipt["passed"] is True, receipt
    assert (
        receipt["receipt"]["governing_task_input_binding"]["version"]
        == expected_version
    )


def test_empty_node_input_key_is_hashable_but_not_exposed_as_internal_receipt_field(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "": "model supplied an empty JSON object key",
        "experiment_focus": "Verify one package without rewriting caller input.",
    }

    classified = _classify(state, scope="operation")
    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    audit = audit_execution_intent_binding(state, require=False)

    assert classified["status"] == "success", classified
    assert "intent_input_keys" not in classified["classification"]
    assert resolved["passed"] is True, resolved
    assert "" in resolved["receipt"]["intent_input_keys"]
    assert audit["passed"] is True, audit
    assert "intent_input_keys" not in audit


def test_declared_missing_prereg_version_cannot_fall_back_to_head(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "missing_version")
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "prereg_version": 999,
        "experiment_focus": "Run exactly preregistration version 999.",
    }

    classified = _classify(state, scope="operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_declared_prereg_unavailable"
    assert _receipt_events(state) == []


def test_declared_exact_old_frozen_version_is_a_legal_amendment_escape(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "versioned")
    state.save_artifact(
        "pre_registration",
        "versioned",
        "# unfrozen version two",
        amendment_reason="Prepare a later protocol without changing this run.",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "prereg_version": 1,
        "experiment_focus": "Run the explicitly selected frozen version one.",
    }

    classified = _classify(state, scope="operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert receipt["governing_task_input_binding"]["version"] == 1
    assert receipt["binding_source"] == "explicit_node_input"


def test_omitted_assignment_never_claims_sole_candidate_and_remains_stable(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "original")
    node_inputs = {"experiment_focus": "Build without an upstream prereg assignment."}
    state.hook_state["node_inputs"] = dict(node_inputs)
    assert _classify(state, scope="operation")["status"] == "success"
    first = resolve_run_acceptance(state, bind_if_absent=False)
    assert first["receipt"]["schema_version"] == 2
    assert first["receipt"]["prereg_assignment"] == {"kind": "pending"}
    assert first["receipt"]["governing_task_input_binding"] is None
    assert first["receipt"]["binding_source"] == "none"

    _save_frozen_prereg(state, "unrelated")
    state.hook_state.clear()
    second = resolve_run_acceptance(state, bind_if_absent=False)

    assert second["receipt"] == first["receipt"]
    assert load_run_contract(state)["prereg_artifact_id"] is None

    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = dict(node_inputs)
    reopened_result = resolve_run_acceptance(reopened, bind_if_absent=False)
    assert reopened_result["receipt"] == first["receipt"]

def test_pending_receipt_reopens_with_operational_scope_projection(
    tmp_path: Path,
) -> None:
    node_inputs = {"experiment_focus": "Mechanically verify one package."}
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = dict(node_inputs)
    classified = _classify(state, scope="operation")
    first = resolve_run_acceptance(state, bind_if_absent=False)
    assert classified["status"] == "success", classified
    assert first["receipt"]["governing_task_input_binding"] is None
    assert first["receipt"]["prereg_assignment"] == {"kind": "pending"}

    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = dict(node_inputs)
    resolved = resolve_run_acceptance(reopened, bind_if_absent=False)
    contract = load_run_contract(reopened)
    audit = audit_execution_intent_binding(reopened, require=True)

    assert resolved["receipt"] == first["receipt"]
    assert resolved["durable_scope_projection"]["mode"] == "operational"
    assert resolved["durable_scope_projection"]["category"] == "toolchain_build"
    assert contract["execution_mode"] == "operational"
    assert contract["operation_kind"] == "toolchain_build"
    assert audit["passed"] is True, audit
    assert audit["scope_mode"] == "operational"


def test_repeated_identical_receipts_ignore_transcript_timestamp(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    assert _classify(state)["status"] == "success"
    event = _receipt_events(state)[0]
    payload = _event_payload(event)

    state.append_transcript(_RECEIPT_EVENT, at="2099-01-01T00:00:00+00:00", **payload)
    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is True, resolved
    assert resolved["status"] == "bound"
    assert resolved["identical_event_count"] == 2


def test_jsonl_reducer_does_not_split_legal_unicode_line_separator(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    assert _classify(state)["status"] == "success"
    before = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    state.append_transcript("unicode_note", text="left\u2028right")

    after = resolve_run_acceptance(state, bind_if_absent=False)

    assert after["passed"] is True, after
    assert after["receipt"] == before


def test_unpaired_surrogate_input_is_structured_unavailable_not_a_crash(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "\ud800"}

    classified = _classify(state, scope="operation")
    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    audit = audit_execution_intent_binding(state, require=True)

    assert classified["status"] == "success", classified
    assert resolved["passed"] is True, resolved
    assert resolved["receipt"]["intent_status"] == "unavailable_at_acceptance"
    assert audit["passed"] is False
    assert audit["status"] == "intent_unavailable_at_classification"


def test_receipt_digest_is_plain_canonical_payload_sha256(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    assert _classify(state)["status"] == "success"
    payload = _event_payload(_receipt_events(state)[0])
    receipt_digest = payload.pop("receipt_digest")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    assert receipt_digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_non_utf8_transcript_fails_closed_as_unreadable(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.transcript_path.write_bytes(b"\xff\xfe\x00")

    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_unreadable"


def test_distinct_valid_receipts_fail_closed_instead_of_selecting_latest(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    assert _classify(state)["status"] == "success"
    payload = _event_payload(_receipt_events(state)[0])
    payload["intent_input_keys"] = ["experiment_focus", "unexpected"]
    payload["receipt_digest"] = _run_acceptance_receipt_digest(payload)
    state.append_transcript(_RECEIPT_EVENT, **payload)

    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_conflict"
    assert resolved["distinct_receipt_count"] == 2


@pytest.mark.parametrize("damage_kind", ["unreadable", "conflict"])
def test_damaged_receipt_preserves_durable_primary_scientific_verdict_obligation(
    tmp_path: Path, damage_kind: str,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "durable_primary",
        run_role="primary",
        execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Execute the primary scientific preregistration.",
    }
    assert _classify(state, scope="scientific")["status"] == "success"
    receipt_payload = _event_payload(_receipt_events(state)[0])
    state.hook_state.clear()

    if damage_kind == "unreadable":
        with state.transcript_path.open("a", encoding="utf-8") as stream:
            stream.write('{"event":"interrupted')
    else:
        receipt_payload["intent_input_keys"] = ["different"]
        receipt_payload["receipt_digest"] = _run_acceptance_receipt_digest(
            receipt_payload
        )
        state.append_transcript(_RECEIPT_EVENT, **receipt_payload)

    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    contract = load_run_contract(state)
    manifest = create_run_manifest(state, status="blocked")
    audit = audit_execution_intent_binding(state, require=True)

    assert resolved["passed"] is False
    assert resolved["status"] == f"run_authority_receipt_{damage_kind}"
    assert contract["execution_contract_valid"] is False
    assert contract["execution_mode"] == "scientific"
    assert contract["run_role"] == "primary"
    assert contract["requires_hypothesis_verdict"] is True
    assert contract["analysis_eligible"] is False
    assert contract["review_eligible"] is False
    assert contract["verdict_obligation_status"] == (
        "required_from_durable_scope_witness"
    )
    assert contract["run_authority_identity_status"] == "durable_scope_witness"
    assert contract["contract_source"] == (
        "durable_scope_transcript:run_authority_failure"
    )
    assert "run_role_missing_defaulted_to_secondary" not in (
        contract.get("contract_warnings") or []
    )
    assert manifest["requires_hypothesis_verdict"] is True
    assert manifest["analysis_eligible"] is False
    assert manifest["review_eligible"] is False
    assert manifest["verdict_obligation_status"] == (
        "required_from_durable_scope_witness"
    )
    assert manifest["run_authority_identity_status"] == "durable_scope_witness"
    assert audit["passed"] is False
    assert audit["status"] == resolved["status"]


def test_secondary_scope_cannot_become_a_durable_primary_witness_after_damage(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "durable_secondary",
        run_role="secondary",
        execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Execute a secondary scientific support run.",
    }
    assert _classify(state, scope="scientific")["status"] == "success"
    state.hook_state.clear()
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"interrupted')

    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    contract = load_run_contract(state)

    assert resolved["status"] == "run_authority_receipt_unreadable"
    assert "durable_verdict_obligation_witness" not in resolved
    assert contract["run_authority_identity_status"] == "unknown"
    assert contract["run_role"] == "unknown"
    assert contract["execution_mode"] == "unknown"


def test_bound_receipt_ignores_a_non_scientific_hook_cache_mutation(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "bound_scientific",
        run_role="secondary",
        execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Execute the exact frozen scientific input.",
    }
    assert _classify(state, scope="scientific")["status"] == "success"
    state.hook_state["experiment_execution_scope"]["mode"] = "operational"
    state.hook_state["experiment_execution_scope"]["category"] = "other"
    state.hook_state["_request_mode"] = "operation"

    audit = audit_execution_intent_binding(state, require=True)
    contract = load_run_contract(state)

    assert audit["passed"] is True, audit
    assert audit["status"] == "bound"
    assert audit["scope_mode"] == "scientific"
    assert audit["prereg_binding"]["artifact_id"] == prereg_id
    assert contract["execution_mode"] == "scientific"


def test_damaged_receipt_without_durable_scope_witness_is_explicitly_unknown_and_conservative(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.append_transcript(
        "experiment_scope_classified",
        mode="scientific",
        run_role="primary",
        source="governing_task_input_binding",
        run_acceptance_receipt_digest="b" * 64,
        intent_digest="c" * 64,
        binding_source="explicit_node_input",
        governing_task_input_binding={
            "artifact_id": "pre_registration__forged",
            "version": 1,
            "content_hash": "a" * 64,
        },
    )
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"interrupted')

    contract = load_run_contract(state)
    manifest = create_run_manifest(state, status="blocked")
    audit = audit_execution_intent_binding(state, require=True)

    assert contract["execution_contract_valid"] is False
    assert contract["execution_mode"] == "unknown"
    assert contract["run_role"] == "unknown"
    assert contract["requires_hypothesis_verdict"] is True
    assert contract["analysis_eligible"] is False
    assert contract["review_eligible"] is False
    assert contract["verdict_obligation_status"] == "unknown_fail_closed_required"
    assert contract["run_authority_identity_status"] == "unknown"
    assert contract["contract_source"] == "run_authority_failure"
    assert "run_role_missing_defaulted_to_secondary" not in (
        contract.get("contract_warnings") or []
    )
    assert manifest["requires_hypothesis_verdict"] is True
    assert manifest["execution_mode"] == "unknown"
    assert manifest["run_role"] == "unknown"
    assert manifest["analysis_eligible"] is False
    assert manifest["review_eligible"] is False
    assert manifest["verdict_obligation_status"] == "unknown_fail_closed_required"
    assert manifest["run_authority_identity_status"] == "unknown"
    assert audit["passed"] is False
    assert audit["status"] == "run_authority_receipt_unreadable"


@pytest.mark.parametrize("mismatch", ["digest", "binding"])
def test_forged_scope_must_match_the_preceding_valid_receipt_exactly(
    tmp_path: Path, mismatch: str,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(
        state,
        "accepted_without_scope",
        run_role="primary",
        execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Accept the exact input without classifying yet.",
    }
    accepted = resolve_run_acceptance(
        state,
        bind_if_absent=True,
        requested_mode="scientific",
    )
    assert accepted["passed"] is True, accepted
    receipt = accepted["receipt"]
    forged_binding = dict(receipt["governing_task_input_binding"])
    forged_digest = receipt["receipt_digest"]
    if mismatch == "digest":
        forged_digest = "f" * 64
    else:
        forged_binding["artifact_id"] = "pre_registration__different"
    state.append_transcript(
        "experiment_scope_classified",
        mode="scientific",
        category=None,
        reason="Attempt to forge a durable scientific scope projection.",
        invocation={},
        run_role="primary",
        source="governing_task_input_binding",
        run_acceptance_receipt_digest=forged_digest,
        intent_schema_version=receipt["schema_version"],
        intent_digest=receipt["intent_digest"],
        intent_source=receipt["intent_source"],
        intent_status=receipt["intent_status"],
        binding_source=receipt["binding_source"],
        governing_task_input_binding=forged_binding,
    )
    before_damage = resolve_run_acceptance(state, bind_if_absent=False)
    assert before_damage["passed"] is True, before_damage
    assert "durable_scope_projection" not in before_damage
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"interrupted')
    state.hook_state.clear()

    contract = load_run_contract(state)

    assert contract["execution_mode"] == "unknown"
    assert contract["run_role"] == "unknown"
    assert contract["requires_hypothesis_verdict"] is True
    assert contract["verdict_obligation_status"] == "unknown_fail_closed_required"
    assert "durable_verdict_obligation_witness" not in contract


def test_boolean_schema_receipt_is_invalid_even_with_matching_digest(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    assert _classify(state)["status"] == "success"
    payload = _event_payload(_receipt_events(state)[0])
    payload["schema_version"] = True
    payload["receipt_digest"] = _run_acceptance_receipt_digest(payload)
    state.append_transcript(_RECEIPT_EVENT, **payload)

    resolved = resolve_run_acceptance(state, bind_if_absent=True)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_invalid"
    assert len(_receipt_events(state)) == 2


def test_foreign_run_receipt_is_rejected_and_cannot_enable_live_catalog_scan(
    tmp_path: Path,
) -> None:
    source = State.new("experiment", tmp_path / "source")
    source.hook_state["node_inputs"] = {"experiment_focus": "Source run."}
    assert _classify(source)["status"] == "success"
    foreign_payload = _event_payload(_receipt_events(source)[0])

    target = State.new("experiment", tmp_path / "target")
    _save_frozen_prereg(target, "live_but_unaccepted", run_role="primary")
    target.hook_state["node_inputs"] = {
        "experiment_focus": "Target run must not accept another run's receipt.",
    }
    target.append_transcript(_RECEIPT_EVENT, **foreign_payload)

    resolved = resolve_run_acceptance(target, bind_if_absent=False)
    contract = load_run_contract(target)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_invalid"
    assert "run_id does not match" in resolved["reason"]
    assert contract["prereg_artifact_id"] is None
    assert contract["prereg_binding_source"] == "none"
    assert contract["execution_contract_valid"] is False
    assert contract["run_acceptance_status"] == "run_authority_receipt_invalid"


def test_missing_transcript_receipt_cannot_be_reconstructed_from_hook_cache(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "cached_projection", run_role="primary")
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Bind once, then prove hook state cannot replace transcript.",
    }
    assert _classify(state, scope="scientific")["status"] == "success"
    assert isinstance(state.hook_state.get("experiment_execution_scope"), dict)
    state.transcript_path.unlink()

    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    contract = load_run_contract(state)
    rebound = resolve_run_acceptance(state, bind_if_absent=True)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_missing"
    assert resolved["receipt"] is None
    assert contract["prereg_artifact_id"] is None
    assert contract["prereg_binding_source"] == "none"
    assert contract["execution_contract_valid"] is False
    assert (
        contract["run_acceptance_status"]
        == "run_authority_receipt_missing_for_existing_scope"
    )
    assert rebound["passed"] is False
    assert rebound["status"] == "run_authority_receipt_missing_for_existing_scope"
    assert rebound["receipt"] is None


def test_resolver_without_bind_permission_never_creates_receipt(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}

    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_missing"
    assert not state.transcript_path.exists()


def test_legacy_scope_cannot_be_reconstructed_as_new_authority(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}
    state.append_transcript(
        "experiment_scope_classified",
        mode="operational",
        category="other",
    )

    resolved = resolve_run_acceptance(state, bind_if_absent=True)

    assert resolved["passed"] is False
    assert resolved["status"] == "run_authority_receipt_missing_for_existing_scope"
    assert _receipt_events(state) == []


def test_concurrent_binders_converge_on_one_persisted_receipt(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Verify one package."}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: resolve_run_acceptance(state, bind_if_absent=True),
            range(2),
        ))

    assert all(result["passed"] for result in results), results
    assert results[0]["receipt"] == results[1]["receipt"]
    assert len(_receipt_events(state)) == 1


def test_self_contained_run_acceptance_probe_is_executed_by_pytest() -> None:
    probe = (
        Path(__file__).parent
        / "probes"
        / "probe_run_acceptance_receipt.py"
    )

    runpy.run_path(str(probe), run_name="__main__")


def test_request_mode_projection_cannot_change_receipt_authority(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen_prereg(state, "authority", run_role="secondary")
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Execute the frozen preregistered task.",
    }
    assert _classify(state, scope="scientific")["status"] == "success"
    before = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    state.hook_state["_request_mode"] = "operation"
    audit = audit_execution_intent_binding(state, require=True)
    after = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert audit["passed"] is True, audit
    assert after == before
