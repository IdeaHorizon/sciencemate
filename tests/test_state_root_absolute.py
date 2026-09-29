"""issue #426：state.root 必须生而绝对。

nidy 现场（run `1786530936-d40096`，summary 里 `state_dir: 'output/...'`）：
CLI 用相对 `output/` 起 run，相对 root 一路毒化到 experiment 的路径角色——
run-local 默认 run_root 被 `_normalize_path` 静默丢弃，agent 给的完全正确的
绝对 workdir 匹配不到任何角色，submit_job 全拒，最终节点被熔断锁死。
第一环（相对 root）和最后一环（拒绝派发）隔五层，没有一层报出真名。
"""
from __future__ import annotations

from pathlib import Path

from core.state import State


def test_state_new_with_relative_base_dir_yields_absolute_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = State.new("experiment", Path("output"))
    assert state.root.is_absolute()
    # 语义不变：还是 cwd 底下那个目录
    assert state.root == (tmp_path / "output" / state.run_id).resolve()


def test_direct_construction_also_absolutized(tmp_path, monkeypatch):
    """chat.py 等处绕过 State.new 直接构造 —— 咽喉在 dataclass 自己身上。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output" / "r1").mkdir(parents=True)
    state = State(run_id="r1", node_type="_orchestrator",
                  root=Path("output") / "r1")
    assert state.root.is_absolute()


def test_relative_root_no_longer_starves_experiment_path_roles(tmp_path, monkeypatch):
    """nidy 现场回放：相对 root 起的 run，默认 run_root 角色必须仍然存在，
    且能匹配 agent 给的绝对 workdir。

    修复前：`_normalize_path(相对路径)` → None → 默认角色静默丢弃 →
    matching_path_roles(绝对 workdir) == [] → submit_job:
    "workdir must be inside a declared writable role"。
    """
    try:
        from nodes.experiment.tools.path_roles import (
            collect_path_roles, matching_path_roles,
        )
    except ImportError:  # 节点代码不在（精简部署）——核心断言已由上面覆盖
        import pytest
        pytest.skip("experiment node not present")

    monkeypatch.chdir(tmp_path)
    state = State.new("experiment", Path("output"))

    roles = collect_path_roles(state)
    run_roots = [r for r in roles if r.role == "run_root"]
    assert run_roots, "run-local 默认 run_root 不得因相对 root 被静默丢弃"
    assert all(Path(r.path).is_absolute() for r in run_roots)

    # agent 实际提交的就是这个绝对路径（nidy 日志原样）
    workdir = (state.root / "outputs" / "experiment" / "runtime").resolve()
    matches = matching_path_roles(str(workdir), state)
    assert any(r.role == "run_root" and r.writable for r in matches), (
        "绝对 workdir 必须命中默认 run_root —— 修复前这里为空，"
        "submit_job 因此拒绝所有提交"
    )
