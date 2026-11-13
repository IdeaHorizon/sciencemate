"""Independent adversarial review of the transferred candidate, using real files."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.state import State
from nodes.experiment.tools import finish, ledger, run_bootstrap, capture, action_boundary, delivery_transaction
from nodes.experiment.tests.test_060_output_identity_invalidation import (
    _state, _produce, _success, _run, _invalidations, _unobserved_rewrite,
)


@pytest.mark.parametrize("sealed_before", [False, True])
def test_observed_missing_output_cannot_revive_by_restoring_original_inode(tmp_path, sealed_before):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = capture.identity(target)
    if sealed_before:
        assert _success(state)["status"] == "success"
    parked = target.with_suffix(".parked")
    target.rename(parked)
    failed = _success(state)
    assert failed["status"] == "success_unsupported", failed
    # Physical disappearance was already observed by finish. Moving the exact
    # original inode back must not erase that observation.
    parked.rename(target)
    assert capture.same_identity(original, capture.identity(target))[0]
    retried = _success(state)
    assert retried["status"] == "success_unsupported", retried
    assert _invalidations(state)


def test_before_check_observation_survives_restoration_inside_check(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs", session_id="review-before-window")
    state.hook_state["node_inputs"] = {"experiment_spec": "Create and verify the result file."}
    _run(run_bootstrap.establish(state))
    target = state.root / "outputs/experiment/runtime/result.txt"
    _produce(state, target)
    original = _unobserved_rewrite(target)

    async def restore_during_check(actual_state, obligation):
        os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
        return {"exit_code": 0, "executed": True, "stdout_tail": "OK", "stderr_tail": ""}

    monkeypatch.setattr(finish, "_check_runner", restore_during_check)
    request = {"outcome": "success", "summary": "Checked result", "deliverables": [{"path": str(target)}],
               "verification": [{"requirement": "Verify the result", "checks": [
                   {"kind": "check", "command": "fixture readonly check", "expect_stdout_contains": "OK"}]}]}
    first = _run(finish._finish(state, **request))
    assert first["status"] == "success_unsupported", first
    assert capture.same_identity(original, capture.identity(target))[0]
    second = _run(finish._finish(state, **request))
    assert second["status"] == "success_unsupported", second
    assert _invalidations(state)


def test_recorded_deletion_without_prior_finish_cannot_revive_old_source(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = capture.identity(target)
    deletion = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
                                     command="fixture deletion", output_candidates=[target])
    parked = state.root / "parked"
    target.rename(parked)
    assert action_boundary.settle(state, deletion, outcome="settled", exit_code=0)["status"] == "recorded"
    parked.rename(target)
    assert capture.same_identity(original, capture.identity(target))[0]
    assert _success(state)["status"] == "success_unsupported"
    assert _invalidations(state)


def test_superseded_physical_generation_cannot_revive_by_restoring_old_inode(tmp_path):
    state, target = _state(tmp_path)
    old_producer = _produce(state, target)
    original = capture.identity(target)
    parked = state.root / "parked"
    target.rename(parked)
    new_producer = _produce(state, target, b"50\n")
    assert new_producer != old_producer
    target.unlink()
    parked.rename(target)
    assert capture.same_identity(original, capture.identity(target))[0]
    assert _success(state)["status"] == "success_unsupported"


@pytest.mark.parametrize("kind", ["directory", "glob"])
def test_selected_member_disappearance_inside_check_survives_restoration(tmp_path, monkeypatch, kind):
    state = State.new("experiment", tmp_path / "runs", session_id="review-members")
    target = state.root / "outputs/experiment/runtime/result.txt"
    state.hook_state["node_inputs"] = {"experiment_spec": "Produce and verify the delivered result."}
    if kind == "glob":
        state.hook_state["node_inputs"]["required_outputs"] = [
            {"id": "result", "kind": "glob", "pattern": str(target.parent / "*.txt"), "min_count": 1}]
    _run(run_bootstrap.establish(state))
    _produce(state, target)
    original = capture.identity(target)
    parked = state.root / "parked"
    calls = []

    async def remove_during_check(actual_state, obligation):
        if not calls:
            target.rename(parked)
        calls.append(1)
        return {"exit_code": 0, "executed": True, "stdout_tail": "OK", "stderr_tail": ""}

    monkeypatch.setattr(finish, "_check_runner", remove_during_check)
    request = {"outcome": "success", "summary": "Checked result",
               "verification": [{"requirement": "Verify the result", "checks": [
                   {"kind": "check", "command": "fixture readonly check", "expect_stdout_contains": "OK"}]}]}
    if kind == "directory":
        request["deliverables"] = [{"path": str(target.parent)}]
    first = _run(finish._finish(state, **request))
    assert first["status"] == "success_unsupported", first
    parked.rename(target)
    assert capture.same_identity(original, capture.identity(target))[0]
    second = _run(finish._finish(state, **request))
    assert second["status"] == "success_unsupported", second
    assert _invalidations(state)


@pytest.mark.parametrize("sealed_before", [False, True])
def test_read_error_does_not_become_permanent_discontinuity(tmp_path, monkeypatch, sealed_before):
    state, target = _state(tmp_path)
    _produce(state, target)
    if sealed_before:
        assert _success(state)["status"] == "success"
    real_identity = capture.identity

    def unreadable(path, **kwargs):
        return {"kind": "error", "error": "fixture PermissionError"} if path == target else real_identity(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(capture, "identity", unreadable)
        assert _success(state)["status"] == "success_unsupported"
        assert _invalidations(state) == []
    assert _success(state)["status"] == "success"


def test_missing_observation_survives_restart_and_allows_new_producer(tmp_path):
    state, target = _state(tmp_path)
    old = _produce(state, target)
    parked = state.root / "parked"
    target.rename(parked)
    assert _success(state)["status"] == "success_unsupported"
    parked.rename(target)
    reopened = State.reopen("experiment", state.root.parent, state.run_id, session_id="output-invalidation")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    assert _success(reopened)["status"] == "success_unsupported"
    new = _produce(reopened, target, replace=True)
    assert new != old
    assert _success(reopened)["status"] == "success"
    assert _invalidations(reopened)[0]["producer_action_id"] == old


def test_sealed_replay_reconfirms_observation_durability_before_success(tmp_path, monkeypatch):
    state, target = _state(tmp_path)
    _produce(state, target)
    assert _success(state)["status"] == "success"
    historical = delivery_transaction.read_manifest(state)
    original = _unobserved_rewrite(target)
    append = ledger.Ledger.append_once

    def unavailable(self, kind, *args, **kwargs):
        if kind == "output_identity_invalidated":
            raise OSError("fixture ledger unavailable")
        return append(self, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ledger.Ledger, "append_once", unavailable)
        failed = _success(state)
        assert failed["status"] == "error" and failed["error_code"] == "output_identity_observation_not_durable", failed
    assert _success(state)["status"] == "success_unsupported"
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    assert _success(state)["status"] == "success_unsupported"
    assert delivery_transaction.read_manifest(state) == historical


def test_new_python_process_rebuilds_invalidation_then_accepts_fresh_generation(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    program = r'''
import json, sys
from pathlib import Path
from core.state import State
from nodes.experiment.tests.test_060_output_identity_invalidation import _produce, _success, _invalidations
state = State.reopen("experiment", Path(sys.argv[1]), sys.argv[2], session_id="output-invalidation")
state.hook_state["node_inputs"] = json.loads(sys.argv[3])
target = Path(sys.argv[4])
blocked = _success(state)
assert blocked["status"] == "success_unsupported", blocked
history = _invalidations(state)
assert len(history) == 1
_produce(state, target, replace=True)
recovered = _success(state)
assert recovered["status"] == "success", recovered
assert _invalidations(state) == history
print("PROCESS_RECOVERY_OK")
'''
    result = subprocess.run([sys.executable, "-B", "-c", program, str(state.root.parent), state.run_id,
                             json.dumps(state.hook_state["node_inputs"]), str(target)],
                            cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROCESS_RECOVERY_OK" in result.stdout


@pytest.mark.parametrize("finish_while_pending", [False, True])
def test_unchanged_action_preserves_its_before_observation_of_broken_source(tmp_path, finish_while_pending):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = _unobserved_rewrite(target)
    observer = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.SUPPORT,
                                     command="fixture read existing bytes", output_candidates=[target])
    assert target.read_bytes() == b"49\n"
    if finish_while_pending:
        # An in-flight action is allowed to settle before a continuity verdict.
        assert _success(state)["status"] == "success_unsupported"
    assert action_boundary.settle(state, observer, outcome="settled", exit_code=0)["status"] == "recorded"
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    assert capture.same_identity(original, capture.identity(target))[0]
    retried = _success(state)
    assert retried["status"] == "success_unsupported", retried
    assert _invalidations(state)


def test_overlapping_observer_does_not_invent_a_definite_discontinuity(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    writer = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
                                   command="fixture pending writer", output_candidates=[target])
    _unobserved_rewrite(target)  # the owned writer's intermediate state
    reader = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.SUPPORT,
                                   command="fixture overlapping read", output_candidates=[target])
    assert action_boundary.settle(state, reader, outcome="settled", exit_code=0)["status"] == "recorded"
    assert action_boundary.settle(state, writer, outcome="settled", exit_code=0)["status"] == "recorded"
    result = _success(state)
    # Existing overlap handling reports uncertain provenance, not proof that
    # a particular producer was invalidated by an outside write.
    assert result["status"] == "success_unsupported"
    assert _invalidations(state) == []


def test_large_source_observation_uses_digest_bound_capture_not_summary(tmp_path):
    state, target = _state(tmp_path)
    paths = [target.with_name(f"result-{i:03d}.txt") for i in range(205)]
    writer = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
                                   command="fixture bounded output set", output_candidates=paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    for path in paths:
        path.write_bytes(b"49\n")
    assert action_boundary.settle(state, writer, outcome="settled", exit_code=0)["status"] == "recorded"
    victim = paths[-1]
    original = _unobserved_rewrite(victim)
    reader = action_boundary.begin(state, tool="safe_run_bash", effect_class=action_boundary.SUPPORT,
                                   command="fixture read many results", output_candidates=paths,
                                   work_root=str(target.parent))
    assert action_boundary.settle(state, reader, outcome="settled", exit_code=0)["status"] == "recorded"
    records = ledger.open_ledger(state).records()
    row = next(r for r in records if r.get("kind") == "observation" and r.get("action_id") == reader.action_id)
    assert row["observation_ref"] and len(row["source_observations"]) == 200
    full = next(r for r in ledger.project_output_observations(records) if r.get("action_id") == reader.action_id)
    assert full["source_observations"][str(victim)]["mtime_ns"] != original["mtime_ns"]
    os.utime(victim, ns=(original["mtime_ns"], original["mtime_ns"]))
    evidence = finish._file_evidence(state, records, {"path": str(victim)})
    assert evidence["passed"] is False
    assert evidence["evidence"]["identity_discontinuities"]
