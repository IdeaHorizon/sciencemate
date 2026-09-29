"""阈值依据：threshold_rationale 硬门；定性/存在性证伪合法分支。"""
from __future__ import annotations

import asyncio

from nodes.hypothesis.tools.threshold_grounding import (
    _audit_threshold_grounding,
    assess_one_threshold,
    assess_threshold_grounding,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tests.test_output_validator import (
    _append_tool_call,
    _grounded_falsifier,
    _make_state,
    _minimal_research_plan,
    _save_hif,
)


def test_assess_fails_ungrounded_ratio() -> None:
    report = assess_threshold_grounding([{
        "label": "H1",
        "metric": "accuracy",
        "comparison": ">",
        "threshold": "20%",
    }])
    assert report["passed"] is False
    assert report["n_failed"] >= 1


def test_assess_fails_vague_rationale() -> None:
    report = assess_threshold_grounding([{
        "label": "H1",
        "metric": "speedup",
        "comparison": ">",
        "threshold": 2.0,
        "threshold_rationale": {
            "source_type": "literature",
            "citation_or_derivation": "经验上显著更好",
            "scientific_meaning": "足够好",
        },
    }])
    assert report["passed"] is False


def test_assess_passes_literature_grounded() -> None:
    report = assess_threshold_grounding([_grounded_falsifier()])
    assert report["passed"] is True
    assert report["n_numeric"] == 1
    assert report["n_qualitative"] == 0


def test_assess_passes_theory_grounded() -> None:
    report = assess_threshold_grounding([{
        "label": "H2",
        "metric": "error_rate",
        "comparison": "<",
        "threshold": 0.05,
        "threshold_rationale": {
            "source_type": "theory",
            "citation_or_derivation": "由 Hoeffding 不等式推导：n=500 时偏差 bound≈0.05",
            "scientific_meaning": "低于该阈值表明观测误差落在理论置信界内，机制可识别",
        },
    }])
    assert report["passed"] is True


def test_assess_passes_qualitative_trend() -> None:
    """定性证伪：SCF 残差不单调下降 — 不要求编数字阈值。"""
    report = assess_threshold_grounding([{
        "label": "H_scf",
        "metric": "scf_residual",
        "comparison": "qualitative",
        "threshold": "若 SCF 残差不随迭代单调下降则证伪",
        "dataset": "scf_traj",
    }])
    assert report["passed"] is True
    assert report["n_qualitative"] == 1
    assert report["per_falsifier"][0]["mode"] == "qualitative"


def test_assess_passes_exists_observation() -> None:
    """存在性证伪：未观察到相分离则证伪。"""
    report = assess_threshold_grounding([{
        "label": "H_phase",
        "metric": "phase_separation",
        "comparison": "exists",
        "criterion": "若未观察到相分离则证伪",
        "dataset": "md_traj",
    }])
    assert report["passed"] is True
    assert report["per_falsifier"][0]["mode"] == "exists"


def test_assess_passes_not_exists() -> None:
    report = assess_threshold_grounding([{
        "label": "H_absent",
        "metric": "spurious_peak",
        "comparison": "not_exists",
        "criterion": "若频谱出现虚假尖峰则证伪",
    }])
    assert report["passed"] is True
    assert report["per_falsifier"][0]["mode"] == "not_exists"


def test_assess_fails_qualitative_without_criterion() -> None:
    report = assess_threshold_grounding([{
        "label": "H_empty",
        "metric": "scf_residual",
        "comparison": "qualitative",
    }])
    assert report["passed"] is False
    assert any("判据" in i for i in report["per_falsifier"][0]["issues"])


def test_assess_fails_qualitative_vague_criterion() -> None:
    row = assess_one_threshold({
        "label": "H_vague",
        "metric": "quality",
        "comparison": "qualitative",
        "threshold": "显著更好",
    })
    assert row["passed"] is False


def test_numeric_comparison_without_threshold_still_fails() -> None:
    """未声明定性分支时，缺数字仍拦（并提示可用 qualitative/exists）。"""
    row = assess_one_threshold({
        "label": "H1",
        "metric": "accuracy",
        "comparison": ">",
    })
    assert row["passed"] is False
    assert any("缺少数值 threshold" in i for i in row["issues"])
    assert any("qualitative" in i for i in row["issues"])


def test_qualitative_with_pure_numeric_threshold_still_needs_rationale() -> None:
    """即便写了 qualitative，若 threshold 是纯数字仍走数值门禁。"""
    report = assess_threshold_grounding([{
        "label": "H_mixed",
        "metric": "accuracy",
        "comparison": "qualitative",
        "threshold": "20%",
    }])
    assert report["passed"] is False
    assert report["per_falsifier"][0]["mode"] == "numeric"


def test_mixed_numeric_and_qualitative_pass() -> None:
    report = assess_threshold_grounding([
        _grounded_falsifier(),
        {
            "label": "H_qual",
            "metric": "phase_separation",
            "comparison": "exists",
            "criterion": "若未观察到相分离则证伪",
        },
    ])
    assert report["passed"] is True
    assert report["n_numeric"] == 1
    assert report["n_qualitative"] == 1


def test_validate_fails_threshold_grounding() -> None:
    state = _make_state()
    claim = "Speedup exceeds arbitrary 20 percent"
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
        "falsification_criteria_structured": {
            "metric": "speedup",
            "comparison": ">",
            "threshold": "20%",
        },
        "prereg_chunk_id": "chunk_x",
        "scope_dimensions": {"task": "latency"},
    })
    _frozen_saved = state.save_artifact(
        "pre_registration", "P",
        "## Inquiry Contract\n\n"
        "### Q1: speedup 是否超过基线？\n"
        "- output_kind: 对一条命题的裁决\n"
        "- decides_a_proposition: yes\n"
        "- hypothesis: H1\n\n"
        "## Hypothesis 1 (H1): speedup\n",
        metadata={},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    # 判决拆除批 3w（threshold_grounding.py:324 降格→H-OB1，经 _ADVISORY_CHECKS）：
    # 阈值依据检查照跑、结果如实进报告交 reviewer，但不再把 run 判死——
    # 它落在 advisory_failures，不落 failed_checks。
    assert "threshold_grounding" not in result["failed_checks"]
    assert "threshold_grounding" in result["advisory_failures"]
    row = next(r for r in result["results"] if r["name"] == "threshold_grounding")
    assert row["passed"] is False
    assert row["tier"] == "advisory"


def test_audit_threshold_grounding_tool() -> None:
    """审计读预注册 head —— 不再回放 transcript（那台机器已删）。"""
    import json as _json

    state = _make_state()
    state.save_artifact(
        "pre_registration", "P",
        "## Inquiry Contract\n\n```json\n" + _json.dumps({
            "hypotheses": [{"label": "H1", "claim_text": "grounded",
                            "falsification_criteria_structured": _grounded_falsifier()}],
        }, ensure_ascii=False) + "\n```\n",
    )
    result = asyncio.run(_audit_threshold_grounding(state))
    assert result["status"] == "success"
    assert result["passed"] is True
    assert state.list_artifacts("hypothesis_threshold_grounding")
