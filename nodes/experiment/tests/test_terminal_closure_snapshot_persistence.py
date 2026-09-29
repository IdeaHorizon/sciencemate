"""Terminal closure has one durable meaning even when event writes fail."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools.contract_audit import TERMINAL_CLOSURE_REGISTRY

_SNAPSHOT_EVENT = "experiment_terminal_closure_snapshot"
_PERSISTENCE_EVENT = "experiment_terminal_closure_persistence_audit"


def _state(tmp_path: Path, name: str) -> State:
    return State.new("experiment", tmp_path / name)


def _events(state: State) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _passing_audit() -> dict[str, dict[str, Any]]:
    audit = {
        key: {
            "passed": True,
            "applicable": True,
            "status": "passed",
            "reason": f"{key} passed",
        }
        for key in TERMINAL_CLOSURE_REGISTRY
    }
    audit["verdict"]["late_declaration"] = False
    audit["sediment"]["late_declaration"] = False
    audit["execution_record"] = {
        "passed": True,
        "required": False,
        "reason": "optional KB registration is not a terminal gate",
    }
    audit["terminal_failure_record"] = {
        "passed": True,
        "applicable": False,
        "reason": "no terminal failure record is required",
    }
    return audit


def _run_on_end(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    audit: dict[str, dict[str, Any]] | Exception,
) -> SimpleNamespace:
    monkeypatch.setattr(hooks, "_is_operational_run", lambda _state: False)
    if isinstance(audit, Exception):
        def raise_audit_error(_state: State) -> dict[str, dict[str, Any]]:
            raise audit

        monkeypatch.setattr(
            hooks, "audit_experiment_contract", raise_audit_error,
        )
    else:
        monkeypatch.setattr(
            hooks, "audit_experiment_contract", lambda _state: audit,
        )
    result = SimpleNamespace(status="completed", final_text="model completion")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), result,
    )
    return result


def _named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == name]


def _assert_snapshot_digest(snapshot: dict[str, Any]) -> None:
    body_keys = {
        "snapshot_schema_version",
        "source",
        "terminal_closure_registry",
        "audit_checks",
        "failed_audit_keys",
        "failed_event_keys",
    }
    if "failure_reason" in snapshot:
        body_keys.add("failure_reason")
    body = {key: snapshot[key] for key in body_keys}
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    assert snapshot["snapshot_sha256"] == digest
    assert snapshot["snapshot_id"] == f"sha256:{digest}"


def test_auxiliary_write_failure_does_not_rewrite_passing_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "aux-write-failure")
    append_transcript = state.append_transcript

    def fail_kb_event(event_type: str, **payload: Any) -> None:
        if event_type == "experiment_kb_registration_audit":
            raise OSError("simulated auxiliary transcript failure")
        append_transcript(event_type, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_kb_event)
    result = _run_on_end(state, monkeypatch, _passing_audit())
    events = _events(state)

    snapshots = _named(events, _SNAPSHOT_EVENT)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["source"] == "audit"
    assert snapshot["passed"] is True
    _assert_snapshot_digest(snapshot)

    for event_name in TERMINAL_CLOSURE_REGISTRY.values():
        projections = _named(events, event_name)
        assert len(projections) == 1
        assert projections[0]["passed"] is True
        assert (
            projections[0]["terminal_closure_snapshot_id"]
            == snapshot["snapshot_id"]
        )

    persistence = _named(events, _PERSISTENCE_EVENT)
    assert len(persistence) == 1
    assert persistence[0]["passed"] is False
    assert persistence[0]["failed_stage"] == (
        "experiment_kb_registration_audit"
    )
    assert persistence[0]["terminal_closure_snapshot_id"] == snapshot[
        "snapshot_id"
    ]
    assert result.status == "blocked"
    assert state.hook_state["experiment_downstream_blocked"][
        "failed_checks"
    ] == [_PERSISTENCE_EVENT]


def test_gate_write_failure_never_emits_a_second_failed_interpretation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "gate-write-failure")
    append_transcript = state.append_transcript
    failed_event = "experiment_result_evidence_audit"

    def fail_one_gate(event_type: str, **payload: Any) -> None:
        if event_type == failed_event:
            raise OSError("simulated gate transcript failure")
        append_transcript(event_type, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_one_gate)
    result = _run_on_end(state, monkeypatch, _passing_audit())
    events = _events(state)

    snapshots = _named(events, _SNAPSHOT_EVENT)
    assert len(snapshots) == 1
    snapshot_id = snapshots[0]["snapshot_id"]
    assert _named(events, failed_event) == []
    for event_name in TERMINAL_CLOSURE_REGISTRY.values():
        projections = _named(events, event_name)
        assert len(projections) <= 1
        for projection in projections:
            assert projection["passed"] is True
            assert projection["terminal_closure_snapshot_id"] == snapshot_id

    persistence = _named(events, _PERSISTENCE_EVENT)
    assert len(persistence) == 1
    assert persistence[0]["passed"] is False
    assert persistence[0]["snapshot_persisted"] is True
    assert result.status == "blocked"


def test_audit_exception_emits_one_all_failed_snapshot_and_one_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "audit-error")
    result = _run_on_end(
        state,
        monkeypatch,
        RuntimeError("simulated audit computation failure"),
    )
    events = _events(state)

    snapshots = _named(events, _SNAPSHOT_EVENT)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["source"] == "audit_error"
    assert snapshot["passed"] is False
    assert set(snapshot["failed_audit_keys"]) == set(
        TERMINAL_CLOSURE_REGISTRY
    )
    _assert_snapshot_digest(snapshot)

    for event_name in TERMINAL_CLOSURE_REGISTRY.values():
        projections = _named(events, event_name)
        assert len(projections) == 1
        assert projections[0]["passed"] is False
        assert projections[0]["status"] == "audit_error"
        assert (
            projections[0]["terminal_closure_snapshot_id"]
            == snapshot["snapshot_id"]
        )

    assert _named(events, _PERSISTENCE_EVENT) == []
    assert result.status == "blocked"


def test_operation_assignment_audit_write_failure_blocks_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "operation-assignment-persistence")
    append_transcript = state.append_transcript

    def fail_assignment_event(event_type: str, **payload: Any) -> None:
        if event_type == "experiment_prereg_assignment_audit":
            raise OSError("simulated assignment audit write failure")
        append_transcript(event_type, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_assignment_event)
    monkeypatch.setattr(hooks, "_is_operational_run", lambda _state: True)
    monkeypatch.setattr(hooks, "audit_prereg_assignment", lambda _state: {
        "passed": True,
        "applicable": True,
        "status": "pending_operation_compatible",
        "reason": "compatibility lane passed",
    })
    monkeypatch.setattr(hooks, "_audit_operation_log", lambda _state: {
        "passed": True,
        "reason": "operation evidence passed",
    })
    monkeypatch.setattr(
        hooks, "_operation_outcome_receipt", lambda _state, _audit: None,
    )
    result = SimpleNamespace(status="completed", final_text="model completion")

    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), result,
    )

    events = _events(state)
    persistence = _named(events, _PERSISTENCE_EVENT)
    assert len(persistence) == 1
    assert persistence[0]["failed_stage"] == (
        "experiment_prereg_assignment_audit"
    )
    assert len(_named(events, "experiment_operation_audit")) == 1
    assert result.status == "blocked"
    failed = state.hook_state["experiment_downstream_blocked"][
        "failed_checks"
    ]
    assert "experiment_prereg_assignment_audit" in failed
    assert _PERSISTENCE_EVENT in failed
