import json
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import literature
from app.schemas.literature import LiteratureSearchIn

PLAN=[{'label':'主题','en':'"Bragg reflection"','zh':'"布拉格反射"'}]


@pytest.mark.asyncio
async def test_plan_endpoint_and_stream_preserve_both_languages(monkeypatch):
    monkeypatch.setattr(literature,'select_effective_backend',AsyncMock(return_value=object()))
    monkeypatch.setattr(literature,'_school_search',AsyncMock(return_value=[]))
    async def bridge(**kwargs):
        if kwargs['op']=='literature_search_plan':
            return {'type':'literature_search_plan_result','at':'now','query':'布拉格反射','search_plan':PLAN}
        await kwargs['on_progress']({'type':'literature_search_progress','stage':'search_plan','search_plan':PLAN})
        return {'type':'literature_search_result','query':'布拉格反射','search_plan':PLAN,'papers':[]}
    monkeypatch.setattr(literature.bridge,'call',bridge)
    payload=LiteratureSearchIn(query='布拉格反射')
    result=await literature.plan_literature(payload,user=object(),db=AsyncMock())
    assert result.search_plan[0].zh=='"布拉格反射"'
    response=await literature.search_literature_stream(payload,user=object(),db=AsyncMock())
    events=[json.loads(chunk.removeprefix('data: ')) async for chunk in response.body_iterator if chunk.startswith('data: ')]
    planned=next(e for e in events if e.get('stage')=='search_plan')
    assert planned['search_plan']==PLAN
    assert events[-1]['type']=='done' and events[-1]['search_plan']==PLAN


@pytest.mark.asyncio
async def test_plan_failure_is_explicit(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(literature,'select_effective_backend',AsyncMock(return_value=object()))
    monkeypatch.setattr(literature.bridge,'call',AsyncMock(side_effect=literature.bridge.BridgeUnavailable('模型超时')))
    with pytest.raises(HTTPException,match='模型超时'):
        await literature.plan_literature(LiteratureSearchIn(query='研究'),user=object(),db=AsyncMock())


@pytest.mark.asyncio
async def test_stream_passes_unlimited_continuation_in_both_directions(monkeypatch):
    monkeypatch.setattr(literature,'select_effective_backend',AsyncMock(return_value=object()))
    monkeypatch.setattr(literature,'_school_search',AsyncMock(return_value=[]))
    cursor={'version':1,'query':'test','groups':{}}
    async def bridge(**kwargs):
        assert kwargs['payload']['limit'] is None
        assert kwargs['payload']['continuation']==cursor
        assert kwargs['payload']['pagination_scope']=='query'
        assert kwargs['payload']['per_category_limit']==200
        return {'query':'问题','papers':[],'continuation':cursor}
    monkeypatch.setattr(literature.bridge,'call',bridge)
    response=await literature.search_literature_stream(
        LiteratureSearchIn(query='问题',continuation=cursor,pagination_scope='query'),user=object(),db=AsyncMock())
    events=[json.loads(chunk.removeprefix('data: ')) async for chunk in response.body_iterator]
    assert events[-1]['type']=='done' and events[-1]['continuation']==cursor


@pytest.mark.asyncio
async def test_closing_stream_cancels_inflight_source_work(monkeypatch):
    import asyncio
    monkeypatch.setattr(literature,'select_effective_backend',AsyncMock(return_value=object()))
    monkeypatch.setattr(literature,'_school_search',AsyncMock(return_value=[]))
    cancelled=asyncio.Event()
    async def bridge(**kwargs):
        try:
            await kwargs['on_progress']({'stage':'search','detail':'working'})
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(literature.bridge,'call',bridge)
    response=await literature.search_literature_stream(
        LiteratureSearchIn(query='问题'),user=object(),db=AsyncMock())
    await anext(response.body_iterator)
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert cancelled.is_set()
