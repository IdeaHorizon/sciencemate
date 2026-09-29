"""收起会话：有账要算的归档，什么都没发生过的直接删。

列表里最碍事的从来不是跑过的研究，是误建、开了没用、或者一开局就失败的空壳
（2026-08-13 node20：一个默认后端配错，连着造出三个零产出的会话）。归档它们
只是把垃圾换个地方堆着，所以这两件事合成一个动作。

判据只有 `session_is_empty` 一处 —— 归档和删除端点共用；前端不自己数消息条数。
"""

import pytest

from app.services.sessions import session_is_empty


class _DB:
    """按"哪张表非空"回答 count —— session_is_empty 只问这三个问题。"""

    def __init__(self, nonempty: set[str] | None = None):
        self.nonempty = nonempty or set()
        self.asked: list[str] = []

    async def scalar(self, statement):
        text = str(statement)
        for name in ("session_messages", "runs"):
            if name in text:
                self.asked.append(name)
                return 1 if name in self.nonempty else 0
        raise AssertionError(f"unexpected statement: {text[:120]}")


@pytest.mark.asyncio
async def test_a_session_where_nothing_happened_is_empty():
    db = _DB()
    assert await session_is_empty(db, "s1") is True
    # 三处都问过 —— 少问一处就会把"有产出"的会话当空的删掉
    assert db.asked == ["session_messages", "runs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["session_messages", "runs"])
async def test_any_recorded_trace_makes_it_not_empty(table):
    assert await session_is_empty(_DB({table}), "s1") is False


@pytest.mark.asyncio
async def test_it_stops_at_the_first_trace_it_finds():
    """短路只是省一次查询；结论必须和全查一遍一样。"""
    db = _DB({"session_messages"})
    assert await session_is_empty(db, "s1") is False
    assert db.asked == ["session_messages"]


def test_both_endpoints_read_the_same_criterion():
    """归档与删除必须调同一个判据函数，不许各自数一遍表。"""
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "app/api/v1/sessions.py"
    ).read_text(encoding="utf-8")
    archive = text.split("async def archive_session")[1].split("@router")[0]
    delete = text.split("async def delete_empty_session")[1].split("@router")[0]
    for endpoint in (archive, delete):
        assert "session_is_empty(db, session_id)" in endpoint
        assert "func.count()" not in endpoint
