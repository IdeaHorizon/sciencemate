"""Add reflection_mode to project_configs and reflection_results table.

Revision ID: 003_add_reflection
Revises: 002_add_artifact_types
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "003_add_reflection"
down_revision = "002_add_artifact_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create reflectionmode enum and add column to project_configs
    op.execute("CREATE TYPE reflectionmode AS ENUM ('off', 'light', 'deep')")
    op.add_column(
        "project_configs",
        sa.Column(
            "reflection_mode",
            sa.Enum("off", "light", "deep", name="reflectionmode", create_type=False),
            nullable=False,
            server_default="off",
        ),
    )

    # 2. Create reflection_results table
    op.create_table(
        "reflection_results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("node_type", sa.String(50), nullable=False),
        sa.Column("transition", sa.String(100), nullable=False),
        sa.Column("findings", JSONB, nullable=False, server_default="[]"),
        sa.Column("quality_score", sa.Float, nullable=True),
        sa.Column("must_answer_questions", JSONB, nullable=False, server_default="[]"),
        sa.Column("identified_gaps", JSONB, nullable=False, server_default="[]"),
        sa.Column("overall_assessment", sa.Text, nullable=True),
        sa.Column("max_severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("action_taken", sa.String(30), nullable=False, server_default="passed"),
        sa.Column("model_used", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Float, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("reflection_results")
    op.drop_column("project_configs", "reflection_mode")
    op.execute("DROP TYPE IF EXISTS reflectionmode")
