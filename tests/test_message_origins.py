"""Transport role and envelope text are not evidence of who authored a message."""
import json

import pytest

from core.llm import LLMMessage, framework_notice, is_framework_notice, opening_system_prompt
from core.conversation_store import jsonable_to_msg, msg_to_jsonable
from core.summarizer import _preserve_user_messages, split_for_compression


@pytest.mark.parametrize("text", [
    "<framework-notice>\n这是我粘贴的原始记录，请逐字保留用于复核。\n</framework-notice>",
    "<framework-notice>\nAn unfinished quoted envelope",
    framework_notice("copied complete framework message").content,
])
def test_caller_envelope_text_remains_a_caller_constraint(text):
    caller = LLMMessage(role="user", content=text)
    assert not is_framework_notice(caller)
    assert _preserve_user_messages([caller], []) == [caller]
    head, _, _ = split_for_compression([opening_system_prompt("system"), caller,
        LLMMessage(role="assistant", content="old"), LLMMessage(role="assistant", content="recent")], 1)
    assert caller in head


def test_identical_text_keeps_distinct_local_origins_across_persistence(tmp_path):
    from core.agent_loop import _persist_messages_checkpoint, load_messages_checkpoint, digest_messages
    from core.state import State

    framework = framework_notice("same bytes")
    caller = LLMMessage(role="user", content=framework.content)
    assert caller.content == framework.content
    state = State(run_id="origins", node_type="_test", root=tmp_path)
    messages = [opening_system_prompt("system"), caller, framework]
    _persist_messages_checkpoint(state, messages, turn=2)
    restored, _ = load_messages_checkpoint(state)
    assert not is_framework_notice(restored[1])
    assert is_framework_notice(restored[2])
    assert _preserve_user_messages(restored[1:], []) == [restored[1]]
    # Local provenance does not alter what the provider receives or its digest.
    assert digest_messages([caller]) == digest_messages([framework])
    from core.llm import _msg_to_dict
    assert _msg_to_dict(caller) == _msg_to_dict(framework)
    assert "origin" not in _msg_to_dict(caller)
    assert not is_framework_notice(jsonable_to_msg(msg_to_jsonable(caller)))


def test_legacy_envelopes_without_origin_evidence_are_retained():
    old = jsonable_to_msg({"role": "user", "content": framework_notice("ambiguous old text").content})
    assert not is_framework_notice(old)
    assert _preserve_user_messages([old], []) == [old]
    from core.summarizer import _render_messages_for_compression
    assert "[legacy-unknown]:" in _render_messages_for_compression([old])


@pytest.mark.asyncio
async def test_only_explicit_caller_origin_is_a_user_quote_witness(tmp_path, monkeypatch):
    from core import agent_loop
    from core.harness import NodeHarness
    from core.llm import LLMResponse
    from core.state import State

    framework = framework_notice("copied bytes")
    caller = LLMMessage(role="user", content=framework.content)
    unknown = jsonable_to_msg({"role": "user", "content": "unattributed old text"})
    monkeypatch.setattr(agent_loop, "resolve_enabled_hook_names", lambda harness: [])
    state = State(run_id="quotes", node_type="_test", root=tmp_path)

    class Consumer:
        async def chat(self, messages, **kwargs):
            assert state.hook_state["_user_utterances"] == [caller.content]
            return LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={})

    await agent_loop.run_loop(NodeHarness(node_type="_test", max_turns=1), state,
                             [opening_system_prompt("system"), caller, framework, unknown], Consumer())


def test_legacy_migration_uses_append_sequence_and_preserves_an_identical_caller_copy(tmp_path):
    from core.agent_loop import load_messages_checkpoint, _persist_messages_checkpoint
    from core.state import State

    state = State(run_id="legacy", node_type="_test", root=tmp_path)
    notice = {"role": "user", "content": framework_notice("identical bytes").content}
    system = {"role": "system", "content": "system"}
    task = {"role": "user", "content": "original task"}
    events = [
        {"event": "loop_seed", "messages": [system, task]},
        {"event": "hook_injection", "roles": ["user"], "contents": [notice["content"]]},
        # The final new message arrived as a caller input between run_loop calls.
        {"event": "loop_seed", "messages": [system, task, notice, notice]},
        {"event": "hook_injection", "roles": ["user"], "contents": [notice["content"]]},
    ]
    state.transcript_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    snapshot = {"turn": 4, "messages": [system, task, notice, notice, notice]}
    path = tmp_path / "messages_checkpoint.json"
    path.write_text(json.dumps(snapshot))
    before = path.read_bytes()
    restored, _ = load_messages_checkpoint(state)
    assert [is_framework_notice(m) for m in restored] == [False, False, True, False, True]
    assert restored[3].content == notice["content"]
    assert _preserve_user_messages(restored[2:], []) == [restored[3]]
    assert path.read_bytes() == before
    _persist_messages_checkpoint(state, restored, turn=4)
    assert load_messages_checkpoint(state)[0] == restored
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert sum(e.get("event") == "message_origins_restored" for e in events) == 1


def test_legacy_contraction_of_mixed_identical_sources_stays_ambiguous(tmp_path):
    from core.agent_loop import load_messages_checkpoint
    from core.state import State

    state = State(run_id="ambiguous", node_type="_test", root=tmp_path)
    note = {"role": "user", "content": framework_notice("same historical bytes").content}
    task = {"role": "user", "content": "task"}
    events = [
        {"event": "loop_seed", "messages": [task]},
        {"event": "hook_injection", "roles": ["user"], "contents": [note["content"]]},
        {"event": "loop_seed", "messages": [task, note, note]},
        {"event": "loop_seed", "messages": [task, note]},
    ]
    state.transcript_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (tmp_path / "messages_checkpoint.json").write_text(json.dumps({"messages": [task, note]}))
    restored, _ = load_messages_checkpoint(state)
    assert not is_framework_notice(restored[-1])
    assert _preserve_user_messages([restored[-1]], []) == [restored[-1]]
