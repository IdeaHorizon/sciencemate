"""F9 回归：operation closure 的 external_job_refs 由受管提交账本自动派生。

三个真实死锁 run（1788333763-880676 / 1788334189-2b872a / 1788337265-2f223d）坐实的
陷阱：首次收尾不带 refs → closure 冻结空 refs → finalize 永不匹配 → 幂等锁死 →
external job workflow 永久 awaiting。修复后 refs 的权威来源是本 run 的提交账本，
调用方声明降级为增选/交叉确认；terminal 墙收窄为 success-only；存量空 refs closure
经幂等披露 + continuation-run 收养解决。规格：docs/F9_fix_spec.md。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tests._mechanical_execution import (
    record_submitted_managed_action,
)
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _submission(**overrides) -> dict:
    result = {
        "status": "success",
        "dry_run": False,
        "scheduler": "local",
        "job_id": "4242",
        "job_name": "f9-job",
        "namespace": None,
        "launch_host": "node20",
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": "nonce-4242",
        "container_runtime_id": "c" * 64,
        "process_group_id": None,
        "process_start_ticks": None,
        "workdir": "/tmp/work",
    }
    result.update(overrides)
    return result


def _terminal_health(*_args, **_kwargs) -> dict:
    return {"status": "success", "scheduler_phase": "terminal",
            "health_state": "terminal", "workflow_status": "awaiting_analysis"}


def _running_health(*_args, **_kwargs) -> dict:
    return {"status": "success", "scheduler_phase": "running",
            "health_state": "running", "workflow_status": "awaiting_external_job"}


def _state(
    tmp_path: Path,
    focus: str,
    *,
    operation_category: str = "job_observation",
) -> State:
    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    state.hook_state.setdefault("node_inputs", {
        "experiment_focus": focus,
        "prereg_assignment": {
            "kind": "none",
            "reason": "This managed operation has no governing preregistration.",
        },
    })
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category=operation_category,
        reason=f"F9 regression fixture: {focus}",
    ))
    return state


@pytest.fixture()
def verified_scheduler(monkeypatch):
    """终态成功的最小机械桩：identity 解析走真实代码，探针/成功证据打桩。"""
    monkeypatch.setattr(rm, "probe_external_job_health", _terminal_health)
    monkeypatch.setattr(rm, "lifecycle_for_submission",
                        lambda _state, _payload, **_kw: {"status": None})
    monkeypatch.setattr(rm, "_task_external_jobs", lambda _state: [])
    monkeypatch.setattr(oc, "_external_job_success_evidence",
                        lambda *_a, **_k: {"verified": True, "succeeded": True,
                                           "returncode": 0})


def _record(state: State, **kwargs) -> dict:
    defaults = {"task_kind": "external_job",
                "objective": "close the managed operation honestly",
                "outcome": "success"}
    defaults.update(kwargs)
    return asyncio.run(oc._record_operation_completion(state, **defaults))


# ── ① 主陷阱消灭：不传 refs → 自动派生并冻结，finalize 身份精确匹配 ──────────


def test_success_without_refs_autoderives_from_own_ledger(tmp_path, verified_scheduler):
    state = _state(tmp_path, "auto-derive refs on first closure")
    payload = _submission()
    state.save_artifact("job_submission", "s1", json.dumps(payload))

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    refs = (log.get("metadata") or {}).get("external_job_refs")
    assert refs and refs[0]["job_id"] == "4242"
    # finalize 侧的同一台精确匹配器必须判无差异 —— 这就是死锁的反向证明。
    assert rm._external_job_evidence_identity_diagnostic(log, payload) is None
    # closure_input 同步携带（供幂等/审计）。
    frozen_input = (log.get("metadata") or {}).get("operation_closure_input") or {}
    assert frozen_input.get("external_job_refs")
    # 自动派生 = 身份已具备：不得再因 managed_job_ids_declared 降级。
    assert frozen_input.get("outcome") == "success"
    assert "outcome_demoted_from" not in frozen_input


# ── ⑫ kind 无关触发：build 经 submit_job 承载同样自动派生 ────────────────────


def test_build_kind_with_managed_job_also_autoderives(tmp_path, verified_scheduler):
    state = _state(
        tmp_path,
        "toolchain build carried by submit_job",
        operation_category="toolchain_build",
    )
    submitted = record_submitted_managed_action(state, job_id="b1")
    payload = {
        **submitted["reference"],
        "workdir": "/tmp/work",
    }
    state.save_artifact("job_submission", "s1", json.dumps(payload))

    completion = _record(state, task_kind="toolchain_build",
                         artifact_paths=["/tmp/work/out.log"])

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    refs = (log.get("metadata") or {}).get("external_job_refs")
    assert refs and refs[0]["job_id"] == "b1"


# ── ⑤ success 墙保留（success-only）＋ ④ 运行中 blocked 收据合法 ─────────────


def test_running_job_still_rejects_success(tmp_path, verified_scheduler, monkeypatch):
    monkeypatch.setattr(rm, "probe_external_job_health", _running_health)
    state = _state(tmp_path, "running job must not close as success")
    state.save_artifact("job_submission", "s1", json.dumps(_submission()))

    completion = _record(state)

    assert completion["status"] == "error"
    assert completion["error_code"] == "external_jobs_not_terminal"
    # BF-12：出口必须可执行。
    assert "blocked" in completion["error"]


def test_running_job_blocked_closure_freezes_with_refs(
    tmp_path, verified_scheduler, monkeypatch,
):
    monkeypatch.setattr(rm, "probe_external_job_health", _running_health)
    state = _state(tmp_path, "honest blocked receipt while job is running")
    payload = _submission()
    state.save_artifact("job_submission", "s1", json.dumps(payload))

    completion = _record(state, outcome="blocked", blocker_id="b-1",
                         next_step="reconcile after the scheduler reports terminal")

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    refs = (log.get("metadata") or {}).get("external_job_refs")
    assert refs and refs[0]["job_id"] == "4242"
    terminal_checks = [
        item for item in ((log.get("metadata") or {})
                          .get("operation_closure_input") or {}).get("checks", [])
        if item.get("name") == "external_jobs_terminal"
    ]
    assert terminal_checks and terminal_checks[0]["passed"] is False


# ── ⑥ 取消不毒化：终局 lifecycle 的提交被排除并披露，success 不降级 ──────────


def test_cancelled_job_excluded_and_disclosed(tmp_path, verified_scheduler, monkeypatch):
    def lifecycle(_state, payload, **_kw):
        if payload.get("job_id") == "dead":
            return {"status": "cancelled", "resolution": "cancelled"}
        return {"status": None}

    monkeypatch.setattr(rm, "lifecycle_for_submission", lifecycle)
    state = _state(tmp_path, "cancel then resubmit must not poison success")
    state.save_artifact("job_submission", "dead", json.dumps(_submission(
        job_id="dead", submission_nonce="nonce-dead")))
    state.save_artifact("job_submission", "live", json.dumps(_submission(
        job_id="live", submission_nonce="nonce-live")))

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    metadata = log.get("metadata") or {}
    refs = metadata.get("external_job_refs") or []
    assert {ref["job_id"] for ref in refs} == {"live"}
    frozen_input = metadata.get("operation_closure_input") or {}
    excluded = frozen_input.get("external_jobs_excluded") or []
    assert excluded and excluded[0]["reference"]["job_id"] == "dead"
    assert excluded[0]["lifecycle_status"] == "cancelled"
    assert "outcome_demoted_from" not in frozen_input


def test_finalized_job_stays_in_refs(tmp_path, verified_scheduler, monkeypatch):
    """已收尾的作业必须留在 refs 里——只有作废的才排除。

    作业层解耦后，「先逐个 finalize、再做 run 级闭环」成为默认顺序。原来这里用
    补集写法 `status not in _ACTIVE_JOB_STATES` 把 finalized 一并排掉，于是这条
    默认顺序会拿到空 refs，成功闭环被机械降级成 partial —— 比解耦之前更差。
    因此这一项必须与作业层解耦同 PR。
    """
    def lifecycle(_state, payload, **_kw):
        if payload.get("job_id") == "done":
            return {"status": "finalized", "resolution": "exact"}
        if payload.get("job_id") == "dead":
            return {"status": "cancelled", "resolution": "cancelled"}
        return {"status": None}

    monkeypatch.setattr(rm, "lifecycle_for_submission", lifecycle)
    state = _state(tmp_path, "finalize each job first, then close the run")
    for job_id in ("done", "dead", "live"):
        state.save_artifact("job_submission", job_id, json.dumps(_submission(
            job_id=job_id, submission_nonce=f"nonce-{job_id}")))

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    metadata = log.get("metadata") or {}
    refs = metadata.get("external_job_refs") or []
    assert {ref["job_id"] for ref in refs} == {"done", "live"}, (
        "已收尾的作业是本 run 做过的事实，必须在 refs 里")
    frozen_input = metadata.get("operation_closure_input") or {}
    excluded = frozen_input.get("external_jobs_excluded") or []
    assert {row["reference"]["job_id"] for row in excluded} == {"dead"}


def test_closure_reads_the_receipt_instead_of_probing_again(
    tmp_path, verified_scheduler, monkeypatch,
):
    """作业已铸下终态收据时，run 级闭环读收据，不再探针。

    探针在这里是重算，而且探的对象常常已被 cleanup 删掉，探回来是「terminal 但
    退出码为空」的假象（2026-09-08 与 09-09 活体各一次）。判据留在铸造收据的那
    一侧，闭环侧不重建第二套。
    """
    probe_calls: list[str] = []

    def probe(_state, _scheduler, job_id, _ns=None, **_kw):
        probe_calls.append(job_id)
        return {"status": "error", "error": "sandbox already removed"}

    monkeypatch.setattr(rm, "probe_external_job_health", probe)
    monkeypatch.setattr(
        oc, "_closure_receipt_for",
        lambda _state, record: {
            "artifact_id": "closure__receipt",
            "payload": {"outcome": "operation_completed"},
        },
    )
    state = _state(tmp_path, "receipt outlives the sandbox")
    state.save_artifact("job_submission", "done", json.dumps(_submission(
        job_id="done", submission_nonce="nonce-done")))

    completion = _record(state)

    assert completion["status"] == "success", completion
    assert probe_calls == [], "有收据就不该再探针"
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    checks = (log.get("metadata") or {}).get("operation_closure_input") or {}
    rows = checks.get("external_job_health") or []
    if rows:
        assert rows[0]["success_evidence"]["source"] == (
            "external_job_operation_closure")


# ── ② 报错一次教育即可执行：近似候选 + 可省略声明；随后省略参数成功 ──────────


def test_wrong_refs_error_is_actionable_then_omission_succeeds(
    tmp_path, verified_scheduler,
):
    state = _state(tmp_path, "one actionable rejection then auto success")
    state.save_artifact("job_submission", "s1", json.dumps(_submission()))

    wrong = _record(state, external_job_refs=[
        {"job_id": "4242", "namespace": "wrong-ns"}])
    assert wrong["status"] == "error"
    assert wrong["error_code"] in {"external_job_identity_missing",
                                   "external_job_identity_ambiguous"}
    details = wrong.get("details") or {}
    assert details.get("near_candidates"), wrong
    assert "namespace" in (details.get("mismatched_fields") or [])
    assert "可整体省略" in str(wrong.get("error") or details.get("error"))

    retry = _record(state)
    assert retry["status"] == "success", retry
    log = state.read_artifact(retry["experiment_log_artifact_id"])
    assert (log.get("metadata") or {}).get("external_job_refs")


# ── ⑪ 账本降级不封死：witness + 机械降 partial，而非拒绝冻结 ─────────────────


def test_ledger_degradation_is_witnessed_not_fatal(
    tmp_path, verified_scheduler, monkeypatch,
):
    def lifecycle(_state, payload, **_kw):
        if payload.get("job_id") == "corrupt":
            raise RuntimeError("ledger row unreadable")
        return {"status": None}

    monkeypatch.setattr(rm, "lifecycle_for_submission", lifecycle)
    state = _state(tmp_path, "ledger corruption must degrade, not deadlock")
    state.save_artifact("job_submission", "corrupt", json.dumps(_submission(
        job_id="corrupt", submission_nonce="nonce-x")))
    state.save_artifact("job_submission", "fine", json.dumps(_submission(
        job_id="fine", submission_nonce="nonce-y")))

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    frozen_input = (log.get("metadata") or {}).get("operation_closure_input") or {}
    degraded = [item for item in frozen_input.get("checks", [])
                if item.get("name") == "external_job_ledger_degraded"]
    assert degraded and degraded[0]["passed"] is False
    # success + failed_checks → 既有机械降 partial 接管（不是拒绝）。
    assert frozen_input.get("outcome") == "partial"
    assert frozen_input.get("outcome_demoted_from") == "success"


# ── ⑦/⑧ continuation run 收养：单例收养成功；多候选一次性教育 ────────────────


def test_continuation_run_adopts_single_unresolved_workflow(
    tmp_path, verified_scheduler, monkeypatch,
):
    state = _state(tmp_path, "continuation run adopts the open workflow")
    foreign = _submission(job_id="7777", submission_nonce="nonce-7777")
    monkeypatch.setattr(rm, "_task_external_jobs", lambda _state: [dict(foreign)])
    monkeypatch.setattr(
        rm, "unresolved_external_workflows",
        lambda _state: [{**{k: foreign.get(k) for k in (
            "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
            "resource_uid", "submission_nonce", "container_runtime_id")},
            "workflow_status": "awaiting_external_job"}],
    )

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    refs = (log.get("metadata") or {}).get("external_job_refs")
    assert refs and refs[0]["job_id"] == "7777"
    adopted = [item for item in ((log.get("metadata") or {})
               .get("operation_closure_input") or {}).get("checks", [])
               if item.get("name") == "external_workflow_adopted"]
    assert adopted and adopted[0]["passed"] is True


def test_ambiguous_adoption_rejects_once_with_full_candidates(
    tmp_path, verified_scheduler, monkeypatch,
):
    rows = [
        {"scheduler": "local", "job_id": "a1", "workflow_status": "awaiting_external_job"},
        {"scheduler": "local", "job_id": "a2", "workflow_status": "awaiting_external_job"},
    ]
    monkeypatch.setattr(rm, "unresolved_external_workflows", lambda _state: list(rows))
    state = _state(tmp_path, "two open workflows cannot be adopted blindly")

    completion = _record(state, outcome="blocked", blocker_id="b-2",
                         next_step="pick one candidate ref and retry closure")

    assert completion["status"] == "error"
    assert completion["error_code"] == "external_job_adoption_ambiguous"
    candidates = completion.get("candidates") or []
    assert {item.get("job_id") for item in candidates} == {"a1", "a2"}


# ── ⑩ 存量空 refs closure：幂等返回披露 + continuation 指引 ──────────────────


def test_idempotent_return_discloses_frozen_empty_refs(
    tmp_path, verified_scheduler, monkeypatch,
):
    state = _state(tmp_path, "legacy closure with empty refs is disclosed")
    monkeypatch.setattr(rm, "unresolved_external_workflows", lambda _state: [])
    first = _record(state, outcome="blocked", blocker_id="b-3",
                    next_step="legacy closure minted before auto-derivation")
    assert first["status"] == "success", first

    monkeypatch.setattr(
        rm, "unresolved_external_workflows",
        lambda _state: [{"scheduler": "local", "job_id": "9999",
                         "workflow_status": "awaiting_external_job"}],
    )
    second = _record(state, outcome="blocked", blocker_id="b-3",
                     next_step="legacy closure minted before auto-derivation")

    assert second["status"] == "success" and second.get("idempotent") is True
    assert second.get("external_job_refs_frozen_empty") is True
    assert second["unresolved_external_workflows"][0]["job_id"] == "9999"
    assert "continuation" in str(second.get("recovery"))


# ── ⑪ 补：真实损坏形态 —— payload 不可解析的 job_submission 必须可归因地进 witness ──


def test_real_payload_corruption_is_witnessed_with_artifact_id(
    tmp_path, verified_scheduler,
):
    """规格 §1a 的原型场景：不用桩，直接落盘一条 content 不可解析的提交收据。

    从前的枚举走 rm._submission_payloads，损坏 payload 被静默丢弃，artifact_id
    根本到不了 errors 通道 —— witness 承诺是空壳。现在按 §1a 逐信封枚举，
    损坏必须以 {artifact_id, 异常名} 落进 external_job_ledger_degraded。
    """
    state = _state(tmp_path, "a corrupt submission receipt must be attributable")
    state.save_artifact("job_submission", "broken", "{this is not json")
    state.save_artifact("job_submission", "fine", json.dumps(_submission(
        job_id="fine", submission_nonce="nonce-fine")))

    completion = _record(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    frozen_input = (log.get("metadata") or {}).get("operation_closure_input") or {}
    degraded = [item for item in frozen_input.get("checks", [])
                if item.get("name") == "external_job_ledger_degraded"]
    assert degraded and degraded[0]["passed"] is False
    witnessed = json.dumps(degraded[0].get("evidence") or {})
    assert "artifact_id" in witnessed and witnessed.count("Error") >= 1
    # 健康提交不受株连：refs 照常派生。
    refs = (log.get("metadata") or {}).get("external_job_refs") or []
    assert {ref["job_id"] for ref in refs} == {"fine"}
    # success 被机械降 partial 而非拒绝（焦点二判决）。
    assert frozen_input.get("outcome") == "partial"


# ── §2b 并集：own 提交存在时 job_ids 仍可增选（跨 run 收养），不退化为子集校验 ──


def test_job_ids_union_can_adopt_cross_run_job_alongside_own(
    tmp_path, verified_scheduler, monkeypatch,
):
    """own_refs 非空时，job_ids 指名的域外作业必须走选择器解析后并集。

    从前 requests = requested_refs or [...]：refs 一旦含自动派生项，job_ids
    退化成子集交叉校验，跨 run 增选被 external_job_identity_mismatch 误拒，
    且文案引用调用方从未传过的 refs。
    """
    foreign = _submission(job_id="7777", submission_nonce="nonce-7777")
    monkeypatch.setattr(rm, "_task_external_jobs", lambda _state: [dict(foreign)])
    state = _state(tmp_path, "job_ids must still be able to adopt across runs")
    state.save_artifact("job_submission", "own", json.dumps(_submission()))

    completion = _record(state, job_ids=["7777"])

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    refs = (log.get("metadata") or {}).get("external_job_refs") or []
    assert {ref["job_id"] for ref in refs} == {"4242", "7777"}


# ── complete/partial 同一把尺：降级过的 closure 冻结中途崩溃后原样参数可续写 ──


def test_demoted_partial_closure_resumes_with_original_params(
    tmp_path, verified_scheduler, monkeypatch,
):
    """fresh success 被机械降 partial、clean 冻结失败后，用**原样**参数重试
    必须走 resumed 续写而不是 operation_closure_conflict —— complete 幂等分支
    对此写了 requested_outcome 回退，partial 分支必须同一把尺。"""
    state = _state(tmp_path, "demoted closure survives a mid-freeze crash")
    state.save_artifact("job_submission", "s1", json.dumps(_submission()))
    original_freeze = oc._freeze_artifact
    calls = {"n": 0}

    async def fail_once_on_clean(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"status": "error", "error": "injected clean freeze interruption"}
        return await original_freeze(*args, **kwargs)

    monkeypatch.setattr(oc, "_freeze_artifact", fail_once_on_clean)
    params = {"checks": [{"name": "declared_gap", "passed": False,
                          "evidence": "intentional failing check to force demotion"}]}
    first = _record(state, **params)
    assert first["status"] == "error", first

    monkeypatch.setattr(oc, "_freeze_artifact", original_freeze)
    resumed = _record(state, **params)  # 原样参数：outcome 仍是默认 success

    assert resumed["status"] == "success", resumed
    log = state.read_artifact(resumed["experiment_log_artifact_id"])
    frozen_input = (log.get("metadata") or {}).get("operation_closure_input") or {}
    # 续写后的生效 outcome 仍是降级值，不被请求值抬回。
    assert frozen_input.get("outcome") == "partial"
    assert frozen_input.get("outcome_demoted_from") == "success"


# ── §5 教育错误的 mismatched 清单与匹配谓词同一把尺（casefold） ──────────────


def test_mismatched_fields_ignore_launch_host_case_drift(
    tmp_path, verified_scheduler,
):
    state = _state(tmp_path, "case drift is not a mismatch")
    state.save_artifact("job_submission", "s1", json.dumps(_submission()))

    wrong = _record(state, external_job_refs=[{
        "scheduler": "local", "job_id": "4242",
        "launch_host": "NODE20",          # 仅大小写漂移：匹配谓词视为相等
        "namespace": "wrong-ns",          # 真实差异：驱动拒绝
    }])

    assert wrong["status"] == "error"
    details = wrong.get("details") or {}
    mismatched = details.get("mismatched_fields") or []
    assert "namespace" in mismatched
    assert "launch_host" not in mismatched
