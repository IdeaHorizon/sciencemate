import httpx
import pytest

from nodes.literature.tools.academic_categories import apply_budget, read_budget, sources_for_category
from nodes.literature.tools.academic_paging import PageClient


def papers(category, start, stop):
    return [{"title":f"{category} research {i}","doi":f"10.1/{category}/{i}","year":2026,
             "publication_category":category} for i in range(start,stop)]


def test_category_budgets_are_independent_and_count_unique_admitted_records():
    first,accepted=apply_budget(papers('期刊文章',0,250)+papers('学位论文',0,30),{},200)
    assert len(first)==230 and len(accepted['期刊文章'])==200
    second,accepted=apply_budget(papers('期刊文章',250,300)+papers('学位论文',20,250),accepted,200)
    assert not any(p['publication_category']=='期刊文章' for p in second)
    assert len(accepted['学位论文'])==200
    # Updates to retained identities are still returned at the cap.
    updated={**papers('期刊文章',0,1)[0],'abstract':'New metadata'}
    assert apply_budget([updated],accepted,200)[0]==[updated]


def test_categories_with_conflicting_dois_do_not_collapse_or_spend_other_budgets():
    rows=[{'title':'Identical title','year':2026,'doi':'10.1/a','publication_category':'书籍'},
          {'title':'Identical title','year':2026,'doi':'10.1/b','publication_category':'书籍'}]
    _,accepted=apply_budget(rows,{},200)
    assert len(accepted['书籍'])==2
    with pytest.raises(ValueError):read_budget({'per_category_limit':200,'accepted':{'学位论文':[{}]*201}},200)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,category,initial,expected',[
    ('crossref','学位论文',{'rows':20,'filter':'type:journal-article,from-pub-date:2020'},'type:dissertation'),
    ('crossref','会议文章',{'rows':20,'filter':'type:journal-article'},'type:proceedings-article'),
    ('openalex','学位论文',{'per-page':20,'filter':'title_and_abstract.search:research'},'type:dissertation'),
    ('openalex','中文期刊',{'per-page':20,'filter':'title_and_abstract.search:research'},'language:zh'),
])
async def test_category_filters_go_to_sources_before_pagination(source,category,initial,expected):
    seen=[]
    def respond(request):
        seen.append(dict(request.url.params))
        payload={'message':{'items':[],'total-results':0}} if source=='crossref' else {'results':[],'meta':{'count':0}}
        return httpx.Response(200,json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await PageClient(client,source,None,category).get('https://example.invalid/works',params=initial)
    assert expected in seen[0]['filter']
    if category=='学位论文':assert 'type:journal-article' not in seen[0]['filter']


def test_categories_do_not_wait_on_sources_which_only_supply_other_categories():
    all_sources={'crossref','openalex','pubmed','arxiv','biorxiv','medrxiv'}
    assert sources_for_category(all_sources,'学位论文')=={'crossref','openalex'}
    assert 'arxiv' in sources_for_category(all_sources,'预印本')
