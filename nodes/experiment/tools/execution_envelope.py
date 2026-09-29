"""Frozen, route-referenced execution envelopes for Experiment.

An execution envelope is evidence, not a second workflow or lifecycle.  It is
content-addressed, frozen, and referenced from an existing
``declared_route.steps[].action.evidence_refs`` entry.  Route attempts,
submission identity, status, cancellation, and finalization remain owned by
the existing route and external-job mechanisms.

The v1 envelope intentionally implements only one portable storage claim:
``shared_filesystem``.  Target-profile registration and target-side runners
remain Core/platform responsibilities; this module records the immutable
reference that those layers must later verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

try:
    from shared.tools.library.artifacts_extra import (
        _freeze_artifact,
        register_freeze_gate,
        register_save_gate,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from tools.artifacts_extra import (
        _freeze_artifact,
        register_freeze_gate,
        register_save_gate,
    )

try:
    from .path_roles import CANONICAL_ROLES
    from .run_contract import (
        compare_execution_intent_binding_receipt,
        execution_intent_binding_receipt,
        load_run_contract,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from tools.path_roles import CANONICAL_ROLES
    from tools.run_contract import (
        compare_execution_intent_binding_receipt,
        execution_intent_binding_receipt,
        load_run_contract,
    )


EXECUTION_ENVELOPE_TYPE = "execution_envelope"
EXECUTION_ENVELOPE_SCHEMA_VERSION = 1
_ASSURANCE_CLASSES = frozenset({"operation", "evidence_bearing"})
_STORAGE_BINDING_KIND = "shared_filesystem"
_LIFECYCLE_FIELDS = frozenset({"status", "current", "current_step", "attempts"})
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "route_step_id",
        "assurance_class",
        "target_profile_ref",
        "storage_binding",
        "environment_lock",
        "scientific_contract_ref",
        "resource_plan_ref",
        "execution_intent_binding",
    }
)
_ROLE_NAMES = frozenset({"input", "build", "run", "output", "logs"})
# 键是语义角色、值是 canonical path role。二者名字体系不同却都叫 "role"，
# 是本工具最容易被写反的一处：真实 E2E 里节点把 "run_root" 当键写，被拒后
# 只得到「不支持」而拿不到合法集合，连续 20 轮猜不出来。凡拒绝必须报出合法形式。
_ROLE_NAMES_HINT = ", ".join(sorted(_ROLE_NAMES))
_CANONICAL_ROLES_HINT = ", ".join(sorted(CANONICAL_ROLES))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ENVELOPE_REF_RE = re.compile(
    r"^artifact:(?P<artifact_id>execution_envelope__[A-Za-z0-9_-]+)"
    r"@v(?P<version>[1-9][0-9]*)#sha256=(?P<content_hash>[0-9a-f]{64})$"
)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_content(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_sha256(value.removeprefix("sha256:"))
    )


def _normalise_artifact_ref(
    value: Any,
    *,
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} 必须是 artifact_id/type/version/content_hash object")
        return None
    expected = {"artifact_id", "artifact_type", "version", "content_hash"}
    unknown = sorted(set(value).difference(expected))
    if unknown:
        errors.append(f"{label} 含不支持字段：{unknown}")
    artifact_id = str(value.get("artifact_id") or "").strip()
    artifact_type = str(value.get("artifact_type") or "").strip()
    version = value.get("version")
    content_hash = str(value.get("content_hash") or "").strip()
    if not artifact_id:
        errors.append(f"{label}.artifact_id 必须是非空字符串")
    if not artifact_type:
        errors.append(f"{label}.artifact_type 必须是非空字符串")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(f"{label}.version 必须是正整数")
    if not _is_sha256(content_hash):
        errors.append(f"{label}.content_hash 必须是 64 位 sha256 十六进制")
    if (
        not artifact_id
        or not artifact_type
        or not isinstance(version, int)
        or not _is_sha256(content_hash)
    ):
        return None
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "version": version,
        "content_hash": content_hash,
    }


def _normalise_profile_ref(value: Any, errors: list[str]) -> dict[str, str] | None:
    if not isinstance(value, dict):
        errors.append("target_profile_ref 必须是 {id, content_digest}")
        return None
    unknown = sorted(set(value).difference({"id", "content_digest"}))
    if unknown:
        errors.append(f"target_profile_ref 含不支持字段：{unknown}")
    profile_id = str(value.get("id") or "").strip()
    digest = str(value.get("content_digest") or "").strip()
    if not profile_id:
        errors.append("target_profile_ref.id 必须是非空部署 profile 身份")
    if not _is_digest(digest):
        errors.append("target_profile_ref.content_digest 必须是 sha256:<64hex>")
    if not profile_id or not _is_digest(digest):
        return None
    return {"id": profile_id, "content_digest": digest}


def _normalise_storage_binding(value: Any, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append("storage_binding 必须是 {kind, roles}")
        return None
    unknown = sorted(set(value).difference({"kind", "roles"}))
    if unknown:
        errors.append(f"storage_binding 含不支持字段：{unknown}")
    kind = str(value.get("kind") or "").strip()
    if kind != _STORAGE_BINDING_KIND:
        errors.append(
            "storage_binding.kind 当前只实现 shared_filesystem；"
            "不得把 staged/object/scratch 写成已支持"
        )
    raw_roles = value.get("roles")
    if not isinstance(raw_roles, dict) or not raw_roles:
        errors.append(
            "storage_binding.roles 必须是至少一个语义角色到 path_role 的映射；"
            f"键取自 [{_ROLE_NAMES_HINT}]，值取自 [{_CANONICAL_ROLES_HINT}]，"
            '例如 {"run": "run_root", "output": "run_root"}'
        )
        return None
    unknown_roles = sorted(set(raw_roles).difference(_ROLE_NAMES))
    if unknown_roles:
        message = (
            f"storage_binding.roles 含不支持的语义角色：{unknown_roles}；"
            f"合法键只有 [{_ROLE_NAMES_HINT}]"
        )
        misplaced = sorted(set(unknown_roles).intersection(CANONICAL_ROLES))
        if misplaced:
            message += (
                f"。{misplaced} 是 path_role，属于值而不是键，"
                f'应写成 {{"run": "{misplaced[0]}"}}'
            )
        else:
            message += '，例如 {"run": "run_root"}'
        errors.append(message)
    roles: dict[str, str] = {}
    for name, path_role in raw_roles.items():
        role_name = str(name).strip()
        role_value = str(path_role).strip()
        if role_name not in _ROLE_NAMES:
            continue
        if role_value not in CANONICAL_ROLES:
            errors.append(
                f"storage_binding.roles.{role_name} 必须引用已有 canonical path role："
                f"[{_CANONICAL_ROLES_HINT}]；当前值 {role_value!r}"
            )
            continue
        roles[role_name] = role_value
    if kind != _STORAGE_BINDING_KIND or not roles:
        return None
    return {"kind": kind, "roles": dict(sorted(roles.items()))}


def _normalise_execution_intent_binding(value: Any, errors: list[str]) -> dict[str, Any] | None:
    """Validate the compact v1 scope receipt stored inside content-addressing."""
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append("execution_intent_binding 必须是 null 或 v1 receipt object")
        return None
    unknown = sorted(set(value).difference({"schema_version", "intent_digest", "prereg_binding"}))
    if unknown:
        errors.append(f"execution_intent_binding 含不支持字段：{unknown}")
    schema_version = value.get("schema_version")
    digest = str(value.get("intent_digest") or "").strip()
    if (not isinstance(schema_version, int) or isinstance(schema_version, bool)
            or schema_version != 1):
        errors.append("execution_intent_binding.schema_version 必须严格等于整数 1")
    if not _is_sha256(digest):
        errors.append("execution_intent_binding.intent_digest 必须是 64 位 sha256 十六进制")
    prereg = value.get("prereg_binding")
    normalised_prereg = None
    if prereg is not None:
        if not isinstance(prereg, dict):
            errors.append("execution_intent_binding.prereg_binding 必须是 null 或 artifact/version/content_hash")
        else:
            prereg_unknown = sorted(set(prereg).difference({"artifact_id", "version", "content_hash"}))
            if prereg_unknown:
                errors.append(f"execution_intent_binding.prereg_binding 含不支持字段：{prereg_unknown}")
            artifact_id = str(prereg.get("artifact_id") or "").strip()
            version = prereg.get("version")
            content_hash = str(prereg.get("content_hash") or "").strip()
            if not artifact_id:
                errors.append("execution_intent_binding.prereg_binding.artifact_id 必须非空")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                errors.append("execution_intent_binding.prereg_binding.version 必须是正整数")
            if not _is_sha256(content_hash):
                errors.append("execution_intent_binding.prereg_binding.content_hash 必须是 64 位 sha256 十六进制")
            if artifact_id and isinstance(version, int) and not isinstance(version, bool) and version >= 1 and _is_sha256(content_hash):
                normalised_prereg = {"artifact_id": artifact_id, "version": version, "content_hash": content_hash}
    if not _is_sha256(digest):
        return None
    return {
        "schema_version": 1,
        "intent_digest": digest,
        "prereg_binding": normalised_prereg,
    }


def validate_execution_envelope_v1(value: Any) -> dict[str, Any]:
    """Validate and normalise v1 data without reading State or execution state."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {
                "valid": False,
                "errors": ["execution_envelope 必须是 JSON object"],
                "envelope": {},
            }
    if not isinstance(value, dict):
        return {"valid": False, "errors": ["execution_envelope 必须是 object"], "envelope": {}}

    source = deepcopy(value)
    errors: list[str] = []
    unknown = sorted(set(source).difference(_TOP_FIELDS))
    if unknown:
        errors.append(f"execution_envelope 含不支持字段：{unknown}")
    lifecycle = sorted(_LIFECYCLE_FIELDS.intersection(source))
    if lifecycle:
        errors.append(
            f"execution_envelope 不能保存生命周期字段：{lifecycle}；"
            "进度必须由 route/transcript 与 external job 事实派生"
        )
    if source.get("schema_version") != EXECUTION_ENVELOPE_SCHEMA_VERSION:
        errors.append("schema_version 必须严格等于整数 1")
    route_step_id = str(source.get("route_step_id") or "").strip()
    if not _STEP_ID_RE.fullmatch(route_step_id):
        errors.append("route_step_id 必须是合法的 route step id")
    assurance = str(source.get("assurance_class") or "").strip()
    if assurance not in _ASSURANCE_CLASSES:
        errors.append("assurance_class 必须是 operation 或 evidence_bearing")

    profile = _normalise_profile_ref(source.get("target_profile_ref"), errors)
    storage = _normalise_storage_binding(source.get("storage_binding"), errors)
    intent_binding = _normalise_execution_intent_binding(
        source.get("execution_intent_binding"), errors
    )

    raw_lock = source.get("environment_lock")
    if not isinstance(raw_lock, dict):
        errors.append("environment_lock 必须是 {kind: frozen_artifact_refs, entries: []}")
        raw_lock = {}
    lock_unknown = sorted(set(raw_lock).difference({"kind", "entries"}))
    if lock_unknown:
        errors.append(f"environment_lock 含不支持字段：{lock_unknown}")
    lock_kind = str(raw_lock.get("kind") or "").strip()
    if lock_kind != "frozen_artifact_refs":
        errors.append("environment_lock.kind 必须是 frozen_artifact_refs")
    raw_entries = raw_lock.get("entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        errors.append("environment_lock.entries 必须是 artifact reference 列表")
        raw_entries = []
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw_entries):
        ref = _normalise_artifact_ref(
            item, label=f"environment_lock.entries[{index}]", errors=errors
        )
        if ref is not None:
            entries.append(ref)
    unique_entries = {
        (item["artifact_id"], item["version"], item["content_hash"]) for item in entries
    }
    if len(unique_entries) != len(entries):
        errors.append("environment_lock.entries 不能重复引用同一 artifact version")
    entries.sort(
        key=lambda item: (
            item["artifact_id"],
            item["version"],
            item["content_hash"],
        )
    )

    scientific_ref = None
    if source.get("scientific_contract_ref") is not None:
        scientific_ref = _normalise_artifact_ref(
            source.get("scientific_contract_ref"), label="scientific_contract_ref", errors=errors
        )
    resource_ref = None
    if source.get("resource_plan_ref") is not None:
        resource_ref = _normalise_artifact_ref(
            source.get("resource_plan_ref"), label="resource_plan_ref", errors=errors
        )

    if assurance == "evidence_bearing":
        if not entries:
            errors.append("evidence_bearing envelope 必须有至少一项冻结 environment_lock 证据")
        if scientific_ref is None:
            errors.append(
                "evidence_bearing envelope 必须绑定 load_run_contract 提供的冻结 prereg 三元组"
            )
    elif assurance == "operation" and scientific_ref is not None:
        errors.append("operation envelope 不得携带 scientific_contract_ref")

    envelope = {
        "schema_version": EXECUTION_ENVELOPE_SCHEMA_VERSION,
        "route_step_id": route_step_id,
        "assurance_class": assurance,
        "target_profile_ref": profile,
        "storage_binding": storage,
        "environment_lock": {
            "kind": "frozen_artifact_refs",
            "entries": entries,
        },
        "scientific_contract_ref": scientific_ref,
        "resource_plan_ref": resource_ref,
        "execution_intent_binding": intent_binding,
    }
    return {"valid": not errors, "errors": errors, "envelope": envelope}


def _record_artifact_ref(record: dict[str, Any]) -> dict[str, Any] | None:
    artifact_id = str(record.get("id") or "").strip()
    artifact_type = str(record.get("type") or "").strip()
    version = record.get("version")
    content_hash = str(record.get("content_hash") or "").strip()
    if (
        not artifact_id
        or not artifact_type
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not _is_sha256(content_hash)
    ):
        return None
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "version": version,
        "content_hash": content_hash,
    }


def _frozen_head_artifact_ref(
    state: Any,
    artifact_id: str,
    *,
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    record = state.read_artifact(artifact_id)
    if not isinstance(record, dict):
        return None, f"{label}={artifact_id!r} 不存在或不可读"
    metadata = record.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
        return None, (
            f"{label}={artifact_id!r} 的 head 未冻结（或已有待冻结修订）；不得静默绑定旧版本"
        )
    ref = _record_artifact_ref({**record, "id": artifact_id})
    if ref is None:
        return None, f"{label}={artifact_id!r} 缺少有效 type/version/content_hash"
    return ref, None


def _exact_frozen_artifact_record(
    state: Any,
    reference: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    artifact_id = str(reference.get("artifact_id") or "")
    expected_version = reference.get("version")
    expected_hash = str(reference.get("content_hash") or "")
    try:
        versions = state.artifact_versions(artifact_id)
    except Exception as exc:
        return None, f"无法读取 artifact versions：{type(exc).__name__}"
    for record in versions:
        if not isinstance(record, dict):
            continue
        if record.get("version") != expected_version:
            continue
        if record.get("content_hash") != expected_hash:
            continue
        if record.get("type") != reference.get("artifact_type"):
            return None, "artifact type 与冻结引用不匹配"
        metadata = record.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
            return None, "artifact 引用的精确版本未冻结"
        return record, None
    return None, "artifact 引用的精确 version/content_hash 不存在"


def _scientific_contract_ref(state: Any) -> tuple[dict[str, Any] | None, str | None]:
    contract = load_run_contract(state)
    artifact_id = str(contract.get("prereg_artifact_id") or "").strip()
    version = contract.get("prereg_version")
    content_hash = str(contract.get("prereg_content_hash") or "").strip()
    if not artifact_id or not isinstance(version, int) or not _is_sha256(content_hash):
        return None, (
            "当前 run 没有唯一、冻结的 prereg version 三元组；"
            "evidence_bearing envelope 不能猜测科学合同"
        )
    record = state.read_artifact(artifact_id)
    if not isinstance(record, dict):
        return None, "绑定的 prereg artifact 不可读取"
    return {
        "artifact_id": artifact_id,
        "artifact_type": str(record.get("type") or "pre_registration"),
        "version": version,
        "content_hash": content_hash,
    }, None


def execution_envelope_evidence_ref(
    artifact_id: str,
    version: int,
    content_hash: str,
) -> str:
    return f"artifact:{artifact_id}@v{version}#sha256={content_hash}"


def parse_execution_envelope_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    match = _ENVELOPE_REF_RE.fullmatch(value.strip())
    if match is None:
        return None
    return {
        "artifact_id": match.group("artifact_id"),
        "version": int(match.group("version")),
        "content_hash": match.group("content_hash"),
    }


def is_execution_envelope_candidate(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(
        f"artifact:{EXECUTION_ENVELOPE_TYPE}__"
    )


def execution_envelope_refs_for_action(action: Any) -> list[str]:
    if not isinstance(action, dict):
        return []
    refs = action.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    return [str(item).strip() for item in refs if is_execution_envelope_candidate(item)]


_ENVELOPE_GATE_ENV = "EXPERIMENT_ENVELOPE_GATE"
_ENVELOPE_GATE_MODES = frozenset({"off", "warn", "enforce"})
# 阶段B 待办（勿删）：默认档从 warn 翻转为 enforce 是独立后续提交。
# 翻转前置证据 = enforce 档（EXPERIMENT_ENVELOPE_GATE=enforce）下 E4 fixture
# （fixtures/e2e_local_scheduler_scientific.yaml）端到端 passed。
# 目标时限：2026-10-31 前完成翻转并删除本默认值，防止 warn 成为永久态。
_ENVELOPE_GATE_DEFAULT = "warn"


def execution_envelope_gate_mode() -> str:
    """解析 scientific_execution 的 envelope 强制门档位。

    EXPERIMENT_ENVELOPE_GATE ∈ {off, warn, enforce}；未设置回落默认档，
    无法识别的值 fail-closed 到 enforce——错拼开关不能变成静默逃生口。
    """
    raw = str(os.environ.get(_ENVELOPE_GATE_ENV) or "").strip().lower()
    if not raw:
        return _ENVELOPE_GATE_DEFAULT
    if raw in _ENVELOPE_GATE_MODES:
        return raw
    return "enforce"


def missing_execution_envelope_steps(route: Any) -> list[str]:
    """纯函数：列出 effects 含 scientific_execution 但未挂 envelope ref 的 step id。

    operation/diagnostic 步骤（effects 不含 scientific_execution）永不入列；
    带 ref 步骤的校验仍由 validate_route_execution_envelope_refs 独立负责。
    """
    if not isinstance(route, dict):
        return []
    missing: list[str] = []
    for step in route.get("steps") or []:
        if not isinstance(step, dict):
            continue
        effects = {str(item) for item in (step.get("effects") or [])}
        if "scientific_execution" not in effects:
            continue
        if execution_envelope_refs_for_action(step.get("action")):
            continue
        missing.append(str(step.get("id") or "?"))
    return missing


def resolve_execution_envelope_ref(state: Any, value: Any) -> dict[str, Any]:
    parsed = parse_execution_envelope_ref(value)
    if parsed is None:
        return {
            "status": "invalid",
            "reason": "execution_envelope_ref_format_invalid",
            "error": (
                "execution envelope 必须使用 artifact:<id>@v<version>#sha256=<64hex> 的精确冻结引用"
            ),
        }
    reference = {
        "artifact_id": parsed["artifact_id"],
        "artifact_type": EXECUTION_ENVELOPE_TYPE,
        "version": parsed["version"],
        "content_hash": parsed["content_hash"],
    }
    record, error = _exact_frozen_artifact_record(state, reference)
    if record is None:
        return {
            "status": "invalid",
            "reason": "execution_envelope_ref_unresolved",
            "error": error,
            "evidence_ref": value,
        }
    if record.get("produced_by_node_type") != "experiment" or record.get(
        "produced_by_run_id"
    ) != getattr(state, "run_id", None):
        return {
            "status": "invalid",
            "reason": "execution_envelope_not_owned_by_current_experiment_run",
            "error": "execution envelope 必须由当前 Experiment run 产生",
            "evidence_ref": value,
        }
    content = record.get("content")
    if not isinstance(content, str) or _content_hash(content) != parsed["content_hash"]:
        return {
            "status": "invalid",
            "reason": "execution_envelope_content_hash_mismatch",
            "error": "execution envelope 内容哈希与精确 route 引用不一致",
            "evidence_ref": value,
        }
    report = validate_execution_envelope_v1(content)
    if not report["valid"]:
        return {
            "status": "invalid",
            "reason": "execution_envelope_schema_invalid",
            "error": "; ".join(report["errors"]),
            "evidence_ref": value,
        }
    for ref in [
        *report["envelope"]["environment_lock"]["entries"],
        *(
            [report["envelope"]["scientific_contract_ref"]]
            if report["envelope"]["scientific_contract_ref"]
            else []
        ),
        *(
            [report["envelope"]["resource_plan_ref"]]
            if report["envelope"]["resource_plan_ref"]
            else []
        ),
    ]:
        _, reference_error = _exact_frozen_artifact_record(state, ref)
        if reference_error is not None:
            return {
                "status": "invalid",
                "reason": "execution_envelope_supporting_artifact_drift",
                "error": reference_error,
                "evidence_ref": value,
            }
    intent_binding = compare_execution_intent_binding_receipt(
        state, report["envelope"].get("execution_intent_binding"), require=False
    )
    if not intent_binding.get("passed"):
        return {
            "status": "invalid",
            "reason": "execution_envelope_intent_" + str(intent_binding.get("status") or "binding_required"),
            "error": intent_binding.get("reason") or "冻结 execution envelope 与当前 Experiment scope 的上游意图收据不一致",
            "evidence_ref": value,
            "execution_intent_binding": intent_binding,
        }
    return {
        "status": "ready",
        "artifact_id": parsed["artifact_id"],
        "version": parsed["version"],
        "content_hash": parsed["content_hash"],
        "evidence_ref": value,
        "envelope": report["envelope"],
    }


def validate_route_execution_envelope_refs(
    state: Any,
    route: dict[str, Any],
) -> list[str]:
    """Validate only opt-in envelope refs; legacy routes remain compatible."""
    errors: list[str] = []
    for step in route.get("steps") or []:
        if not isinstance(step, dict):
            continue
        action = step.get("action") or {}
        candidates = execution_envelope_refs_for_action(action)
        if not candidates:
            continue
        step_id = str(step.get("id") or "?")
        if len(candidates) != 1:
            errors.append(
                f"step {step_id} 至多只能引用一个 execution envelope，实际为 {len(candidates)}"
            )
            continue
        if str(action.get("tool") or "") != "submit_job":
            errors.append(f"step {step_id} 的 execution envelope 只能挂在 submit_job action")
            continue
        resolved = resolve_execution_envelope_ref(state, candidates[0])
        if resolved.get("status") != "ready":
            errors.append(
                f"step {step_id} 的 execution envelope 无效："
                f"{resolved.get('error') or resolved.get('reason')}"
            )
            continue
        envelope = resolved["envelope"]
        if envelope.get("route_step_id") != step_id:
            errors.append(
                f"step {step_id} 引用的 execution envelope 绑定到 "
                f"{envelope.get('route_step_id')!r}，不能跨 step 复用"
            )
        effects = set(step.get("effects") or [])
        if (
            "scientific_execution" in effects
            and envelope.get("assurance_class") != "evidence_bearing"
        ):
            errors.append(
                f"step {step_id} 声明 scientific_execution，必须引用 evidence_bearing envelope"
            )
    return errors


def refresh_execution_envelope_binding(
    state: Any,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Re-resolve the exact envelope before materialization or spawning."""
    reference = decision.get("execution_envelope_ref")
    if not reference:
        return decision
    resolved = resolve_execution_envelope_ref(state, reference)
    updated = dict(decision)
    if resolved.get("status") == "ready":
        updated["execution_envelope"] = {
            key: resolved[key]
            for key in ("artifact_id", "version", "content_hash", "evidence_ref", "envelope")
        }
        updated["execution_envelope_status"] = "ready"
    else:
        updated["execution_envelope_status"] = "invalid"
        updated["execution_envelope_reason"] = resolved.get("reason")
        updated["execution_envelope_error"] = resolved.get("error")
    return updated


def _execution_envelope_save_gate(state: Any, draft: dict[str, Any]) -> dict[str, Any]:
    del state, draft
    return {
        "failures": {
            "execution_envelope_owner": (
                "execution_envelope 是内容寻址、冻结并精确 route 引用的执行合同；"
                "只能由 declare_execution_envelope 创建"
            ),
        },
        "hint": (
            "先声明目标 profile 引用、shared_filesystem binding 与既有冻结证据，"
            "再调用 declare_execution_envelope；不要通过通用 save_artifact 伪造。"
        ),
    }


def _execution_envelope_freeze_gate(
    state: Any,
    artifact_id: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    del state, artifact_id
    report = validate_execution_envelope_v1(record.get("content"))
    metadata = record.get("metadata") or {}
    digest = _content_hash(str(record.get("content") or ""))
    failures: dict[str, str] = {}
    if not report["valid"]:
        failures["execution_envelope_schema"] = "; ".join(report["errors"])
    if metadata.get("content_addressed") is not True:
        failures["content_addressed"] = (
            "execution envelope metadata 必须声明 content_addressed=true"
        )
    if metadata.get("content_sha256") != digest:
        failures["content_sha256"] = "execution envelope metadata.content_sha256 必须匹配实际内容"
    return {
        "failures": failures,
        "hint": "使用 declare_execution_envelope；它会生成结构、内容地址并冻结。",
    }


async def _declare_execution_envelope(
    state: Any,
    route_step_id: str,
    assurance_class: str,
    target_profile_ref: dict[str, Any],
    storage_binding: dict[str, Any],
    environment_lock_artifact_ids: list[str] | None = None,
    resource_plan_artifact_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Create one content-addressed frozen envelope; no execution is started."""
    lock_ids = environment_lock_artifact_ids or []
    if not isinstance(lock_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in lock_ids
    ):
        return {
            "status": "error",
            "error_code": "execution_envelope_lock_refs_invalid",
            "error": (
                "environment_lock_artifact_ids 必须是非空 artifact id 字符串列表"
                "（operation 可传 []）"
            ),
        }
    lock_refs: list[dict[str, Any]] = []
    for artifact_id in lock_ids:
        reference, error = _frozen_head_artifact_ref(
            state, artifact_id.strip(), label="environment_lock_artifact_ids"
        )
        if error is not None:
            return {
                "status": "error",
                "error_code": "execution_envelope_lock_ref_unavailable",
                "error": error,
            }
        assert reference is not None
        lock_refs.append(reference)
    resource_ref = None
    if resource_plan_artifact_id is not None and str(resource_plan_artifact_id).strip():
        resource_ref, error = _frozen_head_artifact_ref(
            state, str(resource_plan_artifact_id).strip(), label="resource_plan_artifact_id"
        )
        if error is not None:
            return {
                "status": "error",
                "error_code": "execution_envelope_resource_plan_unavailable",
                "error": error,
            }
    scientific_ref = None
    if str(assurance_class).strip() == "evidence_bearing":
        scientific_ref, error = _scientific_contract_ref(state)
        if error is not None:
            return {
                "status": "error",
                "error_code": "execution_envelope_scientific_contract_unavailable",
                "error": error,
            }
    intent_receipt = execution_intent_binding_receipt(state, require=False)
    if not intent_receipt.get("passed"):
        return {
            "status": "error",
            "error_code": "execution_intent_binding_required",
            "error": "当前 scope 的上游输入或冻结科学契约已漂移/不可核验；不得冻结 execution envelope。",
            "execution_intent_binding": intent_receipt.get("audit"),
        }
    candidate = {
        "schema_version": EXECUTION_ENVELOPE_SCHEMA_VERSION,
        "route_step_id": route_step_id,
        "assurance_class": assurance_class,
        "target_profile_ref": target_profile_ref,
        "storage_binding": storage_binding,
        "environment_lock": {
            "kind": "frozen_artifact_refs",
            "entries": lock_refs,
        },
        "scientific_contract_ref": scientific_ref,
        "resource_plan_ref": resource_ref,
        "execution_intent_binding": intent_receipt.get("receipt"),
    }
    report = validate_execution_envelope_v1(candidate)
    if not report["valid"]:
        return {
            "status": "error",
            "error_code": "execution_envelope_schema_invalid",
            "error": "execution envelope 校验失败",
            "errors": report["errors"],
        }
    content = _canonical_content(report["envelope"])
    digest = _content_hash(content)
    name = f"sha256_{digest[:40]}"
    artifact_id = f"{EXECUTION_ENVELOPE_TYPE}__{name}"
    existing = state.read_artifact(artifact_id)
    if isinstance(existing, dict):
        if (
            existing.get("produced_by_node_type") == "experiment"
            and existing.get("produced_by_run_id") == getattr(state, "run_id", None)
            and existing.get("content") == content
            and isinstance(existing.get("metadata"), dict)
            and existing["metadata"].get("frozen") is True
        ):
            version = existing.get("version")
            content_hash = str(existing.get("content_hash") or "")
            if isinstance(version, int) and _is_sha256(content_hash):
                return {
                    "status": "success",
                    "artifact_id": artifact_id,
                    "version": version,
                    "content_hash": content_hash,
                    "evidence_ref": execution_envelope_evidence_ref(
                        artifact_id, version, content_hash
                    ),
                    "envelope": report["envelope"],
                    "already_declared": True,
                }
        return {
            "status": "error",
            "error_code": "execution_envelope_content_address_collision",
            "error": "内容地址已被不同或未冻结的 envelope 占用；拒绝覆盖或平行修订。",
        }
    try:
        saved = state.save_artifact(
            EXECUTION_ENVELOPE_TYPE,
            name,
            content,
            metadata={
                "schema_version": EXECUTION_ENVELOPE_SCHEMA_VERSION,
                "content_addressed": True,
                "content_sha256": digest,
            },
        )
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "execution_envelope_persistence_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    frozen = await _freeze_artifact(
        state,
        saved["id"],
        "冻结 route step 所引用的 execution envelope",
    )
    if frozen.get("status") != "success":
        return {
            "status": "error",
            "error_code": "execution_envelope_freeze_failed",
            "error": frozen.get("error", "execution envelope 冻结失败"),
            "artifact_id": saved["id"],
        }
    record = state.read_artifact(saved["id"])
    if not isinstance(record, dict):
        return {
            "status": "error",
            "error_code": "execution_envelope_post_freeze_unreadable",
            "error": "execution envelope 冻结后无法机械读取",
            "artifact_id": saved["id"],
        }
    version = record.get("version")
    content_hash = str(record.get("content_hash") or "")
    if not isinstance(version, int) or not _is_sha256(content_hash):
        return {
            "status": "error",
            "error_code": "execution_envelope_post_freeze_identity_invalid",
            "error": "execution envelope 冻结后缺少 version/content_hash",
            "artifact_id": saved["id"],
        }
    return {
        "status": "success",
        "artifact_id": saved["id"],
        "version": version,
        "content_hash": content_hash,
        "evidence_ref": execution_envelope_evidence_ref(saved["id"], version, content_hash),
        "envelope": report["envelope"],
    }


register_tool(
    ToolDefinition(
        name="declare_execution_envelope",
        description=(
            "创建并冻结 route step 的内容寻址 execution envelope。它只记录目标 profile "
            "的不可变引用、已实现的 shared_filesystem binding、既有冻结环境/资源证据 "
            "及 scientific prereg 三元组；不会创建作业、尝试、状态或远端授权。返回的 "
            "evidence_ref 必须逐字放入该 submit_job step.action.evidence_refs。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "route_step_id": {"type": "string"},
                "assurance_class": {
                    "type": "string",
                    "enum": sorted(_ASSURANCE_CLASSES),
                },
                "target_profile_ref": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content_digest": {
                            "type": "string",
                            "description": "sha256:<64 位小写十六进制>",
                        },
                    },
                    "required": ["id", "content_digest"],
                    "additionalProperties": False,
                },
                "storage_binding": {
                    "type": "object",
                    "description": (
                        '例如 {"kind": "shared_filesystem", '
                        '"roles": {"run": "run_root", "output": "run_root"}}'
                    ),
                    "properties": {
                        "kind": {"type": "string", "const": _STORAGE_BINDING_KIND},
                        "roles": {
                            "type": "object",
                            "description": (
                                f"语义角色 → path_role。键取自 [{_ROLE_NAMES_HINT}]；"
                                f"值取自 [{_CANONICAL_ROLES_HINT}]。"
                                "键和值是两套命名，不要把 path_role 写在键上。"
                            ),
                            "propertyNames": {"enum": sorted(_ROLE_NAMES)},
                            "additionalProperties": {"enum": sorted(CANONICAL_ROLES)},
                            "minProperties": 1,
                        },
                    },
                    "required": ["kind", "roles"],
                    "additionalProperties": False,
                },
                "environment_lock_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "已有且冻结的 source/toolchain/platform 等 artifact id；"
                        "evidence_bearing 至少一项，operation 可为空。"
                    ),
                },
                "resource_plan_artifact_id": {"type": "string"},
            },
            "required": [
                "route_step_id",
                "assurance_class",
                "target_profile_ref",
                "storage_binding",
            ],
            "additionalProperties": False,
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _declare_execution_envelope,
)

register_save_gate(EXECUTION_ENVELOPE_TYPE, _execution_envelope_save_gate)
register_freeze_gate(
    EXECUTION_ENVELOPE_TYPE,
    _execution_envelope_freeze_gate,
    content_contract={
        "route_binding": (
            "必须由 declare_execution_envelope 内容寻址并冻结；不可手工 save_artifact。"
        ),
        "storage": "v1 只允许 shared_filesystem，不得把未建设的数据交付模式宣称为可用。",
    },
)
