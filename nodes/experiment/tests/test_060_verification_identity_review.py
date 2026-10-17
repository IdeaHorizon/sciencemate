"""Mechanical identity boundaries between formal requirements and model methods."""
from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from core.state import State
from nodes.experiment.tools import finish, ledger, obligations, run_bootstrap, safe_bash, task_verification


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path):
    state = State.new("experiment", tmp_path / "runs", project_id="identity-review", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Produce the result file successfully.",
        "required_outputs": [{"id": "task_verification_0", "kind": "file",
                              "path": str(state.root / "outputs/experiment/runtime/result.txt"), "non_empty": True}],
    }
    _run(run_bootstrap.establish(state))
    return state


def test_model_method_names_cannot_shadow_a_formal_nonempty_requirement(tmp_path):
    """A passing model check cannot shadow the formal file-content condition."""
    state = _state(tmp_path)
    program = "from pathlib import Path; Path('result.txt').write_text(''); raise SystemExit(7)"
    payload = _run(safe_bash._safe_run_bash(
        state, cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
        cwd=str(state.root / "outputs/experiment/runtime"), timeout=20,
    ))
    assert payload.get("returncode") == 7, payload
    verification = [{"requirement": "Produce the result file successfully.",
                     "checks": [{"kind": "check", "command": "true"}]}]
    proposed = task_verification.prepare(state, verification)
    formal_ids = {row["id"] for row in obligations.frozen(state)}

    result = _run(finish._finish(
        state, outcome="success", summary="The producer failed; this success must be refused.",
        verification=verification,
    ))

    assert result["status"] == "success_unsupported", result
    assert any(row.get("obligation") == "task_verification_0" and row.get("gap") == "empty"
               for row in result.get("gaps", [])), result
    assert formal_ids.isdisjoint(row["id"] for row in proposed["checks"])
    assert not any(row["kind"] == "finish" for row in ledger.open_ledger(state).records())


def test_generated_check_names_are_distinct_from_all_formal_names_and_each_other(tmp_path):
    state = _state(tmp_path)
    proposed = task_verification.prepare(state, [{
        "requirement": "Produce the result file successfully.",
        "checks": [{"kind": "check", "command": "true"},
                   {"kind": "check", "command": "true"}],
    }])
    formal_ids = {row["id"] for row in obligations.frozen(state)}
    names = [row["id"] for row in proposed["checks"]]
    assert len(names) == len(set(names)) == 2
    assert formal_ids.isdisjoint(names)


def test_verification_risk_admission_refuses_signal_before_spawn(tmp_path, monkeypatch):
    state = _state(tmp_path)
    calls = []

    async def must_not_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("A signal is not a read-only verification method")

    monkeypatch.setattr(safe_bash, "_exec_and_log", must_not_spawn)
    result = _run(finish._default_check_runner(state, {
        "id": "risk_probe", "command": "kill -9 $$", "timeout_s": 10,
    }))

    assert result["executed"] is False and result["exit_code"] is None, result
    assert result["error"] and result["risk_category"], result
    assert calls == []
    assert "原授权工具" in result["error"]


@pytest.mark.parametrize("command", [
    "ssh example.invalid true",
    "systemd-run --user --unit=hf-verification-no-launch sleep 1",
])
def test_verification_cannot_be_an_external_control_launch_channel(tmp_path, monkeypatch, command):
    state = _state(tmp_path)
    calls = []

    async def must_not_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("No external control or network call is allowed in this test")

    monkeypatch.setattr(safe_bash, "_exec_and_log", must_not_spawn)
    result = _run(finish._default_check_runner(state, {
        "id": "external_control_probe", "command": command, "timeout_s": 10,
    }))

    assert result["executed"] is False and result["exit_code"] is None, result
    assert result["error"] and result["admission"], result
    assert calls == []


def test_ordinary_python_verification_still_runs_in_the_read_only_sandbox(tmp_path):
    state = _state(tmp_path)
    program = "assert 2 + 2 == 4; print('REAL_CHECK_PASSED')"
    result = _run(finish._default_check_runner(state, {
        "id": "python_check", "timeout_s": 10,
        "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
    }))

    assert result["exit_code"] == 0 and result["executed"] is True, result
    assert "REAL_CHECK_PASSED" in result["stdout_tail"]
    assert result["execution_profile"]["read_only_roots"]


def test_real_check_only_task_counts_executed_check_not_an_unobserved_payload(tmp_path):
    task = "Verify that two plus two equals four."
    state = State.new("experiment", tmp_path / "runs", project_id="check-only", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": task}
    _run(run_bootstrap.establish(state))
    program = "assert 2 + 2 == 4; print('REAL_CHECK_PASSED')"
    result = _run(finish._finish(
        state, outcome="success", summary="The requested arithmetic check was actually executed.",
        verification=[{"requirement": task, "checks": [{
            "kind": "check", "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
            "expect_exit": 0, "expect_stdout_contains": "REAL_CHECK_PASSED",
        }]}],
    ))

    assert result["status"] == "success" and result["outcome"] == "success", result
    assert result["delivery"]["execution_status"] == "attempted"
    rows = ledger.open_ledger(state).records()
    starts = [row for row in rows if row["kind"] == "action_started"]
    assert starts and all(row["tool"] == "finish.check" for row in starts)
    assert any(row["kind"] == "action_settled" and row.get("executed") is True for row in rows)


def test_refused_check_does_not_claim_execution(tmp_path):
    state = _state(tmp_path)
    led = ledger.open_ledger(state, write=True)
    result = _run(finish._check_evidence(state, led, {
        "id": "denied_signal", "kind": "check", "command": "kill -9 $$", "expect_exit": 0,
    }))

    assert result["passed"] is False and result["evidence"]["executed"] is False
    rows = led.records()
    assert finish._execution_status(rows) == "not_run"
    settled = [row for row in rows if row["kind"] == "action_settled"]
    assert len(settled) == 1 and settled[0]["executed"] is False
