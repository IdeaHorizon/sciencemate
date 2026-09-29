"""项目级阻塞查询（v2.1 P4）。

report_blocker 是新架构的一等机制：节点不硬扛也不静默失败，报事实+证据+需求。
但这个信号此前只以 run.blocked 事件存在，用户要一条条翻 run 的事件流才看得到
—— 等于看不到。阻塞是项目级的问题，所以做成项目级查询。
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models.execution import ExecutionEvent, Run, RunStatus
from app.models.project import Project
from app.models.project import ProjectMembership
from app.models.user import User
from tests._authentication_tables import AUTHENTICATION_TABLES

PASSWORD = "BlockerTest2026!"

# 只建这几张表：默认 conftest 的 SQLite 建不了 graph 那套 JSONB 列。
_TABLES = [
    *AUTHENTICATION_TABLES, Project.__table__,    ProjectMembership.__table__, Run.__table__, ExecutionEvent.__table__,
]


@pytest_asyncio.fixture
async def blocker_client():
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


async def _seed(db: AsyncSession):
    user = User(
        email="blocker.owner@atrium.local",
        hashed_password=hash_password(PASSWORD),
        display_name="Owner",
        role="researcher",
        institution_id="inst",
        group_id="grp",
    )
    other = User(
        email="blocker.outsider@other.local",
        hashed_password=hash_password(PASSWORD),
        display_name="Outsider",
        role="researcher",
        institution_id="other-inst",
        group_id="other-grp",
    )
    db.add_all([user, other])
    await db.flush()
    project = Project(owner_id=user.id, name="Blocked project")
    db.add(project)
    await db.flush()
    return user, other, project


async def _blocked_run(
    db: AsyncSession, project, *, run_id: str, status: RunStatus, summary: str,
    minutes_ago: int = 0, node: str = "experiment", sequence: int = 1,
):
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    run = Run(
        id=run_id, tenant_id="t", workspace_id="w", project_id=project.id,
        session_id="s1", node_type=node, status=status,
    )
    db.add(run)
    db.add(ExecutionEvent(
        id=f"ev_{run_id}", tenant_id="t", workspace_id="w", project_id=project.id,
        session_id="s1", run_id=run_id, sequence=sequence, occurred_at=now,
        origin="raw_transcript", source={}, kind="run.blocked", visibility="summary",
        adapter_version="test", schema_version=1,
        payload={
            "blockerId": f"blk_{run_id}", "reportingNode": node,
            "category": "missing_resource", "summary": summary,
            "requestedAction": "给 GPU 队列授权", "suggestedOwner": "user",
            "retryableAfterChange": True, "evidencePaths": ["experiments/logs/queue.txt"],
        },
    ))
    await db.flush()
    return run


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


@pytest.mark.asyncio
async def test_blockers_are_listed_with_their_ask(blocker_client):
    client, db_session = blocker_client
    user, _, project = await _seed(db_session)
    await _blocked_run(db_session, project, run_id="r1",
                       status=RunStatus.INCOMPLETE, summary="GPU 队列没有权限")
    await db_session.commit()

    response = await client.get(
        f"/api/v1/projects/{project.id}/blockers",
        headers=await _headers(client, user.email))
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert len(blockers) == 1
    row = blockers[0]
    assert row["summary"] == "GPU 队列没有权限"
    assert row["requestedAction"] == "给 GPU 队列授权"      # 光说"卡住了"没用
    assert row["reportingNode"] == "experiment"
    assert row["evidencePaths"] == ["experiments/logs/queue.txt"]
    assert row["stale"] is False


@pytest.mark.asyncio
async def test_blocker_on_a_finished_run_is_marked_stale(blocker_client):
    """报了阻塞之后 run 又跑完了 —— 别让用户去处理一个已经不存在的问题。"""
    client, db_session = blocker_client
    user, _, project = await _seed(db_session)
    await _blocked_run(db_session, project, run_id="r_done",
                       status=RunStatus.COMPLETED, summary="缺 dataset")
    await db_session.commit()

    response = await client.get(
        f"/api/v1/projects/{project.id}/blockers",
        headers=await _headers(client, user.email))
    assert response.json()["blockers"][0]["stale"] is True


@pytest.mark.asyncio
async def test_newest_first(blocker_client):
    client, db_session = blocker_client
    user, _, project = await _seed(db_session)
    await _blocked_run(db_session, project, run_id="old",
                       status=RunStatus.INCOMPLETE, summary="旧的", minutes_ago=60)
    await _blocked_run(db_session, project, run_id="new",
                       status=RunStatus.INCOMPLETE, summary="新的", minutes_ago=1,
                       sequence=2)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/projects/{project.id}/blockers",
        headers=await _headers(client, user.email))
    assert [b["summary"] for b in response.json()["blockers"]] == ["新的", "旧的"]


@pytest.mark.asyncio
async def test_outsider_cannot_read_blockers(blocker_client):
    """阻塞正文里常有路径和内部细节 —— 与项目本体同一道访问控制。"""
    client, db_session = blocker_client
    _, outsider, project = await _seed(db_session)
    await _blocked_run(db_session, project, run_id="r1",
                       status=RunStatus.INCOMPLETE, summary="secret path")
    await db_session.commit()

    response = await client.get(
        f"/api/v1/projects/{project.id}/blockers",
        headers=await _headers(client, outsider.email))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_project_is_404(blocker_client):
    client, db_session = blocker_client
    user, _, _project = await _seed(db_session)
    await db_session.commit()
    response = await client.get(
        "/api/v1/projects/does-not-exist/blockers",
        headers=await _headers(client, user.email))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_no_blockers_is_an_empty_list_not_an_error(blocker_client):
    client, db_session = blocker_client
    user, _, project = await _seed(db_session)
    await db_session.commit()
    response = await client.get(
        f"/api/v1/projects/{project.id}/blockers",
        headers=await _headers(client, user.email))
    assert response.status_code == 200
    assert response.json()["blockers"] == []
