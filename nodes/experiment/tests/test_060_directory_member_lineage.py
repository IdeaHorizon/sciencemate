"""Whole-directory delivery includes empty leaves, not only regular files.

Real sandboxed actions and readonly finish checks, with no mocked execution,
producer observations or verdict. The task has prose only: checks belong to C,
and cannot fabricate a producer for an unobserved member of the delivered tree.
"""
from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from core import sandbox
from core.project_workspace import _git, working_directory
from core.state import State
from nodes.experiment.tools import capture, delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


TASK = ("Create and deliver the bundle tree with fresh empty leaf directories; "
        "any result.txt file must contain exactly 49 followed by a newline.")


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.fixture(params=["project", "direct"])
def directory_run(tmp_path, request):
    with asyncio.Runner() as runner:
        if request.param == "project":
            project = tmp_path / "project"
            project.mkdir()
            _git(project, "init", "-q")
            _git(project, "-c", "user.name=Directory Lineage Test", "-c",
                 "user.email=directory@example.test", "commit", "--allow-empty", "-qm", "fixture")
            state = State.new("experiment", project / ".research/runtime/runs",
                              project_id="directory-lineage", session_id="owner", project_worktree=project)
        else:
            state = State.new("experiment", tmp_path / "runs", session_id="owner")
        state.hook_state["node_inputs"] = {
            "experiment_spec": TASK,
            "prereg_assignment": {"kind": "none", "reason": "deterministic directory lineage test"},
        }
        established = runner.run(run_bootstrap.establish(state))
        assert ledger.bootstrap_phase(state) == "ready", established
        assert not any(row["kind"] in {"file", "glob"} for row in obligations.frozen(state))
        workspace = working_directory(state)
        cwd = workspace / "runtime/child"
        cwd.mkdir()
        bundle = workspace / "runtime/bundle"
        assert len({state.root, workspace, cwd, bundle}) == 4
        try:
            yield state, workspace, cwd, bundle, runner
        finally:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)


def _payload(case, statements, *, exit_code=0):
    state, _, cwd, _, runner = case
    result = runner.run(safe_bash._safe_run_bash(
        state, cwd=str(cwd), timeout=20,
        cmd=_command("from pathlib import Path; p = Path.cwd().parent / 'bundle'; "
                     + statements + f"; raise SystemExit({exit_code})"),
    ))
    assert result.get("returncode") == exit_code, result
    rows = ledger.open_ledger(state).records()
    started = [row for row in rows if row.get("kind") == "action_started"
               and row.get("tool") == "safe_run_bash"][-1]
    settled = next(row for row in reversed(rows) if row.get("kind") == "action_settled"
                   and row.get("action_id") == started["action_id"])
    assert settled["exit_code"] == exit_code, settled
    return started["action_id"]


def _close(case, expected_members):
    state, workspace, _, bundle, runner = case
    frozen_a = obligations.obligations_path(state).read_bytes()
    result = runner.run(finish._finish(
        state, outcome="success", summary="Deliver the whole bundle with checked contents.",
        deliverables=[{"path": str(bundle.relative_to(workspace))}],
        verification=[{"requirement": TASK, "checks": [{
            "kind": "check", "cwd": "runtime", "timeout_s": 20,
            "command": _command(
                "from pathlib import Path; p = Path('bundle'); assert p.is_dir(); "
                f"assert sorted(str(x.relative_to(p)) for x in p.rglob('*')) == {sorted(expected_members)!r}; "
                "assert all(x.read_bytes() == b'49\\n' for x in p.rglob('*') if x.is_file()); "
                "print('TREE_CONTENT_OK')"),
            "expect_exit": 0, "expect_stdout_contains": "TREE_CONTENT_OK",
        }]}],
    ))
    rows = ledger.open_ledger(state).records()
    check_ids = {row["action_id"] for row in rows if row.get("kind") == "action_started"
                 and row.get("tool") == "finish.check"}
    checks = [row for row in rows if row.get("kind") == "action_settled"
              and row.get("action_id") in check_ids]
    assert len(checks) == 1 and checks[0].get("executed") is True and checks[0]["exit_code"] == 0, checks
    assert obligations.obligations_path(state).read_bytes() == frozen_a
    return result


def test_failed_empty_leaf_remains_in_whole_directory_lineage(directory_run):
    state, _, _, bundle, _ = directory_run
    failed_id = _payload(directory_run, "(p / 'stage').mkdir(parents=True)", exit_code=7)
    _payload(directory_run, "(p / 'result.txt').write_text('49\\n')")
    leaf_rows = finish._producer_rows(ledger.open_ledger(state).records(), bundle / "stage")
    assert [row["action_id"] for row in leaf_rows] == [failed_id], leaf_rows
    result = _close(directory_run, ["stage", "result.txt"])
    # The task requires this empty leaf, not a zero exit from its whole shell.
    # A real complete-tree check passed; the original producer is not relabeled.
    assert result.get("status") == "success", result
    rows = ledger.open_ledger(state).records()
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == failed_id
               and row.get("exit_code") == 7 for row in rows)
    assert [row["action_id"] for row in finish._producer_rows(rows, bundle / "stage")] == [failed_id]
    assert delivery_transaction.read_manifest(state) is not None


def test_old_unobserved_empty_leaf_is_not_blessed_by_a_new_successful_file(directory_run):
    state, _, _, bundle, _ = directory_run
    (bundle / "stage").mkdir(parents=True)
    _payload(directory_run, "(p / 'result.txt').write_text('49\\n')")
    assert not finish._producer_rows(ledger.open_ledger(state).records(), bundle / "stage")
    result = _close(directory_run, ["stage", "result.txt"])
    assert result.get("status") == "success_unsupported", result
    assert result.get("gaps"), result
    assert any(member.get("path") == str(bundle / "stage")
               for gap in result["gaps"] for member in gap.get("evidence", {}).get("member_gaps", [])), result
    assert delivery_transaction.read_manifest(state) is None


def test_fresh_multilevel_empty_directory_tree_is_deliverable(directory_run):
    _payload(directory_run, "(p / 'first' / 'leaf').mkdir(parents=True); (p / 'second').mkdir()")
    result = _close(directory_run, ["first", "first/leaf", "second"])
    assert result.get("status") == "success" and result.get("outcome") == "success", result


def test_existing_intermediate_container_can_hold_new_successful_empty_leaves(directory_run):
    _, _, _, bundle, _ = directory_run
    (bundle / "container").mkdir(parents=True)
    _payload(directory_run, "(p / 'container' / 'leaf').mkdir()")
    result = _close(directory_run, ["container", "container/leaf"])
    assert result.get("status") == "success" and result.get("outcome") == "success", result


@pytest.mark.parametrize("change_member", [False, True], ids=["stable-input", "same-size-member-overwrite"])
def test_input_directory_window_checks_member_identity_not_only_root(tmp_path, change_member):
    """A real FS observation window, not a claim of readonly sandbox bypass.

    The test harness changes the fixture directly between observations. No
    model command or mocked check is used to pretend the sandbox allowed it.
    Both file inode and size, and the containing directories' identities, stay
    unchanged; the member's byte identity must still participate in evaluation.
    """
    baseline = tmp_path / "input-baseline"
    member = baseline / "nested" / "result.txt"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"49\n")
    state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Inspect the existing input directory without changing it.",
        "prereg_assignment": {"kind": "none", "reason": "input observation window test"},
        "path_roles": {"source_baseline_root": {"path": str(baseline), "writable": False}},
    }
    assert obligations.input_reference(state, baseline) is not None
    before_root = capture.identity(baseline)
    before_members = capture.snapshot([], [baseline], include_directories=True)
    assert not before_members["truncated"]
    assert all(row["status"] == "complete" for row in before_members["root_coverage"])
    before_file = before_members["entries"][str(member)]
    if change_member:
        with member.open("r+b") as stream:
            stream.write(b"50\n")
    assert capture.same_identity(before_root, capture.identity(baseline))[0]
    after_file = capture.identity(member)
    assert (after_file["inode"], after_file["size"]) == (before_file["inode"], before_file["size"])
    result = finish._directory_evidence(
        state, [], baseline, before_check=before_root, before_members=before_members,
    )
    assert result["evidence"]["input_reference"]["role"] == "source_baseline_root"
    assert result["evidence"]["producers"] == []
    if change_member:
        assert after_file["sha256"] != before_file["sha256"]
        assert result["passed"] is False, result
        assert {"path": str(member), "gap": "input_changed_during_check"} in result["evidence"]["member_gaps"]
    else:
        assert result["passed"] is True, result
        assert result["evidence"]["member_gaps"] == []
