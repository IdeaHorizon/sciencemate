"""Knowledge System v2 — structured knowledge graph, library artifacts, memory proposals.

Revision ID: 007_knowledge_v2
Revises: 006_add_conversations

Changes:
- artifacts: add scope, organization_id, paper_metadata, dataset_metadata,
  is_indexed_in_kb, kb_chunk_count, indexed_at, structured_summary,
  concept_ids, content_hash; make project_id nullable
- kb_chunks: add artifact_id, artifact_version, citability, usage_count,
  last_accessed_at; make kb_entry_id nullable
- memory_entries: add proposal_id, evidence_ids, concept_ids,
  derived_from_artifact_id, embedding, verification_count
- research_skills: add extracted_from_projects, validated_run_count,
  failure_cases, promotion_status, promotion_requested_at, promotion_approved_by
- New tables: kb_claims, kb_concepts, kb_concept_aliases, kb_relations,
  kb_syntheses, memory_proposals, watchlists, tool_audit_logs,
  kb_claim_chunks, kb_claim_concepts, synthesis_sources, kb_concept_usages,
  dreaming_jobs, dreaming_runs
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "007_knowledge_v2"
down_revision = "006_add_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    artifact_scope_enum = sa.Enum("project", "library", name="artifactscope")
    artifact_scope_enum.create(op.get_bind(), checkfirst=True)

    # ================================================================
    # 1. New tables (create before adding FKs that reference them)
    # ================================================================

    # ── memory_proposals (must exist before memory_entries.proposal_id FK) ──
    op.create_table(
        "memory_proposals",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("proposed_content", sa.Text, nullable=False),
        sa.Column("proposed_layer", sa.String(20), nullable=False),
        sa.Column("proposed_type", sa.String(20), nullable=False),
        sa.Column("proposed_confidence", sa.String(20), nullable=False),
        sa.Column("proposed_topic", sa.String(200), nullable=True),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("conflicts_with", ARRAY(sa.String), nullable=True),
        sa.Column("supersedes", ARRAY(sa.String), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "auto_approved", "merged",
                    name="proposalstatus"),
            nullable=False, server_default="pending",
        ),
        sa.Column("decided_by", sa.String(100), nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_memory_id", UUID(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_proposals_project_status", "memory_proposals", ["project_id", "status"])

    # ── kb_concepts ──
    op.create_table(
        "kb_concepts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=False), nullable=True, index=True),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column(
            "concept_type",
            sa.Enum("method", "model", "equation", "dataset", "benchmark", "metric",
                    "field", "technique", "material", "phenomenon", "software",
                    "organization", "other", name="kbconcepttype"),
            nullable=False,
        ),
        sa.Column("short_definition", sa.Text, nullable=True),
        sa.Column("living_summary", sa.Text, nullable=True),
        sa.Column("living_summary_history", JSONB, nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("extra_metadata", JSONB, nullable=True),
        sa.Column("related_concept_ids", ARRAY(sa.String), nullable=True),
        sa.Column("source_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claim_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("project_usage_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_refined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_kb_concepts_org", "kb_concepts", ["organization_id"])
    op.create_index("ix_kb_concepts_type", "kb_concepts", ["concept_type"])

    # ── kb_concept_aliases ──
    op.create_table(
        "kb_concept_aliases",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "concept_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_concepts.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("alias", sa.String(500), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("is_abbreviation", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index(
        "ix_kb_aliases_lower", "kb_concept_aliases",
        [sa.text("lower(alias)")],
    )
    op.create_index(
        "ix_kb_aliases_concept_alias", "kb_concept_aliases",
        ["concept_id", "alias"], unique=True,
    )

    # ── kb_claims (no stance — stance lives in kb_relations) ──
    op.create_table(
        "kb_claims",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column(
            "artifact_id", UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("claim_text", sa.Text, nullable=False),
        sa.Column("source_chunk_ids", ARRAY(sa.String), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("conditions", JSONB, nullable=True),
        sa.Column("concept_ids", ARRAY(sa.String), nullable=True),
        sa.Column("extracted_by", sa.String(50), nullable=False, server_default="agent"),
        sa.Column("extraction_context", JSONB, nullable=True),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("verified_by", sa.String(100), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("superseded_by_id", UUID(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_kb_claims_project_status", "kb_claims", ["project_id", "status"])

    # ── kb_claim_chunks (authoritative claim↔chunk links) ──
    op.create_table(
        "kb_claim_chunks",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "claim_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(30), nullable=False, server_default="source"),
    )
    op.create_index("ix_claim_chunks_claim", "kb_claim_chunks", ["claim_id"])
    op.create_index("ix_claim_chunks_chunk", "kb_claim_chunks", ["chunk_id"])
    op.create_index(
        "ix_claim_chunks_unique", "kb_claim_chunks",
        ["claim_id", "chunk_id"], unique=True,
    )

    # ── kb_claim_concepts (authoritative claim↔concept links) ──
    op.create_table(
        "kb_claim_concepts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "claim_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "concept_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relevance", sa.String(20), nullable=False, server_default="mentions"),
    )
    op.create_index("ix_claim_concepts_claim", "kb_claim_concepts", ["claim_id"])
    op.create_index("ix_claim_concepts_concept", "kb_claim_concepts", ["concept_id"])
    op.create_index(
        "ix_claim_concepts_unique", "kb_claim_concepts",
        ["claim_id", "concept_id"], unique=True,
    )

    # ── kb_relations ──
    op.create_table(
        "kb_relations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", UUID(as_uuid=False), nullable=False),
        sa.Column(
            "relation_type",
            sa.Enum("related_to", "is_a", "part_of", "improves_over", "alternative_to",
                    "used_in", "supports", "contradicts", "extends", "refines",
                    "supersedes", "evaluated_on", "proposes", "mentions", "cites",
                    name="kbrelationtype"),
            nullable=False,
        ),
        sa.Column("object_type", sa.String(20), nullable=False),
        sa.Column("object_id", UUID(as_uuid=False), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("evidence_ids", ARRAY(sa.String), nullable=True),
        sa.Column("extra_metadata", JSONB, nullable=True),
        sa.Column("discovered_by", sa.String(50), nullable=False, server_default="agent"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_kb_relations_subject", "kb_relations", ["subject_type", "subject_id"])
    op.create_index("ix_kb_relations_object", "kb_relations", ["object_type", "object_id"])
    op.create_index("ix_kb_relations_type", "kb_relations", ["relation_type"])
    op.create_index("ix_kb_relations_project_type", "kb_relations", ["project_id", "relation_type"])

    # ── kb_syntheses ──
    op.create_table(
        "kb_syntheses",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("organization_id", UUID(as_uuid=False), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("synthesis_type", sa.String(50), nullable=False),
        sa.Column("source_artifact_ids", ARRAY(sa.String), nullable=False),
        sa.Column("claim_ids", ARRAY(sa.String), nullable=True),
        sa.Column("concept_ids", ARRAY(sa.String), nullable=True),
        sa.Column("key_findings", JSONB, nullable=True),
        sa.Column("conflicts_detected", JSONB, nullable=True),
        sa.Column("gaps_identified", ARRAY(sa.String), nullable=True),
        sa.Column("confidence", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("quality_score", sa.Float, nullable=True),
        sa.Column("generated_by", sa.String(50), nullable=False, server_default="dreaming"),
        sa.Column("model_used", sa.String(100), nullable=True),
        sa.Column("token_cost", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("reviewed_by", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_kb_syntheses_project_type", "kb_syntheses", ["project_id", "synthesis_type"])
    op.create_index("ix_kb_syntheses_status", "kb_syntheses", ["status"])

    # ── synthesis_sources (authoritative synthesis↔artifact links) ──
    op.create_table(
        "synthesis_sources",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "synthesis_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_syntheses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id", UUID(as_uuid=False),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contribution", sa.String(50), nullable=True),
    )
    op.create_index("ix_synthesis_sources_synthesis", "synthesis_sources", ["synthesis_id"])
    op.create_index("ix_synthesis_sources_artifact", "synthesis_sources", ["artifact_id"])

    # ── kb_concept_usages (project-level concept interpretation) ──
    op.create_table(
        "kb_concept_usages",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "concept_id", UUID(as_uuid=False),
            sa.ForeignKey("kb_concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_description", sa.Text, nullable=True),
        sa.Column("importance", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("first_mentioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mention_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("related_memory_ids", ARRAY(sa.String), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_concept_usages_concept_project", "kb_concept_usages",
        ["concept_id", "project_id"], unique=True,
    )
    op.create_index("ix_concept_usages_project", "kb_concept_usages", ["project_id"])

    # ── watchlists ──
    op.create_table(
        "watchlists",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=False), nullable=True, index=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_watchlists_org_active", "watchlists", ["organization_id", "is_active"])
    op.create_index("ix_watchlists_project_active", "watchlists", ["project_id", "is_active"])

    # ── tool_audit_logs ──
    op.create_table(
        "tool_audit_logs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("node_id", UUID(as_uuid=False), nullable=True),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("called_by", sa.String(50), nullable=False),
        sa.Column("input_summary", sa.Text, nullable=True),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("permission_decision", sa.String(20), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("side_effect", sa.String(30), nullable=False),
        sa.Column("execution_ms", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_tool_audit_project_tool", "tool_audit_logs", ["project_id", "tool_name"])
    op.create_index("ix_tool_audit_created", "tool_audit_logs", ["created_at"])

    # ── dreaming_jobs ──
    op.create_table(
        "dreaming_jobs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True, index=True,
        ),
        sa.Column("organization_id", UUID(as_uuid=False), nullable=True, index=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("schedule_cron", sa.String(100), nullable=True),
        sa.Column("min_interval_seconds", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("config", JSONB, nullable=True),
        sa.Column("max_tokens_per_run", sa.Integer, nullable=False, server_default="50000"),
        sa.Column("max_cost_per_run_cents", sa.Integer, nullable=False, server_default="50"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_runs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_consecutive_failures", sa.Integer, nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dreaming_jobs_project_type", "dreaming_jobs", ["project_id", "job_type"])
    op.create_index("ix_dreaming_jobs_status", "dreaming_jobs", ["status"])
    op.create_index("ix_dreaming_jobs_next_run", "dreaming_jobs", ["next_run_at"])

    # ── dreaming_runs ──
    op.create_table(
        "dreaming_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "job_id", UUID(as_uuid=False),
            sa.ForeignKey("dreaming_jobs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("input_snapshot", JSONB, nullable=True),
        sa.Column("output_summary", JSONB, nullable=True),
        sa.Column("created_entity_ids", JSONB, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=True),
        sa.Column("cost_cents", sa.Integer, nullable=True),
        sa.Column("model_used", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("error_traceback", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dreaming_runs_job_status", "dreaming_runs", ["job_id", "status"])
    op.create_index("ix_dreaming_runs_started", "dreaming_runs", ["started_at"])

    # ================================================================
    # 2. Modify existing tables
    # ================================================================

    # ── artifacts: add scope, org, metadata, KB index fields ──
    op.add_column("artifacts", sa.Column(
        "scope",
        artifact_scope_enum,
        nullable=False, server_default="project",
    ))
    op.add_column("artifacts", sa.Column(
        "organization_id", UUID(as_uuid=False), nullable=True,
    ))
    op.add_column("artifacts", sa.Column("paper_metadata", JSONB, nullable=True))
    op.add_column("artifacts", sa.Column("dataset_metadata", JSONB, nullable=True))
    op.add_column("artifacts", sa.Column(
        "is_indexed_in_kb", sa.Boolean, nullable=False, server_default="false",
    ))
    op.add_column("artifacts", sa.Column(
        "kb_chunk_count", sa.Integer, nullable=False, server_default="0",
    ))
    op.add_column("artifacts", sa.Column(
        "indexed_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.add_column("artifacts", sa.Column("structured_summary", JSONB, nullable=True))
    op.add_column("artifacts", sa.Column("concept_ids", ARRAY(sa.String), nullable=True))
    op.add_column("artifacts", sa.Column("content_hash", sa.String(64), nullable=True))

    # Make project_id nullable (library artifacts may not belong to a project)
    op.alter_column("artifacts", "project_id", nullable=True)

    # Indexes
    op.create_index("ix_artifacts_project_scope_type", "artifacts", ["project_id", "scope", "type"])
    op.create_index("ix_artifacts_org_scope", "artifacts", ["organization_id", "scope"])
    op.create_index("ix_artifacts_content_hash", "artifacts", ["content_hash"])

    # ── kb_chunks: add artifact_id, make kb_entry_id nullable ──
    op.add_column("kb_chunks", sa.Column(
        "artifact_id", UUID(as_uuid=False),
        sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=True,
    ))
    op.add_column("kb_chunks", sa.Column(
        "artifact_version", sa.Integer, nullable=False, server_default="1",
    ))
    op.add_column("kb_chunks", sa.Column(
        "citability", sa.String(20), nullable=False, server_default="medium",
    ))
    op.add_column("kb_chunks", sa.Column(
        "usage_count", sa.Integer, nullable=False, server_default="0",
    ))
    op.add_column("kb_chunks", sa.Column(
        "last_accessed_at", sa.DateTime(timezone=True), nullable=True,
    ))

    # Make kb_entry_id nullable (will eventually be dropped)
    op.alter_column("kb_chunks", "kb_entry_id", nullable=True)

    op.create_index("ix_chunks_artifact_index", "kb_chunks", ["artifact_id", "chunk_index"])

    # ── memory_entries: add new v2 fields ──
    op.add_column("memory_entries", sa.Column(
        "proposal_id", UUID(as_uuid=False),
        sa.ForeignKey("memory_proposals.id", ondelete="SET NULL"),
        nullable=True,
    ))
    op.add_column("memory_entries", sa.Column(
        "evidence_ids", ARRAY(sa.String), nullable=True,
    ))
    op.add_column("memory_entries", sa.Column(
        "concept_ids", ARRAY(sa.String), nullable=True,
    ))
    op.add_column("memory_entries", sa.Column(
        "derived_from_artifact_id", UUID(as_uuid=False),
        sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
        nullable=True,
    ))
    op.add_column("memory_entries", sa.Column(
        "embedding", Vector(1536), nullable=True,
    ))
    op.add_column("memory_entries", sa.Column(
        "verification_count", sa.Integer, nullable=False, server_default="0",
    ))

    # ── research_skills: add promotion fields ──
    op.add_column("research_skills", sa.Column(
        "extracted_from_projects", ARRAY(sa.String), nullable=True,
    ))
    op.add_column("research_skills", sa.Column(
        "validated_run_count", sa.Integer, nullable=False, server_default="0",
    ))
    op.add_column("research_skills", sa.Column(
        "failure_cases", JSONB, nullable=True,
    ))
    op.add_column("research_skills", sa.Column(
        "promotion_status", sa.String(30), nullable=False, server_default="project",
    ))
    op.add_column("research_skills", sa.Column(
        "promotion_requested_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.add_column("research_skills", sa.Column(
        "promotion_approved_by", sa.String(100), nullable=True,
    ))


def downgrade() -> None:
    artifact_scope_enum = sa.Enum("project", "library", name="artifactscope")

    # ── research_skills: drop new columns ──
    op.drop_column("research_skills", "promotion_approved_by")
    op.drop_column("research_skills", "promotion_requested_at")
    op.drop_column("research_skills", "promotion_status")
    op.drop_column("research_skills", "failure_cases")
    op.drop_column("research_skills", "validated_run_count")
    op.drop_column("research_skills", "extracted_from_projects")

    # ── memory_entries: drop new columns ──
    op.drop_column("memory_entries", "verification_count")
    op.drop_column("memory_entries", "embedding")
    op.drop_column("memory_entries", "derived_from_artifact_id")
    op.drop_column("memory_entries", "concept_ids")
    op.drop_column("memory_entries", "evidence_ids")
    op.drop_column("memory_entries", "proposal_id")

    # ── kb_chunks: drop new columns, restore kb_entry_id not null ──
    op.drop_index("ix_chunks_artifact_index", table_name="kb_chunks")
    op.drop_column("kb_chunks", "last_accessed_at")
    op.drop_column("kb_chunks", "usage_count")
    op.drop_column("kb_chunks", "citability")
    op.drop_column("kb_chunks", "artifact_version")
    op.drop_column("kb_chunks", "artifact_id")
    op.alter_column("kb_chunks", "kb_entry_id", nullable=False)

    # ── artifacts: drop new columns, restore project_id not null ──
    op.drop_index("ix_artifacts_content_hash", table_name="artifacts")
    op.drop_index("ix_artifacts_org_scope", table_name="artifacts")
    op.drop_index("ix_artifacts_project_scope_type", table_name="artifacts")
    op.drop_column("artifacts", "content_hash")
    op.drop_column("artifacts", "concept_ids")
    op.drop_column("artifacts", "structured_summary")
    op.drop_column("artifacts", "indexed_at")
    op.drop_column("artifacts", "kb_chunk_count")
    op.drop_column("artifacts", "is_indexed_in_kb")
    op.drop_column("artifacts", "dataset_metadata")
    op.drop_column("artifacts", "paper_metadata")
    op.drop_column("artifacts", "organization_id")
    op.drop_column("artifacts", "scope")
    op.alter_column("artifacts", "project_id", nullable=False)
    artifact_scope_enum.drop(op.get_bind(), checkfirst=True)

    # ── Drop new tables (reverse order of creation) ──
    op.drop_index("ix_dreaming_runs_started", table_name="dreaming_runs")
    op.drop_index("ix_dreaming_runs_job_status", table_name="dreaming_runs")
    op.drop_table("dreaming_runs")

    op.drop_index("ix_dreaming_jobs_next_run", table_name="dreaming_jobs")
    op.drop_index("ix_dreaming_jobs_status", table_name="dreaming_jobs")
    op.drop_index("ix_dreaming_jobs_project_type", table_name="dreaming_jobs")
    op.drop_table("dreaming_jobs")

    op.drop_index("ix_tool_audit_created", table_name="tool_audit_logs")
    op.drop_index("ix_tool_audit_project_tool", table_name="tool_audit_logs")
    op.drop_table("tool_audit_logs")

    op.drop_index("ix_watchlists_project_active", table_name="watchlists")
    op.drop_index("ix_watchlists_org_active", table_name="watchlists")
    op.drop_table("watchlists")

    op.drop_index("ix_concept_usages_project", table_name="kb_concept_usages")
    op.drop_index("ix_concept_usages_concept_project", table_name="kb_concept_usages")
    op.drop_table("kb_concept_usages")

    op.drop_index("ix_synthesis_sources_artifact", table_name="synthesis_sources")
    op.drop_index("ix_synthesis_sources_synthesis", table_name="synthesis_sources")
    op.drop_table("synthesis_sources")

    op.drop_index("ix_kb_syntheses_status", table_name="kb_syntheses")
    op.drop_index("ix_kb_syntheses_project_type", table_name="kb_syntheses")
    op.drop_table("kb_syntheses")

    op.drop_index("ix_kb_relations_project_type", table_name="kb_relations")
    op.drop_index("ix_kb_relations_type", table_name="kb_relations")
    op.drop_index("ix_kb_relations_object", table_name="kb_relations")
    op.drop_index("ix_kb_relations_subject", table_name="kb_relations")
    op.drop_table("kb_relations")

    op.drop_index("ix_claim_concepts_unique", table_name="kb_claim_concepts")
    op.drop_index("ix_claim_concepts_concept", table_name="kb_claim_concepts")
    op.drop_index("ix_claim_concepts_claim", table_name="kb_claim_concepts")
    op.drop_table("kb_claim_concepts")

    op.drop_index("ix_claim_chunks_unique", table_name="kb_claim_chunks")
    op.drop_index("ix_claim_chunks_chunk", table_name="kb_claim_chunks")
    op.drop_index("ix_claim_chunks_claim", table_name="kb_claim_chunks")
    op.drop_table("kb_claim_chunks")

    op.drop_index("ix_kb_claims_project_status", table_name="kb_claims")
    op.drop_table("kb_claims")

    op.drop_index("ix_kb_aliases_concept_alias", table_name="kb_concept_aliases")
    op.drop_index("ix_kb_aliases_lower", table_name="kb_concept_aliases")
    op.drop_table("kb_concept_aliases")

    op.drop_index("ix_kb_concepts_type", table_name="kb_concepts")
    op.drop_index("ix_kb_concepts_org", table_name="kb_concepts")
    op.drop_table("kb_concepts")

    op.drop_index("ix_proposals_project_status", table_name="memory_proposals")
    op.drop_table("memory_proposals")

    # Drop enum types
    sa.Enum(name="proposalstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="kbconcepttype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="kbrelationtype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="artifactscope").drop(op.get_bind(), checkfirst=True)
