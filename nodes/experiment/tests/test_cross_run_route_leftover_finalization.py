"""跨 run 遗留作业的收尾：路线投影按归属处理（N-7，无 docker 依赖）。

死状态收养家族第 5 块拼图。上一个 run 提交、本 run 收养并清理干净的遗留作业，
能铸 closure、能清容器，却因为"路线终态投影找不到提交记录"被永久卡在
finalized_needs_route_reconciliation —— lifecycle/task 不关，output_roots 锁不放，
后续 submit_job 撞冲突，而"修复后重试 finalize_external_job"对它是个走不通的许诺：
本 run 的冻结路线里本来就没有这一步。

钉死的不变量：

- 跨 run 遗留（当前路线机械确认无该 submission）+ 已铸 closure → 收尾走完：
  lifecycle=finalized、task completed、output_roots 冲突消失；
- 投影记为 no-op（status=skipped），在 closure payload 与 transcript 事件里
  同时留痕，带完整 identity 与判定依据；
- 本 run 拥有该提交而投影真失败 → 仍 finalized_needs_route_reconciliation，
  一分不放宽（哪怕上游给的正是 route_submission_not_found 这个字符串）；
- 归属确认不了（路线读取异常 / 路线事件里有身份线索却匹配不上提交）→ fail-closed；
- N-5 与科学收尾的既有约束一分不松：核验一致的 simulation 不得宣称
  operation_completed/failed，operation_blocked 仍要非框架来源 blocker + 非空 note，
  analyzed_* 仍要 frozen 非自动 experiment_log + 完整 identity refs。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.blockers import record_blocker
from core.state import State
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import resource_manager as manager

_IDENTITY_FIELDS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id",
    "process_start_ticks", "container_runtime_id",
)


def _route() -> dict:
    return {
        "schema_version": 2,
        "goal": "提交并验证一个受管外部作业",
        "evidence_refs": ["https://example.invalid/official-run-guide"],
        "steps": [
            {
                "id": "run",
                "goal": "运行受管求解器",
                "after": [],
                "action": {"tool": "submit_job", "program": "solver"},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }
        ],
    }


def _declare_route(state: State) -> None:
    declared = asyncio.run(
        execution_route._declare_execution_route(state, route=_route()))
    assert declared["status"] == "success", declared


def _submit_this_run_job(state: State, job_id: str = "31415") -> dict:
    """本 run 通过路线提交的作业：路线里有它的提交收据。"""
    decision = execution_route.resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": [
                "external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    assert decision["decision"] == "matched_ready_step", decision
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
    })
    binding = execution_route.begin_route_step_attempt(
        state, decision, tool="submit_job",
        action={"payload_digest": "cross-run-leftover-test"},
    )
    assert binding and not binding.get("binding_error"), binding
    reference = {
        "scheduler": "slurm",
        "job_id": job_id,
        "namespace": "research",
        "launch_host": "login-01",
        "scheduler_cluster": "cluster-a",
        "resource_uid": f"slurm-cluster-a-{job_id}",
        "submission_nonce": binding["attempt_id"],
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": None,
        "route_attempt_id": binding["attempt_id"],
    }
    outcome = execution_route.finish_route_step_attempt(
        state, binding,
        result={"status": "success", **reference,
                "submission_artifact_id": f"job_submission__{job_id}"},
        external_submission=True,
    )
    assert outcome and outcome["outcome"] == "submitted", outcome
    return reference


def _leftover_submission(tmp_path: Path, **overrides) -> dict:
    """上一个 run 提交、本 run 只是收养的遗留作业记录。"""
    payload = {
        "status": "success",
        "dry_run": False,
        "scheduler": "slurm",
        "job_id": "88001",
        "namespace": "research",
        "launch_host": "login-09",
        "scheduler_cluster": "cluster-a",
        "resource_uid": "slurm-cluster-a-88001",
        "submission_nonce": "nonce-previous-run-88001",
        "execution_class": "simulation",
        "submitted_at": "2026-08-29T00:00:00+00:00",
        "workdir": str(tmp_path / "leftover-root"),
        "output_roots": [str(tmp_path / "leftover-root")],
    }
    payload.update(overrides)
    return payload


def _health(**overrides) -> dict:
    base = {
        "status": "success",
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": [],
        "scheduler_result": {"raw": {}},
    }
    base.update(overrides)
    return base


def _state_with_leftover(tmp_path: Path, monkeypatch, submission: dict,
                         *, submit_this_run: bool = True) -> State:
    state = State.new("experiment", tmp_path / "runs")
    _declare_route(state)
    if submit_this_run:
        _submit_this_run_job(state)
    state.save_artifact(
        "job_submission", "leftover_submission", json.dumps(submission))
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: _health())
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)
    return state


def _external_job_ref(payload: dict) -> dict:
    return {
        field: payload[field]
        for field in _IDENTITY_FIELDS
        if payload.get(field) not in {None, ""}
    }


def _finalize(state: State, submission: dict, outcome: str,
              evidence_artifact_id: str = "", note: str = "",
              disputed_execution_class: str = "") -> dict:
    return asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"],
        evidence_artifact_id, outcome, note=note,
        namespace=submission.get("namespace"),
        disputed_execution_class=disputed_execution_class))


def _events(state: State, event_name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(
            encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == event_name
    ]


def _blocked_leftover_finalize(state: State, submission: dict) -> dict:
    record_blocker(
        state,
        summary="上一个 run 留下的探测作业已死，本 run 无法为它取得科学结论",
        requested_action="修正 execution_class 派生规则并复核该遗留作业")
    return _finalize(
        state, submission, "operation_blocked",
        note="cross-run leftover probe; container already cleaned, no science to freeze",
        disputed_execution_class="diagnostic")


def test_cross_run_leftover_finalizes_and_releases_output_roots(
    tmp_path, monkeypatch,
):
    """活体形态：跨 run 遗留 + operation_blocked + disputed → 收尾走完、锁释放。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)
    monkeypatch.setattr(
        manager, "_external_job_is_active", lambda *_a, **_k: True)
    leftover_root = submission["output_roots"][0]
    assert manager._active_output_conflicts(state, [leftover_root])

    result = _blocked_leftover_finalize(state, submission)

    assert result["status"] == "success", result
    assert result["workflow_status"] == "finalized"
    assert result["outcome"] == "operation_blocked"
    assert result["class_disputed"] is True
    projection = result["route_projection"]
    assert projection["status"] == "skipped"
    assert projection["reason"] == (
        "cross_run_leftover_no_submission_in_current_route")
    assert projection["upstream_reason"] == "route_submission_not_found"
    assert projection["external_identity"]["job_id"] == submission["job_id"]
    assert projection["confirmation"]["status"] == "absent"
    assert projection["confirmation"]["route_declared"] is True
    assert result["task_completion"]["status"] == "success"
    lifecycle = state.list_artifacts("external_job_lifecycle")
    assert len(lifecycle) == 1
    assert json.loads(state.read_artifact(lifecycle[0]["id"])["content"])[
        "lifecycle_status"] == "finalized"
    # 锁必须真的释放：这正是卡住后续 submit_job continue 的那把锁。
    assert manager._active_output_conflicts(state, [leftover_root]) == []
    route_blockers = [
        item for item in state.hook_state["blockers"]
        if str(item.get("reported_by") or "").startswith(
            manager._FINALIZED_NEEDS_ROUTE_PREFIX)
    ]
    assert route_blockers == []


def test_skipped_projection_is_recorded_after_it_actually_happened(
    tmp_path, monkeypatch,
):
    """"这次收尾没有投影任何路线终态"必须完全可审计 —— 而且是事后追加的。

    收据（mint-once）里只准有铸造那一刻确已发生的归属判定；投影结果由引用该
    收据的 external_job_route_projection_record 与 transcript 事件承担。
    """
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    result = _blocked_leftover_finalize(state, submission)
    assert result["status"] == "success", result

    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    # 收据不得预先声明任何投影结论。
    assert "route_projection" not in payload
    assert "skipped" not in json.dumps(payload, ensure_ascii=False)
    ownership = payload["route_ownership"]
    assert ownership["observed_at"] == "operation_closure_minted"
    assert ownership["status"] == "absent"
    assert ownership["reason"] == "no_submission_in_current_route"
    assert ownership["evidence"]["route_declared"] is True

    records = state.list_artifacts("external_job_route_projection_record")
    assert len(records) == 1
    appended = json.loads(state.read_artifact(records[0]["id"])["content"])
    assert appended["record_type"] == "external_route_projection_skipped"
    assert appended["closure_kind"] == "finalize"
    assert appended["outcome"] == "operation_blocked"
    assert appended["operation_closure_artifact_id"] == closures[0]["id"]
    assert appended["projection"]["status"] == "skipped"
    assert appended["projection"]["reason"] == (
        "cross_run_leftover_no_submission_in_current_route")
    assert appended["projection"]["upstream_reason"] == (
        "route_submission_not_found")
    assert appended["projection"]["confirmation"]["status"] == "absent"
    for field in ("scheduler", "job_id", "namespace", "launch_host",
                  "scheduler_cluster", "resource_uid", "submission_nonce"):
        assert appended["identity"][field] == submission[field]
    assert result["route_projection"]["projection_record_artifact_id"] == (
        records[0]["id"])

    skipped = _events(state, "external_job_route_projection_skipped")
    assert len(skipped) == 1
    assert skipped[0]["job_id"] == submission["job_id"]
    assert skipped[0]["closure_kind"] == "finalize"
    assert skipped[0]["reason"] == (
        "cross_run_leftover_no_submission_in_current_route")
    assert skipped[0]["external_identity"]["submission_nonce"] == (
        submission["submission_nonce"])
    assert skipped[0]["upstream_reason"] == "route_submission_not_found"
    assert skipped[0]["operation_closure_artifact_id"] == closures[0]["id"]
    assert skipped[0]["projection_record_artifact_id"] == records[0]["id"]
    assert _events(state, "external_job_workflow_finalized")


def test_receipt_carries_no_skip_claim_when_cleanup_rejects_before_projection(
    tmp_path, monkeypatch,
):
    """复核实测 H：归属 absent 但收尾在投影之前就被拒 → 收据不得说"已跳过投影"。

    closure 是 mint-once 的，它在 cleanup 之前就铸好了；此刻投影根本没跑过，
    收据里任何"已跳过投影"的字样都会被后续复用路径永久固化成假阳性。
    """
    submission = _leftover_submission(
        tmp_path, scheduler="local", job_id="hf-job-leftover",
        resource_uid="local-hf-job-leftover",
        execution_class="diagnostic")
    state = _state_with_leftover(tmp_path, monkeypatch, submission)
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization",
        lambda *_a, **_k: {
            "status": "error", "error_type": "docker_unavailable",
            "error": "docker daemon is not reachable"})
    record_blocker(state, summary="leftover local probe cannot be cleaned",
                   requested_action="restore the container runtime")

    result = _finalize(state, submission, "operation_blocked",
                       note="cross-run leftover local probe, cleanup blocked")

    assert result["status"] == "finalized_needs_cleanup", result
    presence = manager._external_route_submission_presence(state, submission)
    assert presence["status"] == "absent"

    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert "route_projection" not in payload
    assert "skipped" not in json.dumps(payload, ensure_ascii=False)
    assert payload["route_ownership"]["status"] == "absent"
    # 投影确实一次都没跑过：追加记录与 transcript 事件都必须是空的。
    assert state.list_artifacts("external_job_route_projection_record") == []
    assert _events(state, "external_job_route_projection_skipped") == []


def test_skip_trace_survives_closure_reuse(tmp_path, monkeypatch):
    """复核实测 F：首次 fail-closed 拒 → 收据已铸；修好重试复用收据，痕迹不能丢。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("route ledger is unreadable")

    monkeypatch.setattr(
        execution_route, "describe_external_route_submission_presence", _explode)
    first = _blocked_leftover_finalize(state, submission)
    assert first["status"] == "finalized_needs_route_reconciliation", first
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    minted = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert minted["route_ownership"]["status"] == "indeterminate"
    assert "route_projection" not in minted
    assert state.list_artifacts("external_job_route_projection_record") == []

    monkeypatch.undo()
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: _health())
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)

    second = _blocked_leftover_finalize(state, submission)

    assert second["status"] == "success", second
    # 收据被复用（同 identity+outcome 只铸一次），因此它永远说不出投影的事。
    assert state.list_artifacts("external_job_operation_closure") == closures
    frozen = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert frozen == minted
    # 痕迹必须落在追加记录上，不能因为收据复用而消失。
    records = state.list_artifacts("external_job_route_projection_record")
    assert len(records) == 1
    appended = json.loads(state.read_artifact(records[0]["id"])["content"])
    assert appended["projection"]["status"] == "skipped"
    assert appended["operation_closure_artifact_id"] == closures[0]["id"]
    assert len(_events(state, "external_job_route_projection_skipped")) == 1


def test_undurable_skip_record_keeps_the_workflow_open(tmp_path, monkeypatch):
    """留痕是强保证：追加记录写不下去就不能凭 no-op 关掉作业。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)
    monkeypatch.setattr(
        manager, "_record_route_projection_skip", lambda *_a, **_k: None)
    monkeypatch.setattr(
        manager, "_external_job_is_active", lambda *_a, **_k: True)

    result = _blocked_leftover_finalize(state, submission)

    assert result["status"] == "finalized_needs_route_reconciliation", result
    assert result["route_projection"]["status"] == "error"
    assert result["route_projection"]["reason"] == (
        "route_projection_skip_record_not_durable")
    assert result["route_projection"]["skipped_projection"]["status"] == "skipped"
    assert state.list_artifacts("external_job_lifecycle") == []
    assert manager._active_output_conflicts(state, [submission["output_roots"][0]])


def test_owned_submission_projection_failure_still_blocks(tmp_path, monkeypatch):
    """本 run 拥有该提交、投影真失败 → 仍卡在 needs_route，锁不释放。"""
    state = State.new("experiment", tmp_path / "runs")
    _declare_route(state)
    reference = _submit_this_run_job(state)
    submission = {
        "status": "success", "dry_run": False,
        "execution_class": "diagnostic",
        "submitted_at": "2026-08-30T00:00:00+00:00",
        "workdir": str(tmp_path / "owned-root"),
        "output_roots": [str(tmp_path / "owned-root")],
        **{field: reference.get(field) for field in _IDENTITY_FIELDS},
    }
    state.save_artifact(
        "job_submission", "owned_submission", json.dumps(submission))
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: _health())
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {"status": "error", "reason": "route ledger unavailable"})
    monkeypatch.setattr(
        manager, "_external_job_is_active", lambda *_a, **_k: True)
    record_blocker(state, summary="owned job is blocked",
                   requested_action="fix the cluster mount")

    result = _finalize(state, submission, "operation_blocked",
                       note="owned job blocked by cluster mount outage")

    assert result["status"] == "finalized_needs_route_reconciliation"
    assert result["workflow_status"] == "awaiting_route_projection"
    assert result["route_projection"]["status"] == "error"
    assert result["route_projection"]["reason"] == (
        "route_external_projection_not_durable")
    assert result["route_projection"]["ownership"]["status"] == "present"
    assert "本 run 拥有该提交" in result["error"]
    assert state.list_artifacts("external_job_lifecycle") == []
    assert manager._active_output_conflicts(state, [submission["output_roots"][0]])
    assert _events(state, "external_job_route_projection_skipped") == []


def test_owned_submission_not_found_string_is_not_enough(tmp_path, monkeypatch):
    """判据必须机械：上游喊 route_submission_not_found 也不能放行本 run 的提交。"""
    state = State.new("experiment", tmp_path / "runs")
    _declare_route(state)
    reference = _submit_this_run_job(state)
    submission = {
        "status": "success", "dry_run": False,
        "execution_class": "diagnostic",
        "submitted_at": "2026-08-30T00:00:00+00:00",
        "workdir": str(tmp_path / "owned-root"),
        "output_roots": [str(tmp_path / "owned-root")],
        **{field: reference.get(field) for field in _IDENTITY_FIELDS},
    }
    state.save_artifact(
        "job_submission", "owned_submission", json.dumps(submission))
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: _health())
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {
            "status": "error", "reason": "route_submission_not_found"})
    record_blocker(state, summary="owned job is blocked",
                   requested_action="fix the cluster mount")

    result = _finalize(state, submission, "operation_blocked",
                       note="owned job blocked by cluster mount outage")

    assert result["status"] == "finalized_needs_route_reconciliation"
    assert result["route_projection"]["status"] == "error"
    assert result["route_projection"]["ownership"]["status"] == "present"
    assert state.list_artifacts("external_job_lifecycle") == []
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert "route_projection" not in payload
    assert payload["route_ownership"]["status"] == "present"
    assert state.list_artifacts("external_job_route_projection_record") == []


def test_unreadable_ownership_fails_closed(tmp_path, monkeypatch):
    """归属确认不了（路线读取异常）→ 保持现状拒绝，并说明下一步。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("route ledger is unreadable")

    monkeypatch.setattr(
        execution_route, "describe_external_route_submission_presence", _explode)
    monkeypatch.setattr(
        manager, "_external_job_is_active", lambda *_a, **_k: True)

    result = _blocked_leftover_finalize(state, submission)

    assert result["status"] == "finalized_needs_route_reconciliation"
    assert result["route_projection"]["upstream_reason"] == (
        "route_submission_not_found")
    ownership = result["route_projection"]["ownership"]
    assert ownership["status"] == "indeterminate"
    assert ownership["reason"] == "route_presence_probe_failed"
    # 文案必须给出对跨 run 遗留真正走得通的下一步，而不是空许诺式的"修复后重试"。
    assert "无法正面确认该 job 是否属于本 run 的路线" in result["error"]
    assert state.list_artifacts("external_job_lifecycle") == []
    assert manager._active_output_conflicts(state, [submission["output_roots"][0]])
    closures = state.list_artifacts("external_job_operation_closure")
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert "route_projection" not in payload
    assert payload["route_ownership"]["status"] == "indeterminate"
    assert payload["route_ownership"]["reason"] == "route_presence_probe_failed"


def test_identity_trace_without_submission_is_indeterminate(
    tmp_path, monkeypatch,
):
    """路线事件里出现过该身份却匹配不上提交 → 说不清归属，不许当遗留处理。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)
    state.append_transcript(
        "route_step_external_identity_note", job_id=submission["job_id"])

    presence = execution_route.describe_external_route_submission_presence(
        state, scheduler=submission["scheduler"], job_id=submission["job_id"],
        namespace=submission["namespace"],
        submission_nonce=submission["submission_nonce"])
    assert presence["status"] == "indeterminate"
    assert presence["reason"] == "route_identity_trace_without_submission"
    assert "route_step_external_identity_note" in presence["traces"]

    result = _blocked_leftover_finalize(state, submission)

    assert result["status"] == "finalized_needs_route_reconciliation"
    assert result["route_projection"]["ownership"]["reason"] == (
        "route_identity_trace_without_submission")
    assert state.list_artifacts("external_job_lifecycle") == []


def test_cross_run_leftover_does_not_loosen_n5_or_science_gates(
    tmp_path, monkeypatch,
):
    """遗留身份不是免死金牌：宣称成功/失败、缺 note、缺独立 blocker 一律拒。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    claimed = _finalize(state, submission, "operation_completed")
    assert claimed["status"] == "error"
    assert claimed["error_code"] == "simulation_job_requires_analyzed_outcome"

    failed = _finalize(state, submission, "operation_failed",
                       disputed_execution_class="diagnostic")
    assert failed["status"] == "error"
    assert failed["error_code"] == "simulation_job_requires_analyzed_outcome"

    undisputed = _finalize(state, submission, "operation_blocked",
                           note="cross-run leftover probe")
    assert undisputed["status"] == "error"
    assert undisputed["error_code"] == "class_dispute_requires_explicit_claim"

    no_blocker = _finalize(
        state, submission, "operation_blocked",
        note="cross-run leftover probe",
        disputed_execution_class="diagnostic")
    assert no_blocker["status"] == "error"
    assert no_blocker["error_code"] in {
        "operation_blocked_requires_recorded_blocker",
        "class_disputed_requires_independent_blocker",
    }

    record_blocker(state, summary="leftover probe cannot be analyzed",
                   requested_action="fix the derivation rule")
    no_note = _finalize(state, submission, "operation_blocked", note="",
                        disputed_execution_class="diagnostic")
    assert no_note["status"] == "error"
    assert no_note["error_code"] == "operation_blocked_note_required"

    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []
    assert _events(state, "external_job_route_projection_skipped") == []


def test_cross_run_leftover_science_job_keeps_full_evidence_chain(
    tmp_path, monkeypatch,
):
    """遗留的科学作业照样只收 analyzed_*，证据链齐了才走 no-op 投影收尾。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    unfrozen = state.save_artifact(
        "experiment_log", "unfrozen_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    rejected = _finalize(state, submission, "analyzed_success", unfrozen["id"])
    assert rejected["status"] == "error"
    assert "frozen" in rejected["error"]

    log = state.save_artifact(
        "experiment_log", "frozen_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    state.mark_frozen(log["id"])
    result = _finalize(state, submission, "analyzed_inconclusive", log["id"])

    assert result["status"] == "success", result
    assert result["workflow_status"] == "finalized"
    assert result["route_projection"]["status"] == "skipped"
    assert result["route_projection"]["external_identity"]["job_id"] == (
        submission["job_id"])
    assert state.list_artifacts("external_job_operation_closure") == []
    assert len(state.list_artifacts("external_job_lifecycle")) == 1
    assert len(_events(state, "external_job_route_projection_skipped")) == 1
    records = state.list_artifacts("external_job_route_projection_record")
    assert len(records) == 1
    appended = json.loads(state.read_artifact(records[0]["id"])["content"])
    assert appended["operation_closure_artifact_id"] == ""
    assert appended["evidence_artifact_id"] == log["id"]


def test_run_without_declared_external_route_keeps_legacy_projection(
    tmp_path, monkeypatch,
):
    """没声明带 external 效应的路线时不得凭空写"跳过投影"的痕迹。"""
    submission = _leftover_submission(tmp_path, execution_class="diagnostic")
    state = State.new("experiment", tmp_path / "runs")
    state.save_artifact(
        "job_submission", "leftover_submission", json.dumps(submission))
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: _health())
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)
    record_blocker(state, summary="leftover probe is blocked",
                   requested_action="fix the derivation rule")

    presence = manager._external_route_submission_presence(state, submission)
    assert presence["status"] == "not_applicable"

    result = _finalize(state, submission, "operation_blocked",
                       note="no route declared in this run")

    assert result["status"] == "success", result
    assert result["route_projection"].get("status") in {
        "success", "not_applicable"}
    assert result["route_projection"].get("reason") != (
        "cross_run_leftover_no_submission_in_current_route")
    closures = state.list_artifacts("external_job_operation_closure")
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert "route_projection" not in payload
    assert payload["route_ownership"]["status"] == "not_applicable"
    assert _events(state, "external_job_route_projection_skipped") == []
    assert state.list_artifacts("external_job_route_projection_record") == []


def test_absent_ownership_never_promises_a_repair_this_run_cannot_make(
    tmp_path, monkeypatch,
):
    """复核实测 C：归属 absent、上游原因不是 not_found → 文案按归属结论分流。

    identity 来自上一个 run 写的 handoff，本 run 的路线里没有这一步可修；
    "修复后重试 finalize_external_job" 对它是一条走不通的许诺。
    """
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {
            "status": "error",
            "reason": "route_local_container_identity_incomplete"})
    monkeypatch.setattr(
        manager, "_external_job_is_active", lambda *_a, **_k: True)

    result = _blocked_leftover_finalize(state, submission)

    assert result["status"] == "finalized_needs_route_reconciliation", result
    assert result["route_projection"]["ownership"]["status"] == "absent"
    assert "跨 run 遗留" in result["error"]
    assert "route_local_container_identity_incomplete" in result["error"]
    assert "补齐该遗留作业" in result["error"]
    # 不许诺本 run 修不了的东西：不能只说"修复后重试"。
    assert "修复后重试 finalize_external_job" not in result["error"]
    # no-op 仍然只对"上游确实找不到提交"开放：这条路不放行。
    assert state.list_artifacts("external_job_route_projection_record") == []
    assert state.list_artifacts("external_job_lifecycle") == []
    assert manager._active_output_conflicts(state, [submission["output_roots"][0]])


def test_cancel_projection_is_a_noop_for_cross_run_leftover(
    tmp_path, monkeypatch,
):
    """cancel 同族：跨 run 遗留的取消投影同样按归属裁决，并事后追加留痕。"""
    submission = _leftover_submission(tmp_path)
    state = _state_with_leftover(tmp_path, monkeypatch, submission)

    projection = manager._project_external_cancellation(
        state, submission, evidence_artifact_id="cancellation_intent__x")

    assert projection["status"] == "skipped", projection
    assert projection["reason"] == (
        "cross_run_leftover_no_submission_in_current_route")
    assert projection["upstream_reason"] == "route_submission_not_found"
    records = state.list_artifacts("external_job_route_projection_record")
    assert len(records) == 1
    appended = json.loads(state.read_artifact(records[0]["id"])["content"])
    assert appended["closure_kind"] == "cancel"
    assert appended["outcome"] == "cancelled"
    assert appended["evidence_artifact_id"] == "cancellation_intent__x"
    skipped = _events(state, "external_job_route_projection_skipped")
    assert len(skipped) == 1
    assert skipped[0]["closure_kind"] == "cancel"


def test_cancel_projection_does_not_loosen_owned_submissions(
    tmp_path, monkeypatch,
):
    """本 run 拥有该提交的取消投影一分不放宽，文案仍指向可修的那一步。"""
    state = State.new("experiment", tmp_path / "runs")
    _declare_route(state)
    reference = _submit_this_run_job(state)
    record = {field: reference.get(field) for field in _IDENTITY_FIELDS}
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {
            "status": "error", "reason": "route_submission_not_found"})

    projection = manager._project_external_cancellation(
        state, record, evidence_artifact_id="cancellation_intent__y")

    assert projection["status"] == "error"
    assert projection["ownership"]["status"] == "present"
    assert state.list_artifacts("external_job_route_projection_record") == []

    response = manager._cancellation_route_failure_response(
        state, record, cancel_result={"status": "success"},
        evidence_artifact_id="cancellation_intent__y",
        route_projection=projection, idempotent=False)
    assert response["status"] == "cancelled_needs_route_reconciliation"
    assert "取消结果已确认。" in response["error"]
    assert "本 run 拥有该提交" in response["error"]
    assert "重试 cancel_job" in response["error"]
