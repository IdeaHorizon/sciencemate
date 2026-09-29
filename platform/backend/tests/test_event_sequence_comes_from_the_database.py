"""事件序号必须由数据库分配 —— 内存里加一会撞。

## 现场（2026-08-11，真实会话 bc1c7343）

    UniqueViolationError: duplicate key value violates unique constraint
    "uq_events_sequence"
    DETAIL: Key (tenant_id, session_id, sequence)=(local-tenant, bc1c7343…, 613)
            already exists.

整条 run 当场挂掉，用户看到 `execution_failed`。

## 不是计数器落后，是「拿锁」和「读值」被当成了一件事

实测全库 `next_sequence` 与 `max(sequence)` **完全一致**（0 个 session
落后）—— 所以不是持久性错位，是瞬时竞态。

分配器长这样：

    session = await db.scalar(select(SessionProjection)….with_for_update())
    session.next_sequence += 1     # ← 这个 += 加的是**内存里**的值

`with_for_update()` 确实在数据库上锁了那一行。但 SQLAlchemy 对**已在 identity
map 里**的对象，SELECT 回来的列值默认**不覆盖**已加载的属性（要 `populate_existing`
才覆盖），而 `expire_on_commit=False`（`app/database.py:43`）保证对象 commit 之后
继续留在 map 里、属性不过期。

于是：**锁是数据库的锁，值是内存的值**。两个 AsyncSession 各自缓存了同一行的旧
值，谁先 commit 都不会让另一边的内存跟着变 —— 锁排队排得好好的，加出来的数字还是
同一个。

这就是那条反复出现的形状：**两件不同的事按同一条规则处理**。「取得独占权」和
「读到当前值」是两件事，一条 `select(...).with_for_update()` 只保证了前一件。

## 修法：让数据库自己加

    UPDATE sessions SET next_sequence = next_sequence + 1
    WHERE … RETURNING next_sequence

一条语句同时完成加锁、自增、读回，中间没有内存副本可以变陈旧。ORM 实例上的值
同步刷新一份，好让同一事务里后续代码看到的是真值。

判据不是"加了锁"，是"**没有第二份可以走岔的真相**"（一个问题一个真相源）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import ExecutionEvent


def _ctx(session_id: str, run_id: str):
    from app.services.execution_ingest import IngestContext

    return IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id=session_id, run_id=run_id,
        actor_user_id=None, decision_authority=None,
    )


@pytest.mark.asyncio
async def test_the_number_comes_back_from_the_database_not_from_memory(db_engine) -> None:
    """两个 AsyncSession 交替分配 —— 各自缓存的旧值不能影响结果。

    这一条直指根因：把 `+= 1` 换成 `RETURNING` 之前，第二个 session 会拿着自己
    缓存的旧值再加一次，发出重复号。
    """
    from app.services.execution_ingest import ExecutionIngestService

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    service = ExecutionIngestService()
    ctx = _ctx("s-seq-race", "r-seq")

    handed_out: list[int] = []
    async with factory() as a, factory() as b:
        # 先让两边都把这一行读进各自的 identity map（这正是线上的状态：
        # 长驻的 chat worker 和 execution follower 各自缓存了同一个 session 行）。
        for db in (a, b):
            await service._ensure_scope(db, ctx)
            await db.commit()

        for round_no in range(6):
            db = a if round_no % 2 == 0 else b
            sess, _ = await service._lock_scope(db, ctx)
            handed_out.append(await service._next_sequence(db, sess))
            await db.commit()

    assert len(set(handed_out)) == len(handed_out), (
        f"发出了重复序号：{handed_out} —— 说明加的是内存里的值，不是库里的值"
    )
    assert handed_out == sorted(handed_out), f"序号不单调：{handed_out}"


@pytest.mark.asyncio
async def test_concurrent_allocation_never_collides(db_engine) -> None:
    """并发分配：真正跑起来的样子。撞了就是 run 当场挂掉。"""
    from app.services.execution_ingest import ExecutionIngestService

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    service = ExecutionIngestService()
    ctx = _ctx("s-seq-concurrent", "r-seq")

    async with factory() as setup:
        await service._ensure_scope(setup, ctx)
        await setup.commit()

    async def allocate() -> int:
        async with factory() as db:
            sess, _ = await service._lock_scope(db, ctx)
            value = await service._next_sequence(db, sess)
            await db.commit()
            return value

    values = await asyncio.gather(*(allocate() for _ in range(8)))
    assert len(set(values)) == 8, f"并发分配撞号：{sorted(values)}"


@pytest.mark.asyncio
async def test_the_counter_matches_what_actually_landed(db_session) -> None:
    """接线：走真实摄取之后，计数器与库里最大序号必须一致。

    这两个数一旦分叉，下一次分配就会撞 —— 而分叉时**两边都不报错**，
    要等到某一次插入才炸（一个问题一个真相源）。
    """
    import json

    from app.models.execution import SessionProjection
    from app.services.execution_ingest import ExecutionIngestService

    service = ExecutionIngestService()
    ctx = _ctx("s-seq-agree", "r-seq")
    for offset in range(4):
        raw = {
            "event": "llm_response",
            "at": f"2026-08-11T10:00:0{offset}+00:00",
            "turn": offset + 1,
            "content": "这一轮换了更精准的查询词，理由写在这里。",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        await service.ingest_raw_record(
            db_session, context=ctx, file_identity="f", byte_offset=offset * 100,
            raw_line=json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state={},
        )

    counter = await db_session.scalar(
        select(SessionProjection.next_sequence).where(
            SessionProjection.session_id == "s-seq-agree"
        )
    )
    landed = await db_session.scalar(
        select(ExecutionEvent.sequence)
        .where(ExecutionEvent.session_id == "s-seq-agree")
        .order_by(ExecutionEvent.sequence.desc())
        .limit(1)
    )
    assert landed, "一条都没落库"
    assert counter == landed, f"计数器 {counter} 与实际最大序号 {landed} 分叉了"


@pytest.mark.asyncio
async def test_an_event_the_api_cannot_return_is_refused_at_write_time(db_session) -> None:
    """写不进一条读取端序列化不了的事件 —— 契约在**入口**检查。

    ## 现场（2026-08-12）

    叙述事件写了 `source={"derivedFrom": "content"}`，而 `derivedFrom` 的类型是
    `list[str]`。写入一路成功，直到用户打开会话：

        ValidationError: source.derivedFrom → GET /sessions/{id}/events 500
        UI: "Execution details unavailable"

    **一条坏事件放倒整个事件列表**，正在跑的那一轮什么都看不见 —— 而写它的那次
    调用早就成功返回了，测试也全绿。

    契约只在出口检查 = 让错误在**离制造它的人最远的地方**爆炸，报错还指向读取
    代码。这一条把它拉回写入现场。
    """
    from app.services.execution_ingest import ExecutionIngestService, IngestError

    service = ExecutionIngestService()
    ctx = _ctx("s-bad-source", "r-bad")
    with pytest.raises(IngestError) as caught:
        await service._insert_event(
            db_session,
            session=(await service._lock_scope(db_session, ctx))[0],
            context=ctx,
            event_id="e-bad",
            draft=__import__(
                "app.services.execution_ingest", fromlist=["EventDraft"]
            ).EventDraft("agent.message", "standard", {"text": "x"}, None),
            origin="adapter_derived",
            source={"derivedFrom": "not-a-list"},
        )
    assert "derivedFrom" in str(caught.value) or "source" in str(caught.value)


@pytest.mark.asyncio
async def test_the_narration_event_survives_serialization(db_session) -> None:
    """接线：叙述事件本身必须能被读取端序列化出来。

    这一条盯的是**那次真实事故**，不是抽象契约：narration 就是那条把整个
    `/events` 打挂的事件。
    """
    import json

    from app.models.execution import ExecutionEvent
    from app.schemas.execution import ExecutionEventSourceResponse
    from app.services.execution_ingest import ExecutionIngestService

    service = ExecutionIngestService()
    ctx = _ctx("s-narr-serial", "r-narr")
    raw = {
        "event": "llm_response", "at": "2026-08-11T10:00:00+00:00", "turn": 1,
        "content": "换更精准的查询词，理由写在这里。", "usage": {"prompt_tokens": 1},
    }
    await service.ingest_raw_record(
        db_session, context=ctx, file_identity="f", byte_offset=0,
        raw_line=json.dumps(raw, ensure_ascii=False).encode(),
        raw=raw, adapter_state={},
    )
    rows = (await db_session.execute(
        select(ExecutionEvent.source).where(ExecutionEvent.session_id == "s-narr-serial")
    )).scalars().all()
    assert rows, "一条都没落库"
    for source in rows:
        ExecutionEventSourceResponse.model_validate(source or {})
