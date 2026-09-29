"""后端不在的那段时间里，worker 说过的话，补回来（RFC 异步运行时 P0-3）。

## 这个模块存在之前发生了什么

`session_event_log.read_events` 连同它的跨进程测试在 2026-08-18 就写好了，
worker 也一直在往 `events.jsonl` 落盘。但全后端**没有一个生产调用点** ——
读的那一半有库、有测试、没有调用方。于是 P0-3 的验收判据（"事件无丢失无
重复，byte_offset 对账"）在生产路径上从来没有兑现过，而测试全绿。

「机制存在但没接到路径」第 N 次。这个文件就是那条路径。

## 补的是什么、不补什么

补**持久事实**：transcript 包装事件 —— 它们是研究本身留下的痕迹，恢复之后
库里必须有。走的是与实时路径**同一个**函数（`ingest_transcript_wrapper`），
不另写一份摄取。

不补**转瞬即逝的东西**：token 流、进度百分比。它们是给当时盯着屏幕的人看的，
补回来只会在时间线上塞进一堆早已过期的噪音。

## 断点在哪：不存第二份

"读到哪了"不落盘。每次恢复时从库里现算每份 transcript 的**高水位**
（`MAX(byte_offset)`），字节位置在它之下的包装事件直接跳过。

存一列 offset 会是第二个真相源，而它一定会和事件表分叉：一次半途失败的补齐
把 offset 推上去了、记录却没落库，从此那段历史永远补不回来，且没有人报错。
现算的版本反过来：补一半崩了，下次接着补，**幂等且自愈**。

代价是每次恢复要顺序扫一遍 `events.jsonl`。顺序读一个几十 MB 的文件，
换一个不会分叉的判据，这笔交易划算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService, IngestContext
from app.services.harness_transcript_ingest import (
    file_identity_of,
    ingest_transcript_wrapper,
    resolve_wrapper,
)
from app.services.session_event_log import read_events

logger = logging.getLogger(__name__)

#: 每补这么多条提交一次。补齐可能有几万条，攒到最后一次性提交的话，中途
#: 任何一次失败都会把已经做完的活全部回滚 —— 而这活是幂等的，没有理由让它
#: 变成 all-or-nothing。
_COMMIT_EVERY = 200


@dataclass(frozen=True)
class ReplayReport:
    """一次补齐的**见证**：做了什么、跳过了什么、有没有读到坏行。"""

    #: 事件文件在不在。不在 = 这个 worker 没落过盘（老版本），不是错误。
    events_file: bool = False
    #: 扫过的记录条数。
    scanned: int = 0
    #: 真正补进库的条数。
    ingested: int = 0
    #: 因为已在高水位之下而跳过的条数。
    already_known: int = 0
    #: JSON 解析失败的行数（见证，不是故障）。
    malformed: int = 0
    #: 摄取时报错的条数 —— 记下来，不让一条坏记录拦住整段补齐。
    failed: int = 0

    @property
    def worth_logging(self) -> bool:
        return bool(self.ingested or self.failed or self.malformed)


async def _high_water_marks(db: AsyncSession, context: IngestContext) -> dict[str, int]:
    """这个 session 的每份 transcript 已经摄到哪个字节了。

    按 **session** 而不是按 run 取：`events.jsonl` 是会话级的，里面躺着这个
    会话历次 turn 的包装事件。只看当前 run 的水位，历史那些会被当成"没摄过"
    重新摄一遍 —— 而摄取的幂等键含 run_id，换个 run_id 就不会被挡住，结果是
    同一段历史在库里出现两次、挂在错误的 run 名下。
    """
    rows = await db.execute(
        select(ExecutionEvent.file_identity, func.max(ExecutionEvent.byte_offset))
        .where(
            ExecutionEvent.tenant_id == context.tenant_id,
            ExecutionEvent.session_id == context.session_id,
            ExecutionEvent.file_identity.is_not(None),
        )
        .group_by(ExecutionEvent.file_identity)
    )
    return {
        str(identity): int(offset or 0)
        for identity, offset in rows.all()
        if identity is not None
    }


async def replay_missed_events(
    db: AsyncSession,
    *,
    service: ExecutionIngestService,
    context: IngestContext,
    events_path: Path | str,
) -> ReplayReport:
    """把 `events_path` 里还没进库的 transcript 记录补进来。

    幂等：重复调用不会产生第二份记录（高水位挡住），也不会漏（水位是现算的）。
    """
    path = Path(events_path)
    if not path.is_file():
        return ReplayReport(events_file=False)

    high_water = await _high_water_marks(db, context)
    harness_states: dict[str, dict] = {}
    offset = 0
    scanned = ingested = already_known = malformed = failed = 0
    pending = 0

    while True:
        batch = read_events(path, offset=offset)
        if batch.truncated:
            # 文件比断点还短 —— 它被换过（新 worker 重建了会话目录）。从头再来
            # 是安全的：高水位会把已知的部分全挡掉。
            offset = 0
            continue
        malformed += batch.malformed
        if not batch.events:
            break
        offset = batch.offset
        for record in batch.events:
            scanned += 1
            if record.get("type") != "transcript" or not isinstance(
                record.get("event"), dict
            ):
                continue
            try:
                transcript_path, byte_start, _ = resolve_wrapper(
                    record, project_id=context.project_id, session_id=context.session_id
                )
                identity = file_identity_of(transcript_path)
            except (RuntimeError, OSError):
                failed += 1
                continue
            if byte_start <= high_water.get(identity, -1):
                already_known += 1
                continue
            try:
                await ingest_transcript_wrapper(
                    db,
                    service=service,
                    context=context,
                    event=record,
                    harness_states=harness_states,
                )
            except Exception:
                # 一条补不进去不该让整段历史都补不回来。记下来，接着补 ——
                # 报告里的 `failed` 就是这件事的见证。
                logger.exception(
                    "Could not replay one worker event (run=%s offset=%s)",
                    context.run_id,
                    byte_start,
                )
                failed += 1
                continue
            ingested += 1
            pending += 1
            high_water[identity] = byte_start
            if pending >= _COMMIT_EVERY:
                await db.commit()
                pending = 0

    if pending:
        await db.commit()
    return ReplayReport(
        events_file=True,
        scanned=scanned,
        ingested=ingested,
        already_known=already_known,
        malformed=malformed,
        failed=failed,
    )
