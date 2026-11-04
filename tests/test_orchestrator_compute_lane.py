"""协调者的轻量计算通道（execute_python，2026-08-31，wangd 拍板）。

设计不是新闸，是三道**既有**墙的组合——本文件测的就是组合没有散架：
  1. 强制进程沙箱：#1187 起调度器的写边界为空（core/sandbox.model_tool_wall），
     算个数照常、结果只能打到 stdout；沙箱不可用时 fail-closed，不会裸跑；
  2. 代码文本边界：match_boundary_violation(mode="python") 筛框架状态；
  3. 落地走 #721 的门：结果交用户 = write_file(project/...)，
     正式证据仍走 run_node。

所以这里不 spawn 真沙箱（真起进程的判据在
tests/test_the_orchestrator_shell_is_read_only.py），只测**接线**：工具在场、
墙的推导正确、旧闸没有被顺手放松。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.loader import load_harness
from core.sandbox import model_tool_roots, model_tool_wall
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


def test_the_lane_has_no_write_root_at_all(tmp_path: Path) -> None:
    """墙在 spawn：调度器的计算通道一个可写根都没有（#1187）—— 连自己的作用域
    notes/ 和 run 目录都不在里面；唯一可写的是后端给每条命令的私有 scratch。"""
    project = _worktree(tmp_path)
    state = State.new("_orchestrator", tmp_path / "runtime",
                      project_id="p1", project_worktree=project)
    state.hook_state["_shell_probe_only"] = load_harness("_orchestrator").shell_probe_only
    wall = model_tool_wall(state)
    assert wall.writable_roots == [] and wall.ephemeral is True


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
