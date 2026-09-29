"""账号属于一个组织，不属于一台服务器。

邮箱的唯一性从「一台服务器一个」改成「一个组织一个」：一台服务器上住着好几个组织、
它们互相看不见，那么同一个人在其中两个里各有一个账号是正常局面 —— 而按服务器唯一的
邮箱把它禁掉了（2026-09-22：在一台已经有自己账号的机器上建第二个组织，被
「这台机器上已经有人用这个邮箱了」挡住）。

Revision ID: 048_account_per_organisation
Revises: 047_must_change_password
"""

from alembic import op

revision = "048_account_per_organisation"
down_revision = "047_must_change_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先建新的再拆旧的：中间任何一刻都不存在"谁都能重名"的窗口。
    op.create_index("uq_users_institution_email", "users", ["institution_id", "email"], unique=True)
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=False)


def downgrade() -> None:
    # 回退要求每个邮箱在整台服务器上只剩一个账号 —— 多组织之后未必成立，
    # 所以这里只还原声明，装着两份同名账号的库回退时会报冲突（那是实话）。
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.drop_index("uq_users_institution_email", table_name="users")
