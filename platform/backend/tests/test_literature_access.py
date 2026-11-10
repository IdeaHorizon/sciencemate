"""E2E private sources keep the UI's job policy and live user authorization."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.user import User
from app.models.cnki import CnkiDevice, CnkiJob, CnkiPaper, CnkiJobPaper
from app.models.feed import FeedSource, FeedItem
from app.schemas.cnki import JobIn
from app.services import cnki, literature_access as access


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/private.db")
    tables = [User.__table__, FeedSource.__table__, FeedItem.__table__, CnkiDevice.__table__,
              CnkiJob.__table__, CnkiPaper.__table__, CnkiJobPaper.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(access, "get_session_factory", lambda: sessions)
    users = [User(id=str(uuid4()), email=f"test{i}@example.invalid", hashed_password=f"test-{i}",
                  display_name=f"Test {i}", is_active=True, institution_id=f"school-{i}") for i in range(2)]
    async with sessions.begin() as db:
        db.add_all(users)
    broker = access.LiteratureAccess()
    monkeypatch.setattr(broker, "directory", lambda: tmp_path / "access")
    configs = [json.loads(Path((await broker.environment(u))["HARNESS_LITERATURE_ACCESS_FILE"]).read_text()) for u in users]
    yield SimpleNamespace(broker=broker, sessions=sessions, users=users, configs=configs, directory=tmp_path / "access")
    await broker.stop()
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 200])
async def test_cnki_job_matches_ui_and_other_user_cannot_read_it(rig, limit):
    user = rig.users[0]
    async with rig.sessions.begin() as db:
        ui_job = await cnki.create_job(db, user, JobIn(query="颗粒沉降", limit=limit))
    response = await rig.broker.dispatch({"token": rig.configs[0]["token"], "op": "cnki_submit",
                                         "payload": {"query": "颗粒沉降", "limit": limit, "user_id": rig.users[1].id}})
    assert response["id"] == ui_job["id"]
    assert response["recurring"] is False
    with pytest.raises(PermissionError):
        await rig.broker.dispatch({"token": rig.configs[1]["token"], "op": "cnki_status",
                                  "payload": {"job_id": ui_job["id"]}})


@pytest.mark.asyncio
async def test_institution_and_credential_are_rechecked(rig, monkeypatch):
    institutions = []
    def search(query, *, institution_id, limit):
        institutions.append(institution_id)
        return []
    monkeypatch.setattr(access.school_library, "search", search)
    request = {"token": rig.configs[0]["token"], "op": "school",
               "payload": {"query": "test", "institution_id": "school-1"}}
    await rig.broker.dispatch(request)
    assert institutions == ["school-0"]
    async with rig.sessions.begin() as db:
        user = await db.get(User, rig.users[0].id)
        user.hashed_password = "changed"
    with pytest.raises(PermissionError):
        await rig.broker.dispatch(request)
    with pytest.raises(PermissionError):
        await rig.broker.dispatch({**request, "token": "invalid"})


@pytest.mark.asyncio
async def test_capability_survives_backend_restart_without_exposing_login(rig):
    import asyncio
    paths = list(rig.directory.glob("*.json"))
    assert all(p.stat().st_mode & 0o077 == 0 for p in paths)
    token = rig.configs[0]["token"]
    await rig.broker.stop()
    await rig.broker.start()
    path = next(p for p in paths if json.loads(p.read_text())["token"] == token)
    config = json.loads(path.read_text())
    reader, writer = await asyncio.open_connection(config["host"], config["port"])
    writer.write(json.dumps({"token": token, "op": "cnki_submit", "payload": {"query": "新问题"}}).encode()+b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    assert response["ok"] and response["result"]["query"] == "新问题"
    assert "token" not in response["result"] and "credential" not in response["result"]


@pytest.mark.asyncio
async def test_disabled_user_cannot_create_background_work(rig):
    async with rig.sessions.begin() as db:
        user = await db.get(User, rig.users[0].id)
        user.is_active = False
    with pytest.raises(PermissionError):
        await rig.broker.dispatch({"token": rig.configs[0]["token"], "op": "cnki_submit",
                                  "payload": {"query": "test"}})
