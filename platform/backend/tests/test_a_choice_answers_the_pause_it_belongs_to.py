"""点选项 = 回答停着的那个 pause —— 入口按「这次提交是什么」分派，不按占用。

## 现场（2026-09-03，cuib，会话 de4632cc）

决策卡挂着，选了 REVISE、附言空，点 Submit 没反应。前端那一半（四道各自的
"有没有东西可发"闸）另有测试；这里是后端那一半：`chat.py` 曾把
`is_occupied()` 的占用分流排在读 `choice` 之前，而插话路径根本不收 choice ——
worker 在飞时人点的选项会被翻成一句插话文案投进收件箱，choice_id 原地丢失，
卡片原样留着。

现在入口先解析成命令（`ChoiceAnswer | TextAnswer`），再按类型分派：

- `choice` 一律走 answer 路，占用只意味着排队；没有停着的 pause 就响亮地 409。
- `text` 才做占用分流（开新轮 / 插话）。
- 旧线格式（`message` + `choice`）直接 422，不被任何一层重新解释。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models.execution import (
    ExecutionEvent,
    Run,
    RunStatus,
    SessionMessage,
    SessionProjection,
)
from app.models.project import Project, ProjectMembership
from app.models.user import User
from tests._authentication_tables import AUTHENTICATION_TABLES

PASSWORD = "ChoiceAnswers2026!"
OFFER_ID = "1788332859-ecd718:pecb8eac6:o3b1cc632"

_TABLES = [
    *AUTHENTICATION_TABLES, Project.__table__,    ProjectMembership.__table__, Run.__table__, ExecutionEvent.__table__,
    SessionProjection.__table__, SessionMessage.__table__,
]


@pytest_asyncio.fixture
async def paused_client(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_TABLES))
        await connection.exec_driver_sql(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
            " status TEXT NOT NULL)")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        async def override_get_db():
            yield db
        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, db
    app.dependency_overrides.clear()
    await engine.dispose()


async def _headers(client, email: str) -> dict[str, str]:
    """这个人的一张 token —— 直接签，不走登录端点（登录那扇门是专业版的，公开树里没有）。
    用户从这个夹具接给 app 的那条 get_db 里查：夹具怎么装配库，这里就怎么问。"""
    from sqlalchemy import select

    from app.auth import create_access_token
    from app.database import get_db
    from app.main import app as the_app
    from app.models.user import User

    provide = the_app.dependency_overrides.get(get_db) or get_db
    sessions = provide()
    db = await sessions.__anext__()
    try:
        user = await db.scalar(select(User).where(User.email == email))
    finally:
        try:
            await sessions.aclose()
        except Exception:  # noqa: BLE001 - 夹具的生成器怎么收尾是它的事
            pass
    assert user is not None, f"没有 {email} 这个用户"
    return {"Authorization": f"Bearer {create_access_token(user)}"}


async def _seed_waiting_session(db, tmp_path):
    user = User(
        email="choice.owner@atrium.local",
        hashed_password=hash_password(PASSWORD), display_name="Owner",
        role="researcher", institution_id="inst", group_id="grp",
    )
    db.add(user)
    await db.flush()
    project = Project(owner_id=user.id, name="Waiting project")
    db.add(project)
    await db.flush()
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    db.add(SessionProjection(
        tenant_id="t", workspace_id="w", project_id=str(project.id),
        session_id="s-waiting", initiating_user_id=str(user.id),
        git_worktree_path=str(worktree),
    ))
    db.add(Run(
        id="run_waiting", tenant_id="t", workspace_id="w",
        project_id=str(project.id), session_id="s-waiting", parent_run_id=None,
        status=RunStatus.WAITING_HUMAN.value, summary={},
    ))
    await db.flush()
    await db.commit()
    return user, project


def _fake_binding():
    from app.services.harness_sessions import AppRunBinding

    return AppRunBinding("u", "s-waiting", "run_waiting", "s-waiting", "", None, "")


@pytest.mark.asyncio
async def test_a_choice_reaches_the_answer_path_even_when_the_session_is_occupied(
    paused_client, tmp_path, monkeypatch,
) -> None:
    """占用只意味着排队。点选项不许被翻成插话文案。"""
    client, db = paused_client
    user, project = await _seed_waiting_session(db, tmp_path)

    from app.services import harness_sessions, local_execution

    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "is_occupied", lambda *_a, **_k: True)
    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "paused_binding",
        lambda *_a, **_k: _fake_binding())

    async def _no_stale(*_a, **_k):
        return None

    monkeypatch.setattr(harness_sessions, "assert_conversation_runtime_available", _no_stale)

    delivered: list[dict] = []

    async def _deliver(**kwargs):
        delivered.append(kwargs)
        return {"delivered": True, "item_id": "in-1", "occupancy": "working"}

    monkeypatch.setattr(harness_sessions.harness_session_manager, "deliver", _deliver)

    executed: list[dict] = []

    async def _execute(_db, **kwargs):
        executed.append(kwargs)
        return {"reply": "", "run_id": "run_waiting", "status": "completed"}

    monkeypatch.setattr(local_execution, "execute_local_turn", _execute)
    monkeypatch.setattr(local_execution, "retain_detached_execution", lambda task: None)
    monkeypatch.setattr("app.api.v1.chat.LOCAL_SSE_KEEPALIVE_SECONDS", 0.01)

    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={
            "answer": {"kind": "choice", "offer_id": OFFER_ID, "choice_id": "revise", "note": ""},
            "conversation_id": "s-waiting",
        },
        headers=await _headers(client, user.email),
    )
    assert response.status_code == 200, response.text[:300]
    assert "interject" not in response.text, (
        f"点选项被当成插话投进了收件箱 —— choice_id 就这么丢了：{response.text[:300]}"
    )
    assert delivered == [], "answer 路不走收件箱投递"
    assert len(executed) == 1, executed
    assert executed[0]["choice"] == {"offer_id": OFFER_ID, "choice_id": "revise"}
    assert executed[0]["message"] == "", "附言为空时 message 就是空串，不是编一句文案"


@pytest.mark.asyncio
async def test_a_choice_with_nothing_to_answer_is_refused_loudly(
    paused_client, tmp_path, monkeypatch,
) -> None:
    """卡片过期（没有停着的 pause）→ 409 带 code，不落消息、不投递、不开新轮。"""
    client, db = paused_client
    user, project = await _seed_waiting_session(db, tmp_path)

    from app.services import harness_sessions, local_execution

    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "paused_binding", lambda *_a, **_k: None)
    monkeypatch.setattr(
        harness_sessions.harness_session_manager, "is_occupied", lambda *_a, **_k: False)

    async def _never(*_a, **_k):
        raise AssertionError("不该走到执行层")

    monkeypatch.setattr(local_execution, "execute_local_turn", _never)
    monkeypatch.setattr(harness_sessions.harness_session_manager, "deliver", _never)

    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={
            "answer": {"kind": "choice", "offer_id": OFFER_ID, "choice_id": "revise", "note": "理由"},
            "conversation_id": "s-waiting",
        },
        headers=await _headers(client, user.email),
    )
    assert response.status_code == 409, response.text[:300]
    assert response.json()["detail"]["code"] == "no_pause_to_answer"
    rows = (await db.execute(
        select(SessionMessage).where(SessionMessage.session_id == "s-waiting")
    )).scalars().all()
    assert rows == [], "被拒的提交不许留下一条像是发出去了的消息"


@pytest.mark.asyncio
async def test_the_legacy_wire_shape_is_rejected_not_reinterpreted(
    paused_client, tmp_path,
) -> None:
    client, db = paused_client
    user, project = await _seed_waiting_session(db, tmp_path)
    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={
            "message": "REVISE (re-run source_node with reviewer feedback)",
            "choice": {"offer_id": OFFER_ID, "choice_id": "revise"},
            "conversation_id": "s-waiting",
        },
        headers=await _headers(client, user.email),
    )
    assert response.status_code == 422, response.text[:300]


@pytest.mark.asyncio
async def test_a_blank_text_is_refused_at_the_door(paused_client, tmp_path) -> None:
    client, db = paused_client
    user, project = await _seed_waiting_session(db, tmp_path)
    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={"answer": {"kind": "text", "text": "   "}, "conversation_id": "s-waiting"},
        headers=await _headers(client, user.email),
    )
    assert response.status_code == 422, response.text[:300]
