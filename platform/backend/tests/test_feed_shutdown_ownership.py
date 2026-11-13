import asyncio

import pytest

from app.services.feed import scheduler


@pytest.mark.asyncio
async def test_stop_allows_an_inflight_round_to_close_its_database_session():
    collector = scheduler.FeedCollector()
    entered, release, closed = (asyncio.Event() for _ in range(3))

    async def round_():
        entered.set()
        await release.wait()
        closed.set()

    worker = collector._subscription_asset_task = asyncio.create_task(round_())
    await entered.wait()
    stopping = asyncio.create_task(collector.stop())
    try:
        await asyncio.sleep(0)
        release.set()
        await stopping
        assert closed.is_set(), "stop cancelled a short DB round before its session could close"
        assert worker.done()
    finally:
        release.set()
        await stopping


@pytest.mark.asyncio
async def test_shutdown_still_cancels_and_joins_a_stuck_worker(monkeypatch):
    collector = scheduler.FeedCollector()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stuck():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(scheduler, "SHUTDOWN_GRACE_SECONDS", 0.01, raising=False)
    worker = collector._subscription_asset_task = asyncio.create_task(stuck())
    await entered.wait()
    await collector.stop()
    assert worker.done() and cancelled.is_set()
