"""P2 增长门 + P3 结构化账本的机械保证。

P2：上次压缩后 est 没实质增长就不再压 —— v20 的死循环（压不动 → emergency
每轮强制再压 → 缓存全失效直到空响应）在判据层面被封死。
P3：账本固定四 section；口水机械剥除；缺 section 给一次指名纠偏，仍缺则
fail-loud 回退 —— 结构坏的账本比没有账本更糟。
"""
from __future__ import annotations

import asyncio

import pytest

from core import summarizer as sm
from core.harness import SummarizerConfig
from core.llm import LLMMessage


class _H:
    node_type = "_test"
    max_context_tokens = 10_000
    summarizer = SummarizerConfig(trigger_threshold=0.5)


class _State:
    def __init__(self):
        self.hook_state: dict = {}
        self.tokens_used = 0
        self.transcript: list = []

    def append_transcript(self, event, **kw):
        self.transcript.append((event, kw))

    def save_artifact(self, **kw):
        return {"id": "x"}


def _big_msgs(n_turns: int = 8, chars: int = 9_000) -> list[LLMMessage]:
    out = [LLMMessage(role="system", content="SYS"), LLMMessage(role="user", content="go")]
    for i in range(n_turns):
        out.append(LLMMessage(role="assistant", content=f"想法 {i} " + "y" * chars))
        out.append(LLMMessage(role="user", content=f"回 {i}"))
    return out


# ── P2 ───────────────────────────────────────────────────────────────────
def test_no_recompress_until_context_actually_grows() -> None:
    state = _State()
    msgs = _big_msgs()
    should, est = sm.should_compress(_H(), msgs, 5, state=state)
    assert should, "前置：超阈值要触发"

    # 模拟压缩完成：记录压后 est
    state.hook_state[sm._LAST_COMPRESS_EST_KEY] = est - 100  # 压缩几乎没省
    should2, _ = sm.should_compress(_H(), msgs, 6, state=state)
    assert not should2, "est 没增长，再压必然同样结果 —— 不许压"

    # 追加了实质内容之后允许再压
    msgs2 = msgs + [LLMMessage(role="assistant", content="新工作 " + "z" * 20_000)]
    should3, _ = sm.should_compress(_H(), msgs2, 7, state=state)
    assert should3


def test_growth_gate_applies_even_at_emergency_level() -> None:
    """emergency 无视轮次冷却，但不能无视"压缩已证明无能为力"。"""
    state = _State()
    msgs = _big_msgs(n_turns=20)  # 远超 emergency
    state.hook_state[sm._LAST_COMPRESS_EST_KEY] = sm.estimate_tokens(msgs)
    should, _ = sm.should_compress(_H(), msgs, 5, state=state)
    assert not should


def test_run_summarizer_records_est_after() -> None:
    state = _State()
    msgs = _big_msgs()

    class _Hd(_H):
        summarizer = SummarizerConfig(strategy="drop_tool_results")

    out = asyncio.run(sm.run_summarizer(_Hd(), state, msgs, llm=None,
                                        turn=5, estimated_tokens=sm.estimate_tokens(msgs)))
    assert isinstance(state.hook_state.get(sm._LAST_COMPRESS_EST_KEY), int)


# ── P3 ───────────────────────────────────────────────────────────────────
_GOOD_LEDGER = (
    "## 目标\n跑通实验\n\n## 已完成\n- 模拟完成（run 1786214798-3781e0）\n\n"
    "## 未决与下一步\n- 出图\n\n## 决策与理由\n- 低温段拟合，因伪拐点\n"
)


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None):
        self.calls += 1
        class R:
            content = self.replies.pop(0)
            usage = {}
            finish_reason = "stop"
        return R()


def _ledger_run(llm, msgs=None):
    msgs = msgs or _big_msgs()
    ctx = sm.SummarizerContext(harness=_H(), state=_State(), messages=msgs,
                               estimated_tokens=sm.estimate_tokens(msgs),
                               llm=llm, turn=5)
    return asyncio.run(sm._strategy_llm(ctx))


def test_chatter_is_stripped_mechanically() -> None:
    llm = _FakeLLM(["好的，这是合并后的压缩摘要。\n\n" + _GOOD_LEDGER])
    out = _ledger_run(llm)
    notice = next(m for m in out if sm._COMPRESSION_NOTICE_MARKER in (m.content or ""))
    assert "好的" not in notice.content, "口水必须机械剥掉，不能指望模型自觉"
    assert "## 目标" in notice.content


def test_missing_section_gets_one_named_retry() -> None:
    bad = "## 目标\nx\n## 已完成\n- y（artifact:a__b）\n"   # 缺两个 section
    llm = _FakeLLM([bad, _GOOD_LEDGER])
    out = _ledger_run(llm)
    assert llm.calls == 2, "缺 section 要给一次指名纠偏"
    notice = next(m for m in out if sm._COMPRESSION_NOTICE_MARKER in (m.content or ""))
    assert "## 决策与理由" in notice.content


def test_double_failure_falls_back_loudly_not_garbage() -> None:
    llm = _FakeLLM(["没有结构的散文", "还是没有结构"])
    out = _ledger_run(llm)
    assert llm.calls == 2
    # 回退到 drop：不产出坏账本（坏账本会以权威口吻继承到之后每一轮）
    texts = "\n".join(m.content or "" for m in out)
    assert "## 目标" not in texts
    assert "drop_tool_results" in texts


def test_all_sections_survive_roundtrip() -> None:
    llm = _FakeLLM([_GOOD_LEDGER])
    out = _ledger_run(llm)
    notice = next(m for m in out if sm._COMPRESSION_NOTICE_MARKER in (m.content or ""))
    for h in sm._LEDGER_SECTIONS:
        assert h in notice.content


def test_trigger_and_strategy_use_the_same_effective_formula() -> None:
    """v22 首跑 5 分钟抓到的缝：should_compress 按 (est+schema)×calib 说该压，
    escalate 内部按裸 est×calib 说没超 —— 空转（37,622 → 37,622）。判据同源后
    这个组合必然调 llm（或清出东西），不许再出现"triggered 但 no-op"。"""
    state = _State()
    # schema 份额把有效值推过阈值，裸 est 不过
    state.hook_state[sm._TOOL_SCHEMA_TOKENS_KEY] = 4_000
    msgs = _big_msgs(n_turns=6, chars=1_500)   # 裸 est 明显低于阈值

    class _Hs(_H):
        max_context_tokens = 6_000
        summarizer = SummarizerConfig(trigger_threshold=0.7)

    should, est = sm.should_compress(_Hs(), msgs, 5, state=state)
    assert should, "前置：有效值（含 schema）超阈值"

    llm = _FakeLLM([_GOOD_LEDGER])
    ctx = sm.SummarizerContext(harness=_Hs(), state=state, messages=msgs,
                               estimated_tokens=est, llm=llm, turn=5)
    out = asyncio.run(sm._strategy_escalate(ctx))
    changed = [1 for a, b in zip(msgs, out) if (a.content or "") != (b.content or "")]
    assert llm.calls >= 1 or len(out) != len(msgs) or changed, \
        "triggered 之后必须有实际动作，不许两把尺子空转"
