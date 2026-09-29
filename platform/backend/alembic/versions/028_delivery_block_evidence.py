"""交付对账的不可达证据：`runs.delivery_block`。

交付对账每 60s 重试所有「已完成但没 publish」的 run。工作区已经不在磁盘上的
run 每一轮都以同一个错误失败，于是每 60s 一条 WARNING，直到永远 —— 本地库
2026-08-24 实测 9 条历史 run 攒了 4968 条同一句话。真正的代价不是重试，是真
告警被埋在里面没人看得见。

这一列存的是**当时观测到缺了哪个路径**，不是"以后别再试了"的判决。判决现算：
`deliverable_publishing.delivery_block_holds` 每次读的时候看那个路径现在还在
不在。数据根搬回来、`git worktree repair` 修好链接之后，这条 run 自己就重新
进漏斗了 —— 写死一个 never_retry 的话，它会被永远关在门外而没人会知道。

Nullable、不回填：NULL = 从没观测到不可达（绝大多数 run），语义正好是默认。

Revision ID: 028_delivery_block_evidence
Revises: 027_one_session_one_sequence
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "028_delivery_block_evidence"
down_revision = "027_one_session_one_sequence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "delivery_block",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "delivery_block")
