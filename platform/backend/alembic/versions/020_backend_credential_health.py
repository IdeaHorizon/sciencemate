"""Record observed credential health for model backends.

平台此前判断一个后端"ready"的依据只有"key 这个字段填了没有"——一个**声明**。
node20 交付实测：机构默认后端的 key 字段填得好好的，但那个 key 已经失效，
所有同事一发消息就 401，而平台一路把它显示为 ready、并把它选成默认。

这里存的是**观测**（什么时候探的、结果是不是明确拒绝、provider 原话），
不存判决——status 每次现算。判决落盘必然随规则进化作废。

Revision ID: 020_backend_credential_health
Revises: 019_harness_artifact_contract
"""

import sqlalchemy as sa
from alembic import op

revision = "020_backend_credential_health"
down_revision = "019_harness_artifact_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_backend_configs",
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
    )
    # NULL = 从没探过 / 探不出结论（网络不通、超时）。只有 False 才是
    # "provider 明确拒绝了这把 key"。三态是有意的：把"不知道"和"坏了"
    # 混成一个 bool，就只能在"没探过也当坏"和"坏了也当好"里二选一。
    op.add_column(
        "model_backend_configs",
        sa.Column("last_probe_ok", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "model_backend_configs",
        sa.Column("last_probe_detail", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_backend_configs", "last_probe_detail")
    op.drop_column("model_backend_configs", "last_probe_ok")
    op.drop_column("model_backend_configs", "last_probe_at")
