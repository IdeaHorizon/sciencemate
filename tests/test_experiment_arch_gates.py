"""架构级修复：完成度门禁(A) + review 红线否决(B) + review fail-closed(C)。

根因来自 experiment 节点 6 次"修订"实测：转发同一份 frozen experiment_log 就能
蒙过完成度门禁（agent 不执行也 completed）；reviewer 8 维平均把 execution=1 稀释
成 approve→PROCEED；review 解析失败默认 PROCEED（fail-open）。三条都是框架级，
修一次对所有节点通用。不联网。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.harness import NodeHarness
from core.state import State
from shared.tools.library.decision_package import (
    _apply_critical_veto,
    _parse_critique_json,
    _red_line_reason,
)


# ══ A. 完成度门禁只认本 run 产出 ══════════════════════════════════════════════

def _exp_harness() -> NodeHarness:
    h = NodeHarness(node_type="experiment")
    h.required_outputs = ["experiment_log"]
    return h


async def _finalize(state, harness):
    """跑 finalize_run 的完成度判定部分，返回 (status, missing)。"""
    from core.agent_loop import LoopResult
    from core.executor import finalize_run

    # finalize 只做 produced/missing 判定（QC 判定层 #627 已整层删除；这里原本
    # 要把 harness.quality_checks 清空才能只测完成度，现在那个属性不存在了 ——
    # 给 dataclass 凭空赋一个字段不报错，但它谁也不影响）。
    lr = LoopResult(final_text="", turns=1, tool_calls=[], messages=[])
    summary = await finalize_run(state, harness, lr, llm=None)
    return summary["status"], summary["missing_required_outputs"]


@pytest.mark.asyncio
async def test_forwarded_frozen_artifact_does_not_satisfy_gate(tmp_path: Path):
    """核心根因复现：转发一个 frozen experiment_log 进来（模拟 REVISE），agent
    本 run 什么都没产 → 门禁必须判 incomplete，不能因为"state 里有个 experiment_log"
    就算完成。"""
    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_a")
    # 模拟 executor 的 upstream 注入：带 _forwarded_input 标记
    _frozen_saved = state.save_artifact("experiment_log", "RQ1_old", "旧日志，转发进来的",
                         metadata={"_forwarded_input": True})
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    status, missing = await _finalize(state, _exp_harness())
    assert status == "incomplete"
    assert "experiment_log" in missing


@pytest.mark.asyncio
async def test_agent_reproduced_artifact_satisfies_gate(tmp_path: Path):
    """agent 本 run 真的 save 了一份新 experiment_log（无 _forwarded_input）→ 通过。"""
    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_a2")
    state.save_artifact("experiment_log", "RQ1_new", "本 run 真产出的日志")
    status, missing = await _finalize(state, _exp_harness())
    assert status == "completed"
    assert missing == []


@pytest.mark.asyncio
async def test_forwarded_then_overwritten_counts(tmp_path: Path):
    """转发进来 + agent 又覆盖（revise 真干活）→ 覆盖写入无标记的新 metadata → 通过。"""
    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_a3")
    state.save_artifact("experiment_log", "RQ1", "转发旧版",
                         metadata={"_forwarded_input": True})
    # agent 覆盖同 type+name（save_artifact 工具写 fresh metadata，不带标记）
    state.save_artifact("experiment_log", "RQ1", "revise 后的新版")
    status, missing = await _finalize(state, _exp_harness())
    assert status == "completed"


# ══ B. review 红线机械否决 ════════════════════════════════════════════════════

def test_red_line_from_critical_concern():
    crit = {"verdict": "approve_with_revisions", "concerns": [
        {"severity": "minor", "description": "小问题"},
        {"severity": "critical", "description": "没有真正执行实验"}]}
    assert _red_line_reason(crit) is not None
    assert "critical concern" in _red_line_reason(crit)


def test_red_line_from_dimension_floor():
    """execution_completeness=1 被 honesty=5 平均稀释成 overall=3，但维度触底就是红线。"""
    crit = {"verdict": "approve_with_revisions", "concerns": [],
            "per_dimension_scores": {"execution_completeness": 1, "honesty": 5,
                                     "deviation_disclosure": 5, "reproducibility": 2}}
    reason = _red_line_reason(crit)
    assert reason is not None
    assert "execution_completeness" in reason


def test_no_red_line_when_all_above_floor():
    crit = {"concerns": [{"severity": "minor", "description": "x"}],
            "per_dimension_scores": {"a": 3, "b": 4, "c": 2}}
    assert _red_line_reason(crit) is None


def test_veto_downgrades_proceed_to_revise():
    """有红线 + reviewer 推荐 proceed → 机械降为 revise（不受平均分影响）。"""
    crit = {"per_dimension_scores": {"execution_completeness": 1, "honesty": 5}}
    action, note = _apply_critical_veto(crit, "proceed", None)
    assert action == "revise"
    assert note and "框架否决" in note


def test_veto_keeps_redirect_when_target_present():
    crit = {"concerns": [{"severity": "critical", "description": "x"}]}
    action, note = _apply_critical_veto(crit, "proceed", "literature")
    assert action == "redirect_upstream"


def test_no_veto_without_red_line():
    crit = {"per_dimension_scores": {"a": 4, "b": 5}}
    action, note = _apply_critical_veto(crit, "proceed", None)
    assert action == "proceed"
    assert note is None


def test_bool_score_not_treated_as_floor():
    """metadata 里若混入 bool（True==1）不能误判为维度触底。"""
    crit = {"per_dimension_scores": {"passed": True, "score": 4}}
    assert _red_line_reason(crit) is None


# ══ C. review 解析鲁棒 + fail-closed ═════════════════════════════════════════

def test_parse_plain_json():
    assert _parse_critique_json('{"verdict": "approve"}') == {"verdict": "approve"}


def test_parse_fenced_json():
    txt = "这是我的评审：\n```json\n{\"verdict\": \"block\"}\n```\n完毕"
    assert _parse_critique_json(txt) == {"verdict": "block"}


def test_parse_prefix_suffix_noise():
    assert _parse_critique_json('note: {"verdict": "revise"} end') == {"verdict": "revise"}


def test_parse_garbage_returns_none():
    assert _parse_critique_json("完全不是 JSON 的一段话") is None
    assert _parse_critique_json("") is None
