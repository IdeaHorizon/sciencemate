from types import SimpleNamespace

import pytest

from core import execution_target, isolation, project_workspace
from core.state import State


@pytest.fixture(autouse=True)
def isolated_target():
    execution_target._reset_for_tests()
    isolation._reset_for_tests()
    yield
    execution_target._reset_for_tests()
    isolation._reset_for_tests()


def test_mapping_reuses_selected_backend_without_handing_out_launch(monkeypatch):
    execution_target.declare("wsl:fixture")
    seen = []
    backend = SimpleNamespace(
        to_windows=lambda p: seen.append(p) or "C:\\project", to_linux=lambda p: "/drives/c/project"
    )
    monkeypatch.setitem(isolation._wsl_cache, "fixture", backend)
    mapper = isolation.target_paths()
    assert mapper.target_spec == "wsl:fixture"
    assert mapper.to_windows("/drives/c/project") == "C:\\project"
    assert mapper.to_linux("C:\\project") == "/drives/c/project"
    assert seen == ["/drives/c/project"]
    assert not any(hasattr(mapper, name) for name in ["prepare", "launch", "terminate"])
    execution_target.declare("local")
    with pytest.raises(isolation.IsolationContractError, match="changed"):
        mapper.to_windows("/drives/c/project")


def test_wsl_absolute_cwd_is_translated_before_project_admission(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    mine = root / "notes"
    mine.mkdir()
    state = State.new("_orchestrator", tmp_path / "runs")
    state.project_worktree = root
    state.workspace_root = mine
    state.workspace_relative_path = "notes"
    execution_target.declare("wsl:fixture")
    backend = SimpleNamespace(
        to_windows=lambda p: str(mine) if p == "/drives/c/project/notes" else None, to_linux=str
    )
    monkeypatch.setitem(isolation._wsl_cache, "fixture", backend)
    monkeypatch.setattr(project_workspace.sys, "platform", "win32")
    assert project_workspace.validate_tool_cwd(state, "/drives/c/project/notes") == mine
    with pytest.raises(project_workspace.ProjectWorkspaceError, match="no host tool mapping"):
        project_workspace.validate_tool_cwd(state, "/home/other")


def test_long_windows_default_scratch_keeps_identity_and_private_scope(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    notes = root / "notes"
    notes.mkdir()
    run_root = root / ".research" / "runtime" / "runs" / ("long-run-" * 28)
    run_root.mkdir(parents=True)
    state = SimpleNamespace(
        root=run_root,
        node_type="_orchestrator",
        project_worktree=root,
        workspace_root=notes,
        workspace_relative_path="notes",
        hook_state={},
    )
    monkeypatch.setattr(project_workspace.sys, "platform", "win32")
    scratch = project_workspace.working_directory(state)
    assert len(str(scratch)) < 248
    assert state.root == run_root
    assert scratch.is_relative_to(root / ".research/runtime/cwd")
    assert (
        project_workspace.resolve_tool_path(state, "draft.txt", write=True) == scratch / "draft.txt"
    )
    from core.sandbox import write_roots_for

    assert scratch in write_roots_for(state)
    other = root / ".research/runtime/cwd" / ("a" * 24) / "draft.txt"
    with pytest.raises(project_workspace.ProjectWorkspaceError):
        project_workspace.resolve_tool_path(state, str(other), write=True)
    (run_root / "scratch").mkdir()
    old = run_root / "scratch" / "old.txt"
    old.write_text("historical")
    assert project_workspace.resolve_tool_path(state, "old.txt") == old
