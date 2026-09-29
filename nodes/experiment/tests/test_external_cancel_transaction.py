from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from core import sandbox
from core.sandbox import availability
from core.state import State
from core.tasks import TaskList
from nodes.experiment.tools import resource_manager as manager

requires_sandbox = pytest.mark.skipif(
    not availability()[0], reason="mandatory Docker sandbox is unavailable",
)
_RUNTIME_ID_A = "a" * 64
_RUNTIME_ID_B = "b" * 64
_REAL_OBSERVED_JOB_END = manager._observed_job_end


@pytest.fixture(autouse=True)
def _guard_sees_unknown_by_default(monkeypatch):
    """取消前会先读作业实况（不变量 6 守卫）。本文件多数夹具作业是假的，默认让守卫
    "读不出"而放行——行为与守卫之前一致；否则 slurm 夹具在装了 Slurm 的机器上会真去
    squeue 4242。专测守卫的用例自己把真实探针装回来。"""
    async def unknown(*_a, **_k):
        return None
    monkeypatch.setattr(manager, "_observed_job_end", unknown)


def _managed_container_snapshot(job_id: str, runtime_id: str) -> dict:
    from core import sandbox

    return {
        "exists": True,
        "id": runtime_id,
        "managed": True,
        "kind": "job",
        "namespace": sandbox.sandbox_namespace(),
        "running": True,
        "name": job_id,
    }


def _save_submission(
    state: State,
    *,
    scheduler: str = "slurm",
    job_id: str = "4242",
    namespace: str | None = None,
    launch_host: str | None = None,
    scheduler_cluster: str | None = "cluster-a",
    resource_uid: str | None = "resource-a",
    process_group_id: str | None = None,
    process_start_ticks: int | None = None,
    container_runtime_id: str | None = None,
    submission_nonce: str | None = None,
    sandbox_control_dir: str | None = None,
    submitted_at: str | None = None,
    command: str | None = None,
) -> dict:
    payload = {
        "status": "success",
        "dry_run": False,
        "scheduler": scheduler,
        "job_id": job_id,
        "namespace": namespace,
        "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster,
        "resource_uid": resource_uid,
        "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
        "submission_nonce": submission_nonce,
        "sandbox_control_dir": sandbox_control_dir,
        "output_roots": [str(state.root / "outputs")],
    }
    if submitted_at is not None:
        payload["submitted_at"] = submitted_at
    if command is not None:
        payload["command"] = command
    state.save_artifact(
        "job_submission", "managed_submission", json.dumps(payload),
    )
    return payload


def _save_lifecycle(state: State, submission: dict, status: str) -> None:
    manager._record_job_lifecycle(
        state,
        scheduler=submission["scheduler"],
        job_id=submission["job_id"],
        namespace=submission.get("namespace"),
        launch_host=submission.get("launch_host"),
        scheduler_cluster=submission.get("scheduler_cluster"),
        resource_uid=submission.get("resource_uid"),
        lifecycle_status=status,
        reason="fixture",
    )


@pytest.mark.parametrize(
    ("scheduler", "job_id", "expected_category"),
    [
        ("local", "hf-local-confirm", "取消本地受管作业"),
        ("slurm", "4242", "取消真实外部作业"),
    ],
)
def test_cancel_confirmation_category_matches_scheduler(
    tmp_path, monkeypatch, scheduler, job_id, expected_category,
):
    from shared.lib import dangerous_commands as danger

    state = State.new("experiment", tmp_path)
    _save_submission(
        state,
        scheduler=scheduler,
        job_id=job_id,
        container_runtime_id=(_RUNTIME_ID_A if scheduler == "local" else None),
        submission_nonce=f"{scheduler}-confirmation",
    )
    monkeypatch.setattr(danger, "bypass_enabled", lambda: False)
    monkeypatch.setattr(danger, "is_confirmed", lambda *_args: False)

    result = asyncio.run(manager._cancel_job(
        state, scheduler, job_id, reason="operator requested stop",
    ))

    assert result["status"] == "pause", result
    assert result["pause_event"]["metadata"] == {
        "type": "highrisk_confirm",
        "tool": "cancel_job",
        "category": expected_category,
    }
    assert state.list_artifacts("external_job_cancellation_intent") == []


def test_cancel_pause_preview_shows_elapsed_runtime_and_truncated_command(
    tmp_path, monkeypatch,
):
    from shared.lib import dangerous_commands as danger

    class ControlledDateTime(datetime):
        current = datetime(2026, 9, 18, 12, 13, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    state = State.new("experiment", tmp_path)
    command = "python simulate.py --payload " + "x" * 400
    _save_submission(
        state,
        submitted_at="2026-09-18T10:00:00+00:00",
        command=command,
    )
    monkeypatch.setattr(manager, "datetime", ControlledDateTime)
    monkeypatch.setattr(danger, "bypass_enabled", lambda: False)
    monkeypatch.setattr(danger, "is_confirmed", lambda *_args: False)

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="operator requested stop",
    ))

    assert result["status"] == "pause", result
    context = result["pause_event"]["context"]
    assert "已运行时长=2h13m" in context
    assert "命令预览=" in context
    assert command[:200] in context
    assert command not in context
    assert "已截断" in context
    pending = state.hook_state["_highrisk_pending_ask"]
    confirmation = json.loads(pending["text"])
    assert set(confirmation) == {
        "operation", "identity", "reason", "superseded_by",
    }
    assert "已运行时长" not in pending["text"]
    assert command not in pending["text"]


def test_cancel_pause_preview_marks_missing_runtime_and_command_unknown(
    tmp_path, monkeypatch,
):
    from shared.lib import dangerous_commands as danger

    state = State.new("experiment", tmp_path)
    _save_submission(state)
    monkeypatch.setattr(danger, "bypass_enabled", lambda: False)
    monkeypatch.setattr(danger, "is_confirmed", lambda *_args: False)

    result = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert result["status"] == "pause", result
    context = result["pause_event"]["context"]
    assert "已运行时长=未知" in context
    assert "命令预览=未知" in context


def test_cancel_approved_retry_consumes_confirmation_after_clock_advances(
    tmp_path, monkeypatch,
):
    from shared.lib import dangerous_commands as danger

    class ControlledDateTime(datetime):
        current = datetime(2026, 9, 18, 12, 13, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    state = State.new("experiment", tmp_path)
    _save_submission(
        state,
        submitted_at="2026-09-18T10:00:00+00:00",
        command="python simulate.py --steps 20",
    )
    cancel_calls = []
    monkeypatch.setattr(manager, "datetime", ControlledDateTime)
    monkeypatch.setattr(danger, "bypass_enabled", lambda: False)
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_args, **_kwargs: cancel_calls.append(1)
        or {"ok": True, "action": "scheduler_cancel"},
    )
    monkeypatch.setattr(
        manager, "_project_external_cancellation",
        lambda *_args, **_kwargs: {"status": "success"},
    )
    monkeypatch.setattr(
        manager, "_complete_handoff_tasks",
        lambda *_args, **_kwargs: {"status": "success", "completed_task_ids": []},
    )

    first = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="operator requested stop",
    ))
    assert first["status"] == "pause", first
    assert "已运行时长=2h13m" in first["pause_event"]["context"]
    pending = danger.pop_pending_ask(state)
    assert pending is not None
    confirmation = pending["text"]
    danger.mark_confirmed(state, confirmation)

    ControlledDateTime.current += timedelta(minutes=7)
    assert manager._cancel_elapsed_runtime_preview(
        "2026-09-18T10:00:00+00:00",
    ) == "2h20m"
    second = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="operator requested stop",
    ))

    assert second["status"] == "success", second
    assert cancel_calls == [1]
    assert danger.is_confirmed(state, confirmation) is False


@pytest.mark.parametrize("terminal", ["finalized", "finished", "failed"])
def test_terminal_job_never_regresses_to_cancelled(
    tmp_path, monkeypatch, terminal,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    _save_lifecycle(state, submission, terminal)
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync", lambda *_a, **_k: calls.append(1))

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="too late",
    ))

    assert result["status"] == "error"
    assert result["reason"] == "external_job_already_terminal"
    assert result["lifecycle_status"] == terminal
    assert not calls
    assert not state.list_artifacts("external_job_cancellation_intent")
    assert manager.lifecycle_for_submission(state, submission)["status"] == terminal


@pytest.mark.parametrize("terminal", ["cancelled", "superseded"])
def test_cancel_retry_is_idempotent_and_does_not_call_scheduler(
    tmp_path, monkeypatch, terminal,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    _save_lifecycle(state, submission, terminal)
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync", lambda *_a, **_k: calls.append(1))

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="retry",
    ))

    assert result["status"] == "success"
    assert result["idempotent"] is True
    assert result["lifecycle"]["lifecycle_status"] == terminal
    assert result["cancellation_intent_artifact_id"] == result["lifecycle"][
        "lifecycle_artifact_id"]
    assert not calls


def test_active_cancel_persists_full_intent_before_scheduler_and_projects_scope(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(
        state,
        namespace="science",
        launch_host="submit-a",
        process_group_id="9001",
        process_start_ticks=12345,
    )
    observed: dict = {}

    def cancel_sync(*_args, **_kwargs):
        intents = state.list_artifacts("external_job_cancellation_intent")
        assert len(intents) == 1
        observed["intent"] = json.loads(
            state.read_artifact(intents[0]["id"])["content"]
        )
        return {"ok": True, "action": "scheduler_cancel"}

    def route_projection(_state, **kwargs):
        observed["route"] = kwargs
        return {"status": "success", "attempt_id": "attempt-a"}

    monkeypatch.setattr(manager, "_cancel_sync", cancel_sync)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        route_projection,
    )
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", namespace="science",
        reason="bad convergence", superseded_by="job-43",
    ))

    assert result["status"] == "success"
    intent = observed["intent"]
    assert intent["reason"] == "bad convergence"
    assert intent["superseded_by"] == "job-43"
    assert intent["identity"] == {
        field: submission.get(field)
        for field in manager._CANCELLATION_IDENTITY_FIELDS
    }
    assert observed["route"] == {
        "scheduler": "slurm",
        "job_id": "4242",
        "namespace": "science",
        "launch_host": "submit-a",
        "scheduler_cluster": "cluster-a",
        "resource_uid": "resource-a",
        "submission_nonce": None,
        "process_group_id": "9001",
        "process_start_ticks": 12345,
        "container_runtime_id": None,
        "domain_outcome": "cancelled",
        "evidence_artifact_id": result["cancellation_intent_artifact_id"],
    }
    assert manager.lifecycle_for_submission(state, submission)["status"] == "superseded"


def test_unknown_cancel_outcome_blocks_retry_without_false_cancelled_lifecycle(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    calls = []

    def uncertain(*_args, **_kwargs):
        calls.append(1)
        return {
            "ok": False,
            "outcome_unknown": True,
            "error": "scheduler cancel timed out",
        }

    monkeypatch.setattr(manager, "_cancel_sync", uncertain)
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="stop runaway",
    ))
    second = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="stop runaway",
    ))

    assert first["status"] == "cancellation_outcome_unknown"
    assert first["do_not_repeat_cancel"] is True
    assert second["status"] == "cancellation_reconciliation_required"
    assert second["do_not_repeat_cancel"] is True
    assert calls == [1]
    assert manager.lifecycle_for_submission(state, submission)["status"] is None
    assert not state.list_artifacts("external_job_cancellation_recovery")
    outcomes = state.list_artifacts("external_job_cancellation_outcome")
    assert len(outcomes) == 1
    outcome_id = outcomes[0]["id"]
    intent_id = first["cancellation_intent_artifact_id"]
    assert first["cancellation_outcome_artifact_id"] == outcome_id
    assert second["cancellation_outcome_artifact_id"] == outcome_id
    blockers = [
        item for item in state.hook_state.get("blockers", [])
        if str(item.get("reported_by") or "").startswith(
            manager._CANCELLATION_RECONCILIATION_BLOCKER_PREFIX
        )
    ]
    assert len(blockers) == 1
    assert blockers[0]["evidence_paths"] == [outcome_id, intent_id]


@pytest.mark.parametrize("malformed", [{"not": "a-list"}, ["bad-entry"]])
def test_unknown_cancel_normalizes_malformed_blockers_and_records_exact_blocker(
    tmp_path, monkeypatch, malformed,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    state.hook_state["blockers"] = malformed
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: {"ok": False, "outcome_unknown": True,
                            "error": "scheduler response lost"},
    )
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="uncertain cancellation",
    ))

    assert result["status"] == "cancellation_outcome_unknown"
    assert result["do_not_repeat_cancel"] is True
    transaction = manager._latest_cancellation_transaction(state, submission)
    exact_reporter = manager._cancellation_reconciliation_reported_by(
        transaction["intent"]
    )
    exact = [
        item for item in state.hook_state["blockers"]
        if isinstance(item, dict) and item.get("reported_by") == exact_reporter
    ]
    assert len(exact) == 1
    assert exact[0]["evidence_paths"] == [
        result["cancellation_outcome_artifact_id"],
        result["cancellation_intent_artifact_id"],
    ]


def test_outcome_persistence_failure_uses_intent_to_block_repeat(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    calls = []
    original_save_artifact = state.save_artifact

    def fail_outcome(artifact_type, *args, **kwargs):
        if artifact_type == "external_job_cancellation_outcome":
            raise OSError("simulated outcome persistence failure")
        return original_save_artifact(artifact_type, *args, **kwargs)

    def cancelled(*_args, **_kwargs):
        calls.append(1)
        return {"ok": True, "action": "scheduler_cancel"}

    monkeypatch.setattr(state, "save_artifact", fail_outcome)
    monkeypatch.setattr(manager, "_cancel_sync", cancelled)
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="stop runaway",
    ))
    second = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="stop runaway",
    ))

    assert first["status"] == "cancellation_outcome_unknown"
    assert second["status"] == "cancellation_reconciliation_required"
    assert first["do_not_repeat_cancel"] is True
    assert second["do_not_repeat_cancel"] is True
    assert calls == [1]
    assert manager.lifecycle_for_submission(state, submission)["status"] is None
    assert not state.list_artifacts("external_job_cancellation_outcome")
    assert not state.list_artifacts("external_job_cancellation_recovery")
    intents = state.list_artifacts("external_job_cancellation_intent")
    assert len(intents) == 1
    intent_id = intents[0]["id"]
    blockers = [
        item for item in state.hook_state.get("blockers", [])
        if str(item.get("reported_by") or "").startswith(
            manager._CANCELLATION_RECONCILIATION_BLOCKER_PREFIX
        )
    ]
    assert len(blockers) == 1
    assert blockers[0]["evidence_paths"] == [intent_id]


def test_confirmed_outcome_repairs_lifecycle_without_repeating_scheduler(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    intent = manager._persist_cancellation_intent(
        state, submission, reason="recover after crash", superseded_by=None,
    )
    outcome = manager._persist_cancellation_outcome(
        state, intent, outcome="confirmed",
        cancel_result={"ok": True, "action": "scheduler_cancel"},
    )
    assert outcome is not None
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: pytest.fail("confirmed outcome must not be cancelled twice"),
    )

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="retry after crash",
    ))

    assert result["status"] == "success"
    assert result["idempotent"] is True
    assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled"


def test_rejected_cancel_retry_requires_remediation_without_second_scheduler_call(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _save_submission(state)
    calls = []

    def rejected(*_args, **_kwargs):
        calls.append(1)
        return {"ok": False, "outcome_unknown": False, "error": "permission denied"}

    monkeypatch.setattr(manager, "_cancel_sync", rejected)
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(state, "slurm", "4242"))
    second = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert first["reason"] == "external_job_cancellation_rejected"
    assert second["reason"] == "previous_cancellation_rejected"
    assert second["remediation_required"] is True
    assert calls == [1]


def test_corrupt_cancellation_ledger_fails_closed_without_scheduler_call(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _save_submission(state)
    state.save_artifact(
        "external_job_cancellation_intent", "corrupt_intent", "not-json",
    )
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: pytest.fail("corrupt ledger must block repeat cancellation"),
    )

    result = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="must not repeat",
    ))

    assert result["status"] == "error"
    assert result["reason"] == "cancellation_ledger_unreadable"


def test_confirmed_cancel_route_failure_retries_projection_without_second_cancel(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    submission = _save_submission(state)
    task = TaskList(state.project_root / "tasks").create(
        title="Finalize external job",
        owner_node="experiment",
        run_id=state.run_id,
        description=(
            "external_job_key=test\n"
            "scheduler=slurm\njob_id=4242\n"
            "scheduler_cluster=cluster-a\nresource_uid=resource-a\n"
        ),
    )
    cancel_calls = []

    def cancel_once(*_args, **_kwargs):
        cancel_calls.append(1)
        return {"ok": True, "action": "scheduler_cancel"}

    route_calls = []

    def project_route(*_args, **_kwargs):
        route_calls.append(1)
        if len(route_calls) == 1:
            return {"status": "pending", "reason": "receipt not durable"}
        return {"status": "success", "attempt_id": "attempt-cancel"}

    monkeypatch.setattr(manager, "_cancel_sync", cancel_once)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        project_route,
    )
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="stop runaway",
    ))

    assert first["status"] == "cancelled_needs_route_reconciliation"
    assert first["do_not_repeat_cancel"] is True
    assert first["do_not_resubmit"] is True
    assert cancel_calls == [1]
    assert route_calls == [1]
    assert first["route_projection"]["reason"] == "route_external_projection_not_durable"
    assert first["route_projection"]["upstream_status"] == "pending"
    assert manager.lifecycle_for_submission(state, submission)["status"] is None
    tasks = {item.id: item for item in TaskList(state.project_root / "tasks").list_all()}
    assert tasks[task.id].status == "pending"
    blockers = [
        item for item in state.hook_state.get("blockers", [])
        if str(item.get("reported_by") or "").startswith(
            manager._CANCELLATION_ROUTE_BLOCKER_PREFIX
        )
    ]
    assert len(blockers) == 1

    second = asyncio.run(manager._cancel_job(
        state, "slurm", "4242", reason="retry projection only",
    ))

    assert second["status"] == "success"
    assert second["idempotent"] is True
    assert cancel_calls == [1]
    assert route_calls == [1, 1]
    assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled"
    tasks = {item.id: item for item in TaskList(state.project_root / "tasks").list_all()}
    assert tasks[task.id].status == "completed"
    assert not any(
        str(item.get("reported_by") or "").startswith(
            manager._CANCELLATION_ROUTE_BLOCKER_PREFIX
        )
        for item in state.hook_state.get("blockers", [])
    )


def test_confirmed_cancel_task_failure_retries_without_second_cancel(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    cancel_calls = []
    task_calls = []

    def cancel_once(*_args, **_kwargs):
        cancel_calls.append(1)
        return {"ok": True, "action": "scheduler_cancel"}

    def complete_task(*_args, **_kwargs):
        task_calls.append(1)
        if len(task_calls) == 1:
            return {"status": "error", "reason": "task ledger unavailable"}
        return {"status": "success", "completed_task_ids": ["task-a"]}

    monkeypatch.setattr(manager, "_cancel_sync", cancel_once)
    monkeypatch.setattr(manager, "_complete_handoff_tasks", complete_task)
    monkeypatch.setattr(
        manager, "_project_external_cancellation",
        lambda *_a, **_k: {"status": "success", "attempt_id": "attempt-task"},
    )
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert first["status"] == "cancelled_needs_task_reconciliation"
    assert cancel_calls == [1]
    assert manager.lifecycle_for_submission(state, submission)["status"] is None
    task_reported_by = manager._external_job_needs_task_reported_by(submission)
    unrelated_reporter = f"{manager._EXTERNAL_JOB_NEEDS_TASK_PREFIX}unrelated"
    state.hook_state["blockers"].append({
        "reported_by": unrelated_reporter, "reason": "unrelated",
    })
    assert any(
        isinstance(item, dict) and item.get("reported_by") == task_reported_by
        for item in state.hook_state["blockers"]
    )

    second = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert second["status"] == "success"
    assert second["idempotent"] is True
    assert cancel_calls == [1]
    assert task_calls == [1, 1]
    assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled"
    reporters = {
        item.get("reported_by") for item in state.hook_state["blockers"]
        if isinstance(item, dict)
    }
    assert task_reported_by not in reporters
    assert unrelated_reporter in reporters


@pytest.mark.parametrize("ledger_kind", ["dict", "tuple"])
def test_confirmed_retry_normalizes_malformed_blockers_and_resolves_only_exact(
    tmp_path, monkeypatch, ledger_kind,
):
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    intent = manager._persist_cancellation_intent(
        state, submission, reason="recover closure", superseded_by=None,
    )
    outcome = manager._persist_cancellation_outcome(
        state, intent, outcome="confirmed",
        cancel_result={"ok": True, "action": "scheduler_cancel"},
    )
    assert outcome is not None
    unrelated = {"reported_by": "framework:unrelated", "reason": "keep-me"}
    state.hook_state["blockers"] = (
        unrelated if ledger_kind == "dict" else (unrelated,)
    )
    task_calls = []

    def complete_task(*_args, **_kwargs):
        task_calls.append(1)
        if len(task_calls) == 1:
            return {"status": "error", "reason": "task ledger unavailable"}
        return {"status": "success", "completed_task_ids": []}

    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: pytest.fail("confirmed retry must not cancel twice"),
    )
    monkeypatch.setattr(manager, "_complete_handoff_tasks", complete_task)
    monkeypatch.setattr(
        manager, "_project_external_cancellation",
        lambda *_a, **_k: {"status": "success", "attempt_id": "attempt-retry"},
    )

    first = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert first["status"] == "cancelled_needs_task_reconciliation"
    assert isinstance(state.hook_state["blockers"], list)
    exact_task = manager._external_job_needs_task_reported_by(submission)
    exact_route = manager._cancellation_route_reported_by(submission)
    exact_unknown = manager._cancellation_reconciliation_reported_by(intent)
    state.hook_state["blockers"].extend([
        {"reported_by": exact_route, "reason": "route"},
        {"reported_by": exact_unknown, "reason": "unknown"},
    ])
    before_retry = {
        item.get("reported_by") for item in state.hook_state["blockers"]
        if isinstance(item, dict)
    }
    assert {exact_task, exact_route, exact_unknown}.issubset(before_retry)
    assert manager._BLOCKER_LEDGER_RECOVERY_MARKER in before_retry
    assert unrelated["reported_by"] in before_retry

    second = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert second["status"] == "success"
    assert second["idempotent"] is True
    after_retry = {
        item.get("reported_by") for item in state.hook_state["blockers"]
        if isinstance(item, dict)
    }
    assert exact_task not in after_retry
    assert exact_route not in after_retry
    assert exact_unknown not in after_retry
    assert manager._BLOCKER_LEDGER_RECOVERY_MARKER in after_retry
    assert unrelated["reported_by"] in after_retry


def test_local_cleanup_failure_stops_before_route_task_and_lifecycle_and_retries(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    job_id = "hf-harness-cancel-cleanup"
    submission = _save_submission(
        state, scheduler="local", job_id=job_id, scheduler_cluster=None,
        resource_uid=None, submission_nonce="cancel-cleanup",
        container_runtime_id=_RUNTIME_ID_A,
        sandbox_control_dir=str(tmp_path / "control"),
    )
    cancel_calls = []
    cleanup_calls = []
    downstream_calls = []

    def cancel_once(*_args, **_kwargs):
        cancel_calls.append(1)
        return {"ok": True, "action": "sandbox_stop"}

    def cleanup(_record):
        cleanup_calls.append(1)
        if len(cleanup_calls) == 1:
            return {"status": "error", "error": "control dir busy"}
        return {"status": "success", "action": "cleanup"}

    monkeypatch.setattr(manager, "_cancel_sync", cancel_once)
    monkeypatch.setattr(manager, "_cleanup_local_job_for_finalization", cleanup)
    monkeypatch.setattr(
        manager, "_project_external_cancellation",
        lambda *_a, **_k: downstream_calls.append("route") or {"status": "success"},
    )
    monkeypatch.setattr(
        manager, "_complete_handoff_tasks",
        lambda *_a, **_k: downstream_calls.append("task") or {"status": "success"},
    )
    original_lifecycle = manager._record_job_lifecycle

    def record_lifecycle(*args, **kwargs):
        downstream_calls.append("lifecycle")
        return original_lifecycle(*args, **kwargs)

    monkeypatch.setattr(manager, "_record_job_lifecycle", record_lifecycle)
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    first = asyncio.run(manager._cancel_job(state, "local", job_id))

    assert first["status"] == "cancelled_needs_cleanup"
    assert first["do_not_repeat_cancel"] is True
    assert cancel_calls == [1]
    assert cleanup_calls == [1]
    assert downstream_calls == []
    assert manager.lifecycle_for_submission(state, submission)["status"] is None

    second = asyncio.run(manager._cancel_job(state, "local", job_id))

    assert second["status"] == "success"
    assert second["idempotent"] is True
    assert cancel_calls == [1]
    assert cleanup_calls == [1, 1]
    assert downstream_calls == ["route", "task", "lifecycle"]


def test_local_cancel_rejects_missing_immutable_container_id_without_stop(
    tmp_path, monkeypatch,
):
    from core import sandbox

    state = State.new("experiment", tmp_path)
    job_id = "hf-harness-missing-runtime-id"
    submission = _save_submission(
        state,
        scheduler="local",
        job_id=job_id,
        scheduler_cluster=None,
        resource_uid=None,
        container_runtime_id=None,
    )
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda _name: _managed_container_snapshot(job_id, _RUNTIME_ID_A),
    )
    monkeypatch.setattr(
        sandbox, "stop_container",
        lambda *_a, **_k: pytest.fail("missing immutable ID must not stop"),
    )
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    result = asyncio.run(manager._cancel_job(
        state, "local", job_id, reason="missing immutable identity",
    ))

    assert result["status"] == "error"
    assert result["reason"] == "external_job_cancellation_rejected"
    assert "缺少不可变 Docker container ID" in result["error"]
    assert manager.lifecycle_for_submission(state, submission)["status"] is None


def test_local_read_uses_durable_container_id_without_cancel(
    tmp_path, monkeypatch,
):
    from core import sandbox

    state = State.new("experiment", tmp_path)
    job_id = "hf-harness-read-only-status"
    submission = _save_submission(
        state,
        scheduler="local",
        job_id=job_id,
        scheduler_cluster=None,
        resource_uid=None,
        container_runtime_id=_RUNTIME_ID_A,
    )
    inspected: list[str] = []

    def inspect(name):
        inspected.append(name)
        return _managed_container_snapshot(job_id, _RUNTIME_ID_A)

    monkeypatch.setattr(sandbox, "inspect_container", inspect)
    monkeypatch.setattr(
        sandbox, "stop_container",
        lambda *_a, **_k: pytest.fail("read-only status must not cancel"),
    )

    result = asyncio.run(manager._job_status(state, "local", job_id))

    assert result["status"] == "success"
    assert result["raw"]["stdout"] == "RUNNING"
    assert result["raw"]["sandbox_state"]["id"] == _RUNTIME_ID_A
    assert inspected == [job_id]
    assert submission["container_runtime_id"] == _RUNTIME_ID_A
    assert state.list_artifacts("external_job_cancellation_intent") == []


def test_local_record_rejects_explicit_container_runtime_id_conflict(tmp_path):
    state = State.new("experiment", tmp_path)
    job_id = "hf-harness-runtime-id-conflict"
    _save_submission(
        state,
        scheduler="local",
        job_id=job_id,
        scheduler_cluster=None,
        resource_uid=None,
        container_runtime_id=_RUNTIME_ID_A,
    )

    record, error = manager._authoritative_record_for_read(
        state, "local", job_id, None,
    )
    exact = manager._external_job_record(
        state, "local", job_id, container_runtime_id=_RUNTIME_ID_A,
    )
    conflict = manager._external_job_record(
        state, "local", job_id, container_runtime_id=_RUNTIME_ID_B,
    )

    assert error is None
    assert record is not None
    assert record["container_runtime_id"] == _RUNTIME_ID_A
    assert exact is not None
    assert conflict is None


def test_local_cancel_rejects_reused_container_name_id_mismatch(monkeypatch):
    from core import sandbox

    job_id = "hf-harness-reused-container-name"
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda _name: _managed_container_snapshot(job_id, _RUNTIME_ID_A),
    )
    monkeypatch.setattr(
        sandbox, "stop_container",
        lambda *_a, **_k: pytest.fail("mismatched immutable ID must not stop"),
    )

    result = manager._cancel_sync(
        "local", job_id, None, container_runtime_id=_RUNTIME_ID_B,
    )

    assert result["ok"] is False
    assert "不可变 container ID 不匹配" in result["error"]


@requires_sandbox
def test_managed_local_cancel_terminates_only_exact_container_id(tmp_path):
    from core import sandbox

    state = State.new("experiment", tmp_path)
    runtime = manager.experiment_output_dir(
        state, "runtime", create=True,
    ).resolve()
    submission: dict = {}
    try:
        submission = manager._submit_sync(
            runtime_root=runtime,
            scheduler="local",
            command="sleep 30",
            job_name="cancel-exact-id",
            mpi_ranks=1,
            cpus_per_rank=1,
            gpus=0,
            memory_gb=1.0,
            storage_gb=1.0,
            walltime_minutes=1,
            queue=None,
            nodelist=None,
            image=None,
            workdir=str(runtime),
            dry_run=False,
            namespace=None,
            output_paths=[str(runtime)],
            stage_in=None,
            state=state,
            submission_nonce="cancel-exact-container",
            route_attempt_id="cancel-exact-container",
            hard_deadline_s=45,
        )
        assert submission["status"] == "success", submission
        job_id = submission["job_id"]
        runtime_id = submission["container_runtime_id"]
        assert len(runtime_id) == 64
        wrong_id = _RUNTIME_ID_B if runtime_id != _RUNTIME_ID_B else _RUNTIME_ID_A

        refused = manager._cancel_sync(
            "local", job_id, None, container_runtime_id=wrong_id,
        )
        assert refused["ok"] is False
        inspected = sandbox.inspect_container(job_id)
        assert inspected["exists"] is True
        assert inspected["id"] == runtime_id

        cancelled = manager._cancel_sync(
            "local", job_id, None, container_runtime_id=runtime_id,
        )
        assert cancelled["ok"] is True, cancelled
        assert cancelled["action"] == "sandbox_stop"
        assert cancelled["already_absent"] is False
        assert sandbox.inspect_container(job_id)["exists"] is False
    finally:
        job_id = submission.get("job_id")
        runtime_id = submission.get("container_runtime_id")
        if job_id and runtime_id:
            sandbox.stop_container(
                str(job_id), remove=True,
                expected_container_id=str(runtime_id),
            )
        control_dir = submission.get("sandbox_control_dir")
        if control_dir:
            sandbox.cleanup_control_dir(str(control_dir))


def test_local_reused_container_name_does_not_alias_lifecycle_or_record_identity(
    tmp_path,
):
    state = State.new("experiment", tmp_path)
    container_name = "hf-harness-reused-container-name"
    common = {
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": container_name,
    }
    old = {
        **common,
        "submission_nonce": "old-submission",
        "container_runtime_id": _RUNTIME_ID_A,
    }
    new = {
        **common,
        "submission_nonce": "new-submission",
        "container_runtime_id": _RUNTIME_ID_B,
    }
    state.save_artifact("job_submission", "old", json.dumps(old))
    state.save_artifact("job_submission", "new", json.dumps(new))
    manager._record_job_lifecycle(
        state,
        scheduler="local",
        job_id=container_name,
        submission_nonce="old-submission",
        container_runtime_id=_RUNTIME_ID_A,
        lifecycle_status="finalized",
    )

    assert manager.lifecycle_for_submission(state, old)["status"] == "finalized"
    assert manager.lifecycle_for_submission(state, new)["status"] is None
    assert manager._external_job_record(state, "local", container_name) is None
    resolved = manager._external_job_record(
        state,
        "local",
        container_name,
        submission_nonce="new-submission",
        container_runtime_id=_RUNTIME_ID_B,
    )
    mismatched_identity = manager._external_job_record(
        state,
        "local",
        container_name,
        submission_nonce="new-submission",
        container_runtime_id=_RUNTIME_ID_A,
    )

    assert resolved is not None
    assert resolved["submission_nonce"] == "new-submission"
    assert resolved["container_runtime_id"] == _RUNTIME_ID_B
    assert mismatched_identity is None

# ── 不变量 6：自己已经结束的作业不能被记成 cancelled ────────────────────────────
#
# 原先只看**记录的** lifecycle：作业自然结束、还没 finalize 时 lifecycle 仍是空，
# 于是一路走到调度器 kill。本地原生后端对已退出的作业照样"停止成功"，删掉唯一记着
# 退出码的 record.json，再写 cancelled——一个跑完的作业被记成取消，且无法回头。


def _submit_local(state, tmp_path, command, name):
    runtime = manager.experiment_output_dir(state, "runtime", create=True).resolve()
    submission = manager._submit_sync(
        runtime_root=runtime, scheduler="local", command=command,
        job_name=name, mpi_ranks=1, cpus_per_rank=1, gpus=0,
        memory_gb=1.0, storage_gb=1.0, walltime_minutes=1, queue=None,
        nodelist=None, image=None, workdir=str(runtime), dry_run=False,
        namespace=None, output_paths=[str(runtime)], stage_in=None, state=state,
        submission_nonce=name, route_attempt_id=name, hard_deadline_s=45,
    )
    assert submission["status"] == "success", submission
    state.save_artifact("job_submission", name.replace("-", "_"), json.dumps(submission))
    return submission


def _wait_exited(runtime_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = sandbox.inspect_container(runtime_id)
        if observed.get("status") == "exited":
            return observed
        time.sleep(0.05)
    raise AssertionError(sandbox.inspect_container(runtime_id))


def _cleanup(submission):
    job_id = submission.get("job_id")
    runtime_id = submission.get("container_runtime_id")
    if job_id and runtime_id:
        sandbox.stop_container(str(job_id), remove=True, expected_container_id=str(runtime_id))
    if submission.get("sandbox_control_dir"):
        sandbox.cleanup_control_dir(str(submission["sandbox_control_dir"]))


@requires_sandbox
@pytest.mark.parametrize("bypass", [True, False])
@pytest.mark.parametrize("command, exit_code", [("printf complete > done.txt", 0), ("exit 7", 7)])
def test_exited_job_is_never_recorded_cancelled(tmp_path, monkeypatch, command, exit_code, bypass):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: bypass)
    state = State.new("experiment", tmp_path / "run")
    submission: dict = {}
    try:
        submission = _submit_local(state, tmp_path, command, f"exited-{exit_code}")
        runtime_id = submission["container_runtime_id"]
        assert _wait_exited(runtime_id)["exit_code"] == exit_code
        assert manager.lifecycle_for_submission(state, submission)["status"] is None

        result = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="planned stop"))

        assert result["status"] == "already_ended", result
        assert result["reason"] == "external_job_already_ended"
        assert result["cancelled"] is False
        assert result["observed_end"]["source"] == "native_job_record"
        assert result["observed_end"]["exit_code"] == exit_code
        assert result["next_tool"]["name"] == "finalize_external_job"
        assert manager.lifecycle_for_submission(state, submission)["status"] is None
        assert not state.list_artifacts("external_job_cancellation_intent")
        assert not state.list_artifacts("external_job_cancellation_outcome")
        assert "_highrisk_pending" not in json.dumps(list(state.hook_state))  # no card
        after = sandbox.inspect_container(runtime_id)
        assert after["exists"] is True and after["exit_code"] == exit_code
    finally:
        _cleanup(submission)


@requires_sandbox
def test_running_job_is_still_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    state = State.new("experiment", tmp_path / "run")
    submission: dict = {}
    try:
        submission = _submit_local(state, tmp_path, "sleep 30", "running-cancel")
        assert sandbox.inspect_container(submission["container_runtime_id"])["running"] is True

        result = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="planned stop"))

        assert result["status"] == "success", result
        assert result["cancel"]["action"] == "sandbox_stop"
        assert manager.lifecycle_for_submission(state, submission)["status"] == "cancelled"
        assert sandbox.inspect_container(submission["container_runtime_id"])["exists"] is False
    finally:
        _cleanup(submission)


@requires_sandbox
def test_job_ending_after_probe_is_not_signalled_or_recorded_cancelled(tmp_path, monkeypatch):
    """Race: probe sees running, job exits before the kill -> not_sent, evidence kept.

    原先持久化成 rejected（"调度器明确拒绝"），之后再调取消就停在那句不实的话上
    （2026-09-13 审查）。现在如实记 not_sent，重调幂等地指回收尾。
    """
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    state = State.new("experiment", tmp_path / "run")
    submission: dict = {}
    try:
        submission = _submit_local(state, tmp_path, "exit 3", "race-exit")
        runtime_id = submission["container_runtime_id"]
        assert _wait_exited(runtime_id)["exit_code"] == 3

        async def stale_probe(*_a, **_k):
            return None  # what the guard saw a moment before the job exited
        monkeypatch.setattr(manager, "_observed_job_end", stale_probe)

        result = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="planned stop"))

        assert result["status"] == "already_ended", result
        assert result["reason"] == "external_job_already_ended"
        assert result["cancelled"] is False
        assert result["next_tool"]["name"] == "finalize_external_job"
        assert manager.lifecycle_for_submission(state, submission)["status"] is None
        outcomes = state.list_artifacts("external_job_cancellation_outcome")
        assert len(outcomes) == 1
        payload = json.loads(state.read_artifact(outcomes[0]["id"])["content"])
        assert payload["outcome"] == "not_sent"
        assert payload["cancel_result"]["sandbox_state"]["exit_code"] == 3
        assert sandbox.inspect_container(runtime_id)["exit_code"] == 3  # record kept

        again = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="planned stop"))
        assert again["status"] == "already_ended", again
        assert again.get("idempotent") is True
        assert len(state.list_artifacts("external_job_cancellation_outcome")) == 1
    finally:
        _cleanup(submission)


@pytest.mark.parametrize("raw, accounting, proceeds", [
    # squeue 查不到 + 记账库说终止 → 确实结束了，不取消
    ({"ok": True, "stdout": "", "stderr": ""},
     {"ok": True, "stdout": "4242|COMPLETED|0:0||||||\n"}, False),
    # squeue 查不到、但记账库问不到 → 只说明队列里没有它，不是终止证据，取消照旧可行
    ({"ok": True, "stdout": "", "stderr": ""},
     {"ok": False, "stdout": "", "stderr": "sacct: not found"}, True),
    # 还在排队/运行
    ({"ok": True, "stdout": "4242|RUNNING|1:00|n1", "stderr": ""}, None, True),
    # 查询本身失败
    ({"ok": False, "returncode": 1, "stdout": "", "stderr": "Invalid job id"}, None, True),
])
def test_remote_guard_refuses_only_observed_terminal(
    tmp_path, monkeypatch, raw, accounting, proceeds,
):
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    state = State.new("experiment", tmp_path)
    submission = _save_submission(state)
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success" if raw["ok"] else "error", "raw": raw})
    if accounting is not None:
        monkeypatch.setattr(manager, "_run", lambda *_a, **_k: accounting)
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync",
                        lambda *_a, **_k: calls.append(_k) or {"ok": True, "action": "scheduler_cancel"})
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    if proceeds:
        assert result["status"] == "success", result
        assert calls and calls[0]["refuse_if_ended"] is True
    else:
        assert result["status"] == "already_ended", result
        assert not calls
        assert manager.lifecycle_for_submission(state, submission)["status"] is None


def test_closure_receipt_proves_end_even_without_live_record(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    state = State.new("experiment", tmp_path)
    _save_submission(state)
    monkeypatch.setattr(manager, "_operation_closure_receipt", lambda *_a, **_k: {
        "status": "success", "artifact_id": "receipt-1",
        "payload": {"outcome": "operation_completed", "health": {"exit_code": 0}}})
    monkeypatch.setattr(manager, "_job_status_sync",
                        lambda *_a, **_k: pytest.fail("receipt answers first"))
    monkeypatch.setattr(manager, "_cancel_sync", lambda *_a, **_k: pytest.fail("no cancel"))

    result = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert result["status"] == "already_ended"
    assert result["observed_end"]["source"] == "operation_closure_receipt"
    assert result["observed_end"]["recorded_outcome"] == "operation_completed"




@pytest.mark.parametrize("sandbox_state, proceeds", [
    ({"exists": True, "running": True, "status": "running", "exit_code": None}, True),
    ({"exists": True, "running": False, "status": "created", "exit_code": None}, True),
    ({"exists": True, "running": False, "status": "dead", "exit_code": None}, True),   # supervisor vanished
    ({"exists": False}, True),                                                          # record absent
    ({"exists": True, "running": False, "status": "dead", "exit_code": 127}, False),   # launch failure
    ({"exists": True, "running": False, "status": "exited", "exit_code": 0}, False),
])
def test_local_guard_state_table(tmp_path, monkeypatch, sandbox_state, proceeds):
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    state = State.new("experiment", tmp_path)
    payload = {"status": "success", "dry_run": False, "scheduler": "local",
               "job_id": "hf-job-0123456789abcdef", "submission_nonce": "n",
               "container_runtime_id": _RUNTIME_ID_A, "output_roots": [str(state.root / "outputs")]}
    state.save_artifact("job_submission", "managed_submission", json.dumps(payload))
    stdout = "RUNNING" if sandbox_state.get("running") else "NOT_RUNNING"
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success", "raw": {"ok": True, "stdout": stdout, "sandbox_state": sandbox_state}})
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync",
                        lambda *_a, **_k: calls.append(1) or {"ok": True, "action": "sandbox_stop"})
    monkeypatch.setattr(manager, "_cleanup_local_job_for_finalization",
                        lambda _r: {"status": "success"})
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(state, "local", payload["job_id"]))

    if proceeds:
        assert calls == [1] and result["status"] == "success", result
    else:
        assert not calls and result["status"] == "already_ended", result
        assert result["observed_end"]["exit_code"] == sandbox_state["exit_code"]


def test_probe_failure_is_unknown_and_cancel_stays_possible(tmp_path, monkeypatch):
    """读实况本身出错 ≠ 作业已结束：取消必须仍然可行（不变量 8）。"""
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    state = State.new("experiment", tmp_path)
    _save_submission(state)

    def broken(*_a, **_k):
        raise RuntimeError("squeue hung up")

    monkeypatch.setattr(manager, "_job_status_sync", broken)
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync",
                        lambda *_a, **_k: calls.append(_k) or {"ok": True, "action": "scheduler_cancel"})
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(state, "slurm", "4242"))

    assert result["status"] == "success", result
    assert len(calls) == 1


@pytest.mark.parametrize("status, proceeds", [
    # 重试退避窗口：一个 pod 失败了，下一个还没起来，active 缺席。作业还活着。
    ({"failed": 1}, True),
    # 作业自己的终止信号
    ({"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}, False),
    ({"succeeded": 1, "completionTime": "2026-09-12T00:00:00Z"}, False),
    # 条件还没置位、也没有完成时刻 → 读不出，取消照旧可行
    ({"succeeded": 1}, True),
])
def test_a_kubernetes_job_between_retries_is_not_mistaken_for_ended(
    tmp_path, monkeypatch, status, proceeds,
):
    """`调度器不再持有它` ≠ `它自己结束了`。

    k8s 作业在重试退避窗口里 status 是 {failed: 1} 且 active 缺席，_scheduler_phase 就报
    terminal。若守卫照单全收，一个还在跑、马上要起下一个 pod 的作业就取消不掉了
    （不变量 8）。远端要的是作业自己的终止信号：Complete/Failed 条件或 completionTime。
    """
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    state = State.new("experiment", tmp_path)
    submission = _save_submission(
        state, scheduler="kubernetes", job_id="probe", namespace="research")
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": json.dumps({"status": status})}})
    calls = []
    monkeypatch.setattr(manager, "_cancel_sync",
                        lambda *_a, **_k: calls.append(1) or {"ok": True, "action": "scheduler_cancel"})
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(
        state, "kubernetes", submission["job_id"], namespace="research"))

    if proceeds:
        assert calls == [1], result
        assert result["status"] == "success", result
    else:
        assert not calls, result
        assert result["status"] == "already_ended", result
