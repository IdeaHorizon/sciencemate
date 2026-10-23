"""Read an institution-owned catalogue into an institution-only feed projection."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.feed import FeedItem, FeedItemKind, FeedVisibility
from app.services import school_library
from app.services.feed import windows
from app.services.feed.literature_projection import publication_datetime


def _key(institution_id: str, record_id: str) -> str:
    return "school:" + uuid.uuid5(uuid.NAMESPACE_URL, f"{institution_id}\0{record_id}").hex


def _values(row: dict, institution_id: str) -> dict:
    doi = school_library._doi(row.get("doi"))
    record_id = str(row["id"])
    link = f"/api/v1/literature/school/fulltext/{record_id}" if row.get("has_fulltext") else ""
    return {
        "kind": FeedItemKind.PAPER,
        "title": str(row["title"])[:2000],
        "url": str(row.get("url") or (f"https://doi.org/{doi}" if doi else "")) or None,
        "summary": str(row.get("abstract") or "")[:1200] or None,
        "authors": list(row.get("authors") or [])[:50],
        "venue": str(row.get("venue") or "")[:200] or None,
        "published_at": publication_datetime(row.get("published_at"), row.get("year")),
        "domains": [],
        "visibility": FeedVisibility.INSTITUTION,
        "organization_id": institution_id,
        "extra": {
            "school_library": row["library_name"],
            "school_record_id": record_id,
            "school_fulltext_url": link,
            "doi": doi,
            "feed_acquisition_routes": ["journal"],
            "literature_preparation": "ready",
            "publication_date_precision": windows.publication_precision(row.get("published_at")),
        },
    }


async def sync_school_feed(db, *, now: datetime | None = None) -> dict[str, int]:
    """Refresh school metadata only; never copy full text into platform storage."""
    config = school_library._configuration()
    if config is None:
        return {"seen": 0, "stored": 0, "updated": 0, "withdrawn": 0}
    _, institution_id, _ = config
    moment = now or datetime.now(UTC)
    since = windows.academic_cutoff(moment).date().isoformat()
    rows = await asyncio.to_thread(
        school_library.recent_records, institution_id=institution_id, since=since
    )
    current = set()
    stored = updated = withdrawn = 0
    for row in rows:
        key = _key(institution_id, str(row["id"]))
        current.add(key)
        values = _values(row, institution_id)
        item = (
            await db.execute(select(FeedItem).where(FeedItem.canonical_key == key))
        ).scalar_one_or_none()
        if item is None:
            payload = {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                "canonical_key": key,
                **values,
            }
            if db.get_bind().dialect.name == "postgresql":
                result = await db.execute(
                    pg_insert(FeedItem).values(**payload).on_conflict_do_nothing()
                )
                if result.rowcount:
                    stored += 1
                    continue
                item = (
                    await db.execute(select(FeedItem).where(FeedItem.canonical_key == key))
                ).scalar_one()
            else:
                db.add(FeedItem(**payload))
                stored += 1
                continue
        for field, value in values.items():
            setattr(item, field, value)
        updated += 1

    old = (
        (
            await db.execute(
                select(FeedItem).where(
                    FeedItem.visibility == FeedVisibility.INSTITUTION,
                    FeedItem.organization_id == institution_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for item in old:
        if item.canonical_key not in current:
            item.visibility = "withdrawn"
            withdrawn += 1
    if stored or updated or withdrawn:
        from app.services.feed.pool_cache import clear

        clear()
    return {"seen": len(rows), "stored": stored, "updated": updated, "withdrawn": withdrawn}
