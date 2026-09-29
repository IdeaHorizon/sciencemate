"""Observation must hand downstream a source-anchored, directly plottable table."""

from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.state import State
from nodes.observation.tools.observation_contract import audit_observation_results_shape
from shared.lib.artifact_policy import is_permanent
from shared.tools.builtin import _save_artifact


@pytest.fixture()
def state(tmp_path) -> State:
    bootstrap(force=True)
    return State.new(node_type="observation", base_dir=tmp_path)


def _log(state: State) -> str:
    return state.save_artifact(
        "observation_log",
        "linked-log",
        "## Verdict\nSupported\n## Credibility\nSource-anchored.",
        metadata={"mode": "confirmatory"},
    )["id"]


def _timeline_metadata(log_ref: str) -> dict:
    return {
        "schema_version": 1,
        "table_kind": "event_timeline",
        "observation_log_ref": log_ref,
        "field_roles": {
            "item_id": "item_id",
            "lane": "lane",
            "start": "start",
            "end": "end",
            "label": "label",
            "kind": "kind",
            "source_ref": "source_ref",
        },
        "event_timeline_contract": {
            "lane_order": ["Material", "Discourse"],
            "time_label": "Year CE",
        },
    }


@pytest.mark.asyncio
async def test_source_anchored_timeline_can_be_saved(state: State) -> None:
    result = await _save_artifact(
        state,
        "observation_results",
        "roast-beef-timeline",
        content=(
            "item_id,lane,start,end,label,kind,source_ref\n"
            "m1,Material,1100,1500,Elite abundance,interval,ISBN-1\n"
            "d1,Discourse,1620,,Elite-language trope,point,ARCH-1\n"
        ),
        metadata=_timeline_metadata(_log(state)),
    )
    assert result["status"] == "success", result
    record = state.read_artifact(result["id"])
    assert record and record["metadata"]["table_kind"] == "event_timeline"


@pytest.mark.asyncio
async def test_result_rows_without_source_anchors_are_rejected(state: State) -> None:
    result = await _save_artifact(
        state,
        "observation_results",
        "unanchored",
        content=(
            "item_id,lane,start,end,label,kind,source_ref\n"
            "m1,Material,1100,1500,Elite abundance,interval,\n"
        ),
        metadata=_timeline_metadata(_log(state)),
    )
    # 判决拆除批 3w（obs 533 降格）：逐行 source_ref 缺失照存盘，
    # 缺项如实写进 metadata.advisories。
    assert result["status"] == "success", result
    record = state.read_artifact(result["id"])
    assert "source_ref" in (record["metadata"].get("advisories") or {})


def test_timeline_does_not_accept_a_fake_interval_or_long_plot_prose(state: State) -> None:
    audit = audit_observation_results_shape(
        {
            "content": (
                "item_id,lane,start,end,label,kind,source_ref\n"
                "m1,Material,1500,1100,"
                + "A" * 65
                + ",interval,ISBN-1\n"
            ),
            "metadata": _timeline_metadata(_log(state)),
        }
    )
    # 假 interval（end<=start，数据不可画）仍拒；「label ≤64 字符」已删
    # （判决拆除批 3w，obs 545 档一：任意审美阈值）。
    assert audit["passed"] is False
    assert "row.1.end" in audit["failures"]
    assert "short_labels" not in audit["failures"]


def test_radar_requires_comparable_direction_consistent_upstream_values(state: State) -> None:
    audit = audit_observation_results_shape(
        {
            "content": (
                "axis,value,group,source_ref\n"
                "Coverage,0.8,A,S1\nDirectness,1.2,A,S2\nTriangulation,0.5,A,S3\n"
            ),
            "metadata": {
                "schema_version": 1,
                "table_kind": "radar_profile",
                "observation_log_ref": _log(state),
                "field_roles": {
                    "axis": "axis",
                    "value": "value",
                    "group": "group",
                    "source_ref": "source_ref",
                },
                "radar_contract": {
                    "axis_order": ["Coverage", "Directness", "Triangulation"],
                    "value_range": [0, 1],
                    "direction_consistent": False,
                },
            },
        }
    )
    # 真实值域检查（值必须在 [0,1]）保留；「direction_consistent 必须抄
    # true」已删（判决拆除批 3w，obs 666 档一：抄固定字面量框架不验）。
    assert audit["passed"] is False
    assert "radar_values" in audit["failures"]
    assert "radar_contract.direction_consistent" not in audit["failures"]


def test_observation_results_is_a_required_permanent_owner_output() -> None:
    harness = load_harness("observation")
    assert "observation_results" in harness.required_output_artifact_types
    assert is_permanent("observation_results")
