"""Operation approval, owned by the tool dispatcher rather than the model.

Each original tool call owns one locked, atomically replaced record under its
existing run directory. Core owns the mutable phase and immutable invocation;
consumers are the dispatcher, checkpoint recovery and UI projection. Records
share run retention and may only be removed with that run. Legacy pause import
is read-only (0.5.15/0.5.16); retire it when these checkpoints leave the supported
upgrade window. Old highrisk_passes files are inert and never imported.
Messages and UI events are projections of that record.
An approval never authorizes a later model call, another run or another target.
Only the dispatcher can claim the frozen invocation; an interrupted claim needs
the tool owner's receipt recovery, never an automatic second submission.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from core.decision_offer import PAUSE_OFFER_KEY, Answer, Choice, Offer, resolve_answer
from shared.lib import filelock
from shared.lib.filesystem import io_path

TTL_SECONDS = 3600
PERMISSION_TYPES = frozenset({"highrisk_confirm", "permission", "capability_grant"})
_invocation = ContextVar("approval_invocation", default=None)
_depth = ContextVar("approval_invocation_depth", default=0)
_claim = ContextVar("approval_execution_claim", default=None)


class ApprovalError(RuntimeError):
    code = "approval_conflict"


class ApprovalExpired(ApprovalError):
    code = "approval_expired"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _operation(state, tool, arguments, call_id):
    from core.execution_target import current

    return {
        "run_id": state.run_id,
        "session_id": getattr(state, "session_id", None),
        "project_id": getattr(state, "project_id", None),
        "tenant_id": getattr(state, "tenant_id", None),
        "principal": getattr(state, "hook_state", {}).get("approval_principal"),
        "platform_attempt_id": getattr(state, "platform_attempt_id", None),
        "node_type": state.node_type,
        "tool_call_id": call_id,
        "tool": tool,
        "arguments": json.loads(_json(arguments)),
        "execution_target": current().spec,
        "workdir": str(getattr(state, "project_workdir", None) or state.root),
    }


def _same_operation(left, right):
    return {k: v for k, v in left.items() if k != "dispatch_anchor"} == {
        k: v for k, v in right.items() if k != "dispatch_anchor"
    }


@contextmanager
def invocation(state, tool, arguments, *, call_id=None):
    outer = _invocation.get()
    if outer and outer["run_id"] == getattr(state, "run_id", None):
        nested = _depth.set(_depth.get() + 1)
        try:
            yield outer
        finally:
            _depth.reset(nested)
        return
    call_id = call_id or getattr(state, "hook_state", {}).get(
        "_current_tool_call_id_being_executed"
    )
    # Internal calls have no model call ID. They still get an operation identity,
    # carried by the resulting pause instead of a reusable command credential.
    # Pure helpers and schema refusals accept a smaller state protocol. They
    # need no approval record. A permission result still requires the complete
    # persistent run identity, checked at capture before any answer is offered.
    operation = None
    if all(getattr(state, key, None) for key in ("run_id", "root", "node_type")):
        operation = _operation(state, tool, arguments, call_id or f"internal-{uuid4().hex}")
        claim = _claim.get()
        operation["dispatch_anchor"] = (
            claim["operation"]["dispatch_anchor"]
            if claim and _same_operation(operation, claim["operation"])
            else getattr(state, "_tool_dispatches", {}).get(call_id) or uuid4().hex
        )
    token = _invocation.set(operation)
    depth = _depth.set(1)
    try:
        yield operation
    finally:
        _depth.reset(depth)
        _invocation.reset(token)


def _path(state, operation_id):
    if len(operation_id) != 64 or any(c not in "0123456789abcdef" for c in operation_id):
        raise ApprovalError("Invalid approval operation identity")
    return io_path(Path(state.root) / "approvals" / f"{operation_id}.json")


@contextmanager
def _record(state, operation_id):
    path = _path(state, operation_id)
    with filelock.exclusive(path.with_suffix(".lock")):
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if value is not None and (
            value.get("operation_id") != operation_id
            or _digest(value.get("operation")) != operation_id
        ):
            raise ApprovalError("Frozen approval record could not be restored exactly")
        if value is not None and value.get("schema_version") != 1:
            raise ApprovalError("Unsupported approval record version")
        yield value, path


def _write(path, value):
    from core.conversation_store import write_snapshot

    write_snapshot(path, value)


def _is_permission(result):
    return (
        isinstance(result, dict)
        and result.get("status") == "pause"
        and (result.get("pause_event", {}).get("metadata") or {}).get("type") in PERMISSION_TYPES
        and not result.get("child_run_id")
    )


def _challenge(payload):
    meta = payload.get("metadata") or {}
    key = meta.get("authorization_key")
    if meta.get("type") == "capability_grant":
        key = _json({k: meta.get(k) for k in ("capability", "host", "gpus")})
    if not isinstance(key, str) or not key:
        raise ApprovalError("Permission request has no operation authorization boundary")
    return _digest(key)


def _offer(record):
    source = record["question"]
    capability = (source.get("metadata") or {}).get("type") == "capability_grant"
    options = source.get("options") or ["批准执行", "拒绝"]
    operation = record["operation"]
    from core.secrets import redact

    context = (
        source.get("context", "")
        + "\n\n"
        + f"执行工具：{operation['tool']}\n执行环境：{operation['execution_target']}\n"
        + f"默认工作目录：{operation['workdir']}\n完整调用参数：\n"
        + _json(redact(operation["arguments"]))
    )
    offer = Offer(
        decision_id=f"approval:{record['operation_id']}:{record['revision']}",
        kind=(source.get("metadata") or {})["type"],
        question=source["question"],
        context=context,
        choices=(Choice(id="approve", label=options[0]), Choice(id="deny", label=options[1])),
        recommended_id="deny" if capability else None,
    ).to_pause_payload()
    return {
        **source,
        **offer,
        PAUSE_OFFER_KEY: offer,
        "pending_tool_call_id": record["operation"]["tool_call_id"],
        "asking_run_id": record["operation"]["run_id"],
        "asking_node_type": record["operation"]["node_type"],
        "metadata": {**source.get("metadata", {}), "approval_operation_id": record["operation_id"]},
    }


def _paused(record):
    return {"status": "pause", "executed": False, "pause_event": _offer(record)}


def _prepare(state, operation, result):
    from core.secrets import redact

    if redact(operation) != operation:
        raise ApprovalError(
            "Secret-bearing arguments cannot be durably approved; use a managed secret reference"
        )
    operation_id = _digest(operation)
    question = result["pause_event"]
    challenge = _challenge(question)
    with _record(state, operation_id) as (record, path):
        if record is None:
            record = {
                "schema_version": 1,
                "operation_id": operation_id,
                "operation": operation,
                "revision": 0,
                "granted": [],
                "created_at": time.time(),
            }
        if record["operation"] != operation:
            raise ApprovalError("Frozen operation identity changed")
        if record.get("phase") in {"completed", "denied"}:
            return record
        claim = _claim.get()
        if record.get("phase") in {"approved", "executing"} and not (
            claim
            and claim["operation_id"] == operation_id
            and record.get("claim") == claim.get("claim")
        ):
            return record  # a duplicate dispatch cannot replace a live decision
        if challenge in record["granted"]:
            raise ApprovalError("Tool requested an already approved boundary again")
        if (
            record.get("challenge") != challenge
            or record.get("phase") != "pending"
            or time.time() >= record.get("expires_at", 0)
        ):
            record.update(
                revision=record["revision"] + 1,
                phase="pending",
                challenge=challenge,
                question=question,
                expires_at=time.time() + TTL_SECONDS,
            )
            record.pop("claim", None)
            _write(path, record)
        return record


def authorized(state, text):
    """Only the currently claimed, exact invocation may pass its approved gate."""
    claim = _claim.get()
    operation = _invocation.get()
    if not claim or not operation or operation != claim["operation"]:
        return False
    if operation["run_id"] != state.run_id:
        return False
    with _record(state, claim["operation_id"]) as (record, _):
        return bool(
            record
            and record.get("phase") == "executing"
            and record.get("claim") == claim["claim"]
            and _digest(text) in record["granted"]
        )


async def capture(state, result):
    """Bind a tool's permission result before it can reach messages or the UI."""
    operation = _invocation.get()
    if _depth.get() != 1:
        return result
    if operation is None:
        if _is_permission(result):
            raise ApprovalError("A permission requires an addressable persistent run")
        return result
    claim = _claim.get()
    own_claim = claim and claim["operation"] == operation
    if _is_permission(result):
        record = _prepare(state, operation, result)
        if record.get("phase") in {"completed", "denied"}:
            return record["result"]
        if record.get("phase") in {"approved", "executing"}:
            return await run_approved(state, record["operation_id"])
        if not own_claim:
            from shared.lib.dangerous_commands import preauthorized

            category = (record["question"].get("metadata") or {}).get("category", "")
            if (
                category
                and preauthorized(category)
                and record["question"]["metadata"].get("type") != "capability_grant"
            ):
                decide(state, _offer(record), "approve", answered_by="policy")
                return await run_approved(state, record["operation_id"])
        return _paused(record)
    if own_claim:
        with _record(state, claim["operation_id"]) as (record, path):
            if record.get("claim") != claim["claim"] or record.get("phase") != "executing":
                raise ApprovalError("Approval execution ownership changed")
            record.update(phase="completed", result=result, completed_at=time.time())
            _write(path, record)
    return result


def decide(state, payload, choice, *, answered_by="person"):
    operation_id = (payload.get("metadata") or {}).get("approval_operation_id")
    if not operation_id:
        raise ApprovalError("Permission has no frozen operation")
    with _record(state, operation_id) as (record, path):
        if record is None:
            raise ApprovalError("Original operation record is missing")
        operation = record["operation"]
        if not _same_operation(
            operation,
            _operation(state, operation["tool"], operation["arguments"], operation["tool_call_id"]),
        ):
            raise ApprovalError("Approval belongs to a different operation context")
        current = Offer.from_pause_payload(_offer(record))
        supplied = Offer.from_pause_payload(payload)
        if supplied.offer_id != current.offer_id:
            raise ApprovalError("This approval has been superseded")
        if choice not in {"approve", "deny"}:
            raise ApprovalError("An explicit approve or deny choice is required")
        if record.get("decision") == choice and record.get("phase") != "pending":
            return record  # idempotent acknowledgement, never another execution
        if record.get("phase") != "pending":
            raise ApprovalError("This operation already has a different decision")
        if choice == "approve" and time.time() >= record["expires_at"]:
            raise ApprovalExpired(
                "This approval expired; inspect the operation before requesting a new approval"
            )
        record.update(decision=choice, answered_by=answered_by, decided_at=time.time())
        if choice == "deny":
            record.update(
                phase="denied",
                result={
                    "status": "error",
                    "error_code": "approval_denied",
                    "error": "The operation was denied and was not executed.",
                    "executed": False,
                },
            )
        else:
            record["granted"] = list(dict.fromkeys([*record["granted"], record["challenge"]]))
            record["phase"] = "approved"
        _write(path, record)
    state.append_transcript(
        "approval_decided",
        operation_id=operation_id,
        choice=choice,
        answered_by=answered_by,
        offer_id=current.offer_id,
    )
    return record


def choice_for(payload, answer):
    """Strict command adapter. Discussion, conditions and negation never approve."""
    offer = Offer.from_pause_payload(payload)
    if isinstance(answer, dict):
        if answer.get("offer_id") != offer.offer_id:
            raise ApprovalError("An approval must address the current offer_id")
        value = resolve_answer(offer, answer)
    else:
        text = str(answer or "").strip().casefold()
        aliases = {
            "批准": "approve",
            "同意": "approve",
            "approve": "approve",
            "yes": "approve",
            "允许": "approve",
            "allow": "approve",
            "grant": "approve",
            "拒绝": "deny",
            "不同意": "deny",
            "deny": "deny",
            "no": "deny",
            "不允许": "deny",
            "1": "approve",
            "[1]": "approve",
            "2": "deny",
            "[2]": "deny",
        }
        text = aliases.get(text, text)
        if text not in {c.id.casefold() for c in offer.choices} | {
            c.label.casefold() for c in offer.choices
        }:
            raise ApprovalError("Discussion is not an explicit approve or deny choice")
        value = resolve_answer(offer, text)
    if not isinstance(value, Answer) or value.choice_id not in {"approve", "deny"}:
        raise ApprovalError("This message discusses the operation; it is not an explicit approval")
    return value.choice_id


def load(state, payload):
    operation_id = (payload.get("metadata") or {}).get("approval_operation_id")
    if not operation_id:
        return None
    with _record(state, operation_id) as (record, _):
        if record is None:
            raise ApprovalError("Original approval record is missing")
        return record


def apply_capability_decision(state, record):
    """Materialize an accepted capability decision, never an unaccepted answer."""
    meta = record["question"].get("metadata") or {}
    if meta.get("type") != "capability_grant":
        return None
    from core import capability_grants as grants

    approved = record.get("decision") == "approve"
    if approved and meta.get("capability") == grants.GPU_CAPABILITY:
        granted = grants.grant_gpu_from_answer(
            state, int(meta.get("gpus") or 1), "approve", reason=str(meta.get("reason") or "")
        )
        granted = bool(granted and grants.gpu_lease(state) is not None)
    elif approved:
        granted = grants.grant_from_answer(
            state, str(meta.get("host") or ""), "approve", reason=str(meta.get("reason") or "")
        )
        granted = bool(granted and grants.is_granted(state, str(meta.get("host") or "")))
    else:
        granted = False
        if record.get("answered_by") == "auto":
            grants.record_not_granted(state, meta)
    state.append_transcript(
        "capability_grant_answered",
        capability=meta.get("capability"),
        host=meta.get("host"),
        granted=granted,
        answer_unreadable=False,
        answered_by=record.get("answered_by", "person"),
        operation_id=record["operation_id"],
    )
    if approved and not granted:
        return {
            "status": "error",
            "error_code": "capability_not_enforceable",
            "executed": False,
            "error": (
                "The approved capability could not be obtained; the operation was not executed."
            ),
        }
    return None


def payload_result(record):
    return record["result"] if record["phase"] in {"completed", "denied"} else _paused(record)


async def run_approved(state, operation_id):
    # The claim receipt survives a process crash; the OS lock tells us whether
    # its executor is still present. Competing answers wait for that same result.
    import asyncio

    lock = _path(state, operation_id).with_suffix(".execution.lock")
    handle = filelock.try_hold(lock)
    while handle is None:
        from core.job_wait import cancellable

        await cancellable(state, asyncio.sleep(0.05))
        handle = filelock.try_hold(lock)
    try:
        while True:
            result = await _run_approved(state, operation_id)
            if not _is_permission(result):
                return result
            meta = result["pause_event"].get("metadata") or {}
            from shared.lib.dangerous_commands import preauthorized

            category = meta.get("category", "")
            if (
                meta.get("type") == "capability_grant"
                or not category
                or not preauthorized(category)
            ):
                return result
            decide(state, result["pause_event"], "approve", answered_by="policy")
    finally:
        filelock.release(handle)
        handle.close()


async def _run_approved(state, operation_id):
    from core import tool_registry

    with _record(state, operation_id) as (record, path):
        if record is None:
            raise ApprovalError("Original operation is missing")
        if record["phase"] in {"completed", "denied"}:
            if record["phase"] == "denied":
                apply_capability_decision(state, record)
            return record["result"]
        if record["phase"] == "pending":
            return _paused(record)
        if record["phase"] == "executing":
            interrupted = record
        else:
            interrupted = None
        if record["phase"] not in {"approved", "executing"}:
            raise ApprovalError("Operation is not approved for execution")
        operation = record["operation"]
        if not _same_operation(
            operation,
            _operation(state, operation["tool"], operation["arguments"], operation["tool_call_id"]),
        ):
            raise ApprovalError("Execution target or operation identity changed after approval")
        if not interrupted and time.time() >= record["expires_at"]:
            raise ApprovalExpired("Approval expired before execution was claimed")
        if not interrupted:
            record.update(phase="executing", claim=uuid4().hex, claimed_at=time.time())
            _write(path, record)
    if interrupted:
        # Owner recovery may return an existing receipt. Merely declaring a
        # call replayable is not an execution receipt, and does not reopen a claim.
        import inspect

        owner = tool_registry.get_tool(operation["tool"])
        recovery = (
            owner.interrupted_recovery(
                operation["arguments"], state=state, tool_call_id=operation["tool_call_id"]
            )
            if owner and callable(owner.interrupted_recovery)
            else None
        )
        if inspect.isawaitable(recovery):
            recovery = await recovery
        if isinstance(recovery, dict) and "result" in recovery:
            result = recovery["result"]
            if _is_permission(result):
                token = _claim.set(interrupted)
                try:
                    return _paused(_prepare(state, operation, result))
                finally:
                    _claim.reset(token)
            with _record(state, operation_id) as (saved, path):
                if saved["phase"] == "executing" and saved["claim"] == interrupted["claim"]:
                    saved.update(phase="completed", result=result, completed_at=time.time())
                    _write(path, saved)
                elif saved["phase"] == "completed":
                    result = saved["result"]
                else:
                    raise ApprovalError("Execution owner changed during reconciliation")
            return result
        from core.conversation_store import UnconfirmedToolOutcome

        raise UnconfirmedToolOutcome(
            "Approved operation has no confirmed receipt; reconcile its original execution"
        )
    token = _claim.set(record)
    frame = _invocation.set(None)
    previous = state.hook_state.get("_current_tool_call_id_being_executed")
    state.hook_state["_current_tool_call_id_being_executed"] = operation["tool_call_id"]
    try:
        unavailable = apply_capability_decision(state, record)
        if unavailable:
            with _record(state, operation_id) as (saved, path):
                saved.update(phase="completed", result=unavailable, completed_at=time.time())
                _write(path, saved)
            return unavailable
        return await tool_registry.execute(operation["tool"], state, **operation["arguments"])
    finally:
        state.hook_state["_current_tool_call_id_being_executed"] = previous
        _invocation.reset(frame)
        _claim.reset(token)


async def resume(ctx, answer):
    payload = ctx.pause_event.to_dict()
    operation_id = (payload.get("metadata") or {}).get("approval_operation_id")
    if not operation_id:
        # Read-only legacy migration: import the exact pending call, never old
        # cached passes. A fresh explicit answer still addresses this operation.
        call = next(
            (
                c
                for m in ctx.messages
                for c in m.tool_calls or []
                if c.get("id") == ctx.pending_tool_call_id
            ),
            None,
        )
        if call is None:
            raise ApprovalError("Original permission call is missing")
        function = call["function"]
        arguments = function.get("arguments") or "{}"
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        meta = payload.setdefault("metadata", {})
        legacy = ctx.state.hook_state.pop("_highrisk_pending_ask", {})
        if not meta.get("authorization_key"):
            meta["authorization_key"] = legacy.get("text")
        operation = _operation(ctx.state, function["name"], arguments, call["id"])
        operation["dispatch_anchor"] = uuid4().hex
        record = _prepare(ctx.state, operation, {"status": "pause", "pause_event": payload})
        ctx.state.append_transcript(
            "approval_legacy_imported", operation_id=record["operation_id"], tool_call_id=call["id"]
        )
        # Resolve the answer against the question actually shown before migration.
        selected = choice_for(payload, answer)
        payload = _offer(record)
        from core.pause import PauseEvent

        ctx.pause_event = PauseEvent.from_payload(
            payload, pending_tool_call_id=ctx.pending_tool_call_id
        )
    else:
        selected = choice_for(payload, answer)
    try:
        from core.pause_driver import AutomaticAnswer

        decide(
            ctx.state,
            payload,
            selected,
            answered_by="auto" if isinstance(answer, AutomaticAnswer) else "person",
        )
        from core.agent_loop import end_pause
        from core.pause_driver import _settle_relays

        _settle_relays(ctx)
        end_pause(ctx, response_preview=f"Operation {selected}", answer_accepted=True)
        return await run_approved(ctx.state, payload["metadata"]["approval_operation_id"])
    except ApprovalExpired:
        return renew_expired(ctx.state, payload["metadata"]["approval_operation_id"])


def renew_expired(state, operation_id):
    with _record(state, operation_id) as (record, path):
        if record["phase"] in {"pending", "approved"} and time.time() >= record["expires_at"]:
            record.update(
                phase="pending",
                revision=record["revision"] + 1,
                granted=[],
                expires_at=time.time() + TTL_SECONDS,
            )
            record.pop("decision", None)
            _write(path, record)
        return payload_result(record)


def _find_operation(state, tool_call_id, tool, arguments):
    operation = _operation(state, tool, arguments, tool_call_id)
    candidates = []
    anchor = getattr(state, "_tool_dispatches", {}).get(tool_call_id)
    for path in io_path(Path(state.root) / "approvals").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        if _same_operation(operation, item["operation"]) and (
            not anchor or item["operation"].get("dispatch_anchor") == anchor
        ):
            candidates.append(item["operation_id"])
    if len(candidates) != 1:
        return None
    return candidates[0]


def recover_result(state, tool_call_id, tool, arguments):
    """Return confirmed receipts; unknown outcomes still need their real owner."""
    from core.conversation_store import tool_result_is_unconfirmed

    operation_id = _find_operation(state, tool_call_id, tool, arguments)
    if operation_id is None:
        return None
    with _record(state, operation_id) as (record, _):
        if record["phase"] in {"completed", "denied"}:
            if not tool_result_is_unconfirmed(record["result"]):
                return {"result": record["result"]}
        if record["phase"] == "pending":
            return {"result": _paused(record)}
    return None


def record_recovered_result(state, tool_call_id, tool, arguments, result):
    """An owner may correct an unknown receipt without reopening execution."""
    from core.conversation_store import tool_result_is_unconfirmed

    operation_id = _find_operation(state, tool_call_id, tool, arguments)
    if operation_id is None:
        return
    with _record(state, operation_id) as (record, path):
        if (
            record["phase"] == "completed"
            and tool_result_is_unconfirmed(record["result"])
            and result != record["result"]
        ):
            record.setdefault("superseded_results", []).append(record["result"])
            record.update(result=result, reconciled_at=time.time())
            _write(path, record)
