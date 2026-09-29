"""记忆锁是框架内务 —— 它不该出现在用户的文件树里。

2026-09-09 在 qinp 的真项目上看到：工作区根目录躺着一个 0 字节的
`MEMORY.md.lock`，未跟踪、不被忽略，于是它和论文、数据平起平坐地列在
「Project files」里。用户问"这是干嘛的"是完全合理的。

判据落在**落点**上：锁必须落进项目已经忽略掉的那个锁目录，而不是靠展示层
写一条"别显示这个名字"的规则（那种规则对下一个新锁默认失效）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core import memory


class _State:
    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree


def test_the_lock_lands_where_the_project_already_ignores_it(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "MEMORY.md").write_text("# Project memory\n", encoding="utf-8")

    with memory._locked(_State(worktree)) as lock:
        held = Path(lock._lock_path)

    assert held.parent == worktree / ".research" / "locks", (
        f"锁落在了 {held}，那不是项目的锁目录 —— 它会出现在用户的文件树里"
    )
    assert not (worktree / "MEMORY.md.lock").exists(), (
        "MEMORY.md 边上不该再多出一个 0 字节的文件"
    )


def test_git_never_sees_the_lock(tmp_path: Path) -> None:
    """判据走真 Git：`.gitignore` 与锁的落点必须真的对得上。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True)
    (worktree / ".gitignore").write_text(
        ".research/runtime/\n.research/cache/\n.research/locks/\n", encoding="utf-8"
    )
    (worktree / "MEMORY.md").write_text("# Project memory\n", encoding="utf-8")

    with memory._locked(_State(worktree)):
        pass

    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert not [p for p in untracked if p.endswith(".lock")], (
        f"锁对 Git 可见：{untracked}"
    )
