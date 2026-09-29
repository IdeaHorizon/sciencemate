"""v0.8: 项目嵌套 run 布局测试。

覆盖：
  - paths.runs_parent(project_id) 路由（项目嵌套 vs anon）
  - paths.run_dir(run_id, project_id)
  - paths.find_run_dir(run_id) 跨布局搜索
  - 子 run 通过 state.root.parent 自动继承父 dir (run_node behavior)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.paths import (
    find_run_dir, projects_root, run_dir, runs_anon_root,
    runs_parent, runs_root,
)


def test_runs_parent_with_project_id(tmp_path, monkeypatch):
    bootstrap()
    # conftest 把 HARNESS_FRAMEWORK_HOME 指 tmp_path
    parent = runs_parent("e2e-test-1")
    assert parent == tmp_path / "projects" / "e2e-test-1" / "runs"


def test_runs_parent_anon(tmp_path):
    bootstrap()
    parent = runs_parent(None)
    assert parent == tmp_path / "runs_anon"


def test_run_dir_project_nested(tmp_path):
    bootstrap()
    rd = run_dir("abc123", "proj-x")
    assert rd == tmp_path / "projects" / "proj-x" / "runs" / "abc123"


def test_run_dir_anon(tmp_path):
    bootstrap()
    rd = run_dir("abc123")  # 无 project_id
    assert rd == tmp_path / "runs_anon" / "abc123"


def test_find_run_dir_anon(tmp_path):
    bootstrap()
    runs_anon_root().mkdir(parents=True, exist_ok=True)
    (runs_anon_root() / "anon_run_1").mkdir()
    assert find_run_dir("anon_run_1") == runs_anon_root() / "anon_run_1"


def test_find_run_dir_in_project(tmp_path):
    bootstrap()
    proj_runs = projects_root() / "alpha" / "runs"
    proj_runs.mkdir(parents=True, exist_ok=True)
    (proj_runs / "run_42").mkdir()
    assert find_run_dir("run_42") == proj_runs / "run_42"


def test_find_run_dir_in_legacy_flat(tmp_path):
    """旧 runs/<run_id>/ 也能找到（backward compat 读）。"""
    bootstrap()
    legacy = runs_root()
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "old_run").mkdir()
    assert find_run_dir("old_run") == legacy / "old_run"


def test_find_run_dir_in_state_dir_env(tmp_path, monkeypatch):
    """STATE_DIR env var 旧路径也能找到。"""
    bootstrap()
    sd = tmp_path / "custom_state_dir"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "state_dir_run").mkdir()
    monkeypatch.setenv("STATE_DIR", str(sd))
    assert find_run_dir("state_dir_run") == sd / "state_dir_run"


def test_find_run_dir_priority_anon_first(tmp_path):
    """同名 run 同时在 anon 和 project 里 → anon 优先（约定）。"""
    bootstrap()
    runs_anon_root().mkdir(parents=True, exist_ok=True)
    (runs_anon_root() / "shared_id").mkdir()
    (projects_root() / "foo" / "runs").mkdir(parents=True, exist_ok=True)
    (projects_root() / "foo" / "runs" / "shared_id").mkdir()
    # anon 先
    assert find_run_dir("shared_id") == runs_anon_root() / "shared_id"


def test_find_run_dir_missing_returns_none(tmp_path):
    bootstrap()
    assert find_run_dir("never_existed_run") is None


def test_child_run_inherits_parent_runs_dir(tmp_path):
    """关键设计 invariant：父 state.root.parent 是 child 的 base_dir。
    所以父 orchestrator 在 projects/<pid>/runs/<orch_id>/ 时，子节点自动在
    projects/<pid>/runs/<child_id>/。这是项目嵌套的级联机制。"""
    bootstrap()
    from core.state import State

    pid = "test-cascade"
    runs_dir = runs_parent(pid)
    runs_dir.mkdir(parents=True, exist_ok=True)

    # 父 orch state 落在 projects/<pid>/runs/orch_run_id/
    parent_state = State.new(
        node_type="_orchestrator",
        base_dir=runs_dir,
        project_id=pid,
    )
    assert parent_state.root.parent == runs_dir

    # run_node tool 会用 state.root.parent 作 base_dir for child
    child_base = parent_state.root.parent
    assert child_base == runs_dir

    # 子 state 也落 projects/<pid>/runs/<child>/
    child_state = State.new(
        node_type="literature",
        base_dir=child_base,
        project_id=pid,
    )
    assert child_state.root.parent == runs_dir
    # 兄弟关系：父子在同个 runs/ 下
    assert parent_state.root.parent == child_state.root.parent


def test_anon_run_lands_in_runs_anon(tmp_path):
    """无 project_id 的 run 落 runs_anon/，不污染任何 project。"""
    bootstrap()
    from core.state import State
    anon_base = runs_parent(None)
    anon_base.mkdir(parents=True, exist_ok=True)
    s = State.new(node_type="literature", base_dir=anon_base, project_id=None)
    assert "runs_anon" in str(s.root)
    assert "projects" not in str(s.root)
