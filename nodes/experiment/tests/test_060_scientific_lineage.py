"""Scientific delivery compares only the actual result producers' witnesses.

These are real bounded local processes, not proof of Slurm execution or of
arbitrary programs' actual parameter use. The symmetric square results make
the parameter contradiction independent of the output-value check.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, run_bootstrap, safe_bash


def _state(tmp_path, *, glob=False):
    state = State.new("experiment", tmp_path / "runs")
    prereg = state.save_artifact("pre_registration", "square", "square value=7", metadata={
        "execution_contract": {
            "version": 1, "scientific_params": {"value": 7}, "runtime_params": {},
        },
    })
    state.mark_frozen(prereg["id"], {"freeze_reason": "task fixture"})
    record = state.read_artifact(prereg["id"])
    binding = {"kind": "bound", "artifact_id": prereg["id"],
               "version": record["version"], "content_hash": record["content_hash"]}
    obligation = ({"id": "measurements", "kind": "glob", "pattern": "runtime/*.dat"}
                  if glob else {"id": "measurement", "kind": "file", "path": "runtime/result.dat"})
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Measure the square of the preregistered scalar.",
        "prereg_assignment": binding, "required_outputs": [obligation],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state


def _square(state, value, *, filename="result.dat", witnessed=True, replace=False):
    program = f"value = {value}; print(value * value)"
    target = working_directory(state) / "runtime" / filename
    output = target.with_suffix(".new") if replace else target
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} > {shlex.quote(str(output))}"
    if replace:
        command += f" && mv {shlex.quote(str(output))} {shlex.quote(str(target))}"
    result = asyncio.run(safe_bash._safe_run_bash(
        state, cmd=command,
        timeout=20,
        execution_params=({"scientific_params": {"value": value}, "runtime_params": {}}
                          if witnessed else None),
    ))
    assert result["status"] == "success", result
    return target


def _finish(state):
    return asyncio.run(finish._finish(state, outcome="success", summary="Measured and retained the square."))


def test_unrelated_preparation_mismatch_does_not_poison_scientific_delivery(tmp_path):
    state = _state(tmp_path)
    _square(state, -7, filename="diagnostic.txt")
    _square(state, 7)
    result = _finish(state)
    assert result["status"] == "success", result
    assert not result["contradictions"]


def test_matching_unrelated_action_cannot_clear_the_result_producers_mismatch(tmp_path):
    state = _state(tmp_path)
    _square(state, -7)
    _square(state, 7, filename="diagnostic.txt")
    result = _finish(state)
    assert result["status"] == "success_unsupported", result
    assert any(c["name"] == "producer_parameter_claim_conflict" for c in result["contradictions"])
    assert delivery_transaction.read_manifest(state) is None
    honest = asyncio.run(finish._finish(state, outcome="partial", summary="The result used a different declared input."))
    assert honest["status"] == "success" and honest["outcome"] == "partial", honest


def test_independent_output_identity_replaces_the_old_parameter_witness(tmp_path):
    state = _state(tmp_path)
    target = _square(state, -7)
    before = target.stat().st_mtime_ns
    rejected = _finish(state)
    assert rejected["status"] == "success_unsupported", rejected
    _square(state, 7, replace=True)
    assert target.read_text().strip() == "49" and target.stat().st_mtime_ns != before
    result = _finish(state)
    assert result["status"] == "success", result
    starts = [r for r in ledger.open_ledger(state).records()
              if r.get("kind") == "action_started" and r.get("tool") == "safe_run_bash"]
    clean = json.loads(state.read_artifact(result["artifact_ids"]["clean_results"])["content"])
    check, = [row for row in clean["evaluation"]["checks"] if row["name"] == "measurement"]
    assert check["evidence"]["producers"] == [starts[-1]["action_id"]]


def test_glob_uses_the_same_producer_parameter_comparison(tmp_path):
    state = _state(tmp_path, glob=True)
    _square(state, -7)
    result = _finish(state)
    assert result["status"] == "success_unsupported", result
    assert any(c["name"] == "producer_parameter_claim_conflict" for c in result["contradictions"])


def test_absent_parameter_witness_is_disclosed_not_invented_or_a_new_gate(tmp_path):
    state = _state(tmp_path)
    _square(state, 7, witnessed=False)
    result = _finish(state)
    assert result["status"] == "success", result
    clean = json.loads(state.read_artifact(result["artifact_ids"]["clean_results"])["content"])
    entry, = clean["evaluation"]["execution_lineage"]
    assert entry["parameter_source"] == "not_observed"
    assert entry["parameter_comparison"]["passed"] is None
    assert result["delivery"]["limitations"]
