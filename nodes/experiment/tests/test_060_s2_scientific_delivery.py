"""S2-min local scientific execution uses the shared delivery transaction.

The two positive cases fail on a7c7eaef at its scientific-only blanket gate.
These real local processes do not establish Slurm or model-driven UI support.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import (
    contract_audit,
    delivery_transaction,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)
from nodes.experiment.tools.path_roles import collect_path_roles


OUTPUT_RELATIVE = "runtime/sample_mean.txt"
EXPECTED_RESULT = "sample_mean=3.750000"
BOUND_OUTPUT_RELATIVE = "runtime/preregistered_square.txt"
BOUND_EXPECTED_RESULT = "squared_value=49"
_OMITTED = object()


def _run(awaitable):
    return asyncio.run(awaitable)


def _roots(state: State) -> dict[str, Path]:
    return {
        role.role: Path(role.path)
        for role in collect_path_roles(state)
        if role.writable and not role.container_only
    }


def _node_inputs(
    *,
    prereg_assignment=_OMITTED,
    output_relative: str = OUTPUT_RELATIVE,
    expected_result: str = EXPECTED_RESULT,
    experiment_spec: str = (
        "Explore the arithmetic mean of the task-embedded sample "
        "[1.0, 2.0, 4.0, 8.0]."
    ),
) -> dict:
    inputs = {
        "scope": "scientific",
        "experiment_spec": experiment_spec,
        "required_outputs": [
            {
                "id": "sample_mean_file",
                "kind": "file",
                "path": output_relative,
            },
            {
                "id": "sample_mean_check",
                "kind": "check",
                "command": (
                    "grep -Fx "
                    f"{shlex.quote(expected_result)} "
                    f"{shlex.quote(output_relative)}"
                ),
                "expect_exit": 0,
                "expect_stdout_contains": expected_result,
            },
        ],
    }
    if prereg_assignment is not _OMITTED:
        inputs["prereg_assignment"] = prereg_assignment
    return inputs


def _state(tmp_path: Path, *, prereg_assignment=_OMITTED) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="s2-scientific-delivery",
        session_id="owner",
    )
    inputs = _node_inputs(prereg_assignment=prereg_assignment)
    state.hook_state["node_inputs"] = inputs
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _bound_state(tmp_path: Path) -> tuple[State, dict, dict]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="s2-bound-scientific-delivery",
        session_id="owner",
    )
    execution_contract = {
        "version": 1,
        "scientific_params": {"value": 7},
        "runtime_params": {},
    }
    saved = state.save_artifact(
        "pre_registration",
        "prereg__s2_bound_square",
        "# Preregistered square calculation\n",
        metadata={"execution_contract": execution_contract},
    )
    state.mark_frozen(saved["id"], {"freeze_reason": "S2 exact-binding fixture"})
    record = state.read_artifact(saved["id"])
    assert isinstance(record, dict), saved
    binding = {
        "kind": "bound",
        "artifact_id": saved["id"],
        "version": int(record["version"]),
        "content_hash": record["content_hash"],
    }
    state.hook_state["node_inputs"] = _node_inputs(
        prereg_assignment=binding,
        output_relative=BOUND_OUTPUT_RELATIVE,
        expected_result=BOUND_EXPECTED_RESULT,
        experiment_spec="Run the preregistered value=7 square calculation.",
    )
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state, execution_contract, binding


def _experiment_action_starts(state: State) -> list[dict]:
    return [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started"
        and row.get("effect_class") == "experiment"
    ]


def _assert_frozen_delivery(state: State, result: dict, outcome: str) -> None:
    for artifact_type in ("raw_results", "clean_results", "experiment_log"):
        record = state.read_artifact(result["artifact_ids"][artifact_type])
        assert record is not None
        assert record["metadata"]["frozen"] is True
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True and audit["outcome"] == outcome, audit


def _mean_command(target: Path) -> str:
    program = (
        "from statistics import mean; "
        "sample = [1.0, 2.0, 4.0, 8.0]; "
        "print(f'sample_mean={mean(sample):.6f}')"
    )
    return (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
        f"> {shlex.quote(str(target))}"
    )


def test_scientific_exploration_without_formal_input_can_deliver_success(tmp_path: Path):
    state = _state(
        tmp_path,
        prereg_assignment={
            "kind": "none",
            "reason": "This exploratory task has no governing preregistration.",
        },
    )
    target = working_directory(state) / OUTPUT_RELATIVE
    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=_mean_command(target),
            cwd=str(_roots(state)["run_root"]),
            timeout=30,
        )
    )

    assert executed.get("status") == "success", executed
    assert executed.get("returncode") == 0, executed
    assert target.read_text(encoding="utf-8").strip() == EXPECTED_RESULT
    actions = _experiment_action_starts(state)
    assert len(actions) == 1 and actions[0]["tool"] == "safe_run_bash", actions
    observation = next(
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "observation"
        and row.get("action_id") == actions[0]["action_id"]
    )
    assert str(target) in set(observation.get("created") or []) | set(
        observation.get("modified") or []
    )
    assert (observation.get("identities") or {})[str(target)]["after"]["kind"] == "file"

    first = _run(
        finish._finish(
            state,
            outcome="success",
            summary="The task-embedded sample mean was computed and mechanically checked.",
            deliverables=[
                {"path": OUTPUT_RELATIVE, "obligation_id": "sample_mean_file"},
            ],
        )
    )

    # This is the deliberate base-red assertion for S2-min: current S1d
    # returns scientific_success_not_supported despite complete A/B evidence.
    assert first["status"] == "success", first
    assert first["required_satisfied"] == "2/2"
    delivery = first["delivery"]
    assert delivery["scope"] == "scientific"
    assert delivery["formal_input"] == {
        "status": "none",
        "binding": None,
        "error": None,
    }
    assert delivery["execution_status"] == "attempted"
    assert delivery["result_status"] == "delivered"
    assert delivery["gaps"] == [] and delivery["contradictions"] == []
    _assert_frozen_delivery(state, first, "success")

    identity = (target.stat().st_mtime_ns, target.read_bytes())
    rows_before = ledger.open_ledger(state).records()
    replay = _run(
        finish._finish(
            state,
            outcome="success",
            summary="The task-embedded sample mean was computed and mechanically checked.",
            deliverables=[
                {"path": OUTPUT_RELATIVE, "obligation_id": "sample_mean_file"},
            ],
        )
    )
    assert replay["status"] == "success" and replay["reused_manifest"] is True
    assert replay["artifact_ids"] == first["artifact_ids"]
    assert (target.stat().st_mtime_ns, target.read_bytes()) == identity
    assert len(_experiment_action_starts(state)) == 1
    assert ledger.open_ledger(state).records() == rows_before


def test_exact_bound_prereg_with_matching_execution_params_can_deliver_success(
    tmp_path: Path,
):
    state, execution_contract, binding = _bound_state(tmp_path)
    target = working_directory(state) / BOUND_OUTPUT_RELATIVE
    program = "value = 7; print(f'squared_value={value * value}')"
    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
                f"> {shlex.quote(str(target))}"
            ),
            cwd=str(_roots(state)["run_root"]),
            timeout=30,
            stage="simulation",
            execution_params={key: execution_contract[key] for key in ("scientific_params", "runtime_params")},
        )
    )

    assert executed.get("status") == "success", executed
    assert executed.get("returncode") == 0, executed
    assert target.read_text(encoding="utf-8").strip() == BOUND_EXPECTED_RESULT
    actions = _experiment_action_starts(state)
    assert len(actions) == 1 and actions[0]["tool"] == "safe_run_bash", actions
    assert actions[0]["execution_context"]["parameter_comparison"]["passed"] is True

    result = _run(
        finish._finish(
            state,
            outcome="success",
            summary="The exact preregistered calculation ran with matching parameters.",
            deliverables=[
                {"path": BOUND_OUTPUT_RELATIVE, "obligation_id": "sample_mean_file"},
            ],
        )
    )

    # Like the formal-none positive case, this remains a deterministic S2
    # base-red until the shared scientific finish predicate is implemented.
    assert result["status"] == "success", result
    assert result["required_satisfied"] == "2/2"
    formal = result["delivery"]["formal_input"]
    assert formal == {
        "status": "valid",
        "binding": {
            key: binding[key]
            for key in ("artifact_id", "version", "content_hash")
        },
        "error": None,
    }
    assert result["delivery"]["scope"] == "scientific"
    assert result["delivery"]["execution_status"] == "attempted"
    assert result["delivery"]["result_status"] == "delivered"
    assert result["delivery"]["gaps"] == []
    assert result["delivery"]["contradictions"] == []
    _assert_frozen_delivery(state, result, "success")


def test_failed_scientific_action_cannot_claim_success_but_replays_honest_failure(
    tmp_path: Path,
):
    state = _state(
        tmp_path,
        prereg_assignment={
            "kind": "none",
            "reason": "This exploratory task has no governing preregistration.",
        },
    )
    target = working_directory(state) / OUTPUT_RELATIVE
    program = (
        "import sys; "
        "print('the exploratory calculation failed', file=sys.stderr); "
        "raise SystemExit(7)"
    )
    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
            cwd=str(_roots(state)["run_root"]),
            timeout=30,
        )
    )
    assert executed.get("returncode") == 7, executed
    assert not target.exists()

    unsupported = _run(
        finish._finish(
            state,
            outcome="success",
            summary="A nonzero action with no result must not be called success.",
        )
    )
    assert unsupported.get("status") != "success", unsupported
    assert delivery_transaction.read_manifest(state) is None
    assert not state.list_artifacts("experiment_log", own_only=True)
    assert unsupported.get("status") == "success_unsupported"
    assert {gap["obligation"] for gap in unsupported["gaps"]} >= {
        "sample_mean_file",
        "sample_mean_check",
    }
    # A missing required result/check is sufficient evidence against success;
    # no output means no producer identity to fabricate for this failed action.
    assert any(row.get("kind") == "action_settled" and row.get("exit_code") == 7
               for row in ledger.open_ledger(state).records())

    failed = _run(
        finish._finish(
            state,
            outcome="failed",
            summary="The exploratory calculation exited 7 and produced no result.",
            next_step="Correct the calculation before starting a new action.",
        )
    )
    assert failed["status"] == "success" and failed["outcome"] == "failed", failed
    delivery = failed["delivery"]
    assert delivery["scope"] == "scientific"
    assert delivery["execution_status"] == "attempted"
    assert delivery["result_status"] == "unavailable"
    assert {gap["obligation"] for gap in delivery["gaps"]} >= {
        "sample_mean_file",
        "sample_mean_check",
    }
    assert any("Historical failed actions" in item for item in delivery["limitations"])
    _assert_frozen_delivery(state, failed, "failed")

    rows_before = ledger.open_ledger(state).records()
    replay = _run(
        finish._finish(
            state,
            outcome="failed",
            summary="The exploratory calculation exited 7 and produced no result.",
            next_step="Correct the calculation before starting a new action.",
        )
    )
    assert replay["status"] == "success" and replay["reused_manifest"] is True
    assert replay["delivery"]["outcome"] == "failed"
    assert replay["delivery"]["summary"] == failed["delivery"]["summary"]
    assert replay["artifact_ids"] == failed["artifact_ids"]
    assert ledger.open_ledger(state).records() == rows_before


@pytest.mark.parametrize("input_case", ("pending", "omitted"))
def test_unavailable_formal_input_can_close_blocked_without_inventing_execution(
    tmp_path: Path,
    input_case: str,
):
    assignment = {"kind": "pending"} if input_case == "pending" else _OMITTED
    state = _state(tmp_path, prereg_assignment=assignment)
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] == "scientific"
    assert authority["prereg_status"] == input_case
    assert _experiment_action_starts(state) == []

    kwargs = {}
    if input_case == "omitted":
        kwargs["blocker"] = {
            "blocker_id": "observations_missing",
            "summary": "The observations needed by this exploration were not supplied.",
        }
    result = _run(
        finish._finish(
            state,
            outcome="blocked",
            summary="No scientific calculation was executed because its input was unavailable.",
            **kwargs,
        )
    )

    assert result["status"] == "success" and result["outcome"] == "blocked", result
    delivery = result["delivery"]
    assert delivery["scope"] == "scientific"
    assert delivery["execution_status"] == "not_run"
    assert delivery["result_status"] == "unavailable"
    assert delivery["external_job_refs"] == []
    assert _experiment_action_starts(state) == []
    if input_case == "pending":
        assert delivery["formal_input"]["status"] == "pending"
        assert delivery["formal_input"]["binding"] is None
        assert delivery["formal_input"]["error"]["code"] == "prereg_assignment_pending"
        assert any(gap.get("name") == "formal_input_unavailable" for gap in delivery["gaps"])
    else:
        assert delivery["formal_input"] == {
            "status": "none",
            "binding": None,
            "error": None,
        }
    _assert_frozen_delivery(state, result, "blocked")
