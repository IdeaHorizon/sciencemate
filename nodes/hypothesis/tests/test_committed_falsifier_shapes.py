"""`falsification_criteria_structured` 一个字段两种形状（#409 / #453-1）。

E2E 现场（英国饮食文化课题，会话 5f610fe9）：假说 H1 拆成 F1.1/F1.2/F1.3 三条
定性判据，模型写成 `{"F1.1": {...}, "F1.2": {...}, "F1.3": {...}}`。读取端把整个
映射当成一条判据，comparison 取到 None → `✗ claim (mode=numeric, comparison=None,
threshold=None, source=None)`，threshold_grounding 从第 79 轮磕到第 83 轮出不来。
08-12 那次修的是第一版诊断（transcript 压产物）；这条真根因当时留在原地。
"""
from __future__ import annotations

import json

from nodes.hypothesis.committed import _criteria_from_structured, _extract_prereg_falsifiers


def _prereg(fs) -> str:
    return "```json\n" + json.dumps({"falsification_criteria_structured": fs}, ensure_ascii=False) + "\n```"


def test_mapping_shape_expands_to_one_criterion_per_label():
    fs = {
        "F1.1": {"comparison": "qualitative", "criterion": "出现区分性证据 A", "threshold_rationale": "史学惯例"},
        "F1.2": {"comparison": "qualitative", "criterion": "出现区分性证据 B", "threshold_rationale": "史学惯例"},
        "F1.3": {"comparison": "qualitative", "criterion": "出现区分性证据 C", "threshold_rationale": "史学惯例"},
    }
    found = _extract_prereg_falsifiers(_prereg(fs))
    assert [f["label"] for f in found] == ["F1.1", "F1.2", "F1.3"]
    assert all(f["comparison"] == "qualitative" for f in found)
    assert all(f.get("threshold") is None for f in found)


def test_single_criterion_dict_stays_one():
    fs = {"metric": "Tg", "comparison": ">", "threshold": 0.2, "threshold_rationale": "literature"}
    found = _extract_prereg_falsifiers(_prereg(fs))
    assert len(found) == 1
    assert found[0]["comparison"] == ">" and found[0]["threshold"] == 0.2


def test_list_shape_unchanged():
    fs = [{"label": "H1", "comparison": "<", "threshold": 0}, {"label": "H2", "comparison": ">", "threshold": 0.2}]
    found = _extract_prereg_falsifiers(_prereg(fs))
    assert [f["label"] for f in found] == ["H1", "H2"]


def test_helper_rejects_non_criteria():
    assert _criteria_from_structured(None) == []
    assert _criteria_from_structured({}) == []
    assert _criteria_from_structured("nope") == []
