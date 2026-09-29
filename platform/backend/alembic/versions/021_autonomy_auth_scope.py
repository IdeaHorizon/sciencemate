"""无人值守出发前声明的授权范围。

2026-08-10：一轮无人值守 E2E 停在「真实外部作业提交」的高危审批上，静默挂了
两小时 —— 而提交真实作业**正是**这一趟要干的事。

在此之前只有两个极端：全旁路（连 `dd of=/dev/` 一起放行）或全停（每个高危点
都等人）。于是"无人值守"这个承诺，在**最需要它的那个节点**上必然失效。

存的是**授权声明**（哪些类别这一趟不必再问人），不是判决。类别标签取自
`shared/lib/dangerous_commands.match_high_risk` 的返回值，是那份词表的引用，
不在这里另抄一份 —— 抄一份就会各自演化。

NULL / [] = 每个高危点都停下问人。这必须是默认：授权范围只能由人显式给出，
框架不替他推断。

Revision ID: 021_autonomy_auth_scope
Revises: 020_backend_credential_health
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# ⚠️ ≤32 字符：`alembic_version.version_num` 是 varchar(32)。
# 第一版叫 `021_autonomous_authorization_scope`（34 字符），DDL 跑完了、
# 记版本号那一步炸在 StringDataRightTruncationError，整个迁移回滚 ——
# 而症状出现在几层之外：模型 SELECT 一个不存在的列 → 事务中止 →
# `InFailedSQLTransactionError`，完全看不出病因。
revision = "021_autonomy_auth_scope"
down_revision = "020_backend_credential_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_configs",
        sa.Column("autonomous_authorized_risk_classes", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_configs", "autonomous_authorized_risk_classes")
