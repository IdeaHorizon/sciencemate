"""issue #710：渲染出的 context ≤ 窗口是可证性质，不是撞上 400 再救的火。

事故（qinp 会话 673ecc90，2026-08-28）：13+ 条各 ~25k 字符的 run_node blocked
结果享受驱逐豁免无限累积；三个各自为政的预算互相不一致；17 次压缩全部按时
触发但 11 连次 ratio≈1.0；最后发出 input 245761 + output 16384 = 262145 >
262144 的请求，网关 400 直接把 run 打死。

四条判据：
  1. context 超限 400 的判别与取数（服务端报错正文是权威测量）。
  2. qinp 形状回放：同签名 blocked 结果反复堆积必须被机械驱逐兜住，
     发出的每一个请求都装得下。
  3. 非工具部分独自超窗时，明确失败（failed LoopResult，文案指名框架侧），
     绝不发出注定 400 的请求。
  4. 真撞了 context 400（估算被服务端证伪）：回滚本轮、用服务端数字重校准、
     下一轮自愈 —— 不杀 run。
"""
from __future__ import annotations

import json

import pytest

import shared.tools  # noqa: F401
from core import summarizer as sm
from core.llm import LLMHTTPError, LLMMessage, LLMResponse, context_overflow_numbers


_DEEPSEEK_400_BODY = json.dumps({"error": {
    "message": ("This model's maximum context length is 262144 tokens. "
                "However, you requested 16384 output tokens and your prompt "
                "contains at least 245761 input tokens, for a total of at "
                "least 262145 tokens. Please reduce the length of the input "
                "prompt or the number of requested output tokens. "
                "(parameter=input_tokens, value=245761)"),
    "type": "invalid_request_error"}})


# ── 1. 判别与取数 ───────────────────────────────────────────────────────────

def test_context_overflow_numbers_reads_the_server_truth():
    exc = LLMHTTPError(400, f"LLM API HTTP 400: {_DEEPSEEK_400_BODY}",
                       body=_DEEPSEEK_400_BODY)
    got = context_overflow_numbers(exc)
    assert got == (262144, 245761), got


def test_a_non_context_400_is_not_treated_as_capacity():
    body = json.dumps({"error": {"message": "Unterminated string starting at: "
                                            "line 1 column 139 (char 138)"}})
    exc = LLMHTTPError(400, f"LLM API HTTP 400: {body}", body=body)
    assert context_overflow_numbers(exc) is None
    exc2 = LLMHTTPError(429, "rate limited", body="")
    assert context_overflow_numbers(exc2) is None


# ── 脚手架 ──────────────────────────────────────────────────────────────────

def _register_dispatch_tool(monkeypatch, *, big_chars: int = 25_000):
    """一个 run_node 形状的工具：非 replayable、无 compactor、每次换参数、
    每次返回 ~25k 字符的 blocked 报告 —— qinp 事故里无限累积的那类内容。"""
    from core import tool_registry

    async def _dispatch(*, state, **kwargs):
        return {"status": "blocked",
                "child_run_id": kwargs.get("req", "?"),
                "report": "阻" * big_chars}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "dispatch_child", _dispatch)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "dispatch_child",
                        tool_registry.ToolDefinition(
                            name="dispatch_child", description="d",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=False))


def _retrying_llm(tool_name: str, n_calls: int, *, fail_first_with=None):
    """每轮换参数调同一工具；可选：第一次调用先抛一个异常。"""

    class _LLM:
        def __init__(self):
            self.n = 0
            self.seen: list[list[LLMMessage]] = []

        async def chat(self, messages, **kw):
            self.n += 1
            self.seen.append(list(messages))
            if fail_first_with is not None and self.n == 1:
                raise fail_first_with
            if self.n > n_calls:
                return LLMResponse(content="done", tool_calls=[],
                                   finish_reason="stop", usage={})
            return LLMResponse(
                content=f"重试第 {self.n} 次",
                tool_calls=[{"id": f"c{self.n}", "type": "function",
                             "function": {"name": tool_name,
                                          "arguments": json.dumps({"req": f"r{self.n}"})}}],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 100, "completion_tokens": 10,
                       "total_tokens": 110})
    return _LLM()


# ── 1.5 预算换算比与当班量尺同源（不依赖 tiktoken 是否安装）──────────────────

def test_tool_budget_translates_tokens_with_the_ruler_on_duty(monkeypatch):
    """token 预算 → 字符预算的换算比必须由**当班估算器**对**正被预算的 pile**
    现算，不是硬编码 ×4。

    ×4 是 char/4 fallback 的逆 —— tiktoken 在场时中文 ≈2 token/字，×4 让字符
    预算宽 8 倍：CI 实测 4 份 25k 字 blocked 报告钉死在 context、eff 263k 对着
    60k 的窗（qinp 事故在真 tokenizer 下复活）。本测试 stub 一个 2 token/字的
    encoder 钉住换算缝，**不管本机装没装 tiktoken 都在测真刻度**。
    """
    from core.harness import NodeHarness, SummarizerConfig

    class _Enc:
        def encode(self, text):
            return [0] * (2 * len(text))    # 2 token/字：cl100k 对中文的量级

    harness = NodeHarness(node_type="literature", max_turns=40, tools=[],
                          summarizer=SummarizerConfig(enabled=False))
    harness.max_context_tokens = 60_000
    harness.max_output_tokens = 2_000
    msgs = [LLMMessage(role="system", content="x"),
            LLMMessage(role="tool", content="阻" * 25_000, tool_call_id="c1"),
            LLMMessage(role="tool", content="阻" * 25_000, tool_call_id="c2")]

    monkeypatch.setattr(sm, "_TIKTOKEN_ENC", _Enc())
    budget = sm.derived_tool_budget_bytes(harness, None, msgs, output_tokens=2_000)
    # 真刻度下窗口只装得下 ~30k token 的工具份额 ≈ 1.5 万字：预算必须小到
    # 连**一份** 25k 字报告都坐不满 —— 旧 ×4 给 ~17 万字（能坐 6 份），必转红。
    assert budget < 25_000, f"2 token/字 的世界里预算给了 {budget} 字符"

    # 对照：fallback 尺（char/4）下换算比 ≈4，行为与旧版同量级 —— 收紧只
    # 发生在真刻度更紧的时候，不误伤英文/无 tiktoken 环境。
    monkeypatch.setattr(sm, "_TIKTOKEN_ENC", False)
    budget_fallback = sm.derived_tool_budget_bytes(
        harness, None, msgs, output_tokens=2_000)
    assert budget_fallback > 100_000, (
        f"char/4 世界的预算被错误收紧到 {budget_fallback} 字符")


# ── 2. qinp 形状回放：堆积被兜住、每个请求都装得下 ──────────────────────────

@pytest.mark.asyncio
async def test_the_blocked_dispatch_pile_is_bounded_and_every_request_fits(
        tmp_path, monkeypatch):
    """同签名 blocked 结果反复堆积（qinp：13 次重试 ×25k 字符）不再无界。

    变异校验：把 enforce_budget 的资格判据改回旧豁免（logged 不算数），
    本测试必转红 —— 堆积撑破窗口，发送闸拒发，run 失败。
    """
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    _register_dispatch_tool(monkeypatch)
    harness = NodeHarness(node_type="literature", max_turns=40,
                          tools=["dispatch_child"],
                          summarizer=SummarizerConfig(enabled=False))
    harness.max_context_tokens = 60_000
    harness.max_output_tokens = 2_000
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p710")
    llm = _retrying_llm("dispatch_child", 15)
    result = await run_loop(harness, state, [], llm)

    assert result.status != "failed", f"回放本该完成：{result.final_text[:200]}"
    assert llm.n >= 16, "没跑完全部重试"
    # 每一个真正发出的请求：除**上一轮刚产出的受保护结果**外，其余一切必须
    # 装进窗口 —— 用框架自己的量尺复核。
    #
    # 为什么不是裸的 eff ≤ window：最近一轮的结果是本轮的输入，驱逐它是
    # 活锁（enforce_budget 的 protect_turn_ge），收缩输出也盖不住一份独自
    # 超窗的结果 —— 单份结果超过配置窗口时框架**自觉选择**交付它（配置窗口
    # 是 summarizer 触发参考，不是 API hard limit；拒发权只属于 provider
    # 亲口说过的硬上限）。所以可证的不变量是：超窗量 ≤ 受保护新鲜结果的
    # 贡献。旧的 ×4 逆换算 bug 下（中文 pile 预算宽 8 倍）第 3 个请求起
    # 就带着 2+ 份旧报告、超出豁免额 —— 本断言转红（CI 实测 eff 260k）。
    for i, msgs in enumerate(llm.seen):
        eff = sm.effective_prompt_tokens(sm.estimate_tokens(msgs), state)
        window = sm.effective_context_window(harness, state)
        trailing = []
        for m in reversed(msgs):
            if getattr(m, "role", None) == "tool":
                trailing.append(m)
            else:
                break
        fresh_allowance = sm.effective_prompt_tokens(
            sm.estimate_tokens(trailing), state) if trailing else 0
        assert eff + harness.max_output_tokens <= window + fresh_allowance, (
            f"第 {i+1} 个请求超窗且超出新鲜结果豁免：eff={eff}"
            f" + out={harness.max_output_tokens}"
            f" > window={window} + fresh={fresh_allowance}")
    # 堆没有无限长：留在 context 里的完整 blocked 报告至多是预算内那几份。
    full = [m for m in llm.seen[-1]
            if getattr(m, "role", None) == "tool"
            and "阻阻阻" in (m.content or "")]
    assert 1 <= len(full) <= 7, f"context 里躺着 {len(full)} 份完整 blocked 报告"


# ── 3. 结构性放不下 = 明确失败，不发注定 400 的请求 ─────────────────────────

@pytest.mark.asyncio
async def test_a_request_that_cannot_fit_is_refused_not_sent(tmp_path, monkeypatch):
    """非工具部分独自超过 **provider 亲口说过的硬上限**：驱逐救不了、收缩
    输出救不了 → 明确失败，且 LLM **一次都没有收到**那个注定 400 的请求。

    拒发权只来自权威观测（PROVIDER_HARD_CAP_KEY，来自 context-400 正文）；
    只超配置窗口（summarizer 触发参考）时闸只做尽力而为的驱逐/收缩后放行
    —— 否则"小窗 + 大输出"的合法配置会被整类判死（真实回归：
    test_inject_survives_summarizer 的 1000 窗 harness 被闸当场枪毙）。
    """
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    harness = NodeHarness(node_type="literature", max_turns=3, tools=[],
                          summarizer=SummarizerConfig(enabled=False))
    harness.max_context_tokens = 2_000
    harness.max_output_tokens = 1_000
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_refuse")
    state.hook_state[sm.PROVIDER_HARD_CAP_KEY] = 2_000   # provider 说过的
    llm = _retrying_llm("noop", 0)
    seed = [LLMMessage(role="user", content="长" * 40_000)]

    result = await run_loop(harness, state, seed, llm)
    assert result.status == "failed"
    assert "context 装不下" in (result.final_text or "") or \
           "context 装不下" in str(result.__dict__), result.final_text
    assert llm.n == 0, "明知装不下还是把请求发出去了"


# ── 4. 真撞 context 400：回滚 + 服务端数字重校准 + 自愈 ─────────────────────

@pytest.mark.asyncio
async def test_a_context_400_recalibrates_and_recovers(tmp_path, monkeypatch):
    """服务端 400 里的数字是权威测量：喂给校准、回滚本轮、下一轮自愈。

    第一次调用抛 DeepSeek 形状的 context 400（服务端实收远大于本地估算），
    之后正常 —— run 必须活下来，且观测比被服务端数字抬高。
    """
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    _register_dispatch_tool(monkeypatch, big_chars=100)
    exc = LLMHTTPError(400, f"LLM API HTTP 400: {_DEEPSEEK_400_BODY}",
                       body=_DEEPSEEK_400_BODY)
    harness = NodeHarness(node_type="literature", max_turns=6,
                          tools=["dispatch_child"],
                          summarizer=SummarizerConfig(enabled=False))
    harness.max_context_tokens = 300_000
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_400")
    llm = _retrying_llm("dispatch_child", 2, fail_first_with=exc)

    result = await run_loop(harness, state, [], llm)
    assert result.status != "failed", f"context 400 杀掉了 run：{result.final_text[:200]}"
    assert llm.n >= 2, "400 之后没有重试"
    # 服务端权威数字进了校准（观测比 > 静态系数下限）。
    ratio = float(state.hook_state.get("_ctx_observed_prompt_ratio") or 0.0)
    assert ratio > 1.3, f"服务端 245761 vs 本地小估算没有抬高观测比：{ratio}"
    # provider 宣称的硬上限进了容量后验（硬上限键，不是解码退化键）。
    assert int(state.hook_state.get(sm.PROVIDER_HARD_CAP_KEY) or 0) == 262144
    assert not state.hook_state.get(sm.OBSERVED_CEILING_KEY), \
        "400 硬上限写错了键 —— 解码退化键会让发送闸把 output 也算进去"
