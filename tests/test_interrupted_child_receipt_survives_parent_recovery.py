"""The existing run_node owner identifies a child; Core does not replay a dispatch."""

import json

import pytest

from core.agent_loop import _persist_messages_checkpoint, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State


@pytest.fixture
def child_pair(tmp_path):
    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", project_id="p")
    call_id = "original-child-call"
    args = {"node_type": "_reviewer", "user_note": "review the existing results"}
    messages = [
        LLMMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_node",
                        "arguments": json.dumps(args),
                    },
                }
            ],
        )
    ]
    parent.hook_state["_current_tool_call_id_being_executed"] = call_id
    _persist_messages_checkpoint(parent, messages, turn=1)
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    parent.append_transcript(
        "subagent_call_start",
        child_node_type="_reviewer",
        dispatch_id="original-dispatch",
        parent_tool_call_id=call_id,
    )
    child = State.new("_reviewer", parent.root.parent, project_id="p")
    child.append_transcript(
        "run_start",
        node_type="_reviewer",
        parent_run_id=parent.run_id,
        parent_dispatch_id="original-dispatch",
    )
    _persist_messages_checkpoint(
        child, [LLMMessage(role="user", content="review checkpoint")], turn=1
    )
    import chat

    chat._repair_message_tool_protocol(messages)
    return parent, child, messages, args


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_parent_consumes_its_existing_child_receipt_without_another_dispatch(
    child_pair,
    monkeypatch,
    terminal,
):
    from core import tool_registry
    from shared.tools.run_node import _resumable_run_for
    from tests.test_transcript_reconstructs_requests import _rebuild_and_check

    parent, child, messages, _ = child_pair
    if terminal:
        child.append_transcript("run_end", status="completed")
        (child.root / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": child.run_id,
                    "status": "completed",
                    "state_dir": str(child.root),
                    "final_text_preview": "the saved review",
                    "artifacts": [],
                }
            )
        )
    else:
        assert _resumable_run_for(parent, "_reviewer")["run_id"] == child.run_id

    async def forbidden(*args, **kwargs):
        pytest.fail("recovery blindly dispatched run_node instead of identifying its child")

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "run_node", forbidden)

    class LLM:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            result = json.loads(
                next(m.content for m in messages if m.tool_call_id == "original-child-call")
            )
            assert result["child_run_id"] == child.run_id
            assert result["do_not_start_fresh"] is True
            if terminal:
                assert result["child_status"] == "completed"
            else:
                assert result["resume_run_id"] == child.run_id
            return LLMResponse(
                content="use the original child and its existing owner path",
                tool_calls=[],
                finish_reason="stop",
                usage={},
            )

    llm = LLM()
    node = NodeHarness(
        node_type="_orchestrator", system_prompt="test", tools=["run_node"], max_turns=2
    )
    await run_loop(node, parent, messages, llm)
    assert llm.calls == 1 and _rebuild_and_check(parent) == 1
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.mark.parametrize("boundary", ["unrelated_dispatch", "live_child", "human_pause"])
def test_receipt_owner_does_not_guess_or_override_an_existing_driver(
    child_pair, monkeypatch, boundary
):
    import shared.tools.run_node as owner

    parent, child, _, args = child_pair
    if boundary == "unrelated_dispatch":
        (child.root / "transcript.jsonl").write_text("")
        child.append_transcript(
            "run_start",
            node_type="_reviewer",
            parent_run_id=parent.run_id,
            parent_dispatch_id="unrelated",
        )
    elif boundary == "live_child":
        monkeypatch.setattr(owner, "run_has_live_executor", lambda _: True)
    else:
        child.append_transcript("run_end", status="paused")
        (child.root / "summary.json").write_text(json.dumps({"status": "paused"}))
    assert (
        owner._recover_run_node_receipt(args, state=parent, tool_call_id="original-child-call")
        is None
    )
