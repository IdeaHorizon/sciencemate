"""运行时记录住在 `.research/runtime/`，而且名字不许再叫 cache。

这条路径下存的是一次研究**到底发生了什么**的全部原始记录（transcript /
events.jsonl / conversation.json / checkpoint / 工具原文）。平台库里的
`execution_events` 只是它的一份有损投影 —— 白名单字段、截断、未知事件静默
丢弃。它曾经叫 `.research/cache/`，而 `git clean -xdf` 会照着那个名字把它
清掉：清掉的不是缓存，是没法重算的科研过程记录。
"""

from __future__ import annotations

from pathlib import Path

from app.services.session_runtime_paths import (
    runtime_path,
    session_runs_root,
    session_runtime_root,
)


def test_new_sessions_write_where_the_name_tells_the_truth(tmp_path: Path) -> None:
    root = session_runtime_root(tmp_path)
    assert root == tmp_path / ".research" / "runtime"
    assert "cache" not in root.parts, (
        "运行记录又住进了一个叫 cache 的目录 —— git clean -xdf 会把它当垃圾清掉"
    )
    assert session_runs_root(tmp_path) == root / "runs"


def test_runtime_path_is_the_one_location(tmp_path: Path) -> None:
    """读和写是同一个地方 —— 不存在时也给这个位置，调用方拿它去创建。"""
    assert runtime_path(tmp_path, "runs") == session_runtime_root(tmp_path) / "runs"
    assert runtime_path(tmp_path, "runs", "r") == session_runtime_root(tmp_path) / "runs" / "r"


def test_the_runtime_record_stays_out_of_version_control() -> None:
    """判据读的是**真正写进仓库的那段 .gitignore 文本**，不是某个常量的名字。"""
    import inspect

    from app.services import project_repository

    source = inspect.getsource(project_repository)
    assert ".research/runtime/" in source
