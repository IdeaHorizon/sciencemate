"""「这一轮干了什么」包含子节点做的事。

## 现场（wangd 2026-08-11 试用）

    「这 literature 都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨…
      按理说 literature 这一大坨运行的内容就应该是 over 了，然后可以把它弄成
      一大坨，然后调度器说话，然后接下来那再开始个新的 hypothesis」

会话视图按 `runId` 精确过滤，一轮里只看得到**编排器自己**的 197 条工具调用，
平铺成一坨 "Research activity"。literature / hypothesis 真正做了什么一条都取
不回来 —— 它们是各自独立的 run。

实测（会话 bc1c7343）：

    run_357d167b…（编排器）        612 事件
    _orchestrator->_reviewer@d1    180 事件   ← 取不到
    _orchestrator->_curator@d1      58 事件   ← 取不到
    _orchestrator->hypothesis@d1    38 事件   ← 取不到

按 run 切是**数据模型的内部划分**，不该原样泄漏成 UX。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.execution import Run, SessionProjection


async def _seed(db, session_id: str, parent: str, child: str) -> None:
    from app.config import settings
    from app.models.execution import ExecutionEvent
    from datetime import UTC, datetime

    db.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id=session_id, initiating_user_id="u",
    ))
    for run_id, parent_run_id in ((parent, None), (child, parent)):
        db.add(Run(
            id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id=session_id, parent_run_id=parent_run_id,
            status="running", summary={},
        ))
    await db.flush()
    for index, (run_id, parent_run_id) in enumerate(
        [(parent, None)] * 2 + [(child, parent)] * 3 + [("other-run", None)]
    ):
        db.add(ExecutionEvent(
            id=f"e{index}", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id="p", session_id=session_id, run_id=run_id,
            parent_run_id=parent_run_id, attempt_no=1, sequence=index + 1,
            schema_version=1, occurred_at=datetime.now(UTC), origin="raw_transcript",
            source={}, kind="tool.started", visibility="standard", payload={},
            adapter_version="1.0.0",
        ))
    await db.flush()


@pytest.mark.asyncio
async def test_the_filter_can_include_the_children_it_dispatched(db_session) -> None:
    """`includeChildren` 把子节点的事件也带回来，且**不带别人的**。"""
    from app.api.v1.execution import list_session_events
    from app.config import settings

    await _seed(db_session, "s-children", "parent-run", "child-run")

    async def _authorized(*_args, **_kwargs):
        return True

    import app.api.v1.execution as module

    original = module._session_authorized
    module._session_authorized = _authorized
    try:
        without = await list_session_events(
            "s-children", 0, 200, "parent-run", False,
            user=None, tenant_id=settings.runtime_tenant_id, db=db_session,
        )
        with_children = await list_session_events(
            "s-children", 0, 200, "parent-run", True,
            user=None, tenant_id=settings.runtime_tenant_id, db=db_session,
        )
    finally:
        module._session_authorized = original

    assert {e.run_id for e in without.items} == {"parent-run"}
    assert {e.run_id for e in with_children.items} == {"parent-run", "child-run"}, (
        "子节点做的事取不回来 —— 用户看到的就只有编排器自己那一坨"
    )
    assert "other-run" not in {e.run_id for e in with_children.items}, (
        "把不相干的 run 也捞进来了 —— 那是另一轮的事"
    )


@pytest.mark.asyncio
async def test_it_stays_off_by_default(db_session) -> None:
    """默认不变 —— 已有调用方（逐 run 读取、契约断言不跨 run）不能被悄悄改语义。"""
    import inspect

    from app.api.v1.execution import list_session_events

    default = inspect.signature(list_session_events).parameters["include_children"].default
    assert getattr(default, "default", default) is False
