"""状态永久读不出的作业不挡同项目后续 run 的 success（2026-09-14 第三会话复审 §四 问题 2）。

诚实出口不写 lifecycle，作业一直算未决 workflow；同项目后续 run 只要自己没提交作业，
ROC 就自动收养它，而它的终态永远读不出——那个 run 永远不能 success，报错还说「作业仍在
运行」。改为：此刻仍永久读不出、且之前某个 run 已带着它以 blocked 如实封口的，不收养、
只披露；暂时读不出、或没人封口过的，照常收养。探针：
.hf-879-test/acceptance/review-0914-third/probe_zombie_adoption。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.blockers import record_blocker
from core.state import State
from nodes.experiment.tools import execution_route
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools.run_contract import _classify_experiment_scope
import test_operation_job_finalization as tojf

_SLURM_FORGOTTEN = {"ok": False, "returncode": 1, "stdout": "",
                    "stderr": "slurm_load_jobs error: Invalid job id specified"}
_SLURM_UNREACHABLE = {"ok": False, "returncode": 1, "stdout": "",
                      "stderr": "slurm_load_jobs error: Unable to contact slurm controller "
                                "(connect failure)"}
_SLURM_ENDED = {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
_LOCAL_RECORD_MISSING = {"ok": True, "returncode": 0, "stdout": "NOT_RUNNING", "stderr": "",
                         "sandbox_state": {"exists": False}}


def _health(raw: dict, phase: str) -> dict:
    return {"status": "success", "scheduler_phase": phase, "health_state": "unknown",
            "scheduler_result": {"raw": dict(raw)}}


_JOBS = {
    "slurm": ({"scheduler": "slurm", "job_id": "4242", "namespace": None,
               "launch_host": "login", "scheduler_cluster": "c",
               "resource_uid": "slurm-c-4242", "submission_nonce": "nonce-z"},
              _health(_SLURM_FORGOTTEN, "unknown"), "scheduler_reports_job_unknown"),
    "local": ({"scheduler": "local", "job_id": "hf-job-0123456789abcdef",
               "submission_nonce": "nonce-l", "container_runtime_id": "a" * 64},
              _health(_LOCAL_RECORD_MISSING, "terminal"), "local_record_missing"),
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf-home"))
    monkeypatch.setattr(rm, "_persist_execution_environment_evidence", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_route, "record_external_route_finalization",
                        lambda *_a, **_k: {"status": "success"})


def _workspace(tmp_path):
    worktree = tmp_path / "worktree"
    records = worktree / tojf._NODE_WORKSPACES["experiment"]
    records.mkdir(parents=True)
    return worktree, records


def _run_state(
    tmp_path,
    worktree,
    records,
    focus: str,
    *,
    operation_category: str = "job_observation",
) -> State:
    state = State.new("experiment", tmp_path / "runs")
    state.project_worktree = worktree
    state.workspace_records_dir = records
    state.project_root = tmp_path / "project"
    state.hook_state.setdefault("node_inputs", {
        "experiment_focus": focus,
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    })
    asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category=operation_category,
        reason=focus,
    ))
    return state


def _probe_returns(monkeypatch, health: dict) -> None:
    monkeypatch.setattr(rm, "probe_external_job_health", lambda *_a, **_k: dict(health))


def _submit(state: State, identity: dict) -> None:
    payload = {"status": "success", "dry_run": False, "execution_class": "diagnostic",
               "run_id": state.run_id, **identity}
    state.save_artifact("job_submission", "unreadable_job", json.dumps(payload))


def _seal_blocked(tmp_path, worktree, records, identity: dict) -> State:
    first = _run_state(tmp_path, worktree, records, "run 1 with an unreadable job")
    _submit(first, identity)
    finalized = asyncio.run(rm._finalize_external_job(
        first, identity["scheduler"], identity["job_id"], "", "operation_failed", note="z"))
    assert finalized.get("error_code") == "external_job_state_unknown", finalized
    blocker = record_blocker(first, category="external_job", summary="job state unreadable",
                             requested_action="verify the job in scheduler accounting")
    sealed = asyncio.run(oc._record_operation_completion(
        first, task_kind="external_job", objective="close honestly", outcome="blocked",
        blocker_id=(blocker or {}).get("blocker_id", "") if isinstance(blocker, dict) else ""))
    assert sealed["status"] == "success", sealed
    return first


def _unrelated_success(tmp_path, worktree, records):
    second = _run_state(
        tmp_path,
        worktree,
        records,
        "run 2 unrelated operation",
        operation_category="other",
    )
    evidence = tmp_path / "run2_output.log"
    evidence.write_text("ok\n", encoding="utf-8")
    result = asyncio.run(oc._record_operation_completion(
        second, task_kind="generic", objective="unrelated success", outcome="success",
        artifact_paths=[str(evidence)]))
    return second, result


@pytest.mark.parametrize("scheduler", sorted(_JOBS))
def test_a_permanently_unreadable_job_closed_honestly_no_longer_blocks_a_later_run(
    tmp_path, monkeypatch, scheduler,
):
    identity, health, observation = _JOBS[scheduler]
    worktree, records = _workspace(tmp_path)
    _probe_returns(monkeypatch, health)
    first = _seal_blocked(tmp_path, worktree, records, identity)

    second, result = _unrelated_success(tmp_path, worktree, records)

    assert result["status"] == "success", result
    assert result["outcome"] == "success", result
    disclosed = result["disclosed_unreadable_jobs"]
    assert [item["reference"]["job_id"] for item in disclosed] == [identity["job_id"]]
    assert disclosed[0]["state_observation"] == observation
    assert disclosed[0]["sealed_by_run_id"] == first.run_id
    assert disclosed[0]["suggested_owner"] == ("core" if scheduler == "local" else "run_owner")
    log = second.read_artifact(result["experiment_log_artifact_id"])
    assert log["metadata"]["operation_closure_input"]["disclosed_unreadable_jobs"] == disclosed
    # 它确实仍未决：其他消费方照旧看得到。
    assert [row["job_id"] for row in rm.unresolved_external_workflows(second)] == [
        identity["job_id"]]


def test_a_job_that_is_only_unreadable_for_now_is_still_adopted(tmp_path, monkeypatch):
    identity = _JOBS["slurm"][0]
    worktree, records = _workspace(tmp_path)
    _probe_returns(monkeypatch, _health(_SLURM_UNREACHABLE, "unknown"))
    _seal_blocked(tmp_path, worktree, records, identity)

    _second, result = _unrelated_success(tmp_path, worktree, records)

    assert result["status"] == "error", result
    assert result["error_code"] == "external_jobs_not_terminal"
    assert "仍在运行" not in result["error"]
    assert "读不出" in result["error"]


def test_a_job_sealed_while_temporarily_unreadable_is_skipped_once_it_is_permanently_gone(
    tmp_path, monkeypatch,
):
    """封口时只是暂时读不出（控制器连不上），之后才变成永久读不出（过了 MinJobAge 被清除）。
    此刻已永久读不出，收养它对谁都没有进展；条件②放宽为「封口时读不出（任一类）」
    （第三会话复审 review_commits_0914b_third.md cb69292b P3-1）。"""
    identity, forgotten, _observation = _JOBS["slurm"]
    worktree, records = _workspace(tmp_path)
    _probe_returns(monkeypatch, _health(_SLURM_UNREACHABLE, "unknown"))
    first = _seal_blocked(tmp_path, worktree, records, identity)
    _probe_returns(monkeypatch, forgotten)

    _second, result = _unrelated_success(tmp_path, worktree, records)

    assert result["status"] == "success", result
    disclosed = result["disclosed_unreadable_jobs"]
    assert disclosed[0]["sealed_state_observation"] == "state_unreadable"
    assert disclosed[0]["state_observation"] == "scheduler_reports_job_unknown"
    assert disclosed[0]["sealed_by_run_id"] == first.run_id


def test_a_forgotten_job_nobody_has_closed_honestly_is_still_adopted(tmp_path, monkeypatch):
    identity, health, _observation = _JOBS["slurm"]
    worktree, records = _workspace(tmp_path)
    _probe_returns(monkeypatch, health)
    _submit(_run_state(tmp_path, worktree, records, "run 1 crashed before closing"), identity)

    _second, result = _unrelated_success(tmp_path, worktree, records)

    assert result["status"] == "error", result
    assert result["error_code"] == "external_jobs_not_terminal"


def test_once_the_job_is_readable_again_a_later_run_adopts_it(tmp_path, monkeypatch):
    identity, health, _observation = _JOBS["slurm"]
    worktree, records = _workspace(tmp_path)
    _probe_returns(monkeypatch, health)
    _seal_blocked(tmp_path, worktree, records, identity)
    _probe_returns(monkeypatch, _health(_SLURM_ENDED, "terminal"))

    adoption = oc._adopt_unresolved_workflow_refs(
        _run_state(tmp_path, worktree, records, "run 2 after the scheduler recovered"))

    assert adoption["ok"] is True, adoption
    assert [ref["job_id"] for ref in adoption["refs"]] == [identity["job_id"]]
    assert "disclosed_unreadable_jobs" not in adoption
