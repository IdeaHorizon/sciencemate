"""Single terminal blocked-delivery writer for the data service."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from core.context_engine import read_first_turn_node_inputs_receipt
from core.state import State
from nodes.data.delivery_contract import (
    artifact_name_for_attempt, outcome_fields,
)
from nodes.data.planning.request_contract import preprocessing_request_fingerprint
from nodes.data.planning.store import PlanningStore, canonical_hash


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
    if not resume_contract and status == "needs_input":
        resume_contract = {
            "action": "supply_missing_input_and_retry",
            "request_id": payload.get("request_id") or request.get("request_id"),
            "preserve_request_spec_hash": payload.get("request_spec_hash") or request.get("request_spec_hash"),
            "missing_fields": payload.get("required_fields") or payload.get("missing_inputs") or [],
        }
    if (status == "externally_blocked" and isinstance(resume_contract, dict)
            and resume_contract.get("kind") == "environment_required"
            and resume_contract.get("owner") == "experiment"):
        # Describe the caller's existing run_node workflow; Data neither
        # dispatches a child nor grants installation privileges itself.
        environment = {key: resume_contract[key] for key in (
            "python", "runtime", "missing_modules", "required_dependencies", "step_id",
        ) if resume_contract.get(key)}
        resume_contract = {
            **resume_contract,
            "handoff_request": {
                "node_type": "experiment",
                "user_note": "为 Data 补齐缺失的运行依赖，验证后继续原前处理任务。",
                "node_inputs": {
                    "experiment_spec": (
                        "Prepare only the runtime needed by the blocked Data step, using your managed "
                        "environment workflow and existing permissions. This is environment preparation, "
                        "not a scientific simulation or a request to generate Data's deliverables. "
                        "Prepare compatible dependencies in a project-managed dependency_root using "
                        "your existing path-role/install workflow. The reported python is a compatibility "
                        "reference, not an instruction to write its host venv/site-packages. Do not mutate "
                        "the host runtime or repeatedly ask for protected global writes. For Python packages, "
                        "verify imports and versions in that managed directory and include runtime_environment "
                        "in an existing operation artifact: {kind: python_packages, dependency_root: absolute path, "
                        "runtime: {implementation, python_version: major.minor, platform: sys.platform, machine}, "
                        "required_dependencies: [package requirements], import_checks: [module names]}. "
                        "Return the artifact ID and SHA-256 of its exact UTF-8 content. "
                        "A successful installer process alone is not verification. If preparation fails, "
                        "report the obstacle, not a ready environment. Data will verify again in its own sandbox. "
                        + json.dumps(environment, ensure_ascii=False)
                    ),
                },
            },
            "runtime_delivery": {
                "input_field": "runtime_environment_ref",
                "reference_fields": ["artifact_id", "sha256"],
                "instruction": "Forward the verified operation artifact through forward_artifact_ids and set node_inputs.runtime_environment_ref. The dependency directory must already be readable in the new Data attempt's sandbox. A path in text grants no access; Data will not install or widen mounts.",
            },
        }
    if status in {"needs_input", "externally_blocked"}:
        resume_contract = dict(resume_contract or {})
        if (status == "externally_blocked" and resume_contract.get("kind") == "pipeline_stage"
                and resume_contract.get("resume_run_id") != state.run_id
                and resume_contract.get("resume_request")):
            # A mistaken fresh dispatch is redirected to the original stage.
            # Keep that stage's exact inputs and recovery conditions; do not
            # rewrite it into yet another fresh-attempt request below.
            resume_contract["dispatch_correction"] = (
                "Continue the preserved work using resume_request after its retry precondition is met. "
                "Keep recovery_feedback for review; it is not permission to change the frozen request.")
        elif (status == "externally_blocked" and resume_contract.get("kind") == "pipeline_stage"
                and resume_contract.get("resume_run_id") == state.run_id
                and resume_contract.get("retry_target") and resume_contract.get("plan_hash")):
            # The first-turn receipt, not the failed caller's paraphrase, owns
            # the resume inputs. Reuse the platform receipt instead of asking
            # the caller to reconstruct an exact hash-bound request from prose.
            try:
                original_inputs = read_first_turn_node_inputs_receipt(state)["values"]
                record = PlanningStore(state).get_plan(str(resume_contract.get("plan_id") or ""))
                if not record or canonical_hash(record["plan"]) != resume_contract["plan_hash"]:
                    raise ValueError("The saved resume plan is unavailable or its hash changed.")
                request = deepcopy(record["plan"]["preprocessing_request"])
            except (OSError, ValueError, KeyError) as exc:
                resume_contract.pop("resume_request", None)
                resume_contract["input_receipt_error"] = str(exc)
            else:
                resume_contract["resume_request"] = {
                    "node_type": "data", "resume_run_id": state.run_id,
                    # Pin the already-normalized authority as well: a later
                    # dispatch must not reinterpret a mutable project intake.
                    "node_inputs": {**deepcopy(original_inputs), "preprocessing_request": deepcopy(request),
                        **{key: deepcopy(value) for key, value in (state.hook_state.get("node_inputs") or {}).items()
                           if key in {"recovery_feedback", "review_feedback"}}},
                }
            resume_contract.update({
                "action": "resume_pipeline_stage_from_caller",
                "retry_owner": "caller",
                "requires_new_attempt_id": False,
                "requires_new_child_run_id": False,
                "retry_precondition": "stage_service_or_protocol_changed_and_verified",
                "instruction": (
                    "After resolving the cited stage service/protocol problem, call run_node with "
                    "the exact resume_request object below; copy its node_inputs without paraphrasing, "
                    "simplifying or adding repair instructions to spec. Put new observed defects and evidence "
                    "in node_inputs.recovery_feedback; Data will verify them before editing. If resume_request is absent, restore "
                    "the first_turn_node_inputs receipt before calling. "
                    "Resume the saved stage under its approved plan and checkpoint; do not change the request, "
                    "regenerate successful assets, or reuse the blocked receipt as completed delivery. "
                    "If those assets are unavailable, report the limitation instead of starting generation."
                ),
            })
            review = state.hook_state.get("_data_authority_review") or {}
            progress, config = review.get("progress") or {}, review.get("config") or {}
            if resume_contract.get("retry_target") == "authority_review" and any(
                    config.get(limit) is not None and progress.get(used, 0) >= config[limit]
                    for used, limit in (("calls", "max_calls"), ("elapsed_s", "max_seconds"))):
                resume_contract.update(review_budget_exhausted=True,
                    retry_precondition="new_verified_evidence_or_review_budget_explicitly_extended")
        else:
            resume_contract.pop("resume_request", None)
            resume_contract.pop("preserve_request_spec_hash", None)
            resume_contract.update({
                "action": "request_controlled_retry_from_caller",
                "retry_owner": "caller",
                "requires_new_attempt_id": True,
                "requires_new_child_run_id": True,
                "previous_child_run_id": state.run_id,
                "retry_precondition": ("missing_inputs_supplied" if status == "needs_input"
                                       else "external_conditions_changed_and_verified"),
                "instruction": (
                    "The caller must authorize a fresh attempt after the cited inputs/environment change. "
                    "Preserve the old attempt and its evidence; never replay its receipt or resume its child. "
                    "Keep the original request unless the caller changes its requirements. "
                    "If the caller lacks a controlled retry transition, report that limitation instead of repeating dispatch."
                ),

            })
    missing = (payload.get("required_fields") or payload.get("missing_inputs")
               or payload.get("missing_fields") or [])
    fields = outcome_fields(status, missing=missing)
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
        **fields,
        **({"resume_contract": resume_contract} if isinstance(resume_contract, dict) else {}),
    }
    record = state.save_artifact(
        "preprocessing_blocked_report",
        artifact_name_for_attempt(state, name),
        json.dumps(content, indent=2, ensure_ascii=False, default=str),
        metadata={
            **dict(metadata or {}),
            "status": status,
            "artifact_kind": "preprocessing_blocked_report",
            "request_id": content.get("request_id") or request.get("request_id"),
            "request_spec_hash": content.get("request_spec_hash") or request.get("request_spec_hash"),
            "request_fingerprint": content.get("request_fingerprint") or "",
            **fields,
        },
    )
    state.append_transcript(
        "preprocessing_blocked_report_saved",
        artifact_id=record.get("id"),
        status=status,
        reason=content.get("reason") or content.get("stop_reason"),
    )
    return record
