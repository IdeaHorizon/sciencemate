"""调用方必须看得见词表，不能靠猜（P5 实测事故）+ 判决拆除词表删除钉。

原事故：合法取值只存在于 contracts.py 的 frozenset 里、只在运行时报错，
调用方契约里从来没写过 → 模型连猜 27 次。防线不变：词表出口
（工具 schema / harness.expected_inputs）必须与校验同源、不许漂移。

判决拆除（A 刀删 quality_mode，B 刀删产物链）追加的反向钉：quality_mode
不许再回到任何一个出口；调用方仍传时被忽略并记 deprecation 见证，不报错。
"""
from __future__ import annotations

from nodes.postprocess.contracts import (
    ASSET_KINDS,
    DEPRECATED_REQUEST_FIELDS,
    PURPOSES,
    visual_request_schema,
)


def test_schema_is_generated_from_the_same_constants_that_validate():
    schema = visual_request_schema()
    props = schema["properties"]
    assert props["asset_kind"]["enum"] == sorted(ASSET_KINDS)
    assert props["purpose"]["enum"] == sorted(PURPOSES)


def test_quality_mode_stays_deleted_from_every_vocabulary_exit():
    """quality_mode 不许回到 schema、工具契约或调用方契约文本。"""
    import nodes.postprocess.tools.figure  # noqa: F401  —— 注册
    from core.loader import load_harness
    from core.tool_registry import _REGISTRY

    assert "quality_mode" in DEPRECATED_REQUEST_FIELDS
    assert "quality_mode" not in visual_request_schema()["properties"]
    tools = vars(_REGISTRY)["tools"]
    render_schema = tools["render_figure"].parameters_schema["properties"]
    assert "quality_mode" not in render_schema
    contract = str(load_harness("postprocess").expected_inputs["visual_requests"])
    assert "quality_mode 已废除" in contract


def test_deprecated_quality_mode_is_ignored_with_a_witness():
    """调用方传旧词表 → 忽略 + deprecation 见证进记录的账，不报错。"""
    from nodes.postprocess.contracts import normalize_request
    from nodes.postprocess.tools.figure import _request_findings

    normalized = normalize_request(
        {"intent": "draw a sketch", "quality_mode": "draft"}
    )
    assert "quality_mode" not in normalized

    class _State:
        hook_state = {
            "node_inputs": {
                "visual_requests": [
                    {"request_id": "sketch", "intent": "draw a sketch",
                     "quality_mode": "draft"}
                ]
            }
        }

    witnesses, request = _request_findings(_State(), "sketch")
    assert request is not None
    assert [item["field"] for item in witnesses] == ["quality_mode"]
    assert witnesses[0]["collector"] == "OB-DEPRECATION"
    assert witnesses[0]["supplied"] == "draft"


def test_render_figure_exposes_the_enums_not_bare_strings():
    import nodes.postprocess.tools.figure  # noqa: F401  —— 注册
    from core.tool_registry import _REGISTRY

    tools = vars(_REGISTRY)["tools"]
    props = tools["render_figure"].parameters_schema["properties"]
    assert props["asset_kind"]["enum"] == sorted(ASSET_KINDS)
    assert props["purpose"]["enum"] == sorted(PURPOSES)


def test_caller_facing_contract_lists_the_vocabulary():
    """callee_contracts hook 把 expected_inputs 注入调用方 —— 词表必须在里面。"""
    from core.loader import load_harness

    contract = str(load_harness("postprocess").expected_inputs["visual_requests"])
    for kind in ("quantitative", "schematic", "composite"):
        assert kind in contract, f"调用方契约没提到 asset_kind={kind!r}"
    for purpose in PURPOSES:
        assert purpose in contract, f"调用方契约没提到 purpose={purpose!r}"
