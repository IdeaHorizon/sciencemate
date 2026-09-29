"""Database connection and session management."""

import logging
import re
import uuid
import warnings
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)


class UTCDateTime(TypeDecorator[datetime]):
    """一个时刻，在每种方言上取回来都是 aware 的 UTC。

    列的 DDL 与 ``DateTime(timezone=True)`` 一字不差（impl 就是它），所以不需要
    迁移。区别全在驱动边界上：Postgres 本来就给 aware，SQLite 把时区信息丢掉、
    取回来是 naive —— 于是同一行代码在个人档上 ``TypeError: can't subtract
    offset-naive and offset-aware datetimes``，在组织档上没事。此前的答案是
    在每个读到时间戳的地方手抄一遍 ``replace(tzinfo=UTC)``（auth、run_liveness、
    harness_sessions、restart_resume、execution_ingest、sessions、settings 各一份），
    而漏抄的那一处只在 SQLite 上才炸。

    这里把它收成一处：写入前归一到 UTC（带别的时区的值先换算，naive 视为 UTC），
    读出后补上显式的 UTC 时区。存量 naive 值本来就是按 UTC 写的（``func.now()``
    在 SQLite 上是 ``CURRENT_TIMESTAMP``，UTC），所以这样解释它们也让已经装好的
    个人库与 Postgres 口径一致。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


# Lazy initialization — engine created on first use, not at import time
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _pool_options(url: str) -> dict:
    """连接池参数只对服务器型数据库有意义。

    SQLite（个人档的库）走的是文件，SQLAlchemy 给它的默认池不接受
    ``pool_size / max_overflow / pool_timeout`` —— 传了就是
    ``TypeError: Invalid argument(s)``，服务起不来。所以这几个参数按方言给，
    而不是无条件给。
    """
    if url.startswith("sqlite"):
        return {}
    return {
        "pool_size": 20,
        "max_overflow": 10,
        "pool_timeout": 30,      # seconds to wait for a connection from pool
        "pool_recycle": 1800,    # recycle connections after 30 minutes
        "pool_pre_ping": True,   # verify connection is alive before using
    }


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        from app.config import settings
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            **_pool_options(settings.database_url),
        )
    return _engine


async def has_table(connection, name: str) -> bool:
    """这个库里有没有这张表 —— 方言中立。

    从前这个问题用 ``SELECT to_regclass(...)`` 和 ``pg_catalog.pg_tables`` 各答
    一次，两句都只在 Postgres 上有效；个人档的 SQLite 库上它们不是返回
    “没有”，而是直接报错，于是整个就绪检查被当成“数据库坏了”。
    """
    from sqlalchemy import inspect as sa_inspect

    return await connection.run_sync(lambda sync_conn: sa_inspect(sync_conn).has_table(name))


async def table_names(connection) -> set[str]:
    """这个库里现有的表名 —— 方言中立（同上）。"""
    from sqlalchemy import inspect as sa_inspect

    return set(
        await connection.run_sync(lambda sync_conn: sa_inspect(sync_conn).get_table_names())
    )


async def create_schema_for_unmanaged_databases() -> None:
    """个人档的 SQLite 库没有 alembic —— schema 直接从模型建，**并补上缺的列**。

    组织档（Postgres）一律不碰：那儿 schema 由 alembic 管，落后就该被
    ``_refuse_to_serve_a_schema_we_were_not_written_for`` 拦住。在这里对 Postgres
    调 ``create_all`` 会让“库落后”被悄悄补上一半，正是那道闸要防的事。

    ``create_all`` 只建**缺的表**，从不给已有的表加列。于是一台装过旧版的机器
    升级后，模型里新加的每一列在它的库里都不存在，而启动闸看到没有
    ``alembic_version`` 表就放行 —— 症状是碰到那张表的每个请求 500
    （2026-09-18 真机：0.5.0 加了 ``users.must_change_password``，装过 09-07 版
    的机器登录态、更新检查、模型设置全 500，日志 ``no such column``）。
    所以建完表再对一遍：模型有、库里没有的列，逐列 ``ALTER TABLE ADD COLUMN``。

    再对一遍列的**亲和性**：旧版把 id 列声明成 ``UUID``，SQLite 按名字给它 NUMERIC
    亲和性，一个恰好像数的十六进制 id 会被存成数（``_give_text_columns_text_affinity``）。
    改之前先把 ORM 已经读不回来的格子报出来 —— 那时它们还是原样。
    """
    from app.config import settings

    if not settings.database_url.startswith("sqlite"):
        return
    import app.models  # noqa: F401 —— 建表前必须让所有模型注册进 Base.metadata

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_the_columns_the_models_have_and_the_database_lacks)
        reindexed = await conn.run_sync(_make_the_indexes_match_the_models)
        unreadable = await conn.run_sync(_uuid_cells_that_cannot_be_read_back)
        retyped = await conn.run_sync(_give_text_columns_text_affinity)
    for table, column in added:
        logger.info("Schema: added missing column %s.%s", table, column)
    for what, name in reindexed:
        logger.info("Schema: %s index %s", what, name)
    for table, column, rowid, value, stored_as in unreadable:
        logger.error(
            "Schema: %s.%s rowid %s holds %r (stored as %s), which is not a UUID. SQLite "
            "turned the id into a number before this column kept text; the original id "
            "cannot be recovered from that number, and rows carrying it will not load.",
            table, column, rowid, value, stored_as,
        )
    for table, columns, failure in retyped:
        if failure is None:
            logger.info("Schema: rebuilt table %s so that %s keep text", table, ", ".join(columns))
        else:
            logger.error("Schema: could not rebuild table %s so that %s keep text: %s",
                         table, ", ".join(columns), failure)


async def normalise_retired_roles() -> list[tuple[str, str, str]]:
    """库里还写着已经退役的身份（`models.user.RETIRED_ROLES`）的行，转成它现在的样子。

    `group_admin` 这一档删了（`UserRole` 的说明）。枚举里删掉一个值，库里的行不会
    跟着变：那个人登录后 `/auth/me` 报一个界面不认识的身份，权限表查不到它、按
    "未知身份"回落 —— 能用，但说不清他是什么。所以在启动时转一次，SQLite 和
    Postgres 同一处；转过就没有了，再跑一次什么都不做。

    未用的请柬也转：拿着一张 group_admin 请柬来注册的人，进来就该是成员。
    返回 [(表, 标识, 旧身份)]，每一行都进日志 —— 身份变了的人要能被点名。
    """
    from sqlalchemy import select

    from app.models.invitation import Invitation
    from app.models.user import RETIRED_ROLES, User

    changed: list[tuple[str, str, str]] = []
    factory = get_session_factory()
    async with factory() as db:
        for old, new in RETIRED_ROLES.items():
            for person in (await db.scalars(select(User).where(User.role == old))).all():
                person.role = new
                changed.append(("users", person.email, old))
            for card in (await db.scalars(select(Invitation).where(
                    Invitation.role == old, Invitation.accepted_at.is_(None)))).all():
                card.role = new
                changed.append(("invitations", str(card.id), old))
        await db.commit()
    for table, who, old in changed:
        logger.warning("Roles: %s %s was %s, now %s", table, who, old, RETIRED_ROLES[old])
    return changed


def _add_the_columns_the_models_have_and_the_database_lacks(
    sync_conn, metadata=None,
) -> list[tuple[str, str]]:
    """对每张已有的表：模型里有、库里没有的列，加上。返回加了哪些。

    列的 DDL 由 SQLAlchemy 按方言渲染，不手抄类型名。SQLite 给已有的行补一个
    ``NOT NULL`` 列必须带 DEFAULT；模型里写的多半是 Python 侧的
    ``default=False``（不是 ``server_default``），这里把它作为字面量渲染进
    DEFAULT —— 存量行拿到的值正是新行会拿到的值。既 ``NOT NULL`` 又给不出常量
    默认值的列，这里补不了：明说，别加成 nullable 让模型与库悄悄分叉。
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import literal
    from sqlalchemy.schema import CreateColumn

    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    dialect = sync_conn.dialect
    added: list[tuple[str, str]] = []
    for table in (metadata or Base.metadata).sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all 刚建的，本来就全
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = str(CreateColumn(column).compile(dialect=dialect))
            if column.server_default is None and column.default is not None:
                if not column.default.is_scalar:
                    raise RuntimeError(
                        f"Cannot add column {table.name}.{column.name} to an existing SQLite "
                        f"database: its default is computed, not a constant. Give it a "
                        f"server_default."
                    )
                value = literal(column.default.arg, type_=column.type).compile(
                    dialect=dialect, compile_kwargs={"literal_binds": True},
                )
                ddl += f" DEFAULT {value}"
            elif column.server_default is None and not column.nullable:
                raise RuntimeError(
                    f"Cannot add column {table.name}.{column.name} to an existing SQLite "
                    f"database: it is NOT NULL and has no default for the rows already there. "
                    f"Give it a default or a server_default."
                )
            sync_conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')
            added.append((table.name, column.name))
    return added


def _make_the_indexes_match_the_models(sync_conn, metadata=None) -> list[tuple[str, str]]:
    """对每张已有的表：模型声明的索引，库里得有；**唯一性不一样的，重建**。

    加列那一段（上面）管的是"模型多了一列"。这一段管的是另一半：**一条规则改了**。
    2026-09-22 把"邮箱在一台服务器上唯一"改成"邮箱在一个组织里唯一"，模型里是
    换掉一个唯一索引 —— 而 `create_all` 对已经存在的表一个索引都不碰，于是一台
    装过旧版的机器上，旧的 `ix_users_email`（UNIQUE）还在拦着，新的那条从来没建，
    同一个人依旧进不了第二个组织。和当初漏加列是同一个形状：库与模型悄悄分叉。

    只碰**模型声明过名字**的索引：库里那些谁建的都不知道的（手建的、别的工具建的）
    一律不动。唯一性对不上就先 DROP 再 CREATE —— SQLite 上这只是两条 DDL，
    不用重建整张表（所以已经装在同事机器上的那些库能就地升上来）。
    """
    from sqlalchemy import Column
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, DropIndex

    def plain(index) -> bool:
        """这条索引是不是"几个列"而已。

        带表达式的（`coalesce(base_url,'')` 那种）SQLAlchemy **反射不出来** ——
        inspector 直接跳过它并 warn。于是库里明明有，这里看着像"缺了"，再 CREATE
        一次就 `index already exists`。真拿一个旧库跑才撞见的：比不了的东西就别碰。
        """
        return all(isinstance(part, Column) for part in index.expressions)

    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    changed: list[tuple[str, str]] = []
    for table in (metadata or Base.metadata).sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all 刚建的，索引跟着一起建了
        with warnings.catch_warnings():
            # 反射不出来的那种（见 `plain`）本来就不比 —— 它每次都 warn 一句，而这里现在
            # 也在安装脚本的终端里跑（`app.pro.manage create-admin`），那句话在那儿只是噪音。
            warnings.filterwarnings(
                "ignore", message="Skipped unsupported reflection of expression-based index")
            in_database = {info["name"]: bool(info.get("unique"))
                           for info in inspector.get_indexes(table.name) if info.get("name")}
        for index in table.indexes:
            if not plain(index):
                continue
            if index.name in in_database:
                if in_database[index.name] == bool(index.unique):
                    continue
                # 同名不同义 —— 唯一性变了。旧的必须先走，否则它继续拦着。
                sync_conn.execute(DropIndex(index))
                changed.append(("rebuilt", index.name))
            else:
                changed.append(("created", index.name))
            sync_conn.execute(CreateIndex(index))
        # 模型里已经不存在的**同名规则**：`ix_<表>_<列>` 这种由 `index=True` 生成的
        # 名字，改掉声明之后旧的那条还留在库里。只清这一类（名字能对上某一列的），
        # 手建的索引不碰。
        declared = {index.name for index in table.indexes}
        for name, unique in in_database.items():
            if name in declared or not unique:
                continue
            column = name[len(f"ix_{table.name}_"):] if name.startswith(f"ix_{table.name}_") else None
            if column and column in table.columns and not table.columns[column].index:
                sync_conn.exec_driver_sql(f'DROP INDEX "{name}"')
                changed.append(("dropped", name))
    return changed


def _holds_text(column) -> bool:
    """这一列交给 SQLite 的是字符串。

    ``Uuid`` 在 SQLite 上绑成 32 位十六进制串；``Enum`` 是 ``String``。其余的按它
    在 Python 这边是什么来答，好让将来新写的类型不用先登记在这里。
    """
    if isinstance(column.type, (String, Uuid)):
        return True
    try:
        return issubclass(column.type.python_type, str)
    except NotImplementedError:
        return False


def _sqlite_keeps_text(sync_conn, declared_type: str) -> bool:
    """声明成 ``declared_type`` 的列，SQLite 会不会把写进去的字符串原样存成字符串。

    亲和性按类型**名字**推：名字里有 CHAR / CLOB / TEXT 才是 TEXT。``UUID`` 哪条
    都不中，落到 NUMERIC —— 于是 ``12345678901243458e00000000000001`` 这种恰好是
    一个合法数字写法的十六进制 id 被存成 INTEGER（纯数字的存成 REAL），取回来
    ``uuid.UUID(123456789012434576)`` 就炸，而且原值已经丢了。规则不在这里抄一遍：CAST
    用的是同一套亲和性规则，直接问 SQLite。
    """
    if not declared_type.strip():
        return True  # 不声明类型 = 没有亲和性，原样存
    probe = sync_conn.exec_driver_sql(f"SELECT typeof(CAST('1' AS {declared_type}))")
    return probe.scalar() == "text"


def _uuid_cells_that_cannot_be_read_back(
    sync_conn, metadata=None,
) -> list[tuple[str, str, int, object, str]]:
    """Uuid 列里 ORM 读不回来的格子：[(表, 列, rowid, 存着的值, SQLite 存成的类型)]。

    一个被存成数的 id 找不回原值（数字只留得下十几位有效数字，``e`` 后面那段按
    指数算掉了），这里只把它报出来，不替人改：改成什么都是编的，而指着它的那些
    行、目录名、别处存的原文拼法都会跟着对不上。先用 SQL 挑出不是"32 位小写十六
    进制文本"（绑定时写进去的样子）的，再按 ORM 自己的标准（``uuid.UUID``）判。
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    found: list[tuple[str, str, int, object, str]] = []
    for table in (metadata or Base.metadata).sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if not isinstance(column.type, Uuid) or column.name not in present:
                continue
            c = f'"{column.name}"'
            rows = sync_conn.exec_driver_sql(
                f'SELECT rowid, {c}, typeof({c}) FROM "{table.name}" WHERE {c} IS NOT NULL '
                f"AND (typeof({c}) != 'text' OR length({c}) != 32 OR {c} GLOB '*[^0-9a-f]*')"
            )
            for rowid, value, stored_as in rows:
                if stored_as == "text":
                    try:
                        uuid.UUID(value)
                        continue
                    except ValueError:
                        pass
                found.append((table.name, column.name, rowid, value, stored_as))
    return found


def _give_text_columns_text_affinity(
    sync_conn, metadata=None,
) -> list[tuple[str, list[str], str | None]]:
    """模型交字符串、库里的声明却不是 TEXT 亲和性的列：重建它们所在的表。

    返回 [(表, [列], 失败原因或 None)]。SQLite 改不了一列的声明类型，只能按官方的
    做法换一张表（https://sqlite.org/lang_altertable.html#otheralter）：新表建好、
    数据拷过去、旧表删掉、新表改名、索引照原文重建。新表的 DDL 是**库里那份原文**
    只换这几列的类型名 —— 不从模型重新生成：旧库上后来追加的列、命名约束、列序
    都留着原样，模型与库在别处的差别不借这次机会悄悄改掉。

    重建不了的表（有视图、外键在强制执行、原文对不上）照旧用着，原因报出来：它
    还是今天这个样子，不比升级前更坏。
    """
    from sqlalchemy import inspect as sa_inspect

    dialect = sync_conn.dialect
    existing_tables = set(sa_inspect(sync_conn).get_table_names())
    rebuilt: list[tuple[str, list[str], str | None]] = []
    for table in (metadata or Base.metadata).sorted_tables:
        if table.name not in existing_tables:
            continue
        declared = {row[1]: row[2] for row in
                    sync_conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
        retype: dict[str, tuple[str, str]] = {}
        for column in table.columns:
            if column.name not in declared or not _holds_text(column):
                continue
            wanted = column.type.compile(dialect=dialect)
            if (not _sqlite_keeps_text(sync_conn, declared[column.name])
                    and _sqlite_keeps_text(sync_conn, wanted)):
                retype[column.name] = (declared[column.name], wanted)
        if retype:
            failure = _rebuild_table_with_new_column_types(sync_conn, table.name, retype)
            rebuilt.append((table.name, sorted(retype), failure))
    return rebuilt


def _rebuild_table_with_new_column_types(
    sync_conn, name: str, retype: dict[str, tuple[str, str]],
) -> str | None:
    """换一张表，只换 ``retype`` 里那几列的声明类型。成功返回 None，否则返回原因、表不动。"""
    if sync_conn.exec_driver_sql("PRAGMA foreign_keys").scalar():
        return "foreign keys are enforced on this connection, so DROP TABLE would cascade"
    if sync_conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type = 'view'").scalar():
        return "the database has views, and renaming a table under them is not handled here"
    staging = f"_retyped_{name}"
    original = sync_conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).scalar()
    try:
        create_staging = _create_table_with_new_column_types(original, staging, retype)
    except ValueError as exc:
        return str(exc)
    companions = [row[0] for row in sync_conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL", (name,))]
    columns, *rest = _table_shape(sync_conn, name)
    expected = ([(cid, col, retype[col][1] if col in retype else type_, *more)
                 for cid, col, type_, *more in columns], *rest)
    names = ", ".join(f'"{col}"' for _, col, *_, hidden in columns if not hidden)

    sync_conn.exec_driver_sql("SAVEPOINT retype_columns")
    try:
        sync_conn.exec_driver_sql(create_staging)
        sync_conn.exec_driver_sql(f'INSERT INTO "{staging}" ({names}) SELECT {names} FROM "{name}"')
        sync_conn.exec_driver_sql(f'DROP TABLE "{name}"')
        sync_conn.exec_driver_sql(f'ALTER TABLE "{staging}" RENAME TO "{name}"')
        for sql in companions:
            sync_conn.exec_driver_sql(sql)
        after = _table_shape(sync_conn, name)
        if after != expected:
            raise RuntimeError(
                f"the rebuilt table differs beyond the column types: {after} != {expected}")
    except Exception as exc:
        sync_conn.exec_driver_sql("ROLLBACK TO retype_columns")
        sync_conn.exec_driver_sql("RELEASE retype_columns")
        return f"{type(exc).__name__}: {exc}"
    sync_conn.exec_driver_sql("RELEASE retype_columns")
    return None


def _table_shape(sync_conn, name: str) -> tuple:
    """重建前后必须一样的东西：列（名、类型、非空、默认值、主键）、索引、外键、行数。"""
    def pragma(what: str) -> list[tuple]:
        return [tuple(row) for row in sync_conn.exec_driver_sql(f'PRAGMA {what}("{name}")')]

    return (
        pragma("table_xinfo"),
        sorted(row[1:] for row in pragma("index_list")),
        sorted(row[2:] for row in pragma("foreign_key_list")),
        sync_conn.exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar(),
    )


_QUOTE_CLOSES = {'"': '"', "'": "'", "`": "`", "[": "]"}
_LEADING_NAME = re.compile(r'\s*("(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[^\s"`\[(),]+)\s+')


def _create_table_with_new_column_types(
    sql: str, new_name: str, retype: dict[str, tuple[str, str]],
) -> str:
    """``CREATE TABLE`` 原文：表名换成 ``new_name``，``retype`` 里的列换类型名，别的一个字不动。

    只在引号外数括号与逗号，切出每一项定义；列名对上、紧跟着的正是库里声明的那个
    类型名，才换。任何一处对不上都抛 ValueError —— 宁可不改，不猜。
    """
    depth, quote, cuts = 0, None, []
    for i, ch in enumerate(sql):
        if quote:
            quote = None if ch == quote else quote
        elif ch in _QUOTE_CLOSES:
            quote = _QUOTE_CLOSES[ch]
        elif ch == "(":
            depth += 1
            if depth == 1 and not cuts:
                cuts.append(i)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                cuts.append(i)
                break
        elif ch == "," and depth == 1:
            cuts.append(i)
    if depth != 0 or len(cuts) < 2:
        raise ValueError(f"cannot find the column list in {sql[:80]!r}")

    parts = [sql[start + 1:end] for start, end in zip(cuts, cuts[1:])]
    pending = {column.lower(): types for column, types in retype.items()}
    for k, part in enumerate(parts):
        match = _LEADING_NAME.match(part)
        if not match:
            continue
        token = match.group(1)
        column = (token[1:-1].replace(token[0] * 2, token[0]) if token[0] in '"`'
                  else token[1:-1] if token[0] == "[" else token).lower()
        if column not in pending:
            continue
        declared, wanted = pending.pop(column)
        rest = part[match.end():]
        if not re.match(re.escape(declared) + r"(?![\w(])", rest, re.IGNORECASE):
            raise ValueError(f"column {column} is not declared as {declared!r} in {part.strip()!r}")
        parts[k] = part[:match.end()] + wanted + rest[len(declared):]
    if pending:
        raise ValueError(f"columns {sorted(pending)} are not in the CREATE TABLE statement")
    return f'CREATE TABLE "{new_name}" (' + ",".join(parts) + ")" + sql[cuts[-1] + 1:]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
