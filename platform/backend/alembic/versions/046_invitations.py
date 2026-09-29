"""invitations: 组织服务器上「谁能进来」终于有了机制

在此之前 `/register` 全开放，而没有任何路径能写用户角色 —— 门开着，管门的人不存在。

Revision ID: 046_invitations
Revises: 045_token_bound_to_credential
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "046_invitations"
down_revision = "045_token_bound_to_credential"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("institution_id", sa.String(64), nullable=False),
        sa.Column("institution_name", sa.String(200), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("group_name", sa.String(200), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("created_by_user_id", UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", UUID(as_uuid=False), nullable=True),
    )
    op.create_index("ix_invitations_code_hash", "invitations", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invitations_code_hash", table_name="invitations")
    op.drop_table("invitations")
