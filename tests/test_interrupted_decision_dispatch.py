"""An interrupted decision observes its original child and never dispatches twice."""

import json
from datetime import UTC

import pytest

from core.agent_loop import _persist_messages_checkpoint, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State


def pair(tmp_path, phase):
    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", project_id="p", session_id="s")
    args = {
        "source_node_type": "observation",
        "producing_run_id": "old-producer",
        "action": "revise",
    }
    messages = [
        LLMMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "decision-call",
                    "type": "function",
                    "function": {"name": "present_decision_package", "arguments": json.dumps(args)},
                }
            ],
        )
    ]
    parent.hook_state["_current_tool_call_id_being_executed"] = "decision-call"
    _persist_messages_checkpoint(parent, messages, turn=1)
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    path = parent.root / "messages_checkpoint.json"
    saved = json.loads(path.read_text())
    saved.pop("child_dispatches", None)  # actual pre-handle decision
    path.write_text(json.dumps(saved))
    parent.append_transcript(
        "decision_action_authorized",
        producing_run_id="old-producer",
        chosen_action="revise",
        authorized_target_node="observation",
    )
    parent.append_transcript(
        "subagent_call_start",
        child_node_type="observation",
        dispatch_id="dispatch-one",
        parent_tool_call_id="decision-call",
    )
    child = State.new("observation", parent.root.parent, project_id="p", session_id="s")
    child.append_transcript(
        "run_start",
        node_type="observation",
        session_id="s",
        parent_run_id=parent.run_id,
        parent_dispatch_id="dispatch-one",
    )
    child.append_transcript("tool_call", name="execute_python")
    if phase != "unknown":
        _persist_messages_checkpoint(child, [LLMMessage(role="user", content="work")], turn=1)
    if phase == "terminal":
        child.append_transcript("run_end", status="completed")
        (child.root / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": child.run_id,
                    "status": "completed",
                    "artifacts": [],
                    "final_text_preview": "saved result",
                    "state_dir": str(child.root),
                }
            )
        )
    import chat

    chat._repair_message_tool_protocol(messages)
    return parent, child, messages, args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,status",
    [
        ("terminal", "recovered"),
        ("interrupted", "interrupted"),
        ("live", "in_progress"),
        ("unknown", "outcome_unknown"),
    ],
)
async def test_decision_recovery_identifies_the_same_child_twice(
    tmp_path, monkeypatch, phase, status
):
    import shared.tools.run_node as owner
    from core import tool_registry

    parent, child, messages, _ = pair(tmp_path, phase)
    if phase == "live":
        monkeypatch.setattr(owner, "run_has_live_executor", lambda _: True)

    async def forbidden(*a, **kw):
        pytest.fail("recovery repeated the decision or dispatched another child")

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "present_decision_package", forbidden)
    monkeypatch.setitem(tool_registry._REGISTRY.executors, "run_node", forbidden)

    class LLM:
        async def chat(self, messages, **kw):
            result = json.loads(
                next(m.content for m in messages if m.tool_call_id == "decision-call")
            )
            assert result["do_not_resubmit"] is True
            receipt = result["execution"]
            assert receipt["child_run_id"] == child.run_id and receipt["status"] == status
            assert receipt["do_not_start_fresh"] is True
            if phase != "terminal":
                assert receipt.get("child_status") != "completed"
            return LLMResponse(
                content="Observe the existing child", tool_calls=[], finish_reason="stop", usage={}
            )

    harness = NodeHarness(
        node_type="_orchestrator",
        system_prompt="test",
        tools=["present_decision_package"],
        max_turns=2,
    )
    for _ in range(2):
        await run_loop(harness, parent, messages, LLM())
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.mark.parametrize(
    "boundary", ["foreign-producer", "ambiguous", "old-call", "foreign-session"]
)
@pytest.mark.asyncio
async def test_decision_recovery_requires_an_unambiguous_current_binding(tmp_path, boundary):
    from shared.tools.library.decision_package import _recover_decision_dispatch

    parent, child, messages, args = pair(tmp_path, "terminal")
    if boundary == "foreign-producer":
        args["producing_run_id"] = "someone-else"
    elif boundary == "ambiguous":
        parent.append_transcript(
            "decision_action_authorized",
            producing_run_id="old-producer",
            chosen_action="revise",
            authorized_target_node="observation",
        )
    elif boundary == "old-call":
        parent.hook_state["_current_tool_call_id_being_executed"] = "decision-call"
        path = parent.root / "messages_checkpoint.json"
        saved = json.loads(path.read_text())
        saved["tool_dispatches"] = {}
        from datetime import datetime

        saved["written_at"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(saved))
    else:
        child.transcript_path.write_text("")
        child.session_id = "foreign"
        child.append_transcript(
            "run_start",
            node_type="observation",
            session_id="foreign",
            parent_run_id=parent.run_id,
            parent_dispatch_id="dispatch-one",
        )
    assert (
        await _recover_decision_dispatch(args, state=parent, tool_call_id="decision-call") is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_saved_decision_uses_original_execution_owner(tmp_path, monkeypatch, paused):
    import shared.tools.run_node as owner
    from core import child_dispatch
    from shared.tools.library.decision_package import _recover_decision_dispatch

    parent, child, _, args = pair(tmp_path, "interrupted")
    parent.hook_state["_current_tool_call_id_being_executed"] = "decision-call"
    child_dispatch.prepare(
        parent,
        {
            "dispatch_id": "dispatch-one",
            "node_type": "observation",
            "child_run_id": child.run_id,
            "requested_inputs": None,
            "execution": {"node_type": "observation"},
        },
    )
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    seen = []

    async def resume(state, record):
        assert state is parent
        seen.append(record["child_run_id"])
        return {"status": "pause" if paused else "success", "child_run_id": child.run_id}

    monkeypatch.setattr(owner, "_resume_child_dispatch", resume)
    result = await _recover_decision_dispatch(args, state=parent, tool_call_id="decision-call")
    assert seen == [child.run_id]
    execution = result["result"] if paused else result["result"]["execution"]
    assert execution["child_run_id"] == child.run_id
    assert execution["status"] == ("pause" if paused else "success")
