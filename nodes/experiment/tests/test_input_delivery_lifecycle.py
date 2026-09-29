"""Data 请求/交付状态必须跨进程恢复，并允许显式修订请求。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    _dispatch_data_request,
    _validate_data_request_spec,
    _verify_dataset_consumption,
    audit_input_delivery_for_execution,
)
from nodes.experiment.tools.preflight import audit_execution_contract


def _service_spec(asset_name: str, *, purpose: str = "验证求解器可读取输入") -> str:
    return json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": purpose,
        "target_software": "OpenFOAM 11",
        "scientific_parameters": "not_applicable",
        "required_assets": [{
            "name": asset_name,
            "format": "Gmsh",
            "purpose": "solver mesh",
        }],
        "acceptance": {
            "file_exists": True,
            "schema": True,
            "units": True,
            "manifest_lineage": True,
        },
    })


def _formal_spec(prereg_id: str, asset_name: str) -> str:
    return json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": prereg_id,
        "scientific_parameters": "frozen_prereg_only",
        "required_assets": [{
            "name": asset_name,
            "format": "Gmsh",
            "purpose": "solver mesh",
        }],
        "acceptance": {
            "file_exists": True,
            "schema": True,
            "units": True,
            "manifest_lineage": True,
        },
    })


def _deliver(tmp_path: Path, state: State, spec_id: str, asset_name: str, name: str) -> str:
    package = tmp_path / f"package-{name}"
    package.mkdir()
    (package / asset_name).write_text("mesh bytes", encoding="utf-8")
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps({"lineage": [{"op": "prepare_mesh"}]}), encoding="utf-8")
    dataset_id = state.save_artifact("dataset", name, json.dumps({
        "package_dir": str(package),
        "manifest_path": str(manifest),
        "lineage": [{"op": "prepare_mesh"}],
        "downstream_contract": {"files": [asset_name]},
    }))["id"]
    verified = asyncio.run(_verify_dataset_consumption(state, dataset_id, spec_id))
    assert verified["status"] == "success", verified
    return dataset_id


def test_verified_delivery_audit_survives_state_reopen(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    request = asyncio.run(_validate_data_request_spec(state, _service_spec("channel.msh")))
    dataset_id = _deliver(tmp_path, state, request["spec_id"], "channel.msh", "mesh")

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    assert reopened.hook_state.get("input_delivery_state") is None

    audit = audit_input_delivery_for_execution(reopened, dataset_id)

    assert audit["passed"] is True, audit
    assert audit["applicable"] is True
    assert audit["spec_ids"] == [request["spec_id"]]
    assert reopened.hook_state["input_delivery_state"][request["spec_id"]]["verified"] is True


def test_formal_delivery_still_enforces_frozen_parameters_after_reopen(
    tmp_path: Path, monkeypatch,
) -> None:
    from nodes.experiment.tests.test_blocked_operation_closure import _write_child_dataset
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "contract",
        "# prereg",
        metadata={
            "run_role": "secondary",
            "analysis_eligible": False,
            "expected_params": {"grid": [50, 50]},
        },
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "consume the managed formal mesh and enforce its frozen parameters",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Bind the formal delivery fixture before dispatching Data.",
    ))
    assert classified["status"] == "success", classified
    request = asyncio.run(_validate_data_request_spec(
        state,
        _formal_spec(prereg_id, "channel.msh"),
    ))

    child_run_id = "data-formal-lifecycle-child"
    dataset_id = "dataset__formal_lifecycle_mesh"
    package = tmp_path / "formal-lifecycle-package"
    package.mkdir()
    (package / "channel.msh").write_text("mesh bytes", encoding="utf-8")

    async def fake_run_node(**kwargs):
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start",
            "node_type": "data",
            "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        _write_child_dataset(state, child_run_id, dataset_id, package)
        return {
            "status": "completed",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "completed",
            "all_child_artifacts": [{"id": dataset_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(
        state,
        request["spec_id"],
        user_note="Prepare the frozen formal mesh for this run.",
    ))
    assert dispatched["status"] == "completed", dispatched
    delivered = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, request["spec_id"],
    ))
    assert delivered["status"] == "success", delivered

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    mismatch = audit_execution_contract(
        reopened,
        {"grid": [20, 20]},
        stage="simulation",
    )

    assert mismatch["passed"] is False
    assert "execution_params_mismatch" in mismatch["blocking_reasons"]


def test_explicit_supersede_replaces_only_the_named_active_spec(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    old = asyncio.run(_validate_data_request_spec(state, _service_spec("old.msh")))
    new = asyncio.run(_validate_data_request_spec(
        state,
        _service_spec("new.msh", purpose="旧请求资产名称有误，改为新的求解器输入"),
        supersedes_spec_id=old["spec_id"],
        supersede_reason="上一个请求使用了错误的资产名称，必须以修订后的请求为准",
    ))

    assert new["status"] == "success", new
    assert new["supersedes_spec_id"] == old["spec_id"]
    dataset_id = _deliver(tmp_path, state, new["spec_id"], "new.msh", "new-mesh")

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    audit = audit_input_delivery_for_execution(
        reopened,
        input_package_bindings={new["spec_id"]: dataset_id},
    )

    assert audit["passed"] is True, audit
    assert audit["spec_ids"] == [new["spec_id"]]
    old_delivery = reopened.hook_state["input_delivery_state"][old["spec_id"]]
    assert old_delivery["lifecycle_status"] == "superseded"
    assert old_delivery["superseded_by"] == new["spec_id"]


def test_superseded_or_wrong_package_bindings_are_still_rejected(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    old = asyncio.run(_validate_data_request_spec(state, _service_spec("old.msh")))
    new = asyncio.run(_validate_data_request_spec(
        state,
        _service_spec("new.msh", purpose="修正输入资产名称"),
        supersedes_spec_id=old["spec_id"],
        supersede_reason="旧请求的文件名不符合实际求解器输入约定",
    ))
    dataset_id = _deliver(tmp_path, state, new["spec_id"], "new.msh", "current")

    stale_binding = audit_input_delivery_for_execution(
        state,
        input_package_bindings={old["spec_id"]: dataset_id},
    )
    wrong_package = audit_input_delivery_for_execution(
        state,
        input_package_bindings={new["spec_id"]: "dataset__wrong"},
    )

    assert stale_binding["passed"] is False
    assert stale_binding["blocking_reasons"] == ["input_package_binding_incomplete"]
    assert stale_binding["unknown_spec_ids"] == [old["spec_id"]]
    assert wrong_package["passed"] is False
    assert wrong_package["blocking_reasons"] == ["input_package_artifact_mismatch"]


def test_unknown_supersede_target_does_not_register_the_new_spec(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)

    result = asyncio.run(_validate_data_request_spec(
        state,
        _service_spec("new.msh"),
        supersedes_spec_id="data_request__does_not_exist",
        supersede_reason="尝试替代一条不存在的请求",
    ))

    assert result["status"] == "error"
    assert result["error"] == "superseded_spec_not_active"
    assert audit_input_delivery_for_execution(state)["applicable"] is False


def test_corrupt_ledger_fails_closed_after_reopen(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    request = asyncio.run(_validate_data_request_spec(state, _service_spec("channel.msh")))
    ledger_path = state.find_artifact_path(request["input_delivery_ledger_artifact_id"])
    assert ledger_path is not None
    ledger_path.write_text("{not-json", encoding="utf-8")

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    audit = audit_input_delivery_for_execution(reopened, "dataset__anything")

    assert audit["passed"] is False
    assert audit["applicable"] is True
    assert audit["blocking_reasons"] == ["input_delivery_ledger_unreadable"]


def test_missing_ledger_with_data_transcript_fails_closed_after_reopen(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    request = asyncio.run(_validate_data_request_spec(state, _service_spec("channel.msh")))
    ledger_path = state.find_artifact_path(request["input_delivery_ledger_artifact_id"])
    assert ledger_path is not None
    ledger_path.unlink()

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    audit = audit_input_delivery_for_execution(reopened, "dataset__anything")

    assert audit["passed"] is False
    assert audit["blocking_reasons"] == ["input_delivery_ledger_unreadable"]


def test_idempotent_validation_migrates_legacy_hook_state_to_durable_ledger(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    raw_spec = _service_spec("legacy.msh")
    normalized = json.loads(raw_spec)
    spec_id = "data_request__" + hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    state.hook_state["validated_data_request_specs"] = {spec_id: normalized}
    state.hook_state["input_delivery_state"] = {
        spec_id: {
            "provider": "data",
            "verified": True,
            "input_package_artifact_id": "dataset__legacy",
            "scientific_authority": False,
        },
    }

    migrated = asyncio.run(_validate_data_request_spec(state, raw_spec))
    reopened = State.reopen("experiment", tmp_path, state.run_id)
    audit = audit_input_delivery_for_execution(reopened, "dataset__legacy")

    assert migrated["status"] == "success", migrated
    assert migrated["idempotent"] is True
    assert reopened.find_artifact_path(migrated["input_delivery_ledger_artifact_id"]) is not None
    assert audit["passed"] is True, audit
