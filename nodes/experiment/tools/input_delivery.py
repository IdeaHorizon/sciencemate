"""Experiment 内 Data 请求与交付状态的持久化真相源。

``hook_state`` 只适合单进程内快速访问：同一进程的 checkpoint 恢复会回填它，
但 ``State.reopen``（executor 死亡续跑）会得到一个全新的空字典。因此 Data 请求、
验收结果和 supersede 关系必须先写入 run-scoped artifact；hook_state 只是由该 artifact
重建的兼容缓存，不能再作为审计权威。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

LEDGER_SCHEMA_VERSION = "experiment.input_delivery.v1"
_LEDGER_ARTIFACT_TYPE = "input_delivery_ledger"
_ACTIVE = "active"
_SUPERSEDED = "superseded"
_LIFECYCLE_STATES = {_ACTIVE, _SUPERSEDED}


class InputDeliveryLedgerError(RuntimeError):
    """持久账本存在但无法被可信解释；调用方必须 fail-closed。"""


def _ledger_name(state: Any) -> str:
    run_id = str(getattr(state, "run_id", "") or "")
    # 不直接拼 run_id：State.save_artifact 会把 name 截为 60 字符，长 run_id 会让
    # 读取端无法可靠重建同一 identity。固定长度摘要同时保持每个 run 独立。
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
    return f"run_{digest}"


def input_delivery_ledger_artifact_id(state: Any) -> str:
    return f"{_LEDGER_ARTIFACT_TYPE}__{_ledger_name(state)}"


def _empty_ledger(state: Any) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "run_id": str(getattr(state, "run_id", "") or ""),
        "revision": 0,
        "specs": {},
    }


def _legacy_ledger(state: Any) -> dict[str, Any]:
    """把旧 hook_state 投影成内存账本；下一次合法 mutation 才持久化。

    这样既不让只读审计产生副作用，又保持历史测试/旧 checkpoint 可读。旧条目没有
    lifecycle_status，按 active 解释；只有显式 supersede 才能让请求失效。
    """
    ledger = _empty_ledger(state)
    hook_state = getattr(state, "hook_state", {}) or {}
    requests = hook_state.get("validated_data_request_specs") or {}
    deliveries = hook_state.get("input_delivery_state") or {}
    if not isinstance(requests, dict):
        requests = {}
    if not isinstance(deliveries, dict):
        deliveries = {}
    for raw_spec_id in sorted(set(requests) | set(deliveries), key=str):
        spec_id = str(raw_spec_id)
        request = requests.get(raw_spec_id)
        delivery = deliveries.get(raw_spec_id)
        if not isinstance(delivery, dict):
            delivery = {}
        lifecycle = str(delivery.get("lifecycle_status") or _ACTIVE)
        if lifecycle not in _LIFECYCLE_STATES:
            lifecycle = _ACTIVE
        ledger["specs"][spec_id] = {
            "request": copy.deepcopy(request) if isinstance(request, dict) else None,
            "lifecycle_status": lifecycle,
            "superseded_by": (
                str(delivery.get("superseded_by"))
                if delivery.get("superseded_by") else None
            ),
            "supersede_reason": (
                str(delivery.get("supersede_reason"))
                if delivery.get("supersede_reason") else None
            ),
            "delivery": {
                str(key): copy.deepcopy(value)
                for key, value in delivery.items()
                if key not in {"lifecycle_status", "superseded_by", "supersede_reason"}
            },
        }
    return ledger


def _validate_ledger(state: Any, payload: Any, record: dict[str, Any]) -> dict[str, Any]:
    run_id = str(getattr(state, "run_id", "") or "")
    if not isinstance(payload, dict):
        raise InputDeliveryLedgerError("input delivery ledger content must be an object")
    if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise InputDeliveryLedgerError("unsupported input delivery ledger schema")
    if str(payload.get("run_id") or "") != run_id:
        raise InputDeliveryLedgerError("input delivery ledger belongs to another run")
    producer_run_id = str(record.get("produced_by_run_id") or "")
    if producer_run_id != run_id:
        raise InputDeliveryLedgerError("input delivery ledger producer does not match this run")
    producer_node_type = str(record.get("produced_by_node_type") or "")
    if producer_node_type != "experiment":
        raise InputDeliveryLedgerError("input delivery ledger must be produced by experiment")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise InputDeliveryLedgerError("input delivery ledger revision is invalid")
    specs = payload.get("specs")
    if not isinstance(specs, dict):
        raise InputDeliveryLedgerError("input delivery ledger specs must be an object")

    normalized = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "run_id": run_id,
        "revision": revision,
        "specs": {},
    }
    for raw_spec_id, raw_entry in specs.items():
        spec_id = str(raw_spec_id or "").strip()
        if not spec_id or not isinstance(raw_entry, dict):
            raise InputDeliveryLedgerError("input delivery ledger contains an invalid spec entry")
        request = raw_entry.get("request")
        delivery = raw_entry.get("delivery")
        lifecycle = raw_entry.get("lifecycle_status")
        if request is not None and not isinstance(request, dict):
            raise InputDeliveryLedgerError(
                f"input delivery ledger spec {spec_id} has invalid request"
            )
        if not isinstance(delivery, dict):
            raise InputDeliveryLedgerError(f"input delivery ledger spec {spec_id} is incomplete")
        if lifecycle not in _LIFECYCLE_STATES:
            raise InputDeliveryLedgerError(
                f"input delivery ledger spec {spec_id} has invalid lifecycle"
            )
        superseded_by = raw_entry.get("superseded_by")
        supersede_reason = raw_entry.get("supersede_reason")
        if lifecycle == _SUPERSEDED:
            valid_target = (
                isinstance(superseded_by, str)
                and bool(superseded_by.strip())
                and superseded_by != spec_id
            )
            if not valid_target:
                raise InputDeliveryLedgerError(
                    f"input delivery ledger spec {spec_id} has invalid supersede target"
                )
            if not isinstance(supersede_reason, str) or not supersede_reason.strip():
                raise InputDeliveryLedgerError(
                    f"input delivery ledger spec {spec_id} lacks supersede reason"
                )
        elif superseded_by not in (None, ""):
            raise InputDeliveryLedgerError(
                f"active input delivery spec {spec_id} cannot have superseded_by"
            )
        normalized["specs"][spec_id] = {
            "request": copy.deepcopy(request) if isinstance(request, dict) else None,
            "lifecycle_status": lifecycle,
            "superseded_by": str(superseded_by) if superseded_by else None,
            "supersede_reason": (
                str(supersede_reason) if supersede_reason else None
            ),
            "delivery": copy.deepcopy(delivery),
        }

    for spec_id, entry in normalized["specs"].items():
        target = entry.get("superseded_by")
        if target and target not in normalized["specs"]:
            raise InputDeliveryLedgerError(
                f"input delivery ledger spec {spec_id} supersedes to an unknown target"
            )
    return normalized


def _hydrate_hook_cache(state: Any, ledger: dict[str, Any]) -> None:
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return
    requests: dict[str, dict[str, Any]] = {}
    deliveries: dict[str, dict[str, Any]] = {}
    for spec_id, entry in ledger.get("specs", {}).items():
        request = entry.get("request")
        if isinstance(request, dict):
            requests[spec_id] = copy.deepcopy(request)
        delivery = copy.deepcopy(entry.get("delivery") or {})
        delivery.update({
            "lifecycle_status": entry.get("lifecycle_status", _ACTIVE),
            "superseded_by": entry.get("superseded_by"),
            "supersede_reason": entry.get("supersede_reason"),
        })
        deliveries[spec_id] = delivery
    hook_state["validated_data_request_specs"] = requests
    hook_state["input_delivery_state"] = deliveries
    hook_state["input_delivery_ledger"] = {
        "artifact_id": input_delivery_ledger_artifact_id(state),
        "revision": ledger.get("revision", 0),
        "schema_version": LEDGER_SCHEMA_VERSION,
    }


def _transcript_records_data_state(state: Any) -> bool:
    """账本缺失时只用 transcript 判定“曾经有状态”，不从中拼第二份真相。"""
    path = getattr(state, "transcript_path", None)
    if not path or not Path(path).is_file():
        return False
    state_events = {
        "data_request_spec_superseded",
        "data_delivery_terminal_outcome",
        "experiment_fallback_authorized",
    }
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("event")
        if event_type in state_events:
            return True
        if event_type == "data_request_spec_validation" and event.get("passed") is True:
            return True
        if event_type in {
            "dataset_consumption_verification",
            "experiment_fallback_inputs_verification",
        } and event.get("passed") is True:
            return True
    return False


def load_input_delivery_ledger(state: Any) -> dict[str, Any]:
    """读取本 run 的权威账本；不存在时兼容投影旧 hook_state。

    artifact 路径存在但内容不可读，与“从未登记过请求”不是一回事。前者抛出明确
    异常，由执行门 fail-closed；绝不退回空 hook_state 形成绕过。
    """
    artifact_id = input_delivery_ledger_artifact_id(state)
    try:
        record = state.read_artifact(artifact_id)
    except Exception as exc:
        raise InputDeliveryLedgerError(
            f"input delivery ledger read failed: {type(exc).__name__}"
        ) from exc
    if record is None:
        try:
            path = state.find_artifact_path(artifact_id)
        except Exception:
            path = None
        if path is not None:
            raise InputDeliveryLedgerError("input delivery ledger exists but is unreadable")
        ledger = _legacy_ledger(state)
        cache = (getattr(state, "hook_state", {}) or {}).get("input_delivery_ledger")
        expected_from_cache = isinstance(cache, dict) and bool(cache.get("revision"))
        if expected_from_cache or (
            not ledger["specs"] and _transcript_records_data_state(state)
        ):
            raise InputDeliveryLedgerError(
                "input delivery ledger is missing although this run recorded Data state"
            )
        _hydrate_hook_cache(state, ledger)
        return ledger
    if not isinstance(record, dict) or record.get("type") != _LEDGER_ARTIFACT_TYPE:
        raise InputDeliveryLedgerError("input delivery ledger artifact has the wrong type")
    try:
        payload = json.loads(record.get("content") or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise InputDeliveryLedgerError("input delivery ledger content is not valid JSON") from exc
    ledger = _validate_ledger(state, payload, record)
    _hydrate_hook_cache(state, ledger)
    return ledger


def save_input_delivery_ledger(state: Any, ledger: dict[str, Any]) -> dict[str, Any]:
    """先持久化，再刷新 hook cache；写失败时内存不冒充已提交。"""
    candidate = copy.deepcopy(ledger)
    candidate["schema_version"] = LEDGER_SCHEMA_VERSION
    candidate["run_id"] = str(getattr(state, "run_id", "") or "")
    revision = candidate.get("revision", 0)
    candidate["revision"] = (revision if isinstance(revision, int) else 0) + 1
    # 复用同一验证器，写入端和读取端不能接受两套 schema。这里构造最小 record
    # 只为校验 run ownership；真正 provenance 由 State.save_artifact 写入。
    candidate = _validate_ledger(
        state,
        candidate,
        {
            "produced_by_run_id": candidate["run_id"],
            "produced_by_node_type": "experiment",
        },
    )
    active_ids = sorted(
        spec_id for spec_id, entry in candidate["specs"].items()
        if entry.get("lifecycle_status") == _ACTIVE
    )
    saved = state.save_artifact(
        _LEDGER_ARTIFACT_TYPE,
        _ledger_name(state),
        json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True),
        metadata={
            "schema_version": LEDGER_SCHEMA_VERSION,
            "run_id": candidate["run_id"],
            "revision": candidate["revision"],
            "active_spec_ids": active_ids,
        },
    )
    _hydrate_hook_cache(state, candidate)
    return {"ledger": candidate, "artifact": saved}


def active_input_delivery_entries(state: Any) -> dict[str, dict[str, Any]]:
    ledger = load_input_delivery_ledger(state)
    return {
        spec_id: entry
        for spec_id, entry in ledger["specs"].items()
        if entry.get("lifecycle_status") == _ACTIVE
    }
