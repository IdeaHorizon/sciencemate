"""Join upstream CNKI migrations with local incremental result delivery.

Both existing merge revisions may already be installed. Retain their history
and converge without recreating tables or changing collected paper data.
"""
revision = "055_merge_cnki_progress_heads"
down_revision = ("054_cnki_incremental_results", "053_merge_cnki_execution_heads")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
