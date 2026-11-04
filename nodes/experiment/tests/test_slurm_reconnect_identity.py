"""Lost ACK recovery must identify exactly one job without inferring absence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.state import State
from nodes.experiment.tools import ledger, scheduler_adapters as sa


def _state(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


def _commands(monkeypatch, *, queue="", accounting=""):
    monkeypatch.setattr(sa.SlurmAdapter, "capabilities", lambda self: sa.AdapterCapabilities(
        scheduler="slurm", submit=True, query_status=True, cancel=True, terminal_facts=True,
        reconnect_by_correlation_key=True, remote_write_isolation=sa.ISOLATION_MISSING))
    monkeypatch.setattr(sa, "_run", lambda argv, **kwargs: {
        "ok": True, "stdout": queue if argv[0] == "squeue" else accounting,
        "stderr": "", "returncode": 0})


@pytest.mark.parametrize("source", ["queue", "accounting"])
@pytest.mark.parametrize("comment", ["ai4s:nonce-extra", "prefix-ai4s:nonce", "ai4s:nonce,other"])
def test_a_similar_comment_is_not_this_submission(monkeypatch, source, comment):
    row = f"42|job|{comment}|RUNNING|0:0"
    _commands(monkeypatch, **{source: row})
    result = sa.SlurmAdapter(user="cluster-user").reconnect("nonce")
    assert result["status"] == "cannot_query", result
    assert result["jobs"] == []


def test_unrelated_accounting_comment_does_not_authorize_duplicate_submission(tmp_path, monkeypatch):
    _commands(monkeypatch, accounting="42|other|ai4s:other|COMPLETED|0:0")
    state = _state(tmp_path)
    result = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce")
    assert result["decision"] == "unknown"
    assert result["resubmit_allowed"] is False
    assert [r["kind"] for r in ledger.open_ledger(state).records()] == ["job_unknown"]


def test_multiple_exact_matches_remain_unknown_until_identity_can_be_resolved(tmp_path, monkeypatch):
    _commands(monkeypatch, queue="42|one|ai4s:nonce|RUNNING", accounting="43|two|ai4s:nonce|COMPLETED|0:0")
    state = _state(tmp_path)
    result = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce")
    assert result["decision"] == "unknown"
    assert result["resubmit_allowed"] is False
    assert {j["job_id"] for j in result["probe"]["jobs"]} == {"42", "43"}
    assert [r["kind"] for r in ledger.open_ledger(state).records()] == ["job_unknown"]


def test_duplicate_observation_of_one_job_does_not_prevent_recovery(tmp_path, monkeypatch):
    _commands(monkeypatch, queue="42|one|ai4s:nonce|RUNNING", accounting="42|one|ai4s:nonce|RUNNING|0:0")
    state = _state(tmp_path)
    result = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce")
    assert result["decision"] == "adopted"
    assert result["external_id"] == "42"
    assert result["resubmit_allowed"] is False


@pytest.mark.parametrize("jobs", [[], [{"job_id": ""}], [{"job_id": "42"}, {"job_id": "43"}]])
def test_invalid_found_result_is_not_recorded_as_an_adoption(tmp_path, monkeypatch, jobs):
    monkeypatch.setattr(sa, "adapter_for", lambda scheduler: SimpleNamespace(
        reconnect=lambda key: {"status": "found", "jobs": jobs}))
    state = _state(tmp_path)
    result = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce")
    assert result["decision"] == "unknown"
    assert result["resubmit_allowed"] is False
    assert [r["kind"] for r in ledger.open_ledger(state).records()] == ["job_unknown"]
