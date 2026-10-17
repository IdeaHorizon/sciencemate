"""Independent container edges: empty outputs and known input directories.

These real-tool regressions intentionally fail on the b83 product baseline.
A directory's existence is neither a successful producer nor permission to
rewrite an input.  No new manifest, producer ID or declaration is requested.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from core import sandbox
from core.project_workspace import _git, working_directory
from core.state import State
from nodes.experiment.tools import capture, delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


OUTPUT_TASK = "Create a new empty directory named bundle and deliver it."


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.fixture(params=["project", "direct"])
def make_run(tmp_path, request):
    states = []
    with asyncio.Runner() as runner:
        def create(task, *, input_shape=None):
            baseline = None
            if input_shape is not None:
                baseline = tmp_path / "provided-input"
                target = baseline / "bundle"
                target.mkdir(parents=True)
                if input_shape == "nested":
                    (target / "level1/level2").mkdir(parents=True)
                    (target / "level1/level2/result.txt").write_text("49\n")
            if request.param == "project":
                project = tmp_path / "project"
                project.mkdir()
                _git(project, "init", "-q")
                _git(project, "-c", "user.name=Container Review", "-c", "user.email=container@example.test",
                     "commit", "--allow-empty", "-qm", "fixture")
                state = State.new("experiment", project / ".research/runtime/runs",
                                  project_id="container-edges", session_id="owner", project_worktree=project)
            else:
                state = State.new("experiment", tmp_path / "runs", session_id="owner")
            states.append(state)
            inputs = {"experiment_spec": task,
                      "prereg_assignment": {"kind": "none", "reason": "isolated container contract review"}}
            if baseline is not None:
                inputs["path_roles"] = {"source_baseline_root": {"path": str(baseline), "writable": False}}
            state.hook_state["node_inputs"] = inputs
            established = runner.run(run_bootstrap.establish(state))
            assert ledger.bootstrap_phase(state) == "ready", established
            assert not any(row.get("kind") in {"file", "glob"} for row in obligations.frozen(state))
            workspace = working_directory(state)
            cwd = workspace / "runtime/child"
            cwd.mkdir()
            if baseline is None:
                target = workspace / "runtime/bundle"
            else:
                assert obligations.input_reference(state, target), "The fixture must be a known read-only input."
            assert len({state.root, workspace, cwd, target}) == 4
            return state, runner, cwd, target

        try:
            yield create
        finally:
            for state in states:
                if state.sandbox_manifest:
                    sandbox.evict_state_attempt(state)


def _records(state):
    return ledger.open_ledger(state).records()


def _make_empty(case, *, exit_code=0):
    state, runner, cwd, target = case
    assert not target.exists()
    result = runner.run(safe_bash._safe_run_bash(
        state, cwd=str(cwd), timeout=20,
        cmd=_command("from pathlib import Path; (Path.cwd().parent / 'bundle').mkdir(); "
                     f"raise SystemExit({exit_code})"),
    ))
    assert result.get("returncode") == exit_code, result
    assert target.is_dir() and not list(target.iterdir())
    records = _records(state)
    starts = [row for row in records if row.get("kind") == "action_started"
              and row.get("tool") == "safe_run_bash"]
    assert len(starts) == 1, records
    action_id = starts[0]["action_id"]
    settlements = [row for row in records if row.get("kind") == "action_settled"
                   and row.get("action_id") == action_id]
    assert len(settlements) == 1 and settlements[0]["exit_code"] == exit_code, records
    assert any(row.get("kind") == "observation" and row.get("action_id") == action_id for row in records)
    return action_id


def _close(case, task, *, nested=False):
    state, runner, _, target = case
    before_a = obligations.obligations_path(state).read_bytes()
    content = ("assert sorted(str(q.relative_to(p)) for q in p.rglob('*') if q.is_file()) "
               "== ['level1/level2/result.txt']; assert (p / 'level1/level2/result.txt').read_text() == '49\\n'"
               if nested else "assert not list(p.iterdir())")
    result = runner.run(finish._finish(
        state, outcome="success", summary="The requested directory has been checked.",
        deliverables=[{"path": str(target)}],
        verification=[{"requirement": task, "checks": [{
            "kind": "check", "cwd": "runtime",
            "command": _command(f"from pathlib import Path; p = Path({str(target)!r}); "
                                f"assert p.is_dir(); {content}; print('CONTAINER_CHECK_OK')"),
            "expect_exit": 0, "expect_stdout_contains": "CONTAINER_CHECK_OK", "timeout_s": 20,
        }]}],
    ))
    records = _records(state)
    checks = {row["action_id"] for row in records if row.get("kind") == "action_started"
              and row.get("tool") == "finish.check"}
    settled = [row for row in records if row.get("kind") == "action_settled" and row.get("action_id") in checks]
    assert len(settled) == 1 and settled[0].get("executed") is True and settled[0]["exit_code"] == 0, settled
    assert obligations.obligations_path(state).read_bytes() == before_a
    return result


def test_new_empty_directory_has_a_real_successful_producer(make_run):
    case = make_run(OUTPUT_TASK)
    state, _, _, target = case
    action_id = _make_empty(case)
    result = _close(case, OUTPUT_TASK)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
    assert any(row["action_id"] == action_id for row in finish._producer_rows(_records(state), target)), \
        "Empty outputs require the real directory observation, not vacuous member success."


def test_created_empty_directory_can_pass_real_task_check_after_nonzero_exit(make_run):
    case = make_run(OUTPUT_TASK)
    state, _, _, _ = case
    action_id = _make_empty(case, exit_code=7)
    result = _close(case, OUTPUT_TASK)
    assert result.get("status") == "success", result
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == action_id
               and row.get("exit_code") == 7 for row in _records(state))
    assert delivery_transaction.read_manifest(state) is not None


def test_preexisting_empty_directory_is_not_claimed_as_created_in_this_run(make_run):
    case = make_run(OUTPUT_TASK)
    state, _, _, target = case
    target.mkdir()
    assert not finish._producer_rows(_records(state), target)
    result = _close(case, OUTPUT_TASK)
    assert result.get("status") == "success_unsupported", result
    assert delivery_transaction.read_manifest(state) is None


def test_successful_touch_does_not_replace_the_failed_directory_creator(make_run):
    case = make_run(OUTPUT_TASK)
    state, runner, cwd, target = case
    failed_id = _make_empty(case, exit_code=7)
    before = target.stat()
    touched = runner.run(safe_bash._safe_run_bash(
        state, cwd=str(cwd), timeout=20,
        cmd=_command("import os; from pathlib import Path; p = Path.cwd().parent / 'bundle'; "
                     "st = p.stat(); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))"),
    ))
    assert touched.get("returncode") == 0, touched
    after = target.stat()
    assert after.st_ino == before.st_ino and after.st_mtime_ns != before.st_mtime_ns
    assert not list(target.iterdir())
    result = _close(case, OUTPUT_TASK)
    assert result.get("status") == "success", result
    # The object meets the actual task; touch still cannot invent a new creator.
    producers = finish._producer_rows(_records(state), target)
    assert [row["action_id"] for row in producers] == [failed_id], producers
    assert producers[0].get("identity_adjustments"), producers
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == failed_id
               and row.get("exit_code") == 7 for row in _records(state))
    assert delivery_transaction.read_manifest(state) is not None


@pytest.mark.parametrize("shape", ["empty", "nested"])
def test_known_readonly_input_directory_can_be_audited_without_regeneration(make_run, shape):
    task = ("Audit the provided input directory and confirm it is empty; do not create or rewrite inputs."
            if shape == "empty" else
            "Audit the provided input directory: its only file is level1/level2/result.txt containing 49 and a newline; do not rewrite inputs.")
    case = make_run(task, input_shape=shape)
    state, _, _, target = case
    # Preserve every fixture member's identity, not just the top-level mtime.
    before = {str(path): capture.identity(path) for path in [target, *target.rglob('*')]}
    result = _close(case, task, nested=shape == "nested")
    after = {str(path): capture.identity(path) for path in [target, *target.rglob('*')]}
    assert after == before
    assert not any(row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"
                   for row in _records(state)), "Auditing a supplied input must not require a generator action."
    assert not finish._producer_rows(_records(state), target)
    assert result.get("status") == "success" and result.get("outcome") == "success", result
