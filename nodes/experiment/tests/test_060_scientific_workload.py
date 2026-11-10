"""A scientific fixture verifies observed workload through the existing finish checks."""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import contract_audit, finish, run_bootstrap, safe_bash

# The bounded producer and independent verifier are fixture domain code.
# The production harness has no knowledge of these dimensions or token units.
_PRODUCE = """
import itertools
import json
import sys

config, fault = json.loads(sys.argv[1]), sys.argv[2]
shape = config["shape"].copy()
if fault in {"variants", "rounds"}:
    shape[fault] -= 1
episodes = [
    {"key": key, "calls": [1, 1, 1]}
    for key in itertools.product(*(range(size) for size in shape.values()))
]
print(json.dumps({"source": "fake" if fault == "fake" else "measured",
                  "episodes": episodes}))
"""

_VERIFY = """
import itertools
import json
import sys
from pathlib import Path

expected = json.loads(sys.argv[1])
output = json.loads(Path("runtime/result.json").read_text())
episodes = output["episodes"]
keys = {tuple(row["key"]) for row in episodes}
matrix = set(itertools.product(*(range(size) for size in expected["shape"].values())))
assert output["source"] == "measured", "fixture data"
assert keys == matrix and len(keys) == len(episodes), "incomplete episode coverage"
assert sum(sum(row["calls"]) for row in episodes) <= expected["tokens"], "token cap exceeded"
"""


def _argv(program, *args):
    return [sys.executable, "-c", program, *args]


@pytest.mark.parametrize(
    ("fault", "expected_failure"),
    [
        ("none", None),
        ("budget", "producer_parameter_claim_conflict"),
        ("variants", "check_failed"),
        ("rounds", "check_failed"),
        ("fake", "check_failed"),
        ("unattributed", "unattributed"),
    ],
)
def test_workload_acceptance_uses_actual_evidence(tmp_path, fault, expected_failure):
    state = State.new("experiment", tmp_path / "runs")
    params = {
        "shape": {"variants": 2, "seeds": 2, "rounds": 2, "candidates": 2, "instances": 3},
        "tokens": 144,
    }
    prereg = state.save_artifact(
        "pre_registration", "bounded measurement", "Measure the full matrix.",
        metadata={"execution_contract": {"version": 1, "scientific_params": params}},
    )
    state.mark_frozen(prereg["id"], {"freeze_reason": "workload fixture"})
    frozen = state.read_artifact(prereg["id"])
    expected = json.dumps(frozen["metadata"]["execution_contract"]["scientific_params"])
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Record and verify the complete measured workload.",
        "prereg_assignment": {
            "kind": "bound", "artifact_id": prereg["id"],
            "version": frozen["version"], "content_hash": frozen["content_hash"],
        },
        "required_outputs": [
            {"id": "measurement", "kind": "file", "path": "runtime/result.json"},
            {"id": "coverage", "kind": "check", "command": shlex.join(_argv(_VERIFY, expected))},
        ],
    }
    asyncio.run(run_bootstrap.establish(state))
    actual = {**params, "tokens": 72} if fault == "budget" else params
    argv = _argv(_PRODUCE, json.dumps(actual), fault)
    output = working_directory(state) / "runtime" / "result.json"
    if fault == "unattributed":
        # A generic transport receipt and a valid-looking file lack a
        # matching Experiment B producer observation.
        with output.open("w") as target:
            subprocess.run(argv, stdout=target, check=True, timeout=10)
        state.append_transcript("remote_resource_operation", action="submit",
                                job_id="fixture-job", remote_state="succeeded")
    else:
        execution = asyncio.run(safe_bash._safe_run_bash(
            state, cmd=f"{shlex.join(argv)} > {shlex.quote(str(output))}", timeout=20,
            execution_params={"scientific_params": actual},
        ))
        assert execution["status"] == "success", execution

    result = asyncio.run(finish._finish(state, outcome="success", summary="Measured the workload."))
    if expected_failure is None:
        assert result["status"] == "success", result
        assert contract_audit.audit_experiment_delivery(state)["passed"]
        measured = json.loads(output.read_text())["episodes"]
        assert len(measured) == 48 and sum(len(row["calls"]) for row in measured) == 144
    else:
        assert result["status"] == "success_unsupported", result
        failures = {row.get("name") for row in result["contradictions"]}
        failures.update(row.get("gap") for row in result["gaps"])
        assert expected_failure in failures
        honest = asyncio.run(finish._finish(
            state, outcome="partial", summary="Retain the incomplete evidence.",
        ))
        assert honest["status"] == "success" and honest["outcome"] == "partial", honest
        assert contract_audit.audit_experiment_delivery(state)["passed"]
