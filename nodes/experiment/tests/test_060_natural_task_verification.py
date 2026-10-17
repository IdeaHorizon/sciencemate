"""Natural task acceptance without caller-supplied internal obligations.

The payload and finish checks are real sandboxed subprocesses.  Tests supply
the model's proposed verification methods, not a replacement task contract.
This proves mechanical execution/evidence behavior, not a model's ability to
interpret every natural-language requirement correctly.

Known semantic boundary: a model could quote "write 49" while proposing only
an existence check and ``true`` for a file containing 48.  Source quotation
and genuine execution do not establish that this interpretation is adequate.
That counterexample must not be claimed as mechanically prevented, nor made
into a regression demanding that the wrong result always be accepted.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import (
    contract_audit,
    delivery_transaction,
    finish,
    ledger,
    obligations,
    run_bootstrap,
    safe_bash,
)


RESULT = "outputs/experiment/runtime/result.txt"
SECOND = "outputs/experiment/runtime/second.txt"
TASK = "Write result.txt containing exactly 49 followed by a newline."
SECOND_REQUIREMENT = "Also write second.txt containing exactly verified followed by a newline."


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path, *, task=TASK, formal_required=None):
    state = State.new(
        "experiment", tmp_path / "runs", project_id="natural-task", session_id="owner",
    )
    inputs = {
        "experiment_spec": task,
        "prereg_assignment": {"kind": "none", "reason": "deterministic software validation"},
    }
    if formal_required is not None:
        if callable(formal_required):
            formal_required = formal_required(state)
        inputs["required_outputs"] = copy.deepcopy(formal_required)
    state.hook_state["node_inputs"] = inputs
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _python_command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _payload(state, content="49\n", *, second=None):
    program = f"from pathlib import Path; Path('result.txt').write_text({content!r})"
    if second is not None:
        program += f"; Path('second.txt').write_text({second!r})"
    result = _run(safe_bash._safe_run_bash(
        state, cmd=_python_command(program),
        cwd=str(state.root / "outputs/experiment/runtime"), timeout=20,
    ))
    assert result.get("returncode") == 0, result
    return result


def _content_check(state, path=RESULT, expected="49\n", *, method="text"):
    path = str(Path(path) if Path(path).is_absolute() else state.root / path)
    if method == "bytes":
        assertion = f"Path({path!r}).read_bytes() == {expected.encode()!r}"
    else:
        assertion = f"Path({path!r}).read_text() == {expected!r}"
    return {
        "kind": "check",
        "command": _python_command(f"from pathlib import Path; assert {assertion}; print('CONTENT_VERIFIED')"),
        "expect_exit": 0,
        "expect_stdout_contains": "CONTENT_VERIFIED",
        "timeout_s": 20,
    }


def _verification(state, requirement=TASK, *, path=RESULT, expected="49\n", method="text"):
    path = str(Path(path) if Path(path).is_absolute() else state.root / path)
    return [{
        "requirement": requirement,
        "checks": [{"kind": "file", "path": path}, _content_check(state, path, expected, method=method)],
        "explanation": "Check current output provenance and its exact contents independently.",
    }]


def _close(state, verification, **extra):
    return _run(finish._finish(
        state, outcome="success", summary="The requested output and its checks passed.",
        verification=verification, **extra,
    ))


def _assert_success(state, result):
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    for kind in ("raw_results", "clean_results", "experiment_log"):
        assert state.read_artifact(result["artifact_ids"][kind])["metadata"]["frozen"] is True


def _raw_and_clean(state, result):
    return [json.loads(state.read_artifact(result["artifact_ids"][kind])["content"])
            for kind in ("raw_results", "clean_results")]


def _payload_starts(state):
    return [row for row in ledger.open_ledger(state).records()
            if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]


def _values_named(value, name):
    if isinstance(value, dict):
        if name in value:
            yield value[name]
        for child in value.values():
            yield from _values_named(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from _values_named(child, name)


def test_natural_task_has_real_acceptance_and_replay_without_internal_input_fields(tmp_path):
    state = _state(tmp_path)
    original_inputs = copy.deepcopy(state.hook_state["node_inputs"])
    _payload(state)

    result = _close(state, _verification(state))

    _assert_success(state, result)
    assert state.hook_state["node_inputs"] == original_inputs
    assert not {"required_outputs", "deliverables", "obligations", "scope"} & original_inputs.keys()
    source = obligations.task_source(state)
    assert source["text"] == TASK and source["source"]
    assert any(row.get("kind") == "task" for row in obligations.frozen(state))
    rows = ledger.open_ledger(state).records()
    checks = [row for row in rows if row.get("kind") == "action_started" and row.get("tool") == "finish.check"]
    assert checks and _content_check(state)["command"] in {row.get("command") for row in checks}
    assert len(_payload_starts(state)) == 1
    payloads = _raw_and_clean(state, result)
    assert "model_interpretation" in list(_values_named(payloads, "semantic_coverage"))
    assert TASK in json.dumps(payloads, ensure_ascii=False)
    before_replay = copy.deepcopy(rows)

    again = _close(state, _verification(state))

    assert again.get("reused_manifest") is True, again
    assert again["artifact_ids"] == result["artifact_ids"]
    assert ledger.open_ledger(state).records() == before_replay


def test_a_missing_requested_file_fails_the_same_real_verification(tmp_path):
    state = _state(tmp_path, task=TASK + "\n" + SECOND_REQUIREMENT)
    _payload(state)
    verification = _verification(state) + _verification(
        state, SECOND_REQUIREMENT, path=SECOND, expected="verified\n",
    )

    result = _close(state, verification)

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "missing" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None


def test_wrong_contents_do_not_pass_merely_because_the_file_exists(tmp_path):
    state = _state(tmp_path)
    _payload(state, "48\n")

    result = _close(state, _verification(state))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None
    assert (state.root / RESULT).read_text() == "48\n"


def test_summary_cannot_replace_a_failing_execution_check(tmp_path):
    state = _state(tmp_path)
    _payload(state, "48\n")

    result = _run(finish._finish(
        state, outcome="success", summary="I independently verified 49; every requirement passed.",
        verification=_verification(state),
    ))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None


def test_an_equivalent_corrected_check_needs_no_payload_rerun_or_task_rewrite(tmp_path):
    state = _state(tmp_path)
    _payload(state)
    original_inputs = copy.deepcopy(state.hook_state["node_inputs"])
    bad_method = _verification(state, path="outputs/experiment/runtime/wrong-check-path.txt")
    first = _close(state, bad_method)
    assert first.get("status") == "success_unsupported", first
    assert delivery_transaction.read_manifest(state) is None
    before_a = [row for row in ledger.open_ledger(state).records() if row.get("kind") == "run_bootstrap"]

    result = _close(state, _verification(state, method="bytes"))

    _assert_success(state, result)
    assert len(_payload_starts(state)) == 1
    assert state.hook_state["node_inputs"] == original_inputs
    assert obligations.task_source(state)["text"] == TASK
    assert [row for row in ledger.open_ledger(state).records() if row.get("kind") == "run_bootstrap"] == before_a


def test_model_verification_cannot_override_an_explicit_required_check(tmp_path):
    state = _state(tmp_path, formal_required=lambda selected: [
        {"id": "formal_exact_content", **_content_check(selected, expected="49\n")},
    ])
    formal = copy.deepcopy(state.hook_state["node_inputs"]["required_outputs"])
    _payload(state, "48\n")
    # This weaker, incorrect interpretation is not allowed to replace the
    # caller's independently supplied formal requirement.
    weaker = _verification(state, expected="48\n")

    result = _close(state, weaker)

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("obligation") == "formal_exact_content" and row.get("gap") == "check_failed"
               for row in result.get("gaps", [])), result
    assert state.hook_state["node_inputs"]["required_outputs"] == formal
    assert delivery_transaction.read_manifest(state) is None


def test_preexisting_correct_file_without_this_runs_producer_is_not_a_new_result(tmp_path):
    state = _state(tmp_path)
    # Fixture material, intentionally not an observed action of this run.
    (state.root / RESULT).write_text("49\n")
    run = _run(safe_bash._safe_run_bash(
        state, cmd=_python_command("from pathlib import Path; Path('unrelated.txt').write_text('ok')"),
        cwd=str(state.root / "outputs/experiment/runtime"), timeout=20,
    ))
    assert run.get("returncode") == 0, run

    result = _close(state, _verification(state))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "unattributed" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None


def test_no_verification_explains_missing_task_evidence_not_hidden_array(tmp_path):
    state = _state(tmp_path)
    _payload(state)

    result = _run(finish._finish(state, outcome="success", summary="The output is ready."))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "task_verification_missing" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None


@pytest.mark.parametrize("outcome", ["partial", "failed", "blocked", "cancelled"])
def test_honest_non_success_does_not_require_model_verification(tmp_path, outcome):
    state = _state(tmp_path)
    extra = {"blocker": {"summary": "A required external input is unavailable."}} if outcome == "blocked" else {}

    result = _run(finish._finish(
        state, outcome=outcome, summary="No output was produced; this is not a success claim.",
        **extra,
    ))

    assert result.get("status") == "success" and result.get("outcome") == outcome, result
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    assert obligations.task_source(state)["text"] == TASK


@pytest.mark.parametrize("location", ["entry", "check"])
def test_model_cannot_supply_a_precomputed_passed_verdict(tmp_path, location):
    state = _state(tmp_path)
    _payload(state, "48\n")
    verification = _verification(state)
    target = verification[0] if location == "entry" else verification[0]["checks"][1]
    target["passed"] = True

    result = _close(state, verification)

    assert result.get("status") == "error", result
    assert result.get("error_code") == "finish_parameters_invalid", result
    assert delivery_transaction.read_manifest(state) is None
    assert not [row for row in ledger.open_ledger(state).records()
                if row.get("kind") == "action_started" and row.get("tool") == "finish.check"]


def test_faithful_paraphrase_is_accepted_without_rewriting_the_frozen_task(tmp_path):
    state = _state(tmp_path)
    _payload(state, "49\n")
    original_a = obligations.obligations_path(state).read_bytes()
    verification = _verification(state,
        "The output must be result.txt with the number 49 and a terminating newline.",
    )

    result = _close(state, verification)

    _assert_success(state, result)
    assert obligations.obligations_path(state).read_bytes() == original_a
    assert obligations.task_source(state)["text"] == TASK
    assert state.hook_state["node_inputs"]["experiment_spec"] == TASK


def test_passing_weak_method_is_labelled_model_interpretation_not_semantic_proof(tmp_path):
    """A declared passing check does not mechanically prove natural-language coverage.

    Deliberately use a weak content method over a correct, genuinely produced file.
    This is a disclosed semantic limit, not a recommendation to use weak tests.
    """
    state = _state(tmp_path)
    _payload(state, "49\n")
    verification = [{
        "requirement": TASK,
        "checks": [{"kind": "file", "path": str(state.root / RESULT)}, {"kind": "check", "command": "true", "expect_exit": 0}],
        "explanation": "This method only establishes that a file exists and a trivial check exits zero.",
    }]

    result = _close(state, verification)

    _assert_success(state, result)
    payloads = _raw_and_clean(state, result)
    assert "model_interpretation" in list(_values_named(payloads, "semantic_coverage"))
    assert result["delivery"].get("limitations"), "Passing checks must not imply mechanical semantic coverage."
    assert (state.root / RESULT).read_text() == "49\n"


@pytest.mark.parametrize("provide_second", [False, True])
def test_glob_verification_uses_actual_producer_count(tmp_path, provide_second):
    task = "Produce two text result files in the run workspace."
    state = _state(tmp_path, task=task)
    _payload(state, second="verified\n" if provide_second else None)
    verification = [{
        "requirement": task,
        "checks": [{"kind": "glob", "pattern": str(state.root / "outputs/experiment/runtime/*.txt"), "min_count": 2}],
    }]

    result = _close(state, verification)

    if provide_second:
        _assert_success(state, result)
    else:
        assert result.get("status") == "success_unsupported", result
        assert result.get("gaps"), result
        assert delivery_transaction.read_manifest(state) is None


def test_new_verification_entry_cannot_write_over_the_output_it_is_checking(tmp_path):
    state = _state(tmp_path)
    _payload(state)
    verification = _verification(state)
    verification[0]["checks"][1] = {
        "kind": "check",
        "command": _python_command(f"from pathlib import Path; Path({str(state.root / RESULT)!r}).write_text('forged')"),
        "expect_exit": 0,
    }

    result = _close(state, verification)

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result
    assert (state.root / RESULT).read_text() == "49\n"
    assert delivery_transaction.read_manifest(state) is None


def test_verification_cannot_bless_wrong_result_after_unrelated_success(tmp_path):
    state = _state(tmp_path)
    failed = _run(safe_bash._safe_run_bash(
        state, cmd=_python_command("from pathlib import Path; Path('result.txt').write_text('17\\n'); raise SystemExit(7)"),
        cwd=str(state.root / "outputs/experiment/runtime"), timeout=20,
    ))
    assert failed.get("returncode") == 7, failed
    unrelated = _run(safe_bash._safe_run_bash(
        state, cmd=_python_command("from pathlib import Path; Path('unrelated.txt').write_text('ok')"),
        cwd=str(state.root / "outputs/experiment/runtime"), timeout=20,
    ))
    assert unrelated.get("returncode") == 0, unrelated

    result = _close(state, _verification(state))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None
    assert (state.root / RESULT).read_text() == "17\n"


def test_check_only_natural_task_counts_the_real_finish_process_as_execution(tmp_path):
    task = "Use Python to check that the sum of the integers from 1 to 100 is 5050."
    state = _state(tmp_path, task=task)
    verification = [{
        "requirement": task,
        "checks": [{
            "kind": "check",
            "command": _python_command("value = sum(range(1, 101)); assert value == 5050; print('SUM_VERIFIED', value)"),
            "expect_exit": 0,
            "expect_stdout_contains": "SUM_VERIFIED 5050",
        }],
    }]

    result = _close(state, verification)

    _assert_success(state, result)
    assert result["delivery"]["execution_status"] == "attempted"
    assert _payload_starts(state) == []
    rows = ledger.open_ledger(state).records()
    checks = [row for row in rows if row.get("kind") == "action_started" and row.get("tool") == "finish.check"]
    assert len(checks) == 1
    settled = [row for row in rows if row.get("kind") == "action_settled"
               and row.get("action_id") == checks[0]["action_id"]]
    assert len(settled) == 1 and settled[0]["exit_code"] == 0
