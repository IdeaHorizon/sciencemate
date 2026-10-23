from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.feed import FeedItem, FeedVisibility
from app.services.feed.profile import Profile, ProjectSignal, extract_terms
from app.services.feed.ranking import candidate_items


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['domain', 'journal', 'scholar', 'project'])
async def test_each_source_keeps_more_than_400_items_without_relaxing_gates(db_session, monkeypatch, source):
    now = datetime.now(UTC)
    monkeypatch.setattr("app.services.literature_harvester._journal_targets", lambda domains: {"test": {"journal": "Test Journal"}})
    profile = Profile(user_id='u1', domains=('070102',),
        journals=frozenset({'waterresearch'}) if source == 'journal' else frozenset(),
        scholars=frozenset({'Researcher'}) if source == 'scholar' else frozenset(),
        scholar_identities=({'author_id': 'A123'},) if source == 'scholar' else (),
        projects=(ProjectSignal('p', 'Particle sedimentation', extract_terms('particle sedimentation')),) if source == 'project' else (),
        active_project_queries=('particle sedimentation',))
    def item(index, **changes):
        extra = {'literature_catalog': True, 'literature_eligible': True,
            'literature_preparation': 'ready', 'feed_acquisition_routes': [{'domain':'journal', 'project':'project_profile'}.get(source, source)],
            'profile_user_ids': ['u1'], 'discovery_query': 'particle sedimentation'}
        if source == 'scholar': extra['author_identities'] = [{'author_id': 'A123'}]
        extra.update(changes.pop('extra', {}))
        values = dict(id=str(uuid4()), canonical_key=f'{source}:{index}', kind='paper',
            title=f'Particle sedimentation research {index}', summary='Valid abstract.',
            published_at=now-timedelta(days=1), visibility=FeedVisibility.PLATFORM,
            domains=['070102'] if source == 'domain' else [], authors=[],
            venue='Water Research' if source == 'journal' else 'Test Journal', extra=extra)
        values.update(changes)
        return FeedItem(**values)
    valid = [item(i) for i in range(405)]
    pending = item('pending', extra={'literature_preparation':'pending'})
    excluded = [item('ineligible',extra={'literature_eligible':False}),
        item('old',published_at=now-timedelta(days=500)),
        item('notice',title='Editorial Board'),
        item('private',visibility=FeedVisibility.ORGANIZATION,organization_id='other-org')]
    db_session.add_all([*valid, pending, *excluded])
    await db_session.flush()
    result = await candidate_items(db_session, profile=profile)
    assert {row.id for row in result} == {row.id for row in valid}
    assert len(result) == 405
    prewarming = await candidate_items(db_session, profile=profile, include_preparing=True)
    assert {row.id for row in prewarming} == {row.id for row in [*valid, pending]}
