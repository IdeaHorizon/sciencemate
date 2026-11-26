"""The same project context drives curation, collection and card admission."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.execution import SessionMessage, SessionProjection
from app.models.feed import FeedItem
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.services.feed import curation, discovery, profile, project_context as context, ranking
from app.services.other_homes import ThereIsNoSuchHomeError


@pytest.fixture
def servers(monkeypatch):
    rows = [{"id": "remote:" + str(uuid4()), "name": "Server", "enabled": True}]
    monkeypatch.setattr(context.assembly, "can_choose_project_folders", lambda: True)
    monkeypatch.setattr(settings, "remote_runtime_token", "")
    monkeypatch.setattr(context.remote_servers, "all_servers", lambda: rows)
    monkeypatch.setattr(context.remote_servers, "cached_projects", lambda _: [])
    monkeypatch.setattr(context.remote_servers, "ask", AsyncMock(return_value={"projects": []}))
    return rows


@pytest.fixture
async def owner(db_session):
    user = User(id=str(uuid4()), email="feed-owner@test.local", display_name="Owner",
                hashed_password="x", is_active=True, preferences={"feed": {"auto_curation": True}})
    db_session.add(user)
    await db_session.flush()
    return user


def remote_project(**overrides):
    return {"id": str(uuid4()), "name": "R1", "status": "active", "mine": True,
            "research_domain": "physics", "description": "research",
            "user_inputs": ["quantum turbulence vortex reconnection"], **overrides}


async def local_project(db, user, **overrides):
    row = Project(id=str(uuid4()), owner_id=user.id, name="Local Ising",
                  research_domain="physics", description="Ising critical scaling",
                  status=ProjectStatus.ACTIVE)
    for key, value in overrides.items():
        setattr(row, key, value)
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_local_context_is_owned_active_and_uses_recent_user_inputs(db_session, owner, servers):
    local = await local_project(db_session, owner)
    await local_project(db_session, owner, owner_id=str(uuid4()), name="Not mine")
    await local_project(db_session, owner, status=ProjectStatus.ARCHIVED, name="Archived")
    sid = str(uuid4())
    db_session.add(SessionProjection(session_id=sid, project_id=str(local.id),
                                    tenant_id="t", workspace_id="w", title="Research", lifecycle_status="active"))
    db_session.add_all([
        SessionMessage(session_id=sid, sequence=1, role="user", content="finite-size critical exponents"),
        SessionMessage(session_id=sid, sequence=2, role="assistant", content="do not mine assistant text"),
    ])
    await db_session.flush()
    from app.api.v1.feed import feed_project_contexts
    result = await feed_project_contexts(user=owner, db=db_session)
    assert [row["id"] for row in result["projects"]] == [str(local.id)]
    assert result["projects"][0]["user_inputs"] == ("finite-size critical exponents",)
    context.remote_servers.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_and_remote_share_curation_and_ranking_without_copying_projects(
    db_session, owner, servers, monkeypatch,
):
    local = await local_project(db_session, owner)
    remote = remote_project()
    context.remote_servers.ask.return_value = {"projects": [remote]}
    await context.refresh_remote_projects(owner)
    projects = await curation.user_projects(db_session, owner)
    assert {p.id for p in projects} == {str(local.id), remote["id"]}
    assert await db_session.scalar(select(func.count(Project.id))) == 1
    monkeypatch.setattr(curation.bridge, "curation_backend", AsyncMock(return_value=SimpleNamespace(
        display_name="Test", model="test",
    )))
    monkeypatch.setattr(curation.domain_service, "domain_registry", lambda: SimpleNamespace(spine_categories=lambda: []))

    async def model(**kwargs):
        supplied = kwargs["payload"]["projects"]
        assert {p["name"] for p in supplied} == {local.name, "R1"}
        assert next(p for p in supplied if p["name"] == "R1")["user_inputs"] == remote["user_inputs"]
        return {"domains": [], "projects": [
            {"queries": ["Ising critical scaling" if p["name"] == local.name else "quantum turbulence vortex"]}
            for p in supplied
        ]}

    monkeypatch.setattr(curation.bridge, "call", model)
    search = AsyncMock(return_value=2)
    monkeypatch.setattr(discovery, "_targeted_search", search)
    monkeypatch.setattr(discovery, "_prebuild_digests", AsyncMock(return_value=0))
    await discovery.curate_one(object(), db_session, user=owner)
    assert set(search.await_args.kwargs["queries"]) == {"Ising critical scaling", "quantum turbulence vortex"}
    # Delivery must never call a server, even after its connection drops.
    context.remote_servers.ask.side_effect = AssertionError("card delivery cannot wait for SSH")
    current = await profile.build_profile(db_session, user=owner)
    now = datetime.now(UTC)
    items = [FeedItem(id=str(uuid4()), canonical_key=str(uuid4()), kind="paper", title=title,
                     summary=title, domains=[], extra={}, published_at=now) for title in (
                         "Ising critical scaling", "quantum turbulence vortex reconnection",
                     )]
    # Ranking measures how distinctive project terms are within the candidate pool.
    fillers = [FeedItem(id=str(uuid4()), canonical_key=str(uuid4()), kind="paper",
                        title=f"Unrelated field {i}", summary="unrelated topic",
                        domains=[], extra={}, published_at=now) for i in range(30)]
    ranked = ranking.rank([*items, *fillers], current, now=now)
    assert {row.project_id for row in ranked if row.project_id} == {str(local.id), remote["id"]}


@pytest.mark.asyncio
async def test_remote_only_five_projects_are_visible_and_stale_ones_are_removed(db_session, owner, servers):
    remote = [remote_project(name=f"R{i}") for i in range(1, 6)]
    context.remote_servers.ask.return_value = {"projects": [*remote, remote[0], remote_project(status="paused") ]}
    await context.refresh_remote_projects(owner)
    first = await profile.build_profile(db_session, user=owner)
    assert len(first.projects) == 5
    before = context.remote_revision(owner)
    context.remote_servers.ask.return_value = {"projects": []}
    await context.refresh_remote_projects(owner, force=True)
    assert not (await profile.build_profile(db_session, user=owner)).projects
    assert before != context.remote_revision(owner)


@pytest.mark.asyncio
async def test_timeout_on_one_server_does_not_hide_local_or_other_remote_projects(
    db_session, owner, servers, monkeypatch,
):
    local = await local_project(db_session, owner)
    servers.append({"id": "remote:" + str(uuid4()), "name": "Offline"})
    remote = remote_project()
    async def ask(sid, *args, **kwargs):
        if sid == servers[1]["id"]:
            await asyncio.Event().wait()
        return {"projects": [remote]}
    monkeypatch.setattr(context.remote_servers, "ask", ask)
    monkeypatch.setattr(context, "SERVER_TIMEOUT_SECONDS", 0.05)
    await context.refresh_remote_projects(owner)
    assert {p.id for p in await curation.user_projects(db_session, owner)} == {str(local.id), remote["id"]}


@pytest.mark.asyncio
async def test_lost_access_or_disabled_connection_removes_cached_signals(db_session, owner, servers):
    context.remote_servers.ask.return_value = {"projects": [remote_project()]}
    await context.refresh_remote_projects(owner)
    assert len(context.cached_remote_projects(owner)) == 1
    servers[0]["enabled"] = False
    assert not context.cached_remote_projects(owner)
    servers[0]["enabled"] = True
    context.remote_servers.ask.side_effect = ThereIsNoSuchHomeError("denied", en="denied", status=403)
    await context.refresh_remote_projects(owner, force=True)
    assert not context.cached_remote_projects(owner)


@pytest.mark.asyncio
async def test_zero_projects_clears_only_inference_and_hides_legacy_results(db_session, owner, monkeypatch):
    from app.api.v1.feed import _curation_state

    monkeypatch.setattr(context, "_servers", lambda: [])
    monkeypatch.setattr(curation.bridge, "curation_backend", AsyncMock(return_value=SimpleNamespace(
        display_name="Test", model="test",
    )))
    owner.preferences = {"feed": {
        "auto_curation": True, "domains": ["cs.LG"], "inferred_domains": ["physics"],
        "inferred_queries": ["old project"], "inferred_news_queries": ["old news"],
        "curated_fingerprint": "old-projects", "curated_at": "2026-01-01T00:00:00+00:00",
    }}
    # Even before the background collector runs, GET must not label an old
    # preference as an inference from a project list that is now empty.
    state = await _curation_state(db_session, owner)
    assert state.project_status == "no_projects"
    assert state.inferred_domains == state.inferred_queries == []
    assert state.local_project_count == state.remote_project_count == 0
    assert await curation.curate(db_session, user=owner) is None
    saved = curation.stored(owner)
    assert saved["domains"] == ["cs.LG"]
    assert saved["inferred_domains"] == saved["inferred_queries"] == saved["inferred_news_queries"] == []
    assert saved["curated_fingerprint"] == curation.project_fingerprint([])


@pytest.mark.asyncio
async def test_curation_distinguishes_offline_empty_and_reconnected_projects(
    db_session, owner, servers, monkeypatch,
):
    from app.api.v1.feed import _curation_state

    remote = remote_project()
    context.remote_servers.ask.return_value = {"projects": [remote]}
    monkeypatch.setattr(curation.bridge, "curation_backend", AsyncMock(return_value=SimpleNamespace(
        display_name="Test", model="test",
    )))
    monkeypatch.setattr(curation.domain_service, "domain_registry",
                        lambda: SimpleNamespace(spine_categories=lambda: []))
    monkeypatch.setattr(curation.domain_service, "label", lambda code, *args, **kwargs: code)
    model = AsyncMock(return_value={"domains": ["physics"], "projects": [{"queries": ["quantum vortex"]}]})
    monkeypatch.setattr(curation.bridge, "call", model)

    await context.refresh_remote_projects(owner, force=True)
    await curation.curate(db_session, user=owner)
    original_time = curation.stored(owner)["curated_at"]
    state = await _curation_state(db_session, owner)
    assert (state.project_status, state.local_project_count, state.remote_project_count) == ("current", 0, 1)

    context.remote_servers.ask.side_effect = TimeoutError()
    await context.refresh_remote_projects(owner, force=True)
    await curation.curate(db_session, user=owner)
    state = await _curation_state(db_session, owner)
    assert state.project_status == "unavailable"
    assert state.inferred_domains == ["physics"]
    assert curation.stored(owner)["curated_at"] == original_time
    assert model.await_count == 1

    await curation.curate(db_session, user=owner, force=True)
    local = await local_project(db_session, owner, name="New local project")
    await curation.curate(db_session, user=owner)
    assert curation.stored(owner)["inferred_domains"] == ["physics"]
    assert curation.stored(owner)["curated_at"] == original_time
    assert model.await_count == 1
    local.status = ProjectStatus.ARCHIVED
    await db_session.flush()

    context.remote_servers.ask.side_effect = None
    context.remote_servers.ask.return_value = {"projects": []}
    await context.refresh_remote_projects(owner, force=True)
    await curation.curate(db_session, user=owner)
    assert (await _curation_state(db_session, owner)).project_status == "no_projects"

    context.remote_servers.ask.return_value = {"projects": [remote_project(name="New project")]}
    await context.refresh_remote_projects(owner, force=True)
    assert (await _curation_state(db_session, owner)).project_status == "pending"
    await curation.curate(db_session, user=owner)
    assert (await _curation_state(db_session, owner)).project_status == "current"
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_shared_backend_never_uses_desktop_connection_snapshots(db_session, owner, servers, monkeypatch):
    await local_project(db_session, owner)
    context.remote_servers.ask.return_value = {"projects": [remote_project()]}
    await context.refresh_remote_projects(owner)
    monkeypatch.setattr(context.assembly, "can_choose_project_folders", lambda: False)
    assert len(await curation.user_projects(db_session, owner)) == 1
    assert not context.cached_remote_projects(owner)


@pytest.mark.asyncio
async def test_older_remote_runtime_supplies_initial_user_input(db_session, owner, servers, monkeypatch):
    remote = remote_project()
    async def ask(sid, method, path, **kwargs):
        if path.endswith("project-contexts"):
            raise ThereIsNoSuchHomeError("old version", en="old version", status=404)
        if path == "/api/v1/projects/":
            return [remote_project(id=remote["id"], user_inputs=[])]
        if path.endswith("/sessions"):
            return [{"id": "session"}]
        return {"items": [{"role": "user", "content": "quantum vortex turbulence"},
                          {"role": "assistant", "content": "irrelevant instructions"}]}
    monkeypatch.setattr(context.remote_servers, "ask", ask)
    await context.refresh_remote_projects(owner)
    projects = await curation.user_projects(db_session, owner)
    assert projects[0].user_inputs == ("quantum vortex turbulence",)


@pytest.mark.asyncio
async def test_new_local_or_remote_project_is_due_before_the_daily_deadline(db_session, owner, servers):
    await local_project(db_session, owner)
    initial = await curation.user_projects(db_session, owner)
    owner.preferences = {"feed": {
        "auto_curation": True, "last_project_search_at": datetime.now(UTC).isoformat(),
        "curated_fingerprint": curation.project_fingerprint(initial), "inferred_queries": ["Ising scaling"],
    }}
    await db_session.flush()
    assert not await discovery.due_users(db_session)
    context.remote_servers.ask.return_value = {"projects": [remote_project()]}
    await context.refresh_remote_projects(owner, force=True)
    assert [str(u.id) for u in await discovery.due_users(db_session)] == [str(owner.id)]


def test_prepared_terms_trigger_the_first_search_without_waiting_one_day(owner):
    owner.preferences = {"feed": {"auto_curation": True, "curated_at": datetime.now(UTC).isoformat(),
                                  "curation_result_version": 2, "inferred_queries": ["Ising scaling"]}}
    assert discovery.is_due(owner)


@pytest.mark.asyncio
async def test_remote_change_invalidates_the_recommendation_pool_immediately(db_session, owner, servers):
    from app.schemas.feed import FeedTodayOut
    from app.services.feed import pool_cache

    pool_cache.clear()
    context.remote_servers.ask.return_value = {"projects": [remote_project()]}
    await context.refresh_remote_projects(owner)
    build = AsyncMock(return_value=FeedTodayOut())
    await pool_cache.get_pool(db_session, owner, build)
    await pool_cache.get_pool(db_session, owner, build)
    assert build.await_count == 1
    context.remote_servers.ask.return_value = {"projects": []}
    await context.refresh_remote_projects(owner, force=True)
    await pool_cache.get_pool(db_session, owner, build)
    assert build.await_count == 2


@pytest.mark.asyncio
async def test_curation_toggle_commits_before_waking_the_collector(db_session, owner, servers, monkeypatch):
    from app.api.v1.feed import put_curation
    from app.schemas.feed import CurationWrite
    from app.services.feed.scheduler import feed_collector

    monkeypatch.setattr(curation.bridge, "curation_backend", AsyncMock(return_value=SimpleNamespace(
        display_name="Test", model="test",
    )))
    monkeypatch.setattr(curation, "curate", AsyncMock(return_value=None))
    def wake():
        assert not db_session.in_transaction()
        return True
    request = Mock(side_effect=wake)
    monkeypatch.setattr(feed_collector, "request_project_refresh", request)
    result = await put_curation(CurationWrite(enabled=True), user=owner, db=db_session)
    assert result.enabled
    request.assert_called_once()
    result = await put_curation(CurationWrite(enabled=False), user=owner, db=db_session)
    assert not result.enabled
    request.assert_called_once()
