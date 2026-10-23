from types import SimpleNamespace

from app.api.v1.feed import _mix_recommendation_sources


def test_available_domain_is_present_before_the_first_page_ends():
    project = [SimpleNamespace(project_id='p1', reason='按你的课题检索词找到', is_today_pick=i < 3) for i in range(60)]
    domain = SimpleNamespace(project_id=None, reason='属于你关注的 汉语言文字学', is_today_pick=False)
    journal = SimpleNamespace(project_id=None, reason='你订阅的期刊 Water Research 有新文章', is_today_pick=False)
    scholar = SimpleNamespace(project_id=None, reason='你订阅的学者有新论文', is_today_pick=False)
    original = [*project, domain, journal, scholar]
    result = _mix_recommendation_sources(original)
    assert sum(card.is_today_pick for card in result) == 3
    assert domain in result[:21] and journal in result[:21] and scholar in result[:21]
    assert len(result) == len(original)
    assert {id(card) for card in result} == {id(card) for card in original}
    assert [card for card in result if card.project_id] == project


def test_source_already_on_first_page_preserves_existing_order():
    cards = [SimpleNamespace(project_id=None, reason='属于你关注的 汉语言文字学', is_today_pick=False)]
    assert _mix_recommendation_sources(cards) == cards


def test_all_four_sources_get_equal_turns_across_pages():
    from collections import Counter
    reasons = ['属于你关注的 学科', '你订阅的期刊有新文章', '你订阅的学者有新论文', '按你的课题检索词找到']
    cards = [SimpleNamespace(project_id=None, reason=reason, is_today_pick=False)
             for reason in reasons for _ in range(15)]
    result = _mix_recommendation_sources(cards)
    for offset in (0, 20, 40):
        assert Counter(card.reason for card in result[offset:offset + 20]) == {reason: 5 for reason in reasons}


def test_one_paper_keeps_all_source_memberships_without_duplicate_cards():
    shared = SimpleNamespace(project_id=None, reason='期刊动态', recommendation_sources=['journal', 'scholar'])
    project = SimpleNamespace(project_id='p', reason='课题', recommendation_sources=['project'])
    result = _mix_recommendation_sources([project, shared])
    assert len(result) == 2
    assert sum(card is shared for card in result) == 1
