"""「Ready」是上一次探活留下的快照，不是当前事实 —— 界面必须拿得到它多旧。

## 2026-08-21 wangd 的原话

「现在在咱们这个机器上注册的那么多 API，感觉很多都不能用，从而放着有啥用呢」

查下来探针本身是好的：拿一把假 key 建连接，当场就报 credentials_rejected。
病根是探活**只在写操作时跑**（新建 / 改凭证 / 设默认），此后 `status` 就再也
不动了。而列表接口只投影这个派生出来的 `status`，不投影观测本身 —— 于是三周
前的快照和当前事实在界面上长得一模一样，没有任何人能分辨。

## 这些测试守什么

- 列表必须把**证据**一起交出去（是否启用、上次探于何时、结论、provider 原话），
  而不是只给那个由证据派生的判决。
- 「测试连接」看得见就能点：研究员改不了机构那条连接，但"它现在还能不能用"
  正是他要判断的事。只让管理员探 = 让其他人继续对着陈旧的 Ready 猜。
- 冷却期内不重复打 provider，且**如实说**这次没真去打（`probed_now=False`）。
  藏起来的话，界面只能假装每次都是新的 —— 又是一个"看起来像事实的快照"。
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import hash_password
from app.config import settings as app_settings
from app.database import get_db
from app.main import app
from app.models.model_backend import ModelBackendConfig
from app.models.user import User

PASSWORD = "ProbeTest2026!"
INSTITUTION = "ieit"
GROUP = "computational-science"


@pytest.fixture(autouse=True)
def _harness_on(monkeypatch):
    """不打开桥，backend_status 会先返回 harness_disabled，测不到凭证那一档。"""
    monkeypatch.setattr(app_settings, "harness_bridge_enabled", True)


@pytest_asyncio.fixture
async def world(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        users = {}
        for email, name, role in (
            ("institution.admin@atrium.local", "Institution Admin", "institution_admin"),
            ("researcher@atrium.local", "Researcher", "researcher"),
        ):
            user = User(
                email=email,
                hashed_password=hash_password(PASSWORD),
                display_name=name,
                role=role,
                institution_id=INSTITUTION,
                institution_name="Atrium University",
                group_id=GROUP,
                group_name="Computational Science Lab",
            )
            db.add(user)
            users[email] = user
        await db.flush()

        backend = ModelBackendConfig(
            scope_kind="institution",
            scope_id=INSTITUTION,
            provider="openai_compatible",
            display_name="Institution gateway",
            model="some-model",
            base_url="https://api.example.com/v1",
            credential_source="encrypted",
            encrypted_api_key="cipher",
            is_enabled=True,
            created_by_user_id=users["institution.admin@atrium.local"].id,
        )
        db.add(backend)
        await db.commit()
        backend_id = backend.id

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory, backend_id
    app.dependency_overrides.clear()


async def _headers(client, email) -> dict[str, str]:
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


async def _stamp_probe(factory, backend_id, *, at, ok, detail="provider said so"):
    async with factory() as db:
        config = await db.get(ModelBackendConfig, backend_id)
        config.last_probe_at = at
        config.last_probe_ok = ok
        config.last_probe_detail = detail
        await db.commit()


@pytest.mark.asyncio
async def test_the_list_hands_over_the_evidence_not_only_the_verdict(world):
    """只给 status = 让人把三周前的快照当现在。"""
    client, factory, backend_id = world
    probed_at = datetime.now(UTC) - timedelta(days=21)
    await _stamp_probe(factory, backend_id, at=probed_at, ok=True, detail="provider accepted the credential")

    headers = await _headers(client, "researcher@atrium.local")
    response = await client.get("/api/v1/settings/model-backends", headers=headers)
    assert response.status_code == 200
    row = next(b for b in response.json() if b["id"] == backend_id)

    for field in ("is_enabled", "last_probe_at", "last_probe_ok", "last_probe_detail"):
        assert field in row, f"列表没有投影 {field} —— 界面就说不出这个 Ready 有多旧"
    assert row["last_probe_ok"] is True
    assert row["last_probe_detail"] == "provider accepted the credential"
    assert row["is_enabled"] is True
    assert row["last_probe_at"] is not None


@pytest.mark.asyncio
async def test_anyone_who_can_see_it_can_test_it(world, monkeypatch):
    """研究员改不了机构那条连接，但必须能问"它现在还活着吗"。"""
    client, factory, backend_id = world
    await _stamp_probe(factory, backend_id, at=datetime.now(UTC) - timedelta(days=3), ok=True)

    calls: list[str] = []

    async def _fake_probe(config):
        calls.append(config.id)
        return False, "provider rejected the credential (HTTP 401)"

    monkeypatch.setattr("app.services.model_backends.probe_backend_credential", _fake_probe)

    headers = await _headers(client, "researcher@atrium.local")
    response = await client.post(
        f"/api/v1/settings/model-backends/{backend_id}/probe", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["probed_now"] is True
    assert calls == [backend_id], "没真去打 provider"
    assert body["last_probe_ok"] is False
    assert body["status"] == "credentials_rejected", "探出来是坏的，判决就得跟着变"
    # 改的权限没有被这个端点顺手放开
    assert body["editable"] is False


@pytest.mark.asyncio
async def test_within_the_cooldown_it_says_so_instead_of_pretending(world, monkeypatch):
    """冷却期内返回旧观测是对的，**假装刚测过**不是。"""
    client, factory, backend_id = world
    await _stamp_probe(factory, backend_id, at=datetime.now(UTC) - timedelta(seconds=5), ok=True)

    calls: list[str] = []

    async def _fake_probe(config):
        calls.append(config.id)
        return True, "should not be called"

    monkeypatch.setattr("app.services.model_backends.probe_backend_credential", _fake_probe)

    headers = await _headers(client, "institution.admin@atrium.local")
    response = await client.post(
        f"/api/v1/settings/model-backends/{backend_id}/probe", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [], "冷却期内不该再打 provider"
    assert body["probed_now"] is False, "没真去打就必须说出来"
    assert body["cooldown_seconds"] > 0


@pytest.mark.asyncio
async def test_a_never_probed_backend_can_be_probed(world, monkeypatch):
    """从没探过 = 没有冷却可言，第一次点就得真去打。"""
    client, factory, backend_id = world

    calls: list[str] = []

    async def _fake_probe(config):
        calls.append(config.id)
        return True, "provider accepted the credential"

    monkeypatch.setattr("app.services.model_backends.probe_backend_credential", _fake_probe)

    headers = await _headers(client, "researcher@atrium.local")
    body = (
        await client.post(f"/api/v1/settings/model-backends/{backend_id}/probe", headers=headers)
    ).json()
    assert calls == [backend_id]
    assert body["probed_now"] is True


@pytest.mark.asyncio
async def test_probing_something_you_cannot_see_is_a_404_not_a_probe(world, monkeypatch):
    """看不见就不该能拿别人的凭证去打 provider。"""
    client, _factory, _backend_id = world

    async def _fake_probe(config):  # pragma: no cover - 不该被调用
        raise AssertionError("不该探一个看不见的后端")

    monkeypatch.setattr("app.services.model_backends.probe_backend_credential", _fake_probe)
    headers = await _headers(client, "researcher@atrium.local")
    response = await client.post(
        "/api/v1/settings/model-backends/00000000-0000-0000-0000-000000000000/probe",
        headers=headers,
    )
    assert response.status_code in (403, 404)
