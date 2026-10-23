"""Publication windows must reflect source precision and acquisition route."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.feed import FeedItem, FeedVisibility
from app.services.feed.profile import Profile, ProjectSignal, extract_terms
from app.services.feed.ranking import candidate_items
from app.services.feed.windows import academic_cutoff, web_cutoff
from app.services.literature_harvester import _recent_publication


@pytest.mark.asyncio
async def test_31_day_academic_and_12_month_web_windows(db_session) -> None:
    now = datetime.now(UTC)
    month_start = academic_cutoff(now).replace(day=1, hour=0, minute=0, second=0)
    entries = [
        ("academic_day_20", now - timedelta(days=20), {}),
        ("academic_day_40", now - timedelta(days=40), {}),
        ("online_journal_day_200", now - timedelta(days=200), {"feed_acquisition_routes": ["journal"]}),
        ("academic_month", month_start, {"publication_date_precision": "month"}),
        ("web_day_200", now - timedelta(days=200), {"source": "web", "feed_acquisition_routes": ["web"]}),
        ("web_day_400", now - timedelta(days=400), {"source": "web", "feed_acquisition_routes": ["web"]}),
    ]
    db_session.add_all([
        FeedItem(
            canonical_key=f"url:https://example.test/{name}", kind="news",
            title=name, url=f"https://example.test/{name}",
            published_at=published, authors=[], domains=[], extra=extra,
            visibility=FeedVisibility.PLATFORM,
        )
        for name, published, extra in entries
    ])
    await db_session.flush()

    titles = {item.title for item in await candidate_items(db_session, profile=Profile(user_id="u1"))}
    assert {"academic_day_20", "academic_month", "web_day_200", "online_journal_day_200"} <= titles
    assert "academic_day_40" not in titles
    assert "web_day_400" not in titles


def test_previous_month_without_day_overlaps_31_day_window() -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert _recent_publication("2026-08", 2026, now=now, cutoff=academic_cutoff(now))[1:] == (True, "month")
    assert _recent_publication("2026-07", 2026, now=now, cutoff=academic_cutoff(now))[1:] == (False, "month")
    assert web_cutoff(datetime(2028, 2, 29, tzinfo=UTC)) == datetime(2027, 2, 28, tzinfo=UTC)


@pytest.mark.asyncio
async def test_owned_project_survives_large_recent_journal_pool(db_session) -> None:
    now = datetime.now(UTC)
    db_session.add_all([
        FeedItem(
            canonical_key=f"url:https://example.test/journal-{index}",
            kind="paper", title=f"Recent journal {index}",
            url=f"https://example.test/journal-{index}", summary="Valid abstract.",
            published_at=now - timedelta(minutes=index), authors=[],
            domains=["070102"], extra={}, visibility=FeedVisibility.PLATFORM,
        )
        for index in range(405)
    ])
    project = FeedItem(
        canonical_key="url:https://example.test/owned-project", kind="paper",
        title="Relevant brake-by-wire control", url="https://example.test/owned-project",
        summary="Vehicle control and stability.",
        published_at=now - timedelta(days=100), authors=[], domains=[],
        extra={"feed_acquisition_routes": ["project_profile"], "profile_user_ids": ["u1"]},
        visibility=FeedVisibility.PLATFORM,
    )
    db_session.add(project)
    await db_session.flush()
    profile = Profile(
        user_id="u1", domains=("070102",),
        projects=(ProjectSignal("project", "Vehicle control", extract_terms("vehicle control")),),
    )
    candidates = await candidate_items(db_session, profile=profile)
    assert any(item.canonical_key == project.canonical_key for item in candidates)


@pytest.mark.asyncio
async def test_selected_domain_survives_recent_owned_discoveries(db_session) -> None:
    now = datetime.now(UTC)
    db_session.add_all([
        FeedItem(
            canonical_key=f"url:https://example.test/owned-{index}",
            kind="paper", title=f"Recent owned discovery {index}",
            url=f"https://example.test/owned-{index}", summary="Valid abstract.",
            published_at=now - timedelta(minutes=index), authors=[], domains=[],
            extra={"feed_acquisition_routes": ["project_profile"], "profile_user_ids": ["u1"]},
            visibility=FeedVisibility.PLATFORM,
        ) for index in range(20)
    ])
    domain_item = FeedItem(
        canonical_key="url:https://example.test/selected-domain", kind="paper",
        title="Prepared selected domain result", url="https://example.test/selected-domain",
        summary="Valid abstract.", published_at=now - timedelta(days=100),
        authors=[], domains=["070102"], extra={"feed_acquisition_routes": ["journal"],
                                                 "literature_preparation": "ready"},
        visibility=FeedVisibility.PLATFORM,
    )
    db_session.add(domain_item)
    await db_session.flush()
    selected = Profile(user_id="u1", domains=("070102",),
        projects=(ProjectSignal("p1", "Control", extract_terms("vehicle control")),))
    result = await candidate_items(db_session, profile=selected, batch_size=5)
    assert any(item.canonical_key == domain_item.canonical_key for item in result)
    excluded = await candidate_items(db_session, profile=Profile(user_id="u1", domains=("070103",)), batch_size=5)
    assert all(item.canonical_key != domain_item.canonical_key for item in excluded)


@pytest.mark.asyncio
async def test_journal_and_scholar_scan_past_unrelated_batches(db_session):
    now = datetime.now(UTC)
    rows = [FeedItem(canonical_key=f'url:https://example.test/noise-{n}', kind='paper',
        title=f'Unrelated paper {n}', url=f'https://example.test/noise-{n}',
        summary='Valid abstract.', published_at=now - timedelta(minutes=n),
        authors=[], domains=[], venue='Unfollowed Journal', extra={},
        visibility=FeedVisibility.PLATFORM) for n in range(25)]
    journal = FeedItem(canonical_key='url:https://example.test/journal-target', kind='paper',
        title='Subscribed journal article', url='https://example.test/journal-target',
        summary='Valid abstract.', published_at=now - timedelta(days=100),
        authors=[], domains=[], venue='Water Research',
        extra={'feed_acquisition_routes': ['journal']}, visibility=FeedVisibility.PLATFORM)
    scholar = FeedItem(canonical_key='url:https://example.test/scholar-target', kind='paper',
        title='Subscribed scholar article', url='https://example.test/scholar-target',
        summary='Valid abstract.', published_at=now - timedelta(days=101),
        authors=['Liu, Shaoda'], domains=[], venue='Other Journal',
        extra={'feed_acquisition_routes': ['scholar'], 'author_identities': [{'author_id': 'A123'}]}, visibility=FeedVisibility.PLATFORM)
    db_session.add_all([*rows, journal, scholar])
    await db_session.flush()
    result = await candidate_items(db_session, profile=Profile(user_id='u1',
        journals=frozenset({'waterresearch'}), scholars=frozenset({'Shaoda Liu'}), scholar_identities=({'author_id': 'A123'},)), batch_size=5)
    keys = {item.canonical_key for item in result}
    assert journal.canonical_key in keys and scholar.canonical_key in keys
