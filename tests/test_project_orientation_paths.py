"""开局地图给的路径必须**可以直接粘进工具**。

## 现场（v22 复盘）

`list_files` 89 次"找不到目录"，**全部**是同一个形状：

    writing/postprocess/figures        ← 想要 postprocess/figures
    writing/writing/latex_build        ← 想要 writing/latex_build
    experiment/writing/latex_build     ← 想要 writing/latex_build

agent 把自己的节点名当前缀重复贴了一遍。它不是在猜 ——

  地图说的：   `figures/`        （工作区根坐标）
  工具解析成： <节点目录>/postprocess/ （working_directory 锚点）

**两套坐标系。** agent 忠实使用了地图给的字符串，工具用另一套解释它。
89 次里 89 次都收到了"下现有：…"的邻居提示，仍然纠正不过来——因为提示指出
"不对"，没有指出"对的长什么样"。

所以地图必须给能直接用的字符串。
"""
from __future__ import annotations

from pathlib import Path


class _State:
    """只需要 project_worktree —— build_orientation_snapshot 是纯扫盘函数。"""

    def __init__(self, worktree: Path):
        self.project_worktree = worktree


def _render(root: Path) -> str:
    from core.loop_hooks_builtin import build_orientation_snapshot

    return build_orientation_snapshot(_State(root)) or ""


def _workspace(tmp_path: Path) -> Path:
    """三个节点目录各落一份记录 —— 地图问的是工作区账本，不是目录扫盘。"""
    from core.ledger import write_record
    from core.project_workspace import _NODE_WORKSPACES

    for node in ("literature", "postprocess", "writing"):
        write_record(tmp_path, artifact_type=f"{node}_out", name="x", content="x",
                     directory=_NODE_WORKSPACES[node],
                     produced_by_node_type=node, produced_by_run_id=f"r-{node}")
    return tmp_path


def test_map_lists_absolute_paths(tmp_path: Path) -> None:
    """相对路径在不同节点下含义不同 —— 地图是给所有节点看的，只能用绝对路径。"""
    root = _workspace(tmp_path)
    text = _render(root)
    assert str((root / "figures").resolve()) in text
    # 不许再出现裸的 `figures/`（那正是被误读成节点内路径的写法）
    assert "`figures/`" not in text


def test_map_states_the_coordinate_system(tmp_path: Path) -> None:
    """光给绝对路径还不够：得说清相对路径锚在哪，否则 agent 仍会自己拼。"""
    text = _render(_workspace(tmp_path))
    assert "相对路径" in text and "自己的节点目录" in text


def test_the_known_bad_shape_is_not_reproducible_from_the_map(tmp_path: Path) -> None:
    """把地图里出现的每个路径拿去当相对路径解析，都不该指向不存在的位置。

    这条直接复刻 v22 的失败形状：拿地图里的 token 当相对路径，从 writing 节点
    的视角解析，看会不会解析出 `paper/postprocess/...` 这种。
    """
    root = _workspace(tmp_path)
    text = _render(root)
    node_cwd = root / "paper"
    for token in text.replace("`", " ").split():
        if "/" not in token or token.startswith("**"):
            continue
        # shell 片段不是路径。判据是"这个 token 被当成路径引用"，不是"它长得
        # 像路径"：探测提示那一段里有 `2>/dev/null`、管道、通配符，按后者判
        # 会把它们当成断掉的相对路径 —— 我自己新加的那段当场被自己的测试误伤。
        if any(ch in token for ch in "|><*?$(){}") or token.endswith("/dev/null"):
            continue
        candidate = Path(token)
        if candidate.is_absolute():
            continue  # 绝对路径天然无歧义
        resolved = (node_cwd / candidate).resolve()
        assert resolved.exists(), (
            f"地图里的 {token!r} 当相对路径解析成 {resolved}，不存在 —— "
            "这正是 v22 那 89 次失败的形状"
        )
