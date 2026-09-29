"""人说了什么留下了，平台答了什么也要留下（RFC 异步运行时 P1）。

## 现场

跑轮中插一句话，平台回三句：「已送达」「上一轮还占着这个会话」「回应会以时间线
事件出现」。这三句走 SSE 的 `progress` 帧 —— **断流即失**。刷新页面、换个设备、
事后复盘，全都看不到平台当时答了什么。

而用户那句话是持久化的（`SessionMessage`）。于是同一次交互里「人说了什么」留下
了、「平台答了什么」没留下 —— 事后只能看到一句没有回音的话。

RFC P1 原文：

    过渡期用户可感知修复顺带落地：interject 回执成为**持久事件**
    （不再走会被覆盖的 progress 通道）。

## 两个门面必须一份逻辑

插话有两条路（chat 流式入口分流 / `/interrupt` 端点），它们共用
`interject_active_session`。回执留痕也必须两条都做 —— 只做一条的话，走另一条
进来的插话事后没有回音，而它们是**同一件事**。这条测试同时打两个门面。

## 回执要锚回那条消息

`messageId` 不是装饰：事后要回答「平台那句话是在回应谁」。
`interject_active_session` 原本把消息 id 留在函数内部没交出来，回执只能编一个
或者不填 —— 决策呈递那次的教训（编 id 当身份，两边再也对不上）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.execution import EventOrigin, ExecutionEvent


def _receipts(events: list[ExecutionEvent]) -> list[ExecutionEvent]:
    return [e for e in events if e.kind == "interject.queued"]


@pytest.mark.asyncio
async def test_the_receipt_outlives_the_stream(db_session) -> None:
    """回执是事件，不是流内提示 —— 断流之后它还在。"""
    from app.services.app_events import record_app_event
    from app.models.execution import SessionProjection

    session = SessionProjection(
        tenant_id="t", workspace_id="w", project_id="p", session_id="s-receipt",
        next_sequence=0,
    )
    db_session.add(session)
    await db_session.flush()

    event = await record_app_event(
        db_session,
        session_id="s-receipt",
        kind="interject.queued",
        payload={"messageId": "m-1", "delivery": "queued_for_resume",
                 "occupancy": "free", "text": "换个方向"},
        dedupe_key="m-1",
    )
    assert event is not None
    # origin 说明这行是**平台自己做的事**，不是转述 worker —— 事实流本来就
    # 是多来源的账，缺的只是平台那一路的口子（EventOrigin.APP_COMMAND 2026 年
    # 就定义了，此前从来没有一行代码用过）。
    assert event.origin == EventOrigin.APP_COMMAND
    assert event.payload["messageId"] == "m-1"

    stored = list((await db_session.execute(
        select(ExecutionEvent).where(ExecutionEvent.session_id == "s-receipt")
    )).scalars().all())
    assert len(_receipts(stored)) == 1


@pytest.mark.asyncio
async def test_the_same_interjection_never_gets_two_receipts(db_session) -> None:
    """重复投递不写出第二行 —— `dedupe_key` 用那条消息自己的 id。"""
    from app.services.app_events import record_app_event
    from app.models.execution import SessionProjection

    db_session.add(SessionProjection(
        tenant_id="t", workspace_id="w", project_id="p", session_id="s-dedupe",
        next_sequence=0))
    await db_session.flush()

    for _ in range(3):
        await record_app_event(
            db_session, session_id="s-dedupe", kind="interject.queued",
            payload={"messageId": "m-2"}, dedupe_key="m-2")

    stored = list((await db_session.execute(
        select(ExecutionEvent).where(ExecutionEvent.session_id == "s-dedupe")
    )).scalars().all())
    assert len(_receipts(stored)) == 1, "同一条插话写出了多份回执"


@pytest.mark.asyncio
async def test_a_missing_session_does_not_break_the_operation(db_session) -> None:
    """留不下痕，也不该让那次插话失败 —— 回执比操作本身次要。"""
    from app.services.app_events import record_app_event

    assert await record_app_event(
        db_session, session_id="s-does-not-exist",
        kind="interject.queued", payload={}) is None


def test_both_interject_entry_points_record_a_receipt() -> None:
    """两个门面一份逻辑 —— 判据取**调用位置**，不是"我记得都加了"。"""
    import inspect

    from app.api.v1 import chat, sessions as session_api

    for module, name in ((chat, "chat 流式入口"), (session_api, "/interrupt 端点")):
        source = inspect.getsource(module)
        assert "interject_active_session" in source, f"{name} 不该绕开唯一落地路径"
        assert 'kind="interject.queued"' in source, (
            f"{name} 没有留回执 —— 走这条路进来的插话事后没有回音"
        )


@pytest.mark.asyncio
async def test_the_receipt_survives_the_read_path(db_session) -> None:
    """接线：回执必须能被 `/events` 的读取端序列化出来。

    ## 现场（2026-08-24，node20，qinp 的会话）

    `record_app_event` 往 `source` 里写 `{"appCommand": kind}` —— 一个零读者、
    且与 `kind` 列逐字重复的字段。而出口 schema 是闭合的：

        ValidationError: source.appCommand — Extra inputs are not permitted

    后果不是"这条读不出来"，是**整页读不出来**：用户插一次话，那一轮的执行
    记录当场变成一片空白，刷新无效（数据本身如此）。

    ## 为什么走真写入端 + 真读取端

    只校验 `ExecutionEventSourceResponse` 不够 —— 那是我以为出问题的那半截。
    炸的是整个 `ExecutionEventResponse`，所以这里过的是 `_event_response`
    本身，也就是 `/events` 每一条都要过的那一关。

    投影器那一路早有同款测试（`test_the_narration_event_survives_serialization`）。
    缺的从来不是守卫，是**第二个写入端没有人盯**：守卫挂在
    `_insert_event` 顶部，而这一路自己 new 了一个 `ExecutionEvent`。
    """
    from app.api.v1.execution import _event_response
    from app.models.execution import SessionProjection
    from app.services.app_events import record_app_event

    db_session.add(SessionProjection(
        tenant_id="t", workspace_id="w", project_id="p", session_id="s-readable",
        next_sequence=0,
    ))
    await db_session.flush()

    event = await record_app_event(
        db_session,
        session_id="s-readable",
        kind="interject.queued",
        run_id="r-1",
        payload={"messageId": "m-1", "delivery": "delivered_to_live_runtime",
                 "occupancy": "busy", "text": "换个方向"},
        dedupe_key="m-1",
    )
    assert event is not None

    response = _event_response(event)
    assert response.kind == "interject.queued", (
        "读取端必须原样交出这条事件的 kind —— 换成任何别的值，"
        "客户端的 kind 词表就对不上，整页照样作废"
    )
