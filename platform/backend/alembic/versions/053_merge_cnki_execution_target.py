"""Join CNKI collection and upstream project execution-target migrations.

Revision ID: 053_merge_cnki_execution_target
Revises: 052_cnki_local_collector, 052_project_execution_target
"""

revision = "053_merge_cnki_execution_target"
down_revision = ("052_cnki_local_collector", "052_project_execution_target")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
