"""instructions are files, not three tables

三张表（instruction_documents / instruction_versions / instruction_snapshots）
回答的问题只有一个：这个会话看到的指令是什么。答案是一段文本，所以它是文件；
版本是 git；冻结是会话行上的一列。

⚠️ 组织档存量：表里的正文在升级前请自行导出（`instruction_versions.content`）。
它们的去处是数据根里的 `user/PROFILE.md` 与 `projects/<id>/PROJECT.md`。

Revision ID: 041_instructions_are_files
Revises: 040_drop_driver_lease
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "041_instructions_are_files"
down_revision = "040_drop_driver_lease"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("instruction_snapshot", _JSON, nullable=True))
        # 指向那张表的外键列也一起走：留着就会有人再去读它，而没有人再维护它。
        batch.drop_column("instruction_snapshot_id")
    op.drop_table("instruction_snapshots")
    op.drop_table("instruction_versions")
    op.drop_table("instruction_documents")


def downgrade() -> None:  # pragma: no cover - 回退要连正文一起回来，做不到
    raise NotImplementedError(
        "指令正文已经搬进文件；回退请从数据根里的 PROFILE.md / PROJECT.md 重建"
    )
