"""Only the durable submission producer grants destructive job authority."""

import asyncio
import json

from core.state import State
from core.tasks import TaskList
from nodes.experiment.tools import resource_manager as manager
from shared.lib import dangerous_commands


def test_shared_project_receipt_does_not_allow_another_run_to_cancel(tmp_path, monkeypatch):
    owner = State.new("experiment", tmp_path / "runs", project_id="ownership")
    other = State.new("experiment", tmp_path / "runs", project_id="ownership")
    payload = {
        "status": "success",
        "dry_run": False,
        "scheduler": "slurm",
        "job_id": "offline-owned",
        "submission_nonce": "nonce-owner",
        "scheduler_cluster": "cluster",
        "resource_uid": "resource",
        "output_roots": [],
        "submitted_by_run_id": other.run_id,
    }
    owner.save_artifact("job_submission", "owned", json.dumps(payload))
    # Model-writable task metadata must never grant cancellation authority.
    TaskList(other.project_root / "tasks").create(
        "shared job",
        run_id=other.run_id,
        owner_node="experiment",
        description=(
            f"external_job_key={other.run_id}:slurm:offline-owned\n"
            "scheduler=slurm\njob_id=offline-owned\nsubmission_nonce=nonce-owner"
        ),
    )
    calls = []

    async def unknown(*args, **kwargs):
        return None

    def cancel(*args, **kwargs):
        calls.append(args)
        return {"ok": False, "outcome_unknown": True, "error": "offline observation"}

    monkeypatch.setattr(manager, "_observed_job_end", unknown)
    monkeypatch.setattr(manager, "_cancel_sync", cancel)
    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    denied = asyncio.run(
        manager._cancel_job(other, "slurm", "offline-owned", submission_nonce="nonce-owner")
    )
    assert denied["status"] == "error", denied
    assert calls == []
    assert not other.list_artifacts("external_job_cancellation_intent")
    allowed = asyncio.run(
        manager._cancel_job(owner, "slurm", "offline-owned", submission_nonce="nonce-owner")
    )
    assert allowed["status"] == "cancellation_outcome_unknown", allowed
    assert len(calls) == 1
    reopened = State.reopen("experiment", tmp_path / "runs", owner.run_id, project_id="ownership")
    retried = asyncio.run(
        manager._cancel_job(reopened, "slurm", "offline-owned", submission_nonce="nonce-owner")
    )
    assert retried["status"] == "cancellation_reconciliation_required", retried
    assert len(calls) == 1
