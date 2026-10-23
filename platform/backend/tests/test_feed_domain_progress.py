from unittest.mock import AsyncMock
import pytest
from app.services.feed import domain_progress
from app.schemas.feed import FeedProgressOut, FeedTodayOut


def test_domain_state_persists_and_isolates_users_and_selections(tmp_path, monkeypatch):
    monkeypatch.setattr(domain_progress, 'the_data_root', lambda: tmp_path)
    domain_progress.mark('one', ['A', 'B'], 'searching')
    domain_progress.mark('one', ['B'], 'completed')
    domain_progress.mark('two', ['A'], 'failed')
    assert domain_progress.read('one')['A']['state'] == 'searching'
    assert domain_progress.read('one')['B']['state'] == 'completed'
    assert domain_progress.read('two')['A']['state'] == 'failed'
    assert domain_progress.read('three') == {}


@pytest.mark.parametrize('active,acquisition,pending,waiting,recommended,expected', [
    (True, 'completed', 0, 0, 0, 'searching'),
    (False, 'searching', 0, 0, 0, 'interrupted'),
    (False, None, 0, 0, 0, 'not_started'),
    (False, 'completed', 1, 0, 0, 'preparing'),
    (False, 'completed', 0, 2, 2, 'translating'),
    (False, 'completed', 0, 0, 2, 'ready'),
    (False, 'completed', 0, 0, 0, 'empty'),
    (False, 'failed', 0, 0, 0, 'failed'),
])
def test_domain_phase_distinguishes_running_empty_and_interrupted(active, acquisition, pending, waiting, recommended, expected):
    assert domain_progress.phase(active=active, acquisition=acquisition, collected=0,
        pending=pending, translation_pending=waiting, recommendable=recommended) == expected


@pytest.mark.asyncio
async def test_progress_recomputes_live_counts_despite_cached_pool(monkeypatch):
    from app.api.v1 import feed
    cached = FeedTodayOut(progress=FeedProgressOut(collected=999, phase='settled'))
    fresh = FeedProgressOut(collected=3, pending=3, phase='searching')
    monkeypatch.setattr(feed, 'recommendation_pool', AsyncMock(return_value=cached))
    monkeypatch.setattr(feed.profile_service, 'build_profile', AsyncMock(return_value=object()))
    counter = AsyncMock(return_value=fresh)
    monkeypatch.setattr(feed, '_recommendation_progress', counter)
    monkeypatch.setattr(feed, '_attach_domain_progress', lambda *args: None)
    result = await feed.feed_progress(user=object(), db=object())
    assert result.collected == 3
    assert result.phase == 'searching'
    counter.assert_awaited_once()


def test_domain_recommendations_exclude_other_sources_and_count_cached_translations(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.api.v1 import feed
    from app.services.feed import translation
    from app.services.literature_harvester import literature_index_harvester
    monkeypatch.setattr(domain_progress, 'the_data_root', lambda: tmp_path)
    domain_progress.mark('one', ['A', 'B'], 'completed')
    monkeypatch.setattr(literature_index_harvester, 'is_online_refreshing', lambda user_id: False)
    monkeypatch.setattr(feed.ranking, '_domain_hits', lambda item, profile: tuple(code for code in item.domains if code in profile.domains))
    monkeypatch.setattr(translation, 'translation_ready', lambda item: item.ready)
    rows = [dict(domain=code, collected=1, prepared=1, pending=0, recommendable=0, translated=0, translation_pending=0) for code in ['A', 'B']]
    progress = FeedProgressOut(domain_progress=rows)
    cards = [SimpleNamespace(recommendation_sources=sources, item=SimpleNamespace(domains=domains, ready=ready)) for sources, domains, ready in [
        (['domain'], ['A'], True), (['domain', 'journal'], ['B'], False), (['project'], ['A'], True)]]
    feed._attach_domain_progress(progress, SimpleNamespace(feed=cards), SimpleNamespace(id='one'))
    assert progress.domain_progress[0]['recommendable'] == 1
    assert progress.domain_progress[0]['translated'] == 1
    assert progress.domain_progress[0]['phase'] == 'ready'
    assert progress.domain_progress[1]['translation_pending'] == 1
    assert progress.domain_progress[1]['phase'] == 'translating'


@pytest.mark.asyncio
async def test_parent_domain_counts_expanded_children_once(monkeypatch):
    from types import SimpleNamespace
    from app.api.v1 import feed
    from app.models.feed import FeedItem
    from app.services.literature_harvester import literature_index_harvester
    item = FeedItem(kind='paper', title='Tactics research', domains=['110401', '110402'], authors=[], extra={'literature_catalog': True, 'literature_preparation': 'pending'})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [item]))))
    user = SimpleNamespace(id='one')
    profile = SimpleNamespace(domains=('110401', '110402'), projects=(), journals={}, scholars=(), active_project_queries=None)
    monkeypatch.setattr(feed, '_visible_clause', lambda user: True)
    monkeypatch.setattr(feed.profile_service, 'stored_domains', lambda user: ('cas:1104',))
    monkeypatch.setattr(feed, 'language_for', lambda user: 'zh')
    monkeypatch.setattr(feed.domain_service, 'label', lambda code, lang: '战术学')
    monkeypatch.setattr(feed.curation, 'stored', lambda user: {})
    monkeypatch.setattr(feed.discovery, 'subscription_task_state', lambda user_id: None)
    monkeypatch.setattr(literature_index_harvester, 'is_online_refreshing', lambda user_id: False)
    result = await feed._recommendation_progress(db, user, profile, recommendable=0)
    assert result.domain_progress[0]['collected'] == 1
    assert result.domain_progress[0]['pending'] == 1
