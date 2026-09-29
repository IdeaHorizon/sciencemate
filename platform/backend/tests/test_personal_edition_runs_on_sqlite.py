"""个人档：一个 SQLite 文件就能起服务，不需要 Postgres、不需要 alembic。

三件事以前各自把这条路堵死过，这里各钉一条：

1. `create_async_engine` 无条件传 `pool_size / max_overflow / pool_timeout`。
   实测（2026-09-05）：文件型 SQLite 用 `AsyncAdaptedQueuePool`，收下不报错；
   内存型用 `StaticPool`，直接 `TypeError: Invalid argument(s)`，进程起不来。
   所以这条缺陷在文件库上是「配错了」（单写者配 20 条连接，把争用翻译成
   database is locked），在内存库上是「起不来」。
2. 启动闸用 `SELECT to_regclass('alembic_version')` 问"这个库是不是 alembic
   建的"。SQLite 上这不是返回"没有"，而是 `no such function: to_regclass`
   —— 被 `except Exception` 吞成「读不到 alembic head」，判据形同虚设。
3. `/health/ready` 用 `pg_catalog.pg_tables` 查表，同上；再加上它要求
   `alembic_version == "ok"`，一个没有 alembic 的库永远 degraded。

第 3 条尤其要命：个人档的就绪判据永远为假，等于这条路上没有就绪判据。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import main as app_main
from app.database import Base, _pool_options, has_table, table_names


def test_sqlite_gets_no_pool_arguments() -> None:
    assert _pool_options("sqlite+aiosqlite:///./x.db") == {}
    served = _pool_options("postgresql+asyncpg://u:p@h/db")
    assert served["pool_size"] == 20 and served["pool_pre_ping"] is True


@pytest.mark.asyncio
async def test_an_engine_for_a_sqlite_url_actually_opens(tmp_path: Path) -> None:
    """光看参数不够 —— 真开一个。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"
    engine = create_async_engine(url, **_pool_options(url))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_in_memory_sqlite_engine_opens_too() -> None:
    """内存库是硬判据：它的 StaticPool 收到 pool_size 就是 TypeError。

    文件库能收下池参数（只是配错），内存库直接起不来 —— 所以这条测试才是
    「无条件传池参数」这个缺陷的杀手，上一条不是。
    """
    url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, **_pool_options(url))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_table_questions_are_answered_on_sqlite(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'y.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE runs (id TEXT)"))
        async with engine.connect() as conn:
            assert await has_table(conn, "runs") is True
            assert await has_table(conn, "alembic_version") is False
            assert "runs" in await table_names(conn)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_without_alembic_is_not_treated_as_behind(
    tmp_path, monkeypatch, caplog,
) -> None:
    """三态而不是两态 —— 走真函数、真库，不比对源码。

    从前这条测试断言的是 `inspect.getsource(...)` 里有没有 `to_regclass`
    这个字符串。那种断言只能证明"这一行文字还在"，改成别的实现就算行为正确
    也会红，而实现从 Postgres-only 变成方言中立恰恰是我们要做的事。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unmanaged.db'}")
    monkeypatch.setattr("app.database.get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with caplog.at_level(logging.INFO, logger=app_main.logger.name):
            # 没有 alembic_version 表 —— 不许抛。
            await app_main._refuse_to_serve_a_schema_we_were_not_written_for()
    finally:
        await engine.dispose()
    # "不抛"一条断言分不清两件事：真的判出了「这个库不归 alembic 管」，还是
    # Postgres-only 的查询在 SQLite 上炸了、被 except 吞成「读不到 alembic head」。
    # 两条路都不抛。所以判据落在它**走的哪条路**上。
    messages = [r.message for r in caplog.records]
    assert any("not managed by Alembic" in m for m in messages), (
        f"没走到「这个库不归 alembic 管」那条路；日志是 {messages}"
    )
    assert not any("Could not read the applied Alembic head" in m for m in messages), (
        "判据被异常吞掉了 —— 查询本身在这个方言上就跑不通"
    )


@pytest.mark.asyncio
async def test_a_schema_behind_this_build_still_refuses_to_start(tmp_path, monkeypatch) -> None:
    """有 alembic_version 但版本对不上 = 拒绝启动，且报错给出解法。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'behind.db'}")
    monkeypatch.setattr("app.database.get_engine", lambda: engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('deadbeef')"))
        with pytest.raises(RuntimeError) as excinfo:
            await app_main._refuse_to_serve_a_schema_we_were_not_written_for()
    finally:
        await engine.dispose()
    message = str(excinfo.value)
    assert "alembic upgrade head" in message, "报错必须给出正确答案"
    assert "deadbeef" in message, "报错要说清楚库里现在是什么"


@pytest.mark.asyncio
async def test_a_dead_database_is_not_reported_as_a_schema_mismatch(monkeypatch) -> None:
    """连不上库是另一回事，别把它伪装成 schema 不匹配。"""
    def _explode():
        raise OSError("connection refused")

    monkeypatch.setattr("app.database.get_engine", _explode)
    await app_main._refuse_to_serve_a_schema_we_were_not_written_for()
