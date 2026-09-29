"""Add missing artifact type enum values.

Revision ID: 002_add_artifact_types
Revises: 001_initial
Create Date: 2026-04-24
"""

from alembic import op

revision = "002_add_artifact_types"
down_revision = "001_initial"
branch_labels = None
depends_on = None

# Values that exist in Python ArtifactType but not in the DB enum
NEW_VALUES = [
    "paper_pdf",
    "paper_draft",
    "paper_outline",
    "review_report",
    "data_profile",
    "data_pipeline",
]


def upgrade() -> None:
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; no-op
    pass
