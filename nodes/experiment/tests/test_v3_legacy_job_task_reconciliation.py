"""Retired v2 handoff tasks must not outlive their exact v3 job facts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.state import State
from core.tasks import TaskList
from nodes.experiment import hooks
from nodes.experiment.tools import job_settlement, ledger, resource_manager as rm


def _state(tmp_path, *, ready=True):
    state = State.new("experiment", tmp_path / "runs")
    state.project_root = tmp_path / "project"
    if ready:
        ledger.open_ledger(state, write=True, bootstrap=True)
        ledger.mark_bootstrap_ready(state)
    return state


def _submission(**changes):
    return {"status": "success", "dry_run": False, "scheduler": "local",
            "job_id": "build", "job_name": "source-build", "submission_nonce": "nonce-1",
            "container_runtime_id": "a" * 64, "workdir": "/work", **changes}


def _legacy_task(state, record):
    task_id = hooks._ensure_external_job_task(state, record, hooks._external_job_key(state, record))
    return TaskList(state.project_root / "tasks").get(task_id)


def _bind(state, record):
    owner = ledger.open_ledger(state, write=True)
    owner.append("intent", nonce=record["submission_nonce"], scheduler=record["scheduler"],
                 output_root=str(state.root), output_roots=[str(state.root)])
    owner.append("job_bound", nonce=record["submission_nonce"], scheduler=record["scheduler"],
                 external_id=record["container_runtime_id"],
                 container_runtime_id=record["container_runtime_id"])


def _settle(state, record, exit_code=0):
    return job_settlement.settle_terminal(state, record,
        evidence={"source": "native_job_record", "sandbox_status": "exited", "exit_code": exit_code},
        observed_by="job_status", output_observation={})


@pytest.mark.parametrize("scheduler", ["local", "slurm"])
def test_v3_submit_does_not_create_retired_workflow_or_task(tmp_path, scheduler):
    state = _state(tmp_path)
    assert hooks.persist_external_job_workflow(state, _submission(scheduler=scheduler)) is None
    assert not (state.project_root / "tasks").exists()
    assert not state.list_artifacts("external_job_workflow")
    assert "external_job_waiting" not in state.hook_state


def test_legacy_protocol_still_creates_its_recoverable_handoff(tmp_path):
    state = _state(tmp_path, ready=False)
    task_id = hooks.persist_external_job_workflow(state, _submission())
    assert TaskList(state.project_root / "tasks").get(task_id).status == "pending"
    assert len(state.list_artifacts("external_job_workflow")) == 1


def test_initializing_v3_never_falls_back_to_legacy_workflow(tmp_path):
    state = _state(tmp_path, ready=False)
    ledger.open_ledger(state, write=True, bootstrap=True)
    assert ledger.bootstrap_phase(state) == "initializing"
    assert hooks.persist_external_job_workflow(state, _submission()) is None
    assert not (state.project_root / "tasks").exists()


@pytest.mark.parametrize("exit_code,derived", [(0, "COMPLETED"), (2, "FAILED")])
def test_terminal_settlement_closes_exact_legacy_task_without_changing_outcome(tmp_path, exit_code, derived):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    _bind(state, record)
    result = _settle(state, record, exit_code)
    assert result["settled"] is True
    assert result["legacy_task_reconciliation"]["completed_task_ids"] == [task.id]
    stored = TaskList(state.project_root / "tasks").get(task.task_instance_uuid)
    assert stored.status == "completed"
    assert derived in stored.notes[-1]
    row = job_settlement.existing_terminal(state, record["submission_nonce"])
    assert (row["derived_state"], row["exit_code"]) == (derived, exit_code)


@pytest.mark.parametrize("change", [
    {"submission_nonce": "another"}, {"container_runtime_id": "b" * 64},
    {"job_id": "other-job"}, {"launch_host": "different-host"},
    {"container_runtime_id": ""},
])
def test_other_or_incomplete_task_identity_stays_open(tmp_path, change):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, dict(record, **change))
    _bind(state, record)
    assert _settle(state, record)["settled"] is True
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


def test_old_task_without_os_ticks_has_exact_immutable_runtime_identity(tmp_path):
    state = _state(tmp_path)
    record = _submission(process_start_ticks="77")
    task = _legacy_task(state, record)
    # Reproduce the historical shape explicitly: the current writer now
    # preserves both OS identity fields, while the old writer omitted them.
    task.description = "\n".join(
        line for line in task.description.splitlines()
        if not line.startswith(("process_group_id=", "process_start_ticks="))
    )
    TaskList(state.project_root / "tasks")._persist(task)
    assert "process_start_ticks=" not in task.description
    _bind(state, record)
    _settle(state, record)
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "completed"


def test_explicit_conflicting_process_identity_is_not_ignored(tmp_path):
    state = _state(tmp_path)
    record = _submission(process_start_ticks="77")
    task = _legacy_task(state, record)
    task.description += "\nprocess_start_ticks=88"
    TaskList(state.project_root / "tasks")._persist(task)
    _bind(state, record)
    _settle(state, record)
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


def test_other_run_task_is_not_closed_even_with_same_physical_identity(tmp_path):
    state = _state(tmp_path)
    record = _submission()
    other = replace(state, run_id="other-run")
    task = _legacy_task(other, record)
    _bind(state, record)
    _settle(state, record)
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


@pytest.mark.parametrize("phase", ["running", "unknown"])
def test_running_or_unknown_has_no_terminal_receipt_and_keeps_task(tmp_path, phase):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    _bind(state, record)
    result = rm._settle_if_terminal(state, record, scheduler_result={
        "raw": {"sandbox_state": {"status": phase, "running": phase == "running"}}},
        evidence=None, observed_by="job_status")
    assert not result or result["settled"] is False
    assert job_settlement.existing_terminal(state, record["submission_nonce"]) is None
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


def test_crash_after_terminal_row_recovers_task_once_without_repeating_job(tmp_path, monkeypatch):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    _bind(state, record)
    complete = TaskList.complete
    def fail(*_args, **_kwargs):
        raise OSError("injected task persistence failure")
    monkeypatch.setattr(TaskList, "complete", fail)
    first = _settle(state, record)
    assert first["settled"] is True
    assert first["legacy_task_reconciliation"]["status"] == "error"
    monkeypatch.setattr(TaskList, "complete", complete)
    second = _settle(state, record)
    third = _settle(state, record)
    assert second["already"] is True and third["already"] is True
    stored = TaskList(state.project_root / "tasks").get(task.task_instance_uuid)
    assert stored.status == "completed" and len(stored.notes) == 1
    assert len([r for r in ledger.open_ledger(state).records() if r["kind"] == "job_terminal"]) == 1


def test_runtime_rebinding_conflict_does_not_close_task(tmp_path):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    _bind(state, record)
    ledger.open_ledger(state, write=True).append("job_reconciled", nonce=record["submission_nonce"],
        container_runtime_id="b" * 64, external_id=record["job_id"], state="submitted")
    _settle(state, record)
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


def test_no_task_update_when_terminal_cannot_be_persisted(tmp_path, monkeypatch):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    _bind(state, record)
    append_once = ledger.Ledger.append_once
    def fail(owner, kind, *args, **kwargs):
        if kind == "job_terminal":
            raise OSError("injected terminal write failure")
        return append_once(owner, kind, *args, **kwargs)
    monkeypatch.setattr(ledger.Ledger, "append_once", fail)
    result = _settle(state, record)
    assert result["settled"] is False
    assert job_settlement.existing_terminal(state, record["submission_nonce"]) is None
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


@pytest.mark.parametrize("target", [None, {"host": "cluster.example", "user": "account"}])
def test_old_remote_task_does_not_close_without_supported_target_identity(tmp_path, target):
    state = _state(tmp_path)
    record = _submission(scheduler="slurm", container_runtime_id=None)
    if target is not None:
        record["slurm_target"] = target
    task = _legacy_task(state, record)
    owner = ledger.open_ledger(state, write=True)
    owner.append("intent", nonce=record["submission_nonce"], scheduler="slurm",
                 output_root=str(state.root), output_roots=[str(state.root)])
    owner.append("job_bound", nonce=record["submission_nonce"], scheduler="slurm",
                 external_id=record["job_id"])
    result = job_settlement.settle_terminal(state, record,
        evidence={"allocation_state": "COMPLETED", "exit_code": 0},
        observed_by="job_status", output_observation={})
    assert result["settled"] is True
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"


def test_local_terminal_without_an_original_binding_cannot_close_task(tmp_path):
    state = _state(tmp_path)
    record = _submission()
    task = _legacy_task(state, record)
    owner = ledger.open_ledger(state, write=True)
    owner.append("intent", nonce=record["submission_nonce"], scheduler="local",
                 output_root=str(state.root), output_roots=[str(state.root)])
    result = _settle(state, record)
    assert result["settled"] is True
    assert TaskList(state.project_root / "tasks").get(task.task_instance_uuid).status == "pending"
