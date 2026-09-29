"""Add tenant-scoped Project logical resources.

Revision ID: 014_project_resources
Revises: 013_personal_research_settings
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "014_project_resources"
down_revision = "013_personal_research_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_resources",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(24), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(1000), nullable=True),
        sa.Column("workspace_binding", sa.String(200), nullable=True),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("secret_ref", sa.String(500), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resource_type IN ('storage', 'dataset', 'database', 'compute')",
            name="ck_project_resources_type",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "resource_type",
            "name",
            name="uq_project_resources_scope_name",
        ),
    )
    op.create_index(
        "ix_project_resources_tenant_project_enabled",
        "project_resources",
        ["tenant_id", "project_id", "is_enabled"],
    )
    op.create_index(
        "ix_project_resources_project_type",
        "project_resources",
        ["project_id", "resource_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_resources_project_type", table_name="project_resources")
    op.drop_index(
        "ix_project_resources_tenant_project_enabled", table_name="project_resources"
    )
    op.drop_table("project_resources")
