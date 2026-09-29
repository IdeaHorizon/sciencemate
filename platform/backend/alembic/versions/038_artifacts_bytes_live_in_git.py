"""产物的字节只留 git 里那一份；删掉九列已退役的 KB 领域字段。

## 字节曾经存了三份

    git blob                              ← 权威（路径即身份 / head 即当前）
    artifact_versions.content             ← 完整拷贝，唯一读者是 800 字的 diff 预览
    artifacts.extra_data["_content"]      ← 又一份，供 /artifacts/{id}/content

而用户实际看到的字节走的是**第四条路**（`/repository/raw` 直接读 worktree）。
两处读点现已改为从 git 现取，两份拷贝删除。

`storage_key` 一并删：它唯一的写入是 `inline://…` 这个假 URI —— 全仓没有一处
`import boto3`，compose 里那个 MinIO 容器至今空转（容器与依赖已在 035 删）。
类注释写着 "Stored in object storage (S3/MinIO)"，那件事从来没有发生过。

`is_milestone` / `milestone_note`：零写入点，"用户标记的里程碑"从未实现。

## 九列 KB 领域字段

`scope` / `organization_id` / `paper_metadata` / `dataset_metadata` /
`is_indexed_in_kb` / `kb_chunk_count` / `indexed_at` / `structured_summary` /
`concept_ids` —— 属于已退役的服务内 KB 领域模型，唯一写者是
`scripts/seed_kb_demo.py`（零调用，随本次一起删）。线上真实项目里一行都没有。

Revision ID: 038_artifacts_bytes_live_in_git
Revises: 037_drop_command_status
"""

import sqlalchemy as sa
from alembic import op

revision = "038_artifacts_bytes_live_in_git"
down_revision = "037_drop_command_status"
branch_labels = None
depends_on = None

_ARTIFACT_COLUMNS = [
    "scope", "organization_id", "paper_metadata", "dataset_metadata",
    "is_indexed_in_kb", "kb_chunk_count", "indexed_at", "structured_summary",
    "concept_ids",
]
_VERSION_COLUMNS = ["content", "storage_key", "is_milestone", "milestone_note"]


def _drop_column_if_present(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    if column not in {col["name"] for col in inspector.get_columns(table)}:
        return
    op.drop_column(table, column)


def upgrade() -> None:
    # 索引先走：它们把 scope 钉在表上。
    op.execute("DROP INDEX IF EXISTS ix_artifacts_project_scope_type")
    op.execute("DROP INDEX IF EXISTS ix_artifacts_org_scope")
    for column in _ARTIFACT_COLUMNS:
        _drop_column_if_present("artifacts", column)
    for column in _VERSION_COLUMNS:
        _drop_column_if_present("artifact_versions", column)
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("artifacts"):
        existing = {ix["name"] for ix in inspector.get_indexes("artifacts")}
        if "ix_artifacts_project_type" not in existing:
            op.create_index("ix_artifacts_project_type", "artifacts", ["project_id", "type"])


def downgrade() -> None:
    raise NotImplementedError(
        "artifact bytes live in git; the KB columns had a single demo-seed writer"
    )
