"""LLM cache 单测 + LLMClient 集成（不打真 LLM；mock httpx）。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import llm_cache
from core.llm import LLMClient, LLMMessage


def test_cache_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_LLM_CACHE", raising=False)
    assert llm_cache.should_cache(0) is False
    assert llm_cache.get("any") is None


def test_should_cache_only_at_temp_zero(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    assert llm_cache.should_cache(0.0) is True
    assert llm_cache.should_cache(0.7) is False
    assert llm_cache.should_cache(0.01) is False


def test_cache_key_stable():
    msg = [LLMMessage(role="user", content="hello")]
    k1 = llm_cache.cache_key(model="x", temperature=0, messages=msg, tools=None)
    k2 = llm_cache.cache_key(model="x", temperature=0, messages=msg, tools=None)
    assert k1 == k2


def test_cache_key_changes_on_input(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    m1 = [LLMMessage(role="user", content="hello")]
    m2 = [LLMMessage(role="user", content="goodbye")]
    assert (llm_cache.cache_key(model="x", temperature=0, messages=m1)
            != llm_cache.cache_key(model="x", temperature=0, messages=m2))
    # 模型不同也不同
    assert (llm_cache.cache_key(model="A", temperature=0, messages=m1)
            != llm_cache.cache_key(model="B", temperature=0, messages=m1))


def test_put_get_roundtrip(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    payload = {"content": "ok", "tool_calls": [], "finish_reason": "stop", "usage": {}}
    llm_cache.put("test_key_1", payload)
    got = llm_cache.get("test_key_1")
    assert got == payload


def test_clear(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    llm_cache.put("k1", {"x": 1})
    llm_cache.put("k2", {"x": 2})
    n = llm_cache.clear()
    assert n >= 2
    assert llm_cache.get("k1") is None


def test_stats(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    llm_cache.put("k1", {"x": 1})
    s = llm_cache.stats()
    assert s["count"] >= 1
    assert s["enabled"] is True


# ─── LLMClient 集成测：mock httpx，验证 cache hit 不发请求 ────────────────

@pytest.mark.asyncio
async def test_llm_client_caches_at_temp_zero(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_BASE_URL", "https://fake.example")
    monkeypatch.setenv("LLM_MODEL", "fake-model")

    # mock httpx response
    fake_resp = {
        "choices": [{"message": {"content": "hi", "tool_calls": None},
                      "finish_reason": "stop"}],
        "usage": {"total_tokens": 5},
    }
    mock_post = AsyncMock(return_value=MagicMock(
        json=MagicMock(return_value=fake_resp),
        raise_for_status=MagicMock(),
        status_code=200,
    ))
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post

        client = LLMClient()
        msgs = [LLMMessage(role="user", content="test")]

        # First call: real (mocked) HTTP
        r1 = await client.chat(msgs, temperature=0)
        assert r1.content == "hi"
        assert mock_post.call_count == 1

        # Second call: cache hit, no HTTP
        r2 = await client.chat(msgs, temperature=0)
        assert r2.content == "hi"
        assert mock_post.call_count == 1  # 没增加


@pytest.mark.asyncio
async def test_llm_client_no_cache_at_temp_nonzero(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_BASE_URL", "https://fake.example")
    monkeypatch.setenv("LLM_MODEL", "fake-model")

    fake_resp = {
        "choices": [{"message": {"content": "hi", "tool_calls": None},
                      "finish_reason": "stop"}],
        "usage": {},
    }
    mock_post = AsyncMock(return_value=MagicMock(
        json=MagicMock(return_value=fake_resp),
        raise_for_status=MagicMock(),
        status_code=200,
    ))
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post

        client = LLMClient()
        msgs = [LLMMessage(role="user", content="test2")]

        await client.chat(msgs, temperature=0.7)
        await client.chat(msgs, temperature=0.7)
        # Temperature > 0 → 不缓存，两次都打 HTTP
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_llm_client_no_cache_flag_bypasses(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_BASE_URL", "https://fake.example")
    monkeypatch.setenv("LLM_MODEL", "fake-model")

    fake_resp = {
        "choices": [{"message": {"content": "x", "tool_calls": None},
                      "finish_reason": "stop"}],
        "usage": {},
    }
    mock_post = AsyncMock(return_value=MagicMock(
        json=MagicMock(return_value=fake_resp),
        raise_for_status=MagicMock(),
        status_code=200,
    ))
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = mock_post

        client = LLMClient()
        msgs = [LLMMessage(role="user", content="bypass")]
        await client.chat(msgs, temperature=0, _no_cache=True)
        await client.chat(msgs, temperature=0, _no_cache=True)
        assert mock_post.call_count == 2
