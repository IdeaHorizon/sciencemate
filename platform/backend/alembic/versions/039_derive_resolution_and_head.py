"""删掉两个可派生的 status 列：merge_conflicts 与 instruction_versions。

## `merge_conflicts.status` —— 与 `resolved_at` 同笔写

open / resolved 两个取值，而 `resolved_at` 回答的是同一件事：人什么时候拍的板。
两列在同一笔事务里写，是同一个事实的两种写法 —— 而两种写法就是两处会各自
演化的答案。判据统一读 `resolved_at IS NULL`。

线上核对（2026-08-27）：0 行，两列**尚未**分叉。这是预防性删除，不是事后修复。

## `instruction_versions.status` —— 只写不清

发布新版本时只移动 `document.published_version_id`，旧行的 `'published'`
永远留着 —— 于是 N 行同时自称已发布，而只有一行真的在位。它做不出"发布过但
被顶掉了"这个区分。

改为现算之后多了一个取值 `superseded`（发布过、已不在位），契约随之放宽 ——
这正是那个存储列答不出来的那一半。

线上核对：暂时每个文档只有 ≤1 行自称 published，也就是分叉还没显形。

Revision ID: 039_derive_resolution_and_head
Revises: 038_artifacts_bytes_live_in_git
"""

import sqlalchemy as sa
from alembic import op

revision = "039_derive_resolution_and_head"
down_revision = "038_artifacts_bytes_live_in_git"
branch_labels = None
depends_on = None


def _drop_column_if_present(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    if column not in {col["name"] for col in inspector.get_columns(table)}:
        return
    op.drop_column(table, column)


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_merge_conflicts_change_status")
    op.execute(
        "ALTER TABLE merge_conflicts DROP CONSTRAINT IF EXISTS ck_merge_conflicts_status"
    )
    op.execute(
        "ALTER TABLE instruction_versions DROP CONSTRAINT IF EXISTS "
        "ck_instruction_version_status"
    )
    _drop_column_if_present("merge_conflicts", "status")
    _drop_column_if_present("instruction_versions", "status")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("merge_conflicts"):
        existing = {ix["name"] for ix in inspector.get_indexes("merge_conflicts")}
        if "ix_merge_conflicts_change_open" not in existing:
            op.create_index(
                "ix_merge_conflicts_change_open",
                "merge_conflicts",
                ["change_set_id", "resolved_at"],
            )


def downgrade() -> None:
    raise NotImplementedError(
        "merge conflict resolution is resolved_at; the in-force instruction "
        "version is the document's head pointer"
    )
