"""Real action evidence retains its input/parameter witness, not a run cache."""
from __future__ import annotations

import asyncio
import json

from core.state import State
from nodes.experiment.tools import ledger, resource_manager, run_bootstrap, safe_bash


def _state(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    prereg = state.save_artifact("pre_registration", "measurement", "measure a fixed scalar", metadata={
        "execution_contract": {
            "version": 1, "scientific_params": {"value": 7}, "runtime_params": {},
        },
    })
    state.mark_frozen(prereg["id"], {"freeze_reason": "test task authority"})
    record = state.read_artifact(prereg["id"])
    binding = {key: record[key] for key in ("version", "content_hash")}
    binding["artifact_id"] = prereg["id"]
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Measure the fixed scalar and retain the actual action evidence.",
        "prereg_assignment": {"kind": "bound", **binding},
        "required_outputs": [],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state, binding


def _execute(state, value, filename):
    params = {"scientific_params": {"value": value}, "runtime_params": {}}
    result = asyncio.run(safe_bash._safe_run_bash(
        state, cmd=f"printf '%s\\n' {value} > {filename}", timeout=20,
        execution_params=params,
    ))
    assert result["status"] == "success", result
    return params


def _starts(state):
    return [row for row in ledger.open_ledger(state).records()
            if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]


def test_real_action_context_records_exact_prereg_and_parameter_witness(tmp_path):
    state, binding = _state(tmp_path)
    params = _execute(state, 7, "measurement.txt")
    started, = _starts(state)
    context = started["execution_context"]
    assert context["prereg_binding"] == binding
    assert context["execution_params"] == params
    assert context["parameter_source"] == "tool_argument_witness"
    assert context["parameter_comparison"]["passed"] is True
    assert context["input_verification"]["status"] == "not_selected"
    assert ledger.project_actions(ledger.open_ledger(state).records())[0]["execution_context"] == context


def test_preparation_without_parameter_claim_is_not_a_fake_scientific_match(tmp_path):
    state, binding = _state(tmp_path)
    result = asyncio.run(safe_bash._safe_run_bash(state, cmd="printf ready > environment.txt", timeout=20))
    assert result["status"] == "success", result
    context = _starts(state)[0]["execution_context"]
    assert context["prereg_binding"] == binding
    assert context["execution_params"] is None
    assert context["parameter_comparison"]["applicable"] is False
    assert context["parameter_comparison"]["passed"] is None


def test_context_uses_frozen_commitment_not_mutable_legacy_contract(tmp_path):
    state, _ = _state(tmp_path)
    state.hook_state["run_contract"] = {
        "execution_contract": {"version": 1, "scientific_params": {"value": 8}, "runtime_params": {}},
        "expected_params": {"value": 8},
    }
    # No explicit formal package is consumed here. Parameter arguments remain
    # witnesses, not a hidden input selector or permission to change the task.
    _execute(state, 8, "diagnostic.txt")
    comparison = _starts(state)[0]["execution_context"]["parameter_comparison"]
    assert comparison["passed"] is False
    assert comparison["mismatched_parameters"] == [{"parameter": "value", "expected": 7, "actual": 8}]


def test_each_action_context_survives_cache_loss_without_cross_action_merge(tmp_path):
    state, _ = _state(tmp_path)
    first = _execute(state, 7, "first.txt")
    second = _execute(state, 8, "second.txt")
    before = json.loads(json.dumps(_starts(state)))
    state.hook_state.clear()
    rows = _starts(state)
    assert rows == before
    assert [row["execution_context"]["execution_params"] for row in rows] == [first, second]
    assert len({row["action_id"] for row in rows}) == 2


def test_python_physical_entry_persists_the_same_parameter_witness(tmp_path):
    state, binding = _state(tmp_path)
    params = {"scientific_params": {"value": 7}, "runtime_params": {}}
    result = asyncio.run(safe_bash._safe_execute_python(
        state, code="print(7)", timeout=20, execution_params=params,
    ))
    assert result["status"] == "success", result
    started, = [row for row in ledger.open_ledger(state).records()
                if row.get("kind") == "action_started" and row.get("tool") == "safe_execute_python"]
    context = started["execution_context"]
    assert context["prereg_binding"] == binding
    assert context["execution_params"] == params
    assert context["parameter_comparison"]["passed"] is True


def test_job_context_is_durable_before_adapter_call_without_a_real_submission(tmp_path, monkeypatch):
    state, binding = _state(tmp_path)
    params = {"scientific_params": {"value": 7}, "runtime_params": {}}
    seen = []

    def uncertain_scheduler(_args, timeout=10):
        seen.extend(row for row in ledger.open_ledger(state).records() if row.get("kind") == "intent")
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "fixture transport outcome unavailable"}

    monkeypatch.setattr(resource_manager, "_run", uncertain_scheduler)
    result = resource_manager._submit_sync(
        runtime_root=tmp_path / "runtime", scheduler="slurm", command="printf 7", job_name="context",
        mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0, walltime_minutes=1,
        queue=None, nodelist=None, image=None, workdir=str(tmp_path), dry_run=False,
        namespace=None, stage_in=None, state=state, execution_params=params,
    )
    assert len(seen) == 1, result
    assert seen[0]["execution_context"]["prereg_binding"] == binding
    assert seen[0]["execution_context"]["execution_params"] == params
    assert seen[0]["execution_context"]["parameter_comparison"]["passed"] is True
    # The simulated adapter gave no proof of absence. Preserve unknown rather
    # than claiming cancellation/absence just because its call returned nonzero.
    jobs = ledger.open_jobs(ledger.open_ledger(state).records())
    assert len(jobs) == 1 and jobs[0]["state"] == "unknown", result


def test_missing_selected_package_cannot_create_an_execution_context(tmp_path):
    state, _ = _state(tmp_path)
    result = asyncio.run(safe_bash._safe_run_bash(
        state, cmd="printf observed", timeout=20,
        input_package_artifact_id="dataset__without_formal_delivery_record",
    ))
    assert result["status"] == "error", result
    assert result["payload_spawned"] is False
    assert result["audit"]["formal_verification"] == "not_performed"
    assert _starts(state) == []
