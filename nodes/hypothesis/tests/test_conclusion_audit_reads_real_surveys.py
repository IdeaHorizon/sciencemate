"""conclusion_audit 的抽取词表与真实 survey 标题分叉 → 0 findings → 空转 passed（#760）。

课题二第一轮实拍（2026-09-01）：`hypothesis_conclusion_audit` 报 `findings scanned: 0`
→ passed=true、cleared=[Q1,Q2]，而 survey 明明含关键发现与空白点。标题是

    ## 一、Lancichinetti & Fortunato 2012 原始论文到底验证到什么程度
    ## 五、公认观点 vs 分歧（空白点信号）
    ## 六、空白点列表（research gaps）

抽取器的词表 `key findings|主要结论|findings|conclusions` 一个都匹配不上。两层：
词表分叉（生成端没有义务对齐）+ 解析失败静默吞（0 findings 按"通过"发）。
"""
from __future__ import annotations

import asyncio

from core.state import State
from nodes.hypothesis.tools.conclusion_audit import (
    _audit_hypothesis_vs_conclusions,
    _extract_survey_findings,
    _survey_headings,
)

REAL_SHAPE = """# consensus clustering 可检测性极限 综述

## 一、Lancichinetti & Fortunato 2012 原始论文到底验证到什么程度
- 原论文只在 LFR 基准上验证了 consensus clustering 提升稳定性，未触及可检测性极限附近的行为。
- 复现实验显示当 mixing parameter μ 超过 0.6 时，consensus 的 NMI 增益消失。

## 五、公认观点 vs 分歧（空白点信号）
- 公认：consensus clustering 在低噪声区提升 NMI；分歧：在可检测性阈值附近是否仍有增益尚无定论。

## 六、空白点列表（research gaps）
- 没有任何工作在可检测性极限附近系统测量 consensus 相对单次 Louvain 的 AMI 增益。
"""


def _state(tmp_path, survey: str) -> State:
    st = State.new(node_type="hypothesis", base_dir=tmp_path / "runs")
    st.save_artifact("survey_report", "consensus", survey, {})
    return st


def test_real_shape_headings_still_yield_findings():
    """词表一个都不命中，但条目在那里 —— 整文切段兜底。"""
    findings = _extract_survey_findings(REAL_SHAPE)
    assert len(findings) >= 4, findings
    assert any("可检测性极限附近系统测量" in f for f in findings)


def test_paraphrase_of_a_gap_is_flagged_not_cleared(tmp_path):
    st = _state(tmp_path, REAL_SHAPE)
    result = asyncio.run(_audit_hypothesis_vs_conclusions(
        st,
        hypotheses=[{"label": "Q1", "claim_text":
                     "复现实验显示当 mixing parameter μ 超过 0.6 时，consensus 的 NMI 增益消失。"}],
    ))
    assert result["status"] == "success"
    assert result["passed"] is False, result
    assert result["report"]["n_findings_scanned"] >= 4


def test_survey_without_extractable_items_is_not_executed_not_passed(tmp_path):
    """缺席的检查不能长得跟通过了一样。"""
    survey = ("# 综述\n\n## 一、原始论文验证到什么程度\n\n一段散文，没有任何条目。\n\n"
              "## 六、空白点列表（research gaps）\n\n还是散文。\n")
    st = _state(tmp_path, survey)
    result = asyncio.run(_audit_hypothesis_vs_conclusions(
        st, hypotheses=[{"label": "Q1", "claim_text": "consensus 在阈值附近有增益"}],
    ))
    assert result["status"] == "not_executed"
    assert "passed" not in result
    assert "cleared" not in result
    assert any("空白点列表（research gaps）" in h for h in result["report"]["scanned_headings"])
    # 报告照存，但 metadata 不带 passed
    rec = st.read_artifact(result["artifact_id"])
    assert rec["metadata"].get("not_executed") is True
    assert "passed" not in rec["metadata"]


def test_headings_are_listed_for_the_operator():
    assert _survey_headings(REAL_SHAPE)[1].startswith("一、Lancichinetti")
