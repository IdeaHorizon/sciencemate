"""管理员重置密码之后，那个人手上是一次性口令 —— 登进来必须改掉。

在此之前平台没有任何找回密码的路：`/auth/change-password` 要旧密码，
`create-admin` 刻意不动密码。唯一的办法是直接改库。

Revision ID: 047_must_change_password
Revises: 046_invitations
"""
from alembic import op
import sqlalchemy as sa

revision = "047_must_change_password"
down_revision = "046_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
