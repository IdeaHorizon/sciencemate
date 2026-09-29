"""订阅要真的喂进推荐 —— 不只是订阅页上的一个筛子。

## 这个文件盯的那次失败

改版前，推荐只有三条路：课题（project_profile）、学科（journal）、网络（web）。
用户**手动订阅**的期刊和学者只能影响「订阅」那个页签，推荐页看不见 —— 于是
"关注了却推不出来"，用户唯一的结论是订阅没用。

现在推荐 = project + 学科 + 期刊订阅 + 学者订阅。下面锁住四件事：

1. 订阅的刊/人，即使不属于任何订阅学科，也要进推荐（跨学科订阅才成立）；
2. 订阅本身算个性画像（`has_signal`），否则整个推荐页会显示成"还没被个性化"；
3. 准入在**送达时**现算 —— 取消订阅后同一批历史记录立刻退出，不需要回滚采集；
4. 没订阅的刊/人不能靠"碰巧同名"混进来。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.feed import FeedItem, FeedVisibility
from app.models.user import User
from app.services.feed.profile import (
    Profile,
    journal_key,
    stored_subscription_journals,
    stored_subscription_scholars,
)
from app.services.feed.ranking import (
    matches_subscribed_journal,
    matches_subscribed_scholar,
    rank,
    rank_rednote,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _item(
    title: str,
    *,
    venue: str = "",
    authors: list[str] | None = None,
    domains=(),
    extra=None,
) -> FeedItem:
    return FeedItem(
        id=f"item-{abs(hash(title)) % 10**9}",
        canonical_key=f"key-{abs(hash(title)) % 10**9}",
        kind="paper",
        title=title,
        url="https://example.org/x",
        summary="A summary so the item is eligible for the feed.",
        authors=authors or [],
        venue=venue or None,
        domains=list(domains),
        published_at=NOW - timedelta(days=1),
        extra=extra or {},
    )


def _profile(*, domains=(), journals=(), scholars=()) -> Profile:
    return Profile(
        user_id="u1",
        domains=domains,
        journals=frozenset(journals),
        scholars=frozenset(scholars),
    )


def test_a_subscribed_journal_crosses_the_subject_boundary() -> None:
    """订了那本刊，就不要求它属于订阅学科 —— 否则跨学科订阅等于没订。"""
    profile = _profile(domains=("cond-mat.mtrl-sci",), journals=("Nature Machine Intelligence",))
    subscribed = _item(
        "A foundation model for materials discovery",
        venue="Nature Machine Intelligence",
        domains=("cs.LG",),  # 不属于用户订阅的学科
        extra={"feed_acquisition_routes": ["journal"]},
    )
    other = _item(
        "Another paper outside every subscription",
        venue="Astronomy & Astrophysics",
        domains=("astro-ph.GA",),
        extra={"feed_acquisition_routes": ["journal"]},
    )

    ranked = rank_rednote([subscribed, other], profile, user_id="u1", now=NOW)

    assert [value.item.title for value in ranked] == [
        "A foundation model for materials discovery"
    ]


def test_a_subscribed_scholar_is_recommended_regardless_of_subject() -> None:
    """订阅学者是最无歧义的信号：他发了什么就推什么，不看学科。"""
    profile = _profile(scholars=("Yoshua Bengio",))
    by_him = _item(
        "Towards principled scientific discovery",
        authors=["Yoshua Bengio", "Someone Else"],
        domains=("q-bio.NC",),
        extra={"feed_acquisition_routes": ["scholar"], "profile_user_ids": ["u1"]},
    )
    # 没有认领任何路线的条目不该进推荐 —— 不是"有订阅就全放进来"。
    unrouted = _item("Unrelated work with no acquisition route", authors=["Other Person"])

    ranked = rank_rednote([by_him, unrouted], profile, user_id="u1", now=NOW)

    assert [value.item.title for value in ranked] == [
        "Towards principled scientific discovery"
    ]


def test_the_scholar_route_does_not_admit_a_same_name_stranger() -> None:
    """同名的人不算命中：准入看的是作者字段里有没有订阅的那个名字。"""
    profile = _profile(scholars=("Yoshua Bengio",))
    stranger = _item(
        "A different researcher's paper",
        authors=["J. Bengio"],
        extra={"feed_acquisition_routes": ["scholar"], "profile_user_ids": ["u1"]},
    )

    assert rank_rednote([stranger], profile, user_id="u1", now=NOW) == []


def test_cancelling_a_subscription_drops_the_old_items_immediately() -> None:
    """准入是**送达时**现算的：取消订阅，同一批历史记录当场退出。

    采集时不写死"这是你的订阅"标记，正是为了这一步 —— 否则取消之后旧标记
    还在，用户会继续看到那本刊，而且没有任何办法清掉。
    """
    paper = _item(
        "A journal paper",
        venue="Nature Machine Intelligence",
        extra={"feed_acquisition_routes": ["journal"]},
    )
    following = _profile(journals=("nature machine intelligence",))
    dropped = _profile(domains=("astro-ph.GA",))

    assert [v.item.title for v in rank_rednote([paper], following, user_id="u1", now=NOW)] == [
        "A journal paper"
    ]
    assert rank_rednote([paper], dropped, user_id="u1", now=NOW) == []


def test_subscriptions_alone_make_the_feed_personalized() -> None:
    """只有订阅（没有学科、没有课题）也是个性画像 —— 否则页面显示"还没被个性化"，
    而用户明明刚关注了一本刊。"""
    assert _profile(journals=("Nature",)).has_signal is True
    assert _profile(scholars=("Yoshua Bengio",)).has_signal is True
    assert _profile().has_signal is False


def test_journal_matching_normalizes_word_order_and_punctuation() -> None:
    """刊名比对必须忽略冠词、标点与大小写，否则"订阅了却匹配不上"。"""
    assert matches_subscribed_journal(
        _item("x", venue="Nature Machine Intelligence"),
        _profile(journals=("nature machine intelligence",)),
    )
    assert matches_subscribed_journal(
        _item("x", venue="The Lancet"),
        _profile(journals=(journal_key("The Lancet"),)),
    )
    assert matches_subscribed_scholar(
        _item("x", authors=["Yoshua Bengio"]),
        _profile(scholars=("yoshua bengio",)),
    )


def test_stored_subscriptions_are_read_from_user_preferences() -> None:
    """画像里的订阅直接来自 `preferences["feed"]["subscriptions"]`，不做第二份存储。"""
    user = User(
        id=uuid4(),
        email="subscriber@example.org",
        preferences={
            "feed": {
                "subscriptions": {
                    "journals": [{"key": "j1", "name": "Nature Machine Intelligence"}],
                    "scholars": [{"key": "s1", "name": "Yoshua Bengio"}],
                }
            }
        },
    )

    assert stored_subscription_journals(user) == frozenset({"naturemachineintelligence"})
    assert stored_subscription_scholars(user) == frozenset({"yoshua bengio"})


def test_a_user_without_subscriptions_reads_nothing() -> None:
    user = User(id=uuid4(), email="plain@example.org", preferences={})

    assert stored_subscription_journals(user) == frozenset()
    assert stored_subscription_scholars(user) == frozenset()


def test_no_card_ever_shows_the_meaningless_fallback_reason() -> None:
    """「本领域近期动态」不许再出现（用户明确要求）。

    那条兜底文案对用户零信息：订阅的刊、订阅的人、课题、学科一个都没命中时，
    卡片上不写"为什么推给我"，用户只能自己猜。改法是每一句理由都指回一件
    可核对的事 —— 指不回用户做过的事，就说这条从哪儿来（期刊名/站点/来源）。
    """
    cases = [
        _item("A journal paper", venue="Water Research", extra={"feed_acquisition_routes": ["journal"]}),
        _item("Science news", venue="Quanta Magazine", extra={"feed_acquisition_routes": ["bignews"]}),
        _item("A web find", venue="phys.org", extra={"feed_acquisition_routes": ["web"]}),
        _item("A project paper", extra={"feed_acquisition_routes": ["project_profile"]}),
        _item("An orphan item with no route and no venue"),
    ]
    for item in cases:
        reason = rank([item], _profile(), now=NOW)[0].reason()
        assert "本领域近期动态" not in reason
        assert reason.strip(), "理由不能为空"


def test_a_subscription_hit_names_the_subscription_it_matched() -> None:
    """订阅命中要说出是哪本刊/哪位学者 —— 这正是"为什么推给我"的答案。"""
    journal_hit = _item(
        "A paper in the followed journal",
        venue="Water Research",
        extra={"feed_acquisition_routes": ["journal"]},
    )
    journal_reason = rank(
        [journal_hit], _profile(journals=("Water Research",)), now=NOW
    )[0].reason()
    assert "Water Research" in journal_reason

    scholar_hit = _item(
        "A paper by the followed scholar",
        authors=["Yoshua Bengio"],
        extra={"feed_acquisition_routes": ["scholar"]},
    )
    scholar_reason = rank(
        [scholar_hit], _profile(scholars=("Yoshua Bengio",)), now=NOW
    )[0].reason()
    assert "Yoshua Bengio" in scholar_reason


async def test_subscriptions_do_not_remove_the_domain_boundary(db_session) -> None:
    """有订阅时，学科边界必须**照旧生效**。

    这条锁的是一次真实回归：为了"让订阅内容进得来"，一度把学科预过滤整个撤掉
    （有订阅就不收窄候选池）。结果是候选池变成全平台最新条目，公共期刊动态把
    课题命中挤出推荐流 —— 用户看到的症状是"我的课题怎么不见了"，而订阅内容
    进不进来反而是次要的。

    正确的做法是加一条**只认订阅命中**的补充扫描（见 candidate_items），
    学科边界不动。
    """
    from app.services.feed.ranking import candidate_items

    now = datetime.now(UTC)

    def _row(item_id: str, title: str, *, domains, venue: str = "", days: int = 1) -> FeedItem:
        return FeedItem(
            id=item_id,
            canonical_key=f"key:{item_id}",
            kind="paper",
            title=title,
            url="https://example.org/x",
            summary="A summary so the item is feed-eligible.",
            authors=[],
            venue=venue or None,
            domains=list(domains),
            published_at=now - timedelta(days=days),
            extra={},
            visibility=FeedVisibility.PLATFORM,
        )

    subscribed_journal = _row(
        "aaaaaaaa-0000-4000-8000-00000000000a",
        "A paper in the journal the user follows",
        domains=["999999"],  # 不属于用户订阅的二级学科
        venue="Nature Machine Intelligence",
    )
    other_domain = _row(
        "aaaaaaaa-0000-4000-8000-00000000000b",
        "A paper in a subject the user never chose",
        domains=["999999"],
        venue="Astronomy & Astrophysics",
    )
    chosen_domain = _row(
        "aaaaaaaa-0000-4000-8000-00000000000c",
        "A paper in the subject the user follows",
        domains=["070101"],
        venue="Some Journal",
    )
    db_session.add_all([subscribed_journal, other_domain, chosen_domain])
    await db_session.flush()

    profile = _profile(domains=("070101",), journals=("nature machine intelligence",))
    titles = {item.title for item in await candidate_items(db_session, profile=profile)}

    assert "A paper in the subject the user follows" in titles
    assert "A paper in the journal the user follows" in titles
    # 学科边界照旧：既不是订阅学科、也不是订阅期刊的内容不许进候选池。
    assert "A paper in a subject the user never chose" not in titles
