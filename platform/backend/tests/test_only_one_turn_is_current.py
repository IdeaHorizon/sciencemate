"""一个会话同一时刻只有**一条**当前一轮 —— 上一轮必须真的结束。

## 现场（2026-08-23，会话 e46448f0）

13:39 hypothesis 跑完，平台呈递 post-node 决策，run 停在 `waiting_human`。
13:40 后端重启，进程内那个 pause 随之消失。14:05 人点了 PROCEED —— 因为
`paused_binding`（后端内存里的一个布尔）已经没了，这句话被当成**新的一轮**
开跑，另起一条顶层 run。

旧那条从此是一具尸体，可库里它还写着 `waiting_human` 加一份完整的 pause
summary。而"当前这一轮"的判据是"顶层 run 里 `updated_at` 最新的那条"：

    run_cedea504…  waiting_human  updated_at 14:20:58.342   ← 12 分钟前答过了
    run_378b00c9…  running        updated_at 14:20:58.240   ← 真正在跑的这一轮

尸体每次被触碰都比在跑的那条晚零点几秒，稳定赢下"最新更新"。于是连续模式下
同一张决策卡反复复活，输入框被锁成「先回答上面的问题」，而卡片背后连个能听的
进程都没有。

## 两条断言

1. **谁是当前一轮**：问 `started_at`（写一次就不再改），不问 `updated_at`
   （任何一次触碰都会改）。
2. **上一轮必须终局**：新的顶层 run 一开跑，更早那些非终态的顶层 run 连同它们
   的子孙一起收终态。「补一条答复时记得回去改上一条」是名单式修法 —— 下次换个
   答复路径又漏；真正机械的判据只有"有人开了新的一轮"。
"""
from __future__ import annotations

import inspect

import pytest

from app.config import settings
from app.models.execution import Run, RunStatus, SessionProjection
from app.services import sessions as sessions_service


def _run(run_id: str, status: str, *, parent: str | None = None) -> Run:
    return Run(
        id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id=parent,
        status=status, summary={},
    )


def test_the_current_turn_is_the_last_one_started() -> None:
    """判据钉在 `started_at` 上，不许回到 `updated_at`。

    只扫**唯一那份 view 生产者**里挑 `current_run` 的那一句 —— 别的地方按
    `updated_at` 排序是正当的（"最近动过的"确实是个合法问题）。

    2026-09-01：这段代码从 `session_response` 搬进了 `session_execution_view`
    （REST payload 与流式终帧从此共用同一份局面）。判据跟着搬，不是放宽。
    """
    source = inspect.getsource(sessions_service.session_execution_view)
    marker = "current_run = await db.scalar("
    assert marker in source, "view 生产者不再自己挑当前一轮了？先看它搬去哪了"
    block = source[source.index(marker):]
    block = block[: block.index(")\n", block.index("limit(1)"))]
    assert "Run.started_at" in block, (
        "当前一轮必须问 started_at —— updated_at 回答的是「谁最近被写过」，"
        "而被顶掉的尸体会一直被触碰"
    )
    assert "Run.updated_at" not in block, (
        "updated_at 又回来了：被顶掉的旧 run 会稳定赢下这个排序（e46448f0 实测）"
    )


@pytest.mark.asyncio
async def test_a_new_turn_ends_the_previous_one(db_engine) -> None:
    """新的顶层 run 一开跑，上一轮（含它的子孙）立刻收终态。"""
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    started = datetime(2026, 8, 23, 13, 16, tzinfo=UTC)
    async with factory() as db:
        db.add(SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id="s", title="supersede"))
        old = _run("run_old", RunStatus.WAITING_HUMAN.value)
        old.started_at = started
        child = _run("run_old::child", RunStatus.WAITING_HUMAN.value, parent="run_old")
        child.started_at = started
        fresh = _run("run_new", RunStatus.QUEUED.value)
        fresh.started_at = None
        db.add_all([old, child, fresh])
        await db.commit()

    async with factory() as db:
        fresh = await db.get(Run, "run_new")
        assert fresh is not None
        fresh.started_at = datetime(2026, 8, 23, 14, 5, tzinfo=UTC)
        event = ExecutionEvent(
            id="e-new-turn", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id="s", run_id="run_new", sequence=1,
            occurred_at=fresh.started_at, origin="raw_transcript", kind="run.started",
            visibility="summary", payload={}, adapter_version="test",
        )
        await ExecutionIngestService()._supersede_previous_turns(
            db, run=fresh, event=event)
        await db.commit()

    async with factory() as db:
        rows = {
            run.id: run.status
            for run in (await db.scalars(select(Run).where(Run.session_id == "s"))).all()
        }
    assert rows["run_old"] == RunStatus.INCOMPLETE.value, (
        "被顶掉的那一轮还挂在 waiting_human 上 —— 它会一直把已经答过的问题递给人"
    )
    assert rows["run_old::child"] == RunStatus.INCOMPLETE.value, (
        "父都没了，子节点更没有主"
    )
    assert rows["run_new"] == RunStatus.QUEUED.value, "不许动开跑的这一条自己"


@pytest.mark.asyncio
async def test_a_late_run_started_never_kills_the_live_turn(db_engine) -> None:
    """迟到的补摄取不能把真正在跑的那条判死 —— 只顶掉**更早开始**的。"""
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id="s2", title="late"))
        live = _run("run_live", RunStatus.RUNNING.value)
        live.session_id = "s2"
        live.started_at = datetime(2026, 8, 23, 15, 0, tzinfo=UTC)
        late = _run("run_late", RunStatus.RUNNING.value)
        late.session_id = "s2"
        late.started_at = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
        db.add_all([live, late])
        await db.commit()

    async with factory() as db:
        late = await db.get(Run, "run_late")
        assert late is not None
        event = ExecutionEvent(
            id="e-late", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id="s2", run_id="run_late", sequence=1,
            occurred_at=late.started_at, origin="raw_transcript", kind="run.started",
            visibility="summary", payload={}, adapter_version="test",
        )
        await ExecutionIngestService()._supersede_previous_turns(
            db, run=late, event=event)
        await db.commit()

    async with factory() as db:
        rows = {
            run.id: run.status
            for run in (await db.scalars(select(Run).where(Run.session_id == "s2"))).all()
        }
    assert rows["run_live"] == RunStatus.RUNNING.value, (
        "一条迟到的 run.started 把正在跑的那一轮判死了"
    )
