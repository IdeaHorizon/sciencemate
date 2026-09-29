"""删掉 `runs.current_attempt_no` 与 `run_attempts.checkpoint_ref`。

## `runs.current_attempt_no` —— 一个反规范化的 MAX()，由三处分别维护

「这条 run 的当前一次 attempt 是哪一次」此前有**两套习语**同时在用：这个列，
和五处各写一遍的 `ORDER BY attempt_no DESC LIMIT 1`。两套回答同一个问题，
而分叉时两边都不报错。

它还是最脆的一个：`local_execution` 在每次摄取前拿它改写
`IngestContext.attempt_no` —— 写错一位，此后所有事件会静默记到别的 attempt
名下（事件表的唯一约束不会报错，只是把它们分到另一行去）。

现在只剩一个答案：`app/services/attempts.latest_attempt_no()`。

## `run_attempts.checkpoint_ref` —— 抄了没人看

一个写者（暂停收尾时抄 `pause_pending_path`），**零读者**。checkpoint 本来就
在盘上，需要时从 harness 结果里现取。

Revision ID: 036_drop_attempt_copies
Revises: 035_drop_empty_scaffolding
"""

import sqlalchemy as sa
from alembic import op

revision = "036_drop_attempt_copies"
down_revision = "035_drop_empty_scaffolding"
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
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_attempt_nonnegative")
    _drop_column_if_present("runs", "current_attempt_no")
    _drop_column_if_present("run_attempts", "checkpoint_ref")


def downgrade() -> None:
    # 不重建。两列都是派生的：attempt 序号现算，checkpoint 在盘上。
    raise NotImplementedError(
        "current_attempt_no is MAX(run_attempts.attempt_no); "
        "checkpoint_ref had zero readers"
    )
