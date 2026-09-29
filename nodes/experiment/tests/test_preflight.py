"""Tests for the Experiment-local start gate."""
from __future__ import annotations

import asyncio
from core.state import State
from nodes.experiment.tools.preflight import audit_execution_contract, audit_experiment_preflight
from nodes.experiment.tools.contract_audit import audit_input_delivery_for_execution
from nodes.experiment.tools.resource_manager import _submit_job
from nodes.experiment.tools.path_roles import experiment_output_dir
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope, PREREG_DEVIATIONS_KEY,
    audit_prereg_assignment,
)
from nodes.experiment.tools import execution_route


def _fixture_node_inputs() -> dict[str, str]:
    return {
        "fixture": "preflight",
        "requested_work": "验证 Experiment 正式执行的预注册、输入与提交前置契约。",
    }


def _frozen_prereg(state, name: str, content: str = "## prereg\n", **metadata) -> str:
    """Save a pre_registration and freeze it on the ledger.

    A freeze is a ledger row, not a metadata flag: readers such as
    ``latest_frozen_artifact`` only see the row.
    """
    saved = state.save_artifact("pre_registration", name, content, metadata=metadata)
    state.mark_frozen(saved["id"], {"freeze_reason": "preflight fixture"})
    return saved["id"]


def _bind_prereg(state, artifact_id: str) -> None:
    """Give a fixture the exact caller authority that production now requires."""
    record = state.read_artifact(artifact_id)
    assert isinstance(record, dict), artifact_id
    state.hook_state.setdefault("node_inputs", _fixture_node_inputs())[
        "prereg_assignment"
    ] = {
        "kind": "bound",
        "artifact_id": artifact_id,
        "version": int(record["version"]),
        "content_hash": record["content_hash"],
    }


def _state(tmp_path, *, role="primary", eligible=True):
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = _fixture_node_inputs()
    prereg_id = _frozen_prereg(
        state,
        "contract",
        run_role=role,
        analysis_eligible=eligible,
        expected_params={"grid": [50, 50]},
    )
    _bind_prereg(state, prereg_id)
    return state


def _declare_formal_route(state, *, tool: str, program: str) -> str:
    """Declare the execution fact that makes this call a scientific action."""
    from nodes.experiment.tools import execution_route

    state.hook_state.setdefault("node_inputs", _fixture_node_inputs())
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="本测试执行受冻结预注册约束的正式科学动作并验证执行契约。",
    ))
    assert classified["status"] == "success", classified
    effects = ["workspace_write", "scientific_execution"]
    if tool == "submit_job":
        effects.extend(["process_tree", "external_job"])
    result = asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "验证正式科学执行的输入与参数门",
        "evidence_refs": ["test:preflight-formal-route"],
        "steps": [{
            "id": "formal_run",
            "goal": "执行正式科学计算",
            "after": [],
            "action": {"tool": tool, "program": program},
            "effects": effects,
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }))
    assert result["status"] == "success", result
    return "formal_run"


def test_preflight_passes_for_a_valid_local_contract(tmp_path):
    result = audit_experiment_preflight(_state(tmp_path))

    assert result["passed"] is True
    assert result["blocking_reasons"] == []
    assert {item["name"] for item in result["checks"]} == {
        "pre_registration", "declared_pre_registration", "run_contract", "stage", "path_roles", "frozen_expected_params",
    }


def test_preflight_fails_closed_for_missing_or_unfrozen_input(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state["run_contract"] = {"run_role": "primary", "stage": "simulation"}

    result = audit_experiment_preflight(state)

    assert result["passed"] is False
    assert "pre_registration" in result["blocking_reasons"]


def test_preflight_derives_secondary_run_owes_no_verdict(tmp_path):
    result = audit_experiment_preflight(
        _state(tmp_path, role="secondary", eligible=True),
    )

    assert result["passed"] is True
    run_contract = next(item for item in result["checks"] if item["name"] == "run_contract")
    assert run_contract["requires_hypothesis_verdict"] is False


def test_unassigned_catalog_preregs_stay_pending_and_invalid_selector_blocks(tmp_path):
    state = State.new("experiment", tmp_path)
    first = _frozen_prereg(state, "first", "# prereg", run_role="primary", analysis_eligible=True, expected_params={"n": 1})
    second = _frozen_prereg(state, "second", "# prereg", run_role="primary", analysis_eligible=True, expected_params={"n": 1})
    state.hook_state["node_inputs"] = _fixture_node_inputs()
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Observe catalog candidates without claiming either one.",
    ))

    assert classified["status"] == "success", classified
    assignment = audit_prereg_assignment(state)
    assert assignment["status"] == "pending"
    assert assignment["passed"] is False
    assert assignment["retryable_in_current_run"] is False
    assert {item["artifact_id"] for item in assignment["candidate_bindings"]} == {
        first,
        second,
    }
    assert state.hook_state.get("blockers", []) == []

    invalid_state = State.new("experiment", tmp_path / "invalid")
    invalid_state.hook_state["node_inputs"] = {
        "prereg_artifact_id": "pre_registration__missing",
    }
    invalid = audit_execution_contract(
        invalid_state, {"n": 1}, stage="simulation",
    )
    assert invalid["passed"] is False and invalid["blocking_reasons"] == ["declared_prereg_not_found"]
    assert len(invalid_state.hook_state["blockers"]) == 1


def test_primary_simulation_contract_requires_complete_frozen_match(tmp_path):
    state = State.new("experiment", tmp_path)
    expected = {"reynolds": [100, 400], "grid": [50, 50], "metrics": ["psi_max"]}
    prereg_id = _frozen_prereg(
        state,
        "contract",
        run_role="primary",
        analysis_eligible=True,
        expected_params=expected,
    )
    _bind_prereg(state, prereg_id)

    missing = audit_execution_contract(state, None, stage="simulation", runner="safe_run_bash")
    mismatch = audit_execution_contract(
        state, {"reynolds": [100, 3200], "grid": [50, 50], "metrics": ["psi_max"]},
        stage="simulation", runner="safe_run_bash")
    matched = audit_execution_contract(state, expected, stage="simulation", runner="safe_run_bash")
    build = audit_execution_contract(state, None, stage="toolchain_build")

    assert missing["passed"] is False
    assert missing["blocking_reasons"] == ["execution_params_missing"]
    assert mismatch["passed"] is False
    assert mismatch["mismatched_parameters"][0]["parameter"] == "reynolds"
    assert matched["passed"] is True
    assert build["passed"] is True and build["applicable"] is False
    invalid_stage = audit_execution_contract(state, expected, stage="ad_hoc")
    assert invalid_stage["passed"] is False
    assert invalid_stage["blocking_reasons"] == ["stage_invalid"]


def test_primary_simulation_rejects_unregistered_execution_parameter(tmp_path):
    state = State.new("experiment", tmp_path)
    prereg_id = _frozen_prereg(
        state,
        "contract",
        run_role="primary",
        analysis_eligible=True,
        expected_params={"grid": [50, 50]},
    )
    _bind_prereg(state, prereg_id)
    result = audit_execution_contract(
        state, {"grid": [50, 50], "temperature_k": 300}, stage="simulation", runner="safe_run_bash")
    assert result["passed"] is False
    assert result["unexpected_execution_parameters"] == ["temperature_k"]


def test_primary_preflight_requires_structured_execution_contract(tmp_path):
    state = State.new("experiment", tmp_path)
    prereg_id = _frozen_prereg(
        state, "contract", run_role="primary", analysis_eligible=True,
    )
    _bind_prereg(state, prereg_id)
    result = audit_experiment_preflight(state)
    assert result["passed"] is False
    assert "frozen_expected_params" in result["blocking_reasons"]


def test_primary_real_submission_declares_deviation_and_reaches_scheduler(tmp_path, monkeypatch):
    """判决拆除 O1（rm:1951 降格，2026-08-31，专审一）。

    与冻结 prereg 不一致不再禁止提交：偏离申报进 hook_state/transcript（三张表），
    提交照常到达 scheduler，且提交记录携带 submission_witness。
    """
    from nodes.experiment.tools import resource_manager as rm
    from nodes.experiment.tools.run_contract import PREREG_DEVIATIONS_KEY
    from shared.lib import dangerous_commands as danger
    from core import sandbox as core_sandbox

    state = State.new("experiment", tmp_path)
    prereg_id = _frozen_prereg(
        state,
        "contract",
        run_role="primary",
        analysis_eligible=True,
        expected_params={"reynolds": [100, 400]},
    )
    _bind_prereg(state, prereg_id)
    route_step_id = _declare_formal_route(
        state, tool="submit_job", program="echo")
    called = False
    def stub_submit(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "success", "scheduler": "local", "job_name": "stub",
                "job_id": "hf-stub", "submission_nonce": "preflight-stub",
                "container_runtime_id": "a" * 64}
    monkeypatch.setattr(rm, "_submit_sync", stub_submit)
    # 沙箱身份在本机测试环境不可用；被测对象是契约门而不是沙箱。
    monkeypatch.setattr(core_sandbox, "trusted_image_id", lambda: "sha256:test")
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(rm._submit_job(
            state=state, command="echo solver", scheduler="local", dry_run=False,
            execution_params={"reynolds": [100, 3200]},
            route_step_id=route_step_id,
        ))
    finally:
        danger.set_bypass_mode(False)

    assert called is True
    assert result["status"] == "success", result
    deviations = state.hook_state.get(PREREG_DEVIATIONS_KEY) or []
    assert deviations, "prereg deviation must be declared"
    assert deviations[0]["kind"] == "execution_params_deviate_from_frozen_prereg"
    assert deviations[0]["mismatched_parameters"], deviations[0]
    witness = result.get("submission_witness") or {}
    assert witness.get("prereg_deviation"), result


def test_stage_simulation_alone_does_not_make_bash_a_formal_scientific_carrier(tmp_path):
    """O1（sb:3521 降格）在 bash 路径上不可达 —— 覆盖在 submit_job 路径上。

    origin/main 的同名测试用 `_safe_run_bash(stage="simulation")` 验「参数偏离改为
    申报」。在本分支这条路走不通，而且不是因为墙还在：`_simulation_contract_block`
    只在 formal_scientific_action 时运行，该判定由**路线**推导，而路线 schema 规定
    scientific_execution 只能由 submit_job 承担（safe_run_bash 不是合法载体）。
    也就是说 bash 永远不是正式科学载体，那条降格自然落在 submit_job 上 ——
    见 test_primary_real_submission_declares_deviation_and_reaches_scheduler。

    这里改钉一条本分支真正成立、且比原测试更值钱的性质：**caller 声明的 stage
    不具权威**。光写 stage="simulation" 不会把一次 bash 调用变成正式科学执行，
    也就不会凭这一句话触发正式科学的契约审计。
    """
    from nodes.experiment.tools import execution_route as er

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        **_fixture_node_inputs(),
        "prereg_assignment": {
            "kind": "none",
            "reason": "This routing fixture consumes no preregistration.",
        },
    }
    asyncio.run(_classify_experiment_scope(
        state, scope="scientific", reason="验证 caller 声明的 stage 不具权威。"))

    decision = er.resolve_execution_context(state, {
        "tool": "safe_run_bash", "program": "touch", "command": "touch out.txt",
        "legacy_policy": {"stage": "simulation", "formal_simulation": True},
    })
    assert decision["policy"] != "formal_scientific_execution", decision


def test_primary_custom_solver_cannot_hide_behind_diagnostic_stage(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route, safe_bash

    state = _state(tmp_path)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="该自定义求解器使用冻结科学参数运行，必须按 scientific 分类。",
    ))
    assert classified["status"] == "success"
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "运行未被固定正则认识的科学求解器",
        "evidence_refs": ["test:custom-solver"],
        "steps": [{
            "id": "solve",
            "goal": "运行求解器",
            "after": [],
        "action": {"tool": "submit_job", "program": "./my_solver"},
        "effects": [
            "workspace_write", "process_tree", "external_job",
            "scientific_execution",
        ],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }))
    spawned = False

    async def forbidden(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        "./my_solver",
        stage="diagnostic",
        route_step_id="solve",
        execution_params={"grid": [999, 999]},
    ))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_external_job_owner_mismatch"
    assert spawned is False


def test_primary_python_scientific_write_requires_contract_before_interpreter(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import safe_bash

    state = _state(tmp_path)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="该 Python 写入使用冻结科学参数，必须先按 scientific 分类。",
    ))
    assert classified["status"] == "success"
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    spawned = False

    async def forbidden(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "from pathlib import Path\nPath('scientific.out').write_text('bad')",
        cwd=str(run_root),
        stage="diagnostic",
        execution_params={"grid": [999, 999]},
    ))

    # 判决拆除 O1（sb:3521 降格）：参数偏离改为申报，不再拦解释器。
    # 但 python 侧的 route/scope 门另有独立防线，这里验的是「偏离被记下来了」。
    assert "execution_contract_blocked" not in result
    deviations = state.hook_state.get(PREREG_DEVIATIONS_KEY) or []
    assert deviations and deviations[-1]["kind"] == \
        "execution_params_deviate_from_frozen_prereg"


def test_direct_real_submission_requires_route_before_approval(tmp_path):
    state = State.new("experiment", tmp_path)
    runtime_root = experiment_output_dir(state, "runtime", create=False)

    unclassified = asyncio.run(_submit_job(
        state=state, command="echo should-not-submit", scheduler="local",
        dry_run=False, stage="diagnostic",
    ))
    assert unclassified["status"] == "error"
    assert unclassified["reason"] == "experiment_scope_classification_required"
    assert runtime_root.exists() is False
    assert state.list_artifacts("job_submission") == []

    state.hook_state["node_inputs"] = {
        **_fixture_node_inputs(),
        "prereg_assignment": {
            "kind": "none",
            "reason": "This scheduler-probe fixture consumes no preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="scheduler_probe",
        reason="本测试只验证调度提交前的路线门，不执行正式科学计算。",
    ))
    assert classified["status"] == "success"
    blocked = asyncio.run(_submit_job(
        state=state, command="echo should-not-submit", scheduler="local",
        dry_run=False, stage="diagnostic",
    ))
    dry_run = asyncio.run(_submit_job(
        state=state, command="echo dry-run", scheduler="local",
        dry_run=True,
    ))

    assert blocked["status"] == "error"
    assert blocked["reason"] == "execution_route_required"
    assert dry_run["status"] == "success"


def test_routed_real_local_submission_reaches_physical_submit_without_resource_artifact(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    submitted: list[str] = []

    def fake_submit(*_args, **_kwargs):
        submitted.append("physical-submit")
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job", "job_id": "preflight-submit",
            "submission_nonce": "preflight-submit-nonce",
        }

    monkeypatch.setattr(manager, "_submit_sync", fake_submit)
    monkeypatch.setattr(
        "core.sandbox.trusted_image_id", lambda: "sha256:test-sandbox",
    )
    state = _state(tmp_path)
    route_step_id = _declare_formal_route(
        state, tool="submit_job", program="echo",
    )

    result = asyncio.run(_submit_job(
        state=state,
        command="echo submit",
        scheduler="local",
        dry_run=False,
        stage="simulation",
        execution_params={"grid": [50, 50]},
        route_step_id=route_step_id,
    ))

    assert state.list_artifacts("build_resource_plan") == []
    assert result["status"] == "success", result
    assert submitted == ["physical-submit"]
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "job_submission_confirmation_not_required" in transcript


def test_hpc_phase_does_not_require_a_resource_recommendation_artifact(tmp_path):
    state = _state(tmp_path)

    result = audit_experiment_preflight(state, phase="hpc_submit")

    assert result["passed"] is True
    assert "resource_recommendation" not in {item["name"] for item in result["checks"]}


def test_preflight_binds_to_declared_prereg_not_latest(tmp_path):
    state = State.new("experiment", tmp_path)
    first = _frozen_prereg(state, "first", "first", run_role="primary",
                           expected_params={"grid": [50, 50]})
    _frozen_prereg(state, "second", "second", run_role="secondary",
                   expected_params={"grid": [100, 100]})
    _bind_prereg(state, first)
    result = audit_experiment_preflight(state)
    declared = next(item for item in result["checks"] if item["name"] == "declared_pre_registration")
    assert result["passed"] is True
    assert declared["prereg_artifact_id"] == first


def test_verified_fallback_input_makes_secondary_simulation_enforce_frozen_parameters(tmp_path):
    state = _state(tmp_path, role="secondary", eligible=False)
    state.hook_state["input_delivery_state"] = {"spec": {"provider": "experiment_fallback", "verified": True, "input_package_artifact_id": "experiment_fallback_inputs__case"}}
    missing_input = audit_input_delivery_for_execution(state)
    blocked_input = audit_input_delivery_for_execution(state, "wrong")
    mismatch = audit_execution_contract(state, {"grid": [20, 20]}, stage="simulation")
    matched = audit_execution_contract(state, {"grid": [50, 50]}, stage="simulation")
    assert missing_input["blocking_reasons"] == ["input_package_artifact_missing"]
    assert blocked_input["passed"] is False
    assert mismatch["passed"] is False and "execution_params_mismatch" in mismatch["blocking_reasons"]
    assert matched["passed"] is True


def test_multiple_input_specs_require_an_explicit_verified_package_mapping(tmp_path):
    state = _state(tmp_path, role="secondary", eligible=False)
    state.hook_state["input_delivery_state"] = {
        "spec-a": {
            "provider": "data", "verified": True,
            "input_package_artifact_id": "dataset__a",
        },
        "spec-b": {
            "provider": "data", "verified": True,
            "input_package_artifact_id": "dataset__b",
        },
    }

    ambiguous = audit_input_delivery_for_execution(state, "dataset__a")
    matched = audit_input_delivery_for_execution(
        state,
        input_package_bindings={
            "spec-a": "dataset__a",
            "spec-b": "dataset__b",
        },
    )
    swapped = audit_input_delivery_for_execution(
        state,
        input_package_bindings={
            "spec-a": "dataset__b",
            "spec-b": "dataset__a",
        },
    )
    missing = audit_input_delivery_for_execution(
        state,
        input_package_bindings={"spec-a": "dataset__a"},
    )

    assert ambiguous["passed"] is False
    assert ambiguous["blocking_reasons"] == ["input_package_binding_ambiguous"]
    assert matched["passed"] is True
    assert matched["spec_ids"] == ["spec-a", "spec-b"]
    assert swapped["passed"] is False
    assert swapped["blocking_reasons"] == ["input_package_artifact_mismatch"]
    assert missing["passed"] is False
    assert missing["blocking_reasons"] == ["input_package_binding_incomplete"]


def test_single_input_spec_keeps_the_legacy_package_id_compatibility(tmp_path):
    state = _state(tmp_path, role="secondary", eligible=False)
    state.hook_state["input_delivery_state"] = {
        "spec": {
            "provider": "data", "verified": True,
            "input_package_artifact_id": "dataset__only",
        },
    }

    result = audit_input_delivery_for_execution(state, "dataset__only")

    assert result["passed"] is True
    assert result["spec_ids"] == ["spec"]


def test_unverified_formal_input_is_witnessed_not_blocked_before_submit(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tools import resource_manager as rm
    from shared.lib import dangerous_commands as danger

    state = _state(tmp_path, role="secondary", eligible=False)
    state.hook_state["input_delivery_state"] = {"spec": {"provider": None, "verified": False}}
    # 目标必须落在本 run 的可写根内：本分支的路径能力门先于输入交付审计开火，
    # 写到 tmp_path 根上会被 local_job_write_target_not_capable 拦掉，
    # 那样就验不到 O2 降格本身了。
    marker = experiment_output_dir(state, "runtime", create=True) / "must-not-run"
    route_step_id = _declare_formal_route(
        state, tool="submit_job", program="touch")
    submitted = False

    def recorded_submit(*_args, **_kwargs):
        # 判决拆除 O2 后「输入未验收」不再先于提交拦下 —— 提交照走，
        # 未验收这件事进见证。桩因此从「不许被调用」改成「记录被调用」。
        nonlocal submitted
        submitted = True
        return {"status": "success", "scheduler": "local", "dry_run": False,
                "job_name": "experiment_job", "job_id": "hf-unverified-input",
                "submission_nonce": "unverified-input-stub",
                "container_runtime_id": "a" * 64}

    monkeypatch.setattr(rm, "_submit_sync", recorded_submit)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(_submit_job(
            state=state,
            command=f"touch {marker}",
            scheduler="local",
            dry_run=False,
            route_step_id=route_step_id,
            execution_params={"grid": [50, 50]},
        ))
    finally:
        danger.set_bypass_mode(False)

    # 判决拆除 O2（rm:1946）：正式输入包未验收照跑 —— 不再拦提交，而是
    # input_delivery:unverified 进提交见证 + 记一条执行前提见证。
    # 2026-09-11 owner 裁决：见证只记账，**不再翻任何资格门** —— 被记过见证的
    # 运行仍然欠裁决（结论有瑕疵不等于没有结论）。
    assert "input_delivery_blocked" not in result, result
    witness = result.get("submission_witness") or {}
    assert witness.get("input_delivery_unverified"), result
    assert submitted is True, "记了见证也照样提交"
    from nodes.experiment.tools.run_contract import load_run_contract
    contract = load_run_contract(state)
    assert contract["execution_precondition_witnesses"], contract
    # 这个夹具是 secondary，本来就不欠裁决；见证不改变这件事。
    # 见证"不翻门"由下一条 primary 用例钉住。
    assert contract["requires_hypothesis_verdict"] is False, contract

def test_a_witnessed_precondition_does_not_strip_a_primary_run_of_its_verdict(
    tmp_path,
):
    """见证只记账：被记过执行前提见证的 primary 科学运行仍然欠裁决。

    原先这条见证会把 analysis_eligible 翻成 False（run_contract.py 旧 308-310），
    而框架判"欠不欠裁决"只读 requires_hypothesis_verdict，所以那个降格对框架义务
    从来没有作用；节点内部却因此少做参数等值审计、少出 repro bundle。
    2026-09-11 owner 裁决：结论有瑕疵不等于没有结论，见证不翻门。
    """
    from nodes.experiment.tools.run_contract import (
        load_run_contract,
        record_execution_precondition_witness,
    )

    state = _state(tmp_path, role="primary", eligible=True)
    before = load_run_contract(state)
    assert before["requires_hypothesis_verdict"] is True

    record_execution_precondition_witness(
        state, "test", "formal input package unverified")
    after = load_run_contract(state)

    assert after["execution_precondition_witnesses"] == [
        {"source": "test", "reason": "formal input package unverified"}]
    assert after["requires_hypothesis_verdict"] is True, after
    # 派生别名也跟着不动（它等根测试改掉后会一起删）。
    assert after["analysis_eligible"] is True, after
