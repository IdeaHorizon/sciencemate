"""C4: operation closure consumers must share the triplet active view."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.hooks import _audit_operation_log
from nodes.experiment.tools import contract_audit, run_contract
from nodes.experiment.tools.contract_audit import (
    _current_run_supersessions,
    _operation_clean_results_freeze_errors,
    _operation_goal_effect_audit,
    _operation_result_bundle,
    _supersede_closure_draft,
    active_closure_artifacts,
)
from nodes.experiment.tools.operation_completion import _record_operation_completion


_TRIPLET_TYPES = ("raw_results", "clean_results", "experiment_log")


def _await(coroutine):
    return asyncio.run(coroutine)


def _operation_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Validate a bounded diagnostic closure.",
        "stage": "diagnostic",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This closure-view fixture has no governing preregistration.",
        },
    }
    result = _await(run_contract._classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Mechanical closure-view diagnostic; no scientific conclusion.",
    ))
    assert result["status"] == "success", result
    return state


def _complete_operation(state: State) -> dict:
    evidence = Path(state.root) / "operation.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")
    return _await(_record_operation_completion(
        state,
        task_kind="generic",
        objective="validate a bounded diagnostic closure",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))


def _preclassification_triplet(state: State) -> dict[str, str]:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Validate a bounded diagnostic closure.",
        "stage": "diagnostic",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This reclassification fixture has no governing preregistration.",
        },
    }
    scientific = _await(run_contract._classify_experiment_scope(
        state,
        scope="scientific",
        reason="The bounded diagnostic was initially misclassified.",
    ))
    assert scientific["status"] == "success", scientific
    raw_id = state.save_artifact(
        "raw_results", "stale_raw", json.dumps({"files": []}),
    )["id"]
    clean_id = state.save_artifact(
        "clean_results", "stale_clean", json.dumps({
            "raw_results_artifact_id": raw_id,
            "rows": [],
        }),
    )["id"]
    log_id = state.save_artifact(
        "experiment_log", "stale_log",
        "## Execution\ncommand: stale\nverification: stale\n",
    )["id"]
    operational = _await(run_contract._classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="The bounded diagnostic is an operational acceptance check.",
    ))
    assert operational["status"] == "success", operational
    return {
        "raw_results": raw_id,
        "clean_results": clean_id,
        "experiment_log": log_id,
    }


def _supersede_triplet_drafts(state: State, stale: dict[str, str]) -> None:
    paired = _await(_supersede_closure_draft(
        state,
        stale["clean_results"],
        reason="noncanonical pre-operation evidence draft",
    ))
    assert paired["status"] == "success", paired
    assert paired.get("linked_superseded_id") == stale["raw_results"], paired
    log = _await(_supersede_closure_draft(
        state,
        stale["experiment_log"],
        reason="noncanonical pre-operation log draft",
    ))
    assert log["status"] == "success", log


def _inject_superseded_drafts(state: State) -> dict[str, str]:
    raw_id = state.save_artifact(
        "raw_results", "extra_raw", json.dumps({"files": []}),
    )["id"]
    clean_id = state.save_artifact(
        "clean_results", "extra_clean", json.dumps({
            "raw_results_artifact_id": raw_id,
            "rows": [],
        }),
    )["id"]
    log_id = state.save_artifact(
        "experiment_log", "extra_log",
        "## Execution\ncommand: extra\nverification: extra\n",
    )["id"]
    _supersede_triplet_drafts(state, {
        "raw_results": raw_id,
        "clean_results": clean_id,
        "experiment_log": log_id,
    })
    return {
        "raw_results": raw_id,
        "clean_results": clean_id,
        "experiment_log": log_id,
    }


def _forge_frozen_raw_supersession(
    state: State,
    *,
    canonical_raw_id: str,
) -> tuple[str, str]:
    """Construct a ledger state the public supersession tool correctly forbids.

    A frozen target must not normally be superseded.  This adversarial fixture
    models an old/corrupt immutable ledger so the active reader proves it does
    not silently hide verified evidence.
    """
    canonical = state.read_artifact(canonical_raw_id)
    assert isinstance(canonical, dict)
    extra = state.save_artifact(
        "raw_results",
        "forged_frozen_raw",
        str(canonical["content"]),
        metadata=dict(canonical.get("metadata") or {}),
    )
    extra_id = str(extra["id"])
    state.mark_frozen(extra_id)
    payload = json.dumps({
        "superseded_id": extra_id,
        "artifact_type": "raw_results",
        "reason": "adversarial frozen-evidence supersession fixture",
        "run_id": state.run_id,
    }, sort_keys=True)
    supersession = state.save_artifact(
        "experiment_log_supersession",
        "forged_frozen_raw_supersession",
        payload,
        metadata={
            "superseded_id": extra_id,
            "artifact_type": "raw_results",
        },
    )
    supersession_id = str(supersession["id"])
    state.mark_frozen(supersession_id)
    return extra_id, supersession_id


@pytest.mark.parametrize("supersede_from", ("raw_results", "clean_results"))
def test_superseded_preclassification_drafts_do_not_leave_partial_operation_closure(
    tmp_path: Path,
    supersede_from: str,
):
    state = State.new("experiment", tmp_path)
    stale = _preclassification_triplet(state)
    paired = _await(_supersede_closure_draft(
        state,
        stale[supersede_from],
        reason="noncanonical pre-operation evidence draft",
    ))
    assert paired["status"] == "success", paired
    assert paired.get("linked_superseded_id") == stale[
        "clean_results" if supersede_from == "raw_results" else "raw_results"
    ], paired
    log = _await(_supersede_closure_draft(
        state,
        stale["experiment_log"],
        reason="noncanonical pre-operation log draft",
    ))
    assert log["status"] == "success", log

    completion = _complete_operation(state)

    assert completion["status"] == "success", completion
    assert _audit_operation_log(state)["passed"] is True
    for artifact_type in _TRIPLET_TYPES:
        active, hidden = active_closure_artifacts(state, artifact_type)
        assert len(active) == 1
        assert hidden == [stale[artifact_type]]


@pytest.mark.parametrize("artifact_type", _TRIPLET_TYPES)
@pytest.mark.parametrize("with_superseded_drafts", (False, True))
def test_operation_closure_consumers_share_active_view(
    tmp_path: Path,
    artifact_type: str,
    with_superseded_drafts: bool,
):
    state = _operation_state(tmp_path)
    completion = _complete_operation(state)
    assert completion["status"] == "success", completion
    stale = _inject_superseded_drafts(state) if with_superseded_drafts else None

    expected_ids = {
        "raw_results": completion["raw_results_artifact_id"],
        "clean_results": completion["clean_results_artifact_id"],
        "experiment_log": completion["experiment_log_artifact_id"],
    }
    active, hidden = active_closure_artifacts(state, artifact_type)
    assert [item["id"] for item in active] == [expected_ids[artifact_type]]
    assert hidden == ([] if stale is None else [stale[artifact_type]])

    clean_record = state.read_artifact(expected_ids["clean_results"])
    assert isinstance(clean_record, dict)
    assert _operation_clean_results_freeze_errors(state, clean_record) == []

    bundle = _operation_result_bundle(state)
    assert bundle["passed"] is True, bundle
    assert bundle["artifacts"]["raw_results"]["count"] == 1
    assert bundle["artifacts"]["clean_results"]["count"] == 1

    binding = run_contract.audit_execution_intent_binding(state, require=False)
    goal_effect = _operation_goal_effect_audit(
        state,
        intent_binding=binding,
    )
    assert goal_effect["passed"] is True, goal_effect
    row = goal_effect["artifacts"][artifact_type]
    assert row["artifact_id"] == expected_ids[artifact_type]
    assert row["upstream_goal_effect"] == "operational_subtask_only"
    assert row["binding_status"] == binding["status"]


def test_active_view_mutation_reproduces_the_legacy_consumer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = _operation_state(tmp_path)
    completion = _complete_operation(state)
    assert completion["status"] == "success", completion
    _inject_superseded_drafts(state)

    def legacy_view(state: State, artifact_type: str):
        return contract_audit.current_run_artifacts(state, artifact_type), []

    monkeypatch.setattr(contract_audit, "active_closure_artifacts", legacy_view)
    clean_record = state.read_artifact(completion["clean_results_artifact_id"])
    assert isinstance(clean_record, dict)
    errors = _operation_clean_results_freeze_errors(state, clean_record)

    assert "operation clean_results requires exactly one current-run raw_results" in errors


def test_frozen_evidence_targeted_by_forged_supersession_stays_counted(
    tmp_path: Path,
):
    """A forged negation must not hide a second frozen raw evidence record."""
    state = _operation_state(tmp_path)
    completion = _complete_operation(state)
    assert completion["status"] == "success", completion
    canonical_raw_id = str(completion["raw_results_artifact_id"])
    extra_raw_id, supersession_id = _forge_frozen_raw_supersession(
        state,
        canonical_raw_id=canonical_raw_id,
    )

    # Public production code correctly refuses to create this state.  A reader
    # nevertheless has to fail closed if an old/corrupt immutable ledger has it.
    denied = _await(_supersede_closure_draft(
        state,
        extra_raw_id,
        reason="public path must reject frozen evidence",
    ))
    assert denied["status"] == "error", denied
    assert "已冻结" in denied["error"]
    supersessions = _current_run_supersessions(state)
    assert supersessions[extra_raw_id]["supersession_id"] == supersession_id
    assert supersessions[extra_raw_id]["artifact_type"] == "raw_results"

    active, hidden = active_closure_artifacts(state, "raw_results")
    assert [item["id"] for item in active] == [canonical_raw_id, extra_raw_id]
    assert hidden == []

    clean_record = state.read_artifact(completion["clean_results_artifact_id"])
    assert isinstance(clean_record, dict)
    errors = _operation_clean_results_freeze_errors(state, clean_record)
    assert "operation clean_results requires exactly one current-run raw_results" in errors
    bundle = _operation_result_bundle(state)
    assert bundle["passed"] is False, bundle
    assert bundle["artifacts"]["raw_results"]["count"] == 2
    goal_effect = _operation_goal_effect_audit(
        state,
        intent_binding=run_contract.audit_execution_intent_binding(state, require=False),
    )
    # This audit is diagnostic at multiplicity; it must still report the bad
    # evidence rather than treating the forged negation as an erasure.
    assert goal_effect["artifacts"]["raw_results"] == {
        "count": 2,
        "status": "unavailable",
    }
