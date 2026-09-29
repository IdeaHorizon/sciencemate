from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import execution_action_census, operation_completion
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    begin_route_step_attempt,
    build_route_snapshot,
    execution_route_block,
    finish_route_step_attempt,
    load_canonical_route,
    record_external_route_execution_verification,
    record_external_route_finalization,
    record_external_route_identity_resolution,
    resolve_execution_context,
)

_RUNTIME_ID_A = "a" * 64
_RUNTIME_ID_B = "b" * 64


def _state(tmp_path: Path) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="external-route-projection",
    )
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["experiment_execution_scope"] = {
        "mode": "operational",
        "category": "other",
    }
    return state


def _route(expected_outputs: list[str] | None = None) -> dict:
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
                "expected_outputs": list(expected_outputs or []),
            }
        ],
    }


def _submitted_route(
    state: State,
    route: dict | None = None,
    *,
    local: bool = False,
    record_census: bool = False,
) -> tuple[dict, dict]:
    declared = asyncio.run(_declare_execution_route(state, route=route or _route()))
    assert declared["status"] == "success"
    decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    assert decision["decision"] == "matched_ready_step", decision
    decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
        }
    )
    binding = begin_route_step_attempt(
        state,
        decision,
        tool="submit_job",
        action={"payload_digest": "external-route-test"},
    )
    assert binding and not binding.get("binding_error")
    action_token = None
    if record_census:
        action_token = execution_action_census.begin_execution_action(
            state,
            {"tool": "submit_job", "program": "solver", "route_step_id": "run"},
            decision,
            route_binding=binding,
        )
        assert action_token["status"] == "success", action_token
    if local:
        reference = {
            "scheduler": "local",
            "job_id": "hf-harness-route-local",
            "namespace": None,
            "launch_host": None,
            "scheduler_cluster": None,
            "resource_uid": None,
            "submission_nonce": binding["attempt_id"],
            "process_group_id": "legacy-process-group",
            "process_start_ticks": "legacy-start-ticks",
            "container_runtime_id": _RUNTIME_ID_A,
            "route_attempt_id": binding["attempt_id"],
        }
    else:
        reference = {
            "scheduler": "slurm",
            "job_id": "31415",
            "namespace": "research",
            "launch_host": "login-01",
            "scheduler_cluster": "cluster-a",
            "resource_uid": "slurm-cluster-a-31415",
            "submission_nonce": binding["attempt_id"],
            "process_group_id": None,
            "process_start_ticks": None,
            "container_runtime_id": None,
            "route_attempt_id": binding["attempt_id"],
        }
    if action_token is not None:
        settled = execution_action_census.settle_execution_action(
            state,
            action_token,
            payload_spawned=None,
            job_submitted=True,
            proof_source="test_scheduler_accepted_identity",
            result={"status": "success", **reference},
        )
        assert settled["passed"] is True, settled
    outcome = finish_route_step_attempt(
        state,
        binding,
        result={
            "status": "success",
            **reference,
            "submission_artifact_id": "job_submission__31415",
        },
        external_submission=True,
    )
    assert outcome and outcome["outcome"] == "submitted"
    return binding, reference


def _success_verification(reference: dict) -> dict:
    return {
        "ok": True,
        "terminal": True,
        "successful": True,
        "external_job_refs": [reference],
        "health": [
            {
                "reference": reference,
                "terminal": True,
                "success_verified": True,
                "success_evidence": {
                    "verified": True,
                    "succeeded": True,
                    "source": "scheduler_accounting",
                    "returncode": 0,
                },
                "scheduler_phase": "terminal",
                "health_status": "success",
            }
        ],
    }


def _persist_reconciled_identity(
    state: State,
    attempt_id: str,
    identity: dict,
    stem: str,
) -> tuple[dict, str]:
    submission_id = state.save_artifact(
        "job_submission",
        f"job_submission__{stem}",
        json.dumps(
            {
                "status": "success",
                "route_attempt_id": attempt_id,
                **identity,
            }
        ),
    )["id"]
    reconciliation_id = state.save_artifact(
        "external_job_submission_recovery",
        f"submission_recovery__{stem}",
        json.dumps(
            {
                "route_attempt_id": attempt_id,
                "submission_nonce": attempt_id,
            }
        ),
    )["id"]
    return {**identity, "submission_artifact_id": submission_id}, reconciliation_id


def _events(state: State, event_name: str) -> list[dict]:
    # 收尾在写任何东西之前就被拒时，这个 run 一行 transcript 都没有 —— 文件不存在
    # 就是「零事件」，正是「不许留下痕迹」那类断言要表达的意思，不该炸成
    # FileNotFoundError。
    if not state.transcript_path.is_file():
        return []
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == event_name
    ]


def _bound_state(tmp_path: Path) -> State:
    """真实派发出来的 run：带非空 node_inputs，scope 是 v1 且意图已绑定。

    `_state()` 造的是**没有 intent_schema_version 的老式 scope**。23a0fe78 之后
    operation 收尾要求当前 v1 绑定（legacy 只能读、不能授权新写入，见
    test_legacy_operation_scope_cannot_create_a_new_closure），所以凡是断言
    「收尾成功」的用例都必须用这个。这里逐字复刻 classify_experiment_scope 写下的
    形状，而不是手搓一个 digest。
    """
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "task": "验证并关闭受管外部作业",
        "route_step_id": "run",
    }
    state.hook_state.pop("experiment_execution_scope", None)
    # Do not hand-forge an authority projection: exercise the production
    # write-once acceptance path exactly as a real dispatched run does.
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Verify and close one managed external job.",
    ))
    assert classified["status"] == "success", classified
    return state


def test_terminal_success_projects_route_before_operation_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = _bound_state(tmp_path)
    binding, reference = _submitted_route(state)
    assert build_route_snapshot(state)["route_state"] == "in_progress"
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: _success_verification(reference),
    )

    completion = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="external_job",
            objective="验证并关闭受管外部作业",
            outcome="success",
            external_job_refs=[reference],
        )
    )

    assert completion["status"] == "success", completion
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["run"]["state"] == "verified"
    projected = _events(state, "route_step_external_execution_verified")
    assert len(projected) == 1
    assert projected[0]["attempt_id"] == binding["attempt_id"]
    assert projected[0]["route_attempt_id"] == binding["attempt_id"]
    assert projected[0]["launch_host"] == "login-01"
    assert projected[0]["scheduler_cluster"] == "cluster-a"
    assert projected[0]["resource_uid"] == "slurm-cluster-a-31415"
    assert projected[0]["submission_nonce"] == binding["attempt_id"]

    archived = record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference["namespace"],
        launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        domain_outcome="operation_completed",
        evidence_artifact_id=completion["experiment_log_artifact_id"],
    )
    repeated = record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference["namespace"],
        launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        domain_outcome="operation_completed",
        evidence_artifact_id=completion["experiment_log_artifact_id"],
    )
    assert archived["status"] == "success"
    assert repeated["status"] == "success"
    assert repeated["already_projected"] is True
    assert len(_events(state, "route_step_external_execution_verified")) == 1
    assert len(_events(state, "route_step_external_finalized")) == 1
    assert build_route_snapshot(state)["route_state"] == "complete"


def test_legacy_finalization_only_transcript_remains_readable(tmp_path: Path):
    """升级前只有 finalization 投影的 run 仍能恢复为已验证终态。"""
    state = _state(tmp_path)
    binding, reference = _submitted_route(state)
    assert _events(state, "route_step_external_execution_verified") == []

    state.append_transcript(
        "route_step_external_finalized",
        attempt_id=binding["attempt_id"],
        **reference,
        domain_outcome="operation_completed",
        route_outcome="success",
        evidence_artifact_id="experiment_log__legacy_finalization_only",
        verified_output_specs=[],
        verified_outputs=[],
    )
    transcript_before_read = state.transcript_path.read_text(encoding="utf-8")

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["run"]["state"] == "verified"
    assert snapshot["steps"]["run"]["external_outcome"] == (
        "operation_completed"
    )
    assert _events(state, "route_step_external_execution_verified") == []
    assert len(_events(state, "route_step_external_finalized")) == 1
    assert state.transcript_path.read_text(encoding="utf-8") == (
        transcript_before_read
    )


def test_toolchain_build_external_refs_project_route_and_persist_exact_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A build backed by a managed job retains its exact closure identity."""
    state = _bound_state(tmp_path)
    # P0a v4：声明的 build 产物必须映射到满足义务的 attempt 收尾时冻结的身份收据——
    # 外部作业的收据随 route_step_external_execution_verified 写下，只覆盖该步骤
    # expected_outputs 里、在 resolved_workdir 内的产物。原写法把产物放在 tmp_path
    # 根下、路线也没声明它：v4 之前从没被断言过（红 0 条 = 盲区），不是被钉住的选择。
    binding, reference = _submitted_route(
        state, _route(expected_outputs=["toolchain-smoke"]), record_census=True)
    executable = state.root / "outputs" / "experiment" / "runtime" / "toolchain-smoke"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: _success_verification(reference),
    )

    completion = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="toolchain_build",
            objective="构建并关闭受管 toolchain smoke 作业",
            outcome="success",
            executable_paths=[str(executable)],
            external_job_refs=[reference],
        )
    )

    assert completion["status"] == "success", completion
    assert build_route_snapshot(state)["route_state"] == "complete"
    assert len(_events(state, "route_step_external_execution_verified")) == 1
    assert any(
        item["name"] == "external_jobs_successful" and item["passed"] is True
        for item in completion["checks"]
    )
    clean = json.loads(state.read_artifact(completion["clean_results_artifact_id"])["content"])
    assert clean["task_kind"] == "build"
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    assert (log.get("metadata") or {}).get("external_job_refs") == [reference]

    finalized = record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference["namespace"],
        launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        domain_outcome="operation_completed",
        evidence_artifact_id=completion["experiment_log_artifact_id"],
    )
    assert finalized["status"] == "success", finalized
    assert len(_events(state, "route_step_external_finalized")) == 1


def test_external_expected_output_contract_correction_cannot_clear_persisted_missing(
    tmp_path: Path,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"][0]["expected_outputs"] = ["phantom-output.dat"]
    binding, reference = _submitted_route(state, route)

    projected = record_external_route_execution_verification(
        state,
        external_job_ref=reference,
        terminal=True,
        success_verified=True,
        success_evidence={
            "verified": True,
            "succeeded": True,
            "source": "scheduler_accounting",
            "returncode": 0,
        },
    )

    assert projected["status"] == "error"
    assert projected["reason"] == "route_expected_outputs_missing"
    blocked = build_route_snapshot(state)
    assert blocked["route_state"] == "blocked"
    assert blocked["steps"]["run"]["reason"] == "expected_outputs_missing"

    stopped = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "solver",
        "route_step_id": "run",
        "read_only": False,
        "observed_effects": [
            "external_job", "process_tree", "workspace_write",
        ],
        "workdir_roles": ["run_root"],
    })
    # This fixture predates immutable input receipts; isolate the route refusal
    # under test from that orthogonal pre-execution gate.
    refusal = execution_route_block({
        **stopped, "scope_required": False,
    })
    assert refusal is not None
    assert refusal["expected_output_correction"]["mode"] == (
        "validated_external_attempt_receipt_no_resubmit")
    assert "本地纯纠正不适用" in refusal["error"]
    assert "managed_external_job" in refusal["error"]
    assert "不要重新提交" in refusal["error"]
    assert "只改变该步骤 expected_outputs" in refusal["error"]
    for misleading in ("step_definition_hash", "route.evidence_refs"):
        assert misleading not in refusal["error"]

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = []
    amended = asyncio.run(
        _declare_execution_route(
            state,
            route=revised,
            amendment_reason="官方入口只承诺成功终态，不承诺额外文件",
            recovery_basis={
                "attempt_id": binding["attempt_id"],
                "failure_class": "expected_output",
                "diagnosis": "旧 expected_outputs 是任务未要求的虚假附加条件",
                "evidence_refs": [],
            },
        )
    )

    assert amended["status"] == "error"
    assert amended["error_code"] == "route_recovery_basis_required"
    assert "report_blocker" in str(amended["violations"])
    assert build_route_snapshot(state)["route_state"] == "blocked"


@pytest.mark.parametrize(
    ("terminal", "success_verified", "evidence"),
    [
        (False, False, {"verified": False, "reason": "scheduler_unknown"}),
        (True, False, {"verified": True, "succeeded": False, "returncode": 7}),
        (True, False, {"verified": False, "reason": "lifecycle_cancelled"}),
    ],
)
def test_failure_cancel_and_unknown_never_complete_route(
    tmp_path: Path,
    terminal: bool,
    success_verified: bool,
    evidence: dict,
):
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)

    result = record_external_route_execution_verification(
        state,
        external_job_ref=reference,
        terminal=terminal,
        success_verified=success_verified,
        success_evidence=evidence,
    )

    assert result["status"] == "error"
    assert result["reason"] == "external_execution_not_successfully_verified"
    assert build_route_snapshot(state)["route_state"] == "in_progress"
    assert not _events(state, "route_step_external_execution_verified")


def test_projection_requires_full_identity_and_exact_route_attempt(tmp_path: Path):
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)
    incomplete = dict(reference)
    incomplete.pop("launch_host")
    wrong_attempt = {**reference, "route_attempt_id": "route-wrong"}

    missing_scope = record_external_route_execution_verification(
        state,
        external_job_ref=incomplete,
        terminal=True,
        success_verified=True,
        success_evidence={"verified": True, "succeeded": True, "source": "test"},
    )
    wrong_binding = record_external_route_execution_verification(
        state,
        external_job_ref=wrong_attempt,
        terminal=True,
        success_verified=True,
        success_evidence={"verified": True, "succeeded": True, "source": "test"},
    )

    assert missing_scope["status"] == "error"
    assert missing_scope["reason"] == "route_external_identity_mismatch"
    assert wrong_binding["status"] == "error"
    assert wrong_binding["reason"] == "route_external_attempt_mismatch"
    assert build_route_snapshot(state)["route_state"] == "in_progress"


def test_projection_is_idempotent_but_conflicting_receipt_fails_closed(tmp_path: Path):
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)
    args = {
        "external_job_ref": reference,
        "terminal": True,
        "success_verified": True,
        "success_evidence": {
            "verified": True,
            "succeeded": True,
            "source": "scheduler_accounting",
            "returncode": 0,
        },
    }

    first = record_external_route_execution_verification(state, **args)
    repeated = record_external_route_execution_verification(state, **args)
    conflict = record_external_route_execution_verification(
        state,
        **{
            **args,
            "success_evidence": {
                "verified": True,
                "succeeded": True,
                "source": "different_receipt",
            },
        },
    )

    assert first["status"] == "success"
    assert repeated["already_projected"] is True
    assert conflict["status"] == "error"
    assert conflict["reason"] == "route_external_execution_verification_conflict"
    assert len(_events(state, "route_step_external_execution_verified")) == 1
    assert build_route_snapshot(state)["route_state"] == "complete"


def test_local_route_identity_uses_immutable_container_id_not_pid(
    tmp_path: Path,
):
    state = _state(tmp_path)
    binding, reference = _submitted_route(state, local=True)
    evidence = {"verified": True, "succeeded": True, "source": "test"}

    missing_runtime = record_external_route_execution_verification(
        state,
        external_job_ref={**reference, "container_runtime_id": None},
        terminal=True,
        success_verified=True,
        success_evidence=evidence,
    )
    wrong_runtime = record_external_route_execution_verification(
        state,
        external_job_ref={**reference, "container_runtime_id": _RUNTIME_ID_B},
        terminal=True,
        success_verified=True,
        success_evidence=evidence,
    )
    verified = record_external_route_execution_verification(
        state,
        external_job_ref={
            **reference,
            "process_group_id": "caller-controlled-pgid",
            "process_start_ticks": "caller-controlled-start-ticks",
        },
        terminal=True,
        success_verified=True,
        success_evidence=evidence,
    )

    assert missing_runtime == {
        "status": "error",
        "reason": "route_local_container_identity_incomplete",
    }
    assert wrong_runtime["status"] == "error"
    assert wrong_runtime["reason"] == "route_external_identity_mismatch"
    assert verified["status"] == "success"
    assert verified["attempt_id"] == binding["attempt_id"]
    execution = _events(state, "route_step_external_execution_verified")[0]
    assert execution["container_runtime_id"] == _RUNTIME_ID_A
    assert execution["process_group_id"] is None
    assert execution["process_start_ticks"] is None

    incomplete_finalization = record_external_route_finalization(
        state,
        scheduler="local",
        job_id=reference["job_id"],
        namespace=None,
        submission_nonce=reference["submission_nonce"],
        domain_outcome="operation_completed",
        evidence_artifact_id="evidence-local",
    )
    finalized = record_external_route_finalization(
        state,
        scheduler="local",
        job_id=reference["job_id"],
        namespace=None,
        submission_nonce=reference["submission_nonce"],
        process_group_id="different-pgid",
        process_start_ticks="different-start-ticks",
        container_runtime_id=_RUNTIME_ID_A,
        domain_outcome="operation_completed",
        evidence_artifact_id="evidence-local",
    )

    assert incomplete_finalization == {
        "status": "error",
        "reason": "route_local_container_identity_incomplete",
    }
    assert finalized["status"] == "success"
    archived = _events(state, "route_step_external_finalized")[0]
    assert archived["container_runtime_id"] == _RUNTIME_ID_A
    assert archived["process_group_id"] is None
    assert archived["process_start_ticks"] is None


def test_unbound_intent_rejection_tells_the_run_who_can_actually_fix_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """调用方没给 node_inputs 时，拒绝必须指向调用方，而不是给一句现场做不到的指令。

    成因：`run_node` 的 schema 里 node_inputs 不是必填（required 只有 node_type 和
    user_note），所以「不带 node_inputs 派发 experiment」是框架允许的动作 —— 但派出来
    的 run 在分类时就拿不到可核验意图，operation 收尾会被
    execution_intent_binding_required 挡住，而它**改不了别人给自己的输入**。

    原文案说的是「real effects require a new run with node_inputs」。
    shared/tools/run_node.py:733 记着这个坑的代价：experiment 收到「请在 node_inputs
    里指名」却改不了自己的 node_inputs，于是原样重派、一模一样地再失败。
    所以这里钉死两件事：拒绝理由说清「不是你能补救的」，next_step 指向 report_blocker。
    """
    state = _state(tmp_path)
    state.hook_state.pop("experiment_execution_scope", None)
    from nodes.experiment.tools.run_contract import _classify_experiment_scope
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Classify a dispatched operation whose caller omitted node_inputs.",
    ))
    assert classified["status"] == "success", classified
    reference = {
        "scheduler": "local",
        "job_id": "5432",
        "namespace": None,
        "launch_host": "node-01",
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": "unbound-intent",
        "container_runtime_id": _RUNTIME_ID_A,
        "route_attempt_id": None,
    }
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: _success_verification(reference),
    )

    completion = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="external_job",
            objective="上游没给 node_inputs 的 operation",
            outcome="success",
            external_job_refs=[reference],
        )
    )

    assert completion["status"] == "error", completion
    assert completion["error_code"] == "execution_intent_binding_required"
    assert (completion["execution_intent_binding"]["status"]
            == "intent_unavailable_at_classification")
    # 门没有被放宽：什么都没写下去
    assert not list(state.list_artifacts("raw_results"))
    # 但拒绝是可执行的
    assert "node_inputs" in completion["error"]
    assert "不是本 run 能自行补救的" in completion["error"]
    assert "report_blocker" in completion["next_step"]
    assert "不要重跑" in completion["next_step"]


def test_legacy_operation_scope_cannot_create_a_new_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = _state(tmp_path)
    reference = {
        "scheduler": "local",
        "job_id": "9876",
        "namespace": None,
        "launch_host": "node-01",
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": "legacy-no-route",
        "container_runtime_id": _RUNTIME_ID_A,
        "route_attempt_id": None,
    }
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: _success_verification(reference),
    )

    completion = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="external_job",
            objective="兼容没有 declared route 的旧 operation",
            outcome="success",
            external_job_refs=[reference],
        )
    )

    assert completion["status"] == "error", completion
    assert completion["error_code"] == "execution_intent_binding_required"
    assert completion["execution_intent_binding"]["status"] == "legacy_unverifiable"
    assert not list(state.list_artifacts("raw_results"))
    assert not _events(state, "route_step_external_execution_verified")


@pytest.mark.parametrize("persist_unknown_outcome", [True, False])
def test_identity_resolution_recovers_unknown_or_spawn_interrupted_attempt(
    tmp_path: Path,
    persist_unknown_outcome: bool,
):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    assert declared["status"] == "success"
    decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
        }
    )
    binding = begin_route_step_attempt(state, decision, tool="submit_job")
    assert binding and not binding.get("binding_error")
    action_token = execution_action_census.begin_execution_action(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "route_step_id": "run",
        },
        decision,
        route_binding=binding,
    )
    assert action_token["status"] == "success", action_token
    scheduler = "local" if persist_unknown_outcome else "slurm"
    if persist_unknown_outcome:
        outcome = finish_route_step_attempt(
            state,
            binding,
            result={
                "status": "accepted_identity_unresolved",
                "scheduler": scheduler,
                "job_id": None,
            },
            external_submission=True,
        )
        assert outcome and outcome["outcome"] == "unknown"
    settled = execution_action_census.settle_execution_action(
        state,
        action_token,
        payload_spawned=None,
        job_submitted=True if persist_unknown_outcome else None,
        proof_source="submit_sync.scheduler_acceptance",
        result=(
            {"status": "accepted_identity_unresolved"}
            if persist_unknown_outcome
            else None
        ),
        error=(None if persist_unknown_outcome else RuntimeError("submit response lost")),
    )
    assert settled["passed"] is True, settled
    assert build_route_snapshot(state)["route_state"] == "interrupted"
    identity = {
        "scheduler": "slurm",
        "job_id": "271828",
        "namespace": "research",
        "launch_host": "login-02",
        "scheduler_cluster": "cluster-a",
        "resource_uid": "slurm-cluster-a-271828",
        "submission_nonce": binding["attempt_id"],
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": None,
    }
    if scheduler == "local":
        identity = {
            "scheduler": "local",
            "job_id": "hf-harness-route-recovered",
            "namespace": None,
            "launch_host": None,
            "scheduler_cluster": None,
            "resource_uid": None,
            "submission_nonce": binding["attempt_id"],
            "process_group_id": "legacy-process-group",
            "process_start_ticks": "legacy-start-ticks",
            "container_runtime_id": _RUNTIME_ID_A,
        }
    receipt, reconciliation_id = _persist_reconciled_identity(
        state,
        binding["attempt_id"],
        identity,
        "reconciled_271828",
    )

    first = record_external_route_identity_resolution(
        state,
        route_attempt_id=binding["attempt_id"],
        domain_receipt=receipt,
        reconciliation_artifact_id=reconciliation_id,
    )
    repeated = record_external_route_identity_resolution(
        state,
        route_attempt_id=binding["attempt_id"],
        domain_receipt=receipt,
        reconciliation_artifact_id=reconciliation_id,
    )

    assert first["status"] == "success"
    assert repeated["already_projected"] is True
    resolved = _events(state, "route_step_external_identity_resolved")
    assert len(resolved) == 1
    if scheduler == "local":
        assert resolved[0]["container_runtime_id"] == _RUNTIME_ID_A
        assert resolved[0]["process_group_id"] is None
        assert resolved[0]["process_start_ticks"] is None
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "in_progress"
    assert snapshot["steps"]["run"]["state"] == "in_progress"

    verified = record_external_route_execution_verification(
        state,
        external_job_ref={**receipt, "route_attempt_id": binding["attempt_id"]},
        terminal=True,
        success_verified=True,
        success_evidence={
            "verified": True,
            "succeeded": True,
            "source": "scheduler_accounting",
            "returncode": 0,
        },
    )
    assert verified["status"] == "success", verified
    assert build_route_snapshot(state)["route_state"] == "complete"
    obligation = execution_action_census.operation_execution_obligation(state)
    assert obligation["passed"] is True, obligation
    assert obligation["real_execution_obligation_satisfied"] is True
    assert obligation["action_census"]["actions"][0]["terminal_status"] in {
        "unknown",
        "error",
    }


def test_identity_resolution_conflict_and_already_submitted_attempt_fail_closed(
    tmp_path: Path,
):
    state = _state(tmp_path)
    binding, reference = _submitted_route(state)
    receipt, known_recovery_id = _persist_reconciled_identity(
        state, binding["attempt_id"], reference, "known_31415"
    )

    already_known = record_external_route_identity_resolution(
        state,
        route_attempt_id=binding["attempt_id"],
        domain_receipt=receipt,
        reconciliation_artifact_id=known_recovery_id,
    )

    assert already_known["status"] == "error"
    assert already_known["reason"] == "route_attempt_not_identity_unresolved"

    unresolved = _state(tmp_path / "conflict")
    declared = asyncio.run(_declare_execution_route(unresolved, route=_route()))
    assert declared["status"] == "success"
    decision = resolve_execution_context(
        unresolved,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(unresolved.root / "runtime"),
        }
    )
    unresolved_binding = begin_route_step_attempt(unresolved, decision, tool="submit_job")
    assert unresolved_binding
    finish_route_step_attempt(
        unresolved,
        unresolved_binding,
        result={"status": "submission_outcome_unknown", "scheduler": "slurm"},
        external_submission=True,
    )
    base, recovery_111 = _persist_reconciled_identity(
        unresolved,
        unresolved_binding["attempt_id"],
        {
            "scheduler": "slurm",
            "job_id": "111",
            "namespace": "research",
            "launch_host": "login-01",
            "scheduler_cluster": "cluster-a",
            "resource_uid": "slurm-cluster-a-111",
            "submission_nonce": unresolved_binding["attempt_id"],
            "process_group_id": None,
            "process_start_ticks": None,
        },
        "111",
    )
    assert (
        record_external_route_identity_resolution(
            unresolved,
            route_attempt_id=unresolved_binding["attempt_id"],
            domain_receipt=base,
            reconciliation_artifact_id=recovery_111,
        )["status"]
        == "success"
    )
    conflicting_receipt, recovery_222 = _persist_reconciled_identity(
        unresolved,
        unresolved_binding["attempt_id"],
        {
            **base,
            "job_id": "222",
            "resource_uid": "slurm-cluster-a-222",
        },
        "222",
    )
    conflict = record_external_route_identity_resolution(
        unresolved,
        route_attempt_id=unresolved_binding["attempt_id"],
        domain_receipt=conflicting_receipt,
        reconciliation_artifact_id=recovery_222,
    )

    assert conflict["status"] == "error"
    assert conflict["reason"] == "route_external_identity_resolution_conflict"
    assert build_route_snapshot(unresolved)["route_state"] == "in_progress"


def test_identity_resolution_rejects_wrong_nonce_and_fake_current_run_artifact(
    tmp_path: Path,
):
    state = _state(tmp_path)
    assert asyncio.run(_declare_execution_route(state, route=_route()))["status"] == "success"
    decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(state.root / "runtime"),
        }
    )
    binding = begin_route_step_attempt(state, decision, tool="submit_job")
    assert binding
    finish_route_step_attempt(
        state,
        binding,
        result={"status": "submission_outcome_unknown", "scheduler": "slurm"},
        external_submission=True,
    )
    fake_id = state.save_artifact(
        "diagnostic_evidence",
        "not_a_submission_receipt",
        json.dumps(
            {
                "status": "success",
                "route_attempt_id": binding["attempt_id"],
                "submission_nonce": binding["attempt_id"],
            }
        ),
    )["id"]
    identity = {
        "scheduler": "slurm",
        "job_id": "999",
        "namespace": "research",
        "launch_host": "login-09",
        "scheduler_cluster": "cluster-a",
        "resource_uid": "slurm-cluster-a-999",
        "submission_nonce": "wrong-nonce",
        "process_group_id": None,
        "process_start_ticks": None,
        "submission_artifact_id": fake_id,
    }

    wrong_nonce = record_external_route_identity_resolution(
        state,
        route_attempt_id=binding["attempt_id"],
        domain_receipt=identity,
        reconciliation_artifact_id=fake_id,
    )
    fake_artifact = record_external_route_identity_resolution(
        state,
        route_attempt_id=binding["attempt_id"],
        domain_receipt={
            **identity,
            "submission_nonce": binding["attempt_id"],
        },
        reconciliation_artifact_id=fake_id,
    )

    assert wrong_nonce["status"] == "error"
    assert wrong_nonce["reason"] == "route_external_nonce_mismatch"
    assert fake_artifact["status"] == "error"
    assert fake_artifact["reason"] == "route_external_identity_evidence_missing"
    assert not _events(state, "route_step_external_identity_resolved")
    assert build_route_snapshot(state)["route_state"] == "interrupted"


def test_verified_terminal_job_unlocks_next_declared_submit_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A multi-job operation advances route facts before final closure."""
    state = _state(tmp_path)
    route = _route()
    first_step = dict(route["steps"][0])
    route["steps"] = [
        {**first_step, "id": "prepare", "goal": "prepare the managed environment"},
        {
            **first_step,
            "id": "run",
            "goal": "run the dependent managed operation",
            "after": ["prepare"],
            "action": {"tool": "submit_job", "program": "solver-run"},
        },
    ]
    first_binding, first_reference = _submitted_route(state, route, local=True)
    first_reference, _ = _persist_reconciled_identity(
        state,
        first_binding["attempt_id"],
        first_reference,
        "first_local",
    )
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda _state, _ids, refs: _success_verification(refs[0]),
    )

    first = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            "local",
            first_reference["job_id"],
        )
    )
    assert first["status"] == "success", first
    assert first["external_job_refs"] == [
        {
            field: first_reference.get(field)
            for field in operation_completion._EXTERNAL_JOB_SCOPE_FIELDS
        }
    ]
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["prepare"]["state"] == "verified"
    assert snapshot["ready_step_ids"] == ["run"]

    second_decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "solver-run",
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        },
    )
    assert second_decision["decision"] == "matched_ready_step", second_decision
    assert second_decision["route_step_id"] == "run"
    second_decision.update(
        {
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
        }
    )
    second_binding = begin_route_step_attempt(
        state,
        second_decision,
        tool="submit_job",
        action={"payload_digest": "external-route-test-second"},
    )
    assert second_binding and not second_binding.get("binding_error")
    second_identity = {
        field: value
        for field, value in first_reference.items()
        if field != "submission_artifact_id"
    }
    second_identity.update(
        {
            "job_id": "hf-harness-route-local-second",
            "submission_nonce": second_binding["attempt_id"],
            "route_attempt_id": second_binding["attempt_id"],
        }
    )
    second_reference, _ = _persist_reconciled_identity(
        state,
        second_binding["attempt_id"],
        second_identity,
        "second_local",
    )
    outcome = finish_route_step_attempt(
        state,
        second_binding,
        result={
            "status": "success",
            **second_reference,
            "submission_artifact_id": second_reference["submission_artifact_id"],
        },
        external_submission=True,
    )
    assert outcome and outcome["outcome"] == "submitted"

    second = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            "local",
            second_reference["job_id"],
        )
    )
    assert second["status"] == "success", second
    assert build_route_snapshot(state)["route_state"] == "complete"
    assert len(_events(state, "route_step_external_execution_verified")) == 2
    assert first_binding["attempt_id"] != second_binding["attempt_id"]


def test_execution_verification_rejects_missing_or_unsuccessful_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = _state(tmp_path)
    binding, reference = _submitted_route(state, local=True)

    missing = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            "local",
            reference["job_id"],
        )
    )
    assert missing["status"] == "error"
    assert missing["error_code"] == "external_job_identity_missing"
    assert not _events(state, "route_step_external_execution_verified")

    reference, _ = _persist_reconciled_identity(
        state,
        binding["attempt_id"],
        reference,
        "unsuccessful_local",
    )
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: {
            "ok": True,
            "terminal": True,
            "successful": False,
            "external_job_refs": [reference],
            "health": [{"reference": reference, "terminal": True}],
        },
    )
    unsuccessful = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            "local",
            reference["job_id"],
        )
    )
    assert unsuccessful["status"] == "error"
    assert unsuccessful["error_code"] == "external_job_success_unverified"
    assert build_route_snapshot(state)["route_state"] == "in_progress"
    assert not _events(state, "route_step_external_execution_verified")


def test_execution_verification_rejects_success_without_route_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = _state(tmp_path)
    binding, reference = _submitted_route(state, local=True)
    reference, _ = _persist_reconciled_identity(
        state,
        binding["attempt_id"],
        reference,
        "missing_projection_receipt",
    )
    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: {
            "ok": True,
            "terminal": True,
            "successful": True,
            "external_job_refs": [reference],
            "health": [],
        },
    )

    result = asyncio.run(
        operation_completion._verify_external_job_execution(
            state,
            "local",
            reference["job_id"],
        )
    )
    assert result["status"] == "error"
    assert result["error_code"] == "external_route_projection_receipt_missing"
    assert build_route_snapshot(state)["route_state"] == "in_progress"
    assert not _events(state, "route_step_external_execution_verified")


# ── expected_outputs 纠正后的重新投影（node20 E2E 死锁回归）──────────────────

_CORRECTION_ARGS = dict(
    terminal=True,
    success_verified=True,
    success_evidence={
        "verified": True,
        "succeeded": True,
        "source": "scheduler_accounting",
        "returncode": 0,
    },
)


def _phantom_output_failure(tmp_path: Path):
    """搭出外部作业实际写出 stdout、但声明路径错误的 missing 投影。"""
    state, route, binding, reference, _workdir, _job_dir = _repoint_failure(
        tmp_path)
    return state, route, binding, reference


def test_expected_outputs_correction_unlocks_reverification(tmp_path: Path):
    """validated 纠正后重新 verify 必须转 success，不再返缓存失败。

    node20 E2E 死锁的直接回归：既有测试纠正后只查 build_route_snapshot（报
    complete），从不再调 record_external_route_execution_verification —— 而 agent
    收尾恰恰再调它，命中缓存失败卡死 30+ turn。本测试补上"纠正后再投影"这一步。
    """
    state, route, binding, reference = _phantom_output_failure(tmp_path)

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = [
        f"jobs/{binding['attempt_id']}_solver/solver.out"]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="官方入口只承诺成功终态，不承诺额外文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "旧 expected_outputs 是任务未要求的虚假附加条件",
            "evidence_refs": [],
        },
    ))
    assert amended["status"] == "success"

    # 核心：修复前这里返缓存的 route_expected_outputs_missing（死锁），修复后 success。
    recovered = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert recovered["status"] == "success", recovered
    assert recovered.get("supersedes_failure_class") == "expected_outputs_missing"

    # 幂等：再调一次仍 success，不因两条 verified 事件被判 conflict。
    again = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert again["status"] == "success", again


def test_expected_outputs_correction_unlocks_refinalization_without_resubmit(
    tmp_path: Path,
):
    """同一终态的验证与 finalization 都必须接受已验证的声明纠正。"""
    state, route, binding, reference = _phantom_output_failure(tmp_path)
    finalization_args = {
        "scheduler": reference["scheduler"],
        "job_id": reference["job_id"],
        "namespace": reference["namespace"],
        "launch_host": reference["launch_host"],
        "scheduler_cluster": reference["scheduler_cluster"],
        "resource_uid": reference["resource_uid"],
        "submission_nonce": reference["submission_nonce"],
        "process_group_id": reference["process_group_id"],
        "process_start_ticks": reference["process_start_ticks"],
        "container_runtime_id": reference["container_runtime_id"],
        "domain_outcome": "operation_completed",
        "evidence_artifact_id": "external_job_operation_closure__receipt",
    }
    first_finalization = record_external_route_finalization(
        state, **finalization_args)
    assert first_finalization["status"] == "error"
    assert first_finalization["reason"] == "route_expected_outputs_missing"

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = [
        f"jobs/{binding['attempt_id']}_solver/solver.out"]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="官方入口只承诺成功终态，不承诺额外文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "旧 expected_outputs 是任务未要求的虚假附加条件",
            "evidence_refs": [],
        },
    ))
    assert amended["status"] == "success"

    # 不显式 reverify：finalize 入口必须自行按已验证纠正重投影同一终态。
    recovered_finalization = record_external_route_finalization(
        state, **finalization_args)
    assert recovered_finalization["status"] == "success", recovered_finalization
    assert (
        recovered_finalization.get("supersedes_failure_class")
        == "expected_outputs_missing"
    )
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["run"]["state"] == "verified"

    recovered_verification = _events(
        state, "route_step_external_execution_verified")[-1]
    assert recovered_verification["route_outcome"] == "success"
    assert (
        recovered_verification["supersedes_failure_class"]
        == "expected_outputs_missing"
    )
    # 第三次只是读取同一 active finalization；不能再写事件或重新绑定/提交。
    repeated = record_external_route_finalization(state, **finalization_args)
    assert repeated["status"] == "success", repeated
    assert repeated.get("already_projected") is True
    assert len(_events(state, "route_step_bound")) == 1
    assert len(_events(state, "route_step_outcome")) == 1
    assert len(_events(state, "route_step_external_execution_verified")) == 2
    assert len(_events(state, "route_step_external_finalized")) == 2


def _rewrite_ledger_rows(state, artifact_id, mutate):
    """直接改一份记录的账本行，模拟不经工具写入的盘上状态。

    C1（2026-09-12）起记录的框架事实（metadata、哈希、冻结）记在账本行里，正文是
    原生文件；不再有可以整体改写的 JSON 信封。测试要造"旧 run 的形状"或"绕过工具
    改盘"，改的就是账本行本身。
    """
    for store in state._stores():
        if store.head(artifact_id) is None:
            continue
        lines = store.ledger_path.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            if line.strip():
                row = json.loads(line)
                if str(row.get("id") or "") == artifact_id:
                    mutate(row)
                line = json.dumps(row, ensure_ascii=False)
            rewritten.append(line)
        store.ledger_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        return
    raise AssertionError(f"ledger has no record {artifact_id}")


def test_a_transcript_recovery_event_alone_no_longer_unlocks_the_projection(
    tmp_path: Path,
):
    """账本里没有恢复收据、只剩 transcript 事件时，纠正不成立，锁保持（方案 C）。

    原先这条钉的是反面：旧 run 只有 transcript recovery event 也能解锁。那条回退
    原本为 metadata 收据出现之前的 run 保留，事件却不带见证；单槽收据丢了以后它
    会被当成依据——2026-09-13 审判借此复现出完整伪造成功。C1 记录层之下没有这批旧
    run，用户定删除回退。
    """
    state, route, binding, reference = _phantom_output_failure(tmp_path)
    finalization_args = {
        "scheduler": reference["scheduler"],
        "job_id": reference["job_id"],
        "namespace": reference["namespace"],
        "launch_host": reference["launch_host"],
        "scheduler_cluster": reference["scheduler_cluster"],
        "resource_uid": reference["resource_uid"],
        "submission_nonce": reference["submission_nonce"],
        "process_group_id": reference["process_group_id"],
        "process_start_ticks": reference["process_start_ticks"],
        "container_runtime_id": reference["container_runtime_id"],
        "domain_outcome": "operation_completed",
        "evidence_artifact_id": "external_job_operation_closure__receipt",
    }
    first_finalization = record_external_route_finalization(
        state, **finalization_args)
    assert first_finalization["reason"] == "route_expected_outputs_missing"

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = [
        f"jobs/{binding['attempt_id']}_solver/solver.out"]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="官方入口只承诺成功终态，不承诺额外文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "旧 expected_outputs 是任务未要求的虚假附加条件",
            "evidence_refs": [],
        },
    ))
    assert amended["status"] == "success"
    assert _events(state, "declared_route_recovery_basis")

    # 模拟新 metadata receipt 出现之前的旧 run：保留已验证 transcript event。
    # C1 起路线 metadata 记在账本行里，旧 run 的形状就是账本行里没有这把钥匙。
    from nodes.experiment.tools.execution_route import _canonical_route_artifact_id
    route_id = _canonical_route_artifact_id(state)
    removed: list[dict] = []

    def _drop_receipt(row):
        for key in ("metadata", "metadata_patch"):
            bag = row.get(key)
            if isinstance(bag, dict) and "validated_recovery_receipt" in bag:
                removed.append(bag.pop("validated_recovery_receipt"))

    _rewrite_ledger_rows(state, route_id, _drop_receipt)
    assert removed and removed[-1]["attempt_id"] == binding["attempt_id"]
    assert "validated_recovery_receipt" not in (
        state.read_artifact(route_id)["metadata"])

    assert _events(state, "declared_route_recovery_basis"), "审计回显仍在，只是不再作依据"
    locked_verification = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert locked_verification["status"] == "error", locked_verification
    assert locked_verification["reason"] == "route_expected_outputs_missing"
    locked_finalization = record_external_route_finalization(
        state, **finalization_args)
    assert locked_finalization["status"] == "error", locked_finalization
    assert locked_finalization["reason"] == "route_expected_outputs_missing"
    # 没有写出任何取代失败的新投影。
    assert len(_events(state, "route_step_external_execution_verified")) == 1
    assert len(_events(state, "route_step_external_finalized")) == 1


def test_expected_outputs_lock_holds_without_validated_recovery(tmp_path: Path):
    """反作弊：未走 validated recovery 就重新 verify，投影不得解锁。

    否则 agent 只要"声明 expected_outputs 再清空"就能关掉一个真失败。锁只由
    validated_recovery_receipt 解开，而该收据仅在 _declare_execution_route 的恢复
    验证通过后原子写入。
    """
    state, route, binding, reference = _phantom_output_failure(tmp_path)

    # 未做任何纠正就重新 verify —— 锁必须仍在。
    again = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert again["status"] == "error"
    assert again["reason"] == "route_expected_outputs_missing"
    assert again.get("already_projected") is True

    # 直接断言门函数：无 validated_recovery_receipt → None（不解锁）。
    from nodes.experiment.tools.execution_route import (
        _validated_expected_outputs_correction,
    )
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(
            encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert _validated_expected_outputs_correction(
        state, events, binding["attempt_id"]) is None

    finalization_args = {
        "scheduler": reference["scheduler"],
        "job_id": reference["job_id"],
        "namespace": reference["namespace"],
        "launch_host": reference["launch_host"],
        "scheduler_cluster": reference["scheduler_cluster"],
        "resource_uid": reference["resource_uid"],
        "submission_nonce": reference["submission_nonce"],
        "process_group_id": reference["process_group_id"],
        "process_start_ticks": reference["process_start_ticks"],
        "container_runtime_id": reference["container_runtime_id"],
        "domain_outcome": "operation_completed",
        "evidence_artifact_id": "external_job_operation_closure__receipt",
    }
    first_finalization = record_external_route_finalization(
        state, **finalization_args)
    repeated_finalization = record_external_route_finalization(
        state, **finalization_args)
    assert first_finalization["status"] == "error"
    assert first_finalization["reason"] == "route_expected_outputs_missing"
    assert repeated_finalization["status"] == "error"
    assert repeated_finalization["reason"] == "route_expected_outputs_missing"
    assert repeated_finalization.get("already_projected") is True
    assert len(_events(state, "route_step_external_finalized")) == 1
    assert build_route_snapshot(state)["route_state"] == "blocked"


def test_unvalidated_supersession_event_cannot_hide_expected_output_failure(
    tmp_path: Path,
):
    """仅写 supersedes 字段不是恢复凭据，snapshot 与 verifier 都必须拒绝。"""
    state, _route_record, _binding, reference = _phantom_output_failure(tmp_path)
    failure = _events(state, "route_step_external_execution_verified")[0]
    forged = {
        key: value for key, value in failure.items()
        if key not in {
            "event", "at", "failure_class", "missing_expected_outputs",
        }
    }
    forged.update({
        "verification_status": "success",
        "route_outcome": "success",
        "supersedes_failure_class": "expected_outputs_missing",
    })
    state.append_transcript(
        "route_step_external_execution_verified", **forged)

    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "invalid_event_history"
    assert (
        snapshot["steps"]["run"]["reason"]
        == "multiple_external_execution_verifications"
    )
    retried = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert retried["status"] == "error"
    assert retried["reason"] == "route_external_execution_verification_conflict"


def test_supersession_requires_the_same_terminal_verification_digest(
    tmp_path: Path,
):
    """有合法 route correction 也不能拿另一份终态证据取代旧失败。"""
    state, route, binding, reference = _phantom_output_failure(tmp_path)
    failure = _events(state, "route_step_external_execution_verified")[0]
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = [
        f"jobs/{binding['attempt_id']}_solver/solver.out"]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="官方入口只承诺成功终态，不承诺额外文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "旧 expected_outputs 是任务未要求的虚假附加条件",
            "evidence_refs": [],
        },
    ))
    assert amended["status"] == "success"
    forged = {
        key: value for key, value in failure.items()
        if key not in {
            "event", "at", "failure_class", "missing_expected_outputs",
        }
    }
    forged.update({
        "verification_status": "success",
        "route_outcome": "success",
        "verification_digest": "0" * 64,
        "supersedes_failure_class": "expected_outputs_missing",
    })
    state.append_transcript(
        "route_step_external_execution_verified", **forged)

    retried = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert retried["status"] == "error"
    assert retried["reason"] == "route_external_execution_verification_conflict"


# ── 外部作业 expected_outputs 改指向（2026-09-10 验收 2 的 jobB 死路）────────────
#
# 作业确实写了产物，只是声明写错了位置。原先外部作业只接受"清空"这一种纠正，
# 于是模型把声明改指向真实落盘位置也收不了尾——违反不变量 1（不得靠清空有意义的
# expected outputs 完成纠正）。改指向放行，但每条新路径都要逐文件见证。


def _repoint_failure(
    tmp_path: Path, *, write_job_files: bool = True, bound: bool = False,
):
    """声明写成 logs/solver.out，作业实际写在自己的 jobs/<attempt>_solver/ 下。"""
    state = _bound_state(tmp_path) if bound else _state(tmp_path)
    route = _route()
    route["steps"][0]["expected_outputs"] = ["logs/solver.out"]
    binding, reference = _submitted_route(state, route)
    workdir = Path(binding["resolved_workdir"]) if binding.get(
        "resolved_workdir") else state.root / "outputs" / "experiment" / "runtime"
    job_dir = workdir / "jobs" / f"{binding['attempt_id']}_solver"
    job_dir.mkdir(parents=True)
    if write_job_files:
        (job_dir / "solver.out").write_text("converged\n", encoding="utf-8")
        (job_dir / "solver.err").write_text("", encoding="utf-8")
    # 受管后端在提交那一刻就把产物路径登记进 job_submission；改指向只认这一组。
    state.save_artifact("job_submission", "solver_submission", json.dumps({
        "status": "success", "scheduler": "slurm", "job_id": "31415",
        "submission_nonce": binding["attempt_id"],
        "route_attempt_id": binding["attempt_id"],
        "stdout_path": str(job_dir / "solver.out"),
        "stderr_path": str(job_dir / "solver.err"),
    }))
    failed = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert failed["reason"] == "route_expected_outputs_missing"
    return state, route, binding, reference, workdir, job_dir


def _repoint(state: State, route: dict, binding: dict, outputs: list[str],
             *, evidence_refs: list[str] | None = None,
             route_evidence: list[str] | None = None,
             failure_class: str = "expected_output") -> dict:
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = outputs
    if route_evidence is not None:
        revised["evidence_refs"] = route_evidence
    return asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="stdout 实际落在受管作业目录，改指向真实位置",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": failure_class,
            "diagnosis": "expected_outputs 路径写错，作业本身成功",
            "evidence_refs": evidence_refs or [],
        },
    ))


def test_external_expected_outputs_repoint_to_the_job_own_files_closes_the_step(
    tmp_path: Path,
):
    state, route, binding, reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    target = f"jobs/{binding['attempt_id']}_solver/solver.out"

    amended = _repoint(state, route, binding, [target])

    assert amended["status"] == "success", amended
    receipt = load_canonical_route(state)["record"]["metadata"][
        "validated_recovery_receipt"]
    assert receipt["external_execution_reused"] is True
    witness = receipt["external_output_repoint_witness"]
    assert witness["mode"] == "repoint"
    assert witness["verified_outputs"] == [target]
    assert [item["path"] for item in witness["files"]] == [target]
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["run"]["recovered_from_external_attempt"] is True
    # 对同一不可变终态重新机械核验：新声明真的在盘上，而不是只信见证。
    recovered = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert recovered["status"] == "success", recovered
    assert recovered["supersedes_failure_class"] == "expected_outputs_missing"
    assert len(_events(state, "route_step_bound")) == 1


def test_external_repoint_accepts_the_diagnosis_note_the_model_attaches(
    tmp_path: Path,
):
    """验收 2 的 v4 原样：诊断笔记同时写进 recovery_basis 与 route.evidence_refs。"""
    state, route, binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    note = state.save_artifact(
        "diagnosis_note", "diagnosis_note__solver_output_path",
        "stdout 由受管后端写进 jobs/<attempt>_<name>/，不是 logs/")["id"]
    ref = f"artifact:{note}"

    amended = _repoint(
        state, route, binding,
        [f"jobs/{binding['attempt_id']}_solver/solver.out"],
        evidence_refs=[ref],
        route_evidence=[*route["evidence_refs"], ref],
    )

    assert amended["status"] == "success", amended
    assert build_route_snapshot(state)["route_state"] == "complete"


def test_external_repoint_rejects_route_evidence_the_basis_does_not_cite(
    tmp_path: Path,
):
    state, route, binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)

    amended = _repoint(
        state, route, binding,
        [f"jobs/{binding['attempt_id']}_solver/solver.out"],
        route_evidence=[*route["evidence_refs"], "https://example.invalid/other"],
    )

    assert amended["status"] == "error"
    assert build_route_snapshot(state)["steps"]["run"]["state"] == "failed"


@pytest.mark.parametrize("attack", [
    "unregistered_name",     # 指向后端登记过、但不是这条声明对应的那个文件
    "outside_job_dir",       # 事后在工作区别处造一个同名文件
    "decoy_job_dir",         # 自己 mkdir 一个同前缀目录再造文件（审查实测的伪造路径）
    "inside_real_job_dir",   # 直接往真作业目录里写一个未登记的同名文件
    "symlink",
    "hardlink",
    "written_after_terminal",
    "older_than_binding",
    "wildcard",
    "one_file_for_two_outputs",
])
def test_external_repoint_refuses_files_the_job_did_not_write(
    tmp_path: Path, attack: str,
):
    import os
    import time

    state, route, binding, _reference, workdir, job_dir = _repoint_failure(tmp_path)
    attempt = binding["attempt_id"]
    real = job_dir / "solver.out"
    if attack == "unregistered_name":
        # solver.err 确实是后端登记过的，但它不是 logs/solver.out 那条声明的对应物
        target = f"jobs/{attempt}_solver/solver.err"
    elif attack == "outside_job_dir":
        (workdir / "copied").mkdir()
        (workdir / "copied" / "solver.out").write_text("x", encoding="utf-8")
        target = "copied/solver.out"
    elif attack == "decoy_job_dir":
        decoy = workdir / "jobs" / f"{attempt}_decoy"
        decoy.mkdir(parents=True)
        (decoy / "solver.out").write_text("FORGED", encoding="utf-8")
        target = f"jobs/{attempt}_decoy/solver.out"
    elif attack == "inside_real_job_dir":
        (job_dir / "extra.out").write_text("FORGED", encoding="utf-8")
        (job_dir / "solver2.out").write_text("FORGED", encoding="utf-8")
        target = f"jobs/{attempt}_solver/solver2.out"
    elif attack == "symlink":
        # 登记路径本身被换成符号链接：其余各条全过，只剩"不经符号链接"这一条拦。
        (job_dir / "sub").mkdir()
        real.rename(job_dir / "sub" / "solver.out")
        real.symlink_to(job_dir / "sub" / "solver.out")
        target = f"jobs/{attempt}_solver/solver.out"
    elif attack == "hardlink":
        os.link(real, workdir / "twin.out")
        target = f"jobs/{attempt}_solver/solver.out"
    elif attack == "written_after_terminal":
        future = time.time() + 60
        os.utime(real, (future, future))
        target = f"jobs/{attempt}_solver/solver.out"
    elif attack == "older_than_binding":
        past = time.time() - 3600
        os.utime(real, (past, past))
        target = f"jobs/{attempt}_solver/solver.out"
    elif attack == "one_file_for_two_outputs":
        target = f"jobs/{attempt}_solver/solver.out"
    else:
        target = f"jobs/{attempt}_solver/*.out"

    if attack == "one_file_for_two_outputs":
        # 两条声明产物同名，被改指向同一个文件——一份顶两份
        route = json.loads(json.dumps(route))
        route["steps"][0]["expected_outputs"] = ["logs/solver.out", "sub/solver.out"]
        amended = _repoint(state, route, binding, [target, target])
    else:
        amended = _repoint(state, route, binding, [target])

    assert amended["status"] == "error", (attack, amended)
    violations = amended.get("violations") or [amended.get("error", "")]
    assert "改指向作业真实写下的文件" in str(violations[0]), violations
    assert build_route_snapshot(state)["steps"]["run"]["state"] == "failed"
    assert not (load_canonical_route(state)["record"].get("metadata") or {}).get(
        "validated_recovery_receipt", {}).get("external_output_repoint_witness")


def test_repoint_witness_catches_a_file_rewritten_after_terminal_even_with_old_mtime(
    tmp_path: Path,
):
    """用户态能把 mtime 改回去，改不了 ctime：事后覆盖再 touch 回旧时间也要被抓。"""
    import os
    import time
    from datetime import datetime, timezone

    from nodes.experiment.tools.execution_route import (
        _external_output_repoint_witness,
    )

    now = time.time()
    job_dir = tmp_path / "jobs" / "route-abc_solver"
    job_dir.mkdir(parents=True)
    target = job_dir / "solver.out"
    target.write_text("forged after the job ended\n", encoding="utf-8")
    inside_window = now - 15
    os.utime(target, (inside_window, inside_window))
    bound = {"resolved_workdir": str(tmp_path),
             "bound_at_ns": int((now - 20) * 1e9)}
    receipt = {
        "recorded_at": datetime.fromtimestamp(now - 10, timezone.utc).isoformat(),
        "missing_expected_outputs": ["logs/solver.out"],
    }

    witness, violations = _external_output_repoint_witness(
        bound, receipt, "route-abc", ["jobs/route-abc_solver/solver.out"],
        {str(target.resolve())})

    assert witness is None
    assert any("终态核验之后" in item for item in violations), violations


def test_editing_outputs_after_a_repoint_does_not_inherit_the_witness(
    tmp_path: Path,
):
    """见证只担保它核过的那组路径；之后把声明换成别处，不算已验证纠正。"""
    from nodes.experiment.tools.execution_route import (
        _content_hash,
        _validated_expected_outputs_correction,
    )

    state, route, binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    target = f"jobs/{binding['attempt_id']}_solver/solver.out"
    assert _repoint(state, route, binding, [target])["status"] == "success"
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert _validated_expected_outputs_correction(
        state, events, binding["attempt_id"]) == [target]

    # 绕过工具直接改盘上的路线。C1 起正文是原生文件、哈希记在账本行里：两处一起改，
    # 读者才会把它当成一份自洽的路线（否则后面的 None 可能只是哈希对不上）。
    from nodes.experiment.tools.execution_route import _canonical_route_artifact_id
    route_id = _canonical_route_artifact_id(state)
    record = state.read_artifact(route_id)
    assert record["content_hash"] == _content_hash(record["content"]), (
        "账本哈希与 _content_hash 口径一致，下面的改写才有意义")
    record_route = json.loads(record["content"])
    record_route["steps"][0]["expected_outputs"] = ["somewhere/else/solver.out"]
    new_content = json.dumps(record_route, ensure_ascii=False)
    state.find_artifact_path(route_id).write_text(new_content, encoding="utf-8")
    new_hash = _content_hash(new_content)
    version = record["version"]

    def _rehash(row):
        if row.get("version") == version and "sha256" in row:
            row["sha256"] = new_hash

    _rewrite_ledger_rows(state, route_id, _rehash)
    # 先确认这份改动确实生效（否则 None 可能只是路线读不出来）。
    assert load_canonical_route(state)["status"] == "ready"

    assert _validated_expected_outputs_correction(
        state, events, binding["attempt_id"]) is None


def test_clearing_outputs_the_job_actually_wrote_is_refused_with_the_repoint_exit(
    tmp_path: Path,
):
    """不变量 1：作业确实写了同名文件，就不许靠清空声明收尾；拒绝里给出改指向的出口。"""
    state, route, binding, reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    target = f"jobs/{binding['attempt_id']}_solver/solver.out"

    cleared = _repoint(state, route, binding, [])

    assert cleared["status"] == "error", cleared
    assert target in str(cleared.get("violations")), cleared
    assert "report_blocker" in str(cleared.get("violations")), cleared
    assert build_route_snapshot(state)["steps"]["run"]["state"] == "failed"

    followed = _repoint(state, route, binding, [target])
    assert followed["status"] == "success", followed
    assert record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)["status"] == "success"


def test_clearing_after_persisted_missing_is_refused_without_a_repoint(
    tmp_path: Path,
):
    """missing 已持久化后不能清空声明；无可改指向文件时走诚实 blocker。"""
    state, route, binding, _reference, _workdir, _job_dir = _repoint_failure(
        tmp_path, write_job_files=False)

    cleared = _repoint(state, route, binding, [])

    assert cleared["status"] == "error", cleared
    assert cleared["error_code"] == "route_recovery_basis_required"
    refusal = str(cleared.get("violations") or cleared.get("error") or "")
    assert "expected_outputs" in refusal
    assert "report_blocker" in refusal
    assert "operation_blocked" in refusal
    assert "保留 step id" in refusal
    assert build_route_snapshot(state)["route_state"] == "blocked"


def test_refusal_guides_a_literal_rerun_to_a_real_failure_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """拒绝给出的重跑出口逐字可走通，且不能让旧 attempt 假闭环。"""
    state, route, binding, reference, _workdir, _job_dir = _repoint_failure(
        tmp_path, write_job_files=False, bound=True)

    cleared = _repoint(state, route, binding, [])

    assert cleared["status"] == "error", cleared
    first_refusal = str(cleared.get("violations") or cleared.get("error") or "")
    assert "failure_class" in first_refusal
    assert "不得继续使用 expected_output" in first_refusal
    recovery_context = cleared["recovery_context"]
    assert "只适用于" in recovery_context["recovery_basis_template_scope"]
    assert any(
        "放弃旧 attempt" in step
        and "failure_class" in step
        and "不得继续使用 expected_output" in step
        for step in recovery_context["required_sequence"]
    )

    evidence_id = state.save_artifact(
        "diagnosis_note", "diagnosis_note__rerun_real_cause",
        "新诊断表明旧构建参数不能产生要求的输出")["id"]
    evidence_ref = f"artifact:{evidence_id}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["bin/solver-v2"]
    revised["evidence_refs"].append(evidence_ref)
    common = {
        "attempt_id": binding["attempt_id"],
        "diagnosis": "旧构建参数不能产生要求的输出",
        "evidence_refs": [evidence_ref],
    }

    wrong_class = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="以新输出契约重跑",
        recovery_basis={**common, "failure_class": "expected_output"},
    ))

    assert wrong_class["status"] == "error", wrong_class
    second_refusal = str(
        wrong_class.get("violations") or wrong_class.get("error") or "")
    assert "放弃旧 attempt" in second_refusal
    assert "failure_class" in second_refusal
    assert "不得继续使用 expected_output" in second_refusal

    recovered = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="以新输出契约重跑",
        recovery_basis={**common, "failure_class": "parameter"},
    ))

    assert recovered["status"] == "success", recovered
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["run"]["state"] == "pending"
    assert "run" in snapshot["ready_step_ids"]

    old_finalization = record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference["namespace"],
        launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        container_runtime_id=reference["container_runtime_id"],
        domain_outcome="operation_completed",
        evidence_artifact_id="external_job_operation_closure__old_attempt",
    )
    assert old_finalization["status"] != "success", old_finalization

    monkeypatch.setattr(
        operation_completion,
        "_managed_external_job_verification",
        lambda *_args, **_kwargs: _success_verification(reference),
    )
    old_completion = asyncio.run(
        operation_completion._record_operation_completion(
            state,
            task_kind="external_job",
            objective="run the solver",
            outcome="success",
            external_job_refs=[reference],
        )
    )
    assert old_completion["status"] != "success", old_completion


def test_general_recovery_can_clear_missing_outputs_only_for_a_new_attempt(
    tmp_path: Path,
):
    """General recovery may replace the contract, but cannot reuse the old success."""
    state, route, binding, _reference, _workdir, _job_dir = _repoint_failure(
        tmp_path, write_job_files=False)
    evidence_id = state.save_artifact(
        "diagnosis_note", "diagnosis_note__declared_parameter",
        "参数诊断表明需要修改程序并重新执行")["id"]
    remediation_id = state.save_artifact(
        "remediation_note", "remediation_note__fixed_parameter",
        "下一次执行改用 solver-fixed")["id"]
    evidence_ref = f"artifact:{evidence_id}"
    remediation_ref = f"artifact:{remediation_id}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = []
    revised["steps"][0]["action"]["program"] = "solver-fixed"
    revised["evidence_refs"].extend([evidence_ref, remediation_ref])

    recovered = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="诊断后修正参数并创建新的执行计划",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "parameter",
            "diagnosis": "原程序参数错误，不能复用旧 attempt",
            "evidence_refs": [evidence_ref],
            "remediation_refs": [remediation_ref],
        },
    ))

    assert recovered["status"] == "success", recovered
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] != "complete"
    assert snapshot["steps"]["run"]["state"] == "pending"
    assert "run" in snapshot["ready_step_ids"]
    assert len(_events(
        state, "route_step_external_execution_verified")) == 1


@pytest.mark.parametrize("variant", ["drop", "replace"])
def test_general_recovery_may_drop_or_replace_a_failed_step_without_reuse(
    tmp_path: Path, variant: str,
):
    """Changing route topology is a new plan, never a correction of old success."""
    state = _state(tmp_path)
    route = _route()
    route["steps"][0]["expected_outputs"] = ["phantom-output.dat"]
    route["steps"].append({
        **route["steps"][0],
        "id": "keep",
        "goal": "run the remaining managed step",
        "after": ["run"],
        "action": {"tool": "submit_job", "program": "postprocess"},
        "expected_outputs": [],
    })
    binding, reference = _submitted_route(state, route)
    failed = record_external_route_execution_verification(
        state, external_job_ref=reference, **_CORRECTION_ARGS)
    assert failed["status"] == "error"
    assert failed["reason"] == "route_expected_outputs_missing"

    evidence_id = state.save_artifact(
        "diagnosis_note", f"diagnosis_note__{variant}_failed_step",
        "诊断确认旧步骤应从下一版执行计划移除")["id"]
    remediation_id = state.save_artifact(
        "remediation_note", f"remediation_note__{variant}_failed_step",
        "下一版路线不再复用旧 attempt")["id"]
    evidence_ref = f"artifact:{evidence_id}"
    remediation_ref = f"artifact:{remediation_id}"
    revised = json.loads(json.dumps(route))
    if variant == "drop":
        revised["steps"] = [
            step for step in revised["steps"] if step["id"] != "run"
        ]
        revised["steps"][0]["after"] = []
        expected_ready = "keep"
    else:
        revised["steps"][0]["id"] = "run-v2"
        revised["steps"][0]["goal"] = "rerun with a corrected execution contract"
        revised["steps"][0]["action"]["program"] = "solver-fixed"
        revised["steps"][0]["expected_outputs"] = ["result-v2.dat"]
        revised["steps"][1]["after"] = ["run-v2"]
        expected_ready = "run-v2"
    revised["evidence_refs"].extend([evidence_ref, remediation_ref])

    recovered = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason=f"{variant} the failed step in a new execution plan",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "parameter",
            "diagnosis": "旧步骤定义错误，不能复用其终态",
            "evidence_refs": [evidence_ref],
            "remediation_refs": [remediation_ref],
        },
    ))

    assert recovered["status"] == "success", recovered
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] != "complete"
    assert expected_ready in snapshot["ready_step_ids"]
    assert all(
        info.get("attempt_id") != binding["attempt_id"]
        or info["state"] != "verified"
        for info in snapshot["steps"].values()
    )
    finalized = record_external_route_finalization(
        state,
        scheduler=reference["scheduler"],
        job_id=reference["job_id"],
        namespace=reference["namespace"],
        launch_host=reference["launch_host"],
        scheduler_cluster=reference["scheduler_cluster"],
        resource_uid=reference["resource_uid"],
        submission_nonce=reference["submission_nonce"],
        process_group_id=reference["process_group_id"],
        process_start_ticks=reference["process_start_ticks"],
        container_runtime_id=reference["container_runtime_id"],
        domain_outcome="operation_completed",
        evidence_artifact_id="external_job_operation_closure__old_attempt",
    )
    assert finalized["status"] == "error"


def test_recovery_for_one_missing_external_step_cannot_clear_another(
    tmp_path: Path,
):
    """A recovery basis for B cannot erase A's persisted external missing fact."""
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [
        {
            **route["steps"][0],
            "id": "first",
            "goal": "first managed external job",
            "action": {"tool": "submit_job", "program": "solver-a"},
            "expected_outputs": ["logs/first.out"],
        },
        {
            **route["steps"][0],
            "id": "second",
            "goal": "second managed external job",
            "action": {"tool": "submit_job", "program": "solver-b"},
            "expected_outputs": ["logs/second.out"],
        },
    ]
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"

    def _submit_missing(step_id: str, program: str, job_id: str) -> tuple[dict, dict]:
        decision = resolve_execution_context(state, {
            "tool": "submit_job", "program": program, "route_step_id": step_id,
            "read_only": False,
            "observed_effects": ["external_job", "process_tree", "workspace_write"],
            "workdir_roles": ["run_root"],
        })
        assert decision["decision"] == "matched_ready_step", decision
        decision.update({
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
        })
        binding = begin_route_step_attempt(state, decision, tool="submit_job")
        assert binding and not binding.get("binding_error")
        reference = {
            "scheduler": "slurm", "job_id": job_id, "namespace": "research",
            "launch_host": "login-01", "scheduler_cluster": "cluster-a",
            "resource_uid": "slurm-cluster-a-" + job_id,
            "submission_nonce": binding["attempt_id"], "process_group_id": None,
            "process_start_ticks": None, "container_runtime_id": None,
            "route_attempt_id": binding["attempt_id"],
        }
        outcome = finish_route_step_attempt(
            state, binding, result={
                "status": "success",
                **reference,
                "submission_artifact_id": "job_submission__" + job_id,
            },
            external_submission=True)
        assert outcome and outcome["outcome"] == "submitted"
        failed = record_external_route_execution_verification(
            state, external_job_ref=reference, **_CORRECTION_ARGS)
        assert failed["reason"] == "route_expected_outputs_missing"
        return binding, reference

    first_binding, _first_reference = _submit_missing("first", "solver-a", "1001")
    second_binding, _second_reference = _submit_missing("second", "solver-b", "1002")
    evidence_id = state.save_artifact(
        "diagnosis_note", "diagnosis_note__second_parameter",
        "second job needs a parameter correction")["id"]
    evidence_ref = "artifact:" + evidence_id
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = []
    revised["steps"][1]["goal"] = "second managed job with corrected parameter"
    revised["evidence_refs"].append(evidence_ref)
    amended = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="correct second parameter",
        recovery_basis={
            "attempt_id": second_binding["attempt_id"],
            "failure_class": "parameter",
            "diagnosis": "second job parameter was wrong",
            "evidence_refs": [evidence_ref],
        },
    ))

    assert amended["status"] == "error", amended
    assert amended["error_code"] == "route_recovery_basis_required"
    assert "外部作业步骤 first" in str(amended["violations"])
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "blocked"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["steps"]["first"]["state"] == "failed"
    assert snapshot["steps"]["first"]["reason"] == "expected_outputs_missing"
    first_decision = resolve_execution_context(state, {
        "tool": "submit_job", "program": "solver-a", "route_step_id": "first",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["run_root"],
    })
    assert first_decision["decision"] != "matched_ready_step"
    assert first_decision["step_state"] == "failed"
    assert first_binding["attempt_id"] != second_binding["attempt_id"]


def _legacy_external_empty_clear_state(
    tmp_path: Path, *, pinned_success: bool, historically_finalized: bool = False,
) -> tuple[State, dict]:
    """Build a v2 legacy empty-clear route as an open persisted state."""
    from nodes.experiment.tools.execution_route import (
        _canonical_route_artifact_id, _content_hash, step_definition_hash,
        step_execution_contract_hash,
    )

    state, route, binding, reference, _workdir, _job_dir = _repoint_failure(
        tmp_path)
    target = f"jobs/{binding['attempt_id']}_solver/solver.out"
    assert _repoint(state, route, binding, [target])["status"] == "success"
    if pinned_success:
        assert record_external_route_execution_verification(
            state, external_job_ref=reference, **_CORRECTION_ARGS)["status"] == "success"

    route_id = _canonical_route_artifact_id(state)
    record = state.read_artifact(route_id)
    legacy_route = json.loads(record["content"])
    legacy_route["steps"][0]["expected_outputs"] = []
    legacy_content = json.dumps(legacy_route, ensure_ascii=False)
    legacy_hash = _content_hash(legacy_content)
    state.find_artifact_path(route_id).write_text(legacy_content, encoding="utf-8")
    version, legacy_step = record["version"], legacy_route["steps"][0]

    def _make_legacy(row):
        if row.get("version") != version:
            return
        if "sha256" in row:
            row["sha256"] = legacy_hash
        for key in ("metadata", "metadata_patch"):
            receipt = ((row.get(key) or {}).get("validated_recovery_receipt"))
            if isinstance(receipt, dict):
                receipt["next_route_content_hash"] = legacy_hash
                receipt["next_step_definition_hash"] = step_definition_hash(legacy_step)
                receipt["next_step_execution_contract_hash"] = step_execution_contract_hash(legacy_step)
                receipt["external_output_repoint_witness"] = None

    _rewrite_ledger_rows(state, route_id, _make_legacy)
    if pinned_success:
        # Model the old successful verification itself, rather than merely
        # leaving a valid repoint verification beside an empty current route.
        # That pins the migration regression to the legacy empty contract.
        events = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rewritten = False
        for event in events:
            if (
                event.get("event") == "route_step_external_execution_verified"
                and event.get("attempt_id") == binding["attempt_id"]
                and event.get("supersedes_failure_class") == "expected_outputs_missing"
            ):
                event["verified_output_specs"] = []
                event["verified_outputs"] = []
                event["supersession_basis_expected_outputs"] = []
                event["supersession_step_execution_contract_hash"] = (
                    step_execution_contract_hash(legacy_step)
                )
                rewritten = True
        assert rewritten
        state.transcript_path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
            + "\n",
            encoding="utf-8",
        )
    if historically_finalized:
        assert pinned_success
        state.append_transcript(
            "route_step_external_finalized",
            attempt_id=binding["attempt_id"],
            **reference,
            domain_outcome="operation_completed",
            route_outcome="success",
            evidence_artifact_id="external_job_operation_closure__legacy",
        )
    reopened = State.reopen(
        "experiment", state.root.parent, state.run_id, project_id=state.project_id)
    reopened.hook_state["_request_mode"] = "operation"
    reopened.hook_state["experiment_execution_scope"] = {
        "mode": "operational", "category": "other"}
    return reopened, reference




def test_a_same_named_file_that_fails_the_witness_does_not_allow_clearing(
    tmp_path: Path,
):
    """同名文件过不了见证时也不能删掉已经失败的后置条件。"""
    import os
    import time

    state, route, binding, _reference, _workdir, job_dir = _repoint_failure(tmp_path)
    past = time.time() - 3600
    os.utime(job_dir / "solver.out", (past, past))

    cleared = _repoint(state, route, binding, [])

    assert cleared["status"] == "error", cleared
    refusal = str(cleared.get("violations") or cleared.get("error") or "")
    assert "report_blocker" in refusal
    assert "operation_blocked" in refusal
    assert build_route_snapshot(state)["route_state"] == "blocked"


def test_one_registered_file_cannot_stand_in_for_two_declared_outputs(tmp_path):
    """两条同名声明产物改指向同一个文件——一份顶不了两份。"""
    import time
    from datetime import datetime, timezone

    from nodes.experiment.tools.execution_route import (
        _external_output_repoint_witness,
    )

    now = time.time()
    job_dir = tmp_path / "jobs" / "route-abc_solver"
    job_dir.mkdir(parents=True)
    stdout = job_dir / "solver.out"
    stdout.write_text("converged\n", encoding="utf-8")
    bound = {"resolved_workdir": str(tmp_path),
             "bound_at_ns": int((now - 20) * 1e9)}
    receipt = {
        "recorded_at": datetime.fromtimestamp(now + 5, timezone.utc).isoformat(),
        # 两条声明，落在不同目录、同一个文件名
        "missing_expected_outputs": ["a/solver.out", "b/solver.out"],
    }
    target = "jobs/route-abc_solver/solver.out"

    witness, violations = _external_output_repoint_witness(
        bound, receipt, "route-abc", [target, target],
        {str(stdout.resolve())})

    assert witness is None
    assert any("一份顶不了两份" in item for item in violations), violations


def _two_step_route(*, with_build: bool) -> dict:
    steps = [{
        "id": "run", "goal": "运行受管求解器", "after": [],
        "action": {"tool": "submit_job", "program": "solver"},
        "effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_role": "run_root", "expected_outputs": [],
    }]
    if with_build:
        steps.append({
            "id": "build", "goal": "编译", "after": [],
            "action": {"tool": "safe_run_bash", "program": "make"},
            "effects": ["process_tree", "workspace_write"],
            "workdir_role": "run_root", "expected_outputs": [],
        })
    return {"schema_version": 2, "goal": "两步路线",
            "evidence_refs": ["https://example.invalid/guide"], "steps": steps}


def test_a_route_frozen_before_declaration_only_rules_stays_readable_after_upgrade(
    tmp_path, monkeypatch,
):
    """升级前合法冻结的路线（含 safe_run_bash + make）换到新代码后仍可读：已提交作业的步骤
    不掉回待执行，重声明仍被「作业还在进行」拦住，v1 上的绑定不丢。原先读取路径也套用
    声明期新规则，整条路线判 route_schema_invalid，重声明随即成功、run 回到 ready，模型
    被引导再提交一次（2026-09-14 第三会话复审 P2，探针 review-0914-third/probe_retro_validation）。"""
    from nodes.experiment.tools import execution_route as er

    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf-home"))
    state = _state(tmp_path)
    classifier = er._program_needs_managed_lifecycle
    # 升级前：声明期还没有按 program 派生所有权的分类器。
    monkeypatch.setattr(er, "_program_needs_managed_lifecycle", lambda _program: False)
    declared = asyncio.run(er._declare_execution_route(
        state, route=_two_step_route(with_build=True)))
    assert declared["status"] == "success", declared

    runtime = state.root / "outputs" / "experiment" / "runtime"
    decision = er.resolve_execution_context(state, {
        "tool": "submit_job", "program": "solver", "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["run_root"]})
    assert decision["route_step_id"] == "run", decision
    decision.update({"workdir_role_observed": True, "workdir_resolution_status": "resolved",
                     "resolved_workdir": str(runtime)})
    binding = er.begin_route_step_attempt(
        state, decision, tool="submit_job", action={"payload_digest": "pd-run"})
    assert binding and not binding.get("binding_error"), binding
    outcome = er.finish_route_step_attempt(state, binding, result={
        "status": "success", "scheduler": "slurm", "job_id": "1001",
        "namespace": "research", "launch_host": "login-01",
        "scheduler_cluster": "cluster-a", "resource_uid": "slurm-cluster-a-1001",
        "submission_nonce": binding["attempt_id"], "process_group_id": None,
        "process_start_ticks": None, "container_runtime_id": None,
        "route_attempt_id": binding["attempt_id"],
        "submission_artifact_id": "job_submission__1001",
    }, external_submission=True)
    assert outcome and outcome["outcome"] == "submitted", outcome

    # 升级到新代码。
    monkeypatch.setattr(er, "_program_needs_managed_lifecycle", classifier)
    snapshot = er.build_route_snapshot(state)
    assert snapshot["route_state"] == "in_progress", snapshot
    assert snapshot["steps"]["run"]["state"] == "in_progress", snapshot
    assert "run" not in snapshot["ready_step_ids"], snapshot

    redeclared = asyncio.run(er._declare_execution_route(
        state, route=_two_step_route(with_build=False), amendment_reason="去掉 build 步骤"))
    assert redeclared.get("error_code") == "route_active_attempt_reconciliation_required", redeclared
    assert {binding[1] for binding in er._known_route_step_bindings(state)} == {1}


def _termination_only_evidence(**overrides: object) -> dict:
    """只靠"符合预期终止"解锁的证据：succeeded 为假，termination_matched 为真。"""
    return {
        "verified": True,
        "succeeded": False,
        "termination_matched": True,
        "source": "scheduler_accounting",
        "returncode": 0,
        **overrides,
    }


def test_termination_matched_alone_still_unlocks_a_route_step(tmp_path: Path):
    """正对照：没有 scheduler_terminated 时，这条侧门本来就该放行。

    没有这一格，下面那条测试证明不了它钉的是 scheduler_terminated 那一层——
    随便哪个别的条件不满足都会让它"通过"。
    """
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)

    projected = record_external_route_execution_verification(
        state,
        external_job_ref=reference,
        terminal=True,
        success_verified=True,
        success_evidence=_termination_only_evidence(),
    )

    assert projected["status"] == "success", projected


def test_scheduler_terminated_evidence_cannot_unlock_through_termination_matched(
    tmp_path: Path,
):
    """046：路线收货门自己也要认 scheduler_terminated，不只靠产生端收紧。

    三个产生端（finalize、cancel 守卫、record_external_route_finalization）今天都
    经 `_termination_verdict` 收口，所以这个组合在真实链路上已经产生不出来——但这一层
    是为"将来第四个产生端漏带"兜的底。它必须自己被钉住，否则谁重构这个 if 把它顺手
    删掉都不会有测试变红（审方 X3 变异：探针绿、34 条测试全绿）。
    """
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)

    projected = record_external_route_execution_verification(
        state,
        external_job_ref=reference,
        terminal=True,
        success_verified=True,
        success_evidence=_termination_only_evidence(scheduler_terminated=True),
    )

    assert projected["status"] == "error", projected
    assert projected["reason"] == "external_execution_not_successfully_verified", projected
    # 没有被记成已完成：路线快照里这一步不能变成 verified
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["run"]["state"] != "verified", snapshot
    assert _events(state, "route_step_external_verified") == []


def test_scheduler_terminated_does_not_block_a_genuinely_successful_job(
    tmp_path: Path,
):
    """收紧不得误伤：succeeded 为真时，这一层不参与判断。"""
    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)

    projected = record_external_route_execution_verification(
        state,
        external_job_ref=reference,
        terminal=True,
        success_verified=True,
        success_evidence={
            "verified": True,
            "succeeded": True,
            "source": "scheduler_accounting",
            "returncode": 0,
        },
    )

    assert projected["status"] == "success", projected
