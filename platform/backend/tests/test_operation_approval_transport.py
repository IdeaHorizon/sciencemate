"""Use the actual permission producer, not a hand-built ideal card."""

import pytest
from core import approvals, tool_registry
from core.pause import PauseEvent
from core.state import State
from shared.lib import dangerous_commands as danger

from app.api.v1.chat import ChoiceAnswer, turn_input_for
from app.services.execution_ingest import IngestContext, TranscriptAdapter
from app.services.local_execution import _offer_id


@pytest.mark.asyncio
async def test_original_operation_identity_reaches_the_platform_and_returns_as_a_choice(
    tmp_path, monkeypatch
):
    state = State.new("experiment", tmp_path, session_id="s")
    state.hook_state["_current_tool_call_id_being_executed"] = "original"
    monkeypatch.setattr(danger, "BYPASS_ENABLED", False)
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", set())

    async def execute(*, state, command, **_):
        return danger.build_pause_payload(
            state, tool="wire_probe", text=command, category="audit", preview=command
        )

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="wire_probe",
            description="probe",
            parameters_schema={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        execute,
    )
    result = await tool_registry.execute("wire_probe", state, command="printf bounded-probe")
    pause = PauseEvent.from_payload(result["pause_event"], pending_tool_call_id="original")
    state.append_transcript("loop_pause", **pause.to_dict())
    import json

    raw = json.loads(state.transcript_path.read_text().splitlines()[-1])
    event = TranscriptAdapter().adapt(
        raw,
        context=IngestContext(
            tenant_id="t", workspace_id="w", project_id="p", session_id="s", run_id=state.run_id
        ),
        adapter_state={},
        event_id="event",
    )
    assert event.kind == "run.paused"
    offer = event.payload["offer"]
    assert offer["offer_id"] == pause.offer_id == _offer_id(result["pause_event"])
    assert offer["option_details"][0]["id"] == "approve"
    assert offer["option_details"][1]["id"] == "deny"
    assert "printf bounded-probe" in offer["context"]
    message, choice = turn_input_for(
        ChoiceAnswer(kind="choice", offer_id=offer["offer_id"], choice_id="approve", note="")
    )
    assert message == ""
    assert approvals.choice_for(pause.to_dict(), choice) == "approve"


@pytest.mark.asyncio
async def test_each_child_permission_relay_reaches_the_platform_with_its_exact_offer(
    tmp_path, monkeypatch
):
    import json

    from core import pause_driver
    from core.harness import NodeHarness
    from core.pause import PausedRunContext, clear_all

    child = State.new("experiment", tmp_path / "child", session_id="s")
    parent = State.new("_orchestrator", tmp_path / "parent", session_id="s")
    effects = []
    monkeypatch.setattr(danger, "BYPASS_ENABLED", False)
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", set())

    async def execute(*, state, command, **_):
        if not danger.is_confirmed(state, command):
            return danger.build_pause_payload(
                state,
                tool="relay_permission_probe",
                text=command,
                category="audit",
                preview=command,
            )
        effects.append(command)
        return {"status": "success"}

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="relay_permission_probe",
            description="bounded relay probe",
            parameters_schema={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        execute,
    )
    context = PausedRunContext(
        run_id=parent.run_id,
        state=parent,
        messages=[],
        harness=NodeHarness(node_type="_orchestrator"),
        llm=None,
        pending_tool_call_id="parent-dispatch",
        pause_event=PauseEvent(question="child"),
    )
    offers = []
    try:
        for number in range(2):
            call_id = f"child-operation-{number}"
            command = f"printf bounded-effect-{number}"
            child.hook_state["_current_tool_call_id_being_executed"] = call_id
            result = await tool_registry.execute("relay_permission_probe", child, command=command)
            event = PauseEvent.from_payload(result["pause_event"], pending_tool_call_id=call_id)
            pause_driver._present_again(context, event, reason="child_paused_again")
            raw = json.loads(parent.transcript_path.read_text().splitlines()[-1])
            projected = TranscriptAdapter().adapt(
                raw,
                context=IngestContext(
                    tenant_id="t",
                    workspace_id="w",
                    project_id="p",
                    session_id="s",
                    run_id=parent.run_id,
                ),
                adapter_state={},
                event_id=f"relay-{number}",
            )
            offer = projected.payload["offer"]
            assert offer["offer_id"] == event.offer_id
            assert command in offer["context"]
            assert projected.payload["askingNodeType"] == "experiment"
            assert raw["pending_tool_call_id"] == "parent-dispatch"
            _, choice = turn_input_for(
                ChoiceAnswer(
                    kind="choice",
                    offer_id=offer["offer_id"],
                    choice_id="approve",
                    note="",
                )
            )
            record = approvals.decide(
                child, event.to_dict(), approvals.choice_for(event.to_dict(), choice)
            )
            approved = await approvals.run_approved(child, record["operation_id"])
            assert approved["status"] == "success", approved
            replayed = await approvals.run_approved(child, record["operation_id"])
            assert replayed["status"] == "success", replayed
            offers.append(offer["offer_id"])
        assert len(set(offers)) == 2
        assert effects == ["printf bounded-effect-0", "printf bounded-effect-1"]
    finally:
        clear_all()
