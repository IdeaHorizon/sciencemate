"""判决拆除三波（postprocess）：一条观察坏不再整份响应作废——剥离/clamp + 记账。

对应清单：contracts:155（region 越界 clamp）、vlm_witness:280（panel_id 越界）、
:285（空观察文本）、:357（visible_elements 非数组）、:121（domain_rubrics 死参数删）、
contracts:118/124（require_list / require_nonempty_text 零调用方删）。
把任何一堵墙加回去，对应断言必转红。
"""
from __future__ import annotations

import inspect

import pytest

from nodes.postprocess import contracts, vlm_witness as rv
from nodes.postprocess.contracts import VisualContractError, normalize_region


def test_region_outside_the_image_is_clamped_and_recorded():
    sink: dict = {}
    out = normalize_region({"x": 0.8, "y": 0.9, "w": 0.5, "h": 0.5}, sink=sink,
                           observation_index=3)
    assert out == {"x": 0.8, "y": 0.9, "w": pytest.approx(0.2), "h": pytest.approx(0.1)}
    finding = sink["validation_findings"][0]
    assert finding["collector"] == "OB-REGION-CLAMPED"
    assert finding["observation_index"] == 3
    assert finding["original_region"]["w"] == 0.5


def test_region_coordinate_protocol_is_still_a_contract():
    with pytest.raises(VisualContractError):
        normalize_region({"x": 1.2, "y": 0, "w": 0.1, "h": 0.1})
    with pytest.raises(VisualContractError):
        normalize_region({"x": 0, "y": 0, "w": "wide", "h": 0.1})


def test_bad_observations_are_stripped_not_fatal():
    sink: dict = {}
    good = {"panel_id": "p1", "observation": "x-axis tick labels are clipped at the right edge",
            "region": {"x": 0.7, "y": 0.8, "w": 0.3, "h": 0.2}, "visible_elements": ["x ticks"]}
    payload = {
        "observations": [
            {**good, "panel_id": "p9"},                       # panel 越界 → 剥离
            {**good, "observation": "   "},                   # 空文本 → 跳过
            {**good, "visible_elements": "x ticks"},          # 非数组 → 置空
            {**good, "region": {"x": 0.7, "y": 0.8, "w": 0.6, "h": 0.6}},  # 越界 → clamp
        ]
    }
    out = rv._validate_observations(payload, "p1", ["p1"], sink=sink)
    assert [o["visible_elements"] for o in out] == [[], ["x ticks"]]
    assert out[1]["region"]["w"] == pytest.approx(0.3)
    collectors = [f["collector"] for f in sink["validation_findings"]]
    assert collectors.count("OB-REVIEWER-ROLE") == 1
    assert collectors.count("OB-MALFORMED-OBSERVATION") == 2
    assert collectors.count("OB-REGION-CLAMPED") == 1
    assert {f.get("observation_index") for f in sink["validation_findings"]} == {0, 1, 2, 3}


def test_dead_domain_rubrics_parameter_and_unused_helpers_are_gone():
    assert "domain_rubrics" not in inspect.signature(rv.review_checklist).parameters
    assert not hasattr(contracts, "require_list")
    assert not hasattr(contracts, "require_nonempty_text")
