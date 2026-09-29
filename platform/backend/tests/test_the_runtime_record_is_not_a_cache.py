"""运行时记录住在 `.research/runtime/`，而且老会话不能因此失联。

这条路径下存的是一次研究**到底发生了什么**的全部原始记录（transcript /
events.jsonl / conversation.json / checkpoint / 工具原文）。平台库里的
`execution_events` 只是它的一份有损投影 —— 白名单字段、截断、未知事件静默
丢弃。它曾经叫 `.research/cache/`，而 `git clean -xdf` 会照着那个名字把它
清掉：清掉的不是缓存，是没法重算的科研过程记录。

改名的危险全在**存量数据**上：老会话的记录还在旧路径下。一次做错就是
"旧会话 worktree 未初始化"那一幕。所以这里两侧都钉住。
"""

from __future__ import annotations

from pathlib import Path

from app.services.session_runtime_paths import (
    legacy_session_runtime_root,
    resolve_existing,
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


def test_an_existing_session_under_the_old_path_is_still_found(tmp_path: Path) -> None:
    """存量会话不能因为改名就失联。续跑是全函数：找不到不是错误，是要去找。"""
    legacy = legacy_session_runtime_root(tmp_path) / "runs" / "orchestrator__1__x"
    legacy.mkdir(parents=True)
    (legacy / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    found = resolve_existing(tmp_path, "runs", "orchestrator__1__x")
    assert found == legacy
    assert (found / "transcript.jsonl").is_file()


def test_the_current_location_wins_when_both_exist(tmp_path: Path) -> None:
    """两处都在时读新的 —— 否则一个迁移过的会话会永远读回旧记录。"""
    for base in (session_runtime_root(tmp_path), legacy_session_runtime_root(tmp_path)):
        (base / "runs" / "r").mkdir(parents=True)
    assert resolve_existing(tmp_path, "runs", "r") == session_runtime_root(tmp_path) / "runs" / "r"


def test_a_brand_new_session_resolves_to_the_current_location(tmp_path: Path) -> None:
    """两处都没有时给现行位置 —— 调用方拿它去创建，新数据只落一处。"""
    assert resolve_existing(tmp_path, "runs") == session_runtime_root(tmp_path) / "runs"


def test_both_locations_stay_out_of_version_control() -> None:
    """新路径要被忽略，老路径也不能松手（存量会话还在写它）。

    判据读的是**真正写进仓库的那段 .gitignore 文本**，不是某个常量的名字。
    """
    import inspect

    from app.services import project_repository

    source = inspect.getsource(project_repository)
    assert ".research/runtime/" in source
    assert ".research/cache/" in source, "老路径还有存量会话在用，不能停止忽略"
