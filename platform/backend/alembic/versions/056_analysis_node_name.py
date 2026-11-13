"""Accept the canonical Analysis overview type without rewriting old evidence."""
from alembic import op

revision = "056_analysis_node_name"
down_revision = "055_merge_cnki_progress_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'analysis_research_overview'")


def downgrade() -> None:
    # PostgreSQL enum values cannot safely be removed in place; old rows and
    # their IDs remain valid on either side of this additive migration.
    pass
