"""完成证据收紧（第 5 步 5a；design_expected_termination_0914.md §1.2、§1.3、§3.2）。

- 读得到的非零退出码，不能由新鲜的 completion_paths 代证 operation_completed；
- ROC 无收据时，declared_completion_paths 与 finalize 用同一个新鲜度判据（原先只看 exists）；
- 模型声明的 error_patterns 替换不掉平台自己写出的失败标记。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as manager
from test_external_job_handoff import _State, _submission as _handoff_submission
from test_operation_job_finalization import (
    _finalize, _health, _patch_pipeline, _prepared_state, _submission,
)


def _completion_snapshot(submission: dict, offset_s: float) -> dict:
    submitted = datetime.fromisoformat(submission["submitted_at"]).timestamp()
    return {"path": "/tmp/op/out.done", "exists": True, "mtime_epoch_s": submitted + offset_s}


def test_a_readable_nonzero_exit_is_not_completed_by_fresh_completion_paths(tmp_path, monkeypatch):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        completion_paths=[_completion_snapshot(submission, 120.0)],
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 3}}}))

    completed = _finalize(state, submission, "operation_completed")

    assert completed["status"] == "error", completed
    assert completed["error_code"] == "operation_completed_positive_evidence_missing"
    assert "exit_code=3" in completed["error"]
    assert state.list_artifacts("external_job_operation_closure") == []
    # 出口仍在：如实记失败。
    failed = _finalize(state, submission, "operation_failed")
    assert failed["status"] == "success", failed


@pytest.mark.parametrize("offset_s, verified", [(120.0, True), (-120.0, False)])
def test_roc_counts_completion_paths_only_when_written_after_submission(offset_s, verified):
    record = _submission()  # slurm：没有退出码可读，走 completion_paths 分支
    health = _health(completion_paths=[_completion_snapshot(record, offset_s)])

    evidence = oc._external_job_success_evidence(record, health, {"status": None})

    assert evidence["verified"] is verified, evidence
    if verified:
        assert evidence["source"] == "declared_completion_paths"


def test_declared_error_patterns_cannot_hide_a_platform_limit_marker(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "out.log").write_text("", encoding="utf-8")
    (logs / "err.log").write_text(
        "HARNESS_SANDBOX_LIMIT pids baseline=3 final=9\n", encoding="utf-8")
    record = _handoff_submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        health_contract={
            "progress_paths": [],
            "completion_paths": [],
            "error_patterns": ["deliberate failure marker"],
            "stall_after_s": 60,
        },
    )
    state = _State(tmp_path, [record])
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "NOT_RUNNING", "stderr": ""},
    })

    health = manager.probe_external_job_health(state, "local", record["job_id"])

    markers = [item.get("marker") for item in health.get("error_evidence") or []]
    assert "HARNESS_SANDBOX_LIMIT" in markers, health.get("error_evidence")
