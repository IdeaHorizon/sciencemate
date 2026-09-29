"""会话被占着的时候，用户的话照样收得下 —— 走真端点，不抄查询。

## 现场回放（wangd 2026-08-21）

跑轮中问「怎么样了？」，读到：

    这一轮没能完成. 平台在记录这次运行时撞上了内部错误。

真实原因：一个 unattended worker 停靠在 4 小时的复查间隔上（还活着、还攥着
会话的操作锁），而它那条顶层 run 早在 3 小时前就被平台盖成 `incomplete`。

- 库里的 run 行说「没有活跃 run」→ 这句话被当成**新一轮**；
- 内存里的 operation lock 说「忙」→ 新一轮在 `_get_or_create` 被拒收。

两个真相源**方向相反地各错一边**，而真实状态（「停靠等唤醒」）两边都表达不
出来。分叉时没有任何一层报错，要等下一个请求撞上才炸。

## 为什么这个测试必须走真端点

同一族的既有测试（`test_a_child_run_never_blocks_the_session`）是**把 chat.py
那条 SQL 抄进测试里**再断言。抄件只能验证「我抄的这条查询按我想的那样返回」，
它永远问不出「除了这条查询，还有没有别的东西会拒收」—— 而 8-21 炸的恰恰是
查询之外的那一层。判据得绑真实调用路径，不是绑我对路径的记忆。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
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

PASSWORD = "OccupiedSession2026!"

_TABLES = [
    *AUTHENTICATION_TABLES, Project.__table__,    ProjectMembership.__table__, Run.__table__, ExecutionEvent.__table__,
    SessionProjection.__table__, SessionMessage.__table__,
]


@pytest.fixture(autouse=True)
def _harness_root_available(monkeypatch):
    """收件箱是 harness 侧的模块 —— 指向真的 checkout，用真的 `deposit`。

    不 stub 它：这条测试问的正是"话有没有真的投出去"，而 stub 掉投递面之后
    它就只能验证"我调了一个我自己写的假函数"。
    """
    from app.config import settings
    from app.services import harness_contract

    repo_root = __import__("pathlib").Path(__file__).resolve().parents[3]
    monkeypatch.setattr(settings, "harness_root", str(repo_root), raising=False)
    harness_contract._harness_root.cache_clear()
    yield
    harness_contract._harness_root.cache_clear()


@pytest_asyncio.fixture
async def occupied_client(tmp_path):
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


async def _seed_parked_session(db, tmp_path, *, run_status: RunStatus):
    """8-21 的局面：worker 还占着，而它那条顶层 run 已经是终态。"""
    user = User(
        email="occupied.owner@atrium.local",
        hashed_password=hash_password(PASSWORD), display_name="Owner",
        role="researcher", institution_id="inst", group_id="grp",
    )
    db.add(user)
    await db.flush()
    project = Project(owner_id=user.id, name="Parked project")
    db.add(project)
    await db.flush()

    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    db.add(SessionProjection(
        tenant_id="t", workspace_id="w", project_id=str(project.id),
        session_id="s-parked", initiating_user_id=str(user.id),
        git_worktree_path=str(worktree),
    ))
    db.add(Run(
        id="run_parked", tenant_id="t", workspace_id="w",
        project_id=str(project.id), session_id="s-parked", parent_run_id=None,
        # 平台三小时前就把它盖成终态了 —— 而 worker 还在同一个 run 名下干活。
        status=run_status.value, summary={},
    ))
    await db.flush()
    await db.commit()
    return user, project


@pytest.mark.asyncio
async def test_a_parked_worker_gets_the_message_instead_of_an_internal_error(
    occupied_client, tmp_path, monkeypatch,
) -> None:
    """8-21 逐字回放：run 行终态 + worker 占用 → 必须收下，且必须不是错误。"""
    client, db = occupied_client
    user, project = await _seed_parked_session(
        db, tmp_path, run_status=RunStatus.INCOMPLETE)

    from app.services.harness_sessions import harness_session_manager

    # 会话被占着 —— 现实里这是 `_operation_lock.locked()`，停靠中的 worker
    # 同样为真（它睡着，但没人能开新一轮）。
    monkeypatch.setattr(
        harness_session_manager, "is_occupied", lambda *_a, **_k: True)
    monkeypatch.setattr(
        harness_session_manager, "live_binding", lambda *_a, **_k: object())
    # 投递面 2026-08-23 起是 socket（P1-2）：这里没有真 worker 进程，所以把
    # **送达**这一步替掉。回执里的 occupancy 是 worker 自己报的 —— 那正是
    # 这条测试要看到的东西（收下了，而且如实说上一轮还占着）。
    async def _delivered(**_kwargs):
        return {"delivered": True, "item_id": "in-1", "occupancy": "working"}

    monkeypatch.setattr(harness_session_manager, "deliver", _delivered)

    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={"answer": {"kind": "text", "text": "怎么样了？"}, "conversation_id": "s-parked"},
        headers=await _headers(client, user.email),
    )

    assert response.status_code == 200, (
        f"占用的会话把用户的话弹回去了：{response.status_code} {response.text[:300]}"
    )
    body = response.text
    assert '"routed": "interject"' in body or '"routed":"interject"' in body, (
        f"没有走插话路径，说明它又去开新一轮了：{body[:400]}"
    )
    assert "内部错误" not in body, "8-21 那句话又出现了"
    # ack 必须如实说出当下局面，不能只说收下了。**词是 worker 自己报的**
    # （回执里的 occupancy），不是平台按自己的判据造的一句 —— 从前那句由
    # `session_occupied` 推，而在这个分支里它恒为真，等于什么也没说。
    assert '"occupancy": "working"' in body or '"occupancy":"working"' in body, (
        f"ack 没带上 worker 自报的占用状态：{body[:400]}"
    )
    assert "上一轮还占着" in body, "ack 没把「它前面还排着活」这句话说给人听"


@pytest.mark.asyncio
async def test_the_message_is_recorded_not_just_deposited(
    occupied_client, tmp_path, monkeypatch,
) -> None:
    """收下 ≠ 落进对话。用户那句话必须成为消息行，否则像对着空气说话。"""
    client, db = occupied_client
    user, project = await _seed_parked_session(
        db, tmp_path, run_status=RunStatus.INCOMPLETE)

    from app.services.harness_sessions import harness_session_manager

    monkeypatch.setattr(
        harness_session_manager, "is_occupied", lambda *_a, **_k: True)
    monkeypatch.setattr(
        harness_session_manager, "live_binding", lambda *_a, **_k: None)

    await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={"answer": {"kind": "text", "text": "怎么样了？"}, "conversation_id": "s-parked"},
        headers=await _headers(client, user.email),
    )

    from sqlalchemy import select

    messages = (await db.execute(
        select(SessionMessage).where(SessionMessage.session_id == "s-parked")
    )).scalars().all()
    assert any("怎么样了" in (m.content or "") for m in messages), (
        "话收下了却没进对话记录 —— 用户看不到自己说过这句"
    )


@pytest.mark.asyncio
async def test_a_free_session_still_starts_a_new_turn(
    occupied_client, tmp_path, monkeypatch,
) -> None:
    """放宽不能变成放过：没被占用时，这句话仍然开新一轮，不许变成插话。

    少了这条，「一律走插话」也能让上面两条全绿 —— 而那样研究就永远不会开跑。
    """
    client, db = occupied_client
    user, project = await _seed_parked_session(
        db, tmp_path, run_status=RunStatus.COMPLETED)

    from app.services.harness_sessions import harness_session_manager

    monkeypatch.setattr(
        harness_session_manager, "is_occupied", lambda *_a, **_k: False)
    monkeypatch.setattr(
        harness_session_manager, "live_binding", lambda *_a, **_k: None)

    response = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        json={"answer": {"kind": "text", "text": "开始吧"}, "conversation_id": "s-parked"},
        headers=await _headers(client, user.email),
    )
    assert '"routed": "interject"' not in response.text, (
        "空闲的会话把新一轮误判成了插话 —— 研究永远不会开跑"
    )


@pytest.mark.asyncio
async def test_is_occupied_tracks_the_real_lock() -> None:
    """`is_occupied` 自己也得被钉住 —— 用真的会话对象、真的锁。

    变异验证逼出来的（2026-08-21）：把 `is_occupied` 改成 `return False`，上面
    那三条端点测试**全绿** —— 因为它们 monkeypatch 掉了这个方法。测试替身遮住
    了被测实现，等于这个方法从此没有测试。端点那层验的是"分流问了它"，这一层
    验的是"它答得对"，两层都要有。
    """
    import asyncio

    from app.services.harness_sessions import (
        _ProjectHarnessSession,
        harness_session_manager,
    )

    session = object.__new__(_ProjectHarnessSession)
    session.closed = False
    session._operation_lock = asyncio.Lock()
    session.worker = type("W", (), {"alive": True})()

    key = harness_session_manager._key("p-lock", "s-lock")
    harness_session_manager._sessions[key] = session
    try:
        assert harness_session_manager.is_occupied("p-lock", "s-lock") is False
        async with session.claim():
            assert harness_session_manager.is_occupied("p-lock", "s-lock") is True, (
                "锁被持有时必须报占用 —— 停靠中的 worker 正是这个形状"
            )
        # `claim()` 用 `async with` 持有：return / raise / 取消都必然释放。
        assert harness_session_manager.is_occupied("p-lock", "s-lock") is False

        # 进程没了就不算占用：死进程不可能"忙"（8-18 那次僵尸攥锁的教训）。
        session.worker = type("W", (), {"alive": False})()
        async with session.claim():
            assert harness_session_manager.is_occupied("p-lock", "s-lock") is False
    finally:
        harness_session_manager._sessions.pop(key, None)


def test_occupancy_is_derived_from_the_lock_not_from_a_flag() -> None:
    """判据必须是那把锁本身 —— 标志位靠"每条路径都记得清"维持，而那维持不住。

    8-18 实测过一次：`busy` 是内存里的 bool，一条路径忘了清，会话被锁死到重启
    后端为止。锁由 `async with` 持有，return / raise / 取消都必然释放。
    """
    import inspect

    from app.services.harness_sessions import _ProjectHarnessSession

    source = inspect.getsource(_ProjectHarnessSession.conversation_in_flight.fget)
    assert "_operation_lock.locked()" in source, (
        "占用又变回标志位了 —— 它必须从锁推导"
    )
