"""「这条 run 的当前一次 attempt 是哪一次」—— 只有这一个答案。

## 为什么单独立一个模块

这个问题此前有**两套习语**同时在用：

- `Run.current_attempt_no` —— 一个反规范化的 `MAX(attempt_no)` 列，由三处
  分别维护（派发时、摄取 run.started 时、沙箱能力变更换 attempt 时）；
- `ORDER BY RunAttempt.attempt_no DESC LIMIT 1` —— 五处各写一遍。

两套习语回答同一个问题，而分叉时两边都不报错。那个列还是**最脆的一个**：
`local_execution` 在每次摄取前拿它改写 `IngestContext.attempt_no`，写错一位
就会把此后所有事件记到别的 attempt 名下（而事件表有唯一约束，错档不报错，
只是静默地把它们分到另一行去）。

列已删（08-27）。这里是它唯一的替代：一次查询，一个答案。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import RunAttempt


async def latest_attempt(db: AsyncSession, run_id: str) -> RunAttempt | None:
    """这条 run 最后一次 attempt —— 没派过就是 None（没有证据，不下结论）。"""
    return await db.scalar(
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )


async def latest_attempt_no(db: AsyncSession, run_id: str) -> int:
    """最后一次 attempt 的序号；没派过是 0（与旧列的默认值一致）。"""
    value = await db.scalar(
        select(RunAttempt.attempt_no)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )
    return int(value or 0)
