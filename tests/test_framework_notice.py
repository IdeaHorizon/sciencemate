"""框架中途发声 = user 角色 + framework-notice 信封。

背景（2026-08-17 积算 deepseek-v4-pro 实测，真实 hypothesis harness + 4 条真实
hook 注入，两轮共 14 次）：中段 role=system 注入 → 13/14 的回复以复述注入文本
的尾巴开头（"没有它，下一轮 Analysis 会…"、"别忘。"），平均输出 191 token；
同内容改 user 角色 → 0/14 复述，平均 61 token。

这些测试守的是**接缝**，不是 happy path —— 改角色最容易出事的不是注入本身，
而是那些**以角色为判据**的下游（复读取证、定向去重）在改造后静默失效。
"""
from __future__ import annotations

import asyncio

from core.llm import (
    FRAMEWORK_NOTICE_OPEN,
    LLMMessage,
    framework_notice,
    framework_notice_body,
    is_framework_notice,
    sanitize_assistant_content,
)
from core.loop_hooks import LoopHook, run_on_turn_end, run_on_turn_start


def _run(coro):
    return asyncio.run(coro)


# ── 信封本身 ────────────────────────────────────────────────────────────
def test_notice_is_user_role_not_system():
    m = framework_notice("白板当前内容：（空）")
    assert m.role == "user", "中段 system 消息正是复述的成因，不许再用"
    assert m.content.lstrip().startswith(FRAMEWORK_NOTICE_OPEN)


def test_notice_declares_it_is_not_the_human():
    """换成 user 角色之后，框架的话和人类的话同角色 —— 归属必须机械可辨。"""
    m = framework_notice("局面：fresh")
    assert "不是用户发言" in m.content
    assert is_framework_notice(m)
    assert not is_framework_notice(LLMMessage(role="user", content="帮我研究英国饮食"))


def test_body_roundtrip_strips_shell_only():
    body = "🗺️ **项目现状**\n第二行"
    assert framework_notice_body(framework_notice(body)) == body


def test_body_of_plain_message_is_itself():
    """非信封消息原样返回 —— 下游可以只写一条判据，不必分支。"""
    m = LLMMessage(role="system", content="截断恢复提示")
    assert framework_notice_body(m) == "截断恢复提示"


def test_wrapping_is_idempotent():
    once = framework_notice("x")
    twice = _run(run_on_turn_start([_hook_returning(once)], ctx=None))
    assert twice[0].content.count(FRAMEWORK_NOTICE_OPEN) == 1, "别把信封套两层"


# ── 咽喉：所有 hook 自动被覆盖（扫盘，不是名单）────────────────────────
def _hook_returning(msg):
    return LoopHook(name="t", on_turn_start=lambda ctx: [msg],
                    on_turn_end=lambda ctx: [msg])


def test_hook_returning_system_message_is_converted():
    """hook 作者照旧写 role='system'，咽喉负责改造 —— 新 hook 自动被覆盖。"""
    raw = LLMMessage(role="system", content="📍 局面（框架机械判定：fresh）")
    for runner in (run_on_turn_start, run_on_turn_end):
        out = _run(runner([_hook_returning(raw)], ctx=None))
        assert len(out) == 1
        assert out[0].role == "user"
        assert framework_notice_body(out[0]) == raw.content


def test_structural_messages_pass_through_untouched():
    """带 tool_calls / tool 结果不是'框架说话'，不许被包。"""
    tool_msg = LLMMessage(role="tool", tool_call_id="c1", name="list_files",
                          content='{"status":"success"}')
    out = _run(run_on_turn_start([_hook_returning(tool_msg)], ctx=None))
    assert out[0] is tool_msg


# ── 接缝 1：复读剥离的取证不能因为改角色而失效 ──────────────────────────
def test_scaffold_stripping_still_finds_evidence_after_role_change():
    """这是本次改造最危险的地方：llm.py 原本按 role=='system' 取证。

    若取证判据没跟着改，防线还在、只是永远拿不到证据 —— 全绿的静默失效。
    """
    injected_body = "没有它，下一轮 Analysis 会从零开始。\n\n现在开始。"
    messages = [
        LLMMessage(role="system", content="（harness 主 system prompt，体量极大）"),
        LLMMessage(role="user", content="帮我研究英国饮食文化"),
        framework_notice(injected_body),
    ]
    evidence = [
        framework_notice_body(m) for m in messages[1:]
        if m.content and (is_framework_notice(m) or m.role == "system")
    ]
    assert injected_body in evidence, "信封形态必须能被取证"
    assert "帮我研究英国饮食文化" not in evidence, "人类原话不是 scaffold"

    parroted = injected_body + "\n\n我先读一下项目记忆。"
    clean, _ = sanitize_assistant_content(parroted, None, evidence)
    assert "没有它，下一轮 Analysis 会从零开始。" not in (clean or ""), \
        "取证失效的症状就是这里原样穿过去"
    assert "我先读一下项目记忆。" in (clean or ""), "正文不许被误剥"


def test_cleaner_cannot_remove_short_parroted_lines():
    """为什么必须在发声侧修，而不是继续加强清洗侧。

    清洗器按行扫且**短行不作证据**（剥了会把复读段从中间截开）。而真机实测的
    复述恰恰大量是短句 —— "别忘。"、"现在开始。"、"不建 v1，下一轮 Analysis
    会读不到状态。"。这些事后擦不掉，token 也已经花掉了。
    """
    evidence = ["别忘。\n先读白板（下面），再开始。"]
    clean, _ = sanitize_assistant_content("别忘。\n\n我开始干活。", None, evidence)
    assert "别忘。" in (clean or ""), \
        "若这条哪天变绿，说明清洗器改了策略，本测试的论据需要重估"


def test_evidence_still_covers_plain_system_messages():
    """框架少数控制消息（截断恢复等）仍是中段 system —— 两种形态都要收。"""
    ctrl = "⚠️ 你上一轮的输出撞到 token 上限被截断。"
    messages = [
        LLMMessage(role="system", content="主 prompt"),
        LLMMessage(role="system", content=ctrl),
    ]
    evidence = [
        framework_notice_body(m) for m in messages[1:]
        if m.content and (is_framework_notice(m) or m.role == "system")
    ]
    assert ctrl in evidence


# ── 接缝 2：定向快照去重不能因为改角色而失效（PR#351 累积 bug 复发面）──
def test_orientation_dedup_matches_enveloped_snapshot():
    from core.loop_hooks_builtin import ORIENTATION_PREFIX

    old = framework_notice(ORIENTATION_PREFIX + "（上一份，已过期）")
    keep = framework_notice("📝 你的白板")
    msgs = [old, keep]
    survivors = [m for m in msgs
                 if not framework_notice_body(m).startswith(ORIENTATION_PREFIX)]
    assert survivors == [keep], "旧定向快照没被去掉 = 每压一次多一份"
