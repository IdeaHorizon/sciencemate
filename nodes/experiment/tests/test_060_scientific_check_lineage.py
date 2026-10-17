"""Check-only scientific delivery must not lose known parameter conflicts."""
from __future__ import annotations

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import (
    contract_audit,
    delivery_transaction,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)


_UNOBSERVED = object()


def _state(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    prereg = state.save_artifact(
        "pre_registration",
        "check_only_scalar",
        "Run the check with value=7.",
        metadata={
            "execution_contract": {
                "version": 1,
                "scientific_params": {"value": 7},
                "runtime_params": {},
            },
        },
    )
    state.mark_frozen(prereg["id"], {"freeze_reason": "check-only lineage fixture"})
    record = state.read_artifact(prereg["id"])
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run the preregistered check-only scientific task.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": prereg["id"],
            "version": record["version"],
            "content_hash": record["content_hash"],
        },
        "required_outputs": [
            {"id": "scientific_check", "kind": "check", "command": "true", "expect_exit": 0},
        ],
    }
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _run_action(state, value=_UNOBSERVED):
    kwargs = {}
    if value is not _UNOBSERVED:
        kwargs["execution_params"] = {
            "scientific_params": {"value": value},
            "runtime_params": {},
        }
    result = asyncio.run(safe_bash._safe_run_bash(state, cmd="true", timeout=20, **kwargs))
    assert result["status"] == "success", result
    starts = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"
    ]
    return starts[-1]


def _finish(state, outcome="success"):
    return asyncio.run(
        finish._finish(
            state,
            outcome=outcome,
            summary="The preregistered check-only task completed.",
        )
    )


def test_check_only_known_parameter_conflict_cannot_freeze_success_but_partial_closes(tmp_path):
    state = _state(tmp_path)
    conflict = _run_action(state, -7)

    rejected = _finish(state)

    assert rejected["status"] == "success_unsupported", rejected
    gap, = [row for row in rejected["gaps"] if row.get("name") == "parameter_lineage_unresolved"]
    assert [row["action"] for row in gap["candidates"]] == [conflict["action_id"]]
    assert not any(row.get("name") == "producer_parameter_claim_conflict"
                   for row in rejected["contradictions"])
    assert delivery_transaction.read_manifest(state) is None

    honest = _finish(state, "partial")

    assert honest["status"] == "success" and honest["outcome"] == "partial", honest
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    for artifact_type in ("raw_results", "clean_results", "experiment_log"):
        artifact = state.read_artifact(honest["artifact_ids"][artifact_type])
        assert artifact["metadata"]["frozen"] is True


@pytest.mark.parametrize("value", [7, _UNOBSERVED], ids=["matching-witness", "not-observed"])
def test_check_only_without_a_known_conflict_stays_supported_and_discloses_limits(tmp_path, value):
    state = _state(tmp_path)
    action = _run_action(state, value)

    result = _finish(state)

    assert result["status"] == "success", result
    assert not any(row.get("name") == "parameter_lineage_unresolved"
                   for row in result["delivery"]["gaps"])
    assert any("No result-producer lineage" in item
               for item in result["delivery"]["limitations"])
    source = action["execution_context"]["parameter_source"]
    assert source == ("not_observed" if value is _UNOBSERVED else "tool_argument_witness")


def test_unrelated_matching_action_cannot_clear_check_only_unresolved_conflict(tmp_path):
    state = _state(tmp_path)
    conflict = _run_action(state, -7)
    _run_action(state, 7)

    result = _finish(state)

    assert result["status"] == "success_unsupported", result
    gap, = [row for row in result["gaps"] if row.get("name") == "parameter_lineage_unresolved"]
    assert [row["action"] for row in gap["candidates"]] == [conflict["action_id"]]
    assert delivery_transaction.read_manifest(state) is None
