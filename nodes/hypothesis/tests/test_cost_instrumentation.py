"""成本采集：cost_instrumentation 硬门。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.cost_instrumentation import (
    _audit_cost_instrumentation,
    assess_cost_instrumentation,
    looks_like_step_cost_analysis,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _grounded_falsifier,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)

_COST_CLAIM = (
    "假设要求分析步骤级计算成本：比较各 Step 的 token cost 与墙钟，"
    "并据此证伪成本-时延权衡。\n"
)

_INSTRUMENTATION = """
## cost_instrumentation
### metric_definition
每步记录 prompt+completion token 与墙钟秒；单位：token 与秒；不含人工时间。
### collection_points
挂接 workflow Step ID：S1, S2, S3, S4, S5；每步开始/结束各打点一次。
### collection_method
用 time.perf_counter 与 provider usage API（tiktoken/账单导出）写 JSONL 日志字段 step_id,tokens,wall_s。
### aggregation
按 step 汇总均值后对齐 falsifier 的 per-step cost 表；run 级为各步之和。
### missing_policy
某步无日志则该 run 剔除该步并在对照臂同样处理；连续缺失则记 fail。
"""


def test_detects_step_cost_claim() -> None:
    assert looks_like_step_cost_analysis(_COST_CLAIM) is True
    assert looks_like_step_cost_analysis("仅做结构弛豫与 phonon") is False


def test_skips_without_cost_claim() -> None:
    report = assess_cost_instrumentation(
        plan=_minimal_research_plan(),
        prereg="# no cost metrics\n",
    )
    assert report["applicable"] is False
    assert report["passed"] is True


def test_fails_without_instrumentation_section() -> None:
    report = assess_cost_instrumentation(
        plan=_minimal_research_plan(),
        prereg=_COST_CLAIM,
    )
    assert report["applicable"] is True
    assert report["passed"] is False
    assert report["has_section"] is False


def test_fails_estimate_only_method() -> None:
    plan = (
        _minimal_research_plan()
        + "\n## cost_instrumentation\n"
        "### metric_definition\n每步 token 与墙钟秒为单位。\n"
        "### collection_points\nS1, S2, S3, S4, S5\n"
        "### collection_method\n粗估 resource_estimates 预算即可，预计差不多。\n"
        "### aggregation\n按 step 汇总对齐 falsifier。\n"
        "### missing_policy\n缺失则剔除该 run。\n"
    )
    report = assess_cost_instrumentation(plan=plan, prereg=_COST_CLAIM)
    assert report["passed"] is False
    assert "collection_method" in report["missing_fields"]


def test_passes_full_instrumentation() -> None:
    plan = _minimal_research_plan() + "\n" + _INSTRUMENTATION
    report = assess_cost_instrumentation(plan=plan, prereg=_COST_CLAIM)
    assert report["passed"] is True, report["reason"]


def test_fails_unmapped_step_ids() -> None:
    plan = (
        _minimal_research_plan()
        + "\n## cost_instrumentation\n"
        "### metric_definition\n每步 GPU-s 与墙钟秒。\n"
        "### collection_points\nS1, S99\n"
        "### collection_method\n用 nsys profiler 与 JSONL 日志记录每步耗时。\n"
        "### aggregation\n按 step 表汇总后对齐 falsifier。\n"
        "### missing_policy\n无日志则记失败，对照一致。\n"
    )
    report = assess_cost_instrumentation(plan=plan, prereg=_COST_CLAIM)
    assert report["passed"] is False
    assert "S99" in (report.get("unmapped_steps") or [])


def test_validate_fails_cost_instrumentation() -> None:
    state = _make_state()
    claim = "Step-level token cost dominates latency"
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
        "hypothesis_text": claim + "；分析步骤级计算成本",
        "falsification_criteria_structured": {
            **_grounded_falsifier(metric="step_cost_tokens"),
            "dataset": "workflow_runs",
        },
        "prereg_chunk_id": "chunk_x",
        "scope_dimensions": {"task": "cost"},
    })
    _frozen_saved = state.save_artifact(
        "pre_registration", "P", "# Prereg\n" + _COST_CLAIM,
        metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "cost_instrumentation" in result["failed_checks"]


def test_audit_cost_instrumentation_tool() -> None:
    state = _make_state()
    _frozen_saved = state.save_artifact(
        "pre_registration", "P", "#\n" + _COST_CLAIM, metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan", _minimal_research_plan() + "\n" + _INSTRUMENTATION,
    )
    result = asyncio.run(_audit_cost_instrumentation(state))
    assert result["status"] == "success"
    assert result["passed"] is True, result.get("message")
    assert state.list_artifacts("hypothesis_cost_instrumentation")
