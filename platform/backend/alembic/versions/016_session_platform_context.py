"""Add immutable, bounded platform context to Research Sessions.

Revision ID: 016_session_platform_context
Revises: 015_session_recovery_lineage
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "016_session_platform_context"
down_revision = "015_session_recovery_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("platform_context_snapshot", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "platform_context_snapshot")
