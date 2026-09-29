"""一个恰好像数的 UUID，在个人档（SQLite）上要原样存、原样取回来。

id 列从前声明成 ``UUID``。SQLite 按类型**名字**定亲和性，``UUID`` 落到 NUMERIC：
绑定进去的 32 位十六进制串，只要全是数字、或者数字中间夹一个 ``e``，就被当成一个
数存成 INTEGER / REAL —— 原值丢掉，取回来 ``uuid.UUID(<float>)`` 抛
``'float' object has no attribute 'replace'``。main 上 CI 撞过一次（一条
``runtime_client`` 夹具在 ``db.flush()`` 的 insertmanyvalues 那一步炸掉）；
随机 uuid4 撞上的概率大约百万分之一，所以本机跑不出来，而用户的库里会悄悄出现
一行再也读不回来的记录。

这里的 id 是挑出来的、**必然**撞上的写法，每条都先证明「旧的声明真会把它变成数」。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.types import UUID

from app import database
from app.database import Base, _holds_text, _sqlite_keeps_text

# 合法的 uuid4 形状（第 13 位是 4，第 17 位是 8/9），SQLite 读作一个数：
LOOKS_LIKE_A_NUMBER = {
    "real": "12345678-9012-4345-8789-012345678901",      # 32 位全是数字，超出 int64
    "integer": "12345678-9012-4345-8e00-000000000001",   # 1234…58 × 10¹，放得进 int64
    "infinity": "12345678-9012-4345-9e99-999999999999",  # 指数九十多万亿
}
AN_ORDINARY_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _old_declaration_stores_it_as(hex_id: str) -> str:
    """真写进一个声明成 UUID 的列（CAST 的换算与列亲和性在细节上不一样，不拿它代替）。"""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE old (id UUID)")
    con.execute("INSERT INTO old VALUES (?)", (hex_id.replace("-", ""),))
    return con.execute("SELECT typeof(id) FROM old").fetchone()[0]


def _a_user(user_id: str, email: str):
    from app.models.user import User

    return User(id=user_id, email=email, hashed_password="x", display_name=email)


@pytest.mark.parametrize("kind", sorted(LOOKS_LIKE_A_NUMBER))
async def test_an_id_that_looks_like_a_number_comes_back_as_the_same_string(
    tmp_path: Path, kind: str,
) -> None:
    from app.models.project import Project
    from app.models.user import User

    the_id = LOOKS_LIKE_A_NUMBER[kind]
    assert _old_declaration_stores_it_as(the_id) == {"infinity": "real"}.get(kind, kind), \
        "前提：旧的声明会把它存成数"

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            # 两行一起 flush：走的正是 CI 炸掉的那条 insertmanyvalues + RETURNING 哨兵
            db.add_all([_a_user(the_id, "numeric@lab.local"),
                        _a_user(AN_ORDINARY_ID, "x@lab.local")])
            await db.flush()
            db.add(Project(owner_id=the_id, name="owned by the numeric-looking id"))
            await db.commit()

        async with sessions() as db:
            assert (await db.get(User, the_id)).id == the_id
            owner = await db.scalar(select(Project.owner_id))
            assert owner == the_id
            stored = (await db.execute(text(
                "SELECT typeof(users.id), users.id, typeof(projects.owner_id) "
                "FROM users JOIN projects ON projects.owner_id = users.id"
            ))).one()
        assert tuple(stored) == ("text", the_id.replace("-", ""), "text")
    finally:
        await engine.dispose()


def test_every_column_that_holds_text_keeps_text_on_sqlite() -> None:
    """扫模型，不列名单：以后谁再写一个 SQLite 上不是 TEXT 亲和性的字符串列，这里红。"""
    import app.models  # noqa: F401

    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        offenders = [
            f"{table.name}.{column.name} {column.type.compile(dialect=conn.dialect)}"
            for table in Base.metadata.sorted_tables for column in table.columns
            if _holds_text(column)
            and not _sqlite_keeps_text(conn, column.type.compile(dialect=conn.dialect))
        ]
        checked = sum(_holds_text(c) for t in Base.metadata.sorted_tables for c in t.columns)
    assert checked > 100, f"只扫到 {checked} 列 —— 模型没注册全？"
    assert not offenders, f"这些列在 SQLite 上会把像数的字符串存成数：{offenders}"


# ── 装过旧版的库：就地换成 TEXT 亲和性 ─────────────────────────────────────────


def _the_old_models():
    """旧版建库时的样子：同一套模型，只是 id 列声明成 ``UUID``（旧版写的正是它）。"""
    from sqlalchemy import MetaData, Uuid

    import app.models  # noqa: F401

    old = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(old)
    for table in old.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Uuid):
                column.type = UUID(as_uuid=False)
    return old


async def _an_old_database(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(_the_old_models().create_all)
        # 让 users 的 DDL 长成真机上那样：后加的列被 SQLite 接在原文末尾同一行上
        await conn.execute(text("ALTER TABLE users DROP COLUMN must_change_password"))
        for user_id, email in ((AN_ORDINARY_ID, "old@lab.local"),
                               (LOOKS_LIKE_A_NUMBER["real"], "lost@lab.local")):
            await conn.execute(text(
                "INSERT INTO users (id, email, hashed_password, display_name, role, "
                "institution_id, institution_name, is_active) "
                "VALUES (:id, :email, 'x', 'Old', 'researcher', 'local', 'Local', 1)"
            ), {"id": user_id.replace("-", ""), "email": email})
        await conn.execute(text(
            "INSERT INTO projects (id, owner_id, name, status) VALUES (:id, :owner, 'P', 'ACTIVE')"
        ), {"id": "5b1f4c2a9d3e4f6a8b7c0d1e2f3a4b5c", "owner": AN_ORDINARY_ID.replace("-", "")})
    return engine


def _declared_types(path: Path) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return {f"{t}.{c}": decl for (t,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                for _, c, decl, *_ in con.execute(f'PRAGMA table_info("{t}")')}
    finally:
        con.close()


def _indexes(path: Path) -> dict[str, set]:
    con = sqlite3.connect(path)
    try:
        return {t: {row[1:] for row in con.execute(f'PRAGMA index_list("{t}")')}
                for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


async def test_an_old_database_is_rebuilt_so_its_ids_keep_text(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    from app.models.user import User

    path = tmp_path / "old.db"
    engine = await _an_old_database(path)
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr("app.config.settings.database_url", "sqlite+aiosqlite:///irrelevant")
    before = _declared_types(path)
    assert before["users.id"] == before["projects.owner_id"] == "UUID", "前提：旧库长这样"
    lost = sqlite3.connect(path).execute(
        "SELECT id, typeof(id) FROM users WHERE email = 'lost@lab.local'").fetchone()
    assert lost[1] == "real", f"前提：旧库里那一行已经被存成数了：{lost}"
    indexes_before = _indexes(path)
    try:
        with caplog.at_level(logging.INFO, logger=database.logger.name):
            await database.create_schema_for_unmanaged_databases()

        after = _declared_types(path)
        uuid_columns = [key for key, decl in before.items() if decl == "UUID"]
        assert len(uuid_columns) > 30, uuid_columns
        assert {after[key] for key in uuid_columns} == {"CHAR(32)"}, after
        assert {k: v for k, v in after.items() if k not in uuid_columns} == \
            {k: v for k, v in before.items() if k not in uuid_columns} | {
                "users.must_change_password": "BOOLEAN"}, "别的列的声明一个都不该动"
        assert _indexes(path) == indexes_before, "索引与唯一约束要原样留着"

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("users.id" in m and repr(lost[0]) in m and "real" in m for m in errors), \
            f"读不回来的那一行要带着原值被报出来：{errors}"
        assert not [m for m in errors if "could not rebuild" in m], errors
        rebuilt = {r.getMessage() for r in caplog.records if "rebuilt table" in r.getMessage()}
        assert any("table users " in m for m in rebuilt), rebuilt

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            assert (await db.get(User, AN_ORDINARY_ID)).email == "old@lab.local"
            db.add(_a_user(LOOKS_LIKE_A_NUMBER["integer"], "new@lab.local"))
            await db.commit()
        async with sessions() as db:
            back = await db.scalar(select(User.id).where(User.email == "new@lab.local"))
        assert back == LOOKS_LIKE_A_NUMBER["integer"], "升级之后新写进去的要原样取回来"

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=database.logger.name):
            await database.create_schema_for_unmanaged_databases()  # 第二次启动
        assert not [r for r in caplog.records if "rebuilt table" in r.getMessage()]
        assert [r for r in caplog.records
                if r.levelno >= logging.ERROR and "users.id" in r.getMessage()], \
            "那一行还读不回来，每次启动都要说"
    finally:
        await engine.dispose()


async def test_a_rebuild_that_fails_halfway_leaves_the_table_as_it_was(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "old.db"
    engine = await _an_old_database(path)
    real_shape = database._table_shape
    calls = {"n": 0}

    def a_shape_that_changes_on_the_way(sync_conn, name):
        calls["n"] += 1
        shape = real_shape(sync_conn, name)
        return shape if calls["n"] % 2 else (*shape[:-1], -1)  # 重建后那次对不上

    monkeypatch.setattr(database, "_table_shape", a_shape_that_changes_on_the_way)
    users = "SELECT * FROM users ORDER BY email"
    rows_before = sqlite3.connect(path).execute(users).fetchall()
    try:
        async with engine.begin() as conn:
            outcome = await conn.run_sync(database._give_text_columns_text_affinity)
        assert outcome and all(failure for _, _, failure in outcome), outcome
        assert _declared_types(path)["users.id"] == "UUID", "失败了就该原样留着"
        assert sqlite3.connect(path).execute(users).fetchall() == rows_before
        assert not sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '\\_retyped\\_%' ESCAPE '\\'"
        ).fetchall(), "半截的中转表不许留下"
    finally:
        await engine.dispose()


def test_only_the_named_column_types_change_in_the_create_table_text() -> None:
    original = (
        "CREATE TABLE users (\n\tid UUID NOT NULL, \n\t\"order\" UUID, \n"
        "\tnote VARCHAR(20) DEFAULT 'a, (b)' CHECK (length(note) IN (1, 2)), \n"
        "\tcreated_at DATETIME NOT NULL, must_change_password BOOLEAN NOT NULL DEFAULT 0, \n"
        "\tPRIMARY KEY (id), \n\tFOREIGN KEY(\"order\") REFERENCES users (id)\n)"
    )
    rewritten = database._create_table_with_new_column_types(
        original, "_retyped_users", {"id": ("UUID", "CHAR(32)"), "order": ("UUID", "CHAR(32)")})
    assert rewritten == (
        original.replace("CREATE TABLE users (", 'CREATE TABLE "_retyped_users" (')
        .replace("id UUID NOT NULL", "id CHAR(32) NOT NULL")
        .replace('"order" UUID', '"order" CHAR(32)')
    )
    con = sqlite3.connect(":memory:")
    con.execute(rewritten)
    assert [row[2] for row in con.execute('PRAGMA table_info("_retyped_users")')][:2] == \
        ["CHAR(32)", "CHAR(32)"]

    with pytest.raises(ValueError, match="not declared as"):
        database._create_table_with_new_column_types(
            original, "x", {"note": ("UUID", "CHAR(32)")})
    with pytest.raises(ValueError, match="not in the CREATE TABLE"):
        database._create_table_with_new_column_types(
            original, "x", {"missing": ("UUID", "CHAR(32)")})
