"""Schema-v2 no-auto-claim coverage plus legacy v1 compatibility."""
from __future__ import annotations

import asyncio
from pathlib import Path

from core.state import State
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    _run_acceptance_receipt_digest,
    audit_execution_intent_binding,
    audit_prereg_assignment,
    execution_intent_snapshot,
    load_run_contract,
    resolve_run_acceptance,
)


_RECEIPT_EVENT = "run_acceptance_receipt_recorded"


def _sole_frozen_prereg(state: State, name: str, **metadata: object) -> str:
    artifact_id = state.save_artifact(
        "pre_registration",
        name,
        f"# sole frozen preregistration: {name}\n",
        metadata={"stage": "simulation", **metadata},
    )["id"]
    state.mark_frozen(artifact_id)
    return artifact_id


def _classify_operation(state: State) -> dict:
    return asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Build the solver toolchain; no scientific conclusion is requested.",
    ))


def _install_legacy_v1_unique_auto_receipt(
    state: State,
    prereg_id: str,
) -> dict:
    record = state.read_artifact(prereg_id)
    assert isinstance(record, dict)
    intent = execution_intent_snapshot(state)
    receipt = {
        "schema_version": 1,
        "run_id": state.run_id,
        "intent_digest": intent["intent_digest"],
        "intent_source": intent["source"],
        "intent_input_keys": intent["input_keys"],
        "intent_status": "bound_at_acceptance",
        "governing_task_input_binding": {
            "artifact_id": prereg_id,
            "version": int(record["version"]),
            "content_hash": record["content_hash"],
        },
        "binding_source": "unique_auto_claim",
        "unbound_prereg_visibility_witness": None,
    }
    receipt["receipt_digest"] = _run_acceptance_receipt_digest(receipt)
    state.append_transcript(_RECEIPT_EVENT, **receipt)
    return receipt


def test_sole_catalog_candidate_is_pending_not_auto_claimed_at_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _sole_frozen_prereg(
        state, "sole_project_prereg", run_role="primary", execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the solver toolchain before any measurement.",
    }

    classified = _classify_operation(state)

    assert classified["status"] == "success", classified
    classification = classified["classification"]
    assert classification["mode"] == "operational"
    assert classification["binding_source"] == "none"
    assert classification["governing_task_input_binding"] is None
    assert classification["prereg_assignment"] == {"kind": "pending"}
    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    assert acceptance["receipt"]["schema_version"] == 2
    assert acceptance["receipt"]["prereg_assignment"] == {"kind": "pending"}
    contract = load_run_contract(state)
    assert contract["prereg_artifact_id"] is None
    assert contract["prereg_binding_source"] == "none"
    assert contract["execution_mode"] == "operational"
    intent_audit = audit_execution_intent_binding(state, require=True)
    assert intent_audit["passed"] is True, intent_audit
    assignment_audit = audit_prereg_assignment(state)
    assert assignment_audit["passed"] is False, assignment_audit
    assert assignment_audit["status"] == "pending"


def test_load_contract_never_claims_a_sole_catalog_candidate_before_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _sole_frozen_prereg(
        state,
        "sole_operational_looking_prereg",
        run_role="secondary",
        execution_mode="operational",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Observe, but do not claim, the sole project preregistration.",
    }

    contract = load_run_contract(state)

    assert contract["prereg_artifact_id"] is None
    assert contract["prereg_binding_source"] == "none"
    assert contract["prereg_assignment"] == {"kind": "pending"}


def test_legacy_v1_unique_auto_receipt_remains_exact_and_scientific(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = _sole_frozen_prereg(
        state, "legacy_sole_prereg", run_role="primary", execution_mode="scientific",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Resume the historical auto-claimed scientific run.",
    }
    legacy = _install_legacy_v1_unique_auto_receipt(state, prereg_id)

    classified = _classify_operation(state)
    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert resolved["receipt"]["schema_version"] == 1
    assert resolved["receipt"]["receipt_digest"] == legacy["receipt_digest"]
    assert resolved["receipt"]["binding_source"] == "unique_auto_claim"
    assert resolved["receipt"]["governing_task_input_binding"]["artifact_id"] == prereg_id

    evidence = Path(state.root) / "configure.stdout"
    evidence.write_text("configure ok\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="configure the solver",
        outcome="success",
        checks=[{
            "name": "configure_returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert completion["status"] == "error", completion
    assert completion["error_code"] == "execution_intent_binding_required"
    assert "closure_id" not in completion
