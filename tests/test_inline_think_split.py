"""_split_inline_think：思维链泄漏进 content 的拆分（2026-07-08 GPUStack 实测）。

GPUStack/vLLM 自建端点不把 reasoning 拆进独立字段——`reasoning` 为 null、
思考文本混在 content 里（`<think>…</think>正文` 或残缺的 `…</think>正文`）。
不拆的话思维链泄漏进用户可见回复和下游 artifact（chat.py 实测回复开头一大段
"（框架 hook 会自动 inject…）" 复读 + `</think>` 残渣）。
"""
from __future__ import annotations

from core.llm import _split_inline_think


def test_no_think_marker_passthrough():
    c, r = _split_inline_think("普通回复", None)
    assert c == "普通回复" and r is None


def test_none_content_passthrough():
    c, r = _split_inline_think(None, "already reasoning")
    assert c is None and r == "already reasoning"


def test_full_think_block_split():
    c, r = _split_inline_think("<think>我先想想这个问题</think>这是正式回复", None)
    assert c == "这是正式回复"
    assert r == "我先想想这个问题"


def test_orphan_close_tag_split():
    """实测最常见形态：无开标签，content 以思考文本开头、</think> 后才是正文。"""
    c, r = _split_inline_think("用户选了 PROCEED，我该总结了</think>好，现在汇报进展。", None)
    assert c == "好，现在汇报进展。"
    assert r == "用户选了 PROCEED，我该总结了"


def test_leading_close_tag_only():
    c, r = _split_inline_think("</think>进步了！这次产出了 4 件 artifact", None)
    assert c == "进步了！这次产出了 4 件 artifact"
    assert r is None    # 思考部分为空


def test_merges_with_existing_reasoning():
    c, r = _split_inline_think("<think>补充想法</think>正文", "已有思考")
    assert c == "正文"
    assert "已有思考" in r and "补充想法" in r


def test_code_fence_before_marker_left_alone():
    """正文里带代码块讨论 think 标签本身 → 不拆（防误伤）。"""
    content = "示例代码：\n```\nprint('</think>')\n```\n以上就是用法</think>后缀"
    c, r = _split_inline_think(content, None)
    assert c == content and r is None
