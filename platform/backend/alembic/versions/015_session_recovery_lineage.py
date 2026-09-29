"""Add explicit lineage for safe stale-session recovery.

Revision ID: 015_session_recovery_lineage
Revises: 014_project_resources
"""

import sqlalchemy as sa

from alembic import op

revision = "015_session_recovery_lineage"
down_revision = "014_project_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions", sa.Column("recovered_from_session_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "sessions", sa.Column("recovery_source_run_id", sa.String(128), nullable=True)
    )
    op.create_index(
        "ix_sessions_recovered_from_session_id",
        "sessions",
        ["recovered_from_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_recovery_source_run_id",
        "sessions",
        ["recovery_source_run_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_sessions_recovery_source",
        "sessions",
        ["tenant_id", "recovered_from_session_id"],
    )
    op.create_foreign_key(
        "fk_sessions_recovered_from_session",
        "sessions",
        "sessions",
        ["recovered_from_session_id"],
        ["session_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_sessions_recovered_from_session", "sessions", type_="foreignkey"
    )
    op.drop_constraint("uq_sessions_recovery_source", "sessions", type_="unique")
    op.drop_index("ix_sessions_recovery_source_run_id", table_name="sessions")
    op.drop_index("ix_sessions_recovered_from_session_id", table_name="sessions")
    op.drop_column("sessions", "recovery_source_run_id")
    op.drop_column("sessions", "recovered_from_session_id")
