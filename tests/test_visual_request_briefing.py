"""要求别人引用一个它无从枚举的标识符，本来就只能靠猜（P5 实测）。

render_figure 用 request_id 对账，而 id = request_id 或 slug(intent) —— 调用方没
显式传时，它是一个节点**在输入里看不到的字符串**。实测节点连猜 8 次
（'visual_requests[0]' / '0' / 'request_0' / '' / '[0]' …）全部被拒，40 轮烧完。
"""
from __future__ import annotations

import pytest

from core.state import State
from nodes.postprocess.hooks import _visual_request_briefing


class _Ctx:
    def __init__(self, state):
        self.state = state


def _state(tmp_path, node_inputs):
    state = State(run_id="r", node_type="postprocess", root=tmp_path / "run")
    state.hook_state["node_inputs"] = node_inputs
    return state


def test_briefing_lists_the_resolved_request_ids(tmp_path):
    # A 刀：quality_mode 已废除 —— 旧调用方仍传时开局简报照常工作（字段被忽略）。
    state = _state(tmp_path, {"visual_requests": [
        {"intent": "LJ cooling rate comparison", "quality_mode": "quick"},
    ]})
    out = _visual_request_briefing(_Ctx(state))
    assert out is not None
    body = out[0].content
    from nodes.postprocess.contracts import normalize_request
    expected = normalize_request({"intent": "LJ cooling rate comparison"})["request_id"]
    assert f"`{expected}`" in body        # 逐字给出工具要的那个字符串
    assert "purpose=exploration" in body
    assert "quality_mode" not in body


def test_only_injected_once(tmp_path):
    state = _state(tmp_path, {"visual_requests": [{"intent": "x"}]})
    assert _visual_request_briefing(_Ctx(state)) is not None
    assert _visual_request_briefing(_Ctx(state)) is None


def test_legacy_figure_requests_key_also_works(tmp_path):
    state = _state(tmp_path, {"figure_requests": {"intent": "legacy caller"}})
    out = _visual_request_briefing(_Ctx(state))
    assert out is not None and "legacy caller" in out[0].content


def test_silent_when_there_is_nothing_to_brief(tmp_path):
    assert _visual_request_briefing(_Ctx(_state(tmp_path, {}))) is None
    assert _visual_request_briefing(_Ctx(_state(tmp_path, {"visual_requests": []}))) is None
    state = State(run_id="r", node_type="postprocess", root=tmp_path / "r2")
    assert _visual_request_briefing(_Ctx(state)) is None


def test_malformed_request_does_not_break_the_briefing(tmp_path):
    """一个坏 request 不该让其余的也看不见。"""
    state = _state(tmp_path, {"visual_requests": [
        {"no_intent_no_source": True},          # normalize 会抛
        {"intent": "good one"},
    ]})
    out = _visual_request_briefing(_Ctx(state))
    assert out is not None and "good one" in out[0].content


def test_error_message_enumerates_available_ids(tmp_path):
    """撞上了也得当场知道正确答案是什么。"""
    from nodes.postprocess.tools.figure import caller_visual_request as _caller_visual_request
    from nodes.postprocess.contracts import VisualContractError

    state = _state(tmp_path, {"visual_requests": [{"intent": "cooling rate"}]})
    with pytest.raises(VisualContractError) as excinfo:
        _caller_visual_request(state, "visual_requests[0]")
    from nodes.postprocess.contracts import normalize_request
    assert normalize_request({"intent": "cooling rate"})["request_id"] in str(excinfo.value)


def test_harness_enables_the_hook():
    from core.loader import load_harness

    assert "visual_request_briefing" in load_harness("postprocess").loop_hooks
