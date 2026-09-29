#!/usr/bin/env python3
"""C1 probe: local exact-output correction reuses one immutable attempt.

The probe uses the real route ledger and operation triplet writer.  It starts
no process and touches only a temporary run directory.  The legacy behavior
exits 1 because a successful local attempt whose output declaration is fixed
returns to ``ready`` and must be executed a second time.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.state import State  # noqa: E402
from nodes.experiment.tools.execution_route import (  # noqa: E402
    _declare_execution_route,
    begin_route_step_attempt,
    build_route_snapshot,
    finish_route_step_attempt,
    load_canonical_route,
    resolve_execution_context,
)
from nodes.experiment.tools.operation_completion import (  # noqa: E402
    _record_operation_completion,
)
from nodes.experiment.tools.run_contract import (  # noqa: E402
    _classify_experiment_scope,
)


LIMITATION_CHECK = "local_exact_output_correction_witness_limitations"
WITNESS_SCOPE = "run_shared_workdir_time_window"


def _await(value: Any) -> Any:
    return asyncio.run(value)


def _events(state: State, name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == name
    ]


def _route(*, expected: str = "declared/source.tar.gz") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "goal": "download and verify one source archive",
        "evidence_refs": ["https://example.invalid/source-contract"],
        "steps": [{
            "id": "acquire",
            "goal": "download the source archive",
            "after": [],
            "action": {"tool": "safe_run_bash", "program": "curl"},
            "effects": ["network_access", "workspace_write"],
            "workdir_role": "managed_source_root",
            "expected_outputs": [expected],
        }],
    }


def _state(root: Path, *, classify: bool = False) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=root / "runs",
        project_id=f"c1-{root.name}",
    )
    if classify:
        state.hook_state["node_inputs"] = {
            "experiment_focus": "Download and mechanically verify one source archive.",
            "stage": "resource_fetch",
        }
        classified = _await(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category="other",
            reason="Bounded file-delivery verification; no scientific conclusion.",
        ))
        assert classified["status"] == "success", classified
    return state


def _bind(state: State, route: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    declared = _await(_declare_execution_route(state, route=route))
    assert declared["status"] == "success", declared
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    workdir.mkdir(parents=True, exist_ok=True)
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    assert decision["decision"] == "matched_ready_step", decision
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(
        state,
        decision,
        tool="safe_run_bash",
        action={"payload_digest": "c1-local-attempt"},
    )
    assert binding and not binding.get("binding_error"), binding
    return binding, workdir


def _corrected_local_attempt(
    root: Path,
    *,
    classify: bool = False,
) -> tuple[State, dict[str, Any], Path, dict[str, Any]]:
    state = _state(root, classify=classify)
    route = _route()
    binding, workdir = _bind(state, route)
    time.sleep(0.01)
    actual = workdir / "actual" / "source.tar.gz"
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_bytes(b"source archive\n")
    outcome = finish_route_step_attempt(
        state,
        binding,
        result={"status": "success", "returncode": 0},
    )
    assert outcome and outcome.get("failure_class") == "expected_outputs_missing", outcome
    before_counts = {
        "bound": len(_events(state, "route_step_bound")),
        "outcome": len(_events(state, "route_step_outcome")),
    }

    revised = deepcopy(route)
    revised["steps"][0]["expected_outputs"] = ["actual/source.tar.gz"]
    recovered = _await(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="correct the path base to the file written by the original attempt",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "the output path was declared relative to the wrong directory",
            "evidence_refs": [],
        },
    ))
    assert recovered["status"] == "success", recovered

    snapshot = build_route_snapshot(state)
    step = snapshot["steps"]["acquire"]
    assert snapshot["route_state"] == "complete", snapshot
    assert step["state"] == "verified", step
    assert step["attempt_id"] == binding["attempt_id"], step
    assert step.get("recovered_from_local_attempt") is True, step
    assert len(_events(state, "route_step_bound")) == before_counts["bound"]
    assert len(_events(state, "route_step_outcome")) == before_counts["outcome"]
    limitations = snapshot.get("correction_witness_limitations") or []
    assert len(limitations) == 1, limitations
    assert limitations[0]["witness_scope"] == WITNESS_SCOPE, limitations
    assert limitations[0]["semantic_identity_independently_verified"] is False, limitations

    receipt = load_canonical_route(state)["record"]["metadata"][
        "validated_recovery_receipt"
    ]
    assert receipt["local_execution_reused"] is True, receipt
    witness = receipt["local_output_correction_witness"]
    assert witness["verified_outputs"] == ["actual/source.tar.gz"], witness
    assert witness["witness_scope"] == WITNESS_SCOPE, witness
    assert witness["semantic_identity_independently_verified"] is False, witness
    return state, binding, actual, recovered


def _legal_path_and_frozen_disclosure(root: Path) -> dict[str, Any]:
    state, binding, actual, _recovered = _corrected_local_attempt(
        root,
        classify=True,
    )
    completion = _await(_record_operation_completion(
        state,
        task_kind="file_delivery",
        objective="download and mechanically verify one source archive",
        outcome="success",
        checks=[{
            "name": "managed_command_returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(actual)],
    ))
    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    assert isinstance(log, dict), log
    metadata = log.get("metadata") or {}
    projection = metadata.get("route_correction_witness_limitations") or []
    assert len(projection) == 1, metadata
    assert projection[0]["witness_scope"] == WITNESS_SCOPE, projection
    assert projection[0]["semantic_identity_independently_verified"] is False, projection
    checks = ((metadata.get("operation_closure_input") or {}).get("checks") or [])
    limitation_checks = [item for item in checks if item.get("name") == LIMITATION_CHECK]
    assert len(limitation_checks) == 1, checks
    evidence = limitation_checks[0]["evidence"]["corrections"][0]
    assert limitation_checks[0]["passed"] is True, limitation_checks
    assert evidence["witness_scope"] == WITNESS_SCOPE, evidence
    assert evidence["semantic_identity_independently_verified"] is False, evidence
    assert WITNESS_SCOPE in str(log.get("content") or ""), log
    assert "semantic_identity_independently_verified" in str(log.get("content") or ""), log
    return {
        "attempt_id": binding["attempt_id"],
        "route_state": build_route_snapshot(state)["route_state"],
        "bound_events": len(_events(state, "route_step_bound")),
        "outcome_events": len(_events(state, "route_step_outcome")),
        "correction_declares": 1,
        "execution_count": 1,
        "limitation_check": limitation_checks[0],
    }


def _true_failure_control(root: Path) -> dict[str, Any]:
    state = _state(root)
    route = _route()
    binding, workdir = _bind(state, route)
    actual = workdir / "actual" / "source.tar.gz"
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_bytes(b"untrusted side effect of a failed command\n")
    outcome = finish_route_step_attempt(
        state,
        binding,
        result={"status": "error", "reason": "curl failed", "returncode": 22},
    )
    assert outcome and outcome.get("failure_class") != "expected_outputs_missing", outcome
    revised = deepcopy(route)
    revised["steps"][0]["expected_outputs"] = ["actual/source.tar.gz"]
    rejected = _await(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="must not relabel a failed command as a correction",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "claim the path declaration was wrong",
            "evidence_refs": [],
        },
    ))
    assert rejected["error_code"] == "route_recovery_basis_required", rejected
    assert "新证据" in rejected["error"], rejected
    return {"outcome": outcome["outcome"], "rejection": rejected["error_code"]}


def _append_and_identity_controls(root: Path) -> dict[str, Any]:
    state, binding, _actual, _recovered = _corrected_local_attempt(root)
    route = load_canonical_route(state)["route"]
    before_bounds = len(_events(state, "route_step_bound"))
    before_outcomes = len(_events(state, "route_step_outcome"))

    appended = deepcopy(route)
    appended["steps"].append({
        "id": "inspect",
        "goal": "inspect the archive",
        "after": ["acquire"],
        "action": {"tool": "safe_run_bash", "program": "tar"},
        "effects": [],
        "workdir_role": "managed_source_root",
        "expected_outputs": [],
    })
    added = _await(_declare_execution_route(
        state,
        route=appended,
        amendment_reason="append one downstream inspection step",
    ))
    assert added["status"] == "success", added
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["acquire"]["state"] == "verified", snapshot
    assert snapshot["steps"]["acquire"]["attempt_id"] == binding["attempt_id"], snapshot
    assert snapshot["ready_step_ids"] == ["inspect"], snapshot
    assert len(_events(state, "route_step_bound")) == before_bounds
    assert len(_events(state, "route_step_outcome")) == before_outcomes

    changed_goal = deepcopy(appended)
    changed_goal["steps"][0]["goal"] = "use the archive for a different intent"
    changed = _await(_declare_execution_route(
        state,
        route=changed_goal,
        amendment_reason="change the execution intent for the completed step",
    ))
    assert changed["status"] == "success", changed
    after = build_route_snapshot(state)
    assert after["steps"]["acquire"]["state"] == "pending", after
    identity = changed["execution_guidance"]["step_identity_contract"]
    fields = identity["definition_hash_fields"]
    assert "goal" in fields, identity
    assert "action.tool" in fields, identity
    assert "action.evidence_refs" in fields, identity
    assert "workdir_role" in fields, identity
    assert "action.workdir_role" not in fields, identity
    assert "invalidates" in identity["step_goal_semantics"].lower(), identity
    return {
        "append_preserved_attempt": binding["attempt_id"],
        "definition_hash_fields": fields,
        "goal_change_state": after["steps"]["acquire"]["state"],
    }


def _run_lane(name: str, fn: Any, failures: list[dict[str, str]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        failures.append({
            "lane": name,
            "exception": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        return {"error": failures[-1]}


def main() -> int:
    failures: list[dict[str, str]] = []
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="c1-legal-path-") as temp_dir:
        root = Path(temp_dir)
        report["legal_path_and_frozen_disclosure"] = _run_lane(
            "legal_path_and_frozen_disclosure",
            lambda: _legal_path_and_frozen_disclosure(root / "legal"),
            failures,
        )
        report["true_failure_control"] = _run_lane(
            "true_failure_control",
            lambda: _true_failure_control(root / "failure"),
            failures,
        )
        report["append_and_identity_controls"] = _run_lane(
            "append_and_identity_controls",
            lambda: _append_and_identity_controls(root / "identity"),
            failures,
        )
    report["failures"] = [
        {key: value for key, value in item.items() if key != "traceback"}
        for item in failures
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
