"""Existing upstream and local databases converge without losing CNKI records."""
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("with_paper", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_personal_sqlite_upgrades_cnki_results_without_alembic(
    tmp_path, monkeypatch, with_paper, interrupted,
):
    """0.5.12 的空库和有题录的库都走桌面启动入口，不依赖 Alembic revision。"""
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import database
    import app.models  # noqa: F401

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'personal.sqlite'}")
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///personal")
    metadata = json.dumps({"title": "已采集文献", "abstract": "保留旧摘要"})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            await connection.execute(text("DROP INDEX ix_cnki_papers_updates"))
            await connection.execute(text("ALTER TABLE cnki_papers DROP COLUMN updated_at"))
            await connection.execute(text("ALTER TABLE cnki_papers ADD COLUMN legacy_note TEXT"))
            if with_paper:
                await connection.execute(text(
                    "INSERT INTO cnki_papers (id,user_id,identity,metadata_json,observed_at,legacy_note) "
                    "VALUES (:id,:user,'legacy',:metadata,'2026-10-01 10:00:00','keep')"
                ), {"id": "a" * 32, "user": "b" * 32, "metadata": metadata})
            if interrupted:
                await connection.execute(text(
                    "ALTER TABLE cnki_papers ADD COLUMN updated_at DATETIME"
                ))

        await database.create_schema_for_unmanaged_databases()
        async with engine.begin() as connection:
            columns = await connection.run_sync(lambda c: inspect(c).get_columns("cnki_papers"))
            updated = next(column for column in columns if column["name"] == "updated_at")
            assert updated["nullable"] is False
            assert "CURRENT_TIMESTAMP" in updated["default"].upper()
            indexes = await connection.run_sync(lambda c: inspect(c).get_indexes("cnki_papers"))
            assert next(index for index in indexes if index["name"] == "ix_cnki_papers_updates")[
                "column_names"] == ["user_id", "updated_at", "id"]
            if with_paper:
                old = (await connection.execute(text(
                    "SELECT metadata_json,observed_at,updated_at,legacy_note FROM cnki_papers"
                ))).one()
                assert tuple(old) == (metadata, "2026-10-01 10:00:00", "2026-10-01 10:00:00", "keep")
                await connection.execute(text(
                    "UPDATE cnki_papers SET updated_at='2026-11-01 12:00:00'"
                ))
            await connection.execute(text(
                "INSERT INTO cnki_papers (id,user_id,identity,metadata_json,observed_at) "
                "VALUES (:id,:user,'new','{}','2026-10-02 10:00:00')"
            ), {"id": "c" * 32, "user": "b" * 32})
            before = (await connection.execute(text("SELECT * FROM cnki_papers ORDER BY id"))).all()
        await database.create_schema_for_unmanaged_databases()
        async with engine.connect() as connection:
            after = (await connection.execute(text("SELECT * FROM cnki_papers ORDER BY id"))).all()
            assert after == before, "重复启动不能改写已交付的更新时间和旧题录"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("installed", [
    "053_merge_cnki_execution_heads", "054_cnki_incremental_results",
])
def test_existing_cnki_records_survive_head_merge(tmp_path, monkeypatch, installed):
    database = tmp_path / "existing.sqlite3"
    metadata = json.dumps({"title": "已采集文献", "abstract": "已缓存摘要"})
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cnki_papers (id TEXT PRIMARY KEY, user_id TEXT, "
                           "metadata_json TEXT, observed_at DATETIME)")
        connection.execute("INSERT INTO cnki_papers VALUES (?, ?, ?, ?)",
                           ("paper", "owner", metadata, "2026-11-09 10:00:00"))
        if installed == "054_cnki_incremental_results":
            connection.execute("ALTER TABLE cnki_papers ADD COLUMN updated_at DATETIME")
            connection.execute("UPDATE cnki_papers SET updated_at = observed_at")
            connection.execute("CREATE INDEX ix_cnki_papers_updates ON cnki_papers (user_id, updated_at, id)")
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{database}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.stamp(config, installed)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("056_analysis_node_name",)]
        assert connection.execute("SELECT metadata_json, updated_at FROM cnki_papers").fetchone() == (
            metadata, "2026-11-09 10:00:00")
