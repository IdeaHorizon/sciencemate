"""跨节点的相对路径，读的时候两套坐标都试一遍 —— 别再让模型猜。

## 病根：模型手里同时有两套坐标系

文件工具的相对路径锚在**节点自己的目录**；而模型读到的每一份跨节点坐标
（开局地图、上游产物清单、别处报错里抄来的路径）都是**工作区根**坐标。它忠实
地把 `figures/figures` 递进来，被解析成 `paper/postprocess/figures`。

E2E v22 实测 `list_files` **89 次**"找不到目录"，89 次全是这个形状：

    writing/postprocess/figures      ← 想要 postprocess/figures
    writing/writing/latex_build      ← 想要 writing/latex_build
    experiment/writing/latex_build   ← 想要 writing/latex_build

修过两轮，都修在**说服模型**那一侧：地图改发绝对路径、报错附上"工作区里叫 X
的在这里"。89 次里 89 次都收到了提示，一次都没纠正过来。2026-09-09 的现场里，
模型甚至拿到了绝对路径提示，还是把它拧回相对路径又贴了一遍节点名。

**框架机械答得出的问题，不该反复交给模型去猜。** 所以改在解析这一侧。

绑真 Project worktree 才测得到这一层（不绑的 run 走另一条早退分支）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.project_workspace import _NODE_WORKSPACES, ProjectWorkspaceError, resolve_tool_path
from core.state import State


def _bound_state(tmp_path: Path, node_type: str = "writing") -> State:
    worktree = tmp_path / "project"
    for node in ("paper", "figures", "experiments"):
        (worktree / node).mkdir(parents=True)
    state = State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()))
    state.project_worktree = worktree
    state.workspace_relative_path = _NODE_WORKSPACES[node_type]
    return state


# ── 89 次全是这三个形状 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("asked, wanted", [
    ("figures", "figures"),
    ("paper/latex_build", "paper/latex_build"),
    ("experiments", "experiments"),
])
def test_root_coordinates_resolve_for_reads(tmp_path, asked, wanted):
    state = _bound_state(tmp_path)
    (state.project_worktree / wanted).mkdir(parents=True, exist_ok=True)
    assert resolve_tool_path(state, asked) == (state.project_worktree / wanted).resolve()


def test_own_directory_still_wins_when_both_exist(tmp_path):
    """歧义只在两处都存在时发生 —— 那时以自己的目录为准（严格保持既有行为）。"""
    state = _bound_state(tmp_path)
    own = state.project_worktree / "paper" / "figures"
    own.mkdir(parents=True)
    (state.project_worktree / "figures").mkdir(exist_ok=True)
    assert resolve_tool_path(state, "figures") == own.resolve()


def test_a_name_that_exists_nowhere_still_points_at_your_own_directory(tmp_path):
    """都不存在时报错要指向默认锚点 —— 那是模型该先去看的地方。"""
    state = _bound_state(tmp_path)
    got = resolve_tool_path(state, "nope/deeper")
    assert got == (state.project_worktree / "paper" / "nope" / "deeper").resolve()


# ── 放开的只是"读"，边界一寸没动 ────────────────────────────────────────────

def test_writes_never_get_the_second_anchor(tmp_path):
    """写多一个锚点就是多一个越界口子：根下的 `figures/` 真实存在，写侧也不许
    看见它 —— 同一个字符串，读解析到根、写照旧锚在自己目录里。"""
    state = _bound_state(tmp_path)
    (state.project_worktree / "figures" / "figures").mkdir(parents=True)
    written = resolve_tool_path(state, "figures/figures/x.png", write=True)
    assert written == (state.project_worktree / "paper" / "figures"
                       / "figures" / "x.png").resolve()
    # 同一个字符串读侧确实会走到根 —— 否则这条测试测的是"根本没有第二锚点"。
    assert resolve_tool_path(state, "figures/figures") == (
        state.project_worktree / "figures" / "figures").resolve()


def test_a_write_outside_your_scope_is_still_refused(tmp_path):
    """别的节点的目录照旧写不进去（绝对路径也不行）。"""
    state = _bound_state(tmp_path)
    target = state.project_worktree / "figures" / "x.png"
    with pytest.raises(ProjectWorkspaceError):
        resolve_tool_path(state, str(target), write=True)


def test_the_project_boundary_still_holds(tmp_path):
    """新候选是 `project_root / path`，`..` 照旧被下面那道边界检查拦住。"""
    state = _bound_state(tmp_path)
    with pytest.raises(ProjectWorkspaceError):
        resolve_tool_path(state, "../../etc/passwd")


def test_explicit_workspace_prefix_stays_pinned_to_your_own_directory(tmp_path):
    """显式前缀 = 显式答案：说了 `workspace/` 就不再另找，哪怕根下有同名目录。"""
    state = _bound_state(tmp_path)
    (state.project_worktree / "notes").mkdir()
    assert resolve_tool_path(state, "workspace/notes") == (
        state.project_worktree / "paper" / "notes").resolve()


def test_project_prefix_still_reaches_the_root_for_writes(tmp_path):
    """`project/` 是**写**侧唯一的跨作用域出口（交付文件），不能被这次改动带走。

    重构时差点带走：新解析器"都不存在时取第一个候选"会让它落回
    `<node>/project/LITERATURE_REVIEW.md` —— 而且不报错，交付文件从此写在
    一个没人读的地方。
    """
    state = _bound_state(tmp_path)
    state.hook_state["_deliverable_writes"] = True   # harness.deliverable_writes
    got = resolve_tool_path(state, "project/LITERATURE_REVIEW.md", write=True)
    assert got == (state.project_worktree / "LITERATURE_REVIEW.md").resolve()


def test_run_local_artifacts_keep_their_priority(tmp_path):
    """`artifacts/` 优先 run 本地目录 —— 既有优先级，重构不许悄悄改。"""
    state = _bound_state(tmp_path)
    run_local = Path(state.root) / "artifacts"
    run_local.mkdir(parents=True, exist_ok=True)
    (state.project_worktree / "paper" / "artifacts").mkdir(parents=True)
    assert resolve_tool_path(state, "artifacts") == run_local.resolve()
