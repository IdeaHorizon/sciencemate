"""Recovery follows actual task checks, not the exit status of every producer.

These cases run real local subprocesses through safe_run_bash, retain their
exit codes and output observations, and call the production finish transaction.
No model, command classifier, evidence evaluator or finish result is mocked.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import contract_audit, delivery_transaction, finish, ledger, run_bootstrap, safe_bash


TASK = "Produce the requested output files containing corrected value=49, preserving failed attempts."


def _content_requirements(state, filenames=("result.dat",)):
    checks = []
    for filename in filenames:
        target = state.root / "outputs/experiment/runtime" / filename
        program = f"from pathlib import Path; assert Path({str(target)!r}).read_text() == 'corrected value=49\\n'"
        checks.extend([
            {"id": filename, "kind": "file", "path": str(target)},
            {"id": filename + "_content", "kind": "check",
             "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}", "expect_exit": 0},
        ])
    return checks


def _state(tmp_path, filenames=("result.dat",), checks=None, task=TASK):
    state = State.new("experiment", tmp_path / "runs", project_id="p", session_id="recovery-owner")
    if callable(checks):
        checks = checks(state)
    state.hook_state["node_inputs"] = {
        "experiment_spec": task,
        "required_outputs": checks if checks is not None else _content_requirements(state, filenames),
    }
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="failure-recovery", target_node="experiment",
        objective=task,
        prereg_assignment=PreregAssignment.none("deterministic software validation"),
        actor="parent", reason="fresh dispatch",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_digest = revision.digest
    state.task_contract_revision = revision.revision
    state.parent_dispatch_id = "recovery-dispatch"
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


async def _python(state, program, *, cwd=None):
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    return await safe_bash._safe_run_bash(
        state, cmd=command, cwd=str(cwd or state.root / "outputs/experiment/runtime"), timeout=20,
    )


async def _write_failed_then(state, followup, filenames=("result.dat",), followup_cwd=None):
    failed_program = "from pathlib import Path; " + "; ".join(
        f"Path({filename!r}).write_text('incomplete value=7\\n')" for filename in filenames
    ) + "; raise SystemExit(7)"
    failed = await _python(state, failed_program)
    assert failed.get("returncode") == 7, failed
    later = await _python(state, followup, cwd=followup_cwd)
    assert later.get("returncode") == 0, later
    return await finish._finish(state, outcome="success", summary="Requested output is complete.")


@pytest.mark.parametrize("followup", [
    "print('unrelated diagnostic succeeded')",
    "from pathlib import Path; Path('result.dat').touch()",
    "from pathlib import Path; Path('unrelated.txt').write_text('a different result')",
])
def test_unrelated_success_cannot_satisfy_the_required_output_content(tmp_path, followup):
    state = _state(tmp_path)

    result = asyncio.run(_write_failed_then(state, followup))

    assert result["status"] == "success_unsupported", result
    assert any(row.get("obligation") == "result.dat_content" for row in result["gaps"])
    assert delivery_transaction.read_manifest(state) is None
    assert (state.root / "outputs/experiment/runtime/result.dat").read_text() == "incomplete value=7\n"
    honest = asyncio.run(finish._finish(state, outcome="partial", summary="Only failed-attempt output remains."))
    assert honest["status"] == "success", honest
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_successful_new_output_producer_replaces_failed_attempt_without_manual_ids(tmp_path):
    state = _state(tmp_path)
    followup = "from pathlib import Path; Path('result.dat').write_text('corrected value=49\\n')"

    result = asyncio.run(_write_failed_then(state, followup))

    assert result["status"] == "success", result
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    raw = json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
    assert raw["delivery"]["outcome"] == "success"
    rows = ledger.open_ledger(state).records()
    payload_ids = {row["action_id"] for row in rows
                   if row["kind"] == "action_started" and row.get("tool") == "safe_run_bash"}
    assert [row["exit_code"] for row in rows
            if row["kind"] == "action_settled" and row["action_id"] in payload_ids] == [7, 0]


def test_repairing_only_one_of_two_failed_outputs_is_not_complete_recovery(tmp_path):
    state = _state(tmp_path, filenames=("result.dat", "other.dat"))
    followup = "from pathlib import Path; Path('result.dat').write_text('corrected value=49\\n')"

    result = asyncio.run(_write_failed_then(state, followup, filenames=("result.dat", "other.dat")))

    assert result["status"] == "success_unsupported", result
    assert any(row.get("obligation") == "other.dat_content" for row in result["gaps"])
    assert delivery_transaction.read_manifest(state) is None


def test_actual_output_recovery_is_not_tied_to_the_original_working_directory(tmp_path):
    state = _state(tmp_path)
    target = state.root / "outputs/experiment/runtime/result.dat"
    other_cwd = state.root / "outputs/experiment/runtime/repair"
    other_cwd.mkdir(parents=True)
    followup = f"from pathlib import Path; Path({str(target)!r}).write_text('corrected value=49\\n')"

    result = asyncio.run(_write_failed_then(state, followup, followup_cwd=other_cwd))

    assert result["status"] == "success", result
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_contract_check_passes_after_a_failed_self_test_and_real_code_fix(tmp_path):
    program = "from pathlib import Path; assert Path('source.txt').read_text() == 'fixed'; print('SELF_TEST_OK')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    state = _state(tmp_path, checks=lambda selected: [{
        "id": "self_test", "kind": "check", "command": command,
        "expect_exit": 0, "expect_stdout_contains": "SELF_TEST_OK",
        "cwd": str(selected.root / "outputs/experiment/runtime"),
    }])
    runtime = state.root / "outputs/experiment/runtime"
    # This is a fixture input, not a produced result; the required check has
    # its cwd fixed before any action and finish runs it with read-only roots.
    source = runtime / "source.txt"
    source.write_text("broken")

    async def execute():
        first = await _python(state, program)
        assert first.get("returncode") == 1, first
        repaired = await _python(state, "from pathlib import Path; Path('source.txt').write_text('fixed')")
        assert repaired.get("returncode") == 0, repaired
        return await finish._finish(state, outcome="success", summary="The required self-test now passes.")

    result = asyncio.run(execute())

    assert result["status"] == "success", result
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    checks = [row for row in ledger.open_ledger(state).records()
              if row.get("kind") == "action_settled" and row.get("action_id") == "check-self_test"]
    assert len(checks) == 1 and checks[0]["exit_code"] == 0


def test_generic_required_check_does_not_waive_the_actual_content_requirement(tmp_path):
    state = _state(tmp_path, checks=lambda selected: [
        *_content_requirements(selected),
        {"id": "unrelated_check", "kind": "check", "command": "true", "expect_exit": 0},
    ])

    result = asyncio.run(_write_failed_then(state, "print('unrelated success')"))

    assert result["status"] == "success_unsupported", result
    assert any(row.get("obligation") == "result.dat_content" for row in result["gaps"])


def test_zero_exit_with_fatal_evidence_still_taints_its_current_output(tmp_path):
    state = _state(tmp_path)

    async def execute():
        first = await _python(state, "from pathlib import Path; Path('result.dat').write_text('bad result'); print('FATAL: invalid result')")
        assert first.get("returncode") == 0, first
        assert "FATAL" in first.get("stdout_tail", "")
        rows = ledger.open_ledger(state).records()
        action_id = next(row["action_id"] for row in rows if row["kind"] == "action_started")
        # Inject only the domain detector's evidence shape, not execution or a
        # verdict: the actual command emitted this fatal diagnostic at exit 0.
        ledger.record(state, "action_settled", action_id=action_id, state="settled", exit_code=0,
                      fatal_signals=["FATAL: invalid result"])
        later = await _python(state, "print('unrelated success')")
        assert later.get("returncode") == 0, later
        return await finish._finish(state, outcome="success", summary="Output exists.")

    result = asyncio.run(execute())

    assert result["status"] == "success_unsupported", result
    assert any(row.get("name") == "fatal_signal" for row in result["contradictions"])


def test_explicit_normal_exit_requirement_is_not_waived_by_valid_output(tmp_path):
    def required(state):
        events = ledger.ledger_dir(state) / "events.jsonl"
        program = (
            "import json; from pathlib import Path; "
            f"rows=[json.loads(line) for line in Path({str(events)!r}).read_text().splitlines()]; "
            "stage=next(row['action_id'] for row in rows "
            "if row['kind']=='action_started' and row.get('tool')=='safe_run_bash'); "
            "exit_code=next(row['exit_code'] for row in reversed(rows) "
            "if row['kind']=='action_settled' and row.get('action_id')==stage); "
            "assert exit_code == 0, f'required stage exited {exit_code}'"
        )
        return [*_content_requirements(state), {
            "id": "required_stage_normal_exit", "kind": "check",
            "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
            "expect_exit": 0,
        }]

    state = _state(
        tmp_path, checks=required,
        task=TASK + " The initial measured stage must also exit normally.",
    )
    actual = asyncio.run(_python(
        state, "from pathlib import Path; Path('result.dat').write_text('corrected value=49\\n'); "
        "raise SystemExit(7)",
    ))
    assert actual.get("returncode") == 7, actual

    result = asyncio.run(finish._finish(
        state, outcome="success", summary="The output content is correct.",
    ))

    assert result["status"] == "success_unsupported", result
    rejected = next(row for row in result["gaps"]
                    if row.get("obligation") == "required_stage_normal_exit")
    assert rejected["evidence"]["executed"] is True, rejected
    assert rejected["evidence"]["exit_code"] == 1, rejected
    assert "required stage exited 7" in rejected["evidence"]["stderr_tail"], rejected
    assert not any(row.get("obligation") == "result.dat_content" for row in result["gaps"]), result
    assert delivery_transaction.read_manifest(state) is None
