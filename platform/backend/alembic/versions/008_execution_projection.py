"""Add durable multi-tenant execution projection.

Revision ID: 008_execution_projection
Revises: 007_knowledge_v2
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "008_execution_projection"
down_revision = "007_knowledge_v2"
branch_labels = None
depends_on = None

IDENTITY_COLUMNS = ("tenant_id", "workspace_id", "project_id", "session_id")


def _identity_columns() -> list[sa.Column]:
    return [
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
    ]


def _session_scope_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        IDENTITY_COLUMNS,
        [f"sessions.{column}" for column in IDENTITY_COLUMNS],
        ondelete="CASCADE",
        name=name,
    )


def _run_scope_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        (*IDENTITY_COLUMNS, "run_id"),
        [*(f"runs.{column}" for column in IDENTITY_COLUMNS), "runs.id"],
        ondelete="CASCADE",
        name=name,
    )


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("next_event_sequence", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(*IDENTITY_COLUMNS, name="uq_sessions_scope"),
        sa.CheckConstraint("next_event_sequence >= 0", name="ck_sessions_sequence_nonnegative"),
    )
    op.create_index("ix_sessions_tenant_project", "sessions", ["tenant_id", "project_id"])

    op.create_table(
        "runs",
        sa.Column("id", sa.String(128), primary_key=True),
        *_identity_columns(),
        sa.Column("parent_run_id", sa.String(128), nullable=True),
        sa.Column("node_type", sa.String(100), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("current_attempt_no", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("cost_currency", sa.String(3), nullable=True),
        sa.Column("usage_coverage", sa.String(20), nullable=False, server_default="partial"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _session_scope_fk("fk_runs_session_scope"),
        sa.UniqueConstraint(*IDENTITY_COLUMNS, "id", name="uq_runs_scope_id"),
        sa.CheckConstraint("current_attempt_no >= 0", name="ck_runs_attempt_nonnegative"),
        sa.CheckConstraint("prompt_tokens >= 0", name="ck_runs_prompt_tokens_nonnegative"),
        sa.CheckConstraint("completion_tokens >= 0", name="ck_runs_completion_tokens_nonnegative"),
        sa.CheckConstraint("total_tokens >= 0", name="ck_runs_total_tokens_nonnegative"),
        sa.CheckConstraint("retry_count >= 0", name="ck_runs_retry_count_nonnegative"),
        sa.CheckConstraint("usage_coverage IN ('complete', 'partial')", name="ck_runs_coverage"),
    )
    op.create_index(
        "ix_runs_tenant_created",
        "runs",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("ix_runs_tenant_session", "runs", ["tenant_id", "session_id"])

    op.create_table(
        "run_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        *_identity_columns(),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="created"),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_ref", sa.String(300), nullable=True),
        sa.Column("exit_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _run_scope_fk("fk_attempts_run_scope"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "attempt_no", name="uq_attempts_tenant_run_number"
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_attempts_number_positive"),
    )
    op.create_index("ix_attempts_tenant_session", "run_attempts", ["tenant_id", "session_id"])

    op.create_table(
        "execution_events",
        sa.Column("id", sa.String(64), primary_key=True),
        *_identity_columns(),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("parent_run_id", sa.String(128), nullable=True),
        sa.Column("attempt_no", sa.Integer, nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source", JSONB, nullable=False, server_default="{}"),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("source_identity", sa.String(64), nullable=True),
        sa.Column("file_identity", sa.String(200), nullable=True),
        sa.Column("byte_offset", sa.Integer, nullable=True),
        sa.Column("raw_line_hash", sa.String(64), nullable=True),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _session_scope_fk("fk_events_session_scope"),
        sa.UniqueConstraint("tenant_id", "session_id", "sequence", name="uq_events_sequence"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "file_identity",
            "byte_offset",
            "raw_line_hash",
            name="uq_events_raw_source",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_events_sequence_positive"),
        sa.CheckConstraint("schema_version = 1", name="ck_events_schema_v1"),
    )
    op.create_index(
        "ix_events_session_sequence",
        "execution_events",
        ["tenant_id", "session_id", "sequence"],
    )
    op.create_index("ix_events_tenant_run", "execution_events", ["tenant_id", "run_id"])

    op.create_table(
        "commands",
        sa.Column("id", sa.String(64), primary_key=True),
        *_identity_columns(),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="accepted"),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _session_scope_fk("fk_commands_session_scope"),
        sa.UniqueConstraint(
            "tenant_id", "actor_user_id", "idempotency_key", name="uq_commands_idempotency"
        ),
    )
    op.create_index("ix_commands_tenant_session", "commands", ["tenant_id", "session_id"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.String(128), primary_key=True),
        *_identity_columns(),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("subtype", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.Column("choices", JSONB, nullable=False, server_default="[]"),
        sa.Column("recommended_choice_id", sa.String(128), nullable=True),
        sa.Column("selected_choice_id", sa.String(128), nullable=True),
        sa.Column("authority_type", sa.String(32), nullable=False),
        sa.Column("authority_subjects", JSONB, nullable=False, server_default="[]"),
        sa.Column("required_approval_count", sa.Integer, nullable=False),
        sa.Column("action_set_version", sa.String(64), nullable=False),
        sa.Column("policy_snapshot_id", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_responses", JSONB, nullable=False, server_default="[]"),
        sa.Column("accepted_response_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _run_scope_fk("fk_decisions_run_scope"),
        sa.CheckConstraint(
            "required_approval_count >= 1", name="ck_decisions_approval_count_positive"
        ),
        sa.CheckConstraint(
            "accepted_response_count >= 0", name="ck_decisions_response_count_nonnegative"
        ),
    )
    op.create_index("ix_decisions_current", "decisions", ["tenant_id", "session_id", "status"])

    op.create_table(
        "transcript_ingest_checkpoints",
        sa.Column("id", sa.String(64), primary_key=True),
        *_identity_columns(),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("file_identity", sa.String(200), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("byte_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_line_hash", sa.String(64), nullable=True),
        sa.Column("adapter_state", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _run_scope_fk("fk_checkpoints_run_scope"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "file_identity", name="uq_checkpoints_file_identity"
        ),
        sa.CheckConstraint("byte_offset >= 0", name="ck_checkpoints_offset_nonnegative"),
    )


def downgrade() -> None:
    op.drop_table("transcript_ingest_checkpoints")
    op.drop_table("decisions")
    op.drop_table("commands")
    op.drop_table("execution_events")
    op.drop_table("run_attempts")
    op.drop_table("runs")
    op.drop_table("sessions")
