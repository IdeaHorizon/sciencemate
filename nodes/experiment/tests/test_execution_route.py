from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from core.ledger import RecordStore
from core.state import State
from nodes.experiment.tools.execution_route import (
    _canonical_route_artifact_id,
    _declare_execution_route,
    _policy_for_effects,
    begin_route_step_attempt,
    build_route_snapshot,
    enforce_execution_route,
    execution_route_block,
    external_job_contract_violation,
    finish_route_step_attempt,
    load_canonical_route,
    normalize_declared_route,
    record_external_route_finalization,
    resolve_execution_action,
    resolve_execution_context,
    route_binding_block,
    route_default_workdir,
    route_outcome_block,
    shadow_execution_route,
    step_definition_hash,
    validate_route_v2,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _state(tmp_path: Path, *, run_id: str | None = None) -> State:
    if run_id is None:
        state = State.new(
            node_type="experiment",
            base_dir=tmp_path / "runs",
            project_id="route-project",
        )
    else:
        state = State.reopen(
            run_id=run_id,
            node_type="experiment",
            base_dir=tmp_path / "runs",
            project_id="route-project",
        )
    state.hook_state.setdefault("node_inputs", {
        "fixture": "execution_route",
        "requested_work": "验证受管执行路线的绑定、恢复与阻断语义",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This route fixture does not consume a preregistration.",
        },
    })
    if "experiment_execution_scope" not in state.hook_state:
        classified = asyncio.run(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category="toolchain_build",
            reason="路线测试的受管构建动作必须绑定稳定的上游测试输入。",
        ))
        assert classified["status"] == "success", classified
    return state


def _unclassified_state(tmp_path: Path) -> State:
    """Build a genuinely unclassified run with no durable scope event."""
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="route-project",
    )
    state.hook_state["node_inputs"] = {
        "fixture": "execution_route",
        "requested_work": "验证未分类路线的规划与执行门语义",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This route fixture does not consume a preregistration.",
        },
    }
    return state


def _route(*, goal: str = "构建并验证示例程序") -> dict:
    return {
        "schema_version": 2,
        "goal": goal,
        "evidence_refs": ["https://example.invalid/official-build-guide"],
        "steps": [
            {
                "id": "acquire",
                "goal": "取得源码",
                "after": [],
                "action": {"tool": "safe_run_bash", "program": "curl"},
                "effects": ["network_access", "workspace_write"],
                "workdir_role": "managed_source_root",
                "expected_outputs": ["src/source.tar.gz"],
            },
            {
                "id": "build",
                "goal": "使用项目入口构建",
                "after": ["acquire"],
                "action": {"tool": "submit_job", "program": "./compile"},
                "effects": ["workspace_write", "process_tree", "external_job"],
                "workdir_role": "build_root",
                "expected_outputs": ["main.exe"],
            },
            {
                "id": "smoke",
                "goal": "最小运行验证",
                "after": ["build"],
                "action": {"tool": "submit_job", "program": "./main.exe"},
                "effects": ["workspace_write", "external_job"],
                "workdir_role": "run_root",
                "expected_outputs": ["smoke.log"],
            },
        ],
    }


def test_route_v2_normalizes_a_valid_dag_without_persisting_progress():
    report = validate_route_v2(_route())

    assert report["valid"] is True
    assert report["errors"] == []
    normalized = report["route"]
    assert normalized["schema_version"] == 2
    assert [step["id"] for step in normalized["steps"]] == [
        "acquire", "build", "smoke",
    ]
    assert normalized["steps"][0]["after"] == []
    assert not any(
        key in step
        for step in normalized["steps"]
        for key in ("status", "current", "attempts")
    )




def test_operational_route_rejects_scientific_effect_and_guides_process_tree(
    tmp_path,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0].update({
        "id": "smoke",
        "after": [],
        "action": {"tool": "submit_job", "program": "./main.exe"},
        "effects": ["scientific_execution", "external_job"],
        "workdir_role": "run_root",
        "expected_outputs": [],
    })

    rejected = asyncio.run(_declare_execution_route(state, route=route))

    assert rejected["error_code"] == "route_scope_effect_mismatch"
    assert rejected["step_ids"] == ["smoke"]
    assert rejected["suggested_effect"] == "process_tree"
    assert load_canonical_route(state)["status"] != "ready"

    route["steps"][0]["effects"] = ["process_tree", "external_job"]
    accepted = asyncio.run(_declare_execution_route(state, route=route))
    assert accepted["status"] == "success"

def test_route_v2_rejects_duplicate_unknown_and_cyclic_dependencies():
    duplicate = _route()
    duplicate["steps"][1]["id"] = "acquire"
    assert any("重复" in error for error in validate_route_v2(duplicate)["errors"])

    unknown = _route()
    unknown["steps"][1]["after"] = ["missing"]
    assert any("不存在" in error for error in validate_route_v2(unknown)["errors"])

    cyclic = _route()
    cyclic["steps"][0]["after"] = ["smoke"]
    assert any("环" in error for error in validate_route_v2(cyclic)["errors"])

    unsafe_output = _route()
    unsafe_output["steps"][0]["expected_outputs"] = ["../escaped.tar"]
    assert any(
        "相对 workdir_role" in error
        for error in validate_route_v2(unsafe_output)["errors"]
    )

    unsupported_tool = _route()
    unsupported_tool["steps"][0]["action"]["tool"] = "run_node"
    assert any(
        "没有实现步骤绑定" in error
        for error in validate_route_v2(unsupported_tool)["errors"]
    )

    fake_data_step = _route()
    fake_data_step["steps"][0]["effects"].append("data_handoff")
    assert any(
        "未实现语义" in error
        for error in validate_route_v2(fake_data_step)["errors"]
    )


def test_route_v2_rejects_command_text_and_unsupported_action_fields():
    command_text = _route()
    command_text["steps"][1]["action"]["program"] = "cmake --build"
    unsupported_args = _route()
    unsupported_args["steps"][1]["action"]["args"] = ["--build", "."]
    unsupported_top = _route()
    unsupported_top["description"] = "不属于路线契约"
    unsupported_step = _route()
    unsupported_step["steps"][0]["build_root"] = "/tmp/build"
    missing_after = _route()
    del missing_after["steps"][0]["after"]

    command_errors = validate_route_v2(command_text)["errors"]
    args_errors = validate_route_v2(unsupported_args)["errors"]

    assert any("只填单个可执行入口" in error for error in command_errors)
    assert any("不支持字段" in error and "args" in error for error in args_errors)
    assert any(
        "不支持字段" in error and "description" in error
        for error in validate_route_v2(unsupported_top)["errors"]
    )
    assert any(
        "不支持字段" in error and "build_root" in error
        for error in validate_route_v2(unsupported_step)["errors"]
    )
    assert any(
        ".after 必须显式提供" in error
        for error in validate_route_v2(missing_after)["errors"]
    )


def test_declare_tool_schema_exposes_complete_v2_step_contract():
    from core.tool_registry import _REGISTRY

    route_schema = _REGISTRY.tools[
        "declare_execution_route"
    ].parameters_schema["properties"]["route"]["oneOf"][0]
    step_schema = route_schema["properties"]["steps"]["items"]
    action_schema = step_schema["properties"]["action"]

    assert set(route_schema["required"]) == {
        "schema_version", "goal", "evidence_refs", "steps",
    }
    assert set(step_schema["required"]) == {
        "id", "goal", "after", "action", "effects", "workdir_role",
        "expected_outputs",
    }
    assert action_schema["additionalProperties"] is False
    assert action_schema["properties"]["program"]["pattern"] == r"^\S+$"

    recovery_schema = _REGISTRY.tools[
        "declare_execution_route"
    ].parameters_schema["properties"]["recovery_basis"]
    assert "evidence_refs" in recovery_schema["required"]
    assert recovery_schema["properties"]["evidence_refs"]["minItems"] == 0
    assert "expected_outputs_missing" in (
        recovery_schema["properties"]["evidence_refs"]["description"]
    )


def test_explicit_unknown_schema_never_falls_back_to_legacy():
    legacy_shaped = {
        "schema_version": 99,
        "route_type": "official_build_system",
        "activities": {"compile": True},
    }

    report = normalize_declared_route(legacy_shaped)

    assert report["valid"] is False
    assert report["source_format"] == "unsupported"
    assert any("schema_version" in error for error in report["errors"])


def test_legacy_build_contract_is_read_only_normalized_to_linear_steps():
    legacy = {
        "route_type": "official_build_system",
        "activities": {"uses_source": True, "compile": True, "run": True},
        "path_roles": {
            "source_baseline_root": "/tmp/source",
            "build_root": "/tmp/build",
            "run_root": "/tmp/run",
        },
        "env_domains": {
            "compiler": {"selected": "gcc", "probes": ["gcc --version"]},
            "build_discovery": {
                "selected": "cmake",
                "probes": ["cmake --version"],
            },
        },
        "expected_artifacts": [{"path": "/tmp/build/app"}],
        "cache_invalidation": {"clean_on_toolchain_change": True},
    }

    report = normalize_declared_route(legacy)

    assert report["valid"] is True
    assert report["source_format"] == "legacy"
    assert [step["id"] for step in report["route"]["steps"]] == [
        "legacy_build", "legacy_run",
    ]
    assert report["route"]["steps"][1]["after"] == ["legacy_build"]


def test_declare_route_uses_one_frozen_identity_and_amends_that_identity(tmp_path):
    state = _state(tmp_path)

    first = asyncio.run(_declare_execution_route(
        state,
        route=json.dumps(_route(), ensure_ascii=False),
    ))

    assert first["status"] == "success"
    assert first["artifact_id"] == _canonical_route_artifact_id(state)
    assert first["route_ref"]["version"] == 1
    assert first["execution_guidance"]["ready_step_ids"] == ["acquire"]
    acquire = first["execution_guidance"]["steps"][0]
    assert acquire["program"] == "curl"
    assert acquire["workdir_role"] == "managed_source_root"
    assert acquire["workdir_candidates"] == [str(
        state.root / "outputs" / "experiment" / "runtime" / "source"
    )]
    assert first["execution_guidance"]["single_entrypoint_per_call"] is True
    record_v1 = state.read_artifact(_canonical_route_artifact_id(state))
    assert record_v1 is not None
    assert record_v1["metadata"]["frozen"] is True
    assert record_v1["content_hash"] == first["route_ref"]["content_hash"]

    missing_reason = asyncio.run(_declare_execution_route(
        state,
        route=_route(goal="修订目标"),
    ))
    assert missing_reason["status"] == "error"
    assert missing_reason["error_code"] == "route_amendment_reason_required"

    second = asyncio.run(_declare_execution_route(
        state,
        route=_route(goal="修订目标"),
        amendment_reason="官方文档证据表明需要调整目标",
    ))

    assert second["status"] == "success"
    assert second["artifact_id"] == _canonical_route_artifact_id(state)
    assert second["route_ref"]["version"] == 2
    assert second["route_ref"]["content_hash"] != first["route_ref"]["content_hash"]
    record_v2 = state.read_artifact(_canonical_route_artifact_id(state))
    assert record_v2 is not None
    assert record_v2["metadata"]["frozen"] is True
    assert record_v2["amendment"]["reason"] == "官方文档证据表明需要调整目标"
    assert [item["version"] for item in state.artifact_versions(
        _canonical_route_artifact_id(state)
    )] == [1, 2]


def test_completed_operation_closure_seals_route_identity(tmp_path):
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )

    state = _state(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    assert declared["status"] == "success"

    completed = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="记录一次已经终止的受限操作",
        outcome="failed",
        checks=[{
            "name": "bounded_operation",
            "passed": False,
            "evidence": {"returncode": 2, "stderr": "configure failed"},
        }],
        next_step="根据新的客观证据开始一个新 run，不改写本 run 的结果身份",
    ))
    assert completed["status"] == "success"

    amended = asyncio.run(_declare_execution_route(
        state,
        route=_route(goal="闭环后试图改写路线"),
        amendment_reason="闭环后出现了另一个目标",
    ))

    assert amended["status"] == "error"
    assert amended["error_code"] == "execution_route_sealed_by_operation_closure"
    assert amended["closure"]["kind"] == "complete"
    assert amended["blocker"]["node_action"] == "start_new_run_for_new_route"
    assert len(state.artifact_versions(_canonical_route_artifact_id(state))) == 1

    idempotent = asyncio.run(_declare_execution_route(state, route=_route()))
    assert idempotent["status"] == "success"
    assert idempotent["already_declared"] is True


def test_operation_completion_without_route_blocks_first_route_declaration(
    tmp_path,
):
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )

    state = _unclassified_state(tmp_path)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="This fixture records a bounded generic operation closure.",
    ))
    assert classified["status"] == "success", classified
    evidence = state.root / "bounded-operation.log"
    evidence.write_text("returncode=0" + chr(10), encoding="utf-8")
    completed = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="完成一个没有预先声明路线的受限操作",
        outcome="success",
        checks=[{
            "name": "bounded_operation",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))
    assert completed["status"] == "success"

    declared = asyncio.run(_declare_execution_route(state, route=_route()))

    assert declared["status"] == "error"
    assert declared["error_code"] == "execution_route_sealed_by_operation_closure"
    assert load_canonical_route(state)["status"] != "ready"


def test_scientific_result_triplet_does_not_impersonate_operation_closure(
    tmp_path,
):
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="route-project",
    )
    state.hook_state["node_inputs"] = {
        "fixture": "execution_route",
        "requested_work": "验证科学结果三件套不会冒充 operation closure",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This exploratory fixture has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="This fixture creates scientific evidence under an immutable entrance receipt.",
    ))
    assert classified["status"] == "success", classified
    for artifact_type in ("raw_results", "clean_results", "experiment_log"):
        saved = state.save_artifact(
            artifact_type,
            f"scientific_{artifact_type}",
            "scientific evidence",
            metadata={"record_kind": "scientific"},
        )
        state.mark_frozen(saved["id"])   # 冻结只出自账本的 freeze 行

    declared = asyncio.run(_declare_execution_route(state, route=_route()))

    assert declared["status"] == "success"
    assert declared["route_ref"]["version"] == 1


@pytest.mark.parametrize("name", ["execution_route", "execution_route! "])
def test_generic_save_cannot_forge_canonical_route_identity(tmp_path, name):
    from shared.tools.builtin import _save_artifact

    state = _state(tmp_path)
    forged = asyncio.run(_save_artifact(
        state,
        artifact_type="declared_route",
        name=name,
        content=json.dumps(_route()),
        metadata={"validated_recovery_receipt": {"recovery_validated": True}},
    ))

    assert forged["status"] == "error"
    assert forged["failed_checks"] == ["canonical_route_owner"]
    assert "declare_execution_route" in forged["hint"]
    assert load_canonical_route(state)["status"] != "ready"

    legacy = asyncio.run(_save_artifact(
        state,
        artifact_type="declared_route",
        name="legacy_build_contract",
        content="route_type: official_build_system",
    ))
    assert legacy["status"] == "success"


def test_loader_accepts_only_current_run_owned_frozen_canonical_route(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    assert declared["status"] == "success"
    assert load_canonical_route(state)["status"] == "ready"

    # 同目录里另一身份的 route 不能与 canonical route 竞争。
    state.save_artifact("declared_route", "parallel_identity", "{}")
    assert load_canonical_route(state)["status"] == "ready"

    old_run_id = state.run_id
    reopened = _state(tmp_path, run_id=old_run_id)
    assert load_canonical_route(reopened)["status"] == "ready"

    other_run = _state(tmp_path)
    # 另一个 run 自己的路线身份下躺着一份冻结拷贝，产出方仍是原 run（路线身份按 run
    # 区分之后，冒充只能冒到对方名下；归属检查照样得拦住）。
    copied = state.read_artifact(_canonical_route_artifact_id(state))
    other_id = _canonical_route_artifact_id(other_run)
    other_store = RecordStore(other_run.root / "artifacts", other_run.root / "records.jsonl")
    other_store.save(
        artifact_id=other_id, artifact_type=copied["type"],
        name=other_id.split("__", 1)[1], content=copied["content"],
        metadata=dict(copied["metadata"]),
        directory=other_run.root / "artifacts", created_at=copied["created_at"],
        provenance=dict(copied["provenance"]),
        produced_by_node_type=copied["produced_by_node_type"],
        produced_by_run_id=copied["produced_by_run_id"],
        by_node="experiment", by_run=other_run.run_id,
    )
    other_store.freeze(other_id, metadata_patch={},
                       by_node="experiment", by_run=other_run.run_id)
    stale = load_canonical_route(other_run)
    assert stale["status"] == "unavailable"
    assert stale["reason"] == "route_not_owned_by_current_run"


def test_loader_rejects_unfrozen_and_malformed_v2_heads(tmp_path):
    state = _state(tmp_path)
    state.save_artifact(
        "declared_route",
        "execution_route",
        json.dumps(_route(), ensure_ascii=False),
    )
    assert load_canonical_route(state)["reason"] == "route_not_frozen"

    bad_state = _state(tmp_path)
    bad = _route()
    bad["steps"][1]["after"] = ["missing"]
    saved = bad_state.save_artifact(
        "declared_route",
        "execution_route",
        json.dumps(bad, ensure_ascii=False),
    )
    # 冻结是账本上的一行，不是文件里的一个标志。
    bad_state.mark_frozen(saved["id"])

    loaded = load_canonical_route(bad_state)
    assert loaded["status"] == "invalid"
    assert loaded["reason"] == "route_schema_invalid"


def _append_bound(
    state: State,
    route_ref: dict,
    step: dict,
    attempt_id: str,
    *,
    applied_policy: str = "test",
) -> None:
    state.append_transcript(
        "route_step_bound",
        route_artifact_id=route_ref["artifact_id"],
        route_version=route_ref["version"],
        route_content_hash=route_ref["content_hash"],
        route_step_id=step["id"],
        step_definition_hash=step_definition_hash(step),
        attempt_id=attempt_id,
        tool=step["action"]["tool"],
        resolved_workdir_role=step["workdir_role"],
        applied_policy=applied_policy,
    )


def test_snapshot_derives_ready_and_verified_from_events_not_route_fields(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    loaded = load_canonical_route(state)
    acquire, build, smoke = loaded["route"]["steps"]

    initial = build_route_snapshot(state)
    assert initial["route_state"] == "actionable"
    assert initial["ready_step_ids"] == ["acquire"]

    _append_bound(state, declared["route_ref"], acquire, "attempt-acquire")
    state.append_transcript(
        "route_step_outcome",
        attempt_id="attempt-acquire",
        outcome="success",
        managed_tool_receipt={"status": "success", "returncode": 0},
        verified_output_specs=["src/source.tar.gz"],
        verified_outputs=["/managed/source/src/source.tar.gz"],
    )
    snapshot = build_route_snapshot(state)

    assert snapshot["steps"]["acquire"]["state"] == "verified"
    assert snapshot["steps"]["build"]["state"] == "pending"
    assert snapshot["steps"]["smoke"]["state"] == "pending"
    assert snapshot["ready_step_ids"] == ["build"]


def test_started_without_outcome_is_interrupted_and_cannot_be_retried(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    loaded = load_canonical_route(state)
    acquire = loaded["route"]["steps"][0]
    _append_bound(state, declared["route_ref"], acquire, "orphan-attempt")

    snapshot = build_route_snapshot(state)
    decision = resolve_execution_action(snapshot, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })

    assert snapshot["steps"]["acquire"]["state"] == "interrupted"
    assert snapshot["ready_step_ids"] == []
    assert decision["decision"] == "reconcile_interrupted_attempt"
    assert decision["attempt_id"] == "orphan-attempt"


def test_cancelled_attempt_is_not_misclassified_as_tool_failure(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    loaded = load_canonical_route(state)
    acquire = loaded["route"]["steps"][0]
    binding = {
        "attempt_id": "cancelled-attempt",
        "route_artifact_id": declared["route_ref"]["artifact_id"],
        "route_version": declared["route_ref"]["version"],
        "route_content_hash": declared["route_ref"]["content_hash"],
        "route_step_id": acquire["id"],
        "step_definition_hash": step_definition_hash(acquire),
        "tool": "safe_run_bash",
        "resolved_workdir_role": "managed_source_root",
        "applied_policy": "guarded_unknown_effect",
    }
    state.append_transcript("route_step_bound", **binding)

    finish_route_step_attempt(
        state, binding, error=asyncio.CancelledError())

    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["acquire"]["state"] == "cancelled"
    assert snapshot["steps"]["acquire"]["reason"] == "cancelled"
    assert snapshot["route_state"] == "blocked"


def test_zero_execution_infrastructure_rejection_returns_step_to_ready(tmp_path):
    """绑定后二道防线拒绝（零执行）必须可恢复，而不是永久 failed。"""
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [dict(route["steps"][1], after=[])]
    declared = asyncio.run(_declare_execution_route(state, route=route))
    loaded = load_canonical_route(state)
    build = loaded["route"]["steps"][0]
    binding = {
        "attempt_id": "rejected-attempt",
        "route_artifact_id": declared["route_ref"]["artifact_id"],
        "route_version": declared["route_ref"]["version"],
        "route_content_hash": declared["route_ref"]["content_hash"],
        "route_step_id": build["id"],
        "step_definition_hash": step_definition_hash(build),
        "tool": "submit_job",
        "resolved_workdir_role": "build_root",
        "applied_policy": "managed_external_job",
    }
    state.append_transcript("route_step_bound", **binding)

    event = finish_route_step_attempt(
        state, binding,
        result={
            "status": "error",
            "reason": "local_sandbox_path_contract_rejected",
            "error": ("local sandbox path contract rejected: "
                      "path_capability_required: build_root not materialized"),
        },
        external_submission=True,
    )

    assert event["outcome"] == "rejected"
    assert event["failure_class"] == "infrastructure_rejection"
    assert event["rejection_reason"] == "local_sandbox_path_contract_rejected"
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["build"]["state"] == "pending"
    assert snapshot["ready_step_ids"] == ["build"]
    assert snapshot["route_state"] == "actionable"


def test_rejection_reason_with_execution_evidence_stays_terminal(tmp_path):
    """带 returncode 的失败说明 payload 真跑过：即使 reason 可识别也保持终态。"""
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [dict(route["steps"][1], after=[])]
    declared = asyncio.run(_declare_execution_route(state, route=route))
    loaded = load_canonical_route(state)
    build = loaded["route"]["steps"][0]
    binding = {
        "attempt_id": "executed-attempt",
        "route_artifact_id": declared["route_ref"]["artifact_id"],
        "route_version": declared["route_ref"]["version"],
        "route_content_hash": declared["route_ref"]["content_hash"],
        "route_step_id": build["id"],
        "step_definition_hash": step_definition_hash(build),
        "tool": "submit_job",
        "resolved_workdir_role": "build_root",
        "applied_policy": "managed_external_job",
    }
    state.append_transcript("route_step_bound", **binding)

    event = finish_route_step_attempt(
        state, binding,
        result={
            "status": "error",
            "reason": "local_sandbox_path_contract_rejected",
            "error": "payload exited nonzero",
            "returncode": 2,
        },
        external_submission=True,
    )

    assert event["outcome"] == "failed"
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["build"]["state"] == "failed"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["route_state"] == "blocked"


def test_recovery_basis_reports_every_violation_at_once(tmp_path):
    """recovery_basis 的形状/证据类违规一次全给，不让调用方一条一条打地鼠。

    2026-09-08 活体里，CMake 那次为了让一份 recovery_basis 通过，连吃 9 种不同
    拒绝：每次只被告知一处不成立，修好再撞下一处。相一（attempt 身份）仍逐条硬停
    ——认不出 attempt 时后面的检查无从谈起；相二这些是可以同时成立的独立缺陷。
    """
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    workdir.mkdir(parents=True)
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(
        state, decision, tool="safe_run_bash",
        action={"payload_digest": "d" * 64})
    finish_route_step_attempt(
        state, binding, result={"status": "error", "returncode": 1})

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["src/other.tar.gz"]
    revised["steps"][0]["action"]["program"] = "wget"
    blocked = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="修正入口与预期产物",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "execution",
            "diagnosis": "下载器不对，且预期产物名写错",
            # 同时踩三处：引用不存在的 artifact、没写进 route.evidence_refs、
            # 用了自由文本而非 artifact: 前缀
            "evidence_refs": ["artifact:does-not-exist", "not-an-artifact-ref"],
        }))

    assert blocked["error_code"] == "route_recovery_basis_required"
    violations = blocked["violations"]
    assert len(violations) > 1, violations
    assert any("写入修订后路线" in v for v in violations), violations
    assert any("不存在" in v or "artifact:<artifact_id>" in v for v in violations), violations
    # 汇总文案要说清有几处，别让模型以为只有一处
    assert str(len(violations)) in blocked["error"]


def test_in_progress_lock_names_the_jobs_to_finalize(tmp_path, monkeypatch):
    """路线上有受管动作进行中时，拒绝要指名道姓该收尾哪个作业。

    这道锁此前**零测试覆盖**，而它给的出口是不可执行的：文案写「先对账或
    finalize」，但受管对账只接受 outcome 为空或 unknown+身份待解析，而 in_progress
    的 attempt outcome 恒为 submitted，于是对账恒返回 integration_pending。
    2026-09-08 与 09-09 两次活体都在这里空转。真出口只有一条：把进行中的作业收尾。
    """
    from nodes.experiment.tools import execution_route as er

    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"

    real_snapshot = er.build_route_snapshot

    def in_progress_snapshot(target_state, *a, **kw):
        snap = real_snapshot(target_state, *a, **kw)
        snap = json.loads(json.dumps(snap))
        snap["route_state"] = "in_progress"
        snap.setdefault("steps", {})["acquire"] = {
            **(snap.get("steps", {}).get("acquire") or {}),
            "state": "in_progress",
            "attempt_id": "attempt-in-progress",
            "scheduler": "slurm",
            "job_id": "9001",
        }
        return snap

    monkeypatch.setattr(er, "build_route_snapshot", in_progress_snapshot)

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["src/other.tar.gz"]
    blocked = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="试图在动作进行中修订"))

    assert blocked["error_code"] == "route_active_attempt_reconciliation_required"
    # 不再教那条走不通的「先对账」
    assert "对账" not in blocked["error"], blocked["error"]
    # 逐字给出该收尾哪个作业
    assert 'finalize_external_job(scheduler="slurm", job_id="9001"' in blocked["error"]
    assert blocked["in_progress_attempts"][0]["route_step_id"] == "acquire"
    assert blocked["in_progress_attempts"][0]["job_id"] == "9001"
    assert blocked["route_state"] == "in_progress"


def test_success_return_without_expected_output_uses_attempt_receipt_recovery(
    tmp_path,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    declared = asyncio.run(_declare_execution_route(state, route=route))
    assert declared["status"] == "success"
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    workdir.mkdir(parents=True)
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(
        state, decision, tool="safe_run_bash",
        action={"payload_digest": "b" * 64})

    time.sleep(0.01)
    actual = workdir / "actual" / "source.tar.gz"
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"downloaded source\n")

    outcome = finish_route_step_attempt(
        state, binding, result={"status": "success", "returncode": 0})

    assert outcome["outcome"] == "failed"
    assert outcome["failure_class"] == "expected_outputs_missing"
    assert outcome["managed_tool_receipt"] == {
        "status": "success",
        "returncode": 0,
    }
    block = route_outcome_block(outcome)
    assert block is not None
    assert block["reason"] == "route_expected_outputs_missing"
    assert block["attempt_id"] == binding["attempt_id"]
    assert block["route_step_id"] == "acquire"
    assert block["recovery_context"]["recovery_basis_template"] == {
        "attempt_id": binding["attempt_id"],
        "failure_class": "expected_output",
        "diagnosis": "<旧 expected_outputs 与入口真实输出语义不一致>",
        "evidence_refs": [],
    }
    assert set(block["recovery_context"][
        "prohibited_evidence_artifact_types"
    ]) >= {"raw_results", "clean_results", "experiment_log"}

    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["acquire"]["state"] == "failed"
    stopped = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    assert stopped["decision"] == "route_step_not_ready"
    assert stopped["attempt_id"] == binding["attempt_id"]
    assert stopped["step_reason"] == "expected_outputs_missing"
    stopped_block = execution_route_block(stopped)
    assert stopped_block["blocker"]["attempt_id"] == binding["attempt_id"]
    assert "expected_outputs" in stopped_block["error"]
    assert "recovery_basis.evidence_refs=[]" in stopped_block["error"]
    assert "不重新执行" in stopped_block["error"]
    for misleading in ("step_definition_hash", "route.evidence_refs"):
        assert misleading not in stopped_block["error"]
    assert "step_definition_hash" not in stopped_block
    assert "step_definition_hash" not in stopped_block["blocker"]
    assert stopped_block["route_state"]
    assert "ready_step_ids" in stopped_block

    action = {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    }
    first_refusal = enforce_execution_route(
        state, action, resolve_execution_context(state, action))
    second_refusal = enforce_execution_route(
        state, action, resolve_execution_context(state, action))
    exhausted = enforce_execution_route(
        state, action, resolve_execution_context(state, action))
    assert first_refusal["reason"] == "execution_route_step_not_ready"
    assert second_refusal["reason"] == "execution_route_step_not_ready"
    assert exhausted["reason"] == "route_attempts_exhausted"
    assert exhausted["underlying_reason"] == "execution_route_step_not_ready"
    assert "recovery_basis.evidence_refs=[]" in exhausted["error"]
    assert "不重新执行" in exhausted["error"]
    for misleading in (
        "以新证据修订", "step_definition_hash", "route.evidence_refs",
    ):
        assert misleading not in exhausted["error"]

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["actual/source.tar.gz"]
    missing_basis = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="按真实下载器输出修正预期源码包",
    ))
    assert missing_basis["error_code"] == "route_recovery_basis_required"
    context = missing_basis["recovery_context"]
    assert context["affected_attempts"][0]["attempt_id"] == binding["attempt_id"]
    assert context["evidence_requirements"]["mode"] == (
        "validated_local_attempt_receipt_no_reexecution")
    assert context["recovery_basis_template"]["evidence_refs"] == []
    reopen = context["reopen_condition"]
    assert "expected_outputs" in reopen["predicate"]
    assert "不重新执行" in reopen["predicate"]
    assert reopen["current_step_definition_hash"][binding["route_step_id"]]
    assert "expected_outputs" in reopen["changeable_fields"]
    assert "goal" in reopen["changeable_fields"]
    assert "action.workdir_role" not in reopen["changeable_fields"]
    assert "same_payload_also_needs" not in reopen

    before_events = state.transcript_path.read_text(encoding="utf-8")
    before_bound_count = before_events.count('"event": "route_step_bound"')
    before_outcome_count = before_events.count('"event": "route_step_outcome"')
    recovered = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="按真实下载器输出修正预期源码包",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "旧 expected_outputs 与下载入口的真实输出文件名不一致",
            "evidence_refs": [],
        },
    ))
    assert recovered["status"] == "success"
    recovered_snapshot = build_route_snapshot(state)
    recovered_step = recovered_snapshot["steps"]["acquire"]
    assert recovered_snapshot["route_state"] == "complete"
    assert recovered_snapshot["ready_step_ids"] == []
    assert recovered_step["state"] == "verified"
    assert recovered_step["attempt_id"] == binding["attempt_id"]
    assert recovered_step["recovered_from_local_attempt"] is True

    loaded = load_canonical_route(state)
    recovery_receipt = loaded["record"]["metadata"][
        "validated_recovery_receipt"]
    assert recovery_receipt["attempt_receipt_used_as_evidence"] is True
    assert recovery_receipt["whole_route_expected_outputs_only"] is True
    assert recovery_receipt["evidence_refs"] == []
    assert recovery_receipt["local_execution_reused"] is True
    assert recovery_receipt["attempt_receipt"]["source_event"] == (
        "route_step_outcome")
    witness = recovery_receipt["local_output_correction_witness"]
    assert witness["verified_outputs"] == ["actual/source.tar.gz"]
    assert witness["original_expected_outputs"] == ["src/source.tar.gz"]
    assert witness["witness_scope"] == "run_shared_workdir_time_window"
    assert witness["semantic_identity_independently_verified"] is False
    for artifact_type in (
        "raw_results", "clean_results", "experiment_log", "diagnostic_evidence",
    ):
        assert state.list_artifacts(artifact_type) == []

    # transcript is only an audit projection.  Removing it must not erase the
    # authoritative frozen receipt or reopen the successfully executed step.
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(
            encoding="utf-8").splitlines()
        if line.strip()
    ]
    state.transcript_path.write_text(
        "\n".join(
            json.dumps(event, ensure_ascii=False)
            for event in events
            if event.get("event") != "declared_route_recovery_basis"
        ) + "\n",
        encoding="utf-8",
    )

    reopened_snapshot = build_route_snapshot(state)
    assert reopened_snapshot["route_state"] == "complete"
    assert reopened_snapshot["steps"]["acquire"]["attempt_id"] == binding[
        "attempt_id"]

    after_events = state.transcript_path.read_text(encoding="utf-8")
    assert after_events.count('"event": "route_step_bound"') == before_bound_count
    assert after_events.count('"event": "route_step_outcome"') == before_outcome_count


def _record_single_step_failure(
    state: State,
    route: dict,
    *,
    result: dict,
) -> dict:
    declared = asyncio.run(_declare_execution_route(state, route=route))
    assert declared["status"] == "success"
    step = route["steps"][0]
    workdir = state.root / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    decision = resolve_execution_context(state, {
        "tool": step["action"]["tool"],
        "program": step["action"]["program"],
        "route_step_id": step["id"],
        "read_only": False,
        "observed_effects": step["effects"],
        "workdir_roles": [step["workdir_role"]],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(
        state,
        decision,
        tool=step["action"]["tool"],
        action={"payload_digest": "e" * 64},
    )
    finish_route_step_attempt(state, binding, result=result)
    return binding


@pytest.mark.parametrize(
    ("change_goal", "failure_class"),
    [(True, "expected_output"), (False, "execution")],
)
def test_empty_evidence_cannot_broaden_expected_output_recovery(
    tmp_path,
    change_goal,
    failure_class,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    binding = _record_single_step_failure(
        state,
        route,
        result={"status": "success", "returncode": 0},
    )
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["actual-source.tar.gz"]
    if change_goal:
        revised["goal"] = "夹带改变整份路线目标"

    rejected = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="测试空证据特例边界",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": failure_class,
            "diagnosis": "旧 expected_outputs 与真实输出不一致",
            "evidence_refs": [],
        },
    ))

    assert rejected["error_code"] == "route_recovery_basis_required"
    assert "evidence_refs" in rejected["error"]
    assert load_canonical_route(state)["route_ref"]["version"] == 1


def test_non_expected_output_failure_still_requires_new_evidence(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    binding = _record_single_step_failure(
        state,
        route,
        result={"status": "error", "reason": "compiler failed"},
    )
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "基于诊断修复编译"

    rejected = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="尚未保存诊断证据",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "execution",
            "diagnosis": "编译器返回失败",
            "evidence_refs": [],
        },
    ))

    assert rejected["error_code"] == "route_recovery_basis_required"
    assert "至少一条失败后新证据" in rejected["error"]


@pytest.mark.parametrize(
    "artifact_type",
    [
        "raw_results", "clean_results", "experiment_log",
        "declared_route", "pre_registration",
    ],
)
def test_reserved_artifact_types_cannot_unlock_route_recovery(
    tmp_path,
    artifact_type,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    binding = _record_single_step_failure(
        state,
        route,
        result={"status": "error", "reason": "compiler failed"},
    )
    saved = state.save_artifact(
        artifact_type,
        f"forged_{artifact_type}",
        "模型自述的恢复证据",
    )
    evidence_ref = f"artifact:{saved['id']}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "尝试用保留类型解锁"
    revised["evidence_refs"].append(evidence_ref)

    rejected = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="测试保留类型证据",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "execution",
            "diagnosis": "声称已诊断",
            "evidence_refs": [evidence_ref],
        },
    ))

    if artifact_type in {
        "raw_results", "clean_results", "experiment_log",
    }:
        # operation 身份一旦开始落盘，closure owner 必须先封口；
        # 这是比路线恢复证据更早、更严格的单一权威边界。
        assert rejected["error_code"] == (
            "operation_closure_reconciliation_required"
        )
        assert rejected["closure"]["sealed"] is True
        # 分治：这一档往往还有救——未冻结的草稿可以否定掉再原样重试，所以它不是
        # 终态。原来两档共用一句「开新 run」并标 retryable_after_change=False，
        # 把这一档说成了假终态。
        assert rejected["blocker"]["retryable_after_change"] is True
        assert "supersede_closure_draft" in rejected["error"] or (
            "continuation run" in rejected["error"])
        assert "supersedable_artifact_ids" in rejected["closure"]
    else:
        assert rejected["error_code"] == (
            "route_recovery_basis_required"
        )
        assert f"artifact_type={artifact_type!r}" in rejected["error"]


def test_diagnostic_evidence_still_unlocks_a_changed_failure_route(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    binding = _record_single_step_failure(
        state,
        route,
        result={"status": "error", "reason": "compiler failed"},
    )
    evidence_id = state.save_artifact(
        "diagnostic_evidence",
        "compiler_failure",
        "编译器缺少所需头文件。",
    )["id"]
    evidence_ref = f"artifact:{evidence_id}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "安装依赖后重新构建"
    revised["evidence_refs"].append(evidence_ref)

    recovered = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="新诊断表明缺少头文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "dependency",
            "diagnosis": "编译器缺少所需头文件",
            "evidence_refs": [evidence_ref],
        },
    ))

    assert recovered["status"] == "success"
    receipt = load_canonical_route(state)["record"]["metadata"][
        "validated_recovery_receipt"]
    assert receipt["attempt_receipt_used_as_evidence"] is False
    assert receipt["evidence_receipts"][0]["artifact_type"] == (
        "diagnostic_evidence")


def test_interrupted_external_attempt_cannot_be_cleared_by_route_amendment(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][2]]
    route["steps"][0]["after"] = []
    first = asyncio.run(_declare_execution_route(state, route=route))
    step = load_canonical_route(state)["route"]["steps"][0]
    _append_bound(
        state,
        first["route_ref"],
        step,
        "external-orphan",
        applied_policy="managed_external_job",
    )
    revised = _route(goal="错误地试图跳过未知外部提交")
    revised["steps"] = [revised["steps"][2]]
    revised["steps"][0]["after"] = []
    revised["evidence_refs"].append("scheduler:still-unknown")

    result = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="试图替换未知提交",
        recovery_basis={
            "attempt_id": "external-orphan",
            "failure_class": "external_identity",
            "diagnosis": "尚未取得 scheduler identity",
            "evidence_refs": ["scheduler:still-unknown"],
        },
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "route_recovery_basis_required"
    assert "scheduler/submission identity" in result["error"]


def test_external_submission_stays_in_progress_until_domain_finalization(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][2]]
    route["steps"][0]["after"] = []
    route["steps"][0]["action"]["program"] = "solver"
    declared = asyncio.run(_declare_execution_route(state, route=route))
    assert declared["status"] == "success"
    decision = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "solver",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["run_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
    })
    binding = begin_route_step_attempt(state, decision, tool="submit_job")
    finish_route_step_attempt(
        state,
        binding,
        result={
            "status": "success",
            "scheduler": "slurm",
            "job_id": "31415",
            "namespace": None,
            "submission_artifact_id": "job_submission__solver",
            "external_workflow_task_id": "task-solver",
        },
        external_submission=True,
    )

    submitted = build_route_snapshot(state)
    assert submitted["route_state"] == "in_progress"
    assert submitted["steps"]["smoke"]["state"] == "in_progress"
    output = Path(binding["resolved_workdir"]) / "smoke.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("solver finished\n", encoding="utf-8")

    projected = record_external_route_finalization(
        state,
        scheduler="slurm",
        job_id="31415",
        namespace=None,
        domain_outcome="analyzed_success",
        evidence_artifact_id="experiment_log__solver",
    )
    repeated = record_external_route_finalization(
        state,
        scheduler="slurm",
        job_id="31415",
        namespace=None,
        domain_outcome="analyzed_success",
        evidence_artifact_id="experiment_log__solver",
    )

    assert projected["status"] == "success"
    assert repeated["already_projected"] is True
    finalized = build_route_snapshot(state)
    assert finalized["steps"]["smoke"]["state"] == "verified"
    assert finalized["route_state"] == "complete"


def test_external_finalization_rejects_missing_output_and_conflicting_replay(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][2]]
    route["steps"][0]["after"] = []
    route["steps"][0]["action"]["program"] = "solver"
    asyncio.run(_declare_execution_route(state, route=route))
    decision = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "solver",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["run_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(state.root / "outputs" / "experiment" / "runtime"),
    })
    binding = begin_route_step_attempt(state, decision, tool="submit_job")
    finish_route_step_attempt(state, binding, result={
        "status": "success",
        "scheduler": "slurm",
        "job_id": "2718",
        "submission_artifact_id": "job_submission__missing-output",
    }, external_submission=True)

    missing = record_external_route_finalization(
        state,
        scheduler="slurm",
        job_id="2718",
        namespace=None,
        domain_outcome="analyzed_success",
        evidence_artifact_id="experiment_log__first",
    )
    conflict = record_external_route_finalization(
        state,
        scheduler="slurm",
        job_id="2718",
        namespace=None,
        domain_outcome="analyzed_failure",
        evidence_artifact_id="experiment_log__second",
    )

    assert missing["reason"] == "route_expected_outputs_missing"
    assert build_route_snapshot(state)["steps"]["smoke"]["state"] == "failed"
    assert conflict["reason"] == "route_external_finalization_conflict"


def test_preexisting_output_cannot_complete_a_new_attempt(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    asyncio.run(_declare_execution_route(state, route=route))
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    stale = workdir / "src" / "source.tar.gz"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "process_tree", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(state, decision, tool="safe_run_bash")

    outcome = finish_route_step_attempt(
        state, binding, result={"status": "success", "returncode": 0})

    assert outcome["failure_class"] == "expected_outputs_missing"


def test_explicit_entrypoint_and_requested_step_are_exact_bindings(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    asyncio.run(_declare_execution_route(state, route=route))
    snapshot = build_route_snapshot(state)

    wrong_path = resolve_execution_action(snapshot, {
        "tool": "submit_job",
        "program": "/tmp/compile",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    })
    typo = resolve_execution_action(snapshot, {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "buid",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    })
    compound = resolve_execution_action(snapshot, {
        "tool": "submit_job",
        "program": "compound:./compile+echo",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    })

    assert wrong_path["decision"] == "route_action_mismatch"
    assert execution_route_block(wrong_path)["reason"] == "execution_route_required"
    assert typo["decision"] == "route_step_binding_mismatch"
    assert execution_route_block(typo)["reason"] == "execution_route_step_binding_mismatch"
    assert compound["binding_mismatch"] == {
        "declared": {"tool": "submit_job", "program": "./compile"},
        "observed": {
            "tool": "submit_job",
            "program": "compound:./compile+echo",
        },
        "hint": (
            "action.program 只填单个可执行入口；每个高后果步骤单独调用，"
            "不要追加 &&、; 或管道命令。"
        ),
    }
    block = execution_route_block(compound)
    assert block["binding_mismatch"] == compound["binding_mismatch"]
    assert "compound:./compile+echo" in block["error"]


def test_goal_only_recovery_cannot_repeat_the_same_payload(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    asyncio.run(_declare_execution_route(state, route=route))
    decision = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(tmp_path / "build"),
    })
    action = {"payload_digest": "a" * 64}
    binding = begin_route_step_attempt(
        state, decision, tool="submit_job", action=action)
    finish_route_step_attempt(
        state, binding, result={"status": "error", "reason": "compile failed"})
    evidence_id = state.save_artifact(
        "diagnostic_evidence",
        "compile_failure",
        "编译器报告缺少头文件。",
    )["id"]
    evidence_ref = f"artifact:{evidence_id}"
    revised = route.copy()
    revised["steps"] = [dict(route["steps"][0])]
    revised["steps"][0]["goal"] = "再次运行同一个构建入口"
    revised["evidence_refs"] = [*route["evidence_refs"], evidence_ref]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="已定位缺少头文件",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "dependency",
            "diagnosis": "编译器缺少头文件",
            "evidence_refs": [evidence_ref],
        },
    ))
    assert amended["status"] == "success"
    retried = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    })
    retried.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(tmp_path / "build"),
    })

    blocked_binding = begin_route_step_attempt(
        state, retried, tool="submit_job", action=action)

    assert blocked_binding["binding_error"] == "route_recovery_payload_unchanged"
    assert route_binding_block(blocked_binding)["reason"] == (
        "route_recovery_payload_unchanged")




def test_unvalidated_recovery_basis_is_never_persisted_or_silently_ignored(
    tmp_path,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    first = asyncio.run(_declare_execution_route(state, route=route))
    assert first["status"] == "success"
    raw_basis = {
        "attempt_id": "forged-attempt",
        "failure_class": "execution",
        "diagnosis": "未经验证的自述",
        "evidence_refs": ["artifact:not-present"],
    }
    fresh = _state(tmp_path / "fresh")
    initial = asyncio.run(_declare_execution_route(
        fresh, route=route, recovery_basis=raw_basis))
    assert initial["error_code"] == "route_recovery_basis_not_applicable"
    assert load_canonical_route(fresh)["status"] != "ready"

    same = asyncio.run(_declare_execution_route(
        state, route=route, recovery_basis=raw_basis))
    assert same["error_code"] == "route_recovery_basis_not_applicable"

    revised = json.loads(json.dumps(route))
    revised["goal"] = "只改说明但伪造恢复依据"
    changed = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="测试未经验证 basis",
        recovery_basis=raw_basis,
    ))
    assert changed["error_code"] == "route_recovery_basis_not_applicable"
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any(
        event.get("event") == "declared_route_recovery_basis"
        for event in events
    )
    assert load_canonical_route(state)["route_ref"]["version"] == 1


def test_recovery_for_one_step_does_not_block_same_payload_in_another_step(
    tmp_path,
):
    state = _state(tmp_path)
    build = json.loads(json.dumps(_route()["steps"][1]))
    build["after"] = []
    build["expected_outputs"] = []
    first_step = json.loads(json.dumps(build))
    first_step["id"] = "build_a"
    second_step = json.loads(json.dumps(build))
    second_step["id"] = "build_b"
    route = {
        "schema_version": 2,
        "goal": "两个独立构建步骤",
        "evidence_refs": ["https://example.invalid/build"],
        "steps": [first_step, second_step],
    }
    asyncio.run(_declare_execution_route(state, route=route))
    workdir = tmp_path / "build"
    workdir.mkdir()
    action = {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "build_a",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    }
    first_decision = resolve_execution_context(state, action)
    first_decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    payload = {"payload_digest": "c" * 64}
    first_binding = begin_route_step_attempt(
        state, first_decision, tool="submit_job", action=payload)
    finish_route_step_attempt(
        state,
        first_binding,
        result={"status": "error", "reason": "compiler failed"},
    )
    evidence_id = state.save_artifact(
        "diagnostic_evidence", "build_a_failure", "build_a 编译器诊断")["id"]
    evidence_ref = f"artifact:{evidence_id}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "基于诊断重试 build_a"
    revised["evidence_refs"].append(evidence_ref)
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="build_a 失败诊断",
        recovery_basis={
            "attempt_id": first_binding["attempt_id"],
            "failure_class": "execution",
            "diagnosis": "build_a 编译器返回失败",
            "evidence_refs": [evidence_ref],
        },
    ))
    assert amended["status"] == "success"

    second_action = {**action, "route_step_id": "build_b"}
    second_decision = resolve_execution_context(state, second_action)
    second_decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    second_binding = begin_route_step_attempt(
        state, second_decision, tool="submit_job", action=payload)
    assert second_binding["route_step_id"] == "build_b"
    assert "binding_error" not in second_binding


def test_non_output_contract_change_does_not_unlock_resource_failure(
    tmp_path,
):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    asyncio.run(_declare_execution_route(state, route=route))
    workdir = tmp_path / "build"
    workdir.mkdir()
    action = {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    }
    decision = resolve_execution_context(state, action)
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    payload = {"payload_digest": "d" * 64}
    binding = begin_route_step_attempt(
        state, decision, tool="submit_job", action=payload)
    finish_route_step_attempt(
        state,
        binding,
        result={"status": "error", "reason": "OOM resource limit"},
    )
    evidence_id = state.save_artifact(
        "diagnostic_evidence", "resource_failure", "cgroup 报告内存耗尽")["id"]
    evidence_ref = f"artifact:{evidence_id}"
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["effects"].append("network_access")
    revised["evidence_refs"].append(evidence_ref)
    amended = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="记录资源故障，但尚未实际改变资源或命令",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "resource",
            "diagnosis": "cgroup 内存上限触发",
            "evidence_refs": [evidence_ref],
        },
    ))
    assert amended["status"] == "success"
    retried = resolve_execution_context(state, action)
    retried.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    blocked = begin_route_step_attempt(
        state, retried, tool="safe_run_bash", action=payload)
    assert blocked["binding_error"] == "route_recovery_payload_unchanged"

def test_route_amendment_inherits_only_unchanged_step_definitions(tmp_path):
    state = _state(tmp_path)
    first = asyncio.run(_declare_execution_route(state, route=_route()))
    loaded_v1 = load_canonical_route(state)
    acquire_v1 = loaded_v1["route"]["steps"][0]
    _append_bound(state, first["route_ref"], acquire_v1, "attempt-v1")
    state.append_transcript(
        "route_step_outcome",
        attempt_id="attempt-v1",
        outcome="success",
        evidence_refs=["artifact:source-archive"],
        verified_output_specs=["src/source.tar.gz"],
        verified_outputs=["/managed/source/src/source.tar.gz"],
    )

    goal_only = asyncio.run(_declare_execution_route(
        state,
        route=_route(goal="调整总目标但不改既有步骤"),
        amendment_reason="新增需求只改变路线总目标",
    ))
    assert goal_only["status"] == "success"
    inherited = build_route_snapshot(state)
    assert inherited["steps"]["acquire"]["state"] == "verified"
    assert inherited["ready_step_ids"] == ["build"]

    changed = _route(goal="调整步骤定义")
    changed["steps"][0]["goal"] = "从经过签名验证的镜像取得源码"
    amended = asyncio.run(_declare_execution_route(
        state,
        route=changed,
        amendment_reason="发现上游发布源需要签名验证",
    ))
    assert amended["status"] == "success"
    invalidated = build_route_snapshot(state)
    assert invalidated["steps"]["acquire"]["state"] == "pending"
    assert invalidated["ready_step_ids"] == ["acquire"]


def test_snapshot_marks_a_torn_transcript_tail_invalid_for_execution(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"route_step_bound","attempt_id":')

    snapshot = build_route_snapshot(state)

    assert snapshot["status"] == "ready"
    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["event_history_error"] == "ignored_malformed_jsonl_line"
    assert snapshot["transcript_warnings"] == ["ignored_malformed_jsonl_line"]


def test_snapshot_rejects_a_valid_json_event_without_line_termination(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "diagnostic_event"}))

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["event_history_error"] == "transcript_tail_unterminated"


def test_torn_transcript_still_allows_an_exact_read_only_probe(tmp_path):
    state = _state(tmp_path)
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(chr(123) + chr(34) + "event" + chr(34) + ":")
    action = {
        "tool": "submit_job",
        "program": "cat",
        "read_only": True,
        "observed_effects": [],
        "workdir_roles": ["source_baseline_root"],
        "dry_run": False,
    }
    decision = resolve_execution_context(state, action)

    assert decision["decision"] == "route_not_required"
    assert enforce_execution_route(
        state, action, decision, phase="pre_materialization",
    ) is None


def test_begin_attempt_rechecks_a_tail_torn_after_resolution(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    action = {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
        "dry_run": False,
    }
    decision = resolve_execution_context(state, action)
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": "/managed/source",
    })
    assert decision["decision"] == "matched_ready_step"
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(chr(123) + chr(34) + "event" + chr(34) + ":")
    broken_bytes = state.transcript_path.read_bytes()

    binding = begin_route_step_attempt(
        state, decision, tool="safe_run_bash", action=action,
    )

    assert binding["binding_error"] == "route_transcript_tail_unwritable"
    assert binding["history_warning"] == "ignored_malformed_jsonl_line"
    assert state.transcript_path.read_bytes() == broken_bytes


def test_snapshot_rejects_corruption_before_the_transcript_tail(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write("{not-json}\n")
        stream.write(json.dumps({"event": "later_valid_event"}) + "\n")

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["transcript_warnings"] == ["transcript_corrupt_before_tail"]


def test_snapshot_rejects_a_bound_event_without_attempt_identity(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    step = load_canonical_route(state)["route"]["steps"][0]
    state.append_transcript(
        "route_step_bound",
        route_artifact_id=declared["route_ref"]["artifact_id"],
        route_version=declared["route_ref"]["version"],
        route_content_hash=declared["route_ref"]["content_hash"],
        route_step_id=step["id"],
        step_definition_hash=step_definition_hash(step),
    )

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert "route_bound_missing_attempt_id" in snapshot["transcript_warnings"]


def test_snapshot_rejects_one_attempt_identity_bound_to_two_steps(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_route()))
    acquire, build, _smoke = load_canonical_route(state)["route"]["steps"]
    _append_bound(state, declared["route_ref"], acquire, "duplicate-attempt")
    _append_bound(state, declared["route_ref"], build, "duplicate-attempt")

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert "duplicate_attempt_identity" in snapshot["transcript_warnings"]


def test_snapshot_fails_closed_when_transcript_is_unreadable(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == state.transcript_path:
            raise OSError("transcript storage unavailable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    snapshot = build_route_snapshot(state)

    assert snapshot["route_state"] == "invalid_event_history"
    assert snapshot["ready_step_ids"] == []
    assert snapshot["event_history_error"] == "transcript_unreadable"


def test_resolver_requires_no_route_for_read_only_and_unions_risk_effects(tmp_path):
    state = _state(tmp_path)
    no_route = build_route_snapshot(state)
    read_only = resolve_execution_action(no_route, {
        "tool": "safe_run_bash",
        "program": "cat",
        "read_only": True,
        "observed_effects": [],
        "workdir_roles": ["source_baseline_root"],
    })
    effectful = resolve_execution_action(no_route, {
        "tool": "safe_run_bash",
        "program": "make",
        "read_only": False,
        "observed_effects": ["process_tree"],
        "workdir_roles": ["build_root"],
    })
    assert read_only["decision"] == "route_not_required"
    assert effectful["decision"] == "route_unavailable"

    asyncio.run(_declare_execution_route(state, route=_route()))
    snapshot = build_route_snapshot(state)
    matched = resolve_execution_action(snapshot, {
        "tool": "safe_run_bash",
        "program": "/usr/bin/curl",
        "read_only": False,
        "observed_effects": ["process_tree"],
        "workdir_roles": ["managed_source_root"],
    })
    assert matched["decision"] == "matched_ready_step"
    assert matched["route_step_id"] == "acquire"
    assert matched["effective_effects"] == [
        "network_access", "process_tree", "workspace_write",
    ]
    assert matched["policy"] == "guarded_process"



def test_planned_execution_requires_scope_classification_before_side_effect(
    tmp_path,
):
    state = _unclassified_state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["expected_outputs"] = []
    asyncio.run(_declare_execution_route(state, route=route))

    action = {
        "tool": "submit_job",
        "program": "./compile",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
    }
    unclassified = resolve_execution_context(state, action)
    block = execution_route_block(
        unclassified, phase="pre_materialization")

    assert unclassified["decision"] == "matched_ready_step"
    assert unclassified["scope_status"] == "absent"
    assert route_default_workdir(
        state, unclassified, create=False)["status"] == "resolved"
    assert block["reason"] == "experiment_scope_classification_required"
    assert block["required_tool"] == "classify_experiment_scope"
    assert block["blocker"]["node_action"] == (
        "classify_experiment_scope_before_execution")

    classified_scope = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="路线测试恢复分类后必须绑定同一份稳定的上游测试输入。",
    ))
    assert classified_scope["status"] == "success", classified_scope
    refreshed = asyncio.run(_declare_execution_route(
        state,
        route=route,
        amendment_reason="scope classification attached the immutable test intent",
    ))
    assert refreshed["status"] == "success", refreshed
    classified = resolve_execution_context(state, action)
    assert classified["decision"] == "matched_ready_step"
    assert classified["scope_status"] == "classified"
    assert execution_route_block(
        classified, phase="pre_materialization") is None


def test_resolver_exception_uses_observed_effects_for_fail_closed_policy(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.build_route_snapshot",
        lambda _state: (_ for _ in ()).throw(RuntimeError("resolver down")),
    )

    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "./unknown-official-wrapper",
        "read_only": False,
        "observed_effects": ["workspace_write"],
        "workdir_roles": ["build_root"],
    })

    assert decision["decision"] == "resolver_error"
    assert decision["policy"] == "low_risk_effectful"
    assert execution_route_block(
        decision, phase="pre_materialization")["reason"] == (
            "execution_route_resolver_failed")


def test_scope_is_required_for_unrouted_writes_but_not_read_only_or_dry_run(
    tmp_path,
):
    state = _unclassified_state(tmp_path)

    write_decision = resolve_execution_context(state, {
        "tool": "safe_execute_python",
        "program": "python",
        "read_only": False,
        "observed_effects": ["workspace_write"],
        "workdir_roles": [],
        "dry_run": False,
    })
    write_block = execution_route_block(
        write_decision, phase="pre_materialization")

    assert write_decision["policy"] == "low_risk_effectful"
    assert write_decision["scope_required"] is True
    assert write_block["reason"] == "experiment_scope_classification_required"

    read_only = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "pwd",
        "read_only": True,
        "observed_effects": [],
        "workdir_roles": [],
        "dry_run": False,
    })
    dry_run = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "solver",
        "read_only": False,
        "observed_effects": ["workspace_write"],
        "workdir_roles": [],
        "dry_run": True,
    })

    assert execution_route_block(
        read_only, phase="pre_materialization") is None
    assert execution_route_block(
        dry_run, phase="pre_materialization") is None


def test_scientific_route_waits_for_acceptance_then_rejects_operational_scope(
    tmp_path,
):
    state = _unclassified_state(tmp_path)
    route = {
        "schema_version": 2,
        "goal": "验证 scope 与科学路线的顺序无关不变量",
        "evidence_refs": ["test:scope-route-order"],
        "steps": [{
            "id": "solve",
            "goal": "执行正式科学计算",
            "after": [],
            "action": {"tool": "submit_job", "program": "./solver"},
            "effects": [
                "workspace_write", "process_tree", "scientific_execution",
                "external_job",
            ],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    before_acceptance = asyncio.run(_declare_execution_route(
        state, route=route,
    ))
    assert before_acceptance["status"] == "error"
    assert before_acceptance["error_code"] == (
        "prereg_assignment_authority_unavailable"
    )
    assert load_canonical_route(state)["status"] != "ready"

    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="This fixture verifies an operational scope cannot carry science.",
    ))
    assert classified["status"] == "success", classified

    after_acceptance = asyncio.run(_declare_execution_route(
        state, route=route,
    ))
    assert after_acceptance["status"] == "error"
    assert after_acceptance["error_code"] == "route_scope_effect_mismatch"


def test_route_gate_is_proportional_but_resolver_errors_fail_closed():
    assert execution_route_block({
        "decision": "route_not_required",
        "policy": "read_only",
        "read_only": True,
    }) is None
    assert execution_route_block({
        "decision": "route_unavailable",
        "policy": "low_risk_effectful",
        "reason": "missing",
    }) is None

    blocked = execution_route_block({
        "decision": "route_unavailable",
        "policy": "guarded_process",
        "reason": "missing",
    })
    assert blocked is not None
    assert blocked["reason"] == "execution_route_required"
    assert blocked["blocker"]["suggested_owner"] == "experiment"

    low_risk_resolver_failed = execution_route_block({
        "decision": "resolver_error",
        "policy": "low_risk_effectful",
        "reason": "RuntimeError",
    })
    assert low_risk_resolver_failed is not None
    assert low_risk_resolver_failed["reason"] == "execution_route_resolver_failed"

    resolver_failed = execution_route_block({
        "decision": "resolver_error",
        "policy": "managed_external_job",
        "reason": "RuntimeError",
    })
    assert resolver_failed is not None
    assert resolver_failed["reason"] == "execution_route_resolver_failed"
    assert resolver_failed["blocker"]["suggested_owner"] == "framework"

    contradictory = execution_route_block({
        "decision": "route_unavailable",
        "policy": "read_only",
        "read_only": True,
        "effective_effects": ["process_tree"],
    })
    assert contradictory is not None
    # 仍然失败关闭；理由从"需要受管生命周期"变成"需要先声明路线"——光会 fork 不再
    # 等于需要外部作业身份（2026-09-12 效应层拆词）。
    assert contradictory["reason"] == "execution_route_required"


def test_route_gate_blocks_wrong_workdir_and_reexecution_after_failure():
    wrong_workdir = execution_route_block({
        "decision": "matched_ready_step",
        "authoritative": True,
        "policy": "low_risk_effectful",
        "route_step_id": "prepare",
        "declared_workdir_role": "run_root",
        "workdir_role_observed": False,
        "workdir_resolution_status": "explicit",
    })
    assert wrong_workdir is not None
    assert wrong_workdir["reason"] == "execution_route_workdir_mismatch"

    repeated = execution_route_block({
        "decision": "route_step_not_ready",
        "policy": "low_risk_effectful",
        "route_step_id": "prepare",
        "step_state": "failed",
    })
    assert repeated is not None
    assert repeated["reason"] == "execution_route_step_not_ready"
    assert repeated["blocker"]["node_action"] == "diagnose_then_amend_route"


def test_shadow_records_only_a_bounded_summary_and_never_controls_execution(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    action = {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        # 机械观察到 process tree，而 legacy stage 没有安装强守卫：
        # 这是需要保留的真实分歧，不是常态调用日志。
        "observed_effects": ["workspace_write", "process_tree"],
        "workdir_roles": ["managed_source_root"],
        "legacy_policy": {"stage": "diagnostic", "guarded_build": False},
        "raw_command": "这一字段不应写入 transcript",
    }

    decision = shadow_execution_route(state, action)
    events = []
    if state.transcript_path.exists():
        events = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    shadow = [event for event in events if event.get("event") == "route_resolution_shadow"][-1]

    assert decision["decision"] == "matched_ready_step"
    assert "raw_command" not in shadow
    assert "code" not in shadow
    assert len(shadow["action_signature"]) == 64


def test_read_only_shadow_does_not_load_route_or_transcript(tmp_path, monkeypatch):
    state = _state(tmp_path)

    def should_not_run(_state):
        raise AssertionError("只读影子路径不应读取 route snapshot")

    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.build_route_snapshot",
        should_not_run,
    )
    decision = shadow_execution_route(state, {
        "tool": "safe_run_bash",
        "program": "cat",
        "read_only": True,
        "observed_effects": [],
        "workdir_roles": ["source_baseline_root"],
    })

    assert decision["decision"] == "route_not_required"
    events = []
    if state.transcript_path.exists():
        events = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    assert not any(
        event.get("event") == "route_resolution_shadow" for event in events
    )


def test_route_workdir_selects_one_existing_authority_role_and_creates_only_framework_default(
    tmp_path,
):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_route()))
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": [],
    })

    selected = route_default_workdir(state, decision, create=True)

    expected = state.root / "outputs" / "experiment" / "runtime" / "source"
    assert selected == {
        "status": "resolved",
        "role": "managed_source_root",
        "path": str(expected),
    }
    assert expected.is_dir()


def test_route_workdir_refuses_to_guess_between_multiple_roots(tmp_path):
    state = _state(tmp_path)
    first = tmp_path / "build-a"
    second = tmp_path / "build-b"
    state.hook_state["path_roles"] = {
        "build_root": [str(first), str(second)],
    }
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    route["steps"][0]["action"]["program"] = "make"
    asyncio.run(_declare_execution_route(state, route=route))
    decision = resolve_execution_context(state, {
        "tool": "submit_job",
        "program": "make",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": [],
    })

    selected = route_default_workdir(state, decision, create=True)

    assert selected["status"] == "ambiguous"
    assert selected["role"] == "build_root"
    assert len(selected["paths"]) == 3  # 两个显式根 + 一个 run-local 默认根


@pytest.mark.parametrize("tool", ("safe_run_bash", "safe_execute_python"))
def test_external_job_effect_has_one_managed_tool_owner(tool):
    violation = external_job_contract_violation(
        tool, ["workspace_write", "external_job"])
    assert violation == {
        "contract_code": "external_job_requires_submit_job",
        "required_tool": "submit_job",
        "message": (
            "external_job 只能由 submit_job 承担；safe_run_bash/"
            "safe_execute_python 没有外部作业身份与完成收据生命周期"
        ),
    }

    route = _route()
    route["steps"][0]["action"]["tool"] = tool
    route["steps"][0]["effects"] = ["workspace_write", "external_job"]
    report = validate_route_v2(route)

    assert report["valid"] is False
    assert any(
        "external_job 只能由 submit_job 承担" in error
        for error in report["errors"]
    )


@pytest.mark.parametrize("tool", ("safe_run_bash", "safe_execute_python"))
@pytest.mark.parametrize("effect", ("managed_lifecycle", "scientific_execution"))
def test_work_that_outlives_the_call_has_one_durable_lifecycle_owner(tool, effect):
    """所有权判据是"要不要活过本次调用"，不是"会不会 fork"。"""
    violation = external_job_contract_violation(tool, [effect])

    assert violation["contract_code"] == "process_tree_requires_submit_job"
    assert violation["required_tool"] == "submit_job"


@pytest.mark.parametrize("tool", ("safe_run_bash", "safe_execute_python"))
def test_forking_alone_does_not_demand_a_managed_job(tool):
    """2026-09-12：process_tree 只是机械事实。

    几乎什么都 fork（tar 调 gzip、make 调编译器），把它当所有权判据会把"解个压缩包"
    逼去走提交作业那条重路。事实照旧申报（资源守卫、进程数限额、策略档位都还靠它），
    但不再单独决定所有权。
    """
    assert external_job_contract_violation(
        tool, ["workspace_write", "process_tree", "environment_change"]) is None


def test_submit_job_route_must_declare_external_job_effect():
    violation = external_job_contract_violation(
        "submit_job", ["workspace_write", "process_tree"])
    assert violation["contract_code"] == "submit_job_requires_external_job"
    assert violation["required_effect"] == "external_job"

    route = _route()
    route["steps"][2]["effects"] = ["workspace_write", "process_tree"]
    report = validate_route_v2(route)

    assert report["valid"] is False
    assert any(
        "submit_job 路线步骤必须显式声明 external_job" in error
        for error in report["errors"]
    )


@pytest.mark.parametrize("tool", ("safe_run_bash", "safe_execute_python"))
@pytest.mark.parametrize("phase", ("pre_materialization", "pre_spawn"))
def test_runtime_reuses_external_job_owner_contract(tool, phase):
    decision = {
        "decision": "matched_ready_step",
        "authoritative": True,
        "tool": tool,
        "policy": "managed_external_job",
        "effective_effects": ["workspace_write", "external_job"],
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "route_step_id": "escaped-job",
    }

    block = execution_route_block(decision, phase=phase)

    assert block["reason"] == "execution_route_external_job_owner_mismatch"
    assert block["required_tool"] == "submit_job"
    assert block["contract_code"] == "external_job_requires_submit_job"
    assert block["blocker"]["required_tool"] == "submit_job"


@pytest.mark.parametrize("phase", ("pre_materialization", "pre_spawn"))
def test_runtime_accepts_external_job_only_through_submit_job(phase):
    decision = {
        "decision": "matched_ready_step",
        "authoritative": True,
        "tool": "submit_job",
        "policy": "managed_external_job",
        "effective_effects": ["workspace_write", "external_job"],
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "route_step_id": "managed-job",
    }

    assert execution_route_block(decision, phase=phase) is None


def test_static_submit_program_sequence_binds_one_ready_step(tmp_path):
    state = _state(tmp_path)
    sequence = [
        "cmake", "cmake", "./hf_toolchain_smoke", "sha256sum",
    ]
    route = {
        "schema_version": 2,
        "goal": "Run a statically analyzable configure-build-smoke job",
        "evidence_refs": ["test:static-submit-sequence"],
        "steps": [{
            "id": "build_and_run",
            "goal": "Configure, build, and smoke in one managed job",
            "after": [],
            "action": {
                "tool": "submit_job",
                "program": "cmake",
                "program_sequence": sequence,
            },
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    declared = asyncio.run(_declare_execution_route(state, route=route))
    assert declared["status"] == "success", declared
    snapshot = build_route_snapshot(state)
    assert declared["execution_guidance"]["steps"][0]["program_sequence"] == sequence
    action = {
        "tool": "submit_job",
        "program": "cmake",
        "program_sequence": list(sequence),
        "route_step_id": "build_and_run",
        "read_only": False,
        "observed_effects": ["workspace_write", "process_tree", "external_job"],
        "workdir_roles": ["run_root"],
    }

    matched = resolve_execution_action(snapshot, action)
    assert matched["decision"] == "matched_ready_step"

    for invalid in (
        {**action, "program_sequence": [
            "cmake", "./hf_toolchain_smoke", "cmake", "sha256sum",
        ]},
        {**action, "program_sequence": [
            "cmake", "cmake", "./hf_toolchain_smoke", "sha256sum", "tee",
        ]},
        {**action, "program": "sha256sum"},
        {key: value for key, value in action.items() if key != "program_sequence"},
    ):
        decision = resolve_execution_action(snapshot, invalid)
        assert decision["decision"] == "route_step_binding_mismatch"
        assert decision["mismatch_kind"] == "compound_sequence_mismatch"
        assert execution_route_block(decision)["reason"] == (
            "execution_route_step_binding_mismatch"
        )

    # 两种失配的成因完全不同，提示必须分开说（2026-09-02 真实 E2E）：
    # 节点把 program_sequence 当成了单个程序的 argv（["python3","x.py","0.01"]），
    # 声明期全部合法、路线照常冻结，直到提交才失配。此时命令只有一个入口、
    # 观测侧压根没有 program_sequence，而旧提示却说"不要增删、重排入口"——
    # 节点没有重排任何东西，于是它得出「submit_job 不接受 program_sequence
    # 参数」这个错误结论才绕开。真正要纠正的是 argv/入口序列这个概念混淆。
    reordered = resolve_execution_action(snapshot, {**action, "program_sequence": [
        "cmake", "cmake", "sha256sum", "./hf_toolchain_smoke",
    ]})
    not_compound = resolve_execution_action(snapshot, {
        key: value for key, value in action.items() if key != "program_sequence"
    })
    reordered_hint = str((reordered.get("binding_mismatch") or {}).get("hint") or "")
    not_compound_hint = str((not_compound.get("binding_mismatch") or {}).get("hint") or "")
    assert reordered_hint != not_compound_hint, "两种成因不能共用一句提示"
    assert "重排" in reordered_hint, reordered_hint
    assert "argv" in not_compound_hint, not_compound_hint
    assert "参数表" in not_compound_hint, not_compound_hint
    # 误导性措辞不得出现在"命令本就不是复合命令"这一支上
    assert "重排" not in not_compound_hint, not_compound_hint


def test_program_sequence_schema_is_submit_job_only_bounded_and_primary_exact():
    route = _route()
    route["steps"] = [route["steps"][1]]
    step = route["steps"][0]
    step["after"] = []
    step["action"] = {
        "tool": "submit_job",
        "program": "cmake",
        "program_sequence": ["cmake", "cmake"],
    }

    assert validate_route_v2(route)["valid"] is True

    wrong_primary = json.loads(json.dumps(route))
    wrong_primary["steps"][0]["action"]["program"] = "make"
    assert any("第一项" in error for error in validate_route_v2(wrong_primary)["errors"])

    wrong_tool = json.loads(json.dumps(route))
    wrong_tool["steps"][0]["action"]["tool"] = "safe_run_bash"
    assert any("只允许 submit_job" in error for error in validate_route_v2(wrong_tool)["errors"])

    oversized = json.loads(json.dumps(route))
    oversized["steps"][0]["action"]["program_sequence"] = ["cmake"] * 65
    assert any("只允许 submit_job" in error for error in validate_route_v2(oversized)["errors"])


def _single_build_step_route() -> dict:
    route = _route()
    route["steps"] = [route["steps"][1]]
    route["steps"][0]["after"] = []
    return route


def _mismatched_build_action() -> dict:
    return {
        "tool": "submit_job",
        "program": "compound:./compile+echo",
        "route_step_id": "build",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
        "dry_run": False,
    }


def _enforce(state: State, action: dict) -> dict:
    return enforce_execution_route(
        state, action, resolve_execution_context(state, action),
    )


def _transcript_events(state: State) -> list[dict]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_third_same_key_route_block_escalates_to_exhausted_blocker(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_single_build_step_route()))
    action = _mismatched_build_action()

    first = _enforce(state, action)
    second = _enforce(state, action)
    third = _enforce(state, action)

    assert first["reason"] == "execution_route_step_binding_mismatch"
    assert second["reason"] == "execution_route_step_binding_mismatch"
    assert third["status"] == "error"
    assert third["route_blocked"] is True
    assert third["reason"] == "route_attempts_exhausted"
    assert third["underlying_reason"] == "execution_route_step_binding_mismatch"
    blocker = third["blocker"]
    assert blocker["kind"] == "route_attempts_exhausted"
    assert blocker["underlying_kind"] == "execution_route_step_binding_mismatch"
    assert blocker["attempts"] == 3
    assert blocker["max_attempts"] == 3
    assert blocker["node_action"] == (
        "amend_route_with_amendment_reason_or_report_blocker"
    )
    assert "amendment_reason" in third["error"]
    assert "report_blocker" in third["error"]

    events = _transcript_events(state)
    blocked = [e for e in events if e.get("event") == "execution_route_blocked"]
    exhausted = [e for e in events if e.get("event") == "route_attempts_exhausted"]
    assert len(blocked) == 3
    assert len(exhausted) == 1
    assert exhausted[0]["attempts"] == 3
    assert exhausted[0]["underlying_reason"] == (
        "execution_route_step_binding_mismatch"
    )


def test_route_block_attempts_do_not_accumulate_across_kinds(tmp_path):
    state = _state(tmp_path)
    asyncio.run(_declare_execution_route(state, route=_single_build_step_route()))
    mismatch = _mismatched_build_action()
    unrouted = {
        "tool": "submit_job",
        "program": "/tmp/compile",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_roles": ["build_root"],
        "dry_run": False,
    }

    assert _enforce(state, mismatch)["reason"] == (
        "execution_route_step_binding_mismatch"
    )
    assert _enforce(state, unrouted)["reason"] == "execution_route_required"
    assert _enforce(state, unrouted)["reason"] == "execution_route_required"
    fourth = _enforce(state, mismatch)
    assert fourth["reason"] == "execution_route_step_binding_mismatch"
    fifth = _enforce(state, unrouted)
    assert fifth["reason"] == "route_attempts_exhausted"
    assert fifth["underlying_reason"] == "execution_route_required"


def test_bound_step_resets_the_same_key_block_counter(tmp_path):
    state = _state(tmp_path)
    declared = asyncio.run(
        _declare_execution_route(state, route=_single_build_step_route()))
    build_step = load_canonical_route(state)["route"]["steps"][0]
    action = _mismatched_build_action()

    assert _enforce(state, action)["reason"] == (
        "execution_route_step_binding_mismatch"
    )
    assert _enforce(state, action)["reason"] == (
        "execution_route_step_binding_mismatch"
    )
    _append_bound(state, declared["route_ref"], build_step, "attempt-build")

    third = _enforce(state, action)
    fourth = _enforce(state, action)
    fifth = _enforce(state, action)

    assert third["reason"] == "execution_route_step_binding_mismatch"
    assert fourth["reason"] == "execution_route_step_binding_mismatch"
    assert fifth["reason"] == "route_attempts_exhausted"
    assert fifth["attempts"] == 3


@pytest.mark.parametrize("reason", [
    "payload_exec_preflight_rejected",
    "payload_abi_preflight_rejected",
    "payload_executable_changed_after_approval",
])
def test_payload_executable_preflight_rejections_return_step_to_ready(
    tmp_path, reason,
):
    """E-14：提交链可执行目标预检/批准后换二进制复检都是零执行拒绝，
    必须走可恢复通道回到 ready，绝不写终态 failed。"""
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [dict(route["steps"][1], after=[])]
    declared = asyncio.run(_declare_execution_route(state, route=route))
    loaded = load_canonical_route(state)
    build = loaded["route"]["steps"][0]
    binding = {
        "attempt_id": f"preflight-{reason}",
        "route_artifact_id": declared["route_ref"]["artifact_id"],
        "route_version": declared["route_ref"]["version"],
        "route_content_hash": declared["route_ref"]["content_hash"],
        "route_step_id": build["id"],
        "step_definition_hash": step_definition_hash(build),
        "tool": "submit_job",
        "resolved_workdir_role": "build_root",
        "applied_policy": "managed_external_job",
    }
    state.append_transcript("route_step_bound", **binding)

    event = finish_route_step_attempt(
        state, binding,
        result={
            "status": "error",
            "reason": reason,
            "error": "payload executable preflight rejected before spawn",
        },
        external_submission=True,
    )

    assert event["outcome"] == "rejected"
    assert event["failure_class"] == "infrastructure_rejection"
    assert event["rejection_reason"] == reason
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["build"]["state"] == "pending"
    assert snapshot["ready_step_ids"] == ["build"]
    assert snapshot["route_state"] == "actionable"


def test_a_never_attempted_step_is_not_told_to_recover_from_a_failure():
    """pending 不是失败：不该发"先诊断根因、改步骤定义、补修复件"那套。

    2026-09-10 两份活体实测：step_state=pending 时照发重开失败步骤的指引，模型照着做
    只能白转——这一步从未失败，没有根因可诊断，也没有定义要改。真实原因要么是前置没
    核验完，要么它根本不是当前该做的那一步，指引必须说这个。
    """
    blocked = execution_route_block({
        "decision": "route_step_not_ready",
        "policy": "low_risk_effectful",
        "route_step_id": "run_solver",
        "step_state": "pending",
        "dependencies": ["build"],
        "ready_step_ids": ["build"],
        "route_state": "actionable",
    })

    assert blocked is not None
    assert blocked["reason"] == "execution_route_step_not_ready"
    assert blocked["blocker"]["node_action"] == "run_ready_step_or_fix_dependencies"
    message = blocked["error"]
    assert "从未失败" in message
    assert "['build']" in message          # 前置与当前可执行步骤都要指名道姓
    for misleading in ("step_definition_hash", "remediation_refs", "诊断根因"):
        assert misleading not in message, message


def test_a_genuine_failed_step_still_gets_the_reopen_instructions():
    """真执行失败仍须诊断、给新证据并改变步骤定义。"""
    blocked = execution_route_block({
        "decision": "route_step_not_ready",
        "policy": "low_risk_effectful",
        "route_step_id": "run_solver",
        "step_state": "failed",
        "step_reason": "execution_failed",
    })

    assert blocked is not None
    assert blocked["blocker"]["node_action"] == "diagnose_then_amend_route"
    assert "step_definition_hash" in blocked["error"]
    assert "route.evidence_refs" in blocked["error"]
    assert "remediation_refs" in blocked["error"]


def test_a_verified_step_is_told_to_declare_a_new_step():
    blocked = execution_route_block({
        "decision": "route_step_not_ready",
        "policy": "low_risk_effectful",
        "route_step_id": "run_solver",
        "step_state": "verified",
        "step_reason": None,
        "step_definition_hash": "a" * 64,
    })

    assert blocked is not None
    assert blocked["blocker"]["node_action"] == "declare_new_step"
    assert "已经成功" in blocked["error"]
    assert "新的步骤" in blocked["error"]
    for misleading in ("step_definition_hash", "route.evidence_refs"):
        assert misleading not in blocked["error"]
    assert "step_definition_hash" not in blocked
    assert "step_definition_hash" not in blocked["blocker"]


def test_invalid_local_success_receipt_is_not_advertised_as_pure_correction(
    tmp_path,
):
    """文案必须复用门的收据判据；非零返回码不能被领去纯纠正。"""
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0]]
    assert asyncio.run(_declare_execution_route(state, route=route))[
        "status"] == "success"
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    binding = begin_route_step_attempt(
        state,
        {
            **decision,
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(tmp_path / "work"),
        },
        tool="safe_run_bash",
        action={"payload_digest": "f" * 64},
    )
    state.append_transcript(
        "route_step_outcome",
        attempt_id=binding["attempt_id"],
        route_step_id="acquire",
        outcome="failed",
        failure_class="expected_outputs_missing",
        missing_expected_outputs=["src/source.tar.gz"],
        managed_tool_receipt={"status": "success", "returncode": 1},
    )

    action = {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    }
    stopped = resolve_execution_context(state, action)
    assert stopped["step_reason"] == "expected_outputs_missing"
    blocked = execution_route_block(stopped)

    assert blocked is not None
    assert "本地纯纠正不适用" in blocked["error"]
    assert "有效的本地成功收据" in blocked["error"]
    assert "recovery_basis.evidence_refs=[]" not in blocked["error"]
    assert "step_definition_hash" in blocked["error"]


def test_the_route_required_refusal_names_the_tool_and_leads_with_the_next_step():
    """要模型先声明路线，就得把工具名和顺序放在最前面，而不是埋在五行豁免之后。"""
    blocked = execution_route_block({
        "decision": "no_route",
        "policy": "managed_external_job",
        "tool": "submit_job",
        "observed_effects": ["external_job"],
    })

    assert blocked is not None
    assert blocked["reason"] == "execution_route_required"
    message = blocked["error"]
    assert "declare_execution_route" in message
    assert "route_step_id" in message
    # 可执行指引要出现在豁免说明之前
    assert message.index("declare_execution_route") < message.index("只读调查")


@pytest.mark.parametrize("cmd, needs_managed_job", [
    ("tar -xzf src.tgz -C build/", False),     # 解包：有界、本地
    ("unzip data.zip", False),
    ("cmake -S . -B build", False),            # configure
    ("pip install numpy", False),
    ("curl -L -o x.tgz http://example.invalid/x.tgz", False),
    ("make -j4", True),                        # 真构建
    ("ninja -C build", True),
    ("mpirun -n 4 ./solver", True),            # 真启动器
])
def test_only_work_that_needs_a_job_identity_is_pushed_to_submit_job(
    cmd, needs_managed_job,
):
    """拆词之后的分界线：有界的本地动作留在 safe_run_bash，真构建与启动器走 submit_job。

    同时钉住"事实不降级"：这些命令照旧申报 process_tree，所以资源守卫、进程数限额和
    guarded_process 档位一个都没丢——拆的是所有权，不是事实。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import safe_bash as sb

    action = sb._bash_route_action(cmd, execution_stage="diagnostic")
    effects = action["observed_effects"]
    violation = external_job_contract_violation("safe_run_bash", effects)

    assert "process_tree" in effects, effects          # 机械事实照旧
    assert _policy_for_effects(set(effects), read_only=False) == "guarded_process"
    assert ("managed_lifecycle" in effects) is needs_managed_job, effects
    assert (violation is not None) is needs_managed_job, (cmd, effects, violation)


def _one_step_route(tool: str, program: str, effects: list[str]) -> dict:
    return {
        "schema_version": 2, "goal": "g", "evidence_refs": ["user_original_input"],
        "steps": [{
            "id": "s", "goal": "g", "after": [],
            "action": {"tool": tool, "program": program},
            "effects": effects, "workdir_role": "run_root", "expected_outputs": [],
        }],
    }


@pytest.mark.parametrize("program, rejected", [
    ("make", True), ("ninja", True), ("mpirun", True), ("srun", True),
    ("tar", False), ("cmake", False), ("python", False),
])
def test_declaring_a_real_build_or_launcher_on_safe_run_bash_is_caught_at_declaration(
    program, rejected,
):
    """声明时就拦下，而不是声明照过、执行才拒（声明期只看 program；make -n 这类探查
    执行期放行，声明期不区分——探查本就不该写成路线步骤）。

    cmake 要看有没有 --build 才知道是不是构建，声明期判不出，留给执行期。
    """
    from nodes.experiment.tools.execution_route import validate_route_v2

    out = validate_route_v2(
        _one_step_route("safe_run_bash", program, ["process_tree"]), declaring=True)

    assert (not out["valid"]) is rejected, out
    if rejected:
        assert any("submit_job" in error and program in error
                   for error in out["errors"]), out


def test_the_same_real_build_declared_on_submit_job_is_valid():
    from nodes.experiment.tools.execution_route import validate_route_v2

    out = validate_route_v2(
        _one_step_route("submit_job", "make", ["external_job", "process_tree"]),
        declaring=True)

    assert out["valid"], out


@pytest.mark.parametrize("program", ["safe_run_bash", "safe_execute_python", "submit_job"])
def test_a_tool_name_is_not_an_executable_entry(program):
    """问题 G：program 写成工具名，路线声明照过，依赖它的步骤永远匹配不上执行动作。"""
    from nodes.experiment.tools.execution_route import (
        _KNOWN_ACTION_TOOLS, validate_route_v2,
    )

    assert program in _KNOWN_ACTION_TOOLS
    out = validate_route_v2(_one_step_route("safe_run_bash", program, []), declaring=True)

    assert not out["valid"], out
    assert any("是工具名" in error for error in out["errors"]), out


def _route_with_tool_name_in_sequence() -> dict:
    route = _one_step_route("submit_job", "solver", ["external_job", "process_tree"])
    route["steps"][0]["action"]["program_sequence"] = ["solver", "submit_job"]
    return route


@pytest.mark.parametrize("route", [
    _one_step_route("safe_run_bash", "make", ["process_tree"]),
    _one_step_route("safe_run_bash", "submit_job", []),
    _route_with_tool_name_in_sequence(),
], ids=["real_build_on_safe_run_bash", "tool_name_as_program", "tool_name_in_sequence"])
def test_reading_a_frozen_route_does_not_apply_declaration_only_rules(route):
    """读取路径也经 validate_route_v2 规范化已冻结的历史版本。只挡新声明的规则不能回头判
    升级前合法冻结的路线，否则整条路线失效（2026-09-14 第三会话复审 P2）。"""
    from nodes.experiment.tools.execution_route import validate_route_v2

    assert validate_route_v2(route)["valid"], validate_route_v2(route)
    assert not validate_route_v2(route, declaring=True)["valid"]
