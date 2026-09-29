"""一条只读的事件流不许攥着请求级数据库会话。

## 现场（2026-09-09 node20）

`pg_stat_activity` 里 7 条 `idle in transaction`，最后一句全是
`SELECT runs.id … WHERE runs.id = $2 AND runs.session_id = $3` —— 每条对应一个
浏览器开着的 `GET /sessions/{id}/events/stream`。`Depends(get_db)` 在响应**结束**
后才收尾，而事件流可以活几小时：鉴权那两句查询开了事务，连接就这么被攥到底。

`sse_response` 是全仓唯一能造 StreamingResponse 的地方，所以表态也只在这一处：
`release=` 必填。传会话进来的，第一个字节发出前归还；真要在流里写库的传 `None`
并说明为什么。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.services.sse import sse_response

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


class _Session:
    def __init__(self) -> None:
        self.closed_before_first_chunk: bool | None = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _chunks(session: _Session):
    session.closed_before_first_chunk = session.closed
    yield "data: 1\n\n"
    yield "data: 2\n\n"


@pytest.mark.asyncio
async def test_the_session_is_closed_before_the_first_chunk_is_produced() -> None:
    session = _Session()
    response = sse_response(_chunks(session), release=session)  # type: ignore[arg-type]
    body = [chunk async for chunk in response.body_iterator]
    assert session.closed_before_first_chunk is True
    assert b"".join(c if isinstance(c, bytes) else c.encode() for c in body).count(b"data:") == 2


@pytest.mark.asyncio
async def test_release_none_keeps_the_session_for_streams_that_write() -> None:
    session = _Session()
    response = sse_response(_chunks(session), release=None)
    [chunk async for chunk in response.body_iterator]
    assert session.closed is False


def test_release_is_a_required_statement() -> None:
    parameter = inspect.signature(sse_response).parameters["release"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, "每条流都得表态，默认值就是不表态"


def test_every_stream_states_what_happens_to_its_session() -> None:
    """扫盘：所有 `sse_response(` 调用都带 `release=`（签名本身也逼着，这条是给读者看的清单）。"""
    sites: list[str] = []
    for source in sorted((_BACKEND / "app").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "sse_response"
            ):
                keywords = {kw.arg for kw in node.keywords}
                sites.append(f"{source.relative_to(_BACKEND)}:{node.lineno}")
                assert "release" in keywords, f"{source}:{node.lineno} 没表态"
    assert len(sites) >= 4, sites
