"""定义保真：锁定用户分类，防止 prereg 改写 ontology。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.definition_lock import (
    _audit_definition_fidelity,
    assess_definition_fidelity,
    build_definition_lock,
    extract_locked_definitions_from_text,
)
from nodes.hypothesis.tools.dialogue_context import build_dialogue_context
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tools.research_goal import parse_research_goal
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)
from core.state import State
from pathlib import Path
import tempfile


def test_extract_locked_definitions_from_heading() -> None:
    text = """
研究 proposal

## 操作类别
- 感知操作：从环境采集观测并写入工作记忆
- 规划操作：基于目标生成下一步动作序列
- 执行操作：调用工具并返回可观测结果
- 验证操作：对照验收标准判定成败

然后做实验。
"""
    locked = extract_locked_definitions_from_text(text)
    labels = [e["label"] for e in locked]
    assert "感知操作" in labels
    assert "规划操作" in labels
    assert "执行操作" in labels
    assert "验证操作" in labels
    assert any("工作记忆" in (e.get("definition") or "") for e in locked)


def test_build_definition_lock_from_explicit_inputs() -> None:
    lock = build_definition_lock({
        "locked_taxonomy": [
            {"label": "路由决策", "definition": "按代价选择下一模型"},
            {"label": "成本记账", "definition": "累计 token 与延迟"},
        ],
    })
    assert lock["n_locked"] == 2
    assert "路由决策" in lock["locked_labels"]


def test_assess_fails_when_categories_renamed() -> None:
    locked = [
        {"label": "感知操作", "definition": "从环境采集观测并写入工作记忆", "source": "t"},
        {"label": "规划操作", "definition": "基于目标生成下一步动作序列", "source": "t"},
        {"label": "执行操作", "definition": "调用工具并返回可观测结果", "source": "t"},
    ]
    # Rewritten taxonomy in prereg
    prereg = """
## definition_lock
- 感知步骤：采集输入
- 推理步骤：做计划
- 行动步骤：跑工具
"""
    plan = "实验只比较感知步骤/推理步骤/行动步骤三类。"
    report = assess_definition_fidelity(locked, prereg=prereg, plan=plan)
    assert report["applicable"] is True
    assert report["passed"] is False
    assert len(report["missing_labels"]) >= 2


def test_assess_passes_when_labels_preserved() -> None:
    locked = [
        {"label": "感知操作", "definition": "从环境采集观测并写入工作记忆", "source": "t"},
        {"label": "规划操作", "definition": "基于目标生成下一步动作序列", "source": "t"},
    ]
    prereg = """
## definition_lock
- 感知操作：从环境采集观测并写入工作记忆
- 规划操作：基于目标生成下一步动作序列

H1: 感知操作延迟下降将提升规划操作成功率。
"""
    plan = "对照实验覆盖感知操作与规划操作两类对象。"
    report = assess_definition_fidelity(locked, prereg=prereg, plan=plan)
    assert report["passed"] is True


def test_parse_research_goal_exposes_locked_definitions() -> None:
    parsed = parse_research_goal(
        {},
        dialogue={
            "user_prompt": "x",
            "locked_definitions": [
                {"label": "感知操作", "definition": "采集观测", "source": "t"},
                {"label": "执行操作", "definition": "调用工具", "source": "t"},
            ],
            "locked_labels": ["感知操作", "执行操作"],
            "must_cover_themes": [],
            "inferred_constraints": [],
            "grounding_checklist": [],
        },
    )
    assert parsed["locked_labels"] == ["感知操作", "执行操作"]
    assert any("分类" in c or "locked" in c for c in parsed["checklist"])


def test_dialogue_context_locks_taxonomy() -> None:
    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    ctx = build_dialogue_context(state, {
        "user_prompt": (
            "## 操作类别\n"
            "- 感知操作：从环境采集观测\n"
            "- 规划操作：生成动作序列\n"
        ),
    })
    labels = ctx["locked_labels"]
    assert "感知操作" in labels
    assert "规划操作" in labels


def test_validate_fails_definition_fidelity_on_rewrite() -> None:
    state = _make_state()
    state.hook_state["research_goal_parsed"] = {
        "must_cover_themes": [],
        "locked_definitions": [
            {"label": "感知操作", "definition": "从环境采集观测并写入工作记忆"},
            {"label": "规划操作", "definition": "基于目标生成下一步动作序列"},
            {"label": "执行操作", "definition": "调用工具并返回可观测结果"},
        ],
    }
    claim = "Step taxonomy with verification levels improves accuracy"
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
        "# 使用步骤分类与验证等级，而非用户操作类别\n",
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
    assert "definition_fidelity" in result["failed_checks"]


def test_audit_definition_fidelity_tool_pass() -> None:
    state = _make_state()
    state.hook_state["research_goal_parsed"] = {
        "locked_definitions": [
            {"label": "感知操作", "definition": "从环境采集观测并写入工作记忆"},
            {"label": "规划操作", "definition": "基于目标生成下一步动作序列"},
        ],
    }
    body = (
        "## definition_lock\n"
        "- 感知操作：从环境采集观测并写入工作记忆\n"
        "- 规划操作：基于目标生成下一步动作序列\n"
    )
    _frozen_saved = state.save_artifact("pre_registration", "P", body, metadata={})
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan",
        "实验对象：感知操作、规划操作。\n" + body,
    )
    result = asyncio.run(_audit_definition_fidelity(state))
    assert result["status"] == "success"
    assert result["passed"] is True
    assert state.list_artifacts("hypothesis_definition_fidelity")
