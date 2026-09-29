"""图上印的数，必须等于它自己声称的出处（#439）。

E2E v26（2D Ising vs Onsager，2026-08-13）现场：论文的中心结论和它自己嵌的图
互相矛盾，而且矛盾**正好跨过预注册的 2σ 门** ——

    正文 / 摘要 / 图注   T_c = 2.269287 ± 0.000087   1.17σ   supported
    图上印的             T_c = 2.268776 ± 0.000148   2.8σ    refuted

同一个头条数字被 experiment 和 postprocess 各算一次（不加权 vs 加权拟合），
两边都不报错。**四轮 reviewer 全部没抓到，是人工审出来的。**

源哈希校验抓不到这一类：两边读的确实是同一份源，分叉在算法里。

下面用的就是现场那两个真实数值，不是造的。
"""
from __future__ import annotations

import pytest

from shared.lib.publication_figures import (
    FigureContradictsItsSource,
    annotated_value_conflicts,
)

#: experiment 冻结的那份（不加权拟合）——正文与判决读的就是它。
_FROZEN = {
    "fss_extrapolation": {"T_c_est": 2.2692871655836173},
    "verdict": {"H1": {"normalized_deviation": 1.1727}},
}

#: postprocess 画图时独立重算的（加权拟合）——只以像素存在的那个数。
_PRINTED_ON_THE_FIGURE = 2.268776


def _annotation(value: float, tolerance: float = 1e-6) -> dict:
    return {
        "label": "T_c",
        "value": value,
        "source_artifact_id": "clean_results__2D_Ising_Tc_CleanResults",
        "source_field": "fss_extrapolation/T_c_est",
        "tolerance": tolerance,
    }


def test_the_v26_contradiction_is_caught():
    conflicts = annotated_value_conflicts(
        {"annotated_values": [_annotation(_PRINTED_ON_THE_FIGURE)]},
        {"clean_results__2D_Ising_Tc_CleanResults": _FROZEN},
    )
    assert conflicts, "v26 那次跨过 2σ 门的矛盾没被抓到"
    assert "2.268776" in conflicts[0] and "2.2692871" in conflicts[0], (
        f"报错没有把两个数一起说出来，人还得自己去翻：{conflicts[0]}")


def test_a_figure_that_agrees_passes():
    """反作弊：对得上的图必须放行，否则上一条可以靠「一律拒绝」通过。"""
    assert annotated_value_conflicts(
        {"annotated_values": [_annotation(2.2692871655836173)]},
        {"clean_results__2D_Ising_Tc_CleanResults": _FROZEN},
    ) == []


def test_within_tolerance_is_not_a_conflict():
    """四舍五入不是矛盾 —— 容差由声明方给。"""
    assert annotated_value_conflicts(
        {"annotated_values": [_annotation(2.26929, tolerance=1e-4)]},
        {"clean_results__2D_Ising_Tc_CleanResults": _FROZEN},
    ) == []


def test_an_unreachable_source_is_not_agreement():
    """够不到出处 ≠ 对上了账。观测不到不许当成一致。"""
    conflicts = annotated_value_conflicts(
        {"annotated_values": [_annotation(_PRINTED_ON_THE_FIGURE)]}, {})
    assert conflicts and "够不到出处" in conflicts[0]


def test_a_number_without_a_source_is_not_a_declaration():
    """只说"我印了 2.268776"而不说来自哪，等于没声明 —— 不许因此蒙混过关。"""
    conflicts = annotated_value_conflicts(
        {"annotated_values": [{"label": "T_c", "value": 2.268776}]}, {})
    assert conflicts and "没说来自哪份产物" in conflicts[0]


def test_not_declaring_anything_skips_but_is_disclosed():
    """postprocess 还没开始填这个字段 —— 闸不拦，但缺席要看得见。"""
    assert annotated_value_conflicts({"figure_hash": "x"}, {}) == []


def test_the_gate_reads_nested_and_indexed_paths():
    """字段路径要能指到嵌套结构里，否则大多数真实产物指不进去。"""
    conflicts = annotated_value_conflicts(
        {"annotated_values": [{
            "label": "sigma", "value": 2.8,
            "source_artifact_id": "a", "source_field": "verdict/H1/normalized_deviation",
            "tolerance": 0.01,
        }]},
        {"a": _FROZEN},
    )
    assert conflicts and "1.1727" in conflicts[0], (
        f"嵌套路径没解析到，或没报出实际值：{conflicts}")
