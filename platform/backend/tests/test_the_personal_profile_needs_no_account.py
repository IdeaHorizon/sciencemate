"""个人档没有登录这一步；组织档一如既往。

领导的要求是「首先个人用得爽，不教育用户」。落到机器上最硬的一条就是：软件装在
自己的电脑上，账号这件事从头到尾不存在 —— 不注册、不登录、不会过期。

组织档不受影响：那里有别人，身份就必须证明。
"""
from __future__ import annotations

import pytest

from app import assembly
from app.auth import get_current_user, implicit_local_user
from app.config import settings
from app.main import app
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


@pytest.mark.asyncio
async def test_without_a_token_the_personal_profile_still_answers(runtime_client, monkeypatch):
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "debug", False)  # 不靠 DEBUG 那条老后门
    assembly.install(app)
    try:
        me = await client.get("/api/v1/auth/me")
        projects = await client.get("/api/v1/projects/")
    finally:
        assembly.uninstall(app)
    assert me.status_code == 200, me.text
    assert me.json()["email"], "个人档也要有一个身份 —— 只是不用去证明它"
    assert projects.status_code == 200, projects.text


@pytest.mark.asyncio
async def test_the_org_profile_still_demands_a_token(runtime_client, monkeypatch):
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "local_demo_mode", True)  # 免去"配一个真密钥"
    assembly.install(app)
    try:
        anonymous = await client.get("/api/v1/projects/")
        token = await _token(client, "researcher@atrium.local")
        with_token = await client.get("/api/v1/projects/", headers=_headers(token))
    finally:
        assembly.uninstall(app)
    assert anonymous.status_code == 401, anonymous.text
    assert with_token.status_code == 200


@pytest.mark.asyncio
async def test_uninstall_puts_authentication_back(monkeypatch):
    """装上不拆，下一次进出 lifespan 就带着上一次的接线跑。

    2026-09-05 实测：模块级开关的版本让同一个 xdist worker 里后面每条测试的
    「当前用户」都变成本机用户，10 条测试报"项目列表是空的" —— 一个指不到
    病因的错。
    """
    monkeypatch.setattr(settings, "profile", "personal")
    assembly.install(app)
    assert app.dependency_overrides.get(get_current_user) is implicit_local_user
    assembly.uninstall(app)
    assert get_current_user not in app.dependency_overrides


def test_only_the_org_profile_demands_a_real_secret_key(monkeypatch) -> None:
    """签发 token 的档位才需要密钥。

    个人档一个 token 都不签。在那里要求"先配一个 SECRET_KEY"，等于让用户为
    一件不存在的事做准备 —— 正是「教育用户」的样子，也正是个人档要消灭的
    那种前置条件。
    """
    monkeypatch.setattr(settings, "local_demo_mode", False)
    monkeypatch.setattr(settings, "secret_key", assembly._DEV_SECRET_KEY)

    monkeypatch.setattr(settings, "profile", "personal")
    assembly.install(app)          # 不抛
    assembly.uninstall(app)

    monkeypatch.setattr(settings, "profile", "org")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        assembly.install(app)
