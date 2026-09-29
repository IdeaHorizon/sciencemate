"""Merge the published RSS and RunAttempt sandbox-manifest heads.

Revision ID: 034_merge_rss_sandbox_heads
Revises: 033_marine_environment_rss, 033_attempt_sandbox_manifest
"""

revision = "034_merge_rss_sandbox_heads"
down_revision = (
    "033_marine_environment_rss",
    "033_attempt_sandbox_manifest",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
