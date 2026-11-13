"""Dispatch progress is a projection of the actual task, not a second brief."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.state import State
from core.tool_registry import get_tool
from shared.tools.run_node import _run_node_tool, _run_nodes_parallel

bootstrap()


def _state(tmp_path):
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    state.hook_state["_callable_nodes"] = ["*"]
    return state


def _announcements(state):
    if not state.transcript_path.exists():
        return []
    return [record for line in state.transcript_path.read_text().splitlines()
            if (record := json.loads(line)).get("event") == "node_dispatch_announced"]


@pytest.mark.parametrize("inputs", [None, {}, {"mode": "targeted_lookup"},
                                        {"research_question": " \n "}])
def test_missing_actual_task_never_launches_a_child(tmp_path, inputs):
    state = _state(tmp_path)
    with patch("core.executor.execute_node") as execute:
        result = asyncio.run(_run_node_tool(
            state, node_type="literature", node_inputs=inputs, user_note="placeholder"))
    assert result["error_code"] == "missing_dispatch_task"
    execute.assert_not_called()
    assert not _announcements(state)
    assert not state.hook_state.get("_dispatched_any_child")


@pytest.mark.parametrize("note", [None, "placeholder", "unrelated claim"])
def test_actual_task_is_both_delivered_and_announced(tmp_path, note):
    state = _state(tmp_path)
    request = {"research_question": "查清资源匹配对照是否足以识别改进器迁移。"}
    received = []

    async def child(**kwargs):
        received.append(kwargs["node_inputs"])
        raise RuntimeError("reached child executor")

    with patch("core.executor.execute_node", child), pytest.raises(RuntimeError):
        asyncio.run(_run_node_tool(state, node_type="literature", node_inputs=request,
                                   user_note=note))
    assert received[0]["research_question"] == request["research_question"]
    assert _announcements(state)[0]["user_note"] == request["research_question"]


def test_no_separate_announcement_argument_in_either_public_schema():
    single = get_tool("run_node").parameters_schema
    parallel = get_tool("run_nodes_parallel").parameters_schema["properties"]["jobs"]["items"]
    for schema in (single, parallel):
        assert "user_note" not in schema["properties"]
        assert "user_note" not in schema["required"]


def test_parallel_dispatch_cannot_turn_a_note_into_a_task(tmp_path):
    state = _state(tmp_path)
    with patch("core.executor.execute_node") as execute:
        result = asyncio.run(_run_nodes_parallel(
            state, jobs=[{"node_type": "literature", "user_note": "probe"}]))
    execute.assert_not_called()
    assert "missing_dispatch_task" in json.dumps(result)
    assert not _announcements(state)


def test_an_invalid_delivery_path_never_announces_a_dispatch(tmp_path):
    state = _state(tmp_path)
    with patch("core.executor.execute_node") as execute:
        result = asyncio.run(_run_node_tool(
            state, node_type="literature", node_inputs={"research_question": "核对已有证据"},
            deliverable={"artifact_type": "survey_report"}))
    assert result["status"] == "error"
    execute.assert_not_called()
    assert not _announcements(state)


def test_structured_research_goal_remains_a_task_input():
    assert load_harness("hypothesis").task_input_fields == ["research_goal", "research_question"]
    assert load_harness("writing").task_input_fields == ["research_question", "rewrite_spec"]
