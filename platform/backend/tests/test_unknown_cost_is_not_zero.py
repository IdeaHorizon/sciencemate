"""成本未知就说未知 —— 别把 NULL 加成 0 再当成事实报出去。

## 现场（2026-08-31，本机跑真课题）

会话列表：`5,932,680 tokens · US$0.00`。
库里这个会话 29 个 run：**28 个 `cost IS NULL`**（真模型后端没配价格），
只有 1 个是 `0.000000`（demo stub，确实免费）。

SQL 的 `SUM` **跳过 NULL** —— 28 个"不知道" + 1 个"零"加出来是"零"，
再带上 `currency: "USD"` 送到前端。前端本来会把 null 渲染成
"cost unavailable"（`SessionIndex.tsx`），是**后端替它编了一个数**。

「不知道」和「零」是两件事。把前者显示成后者，用户对成本的判断就建立在
一个看起来像事实的数字上 —— 而这个会话真实烧掉的是五千多万 token。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.execution import Run, RunAttempt, RunStatus, SessionProjection


async def _noop():
    return None


async def _seed(db_session, monkeypatch, *, costs: list[float | None]):
    from app.models.project import Project
    from app.models.user import User
    from app.services import sessions as sessions_svc
    from app.services.harness_sessions import harness_session_manager

    user = User(email="cost@example.test", hashed_password="x", display_name="C")
    db_session.add(user)
    await db_session.flush()
    project = Project(name="p-cost", owner_id=user.id)
    db_session.add(project)
    await db_session.flush()
    session = SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id="s-cost", initiating_user_id=user.id,
    )
    db_session.add(session)
    await db_session.flush()

    for i, cost in enumerate(costs):
        db_session.add(Run(
            id=f"run_cost_{i}", tenant_id=settings.runtime_tenant_id, workspace_id="w",
            project_id=str(project.id), session_id="s-cost", parent_run_id=None,
            status=RunStatus.COMPLETED.value, summary={},
            total_tokens=1000, cost=cost,
            ended_at=datetime.now(UTC) - timedelta(minutes=5),
        ))
    await db_session.flush()

    monkeypatch.setattr(harness_session_manager, "live_binding", lambda *a, **k: None)
    monkeypatch.setattr(sessions_svc, "_reap_orphaned_runs_of", lambda *a, **k: _noop())
    payload = await sessions_svc.session_response(
        db_session, user=user, project=project, session=session,
    )
    return payload["usage"]


@pytest.mark.asyncio
async def test_one_unpriced_run_makes_the_session_cost_unknown(db_session, monkeypatch):
    """现场配比：一条已知免费 + 一堆未知 → 会话成本必须是未知，不是 0。"""
    usage = await _seed(db_session, monkeypatch, costs=[0.0, None, None])
    assert usage["cost"] is None, (
        f"把「不知道」加成了「零」：cost={usage['cost']!r} —— "
        "界面会告诉用户这个会话花了 US$0.00"
    )
    assert usage["currency"] is None, "成本未知却还带着币种"
    assert usage["totalTokens"] == 3000, "token 是知道的，不该跟着一起变未知"


@pytest.mark.asyncio
async def test_a_fully_priced_session_still_reports_its_cost(db_session, monkeypatch):
    """收缩要有边界：每条都定过价，就该照常报出来。"""
    usage = await _seed(db_session, monkeypatch, costs=[1.5, 2.25])
    assert usage["cost"] == pytest.approx(3.75)
    assert usage["currency"] == "USD"
