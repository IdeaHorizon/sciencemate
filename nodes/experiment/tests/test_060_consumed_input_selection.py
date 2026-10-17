"""Action-local input selection must not turn independent specs into one gate."""
from __future__ import annotations

from typing import Any

import pytest

from core.state import State
from nodes.experiment.tests.test_input_delivery_lifecycle import _deliver
from nodes.experiment.tools.contract_audit import audit_input_delivery_for_execution
from nodes.experiment.tools.input_delivery import save_input_delivery_ledger


def _request(*asset_names: str) -> dict[str, Any]:
    return {
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": "prepare an independently consumable solver input",
        "target_software": "test-solver",
        "scientific_parameters": "not_applicable",
        "required_assets": [
            {
                "name": asset_name,
                "format": "test",
                "purpose": "solver input",
            }
            for asset_name in asset_names
        ],
    }


def _entry(
    artifact_id: str | None,
    *,
    verified: bool,
    asset_names: tuple[str, ...] = ("input.dat",),
) -> dict[str, Any]:
    return {
        "request": _request(*asset_names),
        "lifecycle_status": "active",
        "superseded_by": None,
        "supersede_reason": None,
        "delivery": {
            "provider": "data" if verified else None,
            "verified": verified,
            "data_terminally_blocked": False,
            **(
                {"input_package_artifact_id": artifact_id}
                if artifact_id is not None
                else {}
            ),
        },
    }


def _state_with_specs(tmp_path, specs: dict[str, dict[str, Any]]) -> State:
    state = State.new("experiment", tmp_path)
    save_input_delivery_ledger(
        state,
        {
            "schema_version": "experiment.input_delivery.v1",
            "run_id": state.run_id,
            "revision": 0,
            "specs": specs,
        },
    )
    return state


def test_consumed_scalar_ignores_an_independent_pending_spec(tmp_path) -> None:
    state = _state_with_specs(
        tmp_path,
        {
            "spec-a": _entry(None, verified=False),
            "spec-b": _entry("dataset__b", verified=True),
        },
    )
    assert _deliver(tmp_path, state, "spec-b", "input.dat", "b") == "dataset__b"

    selected = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__b",
        consumed_only=True,
    )

    assert selected["passed"] is True, selected
    assert selected["applicable"] is True
    assert selected["spec_ids"] == ["spec-b"]
    assert selected["input_package_bindings"] == {"spec-b": "dataset__b"}


def test_selected_spec_remains_atomic_when_one_of_its_assets_is_unverified(
    tmp_path,
) -> None:
    state = _state_with_specs(
        tmp_path,
        {
            "spec-b": _entry(
                None,
                verified=False,
                asset_names=("mesh.msh", "boundary.json"),
            ),
            "spec-c": _entry("dataset__c", verified=True),
        },
    )

    selected = audit_input_delivery_for_execution(
        state,
        input_package_bindings={"spec-b": "dataset__partial_b"},
        consumed_only=True,
    )

    assert selected["passed"] is False
    assert selected["blocking_reasons"] == ["formal_input_delivery_unverified"]
    assert selected["spec_ids"] == ["spec-b"]


def test_scalar_and_multi_binding_select_their_exact_active_subsets(tmp_path) -> None:
    state = _state_with_specs(
        tmp_path,
        {
            "spec-a": _entry("dataset__a", verified=True),
            "spec-b": _entry("dataset__b", verified=True),
            "spec-c": _entry("dataset__c", verified=True),
        },
    )
    for name in ("a", "b", "c"):
        assert _deliver(tmp_path, state, f"spec-{name}", "input.dat", name) == f"dataset__{name}"

    scalar = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__b",
        consumed_only=True,
    )
    multi = audit_input_delivery_for_execution(
        state,
        input_package_bindings={
            "spec-c": "dataset__c",
            "spec-a": "dataset__a",
        },
        consumed_only=True,
    )

    assert scalar["passed"] is True, scalar
    assert scalar["spec_ids"] == ["spec-b"]
    assert multi["passed"] is True, multi
    assert multi["spec_ids"] == ["spec-a", "spec-c"]
    assert multi["providers"] == ["data", "data"]


def test_verified_selection_still_requires_the_dataset_record(tmp_path) -> None:
    state = _state_with_specs(
        tmp_path,
        {"spec-a": _entry("dataset__missing", verified=True)},
    )

    selected = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__missing",
        consumed_only=True,
    )

    assert selected["passed"] is False
    assert selected["blocking_reasons"] == ["selected_dataset_revalidation_failed"]
    assert selected["invalid_spec_ids"] == ["spec-a"]
    # A current absence must not rewrite the old verification into another fact.
    assert state.hook_state["input_delivery_state"]["spec-a"]["verified"] is True


def test_default_audit_still_requires_every_active_spec(tmp_path) -> None:
    state = _state_with_specs(
        tmp_path,
        {
            "spec-a": _entry(None, verified=False),
            "spec-b": _entry("dataset__b", verified=True),
        },
    )

    result = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__b",
    )

    assert result["passed"] is False
    assert result["blocking_reasons"] == ["formal_input_delivery_unverified"]
    assert result["spec_ids"] == ["spec-a"]


def test_no_active_spec_does_not_turn_a_missing_dataset_into_verified_input(tmp_path) -> None:
    state = State.new("experiment", tmp_path)

    result = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__parent_forwarded",
        consumed_only=True,
    )

    # Previously no active spec skipped even existence checking. Explicitly
    # selected ordinary packages now share the structural receiver check.
    assert result["passed"] is False
    assert result["applicable"] is True
    assert result["blocking_reasons"] == ["selected_dataset_structure_invalid"]
    assert result["verification_performed"] is False
    assert result["formal_verification"] == "not_performed"
    assert result.get("verified") is not True


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({}, "input_package_selector_missing"),
        (
            {
                "input_package_artifact_id": "dataset__a",
                "input_package_bindings": {"spec-a": "dataset__a"},
            },
            "input_package_selector_conflict",
        ),
        ({"input_package_artifact_id": ""}, "input_package_artifact_invalid"),
        ({"input_package_artifact_id": 1}, "input_package_artifact_invalid"),
        ({"input_package_bindings": {}}, "input_package_bindings_invalid"),
        (
            {"input_package_bindings": {"spec-a": 1}},
            "input_package_bindings_invalid",
        ),
        (
            {"input_package_bindings": {"unknown-spec": "dataset__a"}},
            "input_package_binding_incomplete",
        ),
        (
            {"input_package_artifact_id": "dataset__unknown"},
            "input_package_artifact_unknown",
        ),
    ],
)
def test_consumed_selection_rejects_invalid_or_unknown_selectors(
    tmp_path,
    kwargs: dict[str, Any],
    reason: str,
) -> None:
    state = _state_with_specs(
        tmp_path,
        {"spec-a": _entry("dataset__a", verified=True)},
    )

    result = audit_input_delivery_for_execution(
        state,
        consumed_only=True,
        **kwargs,
    )

    assert result["passed"] is False
    assert result["applicable"] is True
    assert result["blocking_reasons"] == [reason]


def test_consumed_scalar_rejects_an_artifact_shared_by_multiple_specs(tmp_path) -> None:
    state = _state_with_specs(
        tmp_path,
        {
            "spec-a": _entry("dataset__shared", verified=True),
            "spec-b": _entry("dataset__shared", verified=True),
        },
    )

    result = audit_input_delivery_for_execution(
        state,
        input_package_artifact_id="dataset__shared",
        consumed_only=True,
    )

    assert result["passed"] is False
    assert result["applicable"] is True
    assert result["blocking_reasons"] == ["input_package_binding_ambiguous"]
    assert result["spec_ids"] == ["spec-a", "spec-b"]
