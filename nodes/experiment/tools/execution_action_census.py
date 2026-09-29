"""Durable three-phase census for Experiment execution actions.

Generic model tool events have no stable call identity.  This module records
one admitted/spawn-observed/terminal chain per execution action.  Route and
prereg facts remain owned by ``execution_route`` and ``run_contract``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

try:
    from . import execution_route as _execution_route
    from . import run_contract as _run_contract
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import execution_route as _execution_route
    from tools import run_contract as _run_contract


CENSUS_SCHEMA_VERSION = 1
ADMITTED_EVENT = "execution_action_admitted"
SPAWN_EVENT = "execution_action_spawn_observed"
TERMINAL_EVENT = "execution_action_terminal"
COMPATIBILITY_RELEASE_MARKER = "p0a_pending_assignment_one_release_review"
COMPATIBILITY_REMOVAL_SIGNALS = (
    "run_node_schema_supports_typed_prereg_assignment",
    "callee_contract_no_longer_advertises_assignment_omission",
)

_EVENTS = (ADMITTED_EVENT, SPAWN_EVENT, TERMINAL_EVENT)
_ACTION_ID = re.compile(r"action-[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROOF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_EXECUTION_TOOLS = frozenset({"safe_run_bash", "safe_execute_python", "submit_job"})
#: P0a v2（裁决 (a)）：三个 support tool 各自带权威收据（写入记录 / acquisition
#: receipt / dispatch receipt）。它们**入账**（kind=receipted）但永远不是执行：
#: 满足不了任何真实执行义务，也不再被兼容 lane 前置拒绝。
_SUPPORT_TOOLS = frozenset({"safe_write_file", "fetch_resource", "dispatch_data_request"})
_CENSUS_TOOLS = _EXECUTION_TOOLS | _SUPPORT_TOOLS
_PENDING_TOOLS = _EXECUTION_TOOLS | _SUPPORT_TOOLS
#: P0a v2 B1：路线层已放行的低风险可逆写入（BASE 语义）。记为 incidental：
#: 可见、入账，既不满足也不毒化真实执行义务。
_INCIDENTAL_DECISIONS = frozenset({"route_action_mismatch", "route_unavailable"})
#: fetch_resource 不走三阶段 begin/settle：它的权威收据是这两个 transcript 事件，
#: reducer 直接把它们投影成 receipted 动作。
_FETCH_RECEIPT_EVENTS = {
    "resource_acquisition_completed": "success",
    "resource_acquisition_failed": "failed",
}
_LOCAL_PROCESS_TOOLS = frozenset({"safe_run_bash", "safe_execute_python"})
_DRY_RUN_TOOLS = frozenset({"submit_job"})


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _error(code: str, reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "reason": reason, **details}


def _read_events(state: Any) -> tuple[list[dict[str, Any]], list[str]]:
    events, warnings = _execution_route._read_transcript_events(state)
    return events, (["transcript_corrupt", *warnings] if warnings else [])


def _binding_identity(binding: Any) -> tuple[Any, ...] | None:
    try:
        ref = binding["route_ref"]
        identity = (
            ref["artifact_id"],
            ref["version"],
            ref["content_hash"],
            binding["route_step_id"],
            binding["step_definition_hash"],
        )
        return identity if binding.get("route_attempt_id") else None
    except (KeyError, TypeError):
        return None


def _normalize_binding(
    decision: dict[str, Any], binding: dict[str, Any] | None, tool: str
) -> tuple[dict[str, Any] | None, str | None]:
    if tool in _DRY_RUN_TOOLS and decision.get("dry_run") is True:
        return (None, "unexpected_route_binding") if binding is not None else (None, None)
    if decision.get("decision") != "matched_ready_step":
        return (None, "unexpected_route_binding") if binding is not None else (None, None)
    if not isinstance(binding, dict):
        return None, "route_binding_required"
    normalized = {
        "route_ref": {
            "artifact_id": binding.get("route_artifact_id"),
            "version": binding.get("route_version"),
            "content_hash": binding.get("route_content_hash"),
        },
        "route_step_id": str(binding.get("route_step_id") or "").strip(),
        "step_definition_hash": str(binding.get("step_definition_hash") or "").strip(),
        "route_attempt_id": str(binding.get("attempt_id") or "").strip(),
    }
    if _binding_identity(normalized) is None:
        return None, "route_binding_invalid"
    if (
        normalized["route_ref"] != decision.get("route_ref")
        or normalized["route_step_id"] != str(decision.get("route_step_id") or "").strip()
        or normalized["step_definition_hash"]
        != str(decision.get("step_definition_hash") or "").strip()
        or str(binding.get("tool") or "").strip() != tool
    ):
        return None, "route_binding_decision_mismatch"
    return normalized, None


def _binding_persisted(state: Any, binding: dict[str, Any], tool: str) -> bool:
    identity = _binding_identity(binding)
    try:
        known = identity in _execution_route._known_route_step_bindings(state)
    except Exception:
        return False
    events, warnings = _execution_route._read_transcript_events(state)
    if not known or warnings:
        return False
    return (
        sum(
            event.get("event") == "route_step_bound"
            and event.get("attempt_id") == binding["route_attempt_id"]
            and (
                event.get("route_artifact_id"),
                event.get("route_version"),
                event.get("route_content_hash"),
                event.get("route_step_id"),
                event.get("step_definition_hash"),
            )
            == identity
            and event.get("tool") == tool
            for event in events
        )
        == 1
    )


def _admission_record(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
    binding: dict[str, Any] | None,
    action_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": CENSUS_SCHEMA_VERSION,
        "run_id": str(state.run_id),
        "action_id": action_id,
        "action_signature": _execution_route._action_signature(action),
        "tool": str(action.get("tool") or "").strip(),
        "resolver_decision": str(decision.get("decision") or "").strip(),
        "resolver_policy": str(decision.get("policy") or "").strip(),
        "decision_read_only": decision.get("read_only") is True,
        "decision_dry_run": decision.get("dry_run") is True,
        "effective_effects": _strings(decision.get("effective_effects")),
        "route_binding": binding,
        # P0a v2 B2：safe_write_file 的写目标。ROC(build) 据此拒绝"声明的产物
        # 就是工具写出来的文件"。执行工具没有这一项。
        "write_target": str(action.get("write_target") or "").strip() or None,
    }


def _persisted_exact(state: Any, event_type: str, record: dict[str, Any]) -> bool:
    """Recover an append that became durable before its writer raised."""
    events, warnings = _read_events(state)
    matches = [
        event
        for event in events
        if event.get("event") == event_type and event.get("action_id") == record.get("action_id")
    ]
    return (
        not warnings
        and len(matches) == 1
        and all(matches[0].get(key) == value for key, value in record.items())
    )


def _valid_admission(event: dict[str, Any], run_id: str) -> bool:
    expects_binding = (
        event.get("resolver_decision") == "matched_ready_step"
        and event.get("decision_dry_run") is not True
    )
    binding = event.get("route_binding")
    return bool(
        event.get("schema_version") == CENSUS_SCHEMA_VERSION
        and event.get("run_id") == run_id
        and isinstance(event.get("action_id"), str)
        and _ACTION_ID.fullmatch(event["action_id"])
        and event.get("tool") in _CENSUS_TOOLS
        and isinstance(event.get("action_signature"), str)
        and _SHA256.fullmatch(event["action_signature"])
        and isinstance(event.get("resolver_decision"), str)
        and event.get("resolver_decision")
        and isinstance(event.get("resolver_policy"), str)
        and all(type(event.get(key)) is bool for key in ("decision_read_only", "decision_dry_run"))
        and event.get("effective_effects") == _strings(event.get("effective_effects"))
        and expects_binding == (_binding_identity(binding) is not None)
    )


def _token(state: Any, action_id: str, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "status": "success",
        "schema_version": CENSUS_SCHEMA_VERSION,
        "run_id": str(state.run_id),
        "action_id": action_id,
        **({"idempotent": True} if idempotent else {}),
    }


def begin_execution_action(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
    *,
    route_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one admission after normal gates and before any spawn."""
    if not isinstance(action, dict) or not isinstance(decision, dict):
        return _error(
            "execution_action_census_input_invalid", "action and decision must be objects"
        )
    tool = str(action.get("tool") or "").strip()
    if tool not in _CENSUS_TOOLS:
        return _error("execution_action_census_tool_unsupported", "unsupported execution tool")
    if str(decision.get("tool") or "").strip() != tool:
        return _error("execution_action_census_input_invalid", "action and decision tools disagree")
    binding, problem = _normalize_binding(decision, route_binding, tool)
    if problem:
        return _error(
            "execution_action_route_binding_invalid",
            "route-backed admission needs its exact persisted attempt binding",
            binding_error=problem,
        )
    if binding is not None and not _binding_persisted(state, binding, tool):
        return _error(
            "execution_action_route_binding_invalid",
            "route attempt binding is not uniquely persisted",
            binding_error="route_binding_not_persisted",
        )
    run_id = str(getattr(state, "run_id", "") or "")
    if not run_id:
        return _error("execution_action_census_state_invalid", "state has no run identity")

    # Only a route attempt supplies a stable upstream id for lost-response
    # recovery.  An exempt begin crash remains open and therefore fails closed.
    if binding is not None:
        events, warnings = _read_events(state)
        if warnings:
            return _error(
                "execution_action_census_history_invalid",
                "transcript is not safe to extend",
                history_errors=warnings,
            )
        candidates = [
            event
            for event in events
            if event.get("event") == ADMITTED_EVENT and event.get("route_binding") == binding
        ]
        if len(candidates) == 1:
            existing = candidates[0]
            expected = _admission_record(
                state, action, decision, binding, existing.get("action_id")
            )
            if _valid_admission(existing, run_id) and all(
                existing.get(key) == value for key, value in expected.items()
            ):
                return _token(state, existing["action_id"], idempotent=True)
        if candidates:
            return _error(
                "execution_action_route_attempt_already_admitted",
                "route attempt already has a conflicting admission",
            )

    action_id = "action-" + uuid.uuid4().hex
    record = _admission_record(state, action, decision, binding, action_id)
    try:
        state.append_transcript(ADMITTED_EVENT, **record)
    except Exception as exc:
        if _persisted_exact(state, ADMITTED_EVENT, record):
            return _token(state, action_id, idempotent=True)
        return _error(
            "execution_action_census_persistence_failed",
            "admission was not durable; payload must not start",
            error_type=type(exc).__name__,
        )
    return _token(state, action_id)


def _resolve_token(state: Any, token: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    valid = bool(
        isinstance(token, dict)
        and token.get("schema_version") == CENSUS_SCHEMA_VERSION
        and token.get("run_id") == str(getattr(state, "run_id", "") or "")
        and isinstance(token.get("action_id"), str)
        and _ACTION_ID.fullmatch(token["action_id"])
    )
    if not valid:
        return [], _error("execution_action_census_token_invalid", "token does not match this run")
    events, warnings = _read_events(state)
    if warnings:
        return events, _error(
            "execution_action_census_history_invalid",
            "transcript is not safe to extend",
            history_errors=warnings,
        )
    admissions = [
        event
        for event in events
        if event.get("event") == ADMITTED_EVENT and event.get("action_id") == token["action_id"]
    ]
    if len(admissions) != 1 or not _valid_admission(admissions[0], str(state.run_id)):
        return events, _error(
            "execution_action_census_token_unresolved",
            "token must resolve to one valid admission",
        )
    return events, None


def _append_followup(
    state: Any, token: dict[str, Any], event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    events, problem = _resolve_token(state, token)
    if problem:
        return problem
    record = {
        "schema_version": CENSUS_SCHEMA_VERSION,
        "run_id": str(state.run_id),
        "action_id": token["action_id"],
        **payload,
    }
    existing = [
        event
        for event in events
        if event.get("event") == event_type and event.get("action_id") == token["action_id"]
    ]
    if existing:
        if len(existing) == 1 and all(existing[0].get(k) == v for k, v in record.items()):
            return {"status": "success", **record, "idempotent": True}
        phase = "spawn_observation" if event_type == SPAWN_EVENT else "terminal"
        return _error(
            f"execution_action_{phase}_conflict",
            f"conflicting {phase} retry rejected without changing history",
            action_id=token["action_id"],
        )
    try:
        state.append_transcript(event_type, **record)
    except Exception as exc:
        if _persisted_exact(state, event_type, record):
            return {"status": "success", **record, "idempotent": True}
        phase = "spawn_observation" if event_type == SPAWN_EVENT else "terminal"
        return _error(
            "execution_action_census_persistence_failed",
            f"{event_type} was not durable",
            error_type=type(exc).__name__,
            action_token=_token(state, token["action_id"]),
            missing_phase=phase,
            phase_facts=dict(payload),
            payload_must_not_rerun=True,
            do_not_retry_payload=True,
            next_action={
                "owner": "experiment_runtime",
                "action": "retry_missing_census_phase_only",
                "phase": phase,
                "model_callable": False,
            },
            model_next_action={
                "action": "report_blocker_and_end_current_run",
                "reason": "runtime reconciliation is not a model tool",
            },
        )
    return {"status": "success", **record}


def observe_execution_action_spawn(
    state: Any,
    token: dict[str, Any],
    *,
    payload_spawned: bool | None,
    job_submitted: bool | None,
    proof_source: str,
) -> dict[str, Any]:
    """Record the tool-owned spawn/submission observation exactly once."""
    valid = (
        (type(payload_spawned) is bool or payload_spawned is None)
        and (type(job_submitted) is bool or job_submitted is None)
        and isinstance(proof_source, str)
        and _PROOF.fullmatch(proof_source)
    )
    if not valid:
        return _error("execution_action_spawn_observation_invalid", "invalid spawn facts")
    return _append_followup(
        state,
        token,
        SPAWN_EVENT,
        {
            "payload_spawned": payload_spawned,
            "job_submitted": job_submitted,
            "proof_source": proof_source,
        },
    )


def _terminal(result: Any, error: BaseException | None) -> dict[str, Any]:
    if error is not None:
        return {
            "terminal_status": "cancelled" if type(error).__name__ == "CancelledError" else "error",
            "result_status": None,
            "error_type": type(error).__name__,
        }
    raw = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else ""
    if raw in {"success", "ok", "completed"}:
        status = "success"
    elif raw in {"cancelled", "canceled"}:
        status = "cancelled"
    elif raw in {"error", "failed", "failure", "blocked", "rejected", "timeout"}:
        status = "error"
    else:
        status = "unknown"
    return {"terminal_status": status, "result_status": raw[:64] or None, "error_type": None}


def defer_execution_action_terminal(
    token: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Describe a terminal phase that must wait for its missing spawn phase.

    This function records no event.  Tool owners retain these canonical facts
    beside the physical outcome so a runtime owner can append spawn and then
    terminal in causal order.  The model must never replay the payload.
    """
    if result is not None and error is not None:
        return _error(
            "execution_action_terminal_input_invalid",
            "provide result or error, not both",
        )
    return {
        "status": "deferred",
        "action_token": dict(token),
        "missing_phase": "terminal",
        "phase_facts": _terminal(result, error),
        "payload_must_not_rerun": True,
        "do_not_retry_payload": True,
        "next_action": {
            "owner": "experiment_runtime",
            "action": "retry_missing_census_phase_only",
            "phase": "terminal",
            "model_callable": False,
            "after_phase": "spawn_observation",
        },
        "model_next_action": {
            "action": "report_blocker_and_end_current_run",
            "reason": "runtime reconciliation is not a model tool",
        },
    }


def finish_execution_action(
    state: Any,
    token: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Record a terminal projection without copying tool output."""
    if result is not None and error is not None:
        return _error(
            "execution_action_terminal_input_invalid", "provide result or error, not both"
        )
    return _append_followup(state, token, TERMINAL_EVENT, _terminal(result, error))


def settle_execution_action(
    state: Any,
    token: dict[str, Any],
    *,
    payload_spawned: bool | None,
    job_submitted: bool | None,
    proof_source: str,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Idempotently settle one action in causal spawn→terminal order.

    Tool wiring uses this facade instead of independently appending follow-up
    phases.  If the spawn fact is not durable, terminal facts are returned as
    a deferred recovery capsule and are never written out of order.
    """
    spawn = observe_execution_action_spawn(
        state,
        token,
        payload_spawned=payload_spawned,
        job_submitted=job_submitted,
        proof_source=proof_source,
    )
    terminal = (
        defer_execution_action_terminal(token, result=result, error=error)
        if spawn.get("status") != "success"
        else finish_execution_action(
            state, token, result=result, error=error,
        )
    )
    return {
        "passed": (
            spawn.get("status") == "success"
            and terminal.get("status") == "success"
        ),
        "spawn": spawn,
        "terminal": terminal,
    }


def _valid_followup(event: dict[str, Any], run_id: str, *, spawn: bool) -> bool:
    common = bool(
        event.get("schema_version") == CENSUS_SCHEMA_VERSION
        and event.get("run_id") == run_id
        and isinstance(event.get("action_id"), str)
        and _ACTION_ID.fullmatch(event["action_id"])
    )
    if spawn:
        return common and bool(
            (type(event.get("payload_spawned")) is bool or event.get("payload_spawned") is None)
            and (type(event.get("job_submitted")) is bool or event.get("job_submitted") is None)
            and isinstance(event.get("proof_source"), str)
            and _PROOF.fullmatch(event["proof_source"])
        )
    if not common:
        return False
    terminal_status = event.get("terminal_status")
    result_status = event.get("result_status")
    error_type = event.get("error_type")
    valid_result = result_status is None or (
        isinstance(result_status, str) and 0 < len(result_status) <= 64
    )
    valid_error = error_type is None or (
        isinstance(error_type, str) and 0 < len(error_type) <= 128
    )
    if not (valid_result and valid_error):
        return False
    success = {"success", "ok", "completed"}
    cancelled = {"cancelled", "canceled"}
    failed = {"error", "failed", "failure", "blocked", "rejected", "timeout"}
    if terminal_status == "success":
        return result_status in success and error_type is None
    if terminal_status == "cancelled":
        return bool(
            (result_status in cancelled and error_type is None)
            or (result_status is None and error_type is not None)
        )
    if terminal_status == "error":
        return bool(
            (result_status in failed and error_type is None)
            or (result_status is None and error_type is not None)
        )
    if terminal_status == "unknown":
        return error_type is None and (
            result_status is None
            or result_status not in success | cancelled | failed
        )
    return False


def _kind(admission: dict[str, Any], spawn: dict[str, Any]) -> tuple[str, str | None]:
    effects = set(admission["effective_effects"])
    if "scientific_execution" in effects:
        return "ineligible", "scientific_execution_observed"
    if (
        admission["tool"] in _EXECUTION_TOOLS
        and admission["resolver_decision"] == "route_not_required"
        and admission["resolver_policy"] == "read_only"
        and admission["decision_read_only"]
        and not effects
    ):
        return "read_only", None
    if admission["tool"] in _DRY_RUN_TOOLS and admission["decision_dry_run"]:
        if spawn["payload_spawned"] is False and spawn["job_submitted"] is False:
            return "dry_run", None
        return "ineligible", "dry_run_spawn_not_proven_absent"
    if admission["resolver_decision"] == "matched_ready_step":
        return "route_backed", None
    if admission["tool"] in _SUPPORT_TOOLS:
        # P0a v2（裁决 (a)）：support tool 带自己的权威收据，入账但不是执行。
        return "receipted", None
    if (
        admission["tool"] in _LOCAL_PROCESS_TOOLS
        and admission["resolver_policy"] == "low_risk_effectful"
        and admission["resolver_decision"] in _INCIDENTAL_DECISIONS
    ):
        # P0a v2 B1：路线层已放行的低风险可逆写入（例如 make 之后顺手一条 cp）。
        # 复审在 HEAD 上实测：记成 ineligible 会把已满足的构建义务毒成
        # operation_real_execution_required 且事后无出口，而 BASE 同序列是
        # success。它可见、入账，但既不满足也不毒化义务。
        return "incidental", None
    return "ineligible", "unsupported_effectful_action"


def reduce_execution_action_census(state: Any) -> dict[str, Any]:
    """Reduce complete three-phase chains; every ambiguity fails closed."""
    events, errors = _read_events(state)
    buckets: dict[str, dict[str, list[tuple[int, dict[str, Any]]]]] = {}
    for index, event in enumerate(events):
        event_type = event.get("event")
        if not (isinstance(event_type, str) and event_type.startswith("execution_action_")):
            continue
        if event_type not in _EVENTS:
            errors.append("unknown_census_event")
            continue
        action_id = event.get("action_id")
        if not isinstance(action_id, str):
            errors.append("census_event_missing_action_id")
            continue
        buckets.setdefault(action_id, {phase: [] for phase in _EVENTS})[event_type].append(
            (index, event)
        )

    run_id = str(getattr(state, "run_id", "") or "")
    actions: list[dict[str, Any]] = []
    open_ids: list[str] = []
    invalid_ids: list[str] = []
    for action_id in sorted(buckets):
        rows = buckets[action_id]
        admitted, spawned, terminal = (rows[phase] for phase in _EVENTS)
        local: list[str] = []
        if not admitted:
            local += [
                *(["orphan_spawn"] if spawned else []),
                *(["orphan_terminal"] if terminal else []),
            ]
        for name, values in (("admission", admitted), ("spawn", spawned), ("terminal", terminal)):
            if len(values) > 1:
                local.append(f"duplicate_{name}")
        if admitted and not spawned:
            local.append("missing_spawn")
        if admitted and not terminal:
            local.append("missing_terminal")
        admission = admitted[0][1] if len(admitted) == 1 else None
        spawn = spawned[0][1] if len(spawned) == 1 else None
        end = terminal[0][1] if len(terminal) == 1 else None
        if admission is not None and not _valid_admission(admission, run_id):
            local.append("invalid_admission")
        if spawn is not None and not _valid_followup(spawn, run_id, spawn=True):
            local.append("invalid_spawn")
        if end is not None and not _valid_followup(end, run_id, spawn=False):
            local.append("invalid_terminal")
        if admission is not None and spawn is not None and end is not None:
            if not (admitted[0][0] < spawned[0][0] < terminal[0][0]):
                local.append("phase_order_invalid")
        local = list(dict.fromkeys(local))
        if local:
            kind, failure = "ineligible", None
            invalid_ids.append(action_id)
            if {"missing_spawn", "missing_terminal"}.intersection(local):
                open_ids.append(action_id)
            errors.extend(local)
        else:
            assert admission is not None and spawn is not None
            kind, failure = _kind(admission, spawn)
        actions.append(
            {
                "action_id": action_id,
                "tool": admission.get("tool") if admission else None,
                "action_signature": admission.get("action_signature") if admission else None,
                "effective_effects": list(admission.get("effective_effects") or [])
                if admission
                else [],
                "route_binding": admission.get("route_binding") if admission else None,
                "resolver_decision": admission.get("resolver_decision") if admission else None,
                "resolver_policy": admission.get("resolver_policy") if admission else None,
                "write_target": admission.get("write_target") if admission else None,
                "payload_spawned": spawn.get("payload_spawned") if spawn else None,
                "job_submitted": spawn.get("job_submitted") if spawn else None,
                "terminal_status": end.get("terminal_status") if end else None,
                # P0a v3：时序事实随动作走——产物归属看的是 attempt 时间窗，早于任何
                # route-backed 终态的无路线写入要被看见、不能被后来的执行追认成产物。
                "admitted_at": admission.get("at") if admission else None,
                "admitted_index": admitted[0][0] if len(admitted) == 1 else None,
                "terminal_index": terminal[0][0] if len(terminal) == 1 else None,
                "compatibility_kind": kind,
                "compatibility_failure": failure,
                "errors": local,
            }
        )
    errors = list(dict.fromkeys(errors))
    _mark_writes_before_first_route_backed_terminal(actions)
    actions += _receipted_fetch_actions(state)
    return {
        "status": "ready" if not errors else "invalid",
        "complete": not errors,
        "schema_version": CENSUS_SCHEMA_VERSION,
        "action_count": len(actions),
        "actions": actions,
        "open_action_ids": sorted(open_ids),
        "invalid_action_ids": sorted(invalid_ids),
        "error_codes": errors,
    }


def _receipted_fetch_actions(state: Any) -> list[dict[str, Any]]:
    """Project fetch_resource's own durable receipts as receipted actions.

    P0a v2（裁决 (a)）：fetch_resource 的权威事实是 resource_acquisition_completed /
    resource_acquisition_failed 两个 transcript 事件（下载、核验、原子导入都在其
    后面），不另造第二套三阶段账。投影出来的动作只带 receipted 这一种 kind，
    永远不能满足真实执行义务。
    """
    import hashlib
    import json

    try:
        events, _warnings = _execution_route._read_transcript_events(state)
    except Exception:
        return []
    projected: list[dict[str, Any]] = []
    dispatch_rows: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        name = str(event.get("event") or "")
        if name in _FETCH_RECEIPT_EVENTS:
            tool, terminal = "fetch_resource", _FETCH_RECEIPT_EVENTS[name]
            effects = ["network_access", "workspace_write"]
            policy = "controlled_resource_fetch"
            target = str(event.get("destination") or "") or None
        elif name == "data_request_dispatched":
            # P0a v4：dispatch 的权威收据本来就随基线的 data_request_dispatched 落账
            # （contract_audit 在账本提交后写它）；不另造第二条事件。paused 的派发
            # 终态未知——按 unknown 记，_accounted_failures 把它算失败，直到下面的
            # data_dispatch_pause_reconciled 把同一 spec 的终态补上。
            receipt = event.get("data_dispatch_receipt")
            receipt = receipt if isinstance(receipt, dict) else {}
            tool = "dispatch_data_request"
            terminal = _dispatch_terminal_status(
                receipt.get("dispatch_state"), receipt.get("child_status"))
            effects = ["workspace_write"]
            policy = "managed_child_dispatch"
            target = None
        elif name == "data_dispatch_pause_reconciled":
            if str(event.get("status") or "") != "success":
                continue
            spec_id = str(event.get("spec_id") or "")
            terminal = _dispatch_terminal_status(
                "returned", event.get("child_status"),
                terminal_outcome=event.get("terminal_outcome"))
            row = dispatch_rows.get(spec_id)
            if row is not None:
                # 同一 receipt identity 的单调演进：终态由 reconcile 事件接管。
                row["terminal_status"] = terminal
                row["terminal_index"] = index
                row["reconciled_by_event"] = name
                continue
            # 原 dispatched 事件没落下（其持久化失败已结构化返回）：reconcile 事件
            # 本身就是这次派发唯一的账。
            tool = "dispatch_data_request"
            effects = ["workspace_write"]
            policy = "managed_child_dispatch"
            target = None
        else:
            continue
        digest = hashlib.sha256(
            json.dumps({"index": index, "event": event}, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        projected.append(
            {
                "action_id": "receipt-" + digest[:32],
                "tool": tool,
                "action_signature": None,
                "effective_effects": effects,
                "route_binding": None,
                "resolver_decision": "route_not_required",
                "resolver_policy": policy,
                "write_target": target,
                "payload_spawned": False,
                "job_submitted": False,
                "terminal_status": terminal,
                "admitted_at": event.get("at"),
                "admitted_index": index,
                "terminal_index": index,
                "compatibility_kind": "receipted",
                "compatibility_failure": None,
                "receipt_event": name,
                "errors": [],
            }
        )
        if tool == "dispatch_data_request":
            dispatch_rows[str(event.get("spec_id") or "")] = projected[-1]
    # 对抗审查（09-21）：paused 的派发若 data_request_dispatched 事件没落下（持久化
    # 失败已结构化返回），reconcile 在子 run 结束前补不了任何事件——census 看不见这次
    # 派发，build 收尾就能绿。权威账本（input delivery ledger）此时已提交：没有事件的
    # spec 从账本投影一行（paused → unknown），账不能因为事件缺失而变空。
    try:
        try:
            from .input_delivery import load_input_delivery_ledger
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.input_delivery import load_input_delivery_ledger
        ledger = load_input_delivery_ledger(state)
    except Exception:
        ledger = None
    specs = (ledger or {}).get("specs") if isinstance(ledger, dict) else None
    for spec_id, entry in sorted((specs or {}).items()):
        if spec_id in dispatch_rows or not isinstance(entry, dict):
            continue
        delivery = entry.get("delivery")
        receipt = delivery.get("data_dispatch_receipt") if isinstance(delivery, dict) else None
        if not isinstance(receipt, dict) or entry.get("lifecycle_status") not in {None, "active"}:
            continue
        terminal = _dispatch_terminal_status(
            receipt.get("dispatch_state"), receipt.get("child_status"))
        digest = hashlib.sha256(
            json.dumps({"ledger_spec": spec_id, "receipt": receipt}, sort_keys=True, default=str)
            .encode("utf-8")
        ).hexdigest()
        row = {
            "action_id": "ledger-" + digest[:32],
            "tool": "dispatch_data_request",
            "action_signature": None,
            "effective_effects": ["workspace_write"],
            "route_binding": None,
            "resolver_decision": "route_not_required",
            "resolver_policy": "managed_child_dispatch",
            "write_target": None,
            "payload_spawned": False,
            "job_submitted": False,
            "terminal_status": terminal,
            "admitted_at": None,
            "admitted_index": len(events),
            "terminal_index": len(events),
            "compatibility_kind": "receipted",
            "compatibility_failure": None,
            "receipt_event": "input_delivery_ledger",
            "errors": [],
        }
        dispatch_rows[spec_id] = row
        projected.append(row)
    return projected


def _dispatch_terminal_status(
    dispatch_state: Any, child_status: Any, *, terminal_outcome: Any = None,
) -> str:
    """dispatch 收据 → census 终态；paused 一律 unknown。"""
    if terminal_outcome is not None:
        return "success" if str(terminal_outcome) == "dataset" else "failed"
    if str(dispatch_state or "") != "returned":
        return "unknown"
    return (
        "failed" if str(child_status or "") in {"failed", "blocked", "error"}
        else "success"
    )


def _mark_writes_before_first_route_backed_terminal(actions: list[dict[str, Any]]) -> None:
    """Flag accounted writes that happened before any route-backed execution ended.

    P0a v3（Codex 复审 20 号）：无路线写入先发生、之后才出现 route-backed 动作时，
    重新归约不能把它"追认"成产物来源。kind 照旧（它仍是入账的动作，义务由
    route-backed 动作兑现），但这里给它打上可见标记；ROC 的产物归属另按
    attempt 收尾时冻结的身份收据判（v4），早写出的文件不在任何收据里。
    """
    satisfying = [
        action["terminal_index"]
        for action in actions
        if action.get("compatibility_kind") == "route_backed"
        and action.get("terminal_status") == "success"
        and isinstance(action.get("terminal_index"), int)
    ]
    first = min(satisfying) if satisfying else None
    for action in actions:
        if action.get("compatibility_kind") not in {"incidental", "receipted"}:
            continue
        admitted = action.get("admitted_index")
        action["preceded_first_route_backed_terminal"] = bool(
            first is None or (isinstance(admitted, int) and admitted < first)
        )


def _local_correction_receipts(state: Any) -> dict[str, dict[str, Any]]:
    """纠正复用的本地 attempt → 与 route_step_outcome 同形的产物收据事件。"""
    try:
        snapshot = _execution_route.build_route_snapshot(state)
        lineage = _execution_route._recovery_lineage(state)
    except Exception:
        return {}
    receipts: dict[str, dict[str, Any]] = {}
    for step_id, info in (snapshot.get("steps") or {}).items():
        if not isinstance(info, dict):
            continue
        attempt_id = str(info.get("attempt_id") or "")
        if not attempt_id or info.get("state") != "verified":
            continue
        # 只有本地纠正复用才会在 lineage 头里带 local_output_correction_witness；
        # 普通成功没有 lineage，外部改指向只有 external_output_repoint_witness——
        # 所以下面按见证过滤就够了，不再另查投影的 recovered_from_local_attempt
        # （09-22 变异 MC：那条件与见证过滤等价，删掉免得留一条钉不住的守卫）。
        entries = lineage.get(attempt_id) or []
        head = entries[-1] if entries else None
        witness = head.get("local_output_correction_witness") if isinstance(head, dict) else None
        if not (
            isinstance(witness, dict)
            and witness.get("physical_postcondition_verified") is True
            and str(witness.get("route_step_id") or step_id) == str(step_id)
        ):
            continue
        rows = [
            dict(item["output_observation"])
            for item in (witness.get("files") or [])
            if isinstance(item, dict) and isinstance(item.get("output_observation"), dict)
        ]
        if not rows:
            continue
        receipts[attempt_id] = {
            "event": "declared_route_recovery_receipt",
            "attempt_id": attempt_id,
            "route_step_id": step_id,
            "verified_output_specs": [str(s) for s in (witness.get("verified_outputs") or [])],
            "output_observations": rows,
            "output_observations_truncated": False,
        }
    return receipts


def satisfying_attempt_output_receipts(state: Any) -> list[dict[str, Any]]:
    """Frozen output receipts of the route-backed attempts that satisfied real execution.

    P0a v4 的共同不变量：**声明的 build 产物必须映射到满足义务的那次 attempt 在
    收尾时冻结的产物身份收据**。收据由 execution_route 在 ``route_step_outcome``
    （本地步，outcome=success）或 ``route_step_external_execution_verified``
    （外部作业，成功核验）里写下的 ``output_observations``：逐文件的 lexical
    路径、kind、dev/ino/size、正文 sha256。这里只挑出满足义务的那些 attempt 的
    收据，不重新读盘、不看 mtime（v3 的时间窗已删：mtime 不是 producer identity）。
    """
    reduced = reduce_execution_action_census(state)
    try:
        events, _warnings = _execution_route._read_transcript_events(state)
    except Exception:
        return []
    outcome_by_attempt: dict[str, dict[str, Any]] = {}
    for event in events:
        name = str(event.get("event") or "")
        attempt_id = str(event.get("attempt_id") or "")
        if not attempt_id:
            continue
        if name == "route_step_outcome" and str(event.get("outcome") or "") == "success":
            outcome_by_attempt.setdefault(attempt_id, event)
        elif name == "route_step_external_execution_verified" and (
            str(event.get("route_outcome") or "") == "success"
            and str(event.get("verification_status") or "") == "success"
            and (
                not isinstance(event.get("verification_receipt"), dict)
                or event["verification_receipt"].get("success_verified") is True
            )
        ):
            # 外部作业（submit_job）的 attempt 没有 outcome=success：提交时 outcome 是
            # submitted，成功由 record_external_route_execution_verification 写这条
            # 核验事件，收据随它一起冻结。
            outcome_by_attempt.setdefault(attempt_id, event)
    # 051：本地 exact-output 纠正复用的 attempt。它的 outcome 事件是 failed
    # （expected_outputs_missing），成功由冻结在路线版本 metadata 里的纠正收据证明，
    # 收据里的见证逐文件带 P0a 形状的产物身份（_local_output_repoint_witness）。
    # 只认路线投影已核过（state=verified 且 recovered_from_local_attempt）的那次
    # attempt——投影负责 lineage 头与当前步骤身份的三条件校验，这里不重做。
    for attempt_id, event in _local_correction_receipts(state).items():
        outcome_by_attempt.setdefault(attempt_id, event)
    receipts: list[dict[str, Any]] = []
    for action in reduced.get("actions") or []:
        if (
            action.get("compatibility_kind") != "route_backed"
            or action.get("terminal_status") != "success"
        ):
            continue
        binding = action.get("route_binding") or {}
        attempt_id = str(binding.get("route_attempt_id") or "")
        outcome = outcome_by_attempt.get(attempt_id)
        if not outcome:
            continue
        rows = outcome.get("output_observations")
        receipts.append(
            {
                "attempt_id": attempt_id,
                "route_step_id": binding.get("route_step_id"),
                "action_id": action.get("action_id"),
                "source_event": str(outcome.get("event") or ""),
                "verified_output_specs": list(outcome.get("verified_output_specs") or []),
                "output_observations": [
                    dict(row) for row in rows if isinstance(row, dict)
                ] if isinstance(rows, list) else [],
                "truncated": bool(outcome.get("output_observations_truncated")),
            }
        )
    return receipts


def _accounted_failures(actions: list[dict[str, Any]]) -> list[str]:
    """Accounted-only actions (incidental / receipted) must at least be settled."""
    failures: list[str] = []
    if any(action.get("terminal_status") in {None, "unknown"} for action in actions):
        failures.append("accounted_action_terminal_unknown")
    return failures


def _pending_context(state: Any) -> tuple[dict[str, Any], dict[str, Any], bool]:
    pending_scientific_signal: dict[str, Any] | None = None
    try:
        accepted = _run_contract.resolve_run_acceptance(state, bind_if_absent=False)
    except Exception as exc:
        assignment = {"status": "receipt_error", "reason": type(exc).__name__}
    else:
        receipt = accepted.get("receipt") if accepted.get("passed") else None
        raw = receipt.get("prereg_assignment") if isinstance(receipt, dict) else None
        assignment = (
            {
                "status": str(raw.get("kind") or "invalid"),
                "assignment": dict(raw),
                "schema_version": receipt.get("schema_version"),
                "run_acceptance_receipt_digest": receipt.get("receipt_digest"),
            }
            if isinstance(raw, dict)
            else {"status": str(accepted.get("status") or "receipt_invalid")}
        )
        durable_signal = accepted.get("pending_scientific_signal")
        if isinstance(durable_signal, dict):
            pending_scientific_signal = dict(durable_signal)
    if pending_scientific_signal is None:
        volatile_signal = getattr(state, "hook_state", {}).get(
            "_pending_scientific_signal_unpersisted"
        )
        if isinstance(volatile_signal, dict):
            pending_scientific_signal = dict(volatile_signal)
    if pending_scientific_signal is not None:
        assignment["pending_scientific_signal"] = pending_scientific_signal
    try:
        mode = _run_contract.load_execution_mode_view(state)
    except Exception as exc:
        mode = {"status": "audit_error", "mode": None, "reason": type(exc).__name__}
    applicable = bool(
        assignment.get("schema_version") == 2
        and assignment.get("status") == "pending"
        and mode.get("classified") is True
        and mode.get("status") == "classified"
        and mode.get("mode") == "operational"
        and pending_scientific_signal is None
    )
    return assignment, mode, applicable


def pending_operation_action_block(
    state: Any, action: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any] | None:
    """Pre-side-effect guard for the temporary pending-assignment lane."""
    # This supplemental gate must never replace a route-owned rejection and
    # its recovery guidance.  It runs only on decisions that could otherwise
    # reach a side effect.
    if not isinstance(action, dict) or not isinstance(decision, dict):
        return None
    if decision.get("decision") not in {"route_not_required", "matched_ready_step"}:
        return None
    tool = str(action.get("tool") or "").strip()
    effects = set(_strings(decision.get("effective_effects")))
    read_only = bool(
        tool in _PENDING_TOOLS
        and decision.get("decision") == "route_not_required"
        and decision.get("policy") == "read_only"
        and action.get("read_only") is True
        and decision.get("read_only") is True
        and not effects
    )
    assignment, _mode, applicable = _pending_context(state)
    assignment_status = str(assignment.get("status") or "")
    schema_version = assignment.get("schema_version")
    if assignment_status in {"bound", "none"} or schema_version == 1:
        return None
    # Diagnosis is non-materializing and remains available even when the
    # assignment authority cannot be read.  Every other action fails closed.
    if read_only:
        return None
    if assignment_status != "pending" or schema_version != 2:
        reason = "prereg_assignment_authority_unavailable"
    elif assignment.get("pending_scientific_signal"):
        reason = "prior_scientific_signal_requires_redispatch"
    elif not applicable:
        reason = "pending_assignment_requires_explicit_operational_scope"
    else:
        reason = ""
    dry_run = bool(
        tool in _DRY_RUN_TOOLS and action.get("dry_run") is True and decision.get("dry_run") is True
    )
    mechanical = bool(
        tool in _EXECUTION_TOOLS
        and decision.get("decision") == "matched_ready_step"
        and decision.get("authoritative") is True
        and "scientific_execution" not in effects
    )
    if not reason:
        if "scientific_execution" in effects:
            reason = "scientific_execution_requires_exact_prereg_assignment"
        elif tool not in _PENDING_TOOLS:
            reason = "unknown_execution_tool"
        elif tool in _SUPPORT_TOOLS:
            # 裁决 (a)：带收据的 support tool 放行；它们入账为 receipted。
            # 复审实测：原来这里把 fetch_resource / dispatch_data_request 前置拒，
            # 而 safe_write_file 却根本不经过本门也不入账——两头都不对。
            return None
        elif read_only or dry_run or mechanical:
            return None
        else:
            reason = "route_exempt_effectful_action_not_compatible"
    return {
        "status": "error",
        "error_code": "prereg_pending_operation_action_ineligible",
        "reason": reason,
        "assignment_status": assignment_status or "invalid",
        "retryable_in_current_run": False,
        "phase": "pre_side_effect",
        "next_action": {
            "owner": (
                "framework" if assignment_status == "receipt_error" else "dispatching_parent"
            ),
            "action": (
                "repair_run_authority_receipt"
                if assignment_status == "receipt_error"
                else "redispatch_with_typed_prereg_assignment"
            ),
        },
        "release_marker": COMPATIBILITY_RELEASE_MARKER,
    }


def _nonexecuting_failures(actions: list[dict[str, Any]]) -> list[str]:
    failures = [
        str(action["compatibility_failure"])
        for action in actions
        if action.get("compatibility_failure")
    ]
    if any(action.get("terminal_status") == "unknown" for action in actions):
        failures.append("terminal_status_unknown")
    if any(
        (
            action.get("compatibility_kind") == "read_only"
            and (action.get("payload_spawned") is None or action.get("job_submitted") is not False)
        )
        or (
            action.get("compatibility_kind") == "dry_run"
            and (
                action.get("payload_spawned") is not False
                or action.get("job_submitted") is not False
            )
        )
        for action in actions
    ):
        failures.append("spawn_observation_unknown")
    return failures


def _route_owned_external_recovery(
    action: dict[str, Any],
    *,
    active_step: str | None,
    bound_step: str,
    step_projection: dict[str, Any],
) -> bool:
    """Recognize an exact route-owned recovery without rewriting census history.

    A submit response can be lost after the scheduler accepted the job.  The
    immutable census must keep that original ``unknown``/``error`` fact, while
    the route owner may later recover the exact identity and verify the same
    attempt's physical outcome.  Only that complete proof can satisfy the
    derived execution obligation; an explicit negative submission fact never
    converges this way.
    """
    return bool(
        action.get("tool") == "submit_job"
        and action.get("job_submitted") is not False
        and action.get("terminal_status") in {"unknown", "error"}
        and active_step
        and active_step == bound_step
        and step_projection.get("state") == "verified"
        and step_projection.get("identity_resolution") == "exact"
        and step_projection.get("outcome") == "submitted"
        and step_projection.get("external_outcome") == "success"
    )


def _route_failures(state: Any, actions: list[dict[str, Any]]) -> list[str]:
    try:
        snapshot = _execution_route.build_route_snapshot(state)
    except Exception:
        return ["route_snapshot_unavailable"]
    failures: list[str] = []
    if snapshot.get("status") != "ready" or snapshot.get("route_state") != "complete":
        failures.append("route_not_complete")
    if snapshot.get("event_history_error") or snapshot.get("transcript_warnings"):
        failures.append("route_history_invalid")
    if not isinstance(snapshot.get("route_ref"), dict):
        failures.append("route_identity_invalid")
    route_steps = snapshot.get("route", {}).get("steps", [])
    projected = snapshot.get("steps") if isinstance(snapshot.get("steps"), dict) else {}
    step_ids = [step.get("id") for step in route_steps if isinstance(step, dict) and step.get("id")]
    if not step_ids:
        failures.append("route_steps_missing")
    if any(projected.get(step_id, {}).get("state") != "verified" for step_id in step_ids):
        failures.append("route_step_not_verified")
    if any(
        "scientific_execution" in set(step.get("effects") or [])
        for step in route_steps
        if isinstance(step, dict)
    ):
        failures.append("scientific_execution_observed")

    execution_steps = {
        step["id"]
        for step in route_steps
        if isinstance(step, dict)
        and isinstance(step.get("action"), dict)
        and step["action"].get("tool") in _EXECUTION_TOOLS
    }
    expected = {
        step_id: str(projected.get(step_id, {}).get("attempt_id") or "")
        for step_id in execution_steps
    }
    active_attempts = {attempt: step for step, attempt in expected.items()}
    if not all(expected.values()) or len(active_attempts) != len(expected):
        failures.append("route_attempt_projection_invalid")
    observed: dict[str, str] = {}
    seen: set[str] = set()
    recovered_external_action_ids: set[str] = set()
    for action in actions:
        binding = action.get("route_binding") or {}
        attempt = str(binding.get("route_attempt_id") or "")
        step_id = str(binding.get("route_step_id") or "")
        if not attempt or attempt in seen:
            failures.append("route_step_census_mismatch")
        seen.add(attempt)
        if not _binding_persisted(state, binding, str(action.get("tool") or "")):
            failures.append("route_action_binding_unresolved")
        active_step = active_attempts.get(attempt)
        step_projection = (
            projected.get(active_step, {})
            if isinstance(active_step, str)
            and isinstance(projected.get(active_step), dict)
            else {}
        )
        tool = action.get("tool")
        payload_spawned = action.get("payload_spawned")
        job_submitted = action.get("job_submitted")
        recovered_external = _route_owned_external_recovery(
            action,
            active_step=active_step,
            bound_step=step_id,
            step_projection=step_projection,
        )
        if recovered_external:
            recovered_external_action_ids.add(str(action.get("action_id") or ""))
        effective_terminal_status = (
            "success" if recovered_external else action.get("terminal_status")
        )
        effective_job_submitted = True if recovered_external else job_submitted
        if tool in _LOCAL_PROCESS_TOOLS:
            if payload_spawned is None or job_submitted is not False:
                failures.append("route_action_spawn_unknown")
            if effective_terminal_status == "success" and payload_spawned is not True:
                failures.append("route_action_success_without_spawn")
        elif tool == "submit_job":
            if effective_job_submitted is None or (
                effective_job_submitted is True and payload_spawned is False
            ):
                failures.append("route_action_submission_unknown")
            if (
                effective_terminal_status == "success"
                and effective_job_submitted is not True
            ):
                failures.append("route_action_success_without_submission")
        if active_step is not None:
            if active_step != step_id or active_step in observed:
                failures.append("route_step_census_mismatch")
            observed[active_step] = attempt
            if effective_terminal_status != "success":
                failures.append("route_action_not_successful")
            if tool in _LOCAL_PROCESS_TOOLS and payload_spawned is not True:
                failures.append("route_active_payload_not_spawned")
            if tool == "submit_job" and effective_job_submitted is not True:
                failures.append("route_active_job_not_submitted")
    if observed != expected:
        failures.append("route_attempt_census_mismatch")
    if any(
        action.get("terminal_status") == "unknown"
        and str(action.get("action_id") or "") not in recovered_external_action_ids
        for action in actions
    ):
        failures.append("route_action_terminal_unknown")
    return list(dict.fromkeys(failures))


def _route_exempt_failures(state: Any, actions: list[dict[str, Any]]) -> list[str]:
    failures = _nonexecuting_failures(actions)
    try:
        snapshot = _execution_route.build_route_snapshot(state)
    except Exception:
        snapshot = {"status": "error"}
    if snapshot.get("status") == "ready":
        failures.append("route_present_for_route_exempt_census")
        if snapshot.get("route_state") != "complete":
            failures.append("route_not_complete")
    elif not (
        snapshot.get("status") == "unavailable" and snapshot.get("reason") == "route_not_declared"
    ):
        failures.append("route_snapshot_unavailable")
    if snapshot.get("event_history_error") or snapshot.get("transcript_warnings"):
        failures.append("route_history_invalid")
    return list(dict.fromkeys(failures))


def operation_execution_obligation(state: Any) -> dict[str, Any]:
    """Project whether authoritative actions prove a real mechanical execution."""
    reduced = reduce_execution_action_census(state)
    actions = list(reduced.get("actions") or [])
    failures: list[str] = []
    if not reduced.get("complete"):
        failures.append("action_census_incomplete")
    if not actions:
        failures.append("action_census_empty")
    if any("scientific_execution" in set(a.get("effective_effects") or []) for a in actions):
        failures.append("scientific_execution_observed")

    kinds = {action.get("compatibility_kind") for action in actions}
    routed = [action for action in actions if action.get("compatibility_kind") == "route_backed"]
    exempt = [
        action for action in actions if action.get("compatibility_kind") in {"read_only", "dry_run"}
    ]
    accounted = [
        action
        for action in actions
        if action.get("compatibility_kind") in {"incidental", "receipted"}
    ]
    receipted = [action for action in actions if action.get("compatibility_kind") == "receipted"]
    lane: str | None = None
    if routed and kinds.issubset(
        {"route_backed", "read_only", "dry_run", "incidental", "receipted"}
    ):
        # P0a v2：incidental（路线满足后的低风险写入）与 receipted（带收据的
        # support tool）都只入账；义务仍只由 route_backed 动作兑现。
        lane = "route_backed_mechanical"
        failures += _route_failures(state, routed) + _nonexecuting_failures(exempt)
        failures += _accounted_failures(accounted)
    elif actions and kinds.issubset({"read_only", "dry_run"}):
        lane = "route_exempt_non_executing_diagnostic"
        failures += _route_exempt_failures(state, actions)
    elif receipted and kinds.issubset({"read_only", "dry_run", "receipted"}):
        # P0a v2（裁决 (a)）：pending 的 operation run 只做带收据的 support 动作
        # （下载 / 写文件 / 派 Data child）+ 只读诊断。合规，但不是真实执行：
        # build 类 ROC 仍会因义务未满足而拒。incidental 不进这条 lane——没有
        # route-backed 执行垫底的裸写入，与 BASE 一样不算数。
        lane = "route_exempt_receipted_support"
        failures += _nonexecuting_failures(exempt) + _accounted_failures(receipted)
    elif actions:
        failures += [
            str(action["compatibility_failure"])
            for action in actions
            if action.get("compatibility_failure")
        ]
        failures.append("mixed_or_unsupported_action_census")
    failures = list(dict.fromkeys(failures))
    passed = not failures
    real_execution = bool(passed and lane == "route_backed_mechanical")
    return {
        "passed": passed,
        "status": "satisfied" if real_execution else "not_satisfied",
        "compatibility_lane": lane,
        "failure_reasons": failures,
        "action_census": reduced,
        "real_execution_obligation_satisfied": real_execution,
    }


def pending_operation_compatibility(state: Any) -> dict[str, Any]:
    """Audit the one-release omitted-assignment operational lane."""
    assignment, mode, applicable = _pending_context(state)
    obligation = operation_execution_obligation(state)
    failures: list[str] = []
    if assignment.get("status") != "pending":
        failures.append("prereg_assignment_not_pending")
    if assignment.get("pending_scientific_signal"):
        failures.append("prior_scientific_signal_requires_redispatch")
    if not (
        mode.get("classified") is True
        and mode.get("status") == "classified"
        and mode.get("mode") == "operational"
    ):
        failures.append("explicit_operational_scope_required")
    failures.extend(obligation.get("failure_reasons") or [])
    failures = list(dict.fromkeys(failures))
    passed = applicable and obligation.get("passed") is True and not failures
    return {
        "passed": passed,
        "applicable": applicable,
        "status": "compatible" if passed else "incompatible",
        "compatibility_lane": obligation.get("compatibility_lane"),
        "failure_reasons": failures,
        "prereg_assignment": assignment,
        "execution_mode": mode,
        "action_census": obligation.get("action_census"),
        "real_execution_obligation_satisfied": bool(
            passed and obligation.get("real_execution_obligation_satisfied") is True
        ),
        "release_marker": COMPATIBILITY_RELEASE_MARKER,
        "deprecation_witness": {
            "temporary": True,
            "removal_requires": list(COMPATIBILITY_REMOVAL_SIGNALS),
        },
    }


__all__ = [
    "ADMITTED_EVENT",
    "CENSUS_SCHEMA_VERSION",
    "COMPATIBILITY_RELEASE_MARKER",
    "COMPATIBILITY_REMOVAL_SIGNALS",
    "SPAWN_EVENT",
    "TERMINAL_EVENT",
    "begin_execution_action",
    "finish_execution_action",
    "observe_execution_action_spawn",
    "operation_execution_obligation",
    "pending_operation_action_block",
    "pending_operation_compatibility",
    "reduce_execution_action_census",
    "settle_execution_action",
]
