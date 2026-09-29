"""子节点 run 不能把整个会话判成"忙"。

## 现场（2026-08-12，E2E v26）

父 run 正确地停在 `waiting_permission`，等人批准一次真实作业提交。人去答复，
拿到的是：

    409 session_run_active — "This Session already has research running."
    runId: run_53ac…/_orchestrator->_curator@d1        ← 一条**子节点** run

那条 curator 的最后一条事件在 3 小时前，它早就不干活了 —— 但它的 Run 行还写着
`queued`。

## 为什么子节点永远停在 queued

子节点 run 的生命周期由**父 run** 驱动：派发时建行，之后它的 started/终态事件
是信息性的，**不驱动自己的状态机**（否则子节点跑完就会把父 attempt 关掉，
post-node 决策之后再也恢复不了）。所以子节点行一旦建出来就停在 `queued`。

于是这道"会话在不在忙"的闸恒为真，**人再也答不了那个 pause，研究永久卡死**。

## 两件事一条规则

「顶层 run」回答的是"这个会话在不在忙"；「子节点 run」只是顶层那一趟内部的
一段。用同一条状态判据问两种东西 —— 今天在孤儿扫描上已经踩过一次同族的坑
（那次是把正在干活的子进程误杀了）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from datetime import UTC, datetime

from app.config import settings
from app.models.execution import ExecutionEvent, Run, RunStatus, SessionProjection
from app.services.sessions import _pending_approval


async def _noop_reap(*_args, **_kwargs):
    """孤儿回收不是本文件要测的东西 —— 它会改 summary 干扰断言。"""
    return None


async def _session_fixture(db, tag: str):
    """造一个能喂给 `session_response()` 的最小三件套。"""
    import uuid

    from app.models.project import Project
    from app.models.user import User

    user = User(
        email=f"{tag}@example.test", hashed_password="x", display_name="T",
    )
    db.add(user)
    await db.flush()
    project = Project(name=f"p-{tag}", owner_id=user.id)
    db.add_all([user, project])
    await db.flush()
    session = SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id=f"s-{tag}", initiating_user_id=user.id,
    )
    db.add(session)
    await db.flush()
    return project, session, user


@pytest.mark.asyncio
async def test_the_busy_check_only_looks_at_top_level_runs(db_session) -> None:
    """父 run 在等人时，挂着 queued 的子节点不能让会话显得"忙"。"""
    db_session.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p-busy", session_id="s-busy", initiating_user_id="u",
    ))
    db_session.add_all([
        Run(
            id="run_top", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p-busy", session_id="s-busy", parent_run_id=None,
            status=RunStatus.WAITING_PERMISSION.value, summary={},
        ),
        Run(
            id="run_top/_curator@d1", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p-busy", session_id="s-busy", parent_run_id="run_top",
            status=RunStatus.QUEUED.value, summary={},
        ),
    ])
    await db_session.flush()

    # 这正是 chat.py 那道闸的查询：加上 parent_run_id IS NULL 之后，
    # 挂着 queued 的子节点不再让会话显得"忙"。
    blocking = (await db_session.execute(
        select(Run).where(
            Run.session_id == "s-busy",
            Run.parent_run_id.is_(None),
            Run.status.in_(["queued", "dispatching", "running", "waiting_compute", "retrying"]),
        )
    )).scalars().all()
    assert blocking == [], (
        f"会话被判成忙，挡住了人对 pause 的答复：{[r.id for r in blocking]}"
    )


@pytest.mark.asyncio
async def test_a_real_top_level_run_still_blocks(db_session) -> None:
    """放宽不能变成放过：顶层真的在跑时，照旧不许并发发起。"""
    db_session.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p-busy2", session_id="s-busy2", initiating_user_id="u",
    ))
    db_session.add(Run(
        id="run_live", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p-busy2", session_id="s-busy2", parent_run_id=None,
        status=RunStatus.RUNNING.value, summary={},
    ))
    await db_session.flush()

    blocking = (await db_session.execute(
        select(Run).where(
            Run.session_id == "s-busy2",
            Run.parent_run_id.is_(None),
            Run.status.in_(["queued", "dispatching", "running", "waiting_compute", "retrying"]),
        )
    )).scalars().all()
    assert [r.id for r in blocking] == ["run_live"]


def test_the_guard_in_chat_actually_filters_children() -> None:
    """接线：`chat.py` 那道闸真的带了 `parent_run_id IS NULL`。

    只测上面两条等于测我自己写的查询 —— 得确认**真实那道闸**也这么写
    （"写了没人调"今天已经栽过好几次）。
    """
    import ast
    import inspect

    from app.api.v1 import chat

    src = inspect.getsource(chat)
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node)
        # 2026-08-17 起"忙"不再 409：分流成插话（interject_active_session）。
        # 判据本身没变 —— 仍然只看顶层 run；这条测试守的就是这个组合：
        # 闸在、且闸只认顶层。
        if "interject_active_session" in rendered:
            found = True
    assert found, "忙时分流（interject_active_session）不见了 —— 忙会话的消息没有去处"
    assert "Run.parent_run_id.is_(None)" in src, (
        "会话忙判据没有排除子节点 run —— 子节点永远停在 queued，人会答不了 pause"
    )


@pytest.mark.asyncio
async def test_a_stale_child_pause_is_not_the_sessions_pause(db_session, monkeypatch) -> None:
    """上一轮遗留的子 run 在等人，不代表**这个会话**在等人。

    2026-08-22 现场（E2E v27）：顶层 run 正跑着新一轮，上一轮的 experiment 子
    run 停在 waiting_human（worker 早被部署重启掐掉了），而它恰好是全会话
    `updated_at` 最新的一条。`session_response` 拿"最新更新的 run"去问
    `_pending_approval` —— 会话级 pendingApproval 于是取到那个**已经作废的**
    提问，UI 上「进行中」和「等你输入」同屏并存，人点了也没人听。

    "最新被更新的" 答不了 "当前这一轮"：子 run 的 updated_at 天然晚于父 run。

    与本文件开头那条是同一条不变量 —— 8-12 只把它接到了「忙不忙」那道闸，
    没接到「在等人」和「会话状态」。机制在场，路径没接全。

    ⚠️ 必须走真实读路径 `session_response()`：在测试里自己重写一遍查询，
    改坏产品代码它照样绿（本条第一版就是这么空过的）。
    """
    from app.services import sessions as svc

    monkeypatch.setattr(svc, "_reap_orphaned_runs_of", _noop_reap)
    project, session, user = await _session_fixture(db_session, "pause")
    db_session.add_all([
        Run(  # 当前这一轮：正在干活
            id="run_now", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id=str(project.id), session_id=session.session_id,
            parent_run_id=None, status=RunStatus.RUNNING.value, summary={},
            updated_at=datetime(2026, 8, 22, 4, 55, tzinfo=UTC),
        ),
        Run(  # 上一轮遗留的子 run：停在等人，且 updated_at 更晚
            id="run_prev/_experiment@d1", tenant_id=settings.runtime_tenant_id,
            workspace_id="w", project_id=str(project.id),
            session_id=session.session_id, parent_run_id="run_prev",
            status=RunStatus.WAITING_HUMAN.value, summary={},
            updated_at=datetime(2026, 8, 22, 4, 58, tzinfo=UTC),
        ),
    ])
    db_session.add(ExecutionEvent(
        id="ev-stale", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id=session.session_id,
        run_id="run_prev/_experiment@d1", sequence=1,
        occurred_at=datetime(2026, 8, 22, 4, 58, tzinfo=UTC),
        origin="harness", kind="run.paused", visibility="user",
        adapter_version="v1",
        payload={"reason": "waiting_human", "prompt": "是否把 /dev 登记为可写目录？"},
    ))
    await db_session.flush()

    body = await svc.session_response(
        db_session, user=user, project=project, session=session
    )

    assert body["executionView"]["answer"].get("pause") is None, (
        "会话级待审批取到了子 run 的旧提问 —— UI 会同时显示「进行中」和"
        "「等你输入」，而那个问题已经没人在听了"
    )
    assert body["executionState"] == RunStatus.RUNNING.value, (
        f"会话状态被子 run 污染成 {body['executionState']} → 顶部标签显示 Needs input"
    )


@pytest.mark.asyncio
async def test_a_real_top_level_pause_is_still_surfaced(db_session, monkeypatch) -> None:
    """放宽不能变成放过：顶层 run 真的在等人时，问题必须照常送到人面前。"""
    from app.services import sessions as svc

    monkeypatch.setattr(svc, "_reap_orphaned_runs_of", _noop_reap)
    project, session, user = await _session_fixture(db_session, "real")
    db_session.add(Run(
        id="run_asking", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id=session.session_id,
        parent_run_id=None, status=RunStatus.WAITING_HUMAN.value, summary={},
    ))
    db_session.add(ExecutionEvent(
        id="ev-real", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id=session.session_id,
        run_id="run_asking", sequence=1,
        occurred_at=datetime(2026, 8, 22, 5, 0, tzinfo=UTC),
        origin="harness", kind="run.paused", visibility="user",
        adapter_version="v1",
        payload={"reason": "waiting_human", "prompt": "是否批准提交这个作业？"},
    ))
    await db_session.flush()

    body = await svc.session_response(
        db_session, user=user, project=project, session=session
    )

    answer = body["executionView"]["answer"]
    # 「问题送到人面前」= 入口就是那张卡，且卡在同一个字段里。分开两个字段
    # 断言（"有 pendingApproval" + "canSend=false"）正是 2026-09-01 那次
    # 两边各自成立、合起来把人锁死的形状。
    assert answer["via"] == "pause", "顶层 run 在等人，问题必须送到人面前"
    assert answer["pause"]["prompt"] == "是否批准提交这个作业？"
