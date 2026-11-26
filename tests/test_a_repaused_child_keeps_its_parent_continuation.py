"""A second child question must not strand its completed parent call (#1435)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent_loop import _pause_loop, _persist_messages_checkpoint, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import get_deepest_paused, get_paused_run
from core.pause_driver import drive_pause_chain
from core.state import State


def _harness(node_type):
    return NodeHarness(
        node_type=node_type, system_prompt="test", tools=["request_human_input"],
        max_turns=5, kb_query="_disable",
    )


def _question(call_id):
    return LLMResponse(content="", tool_calls=[{
        "id": call_id, "type": "function", "function": {
            "name": "request_human_input", "arguments": json.dumps({"question": call_id}),
        },
    }], finish_reason="tool_calls", usage={})


async def _paused_family(tmp_path, asks_again):
    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs")
    child = State.new("experiment", tmp_path / "runs")
    child.parent_run_id = parent.run_id
    child.depth = 1
    child_llm = MagicMock()
    child_llm.chat = AsyncMock(side_effect=[
        _question("child-first"),
        *([_question("child-second")] if asks_again else []),
        LLMResponse(content="child finished", tool_calls=[], finish_reason="stop", usage={}),
    ])
    initial = await run_loop(
        _harness("experiment"), child, [LLMMessage(role="user", content="go")], child_llm,
    )
    assert initial.status == "paused"
    # This is the exact edge added by _run_child_dispatch after the first pause.
    get_paused_run(child.run_id).parent_tool_call_id = "parent-call"
    call = {"id": "parent-call", "type": "function", "function": {
        "name": "run_node", "arguments": '{"node_type":"experiment"}',
    }}
    paused_result = {
        "status": "pause", "child_run_id": child.run_id,
        "pause_event": initial.pause_event.to_dict(),
    }
    parent_messages = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(role="tool", name="run_node", tool_call_id="parent-call",
                   content=json.dumps(paused_result)),
    ]
    _persist_messages_checkpoint(parent, parent_messages, turn=1)
    seen = []
    parent_llm = MagicMock()

    async def complete_parent(messages, **kwargs):
        saved = json.loads((parent.root / "messages_checkpoint.json").read_text())
        saved_receipt = next(m for m in saved["messages"]
                             if m.get("tool_call_id") == "parent-call")
        assert json.loads(saved_receipt["content"])["status"] == "success"
        assert not (parent.root / "pause_pending.json").exists()
        seen.extend(json.loads(m.content) for m in messages
                    if m.role == "tool" and m.tool_call_id == "parent-call")
        return LLMResponse(content="parent finished", tool_calls=[], finish_reason="stop", usage={})

    parent_llm.chat = AsyncMock(side_effect=complete_parent)
    _pause_loop(_harness("_orchestrator"), parent, parent_messages, parent_llm,
                call, paused_result, turn=1, all_tool_calls=[], max_turns=5)
    return parent, child, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("asks_again", [False, True])
@pytest.mark.parametrize("family_count", [1, 2])
async def test_child_questions_deliver_completion_once_without_reasking_the_parent(
    tmp_path, asks_again, family_count,
):
    families = [await _paused_family(tmp_path / str(index), asks_again)
                for index in range(family_count)]
    answered = []

    async def answer_only_current_child(pause):
        answered.append(pause.pending_tool_call_id)
        assert pause.pending_tool_call_id in {"child-first", "child-second"}, (
            "A completed child's consumed question was re-presented by its parent"
        )
        return "continue"

    text = await drive_pause_chain(ask_fn=answer_only_current_child)
    assert answered.count("child-first") == family_count
    assert answered.count("child-second") == (family_count if asks_again else 0)
    assert text == "parent finished"
    for parent, child, seen in families:
        assert len(seen) == 1 and seen[0]["status"] == "success"
        assert seen[0]["child_run_id"] == child.run_id
        assert not (parent.root / "pause_pending.json").exists()
    assert get_deepest_paused() is None
