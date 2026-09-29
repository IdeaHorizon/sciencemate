"""跨任务/异构比较协议：含指标与定义统一审查。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.comparison_protocol import (
    _audit_comparison_protocol,
    assess_comparison_protocol,
    extract_comparison_protocol,
    looks_like_construct_comparison,
    looks_like_cross_task_comparison,
    looks_like_hetero_benchmark,
    looks_like_unaligned_action_mix,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _grounded_falsifier,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)


_BASE_PROTOCOL = """
## comparison_protocol
### common_sample
共用 benchmark suite S：任务 T1/T2/T3 全量 episode，排除人工标注损坏样本。
### common_denominator
成功率分母 = 每任务启动的 episode 数（含失败启动）；各条件相同。
### comparable_ops
仅横比 locked 操作：感知操作、规划操作、执行操作；验证操作不参与跨任务成功率对比。
### env_failure_policy
超时/工具宕机统一计为该 step 失败并计入分母；最多重试 1 次后记账；对照臂同一策略。
### unified_definitions
- 成功率：两边统一为「达成任务目标 / 启动 episode」；同一定义。
- 阈值：统一用同一 absolute threshold；无需各自口径。
- 操作类别：沿用 locked_labels；跨任务统一，无需另定。
"""

_HETERO_PROTOCOL = _BASE_PROTOCOL + """
### behavior_alignment
映射表：τ-bench API call ↔ WebArena form_submit；τ-bench reasoning span ↔ WebArena plan_thought；
未映射的 scroll/hover 不计入跨数据集「相同复杂度操作比例」。
### complexity_definition
复杂度按归一化步数分三档：低(1-3)/中(4-8)/高(≥9)；跨数据集用同一 bin；与 unified_definitions 一致。
"""


def test_detect_cross_task() -> None:
    assert looks_like_cross_task_comparison("做跨任务场景下的成功率对比")
    assert not looks_like_cross_task_comparison("单任务稀疏 regime 收敛假说")


def test_detect_hetero_construct_and_mix() -> None:
    assert looks_like_hetero_benchmark("比较 τ-bench 与 WebArena 上的操作比例")
    assert looks_like_construct_comparison("跨数据集对比成功率与 latency 分布")
    assert looks_like_unaligned_action_mix(
        "统计 reasoning 和 API 调用 vs 点击、输入、滚动的比例"
    )


def test_extract_maps_complexity_heading_to_unified_definitions() -> None:
    fields = extract_comparison_protocol(_HETERO_PROTOCOL)
    assert fields["common_sample"]
    assert fields["unified_definitions"]
    assert fields["behavior_alignment"]


def test_assess_skips_single_task() -> None:
    report = assess_comparison_protocol(
        plan="Plan sections: experimental_design\n单任务实验。",
        prereg="H1 on sparse regime",
    )
    assert report["applicable"] is False
    assert report["passed"] is True


def test_assess_fails_cross_task_without_unified_definitions() -> None:
    thin = """
## comparison_protocol
### common_sample
共用测试集全量 episode。
### common_denominator
分母为启动 episode 数。
### comparable_ops
只比规划与执行操作。
### env_failure_policy
超时一律计失败计入分母。
"""
    report = assess_comparison_protocol(
        plan="跨任务对比 A vs B 的成功率。\n" + thin,
        prereg="跨场景比较假说",
    )
    assert report["passed"] is False
    assert "unified_definitions" in report["missing_fields"]


def test_assess_passes_homogenous_with_unified_definitions() -> None:
    report = assess_comparison_protocol(
        plan="跨任务比较\n" + _BASE_PROTOCOL,
        prereg="跨任务假说",
    )
    assert report["passed"] is True
    assert report["missing_fields"] == []


def test_assess_fails_metric_compare_without_unify_review() -> None:
    report = assess_comparison_protocol(
        plan=(
            "在两个场景对比成功率与成本指标差异。\n"
            "## comparison_protocol\n"
            "### common_sample\n两边各 100 episode。\n"
            "### common_denominator\n启动数。\n"
            "### comparable_ops\n规划操作。\n"
            "### env_failure_policy\n超时计失败。\n"
        ),
        claim_texts=["成功率对比"],
    )
    assert report["passed"] is False
    assert "unified_definitions" in report["missing_fields"]


def test_assess_fails_tau_webarena_without_alignment() -> None:
    plan = (
        "在 τ-bench（客服 API）与 WebArena（点击、输入、滚动）上统计操作比例，"
        "比较 reasoning 和 API 调用 与 点击、输入 的占比。\n"
        + _BASE_PROTOCOL
    )
    report = assess_comparison_protocol(
        plan=plan,
        prereg="跨数据集假说",
        claim_texts=["相同复杂度操作比例不同"],
    )
    assert report["hetero_benchmark_detected"] is True
    assert report["passed"] is False
    assert (
        "behavior_alignment" in report["missing_fields"]
        or report["naive_cross_taxonomy"]
    )


def test_assess_passes_hetero_full() -> None:
    report = assess_comparison_protocol(
        plan=(
            "τ-bench vs WebArena：在统一复杂度档位下比较已对齐操作类比例。\n"
            + _HETERO_PROTOCOL
        ),
        prereg="异构 benchmark 假说",
    )
    assert report["passed"] is True
    assert report["hetero_benchmark_detected"] is True


def test_validate_fails_comparison_protocol() -> None:
    state = _make_state()
    claim = "跨任务场景下动态路由使成功率提升"
    _save_hif(state, [{
        "label": "H1",
        "claim_text": claim,
        "dimensions": {"R": 1, "Q": 4},
        "tier": "moderate",
        "plausibility_reject": False,
    }])
    _append_tool_call(state, "freeze_artifact", {})
    _append_tool_call(state, "create_claim", {
        "claim_type": "hypothesis",
        "hypothesis_text": claim,
        "falsification_criteria_structured": _grounded_falsifier(),
        "prereg_chunk_id": "chunk_x",
        "scope_dimensions": {"task": "cross_task"},
    })
    _frozen_saved = state.save_artifact(
        "pre_registration", "P",
        "# 跨任务比较假说\n",
        metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan",
        _minimal_research_plan() + "\n\n跨任务对比实验设计。\n",
    )
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "comparison_protocol_complete" in result["failed_checks"]


def test_audit_tool_pass_hetero() -> None:
    state = _make_state()
    _frozen_saved = state.save_artifact(
        "pre_registration", "P",
        "τ-bench 与 WebArena 对齐假说",
        metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan",
        "τ-bench vs WebArena 指标与操作比例\n" + _HETERO_PROTOCOL,
    )
    result = asyncio.run(_audit_comparison_protocol(state))
    assert result["status"] == "success"
    assert result["passed"] is True
