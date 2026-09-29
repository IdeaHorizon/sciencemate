"""个人档升级：模型新加的列要出现在装过旧版的 SQLite 库里。

真机（2026-09-18）：0.5.0 给 users 加了 `must_change_password`；一台 09-07 装的
机器升上去后，登录态、更新检查、模型设置、资讯流全 500，日志
`no such column: users.must_change_password`。`create_all` 只建缺的表，从不给
已有的表加列；启动闸看到没有 `alembic_version` 表就放行。个人档因此没有任何
列升级路径 —— 每一次给模型加列都会把所有老用户砖掉。

夹具用**真表**：按当前模型建库，再把那一列 DROP 掉、写进一行 —— 这就是旧版
建出来的 users 表长的样子，而不是一张自造的小表。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import database
from app.database import Base, create_schema_for_unmanaged_databases

THE_COLUMN_THAT_BRICKED_0_5_0 = "must_change_password"


async def _a_database_installed_before_that_column(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    import app.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(f"ALTER TABLE users DROP COLUMN {THE_COLUMN_THAT_BRICKED_0_5_0}"))
        await conn.execute(text(
            "INSERT INTO users (id, email, hashed_password, display_name, role, "
            "institution_id, institution_name, is_active) "
            "VALUES ('u1', 'old@example.org', 'x', 'Old User', 'researcher', 'local', 'Local', 1)"
        ))
    return engine


@pytest.mark.asyncio
async def test_an_old_database_gains_the_column_and_the_old_row_reads_the_default(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    engine = await _a_database_installed_before_that_column(tmp_path / "old.db")
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr("app.config.settings.database_url", "sqlite+aiosqlite:///irrelevant")
    try:
        async with engine.connect() as conn:  # 前提：这就是升级前会 500 的那句
            with pytest.raises(Exception, match="no such column"):
                await conn.execute(text(f"SELECT {THE_COLUMN_THAT_BRICKED_0_5_0} FROM users"))

        with caplog.at_level(logging.INFO, logger=database.logger.name):
            await create_schema_for_unmanaged_databases()

        async with engine.connect() as conn:
            values = (await conn.execute(
                text(f"SELECT {THE_COLUMN_THAT_BRICKED_0_5_0} FROM users")
            )).scalars().all()
        assert values == [0], "存量行要拿到模型里的默认值，不是 NULL"
        assert any(
            f"users.{THE_COLUMN_THAT_BRICKED_0_5_0}" in r.message and "added" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

        caplog.clear()
        await create_schema_for_unmanaged_databases()  # 第二次启动：幂等
        assert not [r for r in caplog.records if "added missing column" in r.message]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_not_null_column_without_any_default_is_refused_not_added_nullable(
    tmp_path: Path,
) -> None:
    """补不了就明说。加成 nullable 会让模型与库悄悄分叉 —— 那正是这条路要治的病。"""
    old = MetaData()
    Table("things", old, Column("id", Integer, primary_key=True))
    new = MetaData()
    Table("things", new, Column("id", Integer, primary_key=True),
          Column("flag", Boolean, nullable=False),
          Column("label", String, nullable=False, default="n/a"),
          Column("note", String, nullable=True))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'things.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(old.create_all)
            await conn.execute(text("INSERT INTO things (id) VALUES (1)"))
            with pytest.raises(RuntimeError, match="things.flag"):
                await conn.run_sync(
                    database._add_the_columns_the_models_have_and_the_database_lacks,
                    metadata=new,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_columns_with_a_python_default_or_nullable_are_added(tmp_path: Path) -> None:
    old = MetaData()
    Table("things", old, Column("id", Integer, primary_key=True))
    new = MetaData()
    Table("things", new, Column("id", Integer, primary_key=True),
          Column("label", String, nullable=False, default="n/a"),
          Column("note", String, nullable=True))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'things.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(old.create_all)
            await conn.execute(text("INSERT INTO things (id) VALUES (1)"))
            added = await conn.run_sync(
                database._add_the_columns_the_models_have_and_the_database_lacks, metadata=new,
            )
            assert added == [("things", "label"), ("things", "note")]
            row = (await conn.execute(text("SELECT label, note FROM things"))).one()
        assert tuple(row) == ("n/a", None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_served_database_is_never_touched(monkeypatch) -> None:
    """组织档（Postgres）归 alembic 管；这条路对它必须是空操作，连引擎都不碰。"""
    monkeypatch.setattr("app.config.settings.database_url", "postgresql+asyncpg://u:p@h/db")
    def _explode():
        raise AssertionError("touched the engine of a served database")
    monkeypatch.setattr(database, "get_engine", _explode)
    await create_schema_for_unmanaged_databases()
