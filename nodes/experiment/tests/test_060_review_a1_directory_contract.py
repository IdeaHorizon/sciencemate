"""Directory delivery is not a missing file, nor proof of member provenance.

Independent review-a1 regressions: real Project/direct State, sandboxed payload
and finish check.  No execution, observation or verdict is mocked.  Only a
natural task, an ordinary directory path and a content check are supplied; the
model is not required to construct a manifest or internal producer identity.
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
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


TASK = "Produce the bundle directory containing result.txt with exactly 49 followed by a newline."


@pytest.fixture
def runner():
    # Keep bootstrap, payload and finish on one loop, as in a real run.
    with asyncio.Runner() as value:
        yield value


def _command(program: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.fixture(params=["project", "direct"])
def directory_run(tmp_path, request, runner):
    if request.param == "project":
        project = tmp_path / "project"
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "-c", "user.name=Directory Contract Test", "-c",
             "user.email=directory@example.test", "commit", "--allow-empty", "-qm", "fixture")
        state = State.new("experiment", project / ".research/runtime/runs",
                          project_id="directory-contract", session_id="owner", project_worktree=project)
    else:
        state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": TASK,
        "prereg_assignment": {"kind": "none", "reason": "deterministic directory delivery test"},
    }
    established = runner.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    assert not any(row.get("kind") in {"file", "glob"} for row in obligations.frozen(state))
    workspace = working_directory(state)
    cwd = workspace / "runtime/child"
    cwd.mkdir()
    bundle = workspace / "runtime/bundle"
    assert len({state.root, workspace, cwd, bundle}) == 4
    assert bundle.parent == cwd.parent and not bundle.is_relative_to(cwd)
    try:
        yield state, workspace, cwd, bundle, runner
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


def _produce(case, *, content="49\n", exit_code=0):
    state, _, cwd, bundle, runner = case
    result = runner.run(safe_bash._safe_run_bash(
        state, cwd=str(cwd), timeout=20,
        cmd=_command("from pathlib import Path; p = Path.cwd().parent / 'bundle'; "
                     f"p.mkdir(); (p / 'result.txt').write_text({content!r}); raise SystemExit({exit_code})"),
    ))
    assert result.get("returncode") == exit_code, result
    member = bundle / "result.txt"
    assert bundle.is_dir() and member.read_text() == content
    records = ledger.open_ledger(state).records()
    # The directory bug must be isolated from a lost-capture or path bug.
    rows = finish._producer_rows(records, member)
    assert len(rows) == 1, records
    settlements = [row for row in records if row.get("kind") == "action_settled"
                   and row.get("action_id") == rows[0]["action_id"]]
    assert len(settlements) == 1 and settlements[0]["exit_code"] == exit_code
    return rows[0]["action_id"]


def _finish_directory(case, *, expected_check_exit=0):
    state, workspace, _, bundle, runner = case
    frozen_a = obligations.obligations_path(state).read_bytes()
    result = runner.run(finish._finish(
        state, outcome="success", summary="Deliver the bundle and its checked result.",
        deliverables=[{"path": str(bundle.relative_to(workspace)), "note": "The requested output directory."}],
        verification=[{"requirement": TASK, "checks": [{
            "kind": "check", "cwd": "runtime",
            "command": _command("from pathlib import Path; p = Path('bundle'); "
                                "assert p.is_dir(); assert (p / 'result.txt').read_text() == '49\\n'; "
                                "print('DIRECTORY_CONTENT_OK')"),
            "expect_exit": 0, "expect_stdout_contains": "DIRECTORY_CONTENT_OK", "timeout_s": 20,
        }]}],
    ))
    records = ledger.open_ledger(state).records()
    checks = {row["action_id"] for row in records if row.get("kind") == "action_started"
              and row.get("tool") == "finish.check"}
    settlements = [row for row in records if row.get("kind") == "action_settled"
                   and row.get("action_id") in checks]
    assert len(settlements) == 1 and settlements[0].get("executed") is True, settlements
    assert settlements[0]["exit_code"] == expected_check_exit, settlements
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    return result


def test_fresh_directory_with_observed_successful_member_is_not_a_missing_file(directory_run):
    _produce(directory_run)
    result = _finish_directory(directory_run)
    assert result.get("status") == "success" and result.get("outcome") == "success", result


def test_preexisting_directory_does_not_become_a_new_result_by_passing_content_check(directory_run):
    state, _, _, bundle, _ = directory_run
    bundle.mkdir()
    member = bundle / "result.txt"
    member.write_text("49\n")
    assert not finish._producer_rows(ledger.open_ledger(state).records(), member)
    result = _finish_directory(directory_run)
    assert result.get("status") == "success_unsupported", result
    assert delivery_transaction.read_manifest(state) is None


def test_directory_must_retain_failed_member_provenance_when_content_check_passes(directory_run):
    state, _, _, _, _ = directory_run
    failed_id = _produce(directory_run, exit_code=7)
    result = _finish_directory(directory_run)
    assert result.get("status") == "success", result
    # Passing the real task check does not rewrite the member's failed B fact.
    records = ledger.open_ledger(state).records()
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == failed_id
               and row.get("exit_code") == 7 for row in records)
    member = directory_run[3] / "result.txt"
    assert [row["action_id"] for row in finish._producer_rows(records, member)] == [failed_id]
    assert delivery_transaction.read_manifest(state) is not None


def test_observed_directory_does_not_replace_the_actual_content_check(directory_run):
    state, _, _, _, _ = directory_run
    _produce(directory_run, content="50\n")
    result = _finish_directory(directory_run, expected_check_exit=1)
    assert result.get("status") == "success_unsupported", result
    assert any(row.get("kind") == "check" for row in result.get("gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None
