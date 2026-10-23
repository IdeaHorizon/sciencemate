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
                                      title_zh="已缓存" if i < 21 else None)) for i in range(30)]
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
