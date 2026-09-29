"""Per-model context window.

上下文窗口是**模型的属性**，不是平台的。在此之前它只有一个全局 env
``LLM_CONTEXT_WINDOW``，而 App Server 建 worker 环境时压根没传它 —— 于是不管
用户选了哪个模型，harness 一律吃 120000 的兜底默认值。

实测后果（2026-08-18，会话 c9deb4f2）：摘要器按 120k 的 70% 触发压缩，一个
会话压了 15 次，最后调度器自己要求换 session —— 而它跑的模型窗口远大于此。

Nullable，且不回填：NULL = "没配，用平台默认"。给存量配置猜一个数就是替
用户声明一个他没说过的事实，而这个数错了会直接决定何时压缩。

Revision ID: 024_model_context_window
Revises: 023_password_rotation_cutoff
"""

import sqlalchemy as sa
from alembic import op

revision = "024_model_context_window"
down_revision = "023_password_rotation_cutoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_backend_configs",
        sa.Column("context_window_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_backend_configs", "context_window_tokens")
