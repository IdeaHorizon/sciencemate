"""Restarting a parent continues its accepted child before another model turn."""

import asyncio
import json

import pytest

from core.agent_loop import (
    LoopResult,
    _persist_messages_checkpoint,
    load_messages_checkpoint,
    run_loop,
)
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import clear_all
from core.state import State


def response(content=None, calls=None):
    return LLMResponse(
        content=content,
        tool_calls=calls or [],
        finish_reason="tool_calls" if calls else "stop",
        usage={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_starts", [0, 6])
async def test_restart_finishes_original_child_without_a_model_redispatch(
    tmp_path, monkeypatch, prior_starts
):
    import chat
    import core.executor as executor
    import core.loader as loader

    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", session_id="session")
    child_harness = NodeHarness(node_type="_curator", system_prompt="child", tools=[], max_turns=2)
    monkeypatch.setattr(loader, "load_harness", lambda _: child_harness)
    monkeypatch.setattr(executor, "load_harness", lambda _: child_harness)
    monkeypatch.setattr("core.llm.LLMClient", lambda: object())
    starts = []

    async def child_loop(harness, state, messages, llm):
        starts.append(state.run_id)
        if len(starts) == 1:
            for i in range(prior_starts):
                state.append_transcript(
                    "run_start",
                    node_type="_curator",
                    parent_run_id=parent.run_id,
                    parent_dispatch_id=f"old-{i}",
                )
            if prior_starts:
                state.append_transcript(
                    "run_start",
                    node_type="_curator",
                    parent_run_id=parent.run_id,
                    parent_dispatch_id=state.parent_dispatch_id,
                )
            _persist_messages_checkpoint(
                state, [LLMMessage(role="user", content="existing progress")], turn=7
            )
            raise asyncio.CancelledError("worker process lost")
        assert "existing progress" in "\n".join(m.content or "" for m in messages)
        return LoopResult(
            final_text="saved child finished",
            turns=8,
            tool_calls=[],
            messages=messages,
            status="completed",
        )

    monkeypatch.setattr(executor, "run_loop", child_loop)
    call = {
        "id": "original-call",
        "type": "function",
        "function": {
            "name": "run_node",
            "arguments": json.dumps({"node_type": "_curator", "user_note": "do the work"}),
        },
    }

    class First:
        async def chat(self, messages, **kwargs):
            return response(calls=[call])

    harness = NodeHarness(
        node_type="_orchestrator",
        system_prompt="parent",
        tools=["run_node"],
        callable_nodes=["*"],
        max_turns=2,
    )
    with pytest.raises(asyncio.CancelledError):
        await run_loop(harness, parent, [LLMMessage(role="user", content="work")], First())
    checkpoint = json.loads((parent.root / "messages_checkpoint.json").read_text())
    assert next(iter(checkpoint["child_dispatches"].values()))["child_run_id"] == starts[0]
    clear_all()
    reopened = State.reopen(
        "_orchestrator", parent.root.parent, parent.run_id, session_id="session"
    )
    messages, _ = load_messages_checkpoint(reopened)
    chat._repair_message_tool_protocol(messages)

    class Resumed:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            result = json.loads(
                next(m.content for m in messages if m.tool_call_id == "original-call")
            )
            assert result["child_run_id"] == starts[0]
            assert result["child_status"] == "completed"
            return response("done")

    resumed = Resumed()
    result = await run_loop(harness, reopened, messages, resumed)
    assert result.status == "completed" and resumed.calls == 1
    assert starts == [starts[0], starts[0]]
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.fixture
def bound_pair(tmp_path):
    from core import child_dispatch

    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", session_id="s")
    child = State.new("_curator", parent.root.parent, session_id="s")
    args = {"node_type": "_curator"}
    call = {
        "id": "same-call",
        "type": "function",
        "function": {"name": "run_node", "arguments": json.dumps(args)},
    }
    messages = [LLMMessage(role="assistant", tool_calls=[call])]
    parent.hook_state["_current_tool_call_id_being_executed"] = call["id"]
    _persist_messages_checkpoint(parent, messages, turn=1)
    child_dispatch.prepare(
        parent,
        {
            "dispatch_id": "d",
            "node_type": "_curator",
            "execution": {"node_type": "_curator", "project_id": None, "parent_dispatch_id": "d"},
        },
    )
    child_dispatch.bind(parent, "d", child)
    child.append_transcript(
        "run_start", node_type="_curator", parent_run_id=parent.run_id, parent_dispatch_id="d"
    )
    _persist_messages_checkpoint(child, [LLMMessage(role="user", content="old progress")], turn=2)
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    import chat

    chat._repair_message_tool_protocol(messages)
    return parent, child, messages, args


def complete(child, status="completed"):
    child.write_summary(
        {
            "run_id": child.run_id,
            "status": status,
            "state_dir": str(child.root),
            "artifacts": [],
            "final_text_preview": "original result",
        }
    )
    child.append_transcript("run_end", status=status)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled", "error", "incomplete"])
async def test_terminal_child_is_delivered_without_another_execution(
    bound_pair, monkeypatch, status
):
    import shared.tools.run_node as owner

    parent, child, _, args = bound_pair
    complete(child, status)

    async def forbidden(**kwargs):
        pytest.fail("a terminal original child was executed again")

    monkeypatch.setattr("core.executor.execute_node", forbidden)
    recovered = await owner._recover_run_node(args, state=parent, tool_call_id="same-call")
    assert recovered["result"]["child_run_id"] == child.run_id
    assert recovered["result"]["child_status"] == status


@pytest.mark.asyncio
async def test_a_live_child_is_rejoined_without_a_second_executor(bound_pair, monkeypatch):
    import shared.tools.run_node as owner
    from core.pause import ActiveRunInfo, register_active, unregister_active

    parent, child, _, args = bound_pair
    register_active(ActiveRunInfo(child.run_id, child.node_type, child))

    async def forbidden(**kwargs):
        pytest.fail("a live child acquired a second executor")

    monkeypatch.setattr("core.executor.execute_node", forbidden)

    async def finish_existing():
        await asyncio.sleep(0.01)
        complete(child)
        unregister_active(child.run_id)

    producer = asyncio.create_task(finish_existing())
    result = await asyncio.wait_for(
        owner._recover_run_node(args, state=parent, tool_call_id="same-call"), 2
    )
    await producer
    assert result["result"]["child_run_id"] == child.run_id


@pytest.mark.asyncio
async def test_restart_restores_a_child_question_without_asking_the_model(bound_pair, monkeypatch):
    from core.agent_loop import write_pause_pending
    from core.pause import PauseEvent, get_paused_run

    parent, child, messages, _ = bound_pair
    question = {
        "question": "Approve this exact action?",
        "options": ["YES", "NO"],
        "asking_node_type": child.node_type,
        "asking_run_id": child.run_id,
        "metadata": {"type": "capability_grant", "scope": "exact-existing-action"},
    }
    call = {
        "id": "human-call",
        "type": "function",
        "function": {"name": "request_human_input", "arguments": "{}"},
    }
    child_messages = [
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(
            role="tool",
            tool_call_id="human-call",
            name="request_human_input",
            content=json.dumps({"status": "pause", "pause_event": question}),
        ),
    ]
    _persist_messages_checkpoint(child, child_messages, turn=3)
    write_pause_pending(
        child, PauseEvent.from_payload(question, pending_tool_call_id="human-call"), 3
    )

    class Forbidden:
        async def chat(self, *args, **kwargs):
            pytest.fail("a saved human question triggered an autonomous model turn")

    monkeypatch.setattr("core.llm.LLMClient", Forbidden)
    harness = NodeHarness(
        node_type="_orchestrator", system_prompt="test", tools=["run_node"], max_turns=2
    )
    result = await run_loop(harness, parent, messages, Forbidden())
    assert result.status == "paused"
    assert result.pause_event.metadata == question["metadata"]
    assert get_paused_run(child.run_id).parent_tool_call_id == "same-call"
    assert get_paused_run(child.run_id).pause_event.options == ["YES", "NO"]


@pytest.mark.asyncio
async def test_competing_recovery_consumers_deliver_one_child_result(bound_pair, monkeypatch):
    import shared.tools.run_node as owner
    from core.conversation_store import reconcile_reentrant_calls, unresolved_tool_calls

    parent, child, messages, _ = bound_pair
    count = 0

    async def recover(*args, **kwargs):
        nonlocal count
        count += 1
        await asyncio.sleep(0.01)
        return {"status": "success", "child_run_id": child.run_id}

    monkeypatch.setattr(owner, "_resume_child_dispatch", recover)
    other = list(messages)
    await asyncio.gather(
        reconcile_reentrant_calls(parent, messages, turn=1),
        reconcile_reentrant_calls(parent, other, turn=1),
    )
    assert count == 1
    assert unresolved_tool_calls(messages) == unresolved_tool_calls(other) == {}


@pytest.mark.parametrize(
    "fault",
    [
        "missing_directory",
        "corrupt_completion",
        "wrong_session",
        "path_escape",
        "wrong_parent",
        "wrong_dispatch",
        "wrong_task",
    ],
)
def test_a_bound_child_failure_cannot_select_a_replacement(bound_pair, fault):
    import shutil

    from core import child_dispatch

    parent, child, _, _ = bound_pair
    record = child_dispatch.load(parent, "same-call")[0]
    if fault == "missing_directory":
        shutil.rmtree(child.root)
    elif fault == "corrupt_completion":
        complete(child)
        child.summary_path.write_text("{partial")
    elif fault == "wrong_session":
        child.session_id = "other"
        child.append_transcript("run_start", node_type=child.node_type)
    elif fault in {"wrong_parent", "wrong_dispatch", "wrong_task"}:
        fields = {"parent_run_id": parent.run_id, "parent_dispatch_id": "d"}
        if fault == "wrong_parent":
            fields["parent_run_id"] = "someone-else"
        elif fault == "wrong_dispatch":
            fields["parent_dispatch_id"] = "another-dispatch"
        else:
            record["execution"]["task_contract_digest"] = "original-task"
            fields["task_contract_digest"] = "different-task"
        child.append_transcript("run_start", node_type=child.node_type, **fields)
    else:
        record["child_run_id"] = "../elsewhere"
    with pytest.raises((ValueError, OSError)):
        child_dispatch.child_facts(parent, record)


def test_a_reused_provider_call_id_does_not_inherit_old_child_bindings(bound_pair):
    from core import child_dispatch

    parent, child, messages, _ = bound_pair
    # Persisting a confirmed exchange releases the dispatch cursor.
    messages[1].content = json.dumps({"status": "success"})
    _persist_messages_checkpoint(parent, messages, turn=1)
    parent.hook_state["_current_tool_call_id_being_executed"] = "same-call"
    _persist_messages_checkpoint(parent, messages, turn=2)
    assert child_dispatch.load(parent, "same-call") == []


@pytest.mark.asyncio
async def test_parallel_recovery_preserves_job_slots_including_not_started_jobs(
    bound_pair, monkeypatch
):
    import shared.tools.run_node as owner

    parent, child, _, _ = bound_pair
    records = json.loads((parent.root / "messages_checkpoint.json").read_text())
    record = next(iter(records["child_dispatches"].values()))
    record["slot"] = 0
    second = {**record, "dispatch_id": "d2", "child_run_id": "other-original", "slot": 1}
    records["child_dispatches"]["d2"] = second
    from core.conversation_store import write_snapshot

    write_snapshot(parent.root / "messages_checkpoint.json", records)
    resumed, launched = [], []

    async def resume(st, record):
        resumed.append(record["child_run_id"])
        return {"status": "success", "child_run_id": record["child_run_id"]}

    async def launch(st, **kwargs):
        launched.append(kwargs["_call_slot"])
        return {"status": "success", "child_run_id": "new-slot-two"}

    monkeypatch.setattr(owner, "_resume_child_dispatch", resume)
    monkeypatch.setattr(owner, "_run_node_tool", launch)
    result = await owner._recover_run_nodes_parallel(
        {"jobs": [{"node_type": "_curator"}] * 3}, state=parent, tool_call_id="same-call"
    )
    assert [row["child_run_id"] for row in result["result"]["results"]] == [
        child.run_id,
        "other-original",
        "new-slot-two",
    ]
    assert launched == [2] and len(resumed) == 2


@pytest.mark.asyncio
async def test_background_handle_survives_parent_result_and_is_rejoined_once(
    bound_pair, monkeypatch
):
    import shared.tools.run_node as owner
    from core import child_dispatch

    parent, child, messages, _ = bound_pair
    saved = child_dispatch.checkpoint(parent)
    record = saved["child_dispatches"]["d"]
    record.update(background=True, owner_tool="run_node")
    from core.conversation_store import write_snapshot

    write_snapshot(parent.root / "messages_checkpoint.json", saved)
    parent._child_dispatches = saved["child_dispatches"]
    _persist_messages_checkpoint(parent, messages, turn=2)
    assert child_dispatch.checkpoint(parent)["tool_dispatch"] is None
    assert (
        child_dispatch.checkpoint(parent)["child_dispatches"]["d"]["child_run_id"] == child.run_id
    )
    resumed = []

    async def recover(st, record):
        resumed.append(record["child_run_id"])
        await asyncio.sleep(0.01)
        return {"status": "success", "child_run_id": child.run_id, "child_status": "completed"}

    monkeypatch.setattr(owner, "_resume_child_dispatch", recover)
    await child_dispatch.resume_background(parent)
    await child_dispatch.resume_background(parent)
    await asyncio.gather(*list(owner._BACKGROUND_TASKS))
    assert resumed == [child.run_id]
    assert child_dispatch.checkpoint(parent)["child_dispatches"] == {}


@pytest.mark.asyncio
async def test_a_recovered_receipt_survives_another_crash_before_checkpoint_ack(
    bound_pair, monkeypatch
):
    import chat
    import core.agent_loop as loop
    import shared.tools.run_node as owner
    from core.conversation_store import reconcile_reentrant_calls

    parent, child, messages, _ = bound_pair
    complete(child)
    persist = loop._persist_messages_checkpoint

    def lose_ack(state, *args, **kwargs):
        if state.run_id == parent.run_id:
            raise asyncio.CancelledError("crash before parent acknowledgement")
        return persist(state, *args, **kwargs)

    monkeypatch.setattr(loop, "_persist_messages_checkpoint", lose_ack)
    with pytest.raises(asyncio.CancelledError):
        await reconcile_reentrant_calls(parent, messages, turn=1)
    monkeypatch.setattr(loop, "_persist_messages_checkpoint", persist)
    reopened = State.reopen(parent.node_type, parent.root.parent, parent.run_id, session_id="s")
    messages, _ = load_messages_checkpoint(reopened)
    chat._repair_message_tool_protocol(messages)

    async def forbidden(*args, **kwargs):
        pytest.fail("lost acknowledgement caused the recovered operation to execute again")

    monkeypatch.setattr(owner, "_resume_child_dispatch", forbidden)

    class Final:
        async def chat(self, messages, **kwargs):
            saved = json.loads(next(m.content for m in messages if m.tool_call_id == "same-call"))
            assert saved["child_run_id"] == child.run_id
            return response("delivered")

    result = await run_loop(
        NodeHarness(node_type="_orchestrator", system_prompt="test", tools=[]),
        reopened,
        messages,
        Final(),
    )
    assert result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_an_executed_child_without_its_checkpoint_cannot_start_over(
    bound_pair, monkeypatch, corrupt
):
    import core.executor as executor

    parent, child, _, _ = bound_pair
    if corrupt:
        (child.root / "messages_checkpoint.json").write_text("{partial")
    else:
        (child.root / "messages_checkpoint.json").unlink()
    child.append_transcript("tool_call", name="already-produced-an-effect")

    async def forbidden(*args, **kwargs):
        pytest.fail("an unrecoverable child started from scratch")

    monkeypatch.setattr(executor, "run_loop", forbidden)
    with pytest.raises(ValueError, match="checkpoint"):
        await executor.execute_node(
            child.node_type,
            state_dir=parent.root.parent,
            resume_run_id=child.run_id,
            parent_state=parent,
            harness_override=NodeHarness(node_type=child.node_type, system_prompt="test", tools=[]),
        )
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.mark.asyncio
async def test_cancelling_recovery_observation_does_not_cancel_the_live_child(bound_pair):
    import shared.tools.run_node as owner
    from core.cancellation import RunCancelled
    from core.pause import ActiveRunInfo, get_active_run, register_active

    parent, child, _, args = bound_pair
    register_active(ActiveRunInfo(child.run_id, child.node_type, child))
    parent.kill_event.set()
    with pytest.raises(RunCancelled):
        await owner._recover_run_node(args, state=parent, tool_call_id="same-call")
    assert get_active_run(child.run_id) is not None and not child.kill_event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", ["progress", "list_active", "jobs"])
async def test_unknown_outcome_allows_status_but_blocks_new_effects(
    bound_pair, monkeypatch, observation
):
    from core import tool_registry

    parent, child, messages, _ = bound_pair
    # An old unmanaged operation has no child owner that can confirm its result.
    messages[0].tool_calls[0]["function"]["name"] = "remote_exec"
    messages[0].tool_calls[0]["function"]["arguments"] = json.dumps(
        {"resource_id": "r", "command": "original"}
    )
    writes = []

    async def forbidden(*args, **kwargs):
        writes.append(kwargs)
        pytest.fail("unconfirmed operation was resubmitted")

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "remote_exec", forbidden)

    class Observe:
        async def chat(self, messages, **kwargs):
            assert parent.hook_state["_unconfirmed_tool_calls"]
            status = await tool_registry.execute(
                "runtime_control", parent, action=observation, child_run_id=child.run_id
            )
            assert status.get("error_code") != "unconfirmed_tool_outcome"
            rejected = await tool_registry.execute(
                "remote_exec", parent, resource_id="r", command="new"
            )
            assert rejected["executed"] is False
            return response(
                "The original outcome is still unconfirmed; status inspection remains available."
            )

    result = await run_loop(
        NodeHarness(node_type="_orchestrator", system_prompt="test", tools=[]),
        parent,
        messages,
        Observe(),
    )
    assert result.status == "completed" and writes == []


def test_child_bookkeeping_and_messages_restore_from_one_snapshot(bound_pair):
    from core import child_dispatch

    parent, child, _, _ = bound_pair
    parent.tokens_used = 123
    parent.scratchpad = "saved progress"
    parent.hook_state["pending_post_node_flow"] = [
        {"producing_run_id": child.run_id, "review_state": "done"}
    ]
    child_dispatch.finished(parent, "d", {"status": "success", "child_run_id": child.run_id})
    reopened = State.reopen(parent.node_type, parent.root.parent, parent.run_id, session_id="s")
    load_messages_checkpoint(reopened)
    assert reopened.tokens_used == 123 and reopened.scratchpad == "saved progress"
    assert (
        reopened.hook_state["pending_post_node_flow"] == parent.hook_state["pending_post_node_flow"]
    )
    assert "_current_tool_call_id_being_executed" not in reopened.hook_state


@pytest.mark.asyncio
async def test_saved_parent_pause_restores_its_child_chain_after_process_loss(
    bound_pair, monkeypatch
):
    import core.executor as executor
    import core.loader as loader
    from core.agent_loop import write_pause_pending
    from core.pause import PauseEvent, get_paused_run

    parent, child, messages, _ = bound_pair
    node = NodeHarness(node_type="_curator", system_prompt="child", tools=[], max_turns=2)
    monkeypatch.setattr(loader, "load_harness", lambda _: node)
    monkeypatch.setattr(executor, "load_harness", lambda _: node)
    event = {
        "question": "Choose the existing operation",
        "options": ["YES", "NO"],
        "asking_node_type": child.node_type,
        "asking_run_id": child.run_id,
        "metadata": {"type": "capability_grant", "scope": "original"},
    }
    call = {
        "id": "human",
        "type": "function",
        "function": {"name": "request_human_input", "arguments": "{}"},
    }
    _persist_messages_checkpoint(
        child,
        [
            LLMMessage(role="assistant", tool_calls=[call]),
            LLMMessage(
                role="tool",
                tool_call_id="human",
                content=json.dumps({"status": "pause", "pause_event": event}),
            ),
        ],
        turn=3,
    )
    write_pause_pending(child, PauseEvent.from_payload(event, pending_tool_call_id="human"), 3)

    class Forbidden:
        async def chat(self, *args, **kwargs):
            pytest.fail("restoring a saved pause must wait for the original answer")

    monkeypatch.setattr("core.llm.LLMClient", Forbidden)
    harness = NodeHarness(
        node_type="_orchestrator", system_prompt="parent", tools=["run_node"], max_turns=2
    )
    assert (await run_loop(harness, parent, messages, Forbidden())).status == "paused"
    assert json.loads((parent.root / "messages_checkpoint.json").read_text())["child_dispatches"]
    clear_all()
    reopened = State.reopen(parent.node_type, parent.root.parent, parent.run_id, session_id="s")
    restored, _ = load_messages_checkpoint(reopened)
    assert (await run_loop(harness, reopened, restored, Forbidden())).status == "paused"
    assert get_paused_run(child.run_id).parent_run_id == parent.run_id
    assert get_paused_run(child.run_id).parent_tool_call_id == "same-call"
    assert get_paused_run(child.run_id).pause_event.metadata == event["metadata"]

    class Answered:
        async def chat(self, messages, **kwargs):
            return response("Continued after the recorded human answer")

    get_paused_run(child.run_id).llm = Answered()
    get_paused_run(parent.run_id).llm = Answered()
    from core.pause_driver import drive_pause_chain

    async def answer(_):
        return "YES"

    await drive_pause_chain(ask_fn=answer, finalize_fn=executor.finalize_run)
    assert get_paused_run(child.run_id) is None and get_paused_run(parent.run_id) is None
    assert len(list(parent.root.parent.iterdir())) == 2


@pytest.mark.asyncio
async def test_internal_review_recovery_uses_its_existing_bound_dispatch(bound_pair, monkeypatch):
    import shared.tools.run_node as owner
    from core import child_dispatch

    parent, child, _, _ = bound_pair
    inputs = {"artifact_id": "result", "producer_run_id": child.run_id}
    parent.hook_state["_current_tool_call_id_being_executed"] = "same-call"
    child_dispatch.prepare(
        parent,
        {
            "dispatch_id": "review",
            "node_type": "_reviewer",
            "requested_inputs": inputs,
            "execution": {"node_type": "_reviewer"},
            "child_run_id": "original-review",
        },
    )
    parent._recovering_child_call = "same-call"
    calls = []

    async def existing(st, record):
        calls.append(record["child_run_id"])
        return {"status": "success", "child_run_id": record["child_run_id"]}

    monkeypatch.setattr(owner, "_resume_child_dispatch", existing)
    result = await owner._run_node_tool(parent, "_reviewer", node_inputs=inputs)
    assert result["child_run_id"] == "original-review" and calls == ["original-review"]


def test_parent_bookkeeping_is_not_repeated_after_its_receipt_is_saved(bound_pair, monkeypatch):
    import shared.tools.run_node as owner

    parent, child, _, _ = bound_pair
    node = NodeHarness(node_type="_curator", system_prompt="test", tools=[])
    summary = {
        "run_id": child.run_id,
        "node_type": child.node_type,
        "status": "completed",
        "artifacts": [],
        "state_dir": str(child.root),
    }
    first = owner._finish_child(parent, child.node_type, {}, summary, node, None, dispatch_id="d")

    def forbidden(*args, **kwargs):
        pytest.fail("the same child outputs were imported again")

    monkeypatch.setattr(owner, "_import_required_outputs", forbidden)
    assert (
        owner._finish_child(parent, child.node_type, {}, summary, node, None, dispatch_id="d")
        == first
    )
    events = [json.loads(line) for line in parent.transcript_path.read_text().splitlines()]
    assert sum(event["event"] == "subagent_call_end" for event in events) == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_child_action_is_suspended_and_the_parent_can_explain(
    bound_pair, monkeypatch
):
    import core.executor as executor
    import core.loader as loader

    parent, child, messages, _ = bound_pair
    call = {
        "id": "original-external",
        "type": "function",
        "function": {
            "name": "remote_exec",
            "arguments": json.dumps({"resource_id": "r", "command": "unmanaged original"}),
        },
    }
    child.hook_state["_current_tool_call_id_being_executed"] = call["id"]
    _persist_messages_checkpoint(child, [LLMMessage(role="assistant", tool_calls=[call])], turn=1)
    node = NodeHarness(
        node_type="_curator", system_prompt="child", tools=["remote_exec"], max_turns=2
    )
    monkeypatch.setattr(loader, "load_harness", lambda _: node)
    monkeypatch.setattr(executor, "load_harness", lambda _: node)

    class Explain:
        async def chat(self, messages, **kwargs):
            assert parent.hook_state["_unconfirmed_tool_calls"] == {"same-call": "run_node"}
            return response(
                "The original external action is unconfirmed; the saved child is retained."
            )

    result = await run_loop(
        NodeHarness(node_type="_orchestrator", system_prompt="parent", tools=[]),
        parent,
        messages,
        Explain(),
    )
    assert result.status == "completed"
    assert not child.summary_path.exists()
    assert all(
        json.loads(line)["event"] != "run_end"
        for line in child.transcript_path.read_text().splitlines()
    )


def test_an_accepted_authorized_action_is_not_reset_for_redispatch(bound_pair):
    import shared.tools.run_node as owner

    parent, child, _, _ = bound_pair
    entry = {
        "decision_state": "action_in_progress",
        "action_target_node": child.node_type,
        "producing_node": "experiment",
        "producing_run_id": "previous",
        "task_id": None,
    }
    parent.hook_state["pending_post_node_flow"] = [entry]
    assert owner.recover_interrupted_decision_actions(parent) == 0
    assert entry["decision_state"] == "action_in_progress"


@pytest.mark.asyncio
async def test_a_new_dispatch_does_not_guess_an_old_context_of_the_same_node(
    bound_pair, monkeypatch
):
    import core.loader as loader
    import shared.tools.run_node as owner

    parent, old_child, _, _ = bound_pair
    parent.hook_state["_callable_nodes"] = ["*"]
    node = NodeHarness(node_type="_curator", system_prompt="child", tools=[])
    monkeypatch.setattr(loader, "load_harness", lambda _: node)
    chosen = []

    async def execute(**kwargs):
        chosen.append(kwargs.get("resume_run_id"))
        child = State.new("_curator", parent.root.parent, session_id="s")
        return {
            "run_id": child.run_id,
            "status": "completed",
            "node_type": child.node_type,
            "artifacts": [],
            "state_dir": str(child.root),
        }

    monkeypatch.setattr("core.executor.execute_node", execute)
    result = await owner._run_node_tool(parent, "_curator", node_inputs={"mode": "different work"})
    assert chosen == [None] and result["child_run_id"] != old_child.run_id


@pytest.mark.asyncio
async def test_a_failed_binding_cannot_launch_the_child(bound_pair, monkeypatch):
    import core.executor as executor

    parent, child, _, _ = bound_pair
    calls = []

    def fail_binding(state):
        raise OSError("cannot save the original child handle")

    async def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("child acted before its handle was durable")

    monkeypatch.setattr(executor, "run_loop", forbidden)
    with pytest.raises(OSError, match="handle"):
        await executor.execute_node(
            child.node_type,
            state_dir=parent.root.parent,
            parent_state=parent,
            harness_override=NodeHarness(node_type=child.node_type, system_prompt="test", tools=[]),
            on_state_ready=fail_binding,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_repeated_status_turns_and_reused_ids_never_clear_an_unknown_effect(
    tmp_path, monkeypatch
):
    import chat
    from core import tool_registry
    from core.conversation_store import unresolved_tool_calls

    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs")
    call_id = "reused-by-provider"
    parent.append_transcript(
        "tool_result",
        name="remote_exec",
        tool_call_id=call_id,
        content=json.dumps({"status": "success", "stdout": "older action"}),
    )
    original = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "remote_exec",
            "arguments": json.dumps({"resource_id": "r", "command": "original unknown action"}),
        },
    }
    messages = [LLMMessage(role="assistant", tool_calls=[original])]
    parent.hook_state["_current_tool_call_id_being_executed"] = call_id
    _persist_messages_checkpoint(parent, messages, turn=1)
    parent.hook_state.pop("_current_tool_call_id_being_executed")
    chat._repair_message_tool_protocol(messages)
    evidence = parent.root / "status.txt"
    evidence.write_text("current status")

    class Observe:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return response(
                    calls=[
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": str(evidence)}),
                            },
                        }
                    ]
                )
            return response("The external outcome remains unconfirmed")

    harness = NodeHarness(
        node_type="_orchestrator", system_prompt="test", tools=["read_file"], max_turns=3
    )
    await run_loop(harness, parent, messages, Observe())
    assert unresolved_tool_calls(messages) == {call_id: "remote_exec"}
    saved = json.loads((parent.root / "messages_checkpoint.json").read_text())
    assert saved["tool_dispatch"] is None and call_id in saved["tool_dispatches"]
    reopened = State.reopen(parent.node_type, parent.root.parent, parent.run_id)
    messages, _ = load_messages_checkpoint(reopened)

    class Explain:
        async def chat(self, messages, **kwargs):
            assert unresolved_tool_calls(messages) == {call_id: "remote_exec"}
            assert reopened.hook_state["_unconfirmed_tool_calls"] == {call_id: "remote_exec"}
            return response("Status is readable; the original action still needs reconciliation")

    await run_loop(harness, reopened, messages, Explain())
    blocked = await tool_registry.execute(
        "remote_exec", reopened, resource_id="r", command="resubmit"
    )
    assert blocked["executed"] is False
    assert unresolved_tool_calls(messages) == {call_id: "remote_exec"}


@pytest.mark.asyncio
async def test_an_unknown_background_effect_remains_guarded_across_new_turns(
    bound_pair, monkeypatch
):
    import shared.tools.run_node as owner
    from core import child_dispatch
    from core.conversation_store import UnconfirmedToolOutcome, write_snapshot

    parent, child, messages, _ = bound_pair
    saved = child_dispatch.checkpoint(parent)
    record = saved["child_dispatches"]["d"]
    record.update(background=True, owner_tool="run_node")
    write_snapshot(parent.root / "messages_checkpoint.json", saved)
    parent._child_dispatches = saved["child_dispatches"]
    messages[1].content = json.dumps({"status": "started_background"})
    _persist_messages_checkpoint(parent, messages, turn=2)

    async def unknown(*args, **kwargs):
        raise UnconfirmedToolOutcome("physical background outcome is unknown")

    monkeypatch.setattr(owner, "_resume_child_dispatch", unknown)
    await child_dispatch.resume_background(parent)
    await asyncio.gather(*list(owner._BACKGROUND_TASKS))
    assert child_dispatch.unconfirmed_background(parent) == {"background:d": child.node_type}

    class Explain:
        async def chat(self, messages, **kwargs):
            assert parent.hook_state["_unconfirmed_tool_calls"] == {"background:d": child.node_type}
            return response("The original background operation remains available for inspection")

    await run_loop(
        NodeHarness(node_type="_orchestrator", system_prompt="test", tools=[]),
        parent,
        messages,
        Explain(),
    )
    await asyncio.gather(*list(owner._BACKGROUND_TASKS))
    assert child_dispatch.unconfirmed_background(parent) == {"background:d": child.node_type}
    child_dispatch.release_background(parent, child.run_id)
    assert not parent.hook_state["_unconfirmed_tool_calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_pause", ["marker", "receipt", "summary"])
async def test_parallel_question_retains_its_owner_until_the_child_finishes(
    bound_pair, monkeypatch, saved_pause
):
    import core.executor as executor
    import core.loader as loader
    from core import child_dispatch
    from core.agent_loop import write_pause_pending
    from core.conversation_store import unresolved_tool_calls, write_snapshot
    from core.pause import PauseEvent, get_paused_run

    parent, child, messages, _ = bound_pair
    saved = child_dispatch.checkpoint(parent)
    saved["child_dispatches"]["d"]["slot"] = 0
    write_snapshot(parent.root / "messages_checkpoint.json", saved)
    parent._child_dispatches = saved["child_dispatches"]
    call = messages[0].tool_calls[0]
    call["function"].update(
        name="run_nodes_parallel", arguments=json.dumps({"jobs": [{"node_type": "_curator"}]})
    )
    messages[1].content = json.dumps(
        {
            "status": "success",
            "paused": 1,
            "paused_jobs": [{"job_index": 0, "child_run_id": child.run_id}],
        }
    )
    _persist_messages_checkpoint(parent, messages, turn=2)
    assert child_dispatch.load(parent, "same-call")[0]["child_run_id"] == child.run_id

    event = {
        "question": "Keep the original parallel question",
        "options": ["YES", "NO"],
        "asking_node_type": child.node_type,
        "asking_run_id": child.run_id,
    }
    _persist_messages_checkpoint(
        child,
        [
            LLMMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "human",
                        "type": "function",
                        "function": {"name": "request_human_input", "arguments": "{}"},
                    }
                ],
            ),
            LLMMessage(
                role="tool",
                tool_call_id="human",
                content=json.dumps({"status": "pause", "pause_event": event}),
            ),
        ],
        turn=2,
    )
    if saved_pause == "marker":
        write_pause_pending(child, PauseEvent.from_payload(event, pending_tool_call_id="human"), 2)
    elif saved_pause == "summary":
        complete(child, status="paused")
    node = NodeHarness(node_type="_curator", system_prompt="child", tools=[], max_turns=2)
    monkeypatch.setattr(loader, "load_harness", lambda _: node)
    monkeypatch.setattr(executor, "load_harness", lambda _: node)

    class Forbidden:
        async def chat(self, *args, **kwargs):
            pytest.fail("a saved parallel question must not wake its child model")

    monkeypatch.setattr("core.llm.LLMClient", Forbidden)
    harness = NodeHarness(node_type="_orchestrator", system_prompt="parent", tools=[], max_turns=2)

    class Observe:
        async def chat(self, messages, **kwargs):
            paused = get_paused_run(child.run_id)
            assert paused is not None and paused.pause_event.question == event["question"]
            assert unresolved_tool_calls(messages) == {"same-call": "run_nodes_parallel"}
            return response("The original parallel child is waiting for your answer")

    assert (await run_loop(harness, parent, messages, Observe())).status == "completed"
    assert child_dispatch.load(parent, "same-call")
    clear_all()
    (child.root / "pause_pending.json").unlink(missing_ok=True)
    complete(child)

    class Finished:
        async def chat(self, messages, **kwargs):
            assert unresolved_tool_calls(messages) == {}
            receipt = json.loads(next(m.content for m in messages if m.tool_call_id == "same-call"))
            assert receipt["successes"] == 1 and receipt["paused"] == 0
            return response("The same child has finished")

    assert (await run_loop(harness, parent, messages, Finished())).status == "completed"
    assert child_dispatch.checkpoint(parent)["child_dispatches"] == {}
    assert len(list(parent.root.parent.iterdir())) == 2
