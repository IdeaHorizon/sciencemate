"""Add governance identity and scoped model backends.

Revision ID: 010_local_multiuser_runtime
Revises: 009_execution_cost_invariant
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "010_local_multiuser_runtime"
down_revision = "009_execution_cost_invariant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(32), nullable=False, server_default="researcher"),
    )
    op.add_column(
        "users",
        sa.Column("institution_id", sa.String(64), nullable=False, server_default="local"),
    )
    op.add_column(
        "users",
        sa.Column(
            "institution_name", sa.String(200), nullable=False, server_default="Local institution"
        ),
    )
    op.add_column("users", sa.Column("group_id", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("group_name", sa.String(200), nullable=True))
    op.create_check_constraint(
        "ck_users_supported_role",
        "users",
        "role IN ('institution_admin', 'group_admin', 'researcher')",
    )

    op.add_column("sessions", sa.Column("initiating_user_id", sa.String(64), nullable=True))
    op.create_index("ix_sessions_initiating_user", "sessions", ["initiating_user_id"])

    op.create_table(
        "model_backend_configs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("scope_kind", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(1000), nullable=True),
        sa.Column("credential_source", sa.String(32), nullable=False, server_default="none"),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("is_scope_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "scope_kind", "scope_id", "provider", "model", name="uq_model_backend_scope_model"
        ),
        sa.CheckConstraint(
            "scope_kind IN ('institution', 'group', 'personal')",
            name="ck_model_backend_scope_kind",
        ),
        sa.CheckConstraint(
            "credential_source IN ('none', 'environment', 'encrypted')",
            name="ck_model_backend_credential_source",
        ),
    )
    op.create_index(
        "ix_model_backend_scope", "model_backend_configs", ["scope_kind", "scope_id"]
    )

    op.create_table(
        "user_model_backend_preferences",
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "backend_id",
            UUID(as_uuid=False),
            sa.ForeignKey("model_backend_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("user_model_backend_preferences")
    op.drop_index("ix_model_backend_scope", table_name="model_backend_configs")
    op.drop_table("model_backend_configs")
    op.drop_index("ix_sessions_initiating_user", table_name="sessions")
    op.drop_column("sessions", "initiating_user_id")
    op.drop_constraint("ck_users_supported_role", "users", type_="check")
    op.drop_column("users", "group_name")
    op.drop_column("users", "group_id")
    op.drop_column("users", "institution_name")
    op.drop_column("users", "institution_id")
    op.drop_column("users", "role")
