"""纠正 lineage（方案 C，用户 2026-09-13 定）：依据按 attempt 从各冻结路线版本收集，读取失败关闭，钉住跟随步骤契约。

审判复现（.hf-879-test/acceptance/review-0913-judge/witness/，56 场景）：
- A 伪造完整成功：合法纠正后不带 basis 把 expected_outputs 改回原路径、模型写出该文件，
  路线 complete、operation 门禁放行；先重新核验再改回，则卡进 invalid_event_history。
- B 不变量 2：纠正后任何无关修订，已验证外部步骤回到 pending 并进 ready（诱发重提作业）。
- C（参考审查 probe_h）：第二个步骤带 basis 纠正，第一个已纠正步骤回到 pending。

方案 C 的状态约定：纠正过的外部步骤，契约不变 → verified；契约改回作业执行时的原样 →
失败照旧、路线 blocked，出口是带 basis 重新纠正；契约改成别的样子 → pending（新契约没
执行过，与所有已验证外部步骤一致），改回纠正时那份契约 → 恢复 verified。
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools import operation_completion as oc
from test_external_route_projection import (
    _CORRECTION_ARGS, _events, _repoint, _repoint_failure, _state,
)

_IDENTITY_KEYS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id",
    "process_start_ticks", "container_runtime_id",
)


def _finalize(state, reference):
    return er.record_external_route_finalization(
        state, **{key: reference[key] for key in _IDENTITY_KEYS},
        domain_outcome="operation_completed",
        evidence_artifact_id="external_job_operation_closure__receipt")


def _reverify(state, reference):
    return er.record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)


def _corrected(tmp_path, variant):
    """外部作业报 expected_outputs_missing 之后，做一次合法改指向纠正。"""
    if variant == "repoint":
        state, route, binding, reference, workdir, _job = _repoint_failure(tmp_path)
        outputs = [f"jobs/{binding['attempt_id']}_solver/solver.out"]
    else:
        state, route, binding, reference, workdir, _job = _repoint_failure(
            tmp_path, write_job_files=False)
        outputs = []
    result = _repoint(state, route, binding, outputs)
    assert result["status"] == "success", result
    corrected = json.loads(json.dumps(route))
    corrected["steps"][0]["expected_outputs"] = list(outputs)
    return state, corrected, binding, reference, workdir, outputs


def _redeclare(state, route, mutate):
    revised = json.loads(json.dumps(route))
    mutate(revised)
    result = asyncio.run(er._declare_execution_route(
        state, route=revised, amendment_reason="修订路线（不带 recovery_basis）"))
    assert result["status"] == "success", result
    return revised


def _set_outputs(outputs):
    def mutate(route):
        route["steps"][0]["expected_outputs"] = list(outputs)
    return mutate


def _run(state):
    snapshot = er.build_route_snapshot(state)
    return snapshot, snapshot["steps"]["run"]


@pytest.mark.parametrize("reverify_first", [False, True])
@pytest.mark.parametrize("variant", ["repoint"])
def test_restoring_the_original_path_cannot_reuse_the_corrected_outcome(
    tmp_path, variant, reverify_first,
):
    """缺陷 A：改回原路径并自己写出文件，旧结果不得冒充新声明成功。"""
    state, route, _binding, reference, workdir, _outputs = _corrected(tmp_path, variant)
    if reverify_first:
        assert _reverify(state, reference)["status"] == "success"
    _redeclare(state, route, _set_outputs(["logs/solver.out"]))
    forged = workdir / "logs" / "solver.out"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text("FORGED by the model\n", encoding="utf-8")

    _reverify(state, reference)
    _finalize(state, reference)

    snapshot, run = _run(state)
    assert run["state"] == "failed", run
    assert run["reason"] == "expected_outputs_missing", run
    assert snapshot["route_state"] == "blocked", snapshot["route_state"]
    assert oc._route_completion_verification(state).get("ok") is not True


@pytest.mark.parametrize("amendment", ["goal", "added_step"])
@pytest.mark.parametrize("variant", ["repoint"])
def test_an_unrelated_amendment_keeps_the_corrected_step_verified(tmp_path, variant, amendment):
    """缺陷 B（不变量 2）：与该步骤无关的修订不改变它的状态，更不诱发重提。"""
    state, route, *_rest = _corrected(tmp_path, variant)
    bound_before = len(_events(state, "route_step_bound"))

    def mutate(revised):
        if amendment == "goal":
            revised["goal"] = revised["goal"] + "（措辞修订）"
        else:
            revised["steps"].append({
                "id": "report", "goal": "汇总作业结果", "after": ["run"],
                "action": {"tool": "safe_run_bash", "program": "python"},
                "effects": ["process_tree", "workspace_write"],
                "workdir_role": "run_root", "expected_outputs": [],
            })

    _redeclare(state, route, mutate)

    snapshot, run = _run(state)
    assert run["state"] == "verified", run
    assert run.get("recovered_from_external_attempt") is True
    assert "run" not in snapshot["ready_step_ids"]
    assert len(_events(state, "route_step_bound")) == bound_before


def test_changing_external_corrected_step_goal_invalidates_reuse(tmp_path):
    state, route, binding, _reference, _workdir, _outputs = _corrected(
        tmp_path, "repoint")
    changed = _redeclare(
        state,
        route,
        lambda revised: revised["steps"][0].__setitem__(
            "goal", "run the solver for a different scientific intent"),
    )

    snapshot, run = _run(state)

    assert run["state"] == "pending", run
    assert run.get("attempt_id") != binding["attempt_id"]
    assert run.get("recovered_from_external_attempt") is not True
    assert snapshot["ready_step_ids"] == ["run"]

    _redeclare(
        state,
        changed,
        lambda revised: revised["steps"][0].__setitem__(
            "goal", route["steps"][0]["goal"]),
    )
    _snapshot, restored = _run(state)
    assert restored["state"] == "verified", restored
    assert restored["attempt_id"] == binding["attempt_id"]


@pytest.mark.parametrize("legacy_producer", ["current", "different"])
def test_legacy_external_receipt_derives_full_identity_from_frozen_route(
    tmp_path, monkeypatch, legacy_producer,
):
    state, route, binding, _reference, _workdir, _outputs = _corrected(
        tmp_path, "repoint")
    artifact_versions = state.artifact_versions

    def legacy_versions(artifact_id):
        versions = deepcopy(artifact_versions(artifact_id))
        for version in versions:
            receipt = (version.get("metadata") or {}).get(
                "validated_recovery_receipt")
            if isinstance(receipt, dict):
                receipt.pop("next_step_definition_hash", None)
                if legacy_producer == "different":
                    version["produced_by_run_id"] = "legacy-original-run"
        return versions

    monkeypatch.setattr(state, "artifact_versions", legacy_versions)

    _snapshot, unchanged = _run(state)
    assert unchanged["state"] == "verified", unchanged
    assert unchanged["attempt_id"] == binding["attempt_id"]

    _redeclare(
        state,
        route,
        lambda revised: revised["steps"][0].__setitem__(
            "goal", "run the solver for a different scientific intent"),
    )
    snapshot, changed = _run(state)
    assert changed["state"] == "pending", changed
    assert changed.get("attempt_id") != binding["attempt_id"]
    assert snapshot["ready_step_ids"] == ["run"]


@pytest.mark.parametrize("damage", ["unreadable_content", "content_hash_mismatch"])
def test_legacy_external_receipt_fails_closed_when_frozen_route_is_damaged(
    tmp_path, monkeypatch, damage,
):
    state, _route, binding, _reference, _workdir, _outputs = _corrected(
        tmp_path, "repoint")
    artifact_versions = state.artifact_versions

    def damaged_legacy_versions(artifact_id):
        versions = deepcopy(artifact_versions(artifact_id))
        for version in versions:
            receipt = (version.get("metadata") or {}).get(
                "validated_recovery_receipt")
            if not isinstance(receipt, dict):
                continue
            receipt.pop("next_step_definition_hash", None)
            if damage == "unreadable_content":
                version["content"] = None
            else:
                version["content_hash"] = "0" * 64
        return versions

    monkeypatch.setattr(
        state, "artifact_versions", damaged_legacy_versions)

    snapshot, run = _run(state)
    assert run["state"] == "pending", run
    assert run.get("attempt_id") != binding["attempt_id"]
    assert snapshot["ready_step_ids"] == ["run"]


def test_same_attempt_uses_only_the_latest_correction_receipt(
    tmp_path, monkeypatch,
):
    state, route, binding, _reference, _workdir, outputs = _corrected(
        tmp_path, "repoint")
    original = _redeclare(
        state, route, _set_outputs(["logs/solver.out"]))
    assert _repoint(state, original, binding, outputs)["status"] == "success"
    artifact_versions = state.artifact_versions

    def invalid_latest_head(artifact_id):
        versions = deepcopy(artifact_versions(artifact_id))
        receipts = [
            version
            for version in versions
            if isinstance(
                (version.get("metadata") or {}).get(
                    "validated_recovery_receipt"),
                dict,
            )
        ]
        assert len(receipts) >= 2
        latest = max(receipts, key=lambda version: version["version"])
        latest["metadata"]["validated_recovery_receipt"][
            "next_step_definition_hash"] = "0" * 64
        return versions

    monkeypatch.setattr(state, "artifact_versions", invalid_latest_head)

    snapshot, run = _run(state)
    assert run["state"] == "pending", run
    assert run.get("recovered_from_external_attempt") is not True
    assert snapshot["ready_step_ids"] == ["run"]


def test_changing_the_corrected_contract_returns_to_pending_and_back(tmp_path):
    """新契约没执行过 → pending；改回纠正时那份契约 → 恢复 verified，不需要 basis。"""
    state, route, _binding, reference, workdir, outputs = _corrected(tmp_path, "repoint")
    changed = _redeclare(state, route, _set_outputs(["fabricated.out"]))
    (workdir / "fabricated.out").write_text("FORGED by the model\n", encoding="utf-8")

    _snapshot, run = _run(state)
    assert run["state"] == "pending", run
    locked = _reverify(state, reference)
    assert locked["status"] != "success", locked

    _redeclare(state, changed, _set_outputs(outputs))
    snapshot, run = _run(state)
    assert run["state"] == "verified", run
    assert "run" not in snapshot["ready_step_ids"]


def test_a_dropped_pin_leaves_a_fresh_correction_as_the_exit(tmp_path):
    """钉住之后改回原样：钉住被丢弃、失败保留（不是 invalid_event_history）；重新纠正收敛。"""
    state, route, binding, reference, _workdir, outputs = _corrected(tmp_path, "repoint")
    assert _reverify(state, reference)["status"] == "success"
    original = _redeclare(state, route, _set_outputs(["logs/solver.out"]))

    snapshot, run = _run(state)
    assert snapshot["route_state"] == "blocked", snapshot
    assert run["state"] == "failed", run

    again = _repoint(state, original, binding, outputs)
    assert again["status"] == "success", again
    assert _reverify(state, reference)["status"] == "success"
    snapshot, run = _run(state)
    assert run["state"] == "verified", run
    events = [json.loads(line) for line in
              state.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    active = er._active_attempt_projection_events(
        state, events, event_type="route_step_external_execution_verified",
        attempt_id=binding["attempt_id"])
    assert len(active) == 1 and active[0]["route_outcome"] == "success", active


def test_a_second_correction_does_not_undo_the_first(tmp_path):
    """缺陷 C：两个外部步骤先后改指向，第一个不会因为第二次纠正回到 pending。"""
    state = _state(tmp_path)

    def step(step_id, program):
        return {"id": step_id, "goal": f"运行 {program}", "after": [],
                "action": {"tool": "submit_job", "program": program},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [f"logs/{step_id}_result.dat"]}

    route = {"schema_version": 2, "goal": "两个受管外部作业",
             "evidence_refs": ["https://example.invalid/guide"],
             "steps": [step("a", "solver_a"), step("b", "solver_b")]}
    assert asyncio.run(er._declare_execution_route(state, route=route))["status"] == "success"
    runtime = state.root / "outputs" / "experiment" / "runtime"
    attempts = {}
    for step_id, program, job in (("a", "solver_a", "1001"), ("b", "solver_b", "1002")):
        decision = er.resolve_execution_context(state, {
            "tool": "submit_job", "program": program, "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"]})
        assert decision["route_step_id"] == step_id, decision
        decision.update({"workdir_role_observed": True,
                         "workdir_resolution_status": "resolved",
                         "resolved_workdir": str(runtime)})
        binding = er.begin_route_step_attempt(
            state, decision, tool="submit_job", action={"payload_digest": f"pd-{step_id}"})
        assert binding and not binding.get("binding_error"), binding
        reference = {"scheduler": "slurm", "job_id": job, "namespace": "research",
                     "launch_host": "login-01", "scheduler_cluster": "cluster-a",
                     "resource_uid": f"slurm-cluster-a-{job}",
                     "submission_nonce": binding["attempt_id"], "process_group_id": None,
                     "process_start_ticks": None, "container_runtime_id": None,
                     "route_attempt_id": binding["attempt_id"]}
        outcome = er.finish_route_step_attempt(
            state, binding, result={"status": "success", **reference,
                                    "submission_artifact_id": f"job_submission__{job}"},
            external_submission=True)
        assert outcome and outcome["outcome"] == "submitted", outcome
        job_dir = runtime / "jobs" / f"{binding['attempt_id']}_{program}"
        job_dir.mkdir(parents=True)
        output = job_dir / f"{step_id}_result.dat"
        output.write_text("converged\n", encoding="utf-8")
        state.save_artifact("job_submission", f"{step_id}_submission", json.dumps({
            "route_attempt_id": binding["attempt_id"],
            "stdout_path": str(output),
        }))
        assert _reverify(state, reference)["reason"] == "route_expected_outputs_missing"
        attempts[step_id] = (binding["attempt_id"],
                             f"jobs/{binding['attempt_id']}_{program}/{step_id}_result.dat")

    current = route
    for step_id in ("a", "b"):
        revised = json.loads(json.dumps(current))
        next(item for item in revised["steps"] if item["id"] == step_id)["expected_outputs"] = [attempts[step_id][1]]
        result = asyncio.run(er._declare_execution_route(
            state, route=revised, amendment_reason=f"{step_id} 的附加声明是虚的",
            recovery_basis={"attempt_id": attempts[step_id][0], "failure_class": "expected_output",
                            "diagnosis": "expected_outputs 写错，作业成功", "evidence_refs": []}))
        assert result["status"] == "success", result
        current = revised

    snapshot = er.build_route_snapshot(state)
    assert snapshot["steps"]["a"]["state"] == "verified", snapshot["steps"]
    assert snapshot["steps"]["b"]["state"] == "verified", snapshot["steps"]
    assert snapshot["ready_step_ids"] == []
    assert snapshot["route_state"] == "complete"


@pytest.mark.parametrize("reverify_first", [False, True])
@pytest.mark.parametrize("variant", ["repoint"])
def test_a_finalized_success_after_correction_is_dropped_with_its_contract(
    tmp_path, variant, reverify_first,
):
    """verify 清单 #1（第三会话复审探针 review-0914-third/probe_checklist_1_2_4 的 4 格）：
    纠正后先 finalize、再不带 basis 改回原契约。原先 finalized 成功事件没钉契约，钉住的核验
    被丢弃后它仍在、与复活的失败冲突，路线卡进 invalid_event_history，run 内无出口。"""
    from test_external_route_projection import _events, _repoint

    state, route, binding, reference, _workdir, outputs = _corrected(tmp_path, variant)
    if reverify_first:
        assert _reverify(state, reference)["status"] == "success"
    assert _finalize(state, reference)["status"] == "success"

    restored = _redeclare(state, route, _set_outputs(["logs/solver.out"]))

    snapshot, run = _run(state)
    assert snapshot["route_state"] == "blocked", snapshot
    assert run["state"] == "failed", run
    finalized = _events(state, "route_step_external_finalized")[-1]
    assert finalized["supersession_basis_validated"] is True
    assert finalized["supersession_step_execution_contract_hash"]
    # 被丢弃的钉住收尾不算「已写下路线终态事实」（第三会话复审 0914c P3）。
    assert er.external_finalization_already_projected(state, reference) is False
    # 出口：带 basis 重新纠正 → 重新核验 → 路线 complete。纠正回到同一份契约时，
    # 第一次收尾钉住的成功重新生效，再收尾幂等（原先这里返回 already_projected，路线
    # 却卡在 invalid_event_history，两边说法矛盾）。
    assert _repoint(state, restored, binding, outputs)["status"] == "success"
    assert _reverify(state, reference)["status"] == "success"
    assert er.build_route_snapshot(state)["route_state"] == "complete"
    assert er.external_finalization_already_projected(state, reference) is True
    refinalized = _finalize(state, reference)
    assert refinalized["status"] == "success", refinalized
    assert er.build_route_snapshot(state)["route_state"] == "complete"
