"""组织提供的模型，在桌面上记一份指向它的连接。

组织是资源的提供者：成员加入之后，组织的模型出现在他自己的「设置 → 模型与密钥」里、标着
来源，本机项目也能直接用 —— 调用经组织服务器的模型网关转发，密钥从不离开服务器
（`RFC_ORGANISATION_PAGE_20260923` E 批）。桌面上的那一行记着它来自哪条连接、对应服务器上
哪一条。组织服务器自己不写这两列；模型是两边共用的，所以迁移两边都跑。

Revision ID: 049_org_provided_models
Revises: 048_account_per_organisation
"""

import sqlalchemy as sa
from alembic import op

revision = "049_org_provided_models"
down_revision = "048_account_per_organisation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_backend_configs",
                  sa.Column("provided_by_connection", sa.String(length=64), nullable=True))
    op.add_column("model_backend_configs",
                  sa.Column("provided_backend_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("model_backend_configs", "provided_backend_id")
    op.drop_column("model_backend_configs", "provided_by_connection")
