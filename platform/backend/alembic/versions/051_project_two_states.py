"""项目只有两种状态：进行中 / 已归档。

`RFC_ORGANISATION_PAGE_20260923` §4.3（归档那一批）。「暂停」和「已完成」从没有读者 —— 会话照跑、
清单照列、没有任何地方因为它们做了不一样的事 —— 留着就是假的状态。用户真正要的只有「还在做 /
不做了」两态。存量：暂停过的还在做（进行中），完成了的不做了（已归档）。

Revision ID: 051_project_two_states
Revises: 050_project_visibility
"""

from alembic import op

revision = "051_project_two_states"
down_revision = "050_project_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE projects SET status = 'active' WHERE status = 'paused'")
    op.execute("UPDATE projects SET status = 'archived' WHERE status = 'completed'")


def downgrade() -> None:
    # 合掉的两种分不回去：并成「进行中」的原来是不是「暂停」，库里已经没有记录了。
    pass
