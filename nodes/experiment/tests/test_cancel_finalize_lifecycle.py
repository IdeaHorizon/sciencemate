"""取消与收尾对同一个作业终态的两处缺口（verify_third_review_step23_0914.md 清单 #13、#14；
第三会话复审 review_commits_0914b_third.md §三实测，探针 review-0914-third/probe_checklist_13_14）。

- #13：取消结果未知时登记的 blocker，作业自行结束、finalize 成功后仍残留；之后 cancel_job 因
  lifecycle 已是终态被拒，清不掉它；core 只要有 blocker 就把 run 记 blocked——没有出口。
- #14：取消确认后再 finalize 仍成功，lifecycle 从 cancelled 被改写成 finalized，还铸出一份
  operation 收据；与 cancel 一侧「lifecycle 单调」相悖。
"""
from __future__ import annotations

import asyncio

import pytest

from nodes.experiment.tools import resource_manager as manager
from test_operation_job_finalization import (
    _finalize, _health, _patch_pipeline, _prepared_state, _submission,
)


def _scheduler_doubles(monkeypatch, cancel_result: dict) -> None:
    async def unknown(*_a, **_k):
        return None
    monkeypatch.setattr(manager, "_observed_job_end", unknown)
    monkeypatch.setattr(manager, "_cancel_sync", lambda *_a, **_k: dict(cancel_result))
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})


def _cancellation_blockers(state) -> list[dict]:
    return [item for item in state.hook_state.get("blockers", []) or []
            if isinstance(item, dict) and str(item.get("reported_by") or "").startswith(
                manager._CANCELLATION_RECONCILIATION_BLOCKER_PREFIX)]


_CONFIRMED = {"ok": True, "action": "scheduler_cancel", "outcome_unknown": False,
              "result": {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}}
_UNKNOWN = {"ok": False, "outcome_unknown": True, "error": "scheduler cancel timed out"}


@pytest.mark.parametrize("outcome", ["operation_failed", "operation_blocked"])
def test_finalize_does_not_rewrite_a_confirmed_cancellation(tmp_path, monkeypatch, outcome):
    submission = _submission(scheduler_cluster="cluster-a", resource_uid="resource-a")
    state = _prepared_state(tmp_path, submission)
    _scheduler_doubles(monkeypatch, _CONFIRMED)
    cancelled = asyncio.run(manager._cancel_job(state, "slurm", submission["job_id"], reason="stop"))
    assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled", cancelled
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 3}}}))
    if outcome == "operation_blocked":
        from core.blockers import record_blocker
        record_blocker(state, category="external_job", summary="cancelled then blocked",
                       requested_action="none")

    result = _finalize(state, submission, outcome, note="after cancel")

    # 与 cancel 一侧的幂等重放一致：success + idempotent（第三会话复审 0914c P3）。
    assert result["status"] == "success", result
    assert result["already_cancelled"] is True and result["idempotent"] is True
    assert result["reason"] == "external_job_lifecycle_already_cancelled"
    assert result["cancellation_outcome_artifact_id"], result
    assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled"
    assert state.list_artifacts("external_job_operation_closure") == []


def test_finalizing_a_job_that_ended_after_an_unknown_cancellation_clears_that_blocker(
    tmp_path, monkeypatch,
):
    submission = _submission(scheduler_cluster="cluster-a", resource_uid="resource-a")
    state = _prepared_state(tmp_path, submission)
    _scheduler_doubles(monkeypatch, _UNKNOWN)
    unknown = asyncio.run(manager._cancel_job(state, "slurm", submission["job_id"], reason="stop"))
    assert unknown["status"] == "cancellation_outcome_unknown", unknown
    assert len(_cancellation_blockers(state)) == 1
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"sandbox_state": {"exit_code": 3}}}))

    finalized = _finalize(state, submission, "operation_failed", note="job ended on its own")

    assert finalized["status"] == "success", finalized
    assert manager.lifecycle_for_submission(state, submission)["status"] == "finalized"
    assert _cancellation_blockers(state) == []
    assert '"reason": "external_job_finalized_after_unknown_cancellation"' in (
        state.transcript_path.read_text(encoding="utf-8"))


def test_an_unknown_cancellation_blocker_stays_while_the_job_is_not_finalized(
    tmp_path, monkeypatch,
):
    submission = _submission(scheduler_cluster="cluster-a", resource_uid="resource-a")
    state = _prepared_state(tmp_path, submission)
    _scheduler_doubles(monkeypatch, _UNKNOWN)
    asyncio.run(manager._cancel_job(state, "slurm", submission["job_id"], reason="stop"))
    _patch_pipeline(monkeypatch, _health(scheduler_phase="running"))

    refused = _finalize(state, submission, "operation_failed", note="still running")

    assert refused["status"] == "error", refused
    assert len(_cancellation_blockers(state)) == 1
