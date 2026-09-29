"""连接的身份包含它的端点：唯一键加上 `base_url`。

`uq_model_backend_scope_model` 是 `(scope_kind, scope_id, provider, model)`，
**不含 base_url**。于是"同一个模型名挂在两个不同端点"——完全合法、而且我们
真的在用的场景——建不出来。2026-08-24 node20 实测：已有「Kimi K3 · 积算」
(openai_compatible/kimi-k3, https://api.icompify.com/v1)，再加一条走自建网关
的 http://47.103.79.174:30080/v1 直接撞约束。当时靠把 provider 从
openai_compatible 改成 kimi 绕过去了——两者在这条路径上行为一模一样，provider
在这里只是个标签——那是绕行不是修复：身份被编进了一个不表达身份的字段。

## 为什么是表达式索引而不是把列加进 UniqueConstraint

PG 里 NULL 不参与唯一性比较：`(a, b, c, NULL)` 和 `(a, b, c, NULL)` 互不冲突。
直接把 base_url 列进唯一键，等于对所有**没填端点**的连接（内置 provider 走
默认地址时就是这样）取消了这道约束，重复项从此静默共存。

`coalesce(base_url, '')` 把"没填"折进空串，约束原样保留。折成空串而不是别的
哨兵，是因为下游本来就这么读：`_provider_base_url` 判 `if config.base_url`、
`model_backends` 里是 `(config.base_url or "").rstrip("/")` —— None 和 "" 在
这套代码里从来是同一件事。唯一键跟着既有语义走，否则 UI 上会出现两条长得
一模一样的连接。

## 存量数据

新键比旧键**更松**（多一列 = 更少碰撞），旧约束下合法的行在新索引下必然合法。
不需要清理，也不需要回填。

node20 上那条被改名成 provider=kimi 的连接不会被这次迁移改回去——它现在是一条
合法的记录，改不改是人的决定，不是迁移能替他做的判断。

Revision ID: 030_model_backend_endpoint
Revises: 029_feed_images_and_layout
"""

import sqlalchemy as sa

from alembic import op

revision = "030_model_backend_endpoint"
down_revision = "029_feed_images_and_layout"
branch_labels = None
depends_on = None

_OLD_CONSTRAINT = "uq_model_backend_scope_model"
_NEW_INDEX = "uq_model_backend_connection"
_TABLE = "model_backend_configs"


def upgrade() -> None:
    # 先建新的再拆旧的：中间任何一刻都有一道唯一性挡着。
    op.create_index(
        _NEW_INDEX,
        _TABLE,
        ["scope_kind", "scope_id", "provider", "model", sa.text("coalesce(base_url, '')")],
        unique=True,
    )
    op.drop_constraint(_OLD_CONSTRAINT, _TABLE, type_="unique")


def downgrade() -> None:
    # 降级可能**失败**，而且那是对的：这条迁移之后建出来的"同模型两端点"在
    # 旧 schema 里无处安放。静默删掉其中一条 = 悄悄拿走一个人配好的连接。
    # 真要降，先自己决定留哪条。
    op.create_unique_constraint(
        _OLD_CONSTRAINT, _TABLE, ["scope_kind", "scope_id", "provider", "model"]
    )
    op.drop_index(_NEW_INDEX, table_name=_TABLE)
