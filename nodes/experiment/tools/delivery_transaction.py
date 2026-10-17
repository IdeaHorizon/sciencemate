"""Recoverable three-artifact delivery transaction for Experiment v3.

This module owns no scientific or operational judgement.  ``finish`` supplies
one immutable ``ExperimentDeliveryV1`` claim plus the exact three payloads;
this layer only makes their identity, freeze order, and one ledger commit
recoverable across process interruption.

The guarantee is deliberately node-local: every supported writer goes through
the closure OS lock and exact-head checks.  Core currently has no
``mark_frozen(expected_version, expected_hash)`` CAS, so this must not be
described as protection against a trusted caller bypassing the node tools, nor
as a power-loss-atomic multi-artifact transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from shared.lib.filelock import exclusive

try:
    from . import ledger as _ledger
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore

DELIVERY_SCHEMA = "ExperimentDeliveryV1"
MANIFEST_SCHEMA = "ExperimentClosureManifestV3"
TRIO = ("raw_results", "clean_results", "experiment_log")
MODEL_DELIVERY_CONTRACT = {
    "experiment_v3_delivery": (
        "仅对 Experiment v3：raw_results / clean_results / experiment_log 由 finish 自动生成并冻结。"
        "把 outcome、摘要、实际文件路径和核验方法交给 finish；不要逐个 save_artifact / freeze_artifact，"
        "本段是入口说明，不是待填写的 content 字段，不要补内部事务字段。其他节点和非三件套产物仍遵循各自契约。"
    ),
}
_CLOSURE_DIRNAME = "closure"
_OUTCOMES = {"success", "partial", "failed", "blocked", "cancelled"}
_SCOPES = {"operation", "scientific", "unknown"}
_SCOPE_STATUSES = {"resolved", "undecided"}
_FORMAL_INPUT_STATUSES = {"none", "pending", "valid", "invalid", "unsupported", "conflict"}
_EXECUTION_STATUSES = {"not_run", "attempted"}
_RESULT_STATUSES = {"unavailable", "partial", "delivered"}
_DELIVERY_FIELDS = {
    "schema", "delivery_id", "delivery_owner", "scope", "scope_status",
    "outcome", "execution_status", "result_status", "summary", "gaps",
    "contradictions", "formal_input", "limitations",
    "external_job_refs", "next_step", "blocker",
}
_MANIFEST_FIELDS = {
    "schema", "closure_id", "delivery", "delivery_digest", "artifacts",
    "prepared_digest", "commit", "plan_digest", "revision", "supersedes",
}
_ARTIFACT_SPEC_FIELDS = {
    "name", "id", "content", "expected_content_hash", "metadata",
    "prepared", "frozen",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,60}\Z")


class DeliveryTransactionError(RuntimeError):
    """A persisted delivery fact is missing, corrupt, or has drifted."""

    def __init__(self, code: str, detail: str, **facts: Any):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.facts = facts

    def as_result(self) -> dict[str, Any]:
        return {"status": "error", "error_code": self.code,
                "error": self.detail, **self.facts}


def _validate_exact_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise DeliveryTransactionError(
            "canonical_json_invalid", f"{path} contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_exact_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DeliveryTransactionError(
                    "canonical_json_invalid", f"{path} contains a non-string object key")
            _validate_exact_json(item, f"{path}.{key}")
        return
    raise DeliveryTransactionError(
        "canonical_json_invalid", f"{path} contains non-JSON type {type(value).__name__}")


def canonical_digest(value: Any) -> str:
    _validate_exact_json(value)
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DeliveryTransactionError(
            "canonical_json_invalid",
            f"delivery state is not exact JSON: {type(exc).__name__}: {exc}",
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def content_digest(content: str) -> str:
    if not isinstance(content, str):
        raise DeliveryTransactionError(
            "closure_payload_invalid", "delivery artifact payloads must be strings")
    try:
        payload = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DeliveryTransactionError(
            "closure_payload_invalid", "delivery artifact payload is not valid UTF-8 text") from exc
    return hashlib.sha256(payload).hexdigest()


def delivery_id(state: Any, revision: int = 1) -> str:
    if type(revision) is not int or revision < 1:
        raise DeliveryTransactionError("closure_revision_invalid", "revision must be a positive integer")
    return f"{state.run_id}:delivery" + (f":r{revision}" if revision > 1 else "")


def _delivery_revision(state: Any, identifier: Any) -> int:
    prefix = delivery_id(state)
    if identifier == prefix:
        return 1
    if isinstance(identifier, str) and identifier.startswith(prefix + ":r"):
        suffix = identifier[len(prefix) + 2:]
        if suffix.isdigit() and len(suffix) < 12:
            revision = int(suffix)
            if revision > 1 and identifier == delivery_id(state, revision):
                return revision
    raise DeliveryTransactionError("closure_revision_invalid", "delivery identity does not belong to this run")


def deterministic_names(state: Any) -> dict[str, str]:
    suffix = hashlib.sha256(str(state.run_id).encode("utf-8")).hexdigest()[:20]
    return {
        "raw_results": f"experiment_raw_{suffix}",
        "clean_results": f"experiment_clean_{suffix}",
        "experiment_log": f"experiment_log_{suffix}",
    }


def artifact_id(artifact_type: str, name: str) -> str:
    if artifact_type not in TRIO:
        raise DeliveryTransactionError(
            "closure_artifact_type_invalid", f"unsupported delivery artifact type: {artifact_type}")
    if _SAFE_NAME.fullmatch(name) is None:
        raise DeliveryTransactionError(
            "closure_artifact_name_invalid", f"name is not a stable Core slug: {name!r}")
    return f"{artifact_type}__{name}"


def _closure_dir(state: Any) -> Path:
    return _ledger.ledger_dir(state) / _CLOSURE_DIRNAME


def manifest_path(state: Any) -> Path:
    return _closure_dir(state) / "closure.manifest.json"


def history_manifest_path(state: Any, revision: int) -> Path:
    delivery_id(state, revision)
    return _closure_dir(state) / f"revision-{revision}.manifest.json"


def _lock_path(state: Any) -> Path:
    return _closure_dir(state) / "closure.lock"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise DeliveryTransactionError(
            "closure_durability_unavailable",
            f"cannot durably sync delivery directory {path}: {exc}",
            path=str(path),
        ) from exc


def _atomic_write_manifest(state: Any, manifest: dict[str, Any], *, path: Path | None = None) -> None:
    path = path or manifest_path(state)
    tmp = path.with_name(f".{path.name}.tmp")
    stage = "mkdir"
    replace_completed = False
    try:
        canonical_digest(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        stage = "closure_parent_fsync"
        _fsync_dir(path.parent.parent)
        stage = "tmp_open_write"
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            stage = "tmp_fsync"
            os.fsync(handle.fileno())
        stage = "replace"
        os.replace(tmp, path)
        replace_completed = True
        stage = "manifest_dir_fsync"
        _fsync_dir(path.parent)
    except DeliveryTransactionError as exc:
        raise DeliveryTransactionError(
            exc.code, exc.detail,
            closure_id=manifest.get("closure_id"), checkpoint=stage,
            path=str(path), failed_path=exc.facts.get("path"), tmp_path=str(tmp),
            replace_completed=replace_completed, canonical_visible=path.exists(),
            durability="unknown", retryable=True, owner="finish",
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise DeliveryTransactionError(
            "closure_manifest_persist_failed",
            f"cannot persist delivery manifest during {stage}: {exc}",
            closure_id=manifest.get("closure_id"), checkpoint=stage,
            path=str(path), tmp_path=str(tmp), replace_completed=replace_completed,
            canonical_visible=path.exists(), durability="unknown",
            retryable=True, owner="finish",
        ) from exc


def read_manifest(state: Any) -> dict[str, Any] | None:
    path = manifest_path(state)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryTransactionError(
            "closure_manifest_corrupt",
            f"the persisted delivery manifest is unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise DeliveryTransactionError(
            "closure_manifest_corrupt", "the persisted delivery manifest is not an object")
    return value


def _plan_material(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for artifact_type in TRIO:
        spec = (manifest.get("artifacts") or {}).get(artifact_type) or {}
        artifacts[artifact_type] = {
            "name": spec.get("name"), "id": spec.get("id"),
            "expected_content_hash": spec.get("expected_content_hash"),
            "metadata": spec.get("metadata"),
        }
    return {
        "schema": manifest.get("schema"),
        "closure_id": manifest.get("closure_id"),
        "revision": manifest.get("revision"),
        "supersedes": manifest.get("supersedes"),
        "delivery_digest": manifest.get("delivery_digest"),
        "artifacts": artifacts,
    }


def validate_delivery(state: Any, delivery: dict[str, Any]) -> None:
    """Validate the complete, scope-neutral delivery claim at write and readback."""
    if not isinstance(delivery, dict):
        raise DeliveryTransactionError(
            "delivery_envelope_invalid", "delivery must be an object",
            problems=["delivery must be an object"])
    problems: list[str] = []
    missing = sorted(_DELIVERY_FIELDS - set(delivery))
    extra = sorted(set(delivery) - _DELIVERY_FIELDS)
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if extra:
        problems.append(f"unsupported fields: {', '.join(extra)}")
    # schema / delivery_owner / scope_status 由 finish 自己填，不是模型的调用参数；
    # 这里不合就是 finish 内部组装坏了，措辞按内部错误写，不写成对调用方的字段契约
    # （tests/test_tool_contracts_reach_the_caller.py 把「<字段> must …」当作模型可见契约核对）。
    if delivery.get("schema") != DELIVERY_SCHEMA:
        problems.append(f"internal delivery envelope: schema is not {DELIVERY_SCHEMA}")
    if delivery.get("delivery_owner") != "finish":
        problems.append("internal delivery envelope: delivery_owner is not finish")
    try:
        _delivery_revision(state, delivery.get("delivery_id"))
    except DeliveryTransactionError:
        problems.append("delivery_id does not match this run")
    scope = delivery.get("scope")
    scope_status = delivery.get("scope_status")
    outcome = delivery.get("outcome")
    execution_status = delivery.get("execution_status")
    result_status = delivery.get("result_status")
    formal = delivery.get("formal_input")
    if scope not in _SCOPES:
        problems.append("scope must be operation, scientific, or unknown")
    if scope_status not in _SCOPE_STATUSES:
        problems.append("internal delivery envelope: scope_status is not resolved or undecided")
    if scope == "unknown" and scope_status != "undecided":
        problems.append("unknown scope must remain undecided")
    if scope in {"operation", "scientific"} and scope_status != "resolved":
        problems.append("resolved scope is required for operation or scientific")
    if outcome not in _OUTCOMES:
        problems.append("outcome is invalid")
    if execution_status not in _EXECUTION_STATUSES:
        problems.append("execution_status is invalid")
    if result_status not in _RESULT_STATUSES:
        problems.append("result_status is invalid")
    if not isinstance(delivery.get("summary"), str) or not delivery["summary"].strip():
        problems.append("summary must be a non-empty string")
    if not isinstance(formal, dict) or formal.get("status") not in _FORMAL_INPUT_STATUSES:
        problems.append("formal_input.status is invalid")
    else:
        if set(formal) != {"status", "binding", "error"}:
            problems.append("formal_input must contain exactly status, binding, and error")
        status = formal.get("status")
        binding = formal.get("binding")
        formal_error = formal.get("error")
        if formal_error is not None and not isinstance(formal_error, dict):
            problems.append("formal_input.error must be an object or null")
        unavailable = status in {"pending", "invalid", "unsupported", "conflict"}
        if unavailable:
            if scope != "scientific":
                problems.append("an unavailable formal input implies scientific scope")
            if outcome == "success":
                problems.append("unavailable formal input cannot support success")
            if binding is not None:
                problems.append("unavailable formal input must have binding=null")
            if not isinstance(formal_error, dict) or not formal_error:
                problems.append("unavailable formal input requires a non-empty error object")
            elif (set(formal_error) != {"code", "reason"}
                  or not isinstance(formal_error.get("code"), str)
                  or not formal_error.get("code", "").strip()
                  or not isinstance(formal_error.get("reason"), str)
                  or not formal_error.get("reason", "").strip()):
                problems.append("formal_input.error requires non-empty code and reason")
        elif formal_error is not None:
            problems.append("available or absent formal input must have error=null")
        if status == "none" and binding is not None:
            problems.append("absent formal input must have binding=null")
        if status == "valid":
            if not isinstance(binding, dict) or set(binding) != {
                "artifact_id", "version", "content_hash"
            }:
                problems.append("valid formal input requires an exact artifact binding")
            elif (not isinstance(binding.get("artifact_id"), str)
                  or not binding.get("artifact_id", "").strip()
                  or not isinstance(binding.get("version"), int)
                  or isinstance(binding.get("version"), bool)
                  or binding.get("version", 0) < 1
                  or not isinstance(binding.get("content_hash"), str)
                  or re.fullmatch(r"[0-9a-f]{64}", binding.get("content_hash", "")) is None):
                problems.append("formal input artifact binding is invalid")
        if scope in {"operation", "unknown"} and status != "none":
            problems.append(f"{scope} scope cannot claim a formal scientific input")
    if outcome == "success" and execution_status != "attempted":
        problems.append("success requires an attempted execution")
    if outcome == "success" and result_status != "delivered":
        problems.append("success requires delivered results")
    if outcome != "success" and result_status == "delivered":
        problems.append("a non-success outcome cannot claim delivered results")
    if execution_status == "not_run" and result_status != "unavailable":
        problems.append("not_run execution requires unavailable results")
    if result_status == "partial" and execution_status != "attempted":
        problems.append("partial results require an attempted execution")
    for field in ("gaps", "contradictions", "limitations", "external_job_refs"):
        if not isinstance(delivery.get(field), list):
            problems.append(f"{field} must be an array")
    if delivery.get("next_step") is not None and not isinstance(delivery.get("next_step"), str):
        problems.append("next_step must be a string or null")
    if delivery.get("blocker") is not None and not isinstance(delivery.get("blocker"), dict):
        problems.append("blocker must be an object or null")
    if outcome == "blocked" and not delivery.get("blocker"):
        problems.append("blocked outcome requires a non-empty blocker")
    if outcome == "success" and (
            delivery.get("gaps") or delivery.get("contradictions")
            or delivery.get("blocker") is not None):
        problems.append("success cannot carry gaps, contradictions, or a blocker")
    if problems:
        raise DeliveryTransactionError(
            "delivery_envelope_invalid", "; ".join(problems), problems=problems)
    canonical_digest(delivery)


def _artifact_metadata(
    state: Any,
    delivery: dict[str, Any],
    digest: str,
    artifact_type: str,
) -> dict[str, Any]:
    return {
        "record_kind": "experiment_delivery",
        "delivery_id": delivery["delivery_id"],
        "delivery_owner": "finish",
        "schema_version": 1,
        "envelope_sha256": digest,
        "artifact_role": artifact_type,
    }


def _validate_ledger_binding(state: Any, led: _ledger.Ledger) -> None:
    expected = _ledger.ledger_dir(state).resolve()
    actual = Path(led.root).resolve()
    if actual != expected:
        raise DeliveryTransactionError(
            "closure_ledger_mismatch", "the supplied ledger belongs to another run",
            expected_ledger=str(expected), actual_ledger=str(actual))
    try:
        meta = json.loads((actual / "ledger.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise DeliveryTransactionError(
            "closure_ledger_mismatch", f"the run ledger identity is unreadable: {exc}") from exc
    if str(meta.get("run_id") or "") != str(state.run_id):
        raise DeliveryTransactionError(
            "closure_ledger_mismatch", "ledger meta run_id does not match the delivery run")
    if led.epoch != _ledger.PROTOCOL_EPOCH or not _ledger.is_current_epoch(meta):
        raise DeliveryTransactionError(
            "closure_ledger_epoch_mismatch", "delivery requires the current Experiment protocol ledger")
    if not _ledger.v3_active(state):
        raise DeliveryTransactionError(
            "closure_ledger_not_ready", "delivery requires a completed v3 run bootstrap")
    if led.read_only:
        raise DeliveryTransactionError(
            "closure_ledger_read_only", "the delivery transaction requires the authoritative run ledger")
    if led.owner is None:
        raise DeliveryTransactionError(
            "closure_ledger_owner_missing", "the writable delivery ledger has no physical owner")


def build_manifest(
    state: Any,
    *,
    delivery: dict[str, Any],
    contents: dict[str, str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_delivery(state, delivery)
    if previous is not None:
        validate_manifest(state, previous)
    revision = previous["revision"] + 1 if previous is not None else 1
    expected_id = delivery_id(state, revision)
    if delivery["delivery_id"] != expected_id:
        raise DeliveryTransactionError("closure_revision_invalid", "delivery and transaction revisions differ")
    if set(contents) != set(TRIO):
        raise DeliveryTransactionError(
            "closure_triplet_incomplete", "finish must provide exactly raw, clean, and log payloads")
    digest = canonical_digest(delivery)
    names = deterministic_names(state)
    artifacts: dict[str, Any] = {}
    for artifact_type in TRIO:
        name = names[artifact_type]
        content = contents[artifact_type]
        if not isinstance(content, str):
            raise DeliveryTransactionError(
                "closure_payload_invalid",
                f"{artifact_type} payload must be a string",
                artifact_type=artifact_type,
            )
        artifacts[artifact_type] = {
            "name": name,
            "id": artifact_id(artifact_type, name),
            "content": content,
            "expected_content_hash": content_digest(content),
            "metadata": _artifact_metadata(state, delivery, digest, artifact_type),
            "prepared": None,
            "frozen": None,
        }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "closure_id": expected_id,
        "revision": revision,
        "supersedes": ({"closure_id": previous["closure_id"],
                        "revision": previous["revision"],
                        "manifest_digest": canonical_digest(previous)} if previous is not None else None),
        "delivery": delivery,
        "delivery_digest": digest,
        "artifacts": artifacts,
        "prepared_digest": None,
        "commit": None,
    }
    manifest["plan_digest"] = canonical_digest(_plan_material(manifest))
    return manifest


def validate_manifest(state: Any, manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise DeliveryTransactionError(
            "closure_manifest_shape_invalid", "delivery manifest fields do not match its schema")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise DeliveryTransactionError(
            "closure_manifest_schema_invalid", "unsupported delivery manifest schema")
    revision = manifest.get("revision")
    if manifest.get("closure_id") != delivery_id(state, revision):
        raise DeliveryTransactionError(
            "closure_manifest_identity_mismatch", "manifest belongs to another run or delivery")
    prior = manifest.get("supersedes")
    if ((revision == 1 and prior is not None) or (revision > 1 and (
            not isinstance(prior, dict) or set(prior) != {"closure_id", "revision", "manifest_digest"}
            or type(prior.get("revision")) is not int or prior["revision"] != revision - 1
            or prior.get("closure_id") != delivery_id(state, revision - 1)
            or not isinstance(prior.get("manifest_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", prior["manifest_digest"]) is None))):
        raise DeliveryTransactionError("closure_revision_invalid", "successor does not identify its preceding delivery")
    delivery = manifest.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("schema") != DELIVERY_SCHEMA:
        raise DeliveryTransactionError(
            "closure_manifest_delivery_invalid", "manifest has no valid ExperimentDeliveryV1")
    if delivery.get("delivery_id") != manifest.get("closure_id"):
        raise DeliveryTransactionError(
            "closure_manifest_delivery_invalid", "delivery id does not match the closure id")
    validate_delivery(state, delivery)
    if canonical_digest(delivery) != manifest.get("delivery_digest"):
        raise DeliveryTransactionError(
            "closure_manifest_delivery_digest_mismatch", "delivery payload no longer matches its digest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(TRIO):
        raise DeliveryTransactionError(
            "closure_manifest_triplet_invalid", "manifest does not name exactly the delivery triplet")
    for artifact_type in TRIO:
        spec = artifacts.get(artifact_type)
        if not isinstance(spec, dict):
            raise DeliveryTransactionError(
                "closure_manifest_triplet_invalid", f"missing spec for {artifact_type}")
        if set(spec) != _ARTIFACT_SPEC_FIELDS:
            raise DeliveryTransactionError(
                "closure_manifest_triplet_invalid", f"invalid spec fields for {artifact_type}")
        if spec.get("name") != deterministic_names(state)[artifact_type]:
            raise DeliveryTransactionError(
                "closure_manifest_artifact_name_mismatch",
                f"non-deterministic reserved name for {artifact_type}")
        if spec.get("id") != artifact_id(artifact_type, str(spec.get("name") or "")):
            raise DeliveryTransactionError(
                "closure_manifest_artifact_id_mismatch", f"invalid deterministic id for {artifact_type}")
        if not isinstance(spec.get("content"), str):
            raise DeliveryTransactionError(
                "closure_manifest_content_invalid", f"payload is not a string for {artifact_type}")
        if content_digest(spec["content"]) != spec.get("expected_content_hash"):
            raise DeliveryTransactionError(
                "closure_manifest_content_digest_mismatch", f"payload drift for {artifact_type}")
        md = spec.get("metadata")
        if not isinstance(md, dict):
            raise DeliveryTransactionError(
                "closure_manifest_metadata_invalid", f"metadata drift for {artifact_type}")
        expected_metadata = _artifact_metadata(
            state, delivery, manifest["delivery_digest"], artifact_type)
        if canonical_digest(md) != canonical_digest(expected_metadata):
            raise DeliveryTransactionError(
                "closure_manifest_metadata_invalid", f"metadata drift for {artifact_type}")
        expected_identity = {
            "id": spec["id"], "version": revision,
            "content_hash": spec["expected_content_hash"],
        }
        prepared_identity = spec.get("prepared")
        if (prepared_identity is not None
                and canonical_digest(prepared_identity) != canonical_digest(expected_identity)):
            raise DeliveryTransactionError(
                "closure_manifest_prepared_invalid", f"prepared identity drift for {artifact_type}")
        frozen_identity = spec.get("frozen")
        if frozen_identity is not None:
            if (not isinstance(frozen_identity, dict)
                    or set(frozen_identity) != {*expected_identity, "frozen_at"}
                    or canonical_digest({key: frozen_identity.get(key) for key in expected_identity})
                    != canonical_digest(expected_identity)
                    or not isinstance(frozen_identity.get("frozen_at"), str)
                    or not frozen_identity["frozen_at"].strip()):
                raise DeliveryTransactionError(
                    "closure_manifest_frozen_invalid", f"frozen identity drift for {artifact_type}")
    if canonical_digest(_plan_material(manifest)) != manifest.get("plan_digest"):
        raise DeliveryTransactionError(
            "closure_manifest_plan_digest_mismatch", "manifest plan no longer matches its digest")
    prepared = {kind: artifacts[kind].get("prepared") for kind in TRIO}
    if manifest.get("prepared_digest") is not None:
        if any(not isinstance(value, dict) for value in prepared.values()):
            raise DeliveryTransactionError(
                "closure_manifest_prepared_invalid", "prepared digest exists before all identities")
        if canonical_digest(prepared) != manifest.get("prepared_digest"):
            raise DeliveryTransactionError(
                "closure_manifest_prepared_digest_mismatch", "prepared identities no longer match their digest")
    elif any(artifacts[kind].get("frozen") is not None for kind in TRIO):
        raise DeliveryTransactionError(
            "closure_manifest_frozen_invalid", "an artifact was frozen before all identities were prepared")
    commit = manifest.get("commit")
    if commit is not None:
        if (not isinstance(commit, dict)
                or set(commit) != {"idempotency_key", "seq"}
                or not isinstance(commit.get("idempotency_key"), str)
                or not isinstance(commit.get("seq"), int)
                or isinstance(commit.get("seq"), bool)
                or commit.get("seq", 0) < 1):
            raise DeliveryTransactionError(
                "closure_manifest_commit_invalid", "manifest commit checkpoint is invalid")


def _head_identity(state: Any, artifact_type: str, spec: dict[str, Any],
                   *, require_frozen: bool = False) -> dict[str, Any] | None:
    artifact = str(spec["id"])
    head = state.artifact_head(artifact)
    if head is None:
        return None
    expected_hash = spec["expected_content_hash"]
    problems: list[str] = []
    if head.artifact_id != artifact:
        problems.append("id")
    if head.artifact_type != artifact_type:
        problems.append("type")
    if head.name != spec.get("name"):
        problems.append("name")
    if head.sha256 != expected_hash:
        problems.append("head_hash")
    if type(head.version) is not int or head.version < 1:
        problems.append("head_version")
    if head.produced_by_node_type != "experiment" or head.produced_by_run_id != str(state.run_id):
        problems.append("producer")
    record = state.read_artifact(artifact)
    if not isinstance(record, dict):
        problems.append("unreadable")
    else:
        if (record.get("type") != artifact_type
                or record.get("name") != spec.get("name")
                or type(record.get("version")) is not int
                or record.get("version") != head.version
                or record.get("produced_by_node_type") != "experiment"
                or record.get("produced_by_run_id") != str(state.run_id)):
            problems.append("record_identity")
        if record.get("content_hash") != expected_hash:
            problems.append("record_hash")
        content = record.get("content")
        if not isinstance(content, str) or content_digest(content) != expected_hash:
            problems.append("bytes_hash")
        metadata = record.get("metadata") or {}
        expected_metadata = dict(spec["metadata"])
        if head.frozen:
            expected_metadata.update({"frozen": True, "frozen_at": head.frozen_at})
        if canonical_digest(metadata) != canonical_digest(expected_metadata):
            problems.append("metadata")
    identity = {"id": artifact, "version": head.version,
                "content_hash": head.sha256}
    prepared = spec.get("prepared")
    if isinstance(prepared, dict) and canonical_digest(identity) != canonical_digest(prepared):
        problems.append("prepared_identity")
    if require_frozen:
        if not head.frozen:
            problems.append("not_frozen")
        if (type(head.frozen_version) is not int
                or head.frozen_version != head.version):
            problems.append("frozen_version")
        if head.frozen_sha256 != expected_hash:
            problems.append("frozen_hash")
        if head.frozen:
            if not isinstance(head.frozen_at, str) or not head.frozen_at.strip():
                problems.append("frozen_at")
            frozen_identity = {**identity, "frozen_at": head.frozen_at}
            frozen = spec.get("frozen")
            if (isinstance(frozen, dict)
                    and canonical_digest(frozen_identity) != canonical_digest(frozen)):
                problems.append("frozen_identity")
        else:
            frozen_identity = None
    else:
        frozen_identity = None
    if problems:
        raise DeliveryTransactionError(
            "closure_head_mismatch", f"{artifact_type} head does not match the persisted plan",
            artifact_type=artifact_type, artifact_id=artifact, mismatches=problems,
        )
    return frozen_identity if require_frozen else identity


def _previous_manifest(state: Any, manifest: dict[str, Any]) -> dict[str, Any] | None:
    prior = manifest["supersedes"]
    if prior is None:
        return None
    try:
        previous = json.loads(history_manifest_path(state, prior["revision"]).read_text(encoding="utf-8"))
        validate_manifest(state, previous)
        if (previous["closure_id"] != prior["closure_id"] or previous.get("commit") is None
                or canonical_digest(previous) != prior["manifest_digest"]):
            raise ValueError("preceding sealed manifest does not match successor")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DeliveryTransactionError("closure_revision_invalid", f"cannot verify preceding delivery: {exc}") from exc
    return previous


def _prepare(state: Any, manifest: dict[str, Any]) -> None:
    previous = _previous_manifest(state, manifest)
    for artifact_type in TRIO:
        spec = manifest["artifacts"][artifact_type]
        head = state.artifact_head(spec["id"])
        if previous is not None and head is not None and head.version == previous["revision"]:
            # A Core amendment preserves the frozen predecessor. If save
            # succeeds before its checkpoint is lost, recovery sees the
            # precise new head and never creates another version.
            _head_identity(state, artifact_type, previous["artifacts"][artifact_type], require_frozen=True)
            state.save_artifact(
                artifact_type, spec["name"], spec["content"], metadata=spec["metadata"],
                amendment_reason=f"Experiment delivery correction: {previous['closure_id']} -> {manifest['closure_id']}")
        identity = _head_identity(state, artifact_type, spec)
        if identity is None:
            if previous is not None:
                raise DeliveryTransactionError("closure_revision_invalid", "preceding artifact disappeared during correction")
            saved = state.save_artifact(
                artifact_type, spec["name"], spec["content"], metadata=spec["metadata"])
            if str(saved.get("id") or "") != spec["id"]:
                raise DeliveryTransactionError(
                    "closure_saved_identity_mismatch", f"Core returned an unexpected id for {artifact_type}")
            identity = _head_identity(state, artifact_type, spec)
        if identity is None:
            raise DeliveryTransactionError(
                "closure_draft_missing", f"{artifact_type} was not readable after save")
        if identity["version"] != manifest["revision"]:
            raise DeliveryTransactionError(
                "closure_draft_version_conflict", f"{artifact_type} head is not the planned delivery revision",
                artifact_type=artifact_type, version=identity["version"])
        if spec.get("prepared") is None:
            spec["prepared"] = identity
            _atomic_write_manifest(state, manifest)
    prepared = {kind: manifest["artifacts"][kind]["prepared"] for kind in TRIO}
    digest = canonical_digest(prepared)
    if manifest.get("prepared_digest") not in (None, digest):
        raise DeliveryTransactionError(
            "closure_manifest_prepared_digest_mismatch", "prepared digest changed during recovery")
    if manifest.get("prepared_digest") is None:
        manifest["prepared_digest"] = digest
        _atomic_write_manifest(state, manifest)


def _freeze(state: Any, manifest: dict[str, Any]) -> None:
    if not manifest.get("prepared_digest"):
        raise DeliveryTransactionError(
            "closure_not_prepared", "all three exact identities must be prepared before the first freeze")
    for artifact_type in TRIO:
        spec = manifest["artifacts"][artifact_type]
        _head_identity(state, artifact_type, spec)
        try:
            frozen = _head_identity(state, artifact_type, spec, require_frozen=True)
        except DeliveryTransactionError as exc:
            if exc.facts.get("mismatches") != ["not_frozen", "frozen_version", "frozen_hash"]:
                raise
            # Private node writer after exact-head verification.  This bypasses
            # the public model freeze gate, not Core CAS (which does not exist).
            state.mark_frozen(spec["id"], metadata_patch=dict(spec["metadata"]))
            frozen = _head_identity(state, artifact_type, spec, require_frozen=True)
        if frozen is None:
            raise DeliveryTransactionError(
                "closure_freeze_missing", f"{artifact_type} disappeared during freeze")
        if spec.get("frozen") is None:
            spec["frozen"] = frozen
            _atomic_write_manifest(state, manifest)
        elif canonical_digest(spec["frozen"]) != canonical_digest(frozen):
            raise DeliveryTransactionError(
                "closure_frozen_identity_mismatch", f"{artifact_type} frozen identity drifted")


def _commit(led: _ledger.Ledger, manifest: dict[str, Any]) -> dict[str, Any]:
    artifact_ids = {kind: manifest["artifacts"][kind]["id"] for kind in TRIO}
    identities = {kind: manifest["artifacts"][kind]["frozen"] for kind in TRIO}
    key = f"finish:{manifest['closure_id']}:{manifest['prepared_digest']}"
    closure_rows = [row for row in led.records()
                    if row.get("kind") == "finish"
                    and row.get("closure_id") == manifest["closure_id"]]
    if len(closure_rows) > 1:
        raise DeliveryTransactionError(
            "closure_finish_commit_invalid",
            f"expected at most one finish for this closure, observed {len(closure_rows)}")
    if closure_rows and closure_rows[0].get("idempotency_key") != key:
        raise DeliveryTransactionError(
            "closure_finish_commit_mismatch",
            "a different finish row already owns this closure")
    return led.append_once(
        "finish", key, closure_id=manifest["closure_id"],
        prepared_digest=manifest["prepared_digest"],
        delivery_digest=manifest["delivery_digest"],
        artifact_ids=artifact_ids, artifact_identities=identities,
        outcome=manifest["delivery"].get("outcome"),
    )


def _sealed(state: Any, led: _ledger.Ledger, manifest: dict[str, Any]) -> dict[str, Any]:
    validate_manifest(state, manifest)
    _previous_manifest(state, manifest)
    identities: dict[str, Any] = {}
    for artifact_type in TRIO:
        identity = _head_identity(
            state, artifact_type, manifest["artifacts"][artifact_type], require_frozen=True)
        if identity is None:
            raise DeliveryTransactionError(
                "closure_not_sealed", f"{artifact_type} is missing")
        identities[artifact_type] = identity
    key = f"finish:{manifest['closure_id']}:{manifest['prepared_digest']}"
    commits = [row for row in led.records()
               if row.get("kind") == "finish"
               and row.get("closure_id") == manifest["closure_id"]]
    if len(commits) != 1:
        raise DeliveryTransactionError(
            "closure_finish_commit_invalid",
            f"expected exactly one matching finish commit, observed {len(commits)}")
    commit = commits[0]
    if commit.get("idempotency_key") != key:
        raise DeliveryTransactionError(
            "closure_finish_commit_mismatch", "finish idempotency key does not match the delivery")
    expected_ids = {kind: manifest["artifacts"][kind]["id"] for kind in TRIO}
    if (commit.get("delivery_digest") != manifest["delivery_digest"]
            or commit.get("closure_id") != manifest["closure_id"]
            or commit.get("prepared_digest") != manifest["prepared_digest"]
            or commit.get("artifact_ids") != expected_ids
            or canonical_digest(commit.get("artifact_identities")) != canonical_digest(identities)
            or commit.get("outcome") != manifest["delivery"].get("outcome")):
        raise DeliveryTransactionError(
            "closure_finish_commit_mismatch", "finish commit does not match the frozen delivery")
    commit_view = {"idempotency_key": commit.get("idempotency_key"),
                   "seq": commit.get("seq")}
    if canonical_digest(manifest.get("commit")) != canonical_digest(commit_view):
        raise DeliveryTransactionError(
            "closure_manifest_commit_mismatch", "manifest commit checkpoint conflicts with ledger")
    return {
        "status": "success",
        "closure_id": manifest["closure_id"],
        "revision": manifest["revision"],
        "supersedes": manifest["supersedes"],
        "delivery": manifest["delivery"],
        "delivery_digest": manifest["delivery_digest"],
        "artifact_ids": expected_ids,
        "artifact_identities": identities,
        "finish_seq": commit.get("seq"),
    }


def settle_delivery(
    state: Any,
    led: _ledger.Ledger,
    proposed: dict[str, Any] | None,
    *, previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or recover one delivery under the closure OS lock.

    Reentry completes the current transaction. A successor must explicitly
    identify its sealed predecessor; this is an internal compare-and-replace,
    not a model-facing revision ritual.
    """
    _validate_ledger_binding(state, led)
    with exclusive(_lock_path(state)):
        manifest = read_manifest(state)
        reused = manifest is not None
        if previous is not None:
            if proposed is None or manifest is None:
                raise DeliveryTransactionError("closure_revision_invalid", "correction requires a current and a proposed delivery")
            validate_manifest(state, proposed)
            if proposed["supersedes"] != {
                    "closure_id": previous["closure_id"], "revision": previous["revision"],
                    "manifest_digest": canonical_digest(previous)}:
                raise DeliveryTransactionError("closure_revision_invalid", "correction predecessor does not match its plan")
            if canonical_digest(manifest) == canonical_digest(previous):
                _sealed(state, led, manifest)
                archive = history_manifest_path(state, manifest["revision"])
                if archive.exists():
                    try:
                        archived = json.loads(archive.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        raise DeliveryTransactionError("closure_revision_invalid", "preceding delivery archive is unreadable") from exc
                    if canonical_digest(archived) != canonical_digest(manifest):
                        raise DeliveryTransactionError("closure_revision_invalid", "preceding delivery archive conflicts")
                else:
                    _atomic_write_manifest(state, manifest, path=archive)
                # Pending must be durable before any Core head becomes a
                # new draft; readers must not call a mixed triplet sealed.
                _atomic_write_manifest(state, proposed)
                manifest = proposed
                reused = False
            elif manifest.get("plan_digest") != proposed["plan_digest"]:
                raise DeliveryTransactionError("closure_revision_conflict", "another delivery already replaced this predecessor")
        if manifest is None:
            if proposed is None:
                raise DeliveryTransactionError(
                    "closure_manifest_missing", "no delivery was proposed and no manifest exists")
            manifest = proposed
            validate_manifest(state, manifest)
            _atomic_write_manifest(state, manifest)
        else:
            validate_manifest(state, manifest)
        _prepare(state, manifest)
        _freeze(state, manifest)
        commit = _commit(led, manifest)
        commit_view = {"idempotency_key": commit.get("idempotency_key"),
                       "seq": commit.get("seq")}
        if manifest.get("commit") not in (None, commit_view):
            raise DeliveryTransactionError(
                "closure_manifest_commit_mismatch", "manifest commit checkpoint conflicts with ledger")
        if manifest.get("commit") is None:
            manifest["commit"] = commit_view
            _atomic_write_manifest(state, manifest)
        result = _sealed(state, led, manifest)
        result["reused_manifest"] = reused
        return result
