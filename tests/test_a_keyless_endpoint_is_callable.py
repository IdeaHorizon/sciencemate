"""自建端点不鉴权时，"没有 api_key"不是"这个角色不可用"。

现场（2026-09-15，yuankk）：`http://10.128.7.30:8000/v1` 的 vLLM 不鉴权 ——
从同一张网实测，`/v1/models` 带不带 key 都是 200 + JSON。而这一层此前把
`api_key` 写进了"能不能开口"的闸里，于是一台跑得好好的服务器在平台上
永远建不出可用连接。

端点要不要鉴权由**端点自己**回答：它回 401，那条路已经有正确的归属
（upstream_rejected，「模型服务拒绝了这次调用」）。框架不替它猜。
"""
from __future__ import annotations

import json

import pytest

from core import llm as llm_mod
from core.llm import LLMClient, LLMMessage


class _Recorder:
    """记下每次请求的 headers；流式一律 4xx，逼它回落到非流式那条路。"""

    seen: list[dict] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        type(self).seen.append(dict(kw.get("headers") or {}))
        raise AssertionError("这组用例只走非流式")


def _reply() -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 1}}


@pytest.fixture(autouse=True)
def _no_stream(monkeypatch):
    # 流式与非流式各带一次头；这组用例只问"头长什么样"，走哪条传输不影响结论。
    monkeypatch.setenv("LLM_STREAM", "0")
    monkeypatch.setenv("HARNESS_LLM_CACHE", "off")
    _Recorder.seen = []


def _record_posts(monkeypatch) -> list[dict]:
    posted: list[dict] = []

    class _Client(_Recorder):
        async def post(self, url, headers=None, json=None):  # noqa: A002
            posted.append(dict(headers or {}))
            return _Resp()

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return _reply()

        @property
        def is_success(self):
            return True

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
    return posted


@pytest.mark.asyncio
async def test_an_empty_key_sends_no_authorization_header_at_all(monkeypatch):
    """不是发 `Bearer `（空值）—— 那在一部分网关上会被判成一把坏 key，
    回一个指着假因的 401。缺席就让它缺席。"""
    posted = _record_posts(monkeypatch)

    reply = await LLMClient(api_key="", model="qwen3.8-27b", base_url="http://10.128.7.30:8000",
                            max_retries=0).chat([LLMMessage(role="user", content="?")])

    assert reply.content == "ok", "没有 key 也要真的把请求发出去"
    assert posted == [{}], f"不该带 Authorization 头：{posted}"


@pytest.mark.asyncio
async def test_a_key_is_still_sent_when_there_is_one(monkeypatch):
    posted = _record_posts(monkeypatch)

    await LLMClient(api_key="sk-real", model="m", base_url="http://x",
                    max_retries=0).chat([LLMMessage(role="user", content="?")])

    assert posted == [{"Authorization": "Bearer sk-real"}]


@pytest.mark.asyncio
async def test_the_role_is_still_unavailable_without_an_endpoint(monkeypatch):
    """闸没有被拆掉，只是不再拿 api_key 说事：够不到端点仍然是"这个角色不可用"。"""
    from core import model_roles

    _record_posts(monkeypatch)
    with pytest.raises(model_roles.ModelRoleUnavailable):
        await LLMClient(api_key="sk-real", model="m", base_url="",
                        max_retries=0).chat([LLMMessage(role="user", content="?")])


def test_no_call_site_builds_the_authorization_header_by_hand():
    """两个出口（流式 / 非流式）都得走 `_request_headers`。

    判据扫的是**这件事**而不是某两行：新加一个直调点时，手搓一份
    `Bearer {self.api_key}` 会在没有 key 的端点上重新制造那个指着假因的 401，
    而且不会有任何东西报错。
    """
    from pathlib import Path

    source = Path(llm_mod.__file__).read_text(encoding="utf-8")
    handwritten = [
        line.strip() for line in source.splitlines()
        if "Bearer" in line and "self.api_key" in line and "_request_headers" not in line
    ]
    # 唯一合法的一处在 `_request_headers` 自己体内。
    assert len(handwritten) == 1, f"手搓 Authorization 头的地方：{handwritten}"
    assert source.index(handwritten[0]) > source.index("def _request_headers")
    assert source.count("headers=self._request_headers()") >= 2
