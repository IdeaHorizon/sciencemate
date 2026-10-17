"""Integrity regressions for the Core-selected Experiment task input.

These tests use real State and task-contract records.  The synthetic receipt and
refresh rows below are reducer-validation probes: they do not claim that a public
production caller can append arbitrary ledger bytes.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

from core.ledger import record_version
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import ledger, run_bootstrap, run_contract


def _state(tmp_path) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        session_id="owner",
    )
    state.project_root = tmp_path / "project"
    (state.project_root / "tasks").mkdir(parents=True, exist_ok=True)
    state.hook_state["node_inputs"] = {
        "scope": "operation",
        "required_outputs": [],
        "prereg_assignment": {"kind": "none", "reason": "stale caller hint"},
    }
    return state


def _prereg(state: State, *, frozen: bool = True) -> dict:
    saved = state.save_artifact(
        "pre_registration", "prereg_task_integrity", "# protocol\n"
    )
    if frozen:
        state.mark_frozen(saved["id"])
    version = state.artifact_versions(saved["id"])[-1]
    return {
        "artifact_id": saved["id"],
        "version": record_version(version),
        "content_hash": version["content_hash"],
    }


def _bind(state: State, binding: dict):
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="task-integrity",
        objective="run the exact selected task",
        target_node="experiment",
        prereg_assignment=PreregAssignment.exact(
            binding["artifact_id"],
            version=str(binding["version"]),
            content_hash=binding["content_hash"],
        ),
        actor="_orchestrator:parent-run",
        reason="explicit dispatch",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_revision = revision.revision
    state.task_contract_digest = revision.digest
    state.parent_dispatch_id = "disp_integrity"
    return revision


def test_task_contract_receipt_source_needs_a_core_selected_identity(tmp_path):
    """A self-consistent receipt cannot merely label itself task_contract."""
    state = _state(tmp_path)
    binding = _prereg(state)
    assert state.task_instance_uuid is None
    assert state.task_contract_digest is None

    intent = run_contract.execution_intent_snapshot(state)
    payload = {
        "schema_version": 2,
        "run_id": state.run_id,
        "intent_digest": intent["intent_digest"],
        "intent_source": intent["source"],
        "intent_input_keys": intent["input_keys"],
        "intent_status": "bound_at_acceptance",
        "prereg_assignment": {
            "kind": "bound",
            **binding,
            "source": "task_contract",
        },
        "unbound_prereg_visibility_witness": None,
    }
    payload["receipt_digest"] = run_contract._run_acceptance_receipt_digest(payload)
    state.append_transcript(
        run_contract._RUN_ACCEPTANCE_EVENT,
        **run_contract._receipt_event_payload(payload),
    )

    reduced = run_contract.resolve_run_acceptance(state, bind_if_absent=False)
    assert reduced.get("passed") is not True, reduced
    assert reduced.get("status") != "bound", reduced


def test_same_contract_ignores_a_corrected_non_authorizing_prereg_hint(tmp_path):
    """Changing only the mutable mirror cannot change the accepted intent."""
    state = _state(tmp_path)
    binding = _prereg(state)
    _bind(state, binding)

    established = asyncio.run(run_bootstrap.establish(state))
    assert established["scope_status"] == "success", established
    before = run_contract.resolve_run_acceptance(state, bind_if_absent=False)
    before_audit = run_contract.audit_execution_intent_binding(state, require=True)
    assert before.get("passed") is True, before
    assert before_audit.get("passed") is True, before_audit

    # The Core-selected assignment is unchanged.  This only corrects the stale
    # caller mirror that the task-input view deliberately treats as a witness.
    state.hook_state["node_inputs"]["prereg_assignment"] = {
        "kind": "bound",
        **binding,
    }
    after = run_contract.resolve_run_acceptance(state, bind_if_absent=False)
    after_audit = run_contract.audit_execution_intent_binding(state, require=True)

    assert after.get("receipt", {}).get("receipt_digest") == before.get(
        "receipt", {}
    ).get("receipt_digest")
    assert after_audit.get("passed") is True, after_audit
    assert after_audit.get("status") != "intent_changed", after_audit


def test_refresh_is_only_invalid_to_exact_bound_for_the_same_contract(tmp_path):
    """A valid refresh is one-way; malformed later rows cannot redefine A."""
    state = _state(tmp_path)
    binding = _prereg(state, frozen=False)
    revision = _bind(state, binding)

    first = asyncio.run(run_bootstrap.establish(state))
    assert first["scope"]["prereg_status"] == "invalid", first
    state.mark_frozen(binding["artifact_id"])
    second = asyncio.run(run_bootstrap.establish(state))
    assert second["scope"]["prereg_status"] == "bound_valid", second

    records = ledger.open_ledger(state).records()
    refreshes = [row for row in records if row.get("kind") == "run_authority_refreshed"]
    assert len(refreshes) == 1, refreshes
    refresh = refreshes[0]
    assert refresh.get("previous_prereg_status") == "invalid", refresh
    assert refresh.get("authority", {}).get("prereg_status") == "bound_valid", refresh
    assert refresh.get("authority", {}).get("prereg") == binding, refresh
    assert refresh.get("authority", {}).get("task_contract", {}).get("digest") == revision.digest

    canonical = run_bootstrap.project_run_authority(records)
    assert canonical is not None and canonical["prereg_status"] == "bound_valid"
    malformed = {
        "kind": "run_authority_refreshed",
        "previous_prereg_status": "bound_valid",
        "authority": {
            **deepcopy(canonical),
            "prereg_status": "none",
            "prereg": None,
            "startup_blocker": None,
            "formal_input_authority": "unbound",
            "reason": "not a legal refresh transition",
        },
    }
    projected = run_bootstrap.project_run_authority([*records, malformed])
    assert projected == canonical, (canonical, projected)

    # Re-observing an already-valid binding is idempotent, not a second refresh.
    asyncio.run(run_bootstrap.establish(state))
    assert sum(
        row.get("kind") == "run_authority_refreshed"
        for row in ledger.open_ledger(state).records()
    ) == 1
