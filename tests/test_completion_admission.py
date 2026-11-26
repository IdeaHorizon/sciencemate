"""Core completion asks its selected provider and never infers task success."""

import pytest

from core import completion, loop_hooks
from core.agent_loop import LoopResult
from core.harness import NodeHarness
from core.state import State


def context(tmp_path, monkeypatch, provider=None, *, limit=2, can_reenter=True):
    name = "test_completion_admission"
    state = State.new("admission_test", tmp_path)
    harness = NodeHarness(
        node_type="admission_test", completion_provider=name, completion_reentry_limit=limit
    )
    if provider is not None:
        monkeypatch.setitem(
            loop_hooks._HOOKS,
            name,
            loop_hooks.LoopHook(name=name, on_completion_admission=provider),
        )
    return completion.CompletionContext(harness, state, [], 1, can_reenter)


@pytest.mark.asyncio
async def test_unconfigured_nodes_keep_their_existing_contract_without_inferred_success(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path, monkeypatch)
    ctx.harness.completion_provider = None
    result = await completion.evaluate(ctx, LoopResult("", 1))
    assert result.disposition == "allow"
    assert result.task_outcome is None
    assert completion.current_decision(ctx.state) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "exception", "wrong_type"])
async def test_selected_provider_failures_suspend_instead_of_becoming_success(
    tmp_path, monkeypatch, failure
):
    def provider(ctx, result):
        if failure == "exception":
            raise OSError("sensitive provider URL or token must not be serialized")
        return {"passed": True}

    ctx = context(tmp_path, monkeypatch, provider if failure != "missing" else None)
    result = await completion.evaluate(ctx, LoopResult("", 1))
    assert result.disposition == "suspend"
    assert result.code == "completion_provider_unavailable"
    assert result.task_outcome is None
    assert "sensitive" not in str(result.to_dict())
    assert "node_task_outcome" not in ctx.state.hook_state


@pytest.mark.asyncio
async def test_same_read_set_reentry_is_bounded_and_recomputed_after_real_change(
    tmp_path, monkeypatch
):
    revision = ["a"]
    calls = []

    async def provider(ctx, result):
        calls.append(revision[0])
        return completion.CompletionDecision(
            "reenter",
            "delivery_unsealed",
            revision[0],
            {"manifest": revision[0]},
            notice="Seal the delivery.",
        )

    ctx = context(tmp_path, monkeypatch, provider)
    proposed = LoopResult("", 1)
    assert (await completion.evaluate(ctx, proposed)).disposition == "reenter"
    assert (await completion.evaluate(ctx, proposed)).disposition == "reenter"
    denied = await completion.evaluate(ctx, proposed)
    assert denied.disposition == "suspend"
    assert denied.facts["suspension_reason"] == "same_fact_no_progress"
    revision[0] = "b"
    assert (await completion.evaluate(ctx, proposed)).disposition == "reenter"
    assert calls == ["a", "a", "a", "b"]


@pytest.mark.asyncio
async def test_no_remaining_execution_budget_suspends_without_granting_an_extra_round(
    tmp_path, monkeypatch
):
    ctx = context(
        tmp_path,
        monkeypatch,
        lambda *_: completion.CompletionDecision("reenter", "delivery_unsealed", "a", {}),
        can_reenter=False,
    )
    result = await completion.evaluate(ctx, LoopResult("", 3))
    assert result.disposition == "suspend"
    assert result.facts["suspension_reason"] == "reentry_budget_unavailable"
    assert not ctx.state.hook_state["_completion_admission"].get("reentries")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "partial", "failed", "blocked", "cancelled"])
async def test_allowed_delivery_preserves_its_actual_task_outcome(tmp_path, monkeypatch, outcome):
    ctx = context(
        tmp_path,
        monkeypatch,
        lambda *_: completion.CompletionDecision(
            "allow", "delivery_sealed", "sealed-head", {}, task_outcome=outcome
        ),
    )
    result = await completion.evaluate(ctx, LoopResult("", 1))
    assert result.disposition == "allow"
    assert "node_task_outcome" not in ctx.state.hook_state
    completion.accept_outcome(ctx.state, ctx.harness.completion_provider, result)
    assert ctx.state.hook_state["node_task_outcome"]["outcome"] == outcome


@pytest.mark.asyncio
async def test_matching_existing_outcome_keeps_dispatch_feedback_and_identity(
    tmp_path, monkeypatch
):
    ctx = context(
        tmp_path,
        monkeypatch,
        lambda *_: completion.CompletionDecision(
            "allow", "delivery_sealed", "sealed-head", {}, task_outcome="success"
        ),
    )
    original = {
        "outcome": "success",
        "dispatch_action": "reuse_delivery",
        "artifact_id": "exact-delivery",
        "dispatch_feedback": {"reason": "correction"},
    }
    ctx.state.hook_state["node_task_outcome"] = dict(original)
    decision = await completion.evaluate(ctx, LoopResult("", 1))
    completion.accept_outcome(ctx.state, ctx.harness.completion_provider, decision)
    assert ctx.state.hook_state["node_task_outcome"] == original


@pytest.mark.asyncio
async def test_decision_write_failure_cannot_become_an_allowed_completion(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        monkeypatch,
        lambda *_: completion.CompletionDecision(
            "allow", "delivery_sealed", "a", {}, task_outcome="success"
        ),
    )

    def no_storage(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(ctx.state, "append_transcript", no_storage)
    with pytest.raises(completion.CompletionPersistenceError):
        await completion.evaluate(ctx, LoopResult("", 1))
    assert "node_task_outcome" not in ctx.state.hook_state


def test_provider_cannot_mutate_accepted_facts_through_its_input_dict():
    original = {"head": {"hash": "first"}}
    decision = completion.CompletionDecision("allow", "sealed", "v1", original)
    original["head"]["hash"] = "changed"
    assert decision.facts == {"head": {"hash": "first"}}


@pytest.mark.parametrize(
    "changes",
    [
        {"disposition": "success"},
        {"code": ""},
        {"fact_revision": ""},
        {"facts": []},
        {"facts": {"value": float("nan")}},
        {"disposition": "reenter", "task_outcome": "success"},
        {"task_outcome": "maybe"},
    ],
)
def test_invalid_decisions_are_rejected_at_the_boundary(changes):
    with pytest.raises((TypeError, ValueError)):
        completion.CompletionDecision(
            **{
                "disposition": "allow",
                "code": "sealed",
                "fact_revision": "v1",
                "facts": {},
                **changes,
            }
        )
