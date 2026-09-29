"""Add personal Research Settings and immutable Session snapshots.

Revision ID: 013_personal_research_settings
Revises: 012_session_message_run_identity
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "013_personal_research_settings"
down_revision = "012_session_message_run_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_research_settings",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "response_language", sa.String(16), nullable=False, server_default="auto"
        ),
        sa.Column(
            "citation_style", sa.String(24), nullable=False, server_default="author_year"
        ),
        sa.Column(
            "evidence_standard", sa.String(24), nullable=False, server_default="balanced"
        ),
        sa.Column(
            "instructions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", name="uq_user_research_settings_tenant_user"
        ),
        sa.CheckConstraint(
            "response_language IN ('auto', 'zh-CN', 'en')",
            name="ck_user_research_settings_language",
        ),
        sa.CheckConstraint(
            "citation_style IN ('author_year', 'numeric', 'apa')",
            name="ck_user_research_settings_citation",
        ),
        sa.CheckConstraint(
            "evidence_standard IN ('balanced', 'strict', 'exploratory')",
            name="ck_user_research_settings_evidence",
        ),
        sa.CheckConstraint("version >= 1", name="ck_user_research_settings_version"),
    )
    op.add_column(
        "sessions", sa.Column("research_settings_snapshot_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "sessions", sa.Column("research_settings_snapshot", JSONB, nullable=True)
    )
    op.create_index(
        "ix_sessions_research_settings_snapshot_id",
        "sessions",
        ["research_settings_snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_research_settings_snapshot_id", table_name="sessions")
    op.drop_column("sessions", "research_settings_snapshot")
    op.drop_column("sessions", "research_settings_snapshot_id")
    op.drop_table("user_research_settings")
