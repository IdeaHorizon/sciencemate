#!/usr/bin/env python3
"""Self-contained acceptance probe for the C9 cross-owner registry."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.state import State  # noqa: E402
from nodes.experiment.tools import operation_completion  # noqa: E402
from nodes.experiment.cross_owner_registry import (  # noqa: E402
    RegistryValidationError,
    load_cross_owner_registry,
    validate_cross_owner_registry,
)
from nodes.experiment.tools.operation_completion import _record_operation_completion  # noqa: E402
from nodes.experiment.tools.run_contract import _classify_experiment_scope  # noqa: E402

EXPECTED_IDS = {
    "experiment-cross-owner-001",
    "experiment-cross-owner-002",
    "experiment-cross-owner-003",
    "experiment-cross-owner-004",
    "experiment-cross-owner-005",
}
OLD_EVIDENCE = {
    "blocker_id": "probe-foreign-owner",
    "category": "dependency",
    "summary": "Core capability is not available",
    "evidence_paths": ["nodes/experiment/cross_owner_registry.json"],
    "requested_action": "Core owner provides the typed contract",
}
_MISSING = object()


def _classify(state: State) -> None:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Probe an honest blocked closure.",
    }
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Bounded mechanical C9 acceptance probe.",
    ))
    assert result["status"] == "success", result


def _complete(state: State, owner: object = _MISSING) -> dict[str, Any]:
    blocker = {
        "blocker_id": "probe-foreign-owner",
        "reporting_node": "experiment",
        **OLD_EVIDENCE,
    }
    if owner is not _MISSING:
        blocker["suggested_owner"] = owner
    state.hook_state["blockers"] = [blocker]
    return asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="probe an honest blocked closure",
        outcome="blocked",
        blocker_id="probe-foreign-owner",
        checks=[],
        next_step="ask the assigned owner to provide the typed contract",
    ))


def _reported(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item for item in checks if item.get("name") == "reported_blocker")


def _surfaces(state: State, completion: dict[str, Any]) -> dict[str, Any]:
    raw = state.read_artifact(completion["raw_results_artifact_id"])
    manifest = json.loads(raw["content"])
    receipt_path = next(
        item["path"]
        for item in manifest["files"]
        if item["role"] == "operation_verification_receipt"
    )
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    clean_record = state.read_artifact(completion["clean_results_artifact_id"])
    clean = json.loads(clean_record["content"])
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    return {
        "raw": _reported(receipt["checks"]),
        "clean": _reported(clean["verification"]["checks"]),
        "log": log["content"],
        "frozen": all(
            record["metadata"]["frozen"] is True
            for record in (raw, clean_record, log)
        ),
    }


def _expect_invalid(payload: Any, code: str) -> None:
    try:
        validate_cross_owner_registry(payload)
    except RegistryValidationError as exc:
        assert exc.code == code, exc
        return
    raise AssertionError(f"registry mutation unexpectedly passed: {code}")


def _registry_lane() -> dict[str, Any]:
    payload = load_cross_owner_registry()
    assert payload["schema_version"] == 1
    # 导入的五条一条不能少;总数不钉死 —— 登记第六条障碍不该让这个探针转红。
    ids = {item["id"] for item in payload["entries"]}
    assert EXPECTED_IDS <= ids
    assert len(ids) == len(payload["entries"])

    expected_issue_links = {
        "experiment-cross-owner-001": [1084],
        "experiment-cross-owner-002": [1084],
        "experiment-cross-owner-003": [1085],
        "experiment-cross-owner-004": [1085],
        "experiment-cross-owner-005": [1086],
        "experiment-cross-owner-007": [1081],
        "experiment-cross-owner-008": [1082],
        "experiment-cross-owner-010": [1080],
        "experiment-cross-owner-011": [1083],
    }
    by_id = {item["id"]: item for item in payload["entries"]}
    for entry_id, issue_numbers in expected_issue_links.items():
        assert by_id[entry_id]["upstream_issues"] == issue_numbers

    expected_acceptance_targets = {
        "experiment-cross-owner-010": {
            "task-instance-uuid-is-modeled": (
                "source_match_count", "task_instance_uuid",
            ),
            "contract-revision-is-modeled": (
                "source_match_count", "contract_revision",
            ),
            "contract-digest-is-modeled": (
                "source_match_count", "contract_digest",
            ),
            "parent-dispatch-id-crosses-dispatch": (
                "source_match_count", "parent_dispatch_id",
            ),
            "concurrent-create-is-unique": (
                "pytest_node",
                "tests/test_task_contract.py::test_concurrent_create_assigns_unique_uuid_alias_and_preserves_every_task",
            ),
            "contract-revisions-are-immutable": (
                "pytest_node",
                "tests/test_task_contract.py::test_contract_revisions_are_immutable_concurrent_and_digest_selected",
            ),
            "dispatch-requires-task-instance": (
                "pytest_node",
                "tests/test_task_contract.py::test_experiment_dispatch_rejects_missing_task_instance_uuid",
            ),
            "child-preserves-dispatch-identity": (
                "pytest_node",
                "tests/test_task_contract.py::test_child_state_and_run_start_preserve_exact_task_contract_and_parent_dispatch",
            ),
            "resume-checks-exact-task-identity": (
                "pytest_node",
                "tests/test_task_contract.py::test_resume_requires_exact_task_contract_identity",
            ),
            "task-status-and-contract-are-independent": (
                "pytest_node",
                "tests/test_task_contract.py::test_task_status_changes_and_contract_revisions_are_independent",
            ),
            "tasks-do-not-cross-bind-preregistrations": (
                "pytest_node",
                "tests/test_task_contract.py::test_repeated_and_distinct_tasks_keep_exact_preregistration_bindings",
            ),
        },
        "experiment-cross-owner-011": {
            "cascade-propagates-child-status": (
                "source_match_count", "child_status",
            ),
            "parent-resume-transition-is-owned": (
                "pytest_node",
                "tests/test_pause_driver.py::test_cascade_resume_records_parent_run_resumed_transition",
            ),
            "cascade-and-direct-summary-match": (
                "pytest_node",
                "tests/test_pause_driver.py::test_cascade_and_direct_paths_match_status_child_status_blockers_and_end_event",
            ),
            "public-parked-view-is-actionable": (
                "pytest_node",
                "tests/test_pause_driver.py::test_public_parked_view_has_reason_recheck_and_waiting_human_has_pause",
            ),
            "auto-answered-pipeline-terminates": (
                "pytest_node",
                "tests/test_pause_driver.py::test_auto_answered_pipeline_leaves_child_incomplete_and_root_not_waiting_human",
            ),
        },
    }

    def acceptance_targets(entry: dict[str, Any]) -> dict[str, tuple[str, str]]:
        return {
            check["id"]: (
                check["kind"],
                check.get("pattern") or check.get("node_id"),
            )
            for check in entry["deletion_condition"]["checks"]
        }

    semantic_mutations = 0
    for entry_id, expected_targets in expected_acceptance_targets.items():
        assert acceptance_targets(by_id[entry_id]) == expected_targets
        for check_id in expected_targets:
            missing = copy.deepcopy(by_id[entry_id])
            missing["deletion_condition"]["checks"] = [
                check
                for check in missing["deletion_condition"]["checks"]
                if check["id"] != check_id
            ]
            assert acceptance_targets(missing) != expected_targets
            semantic_mutations += 1

    unfiled = next(
        item for item in payload["entries"]
        if item["id"] == "experiment-cross-owner-006"
    )
    assert "upstream_issues" not in unfiled

    optional = copy.deepcopy(payload)
    del optional["entries"][0]["upstream_issues"]
    assert validate_cross_owner_registry(optional) is optional

    for invalid_value, code in (
        (1084, "wrong_type"),
        ([], "invalid_value"),
        ([0], "invalid_value"),
        ([-1], "invalid_value"),
        ([True], "invalid_value"),
        (["1084"], "invalid_value"),
        ([1084, 1084], "invalid_value"),
    ):
        invalid_issue = copy.deepcopy(payload)
        invalid_issue["entries"][0]["upstream_issues"] = invalid_value
        _expect_invalid(invalid_issue, code)

    stable_symbol = next(
        item for item in payload["entries"]
        if item["id"] == "experiment-cross-owner-005"
    )["evidence"][1]
    assert stable_symbol["symbol"] == "experiment_contract_audit_on_end"
    assert "line" not in stable_symbol

    symbol_mutations = (
        ("missing", None),
        ("both", None),
        ("invalid", "experiment_contract_audit_on_end()"),
        ("invalid", ".foo"),
        ("invalid", "foo."),
        ("invalid", "foo..bar"),
    )
    for mutation, invalid_value in symbol_mutations:
        invalid_symbol = copy.deepcopy(payload)
        evidence = next(
            item for item in invalid_symbol["entries"]
            if item["id"] == "experiment-cross-owner-005"
        )["evidence"][1]
        if mutation == "missing":
            del evidence["symbol"]
        elif mutation == "both":
            evidence["line"] = 1
        else:
            assert invalid_value is not None
            evidence["symbol"] = invalid_value
        _expect_invalid(invalid_symbol, "invalid_evidence")

    zombie = by_id["experiment-cross-owner-003"]
    assert "natural payload completion" in zombie["symptom"]
    native_job = next(
        evidence
        for evidence in zombie["evidence"]
        if evidence["path"] == "core/isolation/_native_job.py"
    )
    assert native_job["symbol"] == "_run"
    assert "Group.terminate" in native_job["claim"]
    assert "adopted parent" in native_job["claim"]
    assert "os._exit" not in native_job["claim"]

    task_outcome = by_id["experiment-cross-owner-005"]
    hook = next(
        evidence
        for evidence in task_outcome["evidence"]
        if evidence["path"] == "nodes/experiment/hooks.py"
    )
    assert hook["symbol"] == "experiment_contract_audit_on_end"

    unstable_node_evidence = [
        (entry["id"], evidence["path"])
        for entry in payload["entries"]
        for evidence in entry["evidence"]
        if (
            evidence["kind"] == "source"
            and evidence["path"].startswith("nodes/experiment/")
            and ("symbol" not in evidence or "line" in evidence)
        )
    ]
    assert not unstable_node_evidence, unstable_node_evidence

    planned_stop = by_id["experiment-cross-owner-002"]
    planned_stop_anchor = next(
        evidence for evidence in planned_stop["evidence"]
        if evidence["path"] == "nodes/experiment/tools/resource_manager.py"
    )
    assert planned_stop_anchor["symbol"] == "_anchored_expected_termination"

    reopen = by_id["experiment-cross-owner-007"]
    auto_claim = next(
        evidence
        for evidence in reopen["evidence"]
        if evidence["path"] == "nodes/experiment/tools/run_contract.py"
    )
    assert auto_claim["symbol"] == "load_run_contract"

    revise = by_id["experiment-cross-owner-008"]
    assert "d79e0aa0 (2026-08-19)" in revise["symptom"]
    assert "1d4a9f16" not in revise["symptom"]

    prose = copy.deepcopy(payload)
    prose["entries"][0]["deletion_condition"] = "Delete when fixed."
    _expect_invalid(prose, "wrong_type")

    command = copy.deepcopy(payload)
    command["entries"][0]["deletion_condition"] = {
        "mode": "all",
        "checks": [{"id": "unsafe", "kind": "command", "command": "true"}],
    }
    _expect_invalid(command, "unsupported_check_kind")

    unknown = copy.deepcopy(payload)
    unknown["entries"][0]["unknown"] = True
    _expect_invalid(unknown, "unknown_fields")

    duplicate_entry = copy.deepcopy(payload)
    duplicate_entry["entries"].append(copy.deepcopy(duplicate_entry["entries"][0]))
    _expect_invalid(duplicate_entry, "duplicate_entry_id")

    duplicate_check = copy.deepcopy(payload)
    checks = duplicate_check["entries"][0]["deletion_condition"]["checks"]
    checks.append(copy.deepcopy(checks[0]))
    _expect_invalid(duplicate_check, "duplicate_check_id")

    out_of_order = copy.deepcopy(payload)
    out_of_order["entries"][0], out_of_order["entries"][1] = (
        out_of_order["entries"][1], out_of_order["entries"][0]
    )
    _expect_invalid(out_of_order, "entries_out_of_order")

    missing_field = copy.deepcopy(payload)
    del missing_field["entries"][0]["symptom"]
    _expect_invalid(missing_field, "missing_fields")

    empty_evidence = copy.deepcopy(payload)
    empty_evidence["entries"][0]["evidence"] = []
    _expect_invalid(empty_evidence, "invalid_evidence")

    empty_checks = copy.deepcopy(payload)
    empty_checks["entries"][0]["deletion_condition"]["checks"] = []
    _expect_invalid(empty_checks, "invalid_predicate")

    disjunctive_owner = copy.deepcopy(payload)
    disjunctive_owner["entries"][0]["true_owner"] = "framework_or_platform"
    _expect_invalid(disjunctive_owner, "wrong_type")

    unresolved_closed = copy.deepcopy(payload)
    unresolved = next(
        item for item in unresolved_closed["entries"]
        if item["id"] == "experiment-cross-owner-003"
    )
    unresolved["status"] = "closed"
    _expect_invalid(unresolved_closed, "unresolved_owner_closed")
    return {
        "entry_ids": sorted(EXPECTED_IDS),
        "negative_mutations": 24,
        "semantic_mutations": semantic_mutations,
    }


def _owner_lane(root: Path) -> dict[str, Any]:
    state = State.new("experiment", root)
    _classify(state)
    completion = _complete(state, "  core  ")
    surfaces = _surfaces(state, completion)
    expected = {**OLD_EVIDENCE, "suggested_owner": "core"}
    assert completion["status"] == "success", completion
    assert surfaces["raw"]["evidence"] == expected
    assert surfaces["clean"]["evidence"] == expected
    assert '"suggested_owner": "core"' in surfaces["log"]
    assert surfaces["frozen"] is True
    return {"status": completion["status"], "suggested_owner": "core"}


def _ownerless_lane(root: Path) -> dict[str, Any]:
    variants = (_MISSING, None, "", "   ")
    for index, owner in enumerate(variants):
        state = State.new("experiment", root / str(index))
        _classify(state)
        completion = _complete(state, owner)
        surfaces = _surfaces(state, completion)
        assert completion["status"] == "success", completion
        assert surfaces["raw"]["evidence"] == OLD_EVIDENCE
        assert surfaces["clean"]["evidence"] == OLD_EVIDENCE
        assert "suggested_owner" not in surfaces["log"]
        assert surfaces["frozen"] is True
    return {"variants": ["missing", "none", "empty", "whitespace"]}


def _legacy_reported_blocker_evidence(blocker: dict[str, Any]) -> dict[str, Any]:
    return {
        "blocker_id": blocker.get("blocker_id") or blocker.get("id"),
        "category": blocker.get("category"),
        "summary": blocker.get("summary"),
        "evidence_paths": blocker.get("evidence_paths") or [],
        "requested_action": blocker.get("requested_action"),
    }


def _run_lane(
    name: str,
    callback: Callable[[], dict[str, Any]],
    failures: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        return callback()
    except Exception as exc:  # pragma: no cover - CLI diagnostic
        failures.append({
            "lane": name,
            "exception": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        return {"error": failures[-1]["exception"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--legacy-owner-projection",
        action="store_true",
        help="mutation: restore the pre-C9 reported_blocker projection in this process",
    )
    args = parser.parse_args()
    failures: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "mode": "legacy_owner_projection" if args.legacy_owner_projection else "candidate",
    }

    original = getattr(operation_completion, "_reported_blocker_evidence", None)
    if args.legacy_owner_projection:
        operation_completion._reported_blocker_evidence = _legacy_reported_blocker_evidence
    try:
        report["registry"] = _run_lane("registry", _registry_lane, failures)
        with tempfile.TemporaryDirectory(prefix="c9-cross-owner-registry-") as temp_dir:
            root = Path(temp_dir)
            report["owner"] = _run_lane(
                "owner", lambda: _owner_lane(root / "owner"), failures,
            )
            report["ownerless"] = _run_lane(
                "ownerless", lambda: _ownerless_lane(root / "ownerless"), failures,
            )
    finally:
        if args.legacy_owner_projection:
            if original is None:
                delattr(operation_completion, "_reported_blocker_evidence")
            else:
                operation_completion._reported_blocker_evidence = original

    report["failures"] = [
        {key: value for key, value in item.items() if key != "traceback"}
        for item in failures
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
