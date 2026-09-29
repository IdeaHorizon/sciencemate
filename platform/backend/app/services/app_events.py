"""平台自己说的话，也要留在事实流里（RFC 异步运行时 P1）。

## 现场

用户跑轮中插一句话，平台回三句：「已送达」「上一轮还占着这个会话」「回应会以
时间线事件出现」。这三句走的是 SSE 的 `progress` 帧 —— **流断了就没了**。
刷新页面、换个设备、事后复盘，全都看不到平台当时答了什么。

而用户那句话是**持久化的**（`SessionMessage`）。于是同一次交互里，
「人说了什么」留下了，「平台答了什么」没留下。事后只能看到一句没有回音的话。

RFC P1 明写：

    过渡期用户可感知修复顺带落地：interject 回执成为**持久事件**
    （不再走会被覆盖的 progress 通道）。

## 为什么单开一个入口

`ExecutionEvent` 此前只有一个写者：投影器（`execution_ingest`），它转述 worker
的 transcript。平台自己的动作没有入口 —— `EventOrigin.APP_COMMAND` 这个取值
2026 年就定义了、schema 里也列着，**但从来没有一行代码用过**。

这不是"再开一个写者"（D11 管的是 `run.status` 那个**事实字段**的单写者）。
事件流本来就是多来源的账：worker 说的话、平台做的事、对账补的行，各有 origin
字段标明出处。缺的只是平台那一路的口子。

## 序号仍由库发

`next_sequence` 的自增走同一条 `UPDATE … RETURNING` —— 锁的是库里的行，
不是内存副本（[[序号计数器必须由库发]]，8-11 的事件序号事故 + 8-20 的消息序号
事故是同一个病）。所以这里**复用** `ExecutionIngestService._next_sequence`，
不另写一份。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import EventOrigin, EventVisibility, ExecutionEvent, SessionProjection
from app.services.execution_ingest import ADAPTER_VERSION, ExecutionIngestService, _hash_parts


async def record_app_event(
    db: AsyncSession,
    *,
    session_id: str,
    kind: str,
    payload: dict[str, Any],
    run_id: str | None = None,
    visibility: EventVisibility = EventVisibility.STANDARD,
    dedupe_key: str | None = None,
) -> ExecutionEvent | None:
    """把平台的一次动作记进事实流。会话不存在时返回 `None`（不抛）。

    `dedupe_key` 给同一件事一个稳定 id —— 重复调用不会写出两行（例如同一条
    插话的回执被重试投递）。不给就按时间戳生成，天然不去重。
    """
    session = await db.scalar(
        select(SessionProjection).where(SessionProjection.session_id == session_id)
    )
    if session is None:
        # 事实流挂在会话上；会话不在就没有地方记。这不是错误 —— 调用方要的是
        # "留痕"，留不下也不该让它那次操作失败（回执比操作本身次要）。
        return None
    now = datetime.now(UTC)
    event_id = _hash_parts(session_id, kind, dedupe_key or now.isoformat())
    if await db.get(ExecutionEvent, event_id):
        return None
    event = ExecutionEvent(
        id=event_id,
        tenant_id=session.tenant_id,
        workspace_id=session.workspace_id,
        project_id=session.project_id,
        session_id=session_id,
        run_id=run_id,
        attempt_no=1,
        sequence=await ExecutionIngestService()._next_sequence(db, session),
        occurred_at=now,
        origin=EventOrigin.APP_COMMAND,
        # 平台自己的动作**没有来源可指** —— 它不是从哪份 transcript 的哪个字节
        # 投影出来的，也不是从别的事件派生的。`source` 说的是"这条事实从哪儿来"，
        # 这一路的答案就是空。
        #
        # 这里原来写 `{"appCommand": kind}`：一个没有任何读者、且与 `kind` 列
        # 逐字重复的字段。而出口 schema（`ExecutionEventSourceResponse`）是闭合的，
        # 于是**每一条**平台事件生下来就读不出来 —— 用户插一次话，那一轮的执行
        # 记录整页作废（2026-08-24）。
        source={},
        kind=kind,
        visibility=visibility,
        payload=payload,
        adapter_version=ADAPTER_VERSION,
    )
    db.add(event)
    await db.flush()
    return event
