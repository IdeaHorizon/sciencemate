"""项目在组织里谁看得见：组里所有人（只读）/ 只有项目成员。

`RFC_ORGANISATION_PAGE_20260923` §3.3（B 批）。存量项目都是 `organisation` —— 那正是它们一直
以来的样子（管理员看得见组里所有项目），只是从此成员也能在组织页上看到、只读打开。

Revision ID: 050_project_visibility
Revises: 049_org_provided_models
"""

import sqlalchemy as sa
from alembic import op

revision = "050_project_visibility"
down_revision = "049_org_provided_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("visibility", sa.String(length=24), nullable=False,
                                        server_default="organisation"))


def downgrade() -> None:
    op.drop_column("projects", "visibility")
