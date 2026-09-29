"""drop the driver lease columns

会话的驾驶权从「存一个租约」改成「现算最近一条用户消息的作者」。租约是一台
状态机（拿 / 续 / 放 / 到期），它要防的事在跑轮那一刻由占用判据挡住；个人档
从头到尾只有一个人。字段留着就会有人再去读它，而没有人再维护它。

Revision ID: 040_drop_driver_lease
Revises: 039_derive_resolution_and_head
"""
from alembic import op
import sqlalchemy as sa

revision = "040_drop_driver_lease"
down_revision = "039_derive_resolution_and_head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_index("ix_sessions_primary_driver_user_id")
        batch.drop_column("driver_lease_until")
        batch.drop_column("primary_driver_user_id")


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("primary_driver_user_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("driver_lease_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_index(
            "ix_sessions_primary_driver_user_id", ["primary_driver_user_id"], unique=False
        )
