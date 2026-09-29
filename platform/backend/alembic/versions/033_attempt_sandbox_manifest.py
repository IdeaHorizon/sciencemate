"""Freeze the sandbox capability manifest on RunAttempt before dispatch.

Revision ID: 033_attempt_sandbox_manifest
Revises: 032_drop_ingest_checkpoints
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "033_attempt_sandbox_manifest"
down_revision = "032_drop_ingest_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("run_attempts", sa.Column("sandbox_manifest", json_type, nullable=True))
    op.add_column(
        "run_attempts",
        sa.Column("sandbox_manifest_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_attempts", "sandbox_manifest_hash")
    op.drop_column("run_attempts", "sandbox_manifest")
