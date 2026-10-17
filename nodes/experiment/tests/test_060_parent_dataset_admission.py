"""Ordinary selected datasets are inspected, not invented formal deliveries."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from nodes.experiment.tests.test_060_action_input_admission import _service_spec, _v3_state
from nodes.experiment.tools import (
    contract_audit,
    finish,
    input_admission,
    input_delivery,
    ledger,
    safe_bash,
)


def _dataset(state, tmp_path, *, name="parent", kind="dataset", manifest="{}"):
    package = tmp_path / name
    package.mkdir()
    (package / "input.txt").write_text("parent data\n", encoding="utf-8")
    manifest_path = package / "manifest.json"
    manifest_path.write_text(manifest, encoding="utf-8")
    saved = state.save_artifact(
        kind,
        name,
        json.dumps(
            {
                "package_dir": str(package),
                "manifest_path": str(manifest_path),
                "lineage": [{"op": "upstream preparation"}],
                "downstream_contract": {"files": ["input.txt"]},
            }
        ),
    )
    return saved["id"], manifest_path


def test_parent_dataset_is_structurally_checked_without_registering_a_data_request(tmp_path):
    state = _v3_state(tmp_path)
    dataset_id, _ = _dataset(state, tmp_path)
    record = state.read_artifact(dataset_id)
    before = input_delivery.load_input_delivery_ledger(state)
    result = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd="printf checked > prepared.txt",
            timeout=20,
            input_package_artifact_id=dataset_id,
        )
    )
    assert result["status"] == "success", result
    (started,) = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"
    ]
    observed = started["execution_context"]["input_verification"]
    assert observed["status"] == "structure_checked"
    audit = observed["audit"]
    assert audit["formal_verification"] == "not_performed"
    assert audit["task_fit"] == "not_evaluated"
    check = audit["package_checks"][dataset_id]
    assert check["artifact_id"] == dataset_id
    assert check["artifact_identity"]["version"] == record["version"]
    assert check["artifact_identity"]["content_hash"] == record["content_hash"]
    assert check["limitations"]
    assert input_delivery.load_input_delivery_ledger(state) == before


@pytest.mark.parametrize("defect", ["absent", "wrong_type", "bad_manifest"])
def test_broken_selected_parent_package_cannot_reach_real_action(tmp_path, defect):
    state = _v3_state(tmp_path)
    if defect == "absent":
        dataset_id = "dataset__does_not_exist"
    else:
        dataset_id, _ = _dataset(
            state,
            tmp_path,
            kind="experiment_log" if defect == "wrong_type" else "dataset",
            manifest="not JSON" if defect == "bad_manifest" else "{}",
        )
    result = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd="printf should-not-run > payload.txt",
            timeout=20,
            input_package_artifact_id=dataset_id,
        )
    )
    assert result["status"] == "error", result
    assert result["payload_spawned"] is False
    assert result["job_submitted"] is False
    assert result["retryable_after_change"] is True
    assert not [
        row for row in ledger.open_ledger(state).records() if row.get("kind") == "action_started"
    ]


def test_repaired_parent_package_retries_without_a_new_spec_or_data_run(tmp_path):
    state = _v3_state(tmp_path)
    dataset_id, manifest = _dataset(state, tmp_path, manifest="broken")
    args = {"runner": "safe_run_bash", "input_package_artifact_id": dataset_id}
    refused = input_admission.check_consumed_input(state, **args)
    assert refused is not None
    manifest.write_text("{}", encoding="utf-8")
    observed = {}
    assert input_admission.check_consumed_input(state, observation=observed, **args) is None
    assert observed["status"] == "structure_checked"
    assert input_delivery.active_input_delivery_entries(state) == {}


def test_valid_auxiliary_package_is_not_rejected_as_a_different_parent_task(tmp_path):
    state = _v3_state(tmp_path)
    first, _ = _dataset(state, tmp_path, name="primary")
    second, _ = _dataset(state, tmp_path, name="auxiliary")
    state.hook_state["forwarded_input_ids"] = [first]
    observed = {}
    assert (
        input_admission.check_consumed_input(
            state,
            runner="safe_execute_python",
            input_package_artifact_id=second,
            observation=observed,
        )
        is None
    )
    assert observed["audit"]["task_fit"] == "not_evaluated"
    assert observed["audit"]["package_checks"][second]["passed"] is True


@pytest.mark.parametrize(
    "selector,reason",
    [
        ({"input_package_artifact_id": ""}, "input_package_artifact_invalid"),
        ({"input_package_bindings": {}}, "input_package_bindings_invalid"),
        (
            {"input_package_artifact_id": "id", "input_package_bindings": {"x": "id"}},
            "input_package_selector_conflict",
        ),
    ],
)
def test_no_active_spec_does_not_disable_selector_shape_checks(tmp_path, selector, reason):
    state = _v3_state(tmp_path)
    result = contract_audit.audit_input_delivery_for_execution(
        state, consumed_only=True, **selector
    )
    assert result["passed"] is False
    assert result["blocking_reasons"] == [reason]


def test_pending_registered_spec_cannot_be_bypassed_via_ordinary_parent_path(tmp_path):
    state = _v3_state(tmp_path)
    validated = asyncio.run(
        contract_audit._validate_data_request_spec(
            state,
            spec=_service_spec("input.txt", purpose="formal requested preparation"),
        )
    )
    dataset_id, _ = _dataset(state, tmp_path)
    result = contract_audit.audit_input_delivery_for_execution(
        state,
        input_package_bindings={validated["spec_id"]: dataset_id},
        consumed_only=True,
    )
    assert result["passed"] is False
    assert result["blocking_reasons"] == ["formal_input_delivery_unverified"]
    assert "package_checks" not in result


def test_preparation_without_selection_is_not_reclassified_as_dataset_consumption(tmp_path):
    state = _v3_state(tmp_path)
    observed = {}
    assert (
        input_admission.check_consumed_input(state, runner="safe_run_bash", observation=observed)
        is None
    )
    assert observed["status"] == "not_selected"


def test_multiple_ordinary_packages_are_checked_without_invented_spec_authority(tmp_path):
    state = _v3_state(tmp_path)
    first, _ = _dataset(state, tmp_path, name="mesh")
    second, _ = _dataset(state, tmp_path, name="boundary")
    observed = {}
    assert (
        input_admission.check_consumed_input(
            state,
            runner="safe_run_bash",
            input_package_bindings={"mesh": first, "boundary": second},
            observation=observed,
        )
        is None
    )
    assert set(observed["audit"]["package_checks"]) == {first, second}
    assert observed["audit"]["spec_ids"] == []
    assert input_delivery.active_input_delivery_entries(state) == {}


def test_one_bad_package_in_a_multi_input_action_prevents_all_execution(tmp_path):
    state = _v3_state(tmp_path)
    valid, _ = _dataset(state, tmp_path, name="valid")
    broken, _ = _dataset(state, tmp_path, name="broken", manifest="not JSON")
    result = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd="printf cannot-run > unexpected.txt",
            timeout=20,
            input_package_bindings={"valid": valid, "broken": broken},
        )
    )
    assert result["status"] == "error", result
    assert result["payload_spawned"] is False
    assert result["audit"]["package_checks"][valid]["passed"] is True
    assert result["audit"]["package_checks"][broken]["passed"] is False
    assert not any(
        row.get("kind") == "action_started" for row in ledger.open_ledger(state).records()
    )


@pytest.mark.parametrize("tool", ["safe_run_bash", "safe_execute_python", "submit_job"])
def test_model_tool_schemas_explain_both_ordinary_and_formal_inputs(tool):
    from core.bootstrap import bootstrap
    from core.tool_registry import _REGISTRY

    bootstrap(force=True)
    props = _REGISTRY.tools[tool].parameters_schema["properties"]
    scalar = props["input_package_artifact_id"]["description"]
    mapping = props["input_package_bindings"]["description"]
    assert "Ordinary" in scalar and "no new Data request" in scalar
    assert "formal" in scalar and "verification" in scalar
    assert "spec_id" in mapping and "labels" in mapping
    assert "scientific task fitness" in mapping


def test_parent_dataset_real_calculation_and_delivery_keep_inspection_limits(tmp_path):
    from nodes.experiment.tests.test_060_s2_scientific_delivery import OUTPUT_RELATIVE, _state

    state = _state(
        tmp_path, prereg_assignment={"kind": "none", "reason": "exploratory measurement"}
    )
    # Put the delivered package in the run's actual readable namespace.  A
    # fixture under unrelated /tmp must not require weakening the OS boundary.
    dataset_id, manifest = _dataset(state, state.root)
    source = manifest.parent / "input.txt"
    source.write_text("1 2 4 8", encoding="utf-8")
    target = working_directory(state) / OUTPUT_RELATIVE
    program = (
        "from pathlib import Path; from statistics import mean; "
        f"sample = list(map(float, Path({str(source)!r}).read_text().split())); "
        "print(f'sample_mean={mean(sample):.6f}')"
    )
    result = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd=(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
                f"> {shlex.quote(str(target))}"
            ),
            timeout=20,
            input_package_artifact_id=dataset_id,
        )
    )
    assert result["status"] == "success", result
    closed = asyncio.run(
        finish._finish(state, outcome="success", summary="Computed the upstream sample mean.")
    )
    assert closed["status"] == "success", closed
    assert any(
        "scientific task fitness were not verified" in item
        for item in closed["delivery"]["limitations"]
    )
    clean = json.loads(state.read_artifact(closed["artifact_ids"]["clean_results"])["content"])
    (lineage,) = clean["evaluation"]["execution_lineage"]
    assert lineage["input_verification"]["status"] == "structure_checked"
    assert (
        lineage["input_verification"]["audit"]["package_checks"][dataset_id]["artifact_id"]
        == dataset_id
    )
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    assert input_delivery.active_input_delivery_entries(state) == {}
