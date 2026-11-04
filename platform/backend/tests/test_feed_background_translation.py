from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.user import User
from app.schemas.feed import FeedCard, FeedItemOut, FeedTodayOut
from app.services.feed import translation


def test_translation_readiness_requires_existing_source_fields():
    item = FeedItemOut(id="one", kind="paper", title="Particle settling", summary="Fluid simulation.")
    assert not translation.translation_ready(item)
    item.title_zh = "颗粒沉降"
    assert not translation.translation_ready(item)
    item.abstract_zh = "流体模拟。"
    assert translation.translation_ready(item)
    item.summary = None
    item.abstract_zh = None
    assert translation.translation_ready(item)


@pytest.mark.asyncio
async def test_worker_translates_beyond_first_page_and_cooldown_does_not_block_others(db_session, monkeypatch):
    from app.api.v1 import feed, literature
    user = User(email="background-translation@test.local", display_name="Test", hashed_password="x", is_active=True)
    db_session.add(user)
    await db_session.commit()
    class Session:
        async def __aenter__(self): return db_session
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(translation, "get_session_factory", lambda: Session)
    monkeypatch.setattr(translation, "select_effective_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(translation, "resolved_api_key", lambda backend: "test-credential")
    cards = [FeedCard(item=FeedItemOut(id=str(i), kind="paper", title=f"Study {i}",
                                      title_zh="已缓存" if i < 21 else None),
                      recommendation_sources=["domain"]) for i in range(30)]
    view = AsyncMock(return_value=FeedTodayOut(feed=cards))
    monkeypatch.setattr(feed, "preparing_translation_pool", AsyncMock(return_value=FeedTodayOut()))
    monkeypatch.setattr(feed, "feed_today", view)
    translate = AsyncMock(return_value=SimpleNamespace(status="failed", diagnostics={}))
    monkeypatch.setattr(literature, "translate_literature_page", translate)
    monkeypatch.setattr(translation.settings, "feed_translation_concurrency", 1)
    worker = translation.FeedTranslationWorker()
    assert await worker.run_once() == 3
    assert [p.key for p in translate.call_args.args[0].papers] == ["21", "22", "23"]
    assert view.call_args.kwargs["limit"] > 30
    assert await worker.run_once() == 3
    assert [p.key for p in translate.call_args.args[0].papers] == ["24", "25", "26"]


@pytest.mark.asyncio
async def test_worker_translates_with_a_self_hosted_endpoint_that_needs_no_key(db_session, monkeypatch):
    """自建端点（填了 base_url）不鉴权：以前 worker 见没有 key 就跳过这个人，推荐池永远等不到译文。"""
    from app.api.v1 import feed, literature
    user = User(email="keyless-translation@test.local", display_name="Test", hashed_password="x", is_active=True)
    db_session.add(user)
    await db_session.commit()
    class Session:
        async def __aenter__(self): return db_session
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(translation, "get_session_factory", lambda: Session)
    keyless = SimpleNamespace(base_url="http://127.0.0.1:18495/v1")
    monkeypatch.setattr(translation, "select_effective_backend", AsyncMock(return_value=keyless))
    monkeypatch.setattr(translation, "resolved_api_key", lambda backend: None)
    cards = [FeedCard(item=FeedItemOut(id="keyless-1", kind="paper", title="Particle settling"),
                      recommendation_sources=["domain"])]
    monkeypatch.setattr(feed, "preparing_translation_pool", AsyncMock(return_value=FeedTodayOut()))
    monkeypatch.setattr(feed, "feed_today", AsyncMock(return_value=FeedTodayOut(feed=cards)))
    translate = AsyncMock(return_value=SimpleNamespace(status="ok", diagnostics={}))
    monkeypatch.setattr(literature, "translate_literature_page", translate)
    assert await translation.FeedTranslationWorker().run_once() == 1
    assert [p.key for p in translate.call_args.args[0].papers] == ["keyless-1"]
