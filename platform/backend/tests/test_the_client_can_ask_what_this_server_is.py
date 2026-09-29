"""`/capabilities`：这台 App Server 装配成了什么形态。

个人档里没有登录这一步，所以前端在拿到任何身份之前就得知道「这里要不要登录」。
把这个问题藏在鉴权后面就成了先有鸡还是先有蛋。

它也是「个人档首次使用路径上不出现组织概念」这条规矩的机械判据（RFC R1）：
个人档报出的能力集合里一个组织相关的都没有，前端因此画不出那些入口。
"""
from __future__ import annotations

import pytest

from app.config import settings
from tests.test_local_runtime_api import runtime_client  # noqa: F401

ORG_ONLY = {"auth", "members", "governance", "feed_collection"}


@pytest.mark.asyncio
async def test_anyone_can_ask_without_signing_in(runtime_client) -> None:
    client, _factory = runtime_client
    response = await client.get("/api/v1/capabilities")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) <= {"profile", "edition", "features", "credentialKeyStorage"}
    # 发行和档位一样只是显示用的名字；源码 checkout 里没有 edition.json，所以是个人版。
    # 组织档不答这个问题（见下一条）。
    assert payload.get("edition", "personal") in {"personal", "pro"}


@pytest.mark.asyncio
async def test_an_org_server_does_not_tell_strangers_how_it_stores_keys(
    runtime_client, monkeypatch,
) -> None:
    """组织档不交出 `credentialKeyStorage`。

    这个接口**不需要登录**就能读。个人档只监听 127.0.0.1，读得到它的就是本人；
    组织档是网络可达的，把"这份安装用的是哪种密钥存法"告诉一个还没登录的人，是
    白送一条侦察情报（"它退回文件了"正是攻击者想先知道的那一条）。

    组织档的答案恒为 operator —— 本来也没有第二种可能，界面用兜底那句就够。
    """
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "org")

    payload = (await client.get("/api/v1/capabilities")).json()

    assert "credentialKeyStorage" not in payload, (
        "组织档把密钥存法告诉了未登录的人"
    )


@pytest.mark.asyncio
async def test_the_personal_profile_says_where_the_key_lives(
    runtime_client, monkeypatch,
) -> None:
    """个人档要交出来 —— 首次运行向导靠它说一句为真的话（#825）。

    只交出**类别**，不交出路径：类别够界面选句子了，路径是多余的暴露面。
    """
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "personal")

    payload = (await client.get("/api/v1/capabilities")).json()

    assert payload.get("credentialKeyStorage") in {
        "keychain", "file", "operator", "unknown",
    }, f"给了界面一个它不认识的值：{payload.get('credentialKeyStorage')!r}"
    assert "/" not in str(payload.get("credentialKeyStorage")), (
        "交出了路径 —— 界面只需要知道是哪一类"
    )


@pytest.mark.asyncio
async def test_the_personal_profile_reports_no_organisation_features(
    runtime_client, monkeypatch,
) -> None:
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "personal")
    payload = (await client.get("/api/v1/capabilities")).json()
    assert payload["profile"] == "personal"
    assert not ORG_ONLY & set(payload["features"]), payload["features"]
    # 但正事一样不少 —— 个人档不是阉割版。
    assert {"projects", "sessions", "artifacts", "knowledge", "memory"} <= set(payload["features"])


@pytest.mark.asyncio
async def test_the_org_profile_reports_them(runtime_client, monkeypatch) -> None:
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "org")
    payload = (await client.get("/api/v1/capabilities")).json()
    assert payload["profile"] == "org"
    assert ORG_ONLY <= set(payload["features"]), payload["features"]


@pytest.mark.asyncio
async def test_an_org_server_does_not_claim_to_be_a_desktop_edition(
    runtime_client, monkeypatch,
) -> None:
    """组织服务器不回答"我是哪种发行"。

    那台机器不是谁的桌面，也不从发行的更新源自更新。此前它照答不误，答的是回落值
    `personal` —— 一台专业版的服务器自报"个人版"，是一句假话。2026-09-16 真装一台
    服务器时看见的。答不出的问题就不答，别给一个看起来像答案的默认值。
    """
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "profile", "org")

    payload = (await client.get("/api/v1/capabilities")).json()

    assert "edition" not in payload, "组织服务器自报了一个桌面发行"
