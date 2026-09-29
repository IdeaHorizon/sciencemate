"""Composition regression for local operation closure -> route recovery.

This deliberately keeps the manager and execution-route reducers real.  Only
the scheduler boundary and destructive local cleanup are replaced, so the test
covers the failure mode where cleanup removes live terminal evidence before a
validated route correction is retried.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

from core.blockers import record_blocker
from core.state import State
from core.tasks import TaskList
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import resource_manager as manager


_RUNTIME_ID = "a" * 64


def _events(state: State, event_name: str) -> list[dict]:
    if not state.transcript_path.is_file():
        return []
    return [
        event
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for event in [json.loads(line)]
        if event.get("event") == event_name
    ]


def _route() -> dict:
    return {
        "schema_version": 2,
        "goal": "Run and close one managed diagnostic job",
        "evidence_refs": ["https://example.invalid/managed-job-contract"],
        "steps": [
            {
                "id": "run",
                "goal": "Run the managed diagnostic workload",
                "after": [],
                "action": {"tool": "submit_job", "program": "solver"},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": ["phantom-output.dat"],
            }
        ],
    }


def _prepare_managed_job(tmp_path: Path) -> tuple[State, dict, dict, str]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="operation-route-reconciliation-composition",
    )
    # Exercise a diagnostic operation-class job inside a scientific run.  An
    # operational run has a different completion-log protocol and would not
    # cover the local operation closure receipt used by this regression.
    state.hook_state["run_contract"] = {
        "execution_mode": "scientific",
        "run_role": "secondary",
    }
    state.project_root = tmp_path / "project"

    route = _route()
    declared = asyncio.run(execution_route._declare_execution_route(
        state, route=route))
    assert declared["status"] == "success", declared

    decision = execution_route.resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": [
                "external_job", "process_tree", "workspace_write",
            ],
            "workdir_roles": ["run_root"],
        },
    )
    assert decision["decision"] == "matched_ready_step", decision
    workdir = tmp_path / "managed-workdir"
    workdir.mkdir()
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = execution_route.begin_route_step_attempt(
        state,
        decision,
        tool="submit_job",
        action={"payload_digest": "operation-route-composition"},
    )
    assert binding and not binding.get("binding_error"), binding

    submission = {
        "status": "success",
        "dry_run": False,
        "scheduler": "local",
        "job_id": "hf-operation-route-composition",
        "namespace": None,
        "launch_host": None,
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": binding["attempt_id"],
        "process_group_id": "composition-process-group",
        "process_start_ticks": "12345",
        "container_runtime_id": _RUNTIME_ID,
        "route_attempt_id": binding["attempt_id"],
        "sandbox_control_dir": str(tmp_path / "sandbox-control"),
        "execution_class": "diagnostic",
        "submitted_at": "2026-09-08T00:00:00+00:00",
    }
    artifact = state.save_artifact(
        "job_submission",
        "operation_route_composition_submission",
        json.dumps(submission, ensure_ascii=False),
    )
    outcome = execution_route.finish_route_step_attempt(
        state,
        binding,
        result={**submission, "submission_artifact_id": artifact["id"]},
        external_submission=True,
    )
    assert outcome and outcome["outcome"] == "submitted", outcome

    task = TaskList(Path(state.project_root) / "tasks").create(
        title="Finalize managed diagnostic job",
        owner_node="experiment",
        run_id=state.run_id,
        description="\n".join((
            "external_job_key=operation-route-composition",
            f"scheduler={submission['scheduler']}",
            f"job_id={submission['job_id']}",
            "namespace=",
            "launch_host=",
            "scheduler_cluster=",
            "resource_uid=",
            f"submission_nonce={submission['submission_nonce']}",
            f"process_group_id={submission['process_group_id']}",
            f"process_start_ticks={submission['process_start_ticks']}",
            f"container_runtime_id={submission['container_runtime_id']}",
            "execution_class=diagnostic",
            "output_roots=[]",
        )),
    )
    return state, route, binding, task.id


def test_local_operation_retry_composes_receipt_route_lifecycle_and_task(
    tmp_path: Path, monkeypatch,
):
    state, route, binding, task_id = _prepare_managed_job(tmp_path)
    probe_calls: list[tuple] = []
    cleanup_calls: list[str] = []

    def terminal_health(*args, **kwargs):
        probe_calls.append((args, kwargs))
        assert len(probe_calls) <= 2, (
            "only the retry after a rejected pre-cleanup postcondition may probe again"
        )
        return {
            "status": "success",
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "scheduler_result": {
                "raw": {"sandbox_state": {"exit_code": 0}},
            },
        }

    def idempotent_cleanup(record):
        cleanup_calls.append(str(record["job_id"]))
        return {
            "status": "success",
            "action": "simulated_exact_identity_cleanup",
            "job_id": record["job_id"],
        }

    monkeypatch.setattr(manager, "probe_external_job_health", terminal_health)
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization", idempotent_cleanup)

    first = asyncio.run(manager._finalize_external_job(
        state,
        "local",
        "hf-operation-route-composition",
        outcome="operation_completed",
    ))

    assert first["status"] == "error", first
    assert first["error_code"] == "operation_completed_positive_evidence_missing"
    assert first["completion_postconditions"]["sources"]["route"][
        "missing_expected_outputs"] == ["phantom-output.dat"]
    assert state.list_artifacts("external_job_operation_closure") == []
    assert probe_calls and len(probe_calls) == 1
    assert cleanup_calls == []
    assert manager.lifecycle_for_submission(
        state,
        json.loads(state.read_artifact(
            state.list_artifacts("job_submission")[0]["id"])["content"]),
    )["status"] is None
    tasks = {
        item.id: item
        for item in TaskList(Path(state.project_root) / "tasks").list_all()
    }
    assert tasks[task_id].status == "pending"

    revised = deepcopy(route)
    revised["steps"][0]["expected_outputs"] = []
    amended = asyncio.run(execution_route._declare_execution_route(
        state,
        route=revised,
        amendment_reason="The managed operation promised only a terminal status",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "The phantom file was not part of the job contract",
            "evidence_refs": [],
        },
    ))
    assert amended["status"] == "error", amended
    assert amended["error_code"] == "route_recovery_basis_required"
    assert "report_blocker" in str(amended["violations"])
    assert state.list_artifacts("external_job_operation_closure") == []
    assert cleanup_calls == []

    record_blocker(
        state,
        summary="the external job never produced its declared output",
        requested_action="inspect the job or re-point to a witnessed registered output",
    )
    blocked = asyncio.run(manager._finalize_external_job(
        state,
        "local",
        "hf-operation-route-composition",
        outcome="operation_blocked",
        note="the persisted route expected_outputs_missing cannot be cleared",
    ))

    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"
    assert len(state.list_artifacts("external_job_operation_closure")) == 1
    assert len(_events(state, "route_step_bound")) == 1
    assert len(_events(state, "route_step_outcome")) == 1
    assert blocked["workflow_status"] == "finalized"
    assert blocked["route_projection"]["status"] == "success"
    assert blocked["route_projection"]["route_outcome"] == "blocked"
    assert manager.lifecycle_for_submission(
        state,
        json.loads(state.read_artifact(
            state.list_artifacts("job_submission")[0]["id"])["content"]),
    )["status"] == "finalized"
    tasks = {
        item.id: item
        for item in TaskList(Path(state.project_root) / "tasks").list_all()
    }
    assert tasks[task_id].status == "completed"
    assert len(probe_calls) == 2
    assert len(cleanup_calls) == 1

    repeated = asyncio.run(manager._finalize_external_job(
        state,
        "local",
        "hf-operation-route-composition",
        outcome="operation_blocked",
        note="the persisted route expected_outputs_missing cannot be cleared",
    ))

    assert repeated["status"] == "success", repeated
    assert repeated["workflow_status"] == "finalized"
    assert repeated["route_projection"].get("already_projected") is True
    assert repeated["task_completion"]["completed_task_ids"] == []
    assert len(probe_calls) == 2
    assert len(cleanup_calls) == 2
    assert len(state.list_artifacts("external_job_operation_closure")) == 1
    assert len(_events(state, "route_step_bound")) == 1
    assert len(_events(state, "route_step_outcome")) == 1
    assert len(_events(state, "route_step_external_finalized")) == 1
