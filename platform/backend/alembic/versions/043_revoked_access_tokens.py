"""revoked access tokens: 登出要真的作废那一张 token

JWT 是自证的，服务端不记得发过什么，也因此说不出"这一张不算了"。登出从前只是
让浏览器把 token 扔掉 —— 被复制走的那一份照样有效到过期为止。这张表是撤销名单：
只存 sha256 散列（被读走也拿不到能用的 token），按 `expires_at` 建索引让写路径
上的清理走索引扫描。

只建这一张表。#981 的 043 还在 `users(lower(trim(email)))` 上建了 UNIQUE 函数
索引；组织档存量库里只要有一对大小写/空白重复的邮箱，那句就会让**整个** upgrade
回滚，而失败的表现是"迁移跑不过"，指不到那两行数据。严格邮箱是另一件事，它得
自带一条把冲突挑出来给人看的路，不能夹在这里。

Revision ID: 043_revoked_access_tokens
Revises: 042_git_is_the_ledger
"""
from alembic import op
import sqlalchemy as sa

revision = "043_revoked_access_tokens"
down_revision = "042_git_is_the_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revoked_access_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_revoked_access_tokens_expires_at", "revoked_access_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_revoked_access_tokens_expires_at", table_name="revoked_access_tokens")
    op.drop_table("revoked_access_tokens")
