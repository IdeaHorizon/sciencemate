"""Completion continuation on the existing active-run/checkpoint lifecycle.

The context variable prevents nested default loops from admitting an outer
custom loop's candidate. It is call-stack ownership, never durable run state.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from core.completion import (
    CompletionContext,
    CompletionDecision,
    CompletionPersistenceError,
    accept_outcome,
    configured,
    current_decision,
    evaluate,
)

_OWNER: ContextVar[Any] = ContextVar("completion_owner", default=None)


@dataclass
class Continuation:
    state: Any
    harness: Any
    messages: list
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    used: int = 0
    suspended: bool = False
    persisted: bool = False
    live: bool = False


def owned(state) -> bool:
    owner = _OWNER.get()
    return owner is not None and owner.state is state


def turn_offset(state) -> int:
    owner = _OWNER.get()
    return owner.used if owner is not None and owner.state is state else 0


def suspended_runs() -> list[dict]:
    from core.pause import list_active_runs

    return [
        {
            "run_id": info.run_id,
            "node_type": info.node_type,
            "decision": current_decision(info.state),
            "recoverable": info.completion.persisted,
            "driver_available": info.completion.live,
        }
        for info in list_active_runs()
        if info.completion is not None and info.completion.suspended
    ]


def resume(run_id: str, *, project_id: str, session_id: str) -> None:
    """Management-plane wakeup; no tool id, new child, or copied State."""
    from core.pause import get_active_run

    info = get_active_run(run_id)
    if (
        info is None
        or str(info.state.project_id) != str(project_id)
        or str(info.state.session_id) != str(session_id)
    ):
        raise ValueError("completion run does not belong to this session")
    frame = info.completion
    if frame is None or not frame.suspended or not frame.live:
        raise ValueError("run has no suspended completion continuation")
    frame.wake.set()


def _checkpoint(frame, candidate, *, suspended: bool) -> None:
    from core.agent_loop import _persist_messages_checkpoint

    saved = frame.state.hook_state.setdefault("_completion_admission", {})
    saved["suspended"] = suspended
    saved["candidate"] = {
        key: getattr(candidate, key)
        for key in (
            "final_text",
            "turns",
            "tool_calls",
            "max_turns",
            "status",
            "terminal_cause",
            "terminal_kind",
            "cancel_meta",
            "completion_admission",
        )
    }
    saved["used"] = frame.used
    _persist_messages_checkpoint(frame.state, frame.messages, turn=candidate.turns)
    frame.persisted = True


def publish_finished(state, candidate) -> None:
    state.append_transcript(
        "loop_finished",
        turns=candidate.turns,
        status=candidate.status,
        terminal_kind=candidate.terminal_kind,
        terminal_cause=candidate.terminal_cause,
        managed_finalization=bool(state.hook_state.get("_executor_finalizes")),
        completion_admission=current_decision(state),
    )


async def drive(harness, state, messages, llm, runner, *, candidate=None):
    """Admit every ending; park the original caller until repair/resume/stop.

    A finalizer without a model can expose a suspended view, but cannot execute
    a continuation. Real drivers keep their original parent await intact.
    """
    if not configured(harness) or owned(state):
        return candidate if candidate is not None else await runner()
    import os

    from core import cancellation
    from core.agent_loop import LoopResult
    from core.conversation_store import UnconfirmedToolOutcome
    from core.job_wait import cancellable
    from core.llm import framework_notice
    from core.pause import ActiveRunInfo, get_active_run, register_active, unregister_active
    from core.turn_budget import resolve_cap

    info = get_active_run(state.run_id)
    created = info is None
    if created:
        info = ActiveRunInfo(
            state.run_id, state.node_type, state, parent_run_id=state.parent_run_id
        )
        if llm is not None:
            register_active(info)
    frame = Continuation(state, harness, messages, live=llm is not None)
    info.completion = frame
    token = _OWNER.set(frame)
    saved = state.hook_state.get("_completion_admission") or {}
    if candidate is None and saved.get("suspended") and isinstance(saved.get("candidate"), dict):
        candidate = LoopResult(**saved["candidate"], messages=messages)
        frame.used = int(saved.get("used") or 0)
    if candidate is not None:
        frame.used = max(frame.used, int(candidate.turns or 0))
    all_calls = list(candidate.tool_calls) if candidate is not None else []
    cap = resolve_cap(
        mode_turns=harness.max_turns_for(state.hook_state.get("_request_mode")),
        env_default=os.environ.get("HARNESS_DEFAULT_MAX_TURNS"),
    )
    explicit_cycle = False
    accepted = False
    try:
        while True:
            if candidate is None:
                try:
                    candidate = await runner()
                except cancellation.RunCancelled:
                    raise
                except UnconfirmedToolOutcome:
                    raise
                except Exception as exc:
                    # An LLM/provider failure is an ending candidate, not
                    # permission to stamp unfinished external work terminal.
                    state.append_transcript(
                        "completion_runner_error", error_type=type(exc).__name__
                    )
                    candidate = LoopResult(
                        "模型调用中断，已有现场保留。",
                        frame.used,
                        messages=messages,
                        status="failed",
                        terminal_cause="hook_terminal_failed",
                    )
                all_calls.extend(candidate.tool_calls)
                candidate = replace(candidate, tool_calls=list(all_calls))
                frame.used = max(frame.used + 1, int(candidate.turns or 0))
                if candidate.messages is not messages:
                    messages[:] = candidate.messages
                frame.messages = messages
            cancellation.check("completion.admission")
            if candidate.status == "cancelled":
                # A custom loop's label is not a physical cancellation receipt.
                candidate = replace(
                    candidate, terminal_cause="unconfirmed_cancellation", status="incomplete"
                )
            if candidate.status == "paused" and candidate.pause_event is not None:
                accepted = True
                return candidate
            try:
                decision = await evaluate(
                    CompletionContext(
                        harness,
                        state,
                        frame.messages,
                        candidate.turns,
                        llm is not None and (not cap or frame.used < cap),
                    ),
                    candidate,
                )
                if (
                    decision.disposition == "allow"
                    and candidate.completion_admission is None
                    and llm is not None
                ):
                    from core import loop_hooks
                    from core.agent_loop import resolve_enabled_hook_names

                    try:
                        await loop_hooks.run_on_end(
                            loop_hooks.list_hooks(resolve_enabled_hook_names(harness)),
                            loop_hooks.HookContext(
                                harness=harness,
                                state=state,
                                messages=messages,
                                turn=candidate.turns,
                            ),
                            candidate,
                        )
                    except cancellation.RunCancelled:
                        raise
                    except Exception as exc:
                        decision = CompletionDecision(
                            "suspend",
                            "completion_observer_failed",
                            type(exc).__name__,
                            {"owner": "framework", "error_type": type(exc).__name__},
                            "收尾观察失败。保留已有交付，修正运行条件后可继续。",
                        )
                    else:
                        # Observers cannot invalidate an already-published ending.
                        # Recheck facts before publishing the first ending instead.
                        decision = await evaluate(
                            CompletionContext(
                                harness,
                                state,
                                messages,
                                candidate.turns,
                                llm is not None and (not cap or frame.used < cap),
                            ),
                            candidate,
                        )
                if decision.disposition == "allow":
                    from core.task_outcome import status_floor

                    accept_outcome(state, harness.completion_provider, decision)
                    candidate = replace(
                        candidate,
                        completion_admission=decision.to_dict(),
                        status=status_floor(decision.task_outcome, candidate.status),
                    )
                    _checkpoint(frame, candidate, suspended=False)
                    cancellation.check("completion.accepted")
                    accepted = True
                    return candidate
                manual_reentry = (
                    explicit_cycle
                    and decision.disposition == "suspend"
                    and decision.facts.get("suspension_reason")
                    in {"same_fact_no_progress", "reentry_budget_unavailable"}
                )
                if decision.disposition == "reenter" or manual_reentry:
                    explicit_cycle = False
                    frame.messages.append(
                        framework_notice(
                            decision.notice
                            or "交付尚未满足核验条件；检查已有事实后继续，不要重提交已接受的操作。"
                        )
                    )
                    _checkpoint(frame, candidate, suspended=False)
                    candidate = None
                    continue
                state.hook_state.setdefault("_completion_admission", {})["last_decision"] = {
                    "provider": harness.completion_provider,
                    **decision.to_dict(),
                }
                _checkpoint(frame, candidate, suspended=True)
            except (CompletionPersistenceError, OSError) as exc:
                frame.persisted = False
                decision = CompletionDecision(
                    "suspend",
                    "completion_checkpoint_unavailable",
                    type(exc).__name__,
                    {"owner": "framework", "recoverable": False},
                    "现场保存失败，当前执行仍由原运行持有。恢复存储后可继续，或停止运行。",
                )
                state.hook_state.setdefault("_completion_admission", {})["last_decision"] = (
                    decision.to_dict()
                )
            frame.suspended = True
            try:
                state.append_transcript(
                    "run_suspended",
                    **decision.to_dict(),
                    recoverable=frame.persisted,
                    run_id=state.run_id,
                    resumable=frame.live,
                )
            except OSError:
                pass
            if llm is None:
                return replace(candidate, status="suspended", terminal_cause="completion_suspended")
            await cancellable(state, frame.wake.wait())
            frame.wake.clear()
            # A wake is explicit permission for one bounded period. It does
            # not reset the durable same-fact automatic reentry counters.
            frame.used = 0
            explicit_cycle = True
            frame.suspended = False
            state.append_transcript("completion_resumed", run_id=state.run_id)
    except cancellation.RunCancelled:
        accepted = True
        raise
    finally:
        frame.live = False
        _OWNER.reset(token)
        if accepted:
            info.completion = None
            if created:
                unregister_active(state.run_id)


def suspended_view(state, result) -> dict:
    """An observation, deliberately never persisted as a terminal summary."""
    return {
        "run_id": state.run_id,
        "node_type": state.node_type,
        "project_id": state.project_id,
        "status": "suspended",
        "completion_admission": current_decision(state),
        "missing_required_outputs": [],
        "delivery_empty": False,
        "turns": result.turns,
        "max_turns": result.max_turns,
    }
