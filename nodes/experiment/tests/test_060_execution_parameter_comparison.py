"""Pure comparison tests for frozen preregistration execution parameters."""
from __future__ import annotations

from copy import deepcopy

import pytest

from nodes.experiment.tools.preflight import compare_execution_parameters


def _frozen_metadata() -> dict:
    return {
        "execution_contract": {
            "version": 1,
            "scientific_params": {
                "grid": {"nx": 50, "ny": 40},
                "reynolds": [100, 400],
            },
            "runtime_params": {"threads": 4},
        },
        # A structured contract is authoritative over this legacy projection.
        "expected_params": {"stale": True},
    }


def _execution_params() -> dict:
    return {
        "scientific_params": {
            "grid": {"nx": 50, "ny": 40},
            "reynolds": [100, 400],
        },
        # Runtime declarations are retained as context, not compared as
        # scientific commitments.
        "runtime_params": {"threads": 8},
    }


def test_exact_structured_parameters_match_without_mutating_inputs():
    contract = _frozen_metadata()
    execution_params = _execution_params()
    original_contract = deepcopy(contract)
    original_execution_params = deepcopy(execution_params)

    result = compare_execution_parameters(contract, execution_params)

    assert result["passed"] is True
    assert result["blocking_reasons"] == []
    assert result["expected_params"] == contract["execution_contract"]["scientific_params"]
    assert result["scientific_params"] == execution_params["scientific_params"]
    assert result["execution_params"] == execution_params
    assert contract == original_contract
    assert execution_params == original_execution_params


def test_missing_scientific_parameters_are_reported_by_flattened_path():
    result = compare_execution_parameters(
        _frozen_metadata(),
        {
            "scientific_params": {
                "grid": {"nx": 50},
                "reynolds": [100, 400],
            },
        },
    )

    assert result["passed"] is False
    assert result["missing_expected_parameters"] == ["grid.ny"]
    assert result["blocking_reasons"] == ["execution_params_missing_fields"]


def test_unregistered_scientific_parameters_are_reported():
    execution_params = _execution_params()
    execution_params["scientific_params"]["temperature_k"] = 300

    result = compare_execution_parameters(_frozen_metadata(), execution_params)

    assert result["passed"] is False
    assert result["unexpected_execution_parameters"] == ["temperature_k"]
    assert result["blocking_reasons"] == [
        "execution_params_unregistered_fields",
    ]


def test_mismatched_scientific_parameters_preserve_expected_and_actual():
    execution_params = _execution_params()
    execution_params["scientific_params"]["reynolds"] = [100, 3200]

    result = compare_execution_parameters(_frozen_metadata(), execution_params)

    assert result["passed"] is False
    assert result["mismatched_parameters"] == [
        {
            "parameter": "reynolds",
            "expected": [100, 400],
            "actual": [100, 3200],
        },
    ]
    assert result["blocking_reasons"] == ["execution_params_mismatch"]


def test_caller_cannot_declare_framework_derived_parameters():
    execution_params = _execution_params()
    execution_params["derived_params"] = {"host": "caller-selected"}

    result = compare_execution_parameters(_frozen_metadata(), execution_params)

    assert result["passed"] is False
    assert result["blocking_reasons"] == ["derived_params_forbidden"]


@pytest.mark.parametrize(
    "contract",
    [
        {
            "execution_contract": {
                "version": 2,
                "scientific_params": {"grid": 50},
                "runtime_params": {},
            },
        },
        {
            "execution_contract": {
                "version": 1,
                "scientific_params": ["grid"],
                "runtime_params": {},
            },
        },
        {
            "execution_contract": {
                "version": 1,
                "scientific_params": {"grid": 50},
                "runtime_params": [],
            },
        },
        {
            "execution_contract_valid": False,
            "expected_params": {"grid": 50},
        },
    ],
)
def test_invalid_contract_is_rejected(contract):
    result = compare_execution_parameters(
        contract,
        {"scientific_params": {"grid": 50}},
    )

    assert result["passed"] is False
    assert result["blocking_reasons"] == ["execution_contract_invalid"]


def test_legacy_normalized_expected_params_remain_compatible():
    result = compare_execution_parameters(
        {
            "execution_contract_valid": True,
            "expected_params": {"grid": {"nx": 50}},
        },
        {"grid": {"nx": 50}},
    )

    assert result["passed"] is True
    assert result["scientific_params"] == {"grid": {"nx": 50}}
