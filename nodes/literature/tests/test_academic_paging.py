"""Pagination contracts use controlled source responses, never public traffic."""
import json
import pytest
import httpx

from nodes.literature.tools.academic_paging import PageClient, PagingError, query_key, validate_continuation


@pytest.mark.asyncio
async def test_crossref_continues_past_a_full_page_and_accepts_stateful_same_cursor():
    requests=[]
    def respond(request):
        requests.append(dict(request.url.params))
        n=len(requests)
        rows=[{'DOI':f'10.1/{n}-{i}'} for i in range(2 if n<3 else 1)]
        return httpx.Response(200,json={'message':{'items':rows,'total-results':5,'next-cursor':'opaque'}})
    state=None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for _ in range(3):
            proxy=PageClient(client,'crossref',state)
            await proxy.get('https://api.crossref.org/works',params={'query':'particles','rows':2})
            state=proxy.next_state
    assert state is None
    assert [r['cursor'] for r in requests]==['*','opaque','opaque']
    assert all(r['query']=='particles' for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,params,payload,field,expected',[
    ('openalex',{'per-page':50},{'meta':{'count':300,'next_cursor':'oa-next'},'results':[{'id':'W1'}]},'cursor','oa-start'),
    ('biorxiv',{'pageSize':1},{'hitCount':300,'nextCursorMark':'epmc-next','resultList':{'result':[{'id':'P1'}]}},'cursorMark','epmc-start'),
    ('pubmed',{'retmax':1},{'esearchresult':{'count':'300','idlist':['PM1']}},'retstart','200'),
    ('semantic_scholar',{'limit':1},{'total':300,'next':201,'data':[{'paperId':'S1'}]},'offset','200'),
])
async def test_source_owned_paging_parameters_are_preserved(source,params,payload,field,expected,monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY','controlled-test-key')
    seen=[]
    def respond(request):seen.append(dict(request.url.params));return httpx.Response(200,json=payload)
    token='oa-start' if source=='openalex' else 'epmc-start'
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy=PageClient(client,source,{'offset':200,'token':token})
        await proxy.get('https://example.invalid/esearch.fcgi',params=params)
    assert seen[0][field]==expected and proxy.next_state['offset']==201


@pytest.mark.asyncio
async def test_openalex_without_key_uses_public_pages_and_reports_source_boundary(monkeypatch):
    monkeypatch.delenv('OPENALEX_API_KEY',raising=False)
    seen=[]
    def respond(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200,json={'meta':{'count':20000},'results':[{'id':f'W{i}'} for i in range(100)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy=PageClient(client,'openalex',{'offset':9900})
        await proxy.get('https://api.openalex.org/works',params={'per-page':100})
        assert seen[0]['page']=='100' and proxy.next_state['offset']==10000
        with pytest.raises(PagingError,match='API key'):
            await PageClient(client,'openalex',proxy.next_state).get('https://api.openalex.org/works',params={'per-page':100})
    assert len(seen)==1


@pytest.mark.asyncio
async def test_repeated_page_is_an_error_not_an_infinite_loop_or_success():
    def respond(request):return httpx.Response(200,json={'message':{'items':[{'DOI':'same'}],'total-results':100,'next-cursor':'same'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first=PageClient(client,'crossref',None)
        await first.get('https://api.crossref.org/works',params={'rows':1})
        with pytest.raises(PagingError,match='repeated'):
            await PageClient(client,'crossref',first.next_state).get('https://api.crossref.org/works',params={'rows':1})


def test_continuations_are_bound_to_the_original_question():
    state={'version':1,'query':query_key('question'),'groups':{}}
    assert validate_continuation(state,'question')=={}
    with pytest.raises(ValueError):validate_continuation(state,'another question')


def test_unlimited_output_keeps_every_eligible_paper_and_still_filters_nonresearch():
    from nodes.literature.tools.academic_search import _select_academic_search_results
    papers=[{'title':f'Original study {i}','exact_title_match':True} for i in range(350)]
    papers.append({'title':'Editorial Board','exact_title_match':True})
    selected,excluded=_select_academic_search_results(papers,limit=None)
    assert len(selected)==350 and excluded==1
