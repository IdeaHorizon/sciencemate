"""Dispatch facts never turn private task inputs into conversation text."""
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
            if (record := json.loads(line)).get("event") == "node_dispatched"]


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
def test_actual_task_is_delivered_without_becoming_display_text(tmp_path, note):
    state = _state(tmp_path)
    request = {"research_question": "PRIVATE_TASK\n" * 500}
    received = []

    async def child(**kwargs):
        received.append(kwargs["node_inputs"])
        raise RuntimeError("reached child executor")

    with patch("core.executor.execute_node", child), pytest.raises(RuntimeError):
        asyncio.run(_run_node_tool(state, node_type="literature", node_inputs=request,
                                   user_note=note))
    assert received[0]["research_question"] == request["research_question"]
    announcement = _announcements(state)[0]
    assert announcement["node_type"] == "literature"
    assert announcement["dispatch_id"].startswith("disp_")
    assert "user_note" not in announcement
    assert "PRIVATE_TASK" not in json.dumps(announcement)
    start = next(json.loads(line) for line in state.transcript_path.read_text().splitlines()
                 if json.loads(line).get("event") == "subagent_call_start")
    assert announcement["dispatch_id"] == start["dispatch_id"]


@pytest.mark.parametrize("node,inputs", [
    ("experiment", {"experiment_spec": "PRIVATE_EXPERIMENT", "stage": "toolchain_build"}),
    ("_reviewer", {"artifact_id": "PRIVATE_ARTIFACT", "source_node_type": "experiment"}),
    ("_curator", {"mode": "dreaming", "internal_context": "PRIVATE_CONTEXT"}),
    ("analysis", {"research_goal": {"private": "PRIVATE_STRUCTURED_GOAL"}}),
])
def test_all_dispatch_paths_emit_only_identity(tmp_path, node, inputs):
    state = _state(tmp_path)
    received = []

    async def child(**kwargs):
        received.append(kwargs["node_inputs"])
        raise RuntimeError("reached child executor")

    # Task identity is orthogonal to this content-routing contract. Keep every
    # other validation and the real dispatch/checkpoint/transcript code present.
    with patch("shared.tools.run_node._resolve_task_identity", return_value=(None, None)), \
            patch("core.executor.execute_node", child), pytest.raises(RuntimeError):
        asyncio.run(_run_node_tool(state, node_type=node, node_inputs=inputs,
                                  user_note="PRIVATE_LEGACY_NOTE"))
    assert all(received[0][key] == value for key, value in inputs.items())
    announcement = _announcements(state)[0]
    assert set(announcement) == {"event", "at", "node_type", "dispatch_id", "background"}
    assert "PRIVATE" not in json.dumps(announcement)


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


def test_parallel_dispatch_keeps_inputs_private_and_each_dispatch_distinct(tmp_path):
    state = _state(tmp_path)
    received = []

    async def child(**kwargs):
        received.append(kwargs["node_inputs"])
        raise RuntimeError("stop before payload execution")

    jobs = [
        {"node_type": "literature", "node_inputs": {"research_question": "PRIVATE_FIRST"},
         "user_note": "PRIVATE_NOTE"},
        {"node_type": "literature", "node_inputs": {"research_question": "PRIVATE_SECOND"}},
    ]
    with patch("core.executor.execute_node", child):
        asyncio.run(_run_nodes_parallel(state, jobs=jobs))
    assert {r["research_question"] for r in received} == {"PRIVATE_FIRST", "PRIVATE_SECOND"}
    announcements = _announcements(state)
    assert len(announcements) == 2
    assert len({a["dispatch_id"] for a in announcements}) == 2
    assert "PRIVATE" not in json.dumps(announcements)


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
