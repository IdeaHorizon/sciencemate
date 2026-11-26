"""Upgrade installed SQLite and PostgreSQL schemas without rewriting old saves."""

from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import database
from app.config import settings
from app.models.feed import FeedItem
from app.models.user import User

OWNER = "aaaaaaaa111141118111111111111111"
ITEM = "bbbbbbbb111141118111111111111111"
SAVE = "cccccccc111141118111111111111111"


def migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/059_feed_save_folders.py"
    spec = spec_from_file_location("save_folder_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed_schema(connection):
    database.Base.metadata.create_all(connection)
    connection.execute(
        User.__table__.insert(),
        {"id": OWNER, "email": "reader@lab.test", "display_name": "Reader", "hashed_password": "x"},
    )
    connection.execute(
        FeedItem.__table__.insert(),
        {
            "id": ITEM,
            "canonical_key": "doi:10.1234/old",
            "kind": "paper",
            "title": "Existing paper",
            "url": "https://doi.org/10.1234/old",
        },
    )
    connection.exec_driver_sql("DROP TABLE feed_engagements")
    connection.exec_driver_sql("DROP TABLE feed_save_folders")
    connection.exec_driver_sql(
        "CREATE TABLE feed_engagements (id CHAR(32) PRIMARY KEY, "
        "user_id CHAR(32) NOT NULL, item_id CHAR(32) NOT NULL, "
        "action VARCHAR(20) NOT NULL, created_at DATETIME, "
        "CONSTRAINT uq_feed_engagement_once UNIQUE (user_id,item_id,action))"
    )
    connection.execute(
        text("INSERT INTO feed_engagements VALUES (:id,:user,:item,'save','2026-11-01 12:34:56')"),
        {"id": SAVE, "user": OWNER, "item": ITEM},
    )


def unchanged_save(connection):
    row = connection.execute(
        text("SELECT id,user_id,item_id,action,created_at,folder_id FROM feed_engagements")
    ).one()
    assert tuple(row) == (SAVE, OWNER, ITEM, "save", "2026-11-01 12:34:56", None)
    assert {"feed_save_folders", "feed_engagements"} <= set(inspect(connection).get_table_names())
    assert "ix_feed_engagements_user_folder" in {
        i["name"] for i in inspect(connection).get_indexes("feed_engagements")
    }


@pytest.mark.asyncio
async def test_desktop_upgrade_adds_locations_and_keeps_old_saves_default(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'installed.sqlite3'}")
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(settings, "database_url", str(engine.url))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(installed_schema)
        await database.create_schema_for_unmanaged_databases()
        await database.create_schema_for_unmanaged_databases()
        async with engine.connect() as connection:
            await connection.run_sync(unchanged_save)
    finally:
        await engine.dispose()


def test_alembic_upgrade_and_downgrade_preserve_the_existing_save(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'managed.sqlite3'}")
    try:
        with engine.begin() as connection:
            installed_schema(connection)
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()
                unchanged_save(connection)
                migration().downgrade()
            assert connection.execute(text("SELECT id,created_at FROM feed_engagements")).one() == (
                SAVE,
                "2026-11-01 12:34:56",
            )
            assert "folder_id" not in {
                c["name"] for c in inspect(connection).get_columns("feed_engagements")
            }
    finally:
        engine.dispose()


def test_postgres_upgrade_emits_nullable_location_and_owner_foreign_keys():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration().upgrade()
    ddl = output.getvalue()
    assert "CREATE TABLE feed_save_folders" in ddl
    assert "REFERENCES users (id) ON DELETE CASCADE" in ddl
    assert "ADD COLUMN folder_id UUID" in ddl and "folder_id UUID NOT NULL" not in ddl
    assert "REFERENCES feed_save_folders (id) ON DELETE SET NULL" in ddl
    assert "UPDATE " not in ddl and "DELETE FROM" not in ddl
