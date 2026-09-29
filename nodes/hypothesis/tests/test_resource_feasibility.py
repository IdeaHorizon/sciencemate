"""资源可执行性：resource_feasibility 硬门。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.resource_feasibility import (
    _audit_resource_feasibility,
    assess_resource_feasibility,
    detect_resource_commitments,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _grounded_falsifier,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)

_OVERCOMMIT = (
    "预注册要求评估 GPT-4、Claude、Gemini 三个模型，"
    "聘请五名人工标注者做一致性，并开展大规模人工审计，"
    "在三个完整 benchmark 上报告主结果。\n"
)

_FEASIBLE = """
## resource_feasibility
### commitments
| kind | quantity | status | source_or_fallback |
| multi_model | GPT-4/Claude/Gemini | assumed | fallback: 核心只用 1 个可复现主模型，其余 exploratory |
| human_annotators | 5 名 | deferred | 不进入核心 falsifier；后续工作 |
| human_audit | 大规模全量 | assumed | fallback: 双人抽检 10% |
| full_benchmarks | 3 完整套 | assumed | fallback: 1 完整 + 2 子集/pilot |
"""


def test_detects_heavy_commitments() -> None:
    kinds = detect_resource_commitments(_OVERCOMMIT)
    assert "multi_model" in kinds
    assert "human_annotators" in kinds
    assert "human_audit" in kinds
    assert "full_benchmarks" in kinds


def test_skips_without_heavy_commitment() -> None:
    report = assess_resource_feasibility(
        plan=_minimal_research_plan(),
        prereg="# small single-model pilot\n",
    )
    assert report["applicable"] is False
    assert report["passed"] is True


def test_fails_without_feasibility_section() -> None:
    report = assess_resource_feasibility(
        plan=_minimal_research_plan(),
        prereg=_OVERCOMMIT,
    )
    assert report["applicable"] is True
    assert report["passed"] is False
    assert report["has_section"] is False


def test_fails_assumed_without_fallback() -> None:
    plan = (
        _minimal_research_plan()
        + "\n## resource_feasibility\n### commitments\n"
        "| kind | quantity | status | source_or_fallback |\n"
        "| multi_model | 3 | assumed | 需要三个模型 |\n"
        "| human_annotators | 5 | assumed | 需要五人 |\n"
        "| human_audit | 全量 | assumed | 需要全量审计 |\n"
        "| full_benchmarks | 3 | assumed | 需要三套 |\n"
    )
    report = assess_resource_feasibility(plan=plan, prereg=_OVERCOMMIT)
    assert report["passed"] is False


def test_passes_with_fallback_and_deferred() -> None:
    plan = _minimal_research_plan() + "\n" + _FEASIBLE
    report = assess_resource_feasibility(plan=plan, prereg=_OVERCOMMIT)
    assert report["passed"] is True, report["reason"]
    assert set(report["detected_kinds"]) >= {
        "multi_model",
        "human_annotators",
        "human_audit",
        "full_benchmarks",
    }


def test_validate_fails_resource_feasibility() -> None:
    state = _make_state()
    claim = "Multi-model full-benchmark superiority"
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
        "scope_dimensions": {"task": "eval"},
    })
    _frozen_saved = state.save_artifact(
        "pre_registration", "P", "# Prereg\n" + _OVERCOMMIT, metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "resource_feasibility" in result["failed_checks"]


def test_audit_resource_feasibility_tool() -> None:
    state = _make_state()
    _frozen_saved = state.save_artifact(
        "pre_registration", "P", "#\n" + _OVERCOMMIT, metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact(
        "research_plan", "Plan", _minimal_research_plan() + "\n" + _FEASIBLE,
    )
    result = asyncio.run(_audit_resource_feasibility(state))
    assert result["status"] == "success"
    assert result["passed"] is True, result.get("message")
    assert state.list_artifacts("hypothesis_resource_feasibility")


# ── 否定盲区（2026-08-23 E2E v29 真实误报）──────────────────────────────────

def test_zero_count_disavowal_is_not_a_commitment() -> None:
    """资源表里明确写「0 / 无任何」= 不用，不该被判成承诺。

    E2E v29（纯蒙特卡洛随机游走）现场：模型很规范地在 research_plan 写
    `| human_annotators | 0 | deferred | 无任何人工标注/审计流程；数量为 0 |`，
    旧检测只看到 "annotator" 就判「承诺了人工标注」→ passed=False，白耗一轮。
    """
    plan = (
        "## commitments\n"
        "| kind | quantity | status | note |\n"
        "| human_annotators | 0 | deferred | 无任何人工标注/审计流程；数量为 0 |\n"
        "| full_benchmarks | 0 | n/a | 不使用 benchmark |\n"
    )
    kinds = detect_resource_commitments(plan)
    assert "human_annotators" not in kinds, f"零声明被误判成承诺：{kinds}"
    assert "full_benchmarks" not in kinds, f"零声明被误判成承诺：{kinds}"


def test_deferred_with_positive_count_still_detected() -> None:
    """「五名人工标注者，deferred 到后续」是真承诺（推迟≠不用）—— 不能被否定感知误杀。

    用 pattern 真能匹配的中文数量-前置写法（同 _OVERCOMMIT），确保否定感知只滤掉
    「零/无」而不误伤带正量的承诺。
    """
    plan = "聘请五名人工标注者做一致性检验，deferred 到后续工作。\n"
    assert "human_annotators" in detect_resource_commitments(plan)


def test_bare_chinese_disavowal_is_filtered() -> None:
    """裸「人工标注」出现在否认句里（无任何人工标注）→ 不算承诺。这是 v29 的直接病例。"""
    assert "human_annotators" not in detect_resource_commitments(
        "本研究为纯数值模拟，无任何人工标注或审计流程。")


def test_positive_chinese_bare_still_detected() -> None:
    """裸「人工标注」但带正量语境（大规模人工标注）→ 仍算承诺。"""
    assert "human_annotators" in detect_resource_commitments(
        "需要大规模人工标注来建立金标准。")


def test_unrelated_duration_number_does_not_veto_disavowal() -> None:
    """一行否认某资源却含**无关数字**（时长/编号）→ 那数字不该否决否认。

    E2E v32（样本均值方差 σ²/n，trivial 单机课题）现场：模型在资源汇总行写
    `| 合计 | 单机CPU | < 3 分钟墙钟 | 无需人工标注 |`。旧 _POSITIVE_QTY_RE 的资源
    单位是可选的，于是时长「3 分钟」的「3」被当成正资源量，否决了「无需人工标注」
    → human_annotators 误判成承诺 → 完成门永不闭合 → 模型循环补段烧光 turn
    （单 hypothesis run 1.5M tokens）。正资源量必须自报单位，光一个时长数字不算。
    """
    plan = (
        "## resource_estimates\n"
        "| 合计 | 单机 CPU，无 GPU/集群需求 | < 3 分钟墙钟 | 无需外部算力配额；无需人工标注 |\n"
    )
    kinds = detect_resource_commitments(plan)
    assert "human_annotators" not in kinds, f"时长数字误否决了否认：{kinds}"


def test_five_annotators_with_a_duration_on_the_line_still_detected() -> None:
    """带真实资源量（5 名）的行即便也提到时长，仍算承诺（别把修复做过头）。"""
    plan = "计划聘请 5 名人工标注者，预计每人 3 天完成标注。\n"
    assert "human_annotators" in detect_resource_commitments(plan)
