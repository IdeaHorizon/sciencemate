"""Operation completion must satisfy every declared mechanical postcondition.

The scheduler exit code describes the wrapper process.  It cannot erase a
health-contract output or a bound route output that the same submission said
would exist.  These tests exercise the real external-job finalizer so the
postcondition gate is proven to run before immutable closure minting.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_external_route_projection import _bound_state, _route, _submitted_route
from test_operation_job_finalization import (
    _finalize,
    _health,
    _patch_pipeline,
    _prepared_state,
    _submission,
)

from core.blockers import record_blocker
from nodes.experiment.tools import execution_route, operation_completion
from nodes.experiment.tools import resource_manager as manager


def _events(state, event_name: str) -> list[dict]:
    if not state.transcript_path.is_file():
        return []
    return [
        event
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for event in [json.loads(line)]
        if event.get("event") == event_name
    ]


def _zero_exit_health(*, completion_paths: list[dict] | None = None) -> dict:
    return _health(
        completion_paths=list(completion_paths or []),
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}},
    )


def _declared_health_submission(*, execution_class: str = "diagnostic") -> dict:
    return _submission(
        execution_class=execution_class,
        health_contract={"completion_paths": ["build/result.bin"]},
    )


def _snapshot(submission: dict, *, exists: bool, offset_s: float = 0.0) -> dict:
    submitted = datetime.fromisoformat(submission["submitted_at"]).timestamp()
    return {
        "path": "build/result.bin",
        "exists": exists,
        "mtime_epoch_s": submitted + offset_s if exists else None,
    }


def test_wrf_shaped_zero_exit_toolchain_without_outputs_is_blocked_not_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A wrapper swallowing the compiler failure code is not build success."""
    submission = _submission(execution_class="toolchain_build")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _zero_exit_health())

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["error_code"] == "operation_completed_positive_evidence_missing"
    assert "toolchain_build" in completed["error"]
    assert "report_blocker" in completed["error"]
    assert state.list_artifacts("external_job_operation_closure") == []

    record_blocker(
        state,
        summary="compiler wrapper returned zero without a declared build output",
        requested_action="declare and verify the executable or inspect the failed build",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="the wrapper exit code cannot prove that compilation completed",
    )
    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"


@pytest.mark.parametrize(
    ("snapshot", "expected_status"),
    [
        ({"exists": False, "offset_s": 0.0}, "error"),
        ({"exists": True, "offset_s": -120.0}, "error"),
        ({"exists": True, "offset_s": 120.0}, "success"),
    ],
    ids=("missing", "stale", "fresh"),
)
def test_zero_exit_requires_each_declared_health_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict,
    expected_status: str,
):
    submission = _declared_health_submission()
    state = _prepared_state(tmp_path, submission)
    observed = _snapshot(submission, **snapshot)
    _patch_pipeline(monkeypatch, _zero_exit_health(completion_paths=[observed]))

    calls = 0
    real_view = manager._operation_completion_postconditions

    def _observed_view(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_view(*args, **kwargs)

    monkeypatch.setattr(manager, "_operation_completion_postconditions", _observed_view)

    result = _finalize(state, submission, "operation_completed")

    assert calls == 1, "operation_completed must build one authoritative view"

    assert result["status"] == expected_status, result
    closures = state.list_artifacts("external_job_operation_closure")
    if expected_status == "error":
        assert result["error_code"] == "operation_completed_positive_evidence_missing"
        assert closures == []
    else:
        assert len(closures) == 1
        payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
        health_source = payload["completion_postconditions"]["sources"]["health"]
        assert health_source["declared"] is True
        assert health_source["passed"] is True


def _route_submission(
    tmp_path: Path,
    *,
    expected_outputs: list[str],
    execution_class: str = "toolchain_build",
    health_completion_paths: list[str] | None = None,
    registered_stdout_relative: str | None = None,
) -> tuple[object, dict, dict, dict]:
    state = _bound_state(tmp_path)
    route = _route()
    route["steps"][0]["expected_outputs"] = list(expected_outputs)
    binding, reference = _submitted_route(state, route)
    submission = {
        "status": "success",
        "dry_run": False,
        "execution_class": execution_class,
        "submitted_at": datetime.now(UTC).isoformat(),
        **(
            {"health_contract": {"completion_paths": list(health_completion_paths)}}
            if health_completion_paths is not None
            else {}
        ),
        **(
            {
                "stdout_path": str(
                    Path(binding["resolved_workdir"])
                    / registered_stdout_relative
                )
            }
            if registered_stdout_relative is not None
            else {}
        ),
        **reference,
        "route_attempt_id": binding["attempt_id"],
    }
    state.save_artifact(
        "job_submission",
        "route_postcondition_submission",
        json.dumps(submission),
    )
    Path(binding["resolved_workdir"]).mkdir(parents=True, exist_ok=True)
    return state, submission, binding, reference


def _route_finalize(
    state,
    reference: dict,
    domain_outcome: str,
) -> dict:
    return execution_route.record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference.get("namespace"),
        launch_host=reference.get("launch_host"),
        scheduler_cluster=reference.get("scheduler_cluster"),
        resource_uid=reference.get("resource_uid"),
        submission_nonce=reference.get("submission_nonce"),
        process_group_id=reference.get("process_group_id"),
        process_start_ticks=reference.get("process_start_ticks"),
        container_runtime_id=reference.get("container_runtime_id"),
        domain_outcome=domain_outcome,
        evidence_artifact_id="external_job_operation_closure__receipt",
    )


@pytest.mark.parametrize("materialize_output", [False, True], ids=("missing", "present"))
def test_route_only_postcondition_is_checked_before_operation_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_output: bool,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    output = Path(binding["resolved_workdir"]) / "wrf.exe"
    if materialize_output:
        output.write_bytes(b"verified executable\n")

    calls = 0
    real_verify = execution_route._verify_expected_outputs

    def _observed_verify(bound: dict):
        nonlocal calls
        calls += 1
        return real_verify(bound)

    monkeypatch.setattr(execution_route, "_verify_expected_outputs", _observed_verify)
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    result = _finalize(state, submission, "operation_completed")

    assert calls == 1, "the persisted route fact must reuse the precomputed observation"
    closures = state.list_artifacts("external_job_operation_closure")
    if materialize_output:
        assert result["status"] == "success", result
        assert len(closures) == 1
        payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
        route_source = payload["completion_postconditions"]["sources"]["route"]
        assert route_source["declared"] is True
        assert route_source["passed"] is True
        assert route_source["verified_output_specs"] == ["wrf.exe"]
    else:
        assert result["status"] == "error", result
        assert result["error_code"] == "operation_completed_positive_evidence_missing"
        assert result["completion_postconditions"]["sources"]["route"][
            "missing_expected_outputs"
        ] == ["wrf.exe"]
        assert closures == [], "a rejected postcondition must not mint success first"
        failures = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            and json.loads(line).get("event") == "route_step_external_execution_verified"
        ]
        assert len(failures) == 1
        assert failures[0]["failure_class"] == "expected_outputs_missing"


def test_ambiguous_route_submission_fails_before_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path, expected_outputs=["wrf.exe"]
    )
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    submitted = next(
        event
        for event in events
        if event.get("event") == "route_step_outcome" and event.get("outcome") == "submitted"
    )
    forged = {key: value for key, value in submitted.items() if key not in {"event", "attempt_id"}}
    state.append_transcript(
        "route_step_outcome",
        attempt_id="route-forged-duplicate",
        **forged,
    )
    monkeypatch.setattr(
        execution_route,
        "_current_blocking_event_history_warning",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        execution_route,
        "_blocking_event_history_warning",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error", result
    route_source = result["completion_postconditions"]["sources"]["route"]
    assert route_source["status"] == "indeterminate"
    assert route_source["reason"] == "route_submission_ambiguous"
    assert state.list_artifacts("external_job_operation_closure") == []


def test_changed_route_contract_does_not_reuse_old_attempt_without_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    monkeypatch.setattr(
        execution_route,
        "_attempt_step_contract",
        lambda *_args, **_kwargs: ("changed-contract", ["wrf.exe"]),
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error", result
    route_source = result["completion_postconditions"]["sources"]["route"]
    assert route_source["status"] == "indeterminate"
    assert route_source["reason"] == ("route_attempt_contract_changed_without_validated_correction")
    assert state.list_artifacts("external_job_operation_closure") == []


def test_health_failure_with_route_success_keeps_blocked_exit_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two valid rules must not combine into a dead end."""
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
        health_completion_paths=["health.done"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(b"verified executable\n")
    health = _zero_exit_health(
        completion_paths=[
            {
                "path": "health.done",
                "exists": False,
                "mtime_epoch_s": None,
            }
        ]
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["completion_postconditions"]["sources"]["health"]["passed"] is False
    assert state.list_artifacts("external_job_operation_closure") == []
    assert not [
        event
        for event in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if "route_step_external_execution_verified" in event
    ], "route success must not be persisted before the aggregate gate passes"

    record_blocker(
        state,
        summary="the declared health product was not generated",
        requested_action="inspect the workload and regenerate the health product",
    )
    blocked = _finalize(state, submission, "operation_blocked", note="health output missing")
    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"


def test_verify_tool_cannot_preproject_success_when_health_postcondition_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
        health_completion_paths=["health.done"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(b"verified executable\n")
    health = _zero_exit_health(
        completion_paths=[
            {
                "path": "health.done",
                "exists": False,
                "mtime_epoch_s": None,
            }
        ]
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(
        operation_completion,
        "_external_job_success_evidence",
        lambda *_args, **_kwargs: {
            "verified": True,
            "succeeded": True,
            "source": "test_scheduler_exit",
        },
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    verified = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            scheduler=submission["scheduler"],
            job_id=submission["job_id"],
            namespace=submission.get("namespace"),
        )
    )

    assert verified["status"] == "error", verified
    assert verified["error_code"] == "external_job_success_unverified"
    assert not [
        line
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if "route_step_external_execution_verified" in line
    ]

    record_blocker(
        state,
        summary="the declared health product was not generated",
        requested_action="inspect the workload and regenerate the health product",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="health output missing after terminal execution",
    )
    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"


def test_verify_tool_persists_route_missing_once_and_correction_remains_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
        registered_stdout_relative="logs/wrf.exe",
    )
    actual = Path(binding["resolved_workdir"]) / "logs" / "wrf.exe"
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_bytes(b"verified executable\n")
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        operation_completion,
        "_external_job_success_evidence",
        lambda *_args, **_kwargs: {
            "verified": True,
            "succeeded": True,
            "source": "test_scheduler_exit",
        },
    )

    first = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            scheduler=submission["scheduler"],
            job_id=submission["job_id"],
            namespace=submission.get("namespace"),
        )
    )
    repeated = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            scheduler=submission["scheduler"],
            job_id=submission["job_id"],
            namespace=submission.get("namespace"),
        )
    )

    assert first["error_code"] == "external_job_success_unverified", first
    assert repeated["error_code"] == "external_job_success_unverified", repeated
    failures = _events(state, "route_step_external_execution_verified")
    assert len(failures) == 1
    assert failures[0]["failure_class"] == "expected_outputs_missing"

    revised = _route()
    revised["steps"][0]["expected_outputs"] = ["logs/wrf.exe"]
    amended = asyncio.run(
        execution_route._declare_execution_route(
            state,
            route=revised,
            amendment_reason="The executable was written below the backend log directory",
            recovery_basis={
                "attempt_id": binding["attempt_id"],
                "failure_class": "expected_output",
                "diagnosis": "the output declaration omitted its logs/ prefix",
                "evidence_refs": [],
            },
        )
    )
    assert amended["status"] == "success", amended

    corrected = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            scheduler=submission["scheduler"],
            job_id=submission["job_id"],
            namespace=submission.get("namespace"),
        )
    )
    assert corrected["status"] == "success", corrected
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"


def test_run_level_completion_persists_route_missing_before_rejecting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, _submission, _binding, reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        operation_completion,
        "_external_job_success_evidence",
        lambda *_args, **_kwargs: {
            "verified": True,
            "succeeded": True,
            "source": "test_scheduler_exit",
        },
    )

    result = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="external_job",
            objective="compile and verify one managed executable",
            outcome="success",
            external_job_refs=[reference],
        )
    )

    assert result["status"] == "error", result
    assert result["error_code"] == "external_job_success_unverified"
    assert result["recovery"]["exact_output_correction_available"] is True
    assert "declare_execution_route" in result["error"]
    failures = _events(state, "route_step_external_execution_verified")
    assert len(failures) == 1
    assert failures[0]["failure_class"] == "expected_outputs_missing"


def test_route_missing_then_blocked_preserves_route_failure_without_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")
    assert completed["status"] == "error", completed
    assert completed["route_correction_available"] is True
    assert "declare_execution_route" in completed["error"]
    assert len(_events(state, "route_step_external_execution_verified")) == 1

    record_blocker(
        state,
        summary="the declared route output was not produced",
        requested_action="inspect the workload or correct the declared output",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="the route output is absent and cannot be corrected in this run",
    )

    assert blocked["status"] == "success", blocked
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["route_state"] == "blocked", snapshot
    assert snapshot["steps"]["run"]["state"] == "failed"
    finalized = _events(state, "route_step_external_finalized")
    assert len(finalized) == 1
    assert finalized[0]["domain_outcome"] == "operation_blocked"
    assert finalized[0]["route_outcome"] == "blocked"


@pytest.mark.parametrize(
    "persistence_reason",
    [
        "route_external_execution_verification_persistence_failed",
        "route_external_identity_mismatch",
    ],
)
def test_route_missing_persistence_failure_does_not_advertise_false_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence_reason: str,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        execution_route,
        "record_external_route_execution_verification",
        lambda *_args, **_kwargs: {
            "status": "error",
            "reason": persistence_reason,
        },
    )

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["error_code"] == (
        "operation_completion_postcondition_persistence_failed"
    )
    assert completed["route_persistence_error"]["reason"] == persistence_reason
    assert "correction basis 尚不存在" in completed["error"]
    assert "不要调用 declare_execution_route" in completed["error"]
    assert state.list_artifacts("external_job_operation_closure") == []

    record_blocker(
        state,
        summary="the missing-output observation could not be persisted",
        requested_action="repair the route identity or persistence layer",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="the correction basis is unavailable",
    )
    assert blocked["status"] == "success", blocked


def test_preexisting_route_success_then_health_blocked_preserves_both_axes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, binding, reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
        health_completion_paths=["health.done"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(
        b"verified executable\n"
    )
    projected = execution_route.record_external_route_execution_verification(
        state,
        external_job_ref={
            **reference,
            "route_attempt_id": binding["attempt_id"],
        },
        terminal=True,
        success_verified=True,
        success_evidence={
            "verified": True,
            "succeeded": True,
            "source": "legacy_route_only_verifier",
        },
    )
    assert projected["status"] == "success", projected
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"

    health = _zero_exit_health(
        completion_paths=[
            {
                "path": "health.done",
                "exists": False,
                "mtime_epoch_s": None,
            }
        ]
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: health,
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")
    assert completed["status"] == "error", completed

    record_blocker(
        state,
        summary="the declared health product was not generated",
        requested_action="inspect the workload and regenerate the health product",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="route output exists, but the independent health product is missing",
    )

    assert blocked["status"] == "success", blocked
    assert len(_events(state, "route_step_external_execution_verified")) == 1
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["route_state"] == "blocked", snapshot
    assert snapshot["steps"]["run"]["state"] == "blocked"
    assert snapshot["steps"]["run"]["execution_observation"] == "success"
    assert snapshot["steps"]["run"]["workflow_outcome"] == "operation_blocked"


@pytest.mark.parametrize("domain_outcome", ["operation_failed", "cancelled"])
def test_route_success_cannot_be_rewritten_as_failure_or_cancellation(
    tmp_path: Path,
    domain_outcome: str,
):
    state, _submission, binding, reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(
        b"verified executable\n"
    )
    projected = execution_route.record_external_route_execution_verification(
        state,
        external_job_ref={
            **reference,
            "route_attempt_id": binding["attempt_id"],
        },
        terminal=True,
        success_verified=True,
        success_evidence={
            "verified": True,
            "succeeded": True,
            "source": "test_scheduler_exit",
        },
    )
    assert projected["status"] == "success", projected

    finalized = _route_finalize(state, reference, domain_outcome)

    assert finalized["status"] == "error", finalized
    assert finalized["reason"] == "route_external_finalization_conflict"
    assert _events(state, "route_step_external_finalized") == []


def test_nonzero_exit_does_not_persist_false_route_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    health = _health(
        completion_paths=[],
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 2}}},
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert "completion_postconditions" not in completed
    assert not [
        line
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if "route_step_external_execution_verified" in line
    ], "a failed execution must not receive a succeeded=True route receipt"

    failed = _finalize(state, submission, "operation_failed", note="compiler exited 2")
    assert failed["status"] == "success", failed
    assert failed["outcome"] == "operation_failed"
    assert failed["route_projection"]["route_outcome"] == "failed"


def test_remote_missing_health_output_points_only_to_blocked_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    submission = _declared_health_submission()
    state = _prepared_state(tmp_path, submission)
    health = _health(
        completion_paths=[_snapshot(submission, exists=False)],
        scheduler_result={"raw": {}},
    )
    _patch_pipeline(monkeypatch, health)

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert "operation_failed" not in completed["error"]
    assert "report_blocker" in completed["error"]
    assert "operation_blocked" in completed["error"]

    record_blocker(
        state,
        summary="remote scheduler did not expose an exit code or output",
        requested_action="inspect remote accounting and regenerate the output",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="remote completion cannot be established",
    )
    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"


def test_remote_unknown_exit_does_not_broaden_fallback_to_route_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(
        b"verified executable\n"
    )
    health = _health(completion_paths=[], scheduler_result={"raw": {}})
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: health,
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["error_code"] == "operation_completed_positive_evidence_missing"
    assert "exit_code=None" in completed["error"]
    assert "completion_paths" in completed["error"]
    assert state.list_artifacts("external_job_operation_closure") == []


def test_remote_unknown_exit_route_missing_does_not_forge_correction_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, _binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    health = _health(completion_paths=[], scheduler_result={"raw": {}})
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: health,
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["route_correction_available"] is False
    assert "declare_execution_route" not in completed["error"]
    assert _events(state, "route_step_external_execution_verified") == []
    assert state.list_artifacts("external_job_operation_closure") == []

    record_blocker(
        state,
        summary="remote execution success cannot be established",
        requested_action="inspect scheduler accounting before retry",
    )
    blocked = _finalize(
        state,
        submission,
        "operation_blocked",
        note="remote execution outcome is unknown",
    )
    assert blocked["status"] == "success", blocked
    assert blocked["outcome"] == "operation_blocked"


@pytest.mark.parametrize(
    ("materialize_route_output", "expected_status"),
    [(False, "error"), (True, "success")],
    ids=("route-missing", "route-present"),
)
def test_remote_health_fallback_still_conjoins_declared_route_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_route_output: bool,
    expected_status: str,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
        health_completion_paths=["build/result.bin"],
    )
    if materialize_route_output:
        (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(
            b"verified executable\n"
        )
    health = _health(
        completion_paths=[_snapshot(submission, exists=True, offset_s=120)],
        scheduler_result={"raw": {}},
    )
    _patch_pipeline(monkeypatch, health)

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == expected_status, completed
    if expected_status == "error":
        assert completed["route_correction_available"] is True
        failures = _events(state, "route_step_external_execution_verified")
        assert len(failures) == 1
        assert failures[0]["failure_class"] == "expected_outputs_missing"
        assert state.list_artifacts("external_job_operation_closure") == []
    else:
        closures = state.list_artifacts("external_job_operation_closure")
        assert len(closures) == 1
        payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
        sources = payload["completion_postconditions"]["sources"]
        assert sources["health"]["passed"] is True
        assert sources["route"]["passed"] is True


def test_v2_frozen_view_validator_recomputes_semantic_invariants(tmp_path: Path):
    toolchain = _submission(execution_class="toolchain_build")
    toolchain_state = _prepared_state(tmp_path / "toolchain", toolchain)
    zero_view = manager._operation_completion_postconditions(
        toolchain_state,
        toolchain,
        _zero_exit_health(),
        "toolchain_build",
    )
    forged_zero = deepcopy(zero_view)
    forged_zero["passed"] = True
    forged_zero["zero_declaration_toolchain"] = False
    assert not manager._valid_frozen_completion_postconditions(
        forged_zero,
        toolchain,
        "toolchain_build",
        receipt_health={"completion_paths": []},
    )

    health_submission = _declared_health_submission()
    health_state = _prepared_state(tmp_path / "health", health_submission)
    fresh_snapshot = _snapshot(health_submission, exists=True, offset_s=120.0)
    fresh_health = _zero_exit_health(completion_paths=[fresh_snapshot])
    health_view = manager._operation_completion_postconditions(
        health_state,
        health_submission,
        fresh_health,
        "diagnostic",
    )
    assert manager._valid_frozen_completion_postconditions(
        health_view,
        health_submission,
        "diagnostic",
        receipt_health=fresh_health,
    )
    stale_view = deepcopy(health_view)
    stale_snapshot = _snapshot(health_submission, exists=True, offset_s=-120.0)
    stale_view["sources"]["health"]["snapshots"] = [stale_snapshot]
    assert not manager._valid_frozen_completion_postconditions(
        stale_view,
        health_submission,
        "diagnostic",
        receipt_health={"completion_paths": [stale_snapshot]},
    )


def test_v2_frozen_route_view_rejects_semantic_mutations(tmp_path: Path):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    (Path(binding["resolved_workdir"]) / "wrf.exe").write_bytes(b"verified executable\n")
    health = _zero_exit_health()
    view = manager._operation_completion_postconditions(
        state,
        submission,
        health,
        "toolchain_build",
    )
    assert manager._valid_frozen_completion_postconditions(
        view,
        submission,
        "toolchain_build",
        receipt_health=health,
    )

    mutations = []
    missing = deepcopy(view)
    missing["sources"]["route"]["missing_expected_outputs"] = ["wrf.exe"]
    mutations.append(missing)
    wrong_attempt = deepcopy(view)
    wrong_attempt["sources"]["route"]["attempt_id"] = "route-forged"
    mutations.append(wrong_attempt)
    wrong_hash = deepcopy(view)
    wrong_hash["sources"]["route"]["step_execution_contract_hash"] = "forged"
    mutations.append(wrong_hash)
    empty_specs = deepcopy(view)
    empty_specs["sources"]["route"]["verified_output_specs"] = []
    mutations.append(empty_specs)

    for mutated in mutations:
        assert not manager._valid_frozen_completion_postconditions(
            mutated,
            submission,
            "toolchain_build",
            receipt_health=health,
        )

    wrong_record = deepcopy(submission)
    wrong_record["route_attempt_id"] = "route-forged-record-attempt"
    assert not manager._valid_frozen_completion_postconditions(
        view,
        wrong_record,
        "toolchain_build",
        receipt_health=health,
    )


def test_declared_route_v2_receipt_replays_after_output_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state, submission, binding, _reference = _route_submission(
        tmp_path,
        expected_outputs=["wrf.exe"],
    )
    output = Path(binding["resolved_workdir"]) / "wrf.exe"
    output.write_bytes(b"verified executable\n")
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: _zero_exit_health(),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_args, **_kwargs: None,
    )

    first = _finalize(state, submission, "operation_completed")
    assert first["status"] == "success", first
    closure_ids = [item["id"] for item in state.list_artifacts("external_job_operation_closure")]
    transcript_before = state.transcript_path.read_text(encoding="utf-8")
    verification_count = transcript_before.count("route_step_external_execution_verified")
    output.unlink()
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 retry must not probe the cleaned-up job")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_operation_completion_postconditions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 retry must consume the frozen view")
        ),
    )
    monkeypatch.setattr(
        execution_route,
        "_verify_expected_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 retry must not rescan deleted outputs")
        ),
    )

    replayed = _finalize(state, submission, "operation_completed")

    assert replayed["status"] == "success", replayed
    assert replayed["operation_closure_artifact_id"] == closure_ids[0]
    assert [
        item["id"] for item in state.list_artifacts("external_job_operation_closure")
    ] == closure_ids
    transcript_after = state.transcript_path.read_text(encoding="utf-8")
    assert transcript_after.count("route_step_external_execution_verified") == verification_count


def test_diagnostic_zero_exit_without_postconditions_keeps_existing_success_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    submission = _submission(execution_class="diagnostic")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _zero_exit_health())

    result = _finalize(state, submission, "operation_completed", note="probe done")

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
            "submission_nonce": "nonce-op-1",
            "process_group_id": None,
            "process_start_ticks": None,
            "container_runtime_id": None,
            "lifecycle_status": "finalized",
            "reason": "operation_completed: probe done",
            "recorded_at": "<recorded-at>",
            "superseded_by": None,
        },
    }
    assert len(state.list_artifacts("external_job_operation_closure")) == 1
