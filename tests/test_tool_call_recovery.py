"""跨 backend 工具调用 / 内容修复层（core.tool_call_recovery）。

背景：自建 GPUStack deepseek-v4-pro 端点偶发把 tool-call / reasoning markup 漏进
message.content 而不结构化成 tool_calls。实测 orchestrator session 18 条回复里
11 条 content 前缀挂 dangling `</scratchpad>` / `</｜DSML｜tool_calls>`，个别整条
就是一个残缺碎片、orchestrator 把它当最终回答显示给用户（用户看到
`</write_scratchpad>` 以为坏了）。

这些测试锁死三件事：恢复结构化 tool_calls（工具真执行）、清洗残留碎片、
判定 protocol_leak；并且**绝不误伤**正常回答。
"""
from __future__ import annotations

import pytest

from core.tool_call_recovery import (
    recover_tool_calls,
    XmlInvokeDialect,
    HermesDialect,
    DeepSeekNativeDialect,
    DEFAULT_DIALECTS,
)


def _names(tool_calls):
    return [c["function"]["name"] for c in tool_calls]


def _args(tool_calls):
    return [c["function"]["arguments"] for c in tool_calls]


# ── 真实抓到的泄漏样本（来自 orchestrator__test-projects 真实 transcript）──────

def test_real_dangling_scratchpad_prefix_stripped_keeps_tool_calls():
    """高频真实场景：content 前缀挂 </scratchpad>，正文和已解析的 tool_calls 都在。"""
    existing = [{"id": "x", "type": "function",
                 "function": {"name": "search_kb", "arguments": "{}"}}]
    r = recover_tool_calls("</scratchpad>好，让我从 KB 里拉一些概览信息给你看。\n\n", existing)
    # 前缀的 dangling </scratchpad> 被剥掉，正文（含尾部换行）保留
    assert r.content.startswith("好，让我从 KB 里拉一些概览信息给你看。")
    assert not r.content.startswith("</scratchpad>")
    assert _names(r.tool_calls) == ["search_kb"]        # 原 tool_calls 保留
    assert r.protocol_leak is False
    assert "</scratchpad>" in r.stripped


def test_real_pure_dsml_fragment_is_protocol_leak():
    """最糟真实场景：content 就是一个 </｜DSML｜tool_calls> 碎片、无 tool_calls。"""
    r = recover_tool_calls("</｜DSML｜tool_calls>", [])
    assert r.tool_calls == []
    assert r.content is None
    assert r.protocol_leak is True          # 上层据此重试/兜底，绝不当答案显示


def test_real_reasoning_fragment_with_tool_calls_not_leak():
    """</scratchpad>\\n\\n\\n + 已有 tool_calls：清成空 content，但不是协议失败。"""
    existing = [{"id": "y", "type": "function",
                 "function": {"name": "list_proposals", "arguments": "{}"}}]
    r = recover_tool_calls("</scratchpad>\n\n\n", existing)
    assert r.content is None
    assert _names(r.tool_calls) == ["list_proposals"]
    assert r.protocol_leak is False


# ── 恢复：完整 markup 漏进 content → 结构化 tool_calls（工具真执行）──────────

def test_recover_full_dsml_invoke_with_params():
    content = ('前言 <｜DSML｜invoke name="write_scratchpad">'
               '<｜DSML｜parameter name="note">当前在调研硅碳负极</｜DSML｜parameter>'
               '</｜DSML｜invoke> 后言')
    r = recover_tool_calls(content, [])
    assert r.recovered is True
    assert r.dialect == "xml_invoke"
    assert _names(r.tool_calls) == ["write_scratchpad"]
    assert '"note": "当前在调研硅碳负极"' in _args(r.tool_calls)[0]
    assert r.content == "前言  后言"       # markup 被剜掉，正文保留
    assert r.protocol_leak is False


def test_recover_plain_anthropic_invoke_no_dsml_prefix():
    r = recover_tool_calls('<invoke name="kb_overview"></invoke>', [])
    assert _names(r.tool_calls) == ["kb_overview"]
    assert r.recovered is True


def test_recover_multiple_invokes():
    content = ('<invoke name="a"><parameter name="p">1</parameter></invoke>'
               '<invoke name="b"><parameter name="q">2</parameter></invoke>')
    r = recover_tool_calls(content, [])
    assert _names(r.tool_calls) == ["a", "b"]
    assert '"p": "1"' in _args(r.tool_calls)[0]
    assert '"q": "2"' in _args(r.tool_calls)[1]


def test_recover_hermes_tool_call():
    r = recover_tool_calls(
        '<tool_call>{"name": "search_kb", "arguments": {"query": "硅碳"}}</tool_call>', [])
    assert _names(r.tool_calls) == ["search_kb"]
    assert '"query": "硅碳"' in _args(r.tool_calls)[0]
    assert r.dialect == "hermes"


def test_recover_hermes_ignores_bad_json():
    r = recover_tool_calls("<tool_call>not json</tool_call>", [])
    assert r.tool_calls == []          # 解析不了就不恢复，不抛异常


def test_recover_deepseek_native():
    content = ('<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>'
               'write_scratchpad\n```json\n{"note": "hi"}\n```'
               '<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
    r = recover_tool_calls(content, [])
    assert _names(r.tool_calls) == ["write_scratchpad"]
    assert '"note": "hi"' in _args(r.tool_calls)[0]
    assert r.dialect == "deepseek_native"


def test_existing_tool_calls_not_overwritten_by_recovery():
    """后端已给结构化 tool_calls → 不再从 content 恢复（避免重复执行）。"""
    existing = [{"id": "z", "type": "function",
                 "function": {"name": "already_here", "arguments": "{}"}}]
    r = recover_tool_calls('<invoke name="should_not_recover"></invoke>', existing)
    assert _names(r.tool_calls) == ["already_here"]
    assert r.recovered is False


# ── 绝不误伤正常内容 ─────────────────────────────────────────────────────────

def test_normal_answer_untouched():
    txt = "这是一个正常回答，包含数学 a < b 和 b > c 的比较。"
    r = recover_tool_calls(txt, [])
    assert r.content == txt
    assert r.tool_calls == []
    assert r.protocol_leak is False
    assert r.stripped == []


def test_prose_mentioning_tag_mid_text_untouched():
    """正文中间提到 </think>/</scratchpad> 不该被剥（只清首尾 dangling）。"""
    txt = "我们约定用 </scratchpad> 当分隔符，</think> 当思维链边界。"
    r = recover_tool_calls(txt, [])
    assert r.content == txt
    assert r.stripped == []


def test_unknown_leading_tag_not_stripped():
    """首部是个非 markup 的普通标签（如 HTML <b>）→ 不动它。"""
    txt = "<b>加粗开头</b>的正常内容"
    r = recover_tool_calls(txt, [])
    assert r.content == txt


def test_none_content_passthrough():
    r = recover_tool_calls(None, [{"id": "a", "type": "function",
                                   "function": {"name": "t", "arguments": "{}"}}])
    assert r.content is None
    assert _names(r.tool_calls) == ["t"]
    assert r.protocol_leak is False


def test_empty_content_no_leak():
    r = recover_tool_calls("", [])
    assert r.protocol_leak is False       # 空 ≠ 协议失败


# ── 扩展性：可传自定义 dialect 列表 ─────────────────────────────────────────

def test_custom_dialect_list_is_honored():
    """只给 Hermes dialect → DSML markup 不被恢复（证明 registry 可插拔）。"""
    dsml = '<invoke name="x"></invoke>'
    r = recover_tool_calls(dsml, [], dialects=[HermesDialect()])
    assert r.tool_calls == []             # 没有 xml dialect 就不认


def test_default_dialects_cover_all_three_families():
    names = {d.name for d in DEFAULT_DIALECTS}
    assert names == {"xml_invoke", "hermes", "deepseek_native"}


# ── 通过 llm.py 的解析入口集成验证 ──────────────────────────────────────────

def test_parse_chat_response_recovers_and_flags():
    from core.llm import _parse_chat_response
    # 纯碎片 → protocol_leak
    d1 = {"choices": [{"message": {"content": "</｜DSML｜tool_calls>", "tool_calls": []},
                        "finish_reason": "stop"}], "usage": {}}
    r1 = _parse_chat_response(d1)
    assert r1.protocol_leak is True and r1.content is None and r1.tool_calls == []
    # 完整 markup → 恢复
    d2 = {"choices": [{"message": {"content": '<｜DSML｜invoke name="kb_overview"></｜DSML｜invoke>',
                                    "tool_calls": []}, "finish_reason": "stop"}], "usage": {}}
    r2 = _parse_chat_response(d2)
    assert _names(r2.tool_calls) == ["kb_overview"] and r2.protocol_leak is False
    # 正常
    d3 = {"choices": [{"message": {"content": "正常回答。", "tool_calls": []},
                        "finish_reason": "stop"}], "usage": {}}
    r3 = _parse_chat_response(d3)
    assert r3.content == "正常回答。" and r3.protocol_leak is False


@pytest.mark.asyncio
async def test_chat_retries_on_protocol_leak_then_succeeds(monkeypatch):
    """llm.chat 遇 protocol_leak 自动重请求；第二次返回正常 → 用户不会看到碎片。"""
    from core.llm import LLMClient
    from unittest.mock import AsyncMock

    responses = [
        {"choices": [{"message": {"content": "</｜DSML｜tool_calls>", "tool_calls": []},
                       "finish_reason": "stop"}], "usage": {}},
        {"choices": [{"message": {"content": "这次正常回答了。", "tool_calls": []},
                       "finish_reason": "stop"}], "usage": {}},
    ]
    call_count = [0]

    async def fake_post(payload, *, timeout, max_retries):
        i = call_count[0]
        call_count[0] += 1
        return responses[min(i, len(responses) - 1)]

    c = LLMClient(api_key="k", model="m", base_url="http://x")
    monkeypatch.setattr(c, "_post_with_retry", fake_post)
    resp = await c.chat([], _no_cache=True)
    assert call_count[0] == 2               # 泄漏一次 → 重请求一次
    assert resp.content == "这次正常回答了。"
    assert resp.protocol_leak is False


@pytest.mark.asyncio
async def test_chat_gives_up_after_max_leak_retries(monkeypatch):
    """持续泄漏 → 重请求到上限后返回 protocol_leak=True + 空 content（上层显示明确提示，不显示碎片）。"""
    from core.llm import LLMClient, _PROTOCOL_LEAK_RETRIES

    leak = {"choices": [{"message": {"content": "</｜DSML｜tool_calls>", "tool_calls": []},
                          "finish_reason": "stop"}], "usage": {}}
    call_count = [0]

    async def fake_post(payload, *, timeout, max_retries):
        call_count[0] += 1
        return leak

    c = LLMClient(api_key="k", model="m", base_url="http://x")
    monkeypatch.setattr(c, "_post_with_retry", fake_post)
    resp = await c.chat([], _no_cache=True)
    assert call_count[0] == _PROTOCOL_LEAK_RETRIES + 1   # 首次 + 重试上限
    assert resp.protocol_leak is True
    assert resp.content is None
