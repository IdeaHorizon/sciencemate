"""Direct service driver for caller-scoped data preprocessing."""
from __future__ import annotations

import json
import hashlib
import logging
import re
from pathlib import Path

from core.agent_loop import LoopResult
from core.llm import LLMMessage
from core.research_intake import load_intake

from nodes.data.blocked_delivery import save_preprocessing_blocked_report
from nodes.data.pipeline_contract import pipeline_outcome
from nodes.data.planning.request_contract import (
    normalize_preprocessing_request,
    preprocessing_request_fingerprint,
)
from nodes.data.planning.store import PlanningStore, canonical_hash
from nodes.data.progress import emit_progress
from nodes.data.review.gate_registry import required_gate_names


log = logging.getLogger("data.agent_loop")

# Match only a whole control message. A message such as "继续，但改成翼型"
# contains a new requirement and must retain the normal amendment precedence.
_CONTINUATION_ONLY_RE = re.compile(
    r"(?:请\s*)?(?:继续|重试|接着)(?:\s*(?:执行|运行|完成))?"
    r"(?:\s*(?:之前|刚才|上次|原来|原先|当前|这个|该)(?:的)?)?"
    r"(?:\s*(?:任务|工作|测试))?(?:\s*(?:吧|一下))?"
    r"|(?:please\s+)?(?:continue|resume|retry)"
    r"(?:\s+(?:the\s+)?(?:(?:previous|last|current|same)\s+)?"
    r"(?:task|work|test))?(?:\s+please)?",
    re.IGNORECASE,
)


def _is_continuation_only(text: str) -> bool:
    return _CONTINUATION_ONLY_RE.fullmatch(text.strip().rstrip("。.!！?？")) is not None


_INITIAL_DATASET_IDS = "_data_initial_dataset_artifact_ids"
_DELIVERY_ARTIFACT_KIND = "data_preprocessing_delivery"
_REQUIRED_MANIFEST_FIELDS = {
    "objective",
    "data_model",
    "source",
    "semantics",
    "measurement_context",
    "quality_gates",
    "lineage",
    "assumptions",
    "reproducibility",
    "downstream_contract",
}


def _same_request_identity(
    *,
    actual_hash: str,
    actual_fingerprint: str,
    expected_hash: str,
    expected_fingerprint: str,
) -> bool:
    """Compare persisted terminal state with the current semantic request."""
    if expected_fingerprint:
        return actual_fingerprint == expected_fingerprint
    if expected_hash:
        return actual_hash == expected_hash
    return True


def _quality_gates_complete(value: object, review_profile: str) -> bool:
    """Validate terminal quality gates, including their review evidence."""
    if not isinstance(value, list) or not value:
        return False
    gates = [gate for gate in value if isinstance(gate, dict)]
    if len(gates) != len(value):
        return False
    if any(
        str(gate.get("status") or "").lower() not in {"pass", "passed", "success", "ok"}
        for gate in gates
    ):
        return False
    by_name = {str(gate.get("name") or ""): gate for gate in gates}
    required = required_gate_names(review_profile)
    if not required.issubset(by_name):
        return False
    for name in required:
        evidence = by_name[name].get("evidence")
        if not isinstance(evidence, dict):
            return False
        try:
            expected = int(evidence.get("expected_checks"))
            observed = int(evidence.get("observed_checks"))
        except (TypeError, ValueError):
            return False
        checks = evidence.get("checks")
        if expected < 0 or observed < expected or not isinstance(checks, dict):
            return False
        if expected and len(checks) < expected:
            return False
    return True


def _terminal_dataset_summary(
    state,
    *,
    request_spec_hash: str = "",
    request_fingerprint: str = "",
) -> dict | None:
    """Return a validated delivery only when it belongs to this request."""
    for item in state.list_artifacts("dataset"):
        summary = _validated_dataset_delivery(
            state,
            item,
            request_spec_hash=request_spec_hash,
            request_fingerprint=request_fingerprint,
        )
        if summary is not None:
            return summary
    return None


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _delivery_path(state, value) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = state.root / path
    try:
        resolved = path.resolve()
        allowed_roots = [Path(state.root).resolve()]
        workspace_root = getattr(state, "workspace_root", None)
        if workspace_root is not None:
            allowed_roots.insert(0, Path(workspace_root).expanduser().resolve())
        if not any(
            resolved == root or resolved.is_relative_to(root)
            for root in allowed_roots
        ):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _validated_dataset_delivery(
    state,
    item: dict,
    *,
    request_spec_hash: str = "",
    request_fingerprint: str = "",
) -> dict | None:
    """Return a summary only for a real, reviewed preprocessing delivery."""
    record = state.read_artifact(item["id"]) or {}
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    payload = _json_object(record.get("content"))

    if metadata.get("artifact_kind") != _DELIVERY_ARTIFACT_KIND:
        return None
    if metadata.get("delivery_complete") is not True:
        return None
    if payload.get("artifact_kind") != _DELIVERY_ARTIFACT_KIND:
        return None
    if payload.get("delivery_complete") is not True:
        return None
    review_profile = str(payload.get("review_profile") or "")
    if review_profile not in {"plan_bound", "request_bound"}:
        return None
    if not str(payload.get("request_id") or "").strip():
        return None
    if len(str(payload.get("request_spec_hash") or "")) != 64:
        return None
    delivery_request = payload.get("preprocessing_request")
    delivery_fingerprint = preprocessing_request_fingerprint(delivery_request)
    if not _same_request_identity(
        actual_hash=str(payload.get("request_spec_hash") or ""),
        actual_fingerprint=delivery_fingerprint,
        expected_hash=request_spec_hash,
        expected_fingerprint=request_fingerprint,
    ):
        return None
    if payload.get("payload_kind") not in {"single_asset", "asset_bundle", "dataset"}:
        return None
    if not isinstance(payload.get("files"), list):
        return None
    receipt = payload.get("review_receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "pass"
        or receipt.get("review_profile") != review_profile
        or receipt.get("request_id") != payload.get("request_id")
        or receipt.get("request_spec_hash") != payload.get("request_spec_hash")
    ):
        return None
    manifest_contract = payload.get("manifest_contract")
    if not isinstance(manifest_contract, dict) or manifest_contract.get("complete") is not True:
        return None
    if not _REQUIRED_MANIFEST_FIELDS.issubset(payload):
        return None
    if not _quality_gates_complete(payload.get("quality_gates"), review_profile):
        return None
    if not isinstance(payload.get("lineage"), list) or not payload["lineage"]:
        return None
    if metadata.get("deliverable_valid") is not True:
        return None
    if str(metadata.get("preprocessing_review_status") or "").lower() != "pass":
        return None
    review = payload.get("preprocessing_review")
    if not isinstance(review, dict) or str(review.get("status") or "").lower() != "pass":
        return None

    package_dir = _delivery_path(state, metadata.get("package_dir") or payload.get("package_dir"))
    manifest_path = _delivery_path(
        state,
        metadata.get("manifest_path") or payload.get("manifest_path"),
    )
    if package_dir is None or not package_dir.is_dir():
        return None
    if manifest_path is None or not manifest_path.is_file():
        return None
    try:
        manifest_path.relative_to(package_dir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or not _REQUIRED_MANIFEST_FIELDS.issubset(manifest):
        return None
    audit_path = package_dir / "audit" / "preprocessing_review.json"
    try:
        audit_receipt = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if audit_receipt != receipt:
        return None
    for file_record in payload["files"]:
        if not isinstance(file_record, dict):
            return None
        raw_path = str(file_record.get("path") or "")
        expected_hash = str(file_record.get("sha256") or "").lower()
        try:
            file_path = (package_dir / raw_path).resolve()
            file_path.relative_to(package_dir.resolve())
        except ValueError:
            return None
        if not file_path.is_file() or len(expected_hash) != 64:
            return None
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != expected_hash:
            return None
    if manifest.get("request_id") != payload.get("request_id"):
        return None
    if manifest.get("request_spec_hash") != payload.get("request_spec_hash"):
        return None
    if manifest.get("review_profile") != review_profile:
        return None
    if not _quality_gates_complete(manifest.get("quality_gates"), review_profile):
        return None
    if not isinstance(manifest.get("lineage"), list) or not manifest["lineage"]:
        return None

    return {
        "artifact_id": item["id"],
        "artifact_type": item.get("type"),
        "name": item.get("name"),
        "status": "success",
        "package_dir": str(package_dir),
        "manifest_path": str(manifest_path),
        "request_spec_hash": str(payload.get("request_spec_hash") or ""),
        "request_fingerprint": delivery_fingerprint,
    }


def _dataset_summary(state) -> dict | None:
    """Find a completed delivery, not merely an artifact named ``dataset``."""
    initial_ids = set(state.hook_state.get(_INITIAL_DATASET_IDS) or [])
    for item in state.list_artifacts("dataset"):
        if item["id"] in initial_ids:
            continue
        summary = _validated_dataset_delivery(state, item)
        if summary is not None:
            return summary
    return None


def _classify_initial_datasets_as_inputs(state) -> list[str]:
    """Keep upstream datasets available without counting them as this run's output."""
    initial_ids = state.hook_state.get(_INITIAL_DATASET_IDS)
    if initial_ids is not None:
        return []
    initial_ids = [item["id"] for item in state.list_artifacts("dataset")]
    state.hook_state[_INITIAL_DATASET_IDS] = initial_ids
    classified: list[str] = []
    for artifact_id in initial_ids:
        record = state.read_artifact(artifact_id)
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        # A completed preprocessing delivery may already exist when the loop
        # is resumed. Keep it as the terminal output instead of reclassifying
        # it as an upstream input.
        if metadata.get("artifact_kind") == _DELIVERY_ARTIFACT_KIND:
            continue
        metadata.update({
            "original_artifact_type": "dataset",
            "artifact_role": "upstream_input",
        })
        record["type"] = "upstream_dataset"
        record["metadata"] = metadata
        path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        classified.append(artifact_id)
    if classified:
        state.append_transcript(
            "data_initial_datasets_classified_as_inputs",
            artifact_ids=classified,
        )
    return classified


def _compact_jsonable(value, limit: int = 2000):
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    if len(text) <= limit:
        return value
    return text[:limit] + "...[truncated]"


def _compact_inspected_inputs(state) -> list[dict]:
    inspections = state.hook_state.get("data_input_inspections")
    if not isinstance(inspections, list):
        return []
    compact: list[dict] = []
    for inspection in inspections[-5:]:
        if not isinstance(inspection, dict):
            continue
        files = []
        for item in (inspection.get("files") or [])[:30]:
            if not isinstance(item, dict):
                continue
            files.append({
                "path": item.get("path"),
                "relative_path": item.get("relative_path"),
                "suffix": item.get("suffix"),
                "size_bytes": item.get("size_bytes"),
                "scientific_asset": item.get("scientific_asset"),
                "preview": _compact_jsonable(item.get("preview") or "", 1200),
            })
        compact.append({
            "path": inspection.get("path"),
            "path_type": inspection.get("path_type"),
            "file_count": inspection.get("file_count"),
            "scientific_asset": inspection.get("scientific_asset"),
            "files": files,
        })
    return compact


def _build_service_task_context(state, messages: list[LLMMessage]) -> dict:
    """Build the bounded caller contract consumed by the Data planning service."""
    user_messages = [
        str(message.content or "")
        for message in messages
        if message.role == "user" and str(message.content or "").strip()
    ]
    node_inputs = state.hook_state.get("node_inputs")
    # Data owns a custom service loop, so it does not consume the intake text
    # injected into ordinary node prompts. Reuse the platform's existing
    # immutable intake anchor here: node_inputs remain execution hints, never
    # a stronger scientific authority than the user's latest substantive request.
    # Resume/retry controls leave that request intact; they are still retained
    # in the intake ledger but cannot replace its geometry with the word "继续".
    intake = load_intake(getattr(state, "project_root", None)) or {}
    amendments = [
        str(item.get("text") or "").strip()
        for item in intake.get("amendments") or []
        if isinstance(item, dict)
        and str(item.get("source") or "user") in {"user", "decision_note"}
        and str(item.get("text") or "").strip()
        and not _is_continuation_only(str(item.get("text") or ""))
    ]
    authoritative_request = (
        amendments[-1]
        if amendments
        else str(intake.get("original_text") or "").strip()
        or (user_messages[-1] if user_messages else "")
    )
    return {
        "service_scope": (
            "Produce and review only the preprocessing assets explicitly requested by the caller. "
            "Do not choose hypotheses, expand scientific scope, or execute formal simulations."
        ),
        "node_inputs": node_inputs if isinstance(node_inputs, dict) else {},
        "user_request": _compact_jsonable(authoritative_request, 12000),
        "inspected_inputs": _compact_inspected_inputs(state),
        "request_source": "caller_or_experiment_node",
    }


def _current_request_identity(state, messages: list[LLMMessage]) -> dict:
    """Normalize the caller request before consulting persisted terminal state."""
    context = _build_service_task_context(state, messages)
    authority_artifacts = [
        item for item in state.list_artifacts()
        if str(item.get("type") or "").lower()
        in {"research_plan", "pre_registration", "preregistration"}
    ]
    has_preregistration = any(
        str(item.get("type") or "").lower() in {"pre_registration", "preregistration"}
        for item in authority_artifacts
    )
    request = normalize_preprocessing_request(
        context,
        source_artifact_ids=[str(item.get("id")) for item in authority_artifacts if item.get("id")],
        authority_kind_hint=(
            "pre_registration"
            if has_preregistration
            else "research_plan"
            if authority_artifacts
            else ""
        ),
    )
    state.hook_state["_data_current_request"] = request
    state.hook_state["_data_current_request_fingerprint"] = preprocessing_request_fingerprint(request)
    return request


def _request_hash_from_state(state) -> str:
    request = state.hook_state.get("_data_current_request")
    return str(request.get("request_spec_hash") or "") if isinstance(request, dict) else ""


def _request_fingerprint_from_state(state) -> str:
    value = state.hook_state.get("_data_current_request_fingerprint")
    if value:
        return str(value)
    request = state.hook_state.get("_data_current_request")
    return preprocessing_request_fingerprint(request)


async def _execute_generation_stage(state, reason: str) -> dict | None:
    status = PlanningStore(state).approval_status()
    if not status.get("approved"):
        return None
    state.append_transcript(
        "data_auto_execute_approved_plan",
        reason=reason,
        plan_id=status.get("approved_plan_id"),
    )
    emit_progress(
        state,
        "auto_execute_plan",
        "executing approved plan without another LLM turn",
        reason=reason,
        plan_id=status.get("approved_plan_id"),
    )
    from nodes.data.tools.execute_preprocessing_plan import execute_preprocessing_plan

    return await execute_preprocessing_plan(
        state,
        plan_id=str(status.get("approved_plan_id") or ""),
        plan_kind="preprocessing_generation",
    )


async def _advance_generation_revision(state, messages, execution: dict | None = None, *, planning_feedback: dict | None = None) -> dict:
    """Keep repairable planning failures inside this service invocation."""
    task_context = _build_service_task_context(state, messages)
    reference_evidence = PlanningStore(state).load_reference_state().get("evidence") or []
    from nodes.data.tools.preprocessing_planner import run_preprocessing_planning_loop

    feedback = planning_feedback
    seen = set()
    execution_scope = str(
        ((execution or {}).get("revision_contract") or {}).get("scope") or ""
    ).strip().lower()
    while True:
        planning = await run_preprocessing_planning_loop(
            state,
            task_context=task_context,
            reference_evidence=reference_evidence,
            execution_feedback=execution,
            planning_feedback=feedback,
        )
        if not isinstance(planning, dict):
            return {"status": "error", "outcome": "fatal", "error": "Planner returned no structured result."}
        state.append_transcript("data_generation_revision_result",
                                status=planning.get("status"), approved=planning.get("approved"))
        outcome = pipeline_outcome(planning)
        if planning.get("approved"):
            store = PlanningStore(state)
            if any(approval.get("approved") and
                   approval.get("approved_plan_id") != (execution or {}).get("plan_id")
                   for approval in (store.approval_status(kind) for kind in
                                    ("preprocessing_generation", "reference_evidence_only"))):
                return planning
            planning = {**planning, "status": "needs_revision", "outcome": "revise_plan", "approved": False,
                        "error": "Planner reported approval without a current approved work order."}
            outcome = pipeline_outcome(planning)
        if outcome["kind"] in {"needs_input", "externally_blocked", "fatal"}:
            return planning
        # An asset/step repair may amend its existing producer route, but it
        # cannot authorize a fresh requirement interpretation. If that focused
        # amendment is not approved, return its diagnostic to the controller
        # instead of repeatedly expanding the same request through Analyst.
        if execution_scope in {"asset", "step"} and outcome["kind"] == "revise_plan":
            return planning
        signature = canonical_hash({
            "revision": (planning.get("revision_contract") or {}).get("signature"),
            "schema": planning.get("last_schema_errors") or planning.get("schema_errors") or [],
            "critique": (planning.get("last_critique") or {}).get("critical_concerns") or [],
            "reason": planning.get("stop_reason") or outcome["reason"],
        })
        if signature in seen:
            return {**planning, "outcome": "fatal",
                    "error": "Data exhausted internal planning recovery without new diagnostic progress.",
                    "last_error": outcome["reason"]}
        seen.add(signature)
        if outcome["kind"] != "retry_step":
            feedback = planning
        emit_progress(state, "planning_repair", "continuing internal recovery with the original request and failed attempt",
                      reason=outcome["kind"])


def _pipeline_result(
    state,
    messages,
    *,
    outcome: str,
    reason: str,
    details: dict | None = None,
) -> LoopResult:
    """The sole owner of Data pipeline terminal and needs-input decisions."""
    details = details if isinstance(details, dict) else {}
    if outcome not in {"needs_input", "externally_blocked", "fatal"}:
        outcome = "fatal"
    state.hook_state["_data_pipeline_outcome"] = outcome
    state.append_transcript("data_pipeline_outcome", outcome=outcome, reason=reason)
    emit_progress(state, "pipeline_outcome", reason, outcome=outcome)
    if outcome == "needs_input":
        resume_contract = (
            details.get("resume_contract")
            if isinstance(details.get("resume_contract"), dict)
            else {}
        )
        input_contract = {
            "question": str(details.get("question") or reason),
            "options": details.get("options") or [],
            "context": details.get("context") or (
                "Supply only the missing authoritative values, then reissue the same preprocessing request."
            ),
            "pipeline_outcome": "needs_input",
            "resume_contract": resume_contract,
        }
        save_preprocessing_blocked_report(
            state,
            name="preprocessing_needs_input",
            payload={
                "status": "needs_input",
                "outcome": "needs_input",
                "reason": reason,
                "input_contract": input_contract,
                "resume_contract": resume_contract,
                "preprocessing_request": state.hook_state.get("_data_current_request") or {},
            },
        )
        return LoopResult(
            final_text=reason,
            turns=0,
            messages=messages,
            # Custom data runs cannot safely use core's generic pause-resume
            # continuation. End this attempt normally with a structured
            # needs_input contract so the caller can reissue an amended request.
            status="completed",
        )

    report_status = "externally_blocked" if outcome == "externally_blocked" else "fatal"
    report = save_preprocessing_blocked_report(
        state,
        name=f"preprocessing_blocked_report__{report_status}",
        payload={
            "status": report_status,
            "outcome": outcome,
            "reason": reason,
            "revision_contract": details.get("revision_contract"),
            "failure_category": details.get("failure_category"),
            "diagnostics": {key: details[key] for key in (
                "last_error", "schema_errors", "last_schema_errors", "last_critique",
                "critic_result", "failed_step", "execution_checkpoint",
            ) if details.get(key)},
            "preprocessing_request": state.hook_state.get("_data_current_request") or {},
        },
    )
    return LoopResult(
        final_text=f"{reason} (report: {report['id']})",
        turns=0,
        messages=messages,
        status="failed",
    )


async def _advance_preprocessing_pipeline(state, messages, reason: str) -> LoopResult | None:
    store = PlanningStore(state)
    execution_retries = set()
    revision_states = {}
    execution_record = state.root / "planning" / "execution_result.json"
    try:
        previous_execution = json.loads(execution_record.read_text(encoding="utf-8")) if execution_record.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous_execution = {}
    while True:
        reference_status = store.approval_status("reference_evidence_only")
        if reference_status.get("approved"):
            from nodes.data.tools.execute_preprocessing_plan import execute_preprocessing_plan

            ledger_before = canonical_hash(store.load_reference_state().get("gaps") or {})
            execution = await execute_preprocessing_plan(
                state,
                plan_id=str(reference_status.get("approved_plan_id") or ""),
                plan_kind="reference_evidence_only",
            )
            previous_execution = execution
            outcome = pipeline_outcome(execution)
            if outcome["kind"] == "completed" and execution.get("status") == "needs_final_generation_plan":
                ledger_after = canonical_hash(store.load_reference_state().get("gaps") or {})
                if ledger_before == ledger_after:
                    return _pipeline_result(
                        state, messages, outcome="fatal",
                        reason="Reference acquisition completed without any ledger progress.",
                        details=execution,
                    )
                reason = "reference_evidence_acquired"
                continue
            store.invalidate_plan(
                "reference_evidence_only",
                str(reference_status.get("approved_plan_id") or ""),
                reason=outcome["kind"],
            )
            if outcome["kind"] in {"revise_plan", "retry_step"}:
                ledger_signature = canonical_hash(store.load_reference_state().get("gaps") or {})
                signature = canonical_hash({
                    "outcome": outcome["kind"],
                    "reason": outcome["reason"],
                    "revision_contract": outcome.get("revision_contract") or {},
                })
                if store.transition_signature_seen(signature, ledger_signature=ledger_signature):
                    return _pipeline_result(
                        state, messages, outcome="fatal",
                        reason="Reference execution repeated the same failure without evidence progress.",
                        details=execution,
                    )
                store.record_transition_signature(
                    signature,
                    plan_id=str(execution.get("plan_id") or ""),
                    status=outcome["kind"],
                    ledger_signature=ledger_signature,
                )
                reason = "reference_execution_revision"
                continue
            return _pipeline_result(
                state, messages,
                outcome=outcome["kind"],
                reason=outcome["reason"] or "Reference acquisition could not continue.",
                details=execution,
            )

        status = store.approval_status()
        previous_plan_record = store.get_plan(str(previous_execution.get("plan_id") or "")) if previous_execution else None
        previous_plan_kind = str(((previous_plan_record or {}).get("plan") or {}).get("plan_kind") or "")
        if (
            previous_plan_kind == "preprocessing_generation"
            and str(previous_execution.get("plan_id") or "")
            and (
                not status.get("approved")
                or str(status.get("approved_plan_id") or "") == str(previous_execution.get("plan_id") or "")
            )
        ):
            prior_outcome = pipeline_outcome(previous_execution)
            if prior_outcome["kind"] == "needs_input":
                return _pipeline_result(
                    state, messages, outcome="needs_input",
                    reason=prior_outcome["reason"] or "The generation step requires authoritative input.",
                    details=previous_execution,
                )
            if prior_outcome["kind"] in {"revise_asset", "revise_plan"}:
                if str(previous_execution.get("status") or "") == "needs_reference_search":
                    ledger_signature = canonical_hash(store.load_reference_state().get("gaps") or {})
                    signature = canonical_hash({
                        "outcome": prior_outcome["kind"],
                        "revision_contract": prior_outcome.get("revision_contract") or {},
                        "transition": previous_execution.get("transition") or {},
                    })
                    if store.transition_signature_seen(signature, ledger_signature=ledger_signature):
                        return _pipeline_result(
                            state, messages, outcome="fatal",
                            reason="The same reference transition recurred without evidence progress.",
                            details=previous_execution,
                        )
                    store.record_transition_signature(
                        signature,
                        plan_id=str(previous_execution.get("plan_id") or ""),
                        status=prior_outcome["kind"],
                        ledger_signature=ledger_signature,
                    )
                    from nodes.data.tools.preprocessing_planner import reference_plan_from_generation_transition

                    transition = reference_plan_from_generation_transition(
                        state,
                        (previous_plan_record or {}).get("plan") or {},
                        previous_execution,
                    )
                    if isinstance(transition, dict) and transition.get("approved"):
                        reason = "generation_transition_to_reference"
                        continue
                    previous_execution = {
                        **previous_execution,
                        "status": "needs_revision",
                        "revision_contract": transition.get("revision_contract") if isinstance(transition, dict) else prior_outcome.get("revision_contract"),
                    }
                signature = ((previous_execution.get("publication") or {}).get("review_progress") or {}).get("signature") or str(
                    (prior_outcome.get("revision_contract") or {}).get("signature") or ""
                )
                repeated = revision_states.get(signature, 0)
                if repeated >= 2:
                    return _pipeline_result(state, messages, outcome="fatal",
                        reason="Neither targeted repair nor requirement reconciliation resolved the same findings.",
                        details=previous_execution)
                revision_states[signature] = repeated + 1
                planning = await _advance_generation_revision(state, messages, previous_execution,
                    planning_feedback={"status": "needs_revision", "stop_reason": "generation_revision_no_progress",
                                       "revision_contract": prior_outcome.get("revision_contract")}
                    if repeated else None)
                if planning.get("approved"):
                    approved_id = str(store.approval_status().get("approved_plan_id") or "")
                    if approved_id and approved_id != str(previous_execution.get("plan_id") or ""):
                        reason = "generation_revision_approved"
                        continue
                planned_outcome = pipeline_outcome(planning)
                return _pipeline_result(
                    state, messages,
                    outcome=(
                        planned_outcome["kind"]
                        if planned_outcome["kind"] in {"needs_input", "externally_blocked", "fatal"}
                        else "fatal"
                    ),
                    reason=planned_outcome["reason"] or "Generation revision made no effective progress.",
                    details=planning,
                )
            if prior_outcome["kind"] == "retry_step" and status.get("approved"):
                signature = canonical_hash({"plan": previous_execution.get("plan_id"),
                                            "reason": prior_outcome["reason"]})
                if signature not in execution_retries:
                    execution_retries.add(signature)
                    # The executor checkpoint retains successful assets and
                    # retries only the failed step or publication/review.
                    reason = "retry_failed_execution_or_review"
                else:
                    return _pipeline_result(
                        state, messages, outcome="fatal",
                        reason=(
                            "Internal execution/review retry made no diagnostic progress: "
                            + prior_outcome["reason"]
                        ), details=previous_execution,
                    )
            elif prior_outcome["kind"] in {"externally_blocked", "fatal", "retry_step"}:
                return _pipeline_result(
                    state, messages,
                    outcome="fatal" if prior_outcome["kind"] == "retry_step" else prior_outcome["kind"],
                    reason=prior_outcome["reason"] or "Generation execution could not continue.",
                    details=previous_execution,
                )

        if not status.get("approved"):
            task_context = _build_service_task_context(state, messages)
            reference_evidence = store.load_reference_state().get("evidence") or []
            state.append_transcript(
                "data_service_planning",
                reason=reason,
                inspected_input_count=len(task_context.get("inspected_inputs") or []),
                reference_evidence_count=len(reference_evidence or []),
            )
            emit_progress(
                state,
                "auto_planning",
                "running preprocessing planning loop without another main-model turn",
                reason=reason,
            )
            planning = await _advance_generation_revision(state, messages)
            state.append_transcript(
                "data_service_planning_result",
                reason=reason,
                status=planning.get("status") if isinstance(planning, dict) else None,
                approved=planning.get("approved") if isinstance(planning, dict) else None,
                best_plan_id=planning.get("best_plan_id") if isinstance(planning, dict) else None,
            )
            emit_progress(
                state,
                "auto_planning_done",
                str(planning.get("status") if isinstance(planning, dict) else "unknown"),
                approved=planning.get("approved") if isinstance(planning, dict) else None,
            )
            if isinstance(planning, dict) and planning.get("approved"):
                reason = "planning_approved"
                continue
            planned_outcome = pipeline_outcome(planning)
            return _pipeline_result(
                state, messages,
                outcome=(
                    planned_outcome["kind"]
                    if planned_outcome["kind"] in {"needs_input", "externally_blocked", "fatal"}
                    else "fatal"
                ),
                reason=planned_outcome["reason"] or "Planning made no effective progress.",
                details=planning,
            )

        execution = await _execute_generation_stage(state, reason)
        if execution is None:
            return _pipeline_result(
                state, messages, outcome="fatal",
                reason="No approved generation plan was available to execute.",
            )
        previous_execution = execution
        dataset = _dataset_summary(state)
        if dataset is not None:
            state.append_transcript("data_auto_execute_dataset_saved", **dataset)
            emit_progress(state, "dataset_found", "dataset artifact saved by approved plan")
            return LoopResult(
                final_text=f"Executed approved preprocessing plan; dataset artifact {dataset['artifact_id']} was saved.",
                turns=0,
                messages=messages,
                status="completed",
            )
        outcome = pipeline_outcome(execution)
        if outcome["kind"] in {"revise_asset", "revise_plan", "retry_step"}:
            # All execution transitions re-enter the same checkpoint-aware
            # branch above, including reference discovery and review retries.
            reason = "generation_execution_revision"
            continue
        if outcome["kind"] == "completed":
            return _pipeline_result(
                state, messages, outcome="fatal",
                reason="Execution completed without a reviewed dataset delivery.",
                details=execution,
            )
        return _pipeline_result(
            state, messages,
            outcome="fatal" if outcome["kind"] == "retry_step" else outcome["kind"],
            reason=outcome["reason"] or "Generation execution could not continue.",
            details=execution,
        )


async def _run_loop_impl(harness, state, messages, llm):
    """Run the Data service directly, without a generic conversational-agent loop."""
    del harness
    _classify_initial_datasets_as_inputs(state)
    state.hook_state["_data_planning_llm_client"] = llm
    emit_progress(state, "start", "data preprocessing service entered")
    try:
        _current_request_identity(state, messages)
        terminal = _terminal_dataset_summary(
            state,
            request_spec_hash=_request_hash_from_state(state),
            request_fingerprint=_request_fingerprint_from_state(state),
        )
        if terminal is not None:
            state.append_transcript("data_terminal_dataset_already_present", **terminal)
            emit_progress(state, "terminal_dataset", terminal["artifact_id"], status=terminal["status"])
            return LoopResult(
                final_text=(
                    f"Data node reused validated dataset delivery {terminal['artifact_id']} "
                    f"with status={terminal['status']}."
                ),
                turns=0,
                messages=messages,
                status="completed",
            )

        state.append_transcript(
            "data_service_dispatch",
            request_source="caller_or_experiment_node",
            approved_plan=PlanningStore(state).approval_status().get("approved"),
        )
        result = await _advance_preprocessing_pipeline(
            state,
            messages,
            reason="caller_scoped_service_request",
        )
        if result is not None:
            return result
        return _pipeline_result(
            state,
            messages,
            outcome="fatal",
            reason="Data preprocessing ended without a structured pipeline result.",
        )
    finally:
        state.hook_state.pop("_data_planning_llm_client", None)


async def run_loop(harness, state, messages, llm):
    """Framework entry point.

    控制器自己拥有每一条终止路径，但**正因为如此**结构文件校验必须挂在这里：
    `_run_loop_impl` 有多条早退（blocked / fatal / 提前 return），框架的
    on_end 覆盖不全，而 `_reviewer` 的硬红线（harness.yaml:
    「含 structure_file_validation 且 n_invalid ≥ 1 → critical + 必 revise」）
    以这条事件为唯一依据 —— 事件没人发，那条红线就不是不红，是永远不触发。

    `review/package_reviewer.py` 的 `_scientific_asset_review` 是同一个校验器
    的另一个挂点，但它只扫**已声明发布**的 package_dir；本兜底扫整个 run 根目录，
    两者不重复，缺口不同。
    """
    try:
        return await _run_loop_impl(harness, state, messages, llm)
    finally:
        try:
            from nodes.data.hooks import validate_structure_outputs

            validate_structure_outputs(state)
        except Exception:                                     # noqa: BLE001
            # 兜底本身绝不能把一轮正常结束的 run 变成异常。
            log.warning("structure_file_validation 兜底失败", exc_info=True)
