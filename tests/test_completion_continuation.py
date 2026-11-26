"""Accepted work keeps its original owner while completion is suspended."""

import asyncio
import json

import pytest

from core import cancellation, completion_driver, loop_hooks
from core.agent_loop import LoopResult, load_messages_checkpoint, run_loop
from core.completion import CompletionDecision
from core.harness import NodeHarness
from core.pause import clear_all, get_active_run
from core.state import State


@pytest.fixture(autouse=True)
def registry():
    clear_all()
    yield
    clear_all()


def setup(tmp_path, monkeypatch, *, cap=5):
    state = State.new("completion_test", tmp_path, project_id="completion", session_id="owner")
    harness = NodeHarness(
        node_type="completion_test", completion_provider="test_admit", max_turns=cap
    )
    sealed = [False]

    def provider(ctx, candidate):
        return CompletionDecision(
            "allow" if sealed[0] else "reenter",
            "unsealed",
            str(sealed[0]),
            {},
            notice="Seal the existing delivery without replaying its effects.",
            task_outcome="success" if sealed[0] else None,
        )

    monkeypatch.setitem(
        loop_hooks._HOOKS,
        "test_admit",
        loop_hooks.LoopHook("test_admit", on_completion_admission=provider),
    )
    calls = []

    async def runner():
        calls.append(state.run_id)
        return LoopResult(
            "candidate",
            completion_driver.turn_offset(state) + 1,
            messages=[],
            terminal_cause="model_finished",
        )

    return state, harness, sealed, calls, runner


async def suspended():
    async with asyncio.timeout(3):
        while not completion_driver.suspended_runs():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_bounded_reentry_then_non_tool_resume_keeps_same_parent_await(tmp_path, monkeypatch):
    state, harness, sealed, calls, runner = setup(tmp_path, monkeypatch)
    messages = []

    async def parent():
        with cancellation.bind_run(state):
            return await completion_driver.drive(harness, state, messages, object(), runner)

    task = asyncio.create_task(parent())
    await suspended()
    assert calls == [state.run_id] * 3
    assert not task.done()
    assert get_active_run(state.run_id).state is state
    saved = json.loads((state.root / "messages_checkpoint.json").read_text())
    assert saved["hook_state"]["_completion_admission"]["suspended"] is True
    assert not (state.root / "summary.json").exists()
    with pytest.raises(ValueError, match="belong"):
        completion_driver.resume(state.run_id, project_id="other", session_id="owner")
    sealed[0] = True
    completion_driver.resume(state.run_id, project_id=state.project_id, session_id="owner")
    result = await asyncio.wait_for(task, 3)
    assert result.status == "completed"
    assert calls == [state.run_id] * 3  # A corrected receipt does not rerun effects.
    assert result.completion_admission["task_outcome"] == "success"
    assert get_active_run(state.run_id) is None


@pytest.mark.asyncio
async def test_stop_preempts_suspension_without_a_model_turn(tmp_path, monkeypatch):
    state, harness, _, calls, runner = setup(tmp_path, monkeypatch, cap=1)

    async def drive():
        with cancellation.bind_run(state):
            return await completion_driver.drive(harness, state, [], object(), runner)

    task = asyncio.create_task(drive())
    await suspended()
    state.hook_state["kill_signal"] = {"reason": "user stop", "requested_by": "user"}
    with pytest.raises(cancellation.RunCancelled):
        await asyncio.wait_for(task, 3)
    assert len(calls) == 1
    assert not (state.root / "summary.json").exists()


@pytest.mark.asyncio
async def test_default_loop_owns_admission_but_nested_loop_does_not_stamp_it(tmp_path, monkeypatch):
    state, harness, sealed, calls, runner = setup(tmp_path, monkeypatch, cap=1)

    async def body(*args):
        return await runner()

    monkeypatch.setattr("core.agent_loop._run_loop_body", body)
    task = asyncio.create_task(run_loop(harness, state, [], object()))
    await suspended()
    state.hook_state["kill_signal"] = {"reason": "stop"}
    assert (await asyncio.wait_for(task, 3)).status == "cancelled"
    records = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert not any(
        row["event"] == "loop_finished" and row["status"] == "completed" for row in records
    )


@pytest.mark.asyncio
async def test_restart_recovers_candidate_and_counts_without_model_or_effect_replay(
    tmp_path, monkeypatch
):
    state, harness, sealed, calls, runner = setup(tmp_path, monkeypatch)
    with cancellation.bind_run(state):
        result = await completion_driver.drive(harness, state, [], None, runner)
    assert result.status == "suspended"
    clear_all()  # Simulate losing all process memory, retaining the one existing snapshot.
    state.hook_state.clear()
    messages, _ = load_messages_checkpoint(state)
    assert state.hook_state["_completion_admission"]["suspended"]
    sealed[0] = True
    with cancellation.bind_run(state):
        result = await completion_driver.drive(harness, state, messages, object(), runner)
    assert result.status == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_checkpoint_keeps_owner_and_retry_is_before_any_new_effect(
    tmp_path, monkeypatch
):
    state, harness, sealed, calls, runner = setup(tmp_path, monkeypatch, cap=1)
    real = completion_driver._checkpoint
    broken = [True]

    def checkpoint(*args, **kwargs):
        if broken[0]:
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(completion_driver, "_checkpoint", checkpoint)

    async def drive():
        with cancellation.bind_run(state):
            return await completion_driver.drive(harness, state, [], object(), runner)

    task = asyncio.create_task(drive())
    await suspended()
    assert not completion_driver.suspended_runs()[0]["recoverable"]
    assert get_active_run(state.run_id).state is state
    broken[0] = False
    sealed[0] = True
    completion_driver.resume(state.run_id, project_id=state.project_id, session_id="owner")
    assert (await asyncio.wait_for(task, 3)).status == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        "model_finished",
        "turn_cap",
        "degenerate_repetition",
        "protocol_circuit_break",
        "progress_circuit_break",
        "hook_terminal_completed",
        "hook_terminal_failed",
        "provider_void",
        "external_kill",
    ],
)
async def test_all_candidates_require_admission_including_unconfirmed_cancel(
    tmp_path, monkeypatch, cause
):
    from core.agent_loop import _TERMINAL_CAUSES

    state, harness, _, _, runner = setup(tmp_path, monkeypatch, cap=1)
    status = _TERMINAL_CAUSES[cause][1]
    candidate = LoopResult("ending", 1, status=status, terminal_cause=cause)
    with cancellation.bind_run(state):
        result = await completion_driver.drive(
            harness, state, [], None, runner, candidate=candidate
        )
    assert result.status == "suspended"
    assert not (state.root / "summary.json").exists()


@pytest.mark.asyncio
async def test_finalizer_recheck_cannot_reset_the_original_turn_budget(tmp_path, monkeypatch):
    from core.executor import finalize_run

    state, harness, sealed, calls, runner = setup(tmp_path, monkeypatch, cap=1)
    candidate = LoopResult("ending", 1, max_turns=1, terminal_cause="turn_cap")
    task = asyncio.create_task(finalize_run(state, harness, candidate, object()))
    await suspended()
    assert not calls
    assert (
        state.hook_state["_completion_admission"]["last_decision"]["facts"]["suspension_reason"]
        == "reentry_budget_unavailable"
    )
    sealed[0] = True
    completion_driver.resume(state.run_id, project_id=state.project_id, session_id="owner")
    result = await asyncio.wait_for(task, 3)
    assert result["status"] == "completed"
    assert result["completion_admission"]["disposition"] == "allow"


@pytest.mark.asyncio
async def test_observer_cannot_invalidate_facts_after_an_ending_is_published(tmp_path, monkeypatch):
    state, harness, sealed, _, runner = setup(tmp_path, monkeypatch, cap=1)
    sealed[0] = True

    def change(ctx, candidate):
        sealed[0] = False

    monkeypatch.setitem(
        loop_hooks._HOOKS,
        "changes_delivery",
        loop_hooks.LoopHook("changes_delivery", on_end=change),
    )
    harness.loop_hooks = ["changes_delivery"]

    async def drive():
        with cancellation.bind_run(state):
            return await completion_driver.drive(harness, state, [], object(), runner)

    task = asyncio.create_task(drive())
    await suspended()
    assert "node_task_outcome" not in state.hook_state
    records = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert not any(row["event"] == "loop_finished" for row in records)
    state.hook_state["kill_signal"] = {"reason": "stop"}
    with pytest.raises(cancellation.RunCancelled):
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_default", [False, True])
async def test_custom_executor_cannot_release_its_parent_before_admission(
    tmp_path, monkeypatch, nested_default
):
    from core import custom_loop, executor

    _, harness, sealed, _, _ = setup(tmp_path, monkeypatch, cap=1)
    harness.node_type = "data"
    harness.completion_reentry_limit = 0
    children = []
    calls = []

    async def body(h, state, messages, llm):
        calls.append(state.run_id)
        return LoopResult("custom candidate", 1, messages=messages, terminal_cause="model_finished")

    async def custom(h, state, messages, llm):
        if nested_default:
            return await run_loop(h, state, messages, llm)
        return await body(h, state, messages, llm)

    monkeypatch.setattr(custom_loop, "resolve_custom_loop", lambda _: custom)
    monkeypatch.setattr("core.agent_loop._run_loop_body", body)
    task = asyncio.create_task(
        executor.execute_node(
            node_type="data",
            state_dir=tmp_path,
            harness_override=harness,
            llm=object(),
            on_state_ready=children.append,
        )
    )
    await suspended()
    child = children[0]
    assert not task.done()
    assert get_active_run(child.run_id).state is child
    rows = [json.loads(line) for line in child.transcript_path.read_text().splitlines()]
    assert not any(row["event"] in {"run_end", "loop_finished"} for row in rows)
    sealed[0] = True
    completion_driver.resume(child.run_id, project_id=child.project_id, session_id=child.session_id)
    summary = await asyncio.wait_for(task, 3)
    assert summary["status"] == "completed"
    assert summary["run_id"] == child.run_id
    assert calls == [child.run_id]
    assert get_active_run(child.run_id) is None


@pytest.mark.asyncio
async def test_finalization_projects_artifacts_created_during_completion_reentry(
    tmp_path, monkeypatch
):
    from core import custom_loop
    from core.executor import finalize_run
    from shared.tools.library import kb

    state, harness, sealed, _, _ = setup(tmp_path, monkeypatch)
    state = State.new("analysis", tmp_path / "analysis")
    harness.node_type = "analysis"
    projected = []

    async def repair(h, owner, messages, llm):
        from core.artifact_capabilities import save_typed_artifact

        save_typed_artifact(
            owner, artifact_type="research_state", name="created_during_reentry", content="{}"
        )
        sealed[0] = True
        return LoopResult("delivery ready", 1, messages=messages, terminal_cause="model_finished")

    async def project(owner, record):
        projected.append(record["name"])
        return []

    monkeypatch.setattr(custom_loop, "resolve_custom_loop", lambda _: repair)
    monkeypatch.setattr(kb, "project_research_state_to_kb", project)
    result = await asyncio.wait_for(
        finalize_run(state, harness, LoopResult("initial", 0), object()), 3
    )
    assert result["status"] == "completed"
    assert projected == ["created_during_reentry"]


@pytest.mark.asyncio
async def test_finalizer_revalidates_after_await_before_any_terminal_write(tmp_path, monkeypatch):
    from core.artifact_capabilities import save_typed_artifact
    from core.executor import finalize_run
    from shared.tools.library import kb

    _, harness, sealed, _, _ = setup(tmp_path, monkeypatch, cap=1)
    state = State.new("analysis", tmp_path / "analysis")
    harness.node_type = "analysis"
    save_typed_artifact(state, artifact_type="research_state", name="existing", content="{}")
    sealed[0] = True
    projections = []

    async def project(owner, record):
        projections.append(record["name"])
        await asyncio.sleep(0)
        if len(projections) == 1:
            sealed[0] = False
        return []

    monkeypatch.setattr(kb, "project_research_state_to_kb", project)
    task = asyncio.create_task(finalize_run(state, harness, LoopResult("ending", 1), object()))
    await suspended()
    assert len(projections) == 1
    assert not state.summary_path.exists()
    rows = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert not any(row["event"] == "run_end" for row in rows)
    sealed[0] = True
    completion_driver.resume(state.run_id, project_id=state.project_id, session_id=state.session_id)
    assert (await asyncio.wait_for(task, 3))["status"] == "completed"
    assert len(projections) == 2
