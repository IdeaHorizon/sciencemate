"""Final review receipt for preprocessing deliveries.

Generation tools and the package reviewer own parsers and domain validators.
This module records only the three conclusions needed at publication time:
the locked request is fulfilled, quality evidence passed, and delivered bytes
are safe and intact.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from nodes.data.pipeline_contract import revision_contract

GATE_REGISTRY_VERSION = "3.0"
REQUIRED_GATES = (
    "requirement_alignment",
    "quality_evidence",
    "delivery_integrity",
)


def required_gate_names(_review_profile: str) -> set[str]:
    """Both authority modes use the same review stages; only their content differs."""
    return set(REQUIRED_GATES)


def _passed(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, dict) and any(
        value.get(key) is True for key in ("complete", "verified", "satisfied")
    )


def _gate(name: str, checks: dict[str, Any]) -> dict[str, Any]:
    passed = bool(checks) and all(_passed(value) for value in checks.values())
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "evidence": {
            "expected_checks": len(checks),
            "observed_checks": len(checks),
            "checks": checks,
        },
    }


def build_review_receipt(
    *,
    review_profile: str,
    preprocessing_request: dict[str, Any],
    work_order: dict[str, Any],
    package_review: dict[str, Any],
    authority_review: dict[str, Any] | None = None,
    package_dir: Path,
    delivery_files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize reviewed evidence without repeating parser or domain logic."""
    profile = review_profile if review_profile in {"plan_bound", "request_bound"} else "request_bound"
    root = package_dir.resolve()
    integrity: dict[str, bool] = {}
    for index, item in enumerate(delivery_files):
        if not isinstance(item, dict):
            continue
        relative = PurePosixPath(str(item.get("path") or ""))
        label = str(relative) or f"file_{index + 1}"
        try:
            path = (root / Path(str(relative))).resolve()
            safe = bool(str(relative)) and not relative.is_absolute() and ".." not in relative.parts
            path.relative_to(root)
        except ValueError:
            path = root
            safe = False
        data = path.read_bytes() if safe and path.is_file() else b""
        declared_hash = str(item.get("sha256") or "").strip().lower()
        integrity[f"safe_present_nonempty:{label}"] = safe and bool(data)
        integrity[f"hash_matches:{label}"] = bool(
            data and declared_hash and hashlib.sha256(data).hexdigest() == declared_hash
        )
    semantic = authority_review if isinstance(authority_review, dict) else {}
    semantic_checks = {
        str(item.get("requirement_id") or ""): (
            str(item.get("status") or "").lower() == "pass"
            and bool(item.get("evidence"))
        )
        for item in semantic.get("requirement_checks") or []
        if isinstance(item, dict) and str(item.get("requirement_id") or "").strip()
    }
    request_id = str(preprocessing_request.get("request_id") or "")
    request_hash = str(preprocessing_request.get("request_spec_hash") or "")
    alignment = {
        "authority_review_passed": str(semantic.get("status") or "").lower() == "pass",
        "authority_evidence_present": bool(semantic_checks) and all(semantic_checks.values()),
        "request_identity_preserved": bool(
            request_id
            and request_hash
            and work_order.get("request_id") == request_id
            and work_order.get("request_spec_hash") == request_hash
        ),
    }
    quality = {
        "producer_and_package_review_passed": str(package_review.get("status") or "").lower() == "pass",
    }
    gates = [
        _gate("requirement_alignment", alignment),
        _gate("quality_evidence", quality),
        _gate("delivery_integrity", integrity),
    ]
    failed = [gate["name"] for gate in gates if gate["status"] != "pass"]
    issues = []
    for gate in gates:
        for check, passed in gate["evidence"]["checks"].items():
            if _passed(passed):
                continue
            file = check.split(":", 1)[1] if ":" in check else ""
            issues.append({
                "code": check.split(":", 1)[0],
                "message": f"Delivery review check {check!r} did not pass.",
                "required_change": "Repair the cited evidence without changing the locked authority.",
                **({"file": file} if file else {}),
            })
    return {
        "schema_version": "1.0",
        "gate_registry_version": GATE_REGISTRY_VERSION,
        "review_profile": profile,
        "request_id": preprocessing_request.get("request_id"),
        "request_spec_hash": preprocessing_request.get("request_spec_hash"),
        "work_order_id": work_order.get("work_order_id"),
        "status": "pass" if not failed else "fail",
        "failed_gates": failed,
        "quality_gates": gates,
        "authority_review": semantic,
        **({"revision_contract": revision_contract(issues, scope="asset")} if failed else {}),
    }


def write_review_receipt(package_dir: Path, receipt: dict[str, Any]) -> Path:
    path = package_dir / "audit" / "preprocessing_review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
