"""035a (C5): the health channel and ROC use the same declared-output evaluator.

health (SCOPE G2/G3): the finalizer only checked ``exists ∧ mtime ≥ submitted_at``
and re-read the disk on every finalize, so an empty file, an empty directory, a
same-basename decoy log, or a file created *after* the first terminal refusal all
became ``operation_completed``.  Now: the first terminal observation is frozen per
exact job identity (append-only transcript event), type / non-empty / stale-content
rules come from ``output_postconditions``, and only the current job's registered
stdout/stderr may be empty.

ROC: ``_path_check`` is a thin wrapper over the same evaluator; a trailing ``/``
declares a directory delivery (must contain a non-empty regular file).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core.state import State
from nodes.experiment.tests.test_declared_postcondition_gate import _snapshot, _zero_exit_health
from nodes.experiment.tests.test_operation_job_finalization import (
    _finalize, _prepared_state, _submission,
)
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as manager
from nodes.experiment.tools import run_contract


# ── health：真实探针 ───────────────────────────────────────────────────────────

def _local_record(work: Path, *, completion_paths: list[Path], kinds: dict | None = None,
                  submitted_at: str | None = None) -> dict:
    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "out.log").write_text("")
    (logs / "err.log").write_text("")
    return {
        "status": "success", "dry_run": False, "scheduler": "local", "job_id": "4242",
        "job_name": "c5-job", "workdir": str(work), "output_roots": [str(work)],
        "scheduler_output_dir": str(logs),
        "stdout_path": str(logs / "out.log"), "stderr_path": str(logs / "err.log"),
        "submitted_at": submitted_at or datetime.now(timezone.utc).isoformat(),
        "health_contract": {
            "progress_paths": [], "completion_paths": [str(p) for p in completion_paths],
            "completion_kinds": {str(p): k for p, k in (kinds or {}).items()},
            "error_patterns": [], "stall_after_s": 60,
        },
    }


def _probe(tmp_path: Path, monkeypatch, record: dict) -> tuple[State, dict]:
    state = State.new("experiment", tmp_path / "runs")
    state.save_artifact("job_submission", "c5_submission", json.dumps(record))
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success", "raw": {"ok": True, "stdout": "NOT_RUNNING", "stderr": ""}})
    health = manager.probe_external_job_health(state, "local", record["job_id"])
    assert health["status"] == "success", health
    return state, health


def test_health_empty_completion_file_is_not_positive_evidence(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    result = work / "out" / "result.bin"
    result.write_text("")                                        # crashed after opening the file
    record = _local_record(work, completion_paths=[result])
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is False
    assert "空文件" in reason


def test_health_directory_declared_with_slash_needs_a_fresh_non_empty_entry(tmp_path, monkeypatch):
    work = tmp_path / "work"
    results = work / "out" / "results"
    results.mkdir(parents=True)                                  # mkdir -p, then nothing
    record = _local_record(work, completion_paths=[results], kinds={results: "directory"})
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is False and "空目录" in reason
    (results / "step_001.nc").write_text("data\n")
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is True, reason


def test_health_directory_declared_without_slash_is_a_kind_mismatch(tmp_path, monkeypatch):
    work = tmp_path / "work"
    results = work / "out" / "results"
    results.mkdir(parents=True)
    (results / "step_001.nc").write_text("data\n")
    record = _local_record(work, completion_paths=[results])     # no trailing slash → file
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is False and "类型与声明不符" in reason


def test_health_registered_stdout_may_be_empty_but_a_decoy_may_not(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "other").mkdir(parents=True)
    decoy = work / "other" / "out.log"                          # same basename, not registered
    decoy.write_text("")
    record = _local_record(work, completion_paths=[work / "logs" / "out.log", decoy])
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is False and str(decoy) in reason
    record = _local_record(work, completion_paths=[work / "logs" / "out.log"])
    _state, health = _probe(tmp_path, monkeypatch, record)
    assert manager._operation_completion_paths_fresh(record, health) == (True, "completion_paths_fresh")


def test_health_symlink_to_stale_content_is_not_fresh(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "prev").mkdir(parents=True)
    old = work / "prev" / "result.bin"
    old.write_text("old\n")
    time.sleep(0.15)                                             # content predates the submission
    record = _local_record(work, completion_paths=[work / "result.bin"])
    os.symlink(old, work / "result.bin")
    _state, health = _probe(tmp_path, monkeypatch, record)
    fresh, reason = manager._operation_completion_paths_fresh(record, health)
    assert fresh is False and "早于作业提交" in reason


def test_health_contract_records_the_declared_kind_before_normalising(tmp_path):
    work = tmp_path / "work"
    (work / "logs").mkdir(parents=True)
    contract = manager._health_contract(
        {"completion_paths": ["out/results/", "out/result.bin"]},
        workdir=str(work), output_roots=[str(work)], scheduler_output_dir=work / "logs",
        expected_duration_s=None)
    results = os.path.realpath(str(work / "out" / "results"))
    result = os.path.realpath(str(work / "out" / "result.bin"))
    assert contract["completion_paths"] == sorted([results, result])   # unchanged behaviour
    assert contract["completion_kinds"] == {results: "directory", result: "file"}


# ── health：首次终态观测冻结（G3） ───────────────────────────────────────────

def _events(state: State, name: str) -> list[dict]:
    if not state.transcript_path.is_file():
        return []
    return [json.loads(line) for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == name]


def test_finalize_freezes_the_first_terminal_observation(tmp_path, monkeypatch):
    submission = _submission(execution_class="toolchain_build",
                             health_contract={"completion_paths": ["build/result.bin"]})
    state = _prepared_state(tmp_path, submission)
    healths = [_zero_exit_health(completion_paths=[_snapshot(submission, exists=False)])]
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: dict(healths[-1]))
    monkeypatch.setattr(manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_route, "record_external_route_finalization",
                        lambda *_a, **_k: {"status": "success"})

    first = _finalize(state, submission, "operation_completed")
    assert first["status"] == "error", first
    assert first["error_code"] == "operation_completed_positive_evidence_missing"
    assert len(_events(state, "external_job_terminal_output_observation")) == 1

    # the file appears after the refusal (touched by hand, another process, whatever)
    healths.append(_zero_exit_health(completion_paths=[_snapshot(submission, exists=True, offset_s=120.0)]))
    second = _finalize(state, submission, "operation_completed")
    assert second["status"] == "error", second
    assert second["error_code"] == "operation_completed_positive_evidence_missing"
    assert len(_events(state, "external_job_terminal_output_observation")) == 1

    reopened = State.reopen("experiment", tmp_path, state.run_id)
    third = _finalize(reopened, submission, "operation_completed")
    assert third["status"] == "error", third
    assert state.list_artifacts("external_job_operation_closure") == []


def test_first_terminal_observation_that_cannot_persist_is_not_positive_evidence(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    (work / "out" / "result.bin").write_text("ok\n")
    record = _local_record(work, completion_paths=[work / "out" / "result.bin"])
    state, health = _probe(tmp_path, monkeypatch, record)
    assert manager._operation_completion_paths_fresh(record, health)[0] is True

    def _broken(event, **_fields):
        raise OSError("disk full")
    monkeypatch.setattr(state, "append_transcript", _broken)
    frozen = manager.freeze_first_terminal_output_observation(state, record, health)
    assert frozen["terminal_output_observation"]["source"] == "unfrozen"
    fresh, reason = manager._operation_completion_paths_fresh(record, frozen)
    assert fresh is False and "未能冻结" in reason


# ── ROC：目录声明与非空 ──────────────────────────────────────────────────────

def _delivery_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path / "runs")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Deliver the staged files.",
        "prereg_assignment": {"kind": "none", "reason": "035a ROC fixture"},
    }
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="other", reason="deliver files"))
    assert classified["status"] == "success", classified
    return state


def _deliver(state: State, *paths: str) -> dict:
    return asyncio.run(oc._record_operation_completion(
        state, task_kind="file_delivery", objective="deliver staged files", outcome="success",
        checks=[{"name": "staged", "passed": True, "evidence": {"count": len(paths)}}],
        artifact_paths=list(paths)))


def _path_check_of(result: dict, name: str) -> dict:
    return next(item for item in result["checks"] if item["name"] == name)


def _demoted(result: dict, name: str, reason: str) -> None:
    """ROC 对 outcome=success 但证据检查没过的收据机械降 partial 并披露（不新造拒绝墙）。"""
    assert result["status"] == "success", result
    assert result["outcome"] == "partial" and result.get("outcome_demoted_from") == "success", result
    check = _path_check_of(result, name)
    assert check["passed"] is False and check["evidence"]["reason"] == reason, check


def test_roc_directory_delivery_needs_trailing_slash_and_a_non_empty_entry(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    _demoted(_deliver(_delivery_state(tmp_path / "a"), f"{results}/"), "path:results", "empty_directory")

    (results / "step_001.nc").write_text("data\n")
    delivered = _deliver(_delivery_state(tmp_path / "b"), f"{results}/")
    assert delivered["outcome"] == "success", delivered
    assert _path_check_of(delivered, "path:results")["evidence"]["declared_kind"] == "directory"

    # the same directory declared without the slash is a file declaration → kind mismatch
    _demoted(_deliver(_delivery_state(tmp_path / "c"), str(results)), "path:results", "not_regular_file")


def test_roc_empty_file_and_symlink_to_content(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    _demoted(_deliver(_delivery_state(tmp_path / "a"), str(empty)), "path:empty.txt", "empty_file")
    real = tmp_path / "real.txt"
    real.write_text("content\n")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    delivered = _deliver(_delivery_state(tmp_path / "b"), str(link))
    assert delivered["outcome"] == "success", delivered
    # ROC 的证据路径先 resolve 再检查（既有行为）：检查的是链接目标；归属由 P0a 收据另判。
    check = _path_check_of(delivered, "path:real.txt")
    assert check["evidence"]["kind"] == "file" and check["passed"] is True
