"""A glob minimum selects valid candidates; explicit deliverables stay required.

Fatal cases project actual stdout into the existing domain-detector evidence
shape; they test selection/finish, not automatic domain-diagnostic wiring.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


TASK = "Produce at least the requested number of new valid text results."


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path, *, formal_minimum=None):
    state = State.new("experiment", tmp_path / "runs", project_id="glob-selection", session_id="owner")
    output = state.root / "outputs/experiment/runtime"
    inputs = {"experiment_spec": TASK}
    if formal_minimum is not None:
        inputs["required_outputs"] = [{
            "id": "formal_result_minimum", "kind": "glob", "pattern": str(output / "*.txt"),
            "min_count": formal_minimum,
        }]
    state.hook_state["node_inputs"] = inputs
    _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state, output


def _produce(state, path, *, exit_code=0, fatal=False):
    program = (f"from pathlib import Path; Path({str(path)!r}).write_text('observed result\\n'); "
               + ("print('FATAL: numerical result invalid'); " if fatal else "")
               + f"raise SystemExit({exit_code})")
    result = _run(safe_bash._safe_run_bash(
        state, cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
        cwd=str(path.parent), timeout=20,
    ))
    assert result.get("returncode") == exit_code, result
    starts = [row for row in ledger.open_ledger(state).records()
              if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]
    action_id = starts[-1]["action_id"]
    if fatal:
        assert "FATAL: numerical result invalid" in result.get("stdout_tail", "")
        # Supply only the domain detector projection of a real emitted diagnostic.
        ledger.record(state, "action_settled", action_id=action_id, state="settled",
                      exit_code=exit_code, fatal_signals=["FATAL: numerical result invalid"])
    return action_id


def _candidates(state, output, invalid_kind):
    bad, good = output / "a_bad.txt", output / "z_good.txt"
    if invalid_kind == "fatal":
        failed_id = _produce(state, bad, exit_code=7, fatal=True)
    else:
        # A pre-existing workspace file is not an explicitly bound input and
        # not a result produced by this run. The subsequent action does not touch it.
        bad.write_text("pre-existing result\n")
        failed_id = None
    good_id = _produce(state, good)
    return bad, good, failed_id, good_id


def _close(state, output, *, natural, deliverables=None):
    verification = ([{"requirement": TASK, "checks": [{
        "kind": "glob", "pattern": str(output / "*.txt"), "min_count": 1,
    }]}] if natural else None)
    return _run(finish._finish(
        state, outcome="success", summary="The required number of valid new results was produced.",
        verification=verification, deliverables=deliverables,
    ))


@pytest.mark.parametrize("formal", [False, True])
@pytest.mark.parametrize("invalid_kind", ["fatal", "old"])
def test_minimum_one_selects_good_producer_without_blessing_other_matches(tmp_path, formal, invalid_kind):
    state, output = _state(tmp_path, formal_minimum=1 if formal else None)
    bad, good, failed_id, good_id = _candidates(state, output, invalid_kind)
    before_a = obligations.obligations_path(state).read_bytes()
    before_b = ledger.open_ledger(state).records()

    result = _close(state, output, natural=not formal)

    assert result.get("status") == "success" and result.get("outcome") == "success", result
    clean = json.loads(state.read_artifact(result["artifact_ids"]["clean_results"])["content"])
    raw = json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
    name = "formal_result_minimum" if formal else "task_verification_0"
    predicate = next(row for row in clean["evaluation"]["checks"] if row["name"] == name)
    evidence = predicate["evidence"]
    assert predicate["passed"] is True
    assert evidence["matched"] == 2 and evidence["min_count"] == 1
    assert evidence["selected"] == [str(good)]
    assert evidence["unselected_count"] == 1
    assert evidence["producers"] == [good_id]
    assert [row["path"] for row in evidence["selected_objects"]] == [str(good)]
    assert obligations.obligations_path(state).read_bytes() == before_a
    assert all(row in raw["ledger_records"] for row in before_b)
    # Successful existential selection must not silently rewrite the failed
    # candidate's B fact or promote it into the selected delivery lineage.
    if failed_id:
        action = next(row for row in clean["actions"] if row["action_id"] == failed_id)
        assert action["exit_code"] == 7
        assert any(row.get("kind") == "action_settled" and row.get("action_id") == failed_id
                   and row.get("exit_code") == 7 for row in raw["ledger_records"])
        assert failed_id not in evidence["producers"]
    else:
        assert str(bad) in evidence["unattributed"]


@pytest.mark.parametrize("invalid_kind", ["fatal", "old"])
def test_explicit_bad_deliverable_is_not_exempted_by_a_satisfied_glob(tmp_path, invalid_kind):
    state, output = _state(tmp_path)
    bad, good, failed_id, _ = _candidates(state, output, invalid_kind)

    result = _close(state, output, natural=True, deliverables=[{"path": str(bad)}, {"path": str(good)}])

    assert result.get("status") == "success_unsupported", result
    assert delivery_transaction.read_manifest(state) is None
    if failed_id:
        assert any(row.get("action") == failed_id and row.get("name") == "fatal_signal"
                   for row in result.get("contradictions", [])), result
    else:
        assert any(row.get("kind") == "delivery_object" and row.get("gap") == "unattributed"
                   and row["evidence"]["path"] == str(bad) for row in result.get("gaps", [])), result


@pytest.mark.parametrize("invalid_kind", ["fatal", "old"])
def test_formal_minimum_two_is_not_weakened_to_one_valid_candidate(tmp_path, invalid_kind):
    state, output = _state(tmp_path, formal_minimum=2)
    _, good, failed_id, _ = _candidates(state, output, invalid_kind)
    before_a = obligations.obligations_path(state).read_bytes()

    # The model's minimum-one interpretation cannot replace the formal minimum.
    result = _close(state, output, natural=True)

    assert result.get("status") == "success_unsupported", result
    gap = next(row for row in result["gaps"] if row.get("obligation") == "formal_result_minimum")
    assert gap["evidence"]["min_count"] == 2 and gap["evidence"]["matched"] == 2
    assert gap["evidence"]["selected"] == [str(good)]
    assert obligations.obligations_path(state).read_bytes() == before_a
    assert delivery_transaction.read_manifest(state) is None
    if failed_id:
        assert failed_id in gap["evidence"]["producers"]
        assert any(row.get("action") == failed_id for row in result["contradictions"]), result


def test_nonzero_command_does_not_exclude_a_valid_observed_glob_candidate(tmp_path):
    state, output = _state(tmp_path, formal_minimum=1)
    candidate = output / "result.txt"
    producer = _produce(state, candidate, exit_code=7)
    check = f"from pathlib import Path; assert Path({str(candidate)!r}).read_text() == 'observed result\\n'"
    result = _run(finish._finish(
        state, outcome="success", summary="The required result was independently checked.",
        verification=[{"requirement": TASK, "checks": [{
            "kind": "check", "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}",
            "expect_exit": 0,
        }]}],
    ))
    assert result.get("status") == "success", result
    clean = json.loads(state.read_artifact(result["artifact_ids"]["clean_results"])["content"])
    selected = next(row for row in clean["evaluation"]["checks"] if row["name"] == "formal_result_minimum")
    assert selected["evidence"]["selected"] == [str(candidate)]
    assert selected["evidence"]["producers"] == [producer]
    assert next(row for row in clean["actions"] if row["action_id"] == producer)["exit_code"] == 7
