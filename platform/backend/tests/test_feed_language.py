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
    # 是学科的英文名，不是一串编号 —— 英文界面的搜索框要搜得到「Philosophy」。
    assert {"Philosophy", "Computer Science and Technology"} <= set(labels)
    assert not any(label.startswith("CAS ") for label in labels), "英文界面上还是编号"


def test_every_field_in_the_catalog_has_an_english_name() -> None:
    """目录是一份打包的数据文件：每一格都要有英文名，漏一格英文界面上就冒出一个空名字。"""
    import json
    from importlib.resources import files

    catalog = json.loads(files("app").joinpath("data/discipline_catalog.json").read_text(encoding="utf-8"))
    entries = [*catalog, *(category for group in catalog for category in group["categories"])]
    missing = [entry.get("archive") or entry.get("domain") for entry in entries
               if not str(entry.get("label_en") or "").strip()
               or any("一" <= ch <= "鿿" for ch in entry["label_en"])]
    assert not missing, f"这些学科没有英文名：{missing[:10]}"


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


def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


async def _papers_in_condensed_matter(db) -> list[str]:
    """进得了今日选摘的论文：期刊路线采到、有摘要、刚发表、属于凝聚态。"""
    from tests._clock import just_now

    ids = [f"eeee0002-0000-4000-8000-00000000000{n}" for n in range(1, 5)]
    db.add_all([
        FeedItem(id=item_id, canonical_key=f"arxiv:2402.0000{n}", kind="paper",
                 title=f"Spin liquid study {n}", summary="We study a frustrated magnet.",
                 url=f"https://arxiv.org/abs/2402.0000{n}", published_at=just_now(),
                 venue="Physical Review B", authors=[], domains=["cond-mat"],
                 extra={"feed_acquisition_routes": ["journal"]}, visibility=FeedVisibility.PLATFORM)
        for n, item_id in enumerate(ids, start=1)
    ])
    await db.flush()
    return ids


@pytest.mark.asyncio
async def test_a_daily_pick_is_said_in_the_language_of_the_screen_that_reads_it(
    lang_client, harness_domain_registry
) -> None:
    """今日选摘存一整天，存的是"为什么"的事实，不是一句写好的中文。

    同一行选摘，中文界面读到中文理由，换成英文再读就是英文 —— 判据落在返回的字上。
    来源筛选也不再从理由的措辞里猜：每张卡都带着它属于哪几路。
    """
    from sqlalchemy import select

    from app.models.feed import FeedDailyPick

    client, db, user = lang_client
    await _papers_in_condensed_matter(db)
    assert (await client.put("/api/v1/feed/interests",
                             json={"domains": ["cond-mat"]})).status_code == 200

    await _set_language(db, user, "zh")
    zh = (await client.get("/api/v1/feed/today", params={"ready_only": "false"})).json()["feed"]
    zh_picks = {card["item"]["id"]: card["reason"] for card in zh if card["is_today_pick"]}
    assert zh_picks, "没有今日选摘，这条测试什么也没验"
    assert all(reason.startswith("属于你订阅的学科") for reason in zh_picks.values()), zh_picks
    stored = (await db.execute(select(FeedDailyPick.picks))).scalar_one()
    assert all("why" in pick and "reason" not in pick for pick in stored), stored

    await _set_language(db, user, "en")
    en = (await client.get("/api/v1/feed/today", params={"ready_only": "false"})).json()["feed"]
    en_picks = {card["item"]["id"]: card["reason"] for card in en if card["is_today_pick"]}
    assert set(en_picks) == set(zh_picks), "换语言不该换掉今天的选摘"
    assert all(reason.startswith("In fields you follow") and not _has_chinese(reason)
               for reason in en_picks.values()), en_picks
    assert all(card["recommendation_sources"] for card in en), "有卡片没说自己属于哪一路"


@pytest.mark.asyncio
async def test_the_screen_language_wins_while_the_saved_preference_lags(
    lang_client, harness_domain_registry
) -> None:
    """换语言时前端立刻带着新语言（X-Interface-Language）重取，偏好那次保存可能还没落地。

    11-04 实测：切到英文后第一次拉到的推荐，学科名和推荐理由还是中文 —— 池按存下的偏好建，
    要手点「刷新推荐」才对。现在按这一屏的语言建，两种语言的池各存各的。
    """
    client, db, user = lang_client
    await _papers_in_condensed_matter(db)
    assert (await client.put("/api/v1/feed/interests",
                             json={"domains": ["cond-mat"]})).status_code == 200
    await _set_language(db, user, "zh")   # 存下的偏好还是中文

    async def cards(screen: str) -> list[dict]:
        response = await client.get("/api/v1/feed/today", params={"ready_only": "false"},
                                    headers={"X-Interface-Language": screen})
        assert response.status_code == 200, response.text
        return response.json()["feed"]

    zh = await cards("zh")
    assert zh and all(_has_chinese(card["reason"]) for card in zh), [c["reason"] for c in zh]
    en = await cards("en")
    assert en, "英文这一屏一张卡都没拿到 —— 这条测试什么也没验"
    assert not any(_has_chinese(card["reason"]) for card in en), [c["reason"] for c in en]
    assert not any(_has_chinese(label) for card in en for label in card["item"]["domain_labels"]), \
        [c["item"]["domain_labels"] for c in en]
    # 再切回来，拿到的不是英文那份缓存。
    assert all(_has_chinese(card["reason"]) for card in await cards("zh"))


@pytest.mark.asyncio
async def test_a_pick_stored_as_a_written_reason_is_said_again(
    lang_client, harness_domain_registry
) -> None:
    """更早的版本存的是一句中文 `reason`。升级当天读到这样的行，按今天的打分重说。"""
    from datetime import UTC, datetime

    from app.models.feed import FeedDailyPick

    client, db, user = lang_client
    item_id = (await _papers_in_condensed_matter(db))[0]
    assert (await client.put("/api/v1/feed/interests",
                             json={"domains": ["cond-mat"]})).status_code == 200
    db.add(FeedDailyPick(user_id=_ME, pick_date=datetime.now(UTC).date(), picks=[
        {"item_id": item_id, "reason": "属于你关注的 凝聚态物理", "project_id": None}]))
    await _set_language(db, user, "en")

    feed = (await client.get("/api/v1/feed/today", params={"ready_only": "false"})).json()["feed"]
    pick = next(card for card in feed if card["is_today_pick"])
    assert pick["item"]["id"] == item_id
    assert pick["reason"] == "In fields you follow: Condensed Matter"
    assert pick["recommendation_sources"] == ["domain"]
