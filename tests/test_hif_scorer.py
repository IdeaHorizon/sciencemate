"""HIF 评分工具的报错必须把合法契约送到调用方。

2026-08-17 现场：模型三条 assessment 全没给 G，拿回来的是
  `assessments[0]: 'G'; assessments[1]: 'G'; assessments[2]: 'G'`
—— 那是 KeyError 的 repr。看着像"值 'G' 非法"，实际是"少了 G 这个键"，
而且从头到尾没说过合法维度有哪五个。
"""
from __future__ import annotations

import pytest



# ── 缺维度要点名（2026-08-17：三条评估拿回来全是 `assessments[i]: 'G'`）──


def test_missing_dimension_is_named_with_the_legal_set():
    from nodes.hypothesis.tools.hif_scorer import _assess_one

    with pytest.raises(ValueError) as excinfo:
        _assess_one({"label": "H1", "D": 3, "M": 4, "P": 3, "R": 2})
    msg = str(excinfo.value)
    assert "缺必填维度 G" in msg, "得说清是少了 G 这个键，不是值 'G' 非法"
    for dim in ("G", "D", "M", "P", "R"):
        assert f"{dim}=" in msg, "合法取值必须随报错一起送到调用方"


def test_all_missing_dimensions_reported_at_once():
    from nodes.hypothesis.tools.hif_scorer import _assess_one

    with pytest.raises(ValueError) as excinfo:
        _assess_one({"label": "H1"})
    assert "缺必填维度 G, D, M, P, R" in str(excinfo.value), "一次说全，别让它一轮补一个"


def test_complete_assessment_still_scores():
    from nodes.hypothesis.tools.hif_scorer import _assess_one

    entry = _assess_one({"label": "H1", "G": 4, "D": 3, "M": 4, "P": 3, "R": 2})
    assert entry["label"] == "H1"
    assert isinstance(entry["hif"], int)
