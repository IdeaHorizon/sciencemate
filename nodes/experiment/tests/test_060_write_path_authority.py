"""The guard, capture and actual writer consume the same Core-resolved path.

Use the real writer and ledger, with Project and direct-node coordinates.
These are bounded filesystem checks, not assertions about process isolation.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.project_workspace import _git, resolve_tool_path, working_directory
from core.state import State
from nodes.experiment.tools import ledger, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles


@pytest.fixture(params=[False, True], ids=["direct", "project"])
def state(tmp_path, request):
    project = None
    if request.param:
        project = tmp_path / "project"
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "-c", "user.name=Path Test", "-c", "user.email=path@example.test",
             "commit", "--allow-empty", "-qm", "Initialize path test")
    result = State.new("experiment", tmp_path / "runs", project_id="path-test",
                       session_id="owner", project_worktree=project)
    result.hook_state["node_inputs"] = {"experiment_spec": "Write the requested text file."}
    established = asyncio.run(run_bootstrap.establish(result))
    assert ledger.bootstrap_phase(result) == "ready", established
    return result


def _root(state):
    return next(Path(r.path) for r in collect_path_roles(state) if r.role == "run_root")


def _writes(state):
    return [r for r in ledger.open_ledger(state).records()
            if r["kind"] == "action_started" and r["tool"] == "safe_write_file"]


@pytest.mark.parametrize("spelling", ["relative", "absolute", "literal"])
def test_writer_and_observation_use_core_coordinates(state, spelling, monkeypatch):
    root = _root(state)
    # This is a literal filename, not shell syntax. Core does not expand $VAR.
    monkeypatch.setenv("WRITE_PATH_LITERAL", "/etc")
    target = root / ("$WRITE_PATH_LITERAL/report 'quoted'.txt" if spelling == "literal" else "nested/result.txt")
    path = str(target) if spelling == "absolute" else str(target.relative_to(working_directory(state)))
    expected = resolve_tool_path(state, path, write=True)
    assert expected == target
    result = asyncio.run(safe_bash._safe_write_file(state, path=path, content="verified\n"))
    assert result["status"] == "success", result
    assert result["path"] == str(expected)
    assert expected.read_text() == "verified\n"
    rows = ledger.open_ledger(state).records()
    observations = ledger.project_output_observations(rows)
    assert any(str(expected) in row.get("created", []) for row in observations), observations
    assert _writes(state)[-1]["candidates"] == [str(expected)]


def test_project_workspace_prefix_follows_core_not_state_root(state):
    if state.project_worktree is None:
        pytest.skip("workspace/ is a Project-only Core coordinate")
    target = _root(state) / "prefix.txt"
    path = "workspace/" + target.relative_to(working_directory(state)).as_posix()
    result = asyncio.run(safe_bash._safe_write_file(state, path, "prefix\n"))
    assert result["status"] == "success", result
    assert result["path"] == str(target)
    assert any(str(target) in row.get("created", []) for row in
               ledger.project_output_observations(ledger.open_ledger(state).records()))


def test_core_rejection_precedes_action_and_parent_creation(state, tmp_path):
    target = tmp_path / "not-owned/nested/output.txt"
    before = _writes(state)
    result = asyncio.run(safe_bash._safe_write_file(state, str(target), "must not write"))
    assert result["status"] == "error", result
    assert not target.parent.exists()
    assert _writes(state) == before


def test_subprocess_grant_does_not_extend_core_file_writer_ownership(state, tmp_path):
    from nodes.experiment.tools.subprocess_policy import register_approved_subprocess_write_root

    outside = tmp_path / "subprocess-only"
    outside.mkdir()
    register_approved_subprocess_write_root(state, str(outside))
    result = asyncio.run(safe_bash._safe_write_file(state, str(outside / "file.txt"), "no"))
    assert result["status"] == "error", result
    assert not (outside / "file.txt").exists()
    assert not _writes(state)


def test_existing_file_still_requires_read_before_overwrite(state):
    target = _root(state) / "existing.txt"
    target.write_text("original\n")
    path = str(target.relative_to(working_directory(state)))
    denied = asyncio.run(safe_bash._safe_write_file(state, path, "replacement\n"))
    assert denied["status"] == "error", denied
    assert target.read_text() == "original\n"
    state.files_read.add(str(target))
    allowed = asyncio.run(safe_bash._safe_write_file(state, path, "replacement\n"))
    assert allowed["status"] == "success", allowed
    assert target.read_text() == "replacement\n"


def test_symlink_cannot_redirect_writer_to_baseline(state):
    baseline = _root(state) / "baseline"
    baseline.mkdir()
    protected = baseline / "original.txt"
    protected.write_text("keep\n")
    state.hook_state.setdefault("path_roles", {})["source_baseline_root"] = str(baseline)
    alias = _root(state) / "alias.txt"
    alias.symlink_to(protected)
    state.files_read.add(str(protected))
    before = _writes(state)
    denied = asyncio.run(safe_bash._safe_write_file(state, str(alias), "overwrite\n"))
    assert denied["status"] != "success", denied
    assert protected.read_text() == "keep\n"
    assert _writes(state) == before
