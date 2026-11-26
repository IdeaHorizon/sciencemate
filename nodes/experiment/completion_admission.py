"""Read-only Experiment delivery projection for Core completion admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.completion import CompletionContext, CompletionDecision
from core.task_outcome import OUTCOMES

try:
    from .tools import contract_audit, delivery_transaction, ledger
except ImportError:  # standalone node bootstrap
    from tools import contract_audit, delivery_transaction, ledger


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            size = source.tell()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        return {"read_error": type(exc).__name__}
    return {"exists": True, "sha256": digest, "size": size}


def delivery_read_set(state: Any, audit: dict[str, Any]) -> dict[str, Any]:
    """Cover real persisted facts, excluding Core's own notices and counters."""
    root = ledger.ledger_dir(state)
    identities: dict[str, Any] = {}
    for kind in delivery_transaction.TRIO:
        items = []
        for item in state.list_artifacts(kind, own_only=True):
            artifact_id = item["id"]
            head = state.artifact_head(artifact_id)
            record = state.read_artifact(artifact_id)
            items.append(
                {
                    "artifact_id": artifact_id,
                    "head": {
                        key: getattr(head, key, None)
                        for key in (
                            "version",
                            "sha256",
                            "frozen",
                            "frozen_version",
                            "frozen_sha256",
                        )
                    },
                    "content_digest": _digest((record or {}).get("content")),
                    "metadata_digest": _digest((record or {}).get("metadata")),
                }
            )
        identities[kind] = sorted(items, key=lambda row: row["artifact_id"])
    return {
        "epoch": ledger.detect_epoch(state),
        "epoch_identity": _file_identity(root / ledger._META),
        "manifest": _file_identity(delivery_transaction.manifest_path(state)),
        "ledger": _file_identity(root / ledger._EVENTS),
        "artifacts": identities,
        "audit_code": audit.get("error_code") or audit.get("status"),
    }


def experiment_completion_admission(
    ctx: CompletionContext,
    proposed_result: Any,
) -> CompletionDecision:
    # The C audit never repairs, submits, settles, or creates its own blocker.
    before = delivery_read_set(ctx.state, {})
    audit = contract_audit.audit_experiment_delivery(ctx.state)
    after = delivery_read_set(ctx.state, {})
    if before != after:
        return CompletionDecision(
            "reenter" if ctx.can_reenter else "suspend",
            "experiment_delivery_changed_during_audit",
            _digest(after),
            {"before": before, "after": after},
            notice="交付证据在核验期间发生变化。先对账现有事实，再决定是否可以结束。",
        )
    read_set = {**after, "audit_code": audit.get("error_code") or audit.get("status")}
    revision = _digest(read_set)
    facts = {"audit": audit, "read_set": read_set}
    if read_set["epoch"] != ledger.PROTOCOL_EPOCH or not audit.get("applicable"):
        return CompletionDecision(
            "suspend",
            "experiment_protocol_not_ready",
            revision,
            facts,
            notice="本次运行的任务和交付约束尚未建立。保留已有现场，由运行拥有者检查后继续。",
        )
    if audit.get("passed") is True:
        if audit.get("outcome") not in OUTCOMES:
            raise ValueError("sealed delivery audit did not provide a typed task outcome")
        return CompletionDecision(
            "allow",
            "experiment_delivery_sealed",
            revision,
            facts,
            task_outcome=audit.get("outcome"),
        )
    inaccessible = any(
        value.get("read_error")
        for key, value in read_set.items()
        if key in {"epoch_identity", "manifest", "ledger"} and isinstance(value, dict)
    )
    return CompletionDecision(
        "reenter" if ctx.can_reenter and not inaccessible else "suspend",
        str(audit.get("error_code") or "experiment_delivery_unsealed"),
        revision,
        facts,
        notice=(
            "交付尚未形成可核实的封存。依据现有任务约束修复以下事实，保留已执行记录，"
            "不要重复提交已有作业：" + str(audit.get("reason") or "交付证据不完整")
        ),
    )
