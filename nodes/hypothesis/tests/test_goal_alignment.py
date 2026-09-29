"""防任务退化：must_cover 解析 + goal alignment 硬门。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.dialogue_context import (
    extract_must_cover_themes,
    format_dialogue_brief,
)
from nodes.hypothesis.tools.goal_alignment import (
    _audit_user_goal_alignment,
    assess_goal_coverage,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tools.research_goal import parse_research_goal
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)


def test_extract_must_cover_from_include_clause() -> None:
    text = (
        "完整的 Agent 原生计算架构研究，包含任务拆分、动态路由、多模型执行和成本分析。"
    )
    themes = extract_must_cover_themes(text)
    assert "任务拆分" in themes
    assert "动态路由" in themes
    assert "多模型执行" in themes
    assert "成本分析" in themes
    assert len(themes) >= 4


def test_extract_must_cover_from_numbered_list() -> None:
    text = "研究重点：\n1. 任务拆分\n2. 动态路由\n3. 成本分析\n"
    themes = extract_must_cover_themes(text)
    assert "任务拆分" in themes
    assert "动态路由" in themes
    assert "成本分析" in themes


def test_assess_fails_when_collapsed_to_one_pillar() -> None:
    themes = ["任务拆分", "动态路由", "多模型执行", "成本分析"]
    narrow = (
        "# Plan\n仅研究 agent 步骤分类与验证等级。"
        "分类 taxonomy 与 verification level 对照实验。"
    )
    report = assess_goal_coverage(themes, prereg=narrow, plan=narrow)
    assert report["applicable"] is True
    assert report["passed"] is False
    assert len(report["missing_in_plan"]) >= 3


def test_assess_passes_when_all_pillars_in_plan() -> None:
    themes = ["任务拆分", "动态路由", "多模型执行", "成本分析"]
    plan = (
        "研究任务拆分策略；动态路由策略对比；多模型执行编排；"
        "并做成本分析（token cost）。"
    )
    prereg = "H1 covers 任务拆分 and 动态路由; H2 covers 多模型执行 and 成本分析"
    report = assess_goal_coverage(themes, prereg=prereg, plan=plan)
    assert report["passed"] is True
    assert report["missing_in_plan"] == []


def test_parse_research_goal_exposes_must_cover() -> None:
    parsed = parse_research_goal(
        {},
        dialogue={
            "user_prompt": "Agent 架构，包含任务拆分、动态路由、多模型执行和成本分析",
            "must_cover_themes": ["任务拆分", "动态路由", "多模型执行", "成本分析"],
            "inferred_constraints": [],
            "grounding_checklist": [],
        },
    )
    assert parsed["must_cover_themes"] == [
        "任务拆分", "动态路由", "多模型执行", "成本分析",
    ]
    assert any("退化" in c or "must_cover" in c for c in parsed["checklist"])


def test_format_brief_keeps_must_cover() -> None:
    brief = format_dialogue_brief({
        "user_prompt": "x" * 500,
        "user_prompt_source": "node_inputs",
        "must_cover_themes": ["任务拆分", "成本分析"],
    })
    assert "must_cover_themes" in brief
    assert "任务拆分" in brief
    assert "成本分析" in brief


def test_validate_fails_user_goal_coverage_on_collapse() -> None:
    state = _make_state()
    state.hook_state["node_inputs"] = {
        "user_prompt": (
            "Agent 原生计算架构研究，包含任务拆分、动态路由、多模型执行和成本分析"
        ),
    }
    state.hook_state["research_goal_parsed"] = {
        "must_cover_themes": ["任务拆分", "动态路由", "多模型执行", "成本分析"],
    }
    claim = "Agent step classification with verification levels"
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
        "falsification_criteria_structured": {"metric": "acc"},
        "prereg_chunk_id": "chunk_x",
        "scope_dimensions": {"task": "classification"},
    })
    _frozen_saved = state.save_artifact(
        "pre_registration", "P",
        "# 仅步骤分类与验证等级\n",
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
    assert "user_goal_coverage" in result["failed_checks"]


def test_audit_user_goal_alignment_tool() -> None:
    state = _make_state()
    state.hook_state["research_goal_parsed"] = {
        "must_cover_themes": ["任务拆分", "动态路由"],
    }
    _frozen_saved = state.save_artifact(
        "pre_registration", "P",
        "假设关注任务拆分与动态路由。",
        metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan",
        "实验含任务拆分 benchmark 与动态路由 ablation。",
    )
    result = asyncio.run(_audit_user_goal_alignment(state))
    assert result["status"] == "success"
    assert result["passed"] is True
    assert state.list_artifacts("hypothesis_goal_alignment")


def test_ratio_expression_with_slash_is_one_theme() -> None:
    """#262：`ρ_p/ρ_f=1.5-10` 是一个参数表达式，不是两个主题。

    E2E 实拍：被拆成 `ρ_p` 与 `ρ_f=1.5-10`，goal alignment 持续报后者未覆盖，
    而 research_plan 明明写着 `ρ_p/ρ_f ∈ {1.5, 2.5, 5.0, 10.0}`。
    """
    themes = extract_must_cover_themes("研究需包含 沉降速度、ρ_p/ρ_f=1.5-10 和 雷诺数范围")
    assert "ρ_p/ρ_f=1.5-10" in themes, themes
    assert "ρ_p" not in themes and "ρ_f=1.5-10" not in themes
    # 两侧带空白的斜杠仍是并列
    assert extract_must_cover_themes("研究需包含 密度 / 粘度 / 温度")[:3] == ["密度", "粘度", "温度"]
