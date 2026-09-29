"""删掉 `execution_events.source` 里那个没有读者的 `appCommand` 键。

`source` 回答的是"这条事实从哪儿来"，出口 schema
（`ExecutionEventSourceResponse`）因此是**闭合的**：只有 rawEvent / fileRef /
byteOffset / derivedFrom。平台自己的动作没有来源可指，答案是空。

`record_app_event` 从前往里写 `{"appCommand": kind}` —— 一个零读者、且与
`kind` 列逐字重复的字段。后果不是"这个字段被忽略"，而是这条事件**读不出来**，
而读取端一条读不出来就整页失败：用户插一次话，那一轮的执行记录整页作废
（2026-08-24 node20，qinp 的会话）。

写入端已经改成不写它。库里的历史行还在，这里把键删掉 —— 删的是死字段，
不是事实：`kind` 列一直存着同一个值，没有任何信息随之消失。

## 不加 CHECK 约束

约束只能写死一份键名单，而合法键的唯一真相源是那个 pydantic 模型。
两份名单会各自演化，且新字段默认漏过 —— 那正是写入端守卫
（`_reject_unserializable_source`）刻意复用模型、不另写名单的理由。

Revision ID: 031_event_source_dead_key
Revises: 030_model_backend_endpoint
"""

from alembic import op

revision = "031_event_source_dead_key"
down_revision = "030_model_backend_endpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE execution_events SET source = source - 'appCommand' "
        "WHERE source ? 'appCommand'"
    )


def downgrade() -> None:
    # 回填不了：这个键的值是 `kind` 的副本，而"这一行当初有没有这个键"
    # 没有任何地方记着。装作能回滚，比不能回滚更糟。
    pass
