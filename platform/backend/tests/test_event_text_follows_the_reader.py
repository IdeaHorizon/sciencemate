"""平台写进执行事件的话，按**读它的那一屏**的语言说。

摄取的时候没有人在看：同一条事件日后可能被中文界面打开，也可能被英文界面打开
（组织项目里还是不同的人）。所以写的时候两种都存，读的时候挑一边 —— 判据落在
接口返回的字上，不落在库里存了什么。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.execution import get_runtime_tenant_id, router
from app.auth import get_current_user
from app.database import get_db
from app.services.execution_ingest import ExecutionIngestService

from .test_execution_foundation import AT, _context, _ingest, execution_db  # noqa: F401


@pytest.mark.asyncio
async def test_a_stopped_loop_is_read_in_the_language_of_the_screen(execution_db) -> None:  # noqa: F811
    service = ExecutionIngestService()
    context = _context()
    state: dict = {}
    for offset, record in enumerate([
        {"event": "run_start", "at": AT, "node_type": "orchestrator"},
        {"event": "unattended_loop_stopped", "at": AT, "reason": "live_pause_pending"},
    ]):
        await _ingest(service, execution_db, context, record, offset=offset * 100, state=state)

    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def override_db():
        yield execution_db

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="reader")
    api.dependency_overrides[get_runtime_tenant_id] = lambda: "tenant-a"

    async def message(lang: str) -> str:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
            page = (await client.get("/api/v1/sessions/session-a/events",
                                     headers={"X-Interface-Language": lang})).json()
        return next(item["payload"]["content"] for item in page["items"]
                    if item["kind"] == "session.message")

    assert await message("zh") == "🛑 自主研究已停止：有一个决策在等你处理。"
    assert await message("en") == "🛑 Autonomous research has stopped: a decision is waiting for you."
