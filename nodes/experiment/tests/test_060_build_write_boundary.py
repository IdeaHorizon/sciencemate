"""Build location follows the actual write boundary, not a retired route role."""
from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path

import pytest

from core import sandbox
from core.project_workspace import _git
from core.state import State
from nodes.experiment.tools import ledger, resource_manager, run_bootstrap, safe_bash
from nodes.experiment.tools.subprocess_policy import bash_sandbox_roots
from nodes.experiment.tools.path_roles import collect_path_roles


def _run(value):
    return asyncio.run(value)


@pytest.fixture
def project_state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=Boundary Test", "-c", "user.email=boundary@example.test",
         "commit", "--allow-empty", "-qm", "Initialize isolated build fixture")
    state = State.new(
        "experiment", project / ".research/runtime/runs", project_id="build-boundary",
        session_id="owner", project_worktree=project,
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Build the provided program and keep its output.",
        "prereg_assignment": {"kind": "none", "reason": "software build"},
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    yield state
    if state.sandbox_manifest:
        sandbox.evict_state_attempt(state)


def _makefile(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "Makefile").write_text("all:\n\tprintf 'BUILT\\n' > built.txt\n")


@pytest.mark.parametrize("relative", ["custom build", "fvcom_build/deps/gklib"])
def test_real_build_in_owned_workspace_does_not_require_the_build_root_name(project_state, relative):
    if not shutil.which("make"):
        pytest.skip("make unavailable")
    state = project_state
    cwd = Path(state.workspace_root) / relative
    _makefile(cwd)
    result = _run(safe_bash._safe_run_bash(state, cmd="make -j1", cwd=str(cwd), timeout=30))
    assert result.get("returncode") == 0, result
    assert (cwd / "built.txt").read_text() == "BUILT\n"
    assert any(row["kind"] == "action_settled" for row in ledger.open_ledger(state).records())


def test_explicit_make_directory_uses_the_same_authorized_boundary(project_state):
    state = project_state
    cwd = Path(state.workspace_root)
    output = cwd / "dependency build"
    _makefile(output)
    result = safe_bash._activity_path_role_guard(
        state, f"make -C {shlex.quote(str(output))} -j1", cwd=str(cwd),
    )
    assert result is None, result


@pytest.mark.parametrize("source_worktree", [False, True])
def test_local_submission_dry_run_accepts_the_same_owned_build_directory(project_state, source_worktree):
    state = project_state
    cwd = Path(state.workspace_root) / "deps/small"
    _makefile(cwd)
    if source_worktree:
        _git(cwd, "init", "-q")
        _git(cwd, "-c", "user.name=Boundary Test", "-c", "user.email=boundary@example.test",
             "commit", "--allow-empty", "-qm", "Initialize isolated source worktree")
        state.hook_state["path_roles"] = {
            "source_worktree_root": {"path": str(cwd), "writable": True},
        }
    result = _run(resource_manager._submit_job(
        state, command="make -j1", scheduler="local", workdir=str(cwd),
        dry_run=True, cpus_per_rank=1, memory_gb=1, walltime_minutes=1,
    ))
    assert result.get("status") == "success", result
    assert not (cwd / "built.txt").exists(), "dry-run must not execute the build"


def test_real_local_managed_build_finishes_in_the_owned_custom_directory(project_state):
    state = project_state
    cwd = Path(state.workspace_root) / "managed dependency"
    _makefile(cwd)
    result = _run(resource_manager._submit_job(
        state, command="make -j1", scheduler="local", workdir=str(cwd),
        dry_run=False, cpus_per_rank=1, memory_gb=1, walltime_minutes=1,
        hard_deadline_s=10, foreground_wait_s=20,
    ))
    try:
        assert result.get("status") == "success", result
        status = _run(resource_manager._job_status(state, "local", result["job_id"]))
        assert resource_manager._scheduler_phase("local", status) == "terminal", status
        assert (cwd / "built.txt").read_text() == "BUILT\n"
    finally:
        # A failed assertion may not leave the test-owned physical job running.
        if result.get("job_id"):
            status = _run(resource_manager._job_status(state, "local", result["job_id"]))
            if resource_manager._scheduler_phase("local", status) != "terminal":
                _run(resource_manager._cancel_job(
                    state, "local", result["job_id"], reason="bounded regression cleanup",
                ))


def test_in_tree_build_reaches_the_real_backend_after_consuming_permission_without_a_route(project_state):
    from shared.lib import dangerous_commands as danger

    state = project_state
    cwd = Path(state.workspace_root) / "trusted-source"
    _makefile(cwd)
    _git(cwd, "init", "-q")
    _git(cwd, "-c", "user.name=Boundary Test", "-c", "user.email=boundary@example.test",
         "commit", "--allow-empty", "-qm", "Initialize isolated source worktree")
    state.hook_state["path_roles"] = {
        "source_worktree_root": {"path": str(cwd), "writable": True},
    }
    cmd = "make -j1"
    approval = safe_bash._scope_authorization_text(
        kind="bash", cmd=cmd, cwd=str(cwd), target=str(cwd),
        scope="source_worktree_modification", op="make",
    )
    danger.mark_confirmed(state, approval)
    result = _run(safe_bash._safe_run_bash(state, cmd=cmd, cwd=str(cwd), timeout=30))
    assert not danger.is_confirmed(state, approval)
    if "GIT_UNWRITABLE cannot be enforced" in result.get("error", ""):
        # Landlock cannot rebind a complete Git worktree while denying .git.
        # Removing the route ceremony must not suppress Core's capability veto.
        assert not (cwd / "built.txt").exists()
    else:
        assert result.get("returncode") == 0, result
        assert (cwd / "built.txt").read_text() == "BUILT\n"


def test_real_build_cannot_write_readonly_dependency_or_sibling_node(project_state):
    state = project_state
    build = next(Path(role.path) for role in collect_path_roles(state) if role.role == "build_root")
    dependency = build / "readonly-dependency"
    other_node = Path(state.project_worktree) / "figures"
    for target in (dependency, other_node):
        target.mkdir(parents=True, exist_ok=True)
        (target / "kept.txt").write_text("unchanged\n")
    state.hook_state["path_roles"] = {
        "dependency_root": {"path": str(dependency), "writable": False},
    }
    # Payload-originated writes are intentionally not visible in command text:
    # the real sandbox, not just the shell parser, must refuse them.
    (build / "Makefile").write_text(
        "all:\n\tif printf BAD > " + shlex.quote(str(dependency / "kept.txt"))
        + "; then exit 31; fi\n\tif printf BAD > " + shlex.quote(str(other_node / "kept.txt"))
        + "; then exit 32; fi\n\tprintf 'BOUNDARY_OK\\n' > built.txt\n"
    )
    # Core's allow-list backend preserves a readonly hole by carving the root;
    # existing child files can be written, but parent-level creation can be
    # unavailable. This negative probe does not claim stronger capabilities.
    (build / "built.txt").write_text("before\n")
    result = _run(safe_bash._safe_run_bash(state, cmd="make -j1", cwd=str(build), timeout=30))
    assert result.get("returncode") == 0, result
    assert (build / "built.txt").read_text() == "BOUNDARY_OK\n"
    assert (dependency / "kept.txt").read_text() == "unchanged\n"
    assert (other_node / "kept.txt").read_text() == "unchanged\n"


@pytest.mark.parametrize("location", ["other_node", "state", "symlink", "baseline"])
def test_owned_workspace_does_not_authorize_protected_build_destinations(project_state, location):
    state = project_state
    project = Path(state.project_worktree)
    if location == "state":
        target = Path(state.root)
    elif location == "baseline":
        target = Path(state.workspace_root) / "immutable"
        target.mkdir()
        state.hook_state["path_roles"] = {
            "source_baseline_root": {"path": str(target), "writable": False},
        }
    else:
        target = project / "figures/other-owner"
        target.mkdir(parents=True)
    cwd = target
    if location == "symlink":
        cwd = Path(state.workspace_root) / "apparently-owned"
        cwd.symlink_to(target, target_is_directory=True)
    result = safe_bash._activity_path_role_guard(state, "make -j1", cwd=str(cwd))
    assert result is not None, location
    assert result.get("status") == "error", result


def test_nested_readonly_hole_stays_readonly_even_in_a_writable_build_root(project_state):
    state = project_state
    build = next(Path(role.path) for role in collect_path_roles(state) if role.role == "build_root")
    protected = build / "immutable"
    protected.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "dependency_root": {"path": str(protected), "writable": False},
    }
    writable, readonly = bash_sandbox_roots(state, str(build))
    assert any(protected.is_relative_to(root) for root in writable), writable
    assert protected in readonly
    result = safe_bash._activity_path_role_guard(state, "make -j1", cwd=str(protected))
    assert result is not None
