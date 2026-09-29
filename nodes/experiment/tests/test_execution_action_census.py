from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import execution_route, run_contract, safe_bash


def _state(tmp_path: Path) -> State:
    return State.new("experiment", tmp_path)


def _read_only_decision() -> dict:
    return {
        "tool": "safe_run_bash",
        "decision": "route_not_required",
        "policy": "read_only",
        "read_only": True,
        "dry_run": False,
        "effective_effects": [],
    }


def _dry_run_decision() -> dict:
    return {
        "tool": "submit_job",
        "decision": "route_unavailable",
        "policy": "guarded_unknown_effect",
        "read_only": False,
        "dry_run": True,
        "effective_effects": ["workspace_write"],
    }


def _route_decision() -> dict:
    return {
        "tool": "safe_run_bash",
        "decision": "matched_ready_step",
        "authoritative": True,
        "policy": "guarded_process",
        "read_only": False,
        "dry_run": False,
        "effective_effects": ["process_tree", "workspace_write"],
        "route_ref": {
            "artifact_id": "execution-route",
            "version": 3,
            "content_hash": "a" * 64,
        },
        "route_step_id": "build",
        "step_definition_hash": "b" * 64,
    }


def _route_binding() -> dict:
    return {
        "attempt_id": "route-attempt-1",
        "route_artifact_id": "execution-route",
        "route_version": 3,
        "route_content_hash": "a" * 64,
        "route_step_id": "build",
        "step_definition_hash": "b" * 64,
        "tool": "safe_run_bash",
    }


def _persist_route_binding(state: State, binding: dict | None = None) -> None:
    state.append_transcript("route_step_bound", **(binding or _route_binding()))


def _authorize_route_bindings(
    monkeypatch: pytest.MonkeyPatch,
    *bindings: dict,
) -> None:
    known = {
        (
            binding["route_artifact_id"],
            binding["route_version"],
            binding["route_content_hash"],
            binding["route_step_id"],
            binding["step_definition_hash"],
        )
        for binding in (bindings or (_route_binding(),))
    }
    monkeypatch.setattr(
        census._execution_route,
        "_known_route_step_bindings",
        lambda _state: known,
    )


def _complete_action(
    state: State,
    *,
    action: dict | None = None,
    decision: dict | None = None,
    payload_spawned: bool | None = False,
    job_submitted: bool | None = False,
) -> dict:
    token = census.begin_execution_action(
        state,
        action
        or {
            "tool": "safe_run_bash",
            "program": "test",
            "read_only": True,
        },
        decision or _read_only_decision(),
    )
    assert token["status"] == "success", token
    observed = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=payload_spawned,
        job_submitted=job_submitted,
        proof_source="tool_owned_execution_boundary",
    )
    assert observed["status"] == "success", observed
    terminal = census.finish_execution_action(state, token, result={"status": "success"})
    assert terminal["status"] == "success", terminal
    return token


def _operation_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        census._run_contract,
        "resolve_run_acceptance",
        lambda _state, bind_if_absent=False: {
            "passed": True,
            "receipt": {
                "schema_version": 2,
                "receipt_digest": "d" * 64,
                "prereg_assignment": {"kind": "pending"},
            },
        },
    )
    monkeypatch.setattr(
        census._run_contract,
        "load_execution_mode_view",
        lambda _state: {
            "mode": "operational",
            "classified": True,
            "status": "classified",
            "source": "run_acceptance_receipt",
        },
    )


def _no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: {
            "status": "unavailable",
            "route_state": "unavailable",
            "reason": "route_not_declared",
        },
    )


def _complete_route_snapshot(
    *,
    route_state: str = "complete",
    attempt_id: str = "route-attempt-1",
    route_version: int = 3,
    route_hash: str = "a" * 64,
    definition_hash: str = "b" * 64,
    tool: str = "safe_run_bash",
    identity_resolution: str | None = None,
    outcome: str | None = None,
    external_outcome: str | None = None,
) -> dict:
    step_projection = {
        "state": "verified" if route_state == "complete" else "in_progress",
        "attempt_id": attempt_id,
        "definition_hash": definition_hash,
    }
    if identity_resolution is not None:
        step_projection["identity_resolution"] = identity_resolution
    if outcome is not None:
        step_projection["outcome"] = outcome
    if external_outcome is not None:
        step_projection["external_outcome"] = external_outcome
    return {
        "status": "ready",
        "route_state": route_state,
        "route_ref": {
            "artifact_id": "execution-route",
            "version": route_version,
            "content_hash": route_hash,
        },
        "route": {
            "steps": [
                {
                    "id": "build",
                    "action": {"tool": tool, "program": "make"},
                    "effects": ["process_tree", "workspace_write"],
                }
            ]
        },
        "steps": {"build": step_projection},
        "transcript_warnings": [],
    }


def test_read_only_action_is_a_complete_route_exempt_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    _complete_action(state)

    reduced = census.reduce_execution_action_census(state)
    assert reduced["status"] == "ready", reduced
    assert reduced["complete"] is True
    assert reduced["action_count"] == 1
    assert reduced["actions"][0]["compatibility_kind"] == "read_only"

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    assert audit["compatibility_lane"] == "route_exempt_non_executing_diagnostic"


def test_dry_run_requires_authoritative_no_spawn_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    _complete_action(
        state,
        action={"tool": "submit_job", "program": "python", "dry_run": True},
        decision=_dry_run_decision(),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    assert audit["compatibility_lane"] == "route_exempt_non_executing_diagnostic"
    assert audit["real_execution_obligation_satisfied"] is False


@pytest.mark.parametrize(
    ("payload_spawned", "job_submitted"),
    [(True, False), (False, True), (None, False), (False, None)],
)
def test_dry_run_never_credits_unknown_or_actual_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_spawned: bool | None,
    job_submitted: bool | None,
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    _complete_action(
        state,
        action={"tool": "submit_job", "program": "python", "dry_run": True},
        decision=_dry_run_decision(),
        payload_spawned=payload_spawned,
        job_submitted=job_submitted,
    )
    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "dry_run_spawn_not_proven_absent" in audit["failure_reasons"]


def test_dry_run_false_is_not_route_exempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    _complete_action(
        state,
        action={"tool": "submit_job", "program": "python", "dry_run": False},
        decision={**_dry_run_decision(), "dry_run": False},
        payload_spawned=True,
        job_submitted=True,
    )
    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "unsupported_effectful_action" in audit["failure_reasons"]


@pytest.mark.parametrize("missing", ["spawn", "terminal"])
def test_missing_phase_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test"},
        _read_only_decision(),
    )
    if missing != "spawn":
        census.observe_execution_action_spawn(
            state,
            token,
            payload_spawned=False,
            job_submitted=False,
            proof_source="tool_owned_execution_boundary",
        )
    if missing != "terminal":
        census.finish_execution_action(state, token, result={"status": "success"})

    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is False, reduced
    assert reduced["status"] == "invalid"
    assert census.pending_operation_compatibility(state)["passed"] is False


def test_duplicate_and_orphan_events_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    token = _complete_action(state)
    state.append_transcript(
        census.TERMINAL_EVENT,
        schema_version=census.CENSUS_SCHEMA_VERSION,
        run_id=state.run_id,
        action_id=token["action_id"],
        terminal_status="success",
        result_status="success",
        error_type=None,
    )
    state.append_transcript(
        census.SPAWN_EVENT,
        schema_version=census.CENSUS_SCHEMA_VERSION,
        run_id=state.run_id,
        action_id="action-" + "f" * 32,
        admission_digest="0" * 64,
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )

    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is False, reduced
    assert "duplicate_terminal" in reduced["error_codes"]
    assert "orphan_spawn" in reduced["error_codes"]


@pytest.mark.parametrize(
    ("terminal_status", "result_status", "error_type"),
    [
        ("success", "error", None),
        ("success", None, None),
        ("error", "success", None),
        ("error", None, None),
        ("cancelled", "failed", None),
        ("unknown", "success", None),
    ],
)
def test_contradictory_terminal_event_fails_closed(
    tmp_path: Path,
    terminal_status: str,
    result_status: str | None,
    error_type: str | None,
) -> None:
    state = _state(tmp_path)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    state.append_transcript(
        census.TERMINAL_EVENT,
        schema_version=census.CENSUS_SCHEMA_VERSION,
        run_id=state.run_id,
        action_id=token["action_id"],
        terminal_status=terminal_status,
        result_status=result_status,
        error_type=error_type,
    )

    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is False
    assert reduced["actions"][0]["errors"] == ["invalid_terminal"]
    assert "invalid_terminal" in reduced["error_codes"]


def test_lost_response_retries_are_idempotent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    first_spawn = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    repeated_spawn = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    first_terminal = census.finish_execution_action(state, token, result={"status": "success"})
    repeated_terminal = census.finish_execution_action(state, token, result={"status": "success"})

    assert first_spawn["status"] == first_terminal["status"] == "success"
    assert repeated_spawn["idempotent"] is True
    assert repeated_terminal["idempotent"] is True
    assert census.reduce_execution_action_census(state)["complete"] is True


def test_conflicting_retries_are_rejected_without_poisoning_history(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    conflict = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    terminal_conflict = census.finish_execution_action(state, token, result={"status": "failed"})

    assert conflict["error_code"] == "execution_action_spawn_observation_conflict"
    assert terminal_conflict["error_code"] == "execution_action_terminal_conflict"
    assert census.reduce_execution_action_census(state)["complete"] is True


def test_missing_followup_phase_returns_audit_only_recovery_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    original_append = state.append_transcript
    fail_on = {"event": census.SPAWN_EVENT}

    def fail_before_write(event: str, **payload: object) -> None:
        if fail_on["event"] == event:
            fail_on["event"] = ""
            raise OSError("transcript temporarily unavailable")
        original_append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_before_write)
    missing_spawn = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    assert missing_spawn["status"] == "error"
    assert missing_spawn["action_token"]["action_id"] == token["action_id"]
    assert missing_spawn["next_action"]["action"] == "retry_missing_census_phase_only"
    assert missing_spawn["next_action"]["owner"] == "experiment_runtime"
    assert missing_spawn["next_action"]["model_callable"] is False
    assert missing_spawn["do_not_retry_payload"] is True
    recovered_spawn = census.observe_execution_action_spawn(
        state,
        missing_spawn["action_token"],
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    assert recovered_spawn["status"] == "success"
    conflicting_spawn = census.observe_execution_action_spawn(
        state,
        missing_spawn["action_token"],
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    assert conflicting_spawn["error_code"] == "execution_action_spawn_observation_conflict"

    fail_on["event"] = census.TERMINAL_EVENT
    missing_terminal = census.finish_execution_action(state, token, result={"status": "success"})
    assert missing_terminal["action_token"]["action_id"] == token["action_id"]
    assert missing_terminal["missing_phase"] == "terminal"
    recovered_terminal = census.finish_execution_action(
        state,
        missing_terminal["action_token"],
        result={"status": "success"},
    )
    assert recovered_terminal["status"] == "success"
    conflicting_terminal = census.finish_execution_action(
        state,
        missing_terminal["action_token"],
        result={"status": "error"},
    )
    assert conflicting_terminal["error_code"] == "execution_action_terminal_conflict"
    assert census.reduce_execution_action_census(state)["complete"] is True


def test_settle_facade_never_writes_terminal_before_spawn_and_can_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    original_append = state.append_transcript
    fail_once = {"spawn": True}

    def transient_spawn_failure(event: str, **payload: object) -> None:
        if event == census.SPAWN_EVENT and fail_once["spawn"]:
            fail_once["spawn"] = False
            raise OSError("transcript temporarily unavailable")
        original_append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", transient_spawn_failure)
    first = census.settle_execution_action(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
        result={"status": "success"},
    )

    assert first["passed"] is False
    assert first["spawn"]["missing_phase"] == "spawn_observation"
    assert first["terminal"]["status"] == "deferred"
    assert first["terminal"]["phase_facts"]["terminal_status"] == "success"
    phase_events = [
        json.loads(line).get("event")
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    assert census.SPAWN_EVENT not in phase_events
    assert census.TERMINAL_EVENT not in phase_events

    recovered = census.settle_execution_action(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
        result={"status": "success"},
    )
    assert recovered["passed"] is True
    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is True
    assert reduced["error_codes"] == []
    ordered = [
        json.loads(line)["event"]
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") in {
            census.ADMITTED_EVENT,
            census.SPAWN_EVENT,
            census.TERMINAL_EVENT,
        }
    ]
    assert ordered == [census.ADMITTED_EVENT, census.SPAWN_EVENT, census.TERMINAL_EVENT]


def test_corrupt_jsonl_fails_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _complete_action(state)
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write("{not-json}\n")

    reduced = census.reduce_execution_action_census(state)
    assert reduced["status"] == "invalid"
    assert reduced["complete"] is False
    assert "transcript_corrupt" in reduced["error_codes"]


def test_scientific_effect_never_enters_compatibility_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    decision = {
        **_dry_run_decision(),
        "effective_effects": ["scientific_execution"],
    }
    _complete_action(
        state,
        action={"tool": "submit_job", "program": "python", "dry_run": True},
        decision=decision,
    )
    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "scientific_execution_observed" in audit["failure_reasons"]


def test_complete_mechanical_route_is_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    decision = _route_decision()
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        decision,
        route_binding=_route_binding(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    assert audit["compatibility_lane"] == "route_backed_mechanical"


def test_incomplete_route_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    decision = _route_decision()
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        decision,
        route_binding=_route_binding(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(route_state="in_progress"),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "route_not_complete" in audit["failure_reasons"]


def test_route_backed_admission_requires_persisted_attempt_binding(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    rejected = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        _route_decision(),
    )
    assert rejected["status"] == "error"
    assert rejected["error_code"] == "execution_action_route_binding_invalid"
    assert rejected["binding_error"] == "route_binding_required"
    assert not state.transcript_path.exists()


def test_route_backed_admission_recovers_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    action = {
        "tool": "safe_run_bash",
        "program": "make",
        "route_step_id": "build",
    }
    first = census.begin_execution_action(
        state, action, _route_decision(), route_binding=_route_binding()
    )
    recovered = census.begin_execution_action(
        state, action, _route_decision(), route_binding=_route_binding()
    )
    assert recovered["status"] == "success"
    assert recovered["idempotent"] is True
    assert recovered["action_id"] == first["action_id"]
    admitted = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == census.ADMITTED_EVENT
    ]
    assert len(admitted) == 1


def test_pending_operation_admission_blocks_unrouted_effects_and_science(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(
        census._run_contract,
        "resolve_run_acceptance",
        lambda _state, bind_if_absent=False: {
            "passed": True,
            "receipt": {
                "schema_version": 2,
                "prereg_assignment": {"kind": "pending"},
            },
        },
    )
    monkeypatch.setattr(
        census._run_contract,
        "load_execution_mode_view",
        lambda _state: {
            "mode": "operational",
            "classified": True,
            "status": "classified",
        },
    )

    assert (
        census.pending_operation_action_block(
            state,
            {"tool": "safe_run_bash", "read_only": True},
            _read_only_decision(),
        )
        is None
    )
    assert (
        census.pending_operation_action_block(
            state,
            {"tool": "submit_job", "read_only": True},
            {**_read_only_decision(), "tool": "submit_job"},
        )
        is None
    )
    # P0a v2（裁决 (a)，17 号复审）：support tool（safe_write_file / fetch_resource /
    # dispatch_data_request）带自己的权威收据，在 pending lane 里**放行并入账**
    # （census kind=receipted），不再前置拒。v1 在这里断言它被拒——那正是复审
    # 实测出"fetch/dispatch 被拒而 safe_write_file 既不经此门也不入账"的来源。
    supported = census.pending_operation_action_block(
        state,
        {"tool": "safe_write_file"},
        {
            "decision": "route_not_required",
            "policy": "guarded_reversible_write",
            "read_only": False,
            "dry_run": False,
            "effective_effects": ["workspace_write"],
        },
    )
    assert supported is None
    # 没有路线、也不是 support tool 的 effectful 执行动作仍然被拒：这一半没变。
    effectful = census.pending_operation_action_block(
        state,
        {"tool": "safe_run_bash", "read_only": False, "dry_run": False},
        {
            "tool": "safe_run_bash",
            "decision": "route_not_required",
            "policy": "guarded_reversible_write",
            "read_only": False,
            "dry_run": False,
            "effective_effects": ["workspace_write"],
        },
    )
    assert effectful["error_code"] == "prereg_pending_operation_action_ineligible"
    assert effectful["reason"] == "route_exempt_effectful_action_not_compatible"
    scientific = census.pending_operation_action_block(
        state,
        {"tool": "submit_job", "dry_run": False},
        {
            **_route_decision(),
            "effective_effects": ["scientific_execution"],
        },
    )
    assert scientific["reason"] == "scientific_execution_requires_exact_prereg_assignment"
    unknown = census.pending_operation_action_block(
        state,
        {"tool": "unknown_executor"},
        _read_only_decision(),
    )
    assert unknown["reason"] == "unknown_execution_tool"


def test_pending_admission_does_not_fail_open_when_authority_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(
        census._run_contract,
        "resolve_run_acceptance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )
    blocked = census.pending_operation_action_block(
        state,
        {"tool": "safe_write_file"},
        {
            "decision": "route_not_required",
            "policy": "guarded_reversible_write",
            "effective_effects": ["workspace_write"],
        },
    )
    assert blocked["status"] == "error"
    assert blocked["reason"] == "prereg_assignment_authority_unavailable"


def test_missing_assignment_authority_allows_only_exact_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(
        census._run_contract,
        "resolve_run_acceptance",
        lambda *_args, **_kwargs: {
            "passed": False,
            "status": "run_authority_receipt_missing",
        },
    )
    monkeypatch.setattr(
        census._run_contract,
        "load_execution_mode_view",
        lambda _state: {"status": "unclassified", "classified": False, "mode": None},
    )
    assert (
        census.pending_operation_action_block(
            state,
            {"tool": "safe_run_bash", "read_only": True},
            _read_only_decision(),
        )
        is None
    )
    blocked = census.pending_operation_action_block(
        state,
        {"tool": "safe_run_bash", "read_only": False},
        {
            **_read_only_decision(),
            "read_only": False,
            "policy": "guarded_process",
            "effective_effects": ["process_tree"],
        },
    )
    assert blocked["reason"] == "prereg_assignment_authority_unavailable"
    assert blocked["next_action"]["owner"] == "dispatching_parent"
    dry_run = census.pending_operation_action_block(
        state,
        {"tool": "submit_job", "dry_run": True},
        {**_dry_run_decision(), "decision": "matched_ready_step"},
    )
    assert dry_run["reason"] == "prereg_assignment_authority_unavailable"


def test_route_guard_failure_blocks_effectful_action_when_authority_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(
        census,
        "pending_operation_action_block",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("guard unavailable")
        ),
    )
    monkeypatch.setattr(
        run_contract,
        "resolve_run_acceptance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("authority unavailable")
        ),
    )
    monkeypatch.setattr(
        execution_route, "execution_route_block", lambda *_args, **_kwargs: None,
    )

    blocked = execution_route.enforce_execution_route(
        state,
        {"tool": "safe_run_bash", "read_only": False},
        _route_decision(),
    )
    assert blocked["status"] == "error"
    assert blocked.get("error_code") == (
        "execution_action_census_guard_unavailable"
    ), blocked
    assert blocked["blocker"]["node_action"] == (
        "repair_action_census_guard_before_retry"
    )

    assert execution_route.enforce_execution_route(
        state,
        {"tool": "safe_run_bash", "read_only": True},
        _read_only_decision(),
    ) is None


def test_pending_guard_never_masks_an_authoritative_route_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    assert (
        census.pending_operation_action_block(
            state,
            {"tool": "safe_run_bash", "read_only": False},
            {
                **_route_decision(),
                "decision": "route_step_not_ready",
                "reason": "expected_outputs_missing",
            },
        )
        is None
    )


def test_unknown_census_event_and_non_object_line_fail_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.append_transcript(
        "execution_action_future_phase",
        schema_version=1,
        run_id=state.run_id,
    )
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(["not", "an", "event"]) + "\n")
    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is False
    assert "unknown_census_event" in reduced["error_codes"]
    assert "transcript_corrupt" in reduced["error_codes"]


def test_dry_run_matched_route_remains_exempt_and_does_not_consume_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _no_route(monkeypatch)
    decision = {
        **_dry_run_decision(),
        "decision": "matched_ready_step",
        "authoritative": True,
        "route_ref": _route_decision()["route_ref"],
        "route_step_id": "build",
        "step_definition_hash": "b" * 64,
    }
    token = census.begin_execution_action(
        state,
        {"tool": "submit_job", "program": "python", "dry_run": True},
        decision,
    )
    assert token["status"] == "success", token
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="submit_job.dry_run",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    assert audit["compatibility_lane"] == "route_exempt_non_executing_diagnostic"

    rejected = census.begin_execution_action(
        state,
        {"tool": "submit_job", "program": "python", "dry_run": True},
        decision,
        route_binding=_route_binding(),
    )
    assert rejected["binding_error"] == "unexpected_route_binding"


def test_exact_output_correction_reuses_old_census_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        _route_decision(),
        route_binding=_route_binding(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    corrected = _complete_route_snapshot(
        route_version=4,
        route_hash="c" * 64,
        definition_hash="d" * 64,
    )
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: corrected,
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    original_ref = {
        "artifact_id": _route_binding()["route_artifact_id"],
        "version": _route_binding()["route_version"],
        "content_hash": _route_binding()["route_content_hash"],
    }
    assert corrected["route_ref"] != original_ref
    assert (
        corrected["steps"]["build"]["definition_hash"] != (_route_binding()["step_definition_hash"])
    )


def test_historical_failed_attempt_does_not_poison_active_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    first = _route_binding()
    second = {**first, "attempt_id": "route-attempt-2"}
    _authorize_route_bindings(monkeypatch, first, second)
    for binding, status in ((first, "error"), (second, "success")):
        _persist_route_binding(state, binding)
        token = census.begin_execution_action(
            state,
            {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
            _route_decision(),
            route_binding=binding,
        )
        census.observe_execution_action_spawn(
            state,
            token,
            payload_spawned=True,
            job_submitted=False,
            proof_source="tool_owned_execution_boundary",
        )
        census.finish_execution_action(state, token, result={"status": status})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(attempt_id="route-attempt-2"),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit


def test_read_only_diagnosis_can_precede_route_backed_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _complete_action(state)
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        _route_decision(),
        route_binding=_route_binding(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=True,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit
    assert audit["compatibility_lane"] == "route_backed_mechanical"


@pytest.mark.parametrize("snapshot_mode", ["raises", "invalid"])
def test_route_exempt_fails_closed_when_route_authority_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_mode: str,
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _complete_action(state)
    if snapshot_mode == "raises":

        def unreadable(_state: State) -> dict:
            raise OSError("route store unavailable")

        replacement = unreadable
    else:

        def invalid(_state: State) -> dict:
            return {"status": "unavailable", "reason": "artifact_read_error"}

        replacement = invalid
    monkeypatch.setattr(census._execution_route, "build_route_snapshot", replacement)

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "route_snapshot_unavailable" in audit["failure_reasons"]


def test_active_safe_bash_success_requires_positive_spawn_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    _authorize_route_bindings(monkeypatch)
    _persist_route_binding(state)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "make", "route_step_id": "build"},
        _route_decision(),
        route_binding=_route_binding(),
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="spawn_and_wait.status",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is False, audit
    assert "route_active_payload_not_spawned" in audit["failure_reasons"]


def test_remote_submit_acceptance_allows_unknown_local_payload_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    binding = {**_route_binding(), "tool": "submit_job"}
    decision = {**_route_decision(), "tool": "submit_job", "policy": "managed_external_job"}
    _authorize_route_bindings(monkeypatch, binding)
    _persist_route_binding(state, binding)
    token = census.begin_execution_action(
        state,
        {"tool": "submit_job", "program": "sbatch", "route_step_id": "build"},
        decision,
        route_binding=binding,
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=None,
        job_submitted=True,
        proof_source="submit_sync.scheduler_accepted_identity",
    )
    census.finish_execution_action(state, token, result={"status": "success"})
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(tool="submit_job"),
    )

    audit = census.pending_operation_compatibility(state)
    assert audit["passed"] is True, audit


@pytest.mark.parametrize(
    ("job_submitted", "result", "error"),
    [
        (True, {"status": "accepted_identity_unresolved"}, None),
        (None, {"status": "submission_outcome_unknown"}, None),
        (None, None, RuntimeError("submit response was lost")),
    ],
)
def test_exact_verified_route_recovery_converges_unknown_submit_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_submitted: bool | None,
    result: dict | None,
    error: BaseException | None,
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    binding = {**_route_binding(), "tool": "submit_job"}
    decision = {**_route_decision(), "tool": "submit_job", "policy": "managed_external_job"}
    _authorize_route_bindings(monkeypatch, binding)
    _persist_route_binding(state, binding)
    token = census.begin_execution_action(
        state,
        {"tool": "submit_job", "program": "sbatch", "route_step_id": "build"},
        decision,
        route_binding=binding,
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=None,
        job_submitted=job_submitted,
        proof_source="submit_sync.scheduler_acceptance",
    )
    census.finish_execution_action(state, token, result=result, error=error)
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(
            tool="submit_job",
            identity_resolution="exact",
            outcome="submitted",
            external_outcome="success",
        ),
    )

    obligation = census.operation_execution_obligation(state)

    assert obligation["passed"] is True, obligation
    assert obligation["real_execution_obligation_satisfied"] is True
    assert obligation["action_census"]["actions"][0]["terminal_status"] in {
        "unknown",
        "error",
    }


@pytest.mark.parametrize(
    ("job_submitted", "snapshot_overrides"),
    [
        (False, {}),
        (None, {"identity_resolution": None}),
        (None, {"external_outcome": "failed"}),
        (None, {"attempt_id": "route-attempt-other"}),
        (None, {"route_state": "in_progress"}),
    ],
)
def test_route_recovery_does_not_mask_unproven_or_negative_submit_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_submitted: bool | None,
    snapshot_overrides: dict,
) -> None:
    state = _state(tmp_path)
    _operation_context(monkeypatch)
    binding = {**_route_binding(), "tool": "submit_job"}
    decision = {**_route_decision(), "tool": "submit_job", "policy": "managed_external_job"}
    _authorize_route_bindings(monkeypatch, binding)
    _persist_route_binding(state, binding)
    token = census.begin_execution_action(
        state,
        {"tool": "submit_job", "program": "sbatch", "route_step_id": "build"},
        decision,
        route_binding=binding,
    )
    census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=None,
        job_submitted=job_submitted,
        proof_source="submit_sync.scheduler_acceptance",
    )
    census.finish_execution_action(
        state,
        token,
        result={"status": "submission_outcome_unknown"},
    )
    snapshot = {
        "tool": "submit_job",
        "identity_resolution": "exact",
        "outcome": "submitted",
        "external_outcome": "success",
        **snapshot_overrides,
    }
    monkeypatch.setattr(
        census._execution_route,
        "build_route_snapshot",
        lambda _state: _complete_route_snapshot(**snapshot),
    )

    obligation = census.operation_execution_obligation(state)

    assert obligation["passed"] is False, obligation
    assert obligation["real_execution_obligation_satisfied"] is False


def test_write_then_raise_is_recovered_without_duplicate_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    original_append = state.append_transcript
    fail_on = {"event": census.ADMITTED_EVENT}

    def durable_then_raise(event: str, **payload: object) -> None:
        original_append(event, **payload)
        if fail_on["event"] == event:
            fail_on["event"] = ""
            raise OSError("response lost after durable append")

    monkeypatch.setattr(state, "append_transcript", durable_then_raise)
    token = census.begin_execution_action(
        state,
        {"tool": "safe_run_bash", "program": "test", "read_only": True},
        _read_only_decision(),
    )
    assert token["status"] == "success" and token["idempotent"] is True
    fail_on["event"] = census.SPAWN_EVENT
    observed = census.observe_execution_action_spawn(
        state,
        token,
        payload_spawned=False,
        job_submitted=False,
        proof_source="tool_owned_execution_boundary",
    )
    assert observed["status"] == "success" and observed["idempotent"] is True
    fail_on["event"] = census.TERMINAL_EVENT
    terminal = census.finish_execution_action(state, token, result={"status": "success"})
    assert terminal["status"] == "success" and terminal["idempotent"] is True
    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is True, reduced
    assert reduced["action_count"] == 1


def _prepare_safe_bash_boundary(
    state: State,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import project_workspace

    monkeypatch.setattr(
        safe_bash,
        "resolve_required_workdir",
        lambda *_args, **_kwargs: (str(tmp_path), None),
    )
    monkeypatch.setattr(
        project_workspace,
        "validate_tool_cwd",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(safe_bash, "_frozen_attempt_capability_gap", lambda _state: None)
    monkeypatch.setattr(safe_bash, "_ensure_hardened_attempt_manifest", lambda _state: None)


def test_safe_bash_admission_persistence_failure_never_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _prepare_safe_bash_boundary(state, tmp_path, monkeypatch)
    original_append = state.append_transcript

    def fail_admission(event: str, **payload: object) -> None:
        if event == census.ADMITTED_EVENT:
            raise OSError("transcript unavailable")
        original_append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_admission)
    spawned: list[bool] = []

    async def must_not_spawn(*_args: object, **_kwargs: object) -> tuple:
        spawned.append(True)
        raise AssertionError("payload must not start")

    monkeypatch.setattr(safe_bash, "spawn_and_wait", must_not_spawn)
    result = asyncio.run(
        safe_bash._exec_and_log(
            state,
            "printf test",
            cwd=str(tmp_path),
            sandbox_roots=([tmp_path], []),
            execution_action={"tool": "safe_run_bash", "program": "printf"},
            execution_decision=_read_only_decision(),
        )
    )
    assert spawned == []
    assert result["execution_action_phase"] == "admission"
    assert result["payload_spawned"] is False


def test_safe_bash_spawn_failed_records_no_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _prepare_safe_bash_boundary(state, tmp_path, monkeypatch)

    async def spawn_failed(*_args: object, **_kwargs: object) -> tuple:
        return "spawn_failed", 127, b"", b"spawn rejected"

    monkeypatch.setattr(safe_bash, "spawn_and_wait", spawn_failed)
    result = asyncio.run(
        safe_bash._exec_and_log(
            state,
            "printf test",
            cwd=str(tmp_path),
            sandbox_roots=([tmp_path], []),
            execution_action={"tool": "safe_run_bash", "program": "printf"},
            execution_decision=_read_only_decision(),
        )
    )
    action = census.reduce_execution_action_census(state)["actions"][0]
    assert result["status"] == "error"
    assert action["payload_spawned"] is False
    assert action["job_submitted"] is False
    assert action["terminal_status"] == "error"


def test_safe_bash_spawn_exception_records_unknown_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _prepare_safe_bash_boundary(state, tmp_path, monkeypatch)

    async def spawn_raises(*_args: object, **_kwargs: object) -> tuple:
        raise RuntimeError("spawn boundary failed")

    monkeypatch.setattr(safe_bash, "spawn_and_wait", spawn_raises)
    result = asyncio.run(
        safe_bash._exec_and_log(
            state,
            "printf test",
            cwd=str(tmp_path),
            sandbox_roots=([tmp_path], []),
            execution_action={"tool": "safe_run_bash", "program": "printf"},
            execution_decision=_read_only_decision(),
        )
    )
    action = census.reduce_execution_action_census(state)["actions"][0]
    assert result["status"] == "error"
    assert action["payload_spawned"] is None
    assert action["job_submitted"] is False
    assert action["terminal_status"] == "error"
