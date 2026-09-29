"""输出防火墙（core.llm.sanitize_assistant_content —— 显示/历史层）。

分层（merge PR#125 时确立）：
  - 结构层 core/tool_call_recovery.py：恢复 markup 里的工具调用、剥首尾悬挂碎片、
    判定 protocol_leak（tests/test_tool_call_recovery.py 覆盖）。
  - 显示/历史层（本文件）：scaffold **复读**剥离（recovery 只剥标签不识别被复读
    的正文）+ 正文中间残留控制标记扫尾。

弱端点（自建 vLLM/GPUStack 上 deepseek-v4-pro 实测）把控制标记 / DSML 工具调用
标记 / scaffold 复读吐进 content，污染用户可见回复 + 历史 + 下游 artifact。
进程内不联网。
"""
from __future__ import annotations

import pytest

from core.llm import (
    LLMClient,
    LLMMessage,
    sanitize_assistant_content,
)


# ── sanitize_assistant_content ───────────────────────────────────────────────

def test_normal_content_untouched():
    c, r = sanitize_assistant_content("这是一段正常的中文回复。", None, [])
    assert c == "这是一段正常的中文回复。"
    assert r is None


def test_think_block_still_split():
    """沿用 _split_inline_think：<think>…</think> 拆回 reasoning。"""
    c, r = sanitize_assistant_content("<think>先想想</think>正式回复", None, [])
    assert c == "正式回复"
    assert r == "先想想"


def test_strips_dsml_tool_call_tag():
    """全角竖线的 DSML 工具调用标记（端点没解析成 tool_calls 时泄漏）。"""
    c, _ = sanitize_assistant_content("好的</｜DSML｜tool_calls>", None, [])
    assert "DSML" not in (c or "")
    assert c == "好的"


def test_pure_dsml_becomes_none():
    """整段就是 DSML 标记 → 剥空 → None（让回复契约兜底识别为空）。"""
    c, _ = sanitize_assistant_content("</｜DSML｜tool_calls>", None, [])
    assert c is None


def test_strips_chatml_special_token():
    c, _ = sanitize_assistant_content("回复正文<|im_end|>", None, [])
    assert c == "回复正文"


def test_cuts_scratchpad_echo_when_matches_injection():
    """模型逐字复读注入的 scaffolding，收尾 </scratchpad> 再写正文 → 砍掉复读。"""
    injected = "📝 你的 scratchpad —— 跨轮工作笔记（first turn 引导）\n第一性原理：写给下一轮的自己。"
    leaked = injected + "\n</scratchpad>好，现在给你汇总 KB 内容。"
    c, _ = sanitize_assistant_content(leaked, None, [injected])
    assert c == "好，现在给你汇总 KB 内容。"
    assert "第一性原理" not in c


def test_does_not_cut_scratchpad_without_injection_match():
    """没有注入佐证 → 不砍（避免误伤正文里合法讨论 </scratchpad>）。"""
    text = "我们约定用 </scratchpad> 标签结束笔记，这样更清晰。"
    c, _ = sanitize_assistant_content(text, None, ["完全无关的注入文本"])
    # </scratchpad> 作为孤立标签仍会被 _strip_control_tokens 去掉，但正文保留
    assert "我们约定用" in c and "这样更清晰" in c


def test_code_fence_before_scratchpad_left_alone():
    """正文含代码块讨论标签 → 不做 scaffold 砍切（防误伤）。"""
    injected = "scratchpad 引导文本片段用于匹配 echo 的长句占位"
    text = "示例：\n```\n</scratchpad>\n```\n" + injected + "\n</scratchpad>后缀"
    c, _ = sanitize_assistant_content(text, None, [injected])
    # 有代码围栏 → _cut_scaffold_echo 不动；孤立标签仍被清（但代码块内的保留）
    assert "示例" in c


# ── chat() 集成：结构层（protocol_leak 重试）+ 显示层（echo 剥离）协作 ─────────

def _msg(content, tool_calls=None):
    return {"choices": [{"message": {"content": content,
                                       "tool_calls": tool_calls or []},
                          "finish_reason": "stop"}],
            "usage": {}}


@pytest.mark.asyncio
async def test_protocol_leak_retries_then_clean(monkeypatch):
    """content 只剩 tool-call 碎片（protocol_leak）→ 自动重请求 → 拿到干净正文。
    （重试循环来自 PR#125；这里验证与显示层防火墙叠加后端到端行为不回归。）"""
    seq = [
        _msg("</｜DSML｜tool_calls>"),     # 第一次：协议泄漏
        _msg("这是重问后的干净回复"),          # 第二次：救回
    ]
    calls = {"n": 0}

    async def fake_post(self, payload, *, timeout, max_retries):
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    c = LLMClient(api_key="k", model="m", base_url="http://x")
    resp = await c.chat(
        [LLMMessage(role="user", content="读 KB")],
        tools=[{"type": "function", "function": {"name": "search_kb"}}],
    )
    assert calls["n"] == 2                      # 泄漏触发了一次重请求
    assert resp.content == "这是重问后的干净回复"


@pytest.mark.asyncio
async def test_chat_strips_scaffold_echo_with_recent_system_evidence(monkeypatch):
    """chat() 末段显示层防火墙：以 hook 注入的 system 消息为佐证剥 scaffold 复读。

    消息布局照真实路径：`messages[0]` 是 harness 自己的 system prompt，hook
    注入在其后追加。证据池**排除 messages[0]** —— 实测（2026-08-07）把 14K 的
    orchestrator system prompt 放进证据池后，正常正文的 containment 从 0.30
    飙到 0.67，超过 0.45 判据直接误伤。体量本身就是稀释源。
    """
    injected = ("📝 你的 scratchpad —— 跨轮工作笔记（first turn 引导）"
                "第一性原理：写给下一轮的自己，防止每轮从零推理浪费 token。")
    leaked = injected + "\n</scratchpad>好，汇总如下。"

    async def fake_post(self, payload, *, timeout, max_retries):
        return _msg(leaked)

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    c = LLMClient(api_key="k", model="m", base_url="http://x")
    resp = await c.chat([
        LLMMessage(role="system", content="harness 自己的 system prompt（不作证据）"),
        LLMMessage(role="user", content="KB 里有什么？"),
        LLMMessage(role="system", content=injected),   # hook 注入 = 证据
    ])
    assert resp.content == "好，汇总如下。"


@pytest.mark.asyncio
async def test_normal_tool_call_no_retry(monkeypatch):
    """正常返回 tool_calls → 不触发重请求。"""
    calls = {"n": 0}

    async def fake_post(self, payload, *, timeout, max_retries):
        calls["n"] += 1
        return _msg(None, tool_calls=[{"id": "1", "type": "function",
                                        "function": {"name": "search_kb",
                                                     "arguments": "{}"}}])

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    c = LLMClient(api_key="k", model="m", base_url="http://x")
    resp = await c.chat(
        [LLMMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "search_kb"}}],
    )
    assert calls["n"] == 1
    assert resp.tool_calls and resp.tool_calls[0]["function"]["name"] == "search_kb"
