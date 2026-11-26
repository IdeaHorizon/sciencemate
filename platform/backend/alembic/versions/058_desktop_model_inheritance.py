"""Identify desktop-supplied models without overwriting remote-owned settings."""
from alembic import op
import sqlalchemy as sa

revision = "058_desktop_model_inheritance"
down_revision = "057_project_deletion_intent"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_backend_configs", sa.Column("inherited_from_desktop", sa.String(64), nullable=True))
    op.add_column("model_backend_configs", sa.Column("inherited_backend_id", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("model_backend_configs", "inherited_backend_id")
    op.drop_column("model_backend_configs", "inherited_from_desktop")
