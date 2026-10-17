"""Adjacent guards for selected delivery lineage and bounded check windows.

Actions and checks use the real execution boundary.  Window mutations are
test-owned external writers, not a claim of escaping the readonly checker.
The observation fault lowers a real snapshot's budget. Fatal cases additionally
supply the existing detector event from real stdout; they do not test detector
wiring. No producer, runner result, identity or verdict is fabricated.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import pytest

from core import sandbox
from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import (
    capture, delivery_transaction, finish, ledger, obligations, run_bootstrap,
    safe_bash,
)


TASK = "Produce the required new results and verify the complete declared delivery."


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.fixture
def execution(tmp_path):
    states = []
    with asyncio.Runner() as runner:
        def establish(*, formal=None):
            state = State.new("experiment", tmp_path / f"runs-{len(states)}", session_id="owner")
            states.append(state)
            output = working_directory(state) / "runtime"
            inputs = {"experiment_spec": TASK}
            if formal == "file":
                inputs["required_outputs"] = [{
                    "id": "formal_bad", "kind": "file", "path": str(output / "a_bad.txt"),
                }]
            elif formal == "minimum":
                inputs["required_outputs"] = [{
                    "id": "formal_minimum", "kind": "glob", "pattern": str(output / "*.txt"),
                    "min_count": 2,
                }]
            state.hook_state["node_inputs"] = inputs
            result = runner.run(run_bootstrap.establish(state))
            assert ledger.bootstrap_phase(state) == "ready", result
            return state, output

        yield runner, establish
        for state in states:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)


def _action(runner, state, output, program, *, exit_code=0):
    result = runner.run(safe_bash._safe_run_bash(
        state, cmd=_command(program), cwd=str(output), timeout=20,
    ))
    assert result.get("returncode") == exit_code, result
    starts = [row for row in ledger.open_ledger(state).records()
              if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]
    action_id = starts[-1]["action_id"]
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == action_id
               and row.get("exit_code") == exit_code
               for row in ledger.open_ledger(state).records())
    if "FATAL: result validation failed" in result.get("stdout_tail", ""):
        # Supply only the domain detector projection of the actual stdout signal.
        ledger.record(state, "action_settled", action_id=action_id, state="settled",
                      exit_code=exit_code, fatal_signals=["FATAL: result validation failed"])
    return action_id


def _write(runner, state, output, name, *, exit_code=0, fatal=False):
    return _action(runner, state, output,
                   f"from pathlib import Path; Path({name!r}).write_text('49\\n'); "
                   + ("print('FATAL: result validation failed'); " if fatal else "")
                   + f"raise SystemExit({exit_code})", exit_code=exit_code)


def _glob_finish(runner, state, output, *, deliverables=None):
    return runner.run(finish._finish(
        state, outcome="success", summary="The selected results are ready.",
        deliverables=deliverables,
        verification=[{"requirement": TASK, "checks": [{
            "kind": "glob", "pattern": str(output / "*.txt"), "min_count": 1,
        }]}],
    ))


@pytest.mark.parametrize("change", ["content", "identity"])
def test_unselected_candidate_cannot_hide_same_action_in_explicit_delivery_lineage(execution, change):
    runner, establish = execution
    state, output = establish()
    _write(runner, state, output, "z_good.txt")
    selected = output / "selected.bin"
    original = _write(runner, state, output, selected.name)
    mutation = ("p.write_text('17\\n')" if change == "content" else
                "s = p.stat(); os.utime(p, ns=(s.st_atime_ns, s.st_mtime_ns + 1000000000))")
    failed = _action(runner, state, output,
                     "import os; from pathlib import Path; "
                     "Path('a_bad.txt').write_text('failed candidate\\n'); "
                     f"p = Path({selected.name!r}); {mutation}; print('FATAL: result validation failed'); raise SystemExit(7)", exit_code=7)
    records = ledger.open_ledger(state).records()
    rows = finish._producer_rows(records, selected)
    if change == "identity":
        assert any(row["action_id"] == original and failed in row.get("identity_adjustments", [])
                   for row in rows), rows
    else:
        assert failed in [row["action_id"] for row in rows], rows
    glob = finish._glob_evidence(state, records, {"pattern": str(output / "*.txt"), "min_count": 1})
    assert glob["passed"] and glob["evidence"]["selected"] == [str(output / "z_good.txt")]
    frozen_a = obligations.obligations_path(state).read_bytes()

    result = _glob_finish(runner, state, output, deliverables=[{"path": str(selected)}])

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("action") == failed for row in result.get("contradictions", [])), result
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    assert delivery_transaction.read_manifest(state) is None


@pytest.mark.parametrize("constraint", ["explicit", "file", "minimum"])
def test_failed_candidate_remains_required_when_another_constraint_selects_it(execution, constraint):
    runner, establish = execution
    state, output = establish(formal=None if constraint == "explicit" else constraint)
    _write(runner, state, output, "z_good.txt")
    failed = _write(runner, state, output, "a_bad.txt", exit_code=7, fatal=True)
    frozen_a = obligations.obligations_path(state).read_bytes()

    result = _glob_finish(runner, state, output,
                          deliverables=[{"path": str(output / "a_bad.txt")}] if constraint == "explicit" else None)

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("action") == failed for row in result.get("contradictions", [])), result
    if constraint == "minimum":
        assert any(row.get("obligation") == "formal_minimum" and row["evidence"]["min_count"] == 2
                   for row in result.get("gaps", [])), result
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    assert delivery_transaction.read_manifest(state) is None


@pytest.mark.parametrize("window", ["stable", "container-metadata", "same-name-rewrite", "incomplete-before"])
def test_output_directory_check_uses_complete_consistent_member_window(execution, monkeypatch, window):
    runner, establish = execution
    state, output = establish()
    bundle = output / "bundle"
    _action(runner, state, output,
            "from pathlib import Path; p = Path('bundle'); p.mkdir(); "
            "(p / 'a.txt').write_text('49\\n'); (p / 'b.txt').write_text('49\\n')")
    frozen_a = obligations.obligations_path(state).read_bytes()
    actual_check = finish._check_runner
    actual_snapshot = capture.snapshot
    observed = {}

    def limited_before(candidates, roots=(), **kwargs):
        if (window == "incomplete-before" and not observed.get("limited_before")
                and list(roots) == [bundle]):
            observed["limited_before"] = True
            result = actual_snapshot(candidates, roots, **{**kwargs, "max_files": 1})
            assert result["truncated"] is True, result
            return result
        return actual_snapshot(candidates, roots, **kwargs)

    async def checked_then_external_write(actual_state, ob):
        result = await actual_check(actual_state, ob)
        assert result.get("executed") is True and result.get("exit_code") == 0, result
        assert "MEMBERS_CHECKED" in result.get("stdout_tail", ""), result
        observed["real_check_passed"] = True
        if window == "same-name-rewrite":
            before = bundle.stat()
            # Real in-place, same-size content mutation. No path was added or
            # removed, and a directory-only metadata comparison cannot see it.
            (bundle / "b.txt").write_text("17\n")
            assert bundle.stat().st_mtime_ns == before.st_mtime_ns
        elif window == "container-metadata":
            before = bundle.stat()
            os.utime(bundle, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
            assert bundle.stat().st_mtime_ns != before.st_mtime_ns
            assert (bundle / "a.txt").read_bytes() == (bundle / "b.txt").read_bytes() == b"49\n"
        return result

    monkeypatch.setattr(capture, "snapshot", limited_before)
    monkeypatch.setattr(finish, "_check_runner", checked_then_external_write)
    result = runner.run(finish._finish(
        state, outcome="success", summary="The complete bundle was checked.",
        deliverables=[{"path": str(bundle)}],
        verification=[{"requirement": TASK, "checks": [{
            "kind": "check", "cwd": str(output), "timeout_s": 20,
            "command": _command(f"from pathlib import Path; p = Path({str(bundle)!r}); "
                                "assert sorted(q.name for q in p.iterdir()) == ['a.txt', 'b.txt']; "
                                "assert (p / 'a.txt').read_bytes() == b'49\\n'; "
                                "assert (p / 'b.txt').read_bytes() == b'49\\n'; print('MEMBERS_CHECKED')"),
            "expect_exit": 0, "expect_stdout_contains": "MEMBERS_CHECKED",
        }]}],
    ))

    assert observed.get("real_check_passed") is True
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    if window in {"stable", "container-metadata"}:
        assert result.get("status") == "success" and result.get("outcome") == "success", result
        assert delivery_transaction.read_manifest(state) is not None
    else:
        assert result.get("status") == "success_unsupported", result
        gaps = [gap for row in result.get("gaps", []) for gap in row.get("evidence", {}).get("member_gaps", [])]
        if window == "incomplete-before":
            assert observed.get("limited_before") is True
            assert any(row["gap"] == "directory_before_observation_incomplete" for row in gaps), result
            assert not any("changed_during_check" in row["gap"] for row in gaps), result
        assert delivery_transaction.read_manifest(state) is None
