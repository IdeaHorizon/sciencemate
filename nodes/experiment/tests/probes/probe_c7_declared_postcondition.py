#!/usr/bin/env python3
"""C7/015 probe: a declared completion postcondition is a necessary condition.

The probe uses the real external-job finalizer, route binder and route output
verifier.  It starts no service and submits no job.  On the legacy behavior it
exits 1 because a zero-exit wrapper can mint a success closure before declared
outputs are checked; on the candidate behavior every lane exits cleanly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.blockers import record_blocker  # noqa: E402
from core.state import State  # noqa: E402
from nodes.experiment.tools import execution_route  # noqa: E402
from nodes.experiment.tools import resource_manager as manager  # noqa: E402
from nodes.experiment.tools.execution_route import (  # noqa: E402
    _declare_execution_route,
    begin_route_step_attempt,
    finish_route_step_attempt,
    resolve_execution_context,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope  # noqa: E402


def _await(value: Any) -> Any:
    return asyncio.run(value)


def _submission(*, execution_class: str) -> dict[str, Any]:
    return {
        "status": "success",
        "dry_run": False,
        "scheduler": "slurm",
        "job_id": "9001",
        "submission_nonce": "nonce-c7-probe",
        "execution_class": execution_class,
        "submitted_at": "2026-09-16T00:00:00+00:00",
    }


def _health(*, completion_paths: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "success",
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": list(completion_paths or []),
        "scheduler_result": {"raw": {"sandbox_state": {"exit_code": 0}}},
    }


def _prepared_state(root: Path, submission: dict[str, Any]) -> State:
    state = State.new("experiment", root)
    state.save_artifact("job_submission", "c7_submission", json.dumps(submission))
    return state


def _finalize(
    state: State,
    submission: dict[str, Any],
    outcome: str,
    *,
    note: str = "",
) -> dict[str, Any]:
    return _await(
        manager._finalize_external_job(
            state,
            str(submission["scheduler"]),
            str(submission["job_id"]),
            "",
            outcome,
            note=note,
        )
    )


@contextmanager
def _stubbed_scheduler(
    health: dict[str, Any],
    *,
    stub_route_projection: bool,
) -> Iterator[None]:
    original_probe = manager.probe_external_job_health
    original_persist = manager._persist_execution_environment_evidence
    original_projection = execution_route.record_external_route_finalization
    manager.probe_external_job_health = lambda *_args, **_kwargs: dict(health)
    manager._persist_execution_environment_evidence = lambda *_args, **_kwargs: None
    if stub_route_projection:
        execution_route.record_external_route_finalization = lambda *_args, **_kwargs: {
            "status": "success"
        }
    try:
        yield
    finally:
        manager.probe_external_job_health = original_probe
        manager._persist_execution_environment_evidence = original_persist
        execution_route.record_external_route_finalization = original_projection


def _closure_payload(state: State) -> dict[str, Any]:
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1, closures
    return json.loads(state.read_artifact(closures[0]["id"])["content"])


def _wrf_zero_declaration_lane(root: Path) -> dict[str, Any]:
    submission = _submission(execution_class="toolchain_build")
    state = _prepared_state(root, submission)
    with _stubbed_scheduler(_health(), stub_route_projection=True):
        completed = _finalize(state, submission, "operation_completed")
        assert completed.get("status") == "error", completed
        assert completed.get("error_code") == ("operation_completed_positive_evidence_missing"), (
            completed
        )
        assert "toolchain_build" in str(completed.get("error") or ""), completed
        assert state.list_artifacts("external_job_operation_closure") == []

        record_blocker(
            state,
            summary="zero-exit compiler wrapper has no declared build product",
            requested_action="declare and verify the build product",
        )
        blocked = _finalize(
            state,
            submission,
            "operation_blocked",
            note="the wrapper exit code cannot establish compilation success",
        )
        assert blocked.get("status") == "success", blocked
    return {"completed": completed, "blocked": blocked}


def _health_lane(root: Path, *, exists: bool, offset_s: float) -> dict[str, Any]:
    submission = _submission(execution_class="diagnostic")
    submission["health_contract"] = {"completion_paths": ["build/result.bin"]}
    submitted = datetime.fromisoformat(submission["submitted_at"]).timestamp()
    snapshot = {
        "path": "build/result.bin",
        "exists": exists,
        "mtime_epoch_s": submitted + offset_s if exists else None,
    }
    state = _prepared_state(root, submission)
    with _stubbed_scheduler(
        _health(completion_paths=[snapshot]),
        stub_route_projection=True,
    ):
        result = _finalize(state, submission, "operation_completed")
    should_pass = exists and offset_s >= 0
    assert result.get("status") == ("success" if should_pass else "error"), result
    if should_pass:
        source = _closure_payload(state)["completion_postconditions"]["sources"]["health"]
        assert source.get("declared") is True and source.get("passed") is True, source
    else:
        assert state.list_artifacts("external_job_operation_closure") == []
    return {"result": result, "snapshot": snapshot}


def _route_state(root: Path, *, output: str) -> tuple[State, dict[str, Any], dict[str, Any]]:
    state = State.new(
        node_type="experiment",
        base_dir=root / "runs",
        project_id=f"c7-{root.name}",
    )
    state.hook_state["node_inputs"] = {"task": "compile and verify one executable"}
    classified = _await(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Compile and mechanically verify one declared executable.",
    ))
    assert classified.get("status") == "success", classified
    route = {
        "schema_version": 2,
        "goal": "compile and verify one executable",
        "evidence_refs": ["https://example.invalid/build-contract"],
        "steps": [
            {
                "id": "build",
                "goal": "compile the executable",
                "after": [],
                "action": {"tool": "submit_job", "program": "compiler-wrapper"},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [output],
            }
        ],
    }
    declared = _await(_declare_execution_route(state, route=route))
    assert declared.get("status") == "success", declared
    decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "compiler-wrapper",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    assert decision.get("decision") == "matched_ready_step", decision
    workdir = state.root / "outputs" / "experiment" / "runtime"
    decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(workdir),
        }
    )
    binding = begin_route_step_attempt(
        state,
        decision,
        tool="submit_job",
        action={"payload_digest": "c7-route"},
    )
    assert binding and not binding.get("binding_error"), binding
    reference = {
        "scheduler": "slurm",
        "job_id": "31415",
        "namespace": "research",
        "launch_host": "login-01",
        "scheduler_cluster": "cluster-a",
        "resource_uid": "slurm-cluster-a-31415",
        "submission_nonce": binding["attempt_id"],
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": None,
        "route_attempt_id": binding["attempt_id"],
    }
    outcome = finish_route_step_attempt(
        state,
        binding,
        result={
            "status": "success",
            **reference,
            "submission_artifact_id": "job_submission__c7_route",
        },
        external_submission=True,
    )
    assert outcome and outcome.get("outcome") == "submitted", outcome
    submission = {
        "status": "success",
        "dry_run": False,
        "execution_class": "toolchain_build",
        "submitted_at": datetime.now(UTC).isoformat(),
        **reference,
    }
    state.save_artifact(
        "job_submission",
        "c7_route",
        json.dumps(submission),
    )
    workdir.mkdir(parents=True, exist_ok=True)
    return state, submission, binding


def _route_lane(root: Path, *, materialize: bool) -> dict[str, Any]:
    state, submission, binding = _route_state(root, output="wrf.exe")
    output = Path(str(binding["resolved_workdir"])) / "wrf.exe"
    if materialize:
        output.write_bytes(b"verified executable\n")
    with _stubbed_scheduler(_health(), stub_route_projection=False):
        result = _finalize(state, submission, "operation_completed")
    closures = state.list_artifacts("external_job_operation_closure")
    if materialize:
        assert result.get("status") == "success", result
        source = _closure_payload(state)["completion_postconditions"]["sources"]["route"]
        assert source.get("passed") is True, source
        assert source.get("verified_output_specs") == ["wrf.exe"], source
    else:
        assert result.get("status") == "error", result
        assert result.get("error_code") == ("operation_completed_positive_evidence_missing"), result
        assert closures == [], closures
        events = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            and json.loads(line).get("event") == "route_step_external_execution_verified"
        ]
        assert len(events) == 1 and events[0].get("failure_class") == (
            "expected_outputs_missing"
        ), events
    return {"result": result, "closure_count": len(closures)}


def _diagnostic_control(root: Path) -> dict[str, Any]:
    submission = _submission(execution_class="diagnostic")
    state = _prepared_state(root, submission)
    with _stubbed_scheduler(_health(), stub_route_projection=True):
        result = _finalize(
            state,
            submission,
            "operation_completed",
            note="bounded diagnostic completed",
        )
    closure_id = result.get("operation_closure_artifact_id")
    assert closure_id and result.get("evidence_artifact_id") == closure_id, result
    normalized = deepcopy(result)
    normalized["operation_closure_artifact_id"] = "<closure-id>"
    normalized["evidence_artifact_id"] = "<closure-id>"
    normalized["lifecycle"]["recorded_at"] = "<recorded-at>"
    assert normalized == {
        "status": "success",
        "scheduler": "slurm",
        "job_id": "9001",
        "workflow_status": "finalized",
        "outcome": "operation_completed",
        "evidence_artifact_id": "<closure-id>",
        "operation_closure_artifact_id": "<closure-id>",
        "job_execution_class": "diagnostic",
        "class_unverified": False,
        "cleanup": {"scheduler": "slurm", "status": "not_required"},
        "route_projection": {"status": "success"},
        "task_completion": {"completed_task_ids": [], "status": "success"},
        "lifecycle": {
            "scheduler": "slurm",
            "job_id": "9001",
            "namespace": None,
            "launch_host": None,
            "scheduler_cluster": None,
            "resource_uid": None,
            "submission_nonce": "nonce-c7-probe",
            "process_group_id": None,
            "process_start_ticks": None,
            "container_runtime_id": None,
            "lifecycle_status": "finalized",
            "reason": "operation_completed: bounded diagnostic completed",
            "recorded_at": "<recorded-at>",
            "superseded_by": None,
        },
    }, result
    return result


def _run_lane(name: str, fn: Any, failures: list[dict[str, str]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        failures.append(
            {
                "lane": name,
                "exception": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        return {"error": failures[-1]}


def main() -> int:
    failures: list[dict[str, str]] = []
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="c7-postcondition-") as temp_dir:
        root = Path(temp_dir)
        lanes = {
            "wrf_zero_declaration": lambda: _wrf_zero_declaration_lane(root / "wrf"),
            "health_missing": lambda: _health_lane(
                root / "health-missing",
                exists=False,
                offset_s=0,
            ),
            "health_stale": lambda: _health_lane(
                root / "health-stale",
                exists=True,
                offset_s=-120,
            ),
            "health_fresh": lambda: _health_lane(
                root / "health-fresh",
                exists=True,
                offset_s=120,
            ),
            "route_missing": lambda: _route_lane(root / "route-missing", materialize=False),
            "route_present": lambda: _route_lane(root / "route-present", materialize=True),
            "diagnostic_control": lambda: _diagnostic_control(root / "diagnostic"),
        }
        for name, lane in lanes.items():
            report[name] = _run_lane(name, lane, failures)
    report["failures"] = [
        {key: value for key, value in item.items() if key != "traceback"} for item in failures
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
