"""Shared advisory-signal state for Experiment's stalled-work detectors.

This module deliberately owns only candidate collection, deduplication and
cooldown.  It does not inspect logs, mutate build/repair state, or create
terminal artifacts: those remain with the lifecycle-specific hook adapters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any


PENDING_KEY = "_experiment_stuck_signal_candidates_v1"
LAST_INJECTED_KEY = "_experiment_stuck_signal_last_injected_v1"
LEGACY_PENDING_KEY = "_execution_supervisor_candidates"
LEGACY_LAST_INJECTED_KEY = "_execution_supervisor_last_injected"
VALID_SCOPES = frozenset({"any", "scientific"})


@dataclass(frozen=True)
class StuckSignal:
    """One advisory candidate, produced by a lifecycle-specific adapter."""

    source: str
    priority: int
    content: str
    dedupe_key: str
    scope: str = "any"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SuppressedSignal:
    source: str
    dedupe_key: str
    reason: str


def _normal_scope(scope: str | None) -> str:
    normalized = str(scope or "any").strip().lower()
    return normalized if normalized in VALID_SCOPES else "any"


def _coerce(raw: Any) -> StuckSignal | None:
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    key = raw.get("dedupe_key")
    if not isinstance(content, str) or not content or not isinstance(key, str) or not key:
        return None
    try:
        priority = int(raw.get("priority", 99))
    except (TypeError, ValueError):
        priority = 99
    return StuckSignal(
        source=str(raw.get("source") or "unknown"), priority=priority,
        content=content, dedupe_key=key, scope=_normal_scope(raw.get("scope")),
    )


def _pending(hook_state: dict[str, Any]) -> dict[str, Any]:
    pending = hook_state.get(PENDING_KEY)
    if not isinstance(pending, dict):
        pending = {}
        hook_state[PENDING_KEY] = pending
    return pending


def offer(hook_state: dict[str, Any], *, source: str, priority: int,
          content: str, dedupe_key: str | None = None, scope: str = "any") -> None:
    """Store one signal per source for this arbitration cycle.

    Keeping the source key preserves the old supervisor's overwrite semantics:
    an adapter may refine its own candidate during a turn, but cannot overwrite
    another adapter's candidate.
    """
    if not content:
        return
    raw_key = dedupe_key or sha256(content.encode("utf-8")).hexdigest()[:16]
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 99
    signal = StuckSignal(
        source=str(source), priority=normalized_priority, content=str(content),
        dedupe_key=f"{source}:{raw_key}", scope=_normal_scope(scope),
    )
    _pending(hook_state)[signal.source] = signal.as_dict()


def drain(hook_state: dict[str, Any]) -> tuple[list[StuckSignal], int]:
    """Drain current candidates, migrating an interrupted legacy run once."""
    current = hook_state.pop(PENDING_KEY, {})
    legacy = hook_state.pop(LEGACY_PENDING_KEY, {})
    if not isinstance(current, dict):
        current = {}
    if isinstance(legacy, dict):
        # New-format candidates win if a resumed run happened to hold both.
        merged = dict(legacy)
        merged.update(current)
    else:
        merged = current
    signals = [signal for raw in merged.values() if (signal := _coerce(raw))]
    return signals, len(merged)


def _last_injected(hook_state: dict[str, Any]) -> dict[str, int]:
    current = hook_state.get(LAST_INJECTED_KEY)
    legacy = hook_state.get(LEGACY_LAST_INJECTED_KEY)
    source = current if isinstance(current, dict) else legacy
    if not isinstance(source, dict):
        source = {}
    normalized: dict[str, int] = {}
    for key, value in source.items():
        try:
            normalized[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    hook_state[LAST_INJECTED_KEY] = normalized
    # Retain this alias for resumed runs and extensions that read the old key.
    hook_state[LEGACY_LAST_INJECTED_KEY] = normalized
    return normalized


def choose(hook_state: dict[str, Any], *, turn: int, operational: bool,
           cooldown_turns: int) -> tuple[StuckSignal | None, list[SuppressedSignal], int]:
    """Return the one eligible highest-priority signal for this turn."""
    signals, candidate_count = drain(hook_state)
    if not signals:
        return None, [], candidate_count
    last = _last_injected(hook_state)
    eligible: list[StuckSignal] = []
    suppressed: list[SuppressedSignal] = []
    for signal in signals:
        if operational and signal.scope == "scientific":
            suppressed.append(SuppressedSignal(
                signal.source, signal.dedupe_key, "operation_scope"))
            continue
        if turn - last.get(signal.dedupe_key, -999) < cooldown_turns:
            suppressed.append(SuppressedSignal(
                signal.source, signal.dedupe_key, "cooldown"))
            continue
        eligible.append(signal)
    if not eligible:
        return None, suppressed, candidate_count
    return min(eligible, key=lambda item: (item.priority, item.source)), suppressed, candidate_count


def mark_injected(hook_state: dict[str, Any], signal: StuckSignal, *, turn: int) -> None:
    _last_injected(hook_state)[signal.dedupe_key] = int(turn)
