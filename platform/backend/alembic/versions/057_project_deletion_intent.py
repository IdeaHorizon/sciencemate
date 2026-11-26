"""Retain project identity until its writers and data have been removed."""
from alembic import op
import sqlalchemy as sa

revision = "057_project_deletion_intent"
down_revision = "056_analysis_node_name"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("projects", "deletion_requested_at")
