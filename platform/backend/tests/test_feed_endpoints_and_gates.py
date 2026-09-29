"""资讯流接口的四道闸：出处、幂等、可见性、SSRF。

这四条里有三条是**真跑一次才现形**的（见各自的 docstring）—— 新库里没有重复，
单用户环境里看不出越权，而 SSRF 只有真发一次请求才知道拦没拦住。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.feed import FeedEngagement, FeedItem, FeedSource, FeedVisibility
from app.models.user import User
from app.services.feed.collectors import CollectedItem
from app.services.feed.ingest import ingest_items

_ME ="aaaa1111-1111-4111-8111-111111111111"
_COLLEAGUE = "bbbb2222-2222-4222-8222-222222222222"
_OUTSIDER = "cccc3333-3333-4333-8333-333333333333"


@pytest_asyncio.fixture
async def feed_client(db_session):
    """我 + 一个同组同事 + 一个外人，外加几条内容。"""
    me = User(id=_ME, email="me@lab.test", display_name="Me", hashed_password="x",
              institution_id="inst-a", group_id="group-a")
    colleague = User(id=_COLLEAGUE, email="colleague@lab.test", display_name="Colleague",
                     hashed_password="x", institution_id="inst-a", group_id="group-a")
    outsider = User(id=_OUTSIDER, email="outsider@other.test", display_name="Outsider",
                    hashed_password="x", institution_id="inst-b", group_id="group-b")
    db_session.add_all([me, colleague, outsider])
    db_session.add_all([
        FeedItem(id="aaaaaaaa-0000-4000-8000-000000000001", canonical_key="arxiv:2401.00001",
                 kind="paper", title="A public paper", url="https://arxiv.org/abs/2401.00001",
                 authors=[], domains=[], extra={}, visibility=FeedVisibility.PLATFORM),
        FeedItem(id="aaaaaaaa-0000-4000-8000-000000000002", canonical_key="url:https://lab.test/a",
                 kind="post", title="An internal note", url="https://lab.test/a",
                 authors=[], domains=[], extra={}, visibility=FeedVisibility.ORGANIZATION,
                 organization_id="group-a", author_user_id=_COLLEAGUE),
    ])
    await db_session.flush()

    current = {"user": me}

    async def override_get_db():
        yield db_session

    async def override_current_user() -> User:
        return current["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feed") as client:
            yield client, db_session, current, {"me": me, "colleague": colleague,
                                                "outsider": outsider}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_org_content_reaches_colleagues_and_stops_at_the_boundary(feed_client) -> None:
    """受众是"这个人和谁在一起"，不是"这个人管得着谁"。

    第一版用 `governance_scope_for` 当受众键。那个函数按角色分叉，普通研究员
    的 scope id 就是他自己的 user_id —— 于是研究员发的"组织可见"内容只有他
    自己看得到：一个看起来在工作、实际上谁也送不到的分享按钮。
    """
    client, _db, current, users = feed_client

    titles = {i["title"] for i in (await client.get("/api/v1/feed/items")).json()}
    assert {"A public paper", "An internal note"} <= titles

    current["user"] = users["colleague"]
    titles = {i["title"] for i in (await client.get("/api/v1/feed/items")).json()}
    assert "An internal note" in titles, "同组同事必须看得到"

    current["user"] = users["outsider"]
    titles = {i["title"] for i in (await client.get("/api/v1/feed/items")).json()}
    assert "A public paper" in titles
    assert "An internal note" not in titles, "跨组织泄露"


@pytest.mark.asyncio
async def test_replaying_the_same_engagement_is_idempotent_not_a_500(feed_client) -> None:
    """真跑一次才现形：新库里没有重复，所以第二次点"收藏"是唯一能触发它的路径。

    `db.add()` 放在 savepoint **外面**时，撞唯一键后 savepoint 回滚，但待插入
    对象仍留在 session 里，请求收尾那次 commit 会把它再插一遍 —— 抛
    PendingRollbackError，一个幂等重放变成 500。
    """
    client, db, _current, _users = feed_client
    item_id = "aaaaaaaa-0000-4000-8000-000000000001"

    for _ in range(3):
        response = await client.post(
            f"/api/v1/feed/items/{item_id}/engagement", json={"action": "save"}
        )
        assert response.status_code == 204, response.text

    rows = (await db.execute(
        select(FeedEngagement).where(FeedEngagement.item_id == item_id)
    )).scalars().all()
    assert len(rows) == 1

    detail = (await client.get(f"/api/v1/feed/items/{item_id}")).json()
    assert detail["saved"] is True

    # 撤销必须真的撤销 —— 一个点错了就永久消失的按钮，用户下次就不敢点了。
    assert (await client.delete(
        f"/api/v1/feed/items/{item_id}/engagement/save"
    )).status_code == 204
    assert (await client.get(f"/api/v1/feed/items/{item_id}")).json()["saved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_url",
    [
        "http://127.0.0.1:18081/api/v1/feed/status",   # 平台自己的后端
        "http://169.254.169.254/latest/meta-data/",    # 云厂商元数据服务
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.5/internal",
        "file:///etc/passwd",
    ],
)
async def test_a_shared_link_can_never_reach_the_private_network(
    feed_client, hostile_url: str
) -> None:
    """投稿链接是全平台**唯一**"用户给地址、服务器就去访问"的地方。

    不设防的话，任何一个登录用户都能让 App Server 去打云元数据服务或内网任意
    一台机器，而返回内容会被当成"卡片摘要"显示给他看。
    """
    client, _db, _current, _users = feed_client
    response = await client.post("/api/v1/feed/shares", json={"url": hostile_url})
    assert response.status_code == 422, f"{hostile_url} 没有被拦住：{response.text[:200]}"


@pytest.mark.asyncio
async def test_interests_persist_and_an_empty_choice_still_counts_as_answered(
    feed_client, harness_domain_registry
) -> None:
    """"什么都不订，只看全站" 和 "我从没被问过" 不是同一件事。

    把前者当后者，用户每次进来都会再弹一次问卷。
    """
    client, _db, _current, _users = feed_client

    assert (await client.get("/api/v1/feed/interests")).json()["onboarded"] is False

    bad = await client.put("/api/v1/feed/interests", json={"domains": ["not-a-real-domain"]})
    assert bad.status_code == 422
    assert "not-a-real-domain" in bad.json()["detail"]

    made_up_numeric = await client.put(
        "/api/v1/feed/interests", json={"domains": ["999999"]}
    )
    assert made_up_numeric.status_code == 422

    cas = await client.put("/api/v1/feed/interests", json={"domains": ["070207"]})
    assert cas.status_code == 200
    assert cas.json()["domain_labels"] == ["光学"]

    saved = await client.put("/api/v1/feed/interests",
                             json={"domains": ["cond-mat", "cs.LG"]})
    assert saved.status_code == 200
    assert saved.json()["domains"] == ["cond-mat", "cs.LG"]
    # 人读名来自词表，不是前端自己拼的；资讯流用中文名（见 domains.py）。
    assert saved.json()["domain_labels"] == ["凝聚态物理", "机器学习"]

    empty = await client.put("/api/v1/feed/interests", json={"domains": []})
    assert empty.status_code == 200
    assert empty.json()["onboarded"] is True
    assert (await client.get("/api/v1/feed/interests")).json()["onboarded"] is True


@pytest.mark.asyncio
async def test_today_says_so_when_it_has_nothing_personal_to_offer(feed_client) -> None:
    """没有个性化依据时不假装这是为你挑的 —— 界面据此说实话。"""
    client, _db, _current, _users = feed_client
    body = (await client.get("/api/v1/feed/today")).json()
    assert body["personalized"] is False
    assert body["onboarded"] is False


@pytest.mark.asyncio
async def test_an_item_without_a_source_url_never_enters_the_pool(db_session) -> None:
    """provenance-first：没有出处的条目一律不落库。

    平台上出现一条没有出处的"新闻"，就是在自己身上开一个幻觉传播面。
    """
    source = FeedSource(kind="rss", name="test", config={"url": "https://x"}, domains=[])
    db_session.add(source)
    await db_session.flush()

    report = await ingest_items(db_session, source=source, items=[
        CollectedItem(canonical_key="url:https://a", kind="paper", title="有出处",
                      url="https://a"),
        CollectedItem(canonical_key="url:https://b", kind="paper", title="没出处", url=""),
        CollectedItem(canonical_key="", kind="paper", title="算不出身份",
                      url="https://c"),
    ])
    assert report.stored == 1
    assert report.skipped_no_url == 1
    assert report.skipped_no_key == 1

    titles = {t for t in (await db_session.execute(select(FeedItem.title))).scalars()}
    assert titles == {"有出处"}


@pytest.mark.asyncio
async def test_one_duplicate_does_not_roll_back_the_whole_batch(db_session) -> None:
    """一轮采集里撞到一条重复是常态（源会重发、更正）。

    `db.add` 在 savepoint 外面时，一条撞键会把整批一起废掉 —— 症状是这个源
    从此一条新内容都进不来，而日志只说"stored: 0"。
    """
    source = FeedSource(kind="rss", name="dup", config={"url": "https://x"}, domains=[])
    db_session.add(source)
    await db_session.flush()

    first = await ingest_items(db_session, source=source, items=[
        CollectedItem(canonical_key="url:https://a", kind="paper", title="已有", url="https://a"),
    ])
    assert first.stored == 1

    second = await ingest_items(db_session, source=source, items=[
        CollectedItem(canonical_key="url:https://a", kind="paper", title="又来一次",
                      url="https://a"),
        CollectedItem(canonical_key="url:https://d", kind="paper", title="真的新", url="https://d"),
    ])
    assert second.duplicates == 1
    assert second.stored == 1, "一条重复不该带走同批的新内容"

    titles = {t for t in (await db_session.execute(select(FeedItem.title))).scalars()}
    assert titles == {"已有", "真的新"}


@pytest.mark.asyncio
async def test_an_edition_that_never_collects_does_not_promise_a_first_round(
    feed_client, monkeypatch
) -> None:
    """空资讯流不许承诺一件这台机器不会做的事。

    个人档从不起后台采集（`assembly.background_collection_enabled()` 只在 org
    档为真），可空态从前读的是另外两个开关 —— 而它们默认都是 True 且个人档
    不覆盖。于是一台永远不采集的机器上写着「第一轮采集通常在服务启动后几分钟
    内完成」：等多久都不会有内容，而界面一直在暗示再等等。

    判据落在**那句话上**，不落在"分支走到了哪"：说错话正是这个缺陷本身。
    """
    from app import assembly
    from app.services.feed.copy import COPY

    client, db, _current, _users = feed_client

    # 池子要真的是空的 —— `empty_reason` 只在没有候选时才有话说，而这条测试
    # 问的正是"没有内容时它说什么"。夹具默认铺了两条。
    await db.execute(delete(FeedItem))
    await db.flush()

    monkeypatch.setattr(assembly, "background_collection_enabled", lambda: False)
    body = (await client.get("/api/v1/feed/today")).json()
    assert body["feed"] == []
    reason = body["empty_reason"]
    assert reason == COPY["empty.no_collector_here"]["zh"]
    assert "几分钟" not in reason, "又在一台不采集的机器上承诺采集时间了"

    # 会采集的那一档照旧：这条修复不许把 org 档的话也改掉。
    monkeypatch.setattr(assembly, "background_collection_enabled", lambda: True)
    org_reason = (await client.get("/api/v1/feed/today")).json()["empty_reason"]
    assert org_reason == COPY["empty.first_round"]["zh"]


@pytest.mark.asyncio
async def test_journal_and_scholar_subscriptions_persist_and_drive_their_own_feed(feed_client) -> None:
    client, db, _current, _users = feed_client
    item = await db.get(FeedItem, "aaaaaaaa-0000-4000-8000-000000000001")
    item.venue = "Nature"
    item.authors = ["Ada Lovelace", "Grace Hopper"]
    item.summary = "A verifiable abstract for the subscribed paper."
    await db.flush()

    payload = {
        "journals": [{
            "key": "0028-0836", "name": "Nature", "issn": "0028-0836",
            "eissn": "", "impact_factor": 50.5, "jcr_quartile": "Q1",
        }],
        "scholars": [{
            "key": "author:ada lovelace", "name": "Ada Lovelace",
            "source": "literature",
        }],
    }
    saved = await client.put("/api/v1/feed/subscriptions", json=payload)
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/v1/feed/subscriptions")).json() == payload

    cards = (await client.get("/api/v1/feed/subscriptions/items")).json()
    assert [card["item"]["title"] for card in cards] == ["A public paper"]
    assert cards[0]["reason"].startswith("订阅期刊：")
