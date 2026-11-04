"""不鉴权的自建模型也给会话起标题。

11-04：会话、文献检索、资讯翻译都已经按「谁提供端点」判（`credential_is_optional`），会话命名
还在要 key —— 用不鉴权的自建模型（vLLM / Ollama，key 留空）的人，会话永远是机械标题。
命名把 key 当打码用的 secret 传给子进程桥：没有 key 时传 None，不拿空串当一个值去抹。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import session_naming


def _backend(base_url: str | None) -> SimpleNamespace:
    return SimpleNamespace(provider="local", model="stub-model", base_url=base_url)


@pytest.mark.asyncio
async def test_a_self_hosted_endpoint_without_a_key_still_names_the_session(monkeypatch) -> None:
    asked: dict = {}

    async def fake_autoname(**kwargs) -> None:
        asked.update(kwargs)

    monkeypatch.setattr(session_naming, "resolved_api_key", lambda backend: None)
    monkeypatch.setattr(session_naming, "_provider_base_url", lambda backend: backend.base_url)
    monkeypatch.setattr(session_naming, "_autoname", fake_autoname)
    task = await session_naming.schedule_session_autoname(
        user_id="u", project_id="p", session_id="s", backend=_backend("http://127.0.0.1:18495/v1"))
    assert task is not None, "不鉴权的自建端点没有起命名任务"
    await asyncio.wait_for(task, timeout=5)
    assert asked["api_key"] == "" and asked["base_url"] == "http://127.0.0.1:18495/v1"


@pytest.mark.asyncio
async def test_a_hosted_endpoint_without_a_key_keeps_the_machine_title(monkeypatch) -> None:
    monkeypatch.setattr(session_naming, "resolved_api_key", lambda backend: None)
    monkeypatch.setattr(session_naming, "_provider_base_url", lambda backend: "https://api.deepseek.com")
    task = await session_naming.schedule_session_autoname(
        user_id="u", project_id="p", session_id="s", backend=_backend(None))
    assert task is None


@pytest.mark.asyncio
async def test_no_key_means_nothing_to_redact(monkeypatch, tmp_path) -> None:
    """没有 key 时传给子进程桥的 secret 是 None，不是空串。"""
    from app.config import settings

    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "llm.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "harness_root", str(tmp_path))
    seen: dict = {}

    async def fake_ask_once(request, **kwargs):
        seen.update(kwargs)
        return {"title": "A title"}

    monkeypatch.setattr(session_naming.harness_bridge_once, "ask_once", fake_ask_once)
    title = await session_naming._ask_harness_for_title(
        user_id="u", project_id="p", message="hello", api_key="", base_url="http://127.0.0.1:1/v1",
        model="stub-model")
    assert title == "A title"
    assert seen["secret"] is None, f"没有 key 却把 {seen['secret']!r} 当成 secret 去打码"
    assert seen["child_env"]["LLM_API_KEY"] == ""
