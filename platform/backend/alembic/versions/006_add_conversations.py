"""Add conversations table for persistent chat history.

Revision ID: 006_add_conversations
Revises: 005_add_intent
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "006_add_conversations"
down_revision = "005_add_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("title", sa.String(300), nullable=False, server_default="New conversation"),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("messages", JSONB, nullable=False, server_default="[]"),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conv_user_project_updated",
        "conversations",
        ["user_id", "project_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_conv_user_project_updated", table_name="conversations")
    op.drop_table("conversations")
