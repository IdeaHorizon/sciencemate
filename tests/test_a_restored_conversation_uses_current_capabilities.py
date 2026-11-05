"""An upgrade must update instructions without discarding the user's history."""

from __future__ import annotations

import io
import json

import pytest

from core.conversation_store import checkpoint_path, msg_to_jsonable, save_conversation
from core.llm import LLMMessage, LLMResponse, opening_system_prompt
from platform_runtime import _temporary_home, run_platform_request, serve_jsonl


class InspectingModel:
    stream_display = None

    def __init__(self):
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append((list(messages), kwargs))
        assert messages[0].role == "system"
        assert "resource(action=connect)" in messages[0].content
        assert "OLD-RELEASE-CANNOT-SSH" not in messages[0].content
        assert len([m for m in messages if m.role == "system"]) == 1
        assert any(m.content == "Keep my research context" for m in messages)
        call = next(m for m in messages if m.tool_calls)
        result = next(m for m in messages if m.tool_call_id == "old-call")
        assert call.tool_calls[0]["id"] == result.tool_call_id
        assert result.content == "old result"
        tools = {t["function"]["name"] for t in kwargs["tools"]}
        assert {"resource", "remote_exec", "remote_file", "remote_job"} <= tools
        return LLMResponse(content="Ready", tool_calls=[], finish_reason="stop",
                           usage={"total_tokens": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["request", "serve"])
@pytest.mark.parametrize("store", ["conversation", "checkpoint"])
async def test_upgrade_rebuilds_only_opening_instructions(tmp_path, entry, store):
    import chat

    home = tmp_path / "home"
    base = tmp_path / "runs"
    identity = dict(tenant_id="tenant-upgrade", session_id="session-upgrade") if entry == "serve" else {}
    with _temporary_home(home):
        state = chat._make_or_load_orchestrator_state("project-upgrade", base, **identity)
        old = [
            opening_system_prompt("OLD-RELEASE-CANNOT-SSH"),
            LLMMessage(role="user", content="Keep my research context"),
            LLMMessage(role="assistant", tool_calls=[{
                "id": "old-call", "type": "function",
                "function": {"name": "run_bash", "arguments": '{"cmd":"hostname"}'},
            }]),
            LLMMessage(role="tool", tool_call_id="old-call", name="run_bash",
                       content="old result"),
        ]
        state.scratchpad = "Existing notes"
        state.tokens_used = 17
        if store == "conversation":
            save_conversation(state, old)
        else:
            checkpoint_path(state).write_text(json.dumps({
                "turn": 2,
                "messages": [msg_to_jsonable(m) for m in old],
            }))
        receipt = state.root / "first_turn_node_inputs.json"
        receipt.write_text('{"unchanged":true}')

    model = InspectingModel()
    config = dict(project_id="project-upgrade", home_dir=str(home), state_dir=str(base), **identity)
    events = []
    emit = lambda kind, **fields: events.append(dict(type=kind, **fields))
    if entry == "request":
        await run_platform_request(dict(config, request_id="upgrade", message="Retry SSH"),
                                   emit, llm=model)
    else:
        stream = io.StringIO("".join(json.dumps(r) + "\n" for r in [
            dict(config, op="init", request_id="init-upgrade"),
            dict(op="turn", request_id="retry-upgrade", message="Retry SSH"),
            dict(op="terminate", request_id="end-upgrade"),
        ]))
        await serve_jsonl(stream, emit, llm=model)
    assert model.requests, events
    assert receipt.read_text() == '{"unchanged":true}'
    saved = json.loads((state.root / "conversation.json").read_text())
    if store == "conversation":
        assert saved["scratchpad"] == "Existing notes"
        assert saved["tokens_used"] == 18


def test_shell_diagnostics_offer_the_remote_transport():
    from core.bootstrap import bootstrap
    from core.tool_registry import get_tool
    from shared.lib.dangerous_commands import read_only_shell_refusal

    bootstrap()
    for name in ["run_bash", "execute_python"]:
        description = get_tool(name).description
        assert "resource(action='connect')" in description
        assert "remote_exec" in description
    refusal = read_only_shell_refusal("", "Operation not permitted", 1)
    assert "resource(action='connect')" in refusal["read_only_shell_note"]
    assert "remote_job" in refusal["read_only_shell_note"]
