"""Approvals resume exact operations, without a second model tool call."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import approvals, pause, tool_registry
from core.agent_loop import _pause_loop, resume_loop
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse
from core.state import State
from shared.lib import dangerous_commands as danger


@pytest.fixture
def operation(tmp_path, monkeypatch):
    monkeypatch.setattr(danger, "BYPASS_ENABLED", False)
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", set())
    pause.clear_all()
    state = State.new("experiment", tmp_path / "runs", session_id="s")
    state.hook_state["_current_tool_call_id_being_executed"] = "original"
    effects = []

    async def execute(*, state, value="frozen", **_):
        key = json.dumps({"value": value})
        if not danger.is_confirmed(state, key):
            return danger.build_pause_payload(
                state, tool="approval_probe", text=key, category="audit", preview=key
            )
        effects.append(value)
        return {"status": "success", "value": value}

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="approval_probe",
            description="Bounded approval test",
            parameters_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        execute,
    )
    yield state, effects
    pause.clear_all()


async def pending(state):
    result = await tool_registry.execute("approval_probe", state, value="frozen")
    assert result["status"] == "pause"
    return result["pause_event"]


def approve(state, payload, choice="approve"):
    return approvals.decide(state, payload, choice)


@pytest.mark.asyncio
async def test_approval_executes_frozen_call_and_duplicate_answer_replays_receipt(operation):
    state, effects = operation
    payload = await pending(state)
    record = approve(state, payload)
    result = await approvals.run_approved(state, record["operation_id"])
    assert result == {"status": "success", "value": "frozen"}
    approve(state, payload)
    assert await approvals.run_approved(state, record["operation_id"]) == result
    assert effects == ["frozen"]
    assert not danger.is_confirmed(state, json.dumps({"value": "frozen"}))


@pytest.mark.asyncio
async def test_denial_never_dispatches(operation):
    state, effects = operation
    record = approve(state, await pending(state), "deny")
    result = await approvals.run_approved(state, record["operation_id"])
    assert result["error_code"] == "approval_denied" and not result["executed"]
    assert not effects


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["approved", "executing"])
async def test_a_failed_durable_decision_or_claim_never_starts_the_effect(operation, monkeypatch, phase):
    state, effects = operation
    payload = await pending(state)
    if phase == "executing":
        record = approve(state, payload)
    original = approvals._write

    def unwritable(path, value):
        if value["phase"] == phase:
            raise PermissionError("injected approval storage failure")
        return original(path, value)

    monkeypatch.setattr(approvals, "_write", unwritable)
    with pytest.raises(PermissionError, match="storage failure"):
        if phase == "approved":
            approve(state, payload)
        else:
            await approvals.run_approved(state, record["operation_id"])
    assert effects == []
    stored = approvals.load(state, payload)
    assert stored["phase"] == ("pending" if phase == "approved" else "approved")


@pytest.mark.asyncio
async def test_different_call_does_not_inherit_authorization(operation):
    state, effects = operation
    payload = await pending(state)
    record = approve(state, payload)
    state.hook_state["_current_tool_call_id_being_executed"] = "another"
    second = await pending(state)
    assert second["offer_id"] != payload["offer_id"]
    assert not effects
    await approvals.run_approved(state, record["operation_id"])
    assert effects == ["frozen"]


@pytest.mark.asyncio
async def test_expiry_applies_after_reopening_state(operation, monkeypatch):
    state, effects = operation
    payload = await pending(state)
    record = approve(state, payload)
    reopened = State(
        run_id=state.run_id, node_type=state.node_type, root=state.root, session_id=state.session_id
    )
    monkeypatch.setattr(approvals.time, "time", lambda: record["expires_at"] + 1)
    for current in (state, reopened):
        with pytest.raises(approvals.ApprovalError, match="expired"):
            await approvals.run_approved(current, record["operation_id"])
    assert not effects


@pytest.mark.asyncio
async def test_only_one_execution_claim_wins_across_states(operation):
    state, effects = operation
    record = approve(state, await pending(state))

    # Competing executions use independent State objects and the same durable record.
    def run():
        other = State(
            run_id=state.run_id,
            node_type=state.node_type,
            root=state.root,
            session_id=state.session_id,
        )
        try:
            return asyncio.run(approvals.run_approved(other, record["operation_id"]))
        except approvals.ApprovalError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert all(r and r.get("status") == "success" for r in results)
    assert effects == ["frozen"]


@pytest.mark.asyncio
async def test_changed_execution_target_invalidates_claim(operation, monkeypatch):
    from core import execution_target

    state, effects = operation
    record = approve(state, await pending(state))
    monkeypatch.setattr(execution_target, "current", lambda: execution_target.parse("wsl:Ubuntu"))
    with pytest.raises(approvals.ApprovalError, match="identity changed"):
        await approvals.run_approved(state, record["operation_id"])
    assert not effects


@pytest.mark.asyncio
async def test_claim_without_receipt_is_never_resubmitted(operation):
    state, effects = operation
    record = approve(state, await pending(state))
    with approvals._record(state, record["operation_id"]) as (saved, path):
        saved.update(phase="executing", claim="previous-worker")
        approvals._write(path, saved)
    from core.conversation_store import UnconfirmedToolOutcome

    with pytest.raises(UnconfirmedToolOutcome, match="reconcile"):
        await approvals.run_approved(state, record["operation_id"])
    assert not effects


@pytest.mark.asyncio
async def test_real_loop_resumes_operation_without_model_resubmission(operation):
    state, effects = operation
    payload = await pending(state)
    call = {
        "id": "original",
        "type": "function",
        "function": {"name": "approval_probe", "arguments": json.dumps({"value": "frozen"})},
    }
    messages = [
        LLMMessage(role="system", content="Bounded integration test"),
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(
            role="tool",
            tool_call_id="original",
            name="approval_probe",
            content=json.dumps({"status": "pause", "pause_event": payload}),
        ),
    ]

    class Model:
        async def chat(self, messages, **_):
            assert effects == ["frozen"]  # operation completes before any model turn
            receipt = json.loads(
                next(
                    m.content for m in messages if m.role == "tool" and m.tool_call_id == "original"
                )
            )
            assert receipt["value"] == "frozen" and "response" not in receipt
            return LLMResponse(content="complete", tool_calls=[], finish_reason="stop", usage={})

    harness = NodeHarness(
        node_type="experiment",
        tools=[],
        max_turns=2,
        loop_hooks_disable=[
            "memory_delta",
            "kb_delta",
            "turn_budget",
            "memory_onboarding",
            "law_review_gate",
        ],
        summarizer=SummarizerConfig(enabled=False),
    )
    _pause_loop(
        harness,
        state,
        messages,
        Model(),
        call,
        {"status": "pause", "pause_event": payload},
        turn=1,
        all_tool_calls=[],
        max_turns=2,
    )
    ctx = pause.get_paused_run(state.run_id)
    result = await resume_loop(ctx, {"offer_id": payload["offer_id"], "choice_id": "approve"})
    assert result.status == "completed" and effects == ["frozen"]


@pytest.mark.parametrize(
    "answer", ["不要批准", "不可以", "do not approve", "等我确认后再批准", "1 不要执行"]
)
@pytest.mark.asyncio
async def test_discussion_and_negation_cannot_authorize(operation, answer):
    state, effects = operation
    with pytest.raises(approvals.ApprovalError):
        approvals.choice_for(await pending(state), answer)
    assert not effects


@pytest.mark.asyncio
async def test_reused_provider_call_id_has_a_new_dispatch_identity(operation):
    state, effects = operation
    state._tool_dispatches = {"original": "first-dispatch"}
    first = await pending(state)
    record = approve(state, first)
    await approvals.run_approved(state, record["operation_id"])
    state._tool_dispatches["original"] = "second-dispatch"
    second = await pending(state)
    assert first["offer_id"] != second["offer_id"]
    assert effects == ["frozen"]
    assert (
        approvals.recover_result(state, "original", "approval_probe", {"value": "frozen"})[
            "result"
        ]["status"]
        == "pause"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["principal", "attempt"])
async def test_another_principal_or_attempt_cannot_use_the_approval(operation, field):
    state, effects = operation
    state.hook_state["approval_principal"] = "owner"
    state.platform_attempt_id = "original-attempt"
    record = approve(state, await pending(state))
    if field == "principal":
        state.hook_state["approval_principal"] = "another-user"
    else:
        state.platform_attempt_id = "another-attempt"
    with pytest.raises(approvals.ApprovalError, match="identity changed"):
        await approvals.run_approved(state, record["operation_id"])
    assert not effects


@pytest.mark.asyncio
async def test_sequential_boundaries_replace_the_offer_without_model_retry(operation):
    state, effects = operation

    async def execute(*, state, **_):
        for key in ("scope", "highrisk"):
            if not danger.is_confirmed(state, key):
                return danger.build_pause_payload(
                    state, tool="two_boundaries", text=key, category="audit", preview=key
                )
        effects.append("once")
        return {"status": "success"}

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="two_boundaries", description="two gates", parameters_schema={"type": "object"}
        ),
        execute,
    )
    first = (await tool_registry.execute("two_boundaries", state))["pause_event"]
    record = approve(state, first)
    second = (await approvals.run_approved(state, record["operation_id"]))["pause_event"]
    assert first["offer_id"] != second["offer_id"] and not effects
    with pytest.raises(approvals.ApprovalError, match="superseded"):
        approve(state, first)
    approve(state, second)
    await approvals.run_approved(state, record["operation_id"])
    assert effects == ["once"]


@pytest.mark.asyncio
async def test_acceptance_clears_the_live_card_before_the_effect(operation, monkeypatch):
    from core import agent_loop

    state, effects = operation
    payload = await pending(state)
    event = pause.PauseEvent.from_payload(payload, pending_tool_call_id="original")
    ctx = pause.PausedRunContext(
        run_id=state.run_id,
        state=state,
        harness=None,
        llm=None,
        messages=[],
        pause_event=event,
        pending_tool_call_id="original",
    )
    pause.register_pause(ctx)
    agent_loop.write_pause_pending(state, event, 1)
    original = tool_registry.get_tool("approval_probe")
    execute = tool_registry._REGISTRY.executors["approval_probe"]

    async def checked(*, state, **args):
        assert pause.get_paused_run(state.run_id) is None
        assert not (state.root / "pause_pending.json").exists()
        return await execute(state=state, **args)

    tool_registry.register_tool(original, checked)
    result = await approvals.resume(ctx, {"offer_id": payload["offer_id"], "choice_id": "approve"})
    assert result["status"] == "success" and effects == ["frozen"]


@pytest.mark.asyncio
async def test_network_grant_executes_original_call_and_survives_checkpoint(operation):
    from core import capability_grants as grants
    from core.conversation_store import restore_snapshot, state_snapshot

    state, effects = operation
    host = "dataset.example.org"

    async def execute(*, state, **_):
        if not grants.is_granted(state, host):
            return grants.request_network_grant(state, host, reason="retrieve original input")
        effects.append(host)
        return {"status": "success"}

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name="network_probe",
            description="network grant integration",
            parameters_schema={"type": "object"},
        ),
        execute,
    )
    payload = (await tool_registry.execute("network_probe", state))["pause_event"]
    record = approve(state, payload)
    assert not grants.is_granted(state, host)
    await approvals.run_approved(state, record["operation_id"])
    restored = State(
        run_id=state.run_id, node_type=state.node_type, root=state.root, session_id=state.session_id
    )
    restore_snapshot(restored, state_snapshot(state))
    assert grants.is_granted(restored, host) and effects == [host]
    other = State.new("experiment", state.root.parent, session_id="other")
    restore_snapshot(other, state_snapshot(state))
    assert not grants.is_granted(other, host)


@pytest.mark.asyncio
async def test_predeclared_category_finishes_the_operation_in_the_dispatcher(
    operation, monkeypatch
):
    state, effects = operation
    monkeypatch.setattr(danger, "PREAUTHORIZED_CATEGORIES", {"audit"})
    result = await tool_registry.execute("approval_probe", state, value="frozen")
    assert result == {"status": "success", "value": "frozen"} and effects == ["frozen"]
    assert not danger.is_confirmed(state, json.dumps({"value": "frozen"}))


@pytest.mark.asyncio
async def test_a_duplicate_dispatch_cannot_erase_an_accepted_decision(operation):
    state, effects = operation
    state._tool_dispatches = {"original": "one-dispatch"}
    record = approve(state, await pending(state))
    result = await tool_registry.execute("approval_probe", state, value="frozen")
    assert result["value"] == "frozen" and effects == ["frozen"]
    assert (await approvals.run_approved(state, record["operation_id"])) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_before_crash", [False, True])
async def test_restart_between_decision_receipt_and_message_checkpoint(
    operation, completed_before_crash
):
    from core.agent_loop import _persist_messages_checkpoint, load_messages_checkpoint, run_loop

    state, effects = operation
    payload = await pending(state)
    call = {
        "id": "original",
        "type": "function",
        "function": {"name": "approval_probe", "arguments": json.dumps({"value": "frozen"})},
    }
    messages = [
        LLMMessage(role="system", content="test"),
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(
            role="tool",
            tool_call_id="original",
            name="approval_probe",
            content=json.dumps({"status": "pause", "pause_event": payload}),
        ),
    ]
    harness = NodeHarness(
        node_type="experiment",
        tools=[],
        max_turns=2,
        loop_hooks_disable=[
            "memory_delta",
            "kb_delta",
            "turn_budget",
            "memory_onboarding",
            "law_review_gate",
        ],
        summarizer=SummarizerConfig(enabled=False),
    )

    class Model:
        async def chat(self, messages, **_):
            assert effects == ["frozen"]
            assert (
                json.loads(
                    next(
                        m.content
                        for m in reversed(messages)
                        if m.role == "tool" and m.tool_call_id == "original"
                    )
                )["value"]
                == "frozen"
            )
            return LLMResponse(content="complete", tool_calls=[], finish_reason="stop", usage={})

    _persist_messages_checkpoint(state, messages, turn=1)
    _pause_loop(
        harness,
        state,
        messages,
        Model(),
        call,
        {"status": "pause", "pause_event": payload},
        turn=1,
        all_tool_calls=[],
        max_turns=2,
    )
    record = approve(state, payload)
    if completed_before_crash:
        await approvals.run_approved(state, record["operation_id"])
    pause.clear_all()
    reopened = State(
        run_id=state.run_id, node_type=state.node_type, root=state.root, session_id=state.session_id
    )
    saved, _ = load_messages_checkpoint(reopened)
    result = await run_loop(harness, reopened, saved, Model())
    assert result.status == "completed" and effects == ["frozen"]
    assert not (state.root / "pause_pending.json").exists()


@pytest.mark.asyncio
async def test_old_command_pass_files_cannot_authorize_any_new_operation(operation):
    state, effects = operation
    # Pre-0.5.17 files remain evidence; the new authority never reads them.
    import hashlib
    import time

    key = hashlib.sha256(json.dumps({"value": "frozen"}).encode()).hexdigest()[:16]
    state.project_root = state.root.parent
    (state.project_root / "highrisk_passes.json").write_text(
        json.dumps(
            {
                f"{state.session_id}:{key}": {
                    "granted_at": time.time(),
                    "expires_at": time.time() + 3600,
                }
            }
        )
    )
    state.hook_state["_highrisk_confirmed_cmds"] = {key: True}
    await pending(state)
    assert not effects


@pytest.mark.asyncio
async def test_an_approval_cannot_write_plaintext_secrets_into_its_record(operation, monkeypatch):
    state, effects = operation
    monkeypatch.setenv("APPROVAL_TEST_API_KEY", "test-secret-never-persist-123456")
    with pytest.raises(approvals.ApprovalError, match="Secret-bearing"):
        await tool_registry.execute(
            "approval_probe", state, value="test-secret-never-persist-123456"
        )
    assert not list((state.root / "approvals").glob("*.json")) and not effects


@pytest.mark.asyncio
async def test_approved_result_reaches_owner_hooks_and_can_finish_without_another_model_turn(
    operation, monkeypatch
):
    from core import loop_hooks

    state, effects = operation
    payload = await pending(state)
    observed = []

    def finish(context):
        observed.extend(context.tool_call_records)
        context.state.hook_state["_loop_terminal"] = {
            "status": "completed",
            "final_text": "owner confirmed original receipt",
            "requested_by": "test-owner",
        }

    monkeypatch.setitem(
        loop_hooks._HOOKS,
        "approval_owner",
        loop_hooks.LoopHook(name="approval_owner", on_turn_end=finish),
    )
    harness = NodeHarness(
        node_type="experiment",
        tools=[],
        loop_hooks=["approval_owner"],
        max_turns=2,
        loop_hooks_disable=[
            "memory_delta",
            "kb_delta",
            "turn_budget",
            "memory_onboarding",
            "law_review_gate",
        ],
        summarizer=SummarizerConfig(enabled=False),
    )
    call = {
        "id": "original",
        "type": "function",
        "function": {"name": "approval_probe", "arguments": json.dumps({"value": "frozen"})},
    }
    messages = [
        LLMMessage(role="system", content="test"),
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(
            role="tool",
            tool_call_id="original",
            name="approval_probe",
            content=json.dumps({"status": "pause", "pause_event": payload}),
        ),
    ]

    class Model:
        async def chat(self, *args, **kwargs):
            pytest.fail("The owner already confirmed a terminal result")

    _pause_loop(
        harness,
        state,
        messages,
        Model(),
        call,
        {"status": "pause", "pause_event": payload},
        turn=1,
        all_tool_calls=[],
        max_turns=2,
    )
    result = await resume_loop(
        pause.get_paused_run(state.run_id),
        {"offer_id": payload["offer_id"], "choice_id": "approve"},
    )
    assert result.final_text == "owner confirmed original receipt" and effects == ["frozen"]
    assert len(observed) == 1 and observed[0]["result"]["value"] == "frozen"


@pytest.mark.asyncio
async def test_an_unknown_receipt_can_be_corrected_by_its_owner_without_resubmission(
    operation, monkeypatch
):
    from core.conversation_store import reconcile_reentrant_calls, unresolved_tool_calls

    state, effects = operation
    state._tool_dispatches = {"original": "original-dispatch"}
    unknown = {"status": "error", "outcome": "unknown", "do_not_resubmit": True}
    confirmed = {"status": "success", "receipt": "original-effect"}

    async def execute(*, state, value="frozen", **_):
        key = json.dumps({"value": value})
        if not danger.is_confirmed(state, key):
            return danger.build_pause_payload(
                state, tool="approval_probe", text=key, category="audit", preview=key
            )
        effects.append(value)
        return unknown

    def owner_receipt(arguments, *, state, tool_call_id):
        assert arguments == {"value": "frozen"} and tool_call_id == "original"
        return {"result": confirmed}

    definition = tool_registry.get_tool("approval_probe")
    monkeypatch.setattr(definition, "interrupted_recovery", owner_receipt)
    tool_registry.register_tool(definition, execute)
    record = approve(state, await pending(state))
    initial = await approvals.run_approved(state, record["operation_id"])
    assert initial["outcome"] == "unknown" and initial["do_not_resubmit"]
    call = {
        "id": "original",
        "type": "function",
        "function": {"name": "approval_probe", "arguments": json.dumps({"value": "frozen"})},
    }
    messages = [
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(
            role="tool", tool_call_id="original", name="approval_probe", content=json.dumps(unknown)
        ),
    ]
    await reconcile_reentrant_calls(state, messages, turn=1)
    assert not unresolved_tool_calls(messages)
    assert await approvals.run_approved(state, record["operation_id"]) == confirmed
    assert effects == ["frozen"]


@pytest.mark.asyncio
async def test_a_stale_pause_marker_cannot_replace_a_later_unconfirmed_dispatch(operation):
    from core.agent_loop import _persist_messages_checkpoint, restore_pause

    state, effects = operation
    old = await pending(state)
    event = pause.PauseEvent.from_payload(old, pending_tool_call_id="original")
    from core.agent_loop import write_pause_pending

    write_pause_pending(state, event, 1)
    call = {
        "id": "later-call",
        "type": "function",
        "function": {"name": "approval_probe", "arguments": json.dumps({"value": "later"})},
    }
    messages = [LLMMessage(role="assistant", tool_calls=[call])]
    state.hook_state["_current_tool_call_id_being_executed"] = "later-call"
    _persist_messages_checkpoint(state, messages, turn=2)
    assert await restore_pause(None, state, messages, None) is None
    assert not (state.root / "pause_pending.json").exists() and not effects


@pytest.mark.asyncio
async def test_a_permission_without_persistent_run_identity_is_never_offered(operation):
    from types import SimpleNamespace

    minimal = SimpleNamespace(run_id="no-directory", node_type="experiment", hook_state={})
    with pytest.raises(approvals.ApprovalError, match="persistent run"):
        await tool_registry.execute("approval_probe", minimal, value="frozen")
