"""Read the Core-selected formal input without inventing a second task owner.

This adapter has no durable state and never selects a newer contract.  Without a
Core task identity, the existing explicit node-input protocol remains usable.
With one, mutable node-input prereg hints are observations, never authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.task_contract import assignment_for_state, compute_digest


def _node_input_digest(state: Any) -> str | None:
    """Canonical caller A, excluding descriptions and mutable input availability.

    Reuse execution_intent_snapshot's JSON/set rules, but not its scope binding.
    The original prereg declaration is hashed, never a catalog observation.
    """
    hook = getattr(state, "hook_state", {}) or {}
    raw = hook.get("node_inputs") if isinstance(hook, dict) else None
    if not isinstance(raw, dict) or not raw:
        return None
    inputs = {key: value for key, value in raw.items()
              if key not in {"scope", "operation_category"}}
    forwarded = hook.get("forwarded_input_ids") or []
    if (not isinstance(forwarded, list)
            or any(not isinstance(item, str) or not item for item in forwarded)):
        return None
    try:
        encoded = json.dumps(
            {"node_inputs": inputs, "forwarded_input_ids": sorted(set(forwarded))},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def selected_task_binding_status(recorded: dict[str, Any], current: dict[str, Any]) -> str:
    """Compare an existing A index, never mint a replacement from a resume."""
    if recorded.get("task_contract") != current.get("task_contract"):
        return "run_authority_input_changed"
    if current.get("task_contract") is None:
        before, after = recorded.get("node_input_digest"), current.get("node_input_digest")
        if not before or not after:
            return "run_authority_unavailable"
        if before != after:
            return "run_authority_input_changed"
    return "selected_task_unchanged"


def resolve_task_inputs(state: Any) -> dict[str, Any]:
    raw = (getattr(state, "hook_state", {}) or {}).get("node_inputs")
    inputs = dict(raw) if isinstance(raw, dict) else {}
    uuid = getattr(state, "task_instance_uuid", None)
    digest = getattr(state, "task_contract_digest", None)
    number = getattr(state, "task_contract_revision", None)
    if all(value is None for value in (uuid, digest, number)):
        return {"inputs": inputs, "task_contract": None, "error": None,
                "input_witness": None, "node_input_digest": _node_input_digest(state)}

    reference = {"task_instance_uuid": uuid, "digest": digest, "revision": number}
    witness = {key: inputs.pop(key) for key in
               ("prereg_assignment", "prereg_artifact_id", "prereg_version") if key in inputs}
    # A selected but unavailable authority cannot degrade into explicit-none.
    inputs["prereg_assignment"] = None
    result = {"inputs": inputs, "task_contract": reference, "error": None,
              "input_witness": witness}
    if (not isinstance(uuid, str) or not uuid or not isinstance(digest, str) or not digest
            or type(number) is not int or number < 1):
        result["error"] = {"code": "task_contract_identity_invalid",
                           "reason": "Core task identity requires uuid, revision and digest"}
        return result
    try:
        revision, _kind = assignment_for_state(state)
    except Exception as exc:
        result["error"] = {"code": "task_contract_unavailable",
                           "reason": f"selected task contract could not be read: {type(exc).__name__}"}
        return result
    if revision is None:
        result["error"] = {"code": "task_contract_unavailable",
                           "reason": "the exact Core-selected task contract is unavailable"}
        return result
    if (revision.task_instance_uuid != uuid or revision.revision != number
            or revision.digest != digest or compute_digest(revision.as_dict()) != digest
            or revision.target_node not in {"", "experiment"}):
        result["error"] = {"code": "task_contract_identity_invalid",
                           "reason": "selected contract content does not match the Core task identity"}
        return result
    assignment = revision.prereg_assignment
    result["assignment_kind"] = _kind
    if assignment is None:
        inputs["prereg_assignment"] = {"kind": "pending"}
    elif isinstance(assignment, dict) and assignment.get("kind") == "explicit_none":
        inputs["prereg_assignment"] = {"kind": "none", "reason": assignment.get("reason")}
    elif isinstance(assignment, dict) and assignment.get("kind") == "exact_bound":
        version = assignment.get("version")
        # Core stores versions as strings.  Do not infer latest or fill a hash.
        if isinstance(version, str) and version.isascii() and version.isdecimal():
            version = int(version)
        inputs["prereg_assignment"] = {
            "kind": "bound", "artifact_id": assignment.get("artifact_id"),
            "version": version, "content_hash": assignment.get("content_hash"),
        }
    return result


def audit_selected_task_binding(state: Any) -> dict[str, Any]:
    """Read only the selected task identity for a non-consuming preparation.

    Missing/invalid prereg and undecided scope are not task-identity drift.
    Acquiring an input must remain possible before that input is available.
    This does not authorize input consumption or replace bootstrap/action
    durability, network grants, path capabilities, or the Core task owner.
    """
    from .ledger import bootstrap_phase
    from .run_bootstrap import load_run_authority

    view = resolve_task_inputs(state)
    if view["error"]:
        return {"passed": False, "status": view["error"]["code"],
                "reason": view["error"]["reason"], "task_contract": view["task_contract"]}
    authority = load_run_authority(state)
    if authority is None and bootstrap_phase(state) == "ready":
        return {"passed": False, "status": "run_authority_unavailable",
                "reason": "the ready run's recorded task authority could not be read",
                "task_contract": view["task_contract"]}
    # A half-bootstrap has no authority yet; the common action boundary owns
    # its bootstrap_incomplete refusal, not the old classification protocol.
    if authority is not None:
        status = selected_task_binding_status(authority, view)
        if status != "selected_task_unchanged":
            return {"passed": False, "status": status,
                    "reason": ("the selected task identity differs from this run's recorded task"
                               if status == "run_authority_input_changed" else
                               "the original or current node-input identity is unavailable; cannot verify this resume"),
                    "task_contract": view["task_contract"],
                    "recorded_task_contract": authority.get("task_contract")}
        # Prior rejected resumes remain history, not a permanent veto over the
        # original task. The turn hook records this current comparison for finish.
    return {"passed": True, "status": "selected_task_unchanged",
            "task_contract": view["task_contract"]}
