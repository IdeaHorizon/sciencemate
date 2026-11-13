"""Explicit follows survive busy collectors and desktop restarts."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database import get_session_factory
from app.models.user import User
from app.services.feed import discovery, scheduler


FIRST = {"key": "openalex:A123", "author_id": "A123", "name": "Researcher One"}
SECOND = {"key": "openalex:A456", "author_id": "A456", "name": "Researcher Two"}


@pytest.mark.asyncio
async def test_online_only_harvester_stop_drains_its_asset_worker():
    from app.services.literature_harvester import LiteratureIndexHarvester

    harvester = LiteratureIndexHarvester()
    entered, settled = asyncio.Event(), asyncio.Event()

    async def assets():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    task = harvester._asset_task = asyncio.create_task(assets())
    await entered.wait()
    try:
        assert harvester._task is None
        await harvester.stop()
        assert settled.is_set() and task.done()
        assert harvester._asset_task is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def save_scholars(user_id, entries):
    async with get_session_factory()() as db:
        user = await db.get(User, user_id)
        prefs = dict(user.preferences or {})
        feed = dict(prefs.get("feed") or {})
        feed["subscriptions"] = {"journals": [], "scholars": entries}
        prefs["feed"] = feed
        user.preferences = prefs
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["domain", "subscription"])
async def test_stop_drains_superseded_refresh_without_cancelling_cleanup_again(monkeypatch, kind):
    from app.services.literature_harvester import LiteratureIndexHarvester

    harvester = LiteratureIndexHarvester()
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def refresh(**kwargs):
        if not started.is_set():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()
        else:
            await asyncio.Event().wait()

    monkeypatch.setattr(settings, "literature_harvester_enabled", True)
    monkeypatch.setattr(harvester, "_run_online_refresh", refresh)
    monkeypatch.setattr(harvester, "run_once", refresh)

    def request(value):
        if kind == "domain":
            return harvester.request_online_refresh(user_id="owner", domains=[value])
        return harvester.request_subscription_refresh(user_id="owner", journals=[{"name": value}])

    request("first")
    await started.wait()
    request("second")
    await cleaning.wait()
    stop = asyncio.create_task(harvester.stop())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop.done(), "stop discarded a superseded task still closing its session"
        release.set()
        await stop
        assert finished.is_set(), "a second cancellation interrupted session cleanup"
        assert not harvester._retiring_online_tasks
    finally:
        release.set()
        await stop


@pytest.mark.asyncio
async def test_discovery_progress_cannot_overwrite_a_concurrent_follow(subscriber, monkeypatch):
    await save_scholars(subscriber, [FIRST])
    monkeypatch.setattr(discovery, "discover_for_subscriptions", AsyncMock(return_value=0))
    async with get_session_factory()() as db:
        user = await db.get(User, subscriber)
        refresh = db.refresh
        calls = 0

        async def interleaved_refresh(instance, *args, **kwargs):
            nonlocal calls
            await refresh(instance, *args, **kwargs)
            calls += 1
            if calls == 2:
                # The worker has read its progress snapshot; the next user
                # follow commits before the worker writes that snapshot back.
                await save_scholars(subscriber, [FIRST, SECOND])

        monkeypatch.setattr(db, "refresh", interleaved_refresh)
        await discovery._discover_subscription_batch(
            db, user=user, journals=[], scholars=[FIRST], complete_assets=False,
        )
    async with get_session_factory()() as db:
        saved = await db.get(User, subscriber)
        feed = saved.preferences["feed"]
        assert feed["subscriptions"]["scholars"] == [FIRST, SECOND]
        assert feed["subscription_discovery"]["scholars:openalex:a123"]["archived"] == 0


@pytest.fixture
async def collector(monkeypatch, db_engine):
    value = scheduler.FeedCollector()
    monkeypatch.setattr(scheduler, "feed_collector", value)
    monkeypatch.setattr(settings, "feed_collector_enabled", True)
    monkeypatch.setattr(settings, "feed_external_fetch_enabled", True)
    monkeypatch.setattr(discovery, "run_subscription_asset_round", AsyncMock(return_value={"completed": 0}))
    monkeypatch.setattr(discovery, "complete_subscription_assets_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.feed.translation.FeedTranslationWorker.run_once", AsyncMock(return_value=0))
    try:
        yield value
    finally:
        # The collector owns DB sessions on background tasks. Drain it before
        # db_engine drops this test's tables, including assertion/setup failures.
        await value.stop()


@pytest.fixture
async def subscriber(db_session):
    user = User(id="aaaa1111-1111-4111-8111-111111111111", email="follow@test.local",
                display_name="Follow", hashed_password="x", is_active=True, preferences={})
    db_session.add(user)
    await db_session.commit()
    return str(user.id)


@pytest.mark.asyncio
async def test_follow_during_collection_is_persisted_and_runs_next(subscriber, collector, monkeypatch):
    first_started, release_first, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def discover(db, *, user, journals, scholars):
        name = scholars[0]["name"]
        calls.append(name)
        if name == FIRST["name"]:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return 0

    monkeypatch.setattr(discovery, "discover_for_subscriptions", discover)
    try:
        await save_scholars(subscriber, [FIRST])
        assert discovery.request_subscription_discovery(user_id=subscriber, scholars=[FIRST])
        await asyncio.wait_for(first_started.wait(), 2)
        await save_scholars(subscriber, [FIRST, SECOND])
        assert discovery.request_subscription_discovery(user_id=subscriber, scholars=[SECOND])
        # Let the second request reach a busy worker before the first finishes.
        await asyncio.sleep(0.05)
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 2)
        assert calls == [FIRST["name"], SECOND["name"]]
        assert collector._task is None  # no unsolicited public feed collection
        assert collector._translation_task is not None
    finally:
        release_first.set()
        await collector.stop()


@pytest.mark.asyncio
async def test_restart_resumes_saved_subscriptions_but_empty_install_stays_idle(subscriber, collector, monkeypatch):
    started = asyncio.Event()

    async def discover(*args, **kwargs):
        started.set()
        return 0

    monkeypatch.setattr(discovery, "discover_for_subscriptions", discover)
    try:
        assert not await collector.resume_saved_subscriptions()
        assert collector._subscription_task is None
        await save_scholars(subscriber, [FIRST])
        assert await collector.resume_saved_subscriptions()
        await asyncio.wait_for(started.wait(), 2)
        task = collector._subscription_task
        assert await collector.resume_saved_subscriptions()
        assert collector._subscription_task is task
        assert collector._task is None
        assert collector._journal_browser_task is None
    finally:
        await collector.stop()


@pytest.mark.asyncio
async def test_slow_assets_do_not_block_a_later_follow(subscriber, collector, monkeypatch):
    assets_started, release_assets, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def discover(db, *, user, journals, scholars):
        if scholars[0]["key"] == SECOND["key"]:
            second_started.set()
        return 1

    async def assets():
        assets_started.set()
        await release_assets.wait()
        return {"completed": 0}

    monkeypatch.setattr(discovery, "discover_for_subscriptions", discover)
    monkeypatch.setattr(discovery, "run_subscription_asset_round", assets)
    try:
        await save_scholars(subscriber, [FIRST])
        discovery.request_subscription_discovery(user_id=subscriber, scholars=[FIRST])
        await asyncio.wait_for(assets_started.wait(), 2)
        await save_scholars(subscriber, [FIRST, SECOND])
        discovery.request_subscription_discovery(user_id=subscriber, scholars=[SECOND])
        try:
            await asyncio.wait_for(second_started.wait(), 2)
        except TimeoutError:
            async with get_session_factory()() as reader:
                saved = await reader.get(User, subscriber)
                pytest.fail(f"Second follow did not run; saved preferences: {saved.preferences!r}")
    finally:
        release_assets.set()
        await collector.stop()


@pytest.mark.asyncio
async def test_collection_configuration_is_respected(subscriber, collector, monkeypatch):
    await save_scholars(subscriber, [FIRST])
    monkeypatch.setattr(settings, "feed_external_fetch_enabled", False)
    assert not await collector.resume_saved_subscriptions()
    assert not discovery.request_subscription_discovery(user_id=subscriber, scholars=[FIRST])
    assert collector._subscription_task is None


@pytest.mark.asyncio
async def test_unfollowed_waiting_target_is_not_fetched(subscriber, collector, monkeypatch):
    first_started, release_first, round_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def discover(db, *, user, journals, scholars):
        calls.append(scholars[0]["key"])
        first_started.set()
        await release_first.wait()
        return 0

    original = discovery.run_subscription_round

    async def round_():
        result = await original()
        round_done.set()
        return result

    monkeypatch.setattr(discovery, "discover_for_subscriptions", discover)
    monkeypatch.setattr(discovery, "run_subscription_round", round_)
    try:
        await save_scholars(subscriber, [FIRST, SECOND])
        discovery.request_subscription_discovery(user_id=subscriber, scholars=[FIRST, SECOND])
        await asyncio.wait_for(first_started.wait(), 2)
        await save_scholars(subscriber, [FIRST])
        release_first.set()
        await asyncio.wait_for(round_done.wait(), 2)
        assert calls == [FIRST["key"]]
    finally:
        release_first.set()
        await collector.stop()


@pytest.mark.asyncio
async def test_subscription_api_commits_before_waking_workers_and_requeues_a_refollow(subscriber, monkeypatch):
    from datetime import UTC, datetime
    from app.api.v1.feed import put_subscriptions
    from app.schemas.feed import SubscriptionsWrite

    async with get_session_factory()() as db:
        user = await db.get(User, subscriber)
        user.preferences = {"feed": {"subscription_discovery": {
            "scholars:openalex:a123": {"attempted_at": datetime.now(UTC).isoformat(), "error": None}
        }}}
        await db.commit()

        def wake(**kwargs):
            assert not db.in_transaction(), "worker was woken before subscriptions committed"
            return True

        monkeypatch.setattr(discovery, "request_subscription_discovery", wake)
        await put_subscriptions(SubscriptionsWrite(scholars=[FIRST]), user=user, db=db)
        async with get_session_factory()() as reader:
            saved = await reader.get(User, subscriber)
            assert saved.preferences["feed"]["subscriptions"]["scholars"][0]["author_id"] == "A123"
            assert discovery.due_subscription_targets(saved)[1][0]["author_id"] == "A123"


@pytest.mark.asyncio
async def test_domain_only_restart_resumes_assets_and_translation(subscriber, collector, monkeypatch):
    """A field follow needs preparation even without any journal/author follows."""
    async with get_session_factory()() as db:
        user = await db.get(User, subscriber)
        user.preferences = {"feed": {"domains": ["cas:0701"]}}
        await db.commit()
    prepared = asyncio.Event()

    async def assets():
        prepared.set()
        return {"completed": 0}

    monkeypatch.setattr(discovery, "run_subscription_asset_round", assets)
    try:
        assert await collector.resume_saved_subscriptions()
        await asyncio.wait_for(prepared.wait(), 2)
        assert collector._translation_task is not None
        assert collector._task is None
        assert collector._journal_browser_task is None
    finally:
        await collector.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_selected", [False, True], ids=["new-field", "existing-field"])
async def test_field_save_commits_before_starting_preparation(
    subscriber, collector, monkeypatch, harness_domain_registry, already_selected
):
    from app.api.v1.feed import put_interests
    from app.schemas.feed import InterestsWrite
    from app.services.literature_harvester import literature_index_harvester

    prepared = asyncio.Event()
    requests = []

    async def assets():
        async with get_session_factory()() as reader:
            saved = await reader.get(User, subscriber)
            assert saved.preferences["feed"]["domains"] == ["cas:0701"]
        prepared.set()
        return {"completed": 0}

    monkeypatch.setattr(discovery, "run_subscription_asset_round", assets)
    try:
        async with get_session_factory()() as db:
            user = await db.get(User, subscriber)
            if already_selected:
                user.preferences = {"feed": {"domains": ["cas:0701"]}}
                await db.commit()

            def request(**kwargs):
                assert not db.in_transaction(), "field fetch started before preferences committed"
                requests.append(kwargs)
                return True

            monkeypatch.setattr(literature_index_harvester, "request_online_refresh", request)
            await put_interests(InterestsWrite(domains=["cas:0701"]), user=user, db=db)
            await asyncio.wait_for(prepared.wait(), 2)
            assert collector._translation_task is not None
            assert len(requests) == (0 if already_selected else 1)
            assert collector._task is None
    finally:
        await collector.stop()


@pytest.mark.asyncio
async def test_finished_field_fetch_wakes_waiting_preparation(subscriber, collector, monkeypatch):
    """Indexes arriving after an empty asset round must not wait another tick."""
    from app.services.literature_harvester import LiteratureIndexHarvester

    first_round, after_fetch = asyncio.Event(), asyncio.Event()
    calls = 0

    async def assets():
        nonlocal calls
        calls += 1
        (first_round if calls == 1 else after_fetch).set()
        return {"completed": 0}

    monkeypatch.setattr(discovery, "run_subscription_asset_round", assets)
    monkeypatch.setattr(discovery, "run_subscription_round", AsyncMock(return_value={}))
    monkeypatch.setattr(discovery, "curate_one", AsyncMock(return_value={}))
    harvester = LiteratureIndexHarvester()
    monkeypatch.setattr(harvester, "run_once", AsyncMock(return_value={"archived": 1, "failed": 0}))
    try:
        collector.request_subscription_refresh()
        await asyncio.wait_for(first_round.wait(), 2)
        assert harvester.request_online_refresh(user_id=subscriber, domains=["cas:0701"])
        await harvester._online_tasks[subscriber]
        await asyncio.wait_for(after_fetch.wait(), 2)
        assert collector._task is None
    finally:
        await collector.stop()


@pytest.mark.asyncio
async def test_field_only_paper_reaches_recommendations_after_background_translation(
    subscriber, monkeypatch, harness_domain_registry, tmp_path
):
    """Exercise both real feed views and the real cache, replacing only translation I/O."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from app.api.v1 import feed, literature
    from app.models.feed import FeedItem
    from app.schemas.feed import InterestsWrite
    from app.services.feed import translation, literature_projection
    from app.services import literature_harvester
    from nodes.literature.tools.local_index import LocalPaperIndex

    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "feed_collector_enabled", True)
    monkeypatch.setattr(settings, "feed_external_fetch_enabled", True)
    papers = tmp_path / "papers"
    papers.mkdir()
    monkeypatch.setattr(literature_projection, "literature_papers_root", lambda: papers)
    monkeypatch.setattr(literature_harvester, "_journal_targets", lambda *a, **k: {
        "example": {"journal": "Example Mathematics"}
    })
    monkeypatch.setattr(literature_harvester.literature_index_harvester,
                        "request_online_refresh", lambda **kwargs: True)
    monkeypatch.setattr(translation, "select_effective_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(translation, "resolved_api_key", lambda backend: "test-only")
    translated = asyncio.Event()
    cache = LocalPaperIndex(str(papers / "literature_catalog.sqlite3"))

    async def external_translation(payload, **kwargs):
        cache.save_title_translations({paper.title: "非线性方程的数值研究" for paper in payload.papers})
        cache.save_abstract_translations({paper.abstract: "研究了非线性方程的数值方法。"
                                          for paper in payload.papers if paper.abstract})
        translated.set()
        return SimpleNamespace(status="ok", diagnostics={})

    monkeypatch.setattr(literature, "translate_literature_page", external_translation)
    collector = scheduler.FeedCollector()
    monkeypatch.setattr(scheduler, "feed_collector", collector)
    async with get_session_factory()() as db:
        user = await db.get(User, subscriber)
        user.preferences = {"feed": {"domains": ["cas:0701"]}}
        await db.commit()
        empty = await feed.feed_today(limit=20, offset=0, ready_only=True, sources="domain",
                                     user=user, db=db, screen="zh")
        assert empty.empty_reason != feed.t("empty.no_collector_here", "zh")
        item = FeedItem(
            canonical_key="doi:10.1234/domain-only", kind="paper",
            title="Numerical methods for nonlinear equations",
            summary="This study evaluates numerical methods for nonlinear equations.",
            url="https://example.org/domain-only", venue="Example Mathematics",
            authors=[], domains=["070101"], published_at=datetime.now(UTC) - timedelta(days=1),
            extra={"literature_catalog": True, "literature_preparation": "ready",
                   "literature_eligible": True, "feed_acquisition_routes": ["journal"]},
        )
        db.add(item)
        await db.commit()
        item_id = str(item.id)
        dynamic = await feed.subscription_items(kind="domain", limit=20, offset=0, user=user, db=db)
        before = await feed.feed_today(limit=20, offset=0, ready_only=True, sources="domain",
                                       user=user, db=db, screen="zh")
        assert [str(card.item.id) for card in dynamic] == [item_id]
        assert before.feed == []
        await db.commit()
    try:
        async with get_session_factory()() as db:
            user = await db.get(User, subscriber)
            await feed.put_interests(InterestsWrite(domains=["cas:0701"]), user=user, db=db)
        await asyncio.wait_for(translated.wait(), 3)
        async with get_session_factory()() as db:
            user = await db.get(User, subscriber)
            after = await feed.feed_today(limit=20, offset=0, ready_only=True, sources="domain",
                                          user=user, db=db, screen="zh")
            assert [str(card.item.id) for card in after.feed] == [item_id]
            assert after.feed[0].item.title_zh == "非线性方程的数值研究"
            assert after.feed[0].item.abstract_zh == "研究了非线性方程的数值方法。"
            await db.commit()
        assert collector._task is None
    finally:
        await collector.stop()


@pytest.mark.asyncio
async def test_cancelled_field_fetch_does_not_restart_stopped_workers(subscriber, collector, monkeypatch):
    from app.services.literature_harvester import LiteratureIndexHarvester

    waiting = asyncio.Event()

    async def cancelled_fetch(**kwargs):
        waiting.set()
        await asyncio.Event().wait()

    harvester = LiteratureIndexHarvester()
    monkeypatch.setattr(harvester, "_run_online_refresh", cancelled_fetch)
    harvester.request_online_refresh(user_id=subscriber, domains=["cas:0701"])
    await asyncio.wait_for(waiting.wait(), 2)
    await collector.stop()
    await harvester.stop()
    assert collector._subscription_asset_task is None
    assert collector._translation_task is None
