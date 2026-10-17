"""Partial parameter witnesses are disclosure, not a model-facing form gate.

The bounded local processes below prove only the action argument witnesses and
their output lineage.  They deliberately do not claim that arbitrary program
code actually consumed those values.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import (
    delivery_transaction,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)

OUTPUT = "runtime/result.dat"


def _state(tmp_path, expected_params: dict, *, check_only: bool = False) -> State:
    state = State.new("experiment", tmp_path / "runs")
    prereg = state.save_artifact(
        "pre_registration",
        "partial_parameter_witness",
        "Measure the square of the preregistered scalar.",
        metadata={
            "execution_contract": {
                "version": 1,
                "scientific_params": expected_params,
                "runtime_params": {},
            },
        },
    )
    state.mark_frozen(prereg["id"], {"freeze_reason": "partial witness fixture"})
    record = state.read_artifact(prereg["id"])
    obligation = (
        {"id": "scientific_check", "kind": "check", "command": "true", "expect_exit": 0}
        if check_only
        else {"id": "measurement", "kind": "file", "path": OUTPUT}
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Measure the square with the preregistered parameters.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": prereg["id"],
            "version": record["version"],
            "content_hash": record["content_hash"],
        },
        "required_outputs": [obligation],
    }
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _run_action(
    state: State,
    scientific_params: dict,
    *,
    actual_replicates: int = 1,
    write_output: bool = True,
) -> dict:
    if write_output:
        target = working_directory(state) / OUTPUT
        program = (
            f"value = {scientific_params['value']!r}\n"
            f"replicates = {actual_replicates}\n"
            "for _ in range(replicates):\n"
            "    print(value * value)"
        )
        command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
            f"> {shlex.quote(str(target))}"
        )
    else:
        command = "true"
    executed = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd=command,
            timeout=20,
            execution_params={"scientific_params": scientific_params, "runtime_params": {}},
        )
    )
    assert executed["status"] == "success", executed
    starts = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"
    ]
    return starts[-1]


def _finish(state: State, outcome: str = "success") -> dict:
    return asyncio.run(
        finish._finish(
            state,
            outcome=outcome,
            summary="The bounded local calculation and required verification completed.",
        )
    )


def _lineage_entry(state: State, result: dict, action_id: str) -> dict:
    clean = json.loads(state.read_artifact(result["artifact_ids"]["clean_results"])["content"])
    entry, = [
        row
        for row in clean["evaluation"]["execution_lineage"]
        if row["action_id"] == action_id
    ]
    return entry


def _assert_disclosed_as_unverified(result: dict, *, field: str) -> None:
    disclosure = "\n".join(str(item) for item in result["delivery"]["limitations"])
    assert field in disclosure
    assert "witness" in disclosure.lower()
    assert "actual parameter" in disclosure.lower()


def test_missing_expected_parameter_witness_does_not_block_verified_output(tmp_path):
    state = _state(tmp_path, {"value": 7, "replicates": 3})
    action = _run_action(state, {"value": 7}, actual_replicates=3)
    assert (working_directory(state) / OUTPUT).read_text(encoding="utf-8").splitlines() == ["49"] * 3

    result = _finish(state)

    assert result["status"] == "success", result
    assert result["required_satisfied"] == "1/1"
    assert result["contradictions"] == []
    comparison = _lineage_entry(state, result, action["action_id"])["parameter_comparison"]
    assert comparison["passed"] is False
    assert comparison["missing_expected_parameters"] == ["replicates"]
    assert comparison["unexpected_execution_parameters"] == []
    assert comparison["mismatched_parameters"] == []
    _assert_disclosed_as_unverified(result, field="replicates")


def test_unexpected_parameter_witness_does_not_invent_a_verified_deviation(tmp_path):
    state = _state(tmp_path, {"value": 7})
    action = _run_action(state, {"value": 7, "diagnostic_tag": "trial"})
    assert (working_directory(state) / OUTPUT).read_text(encoding="utf-8").strip() == "49"

    result = _finish(state)

    assert result["status"] == "success", result
    assert result["required_satisfied"] == "1/1"
    assert result["contradictions"] == []
    comparison = _lineage_entry(state, result, action["action_id"])["parameter_comparison"]
    assert comparison["passed"] is False
    assert comparison["missing_expected_parameters"] == []
    assert comparison["unexpected_execution_parameters"] == ["diagnostic_tag"]
    assert comparison["mismatched_parameters"] == []
    _assert_disclosed_as_unverified(result, field="diagnostic_tag")


def test_explicit_value_mismatch_still_refutes_success_with_partial_shape(tmp_path):
    state = _state(tmp_path, {"value": 7, "replicates": 3})
    action = _run_action(
        state,
        {"value": -7, "diagnostic_tag": "trial"},
        actual_replicates=3,
    )
    assert (working_directory(state) / OUTPUT).read_text(encoding="utf-8").splitlines() == ["49"] * 3

    rejected = _finish(state)

    assert rejected["status"] == "success_unsupported", rejected
    assert rejected["required_satisfied"] == "1/1"
    conflict, = [
        row
        for row in rejected["contradictions"]
        if row.get("name") == "producer_parameter_claim_conflict"
    ]
    comparison = conflict["comparison"]
    assert comparison["missing_expected_parameters"] == ["replicates"]
    assert comparison["unexpected_execution_parameters"] == ["diagnostic_tag"]
    assert comparison["mismatched_parameters"] == [
        {"parameter": "value", "expected": 7, "actual": -7},
    ]
    assert conflict["action"] == action["action_id"]
    assert delivery_transaction.read_manifest(state) is None

    honest = _finish(state, "partial")
    assert honest["status"] == "success" and honest["outcome"] == "partial", honest


def test_check_only_missing_witness_is_not_an_unresolved_parameter_conflict(tmp_path):
    state = _state(tmp_path, {"value": 7, "replicates": 3}, check_only=True)
    _run_action(state, {"value": 7}, write_output=False)

    result = _finish(state)

    assert result["status"] == "success", result
    assert result["required_satisfied"] == "1/1"
    assert not any(
        row.get("name") == "parameter_lineage_unresolved"
        for row in result["delivery"]["gaps"]
    )
    _assert_disclosed_as_unverified(result, field="replicates")


@pytest.mark.parametrize(
    (
        "same_binding",
        "missing",
        "unexpected",
        "mismatched",
        "expected_gap",
        "disclosed_fields",
    ),
    [
        pytest.param(
            True,
            ["replicates"],
            ["diagnostic_tag"],
            [{"parameter": "value", "expected": 7, "actual": -7}],
            True,
            ["replicates", "diagnostic_tag"],
            id="same-binding-mixed-value-conflict",
        ),
        pytest.param(
            False,
            ["replicates"],
            ["diagnostic_tag"],
            [{"parameter": "value", "expected": 7, "actual": -7}],
            False,
            [],
            id="different-binding-is-not-current-a",
        ),
        pytest.param(
            True,
            [],
            ["diagnostic_tag"],
            [],
            False,
            ["diagnostic_tag"],
            id="check-only-extra-witness-is-disclosure",
        ),
    ],
)
def test_check_only_consumer_distinguishes_value_conflicts_from_partial_witnesses(
    same_binding,
    missing,
    unexpected,
    mismatched,
    expected_gap,
    disclosed_fields,
):
    binding = {
        "artifact_id": "prereg-current",
        "version": 1,
        "content_hash": "a" * 64,
    }
    context_binding = (
        binding
        if same_binding
        else {
            "artifact_id": "prereg-other",
            "version": 1,
            "content_hash": "b" * 64,
        }
    )
    comparison = {
        "passed": False,
        "missing_expected_parameters": missing,
        "unexpected_execution_parameters": unexpected,
        "mismatched_parameters": mismatched,
    }
    records = [
        {
            "kind": "action_started",
            "action_id": "check-action",
            "execution_context": {
                "prereg_binding": context_binding,
                "parameter_source": "tool_argument_witness",
                "parameter_comparison": comparison,
            },
        },
    ]
    evaluation = {
        "checks": [
            {
                "name": "scientific_check",
                "required": True,
                "evidence": {"producers": []},
            },
        ],
    }

    lineage, gaps, contradictions, limitations = finish._execution_lineage_findings(
        {
            "scope": "scientific",
            "prereg_status": "bound_valid",
            "prereg": binding,
        },
        records,
        evaluation,
    )

    assert lineage == []
    assert contradictions == []
    unresolved = [row for row in gaps if row.get("name") == "parameter_lineage_unresolved"]
    assert bool(unresolved) is expected_gap
    if expected_gap:
        assert unresolved[0]["candidates"] == [
            {"action": "check-action", "comparison": comparison},
        ]
    disclosure = "\n".join(limitations)
    for field in disclosed_fields:
        assert field in disclosure
    if not same_binding:
        assert "replicates" not in disclosure
        assert "diagnostic_tag" not in disclosure
