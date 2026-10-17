"""Presentation limits must not discard identities already observed by B.

Real filesystem writes bracketed by the existing action boundary; no mocked
execution, producer identity or finish verdict. Snapshot budget injection is
only a negative observation-capability test.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import action_boundary, capture, finish, job_settlement, ledger, run_bootstrap


@pytest.fixture
def state(tmp_path):
    state = State.new("experiment", tmp_path / "runs", project_id="capture-facts", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": "Build and deliver the final executable.",
                                      "prereg_assignment": {"kind": "none", "reason": "build test"}}
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state


def _root(state):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _begin(state, root, candidates=()):
    handle = action_boundary.begin(state, tool="capture-test", work_root=str(root),
                                   output_candidates=list(candidates))
    assert isinstance(handle, action_boundary.ActionHandle)
    return handle


def _settle(state, handle):
    result = action_boundary.settle(state, handle, outcome="settled", exit_code=0)
    assert result["status"] == "recorded", result
    return ledger.open_ledger(state).records()


@pytest.mark.parametrize("predeclared", [False, True])
def test_large_complete_action_retains_modified_executable_with_or_without_candidate(state, predeclared):
    root = _root(state)
    target = root / "solver"
    target.write_text("old executable")
    handle = _begin(state, root, [target] if predeclared else [])
    for number in range(240):
        (root / f"object-{number:03}.o").write_text(f"object {number}")
    target.write_text("new compiled executable")
    records = _settle(state, handle)
    observation = ledger.project_output_observations(records)[-1]
    assert observation["truncated"] is False
    assert observation["identities"][str(target)]["after"] == capture.identity(target)
    assert finish._file_evidence(state, records, {"path": str(target)})["passed"]
    # A late declaration is normal: absence of predeclared outputs must not
    # require another build or a model-side observation ceremony.
    if not predeclared:
        closed = asyncio.run(finish._finish(
            state, outcome="success", summary="The executable was built.",
            deliverables=[{"path": str(target)}],
            verification=[{"requirement": "Deliver the built executable", "checks": [
                {"kind": "file", "path": str(target)}]}],
        ))
        assert closed.get("status") == "success", closed


def test_combined_150_created_100_modified_retains_every_observed_identity(state):
    root = _root(state)
    modified = [root / f"modified-{n:03}" for n in range(100)]
    for path in modified:
        path.write_text("old")
    handle = _begin(state, root)
    created = [root / f"created-{n:03}" for n in range(150)]
    for path in created + modified:
        path.write_text("new content")
    rows = _settle(state, handle)
    observation = ledger.project_output_observations(rows)[-1]
    assert not observation["truncated"]
    assert set(map(str, created + modified)) <= set(observation["identities"])
    assert all(finish._producer_rows(rows, path) for path in created + modified)
    stored = next(row for row in rows if row.get("kind") == "observation")
    assert len(stored["identities"]) == 200
    assert stored["summary_truncated"] and not stored["truncated"]
    assert stored["observation_counts"]["identities"] == len(observation["identities"])
    shown = ledger.project_actions(rows)[-1]["observation"]
    assert shown["summary_truncated"] and len(shown["created"]) == 150
    started = next(row for row in rows if row.get("kind") == "action_started")
    ref = stored["observation_ref"]
    assert Path(ref["file"]).name == ref["file"]
    payload = (Path(started["capture_record"]) / ref["file"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == ref["sha256"]
    assert len(payload) == ref["size_bytes"]
    assert json.loads(payload)["observation"]["identities"] == observation["identities"]


def test_actual_snapshot_budget_exhaustion_never_backfills_a_late_output(state, monkeypatch):
    root = _root(state)
    for n in range(20):
        (root / f"old-{n:03}").write_text("existing")
    real_snapshot = capture.snapshot

    def bounded(*args, **kwargs):
        return real_snapshot(*args, **{**kwargs, "max_files": 3})

    monkeypatch.setattr(capture, "snapshot", bounded)
    handle = _begin(state, root)
    target = root / "late-output"
    target.write_text("actual new data, but outside captured budget")
    rows = _settle(state, handle)
    observation = ledger.project_output_observations(rows)[-1]
    assert observation["truncated"]
    assert not finish._file_evidence(state, rows, {"path": str(target)})["passed"]


def test_managed_terminal_retains_more_than_200_observed_identities(state):
    root = _root(state)
    nonce = "bulk-output-job"
    ledger.record(state, "intent", nonce=nonce, output_root=str(root), output_roots=[str(root)])
    baseline = job_settlement.capture_submission_outputs(state, nonce)
    ledger.record(state, "submission_attempted", nonce=nonce, output_baseline=baseline)
    targets = [root / f"result-{n:03}" for n in range(260)]
    for path in targets:
        path.write_text(path.name)
    settled = job_settlement.settle_terminal(
        state, {"submission_nonce": nonce, "scheduler": "local", "job_id": "recorded-test-job"},
        evidence={"sandbox_status": "exited", "exit_code": 0},
    )
    assert settled["settled"]
    rows = ledger.open_ledger(state).records()
    observation = ledger.project_output_observations(rows)[-1]
    assert not observation["truncated"]
    assert set(map(str, targets)) <= set(observation["identities"])
    assert all(finish._producer_rows(rows, path) for path in targets)
    terminal = settled["record"]
    assert len(terminal["output_diff"]["identities"]) == 200
    assert terminal["output_diff"]["summary_truncated"]
    assert len(terminal["output_files"]) == 50
    assert observation["source_kind"] == "job_terminal"


def _large_observation(state):
    root = _root(state)
    old = root / "earlier-result"
    first = _begin(state, root)
    old.write_text("valid earlier result")
    _settle(state, first)
    second = _begin(state, root)
    targets = [root / f"later-{n:03}" for n in range(260)]
    for path in targets:
        path.write_text(path.name)
    rows = _settle(state, second)
    stored = [row for row in rows if row.get("kind") == "observation"][-1]
    directory = Path(next(row["capture_record"] for row in rows if row.get("action_id") == second.action_id))
    return rows, stored, directory, old, targets


@pytest.mark.parametrize("damage", ["missing", "changed", "symlink", "escaped-reference"])
def test_missing_or_invalid_detail_is_unknown_not_partial_summary_success(state, damage, monkeypatch):
    rows, stored, directory, old, targets = _large_observation(state)
    ref = stored["observation_ref"]
    path = directory / ref["file"]
    # Exercise a previously successful read too: no cache may conceal loss.
    assert finish._producer_rows(rows, targets[-1])
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_bytes(original.replace(b"later-", b"other-"))
    elif damage == "symlink":
        copied = directory / "outside-copy.json"
        copied.write_bytes(original)
        path.unlink()
        path.symlink_to(copied)
    else:
        stored["observation_ref"] = {**ref, "file": "../outside-copy.json"}
        # Bad references must be rejected before attempting a filesystem read.
        original_open = capture.os.open
        def no_open(path, *args, **kwargs):
            if "outside-copy.json" in str(path):
                pytest.fail("escaped evidence reference reached os.open")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(capture.os, "open", no_open)
    projected = ledger.project_output_observations(rows)[-1]
    assert projected["truncated"] and projected["observation_error"]
    assert not projected["identities"]
    assert not finish._producer_rows(rows, old), "A damaged window cannot silently preserve an earlier producer."
    assert not finish._producer_rows(rows, targets[0]), "Even a retained summary row is not the full B fact."
    assert not finish._producer_rows(rows, targets[-1])
    # The original event summary is retained for audit, never silently edited.
    assert len(stored["identities"]) == 200


def test_detail_write_failure_reports_committed_effect_and_forbids_payload_retry(state, monkeypatch):
    root = _root(state)
    handle = _begin(state, root)
    for n in range(240):
        (root / f"result-{n:03}").write_text("written")

    def broken_write(*args, **kwargs):
        raise OSError("injected capture evidence storage failure")

    monkeypatch.setattr(capture, "persist_observation", broken_write)
    result = action_boundary.settle(state, handle, outcome="settled", exit_code=0)
    assert result["status"] == "effect_recording_uncertain"
    assert result["side_effect_committed"] and result["do_not_retry_payload"]


def test_overlapping_large_windows_retain_unknown_beyond_summary_limit(state):
    root = _root(state)
    handles = [_begin(state, root), _begin(state, root)]
    targets = [root / f"result-{n:03}" for n in range(250)]
    for path in targets:
        path.write_text("observed but cannot distinguish the two live writers")
    for handle in handles:
        rows = _settle(state, handle)
    observations = ledger.project_output_observations(rows)
    assert all(set(map(str, targets)) <= set(row["unknown"]) for row in observations)
    assert all(not finish._producer_rows(rows, path) for path in targets)


def test_finish_path_read_uses_frozen_details_without_scanning_outputs_again(state, monkeypatch):
    rows, stored, directory, old, targets = _large_observation(state)

    def no_snapshot(*args, **kwargs):
        pytest.fail("finish attempted to reconstruct an old producer by scanning again")

    monkeypatch.setattr(capture, "snapshot", no_snapshot)
    target = targets[-1]
    assert finish._file_evidence(state, rows, {"path": str(target)})["passed"]
    target.write_text("externally changed after the action")
    assert not finish._file_evidence(state, rows, {"path": str(target)})["passed"]


def test_lost_details_only_invalidate_their_observation_boundary(state):
    unrelated = state.root / "outputs/experiment/independent"
    unrelated.mkdir(parents=True)
    handle = _begin(state, unrelated)
    good = unrelated / "good.txt"
    good.write_text("independent output")
    _settle(state, handle)
    rows, stored, directory, old, targets = _large_observation(state)
    (directory / stored["observation_ref"]["file"]).unlink()
    assert not finish._producer_rows(rows, old)
    assert finish._file_evidence(state, rows, {"path": str(good)})["passed"]


def test_managed_output_over_snapshot_budget_stays_unknown(state):
    root = _root(state)
    nonce = "budgeted-job"
    ledger.record(state, "intent", nonce=nonce, output_root=str(root), output_roots=[str(root)])
    ledger.record(state, "submission_attempted", nonce=nonce,
                  output_baseline=job_settlement.capture_submission_outputs(state, nonce))
    for n in range(550):
        (root / f"result-{n:03}").write_text(str(n))
    result = job_settlement.settle_terminal(
        state, {"submission_nonce": nonce, "scheduler": "local", "job_id": "budgeted-test-job"},
        evidence={"sandbox_status": "exited", "exit_code": 0},
    )
    assert result["settled"]
    rows = ledger.open_ledger(state).records()
    observed = ledger.project_output_observations(rows)[-1]
    assert observed["truncated"] and observed["status"] == "partial"
    missing = next(path for path in root.iterdir() if str(path) not in observed["identities"])
    assert not finish._file_evidence(state, rows, {"path": str(missing)})["passed"]


def test_thousand_member_directory_can_use_retained_facts(state):
    root = _root(state) / "delivered"
    root.mkdir()
    handle = _begin(state, root)
    for number in range(1000):
        (root / f"result-{number:04}").write_text(str(number))
    rows = _settle(state, handle)
    before_members = capture.snapshot([], [root], include_directories=True)
    started = time.monotonic()
    result = finish._directory_evidence(state, rows, root, before_members=before_members)
    print(f"directory_members=1000 evaluation_seconds={time.monotonic() - started:.3f}")
    assert result["passed"], result["evidence"]["member_gaps"]
    assert result["evidence"]["member_count"] == 1000
