"""Add research_intent column to project_configs.

Revision ID: 005_add_intent
Revises: 004_add_exploration
Create Date: 2026-04-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "005_add_intent"
down_revision = "004_add_exploration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_configs",
        sa.Column("research_intent", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_configs", "research_intent")
