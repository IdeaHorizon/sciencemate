"""Independent review probes: object facts must not redefine method predicates.

These are isolated contract tests, not natural-model/UI acceptance. Real payloads
and checks use the existing executor; summaries are never the execution oracle.
Some assertions intentionally expose b83d1f7c gaps and should fail on that base.
"""
from __future__ import annotations

import asyncio
import copy
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _one_event_loop_per_test(monkeypatch):
    # Only replace this module's convenience helper, not a runtime executor.
    # Repeated asyncio.run would detach State's cancellation event from its loop.
    with asyncio.Runner() as runner:
        monkeypatch.setattr(sys.modules[__name__], "_run", runner.run)
        yield


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _state(tmp_path, task, formal=None):
    state = State.new("experiment", tmp_path / "runs", project_id="review-a1", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": task,
        "prereg_assignment": {"kind": "none", "reason": "isolated contract review"},
    }
    if formal is not None:
        state.hook_state["node_inputs"]["required_outputs"] = copy.deepcopy(formal)
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _produce(state, filename="result.txt", content="49\n", exit_code=0):
    target = working_directory(state) / "runtime" / filename
    result = _run(safe_bash._safe_run_bash(
        state, cmd=_command(
            f"from pathlib import Path; Path({filename!r}).write_text({content!r}); "
            f"raise SystemExit({exit_code})"
        ), cwd=str(target.parent), timeout=20,
    ))
    assert result.get("returncode") == exit_code, result
    assert target.read_text() == content
    producers = finish._producer_rows(ledger.open_ledger(state).records(), target)
    assert producers, "The execution oracle requires an actual output observation."
    return target


def _check(path, content):
    return {"kind": "check", "command": _command(
        f"from pathlib import Path; assert Path({str(path)!r}).read_bytes() == {content!r}; print('READ_OK')"
    ), "expect_exit": 0, "expect_stdout_contains": "READ_OK", "timeout_s": 20}


def _verification(task, checks):
    return [{"requirement": task, "checks": checks}]


def _close(state, task, checks, deliverables=None):
    return _run(finish._finish(
        state, outcome="success", summary="The actual checks and object observations are available.",
        verification=_verification(task, checks), deliverables=deliverables,
    ))


def _assert_read_executed(state):
    rows = ledger.open_ledger(state).records()
    ids = {row["action_id"] for row in rows
           if row.get("kind") == "action_started" and row.get("tool") == "finish.check"}
    settled = [row for row in rows if row.get("kind") == "action_settled" and row.get("action_id") in ids]
    assert settled and all(row.get("executed") is True and row.get("exit_code") == 0 for row in settled)


@pytest.mark.parametrize("formal", [False, True], ids=["natural-method", "formal-required"])
@pytest.mark.parametrize("with_old_match", [False, True], ids=["only-new", "old-and-new"])
def test_glob_minimum_does_not_require_unclaimed_old_matches(tmp_path, formal, with_old_match):
    task = "Create at least one new .txt result containing 49; unrelated existing files need not be delivered."
    method = {"kind": "glob", "pattern": "runtime/*.txt", "min_count": 1}
    state = _state(tmp_path, task, formal=[{"id": "one_new_result", **method}] if formal else None)
    root = working_directory(state) / "runtime"
    if with_old_match:
        (root / "unrelated-old.txt").write_text("not part of this delivery\n")
    target = _produce(state)
    frozen_a = obligations.obligations_path(state).read_bytes()
    proposed = [] if formal else [method]
    evaluation = _run(finish.evaluate(
        state, ledger.open_ledger(state, write=True),
        _verification(task, proposed) if proposed else None,
    ))
    matching_check = next(row for row in evaluation["checks"] if "min_count" in row["evidence"])
    assert matching_check["passed"] is True, evaluation
    assert str(target) in matching_check["evidence"]["attributed"]
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    assert evaluation["success_supported"] is True, evaluation


@pytest.mark.parametrize("method", ["file", "check", "file-and-check"])
def test_empty_output_has_equivalent_methods_without_fabricated_nonempty_requirement(tmp_path, method):
    task = "Create an empty result file and deliver it."
    state = _state(tmp_path, task)
    target = _produce(state, content="")
    frozen_a = obligations.obligations_path(state).read_bytes()
    checks = [] if method == "check" else [{"kind": "file", "path": str(target), "non_empty": False}]
    if method != "file":
        checks.append(_check(target, b""))
    result = _close(state, task, checks, [{"path": str(target)}])
    assert result.get("status") == "success", result
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    if method != "file":
        _assert_read_executed(state)
    manifest = delivery_transaction.read_manifest(state)
    assert manifest is not None


def test_explicit_formal_nonempty_cannot_be_waived_by_a_passing_empty_check(tmp_path):
    task = "Produce a nonempty result."
    formal = [{"id": "must_have_content", "kind": "file", "path": "runtime/result.txt", "non_empty": True}]
    state = _state(tmp_path, task, formal=formal)
    target = _produce(state, content="")
    frozen_a = obligations.obligations_path(state).read_bytes()
    result = _close(state, task, [_check(target, b"")], [{"path": str(target)}])
    _assert_read_executed(state)
    assert result.get("status") == "success_unsupported", result
    assert any(gap.get("obligation") == "must_have_content" and gap.get("gap") == "empty"
               for gap in result.get("gaps", [])), result
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    assert delivery_transaction.read_manifest(state) is None


def test_correcting_explicit_nonempty_method_does_not_redo_the_payload(tmp_path):
    task = "Create an empty result file and deliver it."
    state = _state(tmp_path, task)
    target = _produce(state, content="")
    # A model's mistaken explicit content condition remains correctable; the
    # new natural-file default no longer silently adds this condition.
    first = _close(state, task, [{"kind": "file", "path": str(target), "non_empty": True}], [{"path": str(target)}])
    assert first.get("status") == "success_unsupported", first
    assert any(gap.get("gap") == "empty" for gap in first.get("gaps", [])), first
    corrected = _close(state, task, [{"kind": "file", "path": str(target), "non_empty": False}],
                       [{"path": str(target)}])
    assert corrected.get("status") == "success", corrected
    starts = [row for row in ledger.open_ledger(state).records()
              if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]
    assert len(starts) == 1


@pytest.mark.xfail(strict=True, reason=(
    "Known semantic-coverage gap: content checks do not establish the explicit prose "
    "requirement that the generator itself exits 0; no stage-outcome check is supplied."
))
@pytest.mark.parametrize("retain_object", [True, False], ids=["explicit-object", "object-omitted"])
def test_natural_generator_exit_requirement_is_not_proved_by_content_checks(tmp_path, retain_object):
    """Keep the unsupported semantic requirement visible, without an old error-code oracle.

    No formal stage check is added: that would test a different requirement
    channel and hide the natural-language coverage boundary.
    """
    task = ("Generate result.txt containing 49 followed by a newline. "
            "The generator process itself must exit with status 0.")
    state = _state(tmp_path, task)
    target = _produce(state, exit_code=7)
    frozen_a = obligations.obligations_path(state).read_bytes()
    result = _close(state, task, [_check(target, b"49\n")],
                    [{"path": str(target)}] if retain_object else None)
    _assert_read_executed(state)
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    assert target.read_bytes() == b"49\n"
    assert any(row.get("kind") == "action_settled" and row.get("exit_code") == 7
               for row in ledger.open_ledger(state).records())
    assert result.get("status") == "success_unsupported", result
    assert delivery_transaction.read_manifest(state) is None


def test_glob_wrong_primary_field_explains_the_legal_correction(tmp_path):
    task = "Create one result file containing 49."
    state = _state(tmp_path, task)
    _produce(state)
    first = _close(state, task, [{"kind": "glob", "path": "runtime/*.txt"}])
    assert first.get("error_code") == "finish_parameters_invalid", first
    rows_before = ledger.open_ledger(state).records()
    assert not any(row.get("tool") == "finish.check" for row in rows_before)
    corrected = _close(state, task, [{"kind": "glob", "pattern": "runtime/*.txt", "min_count": 1}])
    assert corrected.get("status") == "success", corrected
    assert "pattern" in first.get("error", ""), first
