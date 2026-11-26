"""Typed, fail-closed completion admission; scientific facts stay with providers.

The continuation lifecycle uses the existing active-run index and conversation
checkpoint. This module owns the decision and automatic reentry budget only.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from core.task_outcome import OUTCOMES, record_task_outcome, task_outcome

_STATE_KEY = "_completion_admission"
_MAX_FACT_REVISIONS = 64
_MAX_DECISION_BYTES = 128 * 1024


@dataclass(frozen=True)
class CompletionDecision:
    disposition: Literal["allow", "reenter", "suspend"]
    code: str
    fact_revision: str
    facts: dict[str, Any]
    notice: str | None = None
    task_outcome: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in {"allow", "reenter", "suspend"}:
            raise ValueError("invalid completion disposition")
        if not isinstance(self.code, str) or not self.code or len(self.code) > 128:
            raise ValueError("completion code must be a nonempty bounded string")
        if (
            not isinstance(self.fact_revision, str)
            or not self.fact_revision
            or len(self.fact_revision) > 256
        ):
            raise ValueError("completion fact_revision must identify a bounded read-set version")
        if not isinstance(self.facts, dict):
            raise TypeError("completion facts must be a JSON object")
        if self.notice is not None and not isinstance(self.notice, str):
            raise TypeError("completion notice must be text")
        if self.task_outcome is not None and (
            self.task_outcome not in OUTCOMES or self.disposition != "allow"
        ):
            raise ValueError("only an allowed completion may declare a typed task outcome")
        encoded = json.dumps(asdict(self), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_DECISION_BYTES:
            raise ValueError("completion decision exceeds its evidence budget")
        # Providers cannot mutate the accepted evidence through an earlier dict.
        object.__setattr__(self, "facts", json.loads(encoded)["facts"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompletionContext:
    harness: Any
    state: Any
    messages: list[Any]
    turn: int
    can_reenter: bool


class CompletionPersistenceError(RuntimeError):
    """The accepted run still has an owner; it must not acquire a final stamp."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__("completion evidence could not be persisted")


def configured(harness: Any) -> bool:
    return bool(getattr(harness, "completion_provider", None))


def current_decision(state: Any) -> dict[str, Any] | None:
    record = state.hook_state.get(_STATE_KEY)
    decision = record.get("last_decision") if isinstance(record, dict) else None
    return dict(decision) if isinstance(decision, dict) else None


def _provider_failure(provider: str, exc: Exception) -> CompletionDecision:
    facts = {"provider": provider, "error_type": type(exc).__name__, "owner": "framework"}
    revision = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    return CompletionDecision(
        disposition="suspend",
        code="completion_provider_unavailable",
        fact_revision=revision,
        facts=facts,
        notice="暂时无法核实交付状态。保留已有现场，修复核验条件后可以继续。",
    )


async def evaluate(ctx: CompletionContext, proposed_result: Any) -> CompletionDecision:
    """Always reevaluate; only the (code, read-set revision) retry count is cached."""
    provider_name = getattr(ctx.harness, "completion_provider", None)
    if not provider_name:
        return CompletionDecision("allow", "completion_not_configured", "legacy", {})
    try:
        if not isinstance(provider_name, str):
            raise TypeError("completion provider name must be text")
        from core.loop_hooks import get_loop_hook

        hook = get_loop_hook(provider_name)
        provider = hook.on_completion_admission if hook else None
        if provider is None:
            raise LookupError("configured completion provider is not registered")
        decision = provider(ctx, proposed_result)
        if inspect.isawaitable(decision):
            decision = await decision
        if not isinstance(decision, CompletionDecision):
            raise TypeError("completion provider did not return a CompletionDecision")
        # Validate again after an asynchronous callback has released its inputs.
        decision = CompletionDecision(**decision.to_dict())
    except Exception as exc:
        decision = _provider_failure(str(provider_name), exc)

    state = ctx.state
    saved = state.hook_state.setdefault(_STATE_KEY, {})
    if decision.disposition == "reenter":
        limit = ctx.harness.completion_reentry_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            decision = _provider_failure(str(provider_name), ValueError("invalid reentry policy"))
        elif not ctx.can_reenter:
            decision = replace(
                decision,
                disposition="suspend",
                facts={**decision.facts, "suspension_reason": "reentry_budget_unavailable"},
            )
        else:
            key = hashlib.sha256(
                json.dumps(
                    [decision.code, decision.fact_revision],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            counts = saved.setdefault("reentries", {})
            count = int(counts.get(key, 0))
            if count >= limit:
                decision = replace(
                    decision,
                    disposition="suspend",
                    facts={
                        **decision.facts,
                        "suspension_reason": "same_fact_no_progress",
                        "reentries": count,
                    },
                )
            else:
                counts[key] = count + 1
                # Superseded immutable read-set versions are not lifecycle state.
                while len(counts) > _MAX_FACT_REVISIONS:
                    del counts[next(iter(counts))]

    payload = {"provider": provider_name, **decision.to_dict()}
    saved["last_decision"] = payload
    try:
        state.append_transcript("completion_admission", **payload)
    except Exception as exc:
        raise CompletionPersistenceError(state.run_id) from exc
    return decision


def accept_outcome(state, provider_name: str, decision: CompletionDecision) -> None:
    """Bridge only the final admitted decision, after observers and revalidation."""
    if decision.disposition == "allow" and decision.task_outcome is not None:
        previous = task_outcome(state)
        if previous is None or previous.get("outcome") != decision.task_outcome:
            record_task_outcome(
                state,
                decision.task_outcome,
                detail=decision.notice or decision.code,
                reported_by="completion:" + provider_name,
            )
