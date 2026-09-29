"""harness.yaml 指路的 spec 文件，节点必须真的读得到。

2026-08-17 现场：`nodes/_reviewer/harness.yaml` 三处白纸黑字写着
`read_file('nodes/_reviewer/specs/project_synthesis.md')`，而 _reviewer 两次
按它照做拿到「找不到文件：<session>/reviews/nodes/_reviewer/specs/…」（相对路径
锚在节点工作目录），改用绝对仓库路径又拿到「Path escaped the Project boundary」。
两条路都不通 ⇒ `_project` 复盘模式的 spec 100% 读不到，而全套 pytest 是绿的
（测试都不绑 Project，边界层根本不在场）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.loader import node_dir
from core.project_workspace import ProjectWorkspaceError, resolve_tool_path
from core.state import State


def _bound_state(tmp_path, node_type: str) -> State:
    """真的绑一个 Project worktree —— 不绑就测不到边界层。"""
    worktree = tmp_path / "project"
    (worktree / node_type).mkdir(parents=True)
    state = State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()))
    state.project_worktree = worktree
    state.workspace_relative_path = node_type
    return state


def test_repo_relative_own_spec_resolves(tmp_path):
    state = _bound_state(tmp_path, "_reviewer")
    got = resolve_tool_path(state, "nodes/_reviewer/specs/project_synthesis.md")
    assert got == (node_dir("_reviewer") / "specs" / "project_synthesis.md").resolve()
    assert got.is_file(), "harness.yaml 指的这份 spec 必须真的存在且可达"


def test_absolute_own_package_is_readable(tmp_path):
    state = _bound_state(tmp_path, "_reviewer")
    target = node_dir("_reviewer") / "specs" / "project_synthesis.md"
    assert resolve_tool_path(state, str(target)) == target.resolve()


def test_other_nodes_specs_stay_blocked(tmp_path):
    """只读例外严格限自己那个目录 —— 别的节点的 spec 照旧拦。"""
    state = _bound_state(tmp_path, "_reviewer")
    with pytest.raises(ProjectWorkspaceError):
        resolve_tool_path(state, str(node_dir("writing") / "harness.yaml"))


def test_own_package_is_read_only(tmp_path):
    state = _bound_state(tmp_path, "_reviewer")
    target = node_dir("_reviewer") / "specs" / "project_synthesis.md"
    with pytest.raises(ProjectWorkspaceError):
        resolve_tool_path(state, str(target), write=True)


def test_boundary_error_says_what_is_readable(tmp_path):
    """报错要给可行动信息，不能只说"越界了"。"""
    state = _bound_state(tmp_path, "writing")
    with pytest.raises(ProjectWorkspaceError) as excinfo:
        resolve_tool_path(state, "/etc")
    msg = str(excinfo.value)
    assert "可读范围" in msg
    assert str(state.project_worktree) in msg
