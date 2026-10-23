from types import SimpleNamespace
from app.schemas.feed import FeedProgressOut
from app.services.feed import source_progress


def test_four_sources_have_independent_collection_and_translation_stages(monkeypatch):
    monkeypatch.setattr(source_progress.curation, 'stored', lambda user: {'subscriptions': {'journals': [{'key': 'j'}], 'scholars': [{'name': 'Same name'}]}})
    monkeypatch.setattr(source_progress.profile_service, 'stored_domains', lambda user: ('domain',))
    monkeypatch.setattr(source_progress.domain_progress, 'read', lambda uid: {'domain': {'state': 'completed'}})
    monkeypatch.setattr(source_progress.discovery, '_subscription_active_targets', {'one': 'scholars'})
    monkeypatch.setattr(source_progress.discovery, '_project_inflight', {})
    monkeypatch.setattr(source_progress.literature_index_harvester, '_online_tasks', {})
    monkeypatch.setattr(source_progress.literature_index_harvester, 'is_online_refreshing', lambda uid: False)
    progress = FeedProgressOut(source_progress=[{'source': name, 'collected': 2, 'pending': pending} for name, pending in [('domain', 0), ('journal', 1), ('scholar', 0), ('project', 0)]], source_groups=[{'sources': ['domain', 'project'], 'total': 2, 'translated': 1}])
    source_progress.attach(progress, SimpleNamespace(id='one'), SimpleNamespace(journals=('j',), projects=('p',), active_project_queries=('q',)))
    rows = {row['source']: row for row in progress.source_progress}
    assert rows['domain']['phase'] == 'translating'
    assert rows['journal']['phase'] == 'preparing'
    assert rows['scholar']['phase'] == 'searching'
    assert rows['scholar']['identity_pending'] == 1
    assert rows['project']['phase'] == 'translating'
    assert rows['project']['ready'] == 1
