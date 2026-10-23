import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.models.feed import FeedItem
from app.models.user import User
from app.schemas.feed import FeedTodayOut
from app.services.feed import abstract_backfill, pool_cache
from nodes.literature.tools import local_index
import platform_runtime as runtime


@pytest.mark.asyncio
async def test_pool_cache_isolates_users_and_invalidates_committed_feed_edits(db_session):
    pool_cache.clear()
    user = User(email="speed@test.local", display_name="Speed", hashed_password="x", preferences={})
    db_session.add(user)
    await db_session.commit()
    build = AsyncMock(return_value=FeedTodayOut())
    await pool_cache.get_pool(db_session, user, build)
    await pool_cache.get_pool(db_session, user, build)
    assert build.call_count == 1
    user.preferences = {"feed": {"domains": ["081401"]}}
    await pool_cache.get_pool(db_session, user, build)
    assert build.call_count == 2
    await db_session.commit()
    item = FeedItem(canonical_key="speed:1", kind="paper", title="Study", domains=[], authors=[], extra={})
    db_session.add(item)
    await db_session.commit()
    await pool_cache.get_pool(db_session, user, build)
    assert build.call_count == 3
    item.summary = "New abstract"
    await db_session.commit()
    await pool_cache.get_pool(db_session, user, build)
    assert build.call_count == 4
    other = User(email="other-speed@test.local", display_name="Other", hashed_password="x", preferences={})
    db_session.add(other)
    await db_session.commit()
    await pool_cache.get_pool(db_session, other, build)
    assert build.call_count == 5


@pytest.mark.asyncio
async def test_combined_translation_has_no_history_skips_cached_fields_and_reuses_cache(tmp_path, monkeypatch):
    from core.llm import LLMClient
    index = local_index.LocalPaperIndex(str(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(local_index, "get_index", lambda: index)
    index.save_title_translations({"Cached title": "已缓存标题"})
    calls = []
    async def chat(self, messages, **kwargs):
        calls.append(messages)
        assert len(messages) == 2
        rows = json.loads(messages[1].content)
        assert "title" not in rows[0]
        assert rows[0]["abstract"] == "A source abstract."
        return SimpleNamespace(content=json.dumps([{"id": row["id"], **({"title_zh": "新标题"} if "title" in row else {}), **({"abstract_zh": "完整原文摘要译文。"} if "abstract" in row else {})} for row in rows]), usage={"prompt_tokens": 30, "completion_tokens": 50})
    monkeypatch.setattr(LLMClient, "chat", chat)
    payload = {"papers": [{"key": "a", "title": "Cached title", "abstract": "A source abstract."}, {"key": "b", "title": "New title"}], "generate_summaries": False, "combine_translation": True}
    first = await runtime.run_literature_translate_page(payload)
    assert first["status"] == "ok"
    assert first["diagnostics"]["combined"]["history_messages"] == 0
    assert first["diagnostics"]["combined"]["usage"]["prompt_tokens"] == 30
    payload["papers"][0]["key"] = "after-refresh"
    second = await runtime.run_literature_translate_page(payload)
    assert len(calls) == 1
    assert second["diagnostics"]["titles"]["status"] == "cache_hit"
    assert second["diagnostics"]["abstracts"]["status"] == "cache_hit"


@pytest.mark.asyncio
async def test_abstract_fast_source_does_not_wait_for_slow_source_and_failure_cache_is_reused(monkeypatch):
    cancelled = []
    calls = []
    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "api.crossref.org":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return httpx.Response(200, json={"abstract_inverted_index": {"Useful": [0], "abstract.": [1]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        record = {"doi": "10.1/test", "venue": "Acta Geotechnica"}
        result = await asyncio.wait_for(abstract_backfill._lookup(record, client), timeout=1)
    assert result["abstract"] == "Useful abstract."
    assert result["metadata_provenance"]["abstract"] == "openalex"
    assert cancelled
    assert abstract_backfill._source_order(record)[0] == "crossref"
    assert abstract_backfill._source_order(record, "publisher_landing")[0] == "publisher_landing"
    assert abstract_backfill._source_order({"venue": "New England Journal of Medicine"})[0] == "pubmed"


@pytest.mark.asyncio
async def test_per_source_negative_cache_retries_only_transient_source():
    from datetime import UTC, datetime, timedelta
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    record = {"doi": "10.1/retry", "venue": "Engineering", "abstract_lookup_source_state": {
        "crossref": {"result": "no_abstract", "retry_after": future},
        "openalex": {"result": "http_429", "retry_after": old},
        "pubmed": {"result": "no_abstract", "retry_after": future},
        "publisher_landing": {"result": "no_abstract", "retry_after": future},
    }}
    requests = []
    async def handler(request):
        requests.append(request.url.host)
        return httpx.Response(429)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await abstract_backfill._lookup(record, client)
    assert requests == ["api.openalex.org"]
    assert not result["abstract_lookup_completed"]
    assert result["abstract_lookup_source_state"]["crossref"]["retry_after"] == future
    assert result["abstract_lookup_retry_after"] < future


@pytest.mark.asyncio
async def test_four_translation_batches_start_concurrently(db_session, monkeypatch):
    from app.api.v1 import feed, literature
    from app.schemas.feed import FeedCard, FeedItemOut
    from app.services.feed import translation
    user = User(email="parallel-speed@test.local", display_name="Parallel", hashed_password="x", is_active=True)
    db_session.add(user)
    await db_session.commit()
    class Session:
        async def __aenter__(self): return db_session
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(translation, "get_session_factory", lambda: Session)
    monkeypatch.setattr(translation, "select_effective_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(translation, "resolved_api_key", lambda backend: "test-credential")
    monkeypatch.setattr(translation.settings, "feed_translation_concurrency", 4)
    cards = [FeedCard(item=FeedItemOut(id=str(i), kind="paper", title=f"Study {i}")) for i in range(12)]
    monkeypatch.setattr(feed, "preparing_translation_pool", AsyncMock(return_value=FeedTodayOut()))
    monkeypatch.setattr(feed, "feed_today", AsyncMock(return_value=FeedTodayOut(feed=cards)))
    entered, active, maximum = [], 0, 0
    all_started = asyncio.Event()
    async def translate(payload, **kwargs):
        nonlocal active, maximum
        assert payload.combine_translation
        assert len(payload.papers) == 3
        active += 1
        maximum = max(maximum, active)
        entered.extend(p.key for p in payload.papers)
        if active == 4:
            all_started.set()
        await all_started.wait()
        active -= 1
        return SimpleNamespace(status="ok", diagnostics={})
    monkeypatch.setattr(literature, "translate_literature_page", translate)
    assert await asyncio.wait_for(translation.FeedTranslationWorker().run_once(), timeout=2) == 12
    assert maximum == 4
    assert len(set(entered)) == 12


@pytest.mark.asyncio
async def test_ready_pagination_skips_untranslated_and_filters_sources_before_offset(monkeypatch):
    from app.api.v1 import feed
    from app.schemas.feed import FeedCard, FeedItemOut
    cards = [FeedCard(item=FeedItemOut(id=str(i), kind="paper", title=f"Paper {i}", title_zh=None if i < 10 else "已翻译"),
                      recommendation_sources=["domain" if i % 2 == 0 else "project"]) for i in range(20)]
    monkeypatch.setattr(feed, "recommendation_pool", AsyncMock(side_effect=lambda *args, **kwargs: FeedTodayOut(feed=cards)))
    monkeypatch.setattr(feed.ranking, "suppressed_ids", AsyncMock(return_value=frozenset()))
    user = SimpleNamespace(id="pagination-user")
    page = await feed.feed_today(limit=2, offset=2, ready_only=True, sources="domain", user=user, db=None)
    assert [c.item.id for c in page.feed] == ["14", "16"]
    assert page.has_more
    pending_pool = await feed.feed_today(limit=100, offset=0, ready_only=False, user=user, db=None)
    assert len(pending_pool.feed) == 20


@pytest.mark.asyncio
async def test_scrolling_exposure_keeps_pool_and_new_articles_allow_fast_snapshot(db_session):
    from app.models.feed import FeedEngagement
    pool_cache.clear()
    user = User(email="scroll-speed@test.local", display_name="Scroll", hashed_password="x", preferences={})
    item = FeedItem(canonical_key="scroll:1", kind="paper", title="Study", domains=[], authors=[], extra={})
    db_session.add_all([user, item])
    await db_session.commit()
    from app.schemas.feed import FeedCard, FeedItemOut
    build = AsyncMock(return_value=FeedTodayOut(feed=[FeedCard(item=FeedItemOut(id="cached", kind="paper", title="已准备文章"))]))
    await pool_cache.get_pool(db_session, user, build)
    db_session.add(FeedEngagement(user_id=user.id, item_id=item.id, action="impression"))
    await db_session.commit()
    await pool_cache.get_pool(db_session, user, build)
    assert build.call_count == 1
    item.summary = "Newly prepared abstract"
    await db_session.commit()
    await pool_cache.get_pool(db_session, user, build, allow_stale=True)
    assert build.call_count == 1
    await pool_cache.get_pool(db_session, user, build, allow_stale=False)
    assert build.call_count == 2
    db_session.add(FeedEngagement(user_id=user.id, item_id=item.id, action="dismiss"))
    await db_session.commit()
    await pool_cache.get_pool(db_session, user, build, allow_stale=True)
    assert build.call_count == 3
