"""Database round trips must preserve an instant, including legacy SQLite data."""
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, Table, create_engine, insert, select, text

from app.database import UTCDateTime


def test_sqlite_roundtrip_returns_aware_utc_and_interprets_legacy_rows():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    events = Table("utc_events", metadata, Column("at", UTCDateTime()))
    metadata.create_all(engine)
    local = datetime(2026, 9, 13, 12, 34, 56, tzinfo=timezone(timedelta(hours=8)))
    with engine.begin() as connection:
        connection.execute(insert(events).values(at=local))
        connection.execute(text("INSERT INTO utc_events VALUES ('2026-09-13 04:34:56')"))
        values = connection.execute(select(events.c.at)).scalars().all()
    assert values == [local.astimezone(UTC)] * 2
    assert all(value.tzinfo is UTC for value in values)
    engine.dispose()
