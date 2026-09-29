"""排序回答的是"这条会不会影响你手头研究的下一步"，而且理由要能被当场验证。

## 这个文件盯的那次失败

第一版按**重叠术语个数**给课题命中打分。真跑一轮 527 条之后，推给"MLIP 鲁棒性"
课题的头条是一篇卧室雷达论文，理由写着「和你的课题对得上：neural、network」——
那是候选池里每一篇机器学习论文都有的词。用户扫一眼就知道这不算命中。

修法不是往停用词表里加 learning/machine/network（"learning" 对一个做教育心理学
的课题是核心词，区分度是**语料的性质**不是词的性质，而且名单会过时），而是按
候选池现算罕见度。下面锁的就是这条。
"""
from __future__ import annotations

import pytest
from datetime import UTC, datetime, timedelta

from app.models.feed import FeedEngagement, FeedEngagementAction, FeedItem
from app.services.feed.profile import Profile, ProjectSignal, extract_terms
from app.services.feed.ranking import (
    TermDiscrimination,
    apply_behavior_preferences,
    choose_daily_picks,
    rank,
    rank_rednote,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _item(title: str, *, summary: str = "", days_old: int = 0, domains=(), extra=None,
          venue: str = "") -> FeedItem:
    return FeedItem(
        id=f"item-{abs(hash(title)) % 10**9}",
        canonical_key=f"key-{abs(hash(title)) % 10**9}",
        kind="paper",
        title=title,
        url="https://example.org/x",
        summary=summary,
        authors=[],
        venue=venue or None,
        domains=list(domains),
        published_at=NOW - timedelta(days=days_old),
        extra=extra or {},
    )


def _profile(*projects: tuple[str, str], domains: tuple[str, ...] = ()) -> Profile:
    return Profile(
        user_id="u1",
        domains=domains,
        projects=tuple(
            ProjectSignal(project_id=f"p{i}", name=name, terms=extract_terms(text))
            for i, (name, text) in enumerate(projects)
        ),
    )


def test_a_generic_word_overlap_is_not_a_match() -> None:
    """两个到处都是的词凑在一起不构成关联。

    构造：候选池里几乎每条都带 neural/network，只有一条带 interatomic/mlip。
    """
    profile = _profile(
        ("MLIP 鲁棒性", "machine learning interatomic potentials mlip neural network robustness")
    )
    filler = [
        _item(f"Neural network study {i}", summary="neural network learning machine")
        for i in range(30)
    ]
    real = _item(
        "Universal machine-learning molecular dynamics",
        summary="interatomic potentials mlip neural network",
    )
    scored = rank([*filler, real], profile, now=NOW)
    top = scored[0]

    assert top.item.title == real.title, [s.item.title for s in scored[:3]]
    # 理由必须点名有区分度的词，而不是 neural/network。
    assert {"interatomic", "mlip"} & set(top.matched_terms), top.matched_terms
    # 那 30 条泛词条目一条都不该被说成"和你的课题对得上"。
    generic = [s for s in scored if s.item.title.startswith("Neural network study")]
    assert all(s.project_id is None for s in generic), [
        (s.item.title, s.matched_terms) for s in generic if s.project_id
    ][:3]


def test_term_rarity_does_not_drift_with_corpus_size() -> None:
    """罕见度按 log(N) 归一，落在 0..1 —— 不归一的话阈值就得跟着候选池条数走，
    而池子大小随采集节奏变：同一批权重在 400 条里刚好，在 40 条里全不达标。"""
    small = TermDiscrimination([frozenset({"rare"})] + [frozenset({"common"}) for _ in range(9)])
    large = TermDiscrimination([frozenset({"rare"})] + [frozenset({"common"}) for _ in range(399)])
    for scale in (small, large):
        assert 0.0 <= scale.weight("common") < 0.35
        assert 0.6 < scale.weight("rare") <= 1.0


def test_the_reason_names_the_most_distinctive_overlap_first() -> None:
    discrimination = TermDiscrimination(
        [frozenset({"ising", "monte", "carlo"})]
        + [frozenset({"monte", "carlo"}) for _ in range(50)]
    )
    picked = discrimination.most_distinctive(frozenset({"ising", "monte", "carlo"}), limit=2)
    assert picked[0] == "ising"


def test_recency_is_a_discount_not_a_tiebreak() -> None:
    """上周的"新论文"不是新闻。同样命中的两条，新的必须赢。"""
    profile = _profile(("Ising", "ising monte carlo critical exponents finite-size scaling"))
    fresh = _item("Ising critical exponents by Monte Carlo", days_old=0)
    stale = _item("Ising critical exponents via Monte Carlo methods", days_old=12)
    scored = rank([stale, fresh], profile, now=NOW)
    assert scored[0].item.title == fresh.title


def test_daily_picks_cover_different_projects(harness_domain_registry) -> None:
    """几个课题在跑，日报就该替他各看一眼 —— 三条全砸在同一个课题上，
    另外两个课题的动态他永远看不到。

    候选池里要有足够的无关内容，命中的那几条才谈得上"罕见"。这不是为了让
    测试通过而铺的料：真实的候选池就是几百条里绝大多数与你无关。
    """
    profile = _profile(
        ("Ising", "ising monte carlo critical exponents"),
        ("MLIP", "interatomic potentials mlip"),
    )
    filler = [_item(f"Unrelated topic {i}", summary="cosmology galaxy survey") for i in range(40)]
    items = [
        *filler,
        _item("Ising critical exponents Monte Carlo A", summary="ising monte carlo exponents"),
        _item("Ising critical exponents Monte Carlo B", summary="ising monte carlo exponents"),
        _item("Interatomic potentials for MLIP", summary="interatomic potentials mlip"),
    ]
    scored = rank(items, profile, now=NOW)
    picks = choose_daily_picks(scored, count=3)
    assert len({p.project_id for p in picks if p.project_id}) == 2, [
        (p.item.title, p.project_name) for p in picks
    ]


def test_a_term_that_dominates_the_pool_stops_being_evidence(harness_domain_registry) -> None:
    """如果候选池里大多数内容都在讲同一件事，那件事就不再能说明什么。

    这是上面那条罕见度判据的**有意后果**，不是缺陷：跟一个做 Ising 的人说
    "这条和你的 Ising 课题对得上"，而今天池子里四分之三都是 Ising，
    这句话没有传递任何信息。

    副作用是一个刚部署、内容池很小的实例上，个性化会偏保守 —— 那是对的：
    内容不够时，与其编一个关联，不如按最新给他看。
    """
    profile = _profile(("Ising", "ising monte carlo critical exponents"))
    items = [
        _item(f"Ising study {i}", summary="ising monte carlo exponents") for i in range(3)
    ] + [_item("Something else", summary="cosmology")]
    scored = rank(items, profile, now=NOW)
    assert all(s.project_id is None for s in scored), [
        (s.item.title, s.matched_terms) for s in scored
    ]


def test_domain_subscription_flows_down_the_hierarchy_not_up(harness_domain_registry) -> None:
    """订了 `cond-mat` 的人收得到 `cond-mat.mtrl-sci` 的内容，反过来不成立 ——
    订阅是"这个范围我都要"，不是"只要这一格"。"""
    broad = _profile(domains=("cond-mat",))
    narrow = _profile(domains=("cond-mat.mtrl-sci",))
    specific = _item("A materials paper", domains=("cond-mat.mtrl-sci",))
    sibling = _item("A stat-mech paper", domains=("cond-mat.stat-mech",))

    by_title = {s.item.title: s for s in rank([specific, sibling], broad, now=NOW)}
    assert by_title["A materials paper"].matched_domains
    assert by_title["A stat-mech paper"].matched_domains

    by_title = {s.item.title: s for s in rank([specific, sibling], narrow, now=NOW)}
    assert by_title["A materials paper"].matched_domains
    assert not by_title["A stat-mech paper"].matched_domains


def test_selected_domain_is_a_hard_boundary_for_journal(harness_domain_registry) -> None:
    """手选学科是 journal 路线（本领域动态）的边界：期刊不命中订阅学科就不能
    混进学科动态，即便它碰巧命中某个 Project 或检索相关度很高。"""
    profile = _profile(domains=("cond-mat.mtrl-sci",))
    wrong = _item(
        "An unrelated astronomy paper",
        domains=("astro-ph.GA",),
        extra={"feed_acquisition_routes": ["journal"]},
    )
    right = _item(
        "A materials paper",
        domains=("cond-mat.mtrl-sci",),
        extra={"feed_acquisition_routes": ["journal"]},
    )

    ranked = rank_rednote([wrong, right], profile, user_id="u1", now=NOW)

    assert [item.item.title for item in ranked] == ["A materials paper"]


def test_project_profile_items_are_not_bound_by_the_selected_domain(
    harness_domain_registry,
) -> None:
    """project 关键词检索的内容独立于手选学科：project 是车辆工程、订阅是专门史
    时，project 捞到的动力学论文仍归「可能与你相关」，不被订阅学科边界过滤。

    这是「project 关键词策略只变与你相关、不变本领域动态」的排序侧收口 ——
    project_profile 论文不标学科 domains（见 discovery），靠 profile_user_ids
    进候选池、靠检索相关度进 for_you。"""
    profile = _profile(("车辆工程", "vehicle chassis steer-by-wire control"), domains=("cond-mat.mtrl-sci",))
    project_paper = _item(
        "Computational fluid dynamics modeling",
        summary="vehicle dynamics steer-by-wire chassis control",
        domains=(),  # project_profile 论文不标学科 domains
        extra={
            "feed_acquisition_routes": ["project_profile"],
            "profile_user_ids": ["u1"],
            "profile_query_relevance": 0.9,
        },
    )

    filler = [_item(f"Unrelated astronomy {i}") for i in range(20)]
    ranked = rank_rednote([project_paper, *filler], profile, user_id="u1", now=NOW)

    assert [item.item.title for item in ranked] == [
        "Computational fluid dynamics modeling"
    ]


def test_an_item_with_no_match_still_says_something_true() -> None:
    """说不出关联的条目不编课题关联，但**也不许只说一句泛话**。

    原来这里写死"本领域近期动态"。那句话对用户零信息（他不知道为什么被推），
    已被明确要求去掉。现在的契约是：指不回用户做过的事，就说这条从哪儿来
    —— 有刊名说刊名，有站点说站点，可核对、不承诺关联。
    """
    scored = rank([_item("Something unrelated entirely", venue="Water Research")],
                  _profile(("X", "quantum gravity")), now=NOW)
    assert scored[0].project_id is None
    reason = scored[0].reason()
    assert reason != "本领域近期动态"
    assert "Water Research" in reason


def test_old_discovery_score_cannot_bypass_current_project_match() -> None:
    profile = _profile(("车辆工程", "vehicle chassis modeling dynamics framework kinetic"))
    titles = [
        "Bridging tumor growth dynamics and survival in preclinical oncology drug development – a Tumor Growth Inhibition and Time-to-Event (TGI-TTE) modeling framework",
        "Time-resolved investigation of photocatalytic allene deracemization with kinetic modeling and force-field-based molecular dynamics",
    ]
    for route in ("project_profile", "web"):
        items = [_item(title, extra={
            "feed_acquisition_routes": [route],
            "profile_user_ids": ["u1"],
            "profile_query_relevance": 1.0,
        }) for title in titles]
        assert rank_rednote(items, profile, user_id="u1", now=NOW) == []


def test_naive_timestamps_from_sqlite_do_not_crash_scoring() -> None:
    """列是 `DateTime(timezone=True)`，但取回来是不是 aware **取决于驱动**：
    Postgres 给 aware，SQLite 给 naive。相减就是一个只在某些部署上炸的 500。"""
    item = _item("A paper")
    item.published_at = datetime(2026, 8, 21, 12, 0)  # naive，像 SQLite 那样
    scored = rank([item], _profile(), now=NOW)
    assert scored[0].score >= 0


@pytest.mark.asyncio
async def test_saved_topics_raise_similar_items_without_overriding_the_base_rank(db_session) -> None:
    history = FeedItem(
        id="aaaa0000-0000-4000-8000-000000000001", canonical_key="doi:history",
        kind="paper", title="Quantum photonic waveguide experiment",
        url="https://example.org/history", summary="quantum photons optical waveguide",
        authors=["A. Researcher"], domains=[], venue="Optics Letters", extra={},
    )
    db_session.add(history)
    db_session.add(FeedEngagement(
        user_id="aaaa1111-1111-4111-8111-111111111111", item_id=history.id,
        action=FeedEngagementAction.SAVE, created_at=NOW,
    ))
    await db_session.flush()
    related = _item("Photonic waveguide for quantum photons")
    unrelated = _item("Medieval manuscript catalog")
    base = rank([unrelated, related], _profile(), now=NOW)
    adjusted = await apply_behavior_preferences(
        db_session, user_id="aaaa1111-1111-4111-8111-111111111111", scored=base, now=NOW
    )
    assert adjusted[0].item.title == related.title
    old_score = next(value.score for value in base if value.item is related)
    assert adjusted[0].score - old_score <= 0.200001


async def test_an_item_without_a_publication_date_is_not_starved_out(db_session) -> None:
    """没写日期的条目不能被"有日期的条目"挤出候选池。

    排序曾经写成 `published_at DESC NULLS LAST`：无日期条目永远排最后，而候选池
    是有界的（`limit*3`）—— 库里有日期的条目一多，它们就整批消失，连被排序的
    机会都没有。实测症状：project 检索命中的中文网页条目（源没给日期）全部消失，
    推荐流里"和你的课题对得上"一条都不剩，而这些条目明明在库里、也在 14 天窗口内。

    契约：时效排序按 `COALESCE(published_at, created_at)`，无日期条目按入库时间
    参与竞争。
    """
    from app.services.feed.ranking import candidate_items
    from uuid import uuid4

    now = datetime.now(UTC)

    def _row(title: str, *, published: datetime | None, created: datetime) -> FeedItem:
        return FeedItem(
            id=str(uuid4()),
            canonical_key=f"key:{uuid4()}",
            kind="paper",
            title=title,
            url="https://example.org/x",
            summary="A summary so the item is feed-eligible.",
            authors=[],
            domains=["070101"],
            published_at=published,
            created_at=created,
            extra={},
        )

    dated = [
        _row(f"Dated paper {index}", published=now - timedelta(days=1), created=now - timedelta(days=1))
        for index in range(20)
    ]
    undated = _row("A web find with no publication date", published=None, created=now)
    db_session.add_all([*dated, undated])
    await db_session.flush()

    profile = Profile(user_id="u1", domains=("070101",))
    titles = {item.title for item in await candidate_items(db_session, profile=profile, limit=5)}

    assert "A web find with no publication date" in titles
