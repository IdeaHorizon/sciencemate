"""一个会话只有**一条**时间线：消息和执行事件从同一个发生器领号。

## 为什么需要一条测试守着

前端用序号回答"这段活动发生在哪两条消息之间"：`messageRunSegments` 拿消息号
切窗口，`CanonicalRunActivity` 拿事件号落窗口。这个问法只有在两者同序时才成立，
而当时库里是**两个各自从 0 开始的计数器**。

两个号在类型上一模一样，拿来比大小不会报错 —— 只会静默地把顺序画错。实测会话
e46448f0：消息号 1–5、事件号 1–869，于是一条 run 的全部活动统统落进第一条消息
的开放尾窗，13:32 的动作画在 13:17 的对话上面，用户原话「显示的顺序不是很合理，
感觉都不是按照时间顺序来的」。

那个前提当时写在前端的注释里 —— 注释守不住任何东西。所以它现在是一条绑真库的
断言：谁把消息重新接回自己的计数器，这里立刻红。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import SessionMessage, SessionProjection


async def _seed(factory, session_id: str) -> None:
    async with factory() as db:
        db.add(SessionProjection(
            tenant_id="t", workspace_id="w", project_id="p",
            session_id=session_id, title="one timeline",
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_messages_and_events_share_one_generator(db_engine) -> None:
    """交替分配消息号与事件号 —— 必须严格递增且**互不重复**。

    分开的两个计数器在这条断言下必然失败：它们会各自发出 1、2、3……
    于是同一个数字既是"第 1 条消息"又是"第 1 个事件"。
    """
    from app.services.execution_ingest import ExecutionIngestService
    from app.services.sessions import append_session_message

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed(factory, "s-one-timeline")

    handed_out: list[int] = []
    async with factory() as db:
        session = await db.scalar(
            select(SessionProjection)
            .where(SessionProjection.session_id == "s-one-timeline"))
        assert session is not None
        ingestor = ExecutionIngestService()
        for index in range(4):
            message = await append_session_message(
                db, session_id="s-one-timeline", role="user",
                content=f"m{index}", actor_user_id=None)
            handed_out.append(message.sequence)
            # 事件侧的入口。两者若不同源，这里发出的号会与上面撞。
            handed_out.append(await ingestor._next_sequence(db, session))
        await db.commit()

    assert len(set(handed_out)) == len(handed_out), (
        f"消息与事件发出了同一个号：{handed_out} —— 这是两个计数器的指纹，"
        "而两个计数器就是两条时间线"
    )
    assert handed_out == sorted(handed_out), f"序号不单调：{handed_out}"


@pytest.mark.asyncio
async def test_the_session_row_has_exactly_one_counter() -> None:
    """第二个计数器不能悄悄长回来。

    判据扫的是**模型上还有几个"下一个序号"**，不是某个具体旧名字 —— 写成
    `assert not hasattr(…, "next_message_sequence")` 只挡得住那一个名字，
    换个名字加回来照样漏（"护栏要扫盘不要写名单"）。
    """
    counters = [
        name for name in SessionProjection.__mapper__.columns.keys()
        if "sequence" in name
    ]
    assert counters == ["next_sequence"], (
        f"会话上有不止一个序号发生器：{counters}。"
        "多一个就多一条时间线，而两条时间线上的号拿来比大小不会报错。"
    )


@pytest.mark.asyncio
async def test_a_message_can_carry_the_offer_it_is(db_engine) -> None:
    """一条消息可以**就是**一次呈递 —— 身份得能落盘。

    没有这一列，页面上去重只能拿文案去比（`pause-echo.ts`，已删），而"拿文案
    当身份"正是 2026-08-19 决策卡无限重现事故的引擎。
    """
    from app.services.sessions import append_session_message

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed(factory, "s-offer-identity")

    async with factory() as db:
        await append_session_message(
            db, session_id="s-offer-identity", role="assistant",
            content="Post-node decision for hypothesis", actor_user_id=None,
            offer_id="offer-abc123")
        await db.commit()

    async with factory() as db:
        stored = await db.scalar(
            select(SessionMessage)
            .where(SessionMessage.session_id == "s-offer-identity"))
    assert stored is not None and stored.offer_id == "offer-abc123"
