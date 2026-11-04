"""Confirmed cancellation must close the same v3 job fact that finish reads."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import finish, job_settlement, ledger, resource_manager as rm, run_bootstrap
from shared.lib import dangerous_commands as danger


def _prepared(tmp_path, monkeypatch, *, planned=True, scheduler="local",
              legacy_binding=False, bound_runtime=None):
    state = State.new("experiment", tmp_path / "runs")
    product = state.root / "heartbeat.log"
    state.hook_state["node_inputs"] = {
        "requested_work": "Start the worker, preserve its heartbeat, and stop it after the smoke test.",
        "scope": "operation", "operation_category": "other",
        "prereg_assignment": {"kind": "none", "reason": "Operational smoke test."},
        "required_outputs": [{"id": "heartbeat", "kind": "file", "path": str(product)}],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    record = {
        "scheduler": scheduler, "job_id": "owned-worker", "submission_nonce": "owned-nonce",
        "container_runtime_id": "a" * 64 if scheduler == "local" else None,
        "output_roots": [str(state.root)], "output_root": str(state.root),
        "status": "success", "dry_run": False,
        **({"expected_termination": {"planned_stop": True, "task_quote": "stop it after the smoke test"}}
           if planned else {}),
    }
    state.save_artifact("job_submission", "owned-worker", json.dumps(record))
    owner = ledger.open_ledger(state, write=True)
    owner.append("intent", nonce=record["submission_nonce"], scheduler=scheduler,
                 output_root=str(state.root), output_roots=[str(state.root)])
    owner.append("submission_attempted", nonce=record["submission_nonce"], scheduler=scheduler,
                 output_baseline=job_settlement.capture_submission_outputs(state, record["submission_nonce"]))
    product.write_text("beat 1 1000\n", encoding="utf-8")
    binding = {"external_id": record["job_id"],
               "container_runtime_id": record["container_runtime_id"] if bound_runtime is None else bound_runtime}
    if legacy_binding:
        binding = {"external_id": record["container_runtime_id"] or record["job_id"]}
    owner.append("job_bound", nonce=record["submission_nonce"], scheduler=scheduler, **binding)
    monkeypatch.setattr(danger, "bypass_enabled", lambda: True)
    async def not_ended(*_args, **_kwargs):
        return None
    monkeypatch.setattr(rm, "_observed_job_end", not_ended)
    monkeypatch.setattr(rm, "_cleanup_local_job_for_finalization", lambda _record: {"status": "success"})
    calls = []
    def cancel(*_args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "action": "sandbox_stop" if scheduler == "local" else "scancel",
                "container_id": record["job_id"], "already_absent": False}
    monkeypatch.setattr(rm, "_cancel_sync", cancel)
    return state, record, product, calls


def _terminals(state):
    return [row for row in ledger.open_ledger(state).records() if row.get("kind") == "job_terminal"]


def test_confirmed_cancel_closes_v3_and_preserves_cancelled_physical_fact(tmp_path, monkeypatch):
    state, record, product, calls = _prepared(tmp_path, monkeypatch)
    cancelled = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="requested smoke stop"))
    assert cancelled["status"] == "success", cancelled
    assert len(calls) == 1
    rows = _terminals(state)
    assert len(rows) == 1
    assert rows[0]["nonce"] == record["submission_nonce"]
    assert rows[0]["derived_state"] == "CANCELLED"
    assert rows[0]["exit_code"] is None
    assert str(product) in rows[0]["output_files"]
    assert ledger.open_jobs(ledger.open_ledger(state).records()) == []
    closed = asyncio.run(finish._finish(state, outcome="success", summary="Observed heartbeat, then stopped worker."))
    assert closed["status"] == "success", closed
    assert closed["outcome"] == "success"
    assert _terminals(state)[0]["derived_state"] == "CANCELLED"


def test_duplicate_cancel_and_terminal_wait_keep_one_fact(tmp_path, monkeypatch):
    state, record, _product, calls = _prepared(tmp_path, monkeypatch)
    first = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="stop"))
    second = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="stop"))
    assert first["status"] == second["status"] == "success"
    assert len(calls) == 1
    monkeypatch.setattr(rm, "_probe_external_job_health_impl", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal", "workflow_status": "awaiting_analysis",
        "scheduler_result": {"raw": {"sandbox_state": {"exists": False}}},
    })
    wait = asyncio.run(rm._wait_for_external_job(state, "local", record["job_id"], max_wait_s=1))
    assert wait["wait_outcome"] == "scheduler_terminal"
    assert (wait["health"].get("terminal_settlement") or {}).get("settled") is True
    assert len(_terminals(state)) == 1


def test_wait_recovers_old_confirmed_receipt_without_resending_cancel(tmp_path, monkeypatch):
    state, record, _product, calls = _prepared(tmp_path, monkeypatch)
    intent = rm._persist_cancellation_intent(state, record, reason="stop", superseded_by=None)
    rm._persist_cancellation_outcome(state, intent, outcome="confirmed", cancel_result={
        "ok": True, "action": "sandbox_stop", "container_id": record["job_id"], "already_absent": False,
    })
    monkeypatch.setattr(rm, "_probe_external_job_health_impl", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal", "workflow_status": "awaiting_analysis",
        "scheduler_result": {"raw": {"sandbox_state": {"exists": False}}},
    })
    wait = asyncio.run(rm._wait_for_external_job(state, "local", record["job_id"], max_wait_s=1))
    assert (wait["health"].get("terminal_settlement") or {}).get("settled") is True
    assert _terminals(state)[0]["derived_state"] == "CANCELLED"
    assert calls == []


@pytest.mark.parametrize("outcome,physical", [
    ("unknown", {"ok": False, "action": "sandbox_stop", "outcome_unknown": True}),
    ("confirmed", {"ok": True, "action": "sandbox_stop", "already_absent": True}),
    ("confirmed", {"ok": True, "action": "signal_sent", "already_absent": False}),
])
def test_unconfirmed_stop_never_settles_or_allows_finish(tmp_path, monkeypatch, outcome, physical):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    intent = rm._persist_cancellation_intent(state, record, reason="stop", superseded_by=None)
    rm._persist_cancellation_outcome(state, intent, outcome=outcome, cancel_result=physical)
    result = rm._settle_if_terminal(state, record, scheduler_result=None, evidence=None, observed_by="probe")
    assert result is None or result.get("settled") is False
    assert _terminals(state) == []
    closed = asyncio.run(finish._finish(state, outcome="cancelled", summary="Cannot establish the stop."))
    assert closed["status"] == "live_external_effect", closed


def test_remote_cancellation_ack_is_not_physical_terminal(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch, scheduler="slurm")
    result = asyncio.run(rm._cancel_job(state, "slurm", record["job_id"], reason="stop"))
    assert result["cancel"]["ok"] is True
    assert _terminals(state) == []
    closed = asyncio.run(finish._finish(state, outcome="cancelled", summary="Awaiting scheduler terminal."))
    assert closed["status"] == "live_external_effect"


def test_other_nonce_receipt_cannot_settle_this_job(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    wrong = dict(record, submission_nonce="other-nonce", container_runtime_id="b" * 64)
    intent = rm._persist_cancellation_intent(state, wrong, reason="stop", superseded_by=None)
    rm._persist_cancellation_outcome(state, intent, outcome="confirmed", cancel_result={
        "ok": True, "action": "sandbox_stop", "already_absent": False,
    })
    result = rm._settle_if_terminal(state, record, scheduler_result=None, evidence=None, observed_by="probe")
    assert result is None or result.get("settled") is False
    assert _terminals(state) == []


@pytest.mark.parametrize("after_commit", [False, True], ids=["before-row", "after-commit"])
def test_terminal_write_failure_recovers_without_a_second_physical_stop(tmp_path, monkeypatch, after_commit):
    state, record, _product, calls = _prepared(tmp_path, monkeypatch)
    original = ledger.Ledger.append_once
    injected = False
    def fail_once(owner, kind, *args, **kwargs):
        nonlocal injected
        if kind == "job_terminal" and not injected:
            injected = True
            if after_commit:
                original(owner, kind, *args, **kwargs)
            raise OSError("injected terminal persistence failure")
        return original(owner, kind, *args, **kwargs)
    monkeypatch.setattr(ledger.Ledger, "append_once", fail_once)
    first = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="stop"))
    assert first["status"] == "cancellation_reconciliation_required", first
    assert len(calls) == 1
    second = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="stop"))
    assert second["status"] == "success", second
    assert len(calls) == 1
    assert len(_terminals(state)) == 1
    assert ledger.open_jobs(ledger.open_ledger(state).records()) == []


@pytest.mark.parametrize("field", ["submission_nonce", "container_runtime_id", "job_id"])
def test_canonical_writer_rejects_cancellation_evidence_from_another_identity(tmp_path, monkeypatch, field):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    intent = rm._persist_cancellation_intent(state, record, reason="stop", superseded_by=None)
    rm._persist_cancellation_outcome(state, intent, outcome="confirmed", cancel_result={
        "ok": True, "action": "sandbox_stop", "container_id": record["job_id"], "already_absent": False,
    })
    proof = rm._confirmed_local_cancellation_evidence(state, record)
    assert proof is not None
    proof["identity"][field] = "another-identity"
    result = job_settlement.settle_terminal(state, record, evidence=proof, observed_by="negative-test")
    assert result == {"settled": False, "reason": "untrusted_cancellation_evidence",
                      "nonce": record["submission_nonce"]}
    assert _terminals(state) == []


def test_real_owned_process_is_stopped_before_the_cancellation_fact_is_published(tmp_path, monkeypatch):
    """Real subprocess; the adapter stop callback is scoped to this test-owned PID."""
    import os
    import subprocess
    import sys
    import time
    state, record, product, _calls = _prepared(tmp_path, monkeypatch)
    code = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('beat 1 pid=%s\\n'%os.getpid()); time.sleep(60)"
    # Windows venv python.exe can be a launcher with a different child PID.
    # Target the actual interpreter so the owned Popen handle is the payload.
    interpreter = getattr(sys, "_base_executable", sys.executable)
    process = subprocess.Popen([interpreter, "-c", code, str(product)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "pid=" not in product.read_text(encoding="utf-8"):
            time.sleep(0.02)
        assert f"pid={process.pid}" in product.read_text(encoding="utf-8")
        assert process.poll() is None
        def stop_owned_process(_scheduler, job_id, _namespace, **kwargs):
            assert job_id == record["job_id"]
            assert kwargs["container_runtime_id"] == record["container_runtime_id"]
            process.terminate()
            process.wait(timeout=5)
            assert process.poll() is not None
            return {"ok": True, "action": "sandbox_stop", "container_id": job_id, "already_absent": False}
        monkeypatch.setattr(rm, "_cancel_sync", stop_owned_process)
        result = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="stop owned test process"))
        assert result["status"] == "success", result
        assert process.poll() is not None
        assert _terminals(state)[0]["derived_state"] == "CANCELLED"
        assert _terminals(state)[0]["exit_code"] is None
        assert str(product) in _terminals(state)[0]["output_files"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_cancellation_receipt_cannot_close_a_different_ledger_binding(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    intent = rm._persist_cancellation_intent(state, record, reason="stop", superseded_by=None)
    rm._persist_cancellation_outcome(state, intent, outcome="confirmed", cancel_result={
        "ok": True, "action": "sandbox_stop", "container_id": record["job_id"], "already_absent": False,
    })
    proof = rm._confirmed_local_cancellation_evidence(state, record)
    assert proof is not None
    ledger.open_ledger(state, write=True).append(
        "job_reconciled", nonce=record["submission_nonce"], state="submitted", external_id="another-runtime")
    result = job_settlement.settle_terminal(state, record, evidence=proof, observed_by="negative-test")
    assert result["settled"] is False and result["reason"] == "untrusted_cancellation_evidence"
    assert _terminals(state) == []


def _cancellation_evidence(record):
    return {"source": "confirmed_local_cancellation",
            "identity": {key: record.get(key) for key in (
                "scheduler", "job_id", "submission_nonce", "container_runtime_id")},
            "cancel_result": {"ok": True, "action": "sandbox_stop", "already_absent": False,
                              "container_id": record["job_id"]},
            "cancellation_intent_artifact_id": "intent-proof",
            "cancellation_outcome_artifact_id": "outcome-proof"}


def test_cancellation_cannot_settle_a_different_bound_runtime(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch, bound_runtime="b" * 64)
    projected = ledger.project_jobs(ledger.open_ledger(state).records())[record["submission_nonce"]]
    assert projected["external_id"] == record["job_id"]
    assert projected["container_runtime_id"] == "b" * 64
    result = job_settlement.settle_terminal(state, record, evidence=_cancellation_evidence(record))
    assert result["settled"] is False
    assert result["reason"] == "untrusted_cancellation_evidence"
    assert _terminals(state) == []
    assert len(ledger.open_jobs(ledger.open_ledger(state).records())) == 1


def test_legacy_runtime_external_id_still_accepts_confirmed_cancellation(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch, legacy_binding=True)
    result = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="requested stop"))
    assert result["status"] == "success", result
    assert len(_terminals(state)) == 1
    assert _terminals(state)[0]["derived_state"] == "CANCELLED"


def test_reconciliation_without_runtime_preserves_bound_identity(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    ledger.open_ledger(state, write=True).append(
        "job_reconciled", nonce=record["submission_nonce"], external_id=record["job_id"])
    projected = ledger.project_jobs(ledger.open_ledger(state).records())[record["submission_nonce"]]
    assert projected["container_runtime_id"] == record["container_runtime_id"]
    result = asyncio.run(rm._cancel_job(state, "local", record["job_id"], reason="requested stop"))
    assert result["status"] == "success", result
    assert len(_terminals(state)) == 1


def test_conflicting_reconciled_runtime_cannot_authorize_terminal(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    ledger.open_ledger(state, write=True).append(
        "job_reconciled", nonce=record["submission_nonce"], external_id=record["job_id"],
        container_runtime_id="b" * 64)
    projected = ledger.project_jobs(ledger.open_ledger(state).records())[record["submission_nonce"]]
    assert projected["runtime_identity_conflict"] is True
    assert projected["container_runtime_id"] == record["container_runtime_id"]
    result = job_settlement.settle_terminal(state, record, evidence=_cancellation_evidence(record))
    assert result["settled"] is False
    assert _terminals(state) == []


def test_reconciled_other_job_id_cannot_authorize_original_cancellation(tmp_path, monkeypatch):
    state, record, _product, _calls = _prepared(tmp_path, monkeypatch)
    ledger.open_ledger(state, write=True).append(
        "job_reconciled", nonce=record["submission_nonce"], external_id="other-worker")
    result = job_settlement.settle_terminal(state, record, evidence=_cancellation_evidence(record))
    assert result["settled"] is False
    assert _terminals(state) == []
