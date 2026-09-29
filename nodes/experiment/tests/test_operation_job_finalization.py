"""operation 型 external job 的终态通道回归测试（无 docker 依赖）。

钉死的不变量：
- simulation 类收尾一分不松：frozen 非自动 experiment_log + 完整 identity refs；
- execution_class 只认 submit 时持久化 + 交叉核验，caller/finalize 自述不参与；
- operation_completed 无正面机械证据必拒（含"旧产物自证放行"的 mtime 堵点）；
- external_job_operation_closure 经通用 save_artifact 铸造被防伪写入门拦下；
- 作业收尾按作业自己的 execution_class 分流，不读 run 模式（#879 后续解耦）；
- class_unverified 搁浅出口只允许 operation_blocked（需 note + 已登记 blocker）；
- 核验一致的 simulation 作业永不接受 operation_completed/operation_failed，但
  operation_blocked 有出路（N-5）：必须显式声明 disputed_execution_class、必须有
  一条非框架代记的 blocker，closure 标 class_disputed 并留下不自动消解的争议 blocker；
- 「非框架代记 blocker」是**所有** operation_blocked 让步路的公共代价（N-5R）：
  交叉核验失败的搁浅路不得比核验一致的争议路更便宜，且任何 blocked 收尾之后
  account 上必须仍挂着 blocker，不许用一条随即被判 stale 清掉的 blocker 换收尾；
- 搁浅路的 closure 与争议路对称可审计：刻下 claimed/persisted 两端与 verified=False。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.project_workspace import _NODE_WORKSPACES
from core.state import State
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import resource_manager as manager


def _save_frozen(state, artifact_type, name, content, metadata=None):
    """夹具级冻结：save + mark_frozen。冻结的事实只出自账本的 freeze 行，save 行里的
    frozen 键会被剥掉（core.ledger.FREEZE_OWNED_METADATA）。返回 save_artifact 的结果。"""
    saved = state.save_artifact(artifact_type, name, content, metadata=metadata)
    state.mark_frozen(saved["id"])
    return saved


_IDENTITY_FIELDS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id",
    "process_start_ticks", "container_runtime_id",
)
# 本分支沿用的名字；与 main 的 _IDENTITY_FIELDS 是同一组身份字段。
_V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS = _IDENTITY_FIELDS


def _submission(**overrides):
    payload = {
        "status": "success",
        "dry_run": False,
        "scheduler": "slurm",
        "job_id": "9001",
        "submission_nonce": "nonce-op-1",
        "execution_class": "diagnostic",
        "submitted_at": "2026-08-30T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _health(**overrides):
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


def _diagnostic_completion_view(state, submission, health):
    return manager._operation_completion_postconditions(
        state, submission, health, "diagnostic")


def _external_job_ref(payload):
    return {
        field: payload[field]
        for field in _V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS
        if payload.get(field) not in {None, ""}
    }


def _prepared_state(tmp_path, submission):
    state = State.new("experiment", tmp_path)
    state.save_artifact("job_submission", "op_submission", json.dumps(submission))
    return state


def _patch_pipeline(monkeypatch, health, route_calls=None):
    monkeypatch.setattr(
        manager, "probe_external_job_health", lambda *_a, **_k: dict(health))
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **kwargs: (
            (route_calls.append(kwargs) if route_calls is not None else None)
            or {"status": "success"}
        ))


def _mutate_record_execution_class(monkeypatch, execution_class):
    """让受管记录行的自述与持久化 payload 不一致（交叉核验必失败）。

    `_external_job_record` 的候选集包含模型可写的 task 行，行自述被改写是真实
    可达的状态；持久化 payload 仍是唯一权威。
    """
    real = manager._external_job_record

    def _patched(*args, **kwargs):
        row = real(*args, **kwargs)
        return None if row is None else {**row, "execution_class": execution_class}

    monkeypatch.setattr(manager, "_external_job_record", _patched)


def _finalize(state, submission, outcome, evidence_artifact_id="", note="",
              disputed_execution_class=""):
    return asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"],
        evidence_artifact_id, outcome, note=note,
        disputed_execution_class=disputed_execution_class))


def test_simulation_class_job_rejects_operation_outcomes(tmp_path, monkeypatch):
    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "simulation_job_requires_analyzed_outcome"
    assert "analyzed_success|analyzed_failure|analyzed_inconclusive" in result["error"]
    assert result["job_execution_class"] == "simulation"
    assert result["class_unverified"] is False
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_operation_class_job_rejects_analyzed_outcomes(tmp_path, monkeypatch):
    """probe 不能借科学 log 洗白：diagnostic 类 job 拒收 analyzed_*。"""
    submission = _submission(execution_class="toolchain_build")
    state = _prepared_state(tmp_path, submission)
    log = _save_frozen(
        state, "experiment_log", "toolchain_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    _patch_pipeline(monkeypatch, _health())

    result = _finalize(state, submission, "analyzed_success", log["id"])

    assert result["status"] == "error"
    assert result["error_code"] == "operation_job_rejects_analyzed_outcome"
    assert "operation_completed|operation_failed|operation_blocked" in result["error"]
    assert result["job_execution_class"] == "toolchain_build"
    assert state.list_artifacts("external_job_lifecycle") == []


def test_operation_branch_records_caller_evidence_as_a_witness(tmp_path, monkeypatch):
    """调用方传了科学产物：不再拒，改为如实刻进收据当见证。

    反转自 test_operation_branch_rejects_evidence_artifact_id。原来这里直拒，
    与「运维 run 必须提供 evidence_artifact_id」那道墙互为死结：一边要求给、
    另一边收到就拒，2026-09-08 活体里模型正是在这两句之间来回撞。产物不参与
    任何判定，判定权仍在机械证据矩阵；出处校验没有丢，身份诊断照跑并原样入账。
    """
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    log = _save_frozen(
        state, "experiment_log", "caller_log", "text",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    result = _finalize(state, submission, "operation_completed", log["id"])

    assert result["status"] == "success", result
    assert result["caller_evidence"]["artifact_id"] == log["id"]
    assert result["caller_evidence"]["binding"] == "matched"
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1, "终态仍由机械证据铸造，不是由调用方产物"


def test_adopted_cross_run_job_is_marked_in_the_receipt(tmp_path, monkeypatch):
    """替别的 run 关掉遗留作业时，收据必须读得出「执行发生在别处」。

    任务清单是项目级的：框架把未收尾作业的跟进 task 按 owner 注入同项目的**任何**
    后续 experiment run。作业层解耦之后新 run 真的关得掉这些历史待办了（2026-09-10
    活体里一次 run 顺手关掉了另外两个 session 的遗留作业）。关掉本身是对的——开放
    作业不该永远悬空——但一条终态如果不是本 run 自己的执行，账本上必须看得出来。

    任务清单的作用域与生命周期归框架侧，本节点只负责如实记账。
    """
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    # 模拟这条作业是从项目级任务清单收养来的：来源 run 与本 run 不同
    monkeypatch.setattr(
        manager, "_external_job_record",
        lambda *_a, **_k: {**submission,
                           "handoff_origin_run_id": "1788922308-682e75"})
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "success", result
    assert result["adopted_from_run_id"] == "1788922308-682e75", result
    closures = state.list_artifacts("external_job_operation_closure")
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["adopted_from_run_id"] == "1788922308-682e75", (
        "收养事实必须随收据冻结，事后审计才读得出这条终态不是本 run 的执行")


def test_unverified_class_whose_authority_is_simulation_never_launders(
    tmp_path, monkeypatch,
):
    """交叉核验不一致、而受管台账的权威值就是 simulation：两种正面词表一律严格。

    这一格不是「类别不明」，是「调用方主张与权威值相反」。用运维词表关掉它就是把
    科学作业洗白，调用方附的冻结日志也解不开——见证证明的是它读过输出，不能证明
    这个作业不是科学模拟。三段写在同一个函数里：拆开后变异只砍一半会被另一半的
    绿掩盖。
    """
    submission = _local_submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    # record 自述 diagnostic，与台账里持久化的 simulation 对不上 → cross_check_mismatch
    _mutate_record_execution_class(monkeypatch, "diagnostic")
    log = _save_frozen(
        state, "experiment_log", "caller_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    _patch_pipeline(monkeypatch, _health(
        health_state="failure_signal",
        error_evidence=["Traceback (most recent call last):"],
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 1}}}))

    # ① 宣称成功 + 见证：仍拒
    with_witness = _finalize(state, submission, "operation_completed", log["id"])
    assert with_witness["status"] == "error", with_witness
    assert with_witness["error_code"] == "operation_execution_class_unverified"

    # ② 宣称失败：同样拒——权威值是 simulation，失败也要走科学词表
    failed = _finalize(state, submission, "operation_failed")
    assert failed["status"] == "error", failed
    assert failed["error_code"] == "operation_execution_class_unverified"

    # ③ 一份收据都不该被铸出来
    assert state.list_artifacts("external_job_operation_closure") == []


def test_receipt_minted_under_caller_evidence_survives_a_second_finalize(
    tmp_path, monkeypatch,
):
    """凭调用方见证铸出的收据，重读时必须仍然立得住。

    这条钉的是第一版解耦引入的回归：caller_evidence 解开了 class_unverified 的
    fail-closed，收据铸出来了，但见证没有随收据冻结，于是重校验时那把尺又变回
    严格，同一份收据被自己判为 operation_closure_receipt_invalid。撞死的位置正是
    「投影失败 → 改路线 → 重试收尾」这条主路径，也就是本 PR 要修的那个格子。
    """
    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    log = _save_frozen(
        state, "experiment_log", "caller_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    first = _finalize(state, submission, "operation_completed", log["id"])
    assert first["status"] == "success", first
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["caller_evidence"]["binding"] == "matched", (
        "见证必须随收据一起冻结，否则重读时复原不出当初的放行依据")

    # 重试收尾：必须复用同一份收据，而不是把它判成无效
    second = _finalize(state, submission, "operation_completed", log["id"])
    assert second["status"] == "success", second
    assert len(state.list_artifacts("external_job_operation_closure")) == 1


def test_caller_evidence_binding_mismatch_is_recorded_not_rejected(
    tmp_path, monkeypatch,
):
    """产物与本作业身份对不上：照样收尾，但见证如实记 mismatch。"""
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    other = dict(submission, job_id="hf-job-someone-else")
    log = _save_frozen(
        state, "experiment_log", "foreign_log", "text",
        metadata={"external_job_refs": [_external_job_ref(other)]})
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    result = _finalize(state, submission, "operation_completed", log["id"])

    assert result["status"] == "success", result
    assert result["caller_evidence"]["binding"] == "mismatch"
    assert result["caller_evidence"]["diagnostic"]


def test_operation_completed_requires_positive_mechanical_evidence(
    tmp_path, monkeypatch,
):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_completed_positive_evidence_missing"
    assert 'outcome="operation_failed"' in result["error"]
    assert "report_blocker" in result["error"]
    assert 'outcome="operation_blocked"' in result["error"]
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_operation_completed_with_zero_exit_mints_closure_and_finalizes(
    tmp_path, monkeypatch,
):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(
        monkeypatch,
        _health(scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}),
        route_calls,
    )

    result = _finalize(state, submission, "operation_completed",
                       note="deps probe done")

    assert result["status"] == "success"
    assert result["workflow_status"] == "finalized"
    assert result["outcome"] == "operation_completed"
    assert result["job_execution_class"] == "diagnostic"
    assert result["class_unverified"] is False
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    assert result["operation_closure_artifact_id"] == closures[0]["id"]
    assert result["evidence_artifact_id"] == closures[0]["id"]
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert sorted(payload["identity"]) == sorted(
        _V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS
    )
    assert payload["identity"]["submission_nonce"] == "nonce-op-1"
    assert payload["execution_class"] == "diagnostic"
    assert payload["class_unverified"] is False
    assert payload["outcome"] == "operation_completed"
    assert payload["note"] == "deps probe done"
    assert payload["run_id"] == state.run_id
    assert payload["health"]["exit_code"] == 0
    assert len(route_calls) == 1
    assert route_calls[0]["domain_outcome"] == "operation_completed"
    assert route_calls[0]["evidence_artifact_id"] == closures[0]["id"]
    lifecycles = state.list_artifacts("external_job_lifecycle")
    assert len(lifecycles) == 1
    lifecycle = json.loads(state.read_artifact(lifecycles[0]["id"])["content"])
    assert lifecycle["lifecycle_status"] == "finalized"


@pytest.mark.parametrize("mtime_offset_s, expected_status", [
    (120.0, "success"),
    (-120.0, "error"),
])
def test_operation_completed_completion_paths_must_be_fresh(
    tmp_path, monkeypatch, mtime_offset_s, expected_status,
):
    """产物必须晚于 submitted_at：旧产物 exists 不是本次执行的完成证据。"""
    from datetime import datetime, timezone

    submission = _submission(
        health_contract={"completion_paths": ["/tmp/op/out.done"]}
    )
    state = _prepared_state(tmp_path, submission)
    submitted_epoch = datetime.fromisoformat(
        submission["submitted_at"]).timestamp()
    snapshot = {"path": "/tmp/op/out.done", "exists": True,
                "mtime_epoch_s": submitted_epoch + mtime_offset_s}
    _patch_pipeline(monkeypatch, _health(completion_paths=[snapshot]))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == expected_status
    if expected_status == "error":
        assert result["error_code"] == "operation_completed_positive_evidence_missing"
        assert "mtime" in result["error"]
        assert state.list_artifacts("external_job_operation_closure") == []
    else:
        assert result["job_execution_class"] == "diagnostic"
        assert len(state.list_artifacts("external_job_operation_closure")) == 1


def test_operation_completed_rejects_error_evidence(tmp_path, monkeypatch):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        error_evidence=[{"path": "/tmp/op/err.log", "marker": "fatal error"}],
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}},
    ))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_completed_positive_evidence_missing"
    assert "error_evidence" in result["error"]


def test_operation_failed_requires_negative_evidence(tmp_path, monkeypatch):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    clean = _finalize(state, submission, "operation_failed")

    assert clean["status"] == "error"
    assert clean["error_code"] == "operation_failed_negative_evidence_missing"
    assert 'outcome="operation_completed"' in clean["error"]

    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 7}}}))
    failed = _finalize(state, submission, "operation_failed")

    assert failed["status"] == "success"
    assert failed["outcome"] == "operation_failed"
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["health"]["exit_code"] == 7


def test_operation_blocked_requires_note_and_recorded_blocker(
    tmp_path, monkeypatch,
):
    from core.blockers import record_blocker

    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())

    no_note = _finalize(state, submission, "operation_blocked")
    assert no_note["status"] == "error"
    assert no_note["error_code"] == "operation_blocked_note_required"
    assert 'note="<阻塞原因>"' in no_note["error"]

    no_blocker = _finalize(state, submission, "operation_blocked",
                           note="scheduler lost the account")
    assert no_blocker["status"] == "error"
    assert no_blocker["error_code"] == "operation_blocked_requires_recorded_blocker"
    assert "report_blocker" in no_blocker["error"]

    record_blocker(state, summary="scheduler account is unavailable",
                   requested_action="restore slurm account access")
    blocked = _finalize(state, submission, "operation_blocked",
                        note="scheduler lost the account")
    assert blocked["status"] == "success"
    assert blocked["outcome"] == "operation_blocked"
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["class_unverified"] is False
    assert "class_verification" not in payload
    # blocked 收尾不得让账本变空：解锁用的那条 blocker 必须活过本次对账。
    assert state.hook_state["blockers"]


def test_class_unverified_job_rejects_only_unbacked_completed(tmp_path, monkeypatch):
    """身份未证：只有「宣称成功且拿不出匹配的冻结日志」才拒。

    收窄自 test_class_unverified_job_rejects_completed_and_failed。fail-closed 的
    理由是「收尾成功即销毁沙箱，降格等于允许先毁证后补析」，而这个风险只在正面
    宣称上成立：一个 exit≠0 的作业没有可供补析的结果，失败是记录不是拒绝（F11）。
    """
    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))

    # 宣称成功且无证据 → 仍拒，且出口写全
    rejected = _finalize(state, submission, "operation_completed")
    assert rejected["status"] == "error"
    assert rejected["error_code"] == "operation_execution_class_unverified"
    assert "report_blocker" in rejected["error"]
    assert 'outcome="operation_blocked"' in rejected["error"]
    assert state.list_artifacts("external_job_operation_closure") == []


def test_class_unverified_failure_closes_and_marks_it_unverified(
    tmp_path, monkeypatch,
):
    """身份未证的失败作业直接收尾，收据里如实刻明未核验。"""
    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        health_state="failure_signal",
        error_evidence=["Traceback (most recent call last):"],
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 1}}}))

    result = _finalize(state, submission, "operation_failed")

    assert result["status"] == "success", result
    assert result["class_unverified"] is True
    assert len(state.list_artifacts("external_job_operation_closure")) == 1


def test_class_unverified_blocked_exit_requires_recorded_blocker(
    tmp_path, monkeypatch,
):
    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())

    rejected = _finalize(state, submission, "operation_blocked",
                         note="job is dead but its class cannot be verified")
    assert rejected["status"] == "error"
    assert rejected["error_code"] == "operation_blocked_requires_recorded_blocker"


def test_class_unverified_blocked_exit_rejects_framework_blockers(
    tmp_path, monkeypatch,
):
    """N-5R 场景 (A)：payload 缺 execution_class 的搁浅路不比争议路便宜。

    框架为 external job 对账代记的 handoff blocker 会在同一次收尾末尾被判 stale
    清掉，拿它解锁等于零成本关掉一个身份未证的作业，run 最终不带任何 blocker。
    """
    from core.blockers import record_blocker

    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    record_blocker(
        state, summary="framework handoff bookkeeping",
        requested_action="framework reconciliation",
        reported_by=manager._external_job_handoff_reported_by(submission))

    rejected = _finalize(state, submission, "operation_blocked",
                         note="job is dead but its class cannot be verified")

    assert rejected["status"] == "error"
    assert rejected["error_code"] == "operation_blocked_requires_independent_blocker"
    assert "report_blocker" in rejected["error"]
    # 搁浅路不涉及类别争议，纠偏调用里不得出现 disputed_execution_class。
    assert "disputed_execution_class" not in rejected["error"]
    assert rejected["class_unverified"] is True
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_cross_check_mismatch_blocked_exit_rejects_framework_blockers(
    tmp_path, monkeypatch,
):
    """N-5R 场景 (B)：record 行自述被改写导致核验不一致，同样要非框架 blocker。"""
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    _mutate_record_execution_class(monkeypatch, "diagnostic")
    record_blocker(
        state, summary="framework handoff bookkeeping",
        requested_action="framework reconciliation",
        reported_by=manager._external_job_handoff_reported_by(submission))

    rejected = _finalize(state, submission, "operation_blocked",
                         note="probe row says diagnostic, receipt says simulation")

    assert rejected["status"] == "error"
    assert rejected["error_code"] == "operation_blocked_requires_independent_blocker"
    assert rejected["class_unverified"] is True
    assert rejected["claimed_execution_class"] == "diagnostic"
    assert rejected["persisted_execution_class"] == "simulation"
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_cross_check_mismatch_blocked_closure_records_both_class_claims(
    tmp_path, monkeypatch,
):
    """搁浅路放行时 closure 与争议路对称：刻下 claimed/persisted 与 verified=False。"""
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    _mutate_record_execution_class(monkeypatch, "diagnostic")
    record_blocker(
        state, summary="managed row and submission receipt disagree on the class",
        requested_action="reconcile the execution_class of this job")

    result = _finalize(state, submission, "operation_blocked",
                       note="class cannot be cross-checked; no science to freeze")

    assert result["status"] == "success"
    assert result["class_unverified"] is True
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["class_verification"] == {
        "verified": False,
        "claimed_execution_class": "diagnostic",
        "persisted_execution_class": "simulation",
        "reason": "execution_class_cross_check_mismatch",
    }
    assert state.hook_state["blockers"]


def test_class_unverified_blocked_exit_records_fact_and_releases(
    tmp_path, monkeypatch,
):
    from core.blockers import record_blocker

    submission = _submission()
    submission.pop("execution_class")
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(monkeypatch, _health(), route_calls)
    record_blocker(state, summary="dead external job with unverified class",
                   requested_action="manual review of runtime outputs")

    result = _finalize(state, submission, "operation_blocked",
                       note="job is dead but its class cannot be verified")

    assert result["status"] == "success"
    assert result["outcome"] == "operation_blocked"
    assert result["class_unverified"] is True
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["class_unverified"] is True
    assert payload["class_verification"]["verified"] is False
    assert payload["class_verification"]["reason"] == (
        "submission_payload_execution_class_unrecognized")
    assert payload["note"]
    assert route_calls[0]["domain_outcome"] == "operation_blocked"
    lifecycles = state.list_artifacts("external_job_lifecycle")
    assert len(lifecycles) == 1
    # 让步不得换来一本空账：模型登记的那条 blocker 必须活过本次对账。
    assert state.hook_state["blockers"]


@pytest.mark.parametrize("outcome", ["operation_completed", "operation_failed"])
def test_verified_simulation_never_accepts_operation_success_or_failure(
    tmp_path, monkeypatch, outcome,
):
    """核验一致的 simulation 作业永远不许被宣称 operation 型成功/失败。

    连"已登记 blocker + 非空 note + 显式类别争议 + 机械证据齐全"都不能松这一格：
    那是拿运维口径替科学作业下结论。
    """
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}}))
    record_blocker(state, summary="probe was misclassified as simulation",
                   requested_action="re-derive the execution class")

    result = _finalize(state, submission, outcome, note="probe finished",
                       disputed_execution_class="diagnostic")

    assert result["status"] == "error"
    assert result["error_code"] == "simulation_job_requires_analyzed_outcome"
    assert result["job_execution_class"] == "simulation"
    assert result["class_unverified"] is False
    assert "class_disputed" not in result
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []
    assert not any(
        str(item.get("reported_by") or "").startswith(
            manager._EXTERNAL_JOB_CLASS_DISPUTED_PREFIX)
        for item in state.hook_state["blockers"])


def test_verified_simulation_blocked_exit_requires_explicit_dispute_claim(
    tmp_path, monkeypatch,
):
    """让步路不能被"顺手撞上"：必须显式声明主张的类别，且只能是 operation 类。"""
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    record_blocker(state, summary="probe derived as simulation",
                   requested_action="fix the derivation")

    silent = _finalize(state, submission, "operation_blocked",
                       note="environment probe, not a simulation")
    assert silent["status"] == "error"
    assert silent["error_code"] == "class_dispute_requires_explicit_claim"
    assert "class_disputed" not in silent

    # 主张 simulation 等于没有争议：这条路不接受与持久化值相同的类别。
    echoed = _finalize(state, submission, "operation_blocked",
                       note="environment probe, not a simulation",
                       disputed_execution_class="simulation")
    assert echoed["error_code"] == "class_dispute_requires_explicit_claim"
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_verified_simulation_blocked_exit_requires_note_and_blocker(
    tmp_path, monkeypatch,
):
    """误判成 simulation 的作业走 operation_blocked，仍受既有证据门约束。"""
    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())

    no_note = _finalize(state, submission, "operation_blocked",
                        disputed_execution_class="diagnostic")
    assert no_note["status"] == "error"
    assert no_note["error_code"] == "operation_blocked_note_required"
    assert no_note["class_disputed"] is True
    assert no_note["disputed_execution_class"] == "diagnostic"
    assert no_note["persisted_execution_class"] == "simulation"

    no_blocker = _finalize(state, submission, "operation_blocked",
                           note="install_deps_probe misclassified as simulation",
                           disputed_execution_class="diagnostic")
    assert no_blocker["status"] == "error"
    assert no_blocker["error_code"] == "operation_blocked_requires_recorded_blocker"
    assert state.list_artifacts("external_job_operation_closure") == []


def test_class_dispute_rejects_framework_reconciliation_blockers(
    tmp_path, monkeypatch,
):
    """补偿控制不得被框架自己代记的 blocker 满足（否则解锁完就被清掉）。

    handoff / needs_route 这类 blocker 都由框架在同一次收尾末尾判 stale 清除：
    拿它们当"已登记 blocker"，等于零成本关掉一个核验一致的 simulation 作业，
    而且 run 最终不带任何 blocker。
    """
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    for reported_by in (
        manager._external_job_handoff_reported_by(submission),
        manager._finalized_needs_route_reported_by(submission),
    ):
        record_blocker(state, summary="framework bookkeeping",
                       requested_action="framework reconciliation",
                       reported_by=reported_by)

    rejected = _finalize(state, submission, "operation_blocked",
                         note="environment probe, not a simulation",
                         disputed_execution_class="diagnostic")

    assert rejected["status"] == "error"
    assert rejected["error_code"] == "class_disputed_requires_independent_blocker"
    assert "report_blocker" in rejected["error"]
    assert state.list_artifacts("external_job_operation_closure") == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_verified_simulation_blocked_exit_mints_class_disputed_closure(
    tmp_path, monkeypatch,
):
    """N-5：一致地标错的作业有出路，且这次让步在账本里完全可审计。"""
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(monkeypatch, _health(), route_calls)
    record_blocker(
        state,
        summary="install_deps_probe was derived as simulation and has no science",
        requested_action="fix the execution_class derivation for probe jobs")

    result = _finalize(
        state, submission, "operation_blocked",
        note="environment probe, not a scientific simulation; no log to freeze",
        disputed_execution_class="diagnostic")

    assert result["status"] == "success"
    assert result["outcome"] == "operation_blocked"
    assert result["job_execution_class"] == "simulation"
    assert result["class_unverified"] is False
    assert result["class_disputed"] is True
    # 争议的两端必须不同，审计员才读得出争议内容。
    assert result["disputed_execution_class"] == "diagnostic"
    assert result["persisted_execution_class"] == "simulation"
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["class_disputed"] is True
    assert payload["execution_class"] == "simulation"
    assert payload["persisted_execution_class"] == "simulation"
    assert payload["disputed_execution_class"] == "diagnostic"
    assert payload["class_unverified"] is False
    assert payload["note"]
    assert route_calls[0]["domain_outcome"] == "operation_blocked"
    assert len(state.list_artifacts("external_job_lifecycle")) == 1

    # 让步不得静默消失：收尾后账本上仍挂着一条框架自有、不自动消解的争议 blocker。
    disputed_reported_by = manager._external_job_class_disputed_reported_by(
        submission)
    assert payload["class_dispute_blocker_reported_by"] == disputed_reported_by
    standing = [item for item in state.hook_state["blockers"]
                if item.get("reported_by") == disputed_reported_by]
    assert len(standing) == 1
    assert standing[0]["persisted_execution_class"] == "simulation"
    assert standing[0]["disputed_execution_class"] == "diagnostic"


def test_class_disputed_blocker_survives_external_job_reconcilers(
    tmp_path, monkeypatch,
):
    """争议 blocker 不属于任何 external-job 对账前缀，收尾对账不会把它清掉。"""
    from core.blockers import record_blocker

    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    record_blocker(state, summary="probe derived as simulation",
                   requested_action="fix the derivation")
    record_blocker(state, summary="framework handoff bookkeeping",
                   requested_action="framework reconciliation",
                   reported_by=manager._external_job_handoff_reported_by(
                       submission))

    result = _finalize(state, submission, "operation_blocked",
                       note="environment probe, not a simulation",
                       disputed_execution_class="toolchain_build")

    assert result["status"] == "success"
    reported = {item.get("reported_by") for item in state.hook_state["blockers"]}
    # 框架的 handoff blocker 照旧被判 stale 清掉；争议 blocker 必须留下。
    assert manager._external_job_handoff_reported_by(submission) not in reported
    assert manager._external_job_class_disputed_reported_by(
        submission) in reported


def test_verified_simulation_blocked_exit_still_rejects_evidence_artifact(
    tmp_path, monkeypatch,
):
    """这条出路不是科学洗白通道：产物只作见证，不改变终态判定。"""
    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    log = _save_frozen(
        state, "experiment_log", "laundered_log", "text",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    _patch_pipeline(monkeypatch, _health())

    result = _finalize(state, submission, "operation_blocked", log["id"],
                       note="probe blocked",
                       disputed_execution_class="diagnostic")

    # 不再直拒：产物入账为见证，终态仍由机械证据决定（此处缺 blocker 故仍拒）
    assert result["status"] == "error"
    assert result["error_code"] != "operation_finalization_rejects_evidence_artifact"
    assert state.list_artifacts("external_job_operation_closure") == []


def test_true_operation_job_blocked_closure_is_not_disputed(
    tmp_path, monkeypatch,
):
    """真 operation 类作业行为不变：closure 明确标 class_disputed=false。"""
    from core.blockers import record_blocker

    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health())
    record_blocker(state, summary="scheduler account is unavailable",
                   requested_action="restore slurm account access")

    result = _finalize(state, submission, "operation_blocked",
                       note="scheduler lost the account")

    assert result["status"] == "success"
    assert "class_disputed" not in result
    closures = state.list_artifacts("external_job_operation_closure")
    payload = json.loads(state.read_artifact(closures[0]["id"])["content"])
    assert payload["class_disputed"] is False
    assert "disputed_execution_class" not in payload
    assert payload["execution_class"] == "diagnostic"
    assert state.hook_state["blockers"]
    assert not any(
        str(item.get("reported_by") or "").startswith(
            manager._EXTERNAL_JOB_CLASS_DISPUTED_PREFIX)
        for item in state.hook_state["blockers"])


def test_finalize_job_class_cross_check(tmp_path):
    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)

    # task 行自述 diagnostic，payload 持久化 simulation：不一致 → 按 simulation。
    task_row = {**submission, "execution_class": "diagnostic"}
    mismatch = manager._finalize_job_class(state, task_row)
    assert mismatch == {"execution_class": "simulation", "verified": False,
                        "reason": "execution_class_cross_check_mismatch",
                        "claimed_execution_class": "diagnostic",
                        "persisted_execution_class": "simulation"}

    consistent = manager._finalize_job_class(state, submission)
    assert consistent["verified"] is True
    assert consistent["execution_class"] == "simulation"

    orphan = manager._finalize_job_class(
        state, {"scheduler": "slurm", "job_id": "no-receipt",
                "execution_class": "diagnostic"})
    assert orphan == {"execution_class": "simulation", "verified": False,
                      "reason": "submission_payload_execution_class_missing",
                      "claimed_execution_class": "diagnostic",
                      "persisted_execution_class": ""}


def test_analyzed_outcome_requires_evidence_artifact_id_immediately(tmp_path):
    state = State.new("experiment", tmp_path)

    result = asyncio.run(manager._finalize_external_job(
        state, "slurm", "9001", "", "analyzed_success"))

    assert result["status"] == "error"
    assert "evidence_artifact_id" in result["error"]
    assert "frozen" in result["error"]


def test_finalize_does_not_read_the_run_execution_mode(tmp_path, monkeypatch):
    """作业收尾不看 run 是运维还是科学，只看这个作业自己的 execution_class。

    这条钉的是 2026-09-08 与 09-09 两次活体死锁的开关。原本此处裹着
    `if not operational:`，运维模式下唯一被允许的证据只能由
    record_operation_completion 铸造，而那是 run 级单向门：一调用即封口，封口后
    路线不可修订，于是收尾要先封口、封口后修不了路线、路线修不了又过不了收尾。
    09-08 另一次能跑通靠的是把运维任务误分类成科学任务绕开这堵墙 —— 分类正确
    反而没有出路。

    机械判据：把 run 合同换成任一模式，同一个作业拿到的结果必须逐字相同。
    """
    calls: list[str] = []

    def _spy(_state):
        calls.append("read")
        return {"execution_mode": "operational"}

    monkeypatch.setattr(manager, "load_run_contract", _spy)
    state = State.new("experiment", tmp_path)

    # 词表错误的拒绝仍在，但它出自作业类别那一套，不再是 run 模式那一套
    wrong_family = asyncio.run(manager._finalize_external_job(
        state, "slurm", "9001", "some-log", "not_an_outcome"))
    assert wrong_family["status"] == "error"
    assert "operation_completed" in wrong_family["error"]
    assert "analyzed_success" in wrong_family["error"]

    # 运维模式下不再强制要 evidence_artifact_id：那正是通往单向门的那道墙
    no_evidence = asyncio.run(manager._finalize_external_job(
        state, "slurm", "9001", "", "operation_completed"))
    assert "record_operation_completion" not in str(no_evidence.get("error", ""))

    assert calls == [], "收尾函数不得读取 run 合同"


def test_simulation_job_analyzed_finalize_still_closes(tmp_path, monkeypatch):
    """simulation 类收尾一分不松，且照常成功关闭并回读 execution_class。"""
    submission = _submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    route_calls: list[dict] = []
    _patch_pipeline(monkeypatch, _health(), route_calls)

    unfrozen = state.save_artifact(
        "experiment_log", "unfrozen_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    rejected = _finalize(state, submission, "analyzed_success", unfrozen["id"])
    assert rejected["status"] == "error"
    assert "frozen" in rejected["error"]

    log = _save_frozen(
        state, "experiment_log", "frozen_log", "outputs inspected",
        metadata={"external_job_refs": [_external_job_ref(submission)]})
    result = _finalize(state, submission, "analyzed_success", log["id"])

    assert result["status"] == "success"
    assert result["job_execution_class"] == "simulation"
    assert result["class_unverified"] is False
    assert result["evidence_artifact_id"] == log["id"]
    assert "operation_closure_artifact_id" not in result
    assert state.list_artifacts("external_job_operation_closure") == []
    assert route_calls[0]["domain_outcome"] == "analyzed_success"
    assert route_calls[0]["evidence_artifact_id"] == log["id"]


def test_generic_save_artifact_cannot_forge_operation_closure(tmp_path):
    from shared.tools.builtin import _save_artifact

    state = State.new("experiment", tmp_path)
    forged = asyncio.run(_save_artifact(
        state, artifact_type="external_job_operation_closure",
        name="forged_closure", content="{}"))

    assert forged["status"] == "error"
    assert forged["failed_checks"] == ["managed_external_artifact_owner"]
    assert state.list_artifacts("external_job_operation_closure") == []


def test_task_external_jobs_reads_execution_class(tmp_path):
    from core.tasks import TaskList

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    tasks = TaskList(Path(state.project_root) / "tasks")
    tasks.create(
        title="Finalize external job: probe",
        description="\n".join((
            "external_job_key=key-1",
            "scheduler=slurm",
            "job_id=9001",
            "execution_class=diagnostic",
            "output_roots=[]",
        )),
        owner_node="experiment",
        run_id=state.run_id,
    )

    records = manager._task_external_jobs(state)
    assert len(records) == 1
    assert records[0]["execution_class"] == "diagnostic"


def test_operation_class_job_closure_minting_is_idempotent(tmp_path):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    health = _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}})

    first = manager._record_operation_job_closure(
        state, submission, execution_class="diagnostic",
        outcome="operation_completed", note="", health=health,
        class_unverified=False,
        completion_postconditions=_diagnostic_completion_view(
            state, submission, health))
    second = manager._record_operation_job_closure(
        state, submission, execution_class="diagnostic",
        outcome="operation_completed", note="", health=health,
        class_unverified=False,
        completion_postconditions=_diagnostic_completion_view(
            state, submission, health))

    assert first["reused"] is False
    assert second == {"artifact_id": first["artifact_id"], "reused": True}
    assert len(state.list_artifacts("external_job_operation_closure")) == 1


def _local_submission(**overrides):
    payload = _submission(
        scheduler="local",
        job_id="hf-job-operation-reuse",
        submission_nonce="nonce-local-operation-reuse",
        container_runtime_id="a" * 64,
        sandbox_control_dir="/tmp/hf-job-operation-reuse",
    )
    payload.update(overrides)
    return payload


def _operation_receipt_payload(state, submission, **overrides):
    payload = {
        "schema_version": 1,
        "closure_type": manager._EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
        "identity": manager._operation_closure_identity(submission),
        "execution_class": submission["execution_class"],
        "class_unverified": False,
        "class_disputed": False,
        "outcome": "operation_completed",
        "note": "terminal operation receipt",
        "run_id": state.run_id,
        "recorded_at": "2026-09-08T00:00:00+00:00",
        "health": {
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "exit_code": 0,
        },
    }
    payload.update(overrides)
    return payload


def _legacy_v0_operation_receipt_payload(state, submission, **overrides):
    """复刻已发布 v0 wire；不调用当前 closure writer/identity helper。"""
    payload = {
        "closure_type": "operation_finalization",
        "identity": {
            field: submission.get(field)
            for field in _V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS
        },
        "execution_class": submission["execution_class"],
        "class_unverified": False,
        "class_disputed": False,
        "outcome": "operation_completed",
        "note": "terminal operation receipt",
        "run_id": state.run_id,
        "recorded_at": "2026-09-08T00:00:00+00:00",
        "health": {
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "exit_code": 0,
        },
    }
    payload.update(overrides)
    return payload


def _save_operation_receipt(state, submission, name, payload):
    identity = manager._operation_closure_identity(submission)
    return state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        name,
        json.dumps(payload),
        metadata={
            **identity,
            "schema_version": payload.get("schema_version"),
            "closure_type": manager._EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
            "run_id": state.run_id,
            "outcome": payload.get("outcome"),
            "execution_class": payload.get("execution_class"),
            "class_unverified": payload.get("class_unverified"),
            "class_disputed": payload.get("class_disputed"),
        },
    )


def test_local_operation_class_job_retry_reuses_terminal_closure_after_cleanup(
    tmp_path, monkeypatch,
):
    """cleanup 后 probe 已不可用，仍以同一收据完成 route/task/lifecycle。"""
    submission = _local_submission(
        sandbox_control_dir=str(tmp_path / "local-operation-control"))
    state = _prepared_state(tmp_path, submission)
    calls = {"probe": 0, "persist": 0, "cleanup": 0, "route": 0}
    order = []

    def probe(*_args, **_kwargs):
        calls["probe"] += 1
        order.append("probe")
        if calls["probe"] > 1:
            raise AssertionError("deleted local job record must not be probed")
        return _health(
            scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}})

    def persist(*_args, **_kwargs):
        calls["persist"] += 1
        order.append("persist")

    def cleanup(*_args, **_kwargs):
        calls["cleanup"] += 1
        order.append("cleanup")
        return {"status": "success", "action": "removed"}

    route_results = iter([
        {"status": "error", "reason": "route_expected_outputs_missing"},
        {"status": "success", "attempt_id": "attempt-operation-reuse"},
    ])

    def project(*_args, **_kwargs):
        calls["route"] += 1
        order.append("route")
        return next(route_results)

    monkeypatch.setattr(manager, "probe_external_job_health", probe)
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", persist)
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization", cleanup)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project)

    first = _finalize(
        state, submission, "operation_completed", note="first terminal fact")

    assert first["status"] == "finalized_needs_route_reconciliation"
    assert first["do_not_resubmit"] is True
    assert order == ["probe", "persist", "cleanup", "route"]
    closures = state.list_artifacts(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE)
    assert len(closures) == 1
    closure_id = closures[0]["id"]
    payload = json.loads(state.read_artifact(closure_id)["content"])
    assert payload["schema_version"] == 2
    assert payload["closure_type"] == "operation_finalization"
    assert payload["run_id"] == state.run_id
    assert set(payload["identity"]) == set(manager._OPERATION_CLOSURE_IDENTITY_FIELDS)
    assert payload["health"]["scheduler_phase"] == "terminal"
    assert payload["outcome"] == "operation_completed"
    assert payload["completion_postconditions"]["passed"] is True
    monkeypatch.setattr(
        manager,
        "_operation_completion_postconditions",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("v2 retry must consume the frozen view")),
    )

    second = _finalize(
        state, submission, "operation_completed",
        note="retry text cannot rewrite the receipt")

    assert second["status"] == "success", second
    assert second["workflow_status"] == "finalized"
    assert second["operation_closure_artifact_id"] == closure_id
    assert second["operation_closure_compatibility_mode"] == "schema_v2"
    assert second["evidence_artifact_id"] == closure_id
    assert calls == {"probe": 1, "persist": 1, "cleanup": 2, "route": 2}
    assert order[-2:] == ["cleanup", "route"]
    assert len(state.list_artifacts("job_submission")) == 1
    assert state.list_artifacts(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE) == closures
    assert len(state.list_artifacts("external_job_lifecycle")) == 1


def test_local_operation_blocked_replays_frozen_witness_after_blockers_clear(
    tmp_path, monkeypatch,
):
    """blocked receipt owns its mint-time witness; retry ignores mutable blockers."""
    from core.blockers import record_blocker

    submission = _local_submission(
        sandbox_control_dir=str(tmp_path / "blocked-operation-control"))
    state = _prepared_state(tmp_path, submission)
    record_blocker(
        state,
        category="environment",
        summary="scheduler dependency unavailable",
        requested_action="restore the scheduler dependency",
    )
    calls = {"probe": 0, "persist": 0, "cleanup": 0, "route": 0}

    def probe(*_args, **_kwargs):
        calls["probe"] += 1
        if calls["probe"] > 1:
            raise AssertionError("blocked retry must use its frozen witness")
        return _health()

    def persist(*_args, **_kwargs):
        calls["persist"] += 1

    def cleanup(*_args, **_kwargs):
        calls["cleanup"] += 1
        return {"status": "success", "action": "removed"}

    route_results = iter([
        {"status": "error", "reason": "route_expected_outputs_missing"},
        {"status": "success", "attempt_id": "blocked-witness-retry"},
    ])

    def project(*_args, **_kwargs):
        calls["route"] += 1
        return next(route_results)

    monkeypatch.setattr(manager, "probe_external_job_health", probe)
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", persist)
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization", cleanup)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project)

    first = _finalize(
        state, submission, "operation_blocked",
        note="dependency unavailable at terminal probe")

    assert first["status"] == "finalized_needs_route_reconciliation"
    closure_id = first["operation_closure_artifact_id"]
    payload = json.loads(state.read_artifact(closure_id)["content"])
    witness = payload["independent_blocker_witness"]
    assert witness["count"] == 1
    assert manager._operation_blocker_witness_valid(witness)

    # Both the user blocker and the route-reconciliation blocker are mutable
    # state. Neither may be consulted to reinterpret the immutable receipt.
    state.hook_state["blockers"] = []
    second = _finalize(
        state, submission, "operation_blocked",
        note="retry text must not rewrite the frozen reason")

    assert second["status"] == "success", second
    assert second["operation_closure_artifact_id"] == closure_id
    assert second["operation_closure_compatibility_mode"] == "schema_v2"
    assert calls == {"probe": 1, "persist": 1, "cleanup": 2, "route": 2}
    assert len(state.list_artifacts(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE)) == 1


def test_local_operation_class_job_reuses_legacy_v0_receipt_after_cleanup(
    tmp_path, monkeypatch,
):
    """真实旧收据形状可恢复，但仍须 payload 与 record 双重归属严格成立。"""
    submission = _local_submission(
        sandbox_control_dir=str(tmp_path / "legacy-local-operation-control"))
    state = _prepared_state(tmp_path, submission)
    payload = _legacy_v0_operation_receipt_payload(state, submission)
    # 复刻 schema 落地前的真实 metadata：没有版本和完整 identity。
    artifact = state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        "legacy_v0_operation_receipt",
        json.dumps(payload),
        metadata={
            "scheduler": submission["scheduler"],
            "job_id": submission["job_id"],
            "outcome": payload["outcome"],
            "execution_class": payload["execution_class"],
            "class_unverified": False,
            "class_disputed": False,
        },
    )
    calls = {"cleanup": 0, "route": 0}

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("legacy receipt must replace the deleted local probe")

    def must_not_persist(*_args, **_kwargs):
        raise AssertionError("legacy receipt reuse must not mint new probe evidence")

    def cleanup(*_args, **_kwargs):
        calls["cleanup"] += 1
        return {"status": "success", "action": "already_removed"}

    def project(*_args, **_kwargs):
        calls["route"] += 1
        return {"status": "success", "attempt_id": "legacy-v0-route"}

    monkeypatch.setattr(manager, "probe_external_job_health", must_not_probe)
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", must_not_persist)
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization", cleanup)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project)

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "success", result
    assert result["operation_closure_artifact_id"] == artifact["id"]
    assert result["operation_closure_compatibility_mode"] == "legacy_v0"
    assert result["evidence_artifact_id"] == artifact["id"]
    assert calls == {"cleanup": 1, "route": 1}
    assert len(state.list_artifacts(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE)) == 1


def test_operation_class_job_receipt_audit_lists_current_run_only(
    tmp_path, monkeypatch,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(state, submission)
    artifact = _save_operation_receipt(
        state, submission, "own_operation_receipt", payload)
    real_list = state.list_artifacts
    own_only_values = []

    def audited_list(artifact_type=None, own_only=False):
        if artifact_type == manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE:
            own_only_values.append(own_only)
        return real_list(artifact_type, own_only=own_only)

    monkeypatch.setattr(state, "list_artifacts", audited_list)

    receipt = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert receipt["status"] == "success"
    assert receipt["artifact_id"] == artifact["id"]
    assert own_only_values == [True]


def test_operation_receipt_identity_schema_ignores_future_cancellation_field(
    tmp_path, monkeypatch,
):
    """Generic cancellation evolution cannot rewrite closure v0/v1 on the fly."""
    submission = _local_submission()
    state = _prepared_state(tmp_path / "existing", submission)
    payload = _operation_receipt_payload(state, submission)
    artifact = _save_operation_receipt(
        state, submission, "existing_v1_receipt", payload)
    monkeypatch.setattr(
        manager,
        "_CANCELLATION_IDENTITY_FIELDS",
        (*manager._CANCELLATION_IDENTITY_FIELDS, "future_scope"),
    )
    monkeypatch.setattr(
        manager,
        "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("existing v1 receipt must remain replayable")),
    )
    monkeypatch.setattr(
        manager,
        "_persist_execution_environment_evidence",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("receipt replay must not persist a new probe")),
    )
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization",
        lambda *_a, **_k: {"status": "success", "action": "already_removed"},
    )
    monkeypatch.setattr(
        execution_route,
        "record_external_route_finalization",
        lambda *_a, **_k: {"status": "success"},
    )

    replayed = _finalize(state, submission, "operation_completed")

    assert replayed["status"] == "success", replayed
    assert replayed["operation_closure_artifact_id"] == artifact["id"]

    new_submission = _local_submission(
        job_id="hf-job-future-identity",
        submission_nonce="nonce-future-identity",
        container_runtime_id="b" * 64,
        future_scope="must-not-enter-v1",
    )
    new_state = _prepared_state(tmp_path / "new", new_submission)
    new_health = _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}})
    closure = manager._record_operation_job_closure(
        new_state,
        new_submission,
        execution_class="diagnostic",
        outcome="operation_completed",
        note="",
        health=new_health,
        class_unverified=False,
        completion_postconditions=_diagnostic_completion_view(
            new_state, new_submission, new_health),
    )
    stored = new_state.read_artifact(closure["artifact_id"])
    new_payload = json.loads(stored["content"])

    assert manager._OPERATION_CLOSURE_IDENTITY_FIELDS == (
        _V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS
    )
    assert set(new_payload["identity"]) == set(
        _V0_V1_OPERATION_CLOSURE_IDENTITY_FIELDS)
    assert "future_scope" not in new_payload["identity"]
    assert "future_scope" not in stored["metadata"]


def test_operation_receipt_validates_matching_candidate_metadata(
    tmp_path,
):
    """A later unrelated receipt must not lend its metadata to the candidate."""
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    current_payload = _operation_receipt_payload(state, submission)
    current = _save_operation_receipt(
        state, submission, "current_operation_receipt", current_payload)
    other = _local_submission(
        job_id="hf-job-other-operation",
        submission_nonce="nonce-other-operation",
        container_runtime_id="b" * 64,
    )
    other_payload = _operation_receipt_payload(state, other)
    _save_operation_receipt(
        state, other, "later_unrelated_operation_receipt", other_payload)

    receipt = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert receipt["status"] == "success", receipt
    assert receipt["artifact_id"] == current["id"]


def test_unrelated_malformed_legacy_receipt_does_not_block_current_job(tmp_path):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        "unrelated_malformed_legacy_receipt",
        "{",
        metadata={
            "scheduler": "local",
            "job_id": "hf-job-unrelated",
            "outcome": "operation_completed",
            "execution_class": "diagnostic",
        },
    )

    receipt = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert receipt == {"status": "absent"}


def test_legacy_receipt_with_reused_job_id_and_other_attempt_is_skipped(tmp_path):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    other_attempt = _local_submission(
        submission_nonce="nonce-other-attempt",
        container_runtime_id="b" * 64,
    )
    payload = _legacy_v0_operation_receipt_payload(state, other_attempt)
    state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        "legacy_receipt_for_reused_job_id",
        json.dumps(payload),
        metadata={
            "scheduler": submission["scheduler"],
            "job_id": submission["job_id"],
            "outcome": payload["outcome"],
            "execution_class": payload["execution_class"],
        },
    )

    receipt = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert receipt == {"status": "absent"}


@pytest.mark.parametrize(("outcome", "health", "evidence_error_code"), [
    (
        "operation_completed",
        {
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "exit_code": 7,
        },
        "operation_completed_positive_evidence_missing",
    ),
    (
        "operation_failed",
        {
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "exit_code": 0,
        },
        "operation_failed_negative_evidence_missing",
    ),
])
def test_local_operation_class_job_receipt_rejects_inconsistent_outcome_evidence(
    tmp_path, monkeypatch, outcome, health, evidence_error_code,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(
        state, submission, outcome=outcome, health=health)
    _save_operation_receipt(state, submission, "inconsistent_receipt", payload)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid stored evidence must fail before probing")))

    result = _finalize(state, submission, outcome)

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_receipt_invalid"
    assert result["violations"] == ["outcome_evidence"]
    assert result["evidence_error_code"] == evidence_error_code


@pytest.mark.parametrize("mutation", ["absent", "empty", "forged_digest"])
def test_local_operation_blocked_receipt_rejects_invalid_static_witness(
    tmp_path, monkeypatch, mutation,
):
    from core.blockers import record_blocker

    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    record_blocker(
        state,
        category="environment",
        summary="scheduler dependency unavailable",
        requested_action="restore dependency",
    )
    witness = manager._operation_blocker_witness(state.hook_state["blockers"])
    assert witness is not None
    payload = _operation_receipt_payload(
        state,
        submission,
        outcome="operation_blocked",
        note="dependency unavailable",
        health={
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": [],
            "completion_paths": [],
            "exit_code": None,
        },
    )
    if mutation == "empty":
        payload["independent_blocker_witness"] = {
            "witness_type": manager._OPERATION_BLOCKER_WITNESS_KIND,
            "count": 0,
            "entries": [],
            "sha256": "0" * 64,
        }
    elif mutation == "forged_digest":
        payload["independent_blocker_witness"] = {
            **witness,
            "sha256": "0" * 64,
        }
    else:
        assert "independent_blocker_witness" not in payload
    _save_operation_receipt(state, submission, "invalid_blocked_receipt", payload)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid v1 witness must fail before live probe")))

    result = _finalize(state, submission, "operation_blocked")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_receipt_invalid"
    assert result["violations"] == ["independent_blocker_witness"]


def test_local_legacy_v0_blocked_receipt_uses_dynamic_blocker_fallback(
    tmp_path, monkeypatch,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _legacy_v0_operation_receipt_payload(
        state, submission, outcome="operation_blocked",
        note="legacy blocked fact")
    state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        "legacy_blocked_receipt",
        json.dumps(payload),
        metadata={
            "scheduler": submission["scheduler"],
            "job_id": submission["job_id"],
            "outcome": payload["outcome"],
            "execution_class": payload["execution_class"],
        },
    )
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("legacy receipt is audited before live probe")))

    result = _finalize(state, submission, "operation_blocked")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_receipt_invalid"
    assert result["violations"] == ["outcome_evidence"]
    assert result["evidence_error_code"] == (
        "operation_blocked_requires_recorded_blocker")


@pytest.mark.parametrize(("mutation", "violation"), [
    ("closure_type", "closure_type"),
    ("schema_version", "schema_version"),
    ("schema_downgrade", "schema_version"),
    ("schema_double_downgrade", "legacy_v0_metadata_shape"),
    ("metadata_schema", "schema_version"),
    ("metadata_closure", "metadata_closure_type"),
    ("metadata_run", "metadata_run_id"),
    ("metadata_outcome", "metadata_outcome"),
    ("metadata_class", "metadata_execution_class"),
    ("metadata_identity", "metadata_identity"),
    ("payload_run", "run_id"),
    ("identity", "ReceiptIdentityUnreadable"),
    ("identity_value", "identity"),
    ("terminal", "terminal_phase"),
    ("node_owner", "produced_by_node_type"),
    ("run_owner", "produced_by_run_id"),
])
def test_local_operation_class_job_receipt_reuse_validates_authority(
    tmp_path, monkeypatch, mutation, violation,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(state, submission)
    if mutation == "closure_type":
        payload["closure_type"] = "other"
    elif mutation == "schema_version":
        payload["schema_version"] = 99
    elif mutation in {"schema_downgrade", "schema_double_downgrade"}:
        payload.pop("schema_version")
    elif mutation == "payload_run":
        payload["run_id"] = "other-run"
    elif mutation == "identity":
        payload["identity"] = dict(payload["identity"])
        payload["identity"].pop("submission_nonce")
    elif mutation == "identity_value":
        payload["identity"] = dict(payload["identity"])
        payload["identity"]["submission_nonce"] = "other-nonce"
    elif mutation == "terminal":
        payload["health"] = {**payload["health"], "scheduler_phase": "running"}
    artifact = _save_operation_receipt(
        state, submission, "invalid_operation_receipt", payload)
    record_mutations = {
        "metadata_schema", "metadata_closure", "metadata_run",
        "metadata_outcome", "metadata_class", "metadata_identity",
        "schema_double_downgrade", "node_owner", "run_owner",
    }
    if mutation in record_mutations:
        real_read = state.read_artifact

        def read_with_mutation(artifact_id):
            record = real_read(artifact_id)
            if artifact_id == artifact["id"]:
                record = dict(record)
                if mutation == "node_owner":
                    record["produced_by_node_type"] = "analysis"
                elif mutation == "run_owner":
                    record["produced_by_run_id"] = "other-run"
                elif mutation == "schema_double_downgrade":
                    metadata = dict(record["metadata"])
                    metadata.pop("schema_version")
                    record["metadata"] = metadata
                else:
                    metadata = dict(record["metadata"])
                    field, value = {
                        "metadata_schema": ("schema_version", 99),
                        "metadata_closure": ("closure_type", "other"),
                        "metadata_run": ("run_id", "other-run"),
                        "metadata_outcome": ("outcome", "operation_failed"),
                        "metadata_class": ("execution_class", "simulation"),
                        "metadata_identity": (
                            "submission_nonce", "other-nonce"),
                    }[mutation]
                    metadata[field] = value
                    record["metadata"] = metadata
            return record

        monkeypatch.setattr(state, "read_artifact", read_with_mutation)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid receipt must fail before probing")))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    if mutation == "identity":
        assert result["error_code"] == "operation_closure_receipt_audit_failed"
        assert result["error_type"] == violation
    else:
        assert result["error_code"] == "operation_closure_receipt_invalid"
        assert violation in result["violations"]
    assert state.list_artifacts("external_job_lifecycle") == []


@pytest.mark.parametrize(("failure_mode", "error_type"), [
    ("read_error", "OSError"),
    ("malformed_json", "JSONDecodeError"),
    ("identity_unreadable", "ReceiptIdentityUnreadable"),
])
def test_operation_class_job_receipt_audit_fails_closed_when_unreadable(
    tmp_path, monkeypatch, failure_mode, error_type,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(state, submission)
    if failure_mode == "malformed_json":
        artifact = state.save_artifact(
            manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
            "malformed_operation_receipt",
            "{",
            metadata={
                "scheduler": submission["scheduler"],
                "job_id": submission["job_id"],
                "outcome": payload["outcome"],
                "execution_class": payload["execution_class"],
            },
        )
    elif failure_mode == "identity_unreadable":
        payload.pop("identity")
        artifact = state.save_artifact(
            manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
            "unattributable_operation_receipt",
            json.dumps(payload),
            metadata={
                "scheduler": submission["scheduler"],
                "job_id": submission["job_id"],
                "outcome": payload["outcome"],
                "execution_class": payload["execution_class"],
            },
        )
    else:
        artifact = _save_operation_receipt(
            state, submission, "unreadable_operation_receipt", payload)
        real_read = state.read_artifact

        def raise_for_receipt(artifact_id):
            if artifact_id == artifact["id"]:
                raise OSError("simulated unreadable receipt")
            return real_read(artifact_id)

        monkeypatch.setattr(state, "read_artifact", raise_for_receipt)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("an unreadable current-run receipt is not absent")))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_receipt_audit_failed"
    assert result["artifact_id"] == artifact["id"]
    assert result["error_type"] == error_type
    assert len(state.list_artifacts(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE)) == 1


def test_local_operation_class_job_receipt_reuse_rejects_outcome_change(
    tmp_path, monkeypatch,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(
        state, submission, outcome="operation_failed",
        health={
            "scheduler_phase": "terminal",
            "health_state": "terminal_needs_analysis",
            "error_evidence": ["exit 7"],
            "completion_paths": [],
            "exit_code": 7,
        },
    )
    _save_operation_receipt(state, submission, "failed_receipt", payload)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("outcome conflict must fail before probing")))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_outcome_conflict"
    assert result["recorded_outcome"] == "operation_failed"
    assert result["requested_outcome"] == "operation_completed"


def test_local_operation_class_job_receipt_reuse_rejects_duplicates(
    tmp_path, monkeypatch,
):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(state, submission)
    _save_operation_receipt(state, submission, "receipt_one", payload)
    _save_operation_receipt(state, submission, "receipt_two", payload)
    monkeypatch.setattr(
        manager, "probe_external_job_health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("duplicate receipts must fail before probing")))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_receipt_conflict"
    assert len(result["artifact_ids"]) == 2


def test_operation_class_job_closes_mechanically_in_an_operational_run(
    tmp_path, monkeypatch,
):
    """运维 run 里的 operation 类作业，直接凭机械证据铸收据收尾，不需要任何 run 级产物。

    反转自 test_operational_finalize_never_reuses_operation_class_job_receipt。
    那条原本钉的是「运维 run 一律走 run 级冻结日志、绝不铸/复用收据」——正是这条
    耦合把作业的结束凭据放在 run 级单向门后面，构成 2026-09-08 与 09-09 两次活体
    死锁。现在同一个作业在两种 run 身份下走同一条路。
    """
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    monkeypatch.setattr(
        manager, "load_run_contract",
        lambda _state: {"execution_mode": "operational"},
    )
    probe_calls = []

    def probe(*_args, **_kwargs):
        probe_calls.append(True)
        return _health(
            scheduler_result={"raw": {"sandbox_state": {"exit_code": 0}}})

    route_calls = []

    def project(*_args, **kwargs):
        route_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(manager, "probe_external_job_health", probe)
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization",
        lambda *_a, **_k: {"status": "success", "action": "removed"},
    )
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project)

    # 不带任何 run 级证据，直接收尾
    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "success", result
    assert result["workflow_status"] == "finalized"
    assert result["job_execution_class"] == "diagnostic"
    assert result["operation_closure_artifact_id"], "必须铸出机械收据"
    assert "execution_mode" not in result, "作业层不回显 run 身份"
    assert route_calls[0]["domain_outcome"] == "operation_completed"
    closures = state.list_artifacts("external_job_operation_closure")
    assert len(closures) == 1


def test_scientific_finalize_never_reuses_operation_class_job_receipt(
    tmp_path, monkeypatch,
):
    submission = _local_submission(execution_class="simulation")
    state = _prepared_state(tmp_path, submission)
    payload = _operation_receipt_payload(
        state, submission, execution_class="simulation")
    _save_operation_receipt(state, submission, "irrelevant_operation_receipt", payload)
    log = state.save_artifact(
        "experiment_log", "scientific_log", "terminal scientific evidence",
        metadata={
            "frozen": True,
            "external_job_refs": [_external_job_ref(submission)],
        },
    )
    probe_calls = []

    def unavailable_probe(*_args, **_kwargs):
        probe_calls.append(True)
        return {"status": "error", "reason": "live_probe_required"}

    monkeypatch.setattr(manager, "probe_external_job_health", unavailable_probe)

    result = _finalize(
        state, submission, "analyzed_inconclusive", log["id"])

    assert result == {"status": "error", "reason": "live_probe_required"}
    assert probe_calls == [True]
    assert state.list_artifacts("external_job_lifecycle") == []


# ── #879 复审第一条：跨 run 收据归属先于内容裁决 ──────────────────────────────
#
# 研究记录是**工作区**作用域、跨 run 累积的（C1 起一个 worktree 一本账本
# `.research/ledger/records.jsonl`，收据不是 run_local 类型，不含 run_id；
# core/state.py 的 own_only=True 只说"本节点产出"）。此前 _operation_closure_receipt
# 先按 external-job identity 选中候选、再把 produced_by_run_id 不符当成 violations
# 硬拒，于是前一个 run 合法留下的收据会把 continuation run 的复用与重铸两条路
# 一起堵死。既有的跨 run 用例用 State.new 造"上一个 run 的提交"，共享目录里从来
# 没有真正的前 run 收据，测不到这条路 —— 下面三条各钉一侧。


def _shared_workspace_states(tmp_path, submission):
    """两个真实 run 绑定同一个 Project worktree、共用工作区账本（C1 的真实布局）。"""
    worktree = tmp_path / "worktree"
    records = worktree / _NODE_WORKSPACES["experiment"]
    records.mkdir(parents=True)
    first = State.new("experiment", tmp_path / "runs")
    first.project_worktree = worktree
    first.workspace_records_dir = records
    first.save_artifact("job_submission", "op_submission", json.dumps(submission))
    second = State.new("experiment", tmp_path / "runs")
    second.project_worktree = worktree
    second.workspace_records_dir = records
    assert second.run_id != first.run_id
    return first, second


def _terminal_success_health():
    return {
        "scheduler_phase": "terminal",
        "health_state": "terminal_needs_analysis",
        "error_evidence": [],
        "completion_paths": [],
        "scheduler_result": {"raw": {"sandbox_state": {"exit_code": 0}}},
    }


def test_continuation_run_skips_prior_run_receipt_and_mints_its_own(tmp_path):
    """合法的前 run 收据被跳过，本 run 按自己的证据重铸；历史原样保留。"""
    submission = _local_submission()
    first, second = _shared_workspace_states(tmp_path, submission)
    prior = _save_operation_receipt(
        first, submission, "prior_run_receipt",
        _operation_receipt_payload(first, submission))

    resolved = manager._operation_closure_receipt(
        second, submission, "operation_completed")

    assert resolved["status"] == "absent", resolved
    assert resolved["foreign_run_receipt_artifact_ids"] == [prior["id"]]

    minted = manager._record_operation_job_closure(
        second, submission, execution_class=submission["execution_class"],
        outcome="operation_completed", note="continuation close",
        health=_terminal_success_health(), class_unverified=False,
        completion_postconditions=_diagnostic_completion_view(
            second, submission, _terminal_success_health()))

    assert not minted.get("reused"), minted
    assert minted["artifact_id"] != prior["id"]
    assert second.read_artifact(
        minted["artifact_id"])["produced_by_run_id"] == second.run_id
    # 历史只追加：前一个 run 的收据既不被改写也不被删除。
    assert first.read_artifact(
        prior["id"])["produced_by_run_id"] == first.run_id


def test_own_receipt_is_reused_even_when_a_prior_run_receipt_exists(tmp_path):
    """被跳过的外 run 收据不得算进重复冲突，也不得遮住本 run 自己的收据。"""
    submission = _local_submission()
    first, second = _shared_workspace_states(tmp_path, submission)
    _save_operation_receipt(
        first, submission, "prior_run_receipt",
        _operation_receipt_payload(first, submission))
    own = _save_operation_receipt(
        second, submission, "own_run_receipt",
        _operation_receipt_payload(second, submission))

    resolved = manager._operation_closure_receipt(
        second, submission, "operation_completed")

    assert resolved["status"] == "success", resolved
    assert resolved["artifact_id"] == own["id"]


def test_incoherent_run_ownership_is_still_a_ledger_conflict(tmp_path):
    """三处 run 标识互相矛盾才是篡改：produced_by_run_id 指向别的 run，
    payload 却自称本 run —— 这不是合法历史收据，仍须硬拒。"""
    submission = _local_submission()
    first, second = _shared_workspace_states(tmp_path, submission)
    saved = _save_operation_receipt(
        first, submission, "incoherent_receipt",
        _operation_receipt_payload(second, submission))
    assert first.read_artifact(
        saved["id"])["produced_by_run_id"] == first.run_id

    resolved = manager._operation_closure_receipt(
        second, submission, "operation_completed")

    assert resolved["status"] == "error", resolved
    assert resolved["error_code"] == "operation_closure_receipt_invalid"
    assert "produced_by_run_id" in resolved["violations"]


def _corrupt_prior_run_receipt(state, submission, content):
    """上一个 run 留下的、metadata 指名本 job 但正文损坏的收据。"""
    identity = manager._operation_closure_identity(submission)
    return state.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE, "corrupt_prior", content,
        metadata={
            **identity,
            "schema_version": manager._EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION,
            "closure_type": manager._EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
            "run_id": state.run_id, "outcome": "operation_completed",
            "execution_class": submission["execution_class"],
            "class_unverified": False, "class_disputed": False,
        },
    )


@pytest.mark.parametrize("content", ["{ not json", "", '{"identity": {}}'])
def test_corrupt_prior_run_receipt_does_not_block_this_run(tmp_path, content):
    """历史 run 的损坏收据不是本 run 的账本事故。

    收据 identity 十字段不含 run id，所以「metadata 指名本 job」对每一个 run 都
    成立。此前正文缺失/非 JSON/身份不全这三处硬拒排在归属判定之前，于是任何一份
    历史损坏收据都会把此后每一个 run 的 finalize 永久钉死。
    """
    submission = _local_submission()
    first, second = _shared_workspace_states(tmp_path, submission)
    _corrupt_prior_run_receipt(first, submission, content)

    resolved = manager._operation_closure_receipt(
        second, submission, "operation_completed")

    assert resolved["status"] == "absent", resolved
    assert resolved["foreign_run_receipt_artifact_ids"]
    minted = manager._record_operation_job_closure(
        second, submission, execution_class=submission["execution_class"],
        outcome="operation_completed", note="continuation close",
        health=_terminal_success_health(), class_unverified=False,
        completion_postconditions=_diagnostic_completion_view(
            second, submission, _terminal_success_health()))
    assert not minted.get("reused"), minted


@pytest.mark.parametrize("content", ["{ not json", "", '{"identity": {}}'])
def test_corrupt_own_run_receipt_still_fails_closed(tmp_path, content):
    """本 run 自己的收据损坏时，仍然不能当作「不存在」跳过。"""
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    _corrupt_prior_run_receipt(state, submission, content)

    resolved = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert resolved["status"] == "error", resolved
    assert resolved["error_code"] == "operation_closure_receipt_audit_failed"


def test_corrupt_receipt_with_contradictory_run_ownership_is_surfaced(tmp_path):
    """归属信号互相矛盾时不得静默跳过：矛盾本身是必须报出来的账本事实。

    produced_by_run_id（框架落盘时写的）说是别的 run，metadata.run_id 却自称本
    run —— 这不是一份合法的历史收据。若只按「produced_by 不是我就跳过」判定，
    这条矛盾会被无声吞掉。
    """
    submission = _local_submission()
    first, second = _shared_workspace_states(tmp_path, submission)
    identity = manager._operation_closure_identity(submission)
    first.save_artifact(
        manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE, "contradictory_prior",
        "{ not json",
        metadata={
            **identity,
            "schema_version": manager._EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION,
            "closure_type": manager._EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
            # 落盘归属是 first，metadata 却自称 second。
            "run_id": second.run_id, "outcome": "operation_completed",
            "execution_class": submission["execution_class"],
            "class_unverified": False, "class_disputed": False,
        },
    )

    resolved = manager._operation_closure_receipt(
        second, submission, "operation_completed")

    assert resolved["status"] == "error", resolved
    assert resolved["error_code"] == "operation_closure_receipt_audit_failed"
