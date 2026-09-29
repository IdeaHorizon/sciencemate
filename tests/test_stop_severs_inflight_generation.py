"""停止必须立刻切断**在飞**的生成 —— 不等对端吐字，不等 backoff 睡完。

现场（2026-08-20，积算 vLLM 网关，会话 a0381b7d）：turn 4 的生成从 10:12 挂到
10:25，期间用户连按 10 次停止全部送达（kill_signal 已写入、transcript 有
user_stop_received×10），生成纹丝不动 —— 旧实现把中止检查挂在"收到一行之后"，
上游停止吐数据时检查永远轮不到，最后是 worker 自己死掉才算完。

判据：停止时延的上界由**我们的轮询节拍**（_GENERATION_ABORT_POLL_S=0.5s）
决定，不由对端的吐字节奏决定。三条在飞路径都要能切断：
①流式读取挂死（零字节）②非流式请求在途 ③传输重试的 backoff 睡眠中。
不联网。
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from core import cancellation
from core import llm as llm_mod
from core.cancellation import RunCancelled
from core.llm import LLMClient, LLMMessage, set_stream_abort_check
from core.state import State

#: 判"立刻"的上界：信号落下后 0.5s 节拍 + 调度余量。真实上界是
#: _GENERATION_ABORT_POLL_S，放宽到 3s 只为吸收 CI 机器的调度抖动。
_PROMPT_SECONDS = 3.0


class _HungStreamCM:
    """一个病态上游：连接建立成功，然后永远不吐一个字节。

    2026-08-20 复放实测的真实形态（积算网关 300s 零字节读超时）。
    """

    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def aiter_lines(self):
        await asyncio.Event().wait()  # 永不 set —— 只能靠取消解开
        yield ""  # pragma: no cover —— 永远到不了；只为标记这是生成器


class _HungClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def stream(self, method, url, **kw):
        return _HungStreamCM()

    async def post(self, url, **kw):
        await asyncio.Event().wait()


def _client() -> LLMClient:
    return LLMClient(api_key="k", model="m", base_url="http://x", max_retries=0)


def _state() -> State:
    return State.new(node_type="literature", base_dir=Path(tempfile.mkdtemp()))


@pytest.fixture(autouse=True)
def _clean_abort_check():
    set_stream_abort_check(None)
    yield
    set_stream_abort_check(None)


@pytest.mark.asyncio
async def test_hung_stream_severed_by_kill_signal(monkeypatch):
    """流式读取挂死（零字节）+ kill_signal → 生成在节拍上界内被切断。

    这正是平台停止按钮的路径：没有任何 set_stream_abort_check 注册，
    只有绑定 run 的 hook_state['kill_signal']。
    """
    monkeypatch.setenv("LLM_STREAM", "1")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _HungClient)
    state = _state()

    async def _run():
        with cancellation.bind_run(state):
            return await _client().chat([LLMMessage(role="user", content="?")])

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.3)
    assert not task.done(), "信号未发前生成不该自己结束"
    state.hook_state["kill_signal"] = {"reason": "stop", "requested_by": "user"}
    t0 = time.monotonic()
    resp = await asyncio.wait_for(task, timeout=_PROMPT_SECONDS + 2)
    assert time.monotonic() - t0 < _PROMPT_SECONDS, \
        "停止时延必须由轮询节拍决定，不由对端吐字节奏决定"
    assert resp.content is None
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_hung_stream_severed_by_panic_callback(monkeypatch):
    """同一挂死流，CLI panic 回调路径（set_stream_abort_check）也要能切断。"""
    monkeypatch.setenv("LLM_STREAM", "1")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _HungClient)
    flag = {"stop": False}
    set_stream_abort_check(lambda: flag["stop"])

    task = asyncio.ensure_future(
        _client().chat([LLMMessage(role="user", content="?")]))
    await asyncio.sleep(0.3)
    flag["stop"] = True
    t0 = time.monotonic()
    resp = await asyncio.wait_for(task, timeout=_PROMPT_SECONDS + 2)
    assert time.monotonic() - t0 < _PROMPT_SECONDS
    assert resp.content is None


@pytest.mark.asyncio
async def test_hung_stream_aborted_by_data_idle_timeout(monkeypatch):
    """流只连不吐（或只发 keepalive）、**没有任何停止信号** → 靠数据空闲超时
    中止，抛可重试 ReadTimeout（落进 _stream_with_retry），不再永远挂 'running'。

    E2E v35 现场：ZJU GPUStack v4-pro 的 reviewer 生成卡死，网关周期性发 SSE
    keepalive 把 httpx 的 read timeout 一次次重置，连接活着却永不吐 data →
    读循环空转 22min、run 挂 running、token 冻住。判据：距上次真数据超过
    timeout 就中止。变异（去掉 last_data_at 空闲检查）→ chat 永挂 → wait_for
    6s 超时 → elapsed 断言转红。
    """
    monkeypatch.setenv("LLM_STREAM", "1")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _HungClient)
    # timeout=1.0：数据空闲 1s 即判卡死（测试里没有真数据会到来）
    client = LLMClient(api_key="k", model="m", base_url="http://x",
                       max_retries=0, timeout=1.0)
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        await asyncio.wait_for(
            client.chat([LLMMessage(role="user", content="?")]), timeout=6.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 4.0, (
        f"应在数据空闲超时(1s)+节拍内中止，实际 {elapsed:.1f}s —— 疑似仍挂死")
    # 中止是超时类（ReadTimeout / 其包装），不是别的偶然异常
    blob = f"{type(ei.value).__name__}: {ei.value}".lower()
    assert "timeout" in blob or "idle" in blob, blob


@pytest.mark.asyncio
async def test_inflight_nonstream_post_severed_by_kill_signal(monkeypatch):
    """非流式请求在途时收到 kill_signal → 取消请求、以 RunCancelled 收口。"""
    monkeypatch.setenv("LLM_STREAM", "0")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _HungClient)

    @asynccontextmanager
    async def _null_slot(*a, **kw):
        yield

    monkeypatch.setattr("core.llm_admission.llm_slot", _null_slot)
    state = _state()

    async def _run():
        with cancellation.bind_run(state):
            return await _client().chat([LLMMessage(role="user", content="?")])

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.3)
    state.hook_state["kill_signal"] = {"reason": "stop", "requested_by": "user"}
    t0 = time.monotonic()
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(task, timeout=_PROMPT_SECONDS + 2)
    assert time.monotonic() - t0 < _PROMPT_SECONDS


@pytest.mark.asyncio
async def test_retry_backoff_sleep_is_abortable():
    """几十秒的 backoff 睡眠不许把停止拖到睡完：睡眠中命中信号立刻收口。"""
    from core.llm import _sleep_or_abort

    state = _state()
    with cancellation.bind_run(state):
        state.hook_state["kill_signal"] = {"reason": "stop"}
        t0 = time.monotonic()
        with pytest.raises(RunCancelled):
            await _sleep_or_abort(30.0, "test_backoff")
        assert time.monotonic() - t0 < _PROMPT_SECONDS


@pytest.mark.asyncio
async def test_healthy_stream_unaffected(monkeypatch):
    """没有停止信号时，赛跑改造不得改变正常流的行为（回归守卫）。"""
    import json as _json

    monkeypatch.setenv("LLM_STREAM", "1")

    def _sse(delta, finish=None):
        return "data: " + _json.dumps(
            {"choices": [{"delta": delta, "finish_reason": finish}]},
            ensure_ascii=False)

    class _OkStreamCM(_HungStreamCM):
        async def aiter_lines(self):
            for line in [
                _sse({"role": "assistant", "content": ""}),
                _sse({"content": "正常"}),
                _sse({"content": "回复"}),
                _sse({}, finish="stop"),
                "data: [DONE]",
            ]:
                yield line

    class _OkClient(_HungClient):
        def stream(self, method, url, **kw):
            return _OkStreamCM()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _OkClient)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert resp.content == "正常回复"
    assert resp.finish_reason == "stop"
