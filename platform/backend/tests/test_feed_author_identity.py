from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.schemas.feed import ScholarSubscription
from app.services.feed import author_identity, ranking, discovery
from app.services.feed.profile import Profile
from app.models.feed import FeedItem


def test_subscription_distinguishes_same_names_and_marks_legacy_unresolved():
    one = ScholarSubscription(key='openalex:A123', name='Heng Wang', institution='University One')
    two = ScholarSubscription(key='openalex:A456', name='Heng Wang', institution='University Two')
    assert one.key != two.key
    assert one.identity_status == two.identity_status == 'resolved'
    assert ScholarSubscription(key='author:heng wang', name='Heng Wang', identity_status='resolved').identity_status == 'unresolved'
    with pytest.raises(ValueError):
        ScholarSubscription(key='openalex:A123', author_id='A456', name='Heng Wang')


def test_same_name_different_id_does_not_match_but_name_variants_with_same_id_do():
    profile = Profile(user_id="one", scholars=frozenset({'heng wang'}), scholar_identities=({'author_id':'A123'},))
    other = FeedItem(kind='paper', title='Other author', authors=['Heng Wang'], extra={'author_identities':[{'author_id':'A456'}]})
    chosen = FeedItem(kind='paper', title='Chosen author', authors=['H. Wang'], extra={'author_identities':[{'author_id':'https://openalex.org/A123'}]})
    legacy = FeedItem(kind='paper', title='Unverified', authors=['Heng Wang'], extra={})
    assert not ranking.matches_subscribed_scholar(other, profile)
    assert ranking.matches_subscribed_scholar(chosen, profile)
    assert not ranking.matches_subscribed_scholar(legacy, profile)
    unresolved = Profile(user_id="one", scholars=frozenset({'heng wang'}), scholar_identities=({'key':'author:heng wang'},))
    assert not ranking.matches_subscribed_scholar(chosen, unresolved)


@pytest.mark.asyncio
async def test_author_search_returns_separate_institutions_and_ids(monkeypatch):
    async def handler(request):
        assert request.url.path == '/authors'
        return httpx.Response(200, json={'results':[
            {'id':'https://openalex.org/A123', 'display_name':'Heng Wang','last_known_institutions':[{'display_name':'University One'}], 'topics':[{'display_name':'Fluid mechanics'}]},
            {'id':'https://openalex.org/A456', 'display_name':'Heng Wang','last_known_institutions':[{'display_name':'University Two'}], 'topics':[{'display_name':'Medicine'}]}
        ]})
    original = httpx.AsyncClient
    monkeypatch.setattr(author_identity.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    rows = await author_identity.search('Heng Wang')
    assert [r['key'] for r in rows] == ['openalex:A123','openalex:A456']
    assert rows[0]['institution'] == 'University One'
    assert rows[1]['topics'] == ['Medicine']


@pytest.mark.asyncio
async def test_author_retrieval_uses_id_and_rechecks_authorships(monkeypatch):
    def work(aid):
        return {'title':'Paper '+aid,'publication_year':2026,'publication_date':'2026-09-01', 'authorships':[{'author':{'id':'https://openalex.org/'+aid,'display_name':'Heng Wang'}}]}
    async def handler(request):
        assert request.url.path == '/works'
        assert 'authorships.author.id:A123' in request.url.params['filter']
        assert 'raw_author_name' not in request.url.params['filter']
        return httpx.Response(200, json={'results':[work('A123'),work('A456')], 'meta':{}})
    original=httpx.AsyncClient
    monkeypatch.setattr(author_identity.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    rows=await author_identity.papers({'author_id':'A123'},since='2025-09-01')
    assert len(rows)==1
    assert rows[0].to_dict()['author_identities'][0]['author_id']=='A123'


@pytest.mark.asyncio
async def test_legacy_subscription_never_falls_back_to_name_search(monkeypatch):
    call=AsyncMock()
    monkeypatch.setattr(author_identity,'papers',call)
    assert await discovery._papers_by_author('Heng Wang',since='2025-09-01') == []
    call.assert_not_called()


def test_legacy_migration_binds_unique_only_and_exposes_ambiguous_candidates():
    from app.services.feed.scholar_migration import choose
    old={'key':'author:heng wang','name':'Heng Wang','source':'literature'}
    one={'key':'openalex:A123','author_id':'A123','name':'Heng Wang','search_complete':True,'identity_status':'resolved'}
    two={**one,'key':'openalex:A456','author_id':'A456'}
    assert choose(old,[one])['author_id']=='A123'
    ambiguous=choose(old,[one,two])
    assert ambiguous['key']==old['key']
    assert ambiguous['identity_status']=='ambiguous'
    assert len(ambiguous['identity_candidates'])==2
    assert choose(old,[{**one,'search_complete':False}])['identity_status']=='ambiguous'
    assert choose(old,[])['identity_status']=='unavailable'


@pytest.mark.asyncio
async def test_zenodo_scholar_work_requires_official_creator_match(monkeypatch):
    bad_doi = '10.5281/zenodo.20768587'
    good_doi = '10.5281/zenodo.12345'
    concept_doi = '10.5281/zenodo.12344'
    def work(doi):
        return {
            'title': 'A repository item', 'doi': 'https://doi.org/' + doi,
            'publication_year': 2026, 'publication_date': '2026-06-20',
            'authorships': [{'author': {'id': 'https://openalex.org/A123',
                                        'display_name': 'Stephen William Hawking'}}],
        }
    async def handler(request):
        if request.url.path == '/works':
            return httpx.Response(200, json={'results': [work(bad_doi), work(good_doi), work(concept_doi)], 'meta': {}})
        if request.url.path.endswith('/20768587'):
            return httpx.Response(200, json={'doi': bad_doi, 'metadata': {
                'creators': [{'name': 'Vintimilla-Carrasco, Paul'}]}})
        if request.url.path.endswith(('/12345', '/12344')):
            return httpx.Response(200, json={'doi': good_doi, 'conceptdoi': concept_doi,
                                             'metadata': {'creators': [
                                                 {'name': 'Hawking, Stephen William'}]}})
        raise AssertionError(request.url)
    original = httpx.AsyncClient
    monkeypatch.setattr(author_identity.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    rows = await author_identity.papers({'author_id': 'A123'}, since='2026-01-01')
    assert [row.doi for row in rows] == [good_doi, concept_doi]
    assert rows[0].verified_author_ids == ['A123']
    legacy = FeedItem(kind='paper', title='Wrong Zenodo author',
                      extra={'doi': bad_doi, 'author_identities': [{'author_id': 'A123'}]})
    profile = Profile(user_id='one', scholars=frozenset({'Stephen William Hawking'}),
                      scholar_identities=({'author_id': 'A123'},))
    assert not ranking.matches_subscribed_scholar(legacy, profile)
    legacy.extra['verified_author_ids'] = ['A123']
    assert ranking.matches_subscribed_scholar(legacy, profile)


@pytest.mark.asyncio
async def test_zenodo_author_check_fails_closed_on_unavailable_source(monkeypatch):
    async def handler(request):
        if request.url.path == '/works':
            return httpx.Response(200, json={'results': [{
                'title': 'Unverified', 'doi': 'https://doi.org/10.5281/zenodo.99',
                'authorships': [{'author': {'id': 'https://openalex.org/A123',
                                            'display_name': 'Stephen William Hawking'}}],
            }], 'meta': {}})
        return httpx.Response(503)
    original = httpx.AsyncClient
    monkeypatch.setattr(author_identity.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    assert await author_identity.papers({'author_id': 'A123'}, since='2026-01-01') == []
