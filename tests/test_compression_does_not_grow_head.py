"""压缩必须**收敛**：连续压 N 次，不可压的 head 不能越来越大。

E2E v20 orchestrator 实测：head 40 条里 33 条是历次压缩留下的 notice
（约 14,500 tokens），每压一次 prompt 反而涨约 1,000 —— 197,671 → 198,942 →
200,226 → 201,113 → 202,527，直到模型返回空响应、run 报 context_ceiling。
压缩机制自己在制造它本该治的症状。
"""
from __future__ import annotations

import asyncio

import pytest

from core import summarizer as sm
from core.llm import LLMMessage


def _conversation(n_turns: int) -> list[LLMMessage]:
    msgs = [
        LLMMessage(role="system", content="SYSTEM PROMPT " + "x" * 400),
        LLMMessage(role="user", content="开始干活"),
    ]
    for i in range(n_turns):
        msgs.append(LLMMessage(role="assistant", content=f"第 {i} 轮想法 " + "y" * 300))
        msgs.append(LLMMessage(role="user", content=f"第 {i} 轮反馈 " + "z" * 300))
    return msgs


def _head_of(msgs: list[LLMMessage]) -> list[LLMMessage]:
    head, _, _ = sm.split_for_compression(msgs, 3)
    return head


def _notices(msgs: list[LLMMessage]) -> list[LLMMessage]:
    return [m for m in msgs if sm._is_compression_notice(m)]


def test_repeated_compression_keeps_exactly_one_notice() -> None:
    msgs = _conversation(12)
    for turn in range(1, 8):
        # 直接走装配逻辑：把 head/middle/tail 重组一次，模拟第 N 次压缩
        head, middle, tail = sm.split_for_compression(msgs, 3)
        notice = LLMMessage(
            role="system",
            content=f"{sm._COMPRESSION_NOTICE_MARKER}（turn {turn}）：把中间 "
                    f"{len(middle)} 条 messages 压成下面这段："
                    f"\n\n{sm._COMPRESSION_SUMMARY_HEADER}\n摘要 {turn}\n",
        )
        msgs = [*sm._head_without_superseded_notices(head), notice, *tail]
        msgs.append(LLMMessage(role="assistant", content="继续 " + "y" * 300))
        msgs.append(LLMMessage(role="user", content="再来 " + "z" * 300))

    assert len(_notices(msgs)) == 1, "旧摘要必须被新摘要取代，而不是并排堆着"


def test_head_does_not_grow_across_compressions() -> None:
    msgs = _conversation(12)
    sizes = []
    for turn in range(1, 8):
        head, middle, tail = sm.split_for_compression(msgs, 3)
        notice = LLMMessage(
            role="system",
            content=f"{sm._COMPRESSION_NOTICE_MARKER}（turn {turn}）：摘要"
                    f"\n\n{sm._COMPRESSION_SUMMARY_HEADER}\n内容 {turn}\n",
        )
        msgs = [*sm._head_without_superseded_notices(head), notice, *tail]
        msgs.append(LLMMessage(role="assistant", content="继续 " + "y" * 300))
        msgs.append(LLMMessage(role="user", content="再来 " + "z" * 300))
        sizes.append(sm.estimate_tokens(_head_of(msgs)))

    assert sizes[-1] <= sizes[0] + 50, f"head 在膨胀：{sizes}"


def test_a_notice_is_recognised_and_a_normal_system_message_is_not() -> None:
    assert sm._is_compression_notice(
        LLMMessage(role="system", content=f"{sm._COMPRESSION_NOTICE_MARKER}（turn 3）：…")
    )
    # 真正的 system prompt / 定向注入不能被误删 —— 它们不是"被取代的摘要"。
    assert not sm._is_compression_notice(
        LLMMessage(role="system", content="🗺️ 项目现状（机械扫描，开局一次）")
    )
    # 用户自己打出这几个字（粘日志、讨论压缩机制）不该被当成框架的 notice：
    # 判据是 marker **加上框架身份**，不是 marker 单独说了算。
    assert not sm._is_compression_notice(LLMMessage(role="user", content="📦 历史压缩"))


def test_system_prompt_and_orientation_survive_compression() -> None:
    msgs = _conversation(12)
    msgs.insert(1, LLMMessage(role="system", content="🗺️ 项目现状：不能被删"))
    head, _, tail = sm.split_for_compression(msgs, 3)
    kept = sm._head_without_superseded_notices(head)
    assert any("SYSTEM PROMPT" in (m.content or "") for m in kept)
    assert any("项目现状" in (m.content or "") for m in kept)
