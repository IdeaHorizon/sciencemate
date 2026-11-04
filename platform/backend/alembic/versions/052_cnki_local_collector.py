"""User-local CNKI collector, metadata only.

Revision ID: 052_cnki_local_collector
Revises: 051_project_two_states
"""
from alembic import op
import sqlalchemy as sa

revision = "052_cnki_local_collector"
down_revision = "051_project_two_states"
branch_labels = None
depends_on = None


def upgrade():
    uuid = sa.Uuid(as_uuid=False)
    date = sa.DateTime(timezone=True)
    op.create_table("cnki_devices",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("pair_hash", sa.String(64), unique=True),
        sa.Column("pair_expires_at", date, nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("credential_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", date, nullable=False), sa.Column("claimed_at", date),
        sa.Column("revoked_at", date), sa.Column("last_seen_at", date),
        sa.Column("last_report", sa.JSON(), nullable=False))
    op.create_index("ix_cnki_devices_user_id", "cnki_devices", ["user_id"])
    op.create_table("cnki_jobs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False), sa.Column("field", sa.String(8), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("recurring", sa.Boolean(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("result_limit", sa.Integer()), sa.Column("created_at", date, nullable=False),
        sa.Column("last_completed_at", date), sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("lease_device_id", uuid), sa.Column("lease_hash", sa.String(64)),
        sa.Column("lease_until", date), sa.Column("last_report_at", date),
        sa.Column("last_report", sa.JSON(), nullable=False))
    op.create_index("ix_cnki_jobs_user_id", "cnki_jobs", ["user_id"])
    op.create_table("cnki_papers",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("identity", sa.String(64), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", date, nullable=False),
        sa.UniqueConstraint("user_id", "identity", name="uq_cnki_paper_owner_identity"))
    op.create_index("ix_cnki_papers_user_id", "cnki_papers", ["user_id"])
    op.create_table("cnki_job_papers",
        sa.Column("job_id", uuid, sa.ForeignKey("cnki_jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("paper_id", uuid, sa.ForeignKey("cnki_papers.id", ondelete="CASCADE"), primary_key=True))


def downgrade():
    for name in ("cnki_job_papers", "cnki_papers", "cnki_jobs", "cnki_devices"):
        op.drop_table(name)
