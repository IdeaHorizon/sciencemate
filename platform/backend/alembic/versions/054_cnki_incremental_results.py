"""Deliver uncapped CNKI results in incremental batches."""
from alembic import op
from app.services.cnki_schema import upgrade_incremental_results

revision = "054_cnki_incremental_results"
down_revision = "053_merge_cnki_execution_target"
branch_labels = None
depends_on = None


def upgrade():
    upgrade_incremental_results(op)


def downgrade():
    with op.batch_alter_table("cnki_papers") as batch:
        batch.drop_index("ix_cnki_papers_updates")
        batch.drop_column("updated_at")
