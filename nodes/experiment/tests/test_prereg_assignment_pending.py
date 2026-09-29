"""Base-red contract tests for explicit preregistration assignment.

These tests deliberately cover only the part of #1099 that is consistent with
the durable Experiment rules:

* project catalog visibility is observation, never assignment authority;
* an omitted assignment is pending, even with zero or one visible candidate;
* existing caller selections remain exact bindings;
* a typed explicit-none assignment means that this run has no governing
  preregistration.  It does not choose operation versus scientific scope;
* historical v1 ``unique_auto_claim`` receipts remain readable verbatim.

The durable ``nodes/experiment/AGENTS.md`` semantics govern this suite: typed
explicit-none permits exploratory science, while an exact-bound run remains
scientific and cannot use the operation closure path.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    enforce_execution_route,
    load_canonical_route,
)
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.resource_manager import _submit_job
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    _run_acceptance_receipt_digest,
    audit_prereg_assignment,
    execution_intent_snapshot,
    load_run_contract,
    prereg_assignment_scientific_block,
    resolve_run_acceptance,
)

_RECEIPT_EVENT = "run_acceptance_receipt_recorded"
_PENDING_EVENT = "experiment_prereg_assignment_pending"


def _save_frozen_prereg(state: State, name: str) -> tuple[str, dict]:
    artifact_id = state.save_artifact(
        "pre_registration",
        name,
        f"# frozen preregistration: {name}\n",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )["id"]
    state.mark_frozen(artifact_id)
    record = state.read_artifact(artifact_id)
    assert isinstance(record, dict)
    return artifact_id, record


def _classify(state: State, scope: str) -> dict:
    return asyncio.run(_classify_experiment_scope(
        state,
        scope=scope,
        operation_category="toolchain_build",
        reason="Record the caller assignment before any real effect.",
    ))


def _scientific_route() -> dict:
    return {
        "schema_version": 2,
        "goal": "Run one scientific measurement assigned by the caller.",
        "evidence_refs": ["user_original_input"],
        "steps": [{
            "id": "measure",
            "goal": "Produce the requested measurement.",
            "after": [],
            "action": {"tool": "submit_job", "program": "./measure"},
            "effects": [
                "workspace_write", "external_job", "scientific_execution",
            ],
            "workdir_role": "run_root",
            "expected_outputs": ["measurement.json"],
        }],
    }


def _mechanical_route() -> dict:
    return {
        "schema_version": 2,
        "goal": "Build one operational tool without scientific execution.",
        "evidence_refs": ["user_original_input"],
        "steps": [{
            "id": "build",
            "goal": "Build the requested tool.",
            "after": [],
            "action": {"tool": "submit_job", "program": "./build"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": ["tool.bin"],
        }],
    }


def _events(state: State, event_name: str) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [event for event in events if event.get("event") == event_name]


def _assignment(receipt: dict) -> dict:
    assignment = receipt.get("prereg_assignment")
    assert isinstance(assignment, dict), receipt
    return assignment


def _assert_v2_durable_receipt_has_no_legacy_aliases(state: State) -> dict:
    receipts = _events(state, _RECEIPT_EVENT)
    assert len(receipts) == 1, receipts
    durable = receipts[0]
    assert durable["schema_version"] == 2
    assert "governing_task_input_binding" not in durable
    assert "binding_source" not in durable
    return durable


def _rewrite_transcript(state: State, events: list[dict]) -> None:
    state.transcript_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def _canonical_pending_witnesses(state: State, receipt: dict) -> list[dict]:
    visibility = receipt.get("unbound_prereg_visibility_witness") or {}
    candidates = list(visibility.get("frozen", []))
    candidates.extend({
        "artifact_id": item["artifact_id"],
        "version": item["latest_frozen_version"],
        "content_hash": item["latest_frozen_content_hash"],
    } for item in visibility.get("pending", []))
    candidates.sort(key=lambda item: (
        item["artifact_id"], item["version"], item["content_hash"],
    ))
    expected = {
        "run_acceptance_receipt_digest": receipt["receipt_digest"],
        "candidate_bindings": candidates,
        "authorizing": False,
        "next_action_owner": "dispatching_parent",
    }
    return [
        event
        for event in _events(state, _PENDING_EVENT)
        if all(event.get(key) == value for key, value in expected.items())
    ]


@pytest.mark.parametrize("candidate_count", [0, 1, 2])
def test_omitted_assignment_is_pending_and_catalog_candidates_do_not_authorize(
    tmp_path: Path,
    candidate_count: int,
) -> None:
    state = State.new("experiment", tmp_path)
    candidates = [
        _save_frozen_prereg(state, f"candidate_{index}")
        for index in range(candidate_count)
    ]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build an unrelated tool without choosing a study.",
    }

    classified = _classify(state, "operation")
    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "operational"
    assert resolved["passed"] is True, resolved
    assert _assignment(resolved["receipt"]) == {"kind": "pending"}
    assert resolved["receipt"].get("governing_task_input_binding") is None
    assert resolved["receipt"].get("binding_source") != "unique_auto_claim"
    _assert_v2_durable_receipt_has_no_legacy_aliases(state)

    pending = _events(state, _PENDING_EVENT)
    assert len(pending) == 1, pending
    assert pending[0]["authorizing"] is False
    assert pending[0]["next_action_owner"] == "dispatching_parent"
    assert pending[0]["candidate_bindings"] == [
        {
            "artifact_id": artifact_id,
            "version": int(record["version"]),
            "content_hash": record["content_hash"],
        }
        for artifact_id, record in candidates
    ]


def test_legacy_flat_explicit_id_remains_an_exact_upstream_binding(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    selected_id, selected = _save_frozen_prereg(state, "selected")
    _save_frozen_prereg(state, "unrelated")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the caller-selected preregistration.",
        "prereg_artifact_id": selected_id,
    }

    classified = _classify(state, "operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert _assignment(receipt) == {
        "kind": "bound",
        "artifact_id": selected_id,
        "version": int(selected["version"]),
        "content_hash": selected["content_hash"],
        "source": "explicit_node_input",
    }
    _assert_v2_durable_receipt_has_no_legacy_aliases(state)


def test_single_forwarded_prereg_remains_an_exact_upstream_binding(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    selected_id, selected = _save_frozen_prereg(state, "forwarded")
    _save_frozen_prereg(state, "unrelated")
    state.hook_state["forwarded_input_ids"] = [selected_id]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the preregistration selected upstream.",
    }

    classified = _classify(state, "operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert _assignment(receipt) == {
        "kind": "bound",
        "artifact_id": selected_id,
        "version": int(selected["version"]),
        "content_hash": selected["content_hash"],
        "source": "selected_input",
    }
    _assert_v2_durable_receipt_has_no_legacy_aliases(state)


def test_caller_cannot_claim_pending_assignment_authority(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Wait for the dispatching parent to decide.",
        "prereg_assignment": {"kind": "pending"},
    }

    classified = _classify(state, "operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_assignment_invalid"
    assert "omitted" in classified["run_acceptance"]["reason"]
    assert _events(state, _RECEIPT_EVENT) == []
    assert _events(state, _PENDING_EVENT) == []


def test_literal_none_in_legacy_artifact_id_namespace_fails_loud(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run without a governing preregistration.",
        "prereg_artifact_id": "none",
    }

    classified = _classify(state, "operation")

    assert classified["status"] == "error", classified
    assert classified["error"] != "prereg_assignment_pending"
    assert _events(state, _PENDING_EVENT) == []
    assert _events(state, _RECEIPT_EVENT) == []


@pytest.mark.parametrize(
    ("scope", "expected_mode"),
    [("operation", "operational"), ("scientific", "scientific")],
)
def test_typed_explicit_none_selects_no_prereg_without_changing_scope(
    tmp_path: Path,
    scope: str,
    expected_mode: str,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "unrelated_project_candidate")
    assignment = {
        "kind": "none",
        "reason": "This run does not consume any project preregistration.",
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the independently assigned task.",
        "prereg_assignment": assignment,
    }

    classified = _classify(state, scope)
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]

    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == expected_mode
    assert _assignment(receipt) == assignment
    assert receipt.get("governing_task_input_binding") is None
    assert _events(state, _PENDING_EVENT) == []
    _assert_v2_durable_receipt_has_no_legacy_aliases(state)


def test_pending_assignment_rejects_scientific_scope_before_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "visible_but_not_assigned")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Explore a scientific question without guessing its assignment.",
    }

    classified = _classify(state, "scientific")

    assert classified["status"] == "error", classified
    assert classified["error_code"] == "prereg_assignment_required"
    assert classified["assignment_status"] == "pending"
    assert classified["retryable_in_current_run"] is False
    assert classified["next_action"]["owner"] == "dispatching_parent"
    assert _events(state, "experiment_scope_classified") == []
    assert len(_events(state, _RECEIPT_EVENT)) == 1
    assert len(_events(state, _PENDING_EVENT)) == 1


def test_pending_scientific_refusal_lists_all_visible_prereg_candidates(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    candidates = [
        _save_frozen_prereg(state, name)
        for name in ("candidate_alpha", "candidate_beta")
    ]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run the scientific task only after the parent assigns it.",
    }

    classified = _classify(state, "scientific")

    expected = sorted(
        [
            {
                "artifact_id": artifact_id,
                "version": int(record["version"]),
                "content_hash": record["content_hash"],
            }
            for artifact_id, record in candidates
        ],
        key=lambda item: (
            item["artifact_id"], item["version"], item["content_hash"],
        ),
    )
    assert classified["status"] == "error", classified
    assert classified["error_code"] == "prereg_assignment_required"
    assert classified["assignment_status"] == "pending"
    assert classified["candidate_bindings"] == expected
    assert classified["next_action"]["owner"] == "dispatching_parent"
    assert _events(state, "experiment_scope_classified") == []


def test_pending_scientific_refusal_cannot_be_reclassified_in_same_child(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "The parent must resolve this scientific assignment.",
    }

    scientific = _classify(state, "scientific")
    operation = _classify(state, "operation")

    assert scientific["error_code"] == "prereg_assignment_required"
    assert operation["error_code"] == "prereg_assignment_required"
    assert operation["scientific_signal_source"] == "scope_classification"
    assert operation["retryable_in_current_run"] is False
    blocked_signals = _events(
        state, "experiment_prereg_assignment_scientific_signal_blocked",
    )
    assert len(blocked_signals) == 1
    assert blocked_signals[0]["assignment_status"] == "pending"
    assert blocked_signals[0]["authorizing"] is False
    assert _events(state, "experiment_scope_classified") == []


def test_pending_operation_rejects_later_scientific_route_declaration(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Keep assignment pending until the parent decides.",
    }
    assert _classify(state, "operation")["status"] == "success"

    declared = asyncio.run(_declare_execution_route(
        state, route=_scientific_route(),
    ))

    assert declared["status"] == "error", declared
    assert declared["error_code"] == "prereg_assignment_required"
    assert declared["scientific_signal_source"] == "route_declaration"
    assert declared["step_ids"] == ["measure"]
    assert load_canonical_route(state)["status"] != "ready"

    mechanical = asyncio.run(_declare_execution_route(
        state, route=_mechanical_route(),
    ))
    assert mechanical["status"] == "success", mechanical
    final_audit = audit_prereg_assignment(state)
    assert final_audit["passed"] is False, final_audit
    assert "prior_scientific_signal_requires_redispatch" in final_audit[
        "compatibility"
    ]["failure_reasons"]


def test_pending_operation_rejects_scientific_managed_action_admission(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Do not admit a scientific action before assignment.",
    }
    assert _classify(state, "operation")["status"] == "success"
    action = {
        "tool": "submit_job",
        "program": "./measure",
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["external_job", "scientific_execution"],
    }
    decision = {
        "decision": "matched_ready_step",
        "policy": "formal_scientific_execution",
        "route_step_id": "measure",
        "effective_effects": ["external_job", "scientific_execution"],
    }

    blocked = enforce_execution_route(
        state, action, decision, phase="pre_materialization",
    )

    assert blocked is not None
    assert blocked["error_code"] == "prereg_assignment_required"
    assert blocked["scientific_signal_source"] == "managed_action_admission"

    mechanical = enforce_execution_route(
        state,
        {
            "tool": "submit_job",
            "program": "./build",
            "read_only": False,
            "dry_run": True,
            "observed_effects": ["workspace_write"],
        },
        {
            "tool": "submit_job",
            "decision": "route_not_required",
            "policy": "guarded_unknown_effect",
            "read_only": False,
            "dry_run": True,
            "effective_effects": ["workspace_write"],
        },
        phase="pre_materialization",
    )
    assert mechanical is not None
    assert mechanical["error_code"] == "prereg_pending_operation_action_ineligible"
    assert mechanical["reason"] == "prior_scientific_signal_requires_redispatch"
    final_audit = audit_prereg_assignment(state)
    assert final_audit["passed"] is False, final_audit
    assert "prior_scientific_signal_requires_redispatch" in final_audit[
        "compatibility"
    ]["failure_reasons"]


def test_typed_none_conflicts_with_legacy_flat_exact_selector(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    selected_id, _selected = _save_frozen_prereg(state, "selected")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Contradictory caller assignment must fail.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "No governing preregistration for this run.",
        },
        "prereg_artifact_id": selected_id,
    }

    classified = _classify(state, "operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_assignment_invalid"
    assert "cannot be combined" in classified["run_acceptance"]["reason"]
    assert _events(state, _RECEIPT_EVENT) == []


def test_typed_none_conflicts_with_forwarded_preregistration(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    selected_id, _selected = _save_frozen_prereg(state, "forwarded")
    state.hook_state["forwarded_input_ids"] = [selected_id]
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Contradictory forwarded authority must fail.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "No governing preregistration for this run.",
        },
    }

    classified = _classify(state, "operation")

    assert classified["status"] == "error", classified
    assert classified["error"] == "run_authority_prereg_assignment_invalid"
    assert "caller-forwarded" in classified["run_acceptance"]["reason"]
    assert _events(state, _RECEIPT_EVENT) == []


def test_existing_pending_receipt_repairs_missing_witness_without_reminting(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    _save_frozen_prereg(state, "visible_candidate")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run an unrelated operation.",
    }
    classified = _classify(state, "operation")
    assert classified["status"] == "success", classified
    durable_receipt = _assert_v2_durable_receipt_has_no_legacy_aliases(state)

    _rewrite_transcript(state, [durable_receipt])
    repaired = resolve_run_acceptance(state, bind_if_absent=True)

    assert repaired["passed"] is True, repaired
    assert len(_events(state, _RECEIPT_EVENT)) == 1
    assert len(_canonical_pending_witnesses(state, repaired["receipt"])) == 1


@pytest.mark.parametrize(
    "bad_witness",
    [
        {"run_acceptance_receipt_digest": "0" * 64},
        {
            "run_acceptance_receipt_digest": "0" * 64,
            "candidate_bindings": [],
            "authorizing": False,
            "next_action_owner": "dispatching_parent",
        },
        {
            "candidate_bindings": [],
            "authorizing": True,
            "next_action_owner": "dispatching_parent",
        },
    ],
    ids=["partial", "wrong-receipt", "authorizing"],
)
def test_noncanonical_pending_witness_does_not_suppress_repair(
    tmp_path: Path,
    bad_witness: dict,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run an operation with no upstream assignment.",
    }
    classified = _classify(state, "operation")
    assert classified["status"] == "success", classified
    durable_receipt = _assert_v2_durable_receipt_has_no_legacy_aliases(state)
    _rewrite_transcript(state, [durable_receipt])
    state.append_transcript(_PENDING_EVENT, **bad_witness)

    repaired = resolve_run_acceptance(state, bind_if_absent=True)

    assert repaired["passed"] is True, repaired
    assert len(_events(state, _RECEIPT_EVENT)) == 1
    assert len(_canonical_pending_witnesses(state, repaired["receipt"])) == 1
    assert len(_events(state, _PENDING_EVENT)) == 2


def test_repeated_pending_resolution_keeps_one_canonical_witness(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run an operation with no upstream assignment.",
    }
    classified = _classify(state, "operation")
    assert classified["status"] == "success", classified

    for _ in range(3):
        resolved = resolve_run_acceptance(state, bind_if_absent=True)
        assert resolved["passed"] is True, resolved

    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert len(_events(state, _RECEIPT_EVENT)) == 1
    assert len(_canonical_pending_witnesses(state, receipt)) == 1
    assert len(_events(state, _PENDING_EVENT)) == 1


def test_unknown_acceptance_receipt_schema_is_rejected(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.append_transcript(
        _RECEIPT_EVENT,
        schema_version=999,
        run_id=state.run_id,
        receipt_digest="0" * 64,
    )

    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is False, resolved
    assert resolved["status"] == "run_authority_receipt_invalid"
    assert "schema_version" in resolved["reason"]


def test_v1_null_receipt_is_readable_but_cannot_authorize_new_science(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Resume a legacy run without inventing prereg authority.",
    }
    intent = execution_intent_snapshot(state)
    legacy = {
        "schema_version": 1,
        "run_id": state.run_id,
        "intent_digest": intent["intent_digest"],
        "intent_source": intent["source"],
        "intent_input_keys": intent["input_keys"],
        "intent_status": "bound_at_acceptance",
        "governing_task_input_binding": None,
        "binding_source": "none",
        "unbound_prereg_visibility_witness": {"frozen": [], "pending": []},
    }
    legacy["receipt_digest"] = _run_acceptance_receipt_digest(legacy)
    state.append_transcript(_RECEIPT_EVENT, **legacy)

    readable = resolve_run_acceptance(state, bind_if_absent=False)
    classified = _classify(state, "scientific")
    final_audit = audit_prereg_assignment(state)

    assert readable["passed"] is True, readable
    assert _assignment(readable["receipt"]) == {"kind": "pending"}
    assert classified["status"] == "error", classified
    assert classified["error_code"] == "prereg_assignment_required"
    assert classified["assignment_status"] == "pending"
    assert classified["retryable_in_current_run"] is False
    assert classified["next_action"]["owner"] == "dispatching_parent"
    assert final_audit["passed"] is False, final_audit
    assert final_audit["status"] == "pending"


def test_v1_unique_auto_claim_receipt_reopens_with_its_original_exact_binding(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    original_id, original = _save_frozen_prereg(state, "legacy_original")
    node_inputs = {
        "experiment_focus": "Resume the historical auto-claimed run exactly.",
    }
    state.hook_state["node_inputs"] = dict(node_inputs)
    intent = execution_intent_snapshot(state)
    legacy = {
        "schema_version": 1,
        "run_id": state.run_id,
        "intent_digest": intent["intent_digest"],
        "intent_source": intent["source"],
        "intent_input_keys": intent["input_keys"],
        "intent_status": "bound_at_acceptance",
        "governing_task_input_binding": {
            "artifact_id": original_id,
            "version": int(original["version"]),
            "content_hash": original["content_hash"],
        },
        "binding_source": "unique_auto_claim",
        "unbound_prereg_visibility_witness": None,
    }
    legacy["receipt_digest"] = _run_acceptance_receipt_digest(legacy)
    state.append_transcript(_RECEIPT_EVENT, **legacy)
    _save_frozen_prereg(state, "later_unrelated")

    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = dict(node_inputs)
    resolved = resolve_run_acceptance(reopened, bind_if_absent=False)
    contract = load_run_contract(reopened)
    classified = _classify(reopened, "scientific")
    final_audit = audit_prereg_assignment(reopened)

    assert resolved["passed"] is True, resolved
    receipt = resolved["receipt"]
    for key, value in legacy.items():
        assert receipt[key] == value
    assert contract["prereg_artifact_id"] == original_id
    assert contract["prereg_version"] == int(original["version"])
    assert contract["prereg_content_hash"] == original["content_hash"]
    assert contract["prereg_binding_source"] == "unique_auto_claim"
    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    assert final_audit["passed"] is True, final_audit
    assert final_audit["status"] == "legacy_bound"
    assert len(_events(reopened, _RECEIPT_EVENT)) == 1
    assert _events(reopened, _PENDING_EVENT) == []


@pytest.mark.parametrize("scope", ["operation", "scientific"])
def test_typed_none_does_not_depend_on_unrelated_catalog_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute without a governing preregistration.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "The caller explicitly assigned no preregistration.",
        },
    }

    def unrelated_catalog_failure(*_args: object, **_kwargs: object) -> list[dict]:
        raise OSError("unrelated catalog is unavailable")

    monkeypatch.setattr(state, "list_artifacts", unrelated_catalog_failure)

    classified = _classify(state, scope)

    assert classified["status"] == "success", classified
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert _assignment(receipt)["kind"] == "none"


def test_typed_exact_bound_does_not_depend_on_unrelated_catalog_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = State.new("experiment", tmp_path)
    artifact_id, record = _save_frozen_prereg(state, "exact_only")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the exact caller-selected protocol.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": artifact_id,
            "version": int(record["version"]),
            "content_hash": record["content_hash"],
        },
    }

    def unrelated_catalog_failure(*_args: object, **_kwargs: object) -> list[dict]:
        raise OSError("unrelated catalog is unavailable")

    monkeypatch.setattr(state, "list_artifacts", unrelated_catalog_failure)

    classified = _classify(state, "scientific")

    assert classified["status"] == "success", classified
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert _assignment(receipt) == {
        "kind": "bound",
        "artifact_id": artifact_id,
        "version": int(record["version"]),
        "content_hash": record["content_hash"],
        "source": "explicit_node_input",
    }


def test_typed_exact_bound_forces_scientific_scope_and_rejects_operation_closure(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    artifact_id, record = _save_frozen_prereg(state, "exact_scientific")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the preregistered solver exactly as assigned.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": artifact_id,
            "version": int(record["version"]),
            "content_hash": record["content_hash"],
        },
    }

    classified = _classify(state, "operation")
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    evidence = Path(state.root) / "claimed-operation.txt"
    evidence.write_text("operation-shaped subtask\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="close only the operation-shaped part of the preregistered run",
        outcome="blocked",
        checks=[{
            "name": "scientific_scope_preserved",
            "passed": False,
            "evidence": {"bound_prereg_artifact_id": artifact_id},
        }],
        artifact_paths=[str(evidence)],
        next_step="continue through the scientific evidence flow",
    ))

    assert classified["status"] == "success", classified
    assert classified["classification"]["requested_mode"] == "operational"
    assert classified["classification"]["mode"] == "scientific"
    assert receipt["schema_version"] == 2
    assert _assignment(receipt)["kind"] == "bound"
    assert load_run_contract(state)["execution_mode"] == "scientific"
    assert completion["status"] == "error", completion
    assert completion["error_code"] == "execution_intent_binding_required"
    assert completion["execution_intent_binding"]["scope_mode"] == "scientific"
    assert state.list_artifacts("raw_results") == []
    assert state.list_artifacts("clean_results") == []
    assert state.list_artifacts("experiment_log") == []


def test_pending_handoff_includes_latest_frozen_amendment_candidate(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    artifact_id, frozen = _save_frozen_prereg(state, "amending")
    state.save_artifact(
        "pre_registration",
        "amending",
        "# pending amendment\n",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
        amendment_reason="Update the protocol after review.",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run an unrelated operation pending assignment.",
    }

    classified = _classify(state, "operation")
    assert classified["status"] == "success", classified
    exact = {
        "artifact_id": artifact_id,
        "version": int(frozen["version"]),
        "content_hash": frozen["content_hash"],
    }
    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    assert _canonical_pending_witnesses(state, resolved["receipt"])
    assert audit_prereg_assignment(state)["candidate_bindings"] == [exact]
    blocked = prereg_assignment_scientific_block(
        state, signal_source="regression_test",
    )
    assert blocked is not None
    assert blocked["candidate_bindings"] == [exact]


def test_unreadable_receipt_cannot_reproject_mutable_typed_none(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Initial request."}
    assert _classify(state, "operation")["status"] == "success"
    state.transcript_path.write_text(
        state.transcript_path.read_text(encoding="utf-8") + "{invalid json\n",
        encoding="utf-8",
    )
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Mutated request must not replace authority.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This mutable value is not durable authority.",
        },
    }

    contract = load_run_contract(state)

    assert contract["run_acceptance_status"] == "run_authority_receipt_unreadable"
    assert contract["prereg_assignment"] == {"kind": "pending"}
    assert contract["execution_contract_valid"] is False


def test_v2_scope_projection_rejects_typed_assignment_mismatch(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exploratory scientific measurement.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "Exploratory science has no governing preregistration.",
        },
    }
    assert _classify(state, "scientific")["status"] == "success"
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for event in events:
        if event.get("event") == "experiment_scope_classified":
            event["prereg_assignment"] = {"kind": "pending"}
    _rewrite_transcript(state, events)

    resolved = resolve_run_acceptance(state, bind_if_absent=False)

    assert resolved["passed"] is True, resolved
    assert "durable_scope_projection" not in resolved


@pytest.mark.parametrize("task_kind", ["build", "toolchain_build"])
def test_pending_dry_run_cannot_close_a_successful_build(
    tmp_path: Path,
    task_kind: str,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the assigned tool without guessing prereg authority.",
    }
    assert _classify(state, "operation")["status"] == "success"
    rendered = asyncio.run(_submit_job(
        state,
        command="printf 'not a real build\\n'",
        scheduler="local",
        job_name=f"dry_run_{task_kind}",
        dry_run=True,
    ))
    assert rendered["status"] == "success", rendered
    assert rendered["dry_run"] is True
    assert rendered.get("job_id") is None
    compatibility = audit_prereg_assignment(state)["compatibility"]
    assert compatibility["compatibility_lane"] == "route_exempt_non_executing_diagnostic"
    assert compatibility["real_execution_obligation_satisfied"] is False

    dry_run_script = Path(rendered["script_path"])
    assert dry_run_script.is_file()
    result = asyncio.run(_record_operation_completion(
        state,
        task_kind=task_kind,
        objective="build the assigned tool",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(dry_run_script)],
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "operation_real_execution_required"
    obligation = result["real_execution_obligation"]
    assert obligation["operation_execution_obligation"][
        "real_execution_obligation_satisfied"
    ] is False
    assert state.list_artifacts("raw_results") == []
    assert state.list_artifacts("clean_results") == []
    assert state.list_artifacts("experiment_log") == []


def test_pending_compatibility_lane_observes_its_two_removal_signals() -> None:
    """Turn upstream capability changes into an explicit owner review failure."""
    import shared.tools.run_node  # noqa: F401 - registers the public tool schema
    from core.tool_registry import get_tool
    from nodes.experiment.tools.execution_action_census import (
        COMPATIBILITY_REMOVAL_SIGNALS,
    )

    run_node = get_tool("run_node")
    assert run_node is not None
    node_inputs = (
        run_node.parameters_schema.get("properties", {}).get("node_inputs", {})
    )
    node_input_properties = node_inputs.get("properties", {})
    typed_assignment_supported = bool(
        isinstance(node_input_properties, dict)
        and isinstance(node_input_properties.get("prereg_assignment"), dict)
    )
    node_inputs_description = str(node_inputs.get("description") or "")
    omission_still_advertised = bool(
        "prereg_artifact_id" in node_inputs_description
        and "只有一份时" in node_inputs_description
        and "可省略" in node_inputs_description
    )
    signals = {
        "run_node_schema_supports_typed_prereg_assignment": (
            typed_assignment_supported
        ),
        "callee_contract_no_longer_advertises_assignment_omission": (
            not omission_still_advertised
        ),
    }

    assert tuple(signals) == COMPATIBILITY_REMOVAL_SIGNALS
    assert not all(signals.values()), (
        "pending prereg compatibility 的两个删除信号都已成立；不要自动收紧运行行为，"
        "但必须由 Experiment owner 复议并删除或明确续期该兼容层"
    )


def test_typed_none_cannot_use_a_handwritten_file_as_build_execution(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build an independently assigned tool.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    assert _classify(state, "operation")["status"] == "success"
    evidence = Path(state.root) / "build.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="build an independently assigned tool",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "operation_real_execution_required"
    assert state.list_artifacts("raw_results") == []
    assert state.list_artifacts("clean_results") == []
    assert state.list_artifacts("experiment_log") == []


def test_toolchain_build_scope_rejects_generic_completion_kind(
    tmp_path: Path,
) -> None:
    """Caller vocabulary cannot downgrade the receipt-bound build obligation."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build an independently assigned tool.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    assert _classify(state, "operation")["status"] == "success"
    evidence = Path(state.root) / "claimed-build.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="build an independently assigned tool",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "operation_task_kind_mismatch"
    assert result["task_kind_mismatch"] == {
        "immutable_operation_kind": "toolchain_build",
        "requested_task_kind": "generic",
        "compatible_task_kinds": ["build", "external_job"],
    }
    assert result["retryable_in_current_run"] is True
    assert result["payload_must_not_rerun"] is True
    assert state.list_artifacts("raw_results") == []
    assert state.list_artifacts("clean_results") == []
    assert state.list_artifacts("experiment_log") == []


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
def test_toolchain_build_scope_allows_honest_nonsuccess_with_generic_kind(
    tmp_path: Path,
    outcome: str,
) -> None:
    """The physical-build obligation guards success, not honest termination."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Attempt a build and preserve its failure evidence.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    assert _classify(state, "operation")["status"] == "success"
    evidence = Path(state.root) / "build-failure.stdout"
    evidence.write_text("returncode=1\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="preserve the attempted build's non-success outcome",
        outcome=outcome,
        checks=[{
            "name": "build_succeeded",
            "passed": False,
            "evidence": {"returncode": 1},
        }],
        artifact_paths=[str(evidence)],
        next_step="inspect the failure before deciding whether to retry",
    ))

    assert result["status"] == "success", result
    assert result["outcome"] == outcome
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1


def test_generic_diagnostic_scope_keeps_nonexecuting_completion(
    tmp_path: Path,
) -> None:
    """An ordinary receipt-bound diagnostic remains a legitimate generic close."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Record a read-only environment diagnostic.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Record a read-only environment diagnostic.",
    ))
    assert classified["status"] == "success", classified
    evidence = Path(state.root) / "environment.txt"
    evidence.write_text("python=available\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="record a read-only environment diagnostic",
        outcome="success",
        checks=[{
            "name": "environment_observed",
            "passed": True,
            "evidence": {"python": "available"},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert result["status"] == "success", result
    assert result["outcome"] == "success"
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1


def test_explicit_build_kind_strengthens_nonbuild_scope(
    tmp_path: Path,
) -> None:
    """A caller may add, but cannot remove, a physical execution obligation."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Run an environment probe and build one helper.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Probe the environment before building one helper.",
    ))
    assert classified["status"] == "success", classified
    evidence = Path(state.root) / "claimed-helper.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="build one helper during the environment probe",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "operation_real_execution_required"
