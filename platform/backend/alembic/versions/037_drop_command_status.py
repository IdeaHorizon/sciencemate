"""删掉 `commands.status` 与 `decisions.accepted_response_count`。

## `commands.status` —— 一个三值拼法的 `== accepted`

`CommandStatus` 有五个取值，而 `QUEUED` / `RUNNING` **从来没有被写过**：
两处读点写的都是 `IN (accepted, queued, running)`，也就是 `== accepted`
的三值拼法。而 `accepted` 的含义正是"还没有收尾"——这件事 `result` /
`error` 两列已经如实回答了：有结果或有错误 = 收尾了，都没有 = 还没。

原写者只是在 result/error 旁边再盖一个词。取消路径同理：取消也是一个
**结果**，写下 `error` 就够了。

## `decisions.accepted_response_count` —— 就是 `len(accepted_responses)`

它唯一的非展示用途是乐观并发（`expected_accepted_response_count`），而
`len()` 一模一样地服务那个用途。一份事实两处记，就会有两处各自演化。
响应里仍然给这个数字 —— 那是**计算值**，不是另存的状态。

Revision ID: 037_drop_command_status
Revises: 036_drop_attempt_copies
"""

import sqlalchemy as sa
from alembic import op

revision = "037_drop_command_status"
down_revision = "036_drop_attempt_copies"
branch_labels = None
depends_on = None


def _drop_column_if_present(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    if column not in {col["name"] for col in inspector.get_columns(table)}:
        return
    op.drop_column(table, column)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE decisions DROP CONSTRAINT IF EXISTS "
        "ck_decisions_response_count_nonnegative"
    )
    _drop_column_if_present("commands", "status")
    _drop_column_if_present("decisions", "accepted_response_count")


def downgrade() -> None:
    raise NotImplementedError(
        "commands.status was a spelling of result/error; "
        "accepted_response_count was len(accepted_responses)"
    )
