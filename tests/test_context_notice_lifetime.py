"""Framework status is a rendered view, not a human constraint or an archive."""
import asyncio
from types import SimpleNamespace

import pytest

from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse, framework_notice, is_framework_notice, opening_system_prompt
from core import summarizer as sm


class SummaryLLM:
    async def chat(self, messages, **kwargs):
        return LLMResponse(content="## 目标\nContinue\n## 已完成\n- none\n"
                                   "## 未决与下一步\n- continue\n## 决策与理由\n- none",
                           tool_calls=[], usage={}, finish_reason="stop")


def compact(messages):
    harness = NodeHarness(node_type="_test", summarizer=SummarizerConfig(keep_last_n_turns=1, keep_tool_calls=["save_artifact"]))
    state = SimpleNamespace(tokens_used=0, save_artifact=lambda **kwargs: None)
    return asyncio.run(sm._strategy_llm(sm.SummarizerContext(
        harness, state, messages, sm.estimate_tokens(messages), SummaryLLM())))


def test_framework_notices_cannot_displace_human_steering():
    steering = LLMMessage(role="user", content="Keep the held-out set untouched.")
    notices = [framework_notice(f"poll facts {i}") for i in range(20)]
    assert sm._preserve_user_messages([steering, *notices], []) == [steering]


def test_repeated_compaction_does_not_promote_notices_into_the_prefix():
    steering = LLMMessage(role="user", content="Use the existing retry helper.")
    messages = [opening_system_prompt("system"), LLMMessage(role="user", content="task")]
    counts = []
    for cycle in range(8):
        for turn in range(4):
            messages += [LLMMessage(role="assistant", content=f"{cycle}:{turn}"),
                         framework_notice(f"poll facts {cycle}:{turn}")]
        if cycle == 0:
            messages.insert(3, steering)
        messages = compact(messages)
        assert steering in messages
        counts.append(sum(is_framework_notice(m) for m in messages))
    assert counts == [2] * 8   # one ledger and one notice in the recent turn


@pytest.mark.parametrize("recent_turns", [0, 1])
def test_a_restored_prefix_can_compact_without_waiting_for_more_assistant_turns(recent_turns):
    messages = [opening_system_prompt("system"), LLMMessage(role="user", content="task"),
                *[framework_notice(f"old snapshot {i}") for i in range(1007)],
                *[LLMMessage(role="assistant", content="recent") for _ in range(recent_turns)]]
    result = compact(messages)
    assert sum(is_framework_notice(m) for m in result) == 1
    assert len(result) == 3 + recent_turns


@pytest.mark.parametrize("legacy", [False, True])
def test_restored_prefix_notices_are_compressible_and_tool_pairs_survive(legacy):
    from core.conversation_store import jsonable_to_msg, msg_to_jsonable

    steering = LLMMessage(role="user", content="Never change the source dataset.")
    old = [LLMMessage(role="system", content=f"old snapshot {i}") if legacy
           else framework_notice(f"old snapshot {i}") for i in range(1007)]
    pair = [LLMMessage(role="assistant", tool_calls=[
        {"id": "a", "type": "function", "function": {"name": "save_artifact", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id="a", content="saved"),
        LLMMessage(role="tool", tool_call_id="b", content="read")]
    messages = [opening_system_prompt("system"), steering, *old, *pair,
                LLMMessage(role="assistant", content="recent")]
    restored = [jsonable_to_msg(msg_to_jsonable(m)) for m in messages]
    result = compact(restored)
    assert len(result) < 10
    assert result[1].content == steering.content
    assert [m.tool_call_id for m in result if m.role == "tool"] == ["a", "b"]


def test_snapshot_hooks_replace_and_clear_their_current_view(tmp_path):
    from core.loop_hooks import HookContext, list_hooks, run_on_turn_start
    from core.state import State
    from core.conversation_store import jsonable_to_msg, msg_to_jsonable
    import core.loop_hooks_builtin  # register the real scratchpad hook

    state = State(run_id="snapshots", node_type="_test", root=tmp_path)
    harness = NodeHarness(node_type="_test")
    messages = [opening_system_prompt("system")]
    hooks = list_hooks(["scratchpad"])
    for turn in range(1, 15):
        state.scratchpad = f"Current step {turn}"
        messages.extend(asyncio.run(run_on_turn_start(hooks, HookContext(harness, state, messages, turn))))
        # Persist and restore between turns; snapshot identity must survive both paths.
        messages = [jsonable_to_msg(msg_to_jsonable(m)) for m in messages]
        assert sum(is_framework_notice(m) for m in messages) == 1
        assert f"Current step {turn}" in messages[-1].content
    state.scratchpad = ""
    state.hook_state["_scratchpad_intro_shown"] = True
    messages.extend(asyncio.run(run_on_turn_start(hooks, HookContext(harness, state, messages, 15))))
    assert not any(is_framework_notice(m) for m in messages)


def test_snapshot_replacement_rolls_back_after_a_void_turn(tmp_path, monkeypatch):
    from dataclasses import replace
    from core import agent_loop
    from core.loop_hooks import LoopHook, register_loop_hook
    from core.state import State

    register_loop_hook(LoopHook(
        name="audit_snapshot", context_slot="audit_snapshot",
        on_turn_start=lambda ctx: [framework_notice("current value")]))
    monkeypatch.setattr(agent_loop, "resolve_enabled_hook_names", lambda harness: ["audit_snapshot"])
    monkeypatch.setattr(agent_loop, "_VOID_RETRY_DELAYS", ())
    original = [opening_system_prompt("system"),
                replace(framework_notice("previous value"), context_slot="audit_snapshot")]
    messages = list(original)

    class VoidLLM:
        async def chat(self, messages, **kwargs):
            assert "current value" in messages[-1].content
            assert not any("previous value" in (m.content or "") for m in messages)
            return LLMResponse(content="", tool_calls=[], usage={"completion_tokens": 1},
                               finish_reason="stop", protocol_leak=True, leak_kind="empty")

    state = State(run_id="rollback", node_type="_test", root=tmp_path)
    result = asyncio.run(agent_loop.run_loop(NodeHarness(node_type="_test", max_turns=2),
                                            state, messages, VoidLLM()))
    assert result.status == "void"
    assert messages == original


def test_snapshot_identity_survives_a_turn_checkpoint(tmp_path):
    from dataclasses import replace
    from core.agent_loop import _persist_messages_checkpoint, load_messages_checkpoint
    from core.state import State

    state = State(run_id="checkpoint", node_type="_test", root=tmp_path)
    messages = [opening_system_prompt("system"),
                replace(framework_notice("current value"), context_slot="scratchpad")]
    _persist_messages_checkpoint(state, messages, turn=4)
    assert load_messages_checkpoint(state) == (messages, 4)


def test_situation_replaces_enveloped_state_and_keeps_human_text(tmp_path, monkeypatch):
    from core.loop_hooks import HookContext, list_hooks, run_on_turn_start
    from core import loop_hooks_builtin as builtin
    from core.state import State

    state = State(run_id="situation", node_type="_orchestrator", root=tmp_path)
    harness = NodeHarness(node_type="_orchestrator")
    human = LLMMessage(role="user", content=builtin._SITUATION_MARKER + " is quoted here")
    messages = [opening_system_prompt("system"), human]
    monkeypatch.setattr(builtin, "_situation_overlays", lambda state: [])
    for label in ("fresh", "in_research", "flow_debt"):
        monkeypatch.setattr(builtin, "_detect_situation", lambda state: (label, [label]))
        messages.extend(asyncio.run(run_on_turn_start(list_hooks(["situation_router"]),
                            HookContext(harness, state, messages, 1))))
        assert sum(is_framework_notice(m) for m in messages) == 1
        assert label in messages[-1].content
        assert human in messages


def test_situation_noop_uses_the_present_view_after_rollback(tmp_path, monkeypatch):
    from core.loop_hooks import HookContext, list_hooks, run_on_turn_start
    from core import loop_hooks_builtin as builtin
    from core.state import State

    state = State(run_id="situation_rollback", node_type="_orchestrator", root=tmp_path)
    harness = NodeHarness(node_type="_orchestrator")
    messages = [opening_system_prompt("system")]
    hooks = list_hooks(["situation_router"])
    monkeypatch.setattr(builtin, "_situation_overlays", lambda state: [])
    monkeypatch.setattr(builtin, "_detect_situation", lambda state: ("fresh", ["fresh"]))
    messages.extend(asyncio.run(run_on_turn_start(hooks, HookContext(harness, state, messages, 1))))
    before = list(messages)
    monkeypatch.setattr(builtin, "_detect_situation", lambda state: ("flow_debt", ["flow_debt"]))
    assert asyncio.run(run_on_turn_start(hooks, HookContext(harness, state, messages, 1)))
    messages[:] = before  # provider failed; the new view was never consumed
    update = asyncio.run(run_on_turn_start(hooks, HookContext(harness, state, messages, 1)))
    assert update, "A parallel last-text cache mistook the restored old view for the current one"
    assert "flow_debt" in update[0].content
