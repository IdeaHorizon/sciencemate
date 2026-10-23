import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.feed import FeedItem
from app.services.feed import discovery, literature_projection, profile as profiles
from nodes.literature.tools import archive_papers, literature_catalog


def test_images_complete_in_parallel_and_skip_publication_metadata(monkeypatch, tmp_path):
    records = [{'doi': f'10.1/{i}', 'title': f'Research {i}', 'figure_status': 'not_requested'} for i in range(3)]
    records += [{'doi': '10.1/notice', 'title': 'Associate Registry', 'figure_status': 'not_requested'}]
    monkeypatch.setattr(archive_papers, 'retry_asset_records', lambda *a, **k: records)
    barrier = threading.Barrier(3)
    def complete(record, root):
        barrier.wait(timeout=2)
        return record
    monkeypatch.setattr(archive_papers, 'archive_index_and_figure', complete)
    keys = []
    result = archive_papers.complete_missing_figures(str(tmp_path), concurrency=3, completed_keys=keys)
    assert result['completed'] == 3 and result['failed'] == 0
    assert set(keys) == {'10.1/0', '10.1/1', '10.1/2'}


def test_finished_assets_repair_pending_gate_without_network(monkeypatch, tmp_path):
    record = {'doi':'10.1/done','title':'Research','abstract':'Existing abstract',
              'literature_preparation':'pending','figure_status':'acquired','image':{'status':'available'}}
    monkeypatch.setattr(archive_papers, 'retry_asset_records', lambda *a, **k: [record])
    saved = []
    monkeypatch.setattr(archive_papers, 'archive_indexes_only', lambda rows, root: saved.extend(rows))
    monkeypatch.setattr(archive_papers, 'archive_index_and_figure', lambda *a: pytest.fail('assets fetched again'))
    result = archive_papers.complete_missing_figures(str(tmp_path))
    assert result['completed'] == 1
    assert saved[0]['literature_preparation'] == 'ready'


@pytest.mark.asyncio
async def test_all_current_subscriptions_are_enrolled_and_receive_fair_turns(db_session, monkeypatch, tmp_path):
    selected = profiles.Profile(user_id='u', domains=('cs.LG',), journals=frozenset({'waterresearch'}),
        scholars=frozenset({'Author'}), scholar_identities=({'author_id':'A123'},))
    monkeypatch.setattr(profiles,'build_profile',AsyncMock(return_value=selected))
    monkeypatch.setattr(literature_projection,'literature_papers_root',lambda:tmp_path)
    projected = AsyncMock()
    monkeypatch.setattr(literature_projection,'project_literature_catalog',projected)
    records=[]
    for index,(title,domains,venue,author) in enumerate([
        ('Field study',['cs.LG'],None,False),('Journal study',[],'Water Research',False),
        ('Author study',[],None,True),('Old subscription',[],'Dropped Journal',False),
        ('Associate Registry',[],'Water Research',False)]):
        key=f'10.1/{index}'
        extra={'catalog_key':key,'literature_catalog':True,'literature_eligible':True,'literature_preparation':'pending'}
        if author: extra['author_identities']=[{'author_id':'A123'}]
        db_session.add(FeedItem(canonical_key=key,title=title,kind='paper',published_at=datetime.now(UTC),
            domains=domains,venue=venue,authors=[],extra=extra))
        records.append({'doi':key,'title':title,'article_dir':str(tmp_path/str(index)),
            'index_path':str(tmp_path/str(index)/'index.json'), 'literature_preparation':'ready' if index==2 else 'pending'})
    literature_catalog.upsert_papers(tmp_path,records)
    saved=[]
    monkeypatch.setattr(archive_papers,'archive_indexes_only',lambda rows,root:saved.extend(rows))
    await db_session.flush()
    keys=await discovery._subscription_asset_keys(db_session,SimpleNamespace(id='u'))
    assert keys==['10.1/0','10.1/1','10.1/2']
    assert {r['doi'] for r in saved}==set(keys)
    assert all(r['profile_user_ids']==['u'] for r in saved)
    assert projected.call_args.kwargs['catalog_keys']==['10.1/2']


@pytest.mark.asyncio
async def test_asset_queries_and_projection_are_scoped_to_selected_keys(db_session,monkeypatch,tmp_path):
    from app.services.feed import abstract_backfill
    monkeypatch.setattr(literature_projection,'literature_papers_root',lambda:tmp_path)
    monkeypatch.setattr(abstract_backfill,'paper_cache_dir',lambda:tmp_path)
    records=[{'doi':f'10.1234/{name}','title':f'{name} research','abstract':'',
        'profile_user_ids':['u'],'venue':'Water Research','source':'crossref','pub_type':'journal-article',
        'pub_date':datetime.now(UTC).date().isoformat(),'subscription_requested':True,
        'feed_acquisition_routes':['journal'],'literature_preparation':'pending',
        'article_dir':str(tmp_path/name),'image':{'status':'not_requested'}} for name in ['selected','old']]
    literature_catalog.upsert_papers(tmp_path,records)
    keys=['10.1234/selected']
    assert [r['doi'] for r in literature_catalog.retry_asset_records(tmp_path,profile_user_id='u',catalog_keys=keys)]==keys
    assert [r['doi'] for r in abstract_backfill._candidates('u',None,20,catalog_keys=keys)]==keys
    report=await literature_projection.project_literature_catalog(db_session,catalog_keys=keys,profile_user_id='u')
    assert report.seen==1 and report.stored==1


def test_html_encoded_journal_is_selected_for_completion(monkeypatch, tmp_path):
    record = {'doi': '10.1021/example', 'title': 'Water treatment research',
              'venue': 'ACS ES&amp;T\nWater', 'figure_status': 'not_requested'}
    monkeypatch.setattr(archive_papers, 'retry_asset_records', lambda *a, **k: [record])
    monkeypatch.setattr(archive_papers, 'archive_index_and_figure', lambda record, root: record)
    result = archive_papers.complete_missing_figures(str(tmp_path), journal_names=['ACS ES&T Water'])
    assert result['completed'] == 1
    assert profiles.journal_key(record['venue']) == profiles.journal_key('ACS ES&T Water')
    item = FeedItem(venue=record['venue'])
    profile = profiles.Profile(user_id='user', journals=frozenset({profiles.journal_key('ACS ES&T Water')}))
    from app.services.feed.ranking import matches_subscribed_journal
    assert matches_subscribed_journal(item, profile)


@pytest.mark.parametrize('raw,plain', [
    ('Computers &amp; Education', 'Computers & Education'),
    ('Computers &amp;amp; Education', 'Computers & Education'),
    ('Astronomy &#38; Astrophysics', 'Astronomy & Astrophysics'),
    ('Environmental Science &amp;\nTechnology', 'Environmental Science & Technology'),
    ('Géotechnique', 'Geotechnique'),
    ('Ｇｅｏｔｅｃｈｎｉｑｕｅ', 'Geotechnique'),
    ('The British Accounting Review', 'British Accounting Review'),
])
def test_journal_keys_agree_across_subscription_and_domain_mapping(raw, plain):
    from nodes.literature.tools.journal_scope import journal_key
    from app.services.literature_harvester import _normal_journal
    assert profiles.journal_key(raw) == profiles.journal_key(plain)
    assert profiles.journal_key(raw) == journal_key(raw) == _normal_journal(raw)


@pytest.mark.asyncio
async def test_subscription_asset_keys_skip_ready_and_cooling_records(db_session, monkeypatch, tmp_path):
    selected = profiles.Profile(user_id='u', journals=frozenset({'waterresearch'}))
    monkeypatch.setattr(profiles, 'build_profile', AsyncMock(return_value=selected))
    monkeypatch.setattr(literature_projection, 'literature_papers_root', lambda: tmp_path)
    rows = [
        ('ready', 'ready', True, ''),
        ('cooling', 'ready', False, '2999-01-01T00:00:00+00:00'),
        ('retry', 'ready', False, '2000-01-01T00:00:00+00:00'),
        ('pending', 'pending', True, ''),
    ]
    catalog = []
    for name, preparation, lookup_done, retry_at in rows:
        key = f'10.1/{name}'
        db_session.add(FeedItem(canonical_key=key, title=f'{name} study', kind='paper',
            published_at=datetime.now(UTC), venue='Water Research',
            extra={'catalog_key':key,'literature_eligible':True,'literature_preparation':preparation}))
        catalog.append({'doi':key,'title':f'{name} study','venue':'Water Research',
            'article_dir':str(tmp_path/name),'profile_user_ids':['u'],
            'literature_preparation':preparation,'abstract':'',
            'abstract_lookup_completed':lookup_done,
            'abstract_lookup_retry_after':retry_at})
    literature_catalog.upsert_papers(tmp_path, catalog)
    await db_session.flush()
    keys = await discovery._subscription_asset_keys(db_session, SimpleNamespace(id='u'))
    assert set(keys) == {'10.1/retry', '10.1/pending'}


@pytest.mark.asyncio
async def test_no_progress_stops_subscription_asset_batch(monkeypatch):
    from app.services.feed import abstract_backfill
    monkeypatch.setattr(abstract_backfill, 'backfill_subscription_abstracts',
                        AsyncMock(return_value={'candidates':20,'found':0}))
    monkeypatch.setattr(archive_papers, 'complete_missing_figures',
                        lambda *a, **k: {'candidates':0,'completed':0,'failed':0})
    result = await discovery.complete_subscription_assets_for_user('no-progress-user', batches=3)
    assert result['abstract_attempted'] == 20
    assert abstract_backfill.backfill_subscription_abstracts.await_count == 1


@pytest.mark.asyncio
async def test_project_lookup_without_new_abstract_waits_for_next_tick(monkeypatch):
    from app.services.feed import scheduler
    collector = scheduler.FeedCollector()
    monkeypatch.setattr(discovery, 'run_project_asset_round',
                        AsyncMock(return_value={'attempted':20,'found':0}))
    waits = []
    async def wait_once(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        collector._stop.set()
        raise TimeoutError
    monkeypatch.setattr(scheduler.asyncio, 'wait_for', wait_once)
    await collector._run_project_assets()
    assert waits == [scheduler.TICK_SECONDS]


@pytest.mark.asyncio
async def test_ready_project_catalog_repairs_pending_feed_projection(db_session, monkeypatch, tmp_path):
    selected = profiles.Profile(user_id='u', projects=(profiles.ProjectSignal('p', 'Fluid simulation', frozenset({'fluid'})),),
        active_project_queries=frozenset({'fluid particle'}))
    monkeypatch.setattr(profiles, 'build_profile', AsyncMock(return_value=selected))
    monkeypatch.setattr(literature_projection, 'literature_papers_root', lambda: tmp_path)
    projected = AsyncMock()
    monkeypatch.setattr(literature_projection, 'project_literature_catalog', projected)
    key = 'url:project-ready'
    db_session.add(FeedItem(canonical_key=key, title='Fluid particle simulation', kind='paper',
        published_at=datetime.now(UTC), venue='Example Journal',
        extra={'catalog_key':key,'literature_eligible':True,'literature_preparation':'pending',
               'feed_acquisition_routes':['project_profile'],'profile_user_ids':['u'],
               'discovery_query':'fluid particle'}))
    literature_catalog.upsert_papers(tmp_path,[{'doi':key,'title':'Fluid particle simulation',
        'article_dir':str(tmp_path/'project'),'profile_user_ids':['u'],
        'literature_preparation':'ready','abstract':'Original abstract',
        'image':{'status':'available'}}])
    await db_session.flush()
    assert await discovery._subscription_asset_keys(db_session, SimpleNamespace(id='u')) == []
    assert projected.call_args.kwargs['catalog_keys'] == [key]
