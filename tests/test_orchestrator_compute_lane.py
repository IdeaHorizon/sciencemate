"""协调者的轻量计算通道（execute_python，2026-08-31，wangd 拍板）。

设计不是新闸，是三道**既有**墙的组合——本文件测的就是组合没有散架：
  1. 强制进程沙箱：write_roots_for 把写根钉死在自己作用域 + run 目录，
     沙箱不可用时 fail-closed（launch rejected），不会裸跑；
  2. 代码文本边界：match_boundary_violation(mode="python") 筛框架状态；
  3. 落地走 #721 的门：结果交用户 = write_file(project/...)，
     正式证据仍走 run_node。

所以这里不 spawn 真沙箱（本机 Landlock 探针本来就起不来，那是沙箱套件的
活），只测**接线**：工具在场、写根推导正确、旧闸没有被顺手放松。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.loader import load_harness
from core.sandbox import model_tool_roots, write_roots_for
from core.state import State
from shared.lib import dangerous_commands as dc


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    for directory in ("literature", "experiment", "writing"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def test_harness_grants_the_lane_without_loosening_the_old_gates() -> None:
    """新通道与旧闸必须**同时**在场——加计算通道时顺手放松 probe_only，
    正是当年 heredoc 绕行事故的回头路。"""
    harness = load_harness("_orchestrator")
    assert "execute_python" in harness.tools
    assert harness.shell_probe_only is True          # run_bash 仍是探查专用
    assert harness.deliverable_writes is True        # 落地仍走 #721 的门


def test_sandbox_write_roots_exclude_everyone_elses_land(tmp_path: Path) -> None:
    """墙在 spawn：写根只有自己作用域 + run 目录。producing 节点的目录、
    Project 根都不在写根里——计算通道碰不到别人的地盘，靠的是这个推导。"""
    project = _worktree(tmp_path)
    state = State.new("_orchestrator", tmp_path / "runtime",
                      project_id="p1", project_worktree=project)
    from core.project_workspace import _NODE_WORKSPACES

    roots = {str(r) for r in write_roots_for(state)}
    own = (project / _NODE_WORKSPACES["_orchestrator"]).resolve()
    assert any(Path(r) == own or own.is_relative_to(Path(r)) for r in roots)
    for foreign in (project, project / "literature", project / "experiments"):
        assert not any(Path(r) == foreign.resolve() for r in roots), roots


def test_worktree_stays_readable(tmp_path: Path) -> None:
    """只读根含整个 worktree：算数要读得到数据，写不进去才是边界。"""
    project = _worktree(tmp_path)
    state = State.new("_orchestrator", tmp_path / "runtime",
                      project_id="p1", project_worktree=project)
    _, readonly = model_tool_roots(state)
    assert any(Path(r) == project.resolve() for r in readonly)


def test_python_text_boundary_still_screens_framework_state() -> None:
    assert dc.match_boundary_violation(
        "open('artifacts/x.json','w').write(s)", mode="python")
    assert dc.match_boundary_violation(
        "Path('project.yaml').write_text(s)", mode="python")
    assert dc.match_boundary_violation(
        "import numpy; print(numpy.mean([1,2,3]))", mode="python") is None
