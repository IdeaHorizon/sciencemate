"""A continuation task must not invent a second identity for its receipt."""
import asyncio
import json

import pytest

from core.state import State
from core.tasks import TaskList
from nodes.experiment.hooks import _ensure_external_job_task
from nodes.experiment.tools import resource_manager as manager


def submission(**changes):
    return {"status": "success", "dry_run": False, "scheduler": "slurm",
            "job_id": "123", "submission_nonce": "nonce-owner", "launch_host": "host",
            "process_group_id": "456", "process_start_ticks": "789", **changes}


def state_with_receipt(tmp_path, payload):
    state = State.new("experiment", tmp_path / "runs", project_id="identity")
    state.save_artifact("job_submission", "submitted", json.dumps(payload))
    return state


def test_generated_task_preserves_the_submission_identity(tmp_path):
    payload = submission()
    state = state_with_receipt(tmp_path, payload)
    assert _ensure_external_job_task(state, payload, "owner:slurm:123")
    task = manager._task_external_jobs(state)[0]
    assert manager._job_key_for_record(task) == manager._job_key_for_record(payload)
    assert manager._external_job_record(state, "slurm", "123", submission_nonce="nonce-owner")


@pytest.mark.parametrize("consumer", ["lookup", "status", "health", "wait"])
def test_legacy_sparse_task_does_not_hide_the_durable_receipt(tmp_path, monkeypatch, consumer):
    payload = submission()
    state = state_with_receipt(tmp_path, payload)
    TaskList(state.project_root / "tasks").create(
        "legacy continuation", owner_node="experiment", run_id=state.run_id,
        description="external_job_key=owner:slurm:123\nscheduler=slurm\njob_id=123\n"
                    "submission_nonce=nonce-owner\nlaunch_host=host",
    )
    state = State.reopen("experiment", tmp_path / "runs", state.run_id, project_id="identity")
    monkeypatch.setattr(manager, "_job_status_sync", lambda *a, **kw: {
        "status": "success", "scheduler": "slurm", "job_id": "123",
        "raw": {"ok": True, "stdout": "RUNNING"},
    })
    if consumer == "lookup":
        result = manager._external_job_record(state, "slurm", "123", submission_nonce="nonce-owner")
        assert result and result["process_start_ticks"] == "789"
    else:
        function = {"status": manager._job_status, "health": manager._check_external_job_health,
                    "wait": manager._wait_for_external_job}[consumer]
        args = {"max_wait_s": 0} if consumer == "wait" else {}
        result = asyncio.run(function(
            state, "slurm", "123", submission_nonce="nonce-owner", **args))
        assert result["status"] == "success", result


@pytest.mark.parametrize("field,value", [("submission_nonce", "different"),
                                         ("launch_host", "elsewhere"),
                                         ("process_start_ticks", "790")])
def test_distinct_task_scopes_stay_ambiguous(tmp_path, field, value):
    payload = submission()
    state = state_with_receipt(tmp_path, payload)
    other = submission(**{field: value})
    assert _ensure_external_job_task(state, other, "other:slurm:123")
    assert manager._external_job_record(state, "slurm", "123") is None


def test_nonce_does_not_hide_two_conflicting_durable_receipts(tmp_path):
    payload = submission()
    state = state_with_receipt(tmp_path, payload)
    state.save_artifact("job_submission", "conflicting",
                        json.dumps(submission(process_start_ticks="790")))
    assert manager._external_job_record(
        state, "slurm", "123", submission_nonce="nonce-owner") is None


def test_task_cannot_grant_destructive_ownership(tmp_path):
    state = State.new("experiment", tmp_path / "runs", project_id="identity")
    assert _ensure_external_job_task(state, submission(), "other:slurm:123")
    result = asyncio.run(manager._cancel_job(state, "slurm", "123", submission_nonce="nonce-owner"))
    assert result["status"] == "error"
    assert not state.list_artifacts("external_job_cancellation_intent")


def test_cancellation_with_a_generated_task_is_owned_and_not_repeated(tmp_path, monkeypatch):
    from shared.lib import dangerous_commands

    state = state_with_receipt(tmp_path, submission())
    assert _ensure_external_job_task(state, submission(), "owner:slurm:123")
    cancellations = []

    async def unknown(*args, **kwargs):
        return None

    def cancel(*args, **kwargs):
        cancellations.append(args)
        return {"ok": False, "outcome_unknown": True, "error": "fixture scheduler unavailable"}

    monkeypatch.setattr(manager, "_observed_job_end", unknown)
    monkeypatch.setattr(manager, "_cancel_sync", cancel)
    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    first = asyncio.run(manager._cancel_job(state, "slurm", "123", submission_nonce="nonce-owner"))
    assert first["status"] == "cancellation_outcome_unknown"
    state = State.reopen("experiment", tmp_path / "runs", state.run_id, project_id="identity")
    retry = asyncio.run(manager._cancel_job(state, "slurm", "123", submission_nonce="nonce-owner"))
    assert retry["status"] == "cancellation_reconciliation_required"
    assert len(cancellations) == 1
