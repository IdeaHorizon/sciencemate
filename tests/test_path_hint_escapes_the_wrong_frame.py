"""找不到路径时，提示必须从**工作区根**去找，不能困在出错的坐标系里。

## 现场（v22 实测，3972 次工具调用）

`list_files` 289 次里败 91 次、`read_file` 241 次里败 64 次，失败形状**全部**是
`<自己节点名>/<上游节点名>/…`，例如 writing 去找 `paper/postprocess/figures`。
真实位置是工作区根下的 `figures/figures`。

这 155 次里，**没有一次被纠正**。不是因为没有提示 —— `_missing_file_hint` 每次
都触发了，它输出的原话是：

    工作区里叫 artifacts 的文件在：artifacts

同义反复。原因：它读 `state.workspace_root` 找东西，而这个字段的名字骗人 ——
`bind_project_workspace` 把它设成**节点自己的目录**，真正的工作区根叫
`project_worktree`。于是"帮你跳出坐标系"的机制，自己就在那个坐标系里搜索、
并按那个坐标系报告结果。

**破框的机制不能困在框里。** 这条测试就钉这一点。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


class _FakeState:
    """只带路径解析必需的字段 —— 刻意把两个 root 设成不同的值。

    真实 State 里它们本来就不同；测试里若图省事设成同一个，这个缺陷
    **结构上测不到**（这正是它活了两个月的原因）。
    """

    def __init__(self, worktree: Path, node_dir: Path) -> None:
        self.project_worktree = worktree
        self.workspace_root = node_dir          # ← 名字骗人：这是节点自己的目录
        self.root = node_dir
        self.node_type = "writing"


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "wt"
    (worktree / "figures" / "figures").mkdir(parents=True)
    (worktree / "figures" / "figures" / "fig1.png").write_bytes(b"x")
    (worktree / "paper").mkdir(parents=True)
    return worktree, worktree / "paper"


def test_hint_finds_the_file_outside_the_callers_own_directory(workspace) -> None:
    """writing 去找 `paper/postprocess/figures` 时，提示要指出真实位置。"""
    from shared.tools.builtin import _missing_file_hint

    worktree, node_dir = workspace
    wrong = node_dir / "figures" / "figures"      # agent 抄地图抄出来的路径

    hint = _missing_file_hint(_FakeState(worktree, node_dir), wrong)

    real = worktree / "figures" / "figures"
    assert str(real) in hint, (
        f"提示必须给出真实位置 {real}；实际给的是：\n{hint}"
    )


def test_hint_paths_are_absolute(workspace) -> None:
    """给绝对路径。

    "相对于谁"正是模型此刻搞错的那件事 —— 再回一个相对路径，等于用出错的
    坐标系去解释错误。
    """
    from shared.tools.builtin import _missing_file_hint

    worktree, node_dir = workspace
    hint = _missing_file_hint(_FakeState(worktree, node_dir),
                              node_dir / "figures" / "figures")

    for line in hint.splitlines():
        if "叫" in line and "的在" in line:
            payload = line.split("：", 1)[1]
            for token in payload.split(", "):
                assert token.startswith("/"), f"提示里出现相对路径：{token}"
            break
    else:
        pytest.fail(f"没有找到『真实位置』那一行：\n{hint}")


def test_hint_does_not_echo_the_path_that_just_failed(workspace) -> None:
    """不要把刚刚失败的那个路径本身当成答案回给模型。"""
    from shared.tools.builtin import _missing_file_hint

    worktree, node_dir = workspace
    missing = node_dir / "figures" / "figures"
    hint = _missing_file_hint(_FakeState(worktree, node_dir), missing)

    for line in hint.splitlines():
        if "的在" in line:
            assert str(missing) not in line.split("：", 1)[1]


def test_orientation_text_gives_upstream_dirs_as_absolute_paths(tmp_path) -> None:
    """开局地图不许给工作区根相对的裸路径。

    地图说 `figures/`（工作区根相对），工具说"相对路径锚在你自己的目录"
    —— 同一个字符串两个含义，且两边都不报错。155 次失败的源头。
    """
    import inspect

    from core import context_engine

    source = inspect.getsource(context_engine)
    assert '"`literature/`' not in source and "`literature/`、" not in source, (
        "上游目录必须以绝对路径给出（project_worktree / name），不能给裸相对路径"
    )
