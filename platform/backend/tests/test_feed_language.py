"""界面语言真的会改变资讯流返回的字 —— 不是只改了设置页上那一格。

一个"存得进、读得出、但没人按它做事"的设置，是这个仓库反复栽过的形状：
字段活着、测试全绿、用户切了没反应。所以判据落在**输出的字**上。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.feed import FeedItem, FeedVisibility
from app.models.user import User
from app.services.feed.copy import COPY

_ME = "eeee1111-1111-4111-8111-111111111111"


@pytest_asyncio.fixture
async def lang_client(db_session):
    user = User(id=_ME, email="me@lab.test", display_name="Me", hashed_password="x",
                institution_id="inst-a", group_id="group-a")
    db_session.add(user)
    # 六条：Top 3 选摘拿走三条，剩下的才落进分区 —— 只放一条的话分区永远
    # 是空的，而"分区标题跟着语言变"这条测试会绿着什么也没验。
    db_session.add_all([
        FeedItem(
            id=f"eeee0001-0000-4000-8000-00000000000{n}",
            canonical_key=f"arxiv:2401.0000{n}", kind="paper", title=f"Some paper {n}",
            url=f"https://arxiv.org/abs/2401.0000{n}",
            authors=[], domains=["cond-mat", "cs.LG"], extra={},
            visibility=FeedVisibility.PLATFORM,
        )
        for n in range(1, 7)
    ])
    await db_session.flush()

    async def override_get_db():
        yield db_session

    async def override_current_user() -> User:
        return await db_session.get(User, _ME)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feed") as c:
            yield c, db_session, user
    finally:
        app.dependency_overrides.clear()


async def _set_language(db, user: User, language: str) -> None:
    preferences = dict(user.preferences or {})
    preferences["interface"] = {**(preferences.get("interface") or {}), "language": language}
    user.preferences = preferences
    await db.flush()


@pytest.mark.asyncio
async def test_switching_the_language_changes_the_domain_labels(
    lang_client, harness_domain_registry
) -> None:
    """域名的人读名跟着语言走 —— 中英两套都在 harness 的词表里，平台不存第二份。"""
    client, db, user = lang_client

    await _set_language(db, user, "zh")
    zh = (await client.get("/api/v1/feed/items")).json()[0]["domain_labels"]
    assert zh == ["凝聚态物理", "机器学习"]

    await _set_language(db, user, "en")
    en = (await client.get("/api/v1/feed/items")).json()[0]["domain_labels"]
    assert en == ["Condensed Matter", "Machine Learning"]


@pytest.mark.asyncio
async def test_the_field_catalog_follows_the_language(
    lang_client, harness_domain_registry
) -> None:
    """挑方向的目录也跟着走 —— 否则选择界面是中文、卡片上是英文。"""
    client, db, user = lang_client

    await _set_language(db, user, "en")
    groups = (await client.get("/api/v1/feed/domains")).json()["groups"]
    labels = [g["label"] for g in groups] + \
        [c["label"] for g in groups for c in g["categories"]]
    assert labels, "目录是空的，这条测试什么也没验"
    assert not any(any("一" <= ch <= "鿿" for ch in text) for text in labels), \
        "英文目录里混着中文名"


def test_every_string_exists_in_both_languages() -> None:
    """加一句中文却忘了英文，症状是英文界面里突然冒出一句中文。

    扫的是**整张表**，不是几个具体的 key —— 写名单的话，下一个人加的那句
    默认漏过（这个仓库的既有纪律：护栏要扫盘，不要写名单）。
    """
    missing = {key: sorted({"zh", "en"} - set(value)) for key, value in COPY.items()
               if set(value) != {"zh", "en"}}
    assert not missing, f"这些文案缺语言：{missing}"


@pytest.mark.asyncio
async def test_feed_today_returns_a_single_feed_stream(lang_client) -> None:
    """单流契约：/feed/today 返回 `feed`（一条流），不再有 picks/sections 分区。"""
    client, db, user = lang_client

    payload = (await client.get("/api/v1/feed/today")).json()

    assert "feed" in payload, f"缺 feed 字段：{sorted(payload)}"
    assert isinstance(payload["feed"], list)
    assert "picks" not in payload, "旧的 picks 分区没清掉"
    assert "sections" not in payload, "旧的 sections 分区没清掉"
    # 每张卡：内容 + 可验证理由 + 是否今日必读。
    for card in payload["feed"]:
        assert "item" in card and "reason" in card and "is_today_pick" in card
