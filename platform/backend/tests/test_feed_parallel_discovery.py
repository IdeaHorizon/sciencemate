import asyncio
from types import SimpleNamespace
import pytest
from app.services.feed import discovery


@pytest.mark.asyncio
async def test_paper_and_web_lanes_start_together_and_use_separate_sessions(monkeypatch):
    started = set()
    sessions = []
    both_started = asyncio.Event()
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def commit(self): pass
    monkeypatch.setattr('app.database.get_session_factory', lambda: Session)
    async def lane(client, db, *, user, queries, news_queries, lane):
        started.add(lane)
        sessions.append(db)
        if len(started) == 2: both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return {'paper': 2, 'web': 3}[lane]
    monkeypatch.setattr(discovery, '_targeted_search_lane', lane)
    assert await discovery._targeted_search(None, None, user=SimpleNamespace(id='u'), queries=['q']) == 5
    assert sessions[0] is not sessions[1]
