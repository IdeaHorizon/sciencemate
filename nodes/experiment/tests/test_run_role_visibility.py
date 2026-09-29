"""Visibility regressions for an undeclared Experiment run role.

These tests deliberately pin observability separately from gate outcomes: a
missing role remains a conservative secondary run, but callers must be able to
distinguish that default from an explicitly declared secondary role.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools.contract_audit import (
    audit_scientific_question_closure,
    audit_sediment,
    audit_verdict,
)
from nodes.experiment.tools.preflight import (
    audit_execution_contract,
    audit_experiment_preflight,
)
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    create_run_manifest,
    load_run_contract,
)
from nodes.experiment.tools.resource_manager import _submit_job


_UNDECLARED_WARNING = "run_role_missing_defaulted_to_secondary"
_UNDECLARED_SOURCE = "undeclared_defaulted_secondary"


def _state(tmp_path: Path, *, role: str | None, bound: bool) -> State:
    state = State.new("experiment", tmp_path)
    node_inputs = {
        "experiment_focus": "Run a scientific diagnostic without changing formal closure gates.",
    }
    if not bound:
        # P0a（#1099）：省略 prereg_assignment 是 pending，科学分类会被拒；这些格测的是
        # 未声明 run_role 的可见性，不是认领——按裁决用 typed none + scientific（允许）。
        node_inputs["prereg_assignment"] = {
            "kind": "none",
            "reason": "Role-visibility fixture: no governing preregistration for this run.",
        }
    if bound:
        metadata: dict[str, object] = {
            "execution_mode": "scientific",
            "stage": "simulation",
        }
        if role is not None:
            metadata["run_role"] = role
        saved = state.save_artifact(
            "pre_registration",
            "role_visibility",
            "# Frozen role-visibility preregistration\n",
            metadata=metadata,
        )
        state.mark_frozen(saved["id"])
        node_inputs["prereg_artifact_id"] = saved["id"]
    state.hook_state["node_inputs"] = node_inputs
    return state


def _classify(state: State) -> dict:
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Exercise the scientific role-visibility contract without opening formal gates.",
    ))
    assert result["status"] == "success", result
    return result["classification"]


def _scope_event(state: State) -> dict:
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    return next(
        event for event in reversed(events)
        if event.get("event") == "experiment_scope_classified"
    )


def _save_agent_log(state: State) -> None:
    state.save_artifact(
        "experiment_log",
        "role_visibility_log",
        "## Execution Status\nstatus: diagnostic_complete\n\n"
        "## Credibility\ncredibility: reliable\n",
    )


def _gate_snapshot(state: State) -> dict:
    contract = load_run_contract(state)
    verdict = audit_verdict(state)
    sediment = audit_sediment(state)
    questions = audit_scientific_question_closure(state)
    preflight = audit_experiment_preflight(state)
    execution = audit_execution_contract(
        state, None, stage="simulation", runner="submit_job",
    )
    return {
        "requires_hypothesis_verdict": contract["requires_hypothesis_verdict"],
        "audits": {
            "verdict": [verdict["passed"], verdict["applicable"]],
            "sediment": [sediment["passed"], sediment["applicable"]],
            "scientific_question_closure": [
                questions["passed"], questions["applicable"],
            ],
        },
        "preflight_blocking_reasons": preflight["blocking_reasons"],
        "formal_input_gate": {
            "passed": execution["passed"],
            "applicable": execution["applicable"],
            "blocking_reasons": execution["blocking_reasons"],
        },
    }


def _assert_undeclared_visibility(state: State) -> None:
    before = load_run_contract(state)
    assert before["run_role"] == "secondary"
    assert before["run_role_source"] == _UNDECLARED_SOURCE
    assert _UNDECLARED_WARNING in before["contract_warnings"]

    classified = _classify(state)
    assert classified["run_role"] == "secondary"
    assert classified["run_role_source"] == _UNDECLARED_SOURCE
    assert _UNDECLARED_WARNING in classified["contract_warnings"]

    event = _scope_event(state)
    assert event["run_role"] == "secondary"
    assert event["run_role_source"] == _UNDECLARED_SOURCE
    assert _UNDECLARED_WARNING in event["contract_warnings"]


def test_bound_prereg_without_role_is_loud_in_contract_classification_and_event(
    tmp_path: Path,
) -> None:
    _assert_undeclared_visibility(_state(tmp_path, role=None, bound=True))


def test_unbound_self_reported_scientific_run_echoes_defaulted_role(
    tmp_path: Path,
) -> None:
    _assert_undeclared_visibility(_state(tmp_path, role=None, bound=False))


def test_explicit_secondary_remains_distinct_and_has_no_undeclared_warning(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role="secondary", bound=True)
    contract = load_run_contract(state)
    assert contract["run_role_source"] == "declared_secondary"
    assert _UNDECLARED_WARNING not in contract.get("contract_warnings", [])

    classified = _classify(state)
    assert classified["run_role_source"] == "declared_secondary"
    assert _UNDECLARED_WARNING not in classified.get("contract_warnings", [])

    _save_agent_log(state)
    for result in (
        audit_verdict(state),
        audit_sediment(state),
        audit_scientific_question_closure(state),
        audit_execution_contract(
            state, None, stage="simulation", runner="submit_job",
        ),
    ):
        assert result["not_applicable_reason"]["context"][
            "run_role_source"
        ] == "declared_secondary"


def test_node_input_run_role_cannot_override_missing_frozen_declaration(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role=None, bound=True)
    state.hook_state["node_inputs"]["run_role"] = "primary"

    contract = load_run_contract(state)
    assert contract["run_role"] == "secondary"
    assert contract["run_role_source"] == _UNDECLARED_SOURCE
    assert _UNDECLARED_WARNING in contract["contract_warnings"]

    classified = _classify(state)
    assert classified["run_role"] == "secondary"
    assert classified["run_role_source"] == _UNDECLARED_SOURCE


@pytest.mark.parametrize(
    ("frozen_role", "legacy_role", "expected_role", "expected_source"),
    [
        (
            "secondary", "primary", "primary",
            "legacy_hook_state_declared_primary",
        ),
        (
            "primary", "secondary", "secondary",
            "legacy_hook_state_declared_secondary",
        ),
        (
            "primary", "not-a-role", "secondary",
            "legacy_hook_state_invalid_declared_defaulted_secondary",
        ),
    ],
)
def test_legacy_hook_role_keeps_behavior_but_does_not_impersonate_frozen_source(
    tmp_path: Path,
    frozen_role: str,
    legacy_role: str,
    expected_role: str,
    expected_source: str,
) -> None:
    state = _state(tmp_path, role=frozen_role, bound=True)
    state.hook_state["run_contract"] = {"run_role": legacy_role}

    contract = load_run_contract(state)
    assert contract["run_role"] == expected_role
    assert contract["run_role_source"] == expected_source
    assert contract["requires_hypothesis_verdict"] is (
        expected_role == "primary"
    )
    assert _UNDECLARED_WARNING not in contract.get("contract_warnings", [])

    classified = _classify(state)
    assert classified["run_role"] == expected_role
    assert classified["run_role_source"] == expected_source


@pytest.mark.parametrize("legacy_role", [None, ""])
def test_empty_legacy_hook_role_keeps_behavior_and_exposes_legacy_source(
    tmp_path: Path,
    legacy_role: str | None,
) -> None:
    state = _state(tmp_path, role="primary", bound=True)
    state.hook_state["run_contract"] = {"run_role": legacy_role}

    contract = load_run_contract(state)
    assert contract["run_role"] == "secondary"
    assert contract["run_role_source"] == (
        "legacy_hook_state_undeclared_defaulted_secondary"
    )
    assert contract["requires_hypothesis_verdict"] is False
    assert _UNDECLARED_WARNING in contract["contract_warnings"]

    classified = _classify(state)
    assert classified["run_role"] == "secondary"
    assert classified["run_role_source"] == (
        "legacy_hook_state_undeclared_defaulted_secondary"
    )
    assert _UNDECLARED_WARNING in classified["contract_warnings"]


@pytest.mark.parametrize(
    ("declared_role", "effective_role", "expected_source"),
    [
        ("primary", "primary", "declared_primary"),
        ("not-a-role", "secondary", "invalid_declared_defaulted_secondary"),
    ],
)
def test_declared_and_invalid_roles_have_truthful_distinct_sources(
    tmp_path: Path,
    declared_role: str,
    effective_role: str,
    expected_source: str,
) -> None:
    state = _state(tmp_path, role=declared_role, bound=True)
    contract = load_run_contract(state)
    assert contract["run_role"] == effective_role
    assert contract["run_role_source"] == expected_source
    assert _UNDECLARED_WARNING not in contract.get("contract_warnings", [])

    classified = _classify(state)
    assert classified["run_role"] == effective_role
    assert classified["run_role_source"] == expected_source
    assert _UNDECLARED_WARNING not in classified.get("contract_warnings", [])


def test_unbound_classification_only_adds_the_role_warning_to_its_schema(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role=None, bound=False)
    state.hook_state["run_contract"] = {
        "execution_contract": {"version": "invalid"},
    }
    contract = load_run_contract(state)
    assert set(contract["contract_warnings"]) >= {
        _UNDECLARED_WARNING,
        "execution_contract_invalid",
    }
    classified = _classify(state)
    assert classified["contract_warnings"] == [_UNDECLARED_WARNING]


def test_manifest_records_the_same_derived_role_source(tmp_path: Path) -> None:
    state = _state(tmp_path, role=None, bound=True)
    manifest = create_run_manifest(state, status="running")
    assert manifest["run_role_source"] == _UNDECLARED_SOURCE
    record = state.list_artifacts("run_manifest")[-1]
    saved = state.read_artifact(record["id"])
    assert saved["metadata"]["run_role_source"] == _UNDECLARED_SOURCE


def test_undeclared_role_does_not_change_any_gate_outcome(tmp_path: Path) -> None:
    snapshots = []
    for name, bound in (("bound", True), ("unbound", False)):
        state = _state(tmp_path / name, role=None, bound=bound)
        _classify(state)
        _save_agent_log(state)
        snapshots.append(_gate_snapshot(state))

    expected = {
        "requires_hypothesis_verdict": False,
        "audits": {
            "verdict": [True, False],
            "sediment": [True, False],
            "scientific_question_closure": [True, False],
        },
        "preflight_blocking_reasons": [],
        "formal_input_gate": {
            "passed": True,
            "applicable": False,
            "blocking_reasons": [],
        },
    }
    assert snapshots == [expected, expected]


def test_every_role_controlled_non_applicable_gate_explains_undeclared_role(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role=None, bound=True)
    _classify(state)
    _save_agent_log(state)
    expected_reason = {
        "code": "formal_gate_not_applicable_for_secondary_scientific_run",
        "context": {
            "execution_mode": "scientific",
            "run_role": "secondary",
            "run_role_source": _UNDECLARED_SOURCE,
        },
    }

    verdict = audit_verdict(state)
    sediment = audit_sediment(state)
    question_closure = audit_scientific_question_closure(state)
    parameter_gate = audit_execution_contract(
        state, None, stage="simulation", runner="submit_job",
    )
    for result in (verdict, sediment, question_closure, parameter_gate):
        assert result["not_applicable_reason"] == expected_reason
    assert verdict["passed"] is True
    assert verdict["applicable"] is False
    assert verdict["reason"] == (
        "secondary/non-formal scientific run has no hypothesis-verdict gate"
    )
    assert sediment["passed"] is True
    assert sediment["applicable"] is False
    assert sediment["reason"] == (
        "secondary/non-formal scientific run has no sediment gate"
    )

    preflight = audit_experiment_preflight(state)
    checks = {item["name"]: item for item in preflight["checks"]}
    for name in (
        "pre_registration", "declared_pre_registration",
        "frozen_expected_params",
    ):
        assert checks[name]["blocking"] is False
        assert checks[name]["not_applicable_reason"] == expected_reason
    assert checks["frozen_expected_params"]["passed"] is True
    assert checks["frozen_expected_params"]["reason"] == (
        "non-primary run does not require frozen expected_params"
    )

    submission = asyncio.run(_submit_job(
        state=state,
        command="echo role-visibility-dry-run",
        scheduler="local",
        dry_run=True,
    ))
    assert submission["status"] == "success", submission
    assert submission["formal_input_gate"] == {
        "evaluated": True,
        "required": False,
        "run_role": "secondary",
        "run_role_source": _UNDECLARED_SOURCE,
        "not_applicable_reason": expected_reason,
    }
    gate_event = next(
        event
        for event in reversed([
            json.loads(line)
            for line in state.transcript_path.read_text(
                encoding="utf-8",
            ).splitlines()
        ])
        if event.get("event") == "submit_job_formal_input_gate_evaluated"
    )
    assert {
        key: gate_event[key] for key in submission["formal_input_gate"]
    } == submission["formal_input_gate"]


def test_operational_secondary_does_not_claim_role_caused_non_applicability(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role=None, bound=False)
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Exercise an operational run whose role remains undeclared.",
    ))
    assert result["status"] == "success", result
    contract = load_run_contract(state)
    assert contract["execution_mode"] == "operational"
    assert contract["run_role_source"] == _UNDECLARED_SOURCE
    assert _UNDECLARED_WARNING in contract["contract_warnings"]
    _save_agent_log(state)

    for audit in (
        audit_verdict(state),
        audit_sediment(state),
        audit_scientific_question_closure(state),
        audit_execution_contract(
            state, None, stage="simulation", runner="submit_job",
        ),
    ):
        assert "not_applicable_reason" not in audit

    preflight = audit_experiment_preflight(state)
    assert all(
        "not_applicable_reason" not in check
        for check in preflight["checks"]
    )
    submission = asyncio.run(_submit_job(
        state=state,
        command="echo operational-role-visibility",
        scheduler="local",
        dry_run=True,
    ))
    assert submission["status"] == "success", submission
    assert submission["formal_input_gate"]["required"] is False
    assert "not_applicable_reason" not in submission["formal_input_gate"]


@pytest.mark.parametrize(
    ("role", "expected_role", "expected_source"),
    [
        ("primary", "primary", "durable_primary_witness"),
        ("secondary", "unknown", "run_authority_unknown"),
    ],
)
def test_authority_damage_overrides_declaration_source_without_missing_warning(
    tmp_path: Path,
    role: str,
    expected_role: str,
    expected_source: str,
) -> None:
    state = _state(tmp_path, role=role, bound=True)
    _classify(state)
    state.hook_state.clear()
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"interrupted')
    contract = load_run_contract(state)
    assert contract["run_role"] == expected_role
    assert contract["run_role_source"] == expected_source
    assert _UNDECLARED_WARNING not in contract.get("contract_warnings", [])


def test_old_scope_event_reopens_with_derived_source_and_one_warning(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role=None, bound=True)
    node_inputs = dict(state.hook_state["node_inputs"])
    _classify(state)
    rewritten = []
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "experiment_scope_classified":
            event.pop("run_role_source", None)
            event.pop("contract_warnings", None)
        rewritten.append(json.dumps(event, ensure_ascii=False))
    state.transcript_path.write_text(
        "\n".join(rewritten) + "\n", encoding="utf-8",
    )
    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = node_inputs
    result = asyncio.run(_classify_experiment_scope(
        reopened,
        scope="scientific",
        reason="Reopen a pre-source-field classified run.",
    ))
    assert result["status"] == "success", result
    assert result["idempotent"] is True
    classification = result["classification"]
    assert classification["run_role_source"] == _UNDECLARED_SOURCE
    assert classification["contract_warnings"].count(_UNDECLARED_WARNING) == 1
    contract = load_run_contract(reopened)
    assert contract["contract_warnings"].count(_UNDECLARED_WARNING) == 1


def test_reopen_does_not_trust_forged_scope_role_source_or_stale_warning(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, role="secondary", bound=True)
    node_inputs = dict(state.hook_state["node_inputs"])
    _classify(state)
    rewritten = []
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "experiment_scope_classified":
            event["run_role_source"] = _UNDECLARED_SOURCE
            event["contract_warnings"] = [_UNDECLARED_WARNING]
        rewritten.append(json.dumps(event, ensure_ascii=False))
    state.transcript_path.write_text(
        "\n".join(rewritten) + "\n", encoding="utf-8",
    )
    reopened = State.reopen("experiment", state.root.parent, state.run_id)
    reopened.hook_state["node_inputs"] = node_inputs
    classification = _classify(reopened)
    assert classification["run_role_source"] == "declared_secondary"
    assert _UNDECLARED_WARNING not in classification.get(
        "contract_warnings", [],
    )
    contract = load_run_contract(reopened)
    assert contract["run_role_source"] == "declared_secondary"
    assert _UNDECLARED_WARNING not in contract.get("contract_warnings", [])


def test_harness_does_not_advertise_run_role_as_node_input() -> None:
    harness = Path("nodes/experiment/harness.yaml").read_text(encoding="utf-8")
    expected_inputs = harness.split("expected_inputs:\n", 1)[1].split(
        "\ntask_prose_input_keys:", 1,
    )[0]
    assert "\n  run_role:" not in expected_inputs
    assert "node_inputs/frozen prereg" not in harness
    assert "node_inputs" in harness and "不能直接指定 run_role" in harness
