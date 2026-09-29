"""Subscribe the feed to five core marine-environment journals.

Revision ID: 033_marine_environment_rss
Revises: 032_drop_ingest_checkpoints

This revision reached the node20 database before its feature branch was
retired.  Alembic revision files are immutable deployment history: removing a
revision that a database has already recorded makes every later upgrade
impossible.  Keep it in the graph and merge its head with the main sandbox
manifest branch in 034.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "033_marine_environment_rss"
down_revision = "032_drop_ingest_checkpoints"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
_SOURCES = (
    ("Marine Pollution Bulletin", "0025326X"),
    ("Science of the Total Environment", "00489697"),
    ("Environmental Pollution", "02697491"),
    ("Marine Environmental Research", "01411136"),
    ("Journal of Hazardous Materials", "03043894"),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM feed_sources WHERE kind = 'rss'")
        ).fetchall()
    }
    rows = []
    for name, issn in _SOURCES:
        if name in existing:
            continue
        config = {
            "url": f"https://rss.sciencedirect.com/publication/science/{issn}",
            "venue": name,
        }
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "kind": "rss",
                "name": name,
                "config": config if bind.dialect.name == "postgresql" else json.dumps(config),
                "domains": ["environment.marine"],
                "poll_interval_seconds": 43200,
            }
        )
    if rows:
        table = sa.table(
            "feed_sources",
            sa.column("id", UUID(as_uuid=False)),
            sa.column("kind", sa.String),
            sa.column("name", sa.String),
            sa.column("config", _JSON),
            sa.column("domains", _JSON),
            sa.column("poll_interval_seconds", sa.Integer),
        )
        op.bulk_insert(table, rows)


def downgrade() -> None:
    names = [name for name, _ in _SOURCES]
    op.get_bind().execute(
        sa.text("DELETE FROM feed_sources WHERE kind = 'rss' AND name IN :names").bindparams(
            sa.bindparam("names", value=tuple(names), expanding=True)
        )
    )
