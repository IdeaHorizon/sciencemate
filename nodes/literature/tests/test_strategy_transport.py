import asyncio
import json
import time

import httpx
import pytest

from nodes.literature.tools.strategy_transport import request_completion


def event(delta=None, finish=None, usage=None):
    payload = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload) + "\n\n").encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for delay, chunk in self.chunks:
            await asyncio.sleep(delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def install(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


async def run(diag, *, idle=.1, total=1):
    return await request_completion("https://model.invalid/chat/completions", {},
                                    {"messages": [{"role": "system", "content": "plan"},
                                                  {"role": "user", "content": "question"}]},
                                    diag, read_timeout=idle, total_timeout=total)


@pytest.mark.asyncio
async def test_active_generation_can_exceed_idle_timeout_and_never_uses_reasoning_as_content(monkeypatch):
    chunks = Chunks([(.04, event({"reasoning": "private reasoning"})),
                     (.04, event({"reasoning_content": "another private step"})),
                     (.04, event({"content": '{"core":'})),
                     (.04, event({"content": '"bridge"}'}, "stop")),
                     (0, event(usage={"completion_tokens": 42})), (0, b"data: [DONE]\n\n")])
    def handle(request):
        body = json.loads(request.content)
        assert body["stream"] and len(body["messages"]) == 2
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks)
    install(monkeypatch, handle)
    diag = {}; started = time.monotonic()
    result = await run(diag)
    assert time.monotonic()-started > .1
    assert result["choices"][0]["message"]["content"] == '{"core":"bridge"}'
    assert result["usage"]["completion_tokens"] == 42
    assert diag["stream_complete"] and diag["reasoning_chars"] > 0 and chunks.closed
    assert "private" not in json.dumps(result) and "private" not in json.dumps(diag)


@pytest.mark.asyncio
async def test_keepalive_only_stream_times_out_and_closes(monkeypatch):
    chunks = Chunks([(.01, b": ping\n\n") for _ in range(30)])
    install(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks))
    diag = {}
    with pytest.raises(httpx.ReadTimeout):
        await run(diag, idle=.05)
    assert diag["timeout_phase"] == "stream_idle" and chunks.closed


@pytest.mark.asyncio
async def test_total_deadline_cancels_even_continuously_active_stream(monkeypatch):
    chunks = Chunks([(.01, event({"reasoning": "progress"})) for _ in range(30)])
    install(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks))
    diag = {}
    with pytest.raises(httpx.ReadTimeout):
        await run(diag, idle=.1, total=.05)
    assert diag["timeout_phase"] == "request_deadline" and chunks.closed


@pytest.mark.asyncio
async def test_total_deadline_also_covers_waiting_for_response_headers(monkeypatch):
    cancelled = []
    async def handle(request):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={})
    install(monkeypatch, handle)
    diag = {}
    with pytest.raises(httpx.ReadTimeout):
        await run(diag, total=.03)
    assert cancelled and diag["timeout_phase"] == "request_deadline"
    assert "http_status" not in diag


@pytest.mark.asyncio
async def test_disconnect_cannot_publish_partial_json_as_success(monkeypatch):
    chunks = Chunks([(0, event({"content": '{"core":'}))])
    install(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks))
    with pytest.raises(httpx.ReadError, match="before completion"):
        await run({})
    assert chunks.closed


@pytest.mark.asyncio
async def test_truncation_finish_reason_and_usage_are_preserved_for_recovery(monkeypatch):
    chunks = Chunks([(0, event({"reasoning": "unpublished"}, "length", {"completion_tokens": 8192})),
                     (0, b"data: [DONE]\n\n")])
    install(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks))
    result = await run({})
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["choices"][0]["message"]["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429])
async def test_access_and_rate_limits_do_not_trigger_transport_fallback(monkeypatch, status):
    calls = []
    def handle(request):
        calls.append(1)
        return httpx.Response(status, json={"error": "stream request refused"})
    install(monkeypatch, handle)
    with pytest.raises(httpx.HTTPStatusError):
        await run({})
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('unsupported', ['stream_options', 'stream'])
async def test_compatible_gateway_fallback_is_bounded(monkeypatch, unsupported):
    bodies = []
    def handle(request):
        body = json.loads(request.content); bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": f"unsupported {unsupported}"})
        assert unsupported not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "valid"}, "finish_reason": "stop"}]})
    install(monkeypatch, handle)
    assert (await run({}))["choices"][0]["message"]["content"] == "valid"
    assert len(bodies) == 2
