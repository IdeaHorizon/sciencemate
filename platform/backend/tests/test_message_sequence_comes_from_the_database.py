"""消息序号也必须由数据库发 —— 和事件序号同一个病，上次只治了一半。

2026-08-11 在**事件**序号上确诊过：`with_for_update()` 锁的是数据库的行，
`session.next_sequence += 1` 加的却是 identity map 里的内存副本
（`expire_on_commit=False` 让它 commit 之后继续留着），两件事被当成一件。
修法写在 `execution_ingest._next_sequence`：`UPDATE … SET n = n + 1 RETURNING n`。

那次只换了一个计数器。`append_session_message` 里**消息**序号原样留着 `+= 1`，
于是同一个病在另一张唯一键上等着：

    2026-08-20，会话 4adbea62（node20）：hypothesis 与 _reviewer 都跑完了
    （34 个工具调用、真产物一件没丢），收尾写 assistant 回复时撞
    `uq_session_messages_sequence` 的 2 号 —— 整轮判 failed，用户看到的是
    "Research stopped before completion / Two writers tried to record this
    Session's history at the same time"。

判据不是"加了锁"，是"**没有第二份可以走岔的真相**"（一个问题一个真相源）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import SessionMessage, SessionProjection


async def _seed(factory, session_id: str) -> None:
    async with factory() as db:
        db.add(SessionProjection(
            tenant_id="t", workspace_id="w", project_id="p",
            session_id=session_id, title="seq test",
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_the_number_comes_back_from_the_database_not_from_memory(db_engine) -> None:
    """两个 AsyncSession 交替追加消息 —— 各自缓存的旧值不能影响结果。

    这一条直指根因：换成 RETURNING 之前，第二个 session 拿着自己缓存的旧值
    再加一次，发出重复号。线上就是这个形状：请求那条连接写下用户消息，
    而跑了半小时的 worker 连接手里还攥着自己那份旧的 session 行。
    """
    from app.services.sessions import append_session_message

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed(factory, "s-msg-race")

    handed_out: list[int] = []
    async with factory() as a, factory() as b:
        # 先让两边都把这一行读进各自的 identity map
        for db in (a, b):
            await db.scalar(
                select(SessionProjection)
                .where(SessionProjection.session_id == "s-msg-race"))

        for round_no in range(6):
            db = a if round_no % 2 == 0 else b
            message = await append_session_message(
                db, session_id="s-msg-race", role="user",
                content=f"m{round_no}", actor_user_id=None)
            handed_out.append(message.sequence)
            await db.commit()

    assert len(set(handed_out)) == len(handed_out), (
        f"发出了重复序号：{handed_out} —— 说明加的是内存里的值，不是库里的值"
    )
    assert handed_out == sorted(handed_out), f"序号不单调：{handed_out}"


@pytest.mark.asyncio
async def test_concurrent_appends_never_collide(db_engine) -> None:
    """并发追加：真正跑起来的样子。撞了就是一整轮研究被判 failed。"""
    from app.services.sessions import append_session_message

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed(factory, "s-msg-concurrent")

    async def append(index: int) -> int:
        async with factory() as db:
            message = await append_session_message(
                db, session_id="s-msg-concurrent", role="user",
                content=f"concurrent-{index}", actor_user_id=None)
            await db.commit()
            return message.sequence

    values = await asyncio.gather(*(append(i) for i in range(8)))
    assert len(set(values)) == 8, f"并发追加撞号：{sorted(values)}"


@pytest.mark.asyncio
async def test_counter_and_rows_never_drift(db_engine) -> None:
    """收尾断言：计数器与实际写下的最大序号必须一致。

    线上那条会话正是在这里失守 —— 两行消息（1、2）而计数器停在 2，
    下一次分配必然发出已经被占掉的 2。
    """
    from app.services.sessions import append_session_message

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed(factory, "s-msg-drift")

    async with factory() as db:
        for i in range(4):
            await append_session_message(
                db, session_id="s-msg-drift", role="user",
                content=f"m{i}", actor_user_id=None)
            await db.commit()

    async with factory() as db:
        counter = await db.scalar(
            select(SessionProjection.next_sequence)
            .where(SessionProjection.session_id == "s-msg-drift"))
        highest = await db.scalar(
            select(SessionMessage.sequence)
            .where(SessionMessage.session_id == "s-msg-drift")
            .order_by(SessionMessage.sequence.desc()).limit(1))
    assert counter == highest, (
        f"计数器 {counter} 与最大序号 {highest} 对不上 —— 下一次分配就会撞号"
    )
