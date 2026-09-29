"""空轮事务性 + 容量后验（E2E-5a 2026-08-03 空响应发散循环）。

现场：deepseek-v4-pro@zju 在 prompt≈174k 时解码退化（completion_tokens=1），
orchestrator 连续 22 轮空响应。旧逻辑每轮把失败**提交进历史**（hook 重灌 +
空 assistant 追加），实测每轮 +2118 token / +7 条消息，严丝合缝的线性发散：

    130,294 → 132,412 → 134,530 → … → 174,772   （22 轮无一例外）

同时压缩线按配置窗口算（0.7×262,144=183.5k），框架手握 22 次"174k 上发请求
返回空"的直接测量，一次也没用来修正对容量的信念 —— 症状是上下文太大，
重试却把上下文做得更大，循环在数学上不可能自愈。

两条不变量级修复（都不是守卫/熔断）：
  1. **失败的转移不许提交状态**：空轮（无工具+无正文）回滚到轮初快照，
     重试是真正相同的一次尝试 —— 驻定，不发散，几乎全命中 provider 缓存。
  2. **配置是先验，观测是证据**：空响应观测压低有效窗口（min），更大 prompt
     的成功清除它（瞬态故障不永久污染信念）。两条恢复路径由此机械分叉：
     容量问题 → 压缩触发 → 请求变小 → 立即重试；瞬态故障 → 退避原样重放。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import core.agent_loop as agent_loop_mod
from core import summarizer as summarizer_mod
from core.agent_loop import run_loop
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _state(node_type: str = "experiment") -> State:
    return State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """退避等待打桩成瞬时（真实值 20..320s），并记录请求的等待时长。"""
    waits: list[float] = []

    async def _fake_sleep(state, delay_s):
        waits.append(delay_s)

    monkeypatch.setattr(agent_loop_mod, "_void_backoff_sleep", _fake_sleep)
    return waits


@pytest.fixture(autouse=True)
def _register_probe_tool():
    async def _probe(state, **kw):
        return {"status": "success"}
    try:
        register_tool(ToolDefinition(
            name="probe_tool", description="test tool",
            parameters_schema={"type": "object", "properties": {}},
            handler=_probe, node_types=["experiment"],
        ))
    except Exception:
        pass


def _harness(**kw) -> NodeHarness:
    h = NodeHarness(
        node_type="experiment", system_prompt="sys",
        tools=["probe_tool"], max_turns=kw.pop("max_turns", 30),
    )
    for k, v in kw.items():
        setattr(h, k, v)
    return h


def _void(prompt_tokens: int, completion_tokens: int = 1) -> LLMResponse:
    """E2E-5a 现场形态：finish=stop、content 空、completion≈1、判为 empty leak。"""
    return LLMResponse(
        content="", tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": prompt_tokens,
               "completion_tokens": completion_tokens,
               "total_tokens": prompt_tokens + completion_tokens},
        protocol_leak=True, leak_kind="empty",
    )


def _answer(text: str = "done", prompt_tokens: int = 1000) -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": 50,
               "total_tokens": prompt_tokens + 50},
    )


class ScriptedLLM:
    """按脚本吐响应；耗尽后一直回最后一个。"""

    def __init__(self, *responses: LLMResponse):
        self.responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def chat(self, messages, **kw):
        self.calls.append([LLMMessage(role=m.role, content=m.content)
                           for m in messages])
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _msgs() -> list[LLMMessage]:
    return [LLMMessage(role="system", content="sys"),
            LLMMessage(role="user", content="做任务")]


# ── 不变量 1：失败的转移不许提交状态 ────────────────────────────────────────

def test_void_turn_is_a_noop_on_messages():
    """空轮重试期间 messages 驻定：第 N 次请求与第 1 次逐字节相同。

    修前形态（E2E-5a 实测）：每轮 +7 条消息 / +2118 token，22 轮线性发散。
    """
    state = _state()
    llm = ScriptedLLM(_void(174_772), _void(174_772), _answer("好了"))
    messages = _msgs()
    result = asyncio.run(run_loop(_harness(), state, messages, llm))

    assert result.final_text == "好了"
    assert len(llm.calls) == 3
    # 判决性断言：三次请求的 messages 逐字节相同 —— 重试是相同的一次尝试
    first = [(m.role, m.content) for m in llm.calls[0]]
    for i, call in enumerate(llm.calls[1:], 2):
        assert [(m.role, m.content) for m in call] == first, \
            f"第 {i} 次重试的上下文与第 1 次不同 —— 失败被提交进了状态"


def test_void_final_still_rolls_back_and_types_the_result():
    """重试用尽：messages 仍回滚（失败不进历史），结果带 status='void' +
    结构化标记 —— 上层（executor 归 infra 账 / chat 驻定重放）有东西可消费，
    而不是从散文里猜。"""
    state = _state()
    llm = ScriptedLLM(_void(174_772))
    messages = _msgs()
    n_before = len(messages)
    result = asyncio.run(run_loop(_harness(), state, messages, llm))

    assert result.status == "void"
    assert len(messages) == n_before, "空轮终态也不许把失败提交进历史"
    info = state.hook_state.get("_void_turn_final")
    assert info and info["prompt_tokens"] == 174_772
    assert info["retries"] == len(agent_loop_mod._VOID_RETRY_DELAYS)
    # 散文层照旧（executor 的 blank_stop 分类靠它），但机器读结构化字段
    assert "近乎空响应" in result.final_text


def test_productive_turn_resets_void_budget():
    """空轮预算 per-问题，不是 per-run：中间有产出就清零。"""
    state = _state()
    n = len(agent_loop_mod._VOID_RETRY_DELAYS)
    tool_call = LLMResponse(
        content=None,
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "probe_tool", "arguments": "{}"}}],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020},
    )
    # 先烧掉 n-1 次空轮 → 一次成功工具调用 → 再来 n-1 次空轮 → 成功收尾。
    # 若预算不清零，第二串空轮会在中途 void-final。
    script = [_void(5000)] * (n - 1) + [tool_call] + [_void(5000)] * (n - 1) + [_answer()]
    llm = ScriptedLLM(*script)
    result = asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert result.status == "completed"
    assert result.final_text == "done"


# ── 不变量 2：配置是先验，观测是证据 ────────────────────────────────────────

def test_void_records_observed_ceiling():
    """**复现**（连续第二次空轮）才算容量证据，一次不算 —— 见下面那条用例。

    回滚保证重放是逐字节相同的一次尝试（不变量 1），所以"连续两次空轮"就是
    "同一个 prompt 上重现了两次"。观测与复现的分界画在这里。
    """
    state = _state()
    llm = ScriptedLLM(_void(174_772), _void(174_772), _answer(prompt_tokens=1000))
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert state.hook_state.get(summarizer_mod.OBSERVED_CEILING_KEY) == 174_772


def test_one_short_reply_is_not_a_ceiling():
    """一个数据点不是天花板 —— 2026-08-25 实测，session 2220d882。

    调度器交完终稿（completion=818，带 CONTINUOUS_STATUS: complete）5 秒后返回
    了一个 **1 token** 的响应。旧判据当场把 ceiling 钉在 prompt_tokens=132850,
    而 `configured_window` 是 **256000** —— 离窗口还差一半。天花板一钉，
    `should_compress` 立刻成立（transcript 里 will_compress=true / wait_s=0），
    prompt 压到 63521，模型再睁眼时自己刚交付的那份报告已经被摘要掉了，于是
    **又写了一遍**，同一张图还给出了另一个路径。用户看到两大段近乎重复的终稿。

    一次短回复最常见的意思是模型没话说了。真撞到容量上限时它不会只出现一次
    —— 而回滚保证重放逐字节相同（不变量 1），所以第二次空轮就是**复现**。
    """
    state = _state()
    working = LLMResponse(
        content=None,
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "probe_tool", "arguments": "{}"}}],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 131_560, "completion_tokens": 818,
               "total_tokens": 132_378},
    )
    # ⚠️ 收尾那次的 prompt 必须**小于** ceiling：更大 prompt 的成功会清除
    # ceiling（agent_loop.py:588，"瞬态故障不永久污染信念"）。用 132_900 收尾
    # 的话，天花板先被钉上、再被清掉，读到的 None 是清除的结果而不是没钉过 ——
    # 变异（去掉判据）照样绿。这个坑是变异抓出来的。
    llm = ScriptedLLM(working, _void(132_850, completion_tokens=1),
                      _answer(prompt_tokens=1_000))
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert summarizer_mod.OBSERVED_CEILING_KEY not in state.hook_state, (
        "一次 1-token 回复把窗口砍成了一半 —— 压缩会把模型刚交付的东西摘要掉，"
        "它就会再交付一遍"
    )


def test_ceiling_lowers_effective_compression_threshold():
    """E2E-5a 判决性形态：同一份 messages，按配置窗口（262144）不触发压缩 ——
    这正是现场 22 轮永远等不到 183.5k 压缩线的旧行为；学到观测上限后必须触发。
    ceiling 由实测 est 反推（不猜 tokenizer 行为），锁的是**证据改变行为**本身。"""
    h = _harness()
    h.max_context_tokens = 262_144
    h.summarizer.enabled = True
    h.summarizer.trigger_type = "token_threshold"
    h.summarizer.trigger_threshold = 0.7

    big = [LLMMessage(role="user", content=("研究记录 %d：结果如下。" % i) * 40)
           for i in range(60)]
    state = _state()
    dec_before, est = summarizer_mod.should_compress(h, big, 5, state=state)
    assert dec_before is False, "前提：按配置窗口不该触发（复刻旧行为）"
    eff = int(est * summarizer_mod.effective_calibration(state))
    assert eff > 0, "前提：messages 非空"

    # 观测上限设成"有效 token 刚好越过 0.7×ceiling"的值 —— 模拟在略高于当前
    # 上下文的位置观测到空响应（现场：est≈174k 的请求在 174,772 上死了）。
    state.hook_state[summarizer_mod.OBSERVED_CEILING_KEY] = int(eff / 0.7) - 10
    dec_after, _ = summarizer_mod.should_compress(h, big, 5, state=state)
    assert dec_after is True, "观测上限必须修正压缩线 —— 证据不改变行为就是摆设"


def test_success_at_or_above_ceiling_clears_it():
    """瞬态故障不许永久污染容量信念：≥上限的成功响应清除观测。"""
    state = _state()
    llm = ScriptedLLM(_void(174_772), _answer(prompt_tokens=175_000))
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert summarizer_mod.OBSERVED_CEILING_KEY not in state.hook_state


def test_small_prompt_success_keeps_ceiling():
    """压缩后的小 prompt 成功**不**清除上限 —— 它没证明大 prompt 能活。"""
    state = _state()
    llm = ScriptedLLM(_void(174_772), _void(174_772), _answer(prompt_tokens=90_000))
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert state.hook_state.get(summarizer_mod.OBSERVED_CEILING_KEY) == 174_772


def test_markup_leak_is_not_capacity_evidence():
    """markup 泄漏模型明明生成了 token（completion 大）—— 不是容量问题，
    不许压低窗口。"""
    state = _state()
    leak = LLMResponse(
        content="", tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 50_000, "completion_tokens": 900,
               "total_tokens": 50_900},
        protocol_leak=True, leak_kind="markup",
    )
    llm = ScriptedLLM(leak, _answer())
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert summarizer_mod.OBSERVED_CEILING_KEY not in state.hook_state


# ── 两条恢复路径的机械分叉 ──────────────────────────────────────────────────

def test_capacity_void_retries_without_waiting(_no_real_sleep):
    """压缩会触发 = 下一次是**不同的（更小的）**尝试 → 不等待。

    ⚠️ 从**复现**那一次起才不等待。第一次空轮不再算容量证据（一个数据点不是
    天花板，见 `test_one_short_reply_after_a_real_answer_is_not_a_ceiling`），
    所以它照旧退避一次。代价是相信容量问题之前多等一个退避档；换到的是"一次
    短回复不会把窗口砍半"。E2E-5a 现场是 22 轮发散，多这一档换得起。"""
    h = _harness()
    h.max_context_tokens = 262_144
    h.summarizer.enabled = True
    h.summarizer.trigger_type = "token_threshold"
    h.summarizer.trigger_threshold = 0.7

    state = _state()
    messages = [LLMMessage(role="system", content="sys")] + [
        LLMMessage(role="user", content=("研究记录 %d：结果如下。" % i) * 40)
        for i in range(60)]
    # 空响应的 prompt_tokens 由实测 est 反推：学到的上限恰好使压缩翻真 ——
    # 模拟现场"请求就死在略高于压缩救得回来的位置"。
    _, est = summarizer_mod.should_compress(h, messages, 1, state=state)
    eff = int(est * summarizer_mod.effective_calibration(state))
    _pt = int(eff / 0.7) - 10
    llm = ScriptedLLM(_void(_pt), _void(_pt), _answer())
    asyncio.run(run_loop(h, state, messages, llm))
    assert _no_real_sleep == [agent_loop_mod._VOID_RETRY_DELAYS[0]], (
        "容量被复现之后就不该再等待（等待救不了太大的请求），"
        f"实际等了 {_no_real_sleep}"
    )


def test_transient_void_backs_off(_no_real_sleep):
    """上下文本来就小 = 压缩救不了 = provider 瞬态 → 退避等待后原样重放。"""
    state = _state()
    llm = ScriptedLLM(_void(5_000), _void(5_000), _answer())
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert _no_real_sleep == list(agent_loop_mod._VOID_RETRY_DELAYS[:2]), \
        "瞬态型空轮必须按退避序列等待"


# ── E2E-5a 全现场回放 ───────────────────────────────────────────────────────

def test_replay_e2e5a_22_round_divergence_is_now_stationary():
    """现场回放：连续 22 次空响应。修前：+2118 tk/轮 单调发散、永不到压缩线。
    修后：上下文驻定（22 次请求逐字节相同），loop 内预算用尽后干净地交给上层
    （status='void'），messages 一条不多。"""
    state = _state()
    llm = ScriptedLLM(*[_void(174_772)] * 22)
    messages = _msgs()
    n_before = len(messages)
    result = asyncio.run(run_loop(_harness(), state, messages, llm))

    assert result.status == "void"
    assert len(messages) == n_before
    sizes = [sum(len(m.content or "") for m in call) for call in llm.calls]
    assert len(set(sizes)) == 1, f"请求大小必须驻定，实测序列：{sizes}"


def test_length_zero_output_is_exempt_from_void_retry(_no_real_sleep):
    """finish=length 零产出走截断恢复（抬预算），不进空轮重试 —— 同一症状
    两套重试不许叠加（那只会把预算修复推迟 10 分钟）。"""
    state = _state()
    trunc = LLMResponse(
        content="", tool_calls=[], finish_reason="length",
        usage={"prompt_tokens": 9_000, "completion_tokens": 12_000,
               "total_tokens": 21_000},
    )
    llm = ScriptedLLM(trunc, trunc, trunc, _answer())
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert _no_real_sleep == [], "length 腿不许触发空轮退避等待"


# ── 不变量 3：markup 泄漏不是容量证据 ──────────────────────────────────────
#
# 现场（2026-08-17，积算 deepseek-v4-pro，literature 节点）：turn 1 后端把
# tool-call markup 当文本返回（leak_kind='markup'、completion=7）。旧逻辑用
# 自己的 `_ct <= 8` 阈值判容量，7 落在阈值内 → ceiling 被钉在 21335（配置窗口
# 120000 的 1/6）→ 框架据此压缩 + 建议轮换 session → 模型读到"你已到上下文
# 极限"，写了张交接便条就不干活 → 5 次空轮重试（退避累计约 10 分钟）→ run
# incomplete、零交付。**一次后端解析抖动被放大成整轮报废。**
#
# "这算不算真退化"已经有唯一答案（LLMResponse.leak_kind，core.llm 判的）。
# 这里锁的是：agent_loop 去问那个答案，而不是自己再判一遍。


def _markup_leak(prompt_tokens: int, completion_tokens: int = 7) -> LLMResponse:
    """后端把成段 tool-call markup 当文本返回：模型**确实生成了** token。"""
    return LLMResponse(
        content="", tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": prompt_tokens,
               "completion_tokens": completion_tokens,
               "total_tokens": prompt_tokens + completion_tokens},
        protocol_leak=True, leak_kind="markup",
    )


def test_markup_leak_does_not_pin_the_ceiling():
    state = _state()
    llm = ScriptedLLM(_markup_leak(21_335), _answer(prompt_tokens=1000))
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert summarizer_mod.OBSERVED_CEILING_KEY not in state.hook_state, (
        "后端解析抖动被当成了容量证据 —— ceiling 会被钉在一个远低于真实窗口的值上"
    )


def test_a_genuinely_degenerate_void_still_pins_the_ceiling():
    """反向：真退化（leak_kind='empty'）仍然要留下容量证据，别把防线一起删了。"""
    state = _state()
    llm = ScriptedLLM(
        _void(21_335, completion_tokens=1),
        _void(21_335, completion_tokens=1),
        _answer(prompt_tokens=1000),
    )
    asyncio.run(run_loop(_harness(), state, _msgs(), llm))
    assert state.hook_state.get(summarizer_mod.OBSERVED_CEILING_KEY) == 21_335
