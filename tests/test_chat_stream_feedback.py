"""issue #166 之三：「和模型说话第一时间模型没反馈」+ 两个串流 bug。

主诉的根因不是 `⏳ 处理中…` 打晚了（位置本来就对），而是打完之后到首个正文
token 之间**全静默**：

  - core/llm.py 把 reasoning 增量整体丢弃 → deepseek-v4-pro 这类推理模型思考
    几十秒期间屏幕全黑；
  - <think> 泄漏进 content 时闭合前一律返回 ""，同样是黑箱；
  - holdback 恒压 24 字，正文再长也慢一截；
  - 非 tty（管道 / tee）下进度提示不 flush → 全堆缓冲区，跑完才一次性吐出。

外加两个"后台任务的 token 串到用户屏幕"的 bug：dreaming 和 summarizer 都复用了
主 orchestrator 那个挂了 stream_display 的 llm 实例。

本文件只测机制，不联网。
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

from core.llm import LLMClient, StreamSanitizer

# ── reasoning 可见：思考阶段不再是黑箱 ──────────────────────────────────────

def _sink():
    got: list[str] = []
    return got, got.append


def test_think_leak_goes_to_reasoning_channel_not_content():
    """<think> 泄漏进 content：正文通道一个字都不能漏，但思维链要透给 reasoning
    通道（原来是直接丢弃 → 屏幕全黑）。"""
    got, sink = _sink()
    s = StreamSanitizer(reasoning_sink=sink)
    content = "".join(s.feed(d) for d in
                      ["<think>我先", "想想这题", "</think>", "答案是 42。"]) + s.flush()
    assert content == "答案是 42。"
    assert "".join(got) == "我先想想这题"


def test_unclosed_think_still_streams_reasoning():
    """think 段没闭合（流被截断）也要边收边透 —— 否则长思考期间依旧全黑。"""
    got, sink = _sink()
    s = StreamSanitizer(reasoning_sink=sink)
    s.feed("<think>第一步")
    assert "".join(got) == "第一步", "think 段内文本应实时透出，不能等闭合"
    s.feed("第二步")
    s.flush()
    assert "".join(got) == "第一步第二步"


def test_orphan_close_think_routed_to_reasoning_not_content():
    """孤立 </think>（模型没开标签直接吐思维链）：之前的文本仍不许进正文，
    但改投 reasoning 通道而不是凭空丢掉。"""
    got, sink = _sink()
    s = StreamSanitizer(reasoning_sink=sink)
    content = "".join(s.feed(d) for d in ["先想想", "</think>", "答案"]) + s.flush()
    assert content == "答案"
    assert "".join(got) == "先想想"


def test_reasoning_sink_optional_behaviour_unchanged():
    """不给 sink 时行为与改造前完全一致（向后兼容）。"""
    s = StreamSanitizer()
    assert "".join(s.feed(d) for d in
                   ["<think>r", "</think>", "正文"]) + s.flush() == "正文"


# ── holdback：既不泄漏半截 markup，也不无谓压住正文 ─────────────────────────

def test_holdback_releases_once_past_orphan_window():
    """过了流开头的孤立-</think> 检测窗口后，尾窗里没有标记起始字符就不该再压
    住正文（原来恒压 24 字）。"""
    s = StreamSanitizer()
    long_head = "正" * (StreamSanitizer._ORPHAN_WINDOW + 10)
    s.feed(long_head)
    out = s.feed("这段应当立刻可见")
    assert out.endswith("这段应当立刻可见"), f"过窗后仍被压住：{out!r}"


def test_holdback_still_guards_partial_control_token():
    """半截控制标记永远不许上屏 —— 即使已经过了开头窗口。"""
    s = StreamSanitizer()
    s.feed("正" * (StreamSanitizer._ORPHAN_WINDOW + 10))
    assert "<|im_" not in s.feed("<|im_")
    assert "<|im_" not in s.feed("end|>")
    assert "<|im_end|>" not in s.flush()


def test_holdback_guards_orphan_think_at_stream_start():
    """流最开头的短思维链泄漏仍被窗口挡住（这条保护不能因为提速被削掉）。"""
    got, sink = _sink()
    s = StreamSanitizer(reasoning_sink=sink)
    assert s.feed("偷偷的思维链") == ""
    s.feed("</think>")
    assert "".join(got) == "偷偷的思维链"


# ── LLMClient：reasoning 通道真的被 _consume_stream 调用 ────────────────────

def _sse(delta: dict, finish: str | None = None) -> str:
    import json
    return "data: " + json.dumps(
        {"choices": [{"delta": delta, "finish_reason": finish}]}, ensure_ascii=False)


class _FakeStreamCM:
    def __init__(self, lines):
        self._lines, self.status_code = lines, 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"{}"


class _FakeClient:
    lines: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def stream(self, method, url, **kw):
        return _FakeStreamCM(type(self).lines)


def _client() -> LLMClient:
    return LLMClient(api_key="k", model="m", base_url="http://x", max_retries=0)


@pytest.mark.asyncio
async def test_stream_reasoning_display_receives_deltas(monkeypatch):
    """reasoning_content 增量必须实时回调显示层，并在正文开始前收束（None）。"""
    from core import llm as llm_mod
    monkeypatch.setenv("LLM_STREAM", "1")
    _FakeClient.lines = [
        _sse({"reasoning_content": "先分析"}),
        _sse({"reasoning_content": "再验算"}),
        _sse({"content": "答案是 42。"}),
        _sse({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)

    events: list[str | None] = []
    content: list[str | None] = []
    c = _client()
    c.stream_reasoning_display = events.append
    c.stream_display = content.append
    resp = await c.chat([llm_mod.LLMMessage(role="user", content="?")])

    assert "".join(e for e in events if e) == "先分析再验算"
    assert events[-1] is None, "推理段必须收束，否则指示器那半行会跟正文挤一起"
    assert resp.content == "答案是 42。"
    # 收束发生在正文第一个字上屏之前
    assert events.index(None) >= 0 and content, "正文仍应正常流出"


@pytest.mark.asyncio
async def test_reasoning_channel_optional(monkeypatch):
    """没设 stream_reasoning_display 时一切照旧（非流式调用方 / 老代码不受影响）。"""
    from core import llm as llm_mod
    monkeypatch.setenv("LLM_STREAM", "1")
    _FakeClient.lines = [
        _sse({"reasoning_content": "想"}),
        _sse({"content": "答"}),
        _sse({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    resp = await _client().chat([llm_mod.LLMMessage(role="user", content="?")])
    assert resp.content == "答"
    assert resp.reasoning_content == "想"


# ── 独立 llm 实例：后台任务的 token 不许串到用户屏幕 ────────────────────────

def test_spawn_silent_drops_display_callbacks_and_keeps_config():
    c = _client()
    c.stream_display = lambda d: None
    c.stream_reasoning_display = lambda d: None
    twin = c.spawn_silent()
    assert twin is not c
    assert twin.stream_display is None
    assert twin.stream_reasoning_display is None
    for attr in ("api_key", "model", "base_url", "timeout", "max_retries"):
        assert getattr(twin, attr) == getattr(c, attr)
    # 反向：改 twin 不该动到主实例
    twin.stream_display = lambda d: None
    assert c.stream_display is not None


@pytest.mark.asyncio
async def test_summarizer_compression_does_not_stream_to_user(monkeypatch):
    """summarizer 压缩必须走独立实例 —— 否则压缩摘要顶着 🔬 Orchestrator header
    流到用户屏幕，还吃掉本轮只打一次的 header_shown（真正的回复反而没 header）。

    走真实的 _strategy_llm 路径，不是只测 spawn_silent 本身。
    """
    from pathlib import Path

    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMMessage, LLMResponse
    from core.state import State
    from core.summarizer import SummarizerContext, _strategy_llm

    seen: list[str] = []          # "流到用户屏幕上的东西"
    used: list[LLMClient] = []

    async def _fake_chat(self, messages, **kw):
        used.append(self)
        if self.stream_display:                 # 模拟流式逐 token 回调
            self.stream_display("压缩后的摘要")
        return LLMResponse(content="压缩后的摘要", tool_calls=[],
                           finish_reason="stop", usage={"total_tokens": 10})

    monkeypatch.setattr(LLMClient, "chat", _fake_chat, raising=True)

    main_llm = _client()                        # 主 orchestrator 实例：挂了显示回调
    main_llm.stream_display = seen.append

    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(enabled=True, strategy="llm", keep_last_n_turns=1),
    )
    msgs = [LLMMessage(role="system", content="s")] + [
        LLMMessage(role="user" if i % 2 == 0 else "assistant", content=f"第 {i} 条 " * 40)
        for i in range(12)
    ]
    ctx = SummarizerContext(
        harness=harness, state=State(run_id="r1", node_type="test", root=Path("/tmp")),
        messages=msgs, estimated_tokens=99999, llm=main_llm,   # type: ignore[arg-type]
    )
    await _strategy_llm(ctx)

    assert used, "压缩没有真的调 LLM"
    assert used[0] is not main_llm, "压缩复用了挂着 stream_display 的主实例"
    assert used[0].stream_display is None
    assert seen == [], f"后台压缩的 token 串到了用户屏幕：{seen}"


def test_chat_uses_silent_llm_for_background_and_summarizer():
    """两处调用点确实换成了独立实例（防回归：改回复用主实例就该红）。"""
    import inspect

    import chat as chat_mod
    from core import summarizer as sum_mod

    src = inspect.getsource(chat_mod._run_dreaming_background)
    assert "llm=_silent_llm(llm)" in src, "dreaming 又把主 llm 实例传给子节点了"

    sum_src = inspect.getsource(sum_mod)
    assert "compress_llm.chat(" in sum_src, "summarizer 又回去用 ctx.llm 了"


def test_silent_llm_tolerates_fake_llm_without_spawn():
    import chat as chat_mod

    class _Fake:
        pass

    fake = _Fake()
    assert chat_mod._silent_llm(fake) is fake


# ── 非 tty：必须 flush，否则管道/tee 下完全没反馈 ───────────────────────────

def test_emit_above_prompt_flushes_when_not_tty(monkeypatch):
    """管道下 stdout 是块缓冲的：不 flush 的话进度提示要等一轮跑完才吐出来 ——
    用户看到的就是"完全没反馈"。"""
    import chat as chat_mod

    flushed: list[bool] = []

    class _Spy(io.StringIO):
        def isatty(self):
            return False

        def flush(self):
            flushed.append(True)
            super().flush()

    spy = _Spy()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setitem(chat_mod._PROMPT_LIVE, "on", False)
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._emit_above_prompt("⏳ 处理中…")

    assert "⏳ 处理中…" in spy.getvalue()
    assert flushed, "非 tty 下必须 flush"


def test_reasoning_display_writes_and_flushes_when_not_tty(monkeypatch):
    """非 tty 下思考指示同样要即时可见。"""
    import chat as chat_mod

    flushed: list[bool] = []

    class _Spy(io.StringIO):
        def isatty(self):
            return False

        def flush(self):
            flushed.append(True)
            super().flush()

    spy = _Spy()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "brief")
    monkeypatch.setattr(chat_mod, "_REASON_STATE",
                        {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0})
    monkeypatch.setattr(sys, "stdout", spy)

    chat_mod._reasoning_display("思考内容")
    chat_mod._reasoning_display(None)

    out = spy.getvalue()
    assert "思考中" in out, f"思考阶段没有任何指示：{out!r}"
    assert out.endswith("\n"), "推理段必须以换行收尾，别跟正文挤一行"
    assert flushed


def test_reasoning_display_off_mode_silent(monkeypatch):
    import chat as chat_mod

    spy = io.StringIO()
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "off")
    monkeypatch.setattr(chat_mod, "_REASON_STATE",
                        {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0})
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._reasoning_display("x")
    chat_mod._reasoning_display(None)
    assert spy.getvalue() == ""


def test_reasoning_display_full_mode_streams_text(monkeypatch):
    import chat as chat_mod

    spy = io.StringIO()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "full")
    monkeypatch.setattr(chat_mod, "_REASON_STATE",
                        {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0})
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._reasoning_display("推理正文")
    chat_mod._reasoning_display(None)
    assert "推理正文" in spy.getvalue()


# ── 状态快照慢时有提示 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_snapshot_hint_when_slow(monkeypatch):
    """query_project_status 是同步阻塞 I/O，随 KB / run 规模线性变慢，且正好卡在
    用户发完话到首 token 之间 —— 慢的时候至少要告诉用户在忙什么。"""
    import chat as chat_mod
    from core import tool_registry

    async def _slow(name, state, **kw):
        await asyncio.sleep(0.2)
        return {"status": "success", "artifacts": [], "memory_count": 1}

    monkeypatch.setattr(tool_registry, "execute", _slow)
    monkeypatch.setattr(chat_mod, "_STATUS_SLOW_HINT_S", 0.01)

    said: list[str] = []
    monkeypatch.setattr(chat_mod, "_emit_above_prompt", said.append)

    class _S:
        project_id = "p1"

    msgs: list = []
    await chat_mod._inject_status_snapshot(_S(), msgs)

    assert any("项目状态" in s for s in said), f"慢查询没有任何提示：{said}"
    assert msgs, "快照本身仍要注入"
