"""Regression coverage for the single current-run owed-closure judgement."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import data_provenance
from core.loop_hooks import HookContext
from core.state import State
from core.tasks import TaskList
from nodes.experiment import hooks
from nodes.experiment.tools import resource_manager as manager
from nodes.experiment.tools.contract_audit import TERMINAL_CLOSURE_REGISTRY
from nodes.experiment.tools import sediment


def _workspace_state(
    tmp_path: Path,
    worktree: Path,
    records: Path,
) -> State:
    state = State.new("experiment", tmp_path / "runs")
    state.project_worktree = worktree
    state.workspace_records_dir = records
    state.project_root = None
    return state


def _shared_workspace(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "worktree"
    records = worktree / "nodes" / "experiment"
    records.mkdir(parents=True)
    return worktree, records


def _submission(job_id: str) -> dict:
    return {
        "status": "success",
        "dry_run": False,
        "scheduler": "local",
        "job_id": job_id,
        "job_name": job_id,
        "workdir": "/tmp/c6-test",
        "output_roots": ["/tmp/c6-test"],
    }


def _unresolved(status: str) -> dict:
    return {
        "status": status,
        "dry_run": False,
        "scheduler": "local",
        "job_id": None,
        "do_not_resubmit": True,
    }


def _complete_terminal_audit() -> dict[str, dict]:
    return {
        key: {"passed": True, "applicable": True, "reason": "ok"}
        for key in TERMINAL_CLOSURE_REGISTRY
    }


def _patch_preview_observers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sediment, "audit_experiment_contract",
        lambda _state: _complete_terminal_audit(),
    )
    monkeypatch.setattr(data_provenance, "undeclared_stale_inputs", lambda _state: [])

    def health(*_args, **_kwargs):
        return {
            "status": "success",
            "scheduler_phase": "running",
            "health_state": "healthy",
            "workflow_status": "awaiting_external_job",
        }

    monkeypatch.setattr(manager, "probe_external_job_health", health)
    try:
        runtime_manager = importlib.import_module("tools.resource_manager")
    except ImportError:
        runtime_manager = manager
    if runtime_manager is not manager:
        monkeypatch.setattr(runtime_manager, "probe_external_job_health", health)


def _ctx(state: State) -> HookContext:
    return HookContext(harness=None, state=state, messages=[], turn=1)


def test_current_run_preview_and_on_end_share_identity_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, records = _shared_workspace(tmp_path)
    foreign = _workspace_state(tmp_path / "foreign", worktree, records)
    foreign.save_artifact(
        "job_submission", "foreign_running", json.dumps(_submission("foreign-running")))

    current = _workspace_state(tmp_path / "current", worktree, records)
    current.save_artifact(
        "job_submission", "own_running", json.dumps(_submission("own-running")))
    current.save_artifact(
        "job_submission", "own_cancelled", json.dumps(_submission("own-cancelled")))
    current.save_artifact("external_job_lifecycle", "own_cancelled", json.dumps({
        "scheduler": "local",
        "job_id": "own-cancelled",
        "lifecycle_status": "cancelled",
    }))
    _patch_preview_observers(monkeypatch)

    judged = manager.owed_external_job_closure_records(current)
    assert [(row["scheduler"], row["job_id"]) for row in judged] == [
        ("local", "own-running")]
    assert hooks._job_submission_records(current) == judged
    assert hooks._read_submission_records_strict(
        current) == manager.read_owed_submission_receipts(current)

    preview = asyncio.run(sediment._preview_experiment_contract(current))
    assert [(row["scheduler"], row["job_id"])
            for row in preview["open_external_jobs"]] == [("local", "own-running")]

    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _row: "running")
    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(current), loop_result)
    assert [(row["scheduler"], row["job_id"])
            for row in current.hook_state["external_job_waiting"][
                "open_external_jobs"]] == [("local", "own-running")]


@pytest.mark.parametrize(
    "status",
    ["accepted_identity_unresolved", "submission_outcome_unknown"],
)
def test_unresolved_receipt_is_named_preview_failure_and_unchanged_terminal_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    state = State.new("experiment", tmp_path)
    receipt = state.save_artifact(
        "job_submission", status, json.dumps(_unresolved(status)))
    _patch_preview_observers(monkeypatch)
    expected_reason = (
        "authoritative job_submission records unavailable: "
        "scheduler submission outcome/identity remains unresolved: "
        + receipt["id"]
    )

    preview = asyncio.run(sediment._preview_experiment_contract(state))
    assert preview["status"] == "success"
    assert preview["overall_status"] == "incomplete"
    assert preview["failed_checks"] == ["job_submission_records_readable"]
    assert preview["checks"]["job_submission_records_readable"] == {
        "passed": False,
        "reason": expected_reason,
    }
    assert preview["review_eligibility"] is False

    messages = hooks._external_job_finish_gate(_ctx(state))
    assert messages and expected_reason in messages[0].content
    assert "不要重复提交" in messages[0].content

    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)
    assert loop_result.status == "blocked"
    assert state.hook_state["experiment_downstream_blocked"] == {
        "reason": expected_reason,
        "failed_checks": ["job_submission_records_readable"],
        "review_eligibility": False,
    }
    blocker = next(
        row for row in state.hook_state["blockers"]
        if row["blocker_id"] == "experiment_job_submission_read_error"
    )
    assert blocker == {
        "blocker_id": "experiment_job_submission_read_error",
        "category": "closure",
        "summary": (
            "unable to read authoritative external job submission records; "
            "cannot verify that external work is finalized"
        ),
        "retryable_after_change": True,
    }


def test_foreign_missing_body_is_filtered_from_head_before_content_read(
    tmp_path: Path,
) -> None:
    worktree, records = _shared_workspace(tmp_path)
    foreign = _workspace_state(tmp_path / "foreign", worktree, records)
    receipt = foreign.save_artifact(
        "job_submission", "foreign_missing", json.dumps(_submission("foreign")))
    body = foreign.find_artifact_path(receipt["id"])
    assert body is not None
    body.unlink()

    current = _workspace_state(tmp_path / "current", worktree, records)

    assert manager.read_owed_submission_receipts(current) == []
    assert manager.owed_external_job_closure_records(current) == []


def test_current_run_missing_body_remains_fail_closed(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    receipt = state.save_artifact(
        "job_submission", "own_missing", json.dumps(_submission("own")))
    body = state.find_artifact_path(receipt["id"])
    assert body is not None
    body.unlink()

    with pytest.raises(
        manager.SubmissionLedgerError,
        match=f"{receipt['id']}: artifact file is missing",
    ):
        manager.owed_external_job_closure_records(state)


@pytest.mark.parametrize(
    ("boundary", "detail"),
    [
        ("list", "artifact listing failed"),
        ("head", "artifact ledger head read failed"),
        ("read", "artifact body read failed"),
    ],
)
def test_ledger_io_failure_is_one_reason_at_every_closure_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    detail: str,
) -> None:
    state = State.new("experiment", tmp_path)
    state.save_artifact(
        "job_submission", "own_running", json.dumps(_submission("own-running")))
    _patch_preview_observers(monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError(f"simulated {boundary} failure")

    monkeypatch.setattr(state, {
        "list": "list_artifacts",
        "head": "artifact_head",
        "read": "read_artifact",
    }[boundary], fail)

    preview = asyncio.run(sediment._preview_experiment_contract(state))
    check = preview["checks"]["job_submission_records_readable"]
    assert check["passed"] is False
    assert check["reason"].startswith(
        "authoritative job_submission records unavailable: job_submission")
    assert detail in check["reason"]
    assert f"simulated {boundary} failure" in check["reason"]
    assert preview["failed_checks"] == ["job_submission_records_readable"]
    assert preview["experiment_workflow_status"] == "awaiting_external_job"

    gate = hooks._external_job_finish_gate(_ctx(state))
    assert gate and check["reason"] in gate[0].content

    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)
    assert loop_result.status == "blocked"
    assert state.hook_state["experiment_downstream_blocked"] == {
        "reason": check["reason"],
        "failed_checks": ["job_submission_records_readable"],
        "review_eligibility": False,
    }


def test_shared_judgement_does_not_create_missing_tasks_directory(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path / "runs")
    state.project_root = tmp_path / "project"
    tasks_dir = state.project_root / "tasks"
    assert not tasks_dir.exists()

    assert manager.owed_external_job_closure_records(state) == []

    assert not tasks_dir.exists()


@pytest.mark.parametrize(
    ("mode", "detail"),
    [
        ("not_directory", "task ledger path is not a directory"),
        ("invalid_json", "task ledger contains invalid JSON at line 1"),
        ("invalid_identity", "invalid identity fields: job_id"),
        (
            "invalid_recovery",
            "invalid identity fields: external_job_identity_recovery_key",
        ),
    ],
)
def test_strict_task_ledger_failure_is_visible_before_and_at_on_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    detail: str,
) -> None:
    worktree, records = _shared_workspace(tmp_path)
    foreign = _workspace_state(tmp_path / "foreign", worktree, records)
    foreign.save_artifact(
        "job_submission", "foreign_running", json.dumps(_submission("foreign")))
    state = _workspace_state(tmp_path / "current", worktree, records)
    state.project_root = tmp_path / "project"
    state.project_root.mkdir()
    tasks_path = state.project_root / "tasks"
    if mode == "not_directory":
        tasks_path.write_text("not a directory", encoding="utf-8")
    elif mode == "invalid_json":
        tasks_path.mkdir()
        (tasks_path / "tasks.jsonl").write_text("{bad json\n", encoding="utf-8")
    elif mode == "invalid_identity":
        TaskList(tasks_path).create(
            title="Malformed external job handoff",
            owner_node="experiment",
            run_id="run-foreign",
            description="external_job_key=broken\nscheduler=local",
        )
    else:
        TaskList(tasks_path).create(
            title="Malformed identity recovery",
            owner_node="experiment",
            run_id="run-foreign",
            description=(
                "external_job_identity_recovery_key=\n"
                "submission_nonce=nonce-1\nscheduler=local"
            ),
        )
    _patch_preview_observers(monkeypatch)

    preview = asyncio.run(sediment._preview_experiment_contract(state))
    check = preview["checks"]["job_submission_records_readable"]
    assert check["passed"] is False
    assert detail in check["reason"]
    assert preview["failed_checks"] == ["job_submission_records_readable"]
    assert preview["experiment_workflow_status"] == "awaiting_external_job"

    gate = hooks._external_job_finish_gate(_ctx(state))
    assert gate and check["reason"] in gate[0].content
    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)
    assert loop_result.status == "blocked"
    assert state.hook_state["experiment_downstream_blocked"]["reason"] == check["reason"]


def test_cross_run_identity_recovery_task_remains_strict_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, records = _shared_workspace(tmp_path)
    project_root = tmp_path / "project"
    foreign = _workspace_state(tmp_path / "foreign", worktree, records)
    foreign.project_root = project_root
    payload = {
        **_unresolved("accepted_identity_unresolved"),
        "job_name": "accepted-without-id",
        "submission_nonce": "nonce-1",
    }
    foreign.save_artifact(
        "job_submission", "foreign_unresolved", json.dumps(payload))
    task_id = hooks.persist_external_job_identity_recovery(foreign, payload)
    assert task_id

    current = _workspace_state(tmp_path / "current", worktree, records)
    current.project_root = project_root
    _patch_preview_observers(monkeypatch)
    preview = asyncio.run(sediment._preview_experiment_contract(current))
    check = preview["checks"]["job_submission_records_readable"]
    assert check["passed"] is False
    assert (
        f"scheduler submission outcome/identity remains unresolved: recovery task {task_id}"
        in check["reason"]
    )
    assert preview["failed_checks"] == ["job_submission_records_readable"]
    assert preview["experiment_workflow_status"] == "awaiting_external_job"

    gate = hooks._external_job_finish_gate(_ctx(current))
    assert gate and check["reason"] in gate[0].content
    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(current), loop_result)
    assert loop_result.status == "blocked"
    assert current.hook_state["experiment_downstream_blocked"] == {
        "reason": check["reason"],
        "failed_checks": ["job_submission_records_readable"],
        "review_eligibility": False,
    }

    TaskList(project_root / "tasks").complete(task_id)
    with pytest.raises(
        manager.SubmissionLedgerError,
        match="scheduler submission outcome/identity remains unresolved",
    ):
        manager.owed_external_job_closure_records(current)

    reconciled = {
        **_submission("recovered-job"),
        "submission_nonce": "nonce-1",
        "submission_persistence": {"status": "identity_reconciled"},
        "reconciliation": {"query_status": "unique"},
    }
    foreign.save_artifact(
        "external_job_submission_recovery",
        "foreign_reconciled",
        json.dumps(reconciled),
    )
    recovered = manager.owed_external_job_closure_records(current)
    assert [(row["scheduler"], row["job_id"]) for row in recovered] == [
        ("local", "recovered-job")
    ]

    preview = asyncio.run(sediment._preview_experiment_contract(current))
    assert [
        (row["scheduler"], row["job_id"])
        for row in preview["open_external_jobs"]
    ] == [("local", "recovered-job")]
    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(current), loop_result)
    assert loop_result.status == "blocked"
    assert [
        (row["scheduler"], row["job_id"])
        for row in current.hook_state["external_job_waiting"]["open_external_jobs"]
    ] == [("local", "recovered-job")]

    current.save_artifact(
        "external_job_lifecycle",
        "recovered_finalized",
        json.dumps({
            "scheduler": "local",
            "job_id": "recovered-job",
            "submission_nonce": "nonce-1",
            "lifecycle_status": "finalized",
        }),
    )
    assert manager.owed_external_job_closure_records(current) == []


def test_task_owner_exact_marker_and_model_completed_status_do_not_fake_closure(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path / "runs")
    state.project_root = tmp_path / "project"
    tasks = TaskList(state.project_root / "tasks")
    tasks.create(
        title="Other node task",
        owner_node="hypothesis",
        run_id="other-run",
        description=(
            "external_job_key=other:local:other-job\n"
            "scheduler=local\njob_id=other-job"
        ),
    )
    tasks.create(
        title="Unrelated Experiment task",
        owner_node="experiment",
        run_id=state.run_id,
        description=(
            "not_external_job_key=not-a-marker\n"
            "note=external_job_key=quoted text only"
        ),
    )
    assert manager.owed_external_job_closure_records(state) == []

    legacy = tasks.create(
        title="Legacy ownerless handoff",
        owner_node="",
        run_id="old-run",
        description=(
            "external_job_key=old-run:local:legacy-job\n"
            "scheduler=local\njob_id=legacy-job\noutput_roots=[]"
        ),
    )
    assert [
        row["job_id"] for row in manager.owed_external_job_closure_records(state)
    ] == ["legacy-job"]

    tasks.complete(legacy.id)
    assert [
        row["job_id"] for row in manager.owed_external_job_closure_records(state)
    ] == ["legacy-job"]

    state.save_artifact(
        "external_job_lifecycle",
        "legacy_finalized",
        json.dumps({
            "scheduler": "local",
            "job_id": "legacy-job",
            "lifecycle_status": "finalized",
        }),
    )
    assert manager.owed_external_job_closure_records(state) == []
