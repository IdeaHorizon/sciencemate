"""每次运行的资格契约与轻量 provenance 记录。

从 repro_snapshot 抽出来的原因是**缺陷成因**，不是文件大小：manifest 的写方
（``create_run_manifest``）此前在 repro_snapshot，读方（experiment 的兜底
记录 hook）在 hooks.py，两边各自拼 artifact 路径，于是读方长期读一个
**永远不存在**的文件名，兜底记录里的 returncode/日志路径恒为空。

因此这里只暴露返回值，不暴露落盘路径约定：调用方拿 ``create_run_manifest()``
的返回 dict 即可，不需要、也不应该自己去猜文件名。

``load_run_contract`` 同时被 hooks.py、preflight.py 引用，是跨模块的共享词汇表
（run_role / analysis_eligible / protocol 引用），放在这里比留在快照采集器里更贴切。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool
from shared.lib.filelock import exclusive

try:
    from .path_roles import experiment_output_dir
except ImportError:  # loaded as top-level ``tools.run_contract`` by node runtime
    from tools.path_roles import experiment_output_dir


_RUN_CONTRACT_KEYS = (
    "experiment_id", "step_id", "stage", "run_role", "analysis_eligible",
    "review_eligible",
    "execution_mode", "operation_kind",
    "protocol_version", "dataset_id", "scope_manifest_id",
    "protocol_amendment_id", "exclusion_reason", "expected_params", "execution_contract", "requires_gpu",
    "input_delivery_policy",
)

_EXECUTION_MODES = frozenset({"scientific", "operational"})
_REQUEST_SCOPES = frozenset({"scientific", "operation"})
_OPERATION_CATEGORIES = frozenset({
    "package_install", "toolchain_build", "environment_probe",
    "scheduler_probe", "job_observation", "format_validation", "other",
})

RUN_ROLE_SOURCE_DECLARED_PRIMARY = "declared_primary"
RUN_ROLE_SOURCE_DECLARED_SECONDARY = "declared_secondary"
RUN_ROLE_SOURCE_UNDECLARED_SECONDARY = "undeclared_defaulted_secondary"
RUN_ROLE_SOURCE_LEGACY_HOOK_PRIMARY = "legacy_hook_state_declared_primary"
RUN_ROLE_SOURCE_LEGACY_HOOK_SECONDARY = "legacy_hook_state_declared_secondary"
RUN_ROLE_SOURCE_LEGACY_HOOK_UNDECLARED = (
    "legacy_hook_state_undeclared_defaulted_secondary"
)
RUN_ROLE_SOURCE_LEGACY_HOOK_INVALID = (
    "legacy_hook_state_invalid_declared_defaulted_secondary"
)
RUN_ROLE_UNDECLARED_WARNING = "run_role_missing_defaulted_to_secondary"
FORMAL_ROLE_GATE_NOT_APPLICABLE = (
    "formal_gate_not_applicable_for_secondary_scientific_run"
)


def run_role_non_applicability_reason(
    contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Project why a secondary role leaves a formal-role gate inapplicable.

    This is visibility only: consumers keep their existing gate predicate and
    call this helper only after that predicate has already said "not
    applicable". Keeping the reason beside the run-contract vocabulary makes
    an undeclared default mechanically distinct from an explicit secondary
    declaration without creating a second gate registry.
    """
    execution_mode = str(contract.get("execution_mode") or "")
    run_role = str(contract.get("run_role") or "")
    if execution_mode != "scientific" or run_role != "secondary":
        return None
    source = str(contract.get("run_role_source") or "")
    return {
        "code": FORMAL_ROLE_GATE_NOT_APPLICABLE,
        "context": {
            "execution_mode": execution_mode,
            "run_role": run_role,
            "run_role_source": source or "unavailable",
        },
    }

_INTENT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ACCEPTANCE_EVENT = "run_acceptance_receipt_recorded"
_RUN_ACCEPTANCE_SCHEMA_VERSION = 2
_LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION = 1
_RUN_ACCEPTANCE_BINDING_SOURCES_V1 = frozenset({
    "explicit_node_input", "selected_input", "unique_auto_claim", "none",
})
_RUN_ACCEPTANCE_BINDING_SOURCES_V2 = frozenset({
    "explicit_node_input", "selected_input",
})
_RUN_ACCEPTANCE_COMMON_DIGEST_FIELDS = (
    "schema_version",
    "run_id",
    "intent_digest",
    "intent_source",
    "intent_input_keys",
    "intent_status",
)
_RUN_ACCEPTANCE_V1_DIGEST_FIELDS = (
    *_RUN_ACCEPTANCE_COMMON_DIGEST_FIELDS,
    "governing_task_input_binding",
    "binding_source",
    "unbound_prereg_visibility_witness",
)
_RUN_ACCEPTANCE_V2_DIGEST_FIELDS = (
    *_RUN_ACCEPTANCE_COMMON_DIGEST_FIELDS,
    "prereg_assignment",
    "unbound_prereg_visibility_witness",
)
_PREREG_ASSIGNMENT_PENDING_EVENT = "experiment_prereg_assignment_pending"
_PENDING_SCIENTIFIC_SIGNAL_EVENT = (
    "experiment_prereg_assignment_scientific_signal_blocked"
)

_LEGACY_CLASSIFIED_RUN_AUTHORITY_REASON = (
    "this run was already classified but has no write-once run acceptance "
    "receipt (for example, it began before run acceptance receipts were "
    "supported); this version does not reconstruct execution authority for "
    "it; start a fresh run. If this run can be closed honestly, use "
    "declare_inconclusive_verdict(reason=..., next_step=...) to record why; "
    "do not fabricate an assessment"
)


def _empty_intent_prereg_binding() -> dict[str, Any]:
    return {"artifact_id": None, "version": None, "content_hash": None}


def _prereg_binding_state(binding: Any) -> str:
    """Return absent, valid or invalid; partial triples are never evidence."""
    if binding is None:
        return "absent"
    if not isinstance(binding, dict):
        return "invalid"
    values = (binding.get("artifact_id"), binding.get("version"), binding.get("content_hash"))
    if values == (None, None, None):
        return "absent"
    artifact_id, version, content_hash = values
    if (
        set(binding) == {"artifact_id", "version", "content_hash"}
        and isinstance(artifact_id, str)
        and artifact_id
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version >= 1
        and isinstance(content_hash, str)
        and _INTENT_DIGEST_RE.fullmatch(content_hash) is not None
    ):
        return "valid"
    return "invalid"


def _validated_prereg_assignment(
    value: Any,
    *,
    require_bound_source: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the mutually exclusive prereg assignment union."""
    if not isinstance(value, dict):
        return None, "prereg_assignment must be an object"
    kind = value.get("kind")
    if kind == "pending":
        if set(value) != {"kind"}:
            return None, "pending prereg_assignment accepts only kind"
        return {"kind": "pending"}, None
    if kind == "none":
        if set(value) != {"kind", "reason"}:
            return None, "none prereg_assignment requires only kind and reason"
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None, "none prereg_assignment requires a non-empty reason"
        return {"kind": "none", "reason": reason.strip()}, None
    if kind == "bound":
        allowed = {"kind", "artifact_id", "version", "content_hash", "source"}
        required = allowed if require_bound_source else allowed - {"source"}
        if not required.issubset(value) or not set(value).issubset(allowed):
            return None, (
                "bound prereg_assignment requires artifact_id, version, "
                "content_hash and a receipt source"
            )
        binding = {
            "artifact_id": value.get("artifact_id"),
            "version": value.get("version"),
            "content_hash": value.get("content_hash"),
        }
        if _prereg_binding_state(binding) != "valid":
            return None, "bound prereg_assignment requires one exact id/version/hash"
        source = value.get("source")
        if require_bound_source:
            if source not in _RUN_ACCEPTANCE_BINDING_SOURCES_V2:
                return None, "bound prereg_assignment source is invalid"
        elif source is not None and source not in _RUN_ACCEPTANCE_BINDING_SOURCES_V2:
            return None, "bound prereg_assignment source is invalid"
        normalized = {"kind": "bound", **binding}
        if source is not None:
            normalized["source"] = source
        return normalized, None
    return None, "prereg_assignment.kind must be bound, none, or pending"


def _receipt_prereg_assignment(receipt: Any) -> dict[str, Any]:
    """Return one typed read view for both v1 and v2 receipts."""
    if not isinstance(receipt, dict):
        return {"kind": "pending"}
    assignment = receipt.get("prereg_assignment")
    normalized, _error = _validated_prereg_assignment(
        assignment, require_bound_source=True,
    )
    if normalized is not None:
        return normalized
    binding = receipt.get("governing_task_input_binding")
    if _prereg_binding_state(binding) == "valid":
        return {
            "kind": "bound",
            **dict(binding),
            "source": str(receipt.get("binding_source") or ""),
        }
    # v1 had no way to distinguish omission from explicit none. Preserve it
    # as an unresolved/pending legacy assignment; never invent a waiver.
    return {"kind": "pending"}


def _normalized_prereg_version_input(
    value: Any,
) -> tuple[int | None, dict[str, Any] | None]:
    """Normalize the model-facing exact-version selector without weakening it."""
    if value is None:
        return None, None
    if type(value) is int and value >= 1:
        return value, None
    if isinstance(value, float) and value.is_integer() and value >= 1:
        return int(value), None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None, None
        if re.fullmatch(r"[1-9][0-9]*", normalized):
            try:
                return int(normalized), None
            except ValueError:
                pass
    return None, {
        "provided_type": type(value).__name__,
        "reason": (
            "invalid prereg_version: expected null/blank, a positive integer, "
            "an integer-valued positive float, or a positive integer string"
        ),
    }


def _durable_verdict_obligation_witness(
    event: dict[str, Any],
    *,
    line_number: int,
    validated_receipts_by_digest: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Project only a complete prior primary-scientific obligation event.

    This witness can preserve a verdict obligation after later transcript
    damage, but it never supplies execution authority or a prereg binding.
    """
    binding = event.get("governing_task_input_binding")
    receipt_digest = event.get("run_acceptance_receipt_digest")
    accepted_receipt = (
        validated_receipts_by_digest.get(receipt_digest)
        if isinstance(receipt_digest, str)
        else None
    )
    if not (
        event.get("event") == "experiment_scope_classified"
        and event.get("mode") == "scientific"
        and event.get("run_role") == "primary"
        and event.get("source") == "governing_task_input_binding"
        and _prereg_binding_state(binding) == "valid"
        and isinstance(receipt_digest, str)
        and _INTENT_DIGEST_RE.fullmatch(receipt_digest) is not None
        and isinstance(accepted_receipt, dict)
        and binding == accepted_receipt.get("governing_task_input_binding")
        and event.get("intent_digest") == accepted_receipt.get("intent_digest")
        and event.get("binding_source") == accepted_receipt.get("binding_source")
        and (
            accepted_receipt.get("schema_version")
            == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
            or event.get("prereg_assignment")
            == accepted_receipt.get("prereg_assignment")
        )
    ):
        return None
    return {
        "event": "experiment_scope_classified",
        "line_number": line_number,
        "mode": "scientific",
        "run_role": "primary",
        "run_acceptance_receipt_digest": receipt_digest,
    }


def _durable_scope_projection(
    event: dict[str, Any],
    *,
    line_number: int,
    validated_receipts_by_digest: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Reduce one validated scope event to a non-authorizing resume projection."""
    receipt_digest = event.get("run_acceptance_receipt_digest")
    accepted_receipt = (
        validated_receipts_by_digest.get(receipt_digest)
        if isinstance(receipt_digest, str)
        else None
    )
    mode = event.get("mode")
    category = event.get("category")
    if not (
        event.get("event") == "experiment_scope_classified"
        and mode in _EXECUTION_MODES
        and isinstance(accepted_receipt, dict)
        and event.get("governing_task_input_binding")
        == accepted_receipt.get("governing_task_input_binding")
        and event.get("intent_schema_version")
        == accepted_receipt.get("schema_version")
        and event.get("intent_digest") == accepted_receipt.get("intent_digest")
        and event.get("intent_source") == accepted_receipt.get("intent_source")
        and event.get("intent_status") == accepted_receipt.get("intent_status")
        and event.get("binding_source") == accepted_receipt.get("binding_source")
        and (
            accepted_receipt.get("schema_version")
            == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
            or event.get("prereg_assignment")
            == accepted_receipt.get("prereg_assignment")
        )
    ):
        return None
    if (
        mode == "operational"
        and category not in _OPERATION_CATEGORIES
    ) or (mode == "scientific" and category is not None):
        return None
    projection: dict[str, Any] = {
        "mode": mode,
        "category": category,
        "reason": str(event.get("reason") or ""),
        "line_number": line_number,
        "run_acceptance_receipt_digest": receipt_digest,
        "intent_schema_version": accepted_receipt["schema_version"],
        "intent_source": accepted_receipt.get("intent_source"),
        "intent_status": accepted_receipt.get("intent_status"),
        "intent_digest": accepted_receipt.get("intent_digest"),
        "governing_task_input_binding": accepted_receipt.get(
            "governing_task_input_binding"
        ),
        "binding_source": accepted_receipt.get("binding_source"),
    }
    if (
        _prereg_binding_state(accepted_receipt.get("governing_task_input_binding"))
        == "valid"
        and event.get("source") == "governing_task_input_binding"
    ):
        projection["source"] = "governing_task_input_binding"
    invocation = event.get("invocation")
    if isinstance(invocation, dict):
        projection["invocation"] = dict(invocation)
    warnings = event.get("contract_warnings")
    if isinstance(warnings, list):
        projection["contract_warnings"] = [
            str(item) for item in warnings if isinstance(item, str) and item
        ]
    return projection


def _empty_prereg_visibility_witness() -> dict[str, list[dict[str, Any]]]:
    """Return the canonical non-authorizing project-prereg visibility shape."""
    return {"frozen": [], "pending": []}


def _validated_prereg_visibility_witness(
    value: Any,
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """Validate a null-binding visibility witness without granting authority."""
    if not isinstance(value, dict) or set(value) != {"frozen", "pending"}:
        return None, "expected an object with frozen and pending lists"
    frozen = value.get("frozen")
    pending = value.get("pending")
    if not isinstance(frozen, list) or not isinstance(pending, list):
        return None, "frozen and pending must be lists"

    frozen_keys = {"artifact_id", "version", "content_hash"}
    pending_keys = {
        "artifact_id",
        "draft_version",
        "draft_content_hash",
        "latest_frozen_version",
        "latest_frozen_content_hash",
    }

    def valid_id(item: dict[str, Any]) -> bool:
        return isinstance(item.get("artifact_id"), str) and bool(item["artifact_id"])

    def valid_version(item: dict[str, Any], key: str) -> bool:
        value = item.get(key)
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    def valid_hash(item: dict[str, Any], key: str) -> bool:
        value = item.get(key)
        return isinstance(value, str) and _INTENT_DIGEST_RE.fullmatch(value) is not None

    for item in frozen:
        if (
            not isinstance(item, dict)
            or set(item) != frozen_keys
            or not valid_id(item)
            or not valid_version(item, "version")
            or not valid_hash(item, "content_hash")
        ):
            return None, "frozen entries must carry one exact artifact id/version/hash"
    for item in pending:
        if (
            not isinstance(item, dict)
            or set(item) != pending_keys
            or not valid_id(item)
            or not valid_version(item, "draft_version")
            or not valid_hash(item, "draft_content_hash")
            or not valid_version(item, "latest_frozen_version")
            or not valid_hash(item, "latest_frozen_content_hash")
        ):
            return None, "pending entries must carry exact draft and latest-frozen identities"

    normalized = {
        "frozen": sorted(
            (dict(item) for item in frozen),
            key=lambda item: (
                item["artifact_id"], item["version"], item["content_hash"],
            ),
        ),
        "pending": sorted(
            (dict(item) for item in pending),
            key=lambda item: (
                item["artifact_id"],
                item["draft_version"],
                item["draft_content_hash"],
                item["latest_frozen_version"],
                item["latest_frozen_content_hash"],
            ),
        ),
    }
    ids = [
        item["artifact_id"]
        for item in (*normalized["frozen"], *normalized["pending"])
    ]
    if len(ids) != len(set(ids)):
        return None, "one artifact identity cannot appear more than once"
    if value != normalized:
        return None, "visibility entries must be canonical and sorted"
    return normalized, None


def _candidate_bindings_from_visibility(
    visibility: Any,
) -> list[dict[str, Any]]:
    """Project every usable exact frozen candidate from one observation.

    A pending amendment does not authorize its draft, but its latest frozen
    predecessor remains an exact option that the dispatching parent can bind
    explicitly.  All pending-assignment handoffs must use this one projection
    so their witness, refusal, and audit views cannot silently disagree.
    """
    if not isinstance(visibility, dict):
        return []
    candidates: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in visibility.get("frozen") or []:
        if not isinstance(item, dict):
            continue
        candidate = {
            "artifact_id": item.get("artifact_id"),
            "version": item.get("version"),
            "content_hash": item.get("content_hash"),
        }
        key = (
            str(candidate["artifact_id"] or ""),
            candidate["version"],
            str(candidate["content_hash"] or ""),
        )
        if key[0] and isinstance(key[1], int) and key[2]:
            candidates[key] = candidate
    for item in visibility.get("pending") or []:
        if not isinstance(item, dict):
            continue
        candidate = {
            "artifact_id": item.get("artifact_id"),
            "version": item.get("latest_frozen_version"),
            "content_hash": item.get("latest_frozen_content_hash"),
        }
        key = (
            str(candidate["artifact_id"] or ""),
            candidate["version"],
            str(candidate["content_hash"] or ""),
        )
        if key[0] and isinstance(key[1], int) and key[2]:
            candidates[key] = candidate
    return [candidates[key] for key in sorted(candidates)]


def _run_acceptance_receipt_digest(payload: dict[str, Any]) -> str:
    """Hash only the semantic receipt payload; transcript timestamps never enter."""
    schema_version = payload.get("schema_version")
    if schema_version == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION:
        fields = _RUN_ACCEPTANCE_V1_DIGEST_FIELDS
    elif schema_version == _RUN_ACCEPTANCE_SCHEMA_VERSION:
        fields = _RUN_ACCEPTANCE_V2_DIGEST_FIELDS
    else:
        raise ValueError("unsupported run acceptance receipt schema_version")
    material = {
        key: payload.get(key)
        for key in fields
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_run_acceptance_receipt(
    state: Any,
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one transcript event without trusting hook-state projections."""
    schema_version = event.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {
            _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION,
            _RUN_ACCEPTANCE_SCHEMA_VERSION,
        }
    ):
        return None, "invalid schema_version: expected integer 1 or 2"
    fields = (
        _RUN_ACCEPTANCE_V1_DIGEST_FIELDS
        if schema_version == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        else _RUN_ACCEPTANCE_V2_DIGEST_FIELDS
    )
    payload = {
        key: event.get(key)
        for key in (*fields, "receipt_digest")
    }
    if schema_version == _RUN_ACCEPTANCE_SCHEMA_VERSION and any(
        key in event
        for key in ("governing_task_input_binding", "binding_source")
    ):
        return None, (
            "v2 receipt must not persist legacy prereg authority aliases"
        )
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not run_id:
        return None, "run_id is missing"
    if run_id != str(getattr(state, "run_id", "") or ""):
        return None, "run_id does not match the current run"
    intent_digest = payload["intent_digest"]
    if intent_digest is not None and (
        not isinstance(intent_digest, str)
        or _INTENT_DIGEST_RE.fullmatch(intent_digest) is None
    ):
        return None, "invalid intent_digest: expected null or sha256"
    intent_source = payload["intent_source"]
    if intent_source is not None and (
        not isinstance(intent_source, str) or not intent_source
    ):
        return None, "invalid intent_source: expected null or non-empty string"
    input_keys = payload["intent_input_keys"]
    if (
        not isinstance(input_keys, list)
        or any(not isinstance(item, str) for item in input_keys)
        or input_keys != sorted(set(input_keys))
    ):
        return None, "invalid intent_input_keys: expected sorted unique string list"
    intent_status = payload["intent_status"]
    if intent_status not in {"bound_at_acceptance", "unavailable_at_acceptance"}:
        return None, "intent_status is invalid"
    if (intent_status == "bound_at_acceptance") != (intent_digest is not None):
        return None, "intent_status and intent_digest disagree"
    if schema_version == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION:
        binding = payload["governing_task_input_binding"]
        binding_state = _prereg_binding_state(binding)
        if binding_state == "invalid":
            return None, (
                "invalid governing_task_input_binding: expected null or one exact triple"
            )
        if binding_state == "absent":
            payload["governing_task_input_binding"] = None
        source = payload["binding_source"]
        if source not in _RUN_ACCEPTANCE_BINDING_SOURCES_V1:
            return None, "binding_source is invalid"
        if (binding_state == "absent") != (source == "none"):
            return None, "binding_source and governing_task_input_binding disagree"
        payload["prereg_assignment"] = _receipt_prereg_assignment(payload)
    else:
        assignment, assignment_error = _validated_prereg_assignment(
            payload.get("prereg_assignment"), require_bound_source=True,
        )
        if assignment is None:
            return None, f"invalid prereg_assignment: {assignment_error}"
        payload["prereg_assignment"] = assignment
        if assignment["kind"] == "bound":
            binding = {
                key: assignment[key]
                for key in ("artifact_id", "version", "content_hash")
            }
            source = str(assignment["source"])
            binding_state = "valid"
        else:
            binding = None
            source = "none"
            binding_state = "absent"
        # Compatibility projections are derived in memory and are not part of
        # the v2 digest or transcript authority payload.
        payload["governing_task_input_binding"] = binding
        payload["binding_source"] = source
    visibility = payload["unbound_prereg_visibility_witness"]
    needs_visibility = (
        schema_version == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        and binding_state == "absent"
    ) or (
        schema_version == _RUN_ACCEPTANCE_SCHEMA_VERSION
        and payload["prereg_assignment"]["kind"] == "pending"
    )
    if needs_visibility:
        normalized_visibility, visibility_error = (
            _validated_prereg_visibility_witness(visibility)
        )
        if normalized_visibility is None:
            return None, (
                "invalid unbound_prereg_visibility_witness: "
                f"{visibility_error}"
            )
        payload["unbound_prereg_visibility_witness"] = normalized_visibility
    elif visibility is not None:
        return None, (
            "only a pending (or legacy unbound) receipt can carry an unbound "
            "visibility witness"
        )
    digest = payload["receipt_digest"]
    try:
        expected_digest = _run_acceptance_receipt_digest(payload)
    except (TypeError, ValueError, UnicodeError):
        return None, "receipt payload is not canonical UTF-8 JSON"
    if (
        not isinstance(digest, str)
        or _INTENT_DIGEST_RE.fullmatch(digest) is None
        or digest != expected_digest
    ):
        return None, "receipt_digest is invalid"
    return payload, None


def _reduce_run_acceptance_receipts(state: Any) -> dict[str, Any]:
    """Strictly reduce the run-local receipt stream.

    This is the sole reducer. hook_state may cache its returned projection,
    but it is never read here and therefore can never authorize a run.
    """
    default_path = Path(getattr(state, "root", ".")) / "transcript.jsonl"
    path = Path(getattr(state, "transcript_path", default_path))
    if not path.exists():
        return {
            "passed": False,
            "status": "run_authority_receipt_missing",
            "receipt": None,
            "identical_event_count": 0,
        }
    try:
        # JSONL records are separated only by LF. str.splitlines() also splits
        # legal JSON string contents such as U+2028/U+2029 and would corrupt a
        # valid transcript before authority reduction.
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError) as exc:
        return {
            "passed": False,
            "status": "run_authority_receipt_unreadable",
            "receipt": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    receipts: list[dict[str, Any]] = []
    validated_receipts_by_digest: dict[str, dict[str, Any]] = {}
    legacy_scope_seen = False
    obligation_witness: dict[str, Any] | None = None
    scope_projection: dict[str, Any] | None = None
    pending_witness_events: list[dict[str, Any]] = []
    pending_scientific_signal_events: list[dict[str, Any]] = []

    def with_durable_witnesses(result: dict[str, Any]) -> dict[str, Any]:
        if obligation_witness is not None:
            result["durable_verdict_obligation_witness"] = dict(
                obligation_witness
            )
        if scope_projection is not None:
            result["durable_scope_projection"] = dict(scope_projection)
        return result

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return with_durable_witnesses({
                "passed": False,
                "status": "run_authority_receipt_unreadable",
                "receipt": None,
                "reason": f"transcript line {line_number} is invalid JSON: {exc.msg}",
            })
        if not isinstance(event, dict):
            return with_durable_witnesses({
                "passed": False,
                "status": "run_authority_receipt_unreadable",
                "receipt": None,
                "reason": f"transcript line {line_number} is not an object",
            })
        if event.get("event") == _PREREG_ASSIGNMENT_PENDING_EVENT:
            pending_witness_events.append(event)
        if event.get("event") == _PENDING_SCIENTIFIC_SIGNAL_EVENT:
            pending_scientific_signal_events.append(event)
        if event.get("event") == "experiment_scope_classified":
            legacy_scope_seen = True
            candidate_projection = _durable_scope_projection(
                event,
                line_number=line_number,
                validated_receipts_by_digest=validated_receipts_by_digest,
            )
            if candidate_projection is not None:
                scope_projection = candidate_projection
            candidate_witness = _durable_verdict_obligation_witness(
                event,
                line_number=line_number,
                validated_receipts_by_digest=validated_receipts_by_digest,
            )
            if candidate_witness is not None:
                obligation_witness = candidate_witness
        if event.get("event") != _RUN_ACCEPTANCE_EVENT:
            continue
        receipt, error = _validated_run_acceptance_receipt(state, event)
        if receipt is None:
            return with_durable_witnesses({
                "passed": False,
                "status": "run_authority_receipt_invalid",
                "receipt": None,
                "reason": f"receipt event at line {line_number}: {error}",
            })
        receipts.append(receipt)
        validated_receipts_by_digest[receipt["receipt_digest"]] = receipt
    if not receipts:
        return with_durable_witnesses({
            "passed": False,
            "status": "run_authority_receipt_missing",
            "receipt": None,
            "identical_event_count": 0,
            "legacy_scope_seen": legacy_scope_seen,
        })
    distinct = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in receipts
    }
    if len(distinct) != 1:
        return with_durable_witnesses({
            "passed": False,
            "status": "run_authority_receipt_conflict",
            "receipt": None,
            "event_count": len(receipts),
            "distinct_receipt_count": len(distinct),
            "reason": "the run transcript contains different valid acceptance receipts",
        })
    canonical_receipt = receipts[0]
    expected_pending_witness = _pending_assignment_witness_payload(
        canonical_receipt
    )
    pending_assignment_witnessed = bool(
        expected_pending_witness
        and any(
            all(event.get(key) == value for key, value in expected_pending_witness.items())
            for event in pending_witness_events
        )
    )
    pending_scientific_signal = next(
        (
            {
                "scientific_signal_source": event.get(
                    "scientific_signal_source"
                ),
                "scientific_signal_details": dict(
                    event.get("scientific_signal_details") or {}
                ),
            }
            for event in pending_scientific_signal_events
            if (
                event.get("run_acceptance_receipt_digest")
                == canonical_receipt.get("receipt_digest")
                and event.get("assignment_status") == "pending"
                and isinstance(event.get("scientific_signal_source"), str)
                and bool(event.get("scientific_signal_source"))
                and isinstance(event.get("scientific_signal_details") or {}, dict)
            )
        ),
        None,
    )
    return with_durable_witnesses({
        "passed": True,
        "status": "bound",
        "receipt": canonical_receipt,
        "identical_event_count": len(receipts),
        "pending_assignment_witnessed": pending_assignment_witnessed,
        **(
            {"pending_scientific_signal": pending_scientific_signal}
            if pending_scientific_signal is not None else {}
        ),
    })


def _receipt_event_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    """Strip in-memory compatibility projections from a durable receipt event."""
    fields = (
        _RUN_ACCEPTANCE_V1_DIGEST_FIELDS
        if receipt.get("schema_version") == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        else _RUN_ACCEPTANCE_V2_DIGEST_FIELDS
    )
    return {key: receipt.get(key) for key in (*fields, "receipt_digest")}


def _pending_assignment_witness_payload(
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if not (
        receipt.get("schema_version") == _RUN_ACCEPTANCE_SCHEMA_VERSION
        and _receipt_prereg_assignment(receipt).get("kind") == "pending"
    ):
        return None
    visibility = receipt.get("unbound_prereg_visibility_witness") or {}
    return {
        "run_acceptance_receipt_digest": receipt.get("receipt_digest"),
        "candidate_bindings": _candidate_bindings_from_visibility(visibility),
        "authorizing": False,
        "next_action_owner": "dispatching_parent",
    }


def _ensure_pending_assignment_witness(
    state: Any,
    reduced: dict[str, Any],
    *,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Record the non-authorizing parent handoff for a v2 pending receipt."""
    receipt = reduced.get("receipt")
    if not (
        reduced.get("passed")
        and isinstance(receipt, dict)
        and receipt.get("schema_version") == _RUN_ACCEPTANCE_SCHEMA_VERSION
        and _receipt_prereg_assignment(receipt).get("kind") == "pending"
    ):
        return reduced
    if reduced.get("pending_assignment_witnessed"):
        return reduced
    if not lock_held:
        root = Path(getattr(state, "root", "."))
        try:
            root.mkdir(parents=True, exist_ok=True)
            with exclusive(root / ".run_acceptance_receipt.lock"):
                return _ensure_pending_assignment_witness(
                    state,
                    _reduce_run_acceptance_receipts(state),
                    lock_held=True,
                )
        except OSError as exc:
            return {
                **reduced,
                "passed": False,
                "status": "run_authority_receipt_lock_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
    payload = _pending_assignment_witness_payload(receipt)
    if payload is None:
        return reduced
    try:
        state.append_transcript(
            _PREREG_ASSIGNMENT_PENDING_EVENT,
            **payload,
        )
    except Exception as exc:
        return {
            **reduced,
            "passed": False,
            "status": "run_authority_pending_witness_write_failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return _reduce_run_acceptance_receipts(state)


def _run_acceptance_missing_for_existing_scope(
    state: Any,
    acceptance: dict[str, Any],
) -> bool:
    """Identify a classified pre-receipt run without reconstructing authority."""
    if acceptance.get("status") != "run_authority_receipt_missing":
        return False
    scope_record = (getattr(state, "hook_state", {}) or {}).get(
        "experiment_execution_scope"
    )
    return bool(
        acceptance.get("legacy_scope_seen")
        or isinstance(scope_record, dict)
    )


def _execution_mode_view_from_acceptance(
    state: Any,
    acceptance: dict[str, Any],
) -> dict[str, Any]:
    """Project classification presence, effective mode, and its source.

    The transcript reducer owns valid receipt-backed scope.  The hook cache is
    retained only for pre-receipt legacy runs and invalid/empty record shape.
    This projection never scans the project artifact catalog.
    """
    cached_scope = (getattr(state, "hook_state", {}) or {}).get(
        "experiment_execution_scope"
    )
    durable_scope = acceptance.get("durable_scope_projection")
    scope_record = (
        durable_scope if isinstance(durable_scope, dict) else None
    ) if acceptance.get("passed") else cached_scope
    raw_mode = (
        str(scope_record.get("mode") or "").strip().lower()
        if isinstance(scope_record, dict)
        else ""
    )
    classified = raw_mode in _EXECUTION_MODES
    receipt = acceptance.get("receipt")
    binding = (
        receipt.get("governing_task_input_binding")
        if isinstance(receipt, dict)
        else None
    )
    receipt_forces_scientific = bool(
        classified
        and acceptance.get("passed")
        and _prereg_binding_state(binding) == "valid"
    )
    mode = "scientific" if receipt_forces_scientific else raw_mode
    present = isinstance(scope_record, dict)
    status = (
        "classified"
        if classified
        else ("invalid" if present and raw_mode else "absent")
    )
    return {
        "mode": mode if classified else None,
        "classified": classified,
        "classification_present": classified or present,
        "status": status,
        "source": (
            "run_acceptance_receipt"
            if receipt_forces_scientific
            else ("classification_result" if classified else "unclassified")
        ),
        "_scope_record": scope_record,
    }


def _resolve_exact_frozen_prereg(
    state: Any,
    *,
    artifact_id: str,
    version: int,
    expected_content_hash: str = "",
) -> dict[str, Any]:
    """Read one caller/receipt-selected prereg version without scanning peers."""
    from core.ledger import record_version as _rv
    from core.ledger import sha256_text as _sha

    try:
        exact = next(
            (
                record
                for record in state.artifact_versions(artifact_id)
                if _rv(record) == version
            ),
            None,
        )
    except Exception as exc:
        return {
            "passed": False,
            "status": "read_failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(exact, dict) or exact.get("type") != "pre_registration":
        return {
            "passed": False,
            "status": "not_found",
            "reason": "the exact preregistration version is unavailable",
        }
    metadata = exact.get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata.get("frozen"):
        return {
            "passed": False,
            "status": "not_frozen",
            "reason": "the exact preregistration version is not frozen",
        }
    content_hash = (
        str(exact.get("content_hash") or "")
        or _sha(str(exact.get("content") or ""))
    )
    if expected_content_hash and content_hash != expected_content_hash:
        return {
            "passed": False,
            "status": "content_hash_mismatch",
            "reason": "the exact preregistration content_hash does not match",
            "actual_content_hash": content_hash,
        }
    return {
        "passed": True,
        "status": "resolved",
        "metadata": metadata,
        "content_hash": content_hash,
    }


def _observe_forwarded_prereg_ids(
    state: Any,
    forwarded_ids: set[str],
) -> dict[str, Any]:
    """Check only caller-forwarded identities for an explicit-none conflict."""
    prereg_ids: list[str] = []
    try:
        for artifact_id in sorted(forwarded_ids):
            record = state.read_artifact(artifact_id)
            if isinstance(record, dict) and record.get("type") == "pre_registration":
                prereg_ids.append(artifact_id)
    except Exception as exc:
        return {
            "passed": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {"passed": True, "prereg_ids": prereg_ids}


def _scan_prereg_catalog(
    state: Any,
    *,
    declared_id: str = "",
    declared_version: int | None = None,
) -> dict[str, Any]:
    """Observe prereg heads once for selection and a non-authorizing witness.

    The visibility witness records exact catalog facts only.  It never selects
    a governing input and is used solely to distinguish preregs already visible
    when a null receipt was accepted from preregs that appeared later.
    """
    from core.ledger import record_version as _rv
    from core.ledger import sha256_text as _sha

    frozen: list[tuple[str, dict[str, Any]]] = []
    bound_versions: dict[str, tuple[int, str]] = {}
    pending_amendments: list[dict[str, Any]] = []
    unfrozen_preregs: list[dict[str, Any]] = []
    visible_frozen: list[dict[str, Any]] = []
    visible_pending: list[dict[str, Any]] = []
    declared_not_found = False
    seen_ids: set[str] = set()

    def record_hash(record: dict[str, Any]) -> str:
        return (
            str(record.get("content_hash") or "")
            or _sha(str(record.get("content") or ""))
        )

    try:
        for entry in state.list_artifacts("pre_registration"):
            artifact_id = str(entry.get("id") or "")
            if not artifact_id:
                continue
            if artifact_id in seen_ids:
                return {
                    "passed": False,
                    "reason": f"duplicate preregistration identity in catalog: {artifact_id}",
                }
            seen_ids.add(artifact_id)
            record = state.read_artifact(artifact_id)
            if not isinstance(record, dict):
                continue
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict):
                continue

            latest_frozen = None
            if metadata.get("frozen"):
                visible_frozen.append({
                    "artifact_id": artifact_id,
                    "version": _rv(record),
                    "content_hash": record_hash(record),
                })
            else:
                latest_frozen = state.latest_frozen_artifact(artifact_id)
                if isinstance(latest_frozen, dict):
                    visible_pending.append({
                        "artifact_id": artifact_id,
                        "draft_version": _rv(record),
                        "draft_content_hash": record_hash(record),
                        "latest_frozen_version": _rv(latest_frozen),
                        "latest_frozen_content_hash": record_hash(latest_frozen),
                    })
                else:
                    # A never-frozen draft is not governing authority and does
                    # not enter the null-receipt visibility witness.  Preserve
                    # its exact identity solely so an explicitly forwarded
                    # caller choice cannot disappear during selection.
                    unfrozen_preregs.append({
                        "artifact_id": artifact_id,
                        "draft_version": _rv(record),
                        "draft_content_hash": record_hash(record),
                    })

            if declared_id == artifact_id and declared_version is not None:
                exact = next(
                    (
                        version_record
                        for version_record in state.artifact_versions(artifact_id)
                        if _rv(version_record) == declared_version
                    ),
                    None,
                )
                exact_metadata = (
                    exact.get("metadata") or {}
                    if isinstance(exact, dict) else {}
                )
                if isinstance(exact_metadata, dict) and exact_metadata.get("frozen"):
                    frozen.append((artifact_id, exact_metadata))
                    bound_versions[artifact_id] = (
                        declared_version,
                        record_hash(exact),
                    )
                else:
                    declared_not_found = True
                continue

            if metadata.get("frozen"):
                frozen.append((artifact_id, metadata))
                bound_versions[artifact_id] = (
                    _rv(record), record_hash(record),
                )
                continue
            if not isinstance(latest_frozen, dict):
                continue                       # 纯草稿，从未冻结 → 不是候选
            lf_version = _rv(latest_frozen)
            lf_meta = latest_frozen.get("metadata") or {}
            if declared_id == artifact_id and declared_version == lf_version:
                # 调度方显式声明本轮按旧冻结版跑 —— 合法出口，绑那一版。
                frozen.append((
                    artifact_id,
                    lf_meta if isinstance(lf_meta, dict) else {},
                ))
                bound_versions[artifact_id] = (
                    lf_version, record_hash(latest_frozen),
                )
            else:
                pending_amendments.append({
                    "artifact_id": artifact_id,
                    "draft_version": _rv(record),
                    "latest_frozen_version": lf_version,
                    "amendment_reason": (record.get("amendment") or {}).get("reason"),
                })
    except Exception as exc:
        return {
            "passed": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    visibility = {
        "frozen": sorted(
            visible_frozen,
            key=lambda item: (
                item["artifact_id"], item["version"], item["content_hash"],
            ),
        ),
        "pending": sorted(
            visible_pending,
            key=lambda item: (
                item["artifact_id"],
                item["draft_version"],
                item["draft_content_hash"],
                item["latest_frozen_version"],
                item["latest_frozen_content_hash"],
            ),
        ),
    }
    normalized, error = _validated_prereg_visibility_witness(visibility)
    if normalized is None:
        return {
            "passed": False,
            "reason": f"invalid prereg visibility observation: {error}",
        }
    return {
        "passed": True,
        "frozen": frozen,
        "bound_versions": bound_versions,
        "pending_amendments": pending_amendments,
        "unfrozen_preregs": sorted(
            unfrozen_preregs,
            key=lambda item: (
                item["artifact_id"],
                item["draft_version"],
                item["draft_content_hash"],
            ),
        ),
        "declared_not_found": declared_not_found,
        "visibility_witness": normalized,
    }


def _scientific_null_receipt_conflict(
    state: Any,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    """Find prereg facts that a null receipt cannot authorize for science.

    A null acceptance receipt is positive evidence that an operation accepted
    no governing preregistration. It may remain stable while unrelated project
    preregs appear. That operation-only property must not become scientific
    authority merely because the model later reclassifies the same run.
    Scientific work without any prereg remains legal; once a frozen or pending
    prereg is visible, however, a scientific run needs a fresh positive binding
    instead of inheriting the operation's null receipt.

    The accepted visibility witness is unioned with the current observation so
    deleting or replacing a previously visible prereg cannot launder the old
    null decision into scientific authority.
    """
    if _prereg_binding_state(
        receipt.get("governing_task_input_binding")
    ) != "absent":
        return None

    scan = _scan_prereg_catalog(state)
    if not scan.get("passed"):
        return {
            "kind": "scan_failed",
            "reason": str(scan.get("reason") or "prereg catalog scan failed"),
        }

    accepted_visibility = receipt.get("unbound_prereg_visibility_witness")
    if not isinstance(accepted_visibility, dict):
        accepted_visibility = _empty_prereg_visibility_witness()
    current_visibility = scan.get("visibility_witness")
    if not isinstance(current_visibility, dict):
        current_visibility = _empty_prereg_visibility_witness()

    def artifact_ids(*collections: Any) -> set[str]:
        return {
            str(item.get("artifact_id") or "")
            for collection in collections
            if isinstance(collection, list)
            for item in collection
            if isinstance(item, dict) and item.get("artifact_id")
        }

    frozen_ids = artifact_ids(
        accepted_visibility.get("frozen"),
        current_visibility.get("frozen"),
    )
    pending_ids = artifact_ids(
        accepted_visibility.get("pending"),
        current_visibility.get("pending"),
    )
    candidate_ids = frozen_ids | pending_ids
    if len(candidate_ids) > 1:
        return {
            "kind": "ambiguous",
            "ambiguous_preregs": sorted(candidate_ids),
        }
    if pending_ids:
        return {
            "kind": "amendment_pending",
            "pending_preregs": sorted(pending_ids),
        }
    if frozen_ids:
        return {
            "kind": "binding_missing",
            "visible_preregs": sorted(frozen_ids),
        }

    forwarded_ids = {
        str(item)
        for item in (
            (getattr(state, "hook_state", {}) or {}).get("forwarded_input_ids")
            or []
        )
        if isinstance(item, str) and item
    }
    forwarded_unfrozen = sorted(
        forwarded_ids
        & artifact_ids(scan.get("unfrozen_preregs"))
    )
    if forwarded_unfrozen:
        return {
            "kind": "forwarded_unavailable",
            "unavailable_forwarded_preregs": forwarded_unfrozen,
        }
    return None


def _acceptance_for_requested_mode(
    state: Any,
    acceptance: dict[str, Any],
    *,
    requested_mode: str | None,
) -> dict[str, Any]:
    """Apply the scientific positive-binding rule to one reduced receipt."""
    if not acceptance.get("passed"):
        return acceptance
    durable_scope = acceptance.get("durable_scope_projection")
    scientific_scope_reclassification = (
        requested_mode == "operational"
        and isinstance(durable_scope, dict)
        and durable_scope.get("mode") == "scientific"
    )
    if requested_mode != "scientific" and not scientific_scope_reclassification:
        return acceptance
    receipt = acceptance.get("receipt")
    if not isinstance(receipt, dict):
        return acceptance
    assignment_kind = _receipt_prereg_assignment(receipt).get("kind")
    if (
        receipt.get("schema_version") == _RUN_ACCEPTANCE_SCHEMA_VERSION
        and assignment_kind in {"pending", "none"}
    ):
        # A typed upstream decision that this run has no governing prereg is
        # authority, while v2 pending is a typed omission witness handled by the
        # single prereg_assignment_scientific_block at the scientific signal
        # boundary.  Neither may be reinterpreted from unrelated catalog count.
        return acceptance
    conflict = _scientific_null_receipt_conflict(state, receipt)
    if conflict is None:
        return acceptance

    guarded = dict(acceptance)
    guarded["passed"] = False
    guarded["scientific_prereg_conflict"] = conflict
    if scientific_scope_reclassification:
        guarded["status"] = "run_authority_scientific_scope_downgrade_blocked"
        guarded["reason"] = (
            "the existing scientific scope has a preregistration conflict; "
            "reclassifying it as an operation cannot restore execution authority"
        )
        return guarded
    kind = conflict["kind"]
    if kind == "scan_failed":
        guarded["status"] = "run_authority_prereg_scan_failed"
        guarded["reason"] = conflict["reason"]
    elif kind == "ambiguous":
        guarded["status"] = "run_authority_prereg_ambiguous"
        guarded["ambiguous_preregs"] = conflict["ambiguous_preregs"]
        guarded["reason"] = (
            "the operation acceptance receipt is unbound, while scientific "
            "classification sees multiple possible preregistrations"
        )
    elif kind == "amendment_pending":
        guarded["status"] = "run_authority_prereg_amendment_pending"
        guarded["pending_preregs"] = conflict["pending_preregs"]
        guarded["reason"] = (
            "the operation acceptance receipt is unbound, while scientific "
            "classification sees a pending preregistration amendment"
        )
    elif kind == "forwarded_unavailable":
        guarded["status"] = "run_authority_forwarded_prereg_unavailable"
        guarded["unavailable_forwarded_preregs"] = conflict[
            "unavailable_forwarded_preregs"
        ]
        guarded["reason"] = (
            "the caller-forwarded preregistration is not frozen and cannot be "
            "bound by this scientific run"
        )
    else:
        guarded["status"] = "run_authority_scientific_prereg_binding_missing"
        guarded["visible_preregs"] = conflict["visible_preregs"]
        guarded["reason"] = (
            "the operation acceptance receipt is unbound, while scientific "
            "classification now requires an exact preregistration binding"
        )
    # P0a v3 B3（Codex 复审 20 号）：升级前开始的 run（v1 收据）从这条分支出去时，
    # 也要给与 pending 分支同源的迁移出口，不能一条说"新开 run"、另一条只说"缺绑定"。
    if receipt.get("schema_version") == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION:
        guarded["legacy_run"] = True
        guarded["legacy_receipt_schema_version"] = _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        guarded["reason"] = (
            str(guarded.get("reason") or "")
            + "; this run started before the upgrade (v1 run-acceptance receipt cannot carry a "
              "typed prereg_assignment) and cannot be repaired in place: the dispatching "
              "parent must start a new run with a typed bound or typed none assignment, and "
              "this run should close as blocked"
        )
    return guarded


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def load_run_contract(state: Any) -> dict[str, Any]:
    """Read the structured run contract without guessing from free text.

    首次分类前只接受 explicit node input 或 selected-input handoff；项目
    catalog 只提供候选观察，绝不自动认领。RunAcceptanceReceipt 一旦存在，
    它就是本 run 唯一 prereg identity 来源：typed assignment 只读该收据；损坏、
    冲突或不可读的收据显式 fail-closed。只有尚未分类且收据缺失时
    才扫描当前项目候选；已有 scope 的 run 不得从活目录重建权威。
    缺角色信息一律 fail-closed：secondary + 不可用于确证。

    ``state.hook_state['run_contract']`` 仍然会被读取，作为编排层直接注入契约的
    扩展点。**但它不是"首选来源"**——全仓没有任何地方写过这个键。此处原本的
    docstring 把它写成首选，于是正路是条死支，消费方永远落到扫描猜测的兜底分支，
    而"文档说有"让这个断路整整三轮 E2E 没被发现。广告必须和默认行为一致。
    """
    candidates: list[tuple[str, dict[str, Any]]] = []
    hook_contract = getattr(state, "hook_state", {}).get("run_contract")
    if isinstance(hook_contract, dict):
        candidates.append(("hook_state", hook_contract))

    # prereg 是 **Analysis 节点**的产物，落在 `plan/artifacts/`；本节点的
    # `state.artifacts_dir` 在 v2.1 workspace-first 之后只指向 `experiments/artifacts/`。
    # 原来这里直接 glob 自己的目录 —— 于是**永远找不到预注册**，contract_source
    # 恒为 "default"，每个 run 都被记成 secondary / analysis_eligible=false。
    #
    # E2E v14/v15/v16 三轮全部栽在这上面：Analysis 那边把 run_role /
    # analysis_eligible / expected_params 声明得再完整，消费方也看不见。
    #
    # v2.1 的规矩是"读跨节点、写不跨节点"，跨节点读的正规入口就是
    # state.list_artifacts()（默认扫全 worktree）+ read_artifact()。用它。
    # 调度方指名的那份 prereg 才是本轮的权威。
    #
    # 这里原本只有"扫全 worktree 找冻结 prereg"一条路，而合并用 setdefault
    # （先见者赢）+ list_artifacts 按文件名排序 —— 一个项目只有一份 prereg 时
    # 碰巧对，多份时就是靠文件名抽签。E2E v20 实测：跨 session 复利打通后，
    # 上一轮的 prereg 随 project main 继承进来，`..._KA_LJ.json`（旧）排在
    # `..._KA_LJ_v11_lowT.json`（新）前面，experiment 拿上一轮的 cooling_steps
    # 去卡本轮方案，怎么提交都对不上、且没有 amendment 工具可自救。
    #
    # 换成时间戳排序只是把抽签换成更好听的启发式 —— 同一 session 里为两个子
    # 问题各冻一份 prereg 完全合法，那时时间戳照样猜错。**根子是"本轮受哪份
    # 约束"该由调度方声明，消费方不该猜**。本函数 docstring 早就写着首选来源
    # 是 hook_state['run_contract']，但全仓没有任何地方写过那个键 —— 正路是条
    # 死支，于是永远走兜底猜测。
    #
    # 现在：node_inputs.prereg_assignment 或 legacy prereg_artifact_id（executor
    # 已把 node_inputs 落进 hook_state）= 权威声明。没声明始终是 pending；
    # catalog 仅作为 non-authorizing observation 回传给调度方。
    hook_state = getattr(state, "hook_state", {}) or {}
    node_declared_id = ""
    declared_stage = ""
    node_declared_version: int | None = None
    node_declared_content_hash = ""
    node_declared_version_invalid: dict[str, Any] | None = None
    input_assignment: dict[str, Any] | None = None
    input_assignment_error: str | None = None
    declared_not_found = False
    node_inputs = hook_state.get("node_inputs")
    if isinstance(node_inputs, dict):
        node_declared_id = str(node_inputs.get("prereg_artifact_id") or "").strip()
        declared_stage = str(node_inputs.get("stage") or "").strip().lower()
        if "prereg_assignment" in node_inputs:
            input_assignment, input_assignment_error = (
                _validated_prereg_assignment(
                    node_inputs.get("prereg_assignment"),
                    require_bound_source=False,
                )
            )
            if (
                isinstance(node_inputs.get("prereg_assignment"), dict)
                and "source" in node_inputs["prereg_assignment"]
            ):
                input_assignment = None
                input_assignment_error = (
                    "caller prereg_assignment must not provide receipt source"
                )
            if input_assignment and input_assignment.get("kind") == "pending":
                input_assignment = None
                input_assignment_error = (
                    "pending prereg assignment is represented only by an omitted "
                    "field; callers may provide only bound or none"
                )
            if node_declared_id or "prereg_version" in node_inputs:
                input_assignment = None
                input_assignment_error = (
                    "prereg_assignment cannot be combined with legacy "
                    "prereg_artifact_id/prereg_version"
                )
            elif input_assignment and input_assignment["kind"] == "bound":
                node_declared_id = str(input_assignment["artifact_id"])
                node_declared_version = int(input_assignment["version"])
                node_declared_content_hash = str(
                    input_assignment["content_hash"]
                )
        if "prereg_version" in node_inputs:
            raw_version = node_inputs["prereg_version"]
            node_declared_version, node_declared_version_invalid = (
                _normalized_prereg_version_input(raw_version)
            )
            if node_declared_version is not None and not node_declared_id:
                node_declared_version_invalid = {
                    "provided_type": type(raw_version).__name__,
                    "reason": "prereg_version requires prereg_artifact_id",
                }
    forwarded_ids = {
        str(item)
        for item in (hook_state.get("forwarded_input_ids") or [])
        if isinstance(item, str) and item
    }

    acceptance = _reduce_run_acceptance_receipts(state)
    execution_mode_view = _execution_mode_view_from_acceptance(state, acceptance)
    # A valid receipt makes the transcript projection canonical. hook_state is
    # only a legacy cache and cannot authorize a scope missing from the ledger.
    scope_record = execution_mode_view["_scope_record"]
    acceptance_status = str(acceptance.get("status") or "")
    acceptance_missing = acceptance_status == "run_authority_receipt_missing"
    acceptance_missing_for_existing_scope = (
        _run_acceptance_missing_for_existing_scope(state, acceptance)
    )
    preclassification_acceptance_missing = (
        acceptance_missing and not acceptance_missing_for_existing_scope
    )
    explicit_none = bool(
        preclassification_acceptance_missing
        and isinstance(input_assignment, dict)
        and input_assignment.get("kind") == "none"
    )
    explicit_typed_bound = bool(
        preclassification_acceptance_missing
        and isinstance(input_assignment, dict)
        and input_assignment.get("kind") == "bound"
    )
    acceptance_receipt = (
        acceptance.get("receipt")
        if acceptance.get("passed") and isinstance(acceptance.get("receipt"), dict)
        else {}
    )
    run_authority_failure = acceptance_missing_for_existing_scope or (
        not acceptance_missing and not acceptance.get("passed", False)
    )
    durable_obligation_witness = acceptance.get(
        "durable_verdict_obligation_witness"
    )
    accepted_binding = acceptance_receipt.get("governing_task_input_binding")
    accepted_assignment = _receipt_prereg_assignment(acceptance_receipt)
    accepted_binding_valid = _prereg_binding_state(accepted_binding) == "valid"
    if accepted_binding_valid:
        declared_id = str(accepted_binding["artifact_id"])
        declared_version = int(accepted_binding["version"])
    elif preclassification_acceptance_missing:
        declared_id = node_declared_id
        declared_version = node_declared_version
    else:
        # Positive null is an explicit unbound decision.  A malformed receipt
        # stream must likewise fail closed instead of reviving live selection.
        declared_id = ""
        declared_version = None

    # ── 身份 + 版本（RFC 2026-08-18）────────────────────────────────────────
    # 一个文件 = 一个 prereg **身份**；head 永远是当前版本。三种局面：
    #   head 已冻结          → 正常候选，绑 head 版本
    #   head 是未冻结修订稿  → **不许静默退回旧冻结版**（那是"赶在修订落地前
    #     抢跑一轮"的作弊窗口）。fail-loud：要么冻结修订稿再派发，要么调度方
    #     显式 node_inputs.prereg_version=<旧冻结版号> 声明本轮按旧版跑。
    #   从未冻结过           → 不是候选（草稿的声明不作数）
    frozen: list[tuple[str, dict[str, Any]]] = []          # (id, bound metadata)
    bound_versions: dict[str, tuple[int, str]] = {}        # id -> (version, content_hash)
    pending_amendments: list[dict[str, Any]] = []
    unfrozen_preregs: list[dict[str, Any]] = []
    direct_forwarded_prereg_choices: list[str] = []
    prereg_scan_error: str | None = None
    prereg_visibility_witness: dict[str, list[dict[str, Any]]] | None = None
    if accepted_binding_valid:
        # Resolve only the receipt-bound exact version.  Unrelated catalog heads
        # cannot change this run or make its contract scan fail.
        exact = _resolve_exact_frozen_prereg(
            state,
            artifact_id=declared_id,
            version=declared_version,
            expected_content_hash=str(accepted_binding.get("content_hash") or ""),
        )
        if exact.get("passed"):
            frozen.append((declared_id, exact["metadata"]))
            bound_versions[declared_id] = (
                declared_version,
                str(exact["content_hash"]),
            )
        elif exact.get("status") == "read_failed":
            prereg_scan_error = str(exact.get("reason") or "exact prereg read failed")
        else:
            declared_not_found = True
    elif explicit_typed_bound:
        # A caller-supplied exact identity is complete authority.  Resolve that
        # version only; an unrelated malformed catalog entry cannot veto it.
        exact = _resolve_exact_frozen_prereg(
            state,
            artifact_id=declared_id,
            version=int(declared_version or 0),
            expected_content_hash=node_declared_content_hash,
        )
        if exact.get("passed"):
            frozen.append((declared_id, exact["metadata"]))
            bound_versions[declared_id] = (
                int(declared_version or 0),
                str(exact["content_hash"]),
            )
        elif exact.get("status") == "read_failed":
            prereg_scan_error = str(exact.get("reason") or "exact prereg read failed")
        elif exact.get("status") == "content_hash_mismatch":
            input_assignment_error = str(exact.get("reason"))
        else:
            declared_not_found = True
    elif explicit_none:
        # Explicit none needs no catalog observation.  Only identities the
        # caller actually forwarded can contradict that explicit decision.
        forwarded = _observe_forwarded_prereg_ids(state, forwarded_ids)
        if forwarded.get("passed"):
            direct_forwarded_prereg_choices = list(forwarded["prereg_ids"])
        else:
            prereg_scan_error = str(
                forwarded.get("reason") or "forwarded prereg read failed"
            )
    elif preclassification_acceptance_missing:
        scan = _scan_prereg_catalog(
            state,
            declared_id=declared_id,
            declared_version=declared_version,
        )
        if scan.get("passed"):
            frozen = list(scan["frozen"])
            bound_versions = dict(scan["bound_versions"])
            pending_amendments = list(scan["pending_amendments"])
            unfrozen_preregs = list(scan["unfrozen_preregs"])
            declared_not_found = bool(scan["declared_not_found"])
            prereg_visibility_witness = dict(scan["visibility_witness"])
        else:
            prereg_scan_error = str(scan.get("reason") or "prereg catalog scan failed")
    # A valid null receipt and invalid/conflicting/unreadable receipt streams do
    # not scan the live catalog.  The latter are exposed as invalid below.

    ambiguous: list[str] = []
    selected_prereg_id = ""
    prereg_binding_source = "none"
    forwarded_preregs = sorted(
        artifact_id for artifact_id, _ in frozen if artifact_id in forwarded_ids
    )
    forwarded_pending_preregs = sorted(
        str(item.get("artifact_id") or "")
        for item in pending_amendments
        if (
            isinstance(item, dict)
            and str(item.get("artifact_id") or "") in forwarded_ids
        )
    )
    forwarded_unfrozen_preregs = sorted(
        str(item.get("artifact_id") or "")
        for item in unfrozen_preregs
        if (
            isinstance(item, dict)
            and str(item.get("artifact_id") or "") in forwarded_ids
        )
    )
    forwarded_prereg_choices = sorted(set(
        forwarded_preregs
        + forwarded_pending_preregs
        + forwarded_unfrozen_preregs
        + direct_forwarded_prereg_choices
    ))
    if explicit_none and forwarded_prereg_choices:
        input_assignment_error = (
            "typed none prereg_assignment conflicts with a caller-forwarded "
            "preregistration"
        )
    if accepted_binding_valid:
        chosen = [(i, m) for i, m in frozen if i == declared_id]
        if chosen:
            selected_prereg_id = chosen[0][0]
            prereg_binding_source = str(acceptance_receipt.get("binding_source") or "none")
            candidates.append((f"artifact:{chosen[0][0]}", chosen[0][1]))
        else:
            declared_not_found = True
    elif not preclassification_acceptance_missing:
        # The receipt reducer, not current node inputs or catalog state, owns the
        # prereg identity once any receipt state exists.  A missing receipt after
        # scope classification is likewise invalid, never permission to rebind.
        pass
    elif node_declared_id:
        chosen = [(i, m) for i, m in frozen if i == declared_id]
        if chosen:
            selected_prereg_id = chosen[0][0]
            prereg_binding_source = "explicit_node_input"
            bound_version, bound_hash = bound_versions[selected_prereg_id]
            if (
                node_declared_content_hash
                and node_declared_content_hash != bound_hash
            ):
                input_assignment_error = (
                    "typed bound prereg_assignment content_hash does not match "
                    "the exact frozen artifact version"
                )
                selected_prereg_id = ""
            else:
                candidates.append((f"artifact:{chosen[0][0]}", chosen[0][1]))
        else:
            # 指名了一份不存在/未冻结的 —— 不能默默退回扫描，那等于声明无效。
            declared_not_found = True
    elif explicit_none:
        pass
    elif len(forwarded_prereg_choices) > 1:
        # A pending amendment is still a caller-selected prereg identity.  It
        # cannot disappear from ambiguity merely because it is not executable.
        ambiguous = forwarded_prereg_choices
    elif len(forwarded_preregs) == 1:
        selected_prereg_id = forwarded_preregs[0]
        prereg_binding_source = "selected_input"
        chosen = [(i, m) for i, m in frozen if i == selected_prereg_id]
        candidates.append((f"artifact:{chosen[0][0]}", chosen[0][1]))
    elif forwarded_unfrozen_preregs:
        # A sole forwarded pure draft is an explicit but unavailable caller
        # choice.  Do not fall through and auto-claim an unrelated frozen item.
        pass
    # Unselected catalog entries, whether zero, one, or many, are observations
    # only. Multiple forwarded preregs above remain an upstream-selection
    # conflict; multiple unrelated project entries do not become authority.

    # 合并而不是遇到第一个非空候选就停止：编排器可能只把 actual-facing
    # expected_params 放在 hook_state，而角色/协议引用仍只在 prereg metadata。
    # hook_state 优先覆盖同名字段，缺失字段从 prereg 补齐。
    source = "default"
    raw: dict[str, Any] = {}
    sources: list[str] = []
    run_role_from_hook_state = False
    for candidate_source, candidate in candidates:
        fields = {key: candidate[key] for key in _RUN_CONTRACT_KEYS if key in candidate}
        # Input-delivery fallback is an upstream scientific authorization, not a
        # runtime tuning knob.  A hook contract may supplement operational facts,
        # but can never create or override this frozen preregistration policy.
        if candidate_source == "hook_state":
            fields.pop("input_delivery_policy", None)
        if not fields:
            continue
        if candidate_source == "hook_state":
            raw.update(fields)
            if "run_role" in fields:
                run_role_from_hook_state = True
        else:
            for key, value in fields.items():
                raw.setdefault(key, value)
        sources.append(candidate_source)
    if declared_stage:
        raw["stage"] = declared_stage
    # Scope is classified by Experiment after reading the request, never supplied by
    # the caller.  It is an auditable node-local decision, not command-text guessing.
    # Keep presence/source beside the same receipt-backed projection that supplies
    # the mode.  Consumers must not infer either one from the mutable hook cache.
    execution_mode_classified = execution_mode_view["classified"]
    if isinstance(scope_record, dict):
        raw["execution_mode"] = (
            execution_mode_view["mode"]
            if execution_mode_classified
            else str(scope_record.get("mode") or "").strip().lower()
        )
        raw["operation_kind"] = (
            str(scope_record.get("category") or "").strip().lower()
            if execution_mode_view["mode"] == "operational"
            else ""
        )
    if sources:
        source = "+".join(sources)

    if declared_not_found:
        # 声明无效必须盖过 "default" —— 否则看起来像"没人声明过"，
        # 调用方不会意识到自己指错了 id。必须在组装 contract 之前改。
        source = "declared_prereg_not_found"
    if node_declared_version_invalid and preclassification_acceptance_missing:
        source = "declared_prereg_version_invalid"

    execution_contract = raw.get("execution_contract")
    execution_contract_valid = execution_contract is None
    if isinstance(execution_contract, dict):
        execution_contract_valid = (
            execution_contract.get("version") == 1
            and isinstance(execution_contract.get("scientific_params"), dict)
            and isinstance(execution_contract.get("runtime_params", {}), dict)
        )
        if execution_contract_valid:
            raw["expected_params"] = execution_contract["scientific_params"]

    declared_role = str(raw.get("run_role") or "").strip().lower()
    if declared_role == "primary":
        role = "primary"
        run_role_source = (
            RUN_ROLE_SOURCE_LEGACY_HOOK_PRIMARY
            if run_role_from_hook_state
            else RUN_ROLE_SOURCE_DECLARED_PRIMARY
        )
    elif declared_role == "secondary":
        role = "secondary"
        run_role_source = (
            RUN_ROLE_SOURCE_LEGACY_HOOK_SECONDARY
            if run_role_from_hook_state
            else RUN_ROLE_SOURCE_DECLARED_SECONDARY
        )
    elif not declared_role:
        role = "secondary"
        run_role_source = (
            RUN_ROLE_SOURCE_LEGACY_HOOK_UNDECLARED
            if run_role_from_hook_state
            else RUN_ROLE_SOURCE_UNDECLARED_SECONDARY
        )
    else:
        role = "secondary"
        run_role_source = (
            RUN_ROLE_SOURCE_LEGACY_HOOK_INVALID
            if run_role_from_hook_state
            else "invalid_declared_defaulted_secondary"
        )
        source = f"{source}:invalid_role"

    execution_mode = str(raw.get("execution_mode") or "scientific").strip().lower()
    warnings: list[str] = []
    prior_scope_warnings = (scope_record.get("contract_warnings")
                            if isinstance(scope_record, dict) else [])
    if isinstance(prior_scope_warnings, list):
        warnings.extend(str(item) for item in prior_scope_warnings if isinstance(item, str) and item)
    if execution_mode not in _EXECUTION_MODES:
        warnings.append("execution_scope_invalid_defaulted_to_scientific")
        execution_mode = "scientific"
    elif "execution_mode" not in raw:
        # Legacy preregs did not encode the execution mode.  Do not let an
        # agent downgrade a bound scientific commitment to operation merely by
        # classifying free text: retain the conservative scientific default and
        # leave an auditable migration signal for the upstream producer.
        warnings.append("execution_mode_missing_legacy_default")
    complete_prereg_binding = bool(
        selected_prereg_id
        and selected_prereg_id in bound_versions
        and _prereg_binding_state({
            "artifact_id": selected_prereg_id,
            "version": bound_versions[selected_prereg_id][0],
            "content_hash": bound_versions[selected_prereg_id][1],
        }) == "valid"
    )
    # A complete frozen prereg binding is sufficient for scientific identity.
    # run_role still controls formal closure obligations, never this authority.
    if execution_mode == "operational" and complete_prereg_binding:
        warnings.append("operation_scope_conflicts_with_governing_prereg")
        execution_mode = "scientific"
    verdict_obligation_status = (
        "required"
        if execution_mode == "scientific" and role == "primary"
        else "not_required"
    )
    run_authority_identity_status = (
        "verified" if acceptance.get("passed") else "unbound"
    )
    if run_authority_failure:
        # Authority damage can never authorize execution.  It also cannot erase
        # a verdict duty that the append-only transcript already established.
        # With no complete durable witness, identity remains explicitly
        # unknown.  The independent verdict-obligation status stays
        # conservative without inventing a role or execution mode.
        if isinstance(durable_obligation_witness, dict):
            role = "primary"
            run_role_source = "durable_primary_witness"
            execution_mode = "scientific"
            source = "durable_scope_transcript:run_authority_failure"
            run_authority_identity_status = "durable_scope_witness"
            verdict_obligation_status = (
                "required_from_durable_scope_witness"
            )
        else:
            role = "unknown"
            run_role_source = "run_authority_unknown"
            execution_mode = "unknown"
            source = "run_authority_failure"
            run_authority_identity_status = "unknown"
            verdict_obligation_status = "unknown_fail_closed_required"
        warnings.append("run_authority_failed_verdict_obligation_preserved")
    operation_kind = str(raw.get("operation_kind") or "other").strip().lower()
    if execution_mode == "operational" and operation_kind not in _OPERATION_CATEGORIES:
        warnings.append("operation_category_invalid_defaulted_to_other")
        operation_kind = "other"

    # `analysis_eligible` 已从目标契约删除（AGENTS.md:143），节点不再解释它：
    # 所有门禁改读自己真正的判据（execution_mode / run_role /
    # requires_hypothesis_verdict）。这里只保留一个**派生只读别名**继续产出，
    # 因为仓库根 tests/test_run_contract_cross_node_read.py 有三处直接下标取这个键，
    # 而根测试不在本 author 的写边界内——单方面删键会把它们变成长红。
    # owner=framework（wangd/jerry）；删除条件：那三处断言改掉之后，连同
    # create_run_manifest 的同名字段与 artifact metadata 一起删。
    requires_hypothesis_verdict = verdict_obligation_status in {
        "required",
        "required_from_durable_scope_witness",
        "unknown_fail_closed_required",
    }
    # This legacy projection must never turn an unusable authority stream into
    # downstream eligibility.  It remains only for normal-contract consumers.
    analysis_eligible = requires_hypothesis_verdict and not run_authority_failure
    # 执行前提见证（原 O2 机械降格）：见证照记，但**不翻任何门**。
    # 被降格的运行仍然欠裁决——降格说的是结论有瑕疵，不是没有结论（owner 2026-09-11）。
    precondition_witnesses = getattr(
        state, "hook_state", {}).get(PRECONDITION_WITNESS_KEY) or []

    review_eligible = (_bool_value(raw.get("review_eligible"), default=True)
                       if execution_mode == "scientific" else False)
    if run_authority_failure:
        review_eligible = False

    if isinstance(execution_contract, dict) and not execution_contract_valid:
        warnings.append("execution_contract_invalid")
    contract = {
        key: raw[key] for key in _RUN_CONTRACT_KEYS if key in raw
    }
    contract.update({
        "run_role": role,
        "run_role_source": run_role_source,
        "execution_mode": execution_mode,
        "operation_kind": operation_kind if execution_mode == "operational" else None,
        "execution_scope_reason": (scope_record.get("reason")
                                   if isinstance(scope_record, dict) else None),
        "invocation": (scope_record.get("invocation")
                       if isinstance(scope_record, dict) else _invocation_context(state)),
        "analysis_eligible": analysis_eligible,
        "stage": str(raw.get("stage") or "simulation").strip().lower(),
        # #726 第一刀（2026-09-02）：去 stage。verdict 义务是 **run 级身份**问题——
        # 一个 primary scientific run 整体就要产结论,不该由 caller 嘴上的 stage
        # 字符串放行/豁免（stage=toolchain_build 曾能一个词逃掉科学裁决义务）。
        # stage 是 step 级执行类别,已由 execution_class 从路线派生掌管,不参与
        # 这里。stage 字段本身在下方保留为只读兼容,只是不再驱动本判定。
        # #726 目标态的精细版（"且已产正式科学执行证据"）需要把判定延后到收尾
        # （契约构建时尚无执行证据,鸡生蛋）,属后续重构;本刀只做"去 stage"。
        "requires_hypothesis_verdict": requires_hypothesis_verdict,
        "verdict_obligation_status": verdict_obligation_status,
        "review_eligible": review_eligible,
        "contract_source": source,
        "prereg_artifact_id": selected_prereg_id or None,
        "prereg_binding_source": prereg_binding_source,
        "execution_contract_valid": execution_contract_valid,
    })
    if acceptance.get("passed"):
        contract["prereg_assignment"] = accepted_assignment
    elif (
        preclassification_acceptance_missing
        and input_assignment_error is None
        and isinstance(input_assignment, dict)
    ):
        if input_assignment["kind"] == "bound" and selected_prereg_id:
            contract["prereg_assignment"] = {
                "kind": "bound",
                "artifact_id": selected_prereg_id,
                "version": bound_versions[selected_prereg_id][0],
                "content_hash": bound_versions[selected_prereg_id][1],
                "source": "explicit_node_input",
            }
        else:
            contract["prereg_assignment"] = dict(input_assignment)
    elif (
        preclassification_acceptance_missing
        and selected_prereg_id
        and selected_prereg_id in bound_versions
    ):
        contract["prereg_assignment"] = {
            "kind": "bound",
            "artifact_id": selected_prereg_id,
            "version": bound_versions[selected_prereg_id][0],
            "content_hash": bound_versions[selected_prereg_id][1],
            "source": prereg_binding_source,
        }
    else:
        contract["prereg_assignment"] = {"kind": "pending"}
    if isinstance(durable_obligation_witness, dict):
        contract["durable_verdict_obligation_witness"] = dict(
            durable_obligation_witness
        )
    contract["run_authority_identity_status"] = run_authority_identity_status
    if precondition_witnesses:
        contract["execution_precondition_witnesses"] = list(precondition_witnesses)
    if prereg_visibility_witness is not None:
        # Observation only: candidate creation may freeze this into a null
        # receipt, but it never selects or authorizes a governing prereg.
        contract["prereg_visibility_witness"] = prereg_visibility_witness
    if selected_prereg_id and selected_prereg_id in bound_versions:
        bound_v, bound_hash = bound_versions[selected_prereg_id]
        contract["prereg_version"] = bound_v
        contract["prereg_content_hash"] = bound_hash
    warnings = [
        item for item in warnings if item != RUN_ROLE_UNDECLARED_WARNING
    ]
    if run_role_source in {
        RUN_ROLE_SOURCE_UNDECLARED_SECONDARY,
        RUN_ROLE_SOURCE_LEGACY_HOOK_UNDECLARED,
    }:
        warnings.insert(0, RUN_ROLE_UNDECLARED_WARNING)
    if declared_not_found:
        contract["declared_prereg_id"] = declared_id
        contract["execution_contract_valid"] = False
    if node_declared_version_invalid and preclassification_acceptance_missing:
        contract["prereg_version_invalid"] = node_declared_version_invalid
        contract["execution_contract_valid"] = False
    if input_assignment_error:
        contract["prereg_assignment_invalid"] = input_assignment_error
        contract["execution_contract_valid"] = False
    if ambiguous:
        # 有歧义就带着歧义往下走，由执行契约门吵着挡住 —— 不在这里替调用方
        # 选一份。消费方（audit_execution_contract）会把候选列表和该怎么办
        # 一起报给模型。注意版本原语上线后，这里的歧义只剩**多个并行研究**
        # 一种真歧义 —— "同一研究的六稿"按构造不再出现。
        contract["ambiguous_preregs"] = ambiguous
    if forwarded_unfrozen_preregs and not node_declared_id:
        contract["unavailable_forwarded_preregs"] = forwarded_unfrozen_preregs
    if prereg_scan_error:
        contract["prereg_scan_error"] = prereg_scan_error
        contract["execution_contract_valid"] = False
    if (
        pending_amendments
        and not explicit_none
        and not (selected_prereg_id and selected_prereg_id in bound_versions)
    ):
        # 修订草稿挂着且本轮没有合法绑定 → 派发被挡。两条出路都写明。
        contract["pending_amendments"] = pending_amendments
        warnings.append("prereg_amendment_pending")
        contract["execution_contract_valid"] = False
    if acceptance_missing_for_existing_scope:
        contract["run_acceptance_status"] = (
            "run_authority_receipt_missing_for_existing_scope"
        )
        contract["run_acceptance_error"] = (
            "an existing classified scope has no acceptance receipt; start a "
            "fresh run instead of reconstructing authority"
        )
        contract["execution_contract_valid"] = False
        warnings.append("run_acceptance_receipt_invalid")
    elif not acceptance_missing and not acceptance.get("passed", False):
        contract["run_acceptance_status"] = acceptance_status
        contract["run_acceptance_error"] = str(
            acceptance.get("reason")
            or "the write-once run acceptance receipt is not usable"
        )
        contract["execution_contract_valid"] = False
        warnings.append("run_acceptance_receipt_invalid")
    if warnings:
        contract["contract_warning"] = ";".join(warnings)
        contract["contract_warnings"] = warnings
    return contract


def load_execution_mode_view(state: Any) -> dict[str, Any]:
    """Return the sole read view for Experiments effective execution mode.

    The view and ``load_run_contract`` share the same receipt/cache projection.
    Reading it reduces only the run transcript; it never scans project artifacts.
    A valid durable scope beats the mutable compatibility cache, while a genuine
    pre-receipt legacy classification keeps its existing behavior.
    """
    projected = _execution_mode_view_from_acceptance(
        state,
        _reduce_run_acceptance_receipts(state),
    )
    return {
        key: projected[key]
        for key in (
            "mode",
            "classified",
            "classification_present",
            "status",
            "source",
        )
    }


def observe_current_prereg_binding_witness(state: Any) -> dict[str, Any]:
    """Observe the binding the live catalog would currently select.

    This view is deliberately non-authorizing: it exists only as the right-hand
    witness for gates that compare current project facts with the write-once run
    receipt.  Callers must never use it as a substitute governing input.
    """
    hook_state = getattr(state, "hook_state", {}) or {}
    node_inputs = hook_state.get("node_inputs")
    declared_id = ""
    declared_version: int | None = None
    declared_content_hash = ""
    typed_assignment: dict[str, Any] | None = None
    if isinstance(node_inputs, dict):
        declared_id = str(node_inputs.get("prereg_artifact_id") or "").strip()
        if "prereg_assignment" in node_inputs:
            typed_assignment, assignment_error = _validated_prereg_assignment(
                node_inputs.get("prereg_assignment"),
                require_bound_source=False,
            )
            if (
                assignment_error is not None
                or declared_id
                or "prereg_version" in node_inputs
                or (
                    isinstance(node_inputs.get("prereg_assignment"), dict)
                    and "source" in node_inputs["prereg_assignment"]
                )
            ):
                return {
                    "passed": False,
                    "status": "prereg_assignment_invalid",
                    "binding": None,
                    "reason": assignment_error or (
                        "typed prereg_assignment cannot include source or be "
                        "combined with legacy prereg selectors"
                    ),
                }
            if typed_assignment and typed_assignment.get("kind") == "pending":
                return {
                    "passed": False,
                    "status": "prereg_assignment_invalid",
                    "binding": None,
                    "reason": (
                        "pending prereg assignment is represented only by an "
                        "omitted field"
                    ),
                }
            if typed_assignment and typed_assignment["kind"] == "bound":
                declared_id = str(typed_assignment["artifact_id"])
                declared_version = int(typed_assignment["version"])
                declared_content_hash = str(typed_assignment["content_hash"])
        if "prereg_version" in node_inputs:
            raw_version = node_inputs["prereg_version"]
            declared_version, version_error = _normalized_prereg_version_input(
                raw_version
            )
            if version_error is not None or (
                declared_version is not None and not declared_id
            ):
                return {
                    "passed": False,
                    "status": "prereg_version_invalid",
                    "binding": None,
                    "reason": (
                        "live prereg witness requires prereg_version to be a "
                        "positive integer (or integer string) paired with "
                        "prereg_artifact_id; null means no exact version selector"
                    ),
                }
    forwarded_ids = {
        str(item)
        for item in (hook_state.get("forwarded_input_ids") or [])
        if isinstance(item, str) and item
    }
    scan = _scan_prereg_catalog(
        state,
        declared_id=declared_id,
        declared_version=declared_version,
    )
    if not scan.get("passed"):
        return {
            "passed": False,
            "status": "prereg_visibility_scan_failed",
            "binding": None,
            "reason": str(scan.get("reason") or "prereg catalog scan failed"),
        }

    frozen = list(scan["frozen"])
    bound_versions = dict(scan["bound_versions"])
    pending = list(scan["pending_amendments"])
    unfrozen = list(scan["unfrozen_preregs"])
    selected_id = ""
    source = "none"
    ambiguous: list[str] = []
    visible_prereg_ids = {
        artifact_id for artifact_id, _metadata in frozen
    } | {
        str(item.get("artifact_id") or "")
        for item in (*pending, *unfrozen)
        if isinstance(item, dict) and item.get("artifact_id")
    }
    if (
        typed_assignment
        and typed_assignment.get("kind") == "none"
        and forwarded_ids.intersection(visible_prereg_ids)
    ):
        return {
            "passed": False,
            "status": "prereg_assignment_conflict",
            "binding": None,
            "reason": (
                "typed none prereg_assignment conflicts with a caller-forwarded "
                "preregistration"
            ),
            "visibility_witness": scan["visibility_witness"],
        }
    if typed_assignment and typed_assignment.get("kind") == "none":
        return {
            "passed": True,
            "status": "explicit_none",
            "binding": None,
            "binding_source": "none",
            "prereg_assignment": dict(typed_assignment),
            "ambiguous_preregs": [],
            "pending_amendments": pending,
            "visibility_witness": scan["visibility_witness"],
            "authorizing": False,
        }
    if declared_id:
        chosen = [item for item in frozen if item[0] == declared_id]
        if chosen:
            selected_id = declared_id
            source = "explicit_node_input"
            if (
                declared_content_hash
                and bound_versions[selected_id][1] != declared_content_hash
            ):
                return {
                    "passed": False,
                    "status": "declared_prereg_unavailable",
                    "binding": None,
                    "declared_prereg_id": declared_id,
                    "reason": "typed bound content_hash does not match",
                    "visibility_witness": scan["visibility_witness"],
                }
        else:
            return {
                "passed": False,
                "status": "declared_prereg_unavailable",
                "binding": None,
                "declared_prereg_id": declared_id,
                "visibility_witness": scan["visibility_witness"],
            }
    else:
        forwarded_preregs = sorted(
            artifact_id
            for artifact_id, _metadata in frozen
            if artifact_id in forwarded_ids
        )
        forwarded_pending_preregs = sorted(
            str(item.get("artifact_id") or "")
            for item in pending
            if (
                isinstance(item, dict)
                and str(item.get("artifact_id") or "") in forwarded_ids
            )
        )
        forwarded_unfrozen_preregs = sorted(
            str(item.get("artifact_id") or "")
            for item in unfrozen
            if (
                isinstance(item, dict)
                and str(item.get("artifact_id") or "") in forwarded_ids
            )
        )
        forwarded_prereg_choices = sorted(set(
            forwarded_preregs
            + forwarded_pending_preregs
            + forwarded_unfrozen_preregs
        ))
        if len(forwarded_prereg_choices) > 1:
            ambiguous = forwarded_prereg_choices
        elif len(forwarded_preregs) == 1:
            selected_id = forwarded_preregs[0]
            source = "selected_input"
        elif forwarded_unfrozen_preregs:
            return {
                "passed": False,
                "status": "forwarded_prereg_unavailable",
                "binding": None,
                "unavailable_forwarded_preregs": forwarded_unfrozen_preregs,
                "visibility_witness": scan["visibility_witness"],
            }
        elif len(frozen) > 1:
            ambiguous = sorted(artifact_id for artifact_id, _metadata in frozen)

    binding = None
    if selected_id:
        version, content_hash = bound_versions[selected_id]
        binding = {
            "artifact_id": selected_id,
            "version": version,
            "content_hash": content_hash,
        }
        if _prereg_binding_state(binding) != "valid":
            return {
                "passed": False,
                "status": "prereg_binding_invalid",
                "binding": None,
                "observed_binding": binding,
                "visibility_witness": scan["visibility_witness"],
            }
    return {
        "passed": True,
        "status": "bound" if binding is not None else "unbound",
        "binding": binding,
        "binding_source": source,
        "prereg_assignment": (
            {"kind": "bound", **binding, "source": source}
            if binding is not None else {"kind": "pending"}
        ),
        "ambiguous_preregs": ambiguous,
        "pending_amendments": pending,
        "visibility_witness": scan["visibility_witness"],
        "authorizing": False,
    }


def requires_experiment_fallback_input_gate(contract: dict[str, Any]) -> bool:
    """Whether a scientific run explicitly opted into formal Experiment fallback input.

    This is not an authorization decision; it only prevents a secondary run that
    already carries the frozen generic fallback policy from bypassing the same
    input and parameter gates used by a primary formal execution.
    """
    policy = contract.get("input_delivery_policy")
    return (
        str(contract.get("execution_mode") or "") == "scientific"
        and isinstance(policy, dict)
        and policy.get("mode") == "experiment_data_fallback"
        and policy.get("experiment_fallback_permitted") is True
    )


def load_bound_frozen_prereg(state: Any) -> dict[str, Any] | None:
    """Read exactly the frozen prereg identity/version/hash bound to this run.

    ``read_artifact`` returns an identity's current head, which may be an
    unfrozen amendment.  Consumers that close execution evidence must instead
    use the version selected by the run contract.  Keeping this here gives
    sediment, preflight and terminal audits one source of truth without a
    ``contract_audit -> sediment`` import cycle.
    """
    contract = load_run_contract(state)
    artifact_id = str(contract.get("prereg_artifact_id") or "").strip()
    if not artifact_id:
        return None
    try:
        bound_version = int(contract.get("prereg_version") or 0) or None
    except (TypeError, ValueError):
        bound_version = None
    bound_hash = str(contract.get("prereg_content_hash") or "").strip()

    def matches_binding(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict) or not metadata.get("frozen"):
            return False
        from core.ledger import record_version, sha256_text
        if bound_version is not None and record_version(record) != bound_version:
            return False
        # 账本记的 content_hash 就是那一版正文的哈希；历史版本的正文可能不在盘上
        # （在 git 里），但哈希永远在 —— 按它核对，不按可能为空的正文算。
        recorded = str(record.get("content_hash") or "")
        actual = recorded or sha256_text(str(record.get("content") or ""))
        return not bound_hash or actual == bound_hash

    head = state.read_artifact(artifact_id)
    if matches_binding(head):
        return {**head, "id": artifact_id}
    versions = getattr(state, "artifact_versions", None)
    if callable(versions):
        try:
            for record in versions(artifact_id):
                if matches_binding(record):
                    return {**record, "id": artifact_id}
        except Exception:
            # A broken version history must not fall back to an arbitrary head.
            return None
    return None


ACTUAL_PARAMS_KEY = "actual_run_params"
ACTUAL_PARAMS_SOURCES_KEY = "actual_run_params_sources"

#: 判决拆除 O2（2026-08-31 起）：执行前提见证账本。执行在缺乏验收/授权的前提下
#: 继续时，如实记下这件事——不拦执行。
#: 2026-09-11 owner 裁决：它**不再翻任何资格门**。原先它把 analysis_eligible 降为
#: False，而框架判"欠不欠裁决"只读 requires_hypothesis_verdict，所以那个降格对
#: 框架义务从来没有作用；而"结论有瑕疵"不等于"没有结论需要裁决"。见证如实进
#: manifest 与 transcript，由读的人自己判断分量。
PRECONDITION_WITNESS_KEY = "execution_precondition_witnesses"

#: 判决拆除 O1（2026-08-31）：prereg 偏离申报账本。偏离不再被拒绝，但必须
#: 申报 —— mismatched/missing/unexpected 三张表进 transcript、提交记录与 run manifest。
PREREG_DEVIATIONS_KEY = "prereg_deviations"


def record_execution_precondition_witness(
    state: Any, source: str, reason: str,
) -> None:
    """如实记下"这次执行在缺乏验收/授权的前提下继续了"（O2，不可在 run 内撤销）。

    只记账，不翻门：四个调用点断言的都是**事实**（输入包未验收、fallback 顶替了
    正式交付），不是资格裁定。
    """
    try:
        witnesses = state.hook_state.setdefault(PRECONDITION_WITNESS_KEY, [])
        entry = {"source": str(source), "reason": str(reason)}
        if isinstance(witnesses, list) and entry not in witnesses:
            witnesses.append(entry)
            try:
                state.append_transcript(
                    "execution_precondition_unmet", **entry)
            except Exception:
                pass
    except Exception:
        pass


def record_prereg_deviation(state: Any, source: str, detail: dict[str, Any]) -> None:
    """申报一次 prereg 偏离（O1）：三张表 + 触发点，进 transcript 与 manifest。"""
    try:
        deviations = state.hook_state.setdefault(PREREG_DEVIATIONS_KEY, [])
        entry = {"source": str(source), **{k: v for k, v in (detail or {}).items() if v not in (None, "", [], {})}}
        if isinstance(deviations, list) and entry not in deviations:
            deviations.append(entry)
            try:
                state.append_transcript("prereg_deviation_declared", **entry)
            except Exception:
                pass
    except Exception:
        pass


def record_actual_run_params(state: Any, source: str,
                             params: dict[str, Any]) -> None:
    """并入本次运行**机械观察到的**实际参数，供协议偏离审计使用。

    只接受结构化来源：工具入参（如 ``submit_job`` 的 mpi_ranks/walltime）或已
    解析的启动器选项（如 ``mpirun -np N``）。**绝不**接受 LLM 自述的参数 ——
    那等于让被审计方自己填审计表，会把伪造包装成"自动审计"。

    ``source`` 一并记录，便于复核每个字段的证据来源。
    """
    if not isinstance(params, dict):
        return
    clean = {key: value for key, value in params.items() if value is not None}
    if not clean:
        return
    try:
        store = state.hook_state.setdefault(ACTUAL_PARAMS_KEY, {})
        if isinstance(store, dict):
            store.update(clean)
        sources = state.hook_state.setdefault(ACTUAL_PARAMS_SOURCES_KEY, [])
        if isinstance(sources, list) and source not in sources:
            sources.append(source)
    except Exception:
        pass


def _run_status(loop_result: Any, override: str | None = None) -> str:
    if override:
        return override
    status = (
        loop_result.get("status")
        if isinstance(loop_result, dict)
        else getattr(loop_result, "status", None)
    )
    return str(status or "unknown")


def _existing_logs_dir(state: Any) -> Path:
    """Read new logs first, with a read-only fallback for historical runs."""
    current = experiment_output_dir(state, "runtime/logs")
    legacy = Path(getattr(state, "root", "") or "") / "logs"
    return legacy if not current.exists() and legacy.exists() else current


def _last_logged_return_code(state: Any) -> int | None:
    logs_dir = _existing_logs_dir(state)
    if not logs_dir.is_dir():
        return None
    latest = sorted(logs_dir.glob("*.log"))[-1:]
    if not latest:
        return None
    try:
        text = latest[0].read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r"^# returncode:\s*(-?\d+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def _index_run_logs(state: Any) -> list[dict[str, Any]]:
    """Index framework-owned logs; do not duplicate their contents."""
    root = Path(getattr(state, "root", "") or "")
    logs_dir = _existing_logs_dir(state)
    if not logs_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(logs_dir.glob("*.log")):
        try:
            records.append({
                "path": str(path.relative_to(root)),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
        except (OSError, ValueError):
            continue
    return records


def _human_write_grants(state: Any) -> list[dict[str, Any]]:
    """Paths a human approved for writing during this run.

    These live in ``hook_state['path_roles']`` and therefore survive a
    checkpoint/resume — deliberately, so a resumed run does not re-ask for
    every directory the operator already cleared.  Surviving the process is
    exactly why they must appear in the audit record: a grant that outlives
    the session it was given in should not be visible only as one transcript
    line in that session.
    """
    try:
        roles = state.hook_state.get("path_roles") or {}
        entries = roles.get("approved_write_root")
    except Exception:
        return []
    if not entries:
        return []
    if not isinstance(entries, list):
        entries = [entries]
    grants: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            path = entry.get("path")
            cleanup = entry.get("cleanup", "contents")
        else:
            path, cleanup = entry, "contents"
        if path:
            grants.append({
                "path": str(path),
                "authority": "human",
                "writable": True,
                "cleanup": cleanup,
                "survives_resume": True,
            })
    return sorted(grants, key=lambda g: g["path"])


def create_run_manifest(
    state: Any,
    result_info: dict[str, Any] | None = None,
    loop_result: Any = None,
    *,
    status: str | None = None,
    bundle: dict[str, Any] | None = None,
    experiment_log_path: Path | None = None,
) -> dict[str, Any]:
    """Write the small, per-run audit record.

    This is intentionally an index, not a second raw-data archive.  Large
    logs remain in the existing run directory and are represented by path,
    size, and SHA256 only.

    Returns the manifest itself: callers must read fields from the return
    value rather than reconstructing the on-disk path, which is what made the
    recovery hook read a filename that never existed.
    """
    root = Path(getattr(state, "root", "") or "")
    contract = load_run_contract(state)
    transcript_path = Path(getattr(state, "transcript_path", root / "transcript.jsonl"))
    try:
        transcript_rel = str(transcript_path.relative_to(root))
    except ValueError:
        transcript_rel = str(transcript_path)
    transcript = {"path": transcript_rel}
    if transcript_path.is_file():
        transcript.update({
            "size_bytes": transcript_path.stat().st_size,
            "sha256": sha256_file(transcript_path),
        })

    manifest: dict[str, Any] = {
        "schema_version": "run_manifest.v1",
        "run_id": getattr(state, "run_id", root.name),
        "project_id": getattr(state, "project_id", None),
        "experiment_id": contract.get("experiment_id"),
        "step_id": contract.get("step_id"),
        "run_role": contract["run_role"],
        "run_role_source": contract["run_role_source"],
        "execution_mode": contract["execution_mode"],
        "operation_kind": contract.get("operation_kind"),
        "execution_scope_reason": contract.get("execution_scope_reason"),
        "invocation": contract.get("invocation"),
        # 派生只读别名，等根测试改掉后与上面那条注释一起删（见 load_run_contract）。
        "analysis_eligible": contract["analysis_eligible"],
        "requires_hypothesis_verdict": contract["requires_hypothesis_verdict"],
        "verdict_obligation_status": contract["verdict_obligation_status"],
        "run_authority_identity_status": contract["run_authority_identity_status"],
        "review_eligible": contract["review_eligible"],
        "contract_source": contract["contract_source"],
        "protocol_version": contract.get("protocol_version"),
        # RFC 2026-08-18：本轮绑定的预注册版本三元组 —— "run 4-9 跑在 v2 下"
        # 从此机械可查，reviewer/writing 不必再猜哪版承诺治理了哪些结果。
        "prereg_version": contract.get("prereg_version"),
        "prereg_content_hash": contract.get("prereg_content_hash"),
        "dataset_id": contract.get("dataset_id"),
        "scope_manifest_id": contract.get("scope_manifest_id"),
        "protocol_amendment_id": contract.get("protocol_amendment_id"),
        "exclusion_reason": contract.get("exclusion_reason"),
        "status": _run_status(loop_result, override=status),
        "return_code": _last_logged_return_code(state),
        "result_info": result_info or {},
        "transcript": transcript,
        "logs": _index_run_logs(state),
        "raw_bundle": bundle or {
            "created": False,
            "reason": "not_finalized",
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if contract.get("run_acceptance_status"):
        manifest["run_acceptance_status"] = contract["run_acceptance_status"]
        manifest["run_acceptance_error"] = contract.get("run_acceptance_error")
    if isinstance(contract.get("durable_verdict_obligation_witness"), dict):
        manifest["durable_verdict_obligation_witness"] = dict(
            contract["durable_verdict_obligation_witness"]
        )
    observed_params = getattr(state, "hook_state", {}).get(ACTUAL_PARAMS_KEY)
    observed_sources = getattr(state, "hook_state", {}).get(ACTUAL_PARAMS_SOURCES_KEY)
    if isinstance(observed_params, dict):
        manifest["actual_run_params"] = observed_params
    if isinstance(observed_sources, list):
        manifest["actual_run_params_sources"] = observed_sources
    # 判决拆除 O1/O2/O3：申报的 prereg 偏离与机械降格见证如实进 manifest ——
    # 偏离/未验收不再拦执行，但记录必须把整个命题带上。
    declared_deviations = getattr(state, "hook_state", {}).get(PREREG_DEVIATIONS_KEY)
    if isinstance(declared_deviations, list) and declared_deviations:
        manifest["prereg_deviations"] = declared_deviations
    precondition_witnesses = getattr(
        state, "hook_state", {}).get(PRECONDITION_WITNESS_KEY)
    if isinstance(precondition_witnesses, list) and precondition_witnesses:
        manifest["execution_precondition_witnesses"] = precondition_witnesses
    preflight_incomplete = getattr(state, "hook_state", {}).get("experiment_preflight_incomplete")
    if isinstance(preflight_incomplete, list) and preflight_incomplete:
        manifest["experiment_preflight_incomplete"] = preflight_incomplete
    grants = _human_write_grants(state)
    if grants:
        manifest["human_write_grants"] = grants
    if experiment_log_path and Path(experiment_log_path).is_file():
        try:
            exp_path = Path(experiment_log_path)
            manifest["experiment_log"] = {
                "path": str(exp_path.relative_to(root)),
                "size_bytes": exp_path.stat().st_size,
                "sha256": sha256_file(exp_path),
            }
        except (OSError, ValueError):
            manifest["experiment_log"] = {"path": str(experiment_log_path)}
    if contract.get("contract_warning"):
        manifest["contract_warning"] = contract["contract_warning"]

    path = experiment_output_dir(state, "repro", create=True) / "run_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        # 每个 run 一份身份：此前所有 run 覆盖同一个 `run_manifest__run_manifest`，
        # 历史 run 的 manifest 只活在版本快照里，读者得翻历史。
        state.save_artifact(
            artifact_type="run_manifest",
            name=f"run_manifest_{manifest['run_id']}",
            content=json.dumps(manifest, ensure_ascii=False, indent=2),
            metadata={
                "run_id": manifest["run_id"],
                "run_role": manifest["run_role"],
                "run_role_source": manifest["run_role_source"],
                "execution_mode": manifest["execution_mode"],
                "operation_kind": manifest.get("operation_kind"),
                "caller_node_type": (manifest.get("invocation") or {}).get("caller_node_type"),
                "caller_run_id": (manifest.get("invocation") or {}).get("caller_run_id"),
                "analysis_eligible": manifest["analysis_eligible"],
                "manifest_path": str(path),
                "manifest_sha256": sha256_file(path),
            },
        )
    except Exception:
        # Manifest creation must never turn a completed experiment into a
        # failed one.  The run-local JSON remains available even if artifact
        # persistence is temporarily unavailable.
        pass
    return manifest


def _invocation_context(state: Any) -> dict[str, str | None]:
    """Expose caller provenance without allowing the Experiment agent to choose it."""
    hook_state = getattr(state, "hook_state", {}) or {}
    node_inputs = hook_state.get("node_inputs")
    sources = (node_inputs if isinstance(node_inputs, dict) else {}, hook_state)

    def _first(*names: str) -> str | None:
        for source in sources:
            for name in names:
                value = source.get(name) if isinstance(source, dict) else None
                if value is not None and str(value).strip():
                    return str(value).strip()
        return None

    caller_node_type = _first("caller_node_type", "source_node_type", "parent_node_type")
    caller_run_id = _first("caller_run_id", "source_run_id", "parent_run_id")
    return {
        "caller_node_type": caller_node_type,
        "caller_run_id": caller_run_id,
        # This mirrors immutable invocation provenance; it is not a routing command.
        "return_target_node_type": caller_node_type,
        "return_target_run_id": caller_run_id,
    }


# 前处理请求的自然语言线索。分类工具只用它来**提示路由**，不改 scope、不拦截、
# 也不结束本 run —— 判定"这活归谁"永远是同步调 data 让它自己答，不能退回
# orchestrator：反向 redirect 的踢皮球仲裁挂在 `core/loader.node_owes_post_node_flow` 下，而 data
# 是 post_run_flow=none，这条链上那个仲裁根本不触发，弹回去就是无账本的对踢。
_PREPROCESSING_REQUEST_HINTS: tuple[tuple[str, str], ...] = (
    ("mesh", r"网格|mesh|grid\s*generation|划分网格|geogrid|metgrid"),
    ("structure", r"POSCAR|超胞|supercell|初始结构|插原子|建模型|structure\s*file|CIF"),
    ("input_deck", r"KPOINTS|INCAR|POTCAR|输入文件|输入包|input\s*(?:deck|files?|package)|namelist"),
    ("conversion", r"格式转换|format\s*conversion|转成|convert\s+\w+\s+to"),
    ("dataset", r"数据集|训练集|dataset|切分|清洗|train[_\s]?test[_\s]?split"),
    ("explicit", r"前处理|preprocess"),
)


def detect_preprocessing_request(state: Any) -> list[str]:
    """从本轮请求文本里机械识别前处理线索，返回命中的类别名。"""
    hook_state = getattr(state, "hook_state", {}) or {}
    node_inputs = hook_state.get("node_inputs")
    text = json.dumps(node_inputs, ensure_ascii=False, default=str) if node_inputs else ""
    return [label for label, pattern in _PREPROCESSING_REQUEST_HINTS
            if re.search(pattern, text, re.I)]


def _preprocessing_routing_advice(state: Any, bound_prereg_id: str) -> dict[str, Any]:
    """命中前处理线索时，给出本 run 该走哪条 data 路由（同步调用，等它返回）。"""
    detected = detect_preprocessing_request(state)
    if not detected:
        return {"detected": False, "categories": []}
    request_kind = ("formal_input_preparation" if bound_prereg_id
                    else "preprocessing_service_request")
    return {
        "detected": True,
        "categories": detected,
        "route": "call_data_synchronously",
        "request_kind": request_kind,
        "next_step": (
            "本轮请求里出现了前处理产物（" + "、".join(detected) + "）。这类产物归 data 节点："
            f"先 validate_data_request_spec(request_kind={request_kind})，再 "
            "dispatch_data_request(spec_id=<返回的 spec_id>, user_note=...)；该工具复用既有 "
            "run_node(data) 路径并持久化当前请求与子 run receipt。若 Data 因权限 pause，"
            "恢复后先调 reconcile_data_dispatch() 回收同一 child 的完整终态 artifact receipt；同步等它返回后用 "
            "verify_dataset_consumption 验收。"
            "不要自己就地生成，也不要把这活退回 orchestrator —— 服务是同步调用的，"
            "退回去只会在两个节点之间来回弹。"
            "确属运行化轻调（按 prereg 复制已有输入、CONTCAR→POSCAR 续跑、MPI/walltime 调整）"
            "则不适用本提示，正常执行即可。"
        ),
    }


def _pending_scientific_block_payload(
    receipt: dict[str, Any],
    *,
    signal_source: str,
    signal_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visibility = receipt.get("unbound_prereg_visibility_witness") or {}
    details = dict(signal_details or {})
    # P0a v2 B3：升级前开始的 run 只有 v1 收据，v1 表达不了 typed none，所以它的
    # 无 prereg 科学 run 在这里会被当作 pending 拒掉——这是有意的（never invent a
    # waiver），但它不能在原地修复，文案必须一次说清（同 043 的形状）。
    legacy_run = receipt.get("schema_version") == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
    legacy_note = (
        "本 run 在升级前开始（v1 run-acceptance 收据没有 typed prereg_assignment），"
        "它无法在原地补声明——请由派发方新开一个 run 并给出 typed bound 或 typed none；"
        "旧 run 请如实以 blocked 收尾。"
        if legacy_run else ""
    )
    return {
        "status": "error",
        "error_code": "prereg_assignment_required",
        "assignment_status": "pending",
        "prereg_assignment": {"kind": "pending"},
        "candidate_bindings": _candidate_bindings_from_visibility(visibility),
        "retryable_in_current_run": False,
        "scientific_signal_source": signal_source,
        **({"scientific_signal_details": details} if details else {}),
        **({"legacy_run": True, "legacy_receipt_schema_version": 1} if legacy_run else {}),
        "next_action": {
            "owner": "dispatching_parent",
            "action": "redispatch_new_run",
            "required_patch": {
                "prereg_assignment": {
                    "kind": "bound_or_none",
                },
            },
        },
        "error": (
            "本 run 的 prereg_assignment 仍为 pending，不能在 child 内猜测或"
            "补写科学任务归属。请由派发方以 typed bound 或 typed none 重派新 run。"
            + legacy_note
        ),
    }


def prereg_assignment_scientific_block(
    state: Any,
    *,
    signal_source: str,
    acceptance: dict[str, Any] | None = None,
    signal_details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reject a *new* scientific signal while assignment is still pending.

    A preregistration is sufficient, not necessary, for scientific work.  The
    distinction here is upstream assignment authority: typed ``none`` is an
    explicit decision and remains valid for exploratory science, while omission
    is a pending dispatch decision that this child cannot repair in-place.
    Legacy v1 exact bindings keep their historical compatibility semantics;
    an unbound v1 receipt remains pending because v1 could not express typed none.
    """
    resolved = acceptance or resolve_run_acceptance(
        state, bind_if_absent=False,
    )
    if not resolved.get("passed", False):
        return {
            "status": "error",
            "error_code": "prereg_assignment_authority_unavailable",
            "assignment_status": "unknown",
            "retryable_in_current_run": False,
            "scientific_signal_source": signal_source,
            "run_acceptance": resolved,
            "error": (
                "无法核验本 run 的 prereg assignment authority；科学动作未获准。"
            ),
        }
    receipt = resolved.get("receipt") or {}
    assignment = _receipt_prereg_assignment(receipt)
    if (
        receipt.get("schema_version") == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        and assignment.get("kind") == "bound"
    ):
        return None
    if assignment.get("kind") != "pending":
        return None
    details = dict(signal_details or {})
    previous = resolved.get("pending_scientific_signal")
    if not isinstance(previous, dict):
        event = {
            "run_acceptance_receipt_digest": receipt.get("receipt_digest"),
            "assignment_status": "pending",
            "scientific_signal_source": signal_source,
            "scientific_signal_details": details,
            "authorizing": False,
            "next_action_owner": "dispatching_parent",
        }
        try:
            state.append_transcript(_PENDING_SCIENTIFIC_SIGNAL_EVENT, **event)
            previous = event
        except Exception as exc:
            previous = {
                "scientific_signal_source": signal_source,
                "scientific_signal_details": details,
            }
            try:
                state.hook_state["_pending_scientific_signal_unpersisted"] = dict(
                    previous
                )
            except Exception:
                pass
            blocked = _pending_scientific_block_payload(
                receipt,
                signal_source=signal_source,
                signal_details=details,
            )
            blocked["error_code"] = (
                "prereg_assignment_scientific_signal_persistence_failed"
            )
            blocked["persistence_error"] = f"{type(exc).__name__}: {exc}"
            return blocked
    return _pending_scientific_block_payload(
        receipt,
        signal_source=str(
            previous.get("scientific_signal_source") or signal_source
        ),
        signal_details=(
            previous.get("scientific_signal_details")
            if isinstance(previous.get("scientific_signal_details"), dict)
            else details
        ),
    )


def _cache_execution_scope_projection(
    hook_state: dict[str, Any],
    classification: dict[str, Any],
) -> None:
    """Project one classified scope into the legacy hook cache.

    The transcript receipt and classified-scope event remain authoritative;
    this paired cache write only serves existing consumers and may be rebuilt
    from that ledger on an idempotent classification call.
    """
    hook_state["experiment_execution_scope"] = classification
    # ``operational`` is the evidence-contract term used by Experiment;
    # ``operation`` is the existing request-mode vocabulary used by
    # NodeHarness/QC. Keep the translation beside the only scope-cache write.
    hook_state["_request_mode"] = (
        "operation" if classification.get("mode") == "operational"
        else "scientific"
    )


async def _classify_experiment_scope(
    state: Any, *, scope: str, reason: str, operation_category: str = "other", **_: Any,
) -> dict[str, Any]:
    """Record Experiment's own pre-execution classification of the requested work."""
    scope = str(scope or "").strip().lower()
    reason = str(reason or "").strip()
    operation_category = str(operation_category or "other").strip().lower()
    # 判决拆除（rc:646 删，2026-08-31）：reason 字数下限验不出诚意，意图放进
    # schema description；空 reason 仍如实记录为空。
    # 判决拆除·第三波（rc:702/706 → schema，2026-09-02）：scope / operation_category
    # 枚举由 classify_experiment_scope schema 在派发口核，这里不再手写。
    requested_mode = "operational" if scope == "operation" else "scientific"
    requested_category = (operation_category or None) if scope == "operation" else None

    hook_state = getattr(state, "hook_state", {})
    cached_previous = hook_state.get("experiment_execution_scope")
    acceptance = resolve_run_acceptance(
        state, bind_if_absent=not isinstance(cached_previous, dict),
        requested_mode=requested_mode,
    )
    if not acceptance.get("passed", False):
        return {
            "status": "error",
            "error": str(acceptance.get("status") or "run_acceptance_unavailable"),
            "run_acceptance": acceptance,
            "guidance": (
                "本 run 无法建立或恢复唯一的执行入口收据；不得根据当前项目目录、"
                "缓存或请求措辞补猜 authority，也不要尝试改写 transcript。请调用 "
                "report_blocker 记录当前状态、候选身份和所需上游动作，再由上游选择"
                "明确 prereg 并重派新 run；没有 prereg 的科学任务则应在无冲突的新 "
                "run 中建立自己的入口收据。"
            ),
            # Keep actionable identity details visible at the tool boundary
            # without hiding this refusal behind an assigned dictionary.
            **{
                key: acceptance[key]
                for key in (
                    "ambiguous_preregs",
                    "unavailable_forwarded_preregs",
                )
                if key in acceptance
            },
        }
    durable_previous = acceptance.get("durable_scope_projection")
    previous = (
        durable_previous
        if isinstance(durable_previous, dict)
        else cached_previous
    )

    receipt = acceptance["receipt"]
    prereg_assignment = _receipt_prereg_assignment(receipt)
    prior_scientific_signal = acceptance.get("pending_scientific_signal")
    if not isinstance(prior_scientific_signal, dict):
        prior_scientific_signal = hook_state.get(
            "_pending_scientific_signal_unpersisted"
        )
    if (
        requested_mode == "operational"
        and prereg_assignment.get("kind") == "pending"
        and isinstance(prior_scientific_signal, dict)
    ):
        return _pending_scientific_block_payload(
            receipt,
            signal_source=str(
                prior_scientific_signal.get("scientific_signal_source")
                or "prior_scientific_signal"
            ),
            signal_details=(
                prior_scientific_signal.get("scientific_signal_details")
                if isinstance(
                    prior_scientific_signal.get("scientific_signal_details"),
                    dict,
                )
                else {}
            ),
        )
    if requested_mode == "scientific":
        assignment_block = prereg_assignment_scientific_block(
            state,
            signal_source="scope_classification",
            acceptance=acceptance,
            signal_details={"requested_scope": scope},
        )
        if assignment_block is not None:
            return assignment_block
    prereg_binding = receipt.get("governing_task_input_binding")
    effective_mode = "scientific" if isinstance(prereg_binding, dict) else requested_mode
    effective_category = requested_category if effective_mode == "operational" else None
    requested = {
        "mode": effective_mode,
        "category": effective_category,
        "reason": reason,
        "invocation": _invocation_context(state),
        "intent_schema_version": int(receipt["schema_version"]),
        "intent_source": receipt.get("intent_source"),
        "intent_status": receipt.get("intent_status"),
        "intent_digest": receipt["intent_digest"],
        "run_acceptance_receipt_digest": receipt["receipt_digest"],
        "governing_task_input_binding": prereg_binding,
        "binding_source": receipt.get("binding_source"),
        "prereg_assignment": prereg_assignment,
    }
    if effective_mode != requested_mode:
        requested["requested_mode"] = requested_mode
        requested["requested_category"] = requested_category
    if isinstance(previous, dict):
        if previous.get("mode") == requested["mode"] and previous.get("category") == requested["category"]:
            binding = audit_execution_intent_binding(state, require=False)
            if not binding.get("passed", False):
                return {
                    "status": "error",
                    "error": "execution_intent_changed",
                    "intent_binding": binding,
                    "guidance": (
                        "本 run 的上游输入或冻结 prereg 绑定已经变化；不得在原 scope "
                        "上继续或重新分类。请由上游重派一个新 run，或先恢复原始输入。"
                    ),
                }
            idempotent_bound_id = (
                str(prereg_binding.get("artifact_id") or "").strip()
                if isinstance(prereg_binding, dict) else ""
            )
            idempotent_routing = _preprocessing_routing_advice(
                state, idempotent_bound_id
            )
            restored = dict(previous)
            restored_contract = load_run_contract(state)
            restored["run_role"] = restored_contract.get("run_role")
            restored["run_role_source"] = restored_contract.get(
                "run_role_source")
            restored_warnings = restored_contract.get("contract_warnings") or []
            restored.pop("contract_warnings", None)
            if idempotent_bound_id:
                if restored_warnings:
                    restored["contract_warnings"] = restored_warnings
            elif RUN_ROLE_UNDECLARED_WARNING in restored_warnings:
                restored["contract_warnings"] = [RUN_ROLE_UNDECLARED_WARNING]
            if idempotent_routing.get("detected"):
                restored["preprocessing_delegation"] = idempotent_routing
            else:
                restored.pop("preprocessing_delegation", None)
            # QC and legacy required-output resolution use this existing mode
            # key. Restore it on resume/idempotent calls rather than leaving
            # a caller-supplied or stale value authoritative.
            _cache_execution_scope_projection(hook_state, restored)
            # 路由建议跟 _request_mode 同理：resume/幂等重调时也要照常给回，
            # 否则同一个工具在第一次和第二次调用返回的形状不一样，agent 会以为
            # 这轮不需要派 data。
            return {
                "status": "success",
                "classification": restored,
                "idempotent": True,
                "preprocessing_delegation": idempotent_routing,
            }
        # 判决拆除 O4（rc:670 降格，2026-08-31）：改道不再被禁止 —— 「不得改道」
        # 与 operation 收据的 mode 闸合成过死路（prereg 写 scientific、实际做了
        # 运维即无处记账）。改道照做，scope_redeclared 如实进账。
        redeclared = {
            "from_mode": previous.get("mode"),
            "from_category": previous.get("category"),
            "to_mode": requested["mode"],
            "to_category": requested["category"],
            "reason": reason,
        }
        requested["scope_redeclared"] = redeclared
        try:
            state.append_transcript("experiment_scope_redeclared", **redeclared)
        except Exception:
            pass

    # The immutable entrance receipt is the authority for whether this run
    # accepted a governing preregistration.  The live contract is still read
    # below to verify that exact frozen artifact and expose its current
    # execution fields; it may not select a different project artifact.
    contract = load_run_contract(state)
    requested["run_role"] = contract.get("run_role")
    requested["run_role_source"] = contract.get("run_role_source")
    bound_prereg_id = (
        str(prereg_binding.get("artifact_id") or "").strip()
        if isinstance(prereg_binding, dict) else ""
    )
    if bound_prereg_id:
        if requested_mode != "scientific":
            record_prereg_deviation(state, "classify_experiment_scope", {
                "kind": "requested_operation_with_governing_prereg",
                "prereg_artifact_id": bound_prereg_id,
                "requested_execution_mode": requested_mode,
                "effective_execution_mode": "scientific",
                "reason": reason,
            })
            requested["prereg_deviation"] = {
                "kind": "requested_operation_with_governing_prereg",
                "requested_execution_mode": requested_mode,
                "effective_execution_mode": "scientific",
            }
        requested["source"] = "governing_task_input_binding"
        requested["analysis_eligible"] = contract.get("analysis_eligible")
        if contract.get("contract_warnings"):
            requested["contract_warnings"] = contract["contract_warnings"]
    elif RUN_ROLE_UNDECLARED_WARNING in (
        contract.get("contract_warnings") or []
    ):
        requested["contract_warnings"] = [RUN_ROLE_UNDECLARED_WARNING]

    # 前处理路由是与 scope 正交的一维，刻意**不**做成第三个 scope 取值：
    # experiment_execution_scope["mode"] 被 _request_mode、required_outputs_by_mode
    # 和 end audit 多处消费，而 end audit 只认 operation / scientific 两档，多一个
    # 取值会让 run 结束时认不出 mode。
    routing = _preprocessing_routing_advice(state, bound_prereg_id)
    if routing.get("detected"):
        requested["preprocessing_delegation"] = routing

    state.append_transcript("experiment_scope_classified", **requested)
    _cache_execution_scope_projection(hook_state, requested)

    if routing.get("detected"):
        state.append_transcript("preprocessing_routing_advised", **routing)
    return {"status": "success", "classification": requested,
            "preprocessing_delegation": routing}


register_tool(ToolDefinition(
    name="classify_experiment_scope",
    description=("After reading the request and before side effects, have Experiment classify this run itself. "
                 "Classify by the purpose of the evidence, not by workload shape: operation means success or failure "
                 "can be decided entirely by predeclared mechanical acceptance criteria without interpreting a scientific "
                 "claim; scientific means this run's results will be used to evaluate a scientific claim, including "
                 "hypothesis tests, confirmatory studies, and replication.  Simulation, training, parameter scans, repeated "
                 "timing, or otherwise analysable results do not by themselves determine the scope.  When the caller does not "
                 "provide a prereg assignment, the assignment remains pending; visible project preregistrations are candidate "
                 "observations and are never automatically bound.  A typed explicit-none assignment states that this run has no "
                 "governing preregistration without choosing its scope.  A bound frozen preregistration makes the run scientific regardless of "
                 "requested operation mode or run_role; only a preregistration that remains unbound does not.  This records scope "
                 "and immutable caller provenance; "
                 "it never selects a downstream node. "
                 "The result also carries preprocessing_delegation: when the request asks for a mesh, a structure, an "
                 "input deck, a format conversion or a dataset split, it names the data-service route and the "
                 "request_kind this run must use.  Call data synchronously and consume its dataset; never hand the work "
                 "back to the orchestrator."),
    parameters_schema={
        "type": "object",
        "properties": {
            # 词表与代码同源（判决拆除·第三波「一题一答」）：enum 从常量派生，派发口按它核值。
            "scope": {"type": "string", "enum": sorted(_REQUEST_SCOPES)},
            "reason": {"type": "string"},
            "operation_category": {"type": "string", "enum": sorted(_OPERATION_CATEGORIES)},
        },
        "required": ["scope", "reason"],
    },
    allowed_node_types=["experiment"], risk_level="low",
), _classify_experiment_scope)


def execution_intent_snapshot(state: Any) -> dict[str, Any]:
    """Return the immutable caller-input projection for this Experiment run.

    An empty or absent ``node_inputs`` is not a meaningful research/operation
    objective.  It is therefore deliberately not hashed as an empty object: a
    new scope records that fact and real effects fail closed rather than turning
    a missing upstream request into a forged immutable target.
    """
    hook_state = getattr(state, "hook_state", {}) or {}
    if not isinstance(hook_state, dict):
        return {"available": False, "reason": "hook_state_unavailable"}
    node_inputs = hook_state.get("node_inputs")
    if not isinstance(node_inputs, dict):
        return {
            "available": False,
            "source": "node_inputs" if "node_inputs" in hook_state else None,
            "reason": "node_inputs_invalid" if "node_inputs" in hook_state else "node_inputs_unavailable",
        }
    if not node_inputs:
        return {
            "available": False,
            "source": "node_inputs",
            "input_keys": [],
            "reason": "node_inputs_empty",
        }
    forwarded_input_ids = hook_state.get("forwarded_input_ids") or []
    if (
        not isinstance(forwarded_input_ids, list)
        or any(not isinstance(item, str) or not item for item in forwarded_input_ids)
    ):
        return {
            "available": False,
            "source": "node_inputs+forwarded_input_ids",
            "reason": "forwarded_input_ids_invalid",
        }
    intent_material = {
        "node_inputs": node_inputs,
        "forwarded_input_ids": sorted(set(forwarded_input_ids)),
    }
    try:
        canonical = json.dumps(
            intent_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = canonical.encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return {
            "available": False,
            "source": "node_inputs+forwarded_input_ids",
            "reason": "upstream_inputs_not_canonical_json",
        }
    return {
        "available": True,
        "source": "node_inputs+forwarded_input_ids",
        "intent_digest": hashlib.sha256(encoded).hexdigest(),
        "input_keys": sorted(str(key) for key in node_inputs),
    }


def _run_acceptance_candidate(
    state: Any,
    *,
    requested_mode: str | None,
) -> dict[str, Any]:
    """Derive one write-once candidate from current structured inputs."""
    contract = load_run_contract(state)
    if contract.get("prereg_scan_error"):
        return {
            "passed": False,
            "status": "run_authority_prereg_scan_failed",
            "receipt": None,
            "reason": str(contract["prereg_scan_error"]),
        }
    if contract.get("prereg_version_invalid"):
        return {
            "passed": False,
            "status": "run_authority_prereg_version_invalid",
            "receipt": None,
            "prereg_version_invalid": contract["prereg_version_invalid"],
        }
    if contract.get("prereg_assignment_invalid"):
        return {
            "passed": False,
            "status": "run_authority_prereg_assignment_invalid",
            "receipt": None,
            "reason": str(contract["prereg_assignment_invalid"]),
        }
    assignment, assignment_error = _validated_prereg_assignment(
        contract.get("prereg_assignment"), require_bound_source=True,
    )
    if assignment is None:
        return {
            "passed": False,
            "status": "run_authority_prereg_assignment_invalid",
            "receipt": None,
            "reason": str(assignment_error),
        }
    hook_state = getattr(state, "hook_state", {}) or {}
    forwarded_ids = {
        str(item)
        for item in (hook_state.get("forwarded_input_ids") or [])
        if isinstance(item, str) and item
    }
    unresolved_prereg_ids = {
        str(item)
        for item in (contract.get("ambiguous_preregs") or [])
        if isinstance(item, str) and item
    }
    unresolved_prereg_ids.update(
        str(item.get("artifact_id") or "")
        for item in (contract.get("pending_amendments") or [])
        if isinstance(item, dict) and item.get("artifact_id")
    )
    caller_forwarded_prereg_ambiguity = bool(
        forwarded_ids.intersection(unresolved_prereg_ids)
    )
    allow_unbound_operation = (
        requested_mode == "operational"
        and assignment["kind"] == "pending"
        and not contract.get("declared_prereg_id")
        and not caller_forwarded_prereg_ambiguity
    )
    explicit_none = assignment["kind"] == "none"
    if (
        contract.get("ambiguous_preregs")
        and not allow_unbound_operation
        and not explicit_none
    ):
        return {
            "passed": False,
            "status": "run_authority_prereg_ambiguous",
            "receipt": None,
            "ambiguous_preregs": list(contract["ambiguous_preregs"]),
        }
    if contract.get("unavailable_forwarded_preregs") and not explicit_none:
        return {
            "passed": False,
            "status": "run_authority_forwarded_prereg_unavailable",
            "receipt": None,
            "unavailable_forwarded_preregs": list(
                contract["unavailable_forwarded_preregs"]
            ),
        }
    if (
        contract.get("pending_amendments")
        and assignment["kind"] != "bound"
        and not allow_unbound_operation
        and not explicit_none
        and caller_forwarded_prereg_ambiguity
    ):
        return {
            "passed": False,
            "status": "run_authority_prereg_amendment_pending",
            "receipt": None,
            "pending_amendments": list(contract["pending_amendments"]),
        }
    if contract.get("declared_prereg_id") and not contract.get("prereg_artifact_id"):
        return {
            "passed": False,
            "status": "run_authority_declared_prereg_unavailable",
            "receipt": None,
            "declared_prereg_id": contract["declared_prereg_id"],
        }

    visibility_witness: dict[str, list[dict[str, Any]]] | None = None
    if assignment["kind"] == "pending":
        raw_visibility = contract.get("prereg_visibility_witness")
        visibility_witness, visibility_error = (
            _validated_prereg_visibility_witness(raw_visibility)
        )
        if visibility_witness is None:
            return {
                "passed": False,
                "status": "run_authority_prereg_visibility_invalid",
                "receipt": None,
                "reason": visibility_error,
            }

    intent = execution_intent_snapshot(state)
    payload = {
        "schema_version": _RUN_ACCEPTANCE_SCHEMA_VERSION,
        "run_id": str(getattr(state, "run_id", "") or ""),
        "intent_digest": intent.get("intent_digest") if intent.get("available") else None,
        "intent_source": intent.get("source"),
        "intent_input_keys": sorted(set(intent.get("input_keys") or [])),
        "intent_status": (
            "bound_at_acceptance"
            if intent.get("available")
            else "unavailable_at_acceptance"
        ),
        "prereg_assignment": assignment,
        # This exact snapshot never selects a prereg.  It only lets later
        # audits distinguish pre-existing ambiguity/pending work from a catalog
        # change after a positive null binding was accepted.
        "unbound_prereg_visibility_witness": visibility_witness,
    }
    payload["receipt_digest"] = _run_acceptance_receipt_digest(payload)
    receipt, error = _validated_run_acceptance_receipt(
        state, {"event": _RUN_ACCEPTANCE_EVENT, **payload}
    )
    if receipt is None:
        return {
            "passed": False,
            "status": "run_authority_candidate_invalid",
            "receipt": None,
            "reason": error,
        }
    return {"passed": True, "status": "candidate", "receipt": receipt}


def resolve_run_acceptance(
    state: Any,
    *,
    bind_if_absent: bool,
    requested_mode: str | None = None,
) -> dict[str, Any]:
    """Read or atomically create the sole same-run governing input receipt.

    The transcript is a transition carrier until Core provides an immutable
    task-scoped contract revision.  At that point this event becomes a
    removable mirror; it must never remain a second authority.
    """
    reduced = _reduce_run_acceptance_receipts(state)
    if reduced.get("status") != "run_authority_receipt_missing":
        if bind_if_absent:
            reduced = _ensure_pending_assignment_witness(state, reduced)
        return _acceptance_for_requested_mode(
            state,
            reduced,
            requested_mode=requested_mode,
        )
    if not bind_if_absent:
        return reduced

    root = Path(getattr(state, "root", "."))
    lock_path = root / ".run_acceptance_receipt.lock"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with exclusive(lock_path):
            reduced = _reduce_run_acceptance_receipts(state)
            if reduced.get("status") != "run_authority_receipt_missing":
                reduced = _ensure_pending_assignment_witness(
                    state, reduced, lock_held=True,
                )
                return _acceptance_for_requested_mode(
                    state,
                    reduced,
                    requested_mode=requested_mode,
                )
            if _run_acceptance_missing_for_existing_scope(state, reduced):
                return {
                    **reduced,
                    "status": "run_authority_receipt_missing_for_existing_scope",
                    "reason": _LEGACY_CLASSIFIED_RUN_AUTHORITY_REASON,
                }
            candidate = _run_acceptance_candidate(
                state,
                requested_mode=requested_mode,
            )
            if not candidate.get("passed"):
                return candidate
            receipt = dict(candidate["receipt"])
            try:
                state.append_transcript(
                    _RUN_ACCEPTANCE_EVENT,
                    **_receipt_event_payload(receipt),
                )
            except Exception as exc:
                return {
                    "passed": False,
                    "status": "run_authority_receipt_write_failed",
                    "receipt": None,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            reread = _reduce_run_acceptance_receipts(state)
            if reread.get("passed"):
                reread["created"] = True
                reread = _ensure_pending_assignment_witness(
                    state, reread, lock_held=True,
                )
                if reread.get("passed"):
                    reread["created"] = True
            return _acceptance_for_requested_mode(
                state,
                reread,
                requested_mode=requested_mode,
            )
    except OSError as exc:
        return {
            "passed": False,
            "status": "run_authority_receipt_lock_failed",
            "receipt": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def audit_execution_intent_binding(
    state: Any,
    *,
    require: bool,
) -> dict[str, Any]:
    """Compatibility audit projected from the canonical run acceptance receipt."""
    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    if (
        not acceptance.get("passed")
        and acceptance.get("status") != "run_authority_receipt_missing"
    ):
        return {
            "passed": False,
            "applicable": True,
            "status": str(
                acceptance.get("status") or "run_authority_receipt_invalid"
            ),
            "reason": str(
                acceptance.get("reason")
                or "execution requires a valid write-once run acceptance receipt"
            ),
            "run_acceptance": acceptance,
        }
    durable_scope = acceptance.get("durable_scope_projection")
    if acceptance.get("passed"):
        scope = durable_scope if isinstance(durable_scope, dict) else None
    else:
        scope = getattr(state, "hook_state", {}).get(
            "experiment_execution_scope"
        )
    if not isinstance(scope, dict):
        return {
            "passed": not require,
            "applicable": bool(require),
            "status": "scope_unbound",
            "reason": "real execution requires classify_experiment_scope before upstream intent can be bound",
        }
    mode = str(scope.get("mode") or "").strip()
    if mode not in {"operational", "scientific"}:
        return {
            "passed": not require,
            "applicable": bool(require),
            "status": "scope_invalid",
            "reason": "execution scope is absent or invalid",
        }

    if not acceptance.get("passed"):
        if (
            not require
            and acceptance.get("status") == "run_authority_receipt_missing"
            and not scope.get("run_acceptance_receipt_digest")
        ):
            return {
                "passed": True,
                "applicable": False,
                "status": "legacy_unverifiable",
                "reason": (
                    "a legacy scope without an entrance receipt remains readable for "
                    "planning, but cannot authorize real effects or terminal closure"
                ),
            }
        if _run_acceptance_missing_for_existing_scope(state, acceptance):
            return {
                "passed": False,
                "applicable": True,
                "status": (
                    "run_authority_receipt_missing_for_existing_scope"
                ),
                "reason": _LEGACY_CLASSIFIED_RUN_AUTHORITY_REASON,
                "run_acceptance": acceptance,
            }
        return {
            "passed": False,
            "applicable": True,
            "status": str(acceptance.get("status") or "run_authority_receipt_invalid"),
            "reason": str(
                acceptance.get("reason")
                or "execution requires a valid write-once run acceptance receipt"
            ),
            "run_acceptance": acceptance,
        }
    receipt = acceptance["receipt"]
    prereg_assignment = _receipt_prereg_assignment(receipt)
    expected_digest = str(receipt.get("intent_digest") or "").strip()
    raw_binding = receipt.get("governing_task_input_binding")
    prereg_binding = (
        dict(raw_binding) if isinstance(raw_binding, dict)
        else _empty_intent_prereg_binding()
    )
    prereg_state = _prereg_binding_state(prereg_binding)
    base = {
        "applicable": True,
        "scope_mode": mode,
        "intent_schema_version": receipt.get("schema_version"),
        "intent_status": receipt.get("intent_status"),
        "intent_digest": expected_digest or None,
        "prereg_binding": prereg_binding,
        "binding_source": receipt.get("binding_source"),
        "prereg_assignment": prereg_assignment,
        "unbound_prereg_visibility_witness": receipt.get(
            "unbound_prereg_visibility_witness"
        ),
        "run_acceptance_receipt_digest": receipt.get("receipt_digest"),
    }
    if scope.get("run_acceptance_receipt_digest") != receipt.get("receipt_digest"):
        return {
            **base,
            "passed": False,
            "status": "scope_acceptance_projection_changed",
            "reason": "the scope projection does not reference the canonical run acceptance receipt",
        }

    if _INTENT_DIGEST_RE.fullmatch(expected_digest) is None:
        return {
            **base,
            "passed": False,
            "status": "intent_unavailable_at_classification",
            "reason": "the run was accepted without a non-empty canonical upstream input; real effects require a fresh run with node_inputs",
        }

    current = execution_intent_snapshot(state)
    if not current.get("available"):
        return {
            **base,
            "passed": False,
            "status": "intent_unavailable",
            "reason": "the run receipt is bound to inputs but the current upstream input is unavailable",
        }
    if current.get("intent_digest") != expected_digest:
        return {
            **base,
            "passed": False,
            "status": "intent_changed",
            "current_intent_digest": current.get("intent_digest"),
            "reason": "upstream inputs changed after run acceptance; start a fresh run",
        }

    if (
        mode == "scientific"
        and prereg_state == "absent"
        and prereg_assignment.get("kind") != "none"
    ):
        conflict = _scientific_null_receipt_conflict(state, receipt)
        if conflict is not None:
            if conflict.get("kind") == "scan_failed":
                status = "run_authority_prereg_scan_failed"
                reason = str(conflict.get("reason") or "prereg catalog scan failed")
            else:
                status = "prereg_binding_missing_at_classification"
                reason = (
                    "a preregistration was visible before or after this null "
                    "scientific scope was classified; start a fresh Experiment "
                    "run with an exact positive binding"
                )
            return {
                **base,
                "passed": False,
                "status": status,
                "scientific_prereg_conflict": conflict,
                "reason": reason,
            }

    if prereg_state != "absent":
        contract = load_run_contract(state)
        current_binding = {
            "artifact_id": str(contract.get("prereg_artifact_id") or "").strip() or None,
            "version": contract.get("prereg_version"),
            "content_hash": str(contract.get("prereg_content_hash") or "").strip() or None,
        }
        if current_binding != prereg_binding:
            return {
                **base,
                "passed": False,
                "status": "prereg_binding_changed",
                "current_prereg_binding": current_binding,
                "reason": "the exact frozen prereg binding changed after run acceptance",
            }
        bound_prereg = load_bound_frozen_prereg(state)
        if not bound_prereg:
            return {
                **base,
                "passed": False,
                "status": "bound_prereg_unavailable",
                "reason": "the receipt-bound frozen prereg snapshot can no longer be read exactly",
            }
        if mode != "scientific":
            return {
                **base,
                "passed": False,
                "status": "scope_acceptance_projection_changed",
                "reason": "a bound prereg receipt requires an effective scientific scope",
            }

    return {
        **base,
        "passed": True,
        "status": "bound",
        "reason": "current inputs and exact prereg binding match the immutable run acceptance receipt",
    }


def audit_prereg_assignment(state: Any) -> dict[str, Any]:
    """Audit whether upstream made an explicit prereg assignment decision.

    This gate does not classify the run. ``none`` is a valid explicit decision
    for either scientific or operational work; ``bound`` is an exact decision;
    ``pending`` means the dispatching parent still owes a choice, including a
    legacy v1 null receipt that could not distinguish omission from explicit none.
    """
    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    if not acceptance.get("passed"):
        return {
            "passed": False,
            "applicable": True,
            "status": str(
                acceptance.get("status") or "run_authority_receipt_missing"
            ),
            "assignment": {"kind": "pending"},
            "reason": str(
                acceptance.get("reason")
                or "the run has no valid immutable prereg assignment receipt"
            ),
        }
    receipt = acceptance["receipt"]
    assignment = _receipt_prereg_assignment(receipt)
    schema_version = receipt.get("schema_version")
    base = {
        "applicable": True,
        "status": str(assignment.get("kind") or "invalid"),
        "assignment": assignment,
        "run_acceptance_receipt_digest": receipt.get("receipt_digest"),
    }
    if (
        schema_version == _LEGACY_RUN_ACCEPTANCE_SCHEMA_VERSION
        and assignment.get("kind") == "bound"
    ):
        return {
            **base,
            "passed": True,
            "status": "legacy_bound",
            "reason": (
                "legacy v1 exact binding remains readable without rewriting its "
                "historical authority"
            ),
        }
    if assignment.get("kind") in {"bound", "none"}:
        return {
            **base,
            "passed": True,
            "reason": (
                "upstream supplied an exact prereg binding"
                if assignment["kind"] == "bound"
                else "upstream explicitly assigned this run no governing prereg"
            ),
        }
    try:
        try:
            from .execution_action_census import pending_operation_compatibility
        except ImportError:
            from tools.execution_action_census import pending_operation_compatibility
        compatibility = pending_operation_compatibility(state)
    except Exception as exc:
        compatibility = {
            "passed": False,
            "applicable": False,
            "status": "audit_error",
            "failure_reasons": [
                f"action_census_audit_error:{type(exc).__name__}",
            ],
        }
    if compatibility.get("applicable") and compatibility.get("passed"):
        return {
            **base,
            "passed": True,
            "status": "pending_operation_compatible",
            "compatibility": compatibility,
            "reason": (
                "upstream assignment remains pending, but this explicitly "
                "operational run is covered by the temporary complete action-census lane"
            ),
        }
    visibility = receipt.get("unbound_prereg_visibility_witness") or {}
    candidate_bindings = _candidate_bindings_from_visibility(visibility)
    return {
        **base,
        "passed": False,
        "status": "pending",
        "candidate_bindings": candidate_bindings,
        "compatibility": compatibility,
        "retryable_in_current_run": False,
        "next_action": {
            "owner": "dispatching_parent",
            "action": "redispatch_new_run",
            "required_patch": {
                "prereg_assignment": {
                    "kind": "bound_or_none",
                },
            },
        },
        "reason": (
            "the caller omitted prereg_assignment; catalog candidates are "
            "non-authorizing observations, so the dispatching parent must "
            "redispatch with typed bound or typed none"
        ),
    }


def execution_intent_binding_receipt(
    state: Any,
    *,
    require: bool,
) -> dict[str, Any]:
    """Return a compact receipt for a frozen route/envelope, when bound.

    A plan may be declared before scope classification, so unbound/legacy
    planning stays readable with ``receipt=None``. It becomes non-executable
    once a v1 scope exists until it is re-frozen with this exact receipt.
    """
    audit = audit_execution_intent_binding(state, require=require)
    if audit.get("passed") and audit.get("status") == "bound":
        prereg = audit.get("prereg_binding") or _empty_intent_prereg_binding()
        receipt = {
            "schema_version": 1,
            "intent_digest": audit.get("intent_digest"),
            "prereg_binding": dict(prereg) if prereg.get("artifact_id") else None,
        }
        return {"passed": True, "status": "bound", "receipt": receipt, "audit": audit}
    return {
        "passed": bool(audit.get("passed")),
        "status": str(audit.get("status") or "binding_required"),
        "receipt": None,
        "audit": audit,
    }


def compare_execution_intent_binding_receipt(
    state: Any,
    receipt: Any,
    *,
    require: bool,
) -> dict[str, Any]:
    """Compare a route/envelope frozen receipt with the current v1 scope."""
    current = execution_intent_binding_receipt(state, require=require)
    if current.get("status") != "bound":
        return {
            "passed": bool(current.get("passed")),
            "status": current.get("status"),
            "receipt": current.get("receipt"),
            "audit": current.get("audit"),
        }
    expected = current["receipt"]
    if not isinstance(receipt, dict):
        return {
            "passed": False,
            "status": "receipt_missing",
            "expected_receipt": expected,
            "audit": current.get("audit"),
            "reason": "a v1-bound run may not execute a route/envelope frozen before its immutable intent receipt",
        }
    if receipt != expected:
        return {
            "passed": False,
            "status": "receipt_changed",
            "stored_receipt": receipt,
            "expected_receipt": expected,
            "audit": current.get("audit"),
            "reason": "frozen route/envelope intent receipt differs from the current immutable Experiment scope",
        }
    return {
        "passed": True,
        "status": "bound",
        "receipt": expected,
        "audit": current.get("audit"),
    }
