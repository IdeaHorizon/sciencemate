from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools.execution_envelope import (
    EXECUTION_ENVELOPE_TYPE,
    _declare_execution_envelope,
    execution_envelope_gate_mode,
    execution_envelope_refs_for_action,
    missing_execution_envelope_steps,
    parse_execution_envelope_ref,
    resolve_execution_envelope_ref,
    validate_execution_envelope_v1,
)
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    execution_route_block,
    resolve_execution_context,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _state(tmp_path: Path) -> State:
    return State.new("experiment", tmp_path / "runs", project_id="envelope-project")


def _freeze(state: State, artifact_id: str) -> None:
    from shared.tools.library.artifacts_extra import _freeze_artifact

    result = asyncio.run(_freeze_artifact(state, artifact_id, "test fixture"))
    assert result["status"] == "success", result


def _frozen_lock(state: State, name: str = "toolchain") -> str:
    saved = state.save_artifact("toolchain_lock", name, '{"compiler": "test"}')
    _freeze(state, saved["id"])
    return saved["id"]


def _profile() -> dict[str, str]:
    return {"id": "test-target-profile", "content_digest": "sha256:" + "a" * 64}


def _shared_run_storage() -> dict:
    return {
        "kind": "shared_filesystem",
        "roles": {"run": "run_root", "output": "run_root"},
    }


def _operation_envelope(state: State, *, route_step_id: str = "build") -> dict:
    result = asyncio.run(
        _declare_execution_envelope(
            state,
            route_step_id=route_step_id,
            assurance_class="operation",
            target_profile_ref=_profile(),
            storage_binding=_shared_run_storage(),
        )
    )
    assert result["status"] == "success", result
    return result


def _route(envelope_ref: str) -> dict:
    return {
        "schema_version": 2,
        "goal": "运行固定执行合同的受管作业",
        "evidence_refs": ["test:execution-envelope"],
        "steps": [
            {
                "id": "build",
                "goal": "构建已锁定的软件",
                "after": [],
                "action": {
                    "tool": "submit_job",
                    "program": "make",
                    "evidence_refs": [envelope_ref],
                },
                "effects": ["workspace_write", "process_tree", "external_job"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }
        ],
    }


def test_operation_envelope_is_content_addressed_frozen_and_idempotent(tmp_path):
    state = _state(tmp_path)

    first = _operation_envelope(state)
    second = _operation_envelope(state)

    assert second["already_declared"] is True
    assert second["artifact_id"] == first["artifact_id"]
    assert first["artifact_id"].startswith(f"{EXECUTION_ENVELOPE_TYPE}__sha256_")
    parsed = parse_execution_envelope_ref(first["evidence_ref"])
    assert parsed == {
        "artifact_id": first["artifact_id"],
        "version": 1,
        "content_hash": first["content_hash"],
    }
    resolved = resolve_execution_envelope_ref(state, first["evidence_ref"])
    assert resolved["status"] == "ready", resolved
    assert resolved["envelope"]["assurance_class"] == "operation"
    assert resolved["envelope"]["storage_binding"]["kind"] == "shared_filesystem"
    assert not {"status", "current", "attempts"}.intersection(resolved["envelope"])


def test_generic_save_cannot_forge_execution_envelope(tmp_path):
    from shared.tools.builtin import _save_artifact

    state = _state(tmp_path)
    result = asyncio.run(
        _save_artifact(
            state,
            artifact_type=EXECUTION_ENVELOPE_TYPE,
            name="forged",
            content=json.dumps({"schema_version": 1}),
        )
    )

    assert result["status"] == "error"
    assert result["failed_checks"] == ["execution_envelope_owner"]
    assert state.list_artifacts(EXECUTION_ENVELOPE_TYPE) == []


def test_evidence_bearing_envelope_binds_frozen_prereg_and_lock(tmp_path):
    state = _state(tmp_path)
    prereg = state.save_artifact(
        "pre_registration",
        "scientific-contract",
        "# prereg",
        metadata={
            "run_role": "primary",
            "analysis_eligible": True,
            "execution_mode": "scientific",
            "expected_params": {"case": "locked"},
        },
    )["id"]
    state.mark_frozen(prereg)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg,
        "experiment_focus": "Run the frozen scientific contract with its native method.",
    }
    classified = asyncio.run(
        _classify_experiment_scope(
            state,
            scope="scientific",
            reason="测试 evidence-bearing execution envelope 的冻结科学合同绑定。",
        )
    )
    assert classified["status"] == "success", classified
    lock_id = _frozen_lock(state)

    result = asyncio.run(
        _declare_execution_envelope(
            state,
            route_step_id="solve",
            assurance_class="evidence_bearing",
            target_profile_ref=_profile(),
            storage_binding=_shared_run_storage(),
            environment_lock_artifact_ids=[lock_id],
        )
    )

    assert result["status"] == "success", result
    envelope = result["envelope"]
    assert envelope["scientific_contract_ref"]["artifact_id"] == prereg
    assert envelope["scientific_contract_ref"]["version"] == 1
    assert envelope["environment_lock"]["entries"][0]["artifact_id"] == lock_id


def test_evidence_bearing_envelope_requires_lock_and_prereg(tmp_path):
    state = _state(tmp_path)

    missing_prereg = asyncio.run(
        _declare_execution_envelope(
            state,
            route_step_id="solve",
            assurance_class="evidence_bearing",
            target_profile_ref=_profile(),
            storage_binding=_shared_run_storage(),
            environment_lock_artifact_ids=[],
        )
    )

    assert missing_prereg["status"] == "error"
    assert missing_prereg["error_code"] == "execution_envelope_scientific_contract_unavailable"


def test_route_rejects_malformed_or_wrong_step_envelope_reference(tmp_path):
    state = _state(tmp_path)
    envelope = _operation_envelope(state, route_step_id="other")
    wrong_step = asyncio.run(_declare_execution_route(state, _route(envelope["evidence_ref"])))
    assert wrong_step["error_code"] == "execution_envelope_invalid"
    assert "不能跨 step 复用" in wrong_step["errors"][0]

    malformed = _route("artifact:execution_envelope__not-exact")
    rejected = asyncio.run(_declare_execution_route(state, malformed))
    assert rejected["error_code"] == "execution_envelope_invalid"
    assert "精确冻结引用" in rejected["errors"][0]


def test_route_projects_and_reresolves_exact_envelope_without_new_lifecycle(tmp_path):
    state = _state(tmp_path)
    envelope = _operation_envelope(state)
    declared = asyncio.run(_declare_execution_route(state, _route(envelope["evidence_ref"])))
    assert declared["status"] == "success", declared

    decision = resolve_execution_context(
        state,
        {
            "tool": "submit_job",
            "program": "make",
            "route_step_id": "build",
            "observed_effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_roles": ["run_root"],
            "read_only": False,
            "dry_run": False,
        },
    )

    assert decision["execution_envelope_ref"] == envelope["evidence_ref"]
    assert decision["execution_envelope_status"] == "ready"
    assert decision["execution_envelope"]["artifact_id"] == envelope["artifact_id"]
    assert execution_envelope_refs_for_action(
        _route(envelope["evidence_ref"])["steps"][0]["action"]
    ) == [envelope["evidence_ref"]]


def test_validator_refuses_unimplemented_storage_and_lifecycle_fields():
    report = validate_execution_envelope_v1(
        {
            "schema_version": 1,
            "route_step_id": "build",
            "assurance_class": "operation",
            "target_profile_ref": _profile(),
            "storage_binding": {"kind": "object_storage", "roles": {"run": "run_root"}},
            "environment_lock": {"kind": "frozen_artifact_refs", "entries": []},
            "scientific_contract_ref": None,
            "resource_plan_ref": None,
            "status": "running",
        }
    )

    assert report["valid"] is False
    assert any("当前只实现 shared_filesystem" in error for error in report["errors"])
    assert any("不能保存生命周期字段" in error for error in report["errors"])


# ---- 真实 E2E 回归：拒绝 storage_binding.roles 时必须报出合法形式 ----
#
# 2026-09-02 e2e_realistic_scientific：节点把 path_role("run_root") 当成语义角色
# 写在键上，只收到「含不支持的语义角色」而拿不到合法集合，连续 20+ 轮猜不出来、
# 最终放弃 envelope。同一次调用里 content_digest 的报错说了 "sha256:<64hex>"，
# 节点 1 轮就改对了 —— 差别只在报错说不说合法形式。


def _roles_errors(roles) -> list[str]:
    report = validate_execution_envelope_v1(
        {
            "schema_version": 1,
            "route_step_id": "build",
            "assurance_class": "operation",
            "target_profile_ref": _profile(),
            "storage_binding": {"kind": "shared_filesystem", "roles": roles},
            "environment_lock": {"kind": "frozen_artifact_refs", "entries": []},
            "scientific_contract_ref": None,
            "resource_plan_ref": None,
        }
    )
    assert report["valid"] is False, report
    return [str(error) for error in report["errors"]]


def test_path_role_written_as_semantic_key_gets_the_swap_diagnosed():
    errors = _roles_errors({"run_root": "run_root"})
    joined = "\n".join(errors)

    # 合法键必须逐个报出来，不能只说「不支持」
    for legal_key in ("input", "build", "run", "output", "logs"):
        assert legal_key in joined, joined
    # 并且要指出键值写反了，给出可直接照抄的写法
    assert "path_role" in joined and "键" in joined, joined
    assert '{"run": "run_root"}' in joined, joined


def test_unknown_semantic_key_still_lists_the_legal_keys():
    errors = _roles_errors({"read": "run_root"})
    joined = "\n".join(errors)

    for legal_key in ("input", "build", "run", "output", "logs"):
        assert legal_key in joined, joined
    # "read" 不是 path_role，不该误报成键值写反
    assert "属于值而不是键" not in joined, joined


def test_bad_path_role_value_lists_the_canonical_roles():
    errors = _roles_errors({"run": "runs"})
    joined = "\n".join(errors)

    for canonical in ("run_root", "build_root", "source_baseline_root"):
        assert canonical in joined, joined
    assert "'runs'" in joined, joined


def test_declared_schema_teaches_the_roles_shape_without_a_failed_call():
    from core.tool_registry import _REGISTRY

    schema = _REGISTRY.tools["declare_execution_envelope"].parameters_schema
    roles = schema["properties"]["storage_binding"]["properties"]["roles"]

    # 不透明的 {"type": "object"} 会让节点只能靠试错学 schema
    assert sorted(roles["propertyNames"]["enum"]) == [
        "build", "input", "logs", "output", "run",
    ]
    assert "run_root" in roles["additionalProperties"]["enum"]
    assert "path_role" in roles["description"]
    digest = schema["properties"]["target_profile_ref"]["properties"]["content_digest"]
    assert "sha256" in digest.get("description", "")


# ---- E-10 阶段A：scientific_execution 的 envelope 强制门（warn 默认 + 三档开关） ----


_GATE_ENV = "EXPERIMENT_ENVELOPE_GATE"


def _events(state: State) -> list[dict]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _scientific_route(evidence_refs: list[str] | None = None) -> dict:
    action: dict = {"tool": "submit_job", "program": "python3"}
    if evidence_refs:
        action["evidence_refs"] = list(evidence_refs)
    return {
        "schema_version": 2,
        "goal": "执行正式科学模拟并验证 envelope 门",
        "evidence_refs": ["test:envelope-gate"],
        "steps": [
            {
                "id": "simulate",
                "goal": "运行 prereg 声明的模拟",
                "after": [],
                "action": action,
                "effects": [
                    "workspace_write", "process_tree",
                    "external_job", "scientific_execution",
                ],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }
        ],
    }


def _assign_no_prereg(state: State) -> None:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the scientific execution-envelope gate.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This envelope-gate test has no governing preregistration.",
        },
    }
    classified = asyncio.run(
        _classify_experiment_scope(
            state,
            scope="scientific",
            reason="Exercise the scientific execution-envelope gate.",
        )
    )
    assert classified["status"] == "success", classified


def _spawn_decision(**overrides) -> dict:
    decision = {
        "decision": "matched_ready_step",
        "authoritative": True,
        "tool": "submit_job",
        "policy": "formal_scientific_execution",
        "route_step_id": "simulate",
        "declared_workdir_role": "run_root",
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "effective_effects": [
            "external_job", "process_tree",
            "scientific_execution", "workspace_write",
        ],
    }
    decision.update(overrides)
    return decision


def test_missing_envelope_steps_pure_function_three_states():
    scientific = _scientific_route()
    assert missing_execution_envelope_steps(scientific) == ["simulate"]

    with_ref = _scientific_route([
        "artifact:execution_envelope__sha256_" + "a" * 40 + "@v1#sha256=" + "a" * 64
    ])
    assert missing_execution_envelope_steps(with_ref) == []

    operation = _route("artifact:execution_envelope__x@v1#sha256=" + "b" * 64)
    operation["steps"][0]["action"].pop("evidence_refs")
    assert missing_execution_envelope_steps(operation) == []
    assert missing_execution_envelope_steps("not a route") == []


def test_gate_mode_parsing_defaults_warn_and_fails_closed(monkeypatch):
    monkeypatch.delenv(_GATE_ENV, raising=False)
    assert execution_envelope_gate_mode() == "warn"
    for mode in ("off", "warn", "enforce"):
        monkeypatch.setenv(_GATE_ENV, mode)
        assert execution_envelope_gate_mode() == mode
    monkeypatch.setenv(_GATE_ENV, "bogus-value")
    assert execution_envelope_gate_mode() == "enforce"
    monkeypatch.setenv(_GATE_ENV, "  Enforce ")
    assert execution_envelope_gate_mode() == "enforce"


def test_declare_route_warn_mode_succeeds_with_warnings_and_fact(tmp_path, monkeypatch):
    monkeypatch.delenv(_GATE_ENV, raising=False)
    state = _state(tmp_path)
    _assign_no_prereg(state)

    declared = asyncio.run(_declare_execution_route(state, _scientific_route()))

    assert declared["status"] == "success", declared
    # 收敛任务书 K3：warn 档不再要模型手动补 envelope，只留结构化事实。
    assert not any(
        "execution envelope" in warning or "declare_execution_envelope" in warning
        for warning in declared["warnings"]
    ), declared["warnings"]
    events = _events(state)
    gate_facts = [e for e in events if e.get("event") == "execution_envelope_gate_mode"]
    assert gate_facts and gate_facts[0]["gate_mode"] == "warn"
    assert gate_facts[0]["missing_envelope_step_ids"] == ["simulate"]
    missing_facts = [e for e in events if e.get("event") == "execution_envelope_missing"]
    assert missing_facts and missing_facts[0]["step_ids"] == ["simulate"]


def test_declare_route_enforce_mode_rejects_with_correction_sequence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    state = _state(tmp_path)
    _assign_no_prereg(state)

    rejected = asyncio.run(_declare_execution_route(state, _scientific_route()))

    assert rejected["status"] == "error"
    assert rejected["error_code"] == "execution_envelope_required"
    assert rejected["step_ids"] == ["simulate"]
    for fragment in (
        "declare_execution_envelope",
        "evidence_bearing",
        "evidence_refs",
        "amendment_reason",
    ):
        assert fragment in rejected["error"], fragment
    events = _events(state)
    assert any(
        e.get("event") == "declared_route_rejected"
        and e.get("reason") == "execution_envelope_required"
        for e in events
    )


def test_off_mode_does_not_block_but_records_gate_mode_fact(tmp_path, monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "off")
    state = _state(tmp_path)
    _assign_no_prereg(state)

    declared = asyncio.run(_declare_execution_route(state, _scientific_route()))

    assert declared["status"] == "success", declared
    assert not any("execution envelope" in warning for warning in declared["warnings"])
    gate_facts = [
        e for e in _events(state)
        if e.get("event") == "execution_envelope_gate_mode"
    ]
    assert gate_facts and gate_facts[0]["gate_mode"] == "off"
    assert gate_facts[0]["missing_envelope_step_ids"] == ["simulate"]


def test_spawn_second_line_blocks_missing_envelope_only_under_enforce(monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    block = execution_route_block(_spawn_decision(), phase="pre_spawn")

    assert block is not None
    assert block["reason"] == "execution_envelope_required"
    assert block["blocker"]["node_action"] == (
        "declare_execution_envelope_then_amend_route")
    assert block["blocker"]["retryable_after_change"] is True
    assert block["blocker"]["suggested_owner"] == "experiment"

    monkeypatch.setenv(_GATE_ENV, "warn")
    assert execution_route_block(_spawn_decision(), phase="pre_spawn") is None
    monkeypatch.setenv(_GATE_ENV, "off")
    assert execution_route_block(_spawn_decision(), phase="pre_spawn") is None


def test_spawn_second_line_exempts_dry_run_probe(monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    assert execution_route_block(
        _spawn_decision(dry_run=True), phase="pre_spawn") is None
    assert execution_route_block(
        {"decision": "route_not_required", "policy": "read_only", "read_only": True},
        phase="pre_spawn",
    ) is None


def test_spawn_second_line_ignores_operation_and_bound_envelope(monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    operation = _spawn_decision(
        policy="managed_external_job",
        effective_effects=["external_job", "process_tree", "workspace_write"],
    )
    assert execution_route_block(operation, phase="pre_spawn") is None

    bound = _spawn_decision(
        execution_envelope_ref=(
            "artifact:execution_envelope__sha256_" + "a" * 40
            + "@v1#sha256=" + "a" * 64
        ),
    )
    assert execution_route_block(bound, phase="pre_spawn") is None


def test_route_with_envelope_ref_declares_cleanly_under_enforce(tmp_path, monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    state = _state(tmp_path)
    prereg = state.save_artifact(
        "pre_registration",
        "scientific-contract",
        "# prereg",
        metadata={
            "run_role": "primary",
            "analysis_eligible": True,
            "execution_mode": "scientific",
            "expected_params": {"case": "locked"},
        },
    )
    state.mark_frozen(prereg["id"])
    prereg_record = state.read_artifact(prereg["id"])
    assert isinstance(prereg_record, dict)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "验证 enforce 档下带 envelope ref 的路线声明保持零变化。",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": prereg["id"],
            "version": int(prereg_record["version"]),
            "content_hash": prereg_record["content_hash"],
        },
    }
    classified = asyncio.run(
        _classify_experiment_scope(
            state,
            scope="scientific",
            reason="验证 enforce 档下带 envelope ref 的路线声明保持零变化。",
        )
    )
    assert classified["status"] == "success", classified
    lock_id = _frozen_lock(state)
    envelope = asyncio.run(
        _declare_execution_envelope(
            state,
            route_step_id="simulate",
            assurance_class="evidence_bearing",
            target_profile_ref=_profile(),
            storage_binding=_shared_run_storage(),
            environment_lock_artifact_ids=[lock_id],
        )
    )
    assert envelope["status"] == "success", envelope

    declared = asyncio.run(
        _declare_execution_route(
            state, _scientific_route([envelope["evidence_ref"]])
        )
    )

    assert declared["status"] == "success", declared
    assert not any("execution envelope" in warning for warning in declared["warnings"])
    gate_facts = [
        e for e in _events(state)
        if e.get("event") == "execution_envelope_gate_mode"
    ]
    assert gate_facts and gate_facts[-1]["gate_mode"] == "enforce"
    assert gate_facts[-1]["missing_envelope_step_ids"] == []


def test_operation_route_never_triggers_gate_under_enforce(tmp_path, monkeypatch):
    monkeypatch.setenv(_GATE_ENV, "enforce")
    state = _state(tmp_path)
    route = _route("artifact:execution_envelope__x@v1#sha256=" + "b" * 64)
    route["steps"][0]["action"].pop("evidence_refs")

    declared = asyncio.run(_declare_execution_route(state, route))

    assert declared["status"] == "success", declared
    assert not any("execution envelope" in warning for warning in declared["warnings"])

def test_envelope_declared_before_scope_must_be_redeclared_with_current_receipt(tmp_path):
    state = _state(tmp_path)
    unbound = _operation_envelope(state)

    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the requested native solver through a managed operation.",
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Build the requested native solver without claiming scientific completion.",
    ))["status"] == "success"

    stale = resolve_execution_envelope_ref(state, unbound["evidence_ref"])
    assert stale["status"] == "invalid", stale
    assert stale["reason"] == "execution_envelope_intent_receipt_missing"

    rebound = _operation_envelope(state)
    assert rebound["artifact_id"] != unbound["artifact_id"]
    ready = resolve_execution_envelope_ref(state, rebound["evidence_ref"])
    assert ready["status"] == "ready", ready
    assert ready["envelope"]["execution_intent_binding"]["intent_digest"]
