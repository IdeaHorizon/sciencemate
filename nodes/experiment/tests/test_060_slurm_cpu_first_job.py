"""Focused single-task CPU/one-GPU Slurm contract without a real scheduler."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import ledger, resource_manager as rm, run_bootstrap


def _state_with_paths(tmp_path: Path) -> tuple[State, Path, Path]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="slurm-first-cpu-or-gpu-job",
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run one bounded CPU or single-GPU Slurm batch and report output.",
        "prereg_assignment": {"kind": "none", "reason": "operational job test"},
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    runtime = rm.experiment_output_dir(state, "runtime", create=True)
    workdir = runtime / "slurm-work"
    output_dir = runtime / "slurm-output"
    workdir.mkdir()
    output_dir.mkdir()
    return state, workdir, output_dir


def _forbid_submit(*_args, **_kwargs):
    pytest.fail("the rejected request must not reach a physical submit boundary")


def test_cpu_submit_persists_intent_before_sbatch_and_uses_explicit_output_root(
    tmp_path, monkeypatch,
):
    state, workdir, output_dir = _state_with_paths(tmp_path)
    calls = []

    def fake_run(argv, timeout=20):
        assert argv[0] == "sbatch", argv
        calls.append(argv)
        rows = ledger.open_ledger(state).records()
        kinds = [row["kind"] for row in rows]
        assert "intent" in kinds and "submission_attempted" in kinds
        assert kinds.index("intent") < kinds.index("submission_attempted")
        script = Path(argv[-1])
        assert script.is_file()
        script_text = script.read_text(encoding="utf-8")
        assert "#SBATCH --output=" not in script_text
        assert any(arg.startswith(f"--output={output_dir}/.harness-bootstrap-")
                   for arg in argv)
        assert any(arg.startswith(f"--error={output_dir}/.harness-bootstrap-")
                   for arg in argv)
        assert "#SBATCH --ntasks=1" in script_text
        assert "#SBATCH --nodes=1" in script_text
        assert "#SBATCH --gres=" not in script_text
        assert "HARNESS_GPU_CONTEXT" not in script_text
        assert f"--chdir={workdir}" in argv
        return {"ok": True, "stdout": "731;cluster-a\n", "stderr": "", "returncode": 0}

    monkeypatch.setattr(rm, "_run", fake_run)
    submitted = rm._submit_sync(
        runtime_root=tmp_path / "submit-host-runtime",
        scheduler="slurm",
        command="printf 'measured\\n' > result.txt",
        job_name="first_cpu_job",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=1.0,
        storage_gb=1.0,
        walltime_minutes=5,
        queue=None,
        nodelist=None,
        image=None,
        workdir=str(workdir),
        dry_run=False,
        namespace=None,
        output_dir=str(output_dir),
        output_paths=[str(output_dir)],
        stage_in=None,
        state=state,
        submission_nonce="first-cpu-nonce",
    )

    assert len(calls) == 1
    assert submitted["status"] == "success", submitted
    assert submitted["job_id"] == "731"
    assert submitted["submission_intent_artifact_id"]
    assert Path(submitted["bootstrap_log_dir"]).parent == output_dir
    assert Path(submitted["bootstrap_log_dir"]).is_dir()
    assert not Path(submitted["bootstrap_log_dir"]).is_relative_to(
        tmp_path / "submit-host-runtime"
    )
    jobs = ledger.project_jobs(ledger.open_ledger(state).records())
    assert jobs["first-cpu-nonce"]["external_id"] == "731"


def test_single_gpu_submit_persists_request_and_renders_bounded_gres_context(
    tmp_path, monkeypatch,
):
    state, workdir, output_dir = _state_with_paths(tmp_path)

    def fake_run(argv, timeout=20):
        assert argv[0] == "sbatch", argv
        script_text = Path(argv[-1]).read_text(encoding="utf-8")
        assert "#SBATCH --nodes=1" in script_text
        assert "#SBATCH --ntasks=1" in script_text
        assert "#SBATCH --gres=gpu:1" in script_text
        assert "HARNESS_GPU_CONTEXT" in script_text
        for variable in (
            "SLURM_JOB_NUM_NODES", "SLURM_NTASKS", "SLURM_JOB_GPUS",
            "SLURM_GPUS_ON_NODE", "CUDA_VISIBLE_DEVICES",
        ):
            assert variable in script_text

        artifacts = state.list_artifacts("external_submission_intent")
        assert len(artifacts) == 1
        intent = json.loads(state.read_artifact(artifacts[0]["id"])["content"])
        assert intent["requested_resources"] == {
            "nodes": 1,
            "mpi_ranks": 1,
            "cpus_per_rank": 2,
            "total_cpus": 2,
            "gpus": 1,
            "memory_gb": 2.0,
            "storage_contract": {
                "input_gb": 1.0,
                "status": "not_applied",
                "reason": "slurm_adapter_has_no_storage_limit_contract",
            },
            "walltime_minutes": 5,
        }
        ledger_intent = next(
            row for row in ledger.open_ledger(state).records()
            if row["kind"] == "intent"
        )
        assert ledger_intent["requested_resources"] == intent["requested_resources"]
        return {
            "ok": True, "stdout": "734;cluster-a\n", "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr(rm, "_run", fake_run)
    submitted = rm._submit_sync(
        runtime_root=tmp_path / "submit-host-runtime",
        scheduler="slurm",
        command="python preinstalled_gpu_workload.py",
        job_name="first_gpu_job",
        mpi_ranks=1,
        cpus_per_rank=2,
        gpus=1,
        memory_gb=2.0,
        storage_gb=1.0,
        walltime_minutes=5,
        queue=None,
        nodelist=None,
        image=None,
        workdir=str(workdir),
        dry_run=False,
        namespace=None,
        output_dir=str(output_dir),
        output_paths=[str(output_dir)],
        stage_in=None,
        state=state,
        submission_nonce="first-gpu-nonce",
    )

    assert submitted["status"] == "success", submitted
    assert submitted["job_id"] == "734"
    assert submitted["requested_resources"]["gpus"] == 1


def test_cpu_confirmation_cannot_authorize_a_single_gpu_request(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import safe_bash
    from shared.lib import dangerous_commands as danger

    state, workdir, output_dir = _state_with_paths(tmp_path)
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_args: None)
    monkeypatch.setattr(
        safe_bash, "_analyze_shell_path_effects", lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(danger, "bypass_enabled", lambda: False)
    approval_texts: list[str] = []
    pause_previews: list[str] = []
    approved: set[str] = set()

    def is_confirmed(_state, text):
        approval_texts.append(text)
        return text in approved

    monkeypatch.setattr(danger, "is_confirmed", is_confirmed)

    def build_pause(_state, **kwargs):
        pause_previews.append(kwargs["preview"])
        return {"status": "pause", "approval_text": kwargs["text"]}

    monkeypatch.setattr(danger, "build_pause_payload", build_pause)
    monkeypatch.setattr(rm, "_submit_sync", _forbid_submit)
    common = {
        "command": "python preinstalled_workload.py",
        "scheduler": "slurm",
        "job_name": "bounded_job",
        "mpi_ranks": 1,
        "cpus_per_rank": 1,
        "memory_gb": 1.0,
        "walltime_minutes": 5,
        "workdir": str(workdir),
        "output_dir": str(output_dir),
        "dry_run": False,
    }

    cpu = asyncio.run(rm._submit_job(state, gpus=0, **common))
    assert cpu["status"] == "pause", cpu
    cpu_approval = approval_texts[-1]
    approved.add(cpu_approval)

    gpu = asyncio.run(rm._submit_job(state, gpus=1, **common))
    assert gpu["status"] == "pause", gpu
    gpu_approval = approval_texts[-1]
    assert gpu_approval != cpu_approval
    assert json.loads(cpu_approval)["gpus"] == 0
    assert json.loads(gpu_approval)["gpus"] == 1

    gpu_preview = pause_previews[-1]
    assert "requested_resources=" in gpu_preview
    assert '"nodes": 1' in gpu_preview
    assert '"mpi_ranks": 1' in gpu_preview
    assert '"gpus": 1' in gpu_preview
    assert "slurm_adapter_has_no_storage_limit_contract" in gpu_preview


def test_remote_environment_evidence_does_not_probe_submit_host_gpu(
    tmp_path, monkeypatch,
):
    state, _, _ = _state_with_paths(tmp_path)
    probes: list[list[str]] = []

    def probe(argv, timeout=12):
        probes.append(argv)
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(rm, "_execution_probe", probe)
    monkeypatch.setattr(
        rm, "_scheduler_environment_snapshot",
        lambda *_args, **_kwargs: {"job_snapshot": {"ok": True}},
    )
    rm._persist_execution_environment_evidence(
        state,
        {
            "scheduler": "slurm", "job_id": "735", "job_name": "gpu-job",
            "command": "python preinstalled_gpu_workload.py", "script_path": "",
        },
        "terminal",
    )

    assert all(argv[0] != "nvidia-smi" for argv in probes)
    artifacts = state.list_artifacts("execution_environment_evidence")
    payload = json.loads(state.read_artifact(artifacts[-1]["id"])["content"])
    assert payload["host"]["gpu"] == {
        "status": "not_applicable",
        "reason": "submit_host_is_not_slurm_compute_allocation",
    }


@pytest.mark.parametrize("gpus", [0, 1])
def test_public_slurm_single_task_path_does_not_require_local_sandbox_identity(
    tmp_path, monkeypatch, gpus,
):
    from core import sandbox
    from shared.lib import dangerous_commands as danger
    from nodes.experiment.tools import safe_bash

    state, workdir, output_dir = _state_with_paths(tmp_path)
    # The shell analyzer is an independent deployment prerequisite. Its
    # missing Python package must not mask this scheduler-admission contract.
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_args: None)
    monkeypatch.setattr(safe_bash, "_analyze_shell_path_effects", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(danger, "bypass_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "image_name", _forbid_submit)
    monkeypatch.setattr(sandbox, "trusted_image_id", _forbid_submit)
    submitted = []

    def fake_submit(*args, **kwargs):
        submitted.append((args, kwargs))
        assert args[6] == gpus
        return {
            "status": "success", "scheduler": "slurm", "dry_run": False,
            "job_id": "731", "submission_nonce": "first-slurm-public",
            "job_name": "bounded_slurm_job",
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit)
    result = asyncio.run(rm._submit_job(
        state,
        command="echo measured",
        scheduler="slurm",
        job_name="bounded_slurm_job",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=gpus,
        memory_gb=1.0,
        walltime_minutes=5,
        workdir=str(workdir),
        output_dir=str(output_dir),
        dry_run=False,
    ))

    assert result["status"] == "success", result
    assert result["job_id"] == "731"
    assert len(submitted) == 1
    assert "actual_run_params" not in state.hook_state


@pytest.mark.parametrize(
    ("scheduler", "extra", "expected"),
    [
        ("slurm", {"workdir": None}, "slurm_shared_paths_required"),
        ("slurm", {"output_dir": None}, "slurm_shared_paths_required"),
        ("slurm", {"gpus": 2}, "slurm_single_task_cpu_or_gpu_only"),
        ("slurm", {"mpi_ranks": 2}, "slurm_single_task_cpu_or_gpu_only"),
        ("slurm", {"walltime_minutes": None}, "slurm_walltime_required"),
        ("pbs", {}, "remote_sandbox_contract_unavailable"),
    ],
)
def test_unaccepted_remote_request_stops_before_submission(
    tmp_path, monkeypatch, scheduler, extra, expected,
):
    state, workdir, output_dir = _state_with_paths(tmp_path)
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_args: None)
    monkeypatch.setattr(rm, "_submit_sync", _forbid_submit)
    kwargs = {
        "command": "echo measured",
        "scheduler": scheduler,
        "workdir": str(workdir),
        "output_dir": str(output_dir),
        "dry_run": False,
        "walltime_minutes": 5,
    }
    kwargs.update(extra)

    result = asyncio.run(rm._submit_job(state, **kwargs))

    assert result["status"] == "error", result
    assert result["blocker"]["kind"] == expected
    assert state.list_artifacts("external_submission_intent") == []


def test_definite_sbatch_rejection_closes_intent_as_absent(tmp_path, monkeypatch):
    state, workdir, output_dir = _state_with_paths(tmp_path)
    monkeypatch.setattr(rm, "_run", lambda *_args, **_kwargs: {
        "ok": False, "returncode": 1, "stdout": "",
        "stderr": "sbatch: error: invalid account",
    })

    result = rm._submit_sync(
        runtime_root=tmp_path / "submit-host-runtime",
        scheduler="slurm", command="printf measured > result.txt",
        job_name="rejected_cpu_job", mpi_ranks=1, cpus_per_rank=1, gpus=0,
        memory_gb=1.0, storage_gb=1.0, walltime_minutes=5,
        queue=None, nodelist=None, image=None, workdir=str(workdir),
        dry_run=False, namespace=None, output_dir=str(output_dir),
        output_paths=[str(workdir)], stage_in=None, state=state,
        submission_nonce="definite-rejection",
    )

    assert result["status"] == "error", result
    assert result["intent_status"] == "rejected_by_scheduler"
    assert result["intent_closure"]["as"] == "job_absent"
    records = ledger.open_ledger(state).records()
    assert ledger.open_jobs(records) == []
    assert ledger.project_jobs(records)["definite-rejection"]["state"] == "absent"


def test_job_id_without_durable_binding_never_claims_success(tmp_path, monkeypatch):
    state, workdir, output_dir = _state_with_paths(tmp_path)
    monkeypatch.setattr(rm, "_run", lambda *_args, **_kwargs: {
        "ok": True, "returncode": 0, "stdout": "732", "stderr": "",
    })
    original_try_record = ledger.try_record

    def lose_binding(owner, kind, **fields):
        if kind == "job_bound":
            return None
        return original_try_record(owner, kind, **fields)

    monkeypatch.setattr(ledger, "try_record", lose_binding)
    result = rm._submit_sync(
        runtime_root=tmp_path / "submit-host-runtime",
        scheduler="slurm", command="printf measured > result.txt",
        job_name="unbound_cpu_job", mpi_ranks=1, cpus_per_rank=1, gpus=0,
        memory_gb=1.0, storage_gb=1.0, walltime_minutes=5,
        queue=None, nodelist=None, image=None, workdir=str(workdir),
        dry_run=False, namespace=None, output_dir=str(output_dir),
        output_paths=[str(workdir)], stage_in=None, state=state,
        submission_nonce="unbound-accepted",
    )

    assert result["status"] == "submitted_needs_recovery", result
    assert result["job_id"] == "732"
    assert result["do_not_resubmit"] is True
    records = ledger.open_ledger(state).records()
    assert ledger.project_jobs(records)["unbound-accepted"]["state"] == "unknown"
