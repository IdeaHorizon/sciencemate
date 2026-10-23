"""Translation failures must preserve completed fields and bound model work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import platform_runtime as runtime
from nodes.literature.tools import local_index
from app.api.v1 import literature
from app.schemas.literature import LiteratureTranslateIn


@pytest.mark.asyncio
async def test_slow_abstract_returns_completed_title_before_bridge_deadline(monkeypatch):
    monkeypatch.setattr(local_index, "get_index", lambda: None)
    monkeypatch.setattr(runtime, "_LITERATURE_TRANSLATION_TIMEOUT_SECONDS", 0.02)
    cancelled = []

    async def titles(papers, _index):
        papers[0]["title_zh"] = "兽医学"
        return {"status": "ok"}

    async def abstracts(papers, _index):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def summaries(papers, _index):
        assert papers == [], "feed must not spend model calls on unused summaries"
        return {"status": "not_needed"}

    monkeypatch.setattr(runtime, "_add_chinese_titles", titles)
    monkeypatch.setattr(runtime, "_add_chinese_abstracts", abstracts)
    monkeypatch.setattr(runtime, "_add_ai_summaries", summaries)
    result = await runtime.run_literature_translate_page({
        "papers": [{"key": "a", "title": "Veterinary medicine", "abstract": "Animal health."}],
        "generate_summaries": False,
    })
    assert result["status"] == "partial"
    assert result["translations"][0]["title_zh"] == "兽医学"
    assert result["diagnostics"]["abstracts"]["error_type"] == "TimeoutError"
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_failed_provider_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(local_index, "get_index", lambda: None)
    monkeypatch.setattr(runtime, "_add_chinese_titles", AsyncMock(return_value={"status": "failed"}))
    monkeypatch.setattr(runtime, "_add_chinese_abstracts", AsyncMock(return_value={"status": "failed"}))
    monkeypatch.setattr(runtime, "_add_ai_summaries", AsyncMock(return_value={"status": "not_needed"}))
    result = await runtime.run_literature_translate_page({"papers": [{"key": "a", "title": "Veterinary medicine"}]})
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_api_passes_feed_summary_option_to_runtime(monkeypatch):
    monkeypatch.setattr(literature, "select_effective_backend", AsyncMock(return_value=SimpleNamespace()))
    call = AsyncMock(return_value={"translations": [], "status": "ok"})
    monkeypatch.setattr(literature.bridge, "call", call)
    payload = LiteratureTranslateIn(papers=[{"key": "a", "title": "Veterinary medicine"}], generate_summaries=False)
    await literature.translate_literature_page(payload, user=SimpleNamespace(), db=AsyncMock())
    assert call.await_args.kwargs["payload"]["generate_summaries"] is False


@pytest.mark.asyncio
async def test_persistent_full_translation_cache_skips_model_on_repeat(monkeypatch, tmp_path):
    from core.llm import LLMClient
    index = local_index.LocalPaperIndex(str(tmp_path / 'cache.sqlite3'))
    title = 'Irregular particle settling'
    abstract = 'A complete particle sedimentation abstract. ' * 150
    index.save_title_translations({title: '不规则颗粒沉降'})
    index.save_abstract_translations({abstract: '完整沉降摘要译文'})
    monkeypatch.setattr(local_index, 'get_index', lambda: index)
    chat = AsyncMock(side_effect=AssertionError('cached translations must not call the model'))
    monkeypatch.setattr(LLMClient, 'chat', chat)
    payload = {'papers': [{'key': 'feed-id', 'title': title, 'abstract': abstract, 'doi': '10.1/cached'}], 'generate_summaries': False}
    first = await runtime.run_literature_translate_page(payload)
    payload['papers'][0]['key'] = 'academic-search-id'
    second = await runtime.run_literature_translate_page(payload)
    assert first['translations'][0]['abstract_zh'] == second['translations'][0]['abstract_zh'] == '完整沉降摘要译文'
    assert second['diagnostics']['titles']['status'] == second['diagnostics']['abstracts']['status'] == 'cache_hit'
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_feed_response_attaches_shared_cache_without_model(monkeypatch, tmp_path):
    from app.api.v1 import feed
    from app.services.feed import literature_projection
    from app.schemas.feed import FeedItemOut
    root = tmp_path / 'papers'
    root.mkdir()
    index = local_index.LocalPaperIndex(str(root / 'literature_catalog.sqlite3'))
    index.save_title_translations({'Particle sedimentation': '颗粒沉降'})
    index.save_abstract_translations({'Fluid simulation.': '流体模拟。'})
    monkeypatch.setattr(literature_projection, 'literature_papers_root', lambda: root)
    items = [FeedItemOut(id='a', kind='paper', title='Particle sedimentation', summary='Fluid simulation.')]
    await feed._attach_cached_translations(items)
    assert items[0].title_zh == '颗粒沉降'
    assert items[0].abstract_zh == '流体模拟。'
