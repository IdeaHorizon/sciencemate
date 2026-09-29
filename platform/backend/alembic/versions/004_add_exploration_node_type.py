"""Add exploration node type to nodetype enum.

Revision ID: 004_add_exploration
Revises: 003_add_reflection
Create Date: 2026-04-25
"""

from alembic import op

revision = "004_add_exploration"
down_revision = "003_add_reflection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE nodetype ADD VALUE IF NOT EXISTS 'exploration'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values.
    # The value will remain but be unused.
    pass
