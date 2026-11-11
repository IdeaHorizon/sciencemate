"""A child consumes explicit current inputs, not its parent's private history."""
import asyncio
import hashlib
import json

import pytest

from core.bootstrap import bootstrap
from core.context_engine import build_messages, read_first_turn_node_inputs_receipt
from core.harness import NodeHarness
from core.llm import LLMMessage, is_framework_notice, opening_system_prompt
from core.state import State


def harness():
    return NodeHarness(node_type="_curator", kb_query="_disable", skills=[], tools=[],
                       system_prompt="Consume the explicitly supplied evidence.")


def inputs(version):
    return {"research_question": "Assess the intervention", "notes": "supporting detail " * 180,
            "current_objective": "Assess the evidence actually consumed by the program",
            "evidence": [{"artifact_id": "result__current", "version": version,
                          "sha256": "f" * 64, "supersedes": "result__old@v1",
                          "fact": "META_SETTINGS_NOT_CONSUMED"}]}


class InputConsumed(Exception):
    pass


@pytest.mark.parametrize("resume", [False, True])
def test_explicit_new_facts_reach_the_child_after_the_old_size_cutoff(tmp_path, monkeypatch, resume):
    from core import executor
    from core.agent_loop import _persist_messages_checkpoint
    from shared.tools.builtin import _read_artifact

    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    runs = tmp_path / "runs"
    parent = State.new(node_type="_orchestrator", base_dir=runs)
    parent.hook_state["_user_utterances"] = ["PRIVATE_PARENT_DISCUSSION"]
    old = State.new(node_type="_curator", base_dir=runs)
    old_inputs = {"research_question": "Use old feedback", "evidence": [{"version": 1}]}
    build_messages(harness(), old, old_inputs)
    original_receipt = (old.root / "first_turn_node_inputs.json").read_bytes()
    _persist_messages_checkpoint(old, [opening_system_prompt("old system"),
        LLMMessage(role="user", content="Use old feedback"),
        LLMMessage(role="assistant", content="old work")], turn=4)
    request = inputs(2)
    body = "META_SETTINGS_NOT_CONSUMED"
    request["evidence"][0].update(artifact_id="raw_results__current",
        sha256=hashlib.sha256(body.encode()).hexdigest())
    consumed = {}

    async def child_loop(harness, state, messages, llm):
        # This is the real executor's child input boundary, after restoration,
        # tool-pair repair and current-request assembly, before provider dispatch.
        text = "\n".join(m.content or "" for m in messages)
        consumed["fact"] = "META_SETTINGS_NOT_CONSUMED" in text
        consumed["state"] = state
        consumed["text"] = text
        consumed["messages"] = list(messages)
        if consumed["fact"]:
            evidence = await _read_artifact(state, request["evidence"][0]["artifact_id"])
            assert evidence["status"] == "success"
            actual = evidence["artifact"]["content"]
            assert hashlib.sha256(actual.encode()).hexdigest() == request["evidence"][0]["sha256"]
            consumed["decision"] = "check_meta_binding" if actual == body else "use_old_feedback"
        raise InputConsumed

    monkeypatch.setattr(executor, "run_loop", child_loop)
    with pytest.raises(InputConsumed):
        asyncio.run(executor.execute_node("_curator", state_dir=runs,
            harness_override=harness(), node_inputs=request, parent_state=parent,
            upstream_artifacts=[{"type": "raw_results", "name": "current", "content": body}],
            resume_run_id=old.run_id if resume else None))
    assert consumed["fact"], "The current evidence was explicitly supplied but never reached the child"
    assert consumed["decision"] == "check_meta_binding"
    assert "result__old@v1" in consumed["text"]
    assert "PRIVATE_PARENT_DISCUSSION" not in consumed["text"]
    assert consumed["state"].hook_state["node_inputs"] == request
    receipt = read_first_turn_node_inputs_receipt(consumed["state"])
    if resume:
        assert (old.root / "first_turn_node_inputs.json").read_bytes() == original_receipt
        assert receipt["values"] == old_inputs  # history must stay history
        assert is_framework_notice(consumed["messages"][-2])
        assert consumed["messages"][-1].content.startswith("## 节点输入\n")
    else:
        assert receipt["values"] == request


def test_resume_receipts_bind_full_values_and_the_replaced_dispatch(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    build_messages(harness(), state, inputs(1))
    first = read_first_turn_node_inputs_receipt(state)
    build_messages(harness(), state, inputs(2))
    build_messages(harness(), state, inputs(3))
    resumed = [event for line in state.transcript_path.read_text().splitlines()
               if (event := json.loads(line)).get("event") == "resumed_node_inputs"]
    assert [event["values"] for event in resumed] == [inputs(2), inputs(3)]
    assert resumed[0]["supersedes_digest"] == first["digest"]
    assert resumed[1]["supersedes_digest"] == resumed[0]["digest"]
    assert all(event["first_turn_digest"] == first["digest"] for event in resumed)
    for event in resumed:
        raw = json.dumps(event["values"], ensure_ascii=False, sort_keys=True)
        assert hashlib.sha256(raw.encode()).hexdigest() == event["digest"]


def test_old_digest_only_resume_events_keep_their_replacement_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    build_messages(harness(), state, inputs(1))
    state.append_transcript("resumed_node_inputs", digest="old-digest-only", keys=["research_question"])
    with state.transcript_path.open("a") as log:
        log.write("interrupted old log record\n")
    build_messages(harness(), state, inputs(2))
    event = json.loads(state.transcript_path.read_text().splitlines()[-1])
    assert event["event"] == "resumed_node_inputs"
    assert event["values"] == inputs(2)
    assert event["supersedes_digest"] == "old-digest-only"


def test_resume_without_new_inputs_does_not_supersede_the_current_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    build_messages(harness(), state, inputs(1))
    build_messages(harness(), state, inputs(2))
    build_messages(harness(), state, None)
    build_messages(harness(), state, {})
    build_messages(harness(), state, inputs(3))
    resumed = [event for line in state.transcript_path.read_text().splitlines()
               if (event := json.loads(line)).get("event") == "resumed_node_inputs"]
    assert [event["values"] for event in resumed] == [inputs(2), inputs(3)]
    assert resumed[-1]["supersedes_digest"] == resumed[0]["digest"]


def test_a_failed_current_receipt_is_reported_without_rewriting_the_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    build_messages(harness(), state, inputs(1))
    original = (state.root / "first_turn_node_inputs.json").read_bytes()
    append = state.append_transcript

    def fail_receipt(event, **kwargs):
        if event == "resumed_node_inputs":
            raise OSError("simulated receipt write failure")
        append(event, **kwargs)

    monkeypatch.setattr(state, "append_transcript", fail_receipt)
    messages = build_messages(harness(), state, inputs(2))
    assert "META_SETTINGS_NOT_CONSUMED" in messages[1].content
    assert (state.root / "first_turn_node_inputs.json").read_bytes() == original
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    failed = [e for e in events if e.get("event") == "resumed_node_inputs_receipt_failed"]
    assert failed and "simulated receipt write failure" in failed[-1]["error"]
