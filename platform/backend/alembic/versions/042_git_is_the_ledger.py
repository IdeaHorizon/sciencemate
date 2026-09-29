"""git is the ledger: drop the second version account

删 project_revisions / change_sets / change_items / merge_conflicts /
artifact_receipts 五张表，以及指向它们的两列（projects.current_revision_id、
sessions.base_revision_id）。

`project_repository` 的 manifest 里写着 `authority: git` —— 这五张表是投影。
两份版本账必然分叉，且分叉时两边都不报错（E2E v22：分支领先 main 15 个 commit，
publish 说成功，图没进库）。

⚠️ 组织档存量：表里的正文本来就在 git 里（产物字节自迁移 038 起在仓库中，收据
由 `write_receipt` 同时写进 git）。真要留档就在升级前自行导出。

Revision ID: 042_git_is_the_ledger
Revises: 041_instructions_are_files
"""
from alembic import op
import sqlalchemy as sa

revision = "042_git_is_the_ledger"
down_revision = "041_instructions_are_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # artifact_versions.change_set_id 从外键降级成一个普通字符串：它现在存的是
    # 发布幂等键（与 git 提交里的 Change-Set-ID trailer 同一个值）。
    #
    # ⚠️ 存量组织库（node20，2026-09-08 真撞）：这一列是 **uuid**，且带外键
    # `fk_artifact_versions_change_set → change_sets(id)`。原写法按 `String(64)` 假设、
    # 也没先拆外键 —— batch 重建表时把旧外键连着新的 varchar 列一起加回去，Postgres 报
    # "cannot be implemented: varchar and uuid"，整次 upgrade 回滚，部署停在门外。
    # 新库（CI / 个人档 SQLite）里没有这条外键、列本来就是字符串，所以两边都没照出来。
    # 判据落在**库里实际有什么**上，不落在"模型现在写的是什么"上：外键在就先拆，
    # 类型按反射出来的现值改，uuid → text 在 Postgres 上要显式 USING。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("artifact_versions")}
    existing_type = next(
        col["type"] for col in inspector.get_columns("artifact_versions")
        if col["name"] == "change_set_id"
    )
    with op.batch_alter_table("artifact_versions") as batch:
        if "fk_artifact_versions_change_set" in existing_fks:
            batch.drop_constraint("fk_artifact_versions_change_set", type_="foreignkey")
        batch.alter_column(
            "change_set_id", existing_type=existing_type, type_=sa.String(128),
            existing_nullable=True,
            postgresql_using="change_set_id::text",
        )
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("base_revision_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("current_revision_id")
    for table in (
        "artifact_receipts",
        "merge_conflicts",
        "change_items",
        "change_sets",
        "project_revisions",
    ):
        op.drop_table(table)


def downgrade() -> None:  # pragma: no cover - 回退要连正文一起回来，做不到
    raise NotImplementedError(
        "第二份版本账已经拆掉；回退请从 git 历史重建，而不是从这个迁移"
    )
