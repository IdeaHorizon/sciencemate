"""同一个模型名可以挂在两个端点上；重复的那条要被 409 挡住，不是 500。

## 现场（2026-08-24，node20）

设置页里已经有一条「Kimi K3 · 积算」——`openai_compatible / kimi-k3`，指向
`https://api.icompify.com/v1`。再加一条同样跑 kimi-k3、但走自建网关
`http://47.103.79.174:30080/v1` 的连接，保存直接失败。

两件事同时坏了：

1. `uq_model_backend_scope_model` 是 `(scope_kind, scope_id, provider, model)`，
   **不含 base_url**。于是"同一个模型名在两个不同端点"这个完全合法的场景在
   schema 层就被判成重复。当时靠把 provider 从 `openai_compatible` 改写成
   `kimi` 绕了过去——两者在这条路径上行为完全一致，provider 只是个标签——
   但那是把身份编进了一个不表达身份的字段，不是修复。
2. 撞了约束之后 `IntegrityError` 一路冒到顶，接口交回 **500 Internal Server
   Error，正文一个字没有**。人看到 500 只会以为平台坏了。

## 这些测试守什么

- 端点进身份：同 scope/provider/model、不同 base_url = 两条连接。
- 端点没填也还是身份的一部分：两条都不填 = 同一条，仍然要撞。（这是
  `coalesce` 的理由 —— 直接把 base_url 列进唯一键，PG/SQLite 里 NULL 不参与
  唯一性比较，等于对所有没填端点的连接**取消**了这道约束。）
- 真撞上时是 409，而且话里点得出**是哪一条、它挂在哪**。
- 被拒之后 session 里不许留下半条记录。
- 先查后写中间那个窗口输掉的请求，拿到的也得是 409。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import hash_password
from app.database import get_db
from app.main import app
from app.models.model_backend import ModelBackendConfig
from app.models.user import User
from app.services.model_backends import find_conflicting_backend

PASSWORD = "EndpointIdentity2026!"
INSTITUTION = "ieit"

ICOMPIFY = "https://api.icompify.com/v1"
SELF_HOSTED = "http://47.103.79.174:30080/v1"


@pytest.fixture(autouse=True)
def _no_real_provider_calls(monkeypatch):
    """建连接会当场探一次 provider。这些测试问的不是探针，别去打真端点。"""

    async def _probe(_config):
        return None, "probe skipped in tests"

    monkeypatch.setattr("app.services.model_backends.probe_backend_credential", _probe)


@pytest_asyncio.fixture(autouse=True)
async def _an_owner(db_session):
    """`created_by_user_id` 是外键 —— 直接写库的那几条也得有个主人。"""
    if await db_session.scalar(select(User.id)) is None:
        db_session.add(
            User(
                email="admin@atrium.local",
                hashed_password=hash_password(PASSWORD),
                display_name="Institution Admin",
                role="institution_admin",
                institution_id=INSTITUTION,
                institution_name="Atrium University",
            )
        )
        await db_session.commit()


@pytest_asyncio.fixture
async def client(db_engine):
    """每个请求一个 session，而且**收尾要 commit**。

    共用一个不 commit 的 session，第二个请求看到的就只是第一个请求留在内存里
    的东西 —— 唯一性到底是不是库在管，这样测不出来。
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 直接签 token，不走登录端点（那扇门是专业版的，公开树里没有）。
        from sqlalchemy import select

        from app.auth import create_access_token

        async with factory() as session:
            admin = await session.scalar(select(User).where(User.email == "admin@atrium.local"))
        assert admin is not None
        ac.headers["Authorization"] = f"Bearer {create_access_token(admin)}"
        yield ac
    app.dependency_overrides.clear()


async def _create(client, *, display_name, model="kimi-k3", base_url=ICOMPIFY, **extra):
    payload = {
        "provider": "openai_compatible",
        "display_name": display_name,
        "model": model,
        "api_key": "sk-test",
        **extra,
    }
    if base_url is not None:
        payload["base_url"] = base_url
    return await client.post("/api/v1/settings/model-backends", json=payload)


async def _row(db, *, display_name, model="kimi-k3", base_url=ICOMPIFY):
    user_id = await db.scalar(select(User.id))
    return ModelBackendConfig(
        scope_kind="institution",
        scope_id=INSTITUTION,
        provider="openai_compatible",
        display_name=display_name,
        model=model,
        base_url=base_url,
        credential_source="none",
        created_by_user_id=user_id,
    )


# ---------------------------------------------------------------------------
# 下面三条**绕开接口直接写库**。
#
# 走接口测不出唯一键长什么样：写入前那道 `_require_free_endpoint` 用的是
# `coalesce(base_url, '')`，它自己就能把"两条都没填端点"判成重复并交回 409。
# 于是把唯一键写成 `(..., base_url)`（NULL 不参与比较、这道约束对没填端点的
# 连接等于不存在）时，接口层的测试**全绿** —— 实测过，一条都没红。
#
# 两条路通向同一个判决，断言落在判决上就分不清是哪条给的。这几条只留库这一条路。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_database_itself_keeps_two_endpoints_apart(db_session):
    db_session.add(await _row(db_session, display_name="积算", base_url=ICOMPIFY))
    db_session.add(await _row(db_session, display_name="自建", base_url=SELF_HOSTED))
    await db_session.flush()  # 不该抛：两个端点 = 两条连接


@pytest.mark.asyncio
async def test_the_database_itself_rejects_a_duplicate_endpoint(db_session):
    db_session.add(await _row(db_session, display_name="积算", base_url=ICOMPIFY))
    await db_session.flush()
    db_session.add(await _row(db_session, display_name="又一条", base_url=ICOMPIFY))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_the_database_itself_rejects_two_rows_without_an_endpoint(db_session):
    """`NULL` 不参与唯一性比较 —— 唯一键必须把"没填"折成一个真值。

    唯一键写成 `(..., base_url)` 的话这两行双双落库，列表里出现两条肉眼完全
    一样的连接，而没有任何一层会吭声。
    """
    db_session.add(await _row(db_session, display_name="内置", base_url=None))
    await db_session.flush()
    db_session.add(await _row(db_session, display_name="又一条内置", base_url=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_the_database_itself_treats_a_blank_endpoint_as_no_endpoint(db_session):
    db_session.add(await _row(db_session, display_name="内置", base_url=None))
    await db_session.flush()
    db_session.add(await _row(db_session, display_name="空串", base_url=""))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_a_connection_is_never_its_own_conflict(db_session):
    """撞键之后那句"谁占着这个身份"，答案不能是它自己。

    编辑面改的要是别的字段（`is_enabled`、显示名），身份根本没动。这时候要是
    **别的**约束炸了，兜底一查就查到它自己，于是一个不相干的失败被讲成"端点
    重了"，人就去改一个根本没问题的字段。
    """
    row = await _row(db_session, display_name="积算", base_url=ICOMPIFY)
    db_session.add(row)
    await db_session.flush()

    identity = dict(
        scope_kind=row.scope_kind,
        scope_id=row.scope_id,
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
    )
    assert await find_conflicting_backend(db_session, **identity, exclude_id=row.id) is None
    # 换个人问，同一个身份当然是被它占着的 —— 上面那个 None 不是因为查询本身瞎。
    occupant = await find_conflicting_backend(db_session, **identity)
    assert occupant is not None and occupant.id == row.id


@pytest.mark.asyncio
async def test_the_same_model_at_two_endpoints_is_two_connections(client):
    """node20 那条真实诉求：kimi-k3 在积算，也在我们自己的网关上。"""
    first = await _create(client, display_name="Kimi K3 · 积算", base_url=ICOMPIFY)
    assert first.status_code == 201, first.text

    second = await _create(client, display_name="郗老板Kimi-K3", base_url=SELF_HOSTED)
    assert second.status_code == 201, (
        "同一个模型名挂在两个端点上是完全合法的 —— 端点不在唯一键里，"
        f"这条就永远建不出来：{second.status_code} {second.text}"
    )

    listing = await client.get("/api/v1/settings/model-backends")
    endpoints = {row["base_url"] for row in listing.json()}
    assert endpoints == {ICOMPIFY, SELF_HOSTED}


@pytest.mark.asyncio
async def test_a_duplicate_connection_is_a_409_that_names_the_one_in_the_way(client):
    """500 + 空正文 = "平台坏了"。真相是"你跟已有的那条重了"。"""
    assert (await _create(client, display_name="Kimi K3 · 积算")).status_code == 201

    duplicate = await _create(client, display_name="又一条同样的")
    assert duplicate.status_code != 500, f"重复不是平台故障：{duplicate.text!r}"
    assert duplicate.status_code == 409, duplicate.text

    detail = duplicate.json()["detail"]
    assert detail, "409 也得有正文 —— 空正文和 500 一样什么都没说"
    # 人要能凭这句话找到那条连接、并判断自己是想改它还是真要新建一条。
    assert "Kimi K3 · 积算" in detail, f"没点名是哪一条：{detail}"
    assert ICOMPIFY in detail, f"没说它挂在哪个端点：{detail}"
    assert "kimi-k3" in detail


@pytest.mark.asyncio
async def test_two_connections_without_an_endpoint_still_collide(client):
    """`NULL` 不参与唯一性比较 —— 这是把 base_url 直接列进唯一键会踩的坑。

    如果唯一键写成 `(..., base_url)`，这两条都没填端点的连接会**双双建成功**，
    列表里出现两条一模一样的记录，而谁也不会收到任何提示。
    """
    assert (await _create(client, display_name="Built-in", base_url=None)).status_code == 201

    duplicate = await _create(client, display_name="又一条 Built-in", base_url=None)
    assert duplicate.status_code == 409, (
        f"两条都没填端点 = 同一条连接，仍然要撞：{duplicate.status_code} {duplicate.text}"
    )
    assert "the provider's default endpoint" in duplicate.json()["detail"]


@pytest.mark.asyncio
async def test_a_blank_endpoint_is_the_same_as_no_endpoint(client):
    """`None` 和 `""` 在下游从来是同一件事（`if config.base_url`）。

    唯一键要是把它们当两个值，UI 上就会出现两条肉眼完全相同的连接。
    """
    assert (await _create(client, display_name="Built-in", base_url=None)).status_code == 201
    duplicate = await _create(client, display_name="空串", base_url="")
    assert duplicate.status_code == 409, duplicate.text


@pytest.mark.asyncio
async def test_editing_a_connection_onto_a_taken_endpoint_is_a_409(client):
    """改端点也能撞车 —— 写入面每一条路都得给同一个答案。"""
    first = await _create(client, display_name="Kimi K3 · 积算", base_url=ICOMPIFY)
    assert first.status_code == 201
    second = await _create(client, display_name="郗老板Kimi-K3", base_url=SELF_HOSTED)
    assert second.status_code == 201

    moved = await client.put(
        f"/api/v1/settings/model-backends/{second.json()['id']}",
        json={"base_url": ICOMPIFY},
    )
    assert moved.status_code != 500, f"改重了不是平台故障：{moved.text!r}"
    assert moved.status_code == 409, moved.text
    assert "Kimi K3 · 积算" in moved.json()["detail"]

    # 被拒了就是没改成：库里那条还指着自建网关。
    listing = await client.get("/api/v1/settings/model-backends")
    still = next(row for row in listing.json() if row["id"] == second.json()["id"])
    assert still["base_url"] == SELF_HOSTED


@pytest.mark.asyncio
async def test_editing_a_connection_without_moving_it_is_not_a_conflict(client):
    """一条连接不跟自己冲突。少了 `exclude_id`，改个显示名都会被自己挡住。"""
    created = await _create(client, display_name="Kimi K3 · 积算")
    renamed = await client.put(
        f"/api/v1/settings/model-backends/{created.json()['id']}",
        json={"display_name": "Kimi K3 · 积算（主）"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Kimi K3 · 积算（主）"


@pytest.mark.asyncio
async def test_a_rejected_edit_leaves_the_connection_as_it_was(client):
    """撞键之后 savepoint 回滚 —— 那条连接必须原样退回去，session 里不留半条。

    这里还顺带钉住一个真踩过的坑：回滚会把 `config` **过期**，兜底那段要是
    在这之后再读它的属性，就是一次同步 IO（`MissingGreenlet`），一个 409 又
    变回 500。
    """
    first = await _create(client, display_name="Kimi K3 · 积算", base_url=ICOMPIFY)
    second = await _create(client, display_name="郗老板Kimi-K3", base_url=SELF_HOSTED)
    assert first.status_code == 201 and second.status_code == 201

    moved = await client.put(
        f"/api/v1/settings/model-backends/{second.json()['id']}",
        json={"display_name": "改个名顺便挪端点", "base_url": ICOMPIFY},
    )
    assert moved.status_code == 409, moved.text

    listing = await client.get("/api/v1/settings/model-backends")
    rows = {row["id"]: row for row in listing.json()}
    assert len(rows) == 2
    kept = rows[second.json()["id"]]
    assert kept["base_url"] == SELF_HOSTED, "端点被改成了半截"
    assert kept["display_name"] == "郗老板Kimi-K3", "显示名跟着那次失败的写入变了"


@pytest.mark.asyncio
async def test_a_rejected_create_leaves_nothing_behind(client):
    """被拒的那条不许留在 session 里等着请求收尾时再插一遍。

    （feed.py 的 engagement 就是这么栽过：`db.add` 落在 savepoint 外面，
    savepoint 回滚了对象还在，收尾 commit 再插一次 → `PendingRollbackError`
    → 一个说得清楚的 409 又变回 500。）
    """
    assert (await _create(client, display_name="Kimi K3 · 积算")).status_code == 201
    assert (await _create(client, display_name="又一条同样的")).status_code == 409

    listing = await client.get("/api/v1/settings/model-backends")
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1, listing.json()


@pytest.mark.asyncio
async def test_the_409_is_produced_by_the_database_not_by_a_second_opinion(client):
    """判"重不重"的必须是唯一键本身。

    写之前先在 Python 里查一遍、查到就 409 —— 那是**第二个真相源**：它和唯一
    键各自演化，分叉的时候没有人会知道。实测过一次：把唯一键写成
    `(..., base_url)`（NULL 不参与唯一性比较）之后，因为预查自己就把重复挡下
    来了，接口层的测试一条都没红。

    这条测试钉的就是"只有一条路"：让那次查询**什么都查不到**，重复请求仍然
    必须被挡住 —— 它是被库挡住的。
    """
    from app.api.v1 import settings as settings_api

    assert (await _create(client, display_name="Kimi K3 · 积算")).status_code == 201

    calls: list[str] = []
    real = settings_api.find_conflicting_backend

    async def _counted(*args, **kwargs):
        calls.append("looked")
        return await real(*args, **kwargs)

    settings_api.find_conflicting_backend = _counted
    try:
        duplicate = await _create(client, display_name="同时提交的那条")
    finally:
        settings_api.find_conflicting_backend = real

    assert duplicate.status_code == 409, duplicate.text
    assert calls == ["looked"], (
        "这次查询只该在**库已经拒绝之后**跑一次；跑两次 = 写之前又查了一遍，"
        f"那就是第二个真相源：{calls}"
    )


@pytest.mark.asyncio
async def test_a_failure_that_is_not_about_the_endpoint_is_not_dressed_up_as_one(
    client, monkeypatch
):
    """兜底只在**真查到占位的那条**时才盖章。

    别的 `IntegrityError`（CHECK 约束之类）要是也被说成"重复连接"，人就会被
    支去改一个根本没问题的字段 —— 产生失败的那层才有资格盖章。这里让两次
    查询都看不见任何东西，于是唯一键照撞、而我们查不出是谁占着：正确的行为
    是把原来的错误原样放走，不是编一个 409。
    """
    from sqlalchemy.exc import IntegrityError

    from app.api.v1 import settings as settings_api

    assert (await _create(client, display_name="Kimi K3 · 积算")).status_code == 201

    async def _never_finds_anything(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "find_conflicting_backend", _never_finds_anything)

    with pytest.raises(IntegrityError):
        await _create(client, display_name="又一条同样的")


# ---------------------------------------------------------------------------
# 把这次的教训扫成一道闸
# ---------------------------------------------------------------------------

#: 唯一键里含可空列的那几条 —— 每条都是**有意的**，理由写在这儿。
#:
#: PG 里 NULL 不参与唯一性比较：唯一键里只要有一列可空，这道约束对"那列是
#: NULL"的所有行**等于不存在**。有时这正是要的（NULL = 不适用，多行并存是
#: 常态），有时就是 `model_backend_configs` 刚踩过的那个洞。两者长得一模一样，
#: 而错的那一种落地即失效、CI 全绿没人知道。
#:
#: 所以不设"豁免名单"，设**决定记录**：新加的唯一键只要含可空列就会把这条
#: 测试打红，来这里写下你判的是哪一种。
_NULLABLE_ON_PURPOSE = {
    # NULL = 这个会话不是从别的会话恢复出来的。绝大多数会话都是 NULL，必须
    # 允许多行 —— 折成空串反而会把整张表压成一行。
    ("sessions", "uq_sessions_recovery_source"): {"recovered_from_session_id"},
    # NULL = 这条消息不是某次命令产生的（平台自己写的、恢复时补的）。这类
    # 消息本来就该能有很多条。
    ("session_messages", "uq_session_messages_command_role"): {"command_id"},
    # 四列一起表达"这条事件来自哪一行原始 transcript"。平台自己合成的事件
    # 四列全 NULL，本来就不该被这条约束管。而真从文件解析出来的事件，
    # `IngestContext.run_id` 是必填（`__post_init__` 当场校验），重放去重另有
    # 主键兜着（id = hash(session_id, source_identity, adapter_version)）。
    ("execution_events", "uq_events_raw_source"): {
        "run_id",
        "file_identity",
        "byte_offset",
        "raw_line_hash",
    },
}


def test_a_nullable_column_in_a_unique_key_is_a_decision_not_an_accident():
    """扫盘，不写名单：新加的唯一键自动被检查。"""
    import app.main  # noqa: F401 —— 把所有 model 装进 Base.metadata
    from app.database import Base

    offenders = {}
    for table in Base.metadata.tables.values():
        keys = [(c.name, [col.name for col in c.columns]) for c in table.constraints
                if type(c).__name__ == "UniqueConstraint"]
        keys += [
            (ix.name, [getattr(e, "name", "") for e in ix.expressions])
            for ix in table.indexes
            if ix.unique
        ]
        for name, columns in keys:
            nullable = {c for c in columns if c in table.c and table.c[c].nullable}
            if nullable and _NULLABLE_ON_PURPOSE.get((table.name, name)) != nullable:
                offenders[(table.name, name)] = sorted(nullable)

    assert not offenders, (
        "这些唯一键里有可空列 —— PG 下 NULL 不参与唯一性比较，约束对那些行"
        "等于不存在。想清楚 NULL 在这里是「不适用」（那就登记到 "
        "_NULLABLE_ON_PURPOSE，写下理由）还是「没填」（那就像 "
        "uq_model_backend_connection 一样用 coalesce 表达式索引）：\n  "
        + "\n  ".join(f"{t}.{n}: {cols}" for (t, n), cols in sorted(offenders.items()))
    )
