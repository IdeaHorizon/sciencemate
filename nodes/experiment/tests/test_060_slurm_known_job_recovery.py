"""A scheduler-accepted job stays queryable even if binding persistence fails."""
from __future__ import annotations

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import ledger, resource_manager as rm, run_bootstrap, safe_bash
from shared.lib import dangerous_commands as danger


@pytest.mark.parametrize("gpus", [0, 1])
def test_known_cpu_or_single_gpu_job_without_durable_binding_remains_queryable(
    tmp_path, monkeypatch, gpus,
):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="slurm-recovery", session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": (
            "Run one CPU job and inspect the result."
            if gpus == 0 else "Run one single-GPU job and inspect the result."
        ),
        "prereg_assignment": {"kind": "none", "reason": "fixed software check"},
    }
    asyncio.run(run_bootstrap.establish(state))
    runtime = rm.experiment_output_dir(state, "runtime", create=True)
    workdir, output_dir = runtime / "work", runtime / "logs"
    workdir.mkdir()
    output_dir.mkdir()

    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_args: None)
    monkeypatch.setattr(safe_bash, "_analyze_shell_path_effects", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(danger, "bypass_enabled", lambda: True)
    monkeypatch.setattr(rm, "_run", lambda *_args, **_kwargs: {
        "ok": True, "returncode": 0, "stdout": "733", "stderr": "",
    })
    original_try_record = ledger.try_record

    def lose_binding(owner, kind, **fields):
        if kind == "job_bound":
            return None
        return original_try_record(owner, kind, **fields)

    monkeypatch.setattr(ledger, "try_record", lose_binding)
    result = asyncio.run(rm._submit_job(
        state, command="printf '49\\n' > result.txt", scheduler="slurm",
        job_name=f"known_unbound_{'gpu' if gpus else 'cpu'}",
        mpi_ranks=1, cpus_per_rank=1, gpus=gpus,
        memory_gb=1.0, storage_gb=1.0, walltime_minutes=5,
        workdir=str(workdir), output_dir=str(output_dir),
        output_paths=[str(workdir)], dry_run=False,
    ))

    assert result["status"] == "submitted_needs_recovery", result
    assert result["job_id"] == "733"
    assert result["requested_resources"]["gpus"] == gpus
    assert result["do_not_resubmit"] is True
    assert result["submission_persistence"]["status"] == "recovered"
    assert result["external_workflow_task_id"]
    receipts = rm.read_owed_submission_receipts(state)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "submitted_needs_recovery"
    assert receipts[0]["requested_resources"]["gpus"] == gpus
    owed = rm.owed_external_job_closure_records(state)
    assert any(row.get("job_id") == "733" for row in owed)
    assert rm._external_job_record(state, "slurm", "733") is not None
    monkeypatch.setattr(rm, "_job_status_sync", lambda *_args, **_kwargs: {
        "status": "success", "raw": {"ok": True, "stdout": "733 running"},
    })
    queried = asyncio.run(rm._job_status(state, "slurm", "733"))
    assert queried["status"] == "success", queried
