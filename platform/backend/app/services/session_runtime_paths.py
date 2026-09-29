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

## 为什么读两处

存量会话的数据在老路径下，而**续跑是全函数**（缓存未命中不是错误）：
新会话写新路径，老会话照旧能被找到。没有数据迁移，没有停机，也没有
"旧会话 worktree 未初始化"那一幕（[[project_data_root_follows_release]]）。

这不是"两个真相源"——写只有一处，读的第二处是只读的历史兼容，且会随着
老会话归档自然消失。
"""

from __future__ import annotations

from pathlib import Path

#: 现行位置。新建的一切都落在这里。
RUNTIME_DIRNAME = "runtime"

#: 2026-08-27 之前的位置。只读，只为让存量会话继续工作。
LEGACY_PARTS = (".research", "cache", "runtime")


def session_runtime_root(worktree: Path | str) -> Path:
    """这个会话的运行时记录根 —— 新会话写这里。

    老会话（数据还在 `.research/cache/runtime/` 下）由 `resolve_existing`
    找回；这个函数永远返回现行位置，好让新数据只往一个地方落。
    """
    return Path(worktree) / ".research" / RUNTIME_DIRNAME


def session_runs_root(worktree: Path | str) -> Path:
    """run 目录的父目录（`<runtime>/runs/`）。"""
    return session_runtime_root(worktree) / "runs"


def legacy_session_runtime_root(worktree: Path | str) -> Path:
    return Path(worktree).joinpath(*LEGACY_PARTS)


def resolve_existing(worktree: Path | str, *suffix: str) -> Path:
    """读路径：现行位置优先，不存在再看老位置。

    `suffix` 是根之下的相对片段（例如 `"runs"`、`"runs", "<run_dir>"`）。
    两处都不存在时返回**现行位置** —— 调用方拿它去创建，新数据因此只落新处。
    """
    current = session_runtime_root(worktree).joinpath(*suffix)
    if current.exists():
        return current
    legacy = legacy_session_runtime_root(worktree).joinpath(*suffix)
    if legacy.exists():
        return legacy
    return current
