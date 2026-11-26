"""Declared risk categories use the same frozen invocation as a human answer.

The dispatcher executes the original invocation and returns its real receipt;
no reusable command pass or instruction to ask the model to repeat it exists.
"""

from __future__ import annotations

import json

import pytest

from core import tool_registry
from core.state import State
from shared.lib import dangerous_commands as dc


@pytest.fixture
def operation(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "BYPASS_ENABLED", False)
    monkeypatch.setattr(dc, "PREAUTHORIZED_CATEGORIES", set())
    state = State.new("experiment", tmp_path, session_id="policy-probe")
    state.hook_state["_current_tool_call_id_being_executed"] = "original"
    effects = []

    async def execute(*, state, command="bounded effect", category="external_job_submit", **_):
        if not dc.is_confirmed(state, command):
            return dc.build_pause_payload(
                state,
                tool="policy_approval_probe",
                text=command,
                category=category,
                preview=command,
            )
        effects.append(command)
        return {"status": "success", "executed_command": command}

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="policy_approval_probe",
            description="bounded approval policy regression",
            parameters_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}, "category": {"type": "string"}},
            },
        ),
        execute,
    )
    return state, effects


@pytest.mark.asyncio
async def test_default_still_stops_and_asks(operation):
    state, effects = operation
    result = await tool_registry.execute("policy_approval_probe", state)
    assert result["status"] == "pause" and not effects
    assert not dc.is_confirmed(state, "bounded effect")


@pytest.mark.asyncio
async def test_declared_category_executes_the_original_call_without_model_retry(
    operation, monkeypatch
):
    state, effects = operation
    monkeypatch.setattr(dc, "PREAUTHORIZED_CATEGORIES", {"external_job_submit"})
    result = await tool_registry.execute(
        "policy_approval_probe", state, command="original parameters"
    )
    assert result["status"] == "success"
    assert result["executed_command"] == "original parameters" and effects == [
        "original parameters"
    ]
    assert "approval_contract" not in result
    assert not dc.is_confirmed(state, "original parameters")


@pytest.mark.asyncio
async def test_undeclared_category_still_stops(operation, monkeypatch):
    state, effects = operation
    monkeypatch.setattr(dc, "PREAUTHORIZED_CATEGORIES", {"external_job_submit"})
    result = await tool_registry.execute(
        "policy_approval_probe", state, category="block_device_write"
    )
    assert result["status"] == "pause" and not effects


@pytest.mark.asyncio
async def test_policy_cannot_transfer_to_an_undeclared_later_call(operation, monkeypatch):
    state, effects = operation
    monkeypatch.setattr(dc, "PREAUTHORIZED_CATEGORIES", {"external_job_submit"})
    first = await tool_registry.execute(
        "policy_approval_probe", state, command="original parameters"
    )
    assert first["status"] == "success"
    state.hook_state["_current_tool_call_id_being_executed"] = "another-call"
    second = await tool_registry.execute(
        "policy_approval_probe", state, command="changed parameters", category="block_device_write"
    )
    assert second["status"] == "pause" and effects == ["original parameters"]


@pytest.mark.asyncio
async def test_preauthorization_is_recorded_as_a_policy_decision(operation, monkeypatch):
    state, _ = operation
    monkeypatch.setattr(dc, "PREAUTHORIZED_CATEGORIES", {"external_job_submit"})
    await tool_registry.execute("policy_approval_probe", state)
    records = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    decisions = [row for row in records if row["event"] == "approval_decided"]
    assert len(decisions) == 1
    assert decisions[0]["answered_by"] == "policy" and decisions[0]["choice"] == "approve"
