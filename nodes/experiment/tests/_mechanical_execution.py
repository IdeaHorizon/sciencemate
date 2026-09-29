"""Production-API fixture for one completed local mechanical route action."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import execution_route


def record_completed_local_mechanical_action(
    state: Any,
    *,
    step_id: str = "build",
    program: str = "tar",
    produce: Any = None,
    expected_outputs: list[str] | None = None,
) -> dict[str, Any]:
    """Persist route, attempt and census facts without faking their audit result.

    ``produce`` (P0a v3) is a zero-arg callable run *inside* the attempt — after
    admission, before settlement — so a fixture can materialize the build product
    the way a real attempt would. ``expected_outputs`` (P0a v4) declares those
    products on the step (relative to ``state.root``, the resolved workdir): the
    attempt's outcome then carries their identity receipt, which is the only
    thing ROC(build) accepts as proof that a declared product came from it.
    """
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route={
            "schema_version": 2,
            "goal": "Complete one bounded mechanical operation.",
            "evidence_refs": ["test:mechanical-execution-fixture"],
            "steps": [{
                "id": step_id,
                "goal": "Run the bounded local mechanical action.",
                "after": [],
                "action": {"tool": "safe_run_bash", "program": program},
                "effects": ["process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": list(expected_outputs or []),
            }],
        },
    ))
    assert declared["status"] == "success", declared
    action = {
        "tool": "safe_run_bash",
        "program": program,
        "route_step_id": step_id,
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["process_tree", "workspace_write"],
        "workdir_roles": ["run_root"],
        "payload_digest": "d" * 64,
    }
    decision = dict(execution_route.resolve_execution_context(state, action))
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "explicit",
        "resolved_workdir": str(Path(state.root)),
    })
    assert decision["decision"] == "matched_ready_step", decision
    binding = execution_route.begin_route_step_attempt(
        state,
        decision,
        tool="safe_run_bash",
        action=action,
    )
    assert binding and not binding.get("binding_error"), binding
    token = census.begin_execution_action(
        state,
        action,
        decision,
        route_binding=binding,
    )
    assert token["status"] == "success", token
    if produce is not None:
        produce()
    settled = census.settle_execution_action(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="test_managed_execution_boundary",
        result={"status": "success", "returncode": 0},
    )
    assert settled["passed"] is True, settled
    outcome = execution_route.finish_route_step_attempt(
        state,
        binding,
        result={"status": "success", "returncode": 0},
    )
    assert outcome and outcome["outcome"] == "success", outcome
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["route_state"] == "complete", snapshot
    obligation = census.operation_execution_obligation(state)
    assert obligation["real_execution_obligation_satisfied"] is True, obligation
    return {
        "binding": binding,
        "decision": decision,
        "obligation": obligation,
    }


def record_submitted_managed_action(
    state: Any,
    *,
    scheduler: str = "slurm",
    job_id: str = "31415",
) -> dict[str, Any]:
    """Persist an exact submitted route attempt; terminal verification stays with ROC."""
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route={
            "schema_version": 2,
            "goal": "Submit and verify one bounded managed build.",
            "evidence_refs": ["test:managed-execution-fixture"],
            "steps": [{
                "id": "build",
                "goal": "Run the managed build job.",
                "after": [],
                "action": {"tool": "submit_job", "program": "solver"},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }],
        },
    ))
    assert declared["status"] == "success", declared
    action = {
        "tool": "submit_job",
        "program": "solver",
        "route_step_id": "build",
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["run_root"],
        "payload_digest": "e" * 64,
    }
    decision = dict(execution_route.resolve_execution_context(state, action))
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "explicit",
        "resolved_workdir": str(Path(state.root)),
    })
    assert decision["decision"] == "matched_ready_step", decision
    binding = execution_route.begin_route_step_attempt(
        state,
        decision,
        tool="submit_job",
        action=action,
    )
    assert binding and not binding.get("binding_error"), binding
    token = census.begin_execution_action(
        state,
        action,
        decision,
        route_binding=binding,
    )
    assert token["status"] == "success", token
    reference = {
        "status": "success",
        "dry_run": False,
        "scheduler": scheduler,
        "job_id": job_id,
        "job_name": "managed-build",
        "namespace": "test" if scheduler != "local" else None,
        "launch_host": "login-test" if scheduler != "local" else None,
        "scheduler_cluster": "cluster-test" if scheduler != "local" else None,
        "resource_uid": (
            f"{scheduler}-cluster-test-{job_id}" if scheduler != "local" else None
        ),
        "submission_nonce": binding["attempt_id"],
        "container_runtime_id": None,
        "process_group_id": None,
        "process_start_ticks": None,
        "route_attempt_id": binding["attempt_id"],
        "submission_artifact_id": f"job_submission__{job_id}",
    }
    settled = census.settle_execution_action(
        state,
        token,
        payload_spawned=None,
        job_submitted=True,
        proof_source="test_scheduler_accepted_identity",
        result=reference,
    )
    assert settled["passed"] is True, settled
    outcome = execution_route.finish_route_step_attempt(
        state,
        binding,
        result=reference,
        external_submission=True,
    )
    assert outcome and outcome["outcome"] == "submitted", outcome
    return {
        "binding": binding,
        "decision": decision,
        "reference": reference,
    }
