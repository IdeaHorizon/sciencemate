"""Register all declared Harness output artifact types.

Revision ID: 019_harness_artifact_contract
Revises: 018_git_project_repository
"""

from alembic import op

revision = "019_harness_artifact_contract"
down_revision = "018_git_project_repository"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for value in (
        "hypothesis_innovation_report",
        "hypothesis_research_overview",
        "literature_index",
        "writing_preflight_plan",
    ):
        op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in-place.  Keeping an
    # unused value is preferable to rewriting artifact rows during rollback.
    pass
