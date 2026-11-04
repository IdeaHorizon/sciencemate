"""项目的执行目标：模型命令在本机跑，还是在这台 Windows 上的某个 WSL 发行版里跑。

`docs/RFC_WINDOWS_EXECUTION_TARGETS_20261030.md` D1/D2。NULL = 本机 —— 存量项目一直就是本机。

Revision ID: 052_project_execution_target
Revises: 051_project_two_states
"""

import sqlalchemy as sa
from alembic import op

revision = "052_project_execution_target"
down_revision = "051_project_two_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_configs",
                  sa.Column("execution_target", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("project_configs", "execution_target")
