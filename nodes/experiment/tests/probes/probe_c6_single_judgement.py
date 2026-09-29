"""C6 probe: preview and on-end share one external-closure judgement.

The process exit status is the gate.  The fixture deliberately uses the real
artifact-list/read interfaces and the real preview/on-end entry points while
replacing only scheduler health observation with a deterministic read-only
result.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.loop_hooks import HookContext
from core.tasks import TaskList
from nodes.experiment import hooks
from nodes.experiment.tools import resource_manager, sediment
from nodes.experiment.tools.contract_audit import TERMINAL_CLOSURE_REGISTRY

_FAILED_CHECK = "job_submission_records_readable"
_BLOCKER_ID = "experiment_job_submission_read_error"
_ERROR_PREFIX = "authoritative job_submission records unavailable: "


def _complete_terminal_audit() -> dict[str, dict[str, Any]]:
    return {
        key: {"passed": True, "applicable": True, "reason": "ok"}
        for key in TERMINAL_CLOSURE_REGISTRY
    }


class _LedgerState:
    """Small artifact-ledger adapter with explicit producer-run ownership."""

    def __init__(self, root: Path, *, run_id: str = "run-current") -> None:
        self.root = root
        self.run_id = run_id
        self.node_type = "experiment"
        self.project_root = None
        self.hook_state: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.reads: list[str] = []
        self._records: dict[str, dict[str, Any]] = {}

    def add(
        self,
        artifact_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        produced_by_run_id: str | None = None,
    ) -> None:
        self._records[artifact_id] = {
            "type": artifact_type,
            "name": artifact_id,
            "content": json.dumps(payload),
            "produced_by_node_type": "experiment",
            "produced_by_run_id": produced_by_run_id or self.run_id,
        }

    def list_artifacts(self, artifact_type=None, own_only=False):
        del own_only
        return [
            {"id": artifact_id, "type": record["type"], "name": record["name"]}
            for artifact_id, record in self._records.items()
            if artifact_type is None or record["type"] == artifact_type
        ]

    def artifact_head(self, artifact_id):
        record = self._records.get(artifact_id)
        if record is None:
            return None
        return SimpleNamespace(
            produced_by_run_id=record.get("produced_by_run_id"))

    def read_artifact(self, artifact_id):
        self.reads.append(artifact_id)
        return self._records.get(artifact_id)

    def save_artifact(self, artifact_type, name, content, metadata=None, **_kwargs):
        artifact_id = f"{artifact_type}__{name}"
        self._records[artifact_id] = {
            "type": artifact_type,
            "name": name,
            "content": content,
            "metadata": dict(metadata or {}),
            "produced_by_node_type": self.node_type,
            "produced_by_run_id": self.run_id,
        }
        return {"id": artifact_id}

    def append_transcript(self, event, **fields):
        self.events.append({"event": event, **fields})


def _submission(job_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "success",
        "dry_run": False,
        "scheduler": "local",
        "job_id": job_id,
        "job_name": job_id,
        "workdir": "/tmp/c6-probe",
        "output_roots": ["/tmp/c6-probe"],
    }
    payload.update(overrides)
    return payload


def _unresolved(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "dry_run": False,
        "scheduler": "local",
        "job_id": None,
        "do_not_resubmit": True,
    }


def _ctx(state: _LedgerState) -> HookContext:
    return HookContext(harness=None, state=state, messages=[], turn=1)


def _patch_read_only_observers():
    original_audit = sediment.audit_experiment_contract
    original_health = resource_manager.probe_external_job_health
    try:
        runtime_manager = importlib.import_module("tools.resource_manager")
    except ImportError:
        runtime_manager = resource_manager
    runtime_original_health = runtime_manager.probe_external_job_health
    from core import data_provenance

    original_stale = data_provenance.undeclared_stale_inputs
    sediment.audit_experiment_contract = (
        lambda _state: _complete_terminal_audit()
    )
    data_provenance.undeclared_stale_inputs = lambda _state: []

    def health(*_args, **_kwargs):
        return {
            "status": "success",
            "scheduler_phase": "running",
            "health_state": "healthy",
            "workflow_status": "awaiting_external_job",
        }

    resource_manager.probe_external_job_health = health
    runtime_manager.probe_external_job_health = health

    def restore() -> None:
        sediment.audit_experiment_contract = original_audit
        data_provenance.undeclared_stale_inputs = original_stale
        resource_manager.probe_external_job_health = original_health
        runtime_manager.probe_external_job_health = runtime_original_health

    return restore


def _assert_identity_grid(root: Path) -> None:
    state = _LedgerState(root)
    running = _submission(
        "own-running",
        namespace="ns",
        launch_host="node20",
        scheduler_cluster="local",
        resource_uid="uid-1",
        submission_nonce="nonce-1",
        process_group_id="pg-1",
        process_start_ticks="ticks-1",
        container_runtime_id="runtime-1",
    )
    state.add("own-running-primary", "job_submission", running)
    state.add(
        "own-running-recovery",
        "external_job_submission_recovery",
        running,
    )
    state.add("own-cancelled", "job_submission", _submission("own-cancelled"))
    state.add(
        "cancelled-lifecycle",
        "external_job_lifecycle",
        {
            "scheduler": "local",
            "job_id": "own-cancelled",
            "lifecycle_status": "cancelled",
        },
    )
    state.add(
        "foreign-running",
        "job_submission",
        _submission("foreign-running"),
        produced_by_run_id="run-foreign",
    )

    restore = _patch_read_only_observers()
    original_classify = hooks._classify_external_job_status
    hooks._classify_external_job_status = lambda _submission: "running"
    try:
        preview = asyncio.run(sediment._preview_experiment_contract(state))
        preview_ids = [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in preview["open_external_jobs"]
        ]
        expected_ids = [("local", "own-running")]
        assert preview_ids == expected_ids, preview
        assert preview_ids.count(("local", "own-running")) == 1, preview_ids
        assert "foreign-running" not in state.reads, state.reads

        loop_result = SimpleNamespace(status="completed", final_text="")
        hooks.external_job_handoff_on_end(_ctx(state), loop_result)
        on_end_ids = [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in state.hook_state["external_job_waiting"]["open_external_jobs"]
        ]
        assert on_end_ids == expected_ids, (preview_ids, on_end_ids)
        assert on_end_ids == preview_ids, (preview_ids, on_end_ids)
        assert on_end_ids.count(("local", "own-running")) == 1, on_end_ids
        assert "foreign-running" not in state.reads, state.reads
        assert loop_result.status == "blocked"
    finally:
        hooks._classify_external_job_status = original_classify
        restore()


def _assert_durable_task_grid(root: Path) -> None:
    state = _LedgerState(root)
    state.project_root = root / "durable-project"
    state.add(
        "foreign-running",
        "job_submission",
        _submission("foreign-running"),
        produced_by_run_id="run-foreign",
    )
    task_list = TaskList(state.project_root / "tasks")
    original_task = task_list.create(
        title="Finalize durable external job",
        owner_node="experiment",
        run_id="run-previous",
        description=(
            "external_job_key=run-previous:local:task-only\n"
            "scheduler=local\n"
            "job_id=task-only\n"
            "workdir=/tmp/c6-task\n"
            "output_roots=[]"
        ),
    )

    restore = _patch_read_only_observers()
    original_classify = hooks._classify_external_job_status
    hooks._classify_external_job_status = lambda _submission: "running"
    try:
        preview = asyncio.run(sediment._preview_experiment_contract(state))
        preview_ids = [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in preview["open_external_jobs"]
        ]
        assert preview_ids == [("local", "task-only")], preview
        assert "foreign-running" not in state.reads, state.reads

        for _ in range(2):
            loop_result = SimpleNamespace(status="completed", final_text="")
            hooks.external_job_handoff_on_end(_ctx(state), loop_result)
            handoffs = state.hook_state[
                "external_job_waiting"]["open_external_jobs"]
            assert [
                (str(row.get("scheduler")), str(row.get("job_id")))
                for row in handoffs
            ] == preview_ids, handoffs
            assert [row.get("task_id") for row in handoffs] == [original_task.id]
            assert [task.id for task in task_list.list_all()] == [original_task.id]
            assert loop_result.status == "blocked"
        task_list.complete(original_task.id)
        completed_task_preview = asyncio.run(
            sediment._preview_experiment_contract(state))
        assert [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in completed_task_preview["open_external_jobs"]
        ] == preview_ids
        assert "foreign-running" not in state.reads, state.reads
    finally:
        hooks._classify_external_job_status = original_classify
        restore()


def _assert_identity_recovery_task_grid(root: Path) -> None:
    state = _LedgerState(root)
    state.project_root = root / "identity-recovery-project"
    state.add(
        "foreign-unresolved",
        "job_submission",
        _unresolved("accepted_identity_unresolved"),
        produced_by_run_id="run-foreign",
    )
    task_list = TaskList(state.project_root / "tasks")
    recovery_task = task_list.create(
        title="Recover external job identity",
        owner_node="experiment",
        run_id="run-foreign",
        description=(
            "external_job_identity_recovery_key=recovery-case-1\n"
            "experiment_workflow_status=accepted_identity_unresolved\n"
            "scheduler=local\n"
            "submission_nonce=nonce-1\n"
            "do_not_resubmit=true"
        ),
    )

    restore = _patch_read_only_observers()
    try:
        preview = asyncio.run(sediment._preview_experiment_contract(state))
        expected_reason = (
            _ERROR_PREFIX
            + "scheduler submission outcome/identity remains unresolved: "
            + f"recovery task {recovery_task.id} (recovery-case-1)"
        )
        assert preview["failed_checks"] == [_FAILED_CHECK], preview
        assert preview["checks"][_FAILED_CHECK] == {
            "passed": False,
            "reason": expected_reason,
        }
        assert preview["experiment_workflow_status"] == "awaiting_external_job"
        assert "foreign-unresolved" not in state.reads, state.reads

        gate = hooks._external_job_finish_gate(_ctx(state))
        assert gate and expected_reason in gate[0].content
        loop_result = SimpleNamespace(status="completed", final_text="")
        hooks.external_job_handoff_on_end(_ctx(state), loop_result)
        assert loop_result.status == "blocked"
        assert state.hook_state["experiment_downstream_blocked"] == {
            "reason": expected_reason,
            "failed_checks": [_FAILED_CHECK],
            "review_eligibility": False,
        }
        blocker = next(
            row for row in state.hook_state["blockers"]
            if row.get("blocker_id") == _BLOCKER_ID
        )
        assert blocker["summary"] == (
            "unable to read authoritative external job submission records; "
            "cannot verify that external work is finalized"
        )

        task_list.complete(recovery_task.id)
        still_owed = asyncio.run(sediment._preview_experiment_contract(state))
        assert still_owed["checks"][_FAILED_CHECK]["reason"] == expected_reason

        reconciled = {
            **_submission("recovered-job"),
            "submission_nonce": "nonce-1",
            "submission_persistence": {"status": "identity_reconciled"},
            "reconciliation": {"query_status": "unique"},
        }
        state.add(
            "foreign-reconciled",
            "external_job_submission_recovery",
            reconciled,
            produced_by_run_id="run-foreign",
        )
        recovered_preview = asyncio.run(
            sediment._preview_experiment_contract(state))
        recovered_ids = [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in recovered_preview["open_external_jobs"]
        ]
        assert recovered_ids == [("local", "recovered-job")], recovered_preview
        recovered_result = SimpleNamespace(status="completed", final_text="")
        hooks.external_job_handoff_on_end(_ctx(state), recovered_result)
        recovered_handoffs = state.hook_state[
            "external_job_waiting"]["open_external_jobs"]
        assert [
            (str(row.get("scheduler")), str(row.get("job_id")))
            for row in recovered_handoffs
        ] == recovered_ids
        assert recovered_result.status == "blocked"
        assert len(task_list.list_all()) == 2

        state.add(
            "recovered-finalized",
            "external_job_lifecycle",
            {
                "scheduler": "local",
                "job_id": "recovered-job",
                "submission_nonce": "nonce-1",
                "lifecycle_status": "finalized",
            },
        )
        settled = asyncio.run(sediment._preview_experiment_contract(state))
        assert settled["overall_status"] == "completed", settled
        assert settled["failed_checks"] == [], settled
    finally:
        restore()


def _assert_unresolved_grid(root: Path, status: str) -> None:
    state = _LedgerState(root)
    state.add("own-running", "job_submission", _submission("own-running"))
    state.add("own-cancelled", "job_submission", _submission("own-cancelled"))
    state.add(
        "cancelled-lifecycle",
        "external_job_lifecycle",
        {
            "scheduler": "local",
            "job_id": "own-cancelled",
            "lifecycle_status": "cancelled",
        },
    )
    unresolved_id = f"own-{status}"
    state.add(unresolved_id, "job_submission", _unresolved(status))
    state.add(
        "foreign-running",
        "job_submission",
        _submission("foreign-running"),
        produced_by_run_id="run-foreign",
    )

    restore = _patch_read_only_observers()
    try:
        # Preview is an observation tool: it must report the strict rejection,
        # never raise it to its caller.
        preview = asyncio.run(sediment._preview_experiment_contract(state))
        assert preview["overall_status"] == "incomplete", preview
        assert preview["experiment_workflow_status"] == "awaiting_external_job", preview
        assert preview["failed_checks"] == [_FAILED_CHECK], preview
        check = preview["checks"][_FAILED_CHECK]
        assert check["passed"] is False, check
        expected_reason = (
            _ERROR_PREFIX
            + "scheduler submission outcome/identity remains unresolved: "
            + unresolved_id
        )
        assert check["reason"] == expected_reason, check
        assert preview["review_eligibility"] is False

        # The final gate consumes the same rejection and retains its established
        # blocker id, wording, failed-check name and status transition verbatim.
        loop_result = SimpleNamespace(status="completed", final_text="")
        hooks.external_job_handoff_on_end(_ctx(state), loop_result)
        assert loop_result.status == "blocked"
        assert state.hook_state["experiment_downstream_blocked"] == {
            "reason": expected_reason,
            "failed_checks": [_FAILED_CHECK],
            "review_eligibility": False,
        }
        blocker = next(
            row
            for row in state.hook_state["blockers"]
            if row.get("blocker_id") == _BLOCKER_ID
        )
        assert blocker == {
            "blocker_id": _BLOCKER_ID,
            "category": "closure",
            "summary": (
                "unable to read authoritative external job submission records; "
                "cannot verify that external work is finalized"
            ),
            "retryable_after_change": True,
        }, blocker
    finally:
        restore()


def main() -> None:
    temporary = tempfile.TemporaryDirectory(prefix="probe-c6-single-judgement-")
    root = Path(temporary.name)
    cases = [
        ("running_cancelled_foreign_identity", lambda: _assert_identity_grid(root)),
        ("durable_task_foreign_receipt", lambda: _assert_durable_task_grid(root)),
        (
            "cross_run_identity_recovery_task",
            lambda: _assert_identity_recovery_task_grid(root),
        ),
        (
            "accepted_identity_unresolved",
            lambda: _assert_unresolved_grid(root, "accepted_identity_unresolved"),
        ),
        (
            "submission_outcome_unknown",
            lambda: _assert_unresolved_grid(root, "submission_outcome_unknown"),
        ),
    ]
    failures: list[str] = []
    for name, case in cases:
        try:
            case()
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"{name}: FAIL ({type(exc).__name__}: {exc})")
        else:
            print(f"{name}: PASS")
    temporary.cleanup()
    assert not failures, failures
    print("probe_c6_single_judgement: PASS")


if __name__ == "__main__":
    main()
