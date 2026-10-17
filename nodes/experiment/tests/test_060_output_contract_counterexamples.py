"""A change of verification method must not change an object's recorded facts.

Real payloads and finish checks use the existing sandboxed execution path.  No
runner, ledger, producer observation or verdict is replaced by a test double.
The tests distinguish a newly produced result from auditing an existing input;
they do not require every delivered/reference file to be generated in this run.
"""
from __future__ import annotations

import asyncio
import copy
import shlex
import sys
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import (
    delivery_transaction,
    finish,
    ledger,
    obligations,
    run_bootstrap,
    safe_bash,
)


RESULT = "outputs/experiment/runtime/result.txt"
TASK = "Produce result.txt containing exactly 49 followed by a newline."


def _run(awaitable):
    return asyncio.run(awaitable)


def _python(program: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _state(tmp_path: Path, *, task=TASK, baseline: Path | None = None, formal_required=None):
    state = State.new(
        "experiment", tmp_path / "runs", project_id="output-contract", session_id="owner",
    )
    inputs = {
        "experiment_spec": task,
        "prereg_assignment": {"kind": "none", "reason": "deterministic execution test"},
    }
    if baseline is not None:
        inputs["path_roles"] = {
            "source_baseline_root": {"path": str(baseline), "writable": False},
        }
    if formal_required is not None:
        inputs["required_outputs"] = copy.deepcopy(formal_required)
    state.hook_state["node_inputs"] = inputs
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _produce(state, *, exit_code=0):
    program = (
        "from pathlib import Path; Path('result.txt').write_text('49\\n'); "
        f"raise SystemExit({exit_code})"
    )
    result = _run(safe_bash._safe_run_bash(
        state, cmd=_python(program), cwd=str(state.root / "outputs/experiment/runtime"),
        timeout=20,
    ))
    assert result.get("returncode") == exit_code, result
    assert (state.root / RESULT).read_text() == "49\n"
    return result


def _check(path: str, *, expect="CONTENT_OK"):
    return {
        "kind": "check",
        "command": _python(
            f"from pathlib import Path; assert Path({path!r}).read_text() == '49\\n'; "
            "print('CONTENT_OK')"
        ),
        "expect_exit": 0,
        "expect_stdout_contains": expect,
        "timeout_s": 20,
    }


def _verification(state, task=TASK, *, path=RESULT, include_file=True, expect="CONTENT_OK"):
    path = str(Path(path) if Path(path).is_absolute() else state.root / path)
    checks = ([{"kind": "file", "path": path}] if include_file else [])
    checks.append(_check(path, expect=expect))
    return [{"requirement": task, "checks": checks}]


def _finish(state, verification, *, path=RESULT):
    path = str(Path(path) if Path(path).is_absolute() else state.root / path)
    return _run(finish._finish(
        state, outcome="success", summary="The declared object passed the content check.",
        deliverables=[{"path": path, "note": "Object to which the verification applies."}],
        verification=verification,
    ))


def _records(state):
    return ledger.open_ledger(state).records()


def _payload_starts(state):
    return [row for row in _records(state)
            if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]


def _assert_check_really_ran(state):
    checks = {row["action_id"] for row in _records(state)
              if row.get("kind") == "action_started" and row.get("tool") == "finish.check"}
    settlements = [row for row in _records(state)
                   if row.get("kind") == "action_settled" and row.get("action_id") in checks]
    assert settlements and all(row.get("executed") is True for row in settlements), settlements
    assert all(row.get("exit_code") == 0 for row in settlements), settlements


def _assert_original_producer_and_exit(state, expected_exit=7):
    starts = _payload_starts(state)
    original = starts[0]["action_id"]
    evidence = finish._file_evidence(state, _records(state), {"path": str(state.root / RESULT)})
    assert evidence["passed"] is True, evidence
    assert evidence["evidence"]["producers"] == [original], evidence
    settled = [row for row in _records(state)
               if row.get("kind") == "action_settled" and row.get("action_id") == original]
    assert settled and settled[-1]["exit_code"] == expected_exit, settled
    return evidence["evidence"]


@pytest.mark.parametrize("include_file", [True, False], ids=["file-and-check", "check-only"])
def test_content_task_can_finish_without_relabelling_the_nonzero_producer(tmp_path, include_file):
    """A real exact check meets the task; it does not rewrite exit 7 into 0."""
    state = _state(tmp_path)
    _produce(state, exit_code=7)

    result = _finish(state, _verification(state, include_file=include_file))

    _assert_check_really_ran(state)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert delivery_transaction.read_manifest(state) is not None
    _assert_original_producer_and_exit(state)
    assert len(_payload_starts(state)) == 1


@pytest.mark.parametrize("provenance", ["successful", "unknown", "failed"])
def test_changing_file_to_check_keeps_the_same_objects_provenance(tmp_path, provenance):
    state = _state(tmp_path)
    if provenance == "unknown":
        # Pre-existing fixture material has no producer observation in this run.
        (state.root / RESULT).write_text("49\n")
    else:
        _produce(state, exit_code=7 if provenance == "failed" else 0)
    original_a = obligations.obligations_path(state).read_bytes()
    original_inputs = copy.deepcopy(state.hook_state["node_inputs"])
    payload_before = copy.deepcopy(_payload_starts(state))
    # Keep the first call unsealed even for the positive control. Correcting
    # the method must preserve source facts; only the task assessment changes.
    first = _finish(state, _verification(state, expect="WRONG_EXPECTED_MARKER"))
    assert first.get("status") == "success_unsupported", first
    assert delivery_transaction.read_manifest(state) is None

    corrected = _finish(state, _verification(state, include_file=False))

    _assert_check_really_ran(state)
    assert obligations.obligations_path(state).read_bytes() == original_a
    assert state.hook_state["node_inputs"] == original_inputs
    assert _payload_starts(state) == payload_before
    if provenance in {"successful", "failed"}:
        assert corrected.get("status") == "success" and corrected.get("outcome") == "success", corrected
        _assert_original_producer_and_exit(state, 7 if provenance == "failed" else 0)
    else:
        assert corrected.get("status") == "success_unsupported", corrected
        assert delivery_transaction.read_manifest(state) is None
        assert any(row.get("gap") == "unattributed" for row in corrected.get("gaps", [])), corrected


@pytest.mark.parametrize("include_file", [True, False], ids=["file-and-check", "check-only"])
def test_known_input_audit_does_not_require_regenerating_its_delivered_reference(tmp_path, include_file):
    baseline = tmp_path / "provided-input"
    baseline.mkdir()
    data = baseline / "result.txt"
    data.write_text("49\n")
    task = "Audit the provided result.txt; verify it contains exactly 49 followed by a newline."
    state = _state(tmp_path, task=task, baseline=baseline)
    before = data.read_bytes()

    result = _finish(
        state, _verification(state, task, path=str(data), include_file=include_file), path=str(data),
    )

    _assert_check_really_ran(state)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert not _payload_starts(state)
    assert data.read_bytes() == before
    assert result["delivery"]["execution_status"] == "attempted"


@pytest.mark.parametrize("formal_kind", ["file", "check"])
def test_input_reference_and_new_method_cannot_waive_failed_formal_requirement(tmp_path, formal_kind):
    baseline = tmp_path / "provided-input"
    baseline.mkdir()
    data = baseline / "result.txt"
    data.write_text("49\n")
    if formal_kind == "file":
        # The caller explicitly demands a generated result, not merely an
        # audit of an input. C's existing-input interpretation cannot waive A.
        formal = {"id": "formal_result", "kind": "file", "path": str(data)}
    else:
        formal = {
            "id": "formal_result", "kind": "check", "command": _python(
                f"from pathlib import Path; assert Path({str(data)!r}).read_text() == '50\\n'"
            ), "expect_exit": 0,
        }
    state = _state(tmp_path, baseline=baseline, formal_required=[formal])
    original_a = obligations.obligations_path(state).read_bytes()
    # All proposed methods pass and the object is a known input reference.
    result = _finish(state, _verification(state, path=str(data), include_file=False), path=str(data))

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("obligation") == "formal_result" for row in result.get("gaps", [])), result
    assert obligations.obligations_path(state).read_bytes() == original_a
    assert state.hook_state["node_inputs"]["required_outputs"] == [formal]
    assert delivery_transaction.read_manifest(state) is None


def test_same_bytes_rewrite_keeps_source_facts_without_blocking_valid_content(tmp_path):
    state = _state(tmp_path)
    _produce(state, exit_code=7)
    original_inode = (state.root / RESULT).stat().st_ino
    _produce(state, exit_code=0)
    assert (state.root / RESULT).stat().st_ino == original_inode

    result = _finish(state, _verification(state, include_file=False))

    _assert_check_really_ran(state)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert delivery_transaction.read_manifest(state) is not None
    evidence = _assert_original_producer_and_exit(state)
    assert evidence["identity_adjustments"] == [_payload_starts(state)[1]["action_id"]], evidence
    assert len(_payload_starts(state)) == 2


def test_correcting_a_mistyped_object_path_does_not_freeze_the_old_typo(tmp_path):
    state = _state(tmp_path)
    _produce(state)
    original_a = obligations.obligations_path(state).read_bytes()
    wrong = str(state.root / "outputs/experiment/runtime/result-typo.txt")
    first = _finish(state, _verification(state, path=wrong), path=wrong)
    assert first.get("status") == "success_unsupported", first
    assert delivery_transaction.read_manifest(state) is None

    corrected = _finish(state, _verification(state, include_file=False))

    assert corrected.get("status") == "success" and corrected.get("outcome") == "success", corrected
    assert not corrected.get("gaps"), corrected
    assert obligations.obligations_path(state).read_bytes() == original_a
    assert len(_payload_starts(state)) == 1


def test_no_file_selftest_needs_no_invented_output_object(tmp_path):
    task = "Run the arithmetic selftest and report whether it passes; no output file is required."
    state = _state(tmp_path, task=task)
    result = _run(finish._finish(
        state, outcome="success", summary="The arithmetic selftest really passed.",
        verification=[{"requirement": task, "checks": [{
            "kind": "check", "command": _python("assert 6 * 7 == 42; print('SELFTEST_OK')"),
            "expect_exit": 0, "expect_stdout_contains": "SELFTEST_OK", "timeout_s": 20,
        }]}],
    ))

    _assert_check_really_ran(state)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert not _payload_starts(state)
    assert result["delivery"]["execution_status"] == "attempted"
