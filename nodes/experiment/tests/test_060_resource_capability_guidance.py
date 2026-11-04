"""Resource advice must not advertise a backend the public submit path refuses."""

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import resource_manager as rm


def _discovery(monkeypatch, *, local=True, slurm=True, pbs=True, kubernetes=True):
    monkeypatch.setattr(rm, "_local_resources", lambda: {
        "scheduler": "local", "available": local, "cpu_count": 8,
        "memory_available_mb": 64000, "gpus": [{"index": "0"}],
        "gpu_execution_supported": False,
    })
    monkeypatch.setattr(rm, "_detect_slurm", lambda: {
        "scheduler": "slurm", "available": slurm,
        "commands": {"sinfo": slurm, "sbatch": slurm},
        "sinfo_error": "controller unavailable" if slurm else None,
    })
    monkeypatch.setattr(rm, "_detect_pbs", lambda: {
        "scheduler": "pbs", "available": pbs,
    })
    monkeypatch.setattr(rm, "_detect_k8s", lambda _namespace: {
        "scheduler": "kubernetes", "available": kubernetes,
        "submission_contract_available": False,
        "submission_blocker": "kubernetes_volume_contract_required",
    })
    monkeypatch.setattr(rm, "_core_platform_capabilities", lambda: {
        "compute_grants": {"slurm": "declared"},
    })


def _recommend(*, gpus=0, preference=None):
    return rm._recommend("test", 1, 2, gpus, 3.0, 7, None, preference, None)


def test_installed_remote_clients_do_not_make_remote_submit_a_candidate(monkeypatch):
    _discovery(monkeypatch)
    result = rm._discover()
    assert result["recommended_default"] == "local"
    assert result["submittable_schedulers"] == ["local"]
    assert "slurm" in result["available_schedulers"]  # Preserve discovery, not authorization.
    slurm = next(r for r in result["resources"] if r["scheduler"] == "slurm")
    assert slurm["availability_basis"] == "client_tools_detected"
    assert slurm["submission_contract_available"] is False
    assert slurm["submission_blocker"] == "slurm_lifecycle_tools_unavailable"
    assert slurm["sinfo_error"] == "controller unavailable"


@pytest.mark.parametrize("remote", [False, True])
def test_no_local_boundary_does_not_fabricate_a_fallback(monkeypatch, remote):
    _discovery(monkeypatch, local=False, slurm=remote, pbs=remote, kubernetes=remote)
    result = rm._discover()
    assert result["recommended_default"] is None
    assert result["submittable_schedulers"] == []
    advice = _recommend()["recommendation"]
    assert advice["scheduler"] is None
    assert advice["warnings"]


@pytest.mark.parametrize("preference", [None, "local", "slurm", "pbs", "kubernetes"])
def test_gpu_request_is_preserved_without_recommending_an_unusable_backend(monkeypatch, preference):
    _discovery(monkeypatch)
    advice = _recommend(gpus=1, preference=preference)["recommendation"]
    assert advice["scheduler"] is None
    assert advice["gpus"] == 1
    warnings = " ".join(advice["warnings"])
    assert "do not provide GPU execution" in warnings
    assert "use a GPU scheduler such as slurm" not in warnings


@pytest.mark.parametrize("preference", ["slurm", "pbs", "kubernetes"])
def test_explicit_remote_preference_is_not_silently_replaced(monkeypatch, preference):
    _discovery(monkeypatch)
    advice = _recommend(preference=preference)["recommendation"]
    assert advice["scheduler"] is None
    assert advice["scheduler_preference"] == preference
    assert advice["warnings"]


def test_local_cpu_advice_keeps_the_requested_resources(monkeypatch):
    _discovery(monkeypatch)
    result = _recommend(preference="local")
    assert result["status"] == "success"
    advice = result["recommendation"]
    assert advice["scheduler"] == "local"
    assert (advice["total_cpus"], advice["memory_gb"], advice["walltime_minutes"]) == (2, 3.0, 7)


def test_auto_without_a_candidate_stops_before_rendering_or_approval(tmp_path, monkeypatch):
    _discovery(monkeypatch, local=False)
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_a: None)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: pytest.fail("must not render or submit"))
    state = State.new("experiment", tmp_path)
    result = asyncio.run(rm._submit_job(state, command="true", scheduler="auto", dry_run=True))
    assert result.get("reason") == "no_submission_candidate", result
    assert result["side_effects"] == "none"


@pytest.mark.parametrize("scheduler", ["local", "auto"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("gpus", [1, 2])
def test_unsupported_local_gpu_needs_a_change_before_retry_and_has_no_effects(
    tmp_path, monkeypatch, scheduler, dry_run, gpus,
):
    from nodes.experiment.tools import input_admission
    from shared.lib import dangerous_commands as danger

    _discovery(monkeypatch)
    # 能不能分卡是 Core 的回答，与跑测试的机器无关（#893 阶段 1 起 Linux 上可分）。
    from core import sandbox

    monkeypatch.setattr(sandbox, "gpu_capability", lambda: sandbox.GpuCapability(
        visible=4, schedulable=False, isolated=False, reason="测试：后端不给 CUDA 开口"))
    # Even a complete Slurm client set must not turn auto into remote authority.
    monkeypatch.setattr(rm, "_detect_slurm", lambda: {
        "scheduler": "slurm", "available": True,
        "commands": {
            name: True
            for name in ("sinfo", "sbatch", "squeue", "sacct", "scancel")
        },
    })
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_a: None)
    monkeypatch.setattr(input_admission, "check_consumed_input", lambda *_a, **_k: None)

    def forbidden_effect(*_args, **_kwargs):
        pytest.fail("unsupported GPU request must not render, authorize, record intent, or submit")

    for name in ("_script_for", "_persist_submission_intent", "_submit_sync"):
        monkeypatch.setattr(rm, name, forbidden_effect)
    for name in ("is_confirmed", "consume_confirmation", "build_pause_payload"):
        monkeypatch.setattr(danger, name, forbidden_effect)
    state = State.new("experiment", tmp_path)

    # Repeating the rejected request cannot create a second attempt or consume
    # approval. A route/capability change is required instead of blind replay.
    for _ in range(2):
        result = asyncio.run(rm._submit_job(
            state, command="true", scheduler=scheduler, gpus=gpus, dry_run=dry_run,
        ))
        assert result["error_code"] == "local_gpu_execution_unsupported", result
        assert result["requested_gpus"] == gpus
        assert result["side_effects"] == "none"
        assert result["retryable"] is False
        assert result["retryable_after_change"] is True
        assert state.list_artifacts("external_submission_intent") == []
        actions = " ".join(result["next_actions"])
        assert "改用有 GPU 的调度器" not in actions
        assert "真实提交" in actions


@pytest.mark.parametrize("scheduler,expected", [("auto", "local"), ("slurm", "slurm"), ("pbs", "pbs")])
def test_auto_uses_local_but_explicit_remote_dry_run_still_renders(tmp_path, monkeypatch, scheduler, expected):
    _discovery(monkeypatch)
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_a: None)
    state = State.new("experiment", tmp_path)
    result = asyncio.run(rm._submit_job(state, command="true", scheduler=scheduler, dry_run=True))
    assert result["status"] == "success", result
    assert result["scheduler"] == expected
    assert result["dry_run"] is True


@pytest.mark.parametrize("scheduler", ["slurm", "pbs"])
def test_advice_does_not_open_remote_real_submission(tmp_path, monkeypatch, scheduler):
    from nodes.experiment.tools import input_admission

    _discovery(monkeypatch)
    monkeypatch.setattr(rm, "_submission_static_validity_block", lambda *_a: None)
    monkeypatch.setattr(input_admission, "check_consumed_input", lambda *_a, **_k: None)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: pytest.fail("must not submit"))
    state = State.new("experiment", tmp_path)
    result = asyncio.run(rm._submit_job(state, command="true", scheduler=scheduler, dry_run=False))
    if scheduler == "slurm":
        assert result["blocker"]["kind"] == "slurm_shared_paths_required"
    else:
        assert result["blocker"]["kind"] == "remote_sandbox_contract_unavailable"
        assert result["blocker"]["scheduler"] == scheduler

def test_complete_slurm_cli_supports_explicit_cpu_or_one_gpu_not_auto_fallback(monkeypatch):
    _discovery(monkeypatch)
    monkeypatch.setattr(rm, "_detect_slurm", lambda: {
        "scheduler": "slurm", "available": True,
        "commands": {name: True for name in ("sinfo", "sbatch", "squeue", "sacct", "scancel")},
        "sinfo_error": None,
    })
    profile = rm._discover()
    assert profile["submittable_schedulers"] == ["local"]
    assert profile["explicit_submission_schedulers"] == ["slurm"]
    slurm = next(item for item in profile["resources"] if item["scheduler"] == "slurm")
    assert slurm["submission_contract_available"] is True
    assert slurm["submission_mode"] == "explicit_single_task_cpu_or_single_gpu"
    assert slurm["submission_request_envelope"] == {
        "nodes": 1, "mpi_ranks": [1], "gpus": [0, 1],
    }
    assert slurm["remote_write_isolation"] == "missing"
    cpu = _recommend(preference="slurm")["recommendation"]
    assert cpu["scheduler"] == "slurm"
    assert any("not authorization" in warning for warning in cpu["warnings"])

    gpu = _recommend(gpus=1, preference="slurm")["recommendation"]
    assert gpu["scheduler"] == "slurm"
    assert gpu["gpus"] == 1
    assert any("does not prove allocation" in warning for warning in gpu["warnings"])

    too_many_gpus = _recommend(gpus=2, preference="slurm")["recommendation"]
    assert too_many_gpus["scheduler"] is None
    mpi = rm._recommend("test", 2, 1, 0, 3.0, 7, None, "slurm", None)
    assert mpi["recommendation"]["scheduler"] is None
