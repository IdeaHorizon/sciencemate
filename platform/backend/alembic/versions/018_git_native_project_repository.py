"""Add Git-native Project repository projections and immutable receipts.

Revision ID: 018_git_project_repository
Revises: 017_instruction_governance
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "018_git_project_repository"
down_revision = "017_instruction_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for value in (
        "manuscript",
        "pre_registration",
        "writing_validation_report",
        "review_critique",
        "clean_results",
        "figure_package",
    ):
        op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{value}'")
    op.add_column("project_revisions", sa.Column("git_commit_sha", sa.String(40)))
    op.add_column("project_revisions", sa.Column("policy_hash", sa.String(64)))
    op.create_index(
        "ix_project_revisions_git_commit_sha",
        "project_revisions",
        ["git_commit_sha"],
        unique=False,
    )
    op.add_column("artifact_versions", sa.Column("git_commit_sha", sa.String(40)))
    op.add_column("artifact_versions", sa.Column("repository_path", sa.String(1000)))
    op.create_index(
        "ix_artifact_versions_git_commit_sha",
        "artifact_versions",
        ["git_commit_sha"],
        unique=False,
    )
    op.add_column("sessions", sa.Column("git_branch", sa.String(200)))
    op.add_column("sessions", sa.Column("git_base_commit_sha", sa.String(40)))
    op.add_column("sessions", sa.Column("git_head_commit_sha", sa.String(40)))
    op.add_column("sessions", sa.Column("git_worktree_path", sa.String(1000)))

    op.create_table(
        "artifact_receipts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("receipt_type", sa.String(64), nullable=False),
        sa.Column("issuer_kind", sa.String(24), nullable=False),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column(
            "source_session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id", ondelete="SET NULL"),
        ),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("receipt_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("git_commit_sha", sa.String(40), nullable=False),
        sa.Column("repository_path", sa.String(1000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "issuer_kind IN ('user', 'node', 'service')",
            name="ck_artifact_receipts_issuer_kind",
        ),
        sa.UniqueConstraint(
            "artifact_id",
            "revision_hash",
            "receipt_type",
            "issuer_kind",
            "issuer_id",
            name="uq_artifact_receipts_evidence",
        ),
    )
    op.create_index(
        "ix_artifact_receipts_revision",
        "artifact_receipts",
        ["project_id", "artifact_id", "revision_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_receipts_revision", table_name="artifact_receipts")
    op.drop_table("artifact_receipts")
    op.drop_column("sessions", "git_worktree_path")
    op.drop_column("sessions", "git_head_commit_sha")
    op.drop_column("sessions", "git_base_commit_sha")
    op.drop_column("sessions", "git_branch")
    op.drop_index("ix_artifact_versions_git_commit_sha", table_name="artifact_versions")
    op.drop_column("artifact_versions", "repository_path")
    op.drop_column("artifact_versions", "git_commit_sha")
    op.drop_index("ix_project_revisions_git_commit_sha", table_name="project_revisions")
    op.drop_column("project_revisions", "policy_hash")
    op.drop_column("project_revisions", "git_commit_sha")
