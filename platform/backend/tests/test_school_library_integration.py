"""A separate, read-only school catalogue joins platform search without leaking data."""

from __future__ import annotations

import asyncio
import runpy
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.v1 import literature as literature_api
from app.schemas.literature import LiteratureSearchIn
from app.services import school_library

create = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "create_demo_school_library.py")
)["create"]


@pytest.fixture
def school_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "independent_school.sqlite3"
    create(path)
    monkeypatch.setenv("SCHOOL_LIBRARY_DB_PATH", str(path))
    monkeypatch.setenv("SCHOOL_LIBRARY_INSTITUTION_ID", "demo-university")
    monkeypatch.setenv("SCHOOL_LIBRARY_NAME", "模拟学校馆藏")
    return path


def test_school_search_is_read_only_and_institution_scoped(school_db: Path) -> None:
    before = school_db.read_bytes()
    found = school_library.search("lattice boltzmann", institution_id="demo-university")
    assert [row["id"] for row in found] == ["LIB-002", "LIB-001"]
    assert school_library.search("lattice boltzmann", institution_id="other-school") == []
    assert (
        school_library.search("campus climate", institution_id="demo-university")[0]["id"]
        == "LIB-003"
    )
    assert school_db.read_bytes() == before
    with school_library._connect(school_db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM school_papers")


def test_merges_duplicate_doi_and_keeps_school_only_record(school_db: Path) -> None:
    rows = school_library.search("lattice boltzmann", institution_id="demo-university")
    public = {
        "papers": [
            {
                "title": "Public version",
                "doi": "https://doi.org/10.5555/SCIENCEMATE.DEMO.001",
                "source": "crossref",
                "abstract": None,
                "metadata_provenance": {},
                "score": 0.8,
                "ranking_score": 0.9,
            }
        ],
        "candidate_count": 1,
        "source_counts": {"crossref": 1},
    }
    merged = school_library.merge_search_results(public, rows, limit=20)
    assert len(merged["papers"]) == 2
    duplicate = merged["papers"][0]
    assert duplicate["source"] == "crossref"
    assert duplicate["school_holdings"] == ["模拟学校馆藏"]
    assert duplicate["school_fulltext_url"].endswith("/LIB-001")
    assert duplicate["abstract"].startswith("A demonstration study")
    assert merged["papers"][1]["source"] == "模拟学校馆藏"
    assert merged["source_counts"] == {"crossref": 1, "school_library": 2}
    assert public["papers"][0].get("school_holdings") is None


def test_full_public_result_still_shows_school_only_papers(school_db: Path) -> None:
    rows = school_library.search("lattice boltzmann", institution_id="demo-university")
    public = {
        "papers": [
            {
                "title": f"Public paper {index}",
                "doi": f"10.5555/public.{index}",
                "source": "crossref",
                "ranking_score": 1 - index / 100,
            }
            for index in range(20)
        ],
        "candidate_count": 20,
        "source_counts": {"crossref": 20},
    }
    merged = school_library.merge_search_results(public, rows, limit=20)
    assert len(merged["papers"]) == 20
    assert [paper["federated_rank"] for paper in merged["papers"]] == list(range(20))
    assert [index for index, paper in enumerate(merged["papers"])
            if paper["source"] == "模拟学校馆藏"] == [4, 9]
    assert [paper["title"] for paper in merged["papers"] if paper["source"] == "crossref"] == [
        f"Public paper {index}" for index in range(18)
    ]
    assert all("federated_rank" not in paper for paper in public["papers"])


def test_duplicate_doi_is_merged_before_result_limit(school_db: Path) -> None:
    rows = school_library.search("lattice boltzmann", institution_id="demo-university")
    public = {
        "papers": [
            {"title": f"Public paper {index}", "doi": f"10.5555/public.{index}", "source": "crossref"}
            for index in range(18)
        ] + [{"title": "Public version", "doi": "10.5555/sciencemate.demo.001", "source": "crossref"}],
    }
    merged = school_library.merge_search_results(public, rows, limit=20)
    assert len(merged["papers"]) == 20
    assert sum(paper["doi"] == "10.5555/sciencemate.demo.001" for paper in merged["papers"]) == 1
    assert merged["papers"][-1]["school_holdings"] == ["模拟学校馆藏"]
    assert any(paper["doi"] == "10.5555/sciencemate.demo.002" for paper in merged["papers"])


def test_api_search_and_fulltext_respect_school_affiliation(
    school_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def backend(*_args, **_kwargs):
        return object()

    async def bridge_call(**_kwargs):
        return {
            "query": "lattice boltzmann",
            "papers": [],
            "total": 0,
            "candidate_count": 0,
            "source_counts": {},
            "search_mode": "remote",
        }

    monkeypatch.setattr(literature_api, "select_effective_backend", backend)
    monkeypatch.setattr(literature_api.bridge, "call", bridge_call)
    school_user = SimpleNamespace(institution_id="demo-university")
    outsider = SimpleNamespace(institution_id="other-school")
    request = LiteratureSearchIn(query="lattice boltzmann")
    result = asyncio.run(literature_api.search_literature(request, user=school_user, db=None))
    assert result.total == 2
    assert result.papers[0].school_holdings == ["模拟学校馆藏"]
    outside = asyncio.run(literature_api.search_literature(request, user=outsider, db=None))
    assert outside.total == 0
    response = asyncio.run(literature_api.school_fulltext("LIB-001", user=school_user))
    assert b"FICTIONAL SCHOOL LIBRARY FULL TEXT" in response.body
    with pytest.raises(HTTPException) as denied:
        asyncio.run(literature_api.school_fulltext("LIB-001", user=outsider))
    assert denied.value.status_code == 404


def test_school_integration_is_opt_in(school_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHOOL_LIBRARY_DB_PATH")
    assert school_library.search("lattice boltzmann", institution_id="demo-university") == []


def test_http_school_search_and_guarded_fulltext(school_db: Path) -> None:
    app = FastAPI()
    app.include_router(literature_api.router, prefix="/api/v1/literature")
    app.dependency_overrides[literature_api.get_current_user] = lambda: SimpleNamespace(
        institution_id="demo-university"
    )

    async def check() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            found = await client.get(
                "/api/v1/literature/school/search", params={"query": "lattice boltzmann"}
            )
            assert found.status_code == 200
            assert found.json()["total"] == 2
            assert found.json()["papers"][0]["school_fulltext_url"].endswith("LIB-002")
            opened = await client.get("/api/v1/literature/school/fulltext/LIB-002")
            assert opened.status_code == 200
            assert "FICTIONAL SCHOOL LIBRARY FULL TEXT" in opened.text
            app.dependency_overrides[literature_api.get_current_user] = lambda: SimpleNamespace(
                institution_id="other-school"
            )
            hidden = await client.get(
                "/api/v1/literature/school/search", params={"query": "lattice boltzmann"}
            )
            denied = await client.get("/api/v1/literature/school/fulltext/LIB-002")
            assert hidden.json()["total"] == 0
            assert denied.status_code == 404

    asyncio.run(check())


@pytest.mark.asyncio
async def test_scheduled_school_feed_is_idempotent_and_institution_scoped(
    school_db: Path,
    db_session,
    harness_domain_registry,
) -> None:
    from sqlalchemy import select

    from app.api.v1.feed import _visible_clause
    from app.models.feed import FeedItem, FeedVisibility
    from app.services.feed.profile import Profile, ProjectSignal, extract_terms
    from app.services.feed.ranking import candidate_items, rank_rednote
    from app.services.feed.scheduler import FeedCollector

    collector = FeedCollector()
    first = await collector.run_school_once()
    assert first == {"seen": 2, "stored": 2, "updated": 0, "withdrawn": 0}
    assert (await collector.run_school_once())["seen"] == 0  # not due for 24 hours
    items = (
        (
            await db_session.execute(
                select(FeedItem).where(FeedItem.visibility == FeedVisibility.INSTITUTION)
            )
        )
        .scalars()
        .all()
    )
    assert len(items) == 2
    assert all(item.organization_id == "demo-university" for item in items)
    assert all("fulltext_text" not in item.extra for item in items)
    project = ProjectSignal(
        "p1",
        "lattice boltzmann particle settling",
        extract_terms("lattice boltzmann particle settling"),
    )
    owner = Profile(
        user_id="u1",
        institution_id="demo-university",
        projects=(project,),
        journals=frozenset({"Demo University Repository"}),
    )
    outsider = Profile(user_id="u2", institution_id="other-school", projects=(project,))
    school_candidates = await candidate_items(db_session, profile=owner)
    assert {
        item.extra["school_record_id"]
        for item in school_candidates
        if item.visibility == FeedVisibility.INSTITUTION
    } == {"LIB-001", "LIB-002"}
    ranked = rank_rednote(school_candidates, owner, user_id="u1")
    assert any(value.item.extra.get("school_record_id") == "LIB-002" for value in ranked)
    assert not [
        item
        for item in await candidate_items(db_session, profile=outsider)
        if item.visibility == FeedVisibility.INSTITUTION
    ]
    outsider_user = SimpleNamespace(group_id=None, institution_id="other-school")
    assert (
        not (await db_session.execute(select(FeedItem).where(_visible_clause(outsider_user))))
        .scalars()
        .all()
    )

    second = await collector.run_school_once(force=True)
    assert second["stored"] == 0 and second["updated"] == 2
    with sqlite3.connect(school_db) as conn:
        conn.execute("DELETE FROM school_papers WHERE id='LIB-002'")
    third = await collector.run_school_once(force=True)
    assert third["withdrawn"] == 1
    assert not any(
        item.extra.get("school_record_id") == "LIB-002"
        for item in await candidate_items(db_session, profile=owner)
    )


@pytest.mark.asyncio
async def test_school_feed_reaches_today_only_for_its_institution(
    school_db: Path,
    db_session,
    harness_domain_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.feed import feed_today
    from app.models.user import User
    from app.services.feed.scheduler import FeedCollector

    await FeedCollector().run_school_once(force=True)
    preference = {"feed": {"subscriptions": {"journals": [{"name": "Demo University Repository"}]}}}
    owner = User(
        email="owner@demo.invalid",
        hashed_password="test",
        display_name="Owner",
        institution_id="demo-university",
        preferences=preference,
    )
    outsider = User(
        email="outsider@demo.invalid",
        hashed_password="test",
        display_name="Outsider",
        institution_id="other-school",
        preferences=preference,
    )
    db_session.add_all([owner, outsider])
    await db_session.commit()

    async def translated(items):
        for item in items:
            item.title_zh = "模拟译文标题"
            item.abstract_zh = "模拟译文摘要"
        return items

    from app.api.v1 import feed as feed_api

    monkeypatch.setattr(feed_api, "_attach_cached_translations", translated)
    own = await feed_today(
        limit=20, offset=0, ready_only=True, sources="journal", user=owner, db=db_session
    )
    assert any(card.item.extra.get("school_record_id") == "LIB-002" for card in own.feed)
    assert any(card.item.extra.get("school_fulltext_url") for card in own.feed)
    outside = await feed_today(
        limit=20, offset=0, ready_only=True, sources="journal", user=outsider, db=db_session
    )
    assert not any(card.item.extra.get("school_library") for card in outside.feed)
