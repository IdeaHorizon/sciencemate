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
    ScoredItem,
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


def test_chinese_project_can_use_specific_english_search_evidence() -> None:
    profile = _profile(("线控底盘", "新能源汽车线控底盘控制"))
    extra = {
        "feed_acquisition_routes": ["project_profile"],
        "profile_user_ids": ["u1"],
        "discovery_query": "steer-by-wire chassis control",
        "profile_query_relevance": 0.67,
    }
    relevant = _item("Design and control of a robot chassis", extra=extra)
    generic = _item("Religious social control and consumer choice", extra=extra)
    biology = _item("Aspergillus chassis expression control", extra=extra)
    filler = [_item(f"Unrelated field {n}", summary="unrelated topic") for n in range(30)]
    scored = rank([*filler, generic, biology, relevant], profile, now=NOW)
    by_title = {item.item.title: item for item in scored}
    assert by_title[relevant.title].matched_query_terms == ("chassis",)
    assert "chassis" in by_title[relevant.title].reason()
    assert by_title[generic.title].matched_query_terms == ()
    assert by_title[biology.title].matched_query_terms == ()


def test_project_title_evidence_recovers_low_scored_vehicle_dynamics_papers() -> None:
    profile = _profile(("车辆动力学和线控底盘", "车辆动力学和线控底盘"))
    extra = {
        "feed_acquisition_routes": ["project_profile"],
        "profile_user_ids": ["u1"],
        "discovery_query": "vehicle dynamics modeling",
        "profile_query_relevance": 0.25,
    }
    relevant = _item("Data-Driven Control of Vehicle Active Suspension", extra=extra)
    unrelated = _item("Electric Vehicle Battery Charging Demand Forecast", extra=extra)
    oncology = _item("Modeling Tumor Growth Dynamics", extra=extra)
    ranked = rank_rednote([relevant, unrelated, oncology], profile, user_id="u1", now=NOW)
    assert [value.item.title for value in ranked] == [relevant.title]
    assert ranked[0].matched_query_terms == ("vehicle suspension",)


def test_unsubscribed_journals_do_not_fill_field_dynamics_without_domains() -> None:
    from app.services.feed.ranking import rank_field_dynamics

    profile = _profile(("车辆动力学", "车辆动力学"))
    journals = [
        _item("Marine contaminants", venue="Marine Pollution Bulletin",
              extra={"feed_acquisition_routes": ["journal"]}),
        _item("Soil contaminants", venue="Science of The Total Environment",
              extra={"feed_acquisition_routes": ["journal"]}),
    ]
    assert rank_field_dynamics(journals, profile, now=NOW) == []


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


def test_project_feed_surfaces_new_relevant_work_ahead_of_old_highly_cited_work() -> None:
    profile = _profile(("钠离子电池硬碳负极", "hard carbon anode sodium ion battery"))
    route = {"feed_acquisition_routes": ["project_profile"], "profile_user_ids": ["u1"]}
    fresh = _item("New hard carbon anode for sodium ion batteries", extra={**route, "citations": 0})
    old = _item("Earlier hard carbon anode for sodium ion batteries", days_old=90,
                extra={**route, "citations": 500})

    filler = [_item(f"Unrelated background {n}") for n in range(30)]
    ranked = rank_rednote([old, fresh, *filler], profile, user_id="u1", now=NOW)

    assert [value.item.title for value in ranked] == [fresh.title, old.title]


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


@pytest.mark.asyncio
async def test_open_moves_only_the_same_paper_down_and_promotes_related_new_work(db_session) -> None:
    opened = FeedItem(
        id="aaaa0000-0000-4000-8000-000000000002", canonical_key="doi:opened",
        kind="paper", title="Quantum photonic waveguide experiment",
        url="https://example.org/opened", summary="quantum photons optical waveguide",
        authors=["A. Researcher"], domains=[], venue="Optics Letters", extra={},
    )
    db_session.add(opened)
    db_session.add(FeedEngagement(
        user_id="aaaa1111-1111-4111-8111-111111111111", item_id=opened.id,
        action=FeedEngagementAction.OPEN, created_at=NOW,
    ))
    await db_session.flush()
    related = _item("Photonic waveguide for quantum photons", venue="Optics Letters")
    unrelated = _item("Medieval manuscript catalog")
    scored = [ScoredItem(item, 0.7) for item in (opened, related, unrelated)]
    adjusted = await apply_behavior_preferences(
        db_session, user_id="aaaa1111-1111-4111-8111-111111111111",
        scored=scored, now=NOW,
    )
    by_title = {value.item.title: value.score for value in adjusted}
    assert by_title[related.title] > 0.7
    assert by_title[opened.title] < by_title[related.title]
    assert by_title[unrelated.title] == 0.7
    assert len(adjusted) == 3  # Viewed work remains available in the feed.


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
    titles = {item.title for item in await candidate_items(db_session, profile=profile, batch_size=5)}

    assert "A web find with no publication date" in titles


def test_project_paper_without_source_abstract_waits_for_lookup_completion() -> None:
    from app.services.feed.ranking import show_in_feed

    item = _item("Vehicle active suspension control", summary="", extra={
        "feed_acquisition_routes": ["project_profile"],
        "literature_preparation": "pending",
        "abstract_lookup_completed": False,
    })
    assert not show_in_feed(item)
    item.extra = {**item.extra, "literature_preparation": "ready",
                  "abstract_lookup_completed": True}
    assert show_in_feed(item)


def test_web_reference_and_storefront_pages_do_not_enter_news_stream() -> None:
    from app.services.feed.ranking import show_in_feed

    route = {"feed_acquisition_routes": ["web"], "web_search_backend": "baidu"}
    assert not show_in_feed(_item("线控底盘 - 百度百科", extra=route))
    assert not show_in_feed(_item("线控底盘在线购买", extra=route))
    assert show_in_feed(_item("同济大学发布线控底盘国家标准", extra=route))


def test_chinese_query_requires_primary_subject_not_only_a_method() -> None:
    profile = _profile(("智能网联新能源汽车的动力学建模与线控底盘控制",
                        "智能网联新能源汽车的动力学建模与线控底盘控制 稳定性 协同"))
    extra = {
        "feed_acquisition_routes": ["project_profile"],
        "profile_user_ids": ["u1"],
        "discovery_query": "线控底盘 动力学建模",
        "profile_query_relevance": 0.5,
    }
    false_hit = _item("土木工程伦理培养的系统动力学建模与协同稳定性", extra=extra)
    true_hit = _item("智能网联线控底盘的动力学建模与控制", extra=extra)
    filler = [_item(f"Unrelated field {n}") for n in range(30)]
    scored = rank([*filler, false_hit, true_hit], profile, now=NOW)
    by_title = {value.item.title: value for value in scored}
    assert by_title[false_hit.title].project_id is None
    assert not by_title[false_hit.title].matched_query_terms
    assert by_title[true_hit.title].project_id is not None


def test_historical_journal_tags_follow_the_repaired_subject_rules(monkeypatch):
    from app.services.feed.ranking import _domain_hits
    monkeypatch.delenv('HARNESS_JOURNAL_DOMAIN_MAP', raising=False)
    profile = _profile(domains=('050103',))
    unrelated = _item('Syntax of Central Javanese', venue='Language', domains=('050103',),
                      extra={'classification_mode': 'offline_journal_mapping'})
    relevant = _item('Ellipsis mismatch', summary='We study Mandarin Chinese.', venue='Language',
                     domains=('050103',), extra={'classification_mode': 'offline_journal_mapping'})
    assert _domain_hits(unrelated, profile) == ()
    assert _domain_hits(relevant, profile) == ('050103',)


@pytest.mark.parametrize('title,summary', [
    ('Visual foundation models robustness', 'Foundation models and AI robustness in computer vision.'),
    ('Clinical data machine learning robustness', 'Foundation models for clinical datasets and emergency care.'),
    ('Forecasting urbanization in Istanbul', 'Project robustness and deep learning extrapolation.'),
    ('Adversarial machine learning in cybersecurity', 'An AI survey of malware detection and robustness.'),
    ('Text-to-SQL on oncology data', 'Execution and robustness of language models.'),
    ('Tabular machine learning robustness', 'Machine learning robustness across tabular datasets.'),
])
def test_mlip_project_does_not_admit_generic_matches_from_other_fields(title, summary):
    profile = _profile(('Foundation MLIP Robustness Survey',
                        'foundation mlip robustness survey ai project execution'))
    extra = {'feed_acquisition_routes': ['project_profile'], 'profile_user_ids': ['u1'],
             'discovery_query': 'machine learning interatomic potential robustness',
             'profile_query_relevance': 0.6}
    item = _item(title, summary=summary, extra=extra)
    filler = [_item(f'Unrelated background {n}') for n in range(30)]
    assert not rank_rednote([*filler, item], profile, user_id='u1', now=NOW)


@pytest.mark.parametrize('title,summary', [
    ('MLIP robustness evaluation', 'Foundation models and robustness for interatomic potentials.'),
    ('Neural network potentials', 'Foundation models and robustness in molecular dynamics.'),
    ('MLFF transferability', 'Foundation models and robustness for atomic force fields.'),
    ('Materials property prediction under extrapolation', 'Foundation models and robustness with uncertainty.'),
    ('AI 生成晶体的声子基准', 'Foundation models and robustness for crystal stability.'),
])
def test_mlip_project_retains_aliases_and_nearby_physics_research(title, summary):
    profile = _profile(('Foundation MLIP Robustness Survey', 'foundation mlip robustness survey'))
    extra = {'feed_acquisition_routes': ['project_profile'], 'profile_user_ids': ['u1'],
             'discovery_query': 'MLIP benchmark uncertainty extrapolation',
             'profile_query_relevance': 0.5}
    item = _item(title, summary=summary, extra=extra)
    filler = [_item(f'Unrelated background {n}') for n in range(30)]
    assert any(value.item.id == item.id for value in rank_rednote([*filler, item], profile, user_id='u1', now=NOW))


def test_visual_foundation_project_retains_its_own_robustness_papers():
    profile = _profile(('Visual Foundation Robustness', 'visual foundation robustness vision'))
    item = _item('Visual foundation models robustness', summary='Vision robustness for foundation models.',
                 extra={'feed_acquisition_routes': ['project_profile'], 'profile_user_ids': ['u1']})
    filler = [_item(f'Unrelated background {n}') for n in range(30)]
    assert any(value.item.id == item.id for value in rank_rednote([*filler, item], profile, user_id='u1', now=NOW))


def test_replaced_project_cannot_reuse_old_query_evidence() -> None:
    profile = _profile(("颗粒沉降", "irregular particle sedimentation LBM IBM"))
    profile.active_project_queries = frozenset({"lattice Boltzmann immersed boundary sedimentation"})
    old = _item("Interatomic potential robustness benchmark", extra={
        "feed_acquisition_routes": ["project_profile"],
        "profile_user_ids": ["u1"],
        "discovery_query": "interatomic potential robustness",
        "profile_query_relevance": 0.9,
    })
    assert not rank_rednote([old], profile, user_id="u1", now=NOW)
    current = _item("Lattice Boltzmann immersed boundary sedimentation", extra={
        "feed_acquisition_routes": ["project_profile"],
        "profile_user_ids": ["u1"],
        "discovery_query": "lattice Boltzmann immersed boundary sedimentation",
        "profile_query_relevance": 0.9,
    })
    filler = [_item(f"Unrelated field {n}", summary="unrelated topic") for n in range(30)]
    assert any(value.item.id == current.id for value in rank_rednote([current, *filler], profile, user_id="u1", now=NOW))


@pytest.mark.parametrize('title,summary', [
    ('Probing Lattice Anharmonicity and Thermal Transport in Ultralow-κ Materials Using Machine Learning Interatomic Potentials',
     'Lattice thermal conductivity and Boltzmann transport equation in crystalline solids.'),
    ('A machine-learned interatomic potential for defects investigation in lead-free double perovskite Cs2NaYbCl6',
     'Chebyshev Interaction Model and lattice thermal conductivity.'),
])
def test_particle_settling_project_rejects_crystal_lattice_word_overlap(title, summary):
    profile = _profile(('LBM IBM 颗粒沉降', 'lattice Boltzmann immersed boundary irregular particle sedimentation fluid interaction'))
    item = _item(title, summary=summary)
    filler = [_item(f'Unrelated subject {n}') for n in range(30)]
    assert not any(s.item.id == item.id for s in rank_rednote([item, *filler], profile, user_id='u1', now=NOW))


@pytest.mark.parametrize('title', [
    'Lattice Boltzmann immersed boundary simulation of irregular particle sedimentation',
    'Settling of non-spherical particles in a viscous fluid',
    'An immersed boundary method for fluid-solid interaction',
    'Lattice Boltzmann simulations of incompressible flow',
    'Hydrodynamic drag on irregular particles',
])
def test_particle_settling_subject_keeps_relevant_methods_and_physics(title):
    from app.services.feed.ranking import _project_subject_matches
    assert _project_subject_matches('lattice Boltzmann immersed boundary particle sedimentation', title, '')


@pytest.mark.parametrize('title,summary', [
    ('Thermal behaviour of settling heavy inertial particles in turbulent natural convection', 'Direct numerical simulations investigate settling particles in thermal convection.'),
    ('Experimental investigation on settling behavior and inter-particle interaction of dual cubic particles released side-by-side in quiescent fluids', 'Experiments measure settling trajectories and wake interactions.'),
    ('Physics-Constrained Neural Network Framework for Predicting Terminal Settling Velocity', 'Terminal velocity of particles in non-Newtonian fluids.'),
    ('Collision efficiency of rapidly settling particle pairs in a turbulent flow', 'Hydrodynamic interactions of sedimenting spheres.'),
    ('An efficient sharp-interface immersed boundary method for complex moving boundary flows', 'An incompressible flow solver with adaptive Cartesian grids.'),
    ('A new conservative formulation of solid boundary conditions for hybrid Lattice–Boltzmann modeling of compressible flows', 'A fluid boundary scheme.'),
    ('On kernel functions for the immersed boundary method in lattice Boltzmann methods', 'Kernel accuracy and stability.'),
    ('Hydrodynamic drag on irregular particles', 'Viscous fluid forces depend on particle shape.'),
])
def test_particle_physics_evidence_recovers_precise_matches_with_low_retrieval_score(title, summary):
    profile = _profile(('格子玻尔兹曼与IBM模拟不规则颗粒沉降', 'lattice Boltzmann immersed boundary particle sedimentation'))
    query = 'irregular non-spherical particle settling'
    profile.active_project_queries = frozenset({query})
    item = _item(title, summary=summary, extra={'feed_acquisition_routes': ['project_profile'],
                 'profile_user_ids': ['u1'], 'discovery_query': query, 'profile_query_relevance': 0.25})
    scored = rank_rednote([item], profile, user_id='u1', now=NOW)
    assert scored and scored[0].item.id == item.id
    assert scored[0].matched_query_terms or scored[0].project_id


@pytest.mark.parametrize('title,summary', [
    ('Non-spherical atom refinement applied in crystal structure determination', 'A structure refinement of crystals and atoms.'),
    ('Low-Fraction Irregular Inclusions Trigger Large Compression Stiffening in Biopolymer Hydrogels', 'Mechanical strength of a material with irregular inclusions.'),
    ('A novel method for inverse kinematics analysis of non-spherical wrist robotic manipulators', 'Robotic wrist motion and inverse kinematics.'),
    ('Exact analytical treatment of thermodynamic signatures in non-spherical quantum dots', 'Quantum particle confinement.'),
    ('Finite-Point Electrostatic Representation of Spherical and Non-Spherical Charged Particles', 'Discrete charge distributions and electrostatic fields.'),
    ('Non-linear evolution of the unstratified polydisperse dust settling instability', 'Astrophysical protoplanetary disks and planetary formation.'),
    ('Lattice thermal conductivity from machine learning interatomic potentials', 'Phonon transport in perovskite crystals.'),
    ('A construction of F-irregular graphs', 'Combinatorial graph degrees.'),
    ('Optimization of Immersed Membrane Integrated in Sedimentation Tank', 'Treatment of Escherichia coli in drinking water.'),
])
def test_new_particle_evidence_does_not_admit_shape_or_lattice_word_collisions(title, summary):
    from app.services.feed.ranking import _particle_physics_evidence
    assert not _particle_physics_evidence('lattice Boltzmann immersed boundary particle sedimentation', title, summary)


def test_particle_physics_evidence_cannot_reuse_an_old_project_query():
    profile = _profile(('格子玻尔兹曼颗粒沉降', 'lattice Boltzmann particle sedimentation'))
    profile.active_project_queries = frozenset({'current query'})
    item = _item('Thermal behaviour of settling heavy inertial particles', summary='Particles settling in viscous fluids.',
                 extra={'feed_acquisition_routes': ['project_profile'], 'profile_user_ids': ['u1'],
                        'discovery_query': 'old query', 'profile_query_relevance': 0.25})
    assert not rank_rednote([item], profile, user_id='u1', now=NOW)


@pytest.mark.parametrize('title,summary', [
    ('Bubble curtains for aquatic pollution control: A lattice Boltzmann study', 'An air-water flow simulation for pollution control.'),
    ('The lattice Boltzmann method for wind farm simulations: a review', 'Fluid flows in large wind farms.'),
    ('Lattice Boltzmann Simulation of Acoustic Liners', 'Compressible flow and acoustic response of perforated liners.'),
])
def test_a_different_application_using_lbm_alone_is_not_sufficient_new_evidence(title, summary):
    from app.services.feed.ranking import _particle_physics_evidence
    assert not _particle_physics_evidence('lattice Boltzmann particle sedimentation', title, summary)


def test_sedimentation_tank_equipment_is_not_particle_physics_evidence():
    from app.services.feed.ranking import _particle_physics_evidence
    assert not _particle_physics_evidence('lattice Boltzmann particle sedimentation',
        'Optimization of Immersed Membrane Integrated in Sedimentation Tank',
        'The water treatment plant removes suspended particles using immersed membranes.')


def test_current_project_title_anchor_rejects_neighboring_battery_topics() -> None:
    """A broad abstract overlap cannot label unrelated chemistry as this project."""
    profile = Profile(
        user_id="u1",
        projects=(ProjectSignal(
            project_id="sodium-project", name="Sodium hard carbon anodes",
            terms=extract_terms("sodium-ion batteries hard carbon anodes initial Coulombic efficiency"),
        ),),
        active_project_queries=frozenset({
            "hard carbon anode sodium-ion battery",
            "solid electrolyte interphase sodium solvation",
            "initial Coulombic efficiency hard carbon",
        }),
    )
    unrelated = _item(
        "Collective optical response of carbon nanotubes",
        summary="The initial efficiency of sodium-ion batteries is discussed as a neighboring application.",
    )
    relevant = _item(
        "Engineering closed pores in hard carbon for enhanced sodium storage",
        summary="Hard carbon anodes improve initial Coulombic efficiency in sodium-ion batteries.",
    )
    fillers = [_item(f"Unrelated optics experiment {index}") for index in range(30)]
    scored = {entry.item.title: entry for entry in rank([*fillers, unrelated, relevant], profile, now=NOW)}
    assert scored[unrelated.title].project_id is None
    assert scored[relevant.title].project_id == "sodium-project"


def test_sodium_ion_project_does_not_claim_lithium_or_potassium_batteries() -> None:
    from app.services.feed.ranking import _project_subject_matches

    topic = "钠离子电池 hard carbon sodium-ion batteries initial Coulombic efficiency"
    assert not _project_subject_matches(topic,
        "High initial Coulombic efficiency in lithium metal batteries",
        "The method may inform sodium-ion batteries in future work.")
    assert not _project_subject_matches(topic,
        "Hard carbon anodes for potassium-ion batteries",
        "Compared with sodium storage materials.")
    assert _project_subject_matches(topic,
        "Hard carbon anodes for Na-ion batteries",
        "Improved initial Coulombic efficiency and stable sodium storage.")


def test_project_discovery_query_cannot_override_current_subject_guard() -> None:
    profile = Profile(
        user_id="u1",
        projects=(ProjectSignal(
            project_id="sodium-project", name="钠离子电池硬碳负极",
            terms=extract_terms("sodium-ion batteries hard carbon anodes initial Coulombic efficiency"),
        ),),
        active_project_queries=frozenset({"initial Coulombic efficiency hard carbon"}),
    )
    extra = {"feed_acquisition_routes": ["project_profile"],
             "profile_user_ids": ["u1"], "discovery_query": "initial Coulombic efficiency hard carbon",
             "profile_query_relevance": 0.6}
    lithium = _item("High initial Coulombic efficiency in lithium metal anodes",
                    summary="Sodium-ion batteries are mentioned for comparison.", extra=extra)
    sodium = _item("Hard carbon anodes improve sodium-ion battery efficiency",
                   summary="Initial Coulombic efficiency of sodium storage is measured.", extra=extra)
    fillers = [_item(f"Unrelated optics experiment {index}") for index in range(30)]
    scored = {entry.item.title: entry for entry in rank([*fillers, lithium, sodium], profile, now=NOW)}
    assert scored[lithium.title].project_id is None
    assert not scored[lithium.title].matched_query_terms
    assert scored[sodium.title].project_id or scored[sodium.title].matched_query_terms
