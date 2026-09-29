"""计算产物属于**本节点在 Project Git 里的目录** —— 不是第二棵树。

E2E v19/v20 的同一个根因：`experiment_output_dir` 锚在 `state.project_root`
（v1 的 harness-state 持久化目录），与 Project worktree 是两棵树。后果：

  · LAMMPS 日志/轨迹落在 worktree 外 → P6-a 的通用文件工具（作用域 worktree）
    够不着 → postprocess 拿不到数据，逼 experiment 手工导平表，两节点互相返工
    卡死一夜（v19）；
  · 产物进不了 checkpoint，也就发不到 project main，下一 session 看不见；
  · v20 里 agent 自己选了 `<worktree>/experiment/runtime/...`（正确的地方），
    反被 submit_job 的路径角色门拒绝 —— 平台自带两套冲突的"输出该放哪"。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.project_workspace import bind_project_workspace
from core.state import State

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
from tools.path_roles import experiment_output_dir, project_workspace_dir  # noqa: E402

NODES = ("hypothesis", "experiment", "postprocess", "writing")


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    for node in NODES:
        (root / node / "artifacts").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _bound(tmp_path: Path, root: Path) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p")
    bind_project_workspace(state, root)
    return state


def test_outputs_land_inside_the_nodes_own_worktree_directory(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    out = experiment_output_dir(_bound(tmp_path, root), "runtime", create=True)

    assert out.is_relative_to(root), f"产物跑到 worktree 外了：{out}"
    assert out.is_relative_to(root / "experiments"), "产物必须在 experiment 自己的目录里"


def test_downstream_nodes_can_reach_the_data_with_generic_tools(tmp_path: Path) -> None:
    """这是整条链的意义所在：postprocess 用 worktree 作用域的通用读就能拿到。"""
    root = _worktree(tmp_path)
    out = experiment_output_dir(_bound(tmp_path, root), "runtime", create=True)
    (out / "log.thermo").write_text("Step Temp Volume\n1 2.0 850\n", encoding="utf-8")

    downstream = State.new(node_type="postprocess", base_dir=tmp_path / "pp", project_id="p")
    bind_project_workspace(downstream, root)
    from core.project_workspace import resolve_tool_path

    seen = resolve_tool_path(downstream, str(out / "log.thermo"))
    assert seen.is_file() and "Volume" in seen.read_text(encoding="utf-8")


def test_write_boundary_and_output_location_share_one_anchor(tmp_path: Path) -> None:
    """允许写的地方和实际写的地方必须是同一棵树 —— 否则 submit_job 会拒绝
    一个其实完全正确的 workdir（v20 实测）。"""
    root = _worktree(tmp_path)
    state = _bound(tmp_path, root)

    boundary = project_workspace_dir(state)
    out = experiment_output_dir(state, "runtime", create=True)

    assert boundary is not None
    assert out.is_relative_to(boundary), (out, boundary)


def test_a_run_without_a_worktree_keeps_the_legacy_location(tmp_path: Path) -> None:
    """非 Project 运行（CLI 直跑）行为不变 —— 不为了统一而砍掉旧路径。"""
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p")
    out = experiment_output_dir(state, "runtime", create=True)
    assert out.exists()
