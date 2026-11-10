"""Repository subscriptions must deliver real preprints through the feed gates."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import get_session_factory
from app.models.feed import FeedItem, FeedSource, FeedVisibility
from app.models.user import User
from app.services.feed import collectors, ingest, ranking
from app.services.feed.profile import Profile
from app.services.feed.scheduler import FeedCollector


def paper(number, **changes):
    values = dict(
        id=str(uuid4()), canonical_key=f"arxiv:2611.0000{number}", kind="paper",
        title=f"Independent preprint study {number}",
        url=f"https://arxiv.org/abs/2611.0000{number}", venue="arXiv",
        summary="A complete research abstract.", domains=["cs.LG"], authors=[],
        published_at=datetime.now(UTC) - timedelta(days=1),
        visibility=FeedVisibility.PLATFORM, extra={},
    )
    values.update(changes)
    return FeedItem(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("domains", [("arxiv",), ("arxiv", "070102")])
async def test_arxiv_selection_reaches_feed_without_project_or_route_marker(
    db_session, harness_domain_registry, domains,
):
    from app.api.v1.feed import _to_out

    profile = Profile(user_id="reader", domains=domains)
    first, second = paper(1), paper(2, domains=["quant-ph"])
    journal = paper(3, canonical_key="doi:10.1234/journal", venue="Nature",
                    url="https://doi.org/10.1234/journal", domains=["cs.LG", "arxiv"])
    stale = paper(4, published_at=datetime.now(UTC) - timedelta(days=400))
    private = paper(5, visibility=FeedVisibility.ORGANIZATION, organization_id="other")
    notice = paper(6, title="Retraction notice to a previous article")
    db_session.add_all([first, second, journal, stale, private, notice])
    await db_session.commit()

    candidates = await ranking.candidate_items(db_session, profile=profile)
    result = ranking.rank_rednote(candidates, profile, user_id=profile.user_id)
    assert {value.item.id for value in result} == {first.id, second.id}
    assert all(value.matched_domains == ("arxiv",) for value in result)
    # API progress counts use serialized cards, which have no canonical_key.
    assert ranking._domain_hits(_to_out(first), profile) == ("arxiv",)
    assert ranking._domain_hits(journal, profile) == ()
    assert ranking.rank_rednote([first, second], Profile(user_id="reader"), user_id="reader") == []


@pytest.mark.asyncio
async def test_completed_arxiv_source_is_visible_before_a_later_source_fails(
    db_session, harness_domain_registry, monkeypatch,
):
    user = User(id=str(uuid4()), email="arxiv-reader@test.local", display_name="Reader",
                hashed_password="unused", is_active=True,
                preferences={"feed": {"domains": ["arxiv"]}})
    sources = [FeedSource(id=str(uuid4()), kind="arxiv", name=f"arXiv {category}",
                          config={"category": category}, domains=[category], is_active=True)
               for category in ("cs.LG", "quant-ph")]
    # A physics label alone must not make an unrelated journal an arXiv source.
    unrelated = FeedSource(id=str(uuid4()), kind="rss", name="Other Journal",
                           config={"url": "https://example.org/rss"},
                           domains=["physics.comp-ph"], is_active=True)
    db_session.add_all([user, *sources, unrelated])
    await db_session.commit()
    monkeypatch.setattr(settings, "feed_external_fetch_enabled", True)
    monkeypatch.setattr(ingest, "enrich_missing_images", AsyncMock(return_value=0))
    collector = FeedCollector()
    fetched = []

    async def fetch(client, semaphore, source):
        fetched.append(source.id)
        entry = collectors.CollectedItem(
            canonical_key="arxiv:2611.00123", kind="paper", title="Fresh arXiv study",
            url="https://arxiv.org/abs/2611.00123", summary="Complete abstract.",
            venue="arXiv", domains=["cs.LG"], published_at=datetime.now(UTC),
        )
        return source, [entry], ""

    original_record = collector._record
    recorded = []

    async def record(db, source, items, error):
        if recorded:
            # Use another DB connection: a flush is insufficient for readers.
            async with get_session_factory()() as reader:
                existing = await reader.scalar(select(FeedItem).where(
                    FeedItem.canonical_key == "arxiv:2611.00123"))
                assert existing is not None
            raise RuntimeError("later source failed")
        recorded.append(source.id)
        return await original_record(db, source, items, error)

    monkeypatch.setattr(collector, "_fetch", fetch)
    monkeypatch.setattr(collector, "_record", record)
    with pytest.raises(RuntimeError, match="later source failed"):
        await collector.run_once()
    assert set(fetched) == {source.id for source in sources}
    async with get_session_factory()() as reader:
        assert await reader.scalar(select(FeedItem.id).where(
            FeedItem.canonical_key == "arxiv:2611.00123"))
