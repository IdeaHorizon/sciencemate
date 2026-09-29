"""Versioned caller contracts for data-node preprocessing work.

The data node accepts either a frozen research authority or a concrete asset
request.  Both are normalized here before an Analyst or Designer is allowed to
interpret them.  This keeps request identity, authority, and scope independent
from the executable planning representation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


PREPROCESSING_REQUEST_SCHEMA_VERSION = "1.0"
PREPROCESSING_WORK_ORDER_SCHEMA_VERSION = "1.0"
REVIEW_PROFILES = {"plan_bound", "request_bound"}
AUTHORITY_KINDS = {
    "research_plan",
    "pre_registration",
    "user_request",
    "experiment_request",
}

_SERVICE_CONTEXT_BOUNDARY_RE = re.compile(
    r"^\s*##\s*(?:(?:可用的?)?上游\s*artifact|(?:项目\s*)?(?:KB(?:\s*状态)?|提醒|提示))\b",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _text(value: Any) -> str:
    return str(value or "").strip()


def caller_request_text(
    value: Any,
    *,
    preprocessing_request: dict[str, Any] | None = None,
) -> str:
    """Return caller-authored request text without service-added context."""
    request = preprocessing_request if isinstance(preprocessing_request, dict) else {}
    snapshot = request.get("authority_snapshot")
    if snapshot not in (None, "", {}, []):
        authoritative_text = caller_request_text(snapshot)
        if authoritative_text:
            return authoritative_text
    candidates: list[Any] = []
    fallbacks: list[Any] = []
    if isinstance(value, dict):
        candidates.extend(
            value.get(key)
            for key in ("user_request", "spec", "task", "objective", "request", "content")
            if value.get(key) not in (None, "", [], {})
        )
        node_inputs = value.get("node_inputs")
        if isinstance(node_inputs, dict):
            candidates.extend(
                node_inputs.get(key)
                for key in ("user_request", "spec", "task", "objective")
                if node_inputs.get(key) not in (None, "", [], {})
            )
        for item in value.get("inputs") or []:
            if isinstance(item, dict):
                candidates.extend(
                    item.get(key)
                    for key in ("user_request", "spec", "task", "objective", "request", "content")
                    if item.get(key) not in (None, "", [], {})
                )
        if value.get("purpose") not in (None, "", [], {}):
            fallbacks.append(value["purpose"])
    elif value not in (None, ""):
        candidates.append(value)

    for candidate in [*candidates, *fallbacks]:
        text = _text(candidate)
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                nested = caller_request_text(parsed)
                if nested:
                    text = nested
        text = re.sub(r"^\s*##\s*节点输入\s*", "", text, flags=re.I)
        text = re.sub(r"^\s*-\s*\*\*spec\*\*\s*[:：]\s*", "", text, flags=re.I)
        boundary = _SERVICE_CONTEXT_BOUNDARY_RE.search(text)
        if boundary:
            text = text[:boundary.start()]
        if text.strip():
            return text.strip()
    return ""


def _slug(value: Any, fallback: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", _text(value)).strip("._-")
    return result or fallback


def _first_mapping(context: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        value = context.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _requested_assets(context: dict[str, Any], explicit: dict[str, Any]) -> list[dict[str, Any]]:
    raw = explicit.get("requested_assets")
    if not isinstance(raw, list):
        raw = context.get("requested_assets")
    if not isinstance(raw, list):
        raw = context.get("required_assets")
    if not isinstance(raw, list):
        raw = context.get("required_files")
    assets: list[dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, str):
            item = {"filename": item}
        if not isinstance(item, dict):
            continue
        filename = _text(
            item.get("filename")
            or item.get("expected_filename")
            or item.get("declared_output_path")
            or item.get("name")
            or item.get("name_or_role")
        )
        asset_id = _slug(item.get("asset_id") or item.get("id") or filename, f"asset_{index + 1}")
        assets.append({
            "asset_id": asset_id,
            "filename": filename,
            "format": _text(item.get("format")),
            "purpose": _text(item.get("purpose") or item.get("reason")),
            "content_constraints": deepcopy(_list(
                item.get("content_constraints")
                or item.get("acceptance_criteria")
            )),
            "dependencies": deepcopy(_list(item.get("dependencies"))),
            **{
                key: deepcopy(item[key])
                for key in (
                    "scientific_role", "representation", "source_strategy",
                    "generation_recipe", "parameter_bindings", "workflow_capability",
                )
                if item.get(key) not in (None, "", [], {})
            },
        })
    return assets


def normalize_preprocessing_request(
    task_context: str | dict[str, Any],
    *,
    source_artifact_ids: list[str] | None = None,
    source_stable_id: str = "",
    authority_kind_hint: str = "",
) -> dict[str, Any]:
    """Return the authoritative, immutable request envelope for one run."""
    context = deepcopy(task_context) if isinstance(task_context, dict) else {
        "user_request": _text(task_context)
    }
    node_inputs = context.get("node_inputs")
    if isinstance(node_inputs, dict):
        # Orchestrated service calls arrive under hook_state.node_inputs.  Make
        # that structured payload the native request surface instead of
        # requiring Experiment to render a Markdown pseudo-plan.  A persisted
        # free-text input from an earlier call cannot replace the latest caller
        # message; an explicit structured preprocessing_request still owns its
        # purpose and authority below.
        current_user_request = context.get("user_request")
        context = {**context, **deepcopy(node_inputs), "node_inputs": deepcopy(node_inputs)}
        if current_user_request not in (None, ""):
            context["user_request"] = current_user_request
    explicit = (
        deepcopy(context.get("preprocessing_request"))
        if isinstance(context.get("preprocessing_request"), dict)
        else {}
    )
    authority = deepcopy(explicit.get("authority")) if isinstance(explicit.get("authority"), dict) else {}
    authority_kind = _text(authority.get("kind"))
    if authority_kind not in AUTHORITY_KINDS:
        request_kind = _text(explicit.get("request_kind") or context.get("request_kind")).casefold()
        if request_kind in {"preprocessing_service_request", "asset_preprocessing"}:
            authority_kind = "experiment_request"
        elif request_kind == "formal_input_preparation" and context.get("source_prereg_artifact_id"):
            authority_kind = "pre_registration"
        elif authority_kind_hint in AUTHORITY_KINDS:
            authority_kind = authority_kind_hint
        elif any(context.get(key) not in (None, "", [], {}) for key in ("pre_registration", "preregistration")):
            authority_kind = "pre_registration"
        elif any(context.get(key) not in (None, "", [], {}) for key in ("research_plan", "plan")):
            authority_kind = "research_plan"
        elif _text(context.get("caller_node") or context.get("source_node")).casefold() == "experiment":
            authority_kind = "experiment_request"
        else:
            authority_kind = "user_request"
    review_profile = (
        "plan_bound"
        if authority_kind in {"research_plan", "pre_registration"}
        else "request_bound"
    )
    caller_text = caller_request_text(context) or caller_request_text(task_context)
    explicit_snapshot = explicit.get("authority_snapshot")
    raw_authority = deepcopy(explicit_snapshot) if isinstance(explicit_snapshot, dict) else {}
    if not raw_authority:
        raw_authority = (
            _first_mapping(context, ("pre_registration", "preregistration"))
            if authority_kind == "pre_registration"
            else _first_mapping(context, ("research_plan", "plan"))
        )
    if not raw_authority:
        authority_keys = (
            ("pre_registration", "preregistration")
            if authority_kind == "pre_registration"
            else ("research_plan", "plan")
        )
        raw_value = next((context.get(key) for key in authority_keys if context.get(key) not in (None, "", [], {})), None)
        if raw_value is not None:
            raw_authority = raw_value if isinstance(raw_value, dict) else {"content": raw_value}
    if not raw_authority:
        raw_authority = {
            "request": context.get("user_request") or context.get("objective") or task_context
        }
    if review_profile == "request_bound" and caller_text:
        # Service-added artifact/KB context is provenance, not part of the
        # caller's locked request and must not influence review or identity.
        raw_authority = {"request": caller_text}
    assets = _requested_assets(context, explicit)
    raw_acceptance = (
        explicit.get("acceptance_criteria")
        or context.get("acceptance_criteria")
        or context.get("acceptance")
    )
    acceptance = deepcopy(
        [key for key, enabled in raw_acceptance.items() if enabled]
        if isinstance(raw_acceptance, dict)
        else _list(raw_acceptance)
    )
    authority_payload = {
        "kind": authority_kind,
        "locked": True,
        "source_hash": source_stable_id or _canonical_hash(raw_authority),
    }
    if authority.get("artifact_id"):
        authority_payload["artifact_id"] = _text(authority["artifact_id"])
    elif context.get("source_prereg_artifact_id"):
        authority_payload["artifact_id"] = _text(context["source_prereg_artifact_id"])
    base = {
        "schema_version": _text(explicit.get("schema_version")) or PREPROCESSING_REQUEST_SCHEMA_VERSION,
        "request_kind": _text(explicit.get("request_kind") or context.get("request_kind")) or "asset_preprocessing",
        "review_profile": review_profile,
        "authority": authority_payload,
        # Preserve the exact initial authority for the final semantic review.
        # It is evidence, not an invitation for planning to expand its scope.
        "authority_snapshot": deepcopy(raw_authority),
        "purpose": _text(
            (caller_text if review_profile == "request_bound" else "")
            or explicit.get("purpose")
            or context.get("purpose")
            or context.get("objective")
            or context.get("user_request")
            or raw_authority.get("purpose")
            or raw_authority.get("objective")
            or raw_authority.get("title")
            or raw_authority.get("content")
            or (task_context if isinstance(task_context, str) else "")
        ),
        "consumer": deepcopy(
            explicit.get("consumer")
            or context.get("consumer")
            or {
                "node": "experiment",
                **({"stage": context.get("requesting_stage")} if context.get("requesting_stage") else {}),
            }
        ),
        "inputs": deepcopy(_list(
            explicit.get("inputs")
            or context.get("inputs")
            or context.get("scientific_parameters")
            or context.get("node_inputs")
        )),
        "requested_assets": assets,
        "acceptance_criteria": acceptance,
        "non_goals": deepcopy(_list(explicit.get("non_goals") or context.get("non_goals"))),
        "scientific_authority": bool(
            review_profile == "plan_bound"
            and explicit.get("scientific_authority", True)
        ),
        "source_artifact_ids": list(dict.fromkeys(
            _text(item) for item in (
                explicit.get("source_artifact_ids")
                or _list(context.get("source_prereg_artifact_id"))
                or source_artifact_ids
                or []
            ) if _text(item)
        )),
    }
    target_software = explicit.get("target_software", context.get("target_software"))
    if target_software not in (None, "", {}):
        base["target_software"] = deepcopy(target_software)
    identity_basis = {
        **base,
        "source_stable_id": source_stable_id,
    }
    spec_hash = _canonical_hash(identity_basis)
    request_id = _text(explicit.get("request_id") or context.get("request_id"))
    base["request_id"] = request_id or f"preq_{spec_hash[:16]}"
    requested_directory = re.search(
        r"(?:子?目录|directory|输出到|output\s+to)\s*"
        r"(?:为|=|:|：|named|called)?\s*[`'\"]?([A-Za-z0-9][A-Za-z0-9._-]*)[/\\]?",
        caller_text,
        flags=re.I,
    )
    base["delivery_name"] = _slug(
        explicit.get("delivery_name")
        or context.get("delivery_name")
        or (requested_directory.group(1) if requested_directory else ""),
        "preprocessing",
    )[:64]
    base["request_spec_hash"] = spec_hash
    provided_hash = _text(explicit.get("request_spec_hash"))
    if provided_hash and provided_hash != spec_hash:
        base["provided_request_spec_hash"] = provided_hash
    return base


def preprocessing_request_fingerprint(value: Any) -> str:
    """Return a stable semantic identity independent of run-local provenance.

    ``request_spec_hash`` also commits the authority/source snapshot used by a
    planning run. That is correct for artifact provenance, but it is too
    specific for deciding whether a persisted blocked report belongs to the
    same caller request on a later run. This fingerprint keeps the requested
    work and authority while excluding run-derived identity fields.
    """
    if not isinstance(value, dict) or not value:
        return ""
    semantic = deepcopy(value)
    for key in ("request_id", "request_spec_hash", "provided_request_spec_hash"):
        semantic.pop(key, None)
    authority = semantic.get("authority")
    if isinstance(authority, dict):
        authority = deepcopy(authority)
        authority.pop("source_hash", None)
        semantic["authority"] = authority
    return _canonical_hash(semantic)


def validate_preprocessing_request(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["preprocessing_request must be an object"]
    errors: list[str] = []
    if _text(value.get("schema_version")) != PREPROCESSING_REQUEST_SCHEMA_VERSION:
        errors.append("unsupported preprocessing_request.schema_version")
    if not _text(value.get("request_id")):
        errors.append("preprocessing_request.request_id is required")
    if not re.fullmatch(r"[0-9a-f]{64}", _text(value.get("request_spec_hash"))):
        errors.append("preprocessing_request.request_spec_hash must be SHA-256")
    if value.get("provided_request_spec_hash"):
        errors.append("preprocessing_request.request_spec_hash does not match the normalized request")
    authority = value.get("authority") if isinstance(value.get("authority"), dict) else {}
    if authority.get("kind") not in AUTHORITY_KINDS:
        errors.append("preprocessing_request.authority.kind is invalid")
    if authority.get("locked") is not True:
        errors.append("preprocessing_request.authority.locked must be true")
    if value.get("review_profile") not in REVIEW_PROFILES:
        errors.append("preprocessing_request.review_profile is invalid")
    if value.get("review_profile") == "request_bound" and value.get("scientific_authority") is not False:
        errors.append("request_bound preprocessing_request cannot claim scientific authority")
    if not _text(value.get("purpose")):
        errors.append("preprocessing_request.purpose is required")
    if not isinstance(value.get("requested_assets"), list):
        errors.append("preprocessing_request.requested_assets must be an array")
    return errors


def decisive_input_gaps(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only omissions that make a requested asset scientifically indeterminate."""
    gaps: list[dict[str, Any]] = []
    request_inputs = request.get("inputs") or []
    for asset in request.get("requested_assets") or []:
        if not isinstance(asset, dict):
            continue
        text = " ".join(str(asset.get(key) or "") for key in (
            "filename", "format", "purpose", "scientific_role", "representation"
        )).casefold()
        evidence = [
            *request_inputs,
            *(asset.get("content_constraints") or []),
            asset.get("generation_recipe"),
            asset.get("parameter_bindings"),
        ]
        missing = ""
        bindings = asset.get("parameter_bindings") if isinstance(asset.get("parameter_bindings"), dict) else {}
        if "kpoints" in text and not all(
            bindings.get(key) not in (None, "", [], {})
            for key in ("grid", "scheme", "shift")
        ):
            missing = "explicit KPOINTS grid, scheme, and shift"
        elif "poscar" in text and not request_inputs and not any(evidence[1:]):
            missing = "atomic structure, lattice, and species coordinates"
        elif re.search(r"(?:^|\W)(?:mesh|grid|网格)(?:$|\W)", text) and not request_inputs and not any(evidence[1:]):
            missing = "geometry/domain and mesh-resolution contract"
        if missing:
            gaps.append({
                "asset_id": asset.get("asset_id"),
                "field": missing,
                "reason": "The value changes the scientific content of the requested file and cannot be guessed.",
            })
    return gaps


def build_preprocessing_work_order(
    request: dict[str, Any],
    requirement_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile authority into execution-neutral work units.

    A plan-bound unit may point to a declared research stage.  A request-bound
    unit is owned by its requested asset and never needs a research stage.
    """
    analysis = requirement_analysis if isinstance(requirement_analysis, dict) else {}
    profile = _text(request.get("review_profile")) or "request_bound"
    assets = [item for item in request.get("requested_assets") or [] if isinstance(item, dict)]
    if not assets:
        assets = [
            {
                "asset_id": _slug(item.get("id") or item.get("name_or_role"), f"asset_{index + 1}"),
                "filename": _text(item.get("expected_filename") or item.get("declared_output_path") or item.get("name_or_role")),
                "format": _text(item.get("format")),
                "purpose": _text(item.get("reason")),
                "content_constraints": deepcopy(_list(item.get("acceptance_criteria"))),
                "dependencies": deepcopy(_list(item.get("dependencies"))),
                "stage_id": _text(item.get("stage_id")),
            }
            for index, item in enumerate(analysis.get("required_files") or [])
            if (
                isinstance(item, dict)
                and item.get("delivery_required") is not False
                and _text(
                    item.get("fulfillment_kind") or item.get("materialization_kind")
                ).lower() not in {"runtime_output", "runtime_access", "runtime_tool"}
            )
        ]
    stages = {
        _text(item.get("id")): item
        for item in analysis.get("calculation_stages") or []
        if isinstance(item, dict) and _text(item.get("id"))
    }
    work_units: list[dict[str, Any]] = []
    for index, asset in enumerate(assets):
        asset_id = _slug(asset.get("asset_id"), f"asset_{index + 1}")
        declared_stage = _text(asset.get("stage_id"))
        unit = {
            "work_unit_id": f"wu_{asset_id}",
            "kind": "plan_stage_asset" if profile == "plan_bound" else "requested_asset",
            "requested_asset_ids": [asset_id],
            "dependencies": deepcopy(_list(asset.get("dependencies"))),
            "acceptance_criteria": deepcopy(_list(asset.get("content_constraints"))),
            "status": "pending",
        }
        if profile == "plan_bound" and declared_stage in stages:
            unit["authority_stage_id"] = declared_stage
        work_units.append(unit)
    basis = {
        "request_id": request.get("request_id"),
        "request_spec_hash": request.get("request_spec_hash"),
        "work_units": work_units,
    }
    return {
        "schema_version": PREPROCESSING_WORK_ORDER_SCHEMA_VERSION,
        "work_order_id": f"pwo_{_canonical_hash(basis)[:16]}",
        "request_id": request.get("request_id"),
        "request_spec_hash": request.get("request_spec_hash"),
        "review_profile": profile,
        "authority_locked": True,
        "authority": deepcopy(request.get("authority") or {}),
        "work_units": work_units,
    }


def validate_preprocessing_work_order(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["preprocessing_work_order must be an object"]
    errors: list[str] = []
    if value.get("schema_version") != PREPROCESSING_WORK_ORDER_SCHEMA_VERSION:
        errors.append("unsupported preprocessing_work_order.schema_version")
    if value.get("authority_locked") is not True:
        errors.append("preprocessing_work_order.authority_locked must be true")
    if value.get("review_profile") not in REVIEW_PROFILES:
        errors.append("preprocessing_work_order.review_profile is invalid")
    if not _text(value.get("request_id")) or not _text(value.get("request_spec_hash")):
        errors.append("preprocessing_work_order request identity is incomplete")
    units = value.get("work_units")
    if not isinstance(units, list) or not units:
        errors.append("preprocessing_work_order.work_units must not be empty")
    return errors
