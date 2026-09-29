"""hypothesis 节点内部自检工具测试。"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from core.state import State
from nodes.hypothesis.tools.conclusion_audit import _audit_hypothesis_vs_conclusions
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs


def _make_state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))


def _minimal_research_plan() -> str:
    return (
        "Plan sections: experimental_design · computational_workflow · "
        "baselines · resource_estimates · risk_analysis\n\n"
        "## Computational Workflow\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  S1[geometry_relax] --> S2[phonon]\n"
        "  S1 --> S3[aimd]\n"
        "  S1 --> S4[doping_substitution]\n"
        "  S2 --> S5[postprocess: merge]\n"
        "  S3 --> S5\n"
        "  S4 --> S5\n"
        "```\n\n"
        "| Step ID | 任务类型 | 前置步骤 | 软件/泛函 | 超胞 | 关键参数 | 产出 | 对应 falsifier |\n"
        "| S1 | geometry_relax | - | VASP PBE | 原胞 | 依据：bulk 平衡结构；ISIF=3 | CONTCAR | - |\n"
        "| S2 | phonon | S1 | VASP DFPT | 2x2x2 | 依据：消除有限尺寸虚频检查 | 无虚频 | - |\n"
        "| S3 | aimd | S1 | VASP | 1x1x2 | 依据：有限温度 MD 最小镜像；300K 8ps | 轨迹 | - |\n"
        "| S4 | doping_substitution | S1 | VASP | 1x1x3 | 依据：稀释掺杂 3% @ 6i | 弛豫结构 | - |\n"
        "| S5 | postprocess | S2, S3, S4 | Python | - | 汇总多分支产出 merge | 汇总 | H1 |\n\n"
        "### 步骤依赖与门控\n"
        "- S1 完成后 S2/S3/S4 可并行执行\n"
        "- S5 需 S2、S3、S4 全部完成（多前置汇合）\n"
    )


def _systems_benchmark_research_plan() -> str:
    """非材料领域 plan：不得依赖 geometry_relax/phonon 等关键词白名单。"""
    return (
        "Plan sections: experimental_design · computational_workflow · "
        "baselines · resource_estimates · risk_analysis\n\n"
        "## Computational Workflow\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  S1[data_import] --> S2[schema_validation]\n"
        "  S2 --> S3[aggregation]\n"
        "  S3 --> S4[paired_bootstrap]\n"
        "  S4 --> S5[postprocess]\n"
        "```\n\n"
        "| Step ID | 任务类型 | 前置步骤 | 软件/方法 | 模型尺度 | 关键参数 | 产出 | 对应 falsifier |\n"
        "| S1 | data_import | - | pandas | full sample | 依据：读入 CSV 性能日志 | raw table | - |\n"
        "| S2 | schema_validation | S1 | pandera | full sample | 依据：校验列类型与缺失率 | clean table | - |\n"
        "| S3 | aggregation | S2 | pandas | by condition | 依据：按部署条件聚合 P95 | agg table | H1 |\n"
        "| S4 | paired_bootstrap | S3 | scipy | B=2000 | 依据：配对 bootstrap 估 CI | ci table | H1 |\n"
        "| S5 | postprocess | S4 | Python | - | 汇总报告表 | report | H1 |\n\n"
        "### 步骤依赖与门控\n"
        "- S2 校验失败则回退修 schema，不进入 S3\n"
    )

def _grounded_falsifier(metric: str = "tau", threshold: float = 0.25) -> dict:
    """Structured falsifier with threshold_rationale (passes threshold_grounding)."""
    return {
        "metric": metric,
        "comparison": ">",
        "threshold": threshold,
        "dataset": "benchmark_v1",
        "regime": "default",
        "threshold_rationale": {
            "source_type": "literature",
            "citation_or_derivation": (
                "survey_report open_question + DOI 10.1000/example.effect-size；"
                "同任务文献报告最小可解释增益约该量级"
            ),
            "scientific_meaning": (
                f"超过 threshold={threshold} 表明相对 baseline 的效应超过文献噪声底线，"
                "支持该机制而非随机波动"
            ),
        },
    }




def _prereg_json_block(claim_text, falsifier):
    """新契约：承诺住在预注册正文里（json 块），审计从 head 解析。"""
    import json as _json
    return ("\n\n```json\n" + _json.dumps({
        "hypotheses": [{"label": "H1", "claim_text": claim_text,
                        "falsification_criteria_structured": falsifier}],
    }, ensure_ascii=False) + "\n```\n" + f"\nclaim_text: {claim_text}\n")

def _append_tool_call(state: State, name: str, args: dict) -> None:
    state.append_transcript("tool_call", name=name, args=args)


def _prereg_with_contract(*, hypothesis_ids: tuple[str, ...] = ("H1",)) -> str:
    """v0.5 起协议以 `## Research Questions` 开头（见 core/prereg_commitments.py）。

    写了 proposition 的问题就是假设 —— 没有单独的 yes/no 开关字段。
    """
    lines = ["## Research Questions", ""]
    for hid in hypothesis_ids:
        lines += [
            f"### {hid}: 该机制在稀疏区是否成立？",
            "- output_kind: 对一条命题的裁决",
            "- proposition: 稀疏区下收敛时间显著低于稠密区",
            "- assumption: 稀疏区与稠密区之外没有第三种相关区制",
            "```yaml",
            "- metric: tau",
            '  comparison: "<"',
            "  threshold: 0.25",
            "```",
            "",
        ]
    return "\n".join(lines)


def _save_hif(state: State, assessments: list[dict]) -> None:
    body = {"summary": {"n_assessed": len(assessments), "max_hif": 60}, "assessments": assessments}
    state.save_artifact(
        "hypothesis_innovation_report", "HIF", "```json\n" + json.dumps(body) + "\n```\n",
        metadata={"n_assessed": len(assessments), "max_hif": 60},
    )


def _save_frozen_prereg(state: State, claim: str, falsifier) -> None:
    """冻结只能来自账本的 freeze 行（save 行里的 frozen 键会被剥掉）：先存再 mark_frozen。"""
    saved = state.save_artifact(
        "pre_registration", "P", _prereg_with_contract() + _prereg_json_block(claim, falsifier),
    )
    state.mark_frozen(saved["id"])


def test_validate_all_pass() -> None:
    state = _make_state()
    claim = "Novel sparse-regime convergence hypothesis"
    _save_hif(state, [{
        "label": "H1",
        "claim_text": claim,
        "dimensions": {"R": 1, "Q": 4},
        "tier": "moderate",
        "plausibility_reject": False,
    }])
    _save_frozen_prereg(state, claim, _grounded_falsifier("tau"))
    _append_tool_call(state, "save_artifact", {"artifact_type": "pre_registration"})
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Research Overview\n\n## Top hypotheses\n\nH1 selected because sharp falsifier.\n\n"
        "## Rejected\n\nNone.\n\n## Unexplored\n\nCross-domain analogy.\n" * 3,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is True
    assert result["failed_checks"] == []


def test_validate_passes_non_materials_workflow() -> None:
    """#160：非材料任务类型（无 geometry_relax/phonon/…）也应通过 research_plan_complete。"""
    state = _make_state()
    claim = "Paired bootstrap P95 improves deploy decision"
    _save_hif(state, [{
        "label": "H1",
        "claim_text": claim,
        "dimensions": {"R": 1, "Q": 4},
        "tier": "moderate",
        "plausibility_reject": False,
    }])
    _save_frozen_prereg(state, claim, _grounded_falsifier("p95_delta"))
    _append_tool_call(state, "save_artifact", {"artifact_type": "pre_registration"})
    state.save_artifact("research_plan", "Plan", _systems_benchmark_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is True
    assert "research_plan_complete" not in result["failed_checks"]


def test_validate_fails_on_plausibility_reject() -> None:
    state = _make_state()
    claim = "Implausible mechanism hypothesis"
    _save_hif(state, [{
        "label": "H1",
        "claim_text": claim,
        "dimensions": {"R": 1, "Q": 0},
        "tier": "minimal",
        "plausibility_reject": True,
    }])
    _save_frozen_prereg(state, claim, _grounded_falsifier("x"))
    _append_tool_call(state, "save_artifact", {"artifact_type": "pre_registration"})
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "hif_plausibility_gate" in result["failed_checks"]


def test_validate_fails_on_high_r() -> None:
    state = _make_state()
    claim = "NHC is better than NH on dense LJ"
    _save_hif(state, [{"label": "H1", "claim_text": claim, "dimensions": {"R": 5}, "tier": "minimal"}])
    _save_frozen_prereg(state, claim, _grounded_falsifier("x"))
    _append_tool_call(state, "save_artifact", {"artifact_type": "pre_registration"})
    state.save_artifact("research_plan", "Plan", _minimal_research_plan())
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "not_conclusion_restatement" in result["failed_checks"]


def test_validate_fails_on_missing_computational_workflow() -> None:
    state = _make_state()
    claim = "Novel sparse-regime convergence hypothesis"
    _save_hif(state, [{
        "label": "H1",
        "claim_text": claim,
        "dimensions": {"R": 1, "Q": 4},
        "tier": "moderate",
        "plausibility_reject": False,
    }])
    _save_frozen_prereg(state, claim, _grounded_falsifier("tau"))
    _append_tool_call(state, "save_artifact", {"artifact_type": "pre_registration"})
    state.save_artifact(
        "research_plan", "Plan",
        "Plan sections: experimental_design · baselines · "
        "resource_estimates · risk_analysis\n",
    )
    state.save_artifact(
        "hypothesis_research_overview", "Overview",
        "# Overview\n" + "x" * 300,
    )
    result = asyncio.run(_validate_hypothesis_outputs(state))
    # research_plan_complete 在 v2.1 P-QC2 被降为 advisory（结构瑕疵不判死 run，
    # 随验证报告交 reviewer 当修订项）。本断言此前还停在两层拆分之前的语义上，
    # 在 origin/main 就是红的 —— 现在按实际契约断言：如实报，但不拦完成。
    assert "research_plan_complete" in result["advisory_failures"]
    assert "research_plan_complete" not in result["failed_checks"]


def test_conclusion_audit_flags_paraphrase() -> None:
    state = _make_state()
    state.save_artifact(
        "survey_report", "S",
        "## Key findings\n\n1. NHC chain is more ergodic than single NH on dense LJ.\n",
    )
    result = asyncio.run(_audit_hypothesis_vs_conclusions(
        state,
        hypotheses=[{"label": "H1", "claim_text": "NHC chain is more ergodic than single NH on dense LJ"}],
    ))
    assert result["passed"] is False
