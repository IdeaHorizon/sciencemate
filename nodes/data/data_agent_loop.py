"""Direct service driver for caller-scoped data preprocessing."""
from __future__ import annotations

import json
import hashlib
import re
from copy import copy, deepcopy
from pathlib import Path
from types import SimpleNamespace

from core.agent_loop import LoopResult
from core.task_outcome import record_task_outcome
from core.llm import LLMClient, LLMMessage, is_framework_notice
from core.research_intake import load_intake
from core.conversation_store import jsonable_to_msg, load_conversation
from core.artifact_provenance import hash_file

from nodes.data.blocked_delivery import save_preprocessing_blocked_report
from nodes.data.delivery_contract import structured_request
from nodes.data.pipeline_contract import pipeline_outcome, revision_progress, RESUMABLE_WRITER_FAILURES
from nodes.data.planning.request_contract import (
    caller_request_text,
    normalize_preprocessing_request,
    preprocessing_request_fingerprint,
    preprocessing_recovery_fingerprint,
)
from nodes.data.planning.store import PlanningStore, canonical_hash
from nodes.data.progress import emit_progress
from nodes.data.model_client import data_model_client
from nodes.data.review.gate_registry import quality_gates_complete as _quality_gates_complete


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


class _DeliveryReviewBlocked(Exception):
    def __init__(self, result: dict, review_artifact_id: str):
        super().__init__(result.get("summary") or "Existing delivery review could not complete.")
        self.result = result
        self.review_artifact_id = review_artifact_id


def _delivery_review_record(
    state, payload: dict, request_fingerprint: str
) -> tuple[str, str, dict]:
    identity = canonical_hash(
        {
            "files": sorted(
                ({"path": item["path"], "sha256": item["sha256"]} for item in payload["files"]),
                key=lambda item: item["path"],
            ),
            "request_fingerprint": request_fingerprint,
        }
    )
    name = "delivery_recheck_" + identity[:24]
    previous = state.read_artifact("preprocessing_review__" + name) or {}
    saved = _json_object(previous.get("content"))
    return identity, name, saved if saved.get("identity") == identity else {}


async def _review_delivery_feedback(state, summary: dict, record: dict, plan: dict) -> dict | None:
    """Check caller findings against published bytes; retain one review budget.

    No generation occurs here. A confirmed defect is returned to the existing
    pipeline; prose requests to change the old spec do not enter this path.
    """
    inputs = state.hook_state.get("node_inputs")
    if not isinstance(inputs, dict):
        inputs = state.hook_state["node_inputs"] = {}
    feedback = inputs.get("review_feedback") or []
    payload = _json_object(record.get("content"))
    identity, name, saved = _delivery_review_record(state, payload, summary["request_fingerprint"])
    if not feedback and not saved:
        return None
    # An unresolved finding does not disappear when the caller omits it.
    if not feedback:
        if (saved.get("review") or {}).get("status") == "pass":
            return saved["review"]
        feedback = saved.get("feedback") or []
    feedback = deepcopy(feedback if isinstance(feedback, list) else [feedback])
    inputs["review_feedback"] = feedback
    if saved.get("review_state"):
        state.hook_state["_data_authority_review"] = deepcopy(saved["review_state"])
    elif not state.hook_state.get("_data_authority_review"):
        source = (
            Path(state.root).parent
            / record["produced_by_run_id"]
            / "planning/execution_checkpoint.json"
        )
        try:
            checkpoint = json.loads(source.read_text())
        except (OSError, ValueError):
            checkpoint = {}
        if checkpoint.get("authority_review_state"):
            state.hook_state["_data_authority_review"] = checkpoint["authority_review_state"]
    from nodes.data.review.authority_reviewer import review_delivery_against_authority

    result = {
        "status": "error",
        "summary": "Delivery review interrupted; preserve the published assets.",
    }
    try:
        result = await review_delivery_against_authority(
            state,
            files={
                item["path"]: {"size_bytes": item.get("size", item.get("size_bytes", 0))}
                for item in payload["files"]
            },
            # Findings are context, not a new authority or a new review budget.
            preprocessing_request=plan["preprocessing_request"],
            work_order=plan.get("preprocessing_work_order") or {},
            plan_context={**plan, "delivery_destination": summary["package_dir"]},
            # _validated_dataset_delivery already checked the complete byte bindings.
            # This check claims integrity only, not new domain validation.
            deterministic_review={
                "status": "pass",
                "common_checks": {"published_byte_bindings": True},
                "issues": [],
            },
            package_dir=summary["package_dir"],
        )
    finally:
        review_artifact = state.save_artifact(
            "preprocessing_review",
            name,
            json.dumps(
                {
                    "identity": identity,
                    "delivery_artifact_id": summary["artifact_id"],
                    "feedback": feedback,
                    "review": result,
                    "review_state": state.hook_state.get("_data_authority_review") or {},
                },
                ensure_ascii=False,
                default=str,
            ),
            metadata={
                "delivery_artifact_id": summary["artifact_id"],
                "review_status": result.get("status"),
            },
        )
    state.append_transcript(
        "data_delivery_feedback_reviewed",
        artifact_id=summary["artifact_id"],
        review_artifact_id=review_artifact["id"],
        status=result.get("status"),
    )
    if result.get("status") not in {"pass", "fail"}:
        raise _DeliveryReviewBlocked(result, review_artifact["id"])
    if result.get("status") == "fail":
        inputs["review_feedback"] = deepcopy(result.get("issues") or [])
        state.hook_state["_data_confirmed_delivery_defects"] = {
            "review_artifact_id": review_artifact["id"],
            "revision_contract": result.get("revision_contract"),
            "existing_delivery": {
                "artifact_id": summary["artifact_id"],
                "package_dir": summary["package_dir"],
                "source_run_id": record["produced_by_run_id"],
            },
            "instruction": "Repair the verified findings using the existing delivery and producer plan. "
            "Preserve valid independent assets and the original authority; do not regenerate "
            "everything merely because this dispatch has a new run ID.",
        }
    return result


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


async def _terminal_dataset_summary(
    state,
    *,
    request_spec_hash: str = "",
    request_fingerprint: str = "",
) -> dict | None:
    """Return a validated delivery only when it belongs to this request."""
    for item in reversed(
        [*state.list_artifacts("dataset"), *state.list_artifacts("preprocessing_document")]
    ):
        summary = _validated_dataset_delivery(
            state,
            item,
            request_spec_hash=request_spec_hash,
            request_fingerprint=request_fingerprint,
            current_run_only=False,
            allow_review_recovery=True,
        )
        if summary is not None:
            record = state.read_artifact(item["id"]) or {}
            plan = (
                _unchanged_source_plan(state, record)
                if summary["delivery_kind"] == "dataset_materialized"
                else None
            )
            if (
                plan is None
                and summary["delivery_kind"] == "dataset_materialized"
                and (state.hook_state.get("node_inputs") or {}).get("review_feedback")
            ):
                raise ValueError(
                    "The original source plan/evidence is unavailable or changed. Preserve the published "
                    "delivery and restore that evidence before reviewing the caller's findings."
                )
            if record.get("produced_by_run_id") != state.run_id and plan is None:
                # Partial deliveries or changed sources do not suppress a
                # legitimate attempt to materialize the requested assets.
                continue
            if plan is not None:
                review = await _review_delivery_feedback(state, summary, record, plan)
                if review is not None:
                    if review.get("status") == "fail":
                        return None  # existing planning/execution consumes verified repair findings
                    summary["verification_summary"] = "\n".join(
                        [
                            str(review.get("summary") or ""),
                            *(f"Delivery note: {note}" for note in review.get("warnings") or []),
                        ]
                    )
            if record.get("produced_by_run_id") != state.run_id:
                summary["reused_from_run_id"] = record["produced_by_run_id"]
                from nodes.data.review.namelist_inspection import (
                    mpas_atmosphere_schema,
                    inspect_mpas_configuration,
                )

                software = (
                    plan.get("simulation_software")
                    or (plan.get("requirement_analysis") or {}).get("target_software")
                    or {}
                )
                files = _json_object(record.get("content"))["files"]
                schema = await mpas_atmosphere_schema(
                    state, software, [item["path"] for item in files]
                )
                if schema:
                    checks = [
                        inspect_mpas_configuration(
                            (Path(summary["package_dir"]) / item["path"]).read_text(),
                            item["path"],
                            schema,
                        )
                        for item in files
                        if Path(item["path"]).name in {"namelist.atmosphere", "streams.atmosphere"}
                    ]
                    issues = [issue for check in checks for issue in check["issues"]]
                    state.append_transcript(
                        "data_delivery_native_recheck", artifact_id=item["id"], checks=checks
                    )
                    if issues:
                        inputs = state.hook_state.setdefault("node_inputs", {})
                        feedback = inputs.get("review_feedback") or []
                        inputs["review_feedback"] = (feedback if isinstance(feedback, list) else [feedback]) + issues
                        continue
                    summary["verification_summary"] = (
                        "MPAS 配置已按当前校验器重新检查通过。Fortran 组名和字段名不区分大小写；"
                        "Registry 的 in_defaults 不能证明运行时必填，不能据此要求重生成。"
                        "应按读取器的默认值初始化与 iostat 分支判断：" + schema["namelist_reader"]["reference_url"])
                    summary["review_summary"] = (json.dumps(schema["namelist_reader"], ensure_ascii=False)
                                                   + "\n" + summary["review_summary"])
            return summary
    return None


def _unchanged_source_plan(state, record: dict) -> dict | None:
    """Reuse the producer's existing source inventory and review hashes."""
    run_id = str(record.get("produced_by_run_id") or "")
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        return None
    planning = Path(state.root).parent / run_id / "planning"
    try:
        plan = json.loads((planning / "approved_plan.json").read_text())["plan"]
        checkpoint = json.loads((planning / "execution_checkpoint.json").read_text())
        if (checkpoint.get("plan_hash") != canonical_hash(plan)
                or (plan.get("preprocessing_request") or {}).get("request_spec_hash") != _json_object(record.get("content")).get("request_spec_hash")):
            return None
        hashes = (checkpoint.get("authority_review_state") or {}).get("fingerprints") or {}
        for asset in (plan.get("requirement_analysis") or {}).get("required_files") or []:
            match = asset.get("local_match") or {}
            source = match.get("path") or match.get("saved_path")
            if source and (not hashes.get(source) or hash_file(Path(source))[0] != hashes[source]):
                return None
        return plan
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
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
    current_run_only: bool = True,
    allow_review_recovery: bool = False,
) -> dict | None:
    """Return a summary only for a real, reviewed preprocessing delivery."""
    record = state.read_artifact(item["id"]) or {}
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    payload = _json_object(record.get("content"))

    if metadata.get("artifact_kind") != _DELIVERY_ARTIFACT_KIND:
        return None
    if payload.get("artifact_kind") != _DELIVERY_ARTIFACT_KIND:
        return None
    kind = payload.get("delivery_kind")
    if kind not in {"dataset_materialized", "partial_dataset", "schema_only", "acquisition_plan"}:
        return None
    expected_type = "dataset" if kind == "dataset_materialized" else "preprocessing_document"
    if record.get("type") != expected_type or (current_run_only and record.get("produced_by_run_id") != state.run_id):
        return None
    if payload.get("delivery_complete") is not (kind == "dataset_materialized"):
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
    if not isinstance(payload.get("files"), list) or not payload["files"]:
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
        file_bytes = file_path.read_bytes()
        if hashlib.sha256(file_bytes).hexdigest() != expected_hash:
            return None
        if file_path.name.upper() == "KPOINTS" or file_path.suffix.lower() == ".kpoints":
            from nodes.data.review.vasp_inspection import validate_kpoints_header

            if validate_kpoints_header(file_bytes.decode("utf-8", errors="replace")):
                state.append_transcript("data_delivery_recheck_failed", artifact_id=item["id"],
                                        file=raw_path, code="invalid_kpoints_header")
                return None
    if manifest.get("delivery_kind") != kind:
        return None
    for exported in payload.get("workspace_exports") or []:
        path = Path(str(exported.get("path") or ""))
        workspace = getattr(state, "workspace_root", None)
        if (not workspace or not path.resolve().is_relative_to(Path(workspace).resolve())
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != exported.get("sha256")):
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

    plan = _unchanged_source_plan(state, record) if kind == "dataset_materialized" else None
    if plan:
        from nodes.data.review.package_reviewer import _required_asset_review

        checked = _required_asset_review({entry["path"]: "materialized" for entry in payload["files"]},
                                         {**plan, "delivery_files": payload["files"]})
        if checked["issues"]:
            state.append_transcript("data_delivery_recheck_failed", artifact_id=item["id"], issues=checked["issues"])
            return None

    # A later verified defect supersedes the old passing receipt for these
    # bytes. Recovery may inspect them; completion/freezing may not claim them
    # as valid again merely because execution returned without a new delivery.
    _, _, recheck = _delivery_review_record(state, payload, delivery_fingerprint)
    if not allow_review_recovery and (recheck.get("review") or {}).get("status") == "fail":
        return None

    return {
        "artifact_id": item["id"],
        "artifact_type": item.get("type"),
        "name": item.get("name"),
        "status": "success",
        "delivery_kind": kind,
        "simulation_ready": payload.get("simulation_ready") is True,
        "package_dir": str(package_dir),
        "manifest_path": str(manifest_path),
        "request_spec_hash": str(payload.get("request_spec_hash") or ""),
        "request_fingerprint": delivery_fingerprint,
        "request_authority": {
            "authority": deepcopy((delivery_request or {}).get("authority") or {}),
            "snapshot_sha256": canonical_hash(
                (delivery_request or {}).get("authority_snapshot") or {}
            ),
            "original_request_excerpt": caller_request_text(
                (delivery_request or {}).get("authority_snapshot") or {}
            )[:2000],
            "excerpt_truncated": len(
                caller_request_text((delivery_request or {}).get("authority_snapshot") or {})
            )
            > 2000,
        },
        "review_summary": "\n".join(
            [
                *(
                    f"Exported file: {item['path']}"
                    for item in payload.get("workspace_exports") or []
                ),
                # run_node exposes only the first 500 characters. Keep the scope
                # ahead of paths, otherwise a mesh delivery looks like a solver case.
                (
                    "Simulation readiness is declared in the manifest."
                    if payload.get("simulation_ready") is True
                    else "Requested preprocessing assets are delivered; a complete runnable simulation is not claimed. "
                    "Unrequested solver configuration or execution is not a missing preprocessing deliverable."
                ),
                "Review: audit/preprocessing_review.json and its cited native evidence. "
                "Check those sources before inferring a missing validation or requesting regeneration.",
                f"Published delivery directory: {package_dir}",
                f"Manifest: {manifest_path}",
                str((receipt.get("authority_review") or {}).get("summary") or ""),
                *(
                    f"Delivery note: {note}"
                    for note in (receipt.get("authority_review") or {}).get("warnings") or []
                ),
            ]
        ).strip(),
    }


def _dataset_summary(state) -> dict | None:
    """Find a completed delivery, not merely an artifact named ``dataset``."""
    for item in [*state.list_artifacts("dataset"), *state.list_artifacts("preprocessing_document")]:
        record = state.read_artifact(item["id"]) or {}
        if record.get("produced_by_run_id") != state.run_id:
            continue
        summary = _validated_dataset_delivery(state, item)
        if summary is not None:
            return summary
    return None


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


def _parent_request_messages(state, parent_id: str) -> list[LLMMessage]:
    """Read the caller's messages as they stood at this child's dispatch."""
    if not parent_id or parent_id in {".", ".."} or Path(parent_id).name != parent_id:
        return []
    parent = SimpleNamespace(root=Path(state.root).parent / parent_id, hook_state={})
    dispatch_id = str(getattr(state, "parent_dispatch_id", None) or "")
    if not dispatch_id:
        return load_conversation(parent) or []
    # The first synchronous child starts before conversation/checkpoint is
    # saved. loop_seed is already durable, and the matching dispatch bounds it
    # so retries cannot adopt a later task from this same parent conversation.
    seed = []
    try:
        with (parent.root / "transcript.jsonl").open(encoding="utf-8") as transcript:
            for line in transcript:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "loop_seed":
                    seed = event.get("messages") or []
                elif (event.get("event") == "subagent_call_start"
                      and event.get("dispatch_id") == dispatch_id):
                    return [jsonable_to_msg(item) for item in seed
                            if isinstance(item, dict) and "role" in item] if isinstance(seed, list) else []
    except OSError:
        pass
    # A missing dispatch is not evidence that the latest saved task is ours.
    return []


def _request_clarifications(messages: list[LLMMessage], request: str, parent_id: str) -> list[dict]:
    """Read runtime human replies, including confirmations on ordinary tools."""
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if messages[i].role == "user" and str(messages[i].content or "").strip() == request.strip()), None)
    if start is None:
        return []
    questions, answers, calls = {}, [], {}
    for message in messages[start + 1:]:
        if (message.role == "user" and str(message.content or "").strip()
                and not is_framework_notice(message)
                and not _is_continuation_only(str(message.content))):
            # Never carry a previous task's decision into a later request.
            return []
        if message.role == "assistant":
            for call in message.tool_calls or []:
                function = call.get("function") or {}
                if call.get("id"):
                    calls[call["id"]] = function.get("name")
                if function.get("name") == "request_human_input":
                    try:
                        arguments = function.get("arguments") or {}
                        questions[call["id"]] = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except (ValueError, KeyError):
                        continue
        question = questions.get(message.tool_call_id)
        if message.role != "tool" or message.tool_call_id not in calls:
            continue
        try:
            result = json.loads(message.content or "{}")
        except ValueError:
            continue
        if not isinstance(result, dict) or result.get("status") != "success":
            continue
        # resume_loop replaces the paused tool result with this human-answer
        # envelope, even when the paused tool was not request_human_input.
        # Ordinary command output or text quoted in notices is not authority.
        if not isinstance(question, dict):
            if not (result.get("asked_by") and isinstance(result.get("response"), str)
                    and set(result) <= {"status", "response", "asked_by", "bypassed"}):
                continue
            question = {"question": f"Runtime confirmation for {calls[message.tool_call_id]}"}
        answer = str(result.get("response") or "").strip()
        options = question.get("options") or []
        chosen = None
        if answer.isdecimal() and options:
            index = int(answer) - 1  # CLI replies are one-based, unlike recommended_option_index.
            if not 0 <= index < len(options):
                continue
            chosen = options[index]
            answer = str(chosen.get("label") or "") if isinstance(chosen, dict) else str(chosen)
        if answer:
            answers.append({"question": question.get("question"), "answer": answer,
                            **({"selected_option": chosen} if chosen is not None else {}),
                            "source_run_id": parent_id, "tool_call_id": message.tool_call_id,
                            "bypassed": bool(result.get("bypassed"))})
    return answers


def _build_service_task_context(state, messages: list[LLMMessage], *, use_bound_request: bool = True) -> dict:
    """Build the bounded caller contract consumed by the Data planning service."""
    user_messages = [
        str(message.content or "")
        for message in messages
        if message.role == "user" and not is_framework_notice(message) and str(message.content or "").strip()
    ]
    node_inputs = state.hook_state.get("node_inputs")
    structured = structured_request(state)
    dispatch_context = {"dispatch_proposal_unverified": structured,
                        **{key: structured[key] for key in (
                            "runtime_environment_ref", "review_feedback", "recovery_feedback")
                           if structured and key in structured}}
    bound = state.hook_state.get("_data_current_request")
    if use_bound_request and isinstance(bound, dict):
        # Internal repair belongs to this invocation. Do not re-read a mutable
        # project intake while another task/session may be changing it.
        return {
            "preprocessing_request": deepcopy(bound),
            "user_request": caller_request_text(bound, preprocessing_request=bound),
            "node_inputs": (deepcopy(dispatch_context)
                            if structured and (bound.get("authority") or {}).get("kind") == "user_request"
                            and set(bound.get("authority_snapshot") or {}) == {"request"}
                            else deepcopy(node_inputs) if isinstance(node_inputs, dict) else {}),
            "inspected_inputs": _compact_inspected_inputs(state),
            "request_source": "bound_data_request",
        }
    # Data owns a custom service loop, so it does not consume the intake text
    # injected into ordinary node prompts. Reuse the platform's existing
    # immutable intake anchor here: node_inputs remain execution hints, never
    # a stronger scientific authority than the user's latest substantive request.
    # Resume/retry controls leave that request intact; they are still retained
    # in the intake ledger but cannot replace its geometry with the word "继续".
    intake = load_intake(getattr(state, "project_root", None)) or {}
    entries = [
        item for item in [
            {**intake, "text": intake.get("original_text")},
            *(intake.get("amendments") or []),
        ]
        if isinstance(item, dict)
        and str(item.get("source") or "user") in {"user", "decision_note"}
        and str(item.get("text") or "").strip()
        and not _is_continuation_only(str(item.get("text") or ""))
    ]
    parent_id = str(getattr(state, "parent_run_id", None) or "")
    parent_messages = _parent_request_messages(state, parent_id)
    recorded_texts = {str(item["text"]).strip() for item in entries}
    # Intake deduplicates earlier text: A -> B -> A does not append A again.
    # Use this caller's chronological messages, not the project's last amendment.
    authoritative_request = next((str(m.content).strip() for m in reversed(parent_messages)
                                  if m.role == "user" and not is_framework_notice(m)
                                  and str(m.content or "").strip() in recorded_texts), "")
    if structured is not None:
        source_authority = _json_object(_json_object(structured.get("preprocessing_request")).get("authority"))
        bound_plan = structured.get("source_prereg_artifact_id") or (
            source_authority.get("artifact_id")
            if source_authority.get("kind") in {"pre_registration", "research_plan"} else None)
        if not authoritative_request or bound_plan:
            # Experiment/service contracts without a directly bound human
            # request, and explicit plan bindings validated by the request
            # normalizer, retain their structured authority.
            return {
                "service_scope": "Produce only the assets in this immutable structured caller request.",
                "node_inputs": structured,
                "user_request": json.dumps(structured, ensure_ascii=False, sort_keys=True),
                "inspected_inputs": _compact_inspected_inputs(state),
                "request_source": "structured_data_request",
            }
        # JSON serialization cannot promote a dispatcher's substitute source or
        # added deliverables above the human request bound to this dispatch.
        node_inputs = dispatch_context
    if not authoritative_request:
        session = str(getattr(state, "session_id", None) or "")
        scoped = [item for item in entries if session and str(item.get("session_id") or "") == session]
        authoritative_request = (str(scoped[-1]["text"]).strip() if scoped else
                                 caller_request_text(node_inputs or {}) or (user_messages[-1] if user_messages else ""))
    clarifications = _request_clarifications(parent_messages, authoritative_request, parent_id)
    if clarifications:
        authoritative_request += (
            "\n\nSubsequent human replies, in chronological order (these are user instructions, "
            "not caller paraphrases): a reply may answer the question OR replace the task. "
            "If it requests a new task, that task supersedes the earlier request; do not merge "
            "the old task's geometry, parameters or deliverables into it. Otherwise preserve "
            "the unchanged requirements. An operational approval alone does not change the task.\n"
            + json.dumps(clarifications, ensure_ascii=False)
        )
    return {
        "service_scope": (
            "Produce and review only the preprocessing assets explicitly requested by the caller. "
            "Do not choose hypotheses, expand scientific scope, or execute formal simulations."
        ),
        "node_inputs": node_inputs if isinstance(node_inputs, dict) else {},
        "user_request": authoritative_request,
        "inspected_inputs": _compact_inspected_inputs(state),
        "request_source": "caller_or_experiment_node",
    }


def _current_request_identity(state, messages: list[LLMMessage]) -> dict:
    """Normalize the caller request before consulting persisted terminal state."""
    context = _build_service_task_context(state, messages, use_bound_request=False)
    # A project's available artifacts are not implicit authorization to turn a
    # new service request into a plan-bound task. Explicit references normalize here.
    request = normalize_preprocessing_request(context, state=state)
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


async def _execute_generation_stage(state, reason: str, *, resume_target: str = "") -> dict | None:
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
        **({"resume_target": resume_target} if resume_target else {}),
    )


async def _advance_generation_revision(state, messages, execution: dict | None = None, *, planning_feedback: dict | None = None) -> dict:
    """Keep repairable planning failures inside this service invocation."""
    if execution and pipeline_outcome(execution)["kind"] == "revise_asset":
        # The executor already owns content repair and dependent rebuilding.
        # A failed local repair cannot acquire plan-edit authority here.
        return {**execution, "approved": False, "outcome": "fatal",
                "error": execution.get("error") or "Local repair did not resolve the cited defects; the approved plan is preserved."}
    task_context = _build_service_task_context(state, messages)
    reference_evidence = PlanningStore(state).load_reference_state().get("evidence") or []
    from nodes.data.tools.preprocessing_planner import run_preprocessing_planning_loop

    feedback = planning_feedback
    if feedback is None and state.hook_state.get("_data_confirmed_delivery_defects"):
        feedback = {
            "status": "needs_revision",
            **deepcopy(state.hook_state["_data_confirmed_delivery_defects"]),
        }
    seen = set()
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
        if planning.get("stop_reason") in {"repeated_schema_failure", "no_effective_progress"}:
            # The planner already attempted correction with exact diagnostics.
            # Re-entering it resets its progress guard and repeats the same DAG.
            return {**planning, "outcome": "fatal",
                    "failure_category": "planning_no_progress",
                    "error": "Data planning correction made no progress. Report the cited contract errors; do not automatically redispatch the unchanged task or rewrite its frozen requirements. Correct the planning defect before retrying."}
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
    task_outcome = record_task_outcome(
        state, "failed" if outcome == "fatal" else "blocked",
        detail=reason, reported_by="data.pipeline",
    )
    task_outcome["failure_category"] = details.get("failure_category")
    for key in ("retry_target", "resume_contract", "revision_contract"):
        if details.get(key) is not None:
            task_outcome[key] = deepcopy(details[key])
    if details.get("retry_target") == "authority_review":
        task_outcome.update(dispatch_action="resolve_review_blocker", redispatch_required=False)
    if details.get("failure_category") == "saved_stage_available":
        task_outcome.update(dispatch_action=details.get("dispatch_action", "resume_saved_stage"), redispatch_required=False,
                            resume_run_id=(details.get("resume_contract") or {}).get("resume_run_id"))
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
                "Supply the missing authoritative values to the caller for a controlled new attempt; do not replay the old dispatch."
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
                "missing_inputs": details.get("missing_fields") or details.get("required_fields") or details.get("missing_inputs") or [],
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
            "retry_target": details.get("retry_target"),
            "missing_inputs": details.get("missing_fields") or details.get("required_fields") or details.get("missing_inputs") or [],
            "resume_contract": details.get("resume_contract"),
            "diagnostics": {key: details[key] for key in (
                "last_error", "schema_errors", "last_schema_errors", "last_critique",
                "critic_result", "failed_step", "failed_tool", "error", "failure_details", "execution_checkpoint", "retry_target", "reference_gap",
            ) if details.get(key)},
            "preprocessing_request": state.hook_state.get("_data_current_request") or {},
        },
    )
    task_outcome["recovery_artifact_id"] = report["id"]
    return LoopResult(
        final_text=(f"{reason} (report: {report['id']})" + (
            "\n这是环境准备交接，不是前处理任务不可行。请读取报告中的 resume_contract，"
            "通过 run_node 调用 handoff_request 指定的 Experiment 环境准备任务；"
            "确认 Data 的实际执行环境验证通过后，由调用方按 resume_contract 创建新 attempt，"
            "保持原请求不变，不要让 Experiment 代替 Data 生成产物。"
            if (details.get("resume_contract") or {}).get("kind") == "environment_required"
            and (details.get("resume_contract") or {}).get("owner") == "experiment" else ""
        )),
        turns=0,
        messages=messages,
        status="failed",
    )


async def _advance_preprocessing_pipeline(state, messages, reason: str) -> LoopResult | None:
    store = PlanningStore(state)
    execution_retries = set()
    stage_resume_consumed = False
    revision_states = {}
    execution_record = state.root / "planning" / "execution_result.json"
    try:
        previous_execution = json.loads(execution_record.read_text(encoding="utf-8")) if execution_record.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous_execution = {}
    while True:
        resume_target = ""
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
        saved_target = str(previous_execution.get("retry_target")
                           or (previous_execution.get("publication") or {}).get("retry_target") or "")
        resumed = state.hook_state.get("_resume_outcome") or {}
        resuming_stage = (pipeline_outcome(previous_execution)["kind"] == "externally_blocked"
                          and saved_target and not stage_resume_consumed
                          and resumed.get("resumed") is True
                          and resumed.get("requested_run_id") == state.run_id)
        if resuming_stage and (
            not status.get("approved")
            or status.get("approved_plan_id") != previous_execution.get("plan_id")
        ):
            return _pipeline_result(
                state, messages, outcome="externally_blocked",
                reason="Pipeline-stage recovery requires the original approved plan. Restore that plan; no generation was started.",
                details=previous_execution,
            )
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
            if prior_outcome["kind"] == "revise_asset":
                return _pipeline_result(
                    state, messages, outcome="fatal",
                    reason=prior_outcome["reason"] or "Local repair did not resolve the cited defects; no plan redesign was attempted.",
                    details=previous_execution,
                )
            if prior_outcome["kind"] == "revise_plan":
                reference_repeated = False
                if str(previous_execution.get("status") or "") == "needs_reference_search":
                    ledger_signature = canonical_hash(store.load_reference_state().get("gaps") or {})
                    signature = canonical_hash({
                        "outcome": prior_outcome["kind"],
                        "revision_contract": prior_outcome.get("revision_contract") or {},
                        "transition": previous_execution.get("transition") or {},
                    })
                    reference_repeated = store.transition_signature_seen(signature, ledger_signature=ledger_signature)
                    from nodes.data.tools.preprocessing_planner import reference_plan_from_generation_transition

                    # Repeated discovery can expose an invented dependency.
                    # Reuse requirement reconciliation before terminal failure,
                    # rather than approving another identical search plan.
                    transition = None
                    if not reference_repeated:
                        store.record_transition_signature(
                            signature,
                            plan_id=str(previous_execution.get("plan_id") or ""),
                            status=prior_outcome["kind"],
                            ledger_signature=ledger_signature,
                        )
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
                        "revision_contract": (transition.get("revision_contract") if isinstance(transition, dict) else None) or prior_outcome.get("revision_contract"),
                    }
                progress = previous_execution.get("review_progress") or (
                    previous_execution.get("publication") or {}
                ).get("review_progress") or revision_progress(
                    (prior_outcome.get("revision_contract") or {}).get("issues") or [],
                    error=prior_outcome.get("reason") or "",
                )
                # Rewriting bytes is not progress when the same checks still
                # fail. Keep new diagnostics/check results, not output hashes.
                signature = canonical_hash({key: value for key, value in progress.items()
                                            if key not in {"signature", "material_signature"}})
                repeated = revision_states.get(signature, 0)
                if repeated >= 2:
                    return _pipeline_result(state, messages, outcome="fatal",
                        reason="Neither targeted repair nor requirement reconciliation resolved the same findings.",
                        details=previous_execution)
                revision_states[signature] = repeated + 1
                planning = await _advance_generation_revision(state, messages, previous_execution,
                    planning_feedback={"status": "needs_revision", "stop_reason": "generation_revision_no_progress",
                                       "revision_contract": prior_outcome.get("revision_contract")}
                    if repeated or reference_repeated else None)
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
            if resuming_stage:
                original_request = ((previous_plan_record or {}).get("plan") or {}).get("preprocessing_request") or {}
                if preprocessing_request_fingerprint(original_request) != _request_fingerprint_from_state(state):
                    return _pipeline_result(
                        state, messages, outcome="externally_blocked",
                        reason="Pipeline-stage recovery must preserve the original request. Restore the same caller inputs; no assets were regenerated.",
                        details=previous_execution,
                    )
                stage_resume_consumed = True
                resume_target = saved_target
                reason = "resume_saved_pipeline_stage"
            elif prior_outcome["kind"] == "retry_step" and status.get("approved"):
                signature = canonical_hash({"plan": previous_execution.get("plan_id"),
                                            "reason": prior_outcome["reason"]})
                if signature not in execution_retries:
                    execution_retries.add(signature)
                    # The executor checkpoint retains successful assets and
                    # retries only the failed step or publication/review.
                    reason = "retry_failed_execution_or_review"
                else:
                    return _pipeline_result(
                        state, messages, outcome="externally_blocked",
                        reason=(
                            "Internal execution/review retry made no diagnostic progress: "
                            + prior_outcome["reason"]
                        ), details=previous_execution,
                    )
            elif prior_outcome["kind"] in {"externally_blocked", "fatal", "retry_step"}:
                return _pipeline_result(
                    state, messages,
                    outcome="externally_blocked" if prior_outcome["kind"] == "retry_step" else prior_outcome["kind"],
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

        execution = await _execute_generation_stage(
            state, reason, **({"resume_target": resume_target} if resume_target else {}),
        )
        if execution is None:
            return _pipeline_result(
                state, messages, outcome="fatal",
                reason="No approved generation plan was available to execute.",
            )
        previous_execution = execution
        dataset = _dataset_summary(state)
        if dataset is not None:
            record_task_outcome(state, "success" if dataset["delivery_kind"] == "dataset_materialized" else "partial",
                                detail=dataset["artifact_id"], reported_by="data.pipeline").update(
                                    request_authority=dataset["request_authority"])
            state.append_transcript("data_auto_execute_dataset_saved", **dataset)
            emit_progress(state, "delivery_found", f"{dataset['delivery_kind']} artifact saved by approved plan")
            return LoopResult(
                final_text=(f"Executed approved preprocessing plan; {dataset['delivery_kind']} artifact {dataset['artifact_id']} was saved.\n"
                            + dataset.get("review_summary", "")),
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
                reason="Execution completed without a reviewed delivery.",
                details=execution,
            )
        return _pipeline_result(
            state, messages,
            outcome="fatal" if outcome["kind"] == "retry_step" else outcome["kind"],
            reason=outcome["reason"] or "Generation execution could not continue.",
            details=execution,
        )


async def _existing_delivery_operation(state, messages) -> LoopResult | None:
    """Honor an explicit artifact operation without reinterpreting it as generation."""
    inputs = state.hook_state.get("node_inputs") or {}
    operation = str(inputs.get("operation") or "")
    artifact_id = str(inputs.get("artifact_id") or "")
    spec = str(inputs.get("spec") or "")
    # Older dispatchers express the exact tool invocation in spec. Require both
    # an affirmative invocation and one concrete dataset ID; frozen grid values
    # and general mentions of freezing are not artifact operations.
    if not operation and re.search(r"(?:调用|调|执行|call|invoke)\s*`?freeze_artifact\b", spec, re.I):
        if re.search(r"(?:不要|不得|禁止|不再|勿|do not|don't|never)\s*(?:重复|再)?\s*(?:调用|调|执行|call|invoke)?\s*`?freeze_artifact\b", spec, re.I):
            return None
        if not re.search(r"不要重新生成|不要重跑|只做冻结|只做一件事|仅.*冻结|only.*freez", spec, re.I):
            return _pipeline_result(state, messages, outcome="needs_input",
                reason="Separate artifact freezing from generation: use operation=freeze_artifact and artifact_id for the existing delivery.")
        operation = "freeze_artifact"
        ids = set(re.findall(r"\bdataset__[\w-]+", spec))
        artifact_id = next(iter(ids)) if len(ids) == 1 else ""
    if not operation:
        return None
    if operation != "freeze_artifact" or not artifact_id:
        return _pipeline_result(state, messages, outcome="needs_input",
                                reason="Specify operation=freeze_artifact and one artifact_id; no assets were regenerated.")
    summary = _validated_dataset_delivery(state, {"id": artifact_id}, current_run_only=False)
    if summary is None:
        return _pipeline_result(state, messages, outcome="fatal",
                                reason=f"Cannot freeze {artifact_id}: delivery receipt or file hashes failed validation; no assets were regenerated.")
    from core.tool_registry import execute

    result = await execute("freeze_artifact", state, artifact_id=artifact_id, reason=spec)
    state.append_transcript("data_existing_delivery_operation", operation=operation, result=result)
    if result.get("status") != "success":
        return _pipeline_result(state, messages, outcome="needs_input" if result.get("status") == "pause" else "fatal",
                                reason=str(result.get("error") or result.get("reason") or result), details=result)
    record_task_outcome(state, "success", detail=artifact_id, reported_by="data.pipeline")
    return LoopResult(final_text=json.dumps(result, ensure_ascii=False), turns=0, messages=messages, status="completed")


def _saved_stage_redispatch(state, messages) -> LoopResult | None:
    """Route a fresh attempt, including its diagnostics, to the preserved work."""
    request = state.hook_state.get("_data_current_request") or {}
    fingerprint = preprocessing_recovery_fingerprint(request)
    for item in reversed(state.list_artifacts("preprocessing_blocked_report")):
        record = state.read_artifact(item["id"]) or {}
        report = _json_object(record.get("content"))
        contract = _json_object(report.get("resume_contract"))
        if preprocessing_recovery_fingerprint(report.get("preprocessing_request")) != fingerprint:
            continue
        if (record.get("produced_by_run_id") == state.run_id
                or report.get("status") != "externally_blocked"
                or contract.get("kind") != "pipeline_stage"
                or not contract.get("resume_request")
                or contract.get("resume_run_id") == state.run_id):
            return None  # The latest matching report supersedes older recovery instructions.
        run_id = str(contract.get("resume_run_id") or "")
        if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
            return None
        root = Path(state.root).parent / run_id
        try:
            plan = json.loads((root / "planning/approved_plan.json").read_text())["plan"]
            checkpoint = json.loads((root / "planning/execution_checkpoint.json").read_text())
            if (canonical_hash(plan) != contract.get("plan_hash")
                    or checkpoint.get("plan_hash") != contract.get("plan_hash")):
                return None
            if contract.get("retry_target") == "artifact_writer":
                if not any(step.get("status") == "externally_blocked" and step.get("step_id") == contract.get("step_id")
                           and (step.get("result") or {}).get("failure_category") in RESUMABLE_WRITER_FAILURES
                           for step in checkpoint.get("steps") or []):
                    return None
            else:
                if {step.get("step_id") for step in checkpoint.get("steps") or [] if step.get("status") == "success"} != {
                        step.get("id") for step in plan.get("generation_steps") or []}:
                    return None
                candidate = checkpoint.get("delivery_candidate") or next((
                    step["result"]["delivery_candidate"] for step in reversed(checkpoint.get("steps") or [])
                    if (step.get("result") or {}).get("delivery_candidate")), {})
                staging = root / ".data_node_work/package_staging"
                if (contract.get("retry_target") not in {"authority_review", "publication"}
                        or Path(candidate.get("staging_dir") or "").resolve() != staging.resolve()
                        or not candidate.get("delivery_files")
                        or any(not (staging / item["path"]).is_file() for item in candidate["delivery_files"])):
                    return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None  # A stale report must not trap a legitimate fresh attempt.
        # Keep new findings separate from frozen scientific requirements. The
        # reviewer verifies them; the dispatcher cannot directly order edits.
        inputs = state.hook_state.get("node_inputs") or {}
        prior_feedback = inputs.get("recovery_feedback") or {}
        feedback = {**(prior_feedback if isinstance(prior_feedback, dict) else {"notes": prior_feedback}),
                    **{key: inputs[key] for key in ("spec", "user_note", "review_feedback") if inputs.get(key)}}
        if request.get("acceptance_criteria") != (plan.get("preprocessing_request") or {}).get("acceptance_criteria"):
            feedback["proposed_checks"] = request.get("acceptance_criteria")
        contract = deepcopy(contract)
        contract["resume_request"]["node_inputs"]["recovery_feedback"] = feedback
        review = checkpoint.get("authority_review_state") or {}
        progress, config = review.get("progress") or {}, review.get("config") or {}
        exhausted = (contract.get("retry_target") == "authority_review" and any(
            config.get(limit) is not None and progress.get(used, 0) >= config[limit]
            for used, limit in (("calls", "max_calls"), ("elapsed_s", "max_seconds"))))
        if exhausted:
            contract["retry_precondition"] = "new_verified_evidence_or_review_budget_explicitly_extended"
            contract["review_budget_exhausted"] = True
        return _pipeline_result(state, messages, outcome="externally_blocked",
            reason=(f"Preserved work is available in {contract['resume_run_id']} at {contract.get('retry_target')}. "
                    "New diagnostic feedback is included in resume_request for verification and targeted repair. "
                    + ("Its review budget is exhausted: unchanged redispatch cannot progress. Supply new verifiable "
                       "evidence or explicitly extend the review budget before resuming. " if exhausted else "")
                    + "Use that resume_request under its original authority; do not regenerate successful steps."),
            details={"failure_category": "saved_stage_available", "resume_contract": contract,
                     "dispatch_action": "resolve_review_blocker" if exhausted else "resume_saved_stage",
                     "retry_target": contract.get("retry_target")})
    return None


async def run_loop(harness, state, messages, llm):
    """Run the Data service directly, without a generic conversational-agent loop."""
    if isinstance(llm, LLMClient):
        # Keep provider recovery in the existing client, not nested Data retries.
        llm = copy(llm)
        if getattr(harness, "llm_timeout_s", None) is not None:
            llm.timeout = harness.llm_timeout_s
        if getattr(harness, "llm_max_retries", None) is not None:
            llm.max_retries = harness.llm_max_retries
    state.hook_state["_data_planning_llm_client"] = data_model_client(llm)
    emit_progress(state, "start", "data preprocessing service entered")
    try:
        operation = await _existing_delivery_operation(state, messages)
        if operation is not None:
            return operation
        try:
            _current_request_identity(state, messages)
        except ValueError as exc:
            return _pipeline_result(
                state, messages, outcome="fatal", reason=f"Invalid Data request identity: {exc}"
            )
        try:
            terminal = await _terminal_dataset_summary(
                state,
                request_spec_hash=_request_hash_from_state(state),
                request_fingerprint=_request_fingerprint_from_state(state),
            )
        except _DeliveryReviewBlocked as exc:
            return _pipeline_result(
                state,
                messages,
                outcome="needs_input"
                if exc.result.get("status") == "needs_input"
                else "externally_blocked",
                reason=str(exc),
                details={
                    **exc.result,
                    "retry_target": "authority_review",
                    "failure_category": exc.result.get("failure_category")
                    or "delivery_review_unavailable",
                    "resume_contract": {
                        "kind": "delivery_review",
                        "review_artifact_id": exc.review_artifact_id,
                        "instruction": "Preserve the delivery and original request. Resolve the review service/evidence "
                        "gap or explicitly extend its budget, then resubmit structured review_feedback. "
                        "A different spec, directory or child ID does not reset this review.",
                    },
                },
            )
        except (OSError, ValueError, TimeoutError, ImportError) as exc:
            return _pipeline_result(
                state,
                messages,
                outcome="externally_blocked",
                reason=f"Existing delivery revalidation could not complete; preserve it without regenerating: {exc}",
                details={"failure_category": "delivery_revalidation_unavailable"},
            )
        if terminal is not None:
            task_outcome = record_task_outcome(
                state,
                "success" if terminal["delivery_kind"] == "dataset_materialized" else "partial",
                detail=f"Reuse {terminal['artifact_id']}; this unchanged request needs no regeneration.",
                reported_by="data.pipeline",
            )
            task_outcome.update(
                dispatch_action="reuse_delivery",
                redispatch_required=False,
                artifact_id=terminal["artifact_id"],
                request_authority=terminal["request_authority"],
                review_scope={
                    "request_fingerprint": terminal["request_fingerprint"],
                    "delivery_kind": terminal["delivery_kind"],
                    "simulation_ready": terminal.get("simulation_ready", False),
                },
            )
            state.append_transcript("data_terminal_dataset_already_present", **terminal)
            emit_progress(
                state,
                "terminal_delivery",
                terminal["artifact_id"],
                status=terminal["status"],
                delivery_kind=terminal["delivery_kind"],
            )
            return LoopResult(
                final_text=(
                    terminal.get("verification_summary", "") + "\n"
                    "No regeneration is needed: the unchanged user request already has a validated delivery. "
                    "Do not redispatch for dispatcher-only preferences. "
                    f"Data node reused {terminal['delivery_kind']} delivery {terminal['artifact_id']} "
                    f"with status={terminal['status']}.\n" + terminal.get("review_summary", "")
                ),
                turns=0,
                messages=messages,
                status="completed",
            )

        redirect = _saved_stage_redispatch(state, messages)
        if redirect is not None:
            return redirect

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
