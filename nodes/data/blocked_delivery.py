"""Single terminal blocked-delivery writer for the data service."""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from nodes.data.planning.request_contract import preprocessing_request_fingerprint


REPORT_STATUSES = {
    "needs_input",
    "externally_blocked",
    "fatal",
}


def save_preprocessing_blocked_report(
    state: State,
    *,
    name: str,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one canonical blocked report; never create a fake dataset."""
    status = str(payload.get("status") or "fatal").strip()
    if status not in REPORT_STATUSES:
        status = "fatal"
    request = payload.get("preprocessing_request") if isinstance(payload.get("preprocessing_request"), dict) else {}
    if not request:
        request = state.hook_state.get("_data_current_request") or {}
    if not request:
        request = (state.hook_state.get("_data_latest_requirement_analysis") or {}).get("preprocessing_request") or {}
    resume_contract = payload.get("resume_contract")
    if not isinstance(resume_contract, dict) and status == "needs_input":
        resume_contract = {
            "action": "supply_missing_input_and_retry",
            "request_id": payload.get("request_id") or request.get("request_id"),
            "preserve_request_spec_hash": payload.get("request_spec_hash") or request.get("request_spec_hash"),
            "missing_fields": payload.get("required_fields") or payload.get("missing_inputs") or [],
        }
    content = {
        **payload,
        **({"preprocessing_request": request} if request else {}),
        "request_id": payload.get("request_id") or request.get("request_id"),
        "request_spec_hash": payload.get("request_spec_hash") or request.get("request_spec_hash"),
        "request_fingerprint": (
            payload.get("request_fingerprint")
            or preprocessing_request_fingerprint(request)
        ),
        "status": status,
        "artifact_kind": "preprocessing_blocked_report",
        "delivery_complete": False,
        "simulation_ready": False,
        **({"resume_contract": resume_contract} if isinstance(resume_contract, dict) else {}),
    }
    record = state.save_artifact(
        "preprocessing_blocked_report",
        name,
        json.dumps(content, indent=2, ensure_ascii=False, default=str),
        metadata={
            **dict(metadata or {}),
            "status": status,
            "artifact_kind": "preprocessing_blocked_report",
            "delivery_complete": False,
            "simulation_ready": False,
            "request_id": content.get("request_id") or request.get("request_id"),
            "request_spec_hash": content.get("request_spec_hash") or request.get("request_spec_hash"),
            "request_fingerprint": content.get("request_fingerprint") or "",
        },
    )
    state.append_transcript(
        "preprocessing_blocked_report_saved",
        artifact_id=record.get("id"),
        status=status,
        reason=content.get("reason") or content.get("stop_reason"),
    )
    return record
