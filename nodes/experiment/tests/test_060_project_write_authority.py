"""Project ownership comes from Core even when a run has old write grants."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.project_workspace import _git
from core.state import State
from nodes.experiment.tools import run_bootstrap, safe_bash, subprocess_policy
from nodes.experiment.tools.path_roles import collect_path_roles


@pytest.fixture
def state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=Path Test", "-c", "user.email=path@example.test",
         "commit", "--allow-empty", "-qm", "Initialize path test")
    run = State.new("experiment", tmp_path / "runs", project_id="path-test",
                    session_id="owner", project_worktree=project)
    run.hook_state["node_inputs"] = {"experiment_spec": "Verify path ownership."}
    asyncio.run(run_bootstrap.establish(run))
    return run


def test_project_cwd_rejects_legacy_project_root_grant(state):
    project = state.project_worktree
    state.hook_state["path_roles"] = {"approved_write_root": str(project)}
    probe = asyncio.run(safe_bash._safe_run_bash(
        state, "pwd; echo hi", cwd=str(project), timeout=10))
    assert probe.get("reason") == safe_bash.WORKDIR_UNAVAILABLE, probe
    assert "pause_event" not in probe

    state.hook_state["_approved_subprocess_write_roots"] = [str(project)]

    result = asyncio.run(safe_bash._safe_run_bash(
        state, 'mkdir -p "工作区 空格"', cwd=str(project), timeout=10))
    assert result["status"] == "error", result
    assert result.get("reason") == safe_bash.WORKDIR_UNAVAILABLE, result
    assert "pause_event" not in result
    assert not (project / "工作区 空格").exists()
    assert all(role.role != "approved_write_root" for role in collect_path_roles(state))


def test_project_write_uses_core_node_ownership(state):
    project = state.project_worktree
    sibling = project / "literature" / "report.txt"
    own = Path(state.workspace_root) / "工作区 空格" / "out.txt"
    assert safe_bash._classify_write_path(sibling, state)[0] == "outside_project_workspace"
    assert safe_bash._classify_write_path(own, state)[0] == "safe"


def test_only_external_approvals_are_reusable(state, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    for path in (state.project_worktree, state.project_worktree.parent):
        with pytest.raises(subprocess_policy.PathRoleSandboxContractError):
            subprocess_policy.register_approved_subprocess_write_root(state, str(path))
    subprocess_policy.register_approved_subprocess_write_root(state, str(external))
    assert subprocess_policy.approved_subprocess_write_roots(state) == [external]


def test_core_authorized_file_delivery_preserves_target(state):
    state.hook_state["_deliverable_writes"] = True
    target = state.project_worktree / "交付 空格" / "report.txt"
    result = asyncio.run(safe_bash._safe_write_file(state, str(target), "verified\n"))
    assert result["status"] == "success", result
    assert target.read_text(encoding="utf-8") == "verified\n"
