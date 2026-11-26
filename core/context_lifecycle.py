"""Request-scoped context measurements and transactional working-set compaction.

The append-only history is evidence. The messages submitted to a model are a
bounded projection of that history. A model-generated summary is one possible
projection, never the authority deciding whether the request fits.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

from .llm import (
    framework_notice,
    framework_notice_body,
    is_framework_notice,
    is_primary_input,
    normalize_request_messages,
)


@dataclass(frozen=True)
class RequestContext:
    message_tokens: int
    schema_tokens: int
    predicted_tokens: int
    budget_tokens: int
    calibration: float
    configured_window: int
    budget_window: int
    provider_window: int | None
    output_tokens: int

    @classmethod
    def measure(cls, harness, state, messages, *, output_tokens):
        from . import summarizer as sm

        estimate = sm.estimate_tokens(messages)
        schemas = sm._state_int(state, sm._TOOL_SCHEMA_TOKENS_KEY)
        samples = (state.hook_state.get("_ctx_prompt_samples") or []) if state else []
        point = sorted(samples)[len(samples) // 2] if samples else 1.0
        return cls(
            estimate,
            schemas,
            int((estimate + schemas) * point),
            sm.effective_prompt_tokens(estimate, state),
            sm.effective_calibration(state),
            int(harness.max_context_tokens),
            sm.effective_context_window(harness, state),
            sm._state_int(state, sm.PROVIDER_HARD_CAP_KEY) or None,
            int(output_tokens),
        )

    @property
    def local_tokens(self):
        return self.message_tokens + self.schema_tokens

    def report(self):
        return {
            "predicted_tokens": self.predicted_tokens,
            "budget_tokens": self.budget_tokens,
            "budget_window": self.budget_window,
            "provider_window": self.provider_window,
            "reserved_output_tokens": self.output_tokens,
            "measurement": "request",
        }


@dataclass(frozen=True)
class CompactionResult:
    strategy: str
    status: str
    tokens_before: int
    tokens_after: int
    target_tokens: int
    reason: str | None = None
    history_ref: str | None = None

    @property
    def target_met(self):
        return self.tokens_after <= self.target_tokens


def target_tokens(harness, state, output_tokens=None):
    """Raw message allowance after schemas, response reserve and safety margin."""
    from . import summarizer as sm

    window = sm.hard_cap_window(harness, state)
    output = int(
        output_tokens
        if output_tokens is not None
        else getattr(harness, "max_output_tokens", 0) or 0
    )
    threshold = float(harness.summarizer.trigger_threshold or 0.7)
    # A decode-degradation observation measures the prompt alone. Reserving
    # output against it a second time can turn a small transient failure into
    # an impossible (zero-token) context budget.
    desired = min(int(sm.effective_context_window(harness, state) * threshold),
                  window - output - sm.presend_margin(window))
    return max(
        0,
        int(desired / sm.effective_calibration(state))
        - sm._state_int(state, sm._TOOL_SCHEMA_TOKENS_KEY),
    )


def projection_signature(messages, target):
    from .conversation_store import msg_to_jsonable

    raw = json.dumps(
        [target, [msg_to_jsonable(m) for m in messages]], sort_keys=True, ensure_ascii=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _current_snapshots(messages):
    """Keep named state and recognize pre-slot ledgers without editing history."""
    from . import summarizer as sm

    snapshots = {}
    for message in messages:
        if not is_framework_notice(message):
            continue
        if message.context_slot and message.context_slot != "context_history":
            snapshots[message.context_slot] = message
        elif (sm._is_compression_notice(message)
              and sm._COMPRESSION_SUMMARY_HEADER in (message.content or "")):
            snapshots[sm.CONTINUATION_SLOT] = replace(message, context_slot=sm.CONTINUATION_SLOT)
    return snapshots


def _archive_projection(ctx, reference=None, target=None):
    """Use the largest recent working set that meets the budget.

    Recency and verbatim preferences are useful defaults, not unlimited memory
    exemptions. Caller authority, current named state and unconfirmed effects
    are the hard retention rules. Confirmed exchanges remain exact in history.
    """
    from . import summarizer as sm
    from .conversation_store import unresolved_tool_calls

    messages = ctx.messages
    assistants = [i for i, m in enumerate(messages) if m.role == "assistant"]
    keep = min(max(0, ctx.harness.summarizer.keep_last_n_turns), len(assistants))
    target = target_tokens(ctx.harness, ctx.state) if target is None else target
    primary_groups = set()
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        end = index + 1
        while end < len(messages) and messages[end].role == "tool":
            end += 1
        if any(is_primary_input(m) for m in messages[index:end]):
            primary_groups.update(id(m) for m in messages[index:end])
    pending = unresolved_tool_calls(messages)
    pending_start = min(
        (
            i
            for i, m in enumerate(messages)
            if any(call.get("id") in pending for call in m.tool_calls or [])
        ),
        default=len(messages),
    )
    reference = reference or sm._context_view_of(ctx.state).archive_history(messages, turn=ctx.turn)
    if not reference:
        return messages, None, "history_storage_unavailable"
    opening = [messages[0]] if messages and messages[0].role == "system" else []
    snapshots = _current_snapshots(messages)
    index_ref = sm._context_view_of(ctx.state).history_index_ref()
    notice = sm.build_compression_notice(
        ctx.turn,
        "archive 策略：已确认的旧消息组已保存，当前状态与未确认动作原样保留。",
        body=(
            f'完整历史只读取回：read_tool_result(result_ref="{reference}")，'
            "用 offset/limit 分页，不会重新执行工具。需要最近记录时，可根据第一页的"
            " total_chars 从末尾分页读取；需要旧结论或原始参数时先取回，不要猜测或重跑动作。"
            + (f'\n全部历史归档目录：read_tool_result(result_ref="{index_ref}")。'
               "目录按 turn 列出每次归档的直接引用，早期原文无需逐层打开快照。"
               if index_ref else "")
        ),
    )
    best = messages
    previous_tail = None
    for count in range(keep, -1, -1):
        tail = min(assistants[-count] if count else len(messages), pending_start)
        if tail == previous_tail:
            continue
        previous_tail = tail
        callers = [m for m in messages[:tail] if is_primary_input(m) or id(m) in primary_groups]
        recent_ids = {id(m) for m in messages[tail:]}
        derived = [m for m in snapshots.values() if id(m) not in recent_ids]
        retained = {id(m) for m in [*opening, *callers, *derived, *messages[tail:]]}
        if all(id(m) in retained for m in messages):
            continue
        candidate = [*opening, *callers, notice, *derived, *messages[tail:]]
        if sm.estimate_tokens(candidate) < sm.estimate_tokens(best):
            best = candidate
        if sm.estimate_tokens(candidate) <= target:
            return candidate, reference, None
    return best, reference, None if best is not messages else "only_protected_messages"


def research_state_snapshot(harness, state):
    """Bind the existing scientific owner, using a reference for large states."""
    from . import research_state_reader, summarizer as sm

    if not callable(getattr(state, "artifact_versions", None)):
        return None
    try:
        data = research_state_reader.read_for_state(state)
    except Exception as exc:
        sm.log.warning("research_state context read failed: %s", type(exc).__name__)
        state.append_transcript("context_research_state_unavailable", error_type=type(exc).__name__)
        return None
    if not data.get("exists"):
        return None
    text = json.dumps(data, ensure_ascii=False)
    allowance = max(256, min(4096, target_tokens(harness, state) // 4))
    notice = framework_notice(text)
    if sm.estimate_tokens([notice]) > allowance:
        # No partial JSON or rounded numeric substitute. The original owner
        # remains readable; version and digest make this a verifiable pointer.
        notice = framework_notice(json.dumps({
            "mode":"reference", "artifact_id":research_state_reader.HEAD_STEM,
            "version":data["version"], "content_sha256":hashlib.sha256(data["content"].encode()).hexdigest(),
            "read":f'read_artifact(artifact_id="{research_state_reader.HEAD_STEM}"); '
                   "check the returned version; a newer owner revision supersedes this pointer.",
        }, ensure_ascii=False))
    notice.context_slot = "research_state"
    return notice


def _refresh_derived_state(state, messages, turn, *, harness=None):
    """Rebuild current derived snapshots before the final capacity check."""
    from .loop_hooks_builtin import ORIENTATION_PREFIX, build_orientation_snapshot
    from .tool_call_cache import findings_digest

    fresh = build_orientation_snapshot(state)
    result = [
        m
        for i, m in enumerate(messages)
        if not (
            is_framework_notice(m) and m.context_slot == "tool_findings"
            or fresh
            and (is_framework_notice(m) or (m.role == "system" and i > 0))
            and framework_notice_body(m).startswith(ORIENTATION_PREFIX)
        )
    ]
    if fresh:
        snapshot = framework_notice(fresh + "\n\n（压缩后重扫，以本快照为准。）")
        snapshot.context_slot = "orientation"
        result.append(snapshot)
        state.append_transcript("orientation_refreshed_after_compress", turn=turn)
    digest = findings_digest(state)
    if digest:
        snapshot = framework_notice(digest)
        snapshot.context_slot = "tool_findings"
        result.append(snapshot)
        state.append_transcript(
            "compression_findings_digest_injected", turn=turn, digest_chars=len(digest)
        )
    if harness is not None:
        research = research_state_snapshot(harness, state)
        if research is not None:
            result = [m for m in result if not (is_framework_notice(m) and m.context_slot == "research_state")]
            result.append(research)
    return result


async def compact(harness, state, messages, llm, turn, estimated_tokens, *, output_tokens=None):
    from . import summarizer as sm

    before = sm.estimate_tokens(messages)
    target = target_tokens(harness, state, output_tokens)
    strategy, reason = "clear_tool_results", None
    reference = sm._context_view_of(state).archive_history(messages, turn=turn)
    if not reference:
        result = CompactionResult(
            "unchanged", "irreducible", before, before, target, "history_storage_unavailable"
        )
        if state:
            state.hook_state["_context_compaction"] = {**asdict(result), "turn": turn}
            state.append_transcript("context_compaction", turn=turn, **asdict(result))
            state.hook_state[sm._LAST_COMPRESS_EST_KEY] = before
            state.hook_state.pop("_context_projection_signature", None)
        return messages
    # Eviction and custom strategies prepare a candidate on private state.
    # The live index is reconciled only by render_context_change at commit.
    planned_view = sm._context_view_of(state).fork()
    ctx = sm.SummarizerContext(harness, state, deepcopy(messages), before, llm, turn,
                               context_view=planned_view)
    cleared = await sm._strategy_clear_tool_results(ctx)
    candidate = cleared
    # Respect an explicit invocation (turn-count/custom callers) even below the
    # normal threshold unless lossless eviction itself solved the pressure.
    if sm.estimate_tokens(cleared) > target or sm.estimate_tokens(cleared) == before:
        fn = sm.get_summarizer_for(harness)
        strategy = harness.summarizer.strategy
        ctx = sm.SummarizerContext(harness, state, deepcopy(cleared),
                                   sm.estimate_tokens(cleared), llm, turn,
                                   context_view=planned_view)
        if fn:
            try:
                candidate = await fn(ctx)
            except Exception as exc:
                candidate, reason = cleared, type(exc).__name__
        else:
            reason = "strategy_unavailable"
        if not isinstance(candidate, list) or not candidate:
            candidate, reason = cleared, "invalid_projection"
        reason = ctx.summary_failure or reason
    # Current named task state remains exact even when a successful model
    # summary omitted a snapshot positioned in the middle of the history.
    snapshots = _current_snapshots(messages)
    # A newly accepted ledger supersedes the previous one; other named state
    # still comes verbatim from its existing authoritative owner.
    new_ledger = _current_snapshots(candidate).get(sm.CONTINUATION_SLOT)
    if new_ledger is not None:
        snapshots[sm.CONTINUATION_SLOT] = new_ledger
    candidate = [
        m for m in candidate if not (
            is_framework_notice(m) and (m.context_slot in snapshots
                                       or m.context_slot == "context_history"))
    ]
    candidate.extend(snapshots.values())
    # A strategy is not allowed to paraphrase away caller authority or remove
    # an unconfirmed effect. Validate even custom/node-provided strategies.
    from .conversation_store import unresolved_tool_calls

    pending = unresolved_tool_calls(messages)
    protected = [
        m
        for m in messages
        if is_primary_input(m) or any(call.get("id") in pending for call in m.tool_calls or [])
    ]
    if any(m not in candidate for m in protected):
        candidate, reason = cleared, "protected_message_removed"
    candidate = normalize_request_messages(candidate)
    try:
        candidate = _refresh_derived_state(state, candidate, turn, harness=harness)
    except Exception:
        pass  # A derived filesystem snapshot cannot invalidate the projection.
    if strategy != "archive":
        index_ref = sm._context_view_of(state).history_index_ref()
        history_notice = framework_notice(
            f'本次整理前的完整历史：read_tool_result(result_ref="{reference}")；'
            "只读、可分页，不会重跑工具。\n"
            + (f'全部历史归档目录：read_tool_result(result_ref="{index_ref}")；'
               "可直接定位早期归档，无需逐层打开快照。" if index_ref else "")
        )
        history_notice.context_slot = "context_history"
        candidate = [
            *candidate,
            history_notice,
        ]
    if sm.estimate_tokens(candidate) > target:
        ctx = sm.SummarizerContext(
            harness, state, candidate, sm.estimate_tokens(candidate), llm, turn
        )
        projected, reference, archive_error = _archive_projection(ctx, reference, target)
        if sm.estimate_tokens(projected) < sm.estimate_tokens(candidate):
            candidate, strategy = projected, "archive"
        else:
            reason = archive_error or reason or "protected_context_exceeds_budget"
    after = sm.estimate_tokens(candidate)
    if after >= before:
        candidate, after, strategy = messages, before, "unchanged"
        reason = reason or "no_reduction"
    status = "ready" if after <= target else "irreducible"
    if status == "irreducible":
        reason = reason or "protected_context_exceeds_budget"
    result = CompactionResult(strategy, status, before, after, target, reason, reference)
    if state:
        data = {**asdict(result), "target_met": result.target_met}
        state.hook_state["_context_compaction"] = {**data, "turn": turn}
        state.hook_state[sm._LAST_COMPRESS_EST_KEY] = after
        state.hook_state["_context_projection_signature"] = projection_signature(candidate, target)
        sm.note_compress_result(state, turn=turn, tokens_before=before, tokens_after=after)
        state.append_transcript("context_compaction", turn=turn, **data)
        state.append_transcript(
            "summarizer_compress",
            turn=turn,
            configured_strategy=harness.summarizer.strategy,
            messages_before=len(messages),
            messages_after=len(candidate),
            compression_ratio=round(after / before, 4) if before else None,
            **data,
        )
    return candidate
