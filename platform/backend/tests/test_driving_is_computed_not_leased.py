"""谁在开这个会话 —— 现算，不租。

## 现场（2026-09-05 删租约）

从前 `sessions` 表上有 `primary_driver_user_id` + `driver_lease_until`，配三个
端点（acquire / renew / release / transfer）和一台状态机。它要防的是「两个人同时
对一个会话动手」，可那件事真正被挡住的地方在跑轮那一刻（会话被占用），不在这台
状态机上。状态机自己反倒长出了几种独有的故障：

- 成员降级 / 移除被一条**过期没人清**的租约挡住（409「先转移或释放驾驶权」）；
- 前端要再算一遍租约到期，于是同一个问题两份判据、各自演化；
- 个人档从头到尾只有一个人 —— 一台永远只有一个参与者的状态机。

现在答案是现算的：**最近一条用户消息的作者**。它不会过期、不需要维护、也不可能
和事实分叉；没有人说过话就退回发起者、创建者。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.execution import SessionMessage, SessionProjection
from app.models.user import User
from app.services.sessions import current_driver_id
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

OWNER = "researcher@atrium.local"


@pytest.mark.asyncio
async def test_the_lease_columns_are_gone() -> None:
    """字段留着就会有人再去读它，而没有人再维护它。"""
    columns = set(SessionProjection.__table__.columns.keys())
    assert "primary_driver_user_id" not in columns
    assert "driver_lease_until" not in columns


@pytest.mark.asyncio
async def test_the_last_speaker_is_the_driver(runtime_client) -> None:
    client, factory = runtime_client
    token = await _token(client, OWNER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token), json={"title": "谁在开"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    async with factory() as db:
        session = await db.get(SessionProjection, session_id)
        owner = await db.scalar(select(User).where(User.email == OWNER))
        assert session is not None and owner is not None
        # 一句话都没说过：退回发起者。
        assert await current_driver_id(db, session) == owner.id

        # 另一个人说了话 —— 他就是当前驾驶者，不需要任何人"转移"。
        other = User(
            email="second@atrium.local", display_name="Second",
            hashed_password="x", is_active=True,
        )
        db.add(other)
        await db.flush()
        db.add(SessionMessage(
            session_id=session_id, sequence=1, actor_user_id=other.id,
            role="user", content="接着我来",
        ))
        await db.commit()

    async with factory() as db:
        session = await db.get(SessionProjection, session_id)
        assert await current_driver_id(db, session) == other.id

    # 会话响应把现算的结果照实交出去。
    body = (await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}", headers=_headers(token)
    )).json()
    assert body["primaryDriverUserId"] == other.id
    assert "driverLeaseUntil" not in body


@pytest.mark.asyncio
async def test_assistant_messages_do_not_make_the_agent_the_driver(runtime_client) -> None:
    """驾驶者是人。agent 说的话不算 —— 否则每跑完一轮驾驶者就变成 null。"""
    client, factory = runtime_client
    token = await _token(client, OWNER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token), json={"title": "agent 不是驾驶者"},
    )).json()["id"]

    async with factory() as db:
        owner = await db.scalar(select(User).where(User.email == OWNER))
        db.add(SessionMessage(
            session_id=session_id, sequence=1, actor_user_id=owner.id,
            role="user", content="开始",
        ))
        db.add(SessionMessage(
            session_id=session_id, sequence=2, actor_user_id=None,
            role="assistant", content="好的",
        ))
        await db.commit()

    async with factory() as db:
        session = await db.get(SessionProjection, session_id)
        assert await current_driver_id(db, session) == owner.id
