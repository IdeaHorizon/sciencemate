"""Finish reads model tool coordinates without widening file capabilities.

The Project and run-state directories are deliberately different.  Checks use
the real sandbox executor; successful cwd assertions are not mocked verdicts.
"""
from __future__ import annotations

import asyncio
import glob
import shlex
import sys
from pathlib import Path

import pytest

from core import sandbox
from core.project_workspace import ProjectWorkspaceError, _git, working_directory
from core.state import State
from nodes.experiment.tools import action_boundary, capture, finish, ledger, obligations, run_bootstrap
from nodes.experiment.tools.path_roles import collect_path_roles


def _run(awaitable):
    return asyncio.run(awaitable)


def _command(program: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.fixture(params=[False, True], ids=["standalone", "project"])
def state(tmp_path, request):
    baseline = tmp_path / "provided-baseline"
    baseline.mkdir()
    (baseline / "input.txt").write_text("supplied input\n")
    project = None
    if request.param:
        project = tmp_path / "project"
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "-c", "user.name=Path Test", "-c", "user.email=path@example.test",
             "commit", "--allow-empty", "-qm", "Initialize isolated fixture")
    run_base = project / ".research/runtime/runs" if project else tmp_path / "runs"
    current = State.new("experiment", run_base, project_id="path-contract", session_id="owner",
                        project_worktree=project)
    current.hook_state["node_inputs"] = {
        "experiment_spec": "Audit the supplied input and report its measured contents.",
        "prereg_assignment": {"kind": "none", "reason": "deterministic path contract check"},
        "path_roles": {"source_baseline_root": {"path": str(baseline), "writable": False}},
    }
    established = _run(run_bootstrap.establish(current))
    assert ledger.bootstrap_phase(current) == "ready", established
    try:
        yield current
    finally:
        if current.sandbox_manifest:
            sandbox.evict_state_attempt(current)


def test_default_check_cwd_uses_core_tool_anchor_not_hidden_state(state):
    workspace = working_directory(state)
    assert workspace != state.root
    (workspace / "where.txt").write_text("workspace")
    (state.root / "where.txt").write_text("hidden state")
    result = _run(finish._default_check_runner(state, {
        "command": _command(
            "from pathlib import Path; "
            f"assert Path.cwd().resolve() == Path({str(workspace)!r}).resolve(); "
            "assert Path('where.txt').read_text() == 'workspace'; print('CWD_OK')"
        ), "timeout_s": 20,
    }))
    assert result.get("executed") is True and result.get("exit_code") == 0, result
    assert "CWD_OK" in result["stdout_tail"]


def test_file_and_glob_share_the_core_relative_anchor(state):
    workspace = working_directory(state)
    folder = workspace / "data"
    folder.mkdir()
    expected = folder / "one.txt"
    expected.write_text("one\n")
    assert obligations.resolve_path(state, "data/one.txt") == expected
    assert glob.glob(str(obligations.resolve_path(state, "data/*.txt"))) == [str(expected)]
    if state.project_worktree is not None:
        # Cross-node / record-root coordinates are also part of Core's read
        # dialect.  Resolve the existing fixed prefix before adding the glob.
        assert obligations.resolve_path(state, "experiments/data/one.txt") == expected
        assert glob.glob(str(obligations.resolve_path(state, "experiments/data/*.txt"))) == [str(expected)]
        assert obligations.resolve_path(state, "project/experiments/data/one.txt") == expected


def test_explicit_relative_check_cwd_uses_same_anchor(state):
    folder = working_directory(state) / "check-dir"
    folder.mkdir()
    (folder / "value.txt").write_text("ok")
    result = _run(finish._default_check_runner(state, {
        "cwd": "check-dir",
        "command": _command("from pathlib import Path; assert Path('value.txt').read_text() == 'ok'"),
        "timeout_s": 20,
    }))
    assert result.get("executed") is True and result.get("exit_code") == 0, result


def test_external_readonly_role_is_usable_as_verification_cwd(state):
    baseline = Path(state.hook_state["node_inputs"]["path_roles"]["source_baseline_root"]["path"])
    result = _run(finish._default_check_runner(state, {
        "cwd": str(baseline), "timeout_s": 20,
        "command": _command(
            "from pathlib import Path; assert Path('input.txt').read_text() == 'supplied input\\n'; "
            "print('INPUT_OK')"
        ),
    }))
    assert result.get("executed") is True and result.get("exit_code") == 0, result
    assert "INPUT_OK" in result["stdout_tail"]
    assert all(not baseline.is_relative_to(Path(root))
               for root in result["execution_profile"]["writable_roots"])


def test_project_symlink_cannot_turn_an_unapproved_external_path_into_a_tool_path(state, tmp_path):
    if state.project_worktree is None:
        pytest.skip("Core standalone reads are intentionally not Project-confined")
    outside = tmp_path / "unapproved"
    outside.mkdir()
    secret = outside / "outside.txt"
    secret.write_text("not project input")
    link = working_directory(state) / "alias"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectWorkspaceError):
        obligations.resolve_path(state, "alias/outside.txt")
    with pytest.raises(ProjectWorkspaceError):
        obligations.resolve_path(state, "alias/*.txt")
    verdict = finish._file_evidence(state, [], {"path": "alias/outside.txt"})
    assert verdict["passed"] is False and verdict["gap"] == "path_unavailable"


def test_input_reference_requires_a_trusted_input_role_not_merely_existing_bytes(state):
    baseline = Path(state.hook_state["node_inputs"]["path_roles"]["source_baseline_root"]["path"])
    supplied = obligations.resolve_path(state, str(baseline / "input.txt"))
    reference = obligations.input_reference(state, supplied)
    assert reference and reference["role"] == "source_baseline_root"
    output = working_directory(state) / "stale-result.txt"
    output.write_text("pre-existing output is not an input role")
    assert obligations.input_reference(state, output) is None


def test_unreadable_glob_is_a_reportable_gap_not_an_uncaught_exception(state, tmp_path):
    if state.project_worktree is None:
        pytest.skip("Core standalone reads are intentionally not Project-confined")
    outside = tmp_path / "outside-glob"
    outside.mkdir()
    (outside / "result.txt").write_text("not authorized")
    row = finish._glob_evidence(state, [], {"pattern": str(outside / "*.txt")})
    assert row["passed"] is False and row["gap"] == "path_unavailable", row


def test_private_python_scratch_is_not_promoted_to_business_output_root(state):
    runtime = next(Path(role.path) for role in collect_path_roles(state) if role.role == "run_root")
    scratch = runtime / ".python-scratch"
    scratch.mkdir(exist_ok=True)
    # Exact capability shape of readonly runtime plus the narrow Python
    # scratch exception.  This tests the pure observation-plan intersection,
    # not a claim that this operation fixture is a scientific execution.
    plan = action_boundary.observation_boundary(state, [scratch], [runtime])
    assert not any(scratch.is_relative_to(Path(root)) for root in plan["roots"]), plan


def test_action_intent_is_durable_before_the_observation_window_opens(state, monkeypatch):
    runtime = next(Path(role.path) for role in collect_path_roles(state) if role.role == "run_root")
    original = capture.snapshot
    starts_seen_at_snapshot = []

    def inspect_order(*args, **kwargs):
        starts_seen_at_snapshot.append([
            row for row in ledger.open_ledger(state).records()
            if row.get("kind") == "action_started" and row.get("tool") == "snapshot-order-probe"
        ])
        return original(*args, **kwargs)

    monkeypatch.setattr(capture, "snapshot", inspect_order)
    handle = action_boundary.begin(
        state, tool="snapshot-order-probe", work_root=str(runtime), output_roots=[runtime],
    )
    assert isinstance(handle, action_boundary.ActionHandle), handle
    settled = action_boundary.settle(state, handle, outcome="settled", exit_code=0)
    assert settled["status"] == "recorded", settled
    assert starts_seen_at_snapshot and starts_seen_at_snapshot[0], (
        "A second recorded action can complete during the before-scan; recording "
        "this action only after that scan hides the overlapping observation window."
    )
    assert starts_seen_at_snapshot[0][0]["action_id"] == handle.action_id
