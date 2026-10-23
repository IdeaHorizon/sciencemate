from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.feed import FeedItem
from app.models.user import User
from app.services.feed import pool_cache, profile, ranking
from nodes.literature.tools.local_index import LocalPaperIndex


def test_incremental_window_survives_restart_and_weekly_backfill(tmp_path):
    path = str(tmp_path / 'cache.sqlite3')
    index = LocalPaperIndex(path)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    key = 'journal-harvest:issn:1234-5678'
    assert index.harvest_window(key, '2025-09-01', now=now) == ('2025-09-01', True)
    index.mark_harvest_window(key, full=True, now=now)
    restarted = LocalPaperIndex(path)
    assert restarted.harvest_window(key, '2025-09-01', now=now + timedelta(days=1)) == ('2026-09-01', False)
    restarted.mark_harvest_window(key, full=False, now=now + timedelta(days=1))
    assert restarted.harvest_window(key, '2025-09-01', now=now + timedelta(days=7)) == ('2025-09-01', True)
    assert restarted.harvest_window('new subscription', '2025-09-01', now=now) == ('2025-09-01', True)


@pytest.mark.asyncio
async def test_early_translation_pool_never_exposes_pending_or_unselected_articles(db_session, monkeypatch, tmp_path):
    from app.api.v1 import feed
    from app.services.feed import literature_projection
    pool_cache.clear()
    user = User(email='preparing@test.local', display_name='Preparing', hashed_password='x', preferences={})
    db_session.add(user)
    await db_session.flush()
    selected = profile.Profile(user_id=str(user.id), journals=frozenset({profile.journal_key('Acta Geotechnica')}))
    monkeypatch.setattr(profile, 'build_profile', AsyncMock(return_value=selected))
    monkeypatch.setattr(literature_projection, 'literature_papers_root', lambda: tmp_path)
    def paper(title, venue='Acta Geotechnica', eligible=True):
        return FeedItem(canonical_key=f'preparing:{title}', title=title, summary='A source abstract.',
                        kind='paper', venue=venue, published_at=datetime.now(UTC), authors=[], domains=[],
                        extra={'literature_catalog': True, 'literature_eligible': eligible, 'literature_preparation': 'pending',
                               'feed_acquisition_routes': ['journal'], 'cas_quartile': 1})
    wanted = paper('Early particle study')
    db_session.add_all([wanted, paper('Editorial Board'), paper('Unselected study', venue='Other journal'),
                        paper('Rejected policy study', eligible=False)])
    await db_session.commit()
    assert await ranking.candidate_items(db_session, profile=selected) == []
    early = await feed.preparing_translation_pool(user, db_session)
    assert [card.item.id for card in early.feed] == [str(wanted.id)]
    index = LocalPaperIndex(str(tmp_path / 'literature_catalog.sqlite3'))
    index.save_title_translations({wanted.title: '提前翻译标题'})
    early = await feed.preparing_translation_pool(user, db_session)
    assert early.feed[0].item.title_zh == '提前翻译标题'
    assert await ranking.candidate_items(db_session, profile=selected) == []


@pytest.mark.asyncio
async def test_project_indexes_are_stored_before_asset_fetch_and_failure_does_not_advance_window(monkeypatch, tmp_path):
    from app.services.feed import discovery, literature_projection
    from nodes.literature.tools import archive_papers, filter as paper_filter, search_engines
    index = LocalPaperIndex(str(tmp_path / 'cache.sqlite3'))
    captured = []
    paper = search_engines.Paper(title='Irregular particle settling', authors=[], abstract='A source abstract.',
                                doi='10.1/test', source='crossref', pub_date=datetime.now(UTC).date().isoformat(), year=2026)
    class Manager:
        local_index = index
        async def search_all(self, query, **kwargs):
            captured.append(kwargs['date_from'])
            return search_engines.SearchResults([paper], source_diagnostics={'crossref': {'status': 'ok'}})
    monkeypatch.setattr(search_engines, 'SearchManager', Manager)
    monkeypatch.setattr(paper_filter, 'compute_relevance_score', lambda *args: 1.0)
    projected = AsyncMock(return_value=literature_projection.ProjectionReport(seen=1))
    monkeypatch.setattr(literature_projection, 'project_literature_catalog', projected)
    monkeypatch.setattr(discovery, 'collectors_politeness', lambda: 0)
    monkeypatch.setattr(archive_papers, 'archive_index_and_figure', lambda *args: pytest.fail('Acquisition must not wait for images'))
    stored = []
    monkeypatch.setattr(archive_papers, 'archive_indexes_only', lambda papers, output: stored.extend(papers) or papers)
    user = SimpleNamespace(id='u')
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    discovery.project_asset_wakeup.clear()
    assert await discovery._targeted_search_lane(None, db, user=user, queries=['particle settling'], lane='paper') == 1
    assert stored[0]['title'] == paper.title
    assert projected.await_count == 1
    assert db.commit.await_count == 1
    assert discovery.project_asset_wakeup.is_set()
    key = 'project:u:particle settling'
    assert index.harvest_window(key, '2025-09-01')[1] is False
    old = index.harvest_window(key, '2025-09-01')
    def fail(*args): raise RuntimeError('disk unavailable')
    monkeypatch.setattr(archive_papers, 'archive_indexes_only', fail)
    assert await discovery._targeted_search_lane(None, db, user=user, queries=['different query'], lane='paper') == 0
    assert index.harvest_window('project:u:different query', '2025-09-01') == ('2025-09-01', True)


@pytest.mark.asyncio
async def test_project_results_are_projected_in_bounded_batches(monkeypatch, tmp_path):
    from app.services.feed import discovery, literature_projection
    from nodes.literature.tools import archive_papers, filter as paper_filter, search_engines
    papers = [search_engines.Paper(title=f'Particle study {i}', authors=[], abstract='A source abstract.',
              doi=f'10.1234/particle{i}', source='crossref', pub_date=datetime.now(UTC).date().isoformat(), year=2026)
              for i in range(55)]
    class Manager:
        local_index = None
        async def search_all(self, query, **kwargs):
            return search_engines.SearchResults(papers, source_diagnostics={'crossref': {'status': 'ok'}})
    monkeypatch.setattr(search_engines, 'SearchManager', Manager)
    monkeypatch.setattr(paper_filter, 'compute_relevance_score', lambda *args: 1.0)
    monkeypatch.setattr(discovery, 'collectors_politeness', lambda: 0)
    batches = []
    def archive(rows, _root):
        batches.append(len(rows))
        return rows
    monkeypatch.setattr(archive_papers, 'archive_indexes_only', archive)
    projected = AsyncMock(side_effect=lambda _db, **kwargs: literature_projection.ProjectionReport(seen=len(kwargs['catalog_keys'])))
    monkeypatch.setattr(literature_projection, 'project_literature_catalog', projected)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    count = await discovery._targeted_search_lane(None, db, user=SimpleNamespace(id='u'),
                                                   queries=['particle'], lane='paper')
    assert count == 55
    assert batches == [25, 25, 5]
    assert projected.await_count == 3
    assert db.commit.await_count == 3
    assert all(len(call.kwargs['catalog_keys']) <= 25 for call in projected.await_args_list)


def test_index_first_acquisition_preserves_existing_users_and_routes(tmp_path):
    from nodes.literature.tools.archive_papers import archive_indexes_only
    first = {"doi": "10.1007/shared-index", "title": "Shared particle study", "source": "crossref",
             "feed_acquisition_routes": ["journal"], "profile_user_ids": ["user-a"], "author_identities": [{"author_id": "A123"}]}
    archive_indexes_only([first], str(tmp_path))
    second = {**first, "feed_acquisition_routes": ["project_profile"], "profile_user_ids": ["user-b"], "author_identities": []}
    record = archive_indexes_only([second], str(tmp_path))[0]
    assert record["feed_acquisition_routes"] == ["journal", "project_profile"]
    assert record["profile_user_ids"] == ["user-a", "user-b"]
    assert record["author_identities"] == [{"author_id": "A123"}]
