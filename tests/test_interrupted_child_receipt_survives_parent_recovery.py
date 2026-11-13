"""Old checkpoints import exact child edges; normal recovery uses saved handles."""
import json

import pytest

from core.agent_loop import _persist_messages_checkpoint
from core.bootstrap import bootstrap
from core.llm import LLMMessage
from core.state import State


@pytest.fixture
def child_pair(tmp_path):
    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", session_id="s")
    args = {"node_type": "_reviewer", "user_note": "review existing results"}
    messages = [LLMMessage(role="assistant", tool_calls=[{
        "id": "original-child-call", "type": "function",
        "function": {"name": "run_node", "arguments": json.dumps(args)},
    }])]
    parent.hook_state["_current_tool_call_id_being_executed"] = "original-child-call"
    _persist_messages_checkpoint(parent, messages, turn=1)
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    path = parent.root / "messages_checkpoint.json"
    saved = json.loads(path.read_text())
    saved.pop("child_dispatches", None)  # actual pre-handle format
    path.write_text(json.dumps(saved))
    parent.append_transcript("subagent_call_start", child_node_type="_reviewer",
                             dispatch_id="original-dispatch", parent_tool_call_id="original-child-call")
    child = State.new("_reviewer", parent.root.parent, session_id="s")
    return parent, child, args


def start(parent, child, dispatch="original-dispatch", session="s"):
    child.session_id = session
    child.append_transcript("run_start", node_type="_reviewer", parent_run_id=parent.run_id,
                            parent_dispatch_id=dispatch, session_id=session)
    _persist_messages_checkpoint(child, [LLMMessage(role="user", content="review checkpoint")], turn=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("old_starts", [0, 6])
async def test_import_recovers_the_exact_resumed_child_then_stops_scanning(child_pair, monkeypatch, terminal, old_starts):
    import shared.tools.run_node as owner
    parent, child, args = child_pair
    for i in range(old_starts):
        start(parent, child, dispatch=f"older-{i}")
    start(parent, child)
    if terminal:
        child.append_transcript("run_end", status="completed")
        child.write_summary({"run_id": child.run_id, "status": "completed"})
    calls = []

    async def same_path(state, node_type, inputs, harness, autoresolved, execution, task_id,
                        dispatch_id, deviations, *, summary=None, run_post_flow=True):
        calls.append(execution)
        assert execution["resume_run_id"] == child.run_id
        assert bool(summary) == terminal
        return {"status": "success", "child_run_id": child.run_id}

    monkeypatch.setattr(owner, "_run_child_dispatch", same_path)
    result = await owner._recover_run_node(args, state=parent, tool_call_id="original-child-call")
    assert result["result"]["child_run_id"] == child.run_id
    def no_import(*args, **kwargs):
        pytest.fail("already bound child was selected by scanning again")
    monkeypatch.setattr(owner, "_import_child_dispatch", no_import)
    await owner._recover_run_node(args, state=parent, tool_call_id="original-child-call")
    assert len(calls) == 2
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unrelated_dispatch", "foreign_session", "ambiguous_children"])
async def test_import_never_guesses_a_child(child_pair, boundary):
    import shared.tools.run_node as owner
    parent, child, args = child_pair
    start(parent, child, dispatch="other" if boundary == "unrelated_dispatch" else "original-dispatch",
          session="other" if boundary == "foreign_session" else "s")
    if boundary == "ambiguous_children":
        other = State.new("_reviewer", parent.root.parent, session_id="s")
        start(parent, other)
    assert await owner._recover_run_node(args, state=parent, tool_call_id="original-child-call") is None
