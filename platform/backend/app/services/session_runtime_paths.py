"""会话的运行时记录放在哪 —— 只有这一个答案。

## 它不是缓存

这里存的是 transcript、events.jsonl、conversation.json、messages_checkpoint、
tool_results、event_blobs —— **一次研究到底发生了什么，全部的原始记录**。
平台库里的 `execution_events` 只是它的一份有损投影（白名单字段、截断、
未知事件静默丢弃），产物的 provenance、决策的逐字上下文、被折叠掉的工具
原文，都只有这里有。

它曾经住在 `<worktree>/.research/cache/` 里。那个名字是个真隐患：
`git clean -xdf` 会连它一起清掉，而清掉的不是缓存，是科研过程记录 ——
没有任何一步能把它重算回来。2026-08-27 拆除时把它迁到 `.research/runtime/`，
名字如实说它是什么。
"""

from __future__ import annotations

from pathlib import Path

#: 运行时记录的目录名。一切都落在这里。
RUNTIME_DIRNAME = "runtime"


def session_runtime_root(worktree: Path | str) -> Path:
    """这个会话的运行时记录根。"""
    return Path(worktree) / ".research" / RUNTIME_DIRNAME


def session_runs_root(worktree: Path | str) -> Path:
    """run 目录的父目录（`<runtime>/runs/`）。"""
    return session_runtime_root(worktree) / "runs"


def runtime_path(worktree: Path | str, *suffix: str) -> Path:
    """根之下的一条路径。`suffix` 是相对片段（例如 `"runs"`、`"runs", "<run_dir>"`）。

    纯路径运算，**不碰文件系统**：调用方拿它去读或去创建。这一点有过代价 ——
    这里曾经先 `exists()` 再回答，而 stat 被拒（Windows 上整个 run 目录
    `[WinError 5]`，lujy 2026-10-15 现场）会把接回活 worker、回收孤儿、转录摄取
    三条路一路炸到顶。worker 自己会在同一个目录上探测、降级、报进 ready
    （RFC_WORKER_START_IS_NOT_A_GATE §G2），这里不替它看。
    """
    return session_runtime_root(worktree).joinpath(*suffix)
