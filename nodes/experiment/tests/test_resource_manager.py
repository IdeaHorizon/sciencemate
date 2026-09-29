"""Safety checks for externally visible job submission."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path

import pytest

from core.sandbox import availability
from core.state import State
from nodes.experiment.tools import execution_route, resource_manager as rm
from nodes.experiment.tools import timeout_escalation as te
from nodes.experiment.tools.resource_manager import _submit_job
from nodes.experiment.tools.run_contract import _classify_experiment_scope

requires_sandbox = pytest.mark.skipif(
    not availability()[0], reason="mandatory Docker sandbox is unavailable")


def _payload_preflight_sync(*args, **kwargs):
    """同步壳：payload 预检随执行器分档转 async（ABI 探针经咽喉）。"""
    return asyncio.run(rm._preflight_local_payload_executables(*args, **kwargs))


@pytest.fixture
def trusted_sandbox(monkeypatch):
    monkeypatch.setattr("core.sandbox.trusted_image_id", lambda: "sha256:test-sandbox")


def _fixture_node_inputs(purpose: str) -> dict[str, str]:
    return {
        "fixture": "resource_manager",
        "requested_work": purpose,
    }


def _planned_state(tmp_path):
    state = State.new("experiment", tmp_path)
    saved = state.save_artifact("pre_registration", "plan", "# prereg", metadata={
        "run_role": "primary", "analysis_eligible": True,
        "execution_mode": "scientific",
        "expected_params": {"case": "test"},
    })
    state.mark_frozen(saved["id"])   # 冻结只出自账本的 freeze 行
    prereg = state.read_artifact(saved["id"])
    assert isinstance(prereg, dict)
    node_inputs = _fixture_node_inputs(
        "验证受管科学作业的提交、确认、恢复与资源契约。"
    )
    node_inputs["prereg_assignment"] = {
        "kind": "bound",
        "artifact_id": saved["id"],
        "version": int(prereg["version"]),
        "content_hash": prereg["content_hash"],
    }
    state.hook_state["node_inputs"] = node_inputs
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="这些测试验证受冻结预注册约束的科学作业提交、确认与恢复行为。",
    ))
    assert classified["status"] == "success"
    steps = []
    for program in ("echo", "python", "solver", "srun", "printf", "make", "sleep", "sudo"):
        steps.append({
            "id": f"submit_{program}",
            "goal": f"测试受管提交入口 {program}",
            "after": [],
            "action": {"tool": "submit_job", "program": program},
            "effects": ["workspace_write", "process_tree", "external_job",
                        "scientific_execution"],
            "workdir_role": "build_root" if program == "make" else "run_root",
            "expected_outputs": [],
        })
    declared = asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "resource_manager 测试的受管提交路线",
        "evidence_refs": ["test:resource-manager"],
        "steps": steps,
    }))
    assert declared["status"] == "success"
    return state


@pytest.mark.parametrize(
    "artifact_type",
    sorted(rm._MANAGED_EXTERNAL_ARTIFACT_TYPES),
)
def test_generic_save_cannot_forge_managed_external_facts(
    tmp_path,
    artifact_type,
):
    from shared.tools.builtin import _save_artifact

    state = State.new("experiment", tmp_path)
    forged = asyncio.run(_save_artifact(
        state,
        artifact_type=artifact_type,
        name=f"forged_{artifact_type}",
        content="forged external scheduler fact",
    ))

    assert forged["status"] == "error"
    assert forged["failed_checks"] == ["managed_external_artifact_owner"]
    assert "submit_job" in forged["hint"]
    assert state.list_artifacts(artifact_type) == []


@pytest.mark.parametrize("cluster_reachable", [False, True])
def test_auto_scheduler_never_selects_kubernetes_without_volume_contract(
    monkeypatch,
    cluster_reachable,
):
    monkeypatch.setattr(
        rm,
        "_local_resources",
        lambda: {"scheduler": "local", "available": True},
    )
    monkeypatch.setattr(
        rm,
        "_detect_slurm",
        lambda: {"scheduler": "slurm", "available": False},
    )
    monkeypatch.setattr(
        rm,
        "_detect_pbs",
        lambda: {"scheduler": "pbs", "available": False},
    )
    monkeypatch.setattr(
        rm,
        "_detect_k8s",
        lambda _namespace=None: {
            "scheduler": "kubernetes",
            "available": True,
            "nodes": [{"name": "node-a"}] if cluster_reachable else [],
            "kubectl_error": None if cluster_reachable else "unreachable",
            "submission_contract_available": False,
            "submission_blocker": "kubernetes_volume_contract_required",
        },
    )
    monkeypatch.setattr(rm, "_core_platform_capabilities", lambda: {})

    profile = rm._discover()

    assert "kubernetes" in profile["available_schedulers"]
    assert profile["recommended_default"] == "local"
    kubernetes = next(
        item for item in profile["resources"]
        if item["scheduler"] == "kubernetes"
    )
    assert kubernetes["submission_contract_available"] is False


def test_recommend_resources_keeps_a_transient_plan_not_an_artifact(tmp_path):
    from nodes.experiment.tools import resource_manager as rm

    state = State.new("experiment", tmp_path)
    result = asyncio.run(rm._recommend_resources(
        state, scheduler_preference="local", mpi_ranks=1, cpus_per_rank=1,
        walltime_minutes=1,
    ))

    assert result["status"] == "success"
    assert state.hook_state["last_resource_recommendation"] == result
    assert state.list_artifacts("resource_recommendation") == []


def test_local_non_highrisk_submission_skips_confirmation_protocol(
    tmp_path, monkeypatch, trusted_sandbox,
):
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)

    def forbidden_confirmation_call(*_args, **_kwargs):
        pytest.fail("ordinary local submission must not enter confirmation protocol")

    for name in (
        "bypass_enabled", "is_confirmed", "consume_confirmation",
        "build_pause_payload",
    ):
        monkeypatch.setattr(danger, name, forbidden_confirmation_call)

    submitted: list[str] = []

    def fake_submit(*_args, **_kwargs):
        submitted.append("physical-submit")
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job", "job_id": "local-no-hitl",
            "submission_nonce": "local-no-hitl-receipt",
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit)

    result = asyncio.run(_submit_job(
        state=state,
        command="echo submit",
        scheduler="local",
        dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "success", result
    assert submitted == ["physical-submit"]
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "job_submission_confirmation_not_required" in transcript
    assert "local_managed_no_highrisk" in transcript
    assert "job_submission_blocked_pending_confirm" not in transcript


def test_submit_boundary_exception_is_unknown_and_must_not_be_resubmitted(
    tmp_path, monkeypatch, trusted_sandbox,
):
    from nodes.experiment.tools.execution_action_census import (
        reduce_execution_action_census,
    )

    state = _planned_state(tmp_path)
    calls: list[str] = []

    def uncertain_submit(*_args, **_kwargs):
        calls.append("submission-boundary-entered")
        raise OSError("connection dropped after request write")

    monkeypatch.setattr(rm, "_submit_sync", uncertain_submit)

    result = asyncio.run(_submit_job(
        state=state,
        command="echo submit",
        scheduler="local",
        dry_run=False,
        execution_params={"case": "test"},
    ))

    assert calls == ["submission-boundary-entered"]
    assert result["status"] == "submission_outcome_unknown"
    assert result["do_not_resubmit"] is True
    assert result["safe_to_retry"] is False
    assert result["blocker"]["kind"] == (
        "external_job_submission_outcome_unknown"
    )
    assert result["submission_persistence"]["status"] == "recovered"
    assert len(state.list_artifacts("external_job_submission_recovery")) == 1
    census = reduce_execution_action_census(state)
    assert census["complete"] is True
    assert len(census["actions"]) == 1
    assert census["actions"][0]["payload_spawned"] is None
    assert census["actions"][0]["job_submitted"] is None
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "interrupted"


def test_submit_census_followup_write_failure_never_reports_success(
    tmp_path, monkeypatch, trusted_sandbox,
):
    from nodes.experiment.tools.execution_action_census import (
        reduce_execution_action_census,
    )

    state = _planned_state(tmp_path)
    append_transcript = state.append_transcript

    def fail_spawn_observation(event_type, **payload):
        if event_type == "execution_action_spawn_observed":
            raise OSError("simulated spawn observation write failure")
        append_transcript(event_type, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_spawn_observation)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success",
        "scheduler": "local",
        "dry_run": False,
        "job_name": "experiment_job",
        "job_id": "local-census-write-failure",
        "container_runtime_id": "a" * 64,
        "submission_nonce": "census-write-failure-receipt",
    })

    result = asyncio.run(_submit_job(
        state=state,
        command="echo submit",
        scheduler="local",
        dry_run=False,
        execution_params={"case": "test"},
    ))

    assert result["status"] == "submitted_needs_recovery"
    assert result["execution_outcome"]["status"] == "success"
    assert result["job_id"] == "local-census-write-failure"
    assert result["do_not_resubmit"] is True
    assert result["safe_to_retry"] is False
    assert result["payload_must_not_rerun"] is True
    assert result["do_not_retry_payload"] is True
    assert result["blocker"]["kind"] == (
        "execution_action_census_persistence_failed"
    )
    assert result["blocker"]["next_action"]["action"] == (
        "retry_missing_census_phase_only"
    )
    assert result["blocker"]["next_action"]["owner"] == "experiment_runtime"
    assert result["blocker"]["next_action"]["model_callable"] is False
    assert result["model_next_action"]["action"] == (
        "report_blocker_and_end_current_run"
    )
    assert result["deferred_terminal"]["missing_phase"] == "terminal"
    artifacts = state.list_artifacts("job_submission")
    assert len(artifacts) == 1
    persisted = json.loads(state.read_artifact(artifacts[0]["id"])["content"])
    assert persisted["status"] == "success"
    assert persisted["job_id"] == "local-census-write-failure"
    assert persisted["execution_action_census"]["spawn"]["missing_phase"] == (
        "spawn_observation"
    )
    assert persisted["execution_action_census"]["terminal"]["status"] == (
        "deferred"
    )
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"
    assert result["execution_outcome"]["route_attempt"]["outcome"] == "submitted"
    census = reduce_execution_action_census(state)
    assert census["complete"] is False
    assert "missing_spawn" in census["error_codes"]
    assert "missing_terminal" in census["error_codes"]
    assert "phase_order_invalid" not in census["error_codes"]
    assert "execution_action_terminal" not in (
        state.transcript_path.read_text(encoding="utf-8")
    )


def test_submit_census_terminal_write_failure_preserves_physical_submission(
    tmp_path, monkeypatch, trusted_sandbox,
):
    from nodes.experiment.tools.execution_action_census import (
        reduce_execution_action_census,
    )

    state = _planned_state(tmp_path)
    append_transcript = state.append_transcript

    def fail_terminal(event_type, **payload):
        if event_type == "execution_action_terminal":
            raise OSError("simulated terminal write failure")
        append_transcript(event_type, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_terminal)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success",
        "scheduler": "local",
        "dry_run": False,
        "job_name": "experiment_job",
        "job_id": "local-census-terminal-failure",
        "container_runtime_id": "b" * 64,
        "submission_nonce": "census-terminal-failure-receipt",
    })

    result = asyncio.run(_submit_job(
        state=state,
        command="echo submit",
        scheduler="local",
        dry_run=False,
        execution_params={"case": "test"},
    ))

    assert result["status"] == "submitted_needs_recovery"
    assert result["execution_outcome"]["status"] == "success"
    assert result["job_id"] == "local-census-terminal-failure"
    assert result["missing_phase"] == "terminal"
    assert result["do_not_resubmit"] is True
    assert result["payload_must_not_rerun"] is True
    assert result["do_not_retry_payload"] is True
    assert result["safe_to_retry"] is False
    assert "deferred_terminal" not in result
    artifacts = state.list_artifacts("job_submission")
    assert len(artifacts) == 1
    persisted = json.loads(state.read_artifact(artifacts[0]["id"])["content"])
    assert persisted["status"] == "success"
    assert persisted["job_id"] == "local-census-terminal-failure"
    assert persisted["execution_action_census"]["spawn"]["status"] == "success"
    assert persisted["execution_action_census"]["terminal"]["missing_phase"] == (
        "terminal"
    )
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"
    assert result["execution_outcome"]["route_attempt"]["outcome"] == "submitted"
    census = reduce_execution_action_census(state)
    assert census["complete"] is False
    assert "missing_spawn" not in census["error_codes"]
    assert "missing_terminal" in census["error_codes"]
    assert "phase_order_invalid" not in census["error_codes"]


@pytest.mark.parametrize(
    ("action", "scheduler", "highrisk", "expected"),
    [
        ("submit", "local", None, None),
        ("submit", "local", "提权 (sudo)", "本地受管作业提交（含高危命令：提权 (sudo)）"),
        ("submit", "slurm", None, "真实外部作业提交"),
        ("submit", "slurm", "提权 (sudo)", "真实外部作业提交（含高危命令：提权 (sudo)）"),
        ("cancel", "local", None, "取消本地受管作业"),
        ("cancel", "slurm", None, "取消真实外部作业"),
    ],
)
def test_managed_job_confirmation_category_matrix(
    action, scheduler, highrisk, expected,
):
    assert rm._managed_job_confirmation_category(
        action=action,
        scheduler=scheduler,
        highrisk_category=highrisk,
    ) == expected


# ── #893 节点侧：本地 GPU 在确认卡之前说清 ────────────────────────────────────


@pytest.mark.parametrize("dry_run", [False, True])
def test_local_gpu_request_is_refused_before_the_confirmation_card(
    tmp_path, trusted_sandbox, dry_run,
):
    """原生后端不提供本地 GPU：同一个事实不能拖到人批准之后才由 Core 说出来。

    修复前真实提交会先返回 pause（弹确认卡），批准后才在 prepare_launch 被拒。
    """
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="echo gpu", scheduler="local", gpus=1,
        dry_run=dry_run, execution_params={"case": "test"},
    ))

    assert result["status"] == "error", result
    assert result["error_code"] == "local_gpu_execution_unsupported"
    assert result["side_effects"] == "none"
    assert any("gpus 改为 0" in step for step in result["next_actions"])
    assert any("slurm" in step for step in result["next_actions"])
    assert state.list_artifacts("job_submission") == []


def test_discovery_does_not_report_visible_gpus_as_locally_executable(monkeypatch):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as rm

    monkeypatch.setattr(rm.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(rm, "_run", lambda *_a, **_k: {
        "ok": True, "stdout": "0, A100, 40000, 39000, 0\n"})
    monkeypatch.setattr(sandbox, "availability", lambda **_k: (True, ""))
    local = rm._local_resources()
    assert len(local["gpus"]) == 1
    assert local["gpu_execution_supported"] is False
    assert local["available"] is True

    # 隔离后端守不住写边界时，本机不算可用的受管调度器——此前这里恒为 True。
    monkeypatch.setattr(sandbox, "availability", lambda **_k: (False, "no bwrap"))
    local = rm._local_resources()
    assert local["available"] is False
    assert local["availability_detail"] == "no bwrap"


def test_local_gpu_recommendation_says_local_cannot_run_gpu(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as rm

    monkeypatch.setattr(rm, "_discover", lambda _ns=None: {
        "available_schedulers": ["local"], "recommended_default": "local",
        "resources": [{"scheduler": "local", "available": True, "cpu_count": 8,
                       "memory_available_mb": 64000,
                       "gpus": [{"index": "0"}, {"index": "1"}],
                       "gpu_execution_supported": False}],
    })
    state = State.new("experiment", tmp_path)
    result = asyncio.run(rm._recommend_resources(
        state, scheduler_preference="local", mpi_ranks=1, cpus_per_rank=1,
        gpus=1, walltime_minutes=1,
    ))

    assert result["status"] == "success", result
    warnings = " ".join(result["recommendation"]["warnings"])
    assert "do not provide GPU execution" in warnings, result


def test_bypass_high_risk_submission_runs_without_environment_gate(tmp_path, monkeypatch, trusted_sandbox):
    from nodes.experiment.tools import resource_manager as rm
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success", "scheduler": "local", "dry_run": False,
        "job_name": "experiment_job", "job_id": "12345",
        "submission_nonce": "bypass-receipt",
    })
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state, command="sudo echo submit", scheduler="local", dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "success"
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "job_submission_bypassed" in events


def test_foreground_wait_starts_after_durable_submission_and_route_receipt(
    tmp_path,
    monkeypatch,
    trusted_sandbox,
):
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    submit_calls = []
    wait_calls = []

    def fake_submit(*_args, **_kwargs):
        submit_calls.append(1)
        return {
            "status": "success",
            "scheduler": "local",
            "dry_run": False,
            "job_name": "experiment_job",
            "job_id": "12345",
            "container_runtime_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "submission_nonce": "foreground-wait-receipt",
        }

    async def fake_wait(_state, scheduler, job_id, **kwargs):
        artifacts = _state.list_artifacts("job_submission")
        snapshot = execution_route.build_route_snapshot(_state)
        assert artifacts
        assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"
        wait_calls.append((scheduler, job_id, kwargs["max_wait_s"]))
        return {
            "status": "success",
            "wait_outcome": "wait_elapsed_running",
            "health": {"health_state": "running_with_output_activity"},
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit)
    monkeypatch.setattr(rm, "_wait_for_external_job", fake_wait)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state,
            command="echo submit",
            scheduler="local",
            dry_run=False,
            expected_duration_s=1,
            foreground_wait_s=1,
            execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert submit_calls == [1]
    assert wait_calls == [("local", "12345", 1)]
    assert result["status"] == "success"
    assert result["submission_status"] == "submitted"
    assert result["workflow_status"] == "awaiting_external_job"
    assert result["foreground_wait"]["wait_outcome"] == "wait_elapsed_running"


def test_second_line_rejection_after_bind_is_recoverable(
    tmp_path,
    monkeypatch,
    trusted_sandbox,
):
    """绑定后 _submit_sync 的零执行基础设施拒绝不得把 step 写成永久 failed。"""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: {
        "status": "error",
        "error": ("local sandbox path contract rejected: "
                  "path_capability_required: run_root not materialized"),
        "reason": "local_sandbox_path_contract_rejected",
        "scheduler": "local", "dry_run": False, "job_name": "experiment_job",
        "blocker": {"kind": "local_sandbox_path_contract_rejected"},
    })
    danger.set_bypass_mode(True)
    try:
        rejected = asyncio.run(rm._submit_job(
            state=state, command="echo submit", scheduler="local",
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert rejected["status"] == "error"
    assert rejected["route_attempt"]["outcome"] == "rejected"
    assert (rejected["route_attempt"]["failure_class"]
            == "infrastructure_rejection")
    # 零执行：没有任何外部作业身份或提交收据残留。
    assert state.list_artifacts("job_submission") == []
    assert state.list_artifacts("external_submission_intent") == []
    assert state.list_artifacts("external_job_workflow") == []

    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "pending"
    assert "submit_echo" in snapshot["ready_step_ids"]
    decision = execution_route.resolve_execution_context(state, {
        "tool": "submit_job", "program": "echo", "command": "echo submit",
    })
    assert decision["decision"] == "matched_ready_step"

    # 拒绝原因解除后，同一 step 可用新 attempt 真正提交成功。
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: {
        "status": "success", "scheduler": "local", "dry_run": False,
        "job_name": "experiment_job", "job_id": "hf-retry",
        "container_runtime_id": "a" * 64,
        "submission_nonce": "retry-after-rejection",
    })
    danger.set_bypass_mode(True)
    try:
        retried = asyncio.run(rm._submit_job(
            state=state, command="echo submit", scheduler="local",
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert retried["status"] == "success"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"


def test_payload_failure_with_execution_evidence_stays_terminal(
    tmp_path,
    monkeypatch,
    trusted_sandbox,
):
    """真跑过（有 returncode）的失败仍是终态 failed，不得静默回 ready。"""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: {
        "status": "error",
        "error": "scheduler rejected the job script",
        "returncode": 1,
        "scheduler": "local", "dry_run": False, "job_name": "experiment_job",
    })
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state, command="echo submit", scheduler="local",
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "error"
    assert result["route_attempt"]["outcome"] == "failed"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "failed"
    assert "submit_echo" not in snapshot["ready_step_ids"]


def test_backend_deadline_fields_are_not_interchangeable(tmp_path):
    state = _planned_state(tmp_path)

    local_walltime = asyncio.run(_submit_job(
        state=state,
        command="echo dry-run",
        scheduler="local",
        walltime_minutes=5,
        dry_run=True,
    ))
    scheduler_hard = asyncio.run(_submit_job(
        state=state,
        command="echo dry-run",
        scheduler="slurm",
        hard_deadline_s=300,
        dry_run=True,
    ))

    assert local_walltime["status"] == "success"
    assert local_walltime["sandbox_contract"]["walltime_seconds"] == 300
    assert local_walltime["time_contract"]["hard_deadline_s"] is None
    assert scheduler_hard["reason"] == "backend_deadline_contract_conflict"


def test_cancelling_foreground_wait_keeps_submitted_route_open(
    tmp_path,
    monkeypatch,
    trusted_sandbox,
):
    from core.cancellation import RunCancelled
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_a, **_k: {
        "status": "success",
        "scheduler": "local",
        "dry_run": False,
        "job_name": "experiment_job",
        "job_id": "12345",
        "container_runtime_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "submission_nonce": "cancelled-foreground-wait",
    })

    async def cancelled_wait(*_args, **_kwargs):
        raise RunCancelled("foreground_wait", {"reason": "user stop"})

    monkeypatch.setattr(rm, "_wait_for_external_job", cancelled_wait)
    danger.set_bypass_mode(True)
    try:
        with pytest.raises(RunCancelled):
            asyncio.run(rm._submit_job(
                state=state,
                command="echo submit",
                scheduler="local",
                dry_run=False,
                foreground_wait_s=1,
                execution_params={"case": "test"},
            ))
    finally:
        danger.set_bypass_mode(False)

    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["route_state"] == "in_progress"
    assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"


def test_accepted_job_is_recoverable_when_primary_submission_write_fails(
    tmp_path, monkeypatch, trusted_sandbox,
):
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success", "scheduler": "local", "dry_run": False,
        "job_name": "experiment_job", "job_id": "12345",
        "container_runtime_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "namespace": None,
        "submission_nonce": "persistence-receipt",
    })
    original_save_artifact = state.save_artifact

    def fail_primary_submission(artifact_type, *args, **kwargs):
        if artifact_type == "job_submission":
            raise OSError("primary artifact store unavailable")
        return original_save_artifact(artifact_type, *args, **kwargs)

    monkeypatch.setattr(state, "save_artifact", fail_primary_submission)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state, command="echo submit", scheduler="local",
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "submitted_needs_recovery"
    assert result["scheduler"] == "local"
    assert result["job_id"] == "12345"
    assert result["submission_persistence"]["status"] == "recovered"
    recovery = state.list_artifacts("external_job_submission_recovery")
    assert len(recovery) == 1
    assert rm._submission_payloads(state)[0]["job_id"] == "12345"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["submit_echo"]["state"] == "in_progress"
    projection = execution_route.record_external_route_finalization(
        state,
        scheduler="local",
        job_id="12345",
        namespace=None,
        submission_nonce="persistence-receipt",
        container_runtime_id="a" * 64,
        domain_outcome="operation_completed",
        evidence_artifact_id="experiment_log__recovered_job",
    )
    assert projection["status"] == "success"
    finalized = execution_route.build_route_snapshot(state)
    assert finalized["steps"]["submit_echo"]["state"] == "verified"
    assert finalized["route_state"] == "actionable"


@pytest.mark.parametrize("scheduler", ("slurm", "pbs"))
def test_remote_real_submission_fails_closed_before_identity_recovery(
        tmp_path, monkeypatch, scheduler):
    """Public remote submission never reaches a scheduler without a sandbox."""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    calls: list[tuple] = []

    def accepted_without_identity(*_args, **_kwargs):
        calls.append(_args)
        return {
            "status": "accepted_identity_unresolved", "scheduler": scheduler,
            "dry_run": False, "job_name": "experiment_job", "job_id": None,
            "namespace": None, "script_path": "/tmp/job.sh",
            "script_sha256": "a" * 64, "submission_nonce": "nonce-123",
            "submitted_at": "2026-08-26T00:00:00+00:00",
            "submit_result": {"ok": True, "stdout": "accepted but malformed"},
            "do_not_resubmit": True,
        }

    monkeypatch.setattr(rm, "_submit_sync", accepted_without_identity)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state, command="echo submit", scheduler=scheduler,
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert calls == []
    assert result["status"] == "error"
    assert result["blocker"] == {
        "kind": "remote_sandbox_contract_unavailable",
        "scheduler": scheduler,
    }
    assert state.list_artifacts("job_submission") == []
    assert state.list_artifacts("external_job_submission_recovery") == []
    assert state.list_artifacts("external_job_identity_recovery_workflow") == []


def test_scheduler_success_without_parseable_identity_is_not_success(tmp_path, monkeypatch):
    """Malformed scheduler stdout must not be presented as a safe resubmit error."""
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(rm, "_run", lambda *_args, **_kwargs: {
        "ok": True, "returncode": 0, "stdout": "not-a-slurm-id", "stderr": "",
    })

    result = rm._submit_sync(
        tmp_path, "slurm", "echo submit", "unresolved-id", 1, 1, 0,
        1.0, 8.0, 1, None, None, None, str(tmp_path), False, None,
        stage_in=None,
        state=state,
    )

    assert result["status"] == "accepted_identity_unresolved"
    assert result["job_id"] is None
    assert result["do_not_resubmit"] is True
    assert result["submit_result"]["stdout"] == "not-a-slurm-id"
    assert result["submit_command"][:2] == ["sbatch", "--parsable"]
    assert result["script_path"]
    assert result["script_sha256"]
    assert result["submission_nonce"]


@pytest.mark.parametrize(("scheduler", "stdout"), [
    ("slurm", "12345;cluster-a\n"),
    ("pbs", "12345.server\n"),
    ("kubernetes", json.dumps({
        "kind": "Job", "metadata": {"name": "job-a", "namespace": "ns", "uid": "uid-a"},
    })),
])
def test_scheduler_identity_parsers_accept_only_machine_formats(scheduler, stdout):
    identity = rm._parse_submission_identity(scheduler, stdout)

    assert identity is not None
    assert identity["job_id"]


@pytest.mark.parametrize(("scheduler", "stdout"), [
    ("slurm", "Submitted batch job 12345"),
    ("pbs", ""),
    ("kubernetes", "job.batch/job-a created"),
])
def test_scheduler_identity_parsers_reject_human_or_unknown_formats(scheduler, stdout):
    assert rm._parse_submission_identity(scheduler, stdout) is None


def test_submit_job_reports_missing_bash_analyzer_to_framework_owner(tmp_path, monkeypatch):
    state = _planned_state(tmp_path)
    monkeypatch.setattr(te, "bash_analyzer_unavailable_reason",
                        lambda: "ModuleNotFoundError: tree_sitter_bash")

    result = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local", dry_run=True))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "bash_semantic_analyzer_unavailable"
    assert result["blocker"]["suggested_owner"] == "framework"
    assert "作业未提交" in result["error"]


def test_submit_job_reports_dynamic_payload_without_calling_it_background(tmp_path):
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="$runner --case test", scheduler="local", dry_run=True))

    assert result["status"] == "error"
    assert result["blocker"] == {
        "kind": "unverifiable_job_payload",
        "uncertainty_kind": "dynamic_execution",
    }
    assert "这不表示检测到了后台任务" in result["error"]



def test_primary_simulation_stage_declaration_is_overridden_by_contract(tmp_path, monkeypatch, trusted_sandbox):
    """判决拆除 O4（rm:1931 降格，2026-08-31）。

    primary simulation 把求解 job 声明成 toolchain_build 时，框架按权威源
    （contract）取值：stage 被机械覆盖为 simulation 并披露，不再拒绝提交。
    """
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success", "scheduler": "local", "dry_run": False,
        "job_name": "experiment_job", "job_id": "hf-stub",
        "submission_nonce": "stage-override-stub",
        "container_runtime_id": "a" * 64,
    })
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state, command="python solver.py", scheduler="local", dry_run=False,
            stage="toolchain_build", execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "success", result
    assert result["stage"] == "simulation"
    witness = result.get("submission_witness") or {}
    assert witness.get("stage_declaration_overridden") == {
        "declared_stage": "toolchain_build", "authoritative_stage": "simulation"}
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "stage_declaration_overridden_by_contract" in events


def test_bound_prereg_operation_build_stays_toolchain_build(
    tmp_path, monkeypatch, trusted_sandbox,
):
    """Scientific identity must not turn a mechanically declared build into simulation."""
    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "build_contract",
        "# frozen build preregistration",
        metadata={"run_role": "primary", "execution_mode": "operational"},
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "requested_work": "Build the selected source tree with make.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Compile a declared build target without running scientific measurement.",
    ))
    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"

    declared = asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "Compile the selected source tree",
        "evidence_refs": ["test:bound-prereg-build-class"],
        "steps": [{
            "id": "build",
            "goal": "Run make in the canonical build root",
            "after": [],
            "action": {"tool": "submit_job", "program": "make"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "build_root",
            "expected_outputs": [],
        }],
    }))
    assert declared["status"] == "success", declared

    build_root = rm.experiment_output_dir(state, "build", create=True)
    output_dir = build_root / "logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success",
        "scheduler": "local",
        "dry_run": False,
        "job_name": "bound-prereg-build",
        "job_id": "hf-bound-prereg-build",
        "submission_nonce": "bound-prereg-build-nonce",
        "container_runtime_id": "a" * 64,
    })
    resolved_actions = []
    resolve_submission_route = rm._resolve_submission_route

    def capture_submission_route(state_arg, action):
        resolved_actions.append(dict(action))
        return resolve_submission_route(state_arg, action)

    monkeypatch.setattr(rm, "_resolve_submission_route", capture_submission_route)

    result = asyncio.run(_submit_job(
        state=state,
        command="make -j2",
        scheduler="local",
        dry_run=False,
        workdir=str(build_root),
        output_dir=str(output_dir),
        output_paths=[str(build_root)],
        route_step_id="build",
    ))

    assert result["status"] == "success", result
    assert resolved_actions
    assert resolved_actions[-1]["mechanical_major_build"] is True
    assert result["execution_class"] == "toolchain_build"
    assert result["stage"] == "toolchain_build"
    events = [
        json.loads(line).get("event")
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "experiment_preflight" not in events


def test_confirmed_high_risk_submission_consumes_one_shot_approval(tmp_path, monkeypatch, trusted_sandbox):
    from nodes.experiment.tools import resource_manager as rm
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    consumed: list[str] = []
    monkeypatch.setattr(danger, "is_confirmed", lambda _state, _text: True)
    monkeypatch.setattr(
        danger, "consume_confirmation",
        lambda _state, text: consumed.append(text),
    )
    monkeypatch.setattr(rm, "_submit_sync", lambda *_args, **_kwargs: {
        "status": "success", "scheduler": "local", "dry_run": False,
        "job_name": "experiment_job", "job_id": "12345",
        "submission_nonce": "confirmed-receipt",
    })

    result = asyncio.run(rm._submit_job(
        state=state, command="sudo echo submit", scheduler="local", dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "success"
    assert len(consumed) == 1
    approved = json.loads(consumed[0])
    assert approved["memory_gb"] == 4.0
    assert approved["storage_gb"] == 8.0
    # 省略 walltime 时审批单如实写 None：本地沙箱的 60 分钟兜底是
    # platform_safety_default，不能在审批快照里伪装成调用方要求的值。
    assert approved["walltime_minutes"] is None
    assert approved["sandbox"]["image_id"] == "sha256:test-sandbox"
    assert "job_submission_confirmed" in state.transcript_path.read_text(encoding="utf-8")



def test_dry_run_does_not_require_external_submission_approval(tmp_path):
    state = _planned_state(tmp_path)

    result = asyncio.run(
        _submit_job(
            state=state,
            command="echo dry-run",
            scheduler="local",
            dry_run=True,
        )
    )

    assert result["status"] == "success"
    assert result["dry_run"] is True


def test_operation_submit_defaults_omitted_stage_to_diagnostic(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = _fixture_node_inputs(
        "验证 operation 调度探针的默认诊断阶段。"
    )
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="scheduler_probe",
        reason="A scheduler probe must not be treated as a scientific simulation.",
    ))

    result = asyncio.run(_submit_job(
        state=state, command="echo operation-dry-run", scheduler="local",
        dry_run=True,
    ))

    assert result["status"] == "success"
    assert result["stage"] == "diagnostic"


def test_operation_submit_rejects_explicit_operation_as_stage(tmp_path):
    """scope 不是 stage：stage=operation 必须被拒，且合法取值随拒绝一起给出。

    合并说明（本分支 vs origin/main）：上游把 stage 词表放进 submit_job schema，
    由派发口核，断言 parameter_violations。本分支刻意**不向 LLM 暴露 stage**
    （它只是旧 caller 的兼容输入，不参与授权/目录/科学身份 —— 见
    test_route_shadow_wiring.test_stage_is_runtime_compatibility_only_and_not_llm_visible），
    schema 里没有它，派发口自然核不到。两边都要求「必须拒」，只是这道 C 类
    契约检查落在工具体内；词表仍与 preflight.EXECUTION_STAGES 同源，且按 BF-12
    把合法取值逐字送到调用方。"""
    from core.tool_registry import execute
    from nodes.experiment.tools.preflight import EXECUTION_STAGES

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = _fixture_node_inputs(
        "验证 operation 不可作为执行 stage。"
    )
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="scheduler_probe",
        reason="Scope is not a valid execution stage.",
    ))

    result = asyncio.run(execute(
        "submit_job", state, command="echo should-not-submit", scheduler="local",
        dry_run=True, stage="operation",
    ))

    assert result["status"] == "error"
    assert result["allowed_stages"] == list(EXECUTION_STAGES)
    assert "operation" not in result["allowed_stages"]
    for stage in EXECUTION_STAGES:
        assert stage in result["error"]
    assert "toolchain_build" in result["error"]
    # 同一份词表：拒绝里报出的合法取值与 preflight 的别名表同源。
    # （上游那版比的是 submit_job schema 的 enum；本分支 stage 不进 schema，
    #  所以比的是同一份词表送到调用方的那条通道。）
    from core.tool_registry import get_tool
    from nodes.experiment.tools.preflight import _STAGE_ALIASES
    assert "stage" not in get_tool("submit_job").parameters_schema["properties"]
    assert set(EXECUTION_STAGES) <= set(_STAGE_ALIASES)


def test_local_dry_run_declares_the_container_contract(tmp_path):
    state = _planned_state(tmp_path)
    baseline = tmp_path / "baseline"
    # local 作业跑在提交机上，其 workdir 必须落在本机写边界内（见
    # _local_job_boundary_block）；共享盘上的运行目录要走 slurm/pbs。
    run = Path(state.root) / "run"
    baseline.mkdir()
    run.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": str(baseline),
        "run_root": str(run),
    }

    result = asyncio.run(_submit_job(
        state=state,
        command="./solver",
        scheduler="local",
        workdir=str(run),
        dry_run=True,
    ))

    assert result["status"] == "success"
    # adapter 是过渡期 wire alias；adapter_kind 才是这次实际用的启动方式。
    assert result["sandbox_contract"]["adapter"] == "docker"
    assert result["sandbox_contract"]["adapter_kind"] == "native_local_job"
    assert result["sandbox_contract"]["network"] == "none"
    assert "./solver" in result["script_preview"]


def test_local_job_mount_projection_keeps_unused_roles_out_of_rw(tmp_path):
    state = _planned_state(tmp_path)
    state_root = Path(state.root).resolve()
    run_root = rm.experiment_output_dir(
        state, "runtime", create=True,
    ).resolve()
    unused_build_root = rm.experiment_output_dir(
        state, "build", create=True,
    ).resolve()

    writable, readonly = rm._local_job_sandbox_roots(
        state,
        "printf result > result.txt",
        run_root,
        output_roots=[str(run_root / "results")],
        scheduler_output_dir=run_root / "logs",
        bootstrap_log_dir=run_root / "jobs" / "minimal-mount",
    )

    assert run_root in writable
    assert unused_build_root not in writable
    assert state_root not in writable
    assert any(rm._mount_contains(root, state_root) for root in readonly)


def test_local_job_mount_projection_requires_core_or_human_capability(
    tmp_path,
):
    from nodes.experiment.tools import subprocess_policy as policy

    state = _planned_state(tmp_path / "state")
    state_root = Path(state.root).resolve()
    runtime_root = rm.experiment_output_dir(
        state, "runtime", create=True,
    ).resolve()
    external_run = (tmp_path / "external-run").resolve()
    external_run.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external_run), "writable": True},
    }

    with pytest.raises(
        policy.BashSandboxContractError, match="path_capability_required",
    ):
        rm._local_job_sandbox_roots(
            state,
            "printf result > result.txt",
            external_run,
            output_roots=[str(external_run)],
            scheduler_output_dir=runtime_root / "logs",
            bootstrap_log_dir=runtime_root / "jobs" / "human-capability",
        )

    policy.register_approved_subprocess_write_root(state, str(external_run))
    writable, readonly = rm._local_job_sandbox_roots(
        state,
        "printf result > result.txt",
        external_run,
        output_roots=[str(external_run)],
        scheduler_output_dir=runtime_root / "logs",
        bootstrap_log_dir=runtime_root / "jobs" / "human-capability",
    )

    assert external_run in writable
    assert state_root not in writable
    assert any(rm._mount_contains(root, state_root) for root in readonly)
    assert rm._local_job_boundary_block(
        state, "local", str(external_run), str(external_run / "logs")
    ) is None


def test_local_sandbox_preflight_materializes_canonical_build_root(tmp_path):
    state = State.new("experiment", tmp_path)
    runtime_root = rm.experiment_output_dir(state, "runtime", create=True)
    build_root = rm.experiment_output_dir(state, "build", create=False)
    target = build_root / "marker.txt"

    assert not build_root.exists()
    block, sandbox = rm._preflight_local_submission_sandbox(
        state,
        f"printf ready > {shlex.quote(str(target))}",
        workdir=str(runtime_root),
        output_roots=[str(build_root)],
        runtime_root=runtime_root,
        output_dir=str(runtime_root / "logs"),
        job_name="canonical-build-root",
    )

    assert block is None
    # 沙箱投影同时是 payload 预检的判定范围（E-14）：作业 cwd + 解析出的挂载根。
    assert sandbox.workdir == runtime_root.resolve()
    assert sandbox.mount_roots
    assert build_root.is_dir()
    assert state.list_artifacts("external_submission_intent") == []
    assert not list(Path(state.root).glob("**/*.sh"))


def test_local_sandbox_preflight_rejects_missing_caller_root_before_route_binding(
    tmp_path,
    trusted_sandbox,
):
    from shared.lib import dangerous_commands as danger

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = _fixture_node_inputs(
        "验证本地沙箱 mount 预检必须先于路线绑定失败。"
    )
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Exercise the local sandbox mount preflight before route binding.",
    ))
    assert classified["status"] == "success"
    declared = asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "Reject an unmaterialized caller-owned local output root",
        "evidence_refs": ["test:local-sandbox-preflight"],
        "steps": [{
            "id": "submit_printf",
            "goal": "Exercise the local sandbox preflight path",
            "after": [],
            "action": {"tool": "submit_job", "program": "printf"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }))
    assert declared["status"] == "success"

    runtime_root = rm.experiment_output_dir(state, "runtime", create=True)
    caller_build_root = Path(state.root) / "caller-owned-build-root"
    target = caller_build_root / "marker.txt"
    state.hook_state["path_roles"] = {
        "build_root": {"path": str(caller_build_root), "writable": True},
    }

    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state,
            command=f"printf ready > {shlex.quote(str(target))}",
            scheduler="local",
            workdir=str(runtime_root),
            output_dir=str(runtime_root / "logs"),
            dry_run=False,
            route_step_id="submit_printf",
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "error"
    assert result["reason"] == "local_sandbox_path_contract_rejected"
    assert "尚未物化" in result["error"]
    assert not caller_build_root.exists()
    assert state.list_artifacts("external_submission_intent") == []
    assert not list(Path(state.root).glob("**/*.sh"))
    events = [
        json.loads(line)["event"]
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "route_step_bound" not in events
    assert "route_step_outcome" not in events
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["route_state"] == "actionable"
    assert snapshot["ready_step_ids"] == ["submit_printf"]


def test_local_script_turns_pids_event_delta_into_nonzero_terminal(
    tmp_path,
    monkeypatch,
):
    events = tmp_path / "pids.events"
    events.write_text("max 0\n", encoding="ascii")
    monkeypatch.setattr(rm, "_LOCAL_PIDS_EVENTS_PATH", str(events))
    # 这条验的是 **cgroup 后端**上的 pids 证据，所以把那个前提写明。
    #
    # 原来它只写 `scheduler="local"` —— 隐含了"本地作业一定跑在 cgroup 里"，
    # 而那正是 2026-09-06 真机实测拆掉的假设：原生后端（seatbelt / Landlock）
    # 下没有 cgroup 可采样，无条件注入会让每个本地作业在 payload 之前 exit 127。
    import core.isolation as isolation

    monkeypatch.setattr(
        isolation, "enforcement_snapshot",
        lambda: {"backend": "cgroup", "enforced": ["pids_cap"]},
    )
    command = f"printf 'max 1\\n' > {shlex.quote(str(events))}; exit 0"
    script = rm._script_for(
        "local", command, "pids-delta", 1, 1, 0, 1.0, 1,
        None, None, None, str(tmp_path),
    )
    script_path = tmp_path / "pids-delta.sh"
    script_path.write_text(script, encoding="utf-8")

    completed = rm._run(["/bin/bash", str(script_path)], timeout=10)

    assert completed["returncode"] == rm._LOCAL_PIDS_LIMIT_EXIT_CODE
    assert "HARNESS_SANDBOX_LIMIT pids baseline=0 final=1" in completed["stderr"]
    assert script.index("HARNESS_PIDS_MAX_BEFORE") < script.index(command)
    assert script.index(command) < script.index("HARNESS_PIDS_MAX_AFTER")


def test_remote_dry_run_is_planning_only(tmp_path):
    state = _planned_state(tmp_path)
    baseline = tmp_path / "baseline"
    run = tmp_path / "run"
    baseline.mkdir()
    run.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": str(baseline),
        "run_root": str(run),
    }

    result = asyncio.run(_submit_job(
        state=state,
        command="srun ./solver",
        scheduler="slurm",
        workdir=str(run),
        dry_run=True,
    ))

    assert result["status"] == "success"
    assert "sandbox_contract" not in result


@pytest.mark.production_sandbox
@requires_sandbox
def test_bypass_really_runs_local_job_and_persists_submission(tmp_path):
    """Integration check: no mocks; submit a short local job into a temp run root."""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    baseline = tmp_path / "baseline"
    run_root = rm.experiment_output_dir(state, "runtime", create=True)
    baseline.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": str(baseline),
        "run_root": {"path": str(run_root), "writable": True},
    }

    result = None
    try:
        danger.set_bypass_mode(True)
        try:
            result = asyncio.run(_submit_job(
                state=state,
                command="printf actual-job > local-job-marker.txt",
                scheduler="local",
                job_name="actual_local_job",
                workdir=str(run_root),
                dry_run=False, execution_params={"case": "test"},
            ))
        finally:
            danger.set_bypass_mode(False)

        assert result["status"] == "success", result
        from core.sandbox import sandbox_namespace

        assert re.fullmatch(
            rf"hf-{re.escape(sandbox_namespace())}-[0-9a-f]{{20}}",
            result["job_id"],
        )
        marker = run_root / "local-job-marker.txt"
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.read_text(encoding="utf-8") == "actual-job"

        artifacts = state.list_artifacts("job_submission")
        assert len(artifacts) == 1
        submitted = state.read_artifact(artifacts[0]["id"])
        payload = json.loads(submitted["content"])
        assert payload["dry_run"] is False
        assert payload["job_id"] == result["job_id"]
    finally:
        danger.set_bypass_mode(False)
        if result and result.get("status") == "success":
            from nodes.experiment.tools.resource_manager import _cancel_sync

            cancelled = _cancel_sync(
                "local", result["job_id"], None,
                container_runtime_id=result["container_runtime_id"],
            )
            assert cancelled.get("ok"), (
                "exact local cancellation failed; preserving sandbox control dir: "
                + str(cancelled)
            )
            from core.sandbox import cleanup_control_dir

            cleanup_control_dir(result["sandbox_control_dir"])


@pytest.mark.production_sandbox
@requires_sandbox
def test_local_docker_pids_event_delta_overrides_swallowed_success(tmp_path):
    """A payload cannot turn a Docker pids.max denial back into exit zero."""
    state = _planned_state(tmp_path)
    runtime_root = rm.experiment_output_dir(
        state, "runtime", create=True,
    ).resolve()
    command = (
        "set +e; "
        "for HARNESS_I in {1..256}; do sleep 2 & done; "
        "wait || true; exit 0"
    )
    result = None
    try:
        result = rm._submit_sync(
            runtime_root,
            "local",
            command,
            "pids_delta",
            1,
            1,
            0,
            0.5,
            1.0,
            1,
            None,
            None,
            None,
            str(runtime_root),
            False,
            None,
            output_dir=str(runtime_root / "logs"),
            output_paths=[str(runtime_root)],
            execution_class="diagnostic",
            expected_duration_s=5,
            highrisk_authorized=True,
            stage_in=None,
            state=state,
            hard_deadline_s=20,
        )
        assert result["status"] == "success", result
        assert re.fullmatch(r"[0-9a-f]{64}", result["container_runtime_id"])

        status = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = rm._job_status_sync(
                "local",
                result["job_id"],
                None,
                container_runtime_id=result["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.05)

        assert status is not None
        assert status["status"] == "success", status
        sandbox_state = status["raw"]["sandbox_state"]
        assert sandbox_state["id"] == result["container_runtime_id"]
        assert sandbox_state["exit_code"] == rm._LOCAL_PIDS_LIMIT_EXIT_CODE
        stderr = Path(result["stderr_path"]).read_text(encoding="utf-8")
        assert "HARNESS_SANDBOX_LIMIT pids" in stderr
    finally:
        if result and result.get("status") == "success":
            cancelled = rm._cancel_sync(
                "local",
                result["job_id"],
                None,
                container_runtime_id=result["container_runtime_id"],
            )
            assert cancelled.get("ok"), (
                "exact local cancellation failed; preserving sandbox control dir: "
                + str(cancelled)
            )
            from core.sandbox import cleanup_control_dir

            cleanup_control_dir(result["sandbox_control_dir"])


def test_submission_dry_run_rejects_undeclared_shell_write_root(tmp_path):
    """dry-run 也必须验证 payload 中可静态证明的实际写目录。"""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    run_root = tmp_path / "run"
    outside = Path("/var/lib/harness-test-undeclared-job")
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state,
            command=f"cd {outside} && make -j 2",
            scheduler="local",
            workdir=None,
            dry_run=True,
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["status"] == "error"
    assert result["blocker"]["scope"] == "unknown_absolute"
    assert not (outside / "marker.txt").exists()


def test_submission_accepts_command_body_inside_declared_run_root(tmp_path):
    state = _planned_state(tmp_path)
    # 同上：local 的 workdir 受本机写边界约束，故把声明的 run_root 放在 run 目录内。
    run_root = Path(state.root) / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    result = asyncio.run(_submit_job(
        state=state,
        command="printf allowed > marker.txt",
        scheduler="local",
        workdir=str(run_root),
        dry_run=True,
    ))

    assert result["status"] == "success"


def test_submission_dry_run_rejects_build_root_hidden_in_shell_text(tmp_path):
    state = _planned_state(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    result = asyncio.run(_submit_job(
        state=state, command="cd /var/lib/harness-test-undeclared-build && make -j 2",
        scheduler="local", dry_run=True))

    assert result["status"] == "error"
    assert result["blocker"]["scope"] == "unknown_absolute"


@pytest.mark.parametrize("scheduler", ["local", "slurm", "pbs"])
def test_submit_payload_cannot_build_in_source_baseline_before_script(
    tmp_path, monkeypatch, scheduler,
) -> None:
    state = _planned_state(tmp_path)
    build_root = Path(state.root) / "build"
    run_root = Path(state.root) / "run"
    baseline = tmp_path / "source-baseline"
    for path in (build_root, run_root, baseline):
        path.mkdir(parents=True, exist_ok=True)
    state.hook_state["path_roles"] = {
        "build_root": {"path": str(build_root), "writable": True},
        "run_root": {"path": str(run_root), "writable": True},
        "source_baseline_root": {
            "path": str(baseline), "writable": False,
        },
    }
    submitted = False

    def forbidden(*_args, **_kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("路径门必须早于脚本生成和提交")

    monkeypatch.setattr(rm, "_submit_sync", forbidden)
    for command in (
        f"cd {baseline} && make -j 2",
        f"make -C {baseline} -j 2",
    ):
        result = asyncio.run(_submit_job(
            state=state, command=command, scheduler=scheduler,
            workdir=str(build_root), output_dir=str(run_root / "logs"),
            dry_run=True, route_step_id="submit_make",
        ))
        assert result["status"] == "error"
        assert result["blocker"]["scope"] == "protected_source_tree"
    assert submitted is False
    assert state.list_artifacts("external_submission_intent") == []
    assert list(Path(state.root).glob("**/*.sh")) == []


def test_submit_framework_boundary_stops_before_runtime_materialization(
    tmp_path, monkeypatch,
) -> None:
    state = _planned_state(tmp_path)
    runtime_root = rm.experiment_output_dir(state, "runtime", create=False)
    target = Path(state.root) / "artifacts" / "poison.json"
    submitted = False

    def forbidden(*_args, **_kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("静态边界门必须早于提交")

    monkeypatch.setattr(rm, "_submit_sync", forbidden)
    result = asyncio.run(_submit_job(
        state=state, command=f"echo x > {target}",
        scheduler="slurm", dry_run=True,
    ))

    assert result["reason"] == "framework_boundary_violation"
    assert submitted is False
    assert runtime_root.exists() is False
    assert target.exists() is False
    assert state.list_artifacts("external_submission_intent") == []


def test_submit_remote_scheduler_scratch_remains_available(tmp_path) -> None:
    state = _planned_state(tmp_path)
    run_root = Path(state.root) / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    result = asyncio.run(_submit_job(
        state=state, command="echo x > $TMPDIR/hf-scratch.log",
        scheduler="slurm", workdir=str(run_root),
        output_dir=str(run_root / "logs"), dry_run=True,
        route_step_id="submit_echo",
    ))

    assert result["status"] == "success", result
    assert Path(result["script_path"]).exists()


def test_container_evidence_records_immutable_digest(monkeypatch):
    from nodes.experiment.tools import resource_manager as rm

    def probe(args, timeout=10):
        if "--format" in args:
            return {"ok": True, "stdout": "example@sha256:abc123", "stderr": "", "returncode": 0}
        return {"ok": True, "stdout": "ok", "stderr": "", "returncode": 0}

    monkeypatch.setattr(rm, "_run", probe)
    evidence = rm._container_execution_evidence("docker run --rm example:latest python solver.py")
    assert evidence["image_digest"] == "example@sha256:abc123"
    assert evidence["image_digest_resolved"] is True


def test_discover_resources_carries_core_host_declarations(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as rm
    from core import capabilities as core_capabilities

    host_file = tmp_path / "host-capabilities.yaml"
    host_file.write_text("software:\n  - name: Registered Solver\n    invoke: python\n    version: 3.x\n", encoding="utf-8")
    grants_file = tmp_path / "grants.yaml"
    grants_file.write_text("default:\n  - kind: lab_cluster\n    endpoint: cluster.example\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_HOST_CAPABILITIES", str(host_file))
    monkeypatch.setenv("HARNESS_GRANTS_FILE", str(grants_file))
    monkeypatch.setattr(core_capabilities, "_cache", None)

    profile = asyncio.run(rm._discover_resources(_planned_state(tmp_path), save_artifact=False))
    declared = profile["core_platform_capabilities"]
    assert declared["host_software"] == [{
        "name": "Registered Solver", "invoke": "python", "version": "3.x",
        "notes": "", "resolvable": True,
    }]
    assert declared["compute_grants"][0]["kind"] == "lab_cluster"
    assert "authoritative" in declared["authority"]


@pytest.mark.production_sandbox
@requires_sandbox
def test_second_run_cannot_overlap_a_live_prior_local_job(tmp_path):
    """Recovery may inspect a durable job, but a new Experiment run cannot submit over it."""
    from nodes.experiment.tools import resource_manager as rm
    from shared.lib import dangerous_commands as danger

    run_root = tmp_path / "run"
    run_root.mkdir()
    first = _planned_state(tmp_path / "first")
    first.project_root = tmp_path / "project"
    first.hook_state["path_roles"] = {"run_root": str(run_root)}
    from nodes.experiment.tools.subprocess_policy import (
        register_approved_subprocess_write_root,
    )

    register_approved_subprocess_write_root(first, str(run_root))
    submitted = None
    try:
        danger.set_bypass_mode(True)
        try:
            submitted = asyncio.run(rm._submit_job(
                first, command="sleep 30", scheduler="local", job_name="prior",
                workdir=str(run_root), output_paths=[str(run_root)], dry_run=False,
                execution_params={"case": "test"},
            ))
        finally:
            danger.set_bypass_mode(False)
        assert submitted["status"] == "success"

        second = _planned_state(tmp_path / "second")
        second.project_root = first.project_root
        second.hook_state["path_roles"] = {"run_root": str(run_root)}
        register_approved_subprocess_write_root(second, str(run_root))
        # Simulate cross-run durable artifact visibility without allowing the second
        # run to mutate the first run record.
        original_list, original_read = second.list_artifacts, second.read_artifact

        def list_artifacts(kind=None, own_only=False):
            owned = original_list(kind, own_only=own_only)
            if kind == "job_submission" and not own_only:
                return owned + [
                    {"id": "prior_submission", "type": "job_submission"}
                ]
            return owned

        def read_artifact(artifact_id):
            if artifact_id == "prior_submission":
                return {"content": json.dumps(submitted)}
            return original_read(artifact_id)

        second.list_artifacts, second.read_artifact = list_artifacts, read_artifact
        danger.set_bypass_mode(True)
        try:
            blocked = asyncio.run(rm._submit_job(
                second, command="sleep 1", scheduler="local",
                workdir=str(run_root), output_paths=[str(run_root)], dry_run=False,
                execution_params={"case": "test"},
            ))
        finally:
            danger.set_bypass_mode(False)
        assert blocked["status"] == "error"
        assert blocked["blocker"]["kind"] == "active_external_job_output_conflict"
        assert blocked["blocker"]["conflicts"][0]["job_id"] == submitted["job_id"]
    finally:
        danger.set_bypass_mode(False)
        if submitted and submitted.get("status") == "success":
            cancelled = rm._cancel_sync(
                "local", submitted["job_id"], None,
                container_runtime_id=submitted["container_runtime_id"],
            )
            assert cancelled.get("ok"), (
                "exact local cancellation failed; preserving sandbox control dir: "
                + str(cancelled)
            )
            from core.sandbox import cleanup_control_dir

            cleanup_control_dir(submitted["sandbox_control_dir"])


def test_build_resource_preflight_requires_explicit_safety_budget(tmp_path):
    import inspect

    from core.tool_registry import _REGISTRY, execute

    required = {
        "compile_mode", "mpi_ranks", "cpus_per_rank", "gpus",
        "memory_gb", "walltime_minutes", "runtime_resource_policy",
    }
    definition = _REGISTRY.tools["preflight_build_resources"]
    assert set(definition.parameters_schema["required"]) == required
    for field in required:
        assert "default" not in (
            definition.parameters_schema["properties"][field]
        )

    signature = inspect.signature(
        _REGISTRY.executors["preflight_build_resources"])
    assert all(
        signature.parameters[field].default is inspect.Parameter.empty
        for field in required
    )

    state = State.new("experiment", tmp_path)
    result = asyncio.run(execute(
        "preflight_build_resources",
        state,
        compile_mode="serial",
    ))

    assert result["error_code"] == "missing_parameters"
    assert set(result["missing_parameters"]) == required - {"compile_mode"}
    assert "build_resource_preflight" not in state.hook_state
    assert state.list_artifacts("build_resource_plan") == []


def test_submit_memory_contract_preserves_explicit_plan_and_auto_sources(
    tmp_path,
) -> None:
    state = State.new("experiment", tmp_path)

    explicit = rm._submission_memory_contract(
        state, memory_gb=3.5, total_cpus=1)
    automatic = rm._submission_memory_contract(
        state, memory_gb=None, total_cpus=1)
    state.hook_state["build_resource_preflight"] = {
        "status": "success",
        "runtime_resource_policy": "flexible",
        "artifact_id": "build_resource_plan__memory",
        "requested_resources": {"memory_gb": 8},
    }
    ordinary_run = rm._submission_memory_contract(
        state, memory_gb=None, total_cpus=1)
    planned_build = rm._submission_memory_contract(
        state, memory_gb=None, total_cpus=1, use_build_resource_plan=True)

    assert explicit == {
        "memory_gb": 3.5,
        "source": "explicit_tool_argument",
        "mode": "fixed",
        "resource_plan_artifact_id": None,
    }
    assert automatic == ordinary_run == {
        "memory_gb": 2.0,
        "source": "automatic_recommendation",
        "mode": "flexible",
        "resource_plan_artifact_id": None,
    }
    assert planned_build == {
        "memory_gb": 8.0,
        "source": "build_resource_plan",
        "mode": "flexible",
        "resource_plan_artifact_id": "build_resource_plan__memory",
    }


def test_submit_job_schema_exposes_main_resource_defaults() -> None:
    import inspect

    from core.tool_registry import _REGISTRY

    definition = _REGISTRY.tools["submit_job"]
    properties = definition.parameters_schema["properties"]
    signature = inspect.signature(_REGISTRY.executors["submit_job"])

    assert properties["memory_gb"]["default"] == 4.0
    assert properties["storage_gb"]["default"] == 8.0
    # walltime 刻意没有默认值：schema 描述承诺"省略→不落调度器指令、记
    # site_default_unknown"，任何一层的 default 都会把省略偷换成显式契约。
    assert "default" not in properties["walltime_minutes"]
    assert signature.parameters["memory_gb"].default == 4.0
    assert signature.parameters["storage_gb"].default == 8.0
    assert signature.parameters["walltime_minutes"].default is None


@pytest.mark.parametrize(
    ("scheduler", "queue", "nodelist"),
    [
        ("slurm", "normal --mem=0", None),
        ("slurm", "normal\t--exclusive", None),
        ("slurm", None, "node01 --exclusive"),
        ("pbs", "workq -l mem=0gb", None),
        ("pbs", "workq\t-l\tmem=0gb", None),
    ],
)
def test_submit_rejects_scheduler_directive_injection_before_materialization(
    tmp_path,
    scheduler,
    queue,
    nodelist,
):
    state = State.new("experiment", tmp_path)
    runtime_root = state.root / "outputs" / "experiment" / "runtime"

    result = asyncio.run(rm._submit_job(
        state,
        command="echo must-not-render",
        scheduler=scheduler,
        queue=queue,
        nodelist=nodelist,
    ))

    assert result["status"] == "error"
    assert result["reason"] == "invalid_scheduler_directive"
    assert result["blocker"]["kind"] == "invalid_scheduler_directive"
    assert runtime_root.exists() is False
    assert state.list_artifacts("external_submission_intent") == []


@pytest.mark.parametrize(
    ("scheduler", "field", "value"),
    [
        ("slurm", "queue", "debug"),
        ("slurm", "queue", "gpu,long"),
        ("pbs", "queue", "workq@server"),
        ("slurm", "nodelist", "node[01-04,08]"),
    ],
)
def test_scheduler_directive_accepts_valid_field_grammar(
    scheduler,
    field,
    value,
):
    assert rm._safe_scheduler_directive(value, field, scheduler) == value


@pytest.mark.parametrize("memory_gb", [0.1, 0.5, 0.999, 1.0, 1.25])
@pytest.mark.parametrize("scheduler", ["slurm", "pbs", "kubernetes"])
def test_scheduler_script_rounds_memory_up_without_zero_capacity(
    memory_gb,
    scheduler,
):
    memory_mb = rm._scheduler_memory_mebibytes(memory_gb)
    script = rm._script_for(
        scheduler,
        "echo ok",
        "memory-test",
        1,
        1,
        0,
        memory_gb,
        5,
        None,
        None,
        None,
        None,
        submission_nonce="memory-test",
    )

    assert memory_mb >= memory_gb * 1024
    if scheduler == "slurm":
        assert f"#SBATCH --mem={memory_mb}M" in script
        assert "--mem=0G" not in script
    elif scheduler == "pbs":
        assert f":mem={memory_mb}mb" in script
        assert ":mem=0gb" not in script
    else:
        assert f'memory: "{memory_mb}Mi"' in script
        assert "memory: 0Gi" not in script


@pytest.mark.parametrize(
    "memory_gb",
    [0, -1, True, float("nan"), float("inf")],
)
def test_submit_job_rejects_invalid_memory_before_materialization(
    tmp_path,
    memory_gb,
):
    state = State.new("experiment", tmp_path)
    runtime_root = state.root / "outputs" / "experiment" / "runtime"

    result = asyncio.run(rm._submit_job(
        state,
        command="echo must-not-render",
        scheduler="local",
        memory_gb=memory_gb,
    ))

    assert result["status"] == "error"
    assert result["reason"] == "invalid_requested_resources"
    assert runtime_root.exists() is False


@pytest.mark.parametrize("memory_gb", [0, float("nan"), float("inf")])
def test_build_resource_preflight_reports_unusable_memory_without_blocking(
    tmp_path,
    memory_gb,
):
    """判决拆除·第三波：本工具只咨询、不拦编译（恒 success）。

    但"不拦"不等于"照说可行"：算不出容量结论时必须如实进 capability_issues ——
    否则 decision=build_mode_feasible 是框架编的假话，而下游 build_resource_plan
    会拿它当已获准的预算（NaN 比较恒假，连 <=0 都挡不住）。
    """
    state = State.new("experiment", tmp_path)

    result = asyncio.run(rm._preflight_build_resources(
        state,
        compile_mode="serial",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=memory_gb,
        walltime_minutes=5,
        runtime_resource_policy="fixed",
    ))

    assert result["status"] == "success"
    assert result["decision"] != "build_mode_feasible"
    assert any("positive finite number" in issue
               for issue in result["capability_issues"])

def test_build_resource_preflight_is_advisory_and_never_blocks_or_pauses(tmp_path, monkeypatch):
    """判决拆除·第三波（rm:701-787 缩成纯咨询）：固定资源不足 / 需用户同意都不再
    返回 error/pause——status 恒 success，疑虑与建议如实记进 payload 与
    build_resource_plan。error/pause 两档加回去本测试即转红。"""
    from nodes.experiment.tools import resource_manager as rm

    state = _planned_state(tmp_path)
    profile = {
        "status": "success", "available_schedulers": ["local"],
        "recommended_default": "local",
        "core_platform_capabilities": {"host_software": [], "compute_grants": [], "errors": []},
        "resources": [{"scheduler": "local", "available": True, "cpu_count": 2,
                       "memory_available_mb": 4096, "gpus": []}],
    }
    monkeypatch.setattr(rm, "_discover", lambda namespace=None: profile)

    fixed = asyncio.run(rm._preflight_build_resources(
        state, compile_mode="serial", mpi_ranks=4, cpus_per_rank=1,
        gpus=0, memory_gb=1, walltime_minutes=5,
        scheduler_preference="local", runtime_resource_policy="fixed",
    ))
    assert fixed["status"] == "success"
    assert fixed["decision"] == "capacity_concerns"
    assert "exceeds local cpu_count" in fixed["capacity_warnings"][0]
    assert "Do not configure" not in fixed["recommendation"]
    assert state.hook_state["build_resource_preflight"]["status"] == "success"

    consent = asyncio.run(rm._preflight_build_resources(
        state, compile_mode="serial", mpi_ranks=4, cpus_per_rank=1,
        gpus=0, memory_gb=1, walltime_minutes=5,
        scheduler_preference="local", runtime_resource_policy="requires_user_approval",
    ))
    assert consent["status"] == "success"
    assert consent["decision"] == "capacity_concerns_user_consent_declared"
    assert "ask the user" in consent["recommendation"]
    assert "pause_event" not in consent


def test_build_resource_preflight_persists_exact_fixed_budget(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tools import resource_manager as rm

    state = _planned_state(tmp_path)
    profile = {
        "status": "success",
        "available_schedulers": ["local"],
        "recommended_default": "local",
        "core_platform_capabilities": {
            "host_software": [],
            "compute_grants": [],
            "errors": [],
        },
        "resources": [{
            "scheduler": "local",
            "available": True,
            "cpu_count": 8,
            "memory_available_mb": 16 * 1024,
            "gpus": [],
        }],
    }
    monkeypatch.setattr(rm, "_discover", lambda namespace=None: profile)

    result = asyncio.run(rm._preflight_build_resources(
        state,
        compile_mode="serial",
        mpi_ranks=1,
        cpus_per_rank=2,
        gpus=0,
        memory_gb=2,
        walltime_minutes=5,
        scheduler_preference="local",
        runtime_resource_policy="fixed",
    ))

    assert result["status"] == "success"
    assert result["requested_resources"]["total_cpus"] == 2
    assert result["requested_resources"]["memory_gb"] == 2
    assert result["requested_resources"]["walltime_minutes"] == 5
    assert state.hook_state["build_resource_preflight"] is result
    artifact = state.read_artifact(result["artifact_id"])
    persisted = json.loads(artifact["content"])
    assert persisted["requested_resources"] == result["requested_resources"]


def test_build_resource_preflight_reports_missing_mpi_capability_as_advice(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as rm

    state = _planned_state(tmp_path)
    profile = {
        "status": "success", "available_schedulers": ["local"],
        "recommended_default": "local",
        "core_platform_capabilities": {"host_software": [], "compute_grants": [], "errors": []},
        "resources": [{"scheduler": "local", "available": True, "cpu_count": 64,
                       "memory_available_mb": 1024 * 128, "gpus": []}],
    }
    monkeypatch.setattr(rm, "_discover", lambda namespace=None: profile)
    monkeypatch.setattr(rm.shutil, "which", lambda name: None)

    result = asyncio.run(rm._preflight_build_resources(
        state, compile_mode="mpi", mpi_ranks=4, cpus_per_rank=1,
        gpus=0, memory_gb=2, walltime_minutes=5,
        scheduler_preference="local", runtime_resource_policy="fixed",
    ))
    assert result["status"] == "success"
    assert result["decision"] == "capability_concerns"
    assert any("MPI build mode" in issue for issue in result["capability_issues"])
    assert "errors" not in result


def test_backgrounded_job_command_is_submitted_and_witnessed(tmp_path):
    """判决拆除·第三波（rm:1908 降格）：nohup/& 不再拒绝提交——照提交（dry_run 同路），
    submission 记录挂 unmanaged_background_launch 见证；墙加回去即转红。"""
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="nohup ./run.sh &", scheduler="local", dry_run=True,
    ))

    assert result["status"] == "success", result
    assert "blocker" not in result
    assert "submit_job" in result["submission_witness"]["unmanaged_background_launch"]["hint"]
    saved = json.loads(state.read_artifact(
        state.list_artifacts("job_submission")[-1]["id"])["content"])
    assert "unmanaged_background_launch" in saved["submission_witness"]
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "unmanaged_background_launch_witnessed" in events
    assert "unmanaged_background_launch_blocked" not in events


def test_health_contract_ignores_unknown_fields_and_records_them(tmp_path):
    """判决拆除·第三波（rm:1528 → schema）：未知字段不再拒绝——只读已知字段，
    忽略的名字如实进契约（从不被当命令执行）。"""
    contract = rm._health_contract(
        {"progress_command": "tail -f log", "poll_interval_s": 60, "stall_after_s": 900},
        workdir=str(tmp_path), output_roots=[str(tmp_path)],
        scheduler_output_dir=tmp_path, expected_duration_s=None,
    )
    assert contract["ignored_fields"] == ["progress_command"]
    assert "progress_command" not in contract
    assert contract["poll_interval_s"] == 60 and contract["stall_after_s"] == 900
    with pytest.raises(ValueError):   # 跨字段下限 schema 表达不了，留手写
        rm._health_contract({"poll_interval_s": 600, "stall_after_s": 900},
                            workdir=str(tmp_path), output_roots=[str(tmp_path)],
                            scheduler_output_dir=tmp_path, expected_duration_s=None)



def test_submission_route_workdir_is_not_reselected_by_legacy_stage(tmp_path):
    state = _planned_state(tmp_path)

    for stage in ("diagnostic", "toolchain_build", "simulation"):
        result = asyncio.run(_submit_job(
            state=state, command="echo dry-run", scheduler="local", dry_run=True, stage=stage))
        expected = rm.experiment_output_dir(state, "runtime", create=False)
        assert result["status"] == "success"
        assert result["workdir"] == str(expected)
        assert result["scheduler_output_dir"] == str(expected / "logs")


def test_submission_rejects_output_paths_outside_build_or_run_roles(tmp_path):
    state = _planned_state(tmp_path)
    run_root = rm.experiment_output_dir(state, "runtime", create=True)
    state.hook_state["path_roles"] = {"run_root": str(run_root)}

    result = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local",
        workdir=str(run_root), output_paths=["/var/lib/undeclared-output"],
        dry_run=True,
    ))

    assert result["status"] == "error"
    assert "output_paths" in result["error"]
    assert str(run_root) in result["error"]
    assert result["blocker"]["kind"] == "scheduler_path_not_declared"
    assert result["blocker"]["human_action"] == "not_applicable"


def test_submission_rejects_unresolved_scheduler_path_placeholder(tmp_path):
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local",
        workdir="<SHARED_ROOT>/experiment", dry_run=True,
    ))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "unresolved_scheduler_path_placeholder"
    assert result["blocker"]["field"] == "workdir"
    assert result["blocker"]["human_action"] == "not_applicable"


def test_approved_write_root_never_authorizes_scheduler_workdir(tmp_path):
    state = _planned_state(tmp_path)
    approved = tmp_path / "human-approved"
    approved.mkdir()
    state.hook_state["path_roles"] = {
        "approved_write_root": {"path": str(approved), "writable": True},
    }

    result = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local",
        workdir=str(approved), output_dir=str(approved), dry_run=True,
    ))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "approved_write_root_not_scheduler_usable"
    assert result["blocker"]["human_action"] == "not_applicable"


def test_scheduler_script_runs_identity_preflight_before_payload(tmp_path):
    state = _planned_state(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {"run_root": str(run_root)}

    result = asyncio.run(_submit_job(
        state=state, command="srun ./solver", scheduler="slurm",
        workdir=str(run_root), dry_run=True,
    ))

    script = Path(result["script_path"]).read_text(encoding="utf-8")
    assert "HARNESS_IDENTITY_PREFLIGHT status=failed reason=nss_lookup" in script
    assert "reason=workdir_access" in script
    assert script.index("HARNESS_IDENTITY_PREFLIGHT") < script.index("srun ./solver")



def test_real_slurm_submission_is_fail_closed_without_remote_sandbox(tmp_path):
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="echo approved-path", scheduler="slurm",
        dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "error"
    assert result["blocker"] == {
        "kind": "remote_sandbox_contract_unavailable", "scheduler": "slurm"}


def test_high_risk_job_payload_uses_the_same_submission_confirmation(tmp_path, trusted_sandbox):
    """A dangerous scheduler payload must pause once at submit_job, not reject then ask again."""
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="sudo echo submit", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "pause"
    assert result["pause_event"]["metadata"]["tool"] == "submit_job"
    assert "本地受管作业提交（含高危命令：提权 (sudo)）" in result["pause_event"]["question"]
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "job_submission_blocked_pending_confirm" in events
    assert "请先 request_human_input" not in events



def test_declared_run_root_is_allowed_but_adjacent_container_path_is_rejected(tmp_path):
    state = _planned_state(tmp_path)
    run_root = rm.experiment_output_dir(state, "runtime", create=True)

    allowed = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local", dry_run=True,
        stage="toolchain_build", workdir=str(run_root),
        output_dir=str(run_root / "logs"), output_paths=[str(run_root)]))
    rejected = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local", dry_run=True,
        stage="diagnostic", workdir=str(run_root.parent / "other")))

    assert allowed["status"] == "success", allowed
    assert rejected["status"] == "error"
    assert rejected["blocker"]["kind"] == "scheduler_path_not_declared"



def test_explicit_local_memory_contract_reaches_docker_submission_boundary(
    tmp_path, monkeypatch, trusted_sandbox,
) -> None:
    from shared.lib import dangerous_commands as danger

    gib = 1024**3
    state = _planned_state(tmp_path)
    captured = {}

    def reached_submit(*args, **kwargs):
        captured["memory_gb"] = args[7]
        captured["storage_gb"] = args[8]
        captured["limits"] = kwargs["precomputed_build_limits"]
        return {"status": "error", "reason": "test_submit_boundary_reached"}

    monkeypatch.setattr(rm, "_submit_sync", reached_submit)

    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state, command="printf blocked", scheduler="local",
            memory_gb=16, dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["reason"] == "test_submit_boundary_reached"
    assert captured["memory_gb"] == 16.0
    assert captured["storage_gb"] == 8.0
    assert captured["limits"].memory_reservation_bytes == 16 * gib
    assert captured["limits"].memory_max_bytes == 16 * gib
    assert captured["limits"].resource_policy == "fixed_submission_contract"
    assert state.list_artifacts("external_submission_intent") == []


def test_default_memory_uses_fixed_main_contract_not_unrelated_build_plan(
    tmp_path, monkeypatch, trusted_sandbox,
) -> None:
    from shared.lib import dangerous_commands as danger

    gib = 1024**3
    state = _planned_state(tmp_path)
    state.hook_state["build_resource_preflight"] = {
        "status": "success",
        "runtime_resource_policy": "fixed",
        "artifact_id": "unrelated_build_plan",
        "requested_resources": {
            "total_cpus": 4, "memory_gb": 8, "walltime_minutes": 10,
        },
    }
    captured = {}

    def reached_submit(*args, **kwargs):
        captured["memory_gb"] = args[7]
        captured["memory_contract"] = kwargs["memory_contract"]
        captured["limits"] = kwargs["precomputed_build_limits"]
        return {"status": "error", "reason": "test_submit_boundary_reached"}

    monkeypatch.setattr(rm, "_submit_sync", reached_submit)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state, command="printf admitted", scheduler="local",
            dry_run=False, execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result["reason"] == "test_submit_boundary_reached"
    assert captured["memory_gb"] == 4.0
    assert captured["memory_contract"]["source"] == "explicit_tool_argument"
    assert captured["memory_contract"]["mode"] == "fixed"
    assert captured["limits"].memory_reservation_bytes == 4 * gib
    assert captured["limits"].memory_max_bytes == 4 * gib
    assert captured["limits"].resource_policy == "fixed_submission_contract"
    assert captured["limits"].resource_plan_artifact_id is None


def test_submit_job_description_does_not_promise_kubernetes_submission():
    from core.tool_registry import get_tool

    description = get_tool("submit_job").description
    assert "绝不会自动选择 Kubernetes" in description
    assert "缺少显式 PVC/volume 合同时连 dry-run 也会拒绝" in description
    assert "then Kubernetes" not in description
    assert "real submission always goes through" not in description
    assert "普通本地受管提交" in description


# ── E-14：本地提交链 payload 可执行目标预检（与 safe_run_bash 同口径） ──────

def test_local_submission_rejects_missing_payload_executable_with_zero_consumption(
    tmp_path,
):
    """目标不存在必须在绑定/HITL/intent 之前拒绝：step 留在 ready。"""
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "error"
    assert result["reason"] == "payload_exec_preflight_rejected"
    assert result["blocker"]["kind"] == "payload_exec_preflight_rejected"
    # 拒绝文案必须给出两条可执行出路：拆 build/run 两个 route 步骤，或
    # stage_in 交付预构建二进制。
    assert "route 步骤" in result["error"]
    assert "stage_in" in result["error"]
    # 零 intent、零 route attempt：没有任何持久提交痕迹，步骤仍可直接重试。
    assert state.list_artifacts("external_submission_intent") == []
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "route_step_bound" not in transcript
    snapshot = execution_route.build_route_snapshot(state)
    assert "submit_solver" in snapshot["ready_step_ids"]


def test_local_submission_rejects_payload_target_without_exec_bit(tmp_path):
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    target = run_dir / "solver"
    target.write_text("not actually executable\n", encoding="utf-8")
    target.chmod(0o644)

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
    ))

    assert result["status"] == "error"
    assert result["reason"] == "payload_exec_preflight_rejected"
    assert "chmod +x" in result["error"]
    assert state.list_artifacts("external_submission_intent") == []
    assert "route_step_bound" not in state.transcript_path.read_text(
        encoding="utf-8")


def test_stage_in_receipt_admits_target_materialized_at_launch(
    tmp_path, monkeypatch, trusted_sandbox,
):
    """stage_in 将物化的可执行目标按收据放行并直达同一次物理提交。"""
    state = _planned_state(tmp_path)
    src = Path(state.root) / "prebuilt_solver"
    src.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    src.chmod(0o755)
    submissions: list[dict] = []

    def fake_submit_sync(*_args, **kwargs):
        submissions.append(kwargs)
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job", "job_id": "staged-job",
            "submission_nonce": "staged-nonce",
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit_sync)

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
        stage_in=[{"src": str(src), "dst": "solver"}],
    ))

    assert result["status"] == "success", result
    assert len(submissions) == 1
    assert submissions[0]["stage_in"][0]["dst"] == "solver"
    assert submissions[0]["stage_in"][0]["sha256"]
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "job_submission_blocked_pending_confirm" not in events


def test_staged_target_without_exec_bit_is_rejected_by_its_receipt(tmp_path):
    state = _planned_state(tmp_path)
    src = Path(state.root) / "prebuilt_solver"
    src.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    src.chmod(0o644)

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=False, execution_params={"case": "test"},
        stage_in=[{"src": str(src), "dst": "solver"}],
    ))

    assert result["status"] == "error"
    assert result["reason"] == "payload_exec_preflight_rejected"
    assert "chmod +x" in result["error"]
    assert "bash" in result["error"]
    assert state.list_artifacts("external_submission_intent") == []


def test_combined_build_and_run_payload_waives_its_own_product(tmp_path):
    """判决改判（owner，2026-09-06 E2E 活体）：`make && ./a.out` 是一条批处理
    作业的正常形态——缺席目标由同一 payload 的前段构建、且落在本作业可写根内
    时，存在性预检放行并如实披露；真实构建失败由运行时 rc/诊断兜住。

    覆盖原摩擦审计裁定的"合一 payload 必拦 + 两出口教育"——那条裁定产生于
    fixture 还允许拆步的时代；现行 benchmark fixture 明确要求单一受管 payload
    串行 configure+build+run。"""
    state = State.new("experiment", tmp_path)
    build = tmp_path / "build"
    build.mkdir()

    block, pinned = _payload_preflight_sync(
        state, "make -j4 && ./a.out",
        sandbox=rm._LocalSandboxProjection(
            workdir=build, mount_roots=[str(build)],
            writable_roots=[str(build)]),
        stage_in=None)

    assert block is None, block
    assert pinned == []          # 还没有内容可钉——内部产物不参与 TOCTOU 钉住
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "payload_internal_exec_targets" in transcript
    assert "a.out" in transcript


def test_first_segment_missing_target_still_walled_even_in_writable_root(tmp_path):
    """豁免只给「前段有生产者」的缺席目标：单段 `./solver` 没有任何前段能
    构建它，仍是打错路径——老墙照拦、教育文案照给（零消耗、step 留 ready）。"""
    state = State.new("experiment", tmp_path)
    build = tmp_path / "build"
    build.mkdir()

    block, pinned = _payload_preflight_sync(
        state, "./a.out --case x",
        sandbox=rm._LocalSandboxProjection(
            workdir=build, mount_roots=[str(build)],
            writable_roots=[str(build)]),
        stage_in=None)

    assert block is not None
    assert block["reason"] == "payload_exec_preflight_rejected"
    assert "stage_in" in block["error"]
    assert pinned == []


def test_collect_exec_path_targets_extracts_mpirun_target(tmp_path):
    from nodes.experiment.tools import safe_bash as sb

    targets = sb.collect_exec_path_targets(
        "mpirun -np 4 --hostfile /tmp/hosts ./solver --flag x",
        cwd=str(tmp_path),
    )

    paths = [item["path"] for item in targets]
    assert str(tmp_path / "solver") in paths
    # launcher 的选项值（--hostfile /tmp/hosts）不得被当作被启动程序。
    assert "/tmp/hosts" not in paths


def test_confirmed_high_risk_submission_pins_payload_executable_sha(
    tmp_path, monkeypatch, trusted_sandbox,
):
    """高危本地提交把预检 sha256 同时钉入批准载荷与物理提交。"""
    import hashlib
    import os

    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    target = run_dir / "solver"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)

    consumed: list[str] = []
    captured: dict = {}
    monkeypatch.setattr(danger, "is_confirmed", lambda _state, _text: True)
    monkeypatch.setattr(
        danger, "consume_confirmation",
        lambda _state, text: consumed.append(text),
    )

    def fake_submit_sync(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job", "job_id": "pinned-job",
            "submission_nonce": "pinned-nonce",
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit_sync)

    result = asyncio.run(rm._submit_job(
        state=state, command="./solver && sudo -n true", scheduler="local", dry_run=False,
        execution_params={"case": "test"},
    ))

    assert result["status"] == "success"
    expected = [{
        "path": os.path.join(os.path.realpath(str(run_dir)), "solver"),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }]
    approved = json.loads(consumed[0])
    assert approved["payload_executables"] == expected
    assert captured["payload_executables"] == expected


def test_submit_sync_rejects_payload_executable_changed_after_approval(tmp_path):
    """TOCTOU 二道防线：批准的是当时内容的 sha256，换了二进制批准即失效。"""
    import hashlib

    state = State.new("experiment", tmp_path)
    runtime_root = rm.experiment_output_dir(state, "runtime", create=True)
    target = runtime_root / "solver"
    target.write_text("approved payload\n", encoding="utf-8")
    target.chmod(0o755)
    approved_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text("swapped after approval\n", encoding="utf-8")

    result = rm._submit_sync(
        runtime_root, "local", "./solver", "experiment_job",
        1, 1, 0, 1.0, 1.0, 5,
        None, None, None, str(runtime_root), False, None,
        stage_in=None,
        state=state,
        payload_executables=[
            {"path": str(target), "sha256": approved_digest},
        ],
    )

    assert result["status"] == "error"
    assert result["reason"] == "payload_executable_changed_after_approval"
    assert result["blocker"]["human_action"] == "resubmit_and_reconfirm"
    assert "重新调用 submit_job" in result["error"]
    # 复检发生在脚本/intent/adapter.prepare 之前：零持久痕迹。
    assert state.list_artifacts("external_submission_intent") == []
    assert not (runtime_root / "jobs").exists()
    assert "job_submission_payload_executable_changed" in \
        state.transcript_path.read_text(encoding="utf-8")


def test_dry_run_never_runs_payload_executable_preflight(tmp_path, monkeypatch):
    """dry_run 完全不预检：目标不存在也照常渲染脚本。"""
    state = _planned_state(tmp_path)
    monkeypatch.setattr(
        rm, "_preflight_local_payload_executables",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry_run must not preflight payload executables")),
    )

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=True,
    ))

    assert result["status"] == "success"
    assert result["dry_run"] is True


# ── E-14 回归：预检口径必须是「本次作业的挂载集合」，不是宿主机文件系统 ──────
#
# 活体实证 2026-08-30 airsea run 1788187822-56908f：payload 是
# `/usr/local/bin/python3.12 .../match_model.py`（scheduler=local）。该解释器
# 在沙箱镜像里存在（容器内 `ls -la` 实测 14480 字节、可执行），在宿主机上不
# 存在。首版预检在 harness 宿主进程 namespace 里 `os.path.exists`，于是四次
# 真实提交全被 payload_exec_preflight_rejected 拦死。

_IMAGE_ONLY_ARGV0 = (
    "/usr/local/bin/python3.12",        # 活体实证里的那一条
    "/usr/bin/gfortran",
    "/opt/openmpi/bin/mpirun",
)


def _local_sandbox(state, command, *, workdir, stage_in=None):
    """按真实提交链算出这次 local 作业的沙箱投影（作业 cwd + 挂载集合）。

    走 `_preflight_local_submission_sandbox` 本身，而不是测试里手搓一份范围：
    E-14 的根因正是判定范围与真实挂载集合是两套东西。
    """
    block, sandbox = rm._preflight_local_submission_sandbox(
        state, command,
        workdir=workdir,
        output_roots=[],
        runtime_root=rm.experiment_output_dir(state, "runtime", create=True),
        output_dir=None,
        job_name="experiment_job",
        stage_in=stage_in,
    )
    assert block is None, block
    assert sandbox is not None
    return sandbox


def test_image_provided_absolute_argv0_is_not_judged_by_host_filesystem(
    tmp_path,
):
    """镜像自带的绝对系统路径必须放行：宿主机看不见不等于容器里没有。"""
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    (run_dir / "match_model.py").write_text("print('ok')\n", encoding="utf-8")

    # 一条保证宿主机上不存在的系统路径：锁住「不存在也放行」这一分支，
    # 使本用例不依赖跑测机器上恰好装没装 python3.12/gfortran。
    absent = f"/usr/local/bin/python3.12-{uuid.uuid4().hex}"
    assert not os.path.exists(absent)

    for argv0 in (*_IMAGE_ONLY_ARGV0, absent):
        command = f"{argv0} match_model.py"
        block, pinned = _payload_preflight_sync(
            state, command,
            sandbox=_local_sandbox(state, command, workdir=str(run_dir)),
            stage_in=None)

        assert block is None, argv0
        # 镜像内目标也不得按宿主机内容 sha256 钉住：宿主机同名文件（若有）
        # 根本不是作业将要执行的那一个。
        assert pinned == [], argv0


def test_declared_role_over_image_tree_cannot_reclaim_host_authority(tmp_path):
    """E-14 的真实失败面：某个路径角色声明覆盖了镜像内的系统树。

    `core/sandbox.py` 的 `_validate_mount` 让 /usr、/bin、/sbin、/lib、/lib64
    及其子树永远不可能成为 bind mount，所以无论这些路径以什么名义（编排
    node_inputs、人工批准的 dependency_root）进入范围计算，宿主机对它们都没有
    判定权。这条用例把「即便系统树混进了挂载集合也照样放行」钉死——首版把
    「非 container_only 的声明角色」当成受管挂载点，正是在这里原样复发。
    """
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    (run_dir / "match_model.py").write_text("print('ok')\n", encoding="utf-8")

    toolchain = f"/opt/hf-toolchain-{uuid.uuid4().hex}"
    cases = (
        # 只读 dependency_root=/usr：path_roles 明写这是合法声明，挂载最小化
        # 之后 /usr 真的会出现在这次作业的 roots 里。
        ("/usr", f"/usr/local/bin/python3.12-{uuid.uuid4().hex}"),
        # 宿主机上并不存在的 /opt/<toolchain>：它上溯成 /opt，凭空让整棵 /opt
        # 拿到宿主机判定权。
        (toolchain, f"{toolchain}/bin/solver"),
    )
    for role_path, argv0 in cases:
        assert not os.path.exists(argv0)
        roles = dict(state.hook_state.get("path_roles") or {})
        roles["dependency_root"] = {"path": role_path, "writable": False}
        state.hook_state["path_roles"] = roles

        command = f"{argv0} match_model.py"
        sandbox = _local_sandbox(state, command, workdir=str(run_dir))
        # 前提断言：该系统树确实进了本次作业的挂载范围——否则本用例锁不住
        # 任何东西（首版的 roots 断言就是这样空转的）。
        assert any(
            rm._mount_contains(Path(root), Path(argv0))
            for root in sandbox.mount_roots
        ), (role_path, sandbox.mount_roots)

        block, pinned = _payload_preflight_sync(
            state, command, sandbox=sandbox, stage_in=None)

        assert block is None, (role_path, block)
        assert pinned == [], role_path


def test_workdir_none_still_preflights_relative_targets_at_job_dir(tmp_path):
    """workdir=None 是合法 local 提交形态，守卫不得因此静默失效。

    作业 cwd 是框架自己的作业目录（在 run_root 内、确为 bind mount），相对
    目标必须按它解析，而不是落到 harness 进程 cwd 上然后一律放行。
    """
    state = _planned_state(tmp_path)
    runtime_root = rm.experiment_output_dir(state, "runtime", create=True)

    for command in ("./solver --case x", "bin/solver --case x"):
        sandbox = _local_sandbox(state, command, workdir=None)
        assert rm._mount_contains(runtime_root.resolve(), sandbox.workdir)

        block, pinned = _payload_preflight_sync(
            state, command, sandbox=sandbox, stage_in=None)

        assert block is not None, command
        assert block["reason"] == "payload_exec_preflight_rejected"
        assert "可执行目标不存在" in block["error"]
        assert pinned == []


def test_unknown_mount_scope_fails_closed_not_open(tmp_path):
    """范围算不出来时守卫收紧回宿主机口径，不是静默全放行。"""
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    empty_scope = rm._LocalSandboxProjection(workdir=run_dir, mount_roots=[])

    block, _pinned = _payload_preflight_sync(
        state, "./solver --case x", sandbox=empty_scope, stage_in=None)

    assert block is not None
    assert block["reason"] == "payload_exec_preflight_rejected"

    # 但 fail-closed 不得把 E-14 请回来：镜像自带的系统树任何时候都不判。
    absent = f"/usr/local/bin/python3.12-{uuid.uuid4().hex}"
    image_block, _ = _payload_preflight_sync(
        state, f"{absent} match_model.py",
        sandbox=empty_scope, stage_in=None)
    assert image_block is None


def test_dangling_symlink_inside_managed_mount_is_still_rejected(tmp_path):
    """归属按词法判定：workdir 内指向范围外的悬空软链仍拒。"""
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    (run_dir / "solver").symlink_to(
        f"/usr/local/bin/nonexistent-{uuid.uuid4().hex}")

    command = "./solver --case x"
    block, pinned = _payload_preflight_sync(
        state, command,
        sandbox=_local_sandbox(state, command, workdir=str(run_dir)),
        stage_in=None)

    assert block is not None
    assert block["reason"] == "payload_exec_preflight_rejected"
    assert "可执行目标不存在" in block["error"]
    assert pinned == []


def test_local_submission_admits_image_provided_interpreter_payload(
    tmp_path, monkeypatch, trusted_sandbox,
):
    """端到端：镜像内解释器的 payload 必须走到提交边界，而不是被事前拒。"""
    from shared.lib import dangerous_commands as danger

    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    (run_dir / "match_model.py").write_text("print('ok')\n", encoding="utf-8")
    # 镜像自带的绝对路径；带 uuid 的父目录保证宿主机上不存在。
    # basename 取已声明路线步骤的 program（solver），路线门与本用例无关。
    interpreter = f"/usr/local/hf-image-{uuid.uuid4().hex}/bin/solver"
    assert not os.path.exists(interpreter)

    captured: dict = {}

    def reached_submit(*_args, **kwargs):
        captured["payload_executables"] = kwargs.get("payload_executables")
        return {"status": "error", "reason": "test_submit_boundary_reached"}

    monkeypatch.setattr(rm, "_submit_sync", reached_submit)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state, command=f"{interpreter} match_model.py",
            scheduler="local", dry_run=False,
            execution_params={"case": "test"},
        ))
    finally:
        danger.set_bypass_mode(False)

    assert result.get("reason") == "test_submit_boundary_reached"
    assert captured["payload_executables"] == []


def test_absolute_target_inside_managed_mount_is_still_rejected(tmp_path):
    """收窄判定范围不得放宽既有防线：受管挂载点内的目标不存在照样拒。"""
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    missing = run_dir / "bin" / "solver"
    command = f"{missing} --case test"

    block, pinned = _payload_preflight_sync(
        state, command,
        sandbox=_local_sandbox(state, command, workdir=str(run_dir)),
        stage_in=None)

    assert block is not None
    assert block["reason"] == "payload_exec_preflight_rejected"
    assert "可执行目标不存在" in block["error"]
    assert pinned == []


def test_managed_mount_target_without_exec_bit_is_still_rejected(tmp_path):
    """workdir 内存在但无执行位：仍是宿主机可判定的事实，仍拒。"""
    state = _planned_state(tmp_path)
    run_dir = rm.experiment_output_dir(state, "runtime", create=True)
    target = run_dir / "solver"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o644)

    command = "./solver --case test"
    block, _pinned = _payload_preflight_sync(
        state, command,
        sandbox=_local_sandbox(state, command, workdir=str(run_dir)),
        stage_in=None)

    assert block is not None
    assert block["reason"] == "payload_exec_preflight_rejected"
    assert "chmod +x" in block["error"]


def test_host_verifiable_target_contract(tmp_path):
    """判定权的三条规则（safe_bash 单元口径）。"""
    from nodes.experiment.tools import safe_bash as sb

    managed = tmp_path / "run"
    managed.mkdir()

    # ① roots=None：本地 bash 主路径，全盘按宿主机判定，行为不变。
    assert sb._host_verifiable_exec_target("/usr/bin/gfortran", None) is True
    # ② 镜像自带系统树：任何 roots 下都不判（就算它被塞进了范围）。
    for roots in ([], [str(managed)], ["/usr"], [str(managed), "/", "/usr"]):
        for target in ("/usr/local/bin/python3.12",
                       "/opt/openmpi/bin/mpirun",
                       "/bin/sh", "/lib64/ld-linux-x86-64.so.2"):
            assert sb._host_verifiable_exec_target(target, roots) is False
    # 但"镜像自带"按词法判：受管挂载点内指向系统树的软链仍归宿主机判定。
    (managed / "py").symlink_to("/usr/local/bin/python3.12")
    assert sb._host_verifiable_exec_target(
        str(managed / "py"), [str(managed)]) is True
    # ③ 挂载点内的目标归宿主机判；范围为空则 fail-closed 回宿主机口径。
    assert sb._host_verifiable_exec_target(
        str(managed / "solver"), [str(managed)]) is True
    assert sb._host_verifiable_exec_target(
        str(managed / "solver"), []) is True
    assert sb._host_verifiable_exec_target(
        str(tmp_path / "elsewhere" / "solver"), [str(managed)]) is False


def test_dry_run_result_states_payload_preflight_was_not_evaluated(tmp_path):
    """消除信号反转：dry_run 成功必须自陈「预检没跑」，不能被读成预检通过。"""
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="./solver --case test", scheduler="local",
        dry_run=True,
    ))

    assert result["status"] == "success"
    note = result["payload_exec_preflight"]
    assert note["evaluated"] is False
    assert note["reason"] == "dry_run"
    assert "dry_run 成功不代表预检会通过" in note["note"]
    # 文案必须与实际判定一致：范围是挂载集合，镜像内绝对路径不判。
    assert "沙箱挂载集合" in note["note"]
    assert "宿主机没有判定权" in note["note"]


@pytest.mark.parametrize(
    ("scheduler", "directive"),
    (("slurm", "#SBATCH --time="), ("pbs", "#PBS -l walltime=")),
)
def test_submit_omitted_walltime_emits_no_directive_and_records_site_default(
    scheduler, directive, tmp_path,
) -> None:
    # 工具级契约（AGENTS.md/schema 同文）：省略 walltime → 调度器脚本不落
    # 时限指令，台账记 site_default_unknown。既有单测只喂内层 _script_for，
    # 挡不住外层签名默认值把省略偷换成 60 分钟并把来源谎报成 explicit。
    state = _planned_state(tmp_path)
    run_root = Path(state.root) / "run"
    run_root.mkdir(exist_ok=True)
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    omitted = asyncio.run(_submit_job(
        state=state, command="echo ok", scheduler=scheduler,
        workdir=str(run_root), output_dir=str(run_root / "logs"),
        dry_run=True, route_step_id="submit_echo",
    ))
    assert omitted["status"] == "success", omitted
    script = Path(omitted["script_path"]).read_text(encoding="utf-8")
    assert directive not in script
    contract = omitted["time_contract"]
    assert contract["scheduler_walltime_minutes"] is None
    assert contract["scheduler_walltime_source"] == "site_default_unknown"

    explicit = asyncio.run(_submit_job(
        state=state, command="echo ok", scheduler=scheduler,
        job_name="experiment_job_explicit",
        workdir=str(run_root), output_dir=str(run_root / "logs"),
        dry_run=True, walltime_minutes=90, route_step_id="submit_echo",
    ))
    assert explicit["status"] == "success", explicit
    script = Path(explicit["script_path"]).read_text(encoding="utf-8")
    assert f"{directive}01:30:00" in script
    assert explicit["time_contract"]["scheduler_walltime_source"] == "explicit"


def test_submit_local_omitted_walltime_keeps_platform_safety_cap(
    tmp_path,
) -> None:
    # 本地沙箱行为保持不变：无 walltime/hard_deadline 时兜底 60 分钟，
    # 且来源如实标为 platform_safety_default（不是调度器契约）。
    state = _planned_state(tmp_path)

    result = asyncio.run(_submit_job(
        state=state, command="echo ok", scheduler="local", dry_run=True,
    ))
    assert result["status"] == "success", result
    sandbox = result["sandbox_contract"]
    assert sandbox["walltime_seconds"] == 3600
    assert sandbox["walltime_source"] == "platform_safety_default"
    # cpus 是请求值，不冒充已生效的限额（#841）：Core 的隔离义务里没有 CPU_CAP。
    assert sandbox["cpus_source"] == "requested"
    assert sandbox["cpu_limit_enforcement"] == "backend_dependent_unverified"
    assert result["time_contract"]["scheduler_walltime_source"] == "not_applicable"
