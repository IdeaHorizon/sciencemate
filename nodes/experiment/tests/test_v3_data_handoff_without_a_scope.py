"""#1052（Experiment 侧）：v3 run 没点名 scope 时，Data 交接不能卡在已退役的分类手续上。

v3 bootstrap 在无 prereg、调用方也没点名 scope 时把 scope 留成「未定」——正常情况。
修复前 dispatch / reconcile / verify_dataset_consumption / authorize_experiment_fallback
仍读旧的 run 级 scope 收据，一律 ``scope_unbound`` 并叫模型去调 classify_experiment_scope
（v3 工具面上已经没有它），派发之后的 v2 pending-census 门又叫父「带 typed prereg 重派」，
连带了 typed none 的 run 也一样。fetch_resource 早已改读 v3 的所选任务绑定；这里钉住
Data 家族与它同一判据，同时钉住：绑定冻结 prereg 的 formal 派发仍逐项核那张收据，
旧协议 run 仍走旧审计。

走真实的 bootstrap → validate_data_request_spec → dispatch_data_request →
verify_dataset_consumption / reconcile_data_dispatch / authorize_experiment_fallback；
只把拉起子 run 的 run_node 换成在磁盘上写出一个 Data 子 run 的替身。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import shared.tools.run_node as run_node_module
from core.state import State
from nodes.experiment.tests.test_blocked_operation_closure import _write_child_dataset
from nodes.experiment.tools import contract_audit, ledger, run_bootstrap


def _service_spec() -> str:
    return json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "operation",
        "purpose": "把翼型坐标转成求解器可读的网格",
        "target_software": "xfoil",
        "scientific_parameters": "not_applicable",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })


def _formal_spec(prereg_id: str) -> str:
    return json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": prereg_id,
        "scientific_parameters": "frozen_prereg_only",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })


def _bound_prereg(state: State, name: str) -> dict:
    from core.ledger import record_version, sha256_text
    saved = state.save_artifact("pre_registration", name, f"# {name}\n",
                                metadata={"execution_mode": "scientific"})
    state.mark_frozen(saved["id"])
    versions = state.artifact_versions(saved["id"])
    return {"kind": "bound", "artifact_id": saved["id"], "version": record_version(versions[-1]),
            "content_hash": str(versions[-1].get("content_hash") or sha256_text(f"# {name}\n"))}


def _v3_state(tmp_path: Path, **inputs) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    if callable(inputs.get("prereg_assignment")):
        inputs["prereg_assignment"] = inputs["prereg_assignment"](state)
    state.hook_state["node_inputs"] = {
        "experiment_spec": "用 xfoil 跑 NACA0012 翼型算例",
        "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}],
        **inputs,
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.v3_active(state)
    return state


_DATASET_ID = "dataset__airfoil_mesh"


def _fake_data_child(monkeypatch, state: State, tmp_path: Path) -> list[dict]:
    launched: list[dict] = []
    child_run_id = "data-child-1052"

    async def fake_run_node(**kwargs):
        launched.append(kwargs)
        package = tmp_path / "package"
        package.mkdir(exist_ok=True)
        (package / "channel.msh").write_text("mesh bytes", encoding="utf-8")
        child_dir = state.root.parent / child_run_id
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        _write_child_dataset(state, child_run_id, _DATASET_ID, package)
        return {"status": "completed", "child_run_id": child_run_id, "child_node_type": "data",
                "child_status": "completed", "all_child_artifacts": [{"id": _DATASET_ID}]}

    monkeypatch.setattr(run_node_module, "_run_node_tool", fake_run_node)
    return launched


def _no_retired_tool(*results: dict) -> None:
    text = json.dumps(results, ensure_ascii=False, default=str)
    assert "classify" not in text, text


@pytest.mark.parametrize("prereg", ["explicit_none", "omitted"])
def test_a_v3_run_with_an_undecided_scope_dispatches_and_consumes_a_data_request(tmp_path, monkeypatch, prereg):
    inputs = {"prereg_assignment": {"kind": "none", "reason": "本 run 没有预注册"}} if prereg == "explicit_none" else {}
    state = _v3_state(tmp_path, **inputs)
    assert run_bootstrap.load_run_authority(state)["scope"] is None
    launched = _fake_data_child(monkeypatch, state, tmp_path)

    validated = asyncio.run(contract_audit._validate_data_request_spec(state, _service_spec()))
    assert validated["status"] == "success", validated
    dispatched = asyncio.run(contract_audit._dispatch_data_request(
        state, validated["spec_id"], user_note="需要翼型网格"))
    assert dispatched["status"] == "completed", dispatched
    assert len(launched) == 1 and launched[0]["node_type"] == "data"

    consumed = asyncio.run(contract_audit._verify_dataset_consumption(
        state, _DATASET_ID, validated["spec_id"]))
    assert consumed["status"] == "success", consumed
    reconciled = asyncio.run(contract_audit._reconcile_data_dispatch(state, validated["spec_id"]))
    assert "intent_binding" not in reconciled, reconciled
    fallback = asyncio.run(contract_audit._authorize_experiment_fallback(state, validated["spec_id"]))
    assert fallback["fallback_permitted_by_contract"] is False
    _no_retired_tool(dispatched, consumed, reconciled, fallback)


def test_a_v3_run_without_a_prereg_still_cannot_formal_dispatch(tmp_path, monkeypatch):
    state = _v3_state(tmp_path, prereg_assignment={"kind": "none", "reason": "本 run 没有预注册"})
    other = _bound_prereg(state, "unrelated")
    launched = _fake_data_child(monkeypatch, state, tmp_path)
    validated = asyncio.run(contract_audit._validate_data_request_spec(state, _formal_spec(other["artifact_id"])))
    dispatched = (asyncio.run(contract_audit._dispatch_data_request(
        state, validated["spec_id"], user_note="正式输入")) if validated.get("spec_id") else validated)
    assert dispatched["status"] == "error", dispatched
    assert launched == []
    _no_retired_tool(validated, dispatched)


def test_a_v3_run_bound_to_a_prereg_formal_dispatches_against_that_exact_receipt(tmp_path, monkeypatch):
    state = _v3_state(tmp_path, prereg_assignment=lambda s: _bound_prereg(s, "governing"))
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] == "scientific" and authority["prereg_status"] == "bound_valid"
    launched = _fake_data_child(monkeypatch, state, tmp_path)

    validated = asyncio.run(contract_audit._validate_data_request_spec(
        state, _formal_spec(authority["prereg"]["artifact_id"])))
    assert validated["status"] == "success", validated
    dispatched = asyncio.run(contract_audit._dispatch_data_request(
        state, validated["spec_id"], user_note="冻结协议规定的正式输入"))
    assert dispatched["status"] == "completed", dispatched
    assert len(launched) == 1
    consumed = asyncio.run(contract_audit._verify_dataset_consumption(
        state, _DATASET_ID, validated["spec_id"]))
    assert consumed["status"] == "success", consumed


def test_a_legacy_run_keeps_the_old_scope_binding_check(tmp_path, monkeypatch):
    """不在 v3 纪元上的 run 不改判据：没分类就没有可派发的绑定。"""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"},
                                       "experiment_focus": "legacy"}
    assert not ledger.v3_active(state)
    launched = _fake_data_child(monkeypatch, state, tmp_path)
    validated = asyncio.run(contract_audit._validate_data_request_spec(state, _service_spec()))
    dispatched = asyncio.run(contract_audit._dispatch_data_request(
        state, validated["spec_id"], user_note="x"))
    assert dispatched["status"] == "error"
    assert dispatched["intent_binding"]["status"] == "scope_unbound"
    assert launched == []
