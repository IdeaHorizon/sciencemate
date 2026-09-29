"""LLMClient transient-error retry。

v2.x：dogfood 实测同事 long-run 被 LLM provider 偶发 httpx.ConnectError /
read timeout / 5xx 干死所有工作。LLMClient 加 retry 兜底。

不联网 —— 用 monkeypatch 替换 httpx.AsyncClient.post 模拟各种错。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.llm import LLMClient, LLMMessage, _compute_backoff


# ── _compute_backoff 数学 ───────────────────────────────────────────────────

def test_backoff_exponential_with_jitter():
    """attempt N → 大致 base × 2^N 区间。

    ⚠️ 2026-08-10 起 5xx 的起步从 1s 提到 5s（调用方传更小的 base 也会被抬到
    这个地板）。原因不是"想慢一点"：5xx 的真实成因是服务端节点重启 / 模型
    重载，量级是几十秒到几分钟，而 1/2/4 总共只扛 7 秒 —— 一轮跑了 3 小时、
    做完 3 个真实 LAMMPS 模拟的 E2E 就死在这 7 秒上。
    改回小值之前先看 tests/test_transient_outage_does_not_kill_a_long_run.py。
    """
    base = 5.0
    delays = [_compute_backoff(i, base, None) for i in range(5)]
    assert 5.0 <= delays[0] <= 7.5
    assert 10.0 <= delays[1] <= 12.5
    assert 20.0 <= delays[2] <= 22.5


def test_backoff_capped_at_max():
    """attempt 大到指数爆 → 不超过上限（5xx 现在是 60s，见上条注释）。"""
    delay = _compute_backoff(10, 1.0, None)
    assert delay == 60.0


def test_backoff_uses_retry_after_header():
    """带 Retry-After → 优先用 header 值，仍受上限约束。

    "听对方的"这条不变；变的只是上限（5xx 30s → 60s）。对方说等 120 秒，
    我们 30 秒就冲上去，等于没听 —— 这条判据本身是对的。
    """
    assert _compute_backoff(0, 1.0, "5") == 5.0
    assert _compute_backoff(0, 1.0, "120") == 60.0


def test_backoff_invalid_retry_after_falls_back():
    """Retry-After 不合法 → 回退到指数退避（5xx 起步 5s）。"""
    delay = _compute_backoff(0, 1.0, "not-a-number")
    assert 5.0 <= delay <= 7.5


# ── retry 行为 ───────────────────────────────────────────────────────────────

def _make_response(status: int, body: str = "{}",
                    headers: dict | None = None) -> MagicMock:
    """Mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.headers = headers or {}
    resp.json.return_value = {
        "choices": [{"message": {"content": "ok", "tool_calls": []},
                       "finish_reason": "stop"}],
        "usage": {},
    }
    return resp


@pytest.mark.asyncio
async def test_retry_on_connect_error_then_success(monkeypatch):
    """httpx.ConnectError 重试到成功。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise httpx.ConnectError("network blip")
            return _make_response(200)

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())  # 跳退避

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=3, retry_backoff_base=0.01)
    resp = await c.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert call_count[0] == 3   # 2 次失败 + 1 次成功


@pytest.mark.asyncio
async def test_retry_on_read_error_then_success(monkeypatch):
    """httpx.ReadError（provider 读响应阶段断流）重试到成功（issue #105）。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise httpx.ReadError("peer closed connection mid-response")
            return _make_response(200)

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=3, retry_backoff_base=0.01)
    resp = await c.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_retry_on_read_error_exhausted_raises(monkeypatch):
    """httpx.ReadError 反复发生、重试用完 → 抛原异常（不是吞掉变成别的错）。"""
    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            raise httpx.ReadError("peer closed connection mid-response")

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=2, retry_backoff_base=0.01)
    with pytest.raises(httpx.ReadError, match="peer closed connection mid-response"):
        await c.chat([LLMMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_retry_on_503_then_success(monkeypatch):
    """HTTP 503 重试到成功。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(503, "Server overloaded")
            return _make_response(200)

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=3, retry_backoff_base=0.01)
    resp = await c.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_no_retry_on_4xx_except_429(monkeypatch):
    """HTTP 401 / 400 等 client error 不重试（重试白费）。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            return _make_response(401, "Invalid API key")

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=5, retry_backoff_base=0.01)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await c.chat([LLMMessage(role="user", content="hi")])
    assert call_count[0] == 1   # 直接挂，不重试


@pytest.mark.asyncio
async def test_retry_on_429_uses_retry_after_header(monkeypatch):
    """HTTP 429 重试且尊重 Retry-After header。"""
    call_count = [0]
    sleep_calls = []

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(429, "Rate limited",
                                       headers={"retry-after": "2"})
            return _make_response(200)

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)

    async def fake_sleep(s):
        sleep_calls.append(s)
    monkeypatch.setattr("core.llm.asyncio.sleep", fake_sleep)

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=3, retry_backoff_base=0.01)
    resp = await c.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert call_count[0] == 2
    assert sleep_calls == [2.0]   # 用了 Retry-After 而不是指数退避


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_error(monkeypatch):
    """所有 retry 用完仍失败 → 抛最后的异常。"""
    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            raise httpx.ConnectError("network down")

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=2, retry_backoff_base=0.01)
    with pytest.raises(httpx.ConnectError, match="network down"):
        await c.chat([LLMMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_max_retries_zero_disables_retry(monkeypatch):
    """max_retries=0 → 旧行为（崩了直接挂，0 重试）。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            raise httpx.ConnectError("blip")

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    sleep_called = [0]
    async def fake_sleep(s): sleep_called[0] += 1
    monkeypatch.setattr("core.llm.asyncio.sleep", fake_sleep)

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=0, retry_backoff_base=0.01)
    with pytest.raises(httpx.ConnectError):
        await c.chat([LLMMessage(role="user", content="hi")])
    assert call_count[0] == 1   # 只 1 次
    assert sleep_called[0] == 0   # 没 sleep


@pytest.mark.asyncio
async def test_per_call_max_retries_overrides_client_default(monkeypatch):
    """chat(max_retries=N) 覆盖 client 默认 self.max_retries。"""
    call_count = [0]

    class _MockClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            call_count[0] += 1
            raise httpx.ConnectError("blip")

    monkeypatch.setattr("core.llm.httpx.AsyncClient", _MockClient)
    monkeypatch.setattr("core.llm.asyncio.sleep", AsyncMock())

    c = LLMClient(api_key="k", model="m", base_url="http://x",
                  max_retries=10, retry_backoff_base=0.01)
    with pytest.raises(httpx.ConnectError):
        await c.chat([LLMMessage(role="user", content="hi")], max_retries=1)
    assert call_count[0] == 2   # 1 + 1 retry，不是 11


# ── env var 默认 ─────────────────────────────────────────────────────────────

def test_env_var_default(monkeypatch):
    """无构造参数时读 env var 默认。"""
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE", "2.5")
    c = LLMClient()
    assert c.max_retries == 7
    assert c.retry_backoff_base == 2.5


def test_env_var_invalid_falls_back_to_default(monkeypatch):
    """LLM_MAX_RETRIES 不是 int → 用默认值不挂。

    默认从 3 提到 5：新阶梯下 5 次约扛 140 秒，够撑过一次后端重启。
    """
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_MAX_RETRIES", "garbage")
    c = LLMClient()
    assert c.max_retries == 5
