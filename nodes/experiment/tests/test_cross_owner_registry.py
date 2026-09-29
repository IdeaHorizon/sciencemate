"""C9: authoritative cross-owner registry and blocker-owner projection."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.state import State
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import _classify_experiment_scope

EXPECTED_ENTRY_IDS = {
    "experiment-cross-owner-001",
    "experiment-cross-owner-002",
    "experiment-cross-owner-003",
    "experiment-cross-owner-004",
    "experiment-cross-owner-005",
}
_MISSING = object()
_OLD_REPORTED_BLOCKER_EVIDENCE = {
    "blocker_id": "foreign-owner-blocker",
    "category": "dependency",
    "summary": "Core capability is not available",
    "evidence_paths": ["nodes/experiment/cross_owner_registry.json"],
    "requested_action": "Core owner provides the typed contract",
}

_CORE_ACCEPTANCE_CHECKS = {
    "experiment-cross-owner-010": (
        (
            "task-instance-uuid-is-modeled", "source_match_count",
            ("core/tasks.py", "core/state.py"), "task_instance_uuid", "gte", 2,
        ),
        (
            "contract-revision-is-modeled", "source_match_count",
            ("core/tasks.py", "core/state.py"), "contract_revision", "gte", 2,
        ),
        (
            "contract-digest-is-modeled", "source_match_count",
            ("core/tasks.py", "core/state.py"), "contract_digest", "gte", 2,
        ),
        (
            "parent-dispatch-id-crosses-dispatch", "source_match_count",
            ("shared/tools/run_node.py", "core/executor.py", "core/state.py"),
            "parent_dispatch_id", "gte", 3,
        ),
        (
            "concurrent-create-is-unique", "pytest_node",
            "tests/test_task_contract.py::test_concurrent_create_assigns_unique_uuid_alias_and_preserves_every_task",
        ),
        (
            "contract-revisions-are-immutable", "pytest_node",
            "tests/test_task_contract.py::test_contract_revisions_are_immutable_concurrent_and_digest_selected",
        ),
        (
            "dispatch-requires-task-instance", "pytest_node",
            "tests/test_task_contract.py::test_experiment_dispatch_rejects_missing_task_instance_uuid",
        ),
        (
            "child-preserves-dispatch-identity", "pytest_node",
            "tests/test_task_contract.py::test_child_state_and_run_start_preserve_exact_task_contract_and_parent_dispatch",
        ),
        (
            "resume-checks-exact-task-identity", "pytest_node",
            "tests/test_task_contract.py::test_resume_requires_exact_task_contract_identity",
        ),
        (
            "task-status-and-contract-are-independent", "pytest_node",
            "tests/test_task_contract.py::test_task_status_changes_and_contract_revisions_are_independent",
        ),
        (
            "tasks-do-not-cross-bind-preregistrations", "pytest_node",
            "tests/test_task_contract.py::test_repeated_and_distinct_tasks_keep_exact_preregistration_bindings",
        ),
    ),
    "experiment-cross-owner-011": (
        (
            "cascade-propagates-child-status", "source_match_count",
            ("core/pause_driver.py",), "child_status", "gte", 1,
        ),
        (
            "parent-resume-transition-is-owned", "pytest_node",
            "tests/test_pause_driver.py::test_cascade_resume_records_parent_run_resumed_transition",
        ),
        (
            "cascade-and-direct-summary-match", "pytest_node",
            "tests/test_pause_driver.py::test_cascade_and_direct_paths_match_status_child_status_blockers_and_end_event",
        ),
        (
            "public-parked-view-is-actionable", "pytest_node",
            "tests/test_pause_driver.py::test_public_parked_view_has_reason_recheck_and_waiting_human_has_pause",
        ),
        (
            "auto-answered-pipeline-terminates", "pytest_node",
            "tests/test_pause_driver.py::test_auto_answered_pipeline_leaves_child_incomplete_and_root_not_waiting_human",
        ),
    ),
}


def _registry_api():
    from nodes.experiment.cross_owner_registry import (
        RegistryValidationError,
        load_cross_owner_registry,
        validate_cross_owner_registry,
    )

    return (
        RegistryValidationError,
        load_cross_owner_registry,
        validate_cross_owner_registry,
    )


def _validated_registry() -> dict[str, Any]:
    _error, load_registry, _validate = _registry_api()
    return load_registry()


def _assert_invalid(payload: Any, expected_code: str) -> None:
    error_type, _load, validate = _registry_api()
    with pytest.raises(error_type) as exc_info:
        validate(payload)
    assert exc_info.value.code == expected_code, str(exc_info.value)


def _entry(payload: dict[str, Any], entry_id: str) -> dict[str, Any]:
    return next(item for item in payload["entries"] if item["id"] == entry_id)


def _acceptance_check_contract(check: dict[str, Any]) -> tuple[Any, ...]:
    if check["kind"] == "source_match_count":
        return (
            check["id"], check["kind"], tuple(check["paths"]), check["pattern"],
            check["expect"]["operator"], check["expect"]["value"],
        )
    return check["id"], check["kind"], check["node_id"]


def _assert_acceptance_contract(payload: dict[str, Any], entry_id: str) -> None:
    checks = _entry(payload, entry_id)["deletion_condition"]["checks"]
    assert tuple(map(_acceptance_check_contract, checks)) == _CORE_ACCEPTANCE_CHECKS[entry_id]


def test_product_registry_is_strict_and_keeps_every_imported_entry():
    """导入的那五条一条不能少；但登记表本来就该继续长。

    原来这里断言 ``len(entries) == 5``。那不是"导入完整"的判据,是"永远只有五条"
    —— 登记第六条跨 owner 障碍时它必然转红,而转红的原因与被登记的那条障碍毫无
    关系。真正要保的不变量是:**阶段一导入的五条身份不丢、003 仍未定责**;
    新增条目由 schema 校验自己把关,不靠一个计数。
    """
    payload = _validated_registry()

    assert payload["schema_version"] == 1
    assert payload["registry_id"] == "experiment_phase1_cross_owner_non_goals"
    assert EXPECTED_ENTRY_IDS <= {item["id"] for item in payload["entries"]}
    assert len({item["id"] for item in payload["entries"]}) == len(payload["entries"])
    assert _entry(payload, "experiment-cross-owner-003")["true_owner"]["state"] == "unresolved"


def test_registry_records_core_issue_links_and_keeps_unfiled_items_absent():
    payload = _validated_registry()
    expected = {
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

    for entry_id, issue_numbers in expected.items():
        assert _entry(payload, entry_id)["upstream_issues"] == issue_numbers
    for unfiled in (
        "experiment-cross-owner-006",
        "experiment-cross-owner-012",
    ):
        assert "upstream_issues" not in _entry(payload, unfiled)


@pytest.mark.parametrize("entry_id", sorted(_CORE_ACCEPTANCE_CHECKS))
def test_core_issue_deletion_conditions_lock_every_numbered_acceptance(entry_id: str):
    _assert_acceptance_contract(_validated_registry(), entry_id)


@pytest.mark.parametrize("entry_id", sorted(_CORE_ACCEPTANCE_CHECKS))
def test_core_issue_acceptance_lock_detects_removed_or_rewritten_checks(entry_id: str):
    payload = _validated_registry()
    checks = _entry(payload, entry_id)["deletion_condition"]["checks"]

    for index in range(len(checks)):
        missing = copy.deepcopy(payload)
        del _entry(missing, entry_id)["deletion_condition"]["checks"][index]
        with pytest.raises(AssertionError):
            _assert_acceptance_contract(missing, entry_id)

        rewritten = copy.deepcopy(payload)
        changed = _entry(rewritten, entry_id)["deletion_condition"]["checks"][index]
        if changed["kind"] == "source_match_count":
            changed["pattern"] = "unrelated_token"
        else:
            changed["node_id"] += "_unrelated"
        with pytest.raises(AssertionError):
            _assert_acceptance_contract(rewritten, entry_id)


def test_upstream_issues_is_optional():
    payload = _validated_registry()
    del _entry(payload, "experiment-cross-owner-001")["upstream_issues"]
    _error, _load, validate = _registry_api()

    assert validate(payload) is payload


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1084, "wrong_type"),
        ([], "invalid_value"),
        ([0], "invalid_value"),
        ([-1], "invalid_value"),
        ([True], "invalid_value"),
        (["1084"], "invalid_value"),
        ([1084, 1084], "invalid_value"),
    ],
)
def test_upstream_issues_rejects_non_positive_or_duplicate_values(value: Any, code: str):
    payload = _validated_registry()
    _entry(payload, "experiment-cross-owner-001")["upstream_issues"] = value

    _assert_invalid(payload, code)


def test_source_evidence_accepts_one_stable_symbol_locator():
    payload = _validated_registry()
    evidence = _entry(
        payload, "experiment-cross-owner-005",
    )["evidence"][1]
    _error, _load, validate = _registry_api()

    assert evidence["symbol"] == "experiment_contract_audit_on_end"
    assert "line" not in evidence
    assert validate(payload) is payload


def test_node_owned_source_evidence_uses_stable_symbols_not_bare_line_numbers():
    payload = _validated_registry()
    unstable = [
        (entry["id"], evidence["path"])
        for entry in payload["entries"]
        for evidence in entry["evidence"]
        if (
            evidence["kind"] == "source"
            and evidence["path"].startswith("nodes/experiment/")
            and ("symbol" not in evidence or "line" in evidence)
        )
    ]

    assert unstable == []


@pytest.mark.parametrize(
    ("mutation", "invalid_symbol"),
    [
        ("missing", None),
        ("both", None),
        ("invalid", "experiment_contract_audit_on_end()"),
        ("invalid", ".foo"),
        ("invalid", "foo."),
        ("invalid", "foo..bar"),
        ("line-end", None),
    ],
)
def test_source_evidence_rejects_missing_or_ambiguous_symbol_locator(
    mutation: str,
    invalid_symbol: str | None,
):
    payload = _validated_registry()
    evidence = _entry(
        payload, "experiment-cross-owner-005",
    )["evidence"][1]
    if mutation == "missing":
        del evidence["symbol"]
    elif mutation == "both":
        evidence["line"] = 1
    elif mutation == "invalid":
        assert invalid_symbol is not None
        evidence["symbol"] = invalid_symbol
    else:
        evidence["line_end"] = 2

    _assert_invalid(payload, "invalid_evidence")


def test_registry_keeps_reviewed_source_facts_for_linked_core_issues():
    payload = _validated_registry()

    zombie = _entry(payload, "experiment-cross-owner-003")
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

    task_outcome = _entry(payload, "experiment-cross-owner-005")
    hook = next(
        evidence
        for evidence in task_outcome["evidence"]
        if evidence["path"] == "nodes/experiment/hooks.py"
    )
    assert hook["symbol"] == "experiment_contract_audit_on_end"

    planned_stop = _entry(payload, "experiment-cross-owner-002")
    planned_stop_anchor = next(
        evidence
        for evidence in planned_stop["evidence"]
        if evidence["path"] == "nodes/experiment/tools/resource_manager.py"
    )
    assert planned_stop_anchor["symbol"] == "_anchored_expected_termination"

    reopen = _entry(payload, "experiment-cross-owner-007")
    auto_claim = next(
        evidence
        for evidence in reopen["evidence"]
        if evidence["path"] == "nodes/experiment/tools/run_contract.py"
    )
    assert auto_claim["symbol"] == "load_run_contract"

    revise = _entry(payload, "experiment-cross-owner-008")
    assert "d79e0aa0 (2026-08-19)" in revise["symptom"]
    assert "1d4a9f16" not in revise["symptom"]


def test_registry_rejects_unknown_fields_at_every_schema_layer():
    payload = _validated_registry()
    mutations = []

    root = copy.deepcopy(payload)
    root["unexpected"] = True
    mutations.append(root)

    entry = copy.deepcopy(payload)
    entry["entries"][0]["unexpected"] = True
    mutations.append(entry)

    evidence = copy.deepcopy(payload)
    evidence["entries"][0]["evidence"][0]["unexpected"] = True
    mutations.append(evidence)

    owner = copy.deepcopy(payload)
    owner["entries"][0]["true_owner"]["owner"]["unexpected"] = True
    mutations.append(owner)

    condition = copy.deepcopy(payload)
    condition["entries"][0]["deletion_condition"]["unexpected"] = True
    mutations.append(condition)

    check = copy.deepcopy(payload)
    check["entries"][0]["deletion_condition"]["checks"][0]["unexpected"] = True
    mutations.append(check)

    for mutation in mutations:
        _assert_invalid(mutation, "unknown_fields")


def test_registry_rejects_free_prose_deletion_condition():
    payload = _validated_registry()
    payload["entries"][0]["deletion_condition"] = "Delete after Core fixes it."
    _assert_invalid(payload, "wrong_type")


def test_registry_rejects_command_argv_and_arbitrary_expression_predicates():
    payload = _validated_registry()
    payload["entries"][0]["deletion_condition"] = {
        "mode": "all",
        "checks": [{
            "id": "unsafe-command",
            "kind": "command",
            "command": "rg typed_receipt core",
        }],
    }
    _assert_invalid(payload, "unsupported_check_kind")

    for forbidden in ("argv", "expression"):
        mutation = _validated_registry()
        mutation["entries"][0]["deletion_condition"]["checks"][0][forbidden] = []
        _assert_invalid(mutation, "unknown_fields")


def test_registry_rejects_duplicate_entry_and_predicate_ids():
    duplicate_entry = _validated_registry()
    duplicate_entry["entries"].append(copy.deepcopy(duplicate_entry["entries"][0]))
    _assert_invalid(duplicate_entry, "duplicate_entry_id")

    duplicate_check = _validated_registry()
    checks = duplicate_check["entries"][0]["deletion_condition"]["checks"]
    checks.append(copy.deepcopy(checks[0]))
    _assert_invalid(duplicate_check, "duplicate_check_id")


def test_registry_rejects_out_of_order_and_incomplete_entries():
    out_of_order = _validated_registry()
    out_of_order["entries"][0], out_of_order["entries"][1] = (
        out_of_order["entries"][1], out_of_order["entries"][0]
    )

    missing_field = _validated_registry()
    del missing_field["entries"][0]["symptom"]

    empty_evidence = _validated_registry()
    empty_evidence["entries"][0]["evidence"] = []

    empty_checks = _validated_registry()
    empty_checks["entries"][0]["deletion_condition"]["checks"] = []

    for payload, code in (
        (out_of_order, "entries_out_of_order"),
        (missing_field, "missing_fields"),
        (empty_evidence, "invalid_evidence"),
        (empty_checks, "invalid_predicate"),
    ):
        _assert_invalid(payload, code)


def test_registry_rejects_disjunctive_string_owner():
    payload = _validated_registry()
    payload["entries"][0]["true_owner"] = "framework_or_platform"
    _assert_invalid(payload, "wrong_type")


def test_unresolved_owner_cannot_be_closed():
    payload = _validated_registry()
    _entry(payload, "experiment-cross-owner-003")["status"] = "closed"
    _assert_invalid(payload, "unresolved_owner_closed")


def _classify_operation(state: State) -> None:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Record an honest blocked operation.",
    }
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="This is a bounded mechanical closure test.",
    ))
    assert result["status"] == "success", result


def _complete_blocked_operation(
    state: State,
    *,
    suggested_owner: object = _MISSING,
) -> dict[str, Any]:
    blocker = {
        "blocker_id": "foreign-owner-blocker",
        "reporting_node": "experiment",
        **_OLD_REPORTED_BLOCKER_EVIDENCE,
    }
    if suggested_owner is not _MISSING:
        blocker["suggested_owner"] = suggested_owner
    state.hook_state["blockers"] = [blocker]
    return asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="record an honest blocked operation",
        outcome="blocked",
        blocker_id="foreign-owner-blocker",
        checks=[],
        next_step="ask the assigned owner to provide the typed contract",
    ))


def _reported_check(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item for item in checks if item.get("name") == "reported_blocker")


def _completion_surfaces(
    state: State,
    completion: dict[str, Any],
) -> dict[str, Any]:
    raw = state.read_artifact(completion["raw_results_artifact_id"])
    raw_manifest = json.loads(raw["content"])
    receipt_path = next(
        item["path"]
        for item in raw_manifest["files"]
        if item["role"] == "operation_verification_receipt"
    )
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    clean = json.loads(state.read_artifact(
        completion["clean_results_artifact_id"],
    )["content"])
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    return {
        "raw": _reported_check(receipt["checks"]),
        "clean": _reported_check(clean["verification"]["checks"]),
        "log": log["content"],
        "records": (raw, state.read_artifact(
            completion["clean_results_artifact_id"],
        ), log),
    }


def test_normalized_suggested_owner_is_projected_to_raw_clean_and_log(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classify_operation(state)
    completion = _complete_blocked_operation(state, suggested_owner="  core  ")
    surfaces = _completion_surfaces(state, completion)
    expected = {**_OLD_REPORTED_BLOCKER_EVIDENCE, "suggested_owner": "core"}

    assert completion["status"] == "success", completion
    assert surfaces["raw"] == {
        "name": "reported_blocker", "passed": False, "evidence": expected,
    }
    assert surfaces["clean"] == surfaces["raw"]
    assert '"suggested_owner": "core"' in surfaces["log"]
    assert all(record["metadata"]["frozen"] is True for record in surfaces["records"])


@pytest.mark.parametrize(
    "suggested_owner",
    [_MISSING, None, "", "   "],
    ids=["missing", "none", "empty", "whitespace"],
)
def test_absent_suggested_owner_preserves_the_old_receipt_shape_and_closure(
    tmp_path: Path,
    suggested_owner: object,
):
    state = State.new("experiment", tmp_path)
    _classify_operation(state)
    completion = _complete_blocked_operation(
        state,
        suggested_owner=suggested_owner,
    )
    surfaces = _completion_surfaces(state, completion)
    expected = {
        "name": "reported_blocker",
        "passed": False,
        "evidence": _OLD_REPORTED_BLOCKER_EVIDENCE,
    }

    assert completion["status"] == "success", completion
    assert surfaces["raw"] == expected
    assert surfaces["clean"] == expected
    assert "suggested_owner" not in surfaces["log"]
    assert all(record["metadata"]["frozen"] is True for record in surfaces["records"])


def test_existing_frozen_ownerless_closure_is_not_rewritten(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classify_operation(state)
    first = _complete_blocked_operation(state)
    before = {
        key: copy.deepcopy(state.read_artifact(first[key]))
        for key in (
            "raw_results_artifact_id",
            "clean_results_artifact_id",
            "experiment_log_artifact_id",
        )
    }

    second = _complete_blocked_operation(state, suggested_owner="core")

    assert second["status"] == "success", second
    assert second["idempotent"] is True
    for key, record in before.items():
        assert state.read_artifact(first[key]) == record
    assert "suggested_owner" not in _completion_surfaces(state, first)["log"]
