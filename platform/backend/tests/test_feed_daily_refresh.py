"""Daily refresh must reach every acquisition route and survive restart/failure."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models.feed import FeedSource
from app.models.user import User
from app.services.feed import curation, discovery
from app.services.feed.scheduler import FeedCollector


def make_user(**feed):
    return User(id="aaaa1111-1111-4111-8111-111111111111", email="daily@test.local",
                display_name="Daily", hashed_password="x", is_active=True,
                preferences={"feed": feed})


@pytest.mark.parametrize("old_interval", [3600, 604800])
def test_public_sources_use_daily_cadence_even_with_old_intervals(old_interval):
    source = FeedSource(id="source", is_active=True, poll_interval_seconds=old_interval)
    collector = FeedCollector()
    source.last_polled_at = datetime.now(UTC) - timedelta(hours=23)
    assert not collector._is_due(source)
    source.last_polled_at = datetime.now(UTC) - timedelta(hours=24, seconds=1)
    assert collector._is_due(source)
    source.is_active = False
    assert not collector._is_due(source)


def test_project_refresh_uses_daily_cadence_and_repairs_legacy_empty_success():
    now = datetime.now(UTC)
    user = make_user(auto_curation=True, inferred_queries=["vehicle dynamics"],
                     curated_at=(now - timedelta(hours=23)).isoformat())
    assert not discovery.is_due(user, now=now)
    assert discovery.is_due(user, now=now + timedelta(hours=1))
    user.preferences["feed"]["inferred_queries"] = []
    assert discovery.is_due(user, now=now)
    curation._remember(user, error="empty model output")
    assert not discovery.is_due(user, now=now)
    assert discovery.is_due(user, now=now + timedelta(hours=1, minutes=1))


def test_subscription_progress_is_persisted_per_target_and_does_not_starve_later_targets():
    now = datetime.now(UTC)
    journals = [{"key": str(i), "name": f"Journal {i}"} for i in range(10)]
    history = {f"journals:{i}": {"attempted_at": now.isoformat()} for i in range(8)}
    user = make_user(subscriptions={"journals": journals, "scholars": [{"name": "Researcher"}]},
                     subscription_discovery=history)
    # Newly constructed user simulates a process restart: state is in preferences.
    restarted = make_user(**user.preferences["feed"])
    due_journals, due_scholars = discovery.due_subscription_targets(restarted, now=now)
    assert [j["key"] for j in due_journals] == ["8", "9"]
    assert due_scholars == [{"name": "Researcher"}]
    tomorrow, _ = discovery.due_subscription_targets(restarted, now=now + timedelta(days=1))
    assert len(tomorrow) == 8


@pytest.mark.asyncio
async def test_subscription_batch_commits_success_and_continues_after_failed_target(db_session, monkeypatch):
    user = make_user(subscriptions={"journals": [{"name": "Journal"}],
                                   "scholars": [{"name": "Researcher"}]})
    db_session.add(user)
    await db_session.commit()
    calls = []

    async def discover(db, **kwargs):
        calls.append(kwargs)
        if kwargs["journals"]:
            raise TimeoutError("source timeout")
        return 3

    monkeypatch.setattr(discovery, "discover_for_subscriptions", discover)
    found = await discovery._discover_subscription_batch(
        db_session, user=user, journals=[{"name": "Journal"}], scholars=[{"name": "Researcher"}])
    assert found == 3 and len(calls) == 2
    await db_session.refresh(user)
    state = curation.stored(user)["subscription_discovery"]
    assert state["journals:journal"]["error"].startswith("TimeoutError")
    assert state["scholars:researcher"]["succeeded_at"]
    assert discovery.due_subscription_targets(user) == ([], [])
    assert str(user.id) not in discovery._subscription_inflight


@pytest.mark.asyncio
async def test_empty_keywords_are_reported_instead_of_a_success(monkeypatch):
    user = make_user(auto_curation=True)
    project = SimpleNamespace(id="p", name="Vehicle dynamics", research_domain="Engineering",
                              description="Chassis control")
    monkeypatch.setattr(curation, "user_projects", AsyncMock(return_value=[project]))
    monkeypatch.setattr(curation, "projects_last_active", AsyncMock(return_value={}))
    monkeypatch.setattr(curation, "project_user_inputs", AsyncMock(return_value=[]))
    monkeypatch.setattr(curation, "saved_project_examples", AsyncMock(return_value=[]))
    monkeypatch.setattr(curation.bridge, "curation_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(curation.domain_service, "domain_registry",
                        lambda: SimpleNamespace(spine_categories=lambda: ["engineering"]))
    monkeypatch.setattr(curation.bridge, "call", AsyncMock(return_value={"domains": [], "projects": []}))
    assert await curation.curate(AsyncMock(), user=user, force=True) is None
    assert "未返回有效检索词" in curation.stored(user)["curated_error"]


@pytest.mark.asyncio
async def test_source_failure_does_not_permanently_disable_daily_retries():
    source = FeedSource(id="source", name="source", is_active=True, consecutive_failures=4,
                        max_consecutive_failures=5, poll_interval_seconds=604800)
    collector = FeedCollector()
    assert await collector._record(AsyncMock(), source, [], "timeout") == 0
    assert source.is_active
    assert not collector._is_due(source)
    source.last_polled_at -= timedelta(days=1, seconds=1)
    assert collector._is_due(source)


@pytest.mark.asyncio
async def test_profile_state_commits_before_remote_search(monkeypatch):
    db = AsyncMock()
    result = {"domains": [], "queries": ["vehicle dynamics"], "news_queries": ["chassis news"]}
    monkeypatch.setattr(curation, "curate", AsyncMock(return_value=result))

    async def search(*args, **kwargs):
        db.commit.assert_awaited_once()
        return 2

    monkeypatch.setattr(discovery, "_targeted_search", search)
    monkeypatch.setattr(discovery, "_prebuild_digests", AsyncMock(return_value=0))
    got = await discovery.curate_one(object(), db, user=make_user())
    assert got["found"] == 2


@pytest.mark.asyncio
async def test_model_timeout_reuses_current_project_queries_once_per_day(monkeypatch):
    project = SimpleNamespace(id="p1", name="Chassis control",
                              research_domain="Engineering", description="Brake by wire")
    fingerprint = curation.project_fingerprint([project])
    user = make_user(
        auto_curation=True, curated_error="feed_curate_profile timed out",
        curated_fingerprint=fingerprint,
        inferred_queries=["brake-by-wire control"],
        inferred_news_queries=["线控底盘 国标"],
    )
    db = AsyncMock()
    monkeypatch.setattr(curation, "curate", AsyncMock(return_value=None))
    monkeypatch.setattr(curation, "user_projects", AsyncMock(return_value=[project]))
    search = AsyncMock(return_value=3)
    monkeypatch.setattr(discovery, "_targeted_search", search)

    first = await discovery.curate_one(AsyncMock(), db, user=user)
    second = await discovery.curate_one(AsyncMock(), db, user=user)
    assert first["found"] == 3
    assert second["found"] == 0
    search.assert_awaited_once()
    assert user.preferences["feed"]["fallback_search_at"]

    user.preferences["feed"]["curated_fingerprint"] = "different-project"
    user.preferences["feed"].pop("fallback_search_at")
    assert (await discovery.curate_one(AsyncMock(), db, user=user))["found"] == 0
    search.assert_awaited_once()

@pytest.mark.asyncio
async def test_unchanged_project_still_searches_on_daily_round(monkeypatch):
    project = SimpleNamespace(id="p1", name="Irregular particle settling",
                              research_domain="Fluid mechanics", description="Immersed boundary")
    fingerprint = curation.project_fingerprint([project])
    user = make_user(auto_curation=True, curated_fingerprint=fingerprint,
                     inferred_queries=["irregular particle settling"],
                     curated_at=(datetime.now(UTC) - timedelta(days=2)).isoformat())
    db = AsyncMock()
    monkeypatch.setattr(curation, "curate", AsyncMock(return_value=None))
    monkeypatch.setattr(curation, "user_projects", AsyncMock(return_value=[project]))
    monkeypatch.setattr(curation, "saved_project_examples", AsyncMock(return_value=[]))
    search = AsyncMock(return_value=7)
    monkeypatch.setattr(discovery, "_targeted_search", search)

    assert discovery.is_due(user)
    assert (await discovery.curate_one(AsyncMock(), db, user=user))["found"] == 7
    assert not discovery.is_due(user)
    search.assert_awaited_once()
