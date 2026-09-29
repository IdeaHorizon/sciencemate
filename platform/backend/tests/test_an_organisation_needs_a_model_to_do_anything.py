"""组织项目跑在组织服务器的模型上 —— 那台服务器上一个模型都没有时，要说清谁来补。

`RFC_ORGANISATION_PAGE_20260923` E 批。2026-09-24 核实：装服务器、建组织都不配任何模型，
桌面上的「设置 → 模型」只改本机 —— 于是一个从桌面新建的组织，组织项目连一次会话都开
不起来，而服务器答的是一句英文 `No ready model backend is available for role 'reasoning'`。
成员读不出"这要组织管理员去配"，也不知道去哪配。

判据：同一个空局面，在组织服务器上说「请组织管理员在『组织 → 设置 → 模型』里加一个」，
在个人版本机上说「去『设置 → 模型与密钥』配一个」。走的是真的建会话那条路。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.config import settings
from app.models.model_backend import ModelBackendConfig
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


async def _start_a_session_with_no_model_anywhere(client, factory):
    async with factory() as db:
        await db.execute(delete(ModelBackendConfig))
        await db.commit()
    member = _headers(await _token(client, "researcher@atrium.local"))
    project_id = (await client.get("/api/v1/projects/", headers=member)).json()[0]["id"]
    return await client.post(f"/api/v1/projects/{project_id}/sessions", headers=member,
                             json={"title": "第一句话"})


@pytest.mark.asyncio
async def test_on_an_organisation_server_it_names_the_administrator(runtime_client, monkeypatch) -> None:  # noqa: F811
    client, factory = runtime_client
    monkeypatch.setattr(settings, "profile", "org")

    refused = await _start_a_session_with_no_model_anywhere(client, factory)

    assert refused.status_code == 503
    said = refused.json()["detail"]
    assert "组织管理员" in said and "组织 → 设置 → 模型" in said, (
        f"组织服务器上没模型时没说谁来补、去哪补：{said}")
    assert "No ready model backend" not in said


@pytest.mark.asyncio
async def test_on_a_personal_desktop_it_points_at_the_settings(runtime_client, monkeypatch) -> None:  # noqa: F811
    client, factory = runtime_client
    monkeypatch.setattr(settings, "profile", "personal")

    refused = await _start_a_session_with_no_model_anywhere(client, factory)

    assert refused.status_code == 503
    said = refused.json()["detail"]
    assert "设置 → 模型与密钥" in said, said
    assert "组织" not in said, "个人版本机上提到了组织 —— 个人版不画任何组织概念"
