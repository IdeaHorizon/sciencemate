from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import execution_route, safe_bash


def _state(tmp_path: Path) -> tuple[State, Path]:
    state = State.new("experiment", tmp_path / "state")
    cwd = tmp_path / "work"
    cwd.mkdir()
    return state, cwd


def _action() -> dict:
    return {
        "tool": "safe_run_bash",
        "program": "printf",
        "read_only": True,
        "dry_run": False,
        "observed_effects": [],
    }


def _decision() -> dict:
    return {
        "tool": "safe_run_bash",
        "decision": "route_not_required",
        "policy": "read_only",
        "read_only": True,
        "dry_run": False,
        "effective_effects": [],
    }


def _route_action() -> dict:
    return {
        "tool": "safe_run_bash",
        "program": "tar",
        "route_step_id": "execute",
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["environment_change", "workspace_write"],
        "workdir_roles": ["run_root"],
        "payload_digest": "d" * 64,
    }


def _events(state: State, kind: str) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    return [
        event
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line)).get("event") == kind
    ]


def _prepare_executor(
    monkeypatch: pytest.MonkeyPatch,
    state: State,
    cwd: Path,
    spawn,
) -> dict:
    monkeypatch.setattr(
        safe_bash,
        "resolve_required_workdir",
        lambda _state, requested, kind="命令": (str(requested or cwd), None),
    )
    monkeypatch.setattr(
        safe_bash, "_frozen_attempt_capability_gap", lambda _state: None)
    monkeypatch.setattr(
        safe_bash, "_ensure_hardened_attempt_manifest", lambda _state: None)
    monkeypatch.setattr(safe_bash, "spawn_and_wait", spawn)
    return {
        "cwd": str(cwd),
        "sandbox_roots": ([cwd], []),
        "execution_action": _action(),
        "execution_decision": _decision(),
    }


def test_admission_persistence_failure_never_calls_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, cwd = _state(tmp_path)
    spawned = False

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("payload must not start")

    kwargs = _prepare_executor(monkeypatch, state, cwd, forbidden_spawn)
    append = state.append_transcript

    def fail_admission(event: str, **payload) -> None:
        if event == census.ADMITTED_EVENT:
            raise OSError("transcript unavailable")
        append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_admission)
    result = asyncio.run(safe_bash._exec_and_log(state, "printf never", **kwargs))

    assert result["status"] == "error"
    assert result["error_code"] == "execution_action_census_persistence_failed"
    assert result["payload_spawned"] is False
    assert spawned is False


@pytest.mark.parametrize(
    ("spawn_status", "returncode", "expected_spawned"),
    [
        ("spawn_failed", 127, False),
        ("done", 0, True),
        ("timeout", 124, True),
        ("cancelled", -15, True),
        ("backend_unknown", 0, None),
    ],
)
def test_spawn_status_records_boundary_owned_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn_status: str,
    returncode: int,
    expected_spawned: bool,
) -> None:
    state, cwd = _state(tmp_path)

    async def spawn(*_args, **_kwargs):
        return spawn_status, returncode, b"payload-out", b"payload-err"

    monkeypatch.setattr(
        safe_bash._te,
        "build_timeout_payload",
        lambda *_args, **_kwargs: {"status": "timeout", "returncode": returncode},
    )
    kwargs = _prepare_executor(monkeypatch, state, cwd, spawn)
    result = asyncio.run(safe_bash._exec_and_log(state, "printf payload", **kwargs))

    assert result["status"] in {"success", "error", "timeout", "cancelled"}
    observations = _events(state, census.SPAWN_EVENT)
    terminals = _events(state, census.TERMINAL_EVENT)
    assert len(observations) == 1
    assert observations[0]["payload_spawned"] is expected_spawned
    assert observations[0]["job_submitted"] is False
    assert observations[0]["proof_source"] == "spawn_and_wait.status"
    assert len(terminals) == 1


def test_spawn_exception_records_unknown_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, cwd = _state(tmp_path)

    async def broken_spawn(*_args, **_kwargs):
        raise RuntimeError("backend disappeared")

    kwargs = _prepare_executor(monkeypatch, state, cwd, broken_spawn)
    result = asyncio.run(safe_bash._exec_and_log(state, "printf payload", **kwargs))

    assert result["status"] == "error"
    observations = _events(state, census.SPAWN_EVENT)
    terminals = _events(state, census.TERMINAL_EVENT)
    assert len(observations) == 1
    assert observations[0]["payload_spawned"] is None
    assert observations[0]["proof_source"] == "spawn_and_wait.raised"
    assert len(terminals) == 1
    assert terminals[0]["terminal_status"] == "error"
    assert terminals[0]["error_type"] == "RuntimeError"


def test_route_backed_action_uses_real_persisted_attempt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, cwd = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route={
            "schema_version": 2,
            "goal": "Execute one route-bound integration step.",
            "evidence_refs": ["test:safe-bash-census-wiring"],
            "steps": [{
                "id": "execute",
                "goal": "Unpack one bounded local fixture.",
                "after": [],
                "action": {"tool": "safe_run_bash", "program": "tar"},
                "effects": ["environment_change", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }],
        },
    ))
    assert declared["status"] == "success", declared
    action = _route_action()
    decision = dict(execution_route.resolve_execution_context(state, action))
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "explicit",
        "resolved_workdir": str(cwd),
    })
    assert decision["decision"] == "matched_ready_step", decision
    binding, binding_error = safe_bash._begin_route_attempt(
        state, decision, tool="safe_run_bash", action=action)
    assert binding_error is None
    assert binding is not None

    async def spawn(*_args, **_kwargs):
        return "done", 0, b"ok", b""

    kwargs = _prepare_executor(monkeypatch, state, cwd, spawn)
    kwargs.update(
        execution_action=action,
        execution_decision=decision,
        execution_route_binding=binding,
    )
    result = asyncio.run(safe_bash._exec_and_log(state, "tar -xf fixture", **kwargs))

    assert result["status"] == "success", result
    admissions = _events(state, census.ADMITTED_EVENT)
    assert len(admissions) == 1
    assert admissions[0]["route_binding"]["route_attempt_id"] == binding["attempt_id"]
    assert admissions[0]["route_binding"]["route_step_id"] == "execute"


@pytest.mark.parametrize("failed_event", [census.SPAWN_EVENT, census.TERMINAL_EVENT])
def test_census_followup_failure_preserves_success_outcome_and_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_event: str,
) -> None:
    state, cwd = _state(tmp_path)

    async def spawn(*_args, **_kwargs):
        return "done", 0, b"payload-ok", b""

    kwargs = _prepare_executor(monkeypatch, state, cwd, spawn)
    append = state.append_transcript

    def fail_census_followup(event: str, **payload) -> None:
        if event == failed_event:
            raise OSError("transcript unavailable")
        append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_census_followup)
    result = asyncio.run(safe_bash._exec_and_log(state, "printf payload", **kwargs))

    assert result["status"] == "error"
    assert result["error_code"] == "execution_action_census_persistence_failed"
    assert result["safe_to_retry"] is False
    assert result["payload_must_not_rerun"] is True
    assert result["do_not_retry_payload"] is True
    outcome = result["execution_outcome"]
    assert outcome["status"] == "success"
    assert outcome["returncode"] == 0
    assert outcome["stdout_tail"] == "payload-ok"
    assert Path(outcome["log_path"]).is_file()
    assert _events(state, census.TERMINAL_EVENT) == []
    reduced = census.reduce_execution_action_census(state)
    assert reduced["complete"] is False
    assert "missing_terminal" in reduced["error_codes"]
    assert "phase_order_invalid" not in reduced["error_codes"]
    if failed_event == census.SPAWN_EVENT:
        assert "missing_spawn" in reduced["error_codes"]
        assert result["deferred_terminal"]["missing_phase"] == "terminal"
        assert result["next_action"]["owner"] == "experiment_runtime"
        assert result["next_action"]["model_callable"] is False
        assert result["model_next_action"]["action"] == (
            "report_blocker_and_end_current_run"
        )
    else:
        assert "missing_spawn" not in reduced["error_codes"]
        assert "deferred_terminal" not in result
