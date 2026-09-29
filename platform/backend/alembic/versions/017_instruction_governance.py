"""Add versioned instruction governance and immutable Session snapshots.

Revision ID: 017_instruction_governance
Revises: 016_session_platform_context
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "017_instruction_governance"
down_revision = "016_session_platform_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instruction_documents",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(32), nullable=False),
        sa.Column("next_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("published_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_version_id", UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "scope_kind", "scope_id", name="uq_instruction_document_scope"
        ),
        sa.CheckConstraint(
            "scope_kind IN ('institution', 'group', 'project', 'personal')",
            name="ck_instruction_document_scope_kind",
        ),
        sa.CheckConstraint("next_version >= 1", name="ck_instruction_document_next_version"),
        sa.CheckConstraint(
            "published_revision >= 0", name="ck_instruction_document_published_revision"
        ),
    )
    op.create_index(
        "ix_instruction_documents_tenant_scope",
        "instruction_documents",
        ["tenant_id", "scope_kind", "scope_id"],
    )
    op.create_table(
        "instruction_versions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("instruction_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "published_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "version", name="uq_instruction_version_number"
        ),
        sa.CheckConstraint("version >= 1", name="ck_instruction_version_positive"),
        sa.CheckConstraint(
            "status IN ('proposed', 'draft', 'published')", name="ck_instruction_version_status"
        ),
    )
    op.create_index(
        "ix_instruction_versions_document",
        "instruction_versions",
        ["tenant_id", "document_id", "version"],
    )
    op.create_table(
        "instruction_snapshots",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "initiating_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "session_id", name="uq_instruction_snapshot_session"),
    )
    op.create_index(
        "ix_instruction_snapshots_tenant_project",
        "instruction_snapshots",
        ["tenant_id", "project_id"],
    )
    op.add_column(
        "sessions", sa.Column("instruction_snapshot_id", UUID(as_uuid=False), nullable=True)
    )
    op.create_index("ix_sessions_instruction_snapshot_id", "sessions", ["instruction_snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_instruction_snapshot_id", table_name="sessions")
    op.drop_column("sessions", "instruction_snapshot_id")
    op.drop_index("ix_instruction_snapshots_tenant_project", table_name="instruction_snapshots")
    op.drop_table("instruction_snapshots")
    op.drop_index("ix_instruction_versions_document", table_name="instruction_versions")
    op.drop_table("instruction_versions")
    op.drop_index("ix_instruction_documents_tenant_scope", table_name="instruction_documents")
    op.drop_table("instruction_documents")
