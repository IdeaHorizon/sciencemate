"""Reject negative Run usage cost.

Revision ID: 009_execution_cost_invariant
Revises: 008_execution_projection
"""

from alembic import op

revision = "009_execution_cost_invariant"
down_revision = "008_execution_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_runs_cost_nonnegative",
        "runs",
        "cost IS NULL OR cost >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_cost_nonnegative", "runs", type_="check")
