"""Retire this service's parallel KB / memory domain.

Knowledge and memory are sedimented by agents into harness JSONL under
``$HARNESS_FRAMEWORK_HOME``.  These tables were an earlier parallel domain model,
built before ``platform_runtime`` bridged execution onto the harness; execution,
artifacts and the Project repository were migrated one at a time and this half
was never done.  Verified on the deployed instance before dropping:

    users              16     <- real usage
    projects           15     <- real usage
    kb_entries          0
    kb_chunks           0
    kb_claims           0
    kb_concepts         0
    kb_syntheses        0
    memory_entries      0
    memory_proposals    0
    research_skills     0
    (every kb_* / memory_* table)

Fifteen real projects, not one row.  The read paths now serve the harness store
(``app.services.harness_kb``), so nothing observable is lost.

``downgrade`` recreates nothing.  These tables never held data, and rebuilding
seventeen empty tables would only restore the ambiguity about which store is
authoritative — the thing this migration exists to end.  Rolling back past this
point means checking out the prior revision of the code with its models.

Revision ID: 022_retire_platform_kb_domain
Revises: 021_autonomy_auth_scope
"""

from alembic import op

revision = "022_retire_platform_kb_domain"
down_revision = "021_autonomy_auth_scope"
branch_labels = None
depends_on = None


# 顺序即依赖顺序：先删引用方（关联表 / 子表），再删被引用方。
_TABLES = (
    "kb_claim_chunks",
    "kb_claim_concepts",
    "synthesis_sources",
    "kb_concept_usages",
    "kb_concept_aliases",
    "kb_relations",
    "kb_syntheses",
    "kb_claims",
    "kb_concepts",
    "kb_chunks",
    "kb_entries",
    "memory_proposals",
    "memory_entries",
    "research_skills",
)


def upgrade() -> None:
    # evidence 的 claims 表还留着（下一批），但它对 kb_chunks 的外键必须先解开，
    # 否则删表会被引用挡住。列本身保留：证据链结构还没迁走，丢列会丢信息。
    with op.batch_alter_table("claims") as batch:
        try:
            batch.drop_constraint("claims_kb_chunk_id_fkey", type_="foreignkey")
        except Exception:  # noqa: BLE001 — 约束名随方言不同，缺了也不该挡住下线
            pass

    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade() -> None:
    raise NotImplementedError(
        "022 是单向的：这些表从未存过数据，重建十七张空表只会把"
        "「哪个 KB 是权威」这个歧义再造出来。要回滚请检出 021 时期的代码。"
    )
