"""Initial schema — all 18 tables for Phase 0.

Revision ID: 001_initial
Revises:
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 1. users ──────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(320), unique=True, nullable=False, index=True),
        sa.Column("hashed_password", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("preferences", JSONB, nullable=True),
        sa.Column("personal_context_md", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 2. projects ───────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("research_domain", sa.String(200), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "paused", "completed", "archived", name="projectstatus"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "entry_type",
            sa.Enum(
                "fuzzy_idea",
                "existing_proposal",
                "mid_project",
                "specific_task",
                "failure_recovery",
                name="entrytype",
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 3. project_configs ────────────────────────────────────────────────
    op.create_table(
        "project_configs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
            index=True,
        ),
        sa.Column(
            "operation_mode",
            sa.Enum("assisted", "autonomous", name="operationmode"),
            nullable=False,
            server_default="assisted",
        ),
        sa.Column(
            "reporting_level",
            sa.Enum("low", "medium", "high", name="reportinglevel"),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("preferred_model", sa.String(100), nullable=True),
        sa.Column("tool_whitelist", JSONB, nullable=True),
        sa.Column("harness_overrides", JSONB, nullable=True),
        sa.Column("max_concurrent_branches", sa.Integer, server_default="3"),
        sa.Column("cycle_soft_limit", sa.Integer, server_default="3"),
        sa.Column("cycle_hard_limit", sa.Integer, server_default="5"),
        sa.Column("notification_channels", JSONB, nullable=True),
    )

    # ── 4. branches (FK to nodes added later due to circular dep) ─────────
    op.create_table(
        "branches",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "merged", "abandoned", name="branchstatus"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("hypothesis", sa.Text, nullable=True),
        sa.Column(
            "parent_branch_id",
            UUID(as_uuid=False),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # fork_point_node_id FK added after nodes table
        sa.Column("fork_point_node_id", UUID(as_uuid=False), nullable=True),
        sa.Column("is_main", sa.Boolean, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── 5. nodes ──────────────────────────────────────────────────────────
    op.create_table(
        "nodes",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "branch_id",
            UUID(as_uuid=False),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "type",
            sa.Enum(
                "survey",
                "planning",
                "experiment",
                "data_process",
                "analysis",
                "writing",
                "review",
                name="nodetype",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "planned", "ready", "active", "completed", "failed", "paused", "archived",
                name="nodestatus",
            ),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("iteration", sa.Integer, server_default="1"),
        sa.Column(
            "parent_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("harness_config", JSONB, nullable=True),
        sa.Column("expected_inputs", JSONB, nullable=True),
        sa.Column("expected_outputs", JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_metadata", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_nodes_project_status", "nodes", ["project_id", "status"])
    op.create_index("ix_nodes_project_type", "nodes", ["project_id", "type"])

    # Add deferred FK from branches to nodes
    op.create_foreign_key(
        "fk_branches_fork_point_node",
        "branches",
        "nodes",
        ["fork_point_node_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── 6. edges ──────────────────────────────────────────────────────────
    op.create_table(
        "edges",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "source_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "target_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "type",
            sa.Enum("depends_on", "informs", "triggers", "iterates", name="edgetype"),
            nullable=False,
        ),
        sa.Column("extra_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_edges_source_target", "edges", ["source_node_id", "target_node_id"], unique=True
    )

    # ── 7. graph_snapshots ────────────────────────────────────────────────
    op.create_table(
        "graph_snapshots",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("trigger_type", sa.String(100), nullable=False),
        sa.Column(
            "trigger_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("graph_state", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 8. artifacts ──────────────────────────────────────────────────────
    op.create_table(
        "artifacts",
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
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "paper_pdf",
                "dataset",
                "code",
                "figure",
                "table",
                "report",
                "experiment_log",
                "research_plan",
                "survey_report",
                "analysis_report",
                "other",
                name="artifacttype",
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("current_version", sa.Integer, server_default="1"),
        sa.Column("extra_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 9. artifact_versions ──────────────────────────────────────────────
    op.create_table(
        "artifact_versions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("is_milestone", sa.Boolean, server_default=sa.text("false")),
        sa.Column("milestone_note", sa.Text, nullable=True),
        sa.Column(
            "created_by_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 10. artifact_relations ────────────────────────────────────────────
    op.create_table(
        "artifact_relations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "source_artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "target_artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "type",
            sa.Enum("derived_from", "references", "supersedes", name="artifactrelationtype"),
            nullable=False,
        ),
        sa.Column("extra_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 11. kb_entries ────────────────────────────────────────────────────
    op.create_table(
        "kb_entries",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "source_type",
            sa.Enum("paper", "news", "documentation", "dataset_info", "benchmark", name="kbsourcetype"),
            nullable=False,
        ),
        sa.Column(
            "scope",
            sa.Enum("organization", "project", name="kbscope"),
            nullable=False,
            server_default="project",
        ),
        sa.Column(
            "quality_tier",
            sa.Enum("tier1", "tier2", "tier3", "tier4", name="qualitytier"),
            nullable=False,
            server_default="tier3",
        ),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("authors", JSONB, nullable=True),
        sa.Column("source_ref", sa.String(500), nullable=True),
        sa.Column("doi", sa.String(200), nullable=True, unique=True),
        sa.Column("publication_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("venue", sa.String(300), nullable=True),
        sa.Column("abstract", sa.Text, nullable=True),
        sa.Column("full_text", sa.Text, nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True, index=True),
        sa.Column("tags", ARRAY(sa.String), nullable=True),
        sa.Column("added_by", sa.String(50), nullable=False, server_default="system"),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("storage_key", sa.String(1000), nullable=True),
    )
    op.create_index("ix_kb_entries_project_scope", "kb_entries", ["project_id", "scope"])
    op.create_index("ix_kb_entries_quality", "kb_entries", ["quality_tier"])

    # ── 12. kb_chunks ─────────────────────────────────────────────────────
    op.create_table(
        "kb_chunks",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "kb_entry_id",
            UUID(as_uuid=False),
            sa.ForeignKey("kb_entries.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("section", sa.String(300), nullable=True),
        sa.Column("page_number", sa.Integer, nullable=True),
        sa.Column("figure_ref", sa.String(100), nullable=True),
        sa.Column("table_ref", sa.String(100), nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chunks_entry_index", "kb_chunks", ["kb_entry_id", "chunk_index"])

    # Add vector column with raw SQL (pgvector type not available in plain SA)
    op.execute("ALTER TABLE kb_chunks ADD COLUMN embedding vector(1536)")

    # ── 13. memory_entries ────────────────────────────────────────────────
    op.create_table(
        "memory_entries",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "layer",
            sa.Enum("organization", "project", "user", "session", name="memorylayer"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("session_id", UUID(as_uuid=False), nullable=True, index=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "type",
            sa.Enum("factual", "judgmental", "preference", "rule", name="memorytype"),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.Enum("high", "medium", "low", name="confidencelevel"),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "status",
            sa.Enum("active", "stale", "archived", "conflicted", "superseded", name="memorystatus"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("tags", ARRAY(sa.String), nullable=True),
        sa.Column("topic", sa.String(200), nullable=True),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "superseded_by_id",
            UUID(as_uuid=False),
            sa.ForeignKey("memory_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("conflicts_with_ids", ARRAY(sa.String), nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_memory_project_layer_status", "memory_entries", ["project_id", "layer", "status"]
    )
    op.create_index("ix_memory_project_type", "memory_entries", ["project_id", "type"])
    op.create_index("ix_memory_user", "memory_entries", ["user_id", "layer"])

    # ── 14. research_skills ───────────────────────────────────────────────
    op.create_table(
        "research_skills",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("node_types", ARRAY(sa.String), nullable=False),
        sa.Column("steps", JSONB, nullable=False),
        sa.Column("tools_required", ARRAY(sa.String), nullable=True),
        sa.Column("parameters", JSONB, nullable=True),
        sa.Column("preconditions", JSONB, nullable=True),
        sa.Column("expected_outcome", sa.Text, nullable=True),
        sa.Column("pitfalls", JSONB, nullable=True),
        sa.Column(
            "extracted_from_node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("extracted_from_execution", JSONB, nullable=True),
        sa.Column("usage_count", sa.Integer, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success_rate", sa.Float, nullable=True),
        sa.Column("is_validated", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_deprecated", sa.Boolean, server_default=sa.text("false")),
        sa.Column("scope", sa.String(20), nullable=False, server_default="project"),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("patch_history", JSONB, nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_skills_scope", "research_skills", ["scope"])

    # ── 15. evidence_chains ───────────────────────────────────────────────
    op.create_table(
        "evidence_chains",
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
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claim_text", sa.Text, nullable=False),
        sa.Column(
            "provenance_level",
            sa.Enum("hard", "soft", "none", name="provenancelevel"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("supported", "contradicted", "insufficient", "unverified", name="claimstatus"),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("claim_location", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_project_node", "evidence_chains", ["project_id", "node_id"]
    )
    op.create_index("ix_evidence_status", "evidence_chains", ["status"])

    # ── 16. claims ────────────────────────────────────────────────────────
    op.create_table(
        "claims",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "evidence_chain_id",
            UUID(as_uuid=False),
            sa.ForeignKey("evidence_chains.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "evidence_type",
            sa.Enum("supports", "contradicts", "partially_supports", name="evidencetype"),
            nullable=False,
        ),
        sa.Column(
            "kb_chunk_id",
            UUID(as_uuid=False),
            sa.ForeignKey("kb_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("data_reference", sa.Text, nullable=True),
        sa.Column("evidence_text", sa.Text, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("is_verified", sa.Boolean, server_default=sa.text("false")),
        sa.Column("verified_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 17. budgets ───────────────────────────────────────────────────────
    op.create_table(
        "budgets",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "budget_type",
            sa.Enum("llm_tokens", "compute", "api_calls", "storage", name="budgettype"),
            nullable=False,
        ),
        sa.Column("total_budget", sa.Float, nullable=False),
        sa.Column("consumed", sa.Float, nullable=False, server_default="0"),
        sa.Column("alert_threshold", sa.Float, server_default="0.8"),
        sa.Column("pause_threshold", sa.Float, server_default="0.95"),
        sa.Column("hard_stop_threshold", sa.Float, server_default="1.0"),
        sa.Column("alert_sent", sa.Boolean, server_default=sa.text("false")),
        sa.Column("pause_sent", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_hard_stopped", sa.Boolean, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_budget_project_type", "budgets", ["project_id", "budget_type"], unique=True
    )

    # ── 18. consumption_records ───────────────────────────────────────────
    op.create_table(
        "consumption_records",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "budget_id",
            UUID(as_uuid=False),
            sa.ForeignKey("budgets.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "node_id",
            UUID(as_uuid=False),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source",
            sa.Enum("llm_call", "tool_call", "embedding", "storage_write", name="consumptionsource"),
            nullable=False,
        ),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_consumption_project_time", "consumption_records", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("consumption_records")
    op.drop_table("budgets")
    op.drop_table("claims")
    op.drop_table("evidence_chains")
    op.drop_table("research_skills")
    op.drop_table("memory_entries")
    # Drop vector column before dropping table
    op.drop_table("kb_chunks")
    op.drop_table("kb_entries")
    op.drop_table("artifact_relations")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
    op.drop_table("graph_snapshots")
    op.drop_table("edges")
    # Drop deferred FK first
    op.drop_constraint("fk_branches_fork_point_node", "branches", type_="foreignkey")
    op.drop_table("nodes")
    op.drop_table("branches")
    op.drop_table("project_configs")
    op.drop_table("projects")
    op.drop_table("users")

    # Drop enums
    for enum_name in [
        "consumptionsource", "budgettype", "evidencetype", "claimstatus",
        "provenancelevel", "memorystatus", "confidencelevel", "memorytype",
        "memorylayer", "qualitytier", "kbscope", "kbsourcetype",
        "artifactrelationtype", "artifacttype", "edgetype", "nodestatus",
        "nodetype", "branchstatus", "operationmode", "reportinglevel",
        "entrytype", "projectstatus",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    op.execute("DROP EXTENSION IF EXISTS vector")
