"""欠着 research_state 要在**还有轮数**时说，不能只在收尾那一刻说。

E2E v16 实测：hypothesis 烧满 40 轮（max_turns）结束，`on_before_finish`
**一次都没被调用**（0 次拦截记录），research_state 从头到尾没产出 → QC 事后
判 incomplete → reviewer 拒绝出意见 → 决策包连 PROCEED 都不给，整条流程停死。

收尾闸只守"自愿收尾"这一条路，**耗尽轮数就是它的静默绕过路径**。没轮数了
再拦也没用，得在还来得及的时候把欠账摆到面前。
"""

import types

import pytest

from nodes.hypothesis import hooks


class _Harness:
    def __init__(self, max_turns=40):
        self.max_turns = max_turns


class _State:
    def __init__(self, baseline=0):
        self.hook_state = {"_research_state_baseline_version": baseline}


def _ctx(turn=1, max_turns=40, baseline=0):
    return types.SimpleNamespace(state=_State(baseline), harness=_Harness(max_turns), turn=turn)


def _patch(monkeypatch, *, frozen_ids, version):
    import nodes.hypothesis.tools.research_state as rs
    monkeypatch.setattr(rs, "frozen_prereg_hypothesis_ids", lambda s: set(frozen_ids))
    monkeypatch.setattr(rs, "current_state", lambda s: {"metadata": {"version": version}})
    monkeypatch.setattr(rs, "_meta", lambda a: (a or {}).get("metadata") or {})


def test_debt_is_announced_while_turns_remain(monkeypatch):
    _patch(monkeypatch, frozen_ids={"H1", "H2"}, version=0)
    out = hooks._outstanding_research_state_debt(_ctx(turn=5))
    assert out and "update_research_state" in out[0].content
    assert "H1" in out[0].content


def test_no_debt_before_the_protocol_is_frozen(monkeypatch):
    """还没冻结协议 = 还没产生要记账的科学内容，别制造噪音。"""
    _patch(monkeypatch, frozen_ids=set(), version=0)
    assert hooks._outstanding_research_state_debt(_ctx()) is None


def test_silent_once_the_version_advanced(monkeypatch):
    _patch(monkeypatch, frozen_ids={"H1"}, version=1)
    assert hooks._outstanding_research_state_debt(_ctx(baseline=0)) is None


def test_late_in_the_budget_it_says_the_consequence(monkeypatch):
    """快没轮数时必须说清后果 —— 否则模型不知道这次真的会以 incomplete 收场。"""
    _patch(monkeypatch, frozen_ids={"H1"}, version=0)
    out = hooks._outstanding_research_state_debt(_ctx(turn=38, max_turns=40))
    assert out and "incomplete" in out[0].content


def test_early_turns_do_not_cry_wolf(monkeypatch):
    _patch(monkeypatch, frozen_ids={"H1"}, version=0)
    out = hooks._outstanding_research_state_debt(_ctx(turn=2, max_turns=40))
    assert out and "incomplete" not in out[0].content


def test_turn_start_hook_carries_both_briefing_and_debt(monkeypatch):
    """接到真入口上 —— 光有函数没接进 on_turn_start 等于没有。"""
    _patch(monkeypatch, frozen_ids={"H1"}, version=0)
    monkeypatch.setattr(hooks, "_research_state_snapshot_baseline", lambda ctx: None)
    monkeypatch.setattr(hooks, "_analysis_round_briefing", lambda ctx: None)
    out = hooks._analysis_round_on_turn_start(_ctx(turn=5))
    assert out and any("update_research_state" in m.content for m in out)
