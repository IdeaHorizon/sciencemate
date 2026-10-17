"""Selected formal Data inputs are re-observed at the action boundary."""
from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core.ledger import record_version
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tests.test_blocked_operation_closure import _write_child_dataset
from nodes.experiment.tools import (
    contract_audit,
    input_admission,
    input_delivery,
    ledger,
    run_bootstrap,
    safe_bash,
)
from nodes.experiment.tools.path_roles import collect_path_roles


def _run(awaitable):
    return asyncio.run(awaitable)


def _formal_state(tmp_path: Path) -> tuple[State, dict[str, Any]]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="verified-input-freshness",
        session_id="owner",
    )
    saved = state.save_artifact(
        "pre_registration",
        "verified_input_freshness",
        "# Formal mesh consumption\n",
    )
    state.mark_frozen(saved["id"], {"freeze_reason": "formal Data fixture"})
    version = state.artifact_versions(saved["id"])[-1]
    binding = {
        "artifact_id": saved["id"],
        "version": record_version(version),
        "content_hash": version["content_hash"],
    }
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="task-verified-input-freshness",
        objective="consume the formally prepared mesh",
        target_node="experiment",
        prereg_assignment=PreregAssignment.exact(
            binding["artifact_id"],
            version=str(binding["version"]),
            content_hash=binding["content_hash"],
        ),
        actor="parent",
        reason="formal dispatch",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_digest = revision.digest
    state.task_contract_revision = revision.revision
    state.parent_dispatch_id = "dispatch-verified-input-freshness"
    state.hook_state["node_inputs"] = {
        "scope": "scientific",
        "experiment_spec": "Consume only the explicitly selected formal mesh package.",
        "required_outputs": [],
        "prereg_assignment": {
            "kind": "none",
            "reason": "stale caller hint must not replace Core authority",
        },
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    assert established["scope"]["prereg_status"] == "bound_valid", established
    return state, binding


def _formal_request(prereg_id: str, *, purpose: str) -> str:
    return json.dumps(
        {
            "request_kind": "formal_input_preparation",
            "source_prereg_artifact_id": prereg_id,
            "scientific_parameters": "frozen_prereg_only",
            "required_assets": [
                {
                    "name": "channel.msh",
                    "format": "Gmsh",
                    "purpose": purpose,
                }
            ],
            "acceptance": {
                "file_exists": True,
                "schema": True,
                "units": True,
                "manifest_lineage": True,
            },
        }
    )


def _deliver_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: State,
    binding: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    validated = _run(
        contract_audit._validate_data_request_spec(
            state,
            _formal_request(
                binding["artifact_id"],
                purpose=f"solver mesh for independent input {suffix}",
            ),
        )
    )
    assert validated["status"] == "success", validated

    child_run_id = f"data-verified-input-{suffix}"
    dataset_id = f"dataset__verified_input_{suffix}"
    package = tmp_path / f"package-{suffix}"
    package.mkdir()
    asset = package / "channel.msh"
    asset.write_text(f"mesh bytes {suffix}\n", encoding="utf-8")

    async def fake_run_node(**kwargs):
        assert kwargs["node_type"] == "data"
        assert kwargs["node_inputs"] == validated["dispatch_node_inputs"]
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "event": "run_start",
                    "node_type": "data",
                    "parent_run_id": state.run_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _write_child_dataset(state, child_run_id, dataset_id, package)
        return {
            "status": "completed",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "completed",
            "all_child_artifacts": [{"id": dataset_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = _run(
        contract_audit._dispatch_data_request(
            state,
            validated["spec_id"],
            user_note=f"Prepare formal input {suffix} for this exact request.",
        )
    )
    assert dispatched["status"] == "completed", dispatched
    verified = _run(
        contract_audit._verify_dataset_consumption(
            state, dataset_id, validated["spec_id"]
        )
    )
    assert verified["status"] == "success" and verified["passed"] is True, verified
    return {
        "spec_id": validated["spec_id"],
        "dataset_id": dataset_id,
        "child_run_id": child_run_id,
        "package": package,
        "manifest": package / "manifest.json",
        "asset": asset,
    }


def _run_root(state: State) -> Path:
    roots = {
        role.role: Path(role.path)
        for role in collect_path_roles(state)
        if role.writable and not role.container_only
    }
    return roots["run_root"]


def _selected_action(
    state: State,
    *,
    dataset_id: str | None,
    marker_name: str,
) -> tuple[dict[str, Any], Path]:
    marker = _run_root(state) / marker_name
    kwargs = (
        {"input_package_artifact_id": dataset_id}
        if dataset_id is not None
        else {}
    )
    result = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=f"printf INPUT_USED > {shlex.quote(str(marker))}",
            cwd=str(_run_root(state)),
            timeout=30,
            **kwargs,
        )
    )
    return result, marker


def _contains_identity(value: Any, expected: Mapping[str, Any]) -> bool:
    if isinstance(value, Mapping):
        if all(value.get(key) == item for key, item in expected.items()):
            return True
        return any(_contains_identity(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_identity(item, expected) for item in value)
    return False


def _historically_verified(state: State, spec_id: str) -> bool:
    entry = input_delivery.active_input_delivery_entries(state)[spec_id]
    return entry["delivery"].get("verified") is True


def test_selected_verified_input_rechecks_current_package_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, binding = _formal_state(tmp_path)
    delivered = _deliver_verified(
        tmp_path, monkeypatch, state, binding, suffix="stale"
    )
    delivered["manifest"].write_text("{broken", encoding="utf-8")

    result, marker = _selected_action(
        state,
        dataset_id=delivered["dataset_id"],
        marker_name="must_not_run.txt",
    )

    assert result["status"] == "error", result
    assert result["error_code"] == "formal_input_not_ready"
    assert result["payload_spawned"] is False
    assert result["job_submitted"] is False
    assert marker.exists() is False
    assert _historically_verified(state, delivered["spec_id"])


def test_repaired_verified_input_retries_without_redelivery_and_records_current_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, binding = _formal_state(tmp_path)
    delivered = _deliver_verified(
        tmp_path, monkeypatch, state, binding, suffix="repair"
    )
    original_manifest = delivered["manifest"].read_text(encoding="utf-8")
    delivered["manifest"].write_text("{broken", encoding="utf-8")

    refused = input_admission.check_consumed_input(
        state,
        runner="safe_run_bash",
        input_package_artifact_id=delivered["dataset_id"],
    )
    assert refused is not None and refused["payload_spawned"] is False, refused
    assert _historically_verified(state, delivered["spec_id"])

    delivered["manifest"].write_text(original_manifest, encoding="utf-8")
    result, marker = _selected_action(
        state,
        dataset_id=delivered["dataset_id"],
        marker_name="retry_after_repair.txt",
    )
    if result.get("status") != "success" and "sandbox" in str(result).lower():
        pytest.skip(f"sandbox unavailable: {str(result)[:200]}")
    assert result["status"] == "success", result
    assert marker.read_text(encoding="utf-8") == "INPUT_USED"
    assert _historically_verified(state, delivered["spec_id"])

    verification_events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        and json.loads(line).get("event") == "dataset_consumption_verification"
    ]
    assert len(verification_events) == 1

    child_record = contract_audit._read_child_run_artifact(
        state,
        delivered["child_run_id"],
        delivered["dataset_id"],
        require_sibling=True,
    )
    assert isinstance(child_record, dict)
    expected_identity = {
        "id": delivered["dataset_id"],
        "version": child_record["version"],
        "content_hash": child_record["content_hash"],
        "produced_by_run_id": delivered["child_run_id"],
    }
    started = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started"
        and row.get("tool") == "safe_run_bash"
    ]
    assert len(started) == 1, started
    observation = started[0]["execution_context"]["input_verification"]
    assert observation["status"] == "verified"
    assert _contains_identity(observation["audit"], expected_identity), observation


def test_no_selector_does_not_recheck_or_block_an_unconsumed_stale_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, binding = _formal_state(tmp_path)
    delivered = _deliver_verified(
        tmp_path, monkeypatch, state, binding, suffix="unselected"
    )
    delivered["manifest"].write_text("{broken", encoding="utf-8")

    result, marker = _selected_action(
        state,
        dataset_id=None,
        marker_name="unselected_preparation.txt",
    )
    if result.get("status") != "success" and "sandbox" in str(result).lower():
        pytest.skip(f"sandbox unavailable: {str(result)[:200]}")
    assert result["status"] == "success", result
    assert marker.read_text(encoding="utf-8") == "INPUT_USED"


def test_selected_healthy_input_is_not_poisoned_by_an_unconsumed_stale_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, binding = _formal_state(tmp_path)
    stale = _deliver_verified(
        tmp_path, monkeypatch, state, binding, suffix="stale_peer"
    )
    healthy = _deliver_verified(
        tmp_path, monkeypatch, state, binding, suffix="healthy_selected"
    )
    stale["manifest"].write_text("{broken", encoding="utf-8")

    result, marker = _selected_action(
        state,
        dataset_id=healthy["dataset_id"],
        marker_name="selected_healthy.txt",
    )
    if result.get("status") != "success" and "sandbox" in str(result).lower():
        pytest.skip(f"sandbox unavailable: {str(result)[:200]}")
    assert result["status"] == "success", result
    assert marker.read_text(encoding="utf-8") == "INPUT_USED"
    assert _historically_verified(state, stale["spec_id"])
    assert _historically_verified(state, healthy["spec_id"])
