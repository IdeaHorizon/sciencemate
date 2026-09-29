"""Canonical data-pipeline outcomes and reviewer revision contracts."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


PIPELINE_OUTCOMES = {
    "completed",
    "retry_step",
    "revise_asset",
    "revise_plan",
    "needs_input",
    "externally_blocked",
    "fatal",
}


def merge_parameter_updates(parameters: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Apply a local amendment without deleting omitted sibling controls."""
    return {**deepcopy(parameters), **{
        key: merge_parameter_updates(parameters[key], value)
        if isinstance(parameters.get(key), dict) and isinstance(value, dict) else deepcopy(value)
        for key, value in updates.items()
    }}

def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_issue_identity(issue: dict[str, Any]) -> tuple[str, ...]:
    """Return the stable identity of a finding, excluding reviewer wording."""
    values = tuple(
        str(issue.get(key) or "").strip().casefold()
        for key in (
            "code", "file", "path", "step_id", "deliverable_id",
            "asset_id", "requirement_id",
        )
    )
    return values if any(values) else (" ".join(str(issue.get("message") or "").split()).casefold(),)


def review_progress_improved(current: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    """Count resolved findings even when they expose another validation layer."""
    if not previous:
        return True
    current_issues = {tuple(item) for item in current.get("issue_ids") or []}
    previous_issues = {tuple(item) for item in previous.get("issue_ids") or []}
    return (
        bool(previous_issues - current_issues)
        or int(current.get("passed_check_count") or 0) > int(previous.get("passed_check_count") or 0)
        or int(current.get("issue_count") or 0) < int(previous.get("issue_count") or 0)
    )


def revision_contract(
    issues: Any,
    *,
    scope: str = "",
    affected_ids: list[str] | None = None,
    authority_locked: bool = True,
) -> dict[str, Any] | None:
    """Normalize reviewer/executor findings into one bounded repair contract."""
    if isinstance(issues, dict):
        nested = issues.get("issues") or issues.get("feedback")
        if nested not in (None, "", [], {}):
            issues = nested
        elif any(issues.get(key) not in (None, "", [], {}) for key in (
            "code", "message", "recommendation", "required_change", "step_id", "file",
        )):
            issues = [issues]
        else:
            issues = []
    if not isinstance(issues, list):
        issues = []
    normalized = [dict(item) for item in issues if isinstance(item, dict)]
    if not normalized:
        return None

    inferred_ids = [
        str(item.get(key) or "").strip()
        for item in normalized
        for key in ("step_id", "deliverable_id", "asset_id", "file", "requirement_id")
        if str(item.get(key) or "").strip()
    ]
    selected_ids = list(dict.fromkeys([*(affected_ids or []), *inferred_ids]))
    if scope not in {"asset", "step", "plan"}:
        scope = (
            "step" if any(item.get("step_id") for item in normalized)
            else "asset" if selected_ids
            else "plan"
        )
    changes = list(dict.fromkeys(
        str(item.get("required_change") or item.get("recommendation") or item.get("message") or "").strip()
        for item in normalized
        if str(item.get("required_change") or item.get("recommendation") or item.get("message") or "").strip()
    ))
    semantic = {
        "scope": scope,
        "affected_ids": selected_ids,
        "issues": normalized,
        "required_changes": changes,
        "authority_locked": bool(authority_locked),
    }
    signature_basis = {
        "scope": scope,
        "affected_ids": sorted(selected_ids, key=str.casefold),
        "issues": sorted(revision_issue_identity(item) for item in normalized),
        "authority_locked": bool(authority_locked),
    }
    return {**semantic, "signature": _hash(signature_basis)}


def pipeline_outcome(result: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize tool and planner statuses once at the controller boundary."""
    value = result if isinstance(result, dict) else {}
    declared = str(value.get("outcome") or "").strip()
    if declared in PIPELINE_OUTCOMES:
        return {
            "kind": declared,
            "reason": str(value.get("reason") or value.get("error") or "").strip(),
            "revision_contract": value.get("revision_contract"),
        }

    status = str(value.get("status") or "").strip().lower()
    category = str(value.get("failure_category") or "").strip().lower()
    contract = value.get("revision_contract")
    contract = contract if isinstance(contract, dict) else None
    if status in {"success", "completed", "needs_final_generation_plan"}:
        kind = "completed"
    elif status == "needs_input":
        kind = "needs_input"
    elif status in {"needs_reference_search", "needs_geometry_processing"}:
        kind = "revise_plan"
    elif status in {"needs_revision", "review_failed"}:
        kind = (
            "revise_asset" if contract and contract.get("scope") in {"asset", "step"}
            else "revise_plan"
        )
    elif category == "transient_execution_error" or status == "retryable_error":
        kind = "retry_step"
    elif category in {"environment_required", "reference_asset_required"} or status == "externally_blocked":
        kind = "externally_blocked"
    elif status in {"error", "blocked"} and contract:
        kind = "revise_asset" if contract.get("scope") in {"asset", "step"} else "revise_plan"
    else:
        kind = "fatal"
    return {
        "kind": kind,
        "reason": str(
            value.get("error") or value.get("reason") or value.get("stop_reason") or status
        ).strip(),
        "revision_contract": contract,
    }


def with_pipeline_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the canonical outcome while detailed status remains tool-local."""
    outcome = pipeline_outcome(payload)
    return {**payload, "outcome": outcome["kind"], **(
        {"revision_contract": outcome["revision_contract"]}
        if outcome.get("revision_contract") else {}
    )}


def needs_input_result(
    *,
    question: str,
    missing_fields: list[Any] | None = None,
    context: str = "",
    options: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one resumable input contract for every preprocessing domain."""
    details = dict(metadata or {})
    fields = [str(item) for item in missing_fields or [] if str(item).strip()]
    contract = {
        "schema_version": "1.0",
        "action": "amend_preprocessing_request",
        "missing_fields": fields,
        "authority_locked": True,
        "resume_instruction": str(details.pop("resume_instruction", "") or (
            "Add the requested values to the same structured preprocessing request and resubmit it."
        )),
        **details,
    }
    return with_pipeline_outcome({
        "status": "needs_input",
        "question": question,
        "context": context,
        "options": list(options or []),
        "missing_fields": fields,
        "resume_contract": contract,
    })
