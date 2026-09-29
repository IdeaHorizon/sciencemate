"""protocol_leak 细分为 empty（近乎空响应）vs markup（真 markup 泄漏）。

背景（jicq 2026-07-24 实测事故）：一条 completion_tokens=1 的近乎空响应，
那 1 个 token 恰好像 tool-call 碎片开头，被 _strip_dangling 判成碎片 →
protocol_leak=True → 上层吐"后端把 tool-call markup 当普通文本返回、检查
endpoint 解析配置"。但真实根因是模型在 108k 上下文下退化、几乎没生成，
把人往错误方向带。两者重试策略相同但归因/提示必须分开。

transcript 实测数据：同一 run 里 3 条 completion=1 的空响应（prompt 57k/67k/108k），
以及一段 38 轮逐字复读——都是大上下文退化，不是 markup 格式问题。
"""
from __future__ import annotations

from core.llm import _classify_leak_kind, _parse_chat_response


class _Rec:
    """recover_tool_calls 返回的最小替身（只用到 stripped）。"""
    def __init__(self, stripped):
        self.stripped = stripped


# ── _classify_leak_kind：completion_tokens 主信号 ────────────────────────────


def test_completion_1_is_empty():
    """jicq 实测那条：completion_tokens=1 → empty（不是 markup）。"""
    assert _classify_leak_kind(_Rec(["<｜"]), {"completion_tokens": 1}) == "empty"


def test_completion_0_or_missing_value_is_empty():
    assert _classify_leak_kind(_Rec(["<｜"]), {"completion_tokens": 0}) == "empty"


def test_substantial_completion_is_markup():
    """真 markup leak：模型生成了成段 markup 文本 → completion 不会小。"""
    assert _classify_leak_kind(_Rec(["<invoke ...>"]), {"completion_tokens": 80}) == "markup"


def test_boundary_at_threshold():
    assert _classify_leak_kind(_Rec(["x"]), {"completion_tokens": 3}) == "empty"
    assert _classify_leak_kind(_Rec(["x"]), {"completion_tokens": 4}) == "markup"


# ── usage 缺 completion_tokens 时退到碎片长度次信号 ──────────────────────────


def test_no_usage_short_fragment_is_empty():
    """usage 没报 completion_tokens：剥掉的碎片很短 → 判 empty。"""
    assert _classify_leak_kind(_Rec(["</s>"]), {}) == "empty"


def test_no_usage_long_markup_is_markup():
    """usage 缺失但剥掉了成段 markup → markup。"""
    big = "<｜DSML｜invoke name='search'><｜DSML｜parameter name='q'>...</...>"
    assert _classify_leak_kind(_Rec([big]), {}) == "markup"


def test_no_usage_no_stripped_defaults_markup():
    """两个信号都取不到 → 保守判 markup（沿用旧提示，不误报退化）。"""
    assert _classify_leak_kind(_Rec([]), {}) == "markup"


# ── _parse_chat_response 端到端：leak_kind 只在 leak 时置位 ──────────────────


def _resp(content, usage, tool_calls=None):
    data = {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []},
                     "finish_reason": "stop"}],
        "usage": usage,
    }
    return _parse_chat_response(data)


def test_parse_empty_response_leak_kind_empty():
    # 单个 DSML 碎片 token + completion=1 → protocol_leak + leak_kind=empty
    r = _resp("<｜DSML｜tool_calls>", {"completion_tokens": 1, "prompt_tokens": 108000})
    assert r.protocol_leak is True
    assert r.leak_kind == "empty"


def test_parse_normal_answer_no_leak_kind():
    r = _resp("这是一段正常的最终回答，没有任何 markup。", {"completion_tokens": 20})
    assert r.protocol_leak is False
    assert r.leak_kind is None


def test_parse_markup_leak_kind_markup():
    # 成段 DSML 碎片（残缺，dialect 抽不出）+ 大 completion → markup
    frag = "</｜DSML｜invoke><｜DSML｜parameter name='x'>残缺没闭合"
    r = _resp(frag, {"completion_tokens": 60})
    if r.protocol_leak:                      # 视 _strip_dangling 是否认定纯碎片
        assert r.leak_kind == "markup"
