"""LLM 流式传输层（R2-b）：SSE 增量重组 + 生成中止 + 非流式回退。

设计要点：流式只是传输变体 —— _consume_stream 把 SSE 增量重组成与非流式同构的
response dict，下游解析/恢复/防火墙全复用。2026-07-09 对 GPUStack deepseek-v4-pro
真端点实测过 SSE 形态（content 增量 / tool_calls 按 index 分块 / usage / [DONE]），
本文件的 fake 流严格按那个真实形态构造。不联网。
"""
from __future__ import annotations

import json

import pytest

from core import llm as llm_mod
from core.llm import (
    LLMClient,
    LLMMessage,
    StreamSanitizer,
    set_stream_abort_check,
)

# ── StreamSanitizer：增量清洗（逐 token 显示的核心）─────────────────────────

def _san(deltas):
    s = StreamSanitizer()
    return "".join(s.feed(d) for d in deltas) + s.flush()


def test_sanitizer_plain_text_passthrough():
    assert _san(list("这是正常回复。")) == "这是正常回复。"


def test_sanitizer_hides_think_leak():
    assert _san(["<think>推理", "过程", "</think>", "正式回复"]) == "正式回复"


def test_sanitizer_orphan_close_think():
    assert _san(["先想想", "</think>", "答案"]) == "答案"


def test_sanitizer_control_token_split_across_chunks():
    """半截控制标记不会被显示出半个（holdback 机制）。"""
    assert _san(["回复正文", "<|im_", "end|>"]) == "回复正文"


def test_sanitizer_dsml_fragment():
    assert _san(["好，", "</｜DSML｜tool_calls>"]) == "好，"


def test_sanitizer_split_close_think_tag():
    assert _san(["<think>r", "</thi", "nk>", "答案"]) == "答案"


def test_sanitizer_pure_control_noise_empty():
    assert _san(["</｜DSML｜tool_calls>"]) == ""


# ── 流式显示回调（stream_display 实例级 + 只 orchestrator 流）─────────────────


def _sse(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False)


def _delta_chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    body = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return _sse(body)


class _FakeStreamCM:
    def __init__(self, lines: list[str], status: int = 200):
        self._lines = lines
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b'{"error": "stream not supported"}'


class _FakeClient:
    """httpx.AsyncClient 替身：只实现流式接口。lines 由测试注入。"""
    lines: list[str] = []
    status: int = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def stream(self, method, url, **kw):
        return _FakeStreamCM(type(self).lines, type(self).status)


@pytest.fixture(autouse=True)
def _stream_on(monkeypatch):
    monkeypatch.setenv("LLM_STREAM", "1")
    yield
    set_stream_abort_check(None)


def _client() -> LLMClient:
    return LLMClient(api_key="k", model="m", base_url="http://x", max_retries=0)


@pytest.mark.asyncio
async def test_stream_content_accumulates(monkeypatch):
    _FakeClient.lines = [
        _delta_chunk({"role": "assistant", "content": ""}),
        _delta_chunk({"content": "水的"}),
        _delta_chunk({"content": "化学式是 H2O。"}),
        _delta_chunk({}, finish="stop", usage={"total_tokens": 42}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert resp.content == "水的化学式是 H2O。"
    assert resp.finish_reason == "stop"
    assert resp.usage.get("total_tokens") == 42


@pytest.mark.asyncio
async def test_stream_tool_call_arguments_accumulate(monkeypatch):
    """tool_calls 按 index 分块（GPUStack 实测形态）：id/name 首块给全，
    arguments 跨块拼接。"""
    _FakeClient.lines = [
        _delta_chunk({"role": "assistant", "content": ""}),
        _delta_chunk({"tool_calls": [{
            "id": "call_abc", "type": "function", "index": 0,
            "function": {"name": "get_weather", "arguments": '{"ci'}}]}),
        _delta_chunk({"tool_calls": [{
            "index": 0, "function": {"arguments": 'ty": "Paris"}'}}]}),
        _delta_chunk({}, finish="tool_calls"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    resp = await _client().chat(
        [LLMMessage(role="user", content="巴黎天气")],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
    )
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}


@pytest.mark.asyncio
async def test_stream_reasoning_delta_split_out(monkeypatch):
    """reasoning_content 增量进 reasoning，不混进正文。"""
    _FakeClient.lines = [
        _delta_chunk({"reasoning_content": "先想想"}),
        _delta_chunk({"content": "答案是 42。"}),
        _delta_chunk({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert resp.content == "答案是 42。"
    assert resp.reasoning_content == "先想想"


@pytest.mark.asyncio
async def test_stream_abort_keeps_partial_drops_tool_calls(monkeypatch):
    """abort check 命中 → 断流：保留已生成正文，丢半截 tool_calls（/stop 语义）。"""
    fired = {"n": 0}

    def abort_after_two():
        fired["n"] += 1
        return fired["n"] > 2      # 第 3 个 chunk 前中止

    _FakeClient.lines = [
        _delta_chunk({"content": "已生成的部分"}),
        _delta_chunk({"content": "内容"}),
        _delta_chunk({"tool_calls": [{
            "id": "c1", "type": "function", "index": 0,
            "function": {"name": "run_bash", "arguments": '{"cmd": "l'}}]}),
        _delta_chunk({"content": "不该出现"}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    set_stream_abort_check(abort_after_two)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert resp.content == "已生成的部分内容"
    assert resp.tool_calls == []          # 半截参数不可执行 → 丢弃
    assert "不该出现" not in (resp.content or "")


@pytest.mark.asyncio
async def test_stream_4xx_falls_back_to_non_streaming(monkeypatch):
    """backend 不支持 stream（4xx）→ 本次调用自动回退非流式，不放弃。"""
    _FakeClient.lines = []
    _FakeClient.status = 400
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        assert "stream" not in payload      # 回退请求必须是干净的非流式 payload
        return {"choices": [{"message": {"content": "非流式兜底", "tool_calls": []},
                              "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert resp.content == "非流式兜底"
    _FakeClient.status = 200    # 还原类属性，防污染其它用例


@pytest.mark.asyncio
async def test_empty_200_stream_falls_back_to_same_non_stream_payload(monkeypatch):
    """HTTP 200 + [DONE] without a completion is an incomplete transport result."""
    _FakeClient.lines = ["data: [DONE]"]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    seen = []

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        seen.append(payload)
        return {
            "choices": [{
                "message": {"content": "recovered", "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {"completion_tokens": 3},
        }

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])

    assert resp.content == "recovered"
    assert len(seen) == 1
    assert "stream" not in seen[0]


@pytest.mark.asyncio
async def test_one_token_eos_stream_falls_back_to_non_stream(monkeypatch):
    """A gateway's one-token EOS is decode failure, not a valid assistant stop."""
    _FakeClient.lines = [
        _delta_chunk({"role": "assistant", "content": ""}),
        _delta_chunk(
            {},
            finish="stop",
            usage={"prompt_tokens": 24_277, "completion_tokens": 1},
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    called = {"post": 0}

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        called["post"] += 1
        return {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "run_bash", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 24_277, "completion_tokens": 12},
        }

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])

    assert called["post"] == 1
    assert [call["function"]["name"] for call in resp.tool_calls] == ["run_bash"]


@pytest.mark.asyncio
async def test_non_stream_fallback_can_remain_void_for_agent_loop(monkeypatch):
    """Two empty transports stay empty so agent_loop can record provider failure."""
    _FakeClient.lines = ["data: [DONE]"]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        return {
            "choices": [{
                "message": {"content": None, "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 24_277,
                "completion_tokens": 1,
                "total_tokens": 24_278,
            },
        }

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])

    assert resp.content is None
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
    assert resp.usage["completion_tokens"] == 1


@pytest.mark.asyncio
async def test_empty_stream_after_user_abort_does_not_restart_request(monkeypatch):
    """Cancellation is authoritative even if it happens before the first token."""
    _FakeClient.lines = [_delta_chunk({"content": "should not be consumed"})]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    set_stream_abort_check(lambda: True)

    async def forbidden_post(self, payload, *, timeout, max_retries):
        raise AssertionError("cancelled stream must not restart non-streaming")

    monkeypatch.setattr(LLMClient, "_post_with_retry", forbidden_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])

    assert resp.content is None
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_stream_env_off_uses_post(monkeypatch):
    """LLM_STREAM=0 → 走非流式 _post_with_retry（存量行为不变）。"""
    monkeypatch.setenv("LLM_STREAM", "0")
    called = {"post": 0}

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        called["post"] += 1
        return {"choices": [{"message": {"content": "ok", "tool_calls": []},
                              "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    resp = await _client().chat([LLMMessage(role="user", content="?")])
    assert called["post"] == 1
    assert resp.content == "ok"


@pytest.mark.asyncio
async def test_stream_display_receives_clean_deltas_then_none(monkeypatch):
    """设了 stream_display → 逐块收到清洗后的 content，结束收到 None。
    reasoning 增量永不进 display。"""
    _FakeClient.lines = [
        _delta_chunk({"reasoning_content": "内部推理不该上屏"}),
        _delta_chunk({"content": "答案是 "}),
        _delta_chunk({"content": "月球。"}),
        _delta_chunk({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    events = []
    c = _client()
    c.stream_display = events.append
    resp = await c.chat([LLMMessage(role="user", content="?")])
    # 最后一个事件是 None（流结束标记）
    assert events[-1] is None
    shown = "".join(e for e in events if e)
    assert shown == "答案是 月球。"
    assert "内部推理" not in shown
    assert resp.content == "答案是 月球。"


@pytest.mark.asyncio
async def test_stream_display_hides_think_leak_live(monkeypatch):
    """<think> 泄漏进 content 流 → 显示层一个字都不漏。"""
    _FakeClient.lines = [
        _delta_chunk({"content": "<think>我先"}),
        _delta_chunk({"content": "琢磨一下</think>"}),
        _delta_chunk({"content": "正式回复"}),
        _delta_chunk({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    events = []
    c = _client()
    c.stream_display = events.append
    await c.chat([LLMMessage(role="user", content="?")])
    shown = "".join(e for e in events if e)
    assert shown == "正式回复"
    assert "琢磨" not in shown


@pytest.mark.asyncio
async def test_no_stream_display_no_calls(monkeypatch):
    """没设 stream_display（子节点的新 LLMClient 默认没有）→ 不产生任何显示副作用。"""
    _FakeClient.lines = [
        _delta_chunk({"content": "子节点内容"}),
        _delta_chunk({}, finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    c = _client()
    assert c.stream_display is None      # 默认无
    resp = await c.chat([LLMMessage(role="user", content="?")])
    assert resp.content == "子节点内容"   # 内容仍正确收集，只是不显示


def test_leading_scaffold_echo_is_cut_by_evidence_not_by_phrase_list():
    """模型把本轮注入的 scaffolding 改写成开场括号 —— 按证据剥，不按短语名单。

    实测三个变体（deepseek-v4-pro 真机 2026-08-07）：
      （hook 已启用：每轮自动 inject 你的 scratchpad 到 system prompt。）
      （框架会在每轮 LLM call 前把 scratchpad 注入 system prompt。）
      （本提示只在 run 的 first turn 出现一次；后续轮次你会看到你自己的笔记。）
    平台侧曾按短语名单拦，三次实测三次漏 —— 名单是错的机制（源头文案一改
    或模型换个说法就静默失效）。判据必须来自本轮真实 injected_texts。
    """
    from core.llm import _cut_leading_scaffold_echo
    from core.loop_hooks_builtin import _SCRATCHPAD_FIRST_PRINCIPLES as INJECTED

    # 三条历史泄漏样本对应的是**当时那版**引导文案。它们是 2026-08-07 真机
    # 现场，价值在于钉住"按证据剥"这个机制 —— 所以把当时的文案冻在测试里，
    # 而不是跟着 live 常量漂。文案一改就让历史回归失效，等于把证据扔了。
    LEGACY_INTRO_20260807 = """📝 你的 scratchpad —— 跨轮工作笔记（first turn 引导）

这次任务可能跑几十轮 LLM call。messages 历史会被压缩成摘要，但你写进
**scratchpad 的笔记跨轮持续存在并每轮自动注入**。这是你**唯一能跨上下文压
缩保留、由你自己写的思路**。

**第一性原理**：写"如果下一轮的我没看到现在的 messages、只看到压缩摘要 +
scratchpad，他/她需要知道什么才能高效继续推进？"

不是 template。不是套话。是你真实思路的速记。

**典型该写的**（按重要性）：
  - 当前 working hypothesis 是啥 + **为啥不选 alternative**（防下轮重新论证）
  - 试过不 work 的：什么尝试 / 失败原因 / 不要再 retry
  - 悬挂线索：稍后核对 X / 不确定 Y 来源 / 没追完的 thread
  - 关键决策的 reasoning（特别是"为啥选 A 不选 B"那条暗线）
  - 临时计算 / 中间数值（取代 mental math）

**不该写**（用别的工具）：
  - 跨 run 持久的可复用经验 → add_memory_candidate（curator 加工）
  - 即时 runtime 约束 → add_runtime_directive（写完下轮立刻生效）
  - 进 KB 的 claim / concept → create_claim / create_concept
  - 完整 artifact 内容 → save_artifact

**写作纪律**：
  - 1-3 行；想长就分多条 write_scratchpad
  - 速记不评分，用任何对你高效的格式（带 emoji / 缩写 / 箭头都行）
  - **每 5-10 轮看一次自己的 scratchpad**。如果都是套话或空 → 你大概率没
    在跨轮真正思考，每轮都从零 reason，浪费 token

（笔记只对你可见，不进 memory.jsonl，不影响下游节点。）
"""

    for leaked, answer in (
        ("（hook 已启用：每轮自动 inject 你的 scratchpad 到 system prompt。）"
         "科研报告要区分观测与推断。", "科研报告要区分观测与推断。"),
        ("（框架会在每轮 LLM call 前把 scratchpad 注入 system prompt。）"
         "根据当前项目上下文：项目名称为 X。", "根据当前项目上下文：项目名称为 X。"),
        ("（本提示只在 run 的 first turn 出现一次；后续轮次你会看到你自己的笔记。）"
         "均值是 3.0。", "均值是 3.0。"),
    ):
        assert _cut_leading_scaffold_echo(leaked, [LEGACY_INTRO_20260807]) == answer

    # 现行文案（白板）同样要挡得住改写复读
    for leaked, answer in (
        ("（白板每轮原样注入回来，整块替换，不是追加日志。）结论：Tg 随冷却速率上升。",
         "结论：Tg 随冷却速率上升。"),
    ):
        assert _cut_leading_scaffold_echo(leaked, [INJECTED]) == answer

    # 正常正文里的开场括号不得误伤
    for intact in (
        "（注：以下结果基于 10000 次采样，误差约 0.4%。）估计值 3.14。",
        "（简言之）这是正常正文。",
        "（我认为把问题问对是最容易被低估的环节。）理由如下。",
    ):
        assert _cut_leading_scaffold_echo(intact, [INJECTED]) == intact

    # 没有注入证据时一律不砍
    assert _cut_leading_scaffold_echo("（hook 已启用：inject scratchpad）答案", []) == (
        "（hook 已启用：inject scratchpad）答案"
    )


def test_verbatim_block_scaffold_echo_is_cut_with_its_trailing_paraphrase():
    """整块逐字复读 + 紧跟的改写括号 —— UI 实测形态（2026-08-07）。

    真机上模型把整段 400+ 字 scratchpad 引导逐字抄进 content，抄完接一句自己
    改写的括号，最后才写答案 —— UI 上等于把框架内部提示当成回复给用户看。
    整块是逐字（containment≈1）；紧跟的括号是改写（只有 0.23），单看它证据不足，
    但**位置**是证据：上一段已确认在复读，同一段落序列里的下一句照剥。
    """
    from core.llm import _cut_leading_scaffold_echo
    from core.loop_hooks_builtin import _SCRATCHPAD_FIRST_PRINCIPLES as INJECTED

    leaked = (
        INJECTED
        + '\n（别写"我收到用户消息" / "我该做 X"之类的复读 —— 那是你的即时行为，'
          "不是跨轮笔记）好的，调研结果出来了。以下是 5 篇可核验论文。"
    )
    assert _cut_leading_scaffold_echo(leaked, [INJECTED]) == (
        "好的，调研结果出来了。以下是 5 篇可核验论文。"
    )

    # 正文本身在讨论 scratchpad 用法 —— 不得误伤（没有整块逐字重合）
    legit = (
        "关于 scratchpad 的使用建议。\n\n"
        "第一，写清楚 working hypothesis 和为什么不选 alternative。"
    )
    assert _cut_leading_scaffold_echo(legit, [INJECTED]) == legit

    # 全是复读、砍完没正文 → 宁可原样返回，不能把回复清空
    assert _cut_leading_scaffold_echo(INJECTED, [INJECTED]) == INJECTED


def test_scaffold_evidence_comes_from_source_not_from_message_position():
    """证据按**来源**取（hook 注入的 system 消息），不按位置取。

    实测事故（2026-08-07 UI）：曾用 messages[-6:] 收集证据。模型在回答前做了
    几轮工具调用后，hook 注入就滑出这个窗口 —— 证据消失，整套剥离静默失效，
    400 字 scratchpad 引导原样渲染给用户。位置不是判据，来源才是。

    同时 messages[0]（harness 自己的巨型 system prompt）必须排除在证据池外：
    它体量极大，放进去会稀释判据、让正常正文误命中。
    """
    from core.llm import LLMMessage, _cut_leading_scaffold_echo
    from core.loop_hooks_builtin import _SCRATCHPAD_FIRST_PRINCIPLES as INJECTED

    messages = [
        LLMMessage(role="system", content="巨型 harness system prompt " * 200),
        LLMMessage(role="user", content="画张图"),
        LLMMessage(role="system", content=INJECTED),      # hook 注入
        LLMMessage(role="assistant", content=""),
        LLMMessage(role="tool", content="tool result 1"),
        LLMMessage(role="assistant", content=""),
        LLMMessage(role="tool", content="tool result 2"),
        LLMMessage(role="assistant", content=""),
        LLMMessage(role="tool", content="tool result 3"),
    ]
    # 复刻 core.llm.chat 里的取证方式
    evidence = [m.content for m in messages[1:] if m.role == "system" and m.content]
    assert INJECTED in evidence, "hook 注入必须在证据池里，无论它离结尾多远"
    assert not any(len(e) > 3000 and "harness system prompt" in e for e in evidence), \
        "harness 自己的 system prompt 不进证据池"

    leaked = INJECTED + "\n好的，图已生成。"
    assert _cut_leading_scaffold_echo(leaked, evidence) == "好的，图已生成。"

    # 旧的位置取证（最后 6 条）在这个 messages 上收不到任何证据 —— 回归守卫
    stale = [m.content for m in messages[-6:] if m.role == "system" and m.content]
    assert INJECTED not in stale


# ── 生成中止不靠前端记得注册（2026-08-17 平台停止按钮实测）───────────────────

def test_bound_run_cancellation_aborts_generation_without_registration():
    """kill_signal 写进绑定 run 的 state 后，无需任何 set_stream_abort_check
    注册，_generation_abort_requested 也必须回 True —— 平台进程从来没注册过
    回调，靠注册的中止只对 CLI 生效，在途长生成会把"立即停"拖成"跑完再停"。
    """
    import tempfile
    from pathlib import Path

    from core import cancellation
    from core.llm import _generation_abort_requested, set_stream_abort_check
    from core.state import State

    set_stream_abort_check(None)
    state = State.new(node_type="literature", base_dir=Path(tempfile.mkdtemp()))
    with cancellation.bind_run(state):
        assert _generation_abort_requested() is False, "没取消不许中止"
        state.hook_state["kill_signal"] = {"reason": "stop", "requested_by": "user"}
        assert _generation_abort_requested() is True, \
            "绑定 run 已被取消，生成必须在下一个 chunk 停下"
    assert _generation_abort_requested() is False, "解绑后不许把别的 run 的生成杀掉"
