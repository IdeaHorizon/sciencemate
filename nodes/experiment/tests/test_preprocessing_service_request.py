"""没有冻结 prereg 的 run 也必须有一条合法的"调 data 要前处理"通道。

之前 `validate_data_request_spec` 无条件要求 source_prereg_artifact_id，
operation / diagnostic / toolchain_build 这类没绑 prereg 的 run 根本构造不出能
过门的请求 —— "缺前处理产物必须调 data" 这条规则对它们等于没有出口，剩下的
唯一动作就是自己就地造。这组测试锁住新出口，同时锁住它**不能**变成绕开冻结
prereg 的后门、也**不能**把 secondary simulation 推进无解的参数比对。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    _validate_data_request_spec, _verify_dataset_consumption,
)
from nodes.experiment.tools.preflight import audit_execution_contract


def _service_spec(**overrides) -> str:
    spec = {
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": "构建后需要一份网格来验证求解器能读入并跑通一步",
        "target_software": "OpenFOAM 11 (simpleFoam)",
        "scientific_parameters": "not_applicable",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    }
    spec.update(overrides)
    return json.dumps(spec)


def _save_frozen(state, artifact_type, name, content, metadata=None):
    """夹具级冻结：save + mark_frozen。冻结的事实只出自账本的 freeze 行，save 行里的
    frozen 键会被剥掉（core.ledger.FREEZE_OWNED_METADATA）。返回 save_artifact 的结果。"""
    saved = state.save_artifact(artifact_type, name, content, metadata=metadata)
    state.mark_frozen(saved["id"])
    return saved


def _bind_prereg(state: State, saved: dict) -> None:
    """Bind the exact frozen version instead of inferring from the catalog."""
    record = state.read_artifact(saved["id"])
    assert isinstance(record, dict), saved
    state.hook_state.setdefault("node_inputs", {})["prereg_assignment"] = {
        "kind": "bound",
        "artifact_id": saved["id"],
        "version": int(record["version"]),
        "content_hash": record["content_hash"],
    }


def _no_prereg(reason: str) -> dict:
    return {"prereg_assignment": {"kind": "none", "reason": reason}}


def test_run_without_bound_prereg_can_request_preprocessing_from_data(tmp_path: Path):
    state = State.new("experiment", tmp_path)

    validated = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    assert validated["status"] == "success", validated
    assert validated["request_kind"] == "preprocessing_service_request"
    assert validated["scientific_authority"] is False
    delivery = state.hook_state["input_delivery_state"][validated["spec_id"]]
    assert delivery["scientific_authority"] is False


def test_preprocessing_service_request_requires_stage_purpose_and_software(tmp_path: Path):
    """没有冻结 prereg 可读时，这三项是 data 推导必需输入文件的全部依据。"""
    state = State.new("experiment", tmp_path)

    no_stage = asyncio.run(_validate_data_request_spec(
        state, _service_spec(requesting_stage="simulation")))
    no_purpose = asyncio.run(_validate_data_request_spec(state, _service_spec(purpose="  ")))
    no_software = asyncio.run(_validate_data_request_spec(state, _service_spec(target_software="")))

    assert no_stage["status"] == "error"
    assert any("requesting_stage" in e for e in no_stage["errors"])
    assert any("purpose" in e for e in no_purpose["errors"])
    assert any("target_software" in e for e in no_software["errors"])


def test_preprocessing_service_request_cannot_carry_scientific_parameters(tmp_path: Path):
    state = State.new("experiment", tmp_path)

    result = asyncio.run(_validate_data_request_spec(
        state, _service_spec(scientific_parameters="frozen_prereg_only")))

    assert result["status"] == "error"
    assert any("scientific parameters" in e for e in result["errors"])


def test_bound_frozen_prereg_forces_the_formal_input_kind(tmp_path: Path):
    """有冻结 prereg = 科学运行；无权威 kind 不能当成绕开 frozen_prereg_only 的后门。"""
    state = State.new("experiment", tmp_path)
    saved = _save_frozen(
        state, "pre_registration", "contract", "## prereg\n",
        metadata={"run_role": "primary", "analysis_eligible": True,
                  "expected_params": {"grid": [50, 50]}},
    )
    _bind_prereg(state, saved)

    result = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    assert result["status"] == "error"
    assert any("frozen pre_registration" in e for e in result["errors"])


def test_formal_input_kind_still_requires_prereg_provenance(tmp_path: Path):
    state = State.new("experiment", tmp_path)

    result = asyncio.run(_validate_data_request_spec(state, json.dumps({
        "request_kind": "formal_input_preparation",
        "required_assets": [{"name": "POSCAR", "format": "VASP", "purpose": "structure"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })))

    assert result["status"] == "error"
    assert any("source_prereg_artifact_id" in e for e in result["errors"])
    assert any("frozen_prereg_only" in e for e in result["errors"])


def test_unknown_request_kind_is_rejected(tmp_path: Path):
    state = State.new("experiment", tmp_path)

    result = asyncio.run(_validate_data_request_spec(state, _service_spec(request_kind="whatever")))

    assert result["status"] == "error"
    assert any("request_kind" in e for e in result["errors"])


def _delivered_package(tmp_path: Path, state: State, spec_id: str) -> str:
    package = tmp_path / "data-package"
    package.mkdir()
    (package / "channel.msh").write_text("mesh bytes", encoding="utf-8")
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps({"lineage": [{"op": "prepare_scientific_mesh"}]}), encoding="utf-8")
    dataset_id = state.save_artifact("dataset", "mesh_package", json.dumps({
        "package_dir": str(package), "manifest_path": str(manifest),
        "lineage": [{"op": "prepare_scientific_mesh"}],
        "downstream_contract": {"files": ["channel.msh"]},
    }))["id"]
    consumed = asyncio.run(_verify_dataset_consumption(state, dataset_id, spec_id))
    assert consumed["passed"] is True, consumed
    return dataset_id


def test_verified_service_delivery_does_not_drag_a_secondary_run_into_param_matching(tmp_path: Path):
    """补出口不能顺手造一个新死锁。

    `audit_execution_contract` 靠 input_delivery_state 里有没有 verified 条目决定
    要不要跟冻结 prereg 逐键比对参数。若无权威交付也算数，一个没有 expected_params
    的 secondary simulation 会被要求拿出它根本没有的参数，且无解。
    """
    state = State.new("experiment", tmp_path)
    _save_frozen(
        state, "pre_registration", "contract", "## prereg\n",
        metadata={"run_role": "secondary", "analysis_eligible": False},
    )
    # 请求在 prereg 绑定前发出（真实顺序：先探测环境，后来才绑定科学契约）。
    validated = asyncio.run(_validate_data_request_spec(
        State.new("experiment", tmp_path / "probe"), _service_spec()))
    state.hook_state["input_delivery_state"] = {
        validated["spec_id"]: {"provider": "data", "verified": True,
                               "scientific_authority": False,
                               "input_package_artifact_id": "dataset__x"},
    }

    result = audit_execution_contract(state, {"grid": [50, 50]}, stage="simulation")

    assert result["applicable"] is False
    assert result["blocking_reasons"] == []


def test_verified_formal_delivery_still_enforces_frozen_parameters(tmp_path: Path):
    """无权威交付放行，不能连带把有权威交付的比对也放掉。"""
    state = State.new("experiment", tmp_path)
    saved = _save_frozen(
        state, "pre_registration", "contract", "## prereg\n",
        metadata={"run_role": "secondary", "analysis_eligible": False,
                  "expected_params": {"grid": [50, 50]}},
    )
    _bind_prereg(state, saved)
    state.hook_state["input_delivery_state"] = {
        "spec": {"provider": "data", "verified": True, "scientific_authority": True,
                 "input_package_artifact_id": "dataset__x"},
    }

    mismatch = audit_execution_contract(state, {"grid": [20, 20]}, stage="simulation")
    matched = audit_execution_contract(state, {"grid": [50, 50]}, stage="simulation")

    assert mismatch["passed"] is False
    assert "execution_params_mismatch" in mismatch["blocking_reasons"]
    assert matched["passed"] is True


def test_service_request_delivery_is_verified_the_same_way_as_a_formal_one(tmp_path: Path):
    """无科学权威 ≠ 免验收：文件、manifest、downstream_contract 一样要对上。"""
    state = State.new("experiment", tmp_path)
    validated = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    _delivered_package(tmp_path, state, validated["spec_id"])

    delivery = state.hook_state["input_delivery_state"][validated["spec_id"]]
    assert delivery["verified"] is True
    assert delivery["provider"] == "data"
    assert delivery["scientific_authority"] is False


def _as_data_sees_it(dispatch_node_inputs: dict) -> dict:
    """把 dispatch payload 摆成 data 真正拿到的形状，再过它自己的入口归一化器。

    data 的 custom loop 用 `_build_service_task_context()` 组装：调用方的
    node_inputs 原样进 `node_inputs`，最后一条 user message 进 `user_request`。
    这里逐字复刻那个形状 —— 测试要问的是"**这条链**能不能把权威送到对面"，
    不是"归一化器单独喂它想要的字典时work 不 work"。
    """
    from nodes.data.planning.request_contract import normalize_preprocessing_request

    return normalize_preprocessing_request({
        "service_scope": "Produce and review only the requested preprocessing assets.",
        "node_inputs": dispatch_node_inputs,
        "user_request": dispatch_node_inputs.get("spec", ""),
        "inspected_inputs": [],
        "request_source": "caller_or_experiment_node",
    })


def test_dispatch_payload_carries_the_request_data_can_actually_classify(tmp_path: Path):
    """派给 data 的 payload 必须让对面认出这是**谁的**请求、要哪些资产。

    这是一条**跨节点契约测试**：它红了要么是本节点的 payload 退化了，要么是
    data 改了入口口径，两种都需要有人知道，不该静默漂移。

    口径变更史（2026-08-31，PR #715）：data 从"按 objective/stages/software/
    deliverables/conditions 五个信号判 research plan"改成"归一化成版本化
    PreprocessingRequest，按 authority 判 plan_bound / request_bound"。
    本节点因此不再需要渲染 Markdown 伪计划去迎合五个信号 —— 新入口**原生认得
    experiment 自己的请求词表**（`request_kind` / `source_prereg_artifact_id` /
    `required_assets` / `acceptance`），把结构化请求如实发过去即可。
    Markdown 那份仍然发，它是给人看的、也是对面 caller_request_text 的来源。
    """
    state = State.new("experiment", tmp_path)
    validated = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    request = _as_data_sees_it(validated["dispatch_node_inputs"])

    assert request["authority"]["kind"] == "experiment_request", request["authority"]
    assert request["review_profile"] == "request_bound"
    # 资产和验收要求必须**结构化地**到达，不是埋在散文里等对面正则去捞。
    assert [a["filename"] for a in request["requested_assets"]] == ["channel.msh"]
    assert set(request["acceptance_criteria"]) == {
        "file_exists", "schema", "units", "manifest_lineage"}


def test_formal_request_payload_names_the_frozen_prereg_as_upstream(tmp_path: Path):
    """正式输入请求要让 data 明确读到上游预注册，而不是靠 artifact id 的字符串巧合。

    这条抓过一次真事故（2026-08-31）：只发 Markdown 伪计划时，data 新口径把
    `formal_input_preparation` 判成 `authority.kind=user_request` /
    `review_profile=request_bound` —— **冻结预注册的科学权威在跨节点这一步
    悄悄丢掉**，两边都不报错。断言落在 authority 上，不落在"payload 里出现过
    那个 id"（字符串出现过不等于对面把它当权威）。
    """
    state = State.new("experiment", tmp_path)
    validated = asyncio.run(_validate_data_request_spec(state, json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": "pre_registration__abc12345",
        "scientific_parameters": "frozen_prereg_only",
        "target_software": "VASP 6.4.2",
        "required_assets": [{"name": "POSCAR", "format": "VASP 5", "purpose": "初始结构"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })))

    assert "pre_registration__abc12345" in validated["dispatch_node_inputs"]["spec"]

    request = _as_data_sees_it(validated["dispatch_node_inputs"])

    assert request["authority"]["kind"] == "pre_registration", request["authority"]
    assert request["review_profile"] == "plan_bound", (
        "冻结预注册的请求被降级成无权威的用户请求 —— 科学参数会失去 frozen 身份")
    assert [a["filename"] for a in request["requested_assets"]] == ["POSCAR"]


def test_dispatch_payload_preserves_exact_external_acquisition_reference(tmp_path: Path):
    """Experiment must not erase the exact source contract before Data sees it.

    Some formal inputs are public files rather than locally generated products.
    Their URL/repository, revision, filename, digest and licence are evidence,
    not optional prose. The request remains generic: assets without this
    optional block are still valid generated/local inputs.
    """
    state = State.new("experiment", tmp_path)
    reference = {
        "source_locator": "https://www.ncei.noaa.gov/data/ibtracs/v04r01/access/csv/ibtracs.WP.list.v04r01.csv",
        "expected_revision": "v04r01",
        "expected_filename": "ibtracs.WP.list.v04r01.csv",
        "expected_sha256": "a" * 64,
        "license": "CC BY 4.0",
    }
    validated = asyncio.run(_validate_data_request_spec(state, json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": "pre_registration__sst_ri",
        "scientific_parameters": "frozen_prereg_only",
        "target_software": "Python xarray analysis",
        "required_assets": [{
            "name": "ibtracs.WP.list.v04r01.csv",
            "format": "CSV",
            "purpose": "western North Pacific best-track observations",
            "acquisition_reference": reference,
        }],
        "acceptance": {
            "file_exists": True, "schema": True, "units": True,
            "manifest_lineage": True,
        },
    })))

    assert validated["status"] == "success", validated
    payload = validated["dispatch_node_inputs"]["spec"]
    assert "## External acquisition references / 外部获取依据" in payload
    assert reference["source_locator"] in payload
    assert reference["expected_revision"] in payload
    assert reference["expected_filename"] in payload
    assert reference["expected_sha256"] in payload
    assert reference["license"] in payload

    # 断言的落点跟着 data 的入口口径走（2026-08-31，PR #715）：原先这里过的是
    # `_research_plan_ingress_contract`，它随 data 入口重写一起删了。改问同一条链
    # 上仍然成立的那部分 —— 权威身份要到得了对面（1197482e 的契约）。
    request = _as_data_sees_it(validated["dispatch_node_inputs"])
    assert request["authority"]["kind"] == "pre_registration", request["authority"]
    assert [a["filename"] for a in request["requested_assets"]] == [
        reference["expected_filename"]]


@pytest.mark.xfail(strict=True, reason=(
    "data 的 normalize_preprocessing_request 目前把 acquisition_reference 整块丢掉："
    "归一化后的资产只剩 asset_id/filename/format/purpose/content_constraints/dependencies。"
    "URL、revision、sha256、licence 因此只活在给人看的 Markdown 里，对面要靠正则去捞 —— "
    "跟 1197482e 修掉的「权威身份埋在散文里」是同一类缺口，只是换了个字段。"
    "归一化器在 nodes/data/planning/request_contract.py，不归 experiment 改；"
    "data 侧开始携带这个块的当天，这条会以 XPASS 变红，提醒把它收紧成硬断言。"))
def test_external_acquisition_reference_should_reach_data_structurally(tmp_path: Path):
    """采集依据是证据，不该只以散文形态跨节点。"""
    state = State.new("experiment", tmp_path)
    reference = {
        "source_locator": "https://www.ncei.noaa.gov/data/ibtracs/v04r01/access/csv/ibtracs.WP.list.v04r01.csv",
        "expected_revision": "v04r01",
        "expected_filename": "ibtracs.WP.list.v04r01.csv",
        "expected_sha256": "a" * 64,
        "license": "CC BY 4.0",
    }
    validated = asyncio.run(_validate_data_request_spec(state, json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": "pre_registration__sst_ri",
        "scientific_parameters": "frozen_prereg_only",
        "target_software": "Python xarray analysis",
        "required_assets": [{
            "name": reference["expected_filename"],
            "format": "CSV",
            "purpose": "western North Pacific best-track observations",
            "acquisition_reference": reference,
        }],
        "acceptance": {
            "file_exists": True, "schema": True, "units": True,
            "manifest_lineage": True,
        },
    })))
    assert validated["status"] == "success", validated

    request = _as_data_sees_it(validated["dispatch_node_inputs"])
    asset = next(a for a in request["requested_assets"]
                 if a["filename"] == reference["expected_filename"])
    acquisition = json.dumps(asset, ensure_ascii=False)
    for evidence in ("source_locator", "expected_revision", "expected_sha256", "license"):
        assert reference[evidence] in acquisition, (evidence, asset)


def test_acquisition_reference_rejects_unsafe_digest_and_filename(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    invalid = asyncio.run(_validate_data_request_spec(state, json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "operation",
        "purpose": "obtain a declared public input for a non-scientific smoke test",
        "target_software": "generic solver",
        "scientific_parameters": "not_applicable",
        "required_assets": [{
            "name": "input.csv", "format": "CSV", "purpose": "smoke-test input",
            "acquisition_reference": {
                "source_locator": "https://example.invalid/input.csv",
                "expected_filename": "../escape.csv",
                "expected_sha256": "not-a-sha256",
            },
        }],
        "acceptance": {
            "file_exists": True, "schema": True, "units": True,
            "manifest_lineage": True,
        },
    })))

    assert invalid["status"] == "error"
    assert any("expected_filename" in error for error in invalid["errors"])
    assert any("expected_sha256" in error for error in invalid["errors"])


def test_classification_routes_a_mesh_request_to_the_data_service(tmp_path: Path):
    """入口分类阶段就要说出"这活归 data"，并给出本 run 该用哪种 request_kind。"""
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "先给通道算例生成一套网格，再用 simpleFoam 跑一步看能不能读进去",
        **_no_prereg("This operational build consumes no preregistration."),
    }

    result = asyncio.run(_classify_experiment_scope(
        state, scope="operation", reason="构建验证：生成网格并冒烟跑一步",
        operation_category="toolchain_build"))

    routing = result["preprocessing_delegation"]
    assert routing["detected"] is True
    assert "mesh" in routing["categories"]
    assert routing["route"] == "call_data_synchronously"
    # 没绑冻结 prereg 的 run 必须被指向新出口，而不是那条它过不了的 formal 门。
    assert routing["request_kind"] == "preprocessing_service_request"


def test_classification_routing_does_not_become_a_third_scope_value(tmp_path: Path):
    """路由是与 scope 正交的一维；mode 多一个取值会让 end audit 认不出本 run。"""
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "生成 POSCAR 超胞后跑 VASP",
        **_no_prereg("This exploratory run has no governing preregistration."),
    }

    asyncio.run(_classify_experiment_scope(
        state, scope="scientific", reason="按冻结预注册执行主仿真运行并产出结果"))

    scope = state.hook_state["experiment_execution_scope"]
    assert scope["mode"] == "scientific"
    assert scope["preprocessing_delegation"]["request_kind"] == "preprocessing_service_request"


def test_bound_prereg_routes_to_the_formal_input_kind(tmp_path: Path):
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    saved = _save_frozen(
        state, "pre_registration", "contract", "## prereg\n",
        metadata={"run_role": "primary", "analysis_eligible": True,
                  "expected_params": {"grid": [50, 50]}},
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "缺 KPOINTS 输入包，补齐后按预注册跑主仿真",
    }
    _bind_prereg(state, saved)

    result = asyncio.run(_classify_experiment_scope(
        state, scope="scientific", reason="按冻结预注册执行主仿真运行并产出结果"))

    assert result["preprocessing_delegation"]["request_kind"] == "formal_input_preparation"


def test_a_plain_run_gets_no_routing_noise(tmp_path: Path):
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "pip 安装 xlrd 并验证 import 与版本",
        **_no_prereg("This package-install check consumes no preregistration."),
    }

    result = asyncio.run(_classify_experiment_scope(
        state, scope="operation", reason="安装一个 python 包并验证可导入",
        operation_category="package_install"))

    assert result["preprocessing_delegation"] == {"detected": False, "categories": []}
    assert "preprocessing_delegation" not in state.hook_state["experiment_execution_scope"]


def test_end_to_end_a_run_without_prereg_routes_a_mesh_through_data_and_emits_audit(tmp_path: Path):
    """分类导流 → 建请求 → data 交付 → 验收 → 留下可追溯审计事件。

    这条路径此前是断的：没有冻结 prereg 的 run 连一个能过门的 spec 都构造不出来，
    最后只能自己就地生成网格，而那件事没有任何机械证据。
    """
    from core.loop_hooks import HookContext
    from nodes.experiment.hooks import preprocessing_boundary_audit_on_end
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "给通道算例准备一套网格，然后用 simpleFoam 验证能读入",
        **_no_prereg("This operational smoke test consumes no preregistration."),
    }

    # 1) 分类阶段就被告知这活归 data，且给出本 run 该用的 kind。
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="operation", reason="构建验证：需要网格并冒烟跑一步",
        operation_category="toolchain_build"))
    routing = classified["preprocessing_delegation"]
    assert routing["request_kind"] == "preprocessing_service_request"

    # 2) 按它给的 kind 建请求 —— 这在改动前直接被 spec 门拒掉。
    validated = asyncio.run(_validate_data_request_spec(
        state, _service_spec(request_kind=routing["request_kind"])))
    assert validated["status"] == "success", validated
    assert validated["dispatch_node_inputs"]["spec"].startswith("# Preprocessing request")

    # 3) data 交付并验收。
    _delivered_package(tmp_path, state, validated["spec_id"])

    # 4) 拿到网格后照常执行；on_end 写入机械审计，供 closure/review 追溯。
    state.append_transcript("tool_call", turn=3, name="safe_run_bash",
                            args={"command": "simpleFoam -case channel > run.log"})
    preprocessing_boundary_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=3), None)

    events = [json.loads(line) for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
              if line.strip() and json.loads(line).get("event") == "preprocessing_boundary_audit"]
    assert len(events) == 1
    assert events[0]["verdict"] == "PASS"
    assert events[0]["n_unaccounted"] == 0


def test_idempotent_reclassification_still_reports_the_routing(tmp_path: Path):
    """幂等重调（resume）返回的形状必须和首次一致，否则 agent 会以为不用派 data。"""
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "先生成网格再跑 simpleFoam 验证",
        **_no_prereg("This operational smoke test consumes no preregistration."),
    }
    args = dict(scope="operation", reason="构建验证：需要网格并冒烟跑一步",
                operation_category="toolchain_build")

    first = asyncio.run(_classify_experiment_scope(state, **args))
    again = asyncio.run(_classify_experiment_scope(state, **args))
    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    after_reopen = asyncio.run(_classify_experiment_scope(reopened, **args))

    assert again["idempotent"] is True
    assert again["preprocessing_delegation"] == first["preprocessing_delegation"]
    assert after_reopen["idempotent"] is True
    assert after_reopen["preprocessing_delegation"] == first["preprocessing_delegation"]
    assert reopened.hook_state["experiment_execution_scope"][
        "preprocessing_delegation"
    ] == first["preprocessing_delegation"]
    # 对面那半（_request_mode 恢复）不能被这条顺带弄坏。
    assert state.hook_state["_request_mode"] == "operation"
    assert reopened.hook_state["_request_mode"] == "operation"
