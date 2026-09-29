"""Generic external-job handoff tests for the experiment node."""
from __future__ import annotations

import asyncio
import os
import socket
import time
import threading

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.sandbox import availability
from core.loop_hooks import HookContext
from core.state import State
from core.tasks import TaskList
from nodes.experiment.tools.contract_audit import TERMINAL_CLOSURE_REGISTRY
from nodes.experiment.tools.path_roles import experiment_output_dir

_NODE_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "experiment_handoff_hooks_under_test", _NODE_DIR / "hooks.py")
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)

requires_sandbox = pytest.mark.skipif(
    not availability()[0], reason="mandatory Docker sandbox is unavailable")


def _save_frozen(state, artifact_type, name, content, metadata=None):
    """夹具级冻结：save + mark_frozen。冻结的事实只出自账本的 freeze 行，save 行里的
    frozen 键会被剥掉（core.ledger.FREEZE_OWNED_METADATA）。返回 save_artifact 的结果。"""
    saved = state.save_artifact(artifact_type, name, content, metadata=metadata)
    state.mark_frozen(saved["id"])
    return saved


def _complete_terminal_audit() -> dict[str, dict]:
    return {
        key: {"passed": True, "applicable": True, "reason": "ok"}
        for key in TERMINAL_CLOSURE_REGISTRY
    }


class _State:
    def __init__(self, tmp_path: Path, payloads: list[dict], project: bool = True):
        self.run_id = "run-123"
        self.node_type = "experiment"
        self.root = tmp_path
        self.project_root = tmp_path / "project" if project else None
        self._records = {
            f"job-{index}": {
                "type": "job_submission",
                "content": json.dumps(payload),
                "produced_by_node_type": "experiment",
                "produced_by_run_id": self.run_id,
            }
            for index, payload in enumerate(payloads, start=1)
        }
        self.events: list[dict] = []
        self.hook_state: dict = {}

    def list_artifacts(self, artifact_type=None, own_only=False):
        if artifact_type != "job_submission":
            return []
        return [{"id": key, "type": "job_submission", "name": key}
                for key in self._records]

    def read_artifact(self, artifact_id):
        return self._records[artifact_id]

    def append_transcript(self, event, **fields):
        self.events.append({"event": event, **fields})


def _submission(**overrides):
    result = {
        "status": "success",
        "dry_run": False,
        "scheduler": "local",
        "job_id": "4242",
        "job_name": "long-job",
        "workdir": "/tmp/work",
        "scheduler_output_dir": "/tmp/work/logs",
        "stdout_path": "/tmp/work/out.log",
        "stderr_path": "/tmp/work/err.log",
    }
    result.update(overrides)
    return result


def _external_job_ref(submission: dict) -> dict:
    """实验日志引用提交工具返回的完整 identity，不从 job_id 猜 scope。"""
    fields = (
        "scheduler", "job_id", "namespace", "launch_host",
        "scheduler_cluster", "resource_uid", "submission_nonce",
        "container_runtime_id",
    )
    return {
        field: submission[field]
        for field in fields
        if submission.get(field) not in {None, ""}
    }


def _cleanup_local_submission(submission: dict):
    """Cleanup one test-owned local job only after exact cancellation succeeds."""
    from core.sandbox import cleanup_control_dir
    from nodes.experiment.tools.resource_manager import _cancel_sync

    job_id = str(submission.get("job_id") or "")
    runtime_id = str(submission.get("container_runtime_id") or "")
    control_dir = str(submission.get("sandbox_control_dir") or "")
    if not job_id or len(runtime_id) != 64 or not all(
        character in "0123456789abcdef" for character in runtime_id
    ):
        raise AssertionError(
            "local production cleanup requires the exact immutable container identity"
        )
    cancel_result = _cancel_sync(
        "local", job_id, None, container_runtime_id=runtime_id,
    )
    if not cancel_result.get("ok"):
        raise AssertionError(
            "exact local cancellation failed; sandbox control directory was preserved: "
            + str(cancel_result)
        )
    if control_dir:
        cleanup_control_dir(control_dir)
    return cancel_result


def _ctx(state):
    return HookContext(harness=None, state=state, messages=[], turn=1)


def _run_finish_gate(state):
    """走框架真实相位派发收尾闸，而不是直接调节点内部函数。

    旧测试直接把一个可变对象喂给内部函数并断言它被改写，于是 2026-08-01 豁免到期、
    该相位改成只读之后，机制已经完全失效而测试仍然全绿（ROADMAP N-005）。派发口是
    唯一能同时覆盖"逻辑对"和"这条路真的走得通"的入口。
    """
    from core import loop_hooks

    return asyncio.run(loop_hooks.run_on_before_finish(
        [hooks.external_job_handoff], _ctx(state)))


def _bind_operation_inputs(state) -> None:
    state.hook_state.setdefault("node_inputs", {
        "experiment_focus": "Execute the declared managed operation fixture and retain its evidence.",
    })


def test_local_production_cleanup_preserves_control_dir_when_cancel_fails(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as manager

    control_dir = tmp_path / "sandbox-control"
    control_dir.mkdir()
    cleanup_calls = []
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_args, **_kwargs: {"ok": False, "error": "identity mismatch"},
    )
    monkeypatch.setattr(
        sandbox, "cleanup_control_dir",
        lambda path: cleanup_calls.append(str(path)),
    )

    with pytest.raises(AssertionError, match="control directory was preserved"):
        _cleanup_local_submission({
            "status": "success",
            "job_id": "hf-test-owned",
            "container_runtime_id": "a" * 64,
            "sandbox_control_dir": str(control_dir),
        })

    assert control_dir.is_dir()
    assert cleanup_calls == []


def test_running_job_creates_pending_task_and_footer(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")
    result = SimpleNamespace(final_text="done")

    hooks.external_job_handoff_on_end(_ctx(state), result)

    tasks = TaskList(state.project_root / "tasks").list_all()
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert "external_job_key=run-123:local:4242" in tasks[0].description
    assert "## External Job Workflow" in result.final_text
    assert "experiment_workflow_status: awaiting_external_job" in result.final_text
    event = state.events[-1]
    assert event["event"] == "external_job_handoff"
    assert event["status"] == "running"
    assert event["task_id"] == tasks[0].id


def test_repeated_handoff_reuses_existing_task(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")

    hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))
    hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))

    tasks = TaskList(state.project_root / "tasks").list_all()
    assert len(tasks) == 1
    handoffs = [event for event in state.events if event["event"] == "external_job_handoff"]
    assert [event["task_id"] for event in handoffs] == ["T01", "T01"]


def test_finished_job_remains_pending_analysis_workflow(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_classify_external_job_status",
                        lambda _: "finished_or_unavailable")

    hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))

    tasks = TaskList(state.project_root / "tasks").list_all()
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert "experiment_workflow_status=awaiting_external_job" in tasks[0].description
    assert state.events[-1]["status"] == "finished_or_unavailable"
    assert state.events[-1]["workflow_status"] == "awaiting_analysis"


def test_dry_run_submission_is_not_handed_off(tmp_path, monkeypatch):
    # submit_job dry-run receipts intentionally have no scheduler identity.
    state = _State(tmp_path, [_submission(dry_run=True, job_id=None)])
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")

    hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))

    assert state.events == []
    assert not (state.project_root / "tasks").exists()


def test_dry_run_receipt_does_not_block_finalized_real_submission(tmp_path):
    """The normal dry-run -> submit -> finalize lifecycle closes cleanly."""
    from core.agent_loop import LoopResult
    from core.executor import finalize_run
    from core.harness import NodeHarness
    from core.loop_hooks import run_on_end

    state = State.new("experiment", tmp_path)
    state.save_artifact("job_submission", "dry_run", json.dumps(
        _submission(dry_run=True, job_id=None)))
    state.save_artifact("job_submission", "real_submission", json.dumps(
        _submission(dry_run=False, job_id="4242")))
    state.save_artifact("external_job_lifecycle", "real_submission_finalized",
                        json.dumps({
                            "scheduler": "local",
                            "job_id": "4242",
                            "lifecycle_status": "finalized",
                        }))

    loop_result = LoopResult(final_text="model claimed completion", turns=1,
                             tool_calls=[], messages=[], status="completed")
    asyncio.run(run_on_end([hooks.external_job_handoff], _ctx(state), loop_result))

    assert loop_result.status == "completed"
    assert state.hook_state.get("blockers", []) == []
    summary = asyncio.run(finalize_run(
        state, NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result, llm=None))
    assert summary["status"] == "completed"


def test_real_success_receipt_without_job_id_remains_strictly_invalid(tmp_path):
    state = State.new("experiment", tmp_path)
    state.save_artifact("job_submission", "missing_real_identity", json.dumps(
        _submission(dry_run=False, job_id=None)))

    with pytest.raises(hooks.SubmissionLedgerError,
                       match="successful receipt has no job_id"):
        hooks._job_submission_records(state)


def test_unknown_job_is_preserved_for_follow_up(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "unknown")

    hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))

    tasks = TaskList(state.project_root / "tasks").list_all()
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert state.events[-1]["status"] == "unknown"


def test_running_job_records_machine_readable_waiting_state(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")
    result = SimpleNamespace(final_text="done")

    hooks.external_job_handoff_on_end(_ctx(state), result)

    waiting = state.hook_state["external_job_waiting"]
    assert waiting["scientific_result_status"] == "awaiting_external_job"
    assert waiting["assessment_status"] == "not_available"
    assert waiting["review_eligibility"] is False
    assert waiting["open_external_jobs"][0]["job_id"] == "4242"
    assert "You may exit chat safely" in result.final_text


def test_reconciliation_tolerates_malformed_output_roots(tmp_path, monkeypatch):
    state = _State(tmp_path, [])
    tasks = TaskList(state.project_root / "tasks")
    tasks.create(title="Finalize", owner_node="experiment", run_id="old-run", description=(
        "external_job_key=old-run:local:4242\n"
        "scheduler=local\njob_id=4242\nworkdir=/tmp/work\noutput_roots=[not-json"))
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")

    messages = hooks.external_job_reconciliation_on_turn_start(_ctx(state))

    assert messages
    assert "不得向重叠 output_roots/workdir 再次 submit_job" in messages[0].content
    event = state.events[-1]
    assert event["event"] == "external_job_reconciliation"
    assert event["jobs"][0]["output_roots"] == []

@requires_sandbox
def test_local_cancel_stops_container_and_releases_output(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _cancel_sync, _job_status_sync, _submit_sync,
    )

    run_state = State.new("experiment", tmp_path)
    runtime = experiment_output_dir(run_state, "runtime", create=True)
    result = _submit_sync(
        runtime, "local", "sleep 30 & wait", "container-cancel",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        stage_in=None,
        state=run_state,
    )
    assert result["status"] == "success"
    assert len(result["container_runtime_id"]) == 64
    try:
        state = _State(tmp_path, [result])
        before = _job_status_sync(
            "local", result["job_id"], None,
            container_runtime_id=result["container_runtime_id"],
        )
        assert before["raw"]["stdout"] == "RUNNING"
        conflicts = _active_output_conflicts(state, [str(runtime)])
        assert [row["job_id"] for row in conflicts] == [result["job_id"]]
        cancelled = _cancel_sync(
            "local", result["job_id"], None,
            container_runtime_id=result["container_runtime_id"],
        )
        assert cancelled["ok"] is True
        assert cancelled["action"] == "sandbox_stop"
        after = _job_status_sync(
            "local", result["job_id"], None,
            container_runtime_id=result["container_runtime_id"],
        )
        assert after["raw"]["stdout"] == "NOT_RUNNING"
        assert _active_output_conflicts(state, [str(runtime)]) == []
    finally:
        _cleanup_local_submission(result)


@requires_sandbox
def test_preview_reports_awaiting_external_job_for_real_local_submission(tmp_path, monkeypatch):
    from core import data_provenance
    from nodes.experiment.tools import sediment
    from nodes.experiment.tools.resource_manager import _cancel_sync, _submit_sync

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    result = _submit_sync(
        runtime, "local", "sleep 30 & wait", "preview-wait", 1, 1, 0,
        1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        stage_in=None,
        state=state,
    )
    assert result["status"] == "success"
    assert len(result["container_runtime_id"]) == 64
    try:
        state.save_artifact("job_submission", "job_submission_preview", json.dumps(result))
        monkeypatch.setattr(
            sediment, "audit_experiment_contract",
            lambda _state: _complete_terminal_audit(),
        )
        monkeypatch.setattr(data_provenance, "undeclared_stale_inputs", lambda _state: [])
        preview = asyncio.run(sediment._preview_experiment_contract(state))
        assert preview["overall_status"] == "awaiting_external_job"
        assert preview["review_eligibility"] is False
        assert preview["open_external_jobs"][0]["job_id"] == result["job_id"]
    finally:
        _cleanup_local_submission(result)


def test_submission_result_contains_generic_handoff_identity(tmp_path):
    from nodes.experiment.tools.resource_manager import _submit_sync

    result = _submit_sync(
        tmp_path, "local", "echo complete", "identity-check", 1, 1, 0,
        1.0, 8.0, 1, None, None, None, str(tmp_path), True, None,
        stage_in=None,
        state=None,
    )

    assert result["status"] == "success"
    assert result["dry_run"] is True
    assert result["submitted_at"]
    assert result["command"] == "echo complete"
    assert result["workdir"] == str(tmp_path)
    assert "namespace" in result


@requires_sandbox
def test_health_snapshot_detects_recent_progress_and_stall(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _cancel_sync, _submit_sync, probe_external_job_health,
    )

    run_state = State.new("experiment", tmp_path)
    runtime = experiment_output_dir(run_state, "runtime", create=True)
    result = _submit_sync(
        runtime, "local", "printf tick > progress.log; sleep 30", "health-check",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        health_check={"progress_paths": ["progress.log"], "poll_interval_s": 30,
                      "stall_after_s": 60},
        stage_in=None,
        state=run_state,
    )
    assert result["status"] == "success"
    assert len(result["container_runtime_id"]) == 64
    try:
        state = _State(tmp_path, [result])
        progress = runtime / "progress.log"
        deadline = time.monotonic() + 3
        while not progress.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        health = probe_external_job_health(state, "local", result["job_id"])
        assert health["scheduler_phase"] == "running"
        assert health["health_state"] == "healthy"
        stale_at = time.time() - 120
        os.utime(progress, (stale_at, stale_at))
        for log_path in (result["stdout_path"], result["stderr_path"]):
            if os.path.exists(log_path):
                os.utime(log_path, (stale_at, stale_at))
        stalled = probe_external_job_health(state, "local", result["job_id"])
        assert stalled["health_state"] == "stalled"
    finally:
        _cleanup_local_submission(result)


def test_health_probe_combines_progress_with_resource_pressure(
    tmp_path,
    monkeypatch,
) -> None:
    from nodes.experiment.tools import resource_manager as manager

    logs = tmp_path / "logs"
    logs.mkdir()
    progress = tmp_path / "progress.dat"
    progress.write_text("step 1\n", encoding="utf-8")
    guard_status = logs / "resource_guard.json"
    guard_status.write_text(json.dumps({
        "status": "running_warning",
        "resource_health": "critical",
        "decision": "continue_with_fast_sampling",
        "decision_reasons": ["memory_bytes_critical"],
        "active_warnings": ["memory_bytes"],
        "behavior_evidence": {"memory_bytes": 9_600},
    }), encoding="utf-8")
    record = _submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        resource_guard_status_path=str(guard_status),
        health_contract={
            "progress_paths": [str(progress)],
            "completion_paths": [],
            "error_patterns": [],
            "stall_after_s": 60,
        },
    )
    state = _State(tmp_path, [record])
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["health_state"] == "healthy"
    assert health["resource_health"] == "critical"
    assert health["decision"] == "continue_with_fast_sampling"
    assert health["resource_health_evidence"]["active_warnings"] == [
        "memory_bytes",
    ]


def test_health_probe_never_recommends_cancel_without_stall_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    from nodes.experiment.tools import resource_manager as manager

    logs = tmp_path / "logs"
    logs.mkdir()
    guard_status = logs / "resource_guard.json"
    guard_status.write_text(json.dumps({
        "status": "running_warning",
        "resource_health": "critical",
        "decision": "continue_with_fast_sampling",
        "active_warnings": ["memory_bytes"],
    }), encoding="utf-8")
    record = _submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        resource_guard_status_path=str(guard_status),
        health_contract={
            "progress_paths": [],
            "completion_paths": [],
            "error_patterns": [],
            "stall_after_s": 60,
        },
    )
    state = _State(tmp_path, [record])
    cancelled = []
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: cancelled.append(True),
    )

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["health_state"] == "running_without_progress_evidence"
    assert health["resource_health"] == "critical"
    assert health["decision"] == "diagnose"
    assert health["decision"] != "managed_cancel_recommended"
    assert cancelled == []


def test_health_probe_recommends_but_does_not_execute_managed_cancel(
    tmp_path,
    monkeypatch,
) -> None:
    from nodes.experiment.tools import resource_manager as manager

    logs = tmp_path / "logs"
    logs.mkdir()
    progress = tmp_path / "progress.dat"
    progress.write_text("old step\n", encoding="utf-8")
    stale_at = time.time() - 120
    os.utime(progress, (stale_at, stale_at))
    guard_status = logs / "resource_guard.json"
    guard_status.write_text(json.dumps({
        "status": "running_warning",
        "resource_health": "pressure",
        "decision": "continue_with_fast_sampling",
        "active_warnings": ["host_swap_free_bytes"],
    }), encoding="utf-8")
    record = _submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        resource_guard_status_path=str(guard_status),
        health_contract={
            "progress_paths": [str(progress)],
            "completion_paths": [],
            "error_patterns": [],
            "stall_after_s": 60,
        },
    )
    state = _State(tmp_path, [record])
    cancelled = []
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })
    monkeypatch.setattr(
        manager, "_cancel_sync",
        lambda *_a, **_k: cancelled.append(True),
    )

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["health_state"] == "stalled"
    assert health["resource_health"] == "pressure"
    assert health["decision"] == "managed_cancel_recommended"
    assert cancelled == []


def test_health_probe_resource_exhaustion_requires_analysis(
    tmp_path,
    monkeypatch,
) -> None:
    from nodes.experiment.tools import resource_manager as manager

    logs = tmp_path / "logs"
    logs.mkdir()
    guard_status = logs / "resource_guard.json"
    guard_status.write_text(json.dumps({
        "status": "error",
        "resource_health": "exhausted",
        "decision": "emergency_stop",
        "decision_reasons": ["build_memory_limit_exhausted"],
        "active_warnings": ["memory_bytes"],
        "reason": "build_memory_limit_exhausted",
        "failure_class": "resource_exhaustion",
    }), encoding="utf-8")
    record = _submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        resource_guard_status_path=str(guard_status),
        health_contract={
            "progress_paths": [],
            "completion_paths": [],
            "error_patterns": [],
        },
    )
    state = _State(tmp_path, [record])
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["scheduler_phase"] == "running"
    assert health["resource_health"] == "exhausted"
    assert health["workflow_status"] == "awaiting_analysis"
    assert health["decision"] == "diagnose_resource_exhaustion"


def test_health_probe_reports_missing_managed_status_as_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    from nodes.experiment.tools import resource_manager as manager

    logs = tmp_path / "logs"
    logs.mkdir()
    record = _submission(
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(logs),
        stdout_path=str(logs / "out.log"),
        stderr_path=str(logs / "err.log"),
        resource_guard_status_path=str(logs / "missing.json"),
        health_contract={
            "progress_paths": [],
            "completion_paths": [],
            "error_patterns": [],
        },
    )
    state = _State(tmp_path, [record])
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["resource_health"] == "unknown"
    assert health["decision"] == "diagnose"
    assert "resource_guard_status_not_yet_available" in (
        health["decision_reasons"]
    )


def test_expected_duration_expiry_reports_activity_without_killing(
    tmp_path,
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from nodes.experiment.tools import resource_manager as manager

    stdout = tmp_path / "job.out"
    stderr = tmp_path / "job.err"
    stdout.write_text("still producing output\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    record = _submission(
        submitted_at=(
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat(),
        expected_duration_s=1,
        output_roots=[str(tmp_path)],
        scheduler_output_dir=str(tmp_path),
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        health_contract={
            "progress_paths": [],
            "completion_paths": [],
            "error_patterns": [],
            "stall_after_s": 30,
        },
    )
    state = _State(tmp_path, [record])
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success",
        "raw": {"ok": True, "stdout": "RUNNING", "stderr": ""},
    })

    health = manager.probe_external_job_health(
        state, "local", record["job_id"],
    )

    assert health["health_state"] == "overdue_with_output_activity"
    assert health["health_evidence_level"] == "output_activity"
    assert health["expected_duration_s"] == 1


def test_health_contract_rejects_paths_outside_declared_outputs(tmp_path):
    from nodes.experiment.tools.resource_manager import _submit_sync

    rejected = _submit_sync(
        tmp_path, "local", "echo no-submit", "bad-health",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(tmp_path), True, None,
        output_paths=[str(tmp_path)],
        health_check={"progress_paths": ["/etc/passwd"]},
        stage_in=None,
        state=None,
    )
    assert rejected["status"] == "error"
    assert "output_paths/output_dir" in rejected["error"]


def test_unknown_orphan_reserves_overlapping_outputs(tmp_path):
    from nodes.experiment.tools.resource_manager import _active_output_conflicts, _record_unknown_orphan

    state = State.new("experiment", tmp_path)
    result = asyncio.run(_record_unknown_orphan(state, [str(tmp_path / "old-run")], ["pid=42"], [str(tmp_path / "old-run" / "huge.log")]))
    assert result["classification"] == "unknown_orphan"
    conflicts = _active_output_conflicts(state, [str(tmp_path / "old-run" / "results")])
    assert conflicts and conflicts[0]["kind"] == "unknown_orphan"
    # 隔离是阻断而不是释放：登记回执和冲突都必须指名唯一的解除通道。
    assert "resolve_unknown_orphan" in result["message"]
    assert "resolve_unknown_orphan" in conflicts[0]["resolution"]
    assert conflicts[0]["artifact_id"] in conflicts[0]["resolution"]
    assert "本地作业隔离层" in conflicts[0]["resolution"]
    assert "docker inspect" not in conflicts[0]["resolution"]
    # 裸 pid 以后一定探不活：登记当场就得说清这条记录已经不可解除。
    assert [item["reference"] for item in result["unprobeable_observed_processes"]] == ["pid=42"]
    assert "pid=<数字>@<主机名>" in result["unprobeable_observed_processes"][0]["problem"]
    assert "不可机械复核" in result["message"]


def test_resolve_unknown_orphan_tool_description_uses_native_isolation_terms():
    from core.tool_registry import get_tool
    from nodes.experiment.tools import resource_manager  # noqa: F401

    description = get_tool("resolve_unknown_orphan").description
    assert "本地作业隔离层" in description
    assert "docker inspect" not in description


def _dead_pid() -> int:
    """A pid that is mechanically provable as absent on this host."""
    candidate = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    while os.path.exists(f"/proc/{candidate}"):
        candidate -= 1
    return candidate


def _local_pid_reference(pid: int | str) -> str:
    """自述必须显式带主机名，这里绑定到本机 —— 裸 pid 一律不可解除。"""
    return f"pid={pid}@{socket.gethostname()}"


def _isolated_orphan(tmp_path: Path, observed_processes: list[str],
                     *, root_name: str = "old-run"):
    """Record one orphan over a root whose files predate the isolation."""
    from nodes.experiment.tools.resource_manager import _record_unknown_orphan

    root = tmp_path / root_name
    root.mkdir(parents=True, exist_ok=True)
    stale = root / "features.csv"
    stale.write_text("x,y\n", encoding="utf-8")
    # 目录自身也回拨：真实的 orphan 输出根不会在登记前一瞬才被建出来，而静默
    # 核验带时钟安全余量，紧贴 recorded_at 的 mtime 一律按"可能被写过"处理。
    old = time.time() - 600
    for path in (stale, root):
        os.utime(path, (old, old))
    state = State.new("experiment", tmp_path)
    recorded = asyncio.run(
        _record_unknown_orphan(state, [str(root)], observed_processes))
    return state, root, recorded["artifact_id"]


def test_resolve_unknown_orphan_releases_lock_on_dead_processes_and_silent_roots(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _read_json_artifact, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(_dead_pid())])
    released = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="属主确认已停机并核对过输出"))

    assert released["status"] == "success"
    assert released["orphan_artifact_id"] == orphan_id
    assert [probe["state"] for probe in released["process_probes"]] == ["dead"]
    assert released["silence_probe"]["state"] == "silent"
    assert _active_output_conflicts(state, [str(root / "results")]) == []
    # orphan 本体不删不改：解除只是追加了一条否定记录。
    orphan_payload = _read_json_artifact(
        state, {"id": orphan_id})
    assert orphan_payload["status"] == "active"
    resolutions = state.list_artifacts("unknown_orphan_resolution")
    assert len(resolutions) == 1
    frozen = _read_json_artifact(state, resolutions[0])
    assert frozen["reason"] == "属主确认已停机并核对过输出"
    assert frozen["orphan_artifact_id"] == orphan_id
    events = [json.loads(line) for line in
              state.transcript_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(event["event"] == "unknown_orphan_resolved" for event in events)


def test_resolve_unknown_orphan_without_observed_processes_uses_root_silence(tmp_path):
    """airsea 形态：observed_processes 为空，仅凭输出根静默 + reason 解除。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [])
    released = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="59 轮恢复全被这条隔离拦下，已人工核查输出无写者"))

    assert released["status"] == "success"
    assert released["process_probes"] == []
    assert _active_output_conflicts(state, [str(root)]) == []


def test_resolve_unknown_orphan_refuses_while_declared_process_is_alive(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(os.getpid())])
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="想直接解锁"))

    assert refused["status"] == "error"
    assert "存活" in refused["error"]
    assert "resolve_unknown_orphan" in refused["next_action"]
    assert "output_paths" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_unprobeable_process_reference(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(
        tmp_path, ["上周残留的 solver 进程，看起来早就没了"])
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="看起来已经没了"))

    assert refused["status"] == "error"
    assert "无法机械探活" in refused["error"]
    assert [probe["state"] for probe in refused["process_probes"]] == ["unavailable"]
    assert "pid=<数字>" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_when_roots_were_written_after_record(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(_dead_pid())])
    fresh = root / "match_log.json"
    fresh.write_text("{}", encoding="utf-8")
    os.utime(fresh, (time.time() + 60, time.time() + 60))
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="以为没人写了"))

    assert refused["status"] == "error"
    assert "静默核验未通过" in refused["error"]
    assert os.path.realpath(fresh) in refused["suspicious_files"]
    assert "suspicious_files" in refused["next_action"]
    # 写过就再也静默不了（mtime 单调），文案不得让调用方去等或去"清理"。
    assert "唯一可执行的下一步是换目录" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_silence_baseline_keeps_a_clock_skew_margin_on_both_sides(tmp_path):
    """recorded_at 是墙钟、mtime 是文件系统盖章：基线往前推余量，只会更保守。

    实测同机"严格在 recorded_at 之后"的写入，其 st_mtime 反而可能早几毫秒；
    余量把可疑窗口放宽，方向永远是更容易判 written、更难解除。
    """
    from nodes.experiment.tools.resource_manager import (
        _SILENCE_MTIME_MARGIN_S, _probe_output_roots_silent,
    )

    root = tmp_path / "old-run"
    root.mkdir()
    entry = root / "features.csv"
    entry.write_text("x,y\n", encoding="utf-8")
    recorded_at = datetime.now(timezone.utc)
    baseline = recorded_at.timestamp()

    # 余量之内（名义上早于 recorded_at）仍按"可能是隔离期内的写入"处理。
    inside = baseline - _SILENCE_MTIME_MARGIN_S / 2
    for path in (root, entry):
        os.utime(path, (inside, inside))
    near = _probe_output_roots_silent([str(root)], recorded_at.isoformat())
    assert near["state"] == "written"
    assert str(entry) in near["suspicious_files"]

    # 余量之外才算真静默，否则每次核验都不可能通过。
    outside = baseline - _SILENCE_MTIME_MARGIN_S - 1.0
    for path in (root, entry):
        os.utime(path, (outside, outside))
    far = _probe_output_roots_silent([str(root)], recorded_at.isoformat())
    assert far["state"] == "silent"
    assert far["entries_checked"] == 2


def test_resolve_unknown_orphan_requires_reason_and_project_local_orphan(tmp_path):
    from nodes.experiment.tools.resource_manager import _resolve_unknown_orphan

    state, _root, orphan_id = _isolated_orphan(tmp_path, [])
    blank = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="   "))
    assert blank["status"] == "error"
    assert "reason 必填" in blank["error"]
    assert orphan_id in blank["next_action"]

    foreign = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id="not-a-local-orphan", reason="随便写"))
    assert foreign["status"] == "error"
    assert foreign["known_unknown_orphan_ids"] == [orphan_id]
    assert "resolve_unknown_orphan" in foreign["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []


def test_resolve_unknown_orphan_is_idempotent(tmp_path):
    from nodes.experiment.tools.resource_manager import _resolve_unknown_orphan

    state, _root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(_dead_pid())])
    first = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="属主确认已停机"))
    second = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="再来一次"))

    assert first["status"] == second["status"] == "success"
    assert second["reused"] is True
    assert second["artifact_id"] == first["artifact_id"]
    assert len(state.list_artifacts("unknown_orphan_resolution")) == 1
    # 幂等短路不得吞掉 reason 必填契约（校验在短路之前）。
    blank = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason=""))
    assert blank["status"] == "error"
    assert "reason 必填" in blank["error"]


def test_generic_save_artifact_cannot_forge_unknown_orphan_resolution(tmp_path):
    from shared.tools.builtin import _save_artifact
    from nodes.experiment.tools.resource_manager import _active_output_conflicts

    state, root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(os.getpid())])
    forged = asyncio.run(_save_artifact(
        state,
        artifact_type="unknown_orphan_resolution",
        name="forged_release",
        content=json.dumps({"orphan_artifact_id": orphan_id}),
    ))

    assert forged["status"] == "error"
    assert forged["failed_checks"] == ["managed_external_artifact_owner"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_pid_declared_on_another_host(tmp_path):
    """本机 /proc 证不了远端 pid：SLURM 计算节点上仍在跑的作业不得被判为已死。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(
        tmp_path, [f"pid={_dead_pid()}@spr-cu04.cluster.invalid"])
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="本机 /proc 里查不到就当它死了"))

    assert refused["status"] == "error"
    assert "无法机械探活" in refused["error"]
    assert [probe["state"] for probe in refused["process_probes"]] == ["unavailable"]
    assert "spr-cu04" in refused["process_probes"][0]["detail"]
    assert "主机" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_bare_pid_even_on_the_recording_host(tmp_path):
    """裸 pid 不再按登记主机解释：登记主机 != 进程主机，猜错就是假死亡证明。

    复核者的实测形态：登记发生在登录节点，作业其实在 spr-cu04/05；本机 /proc 查
    不到就判 dead 会直接放行隔离锁。所以缺主机一律 unavailable —— 后果是解不开
    （只能换目录），而不是误放行。
    """
    from nodes.experiment.tools import resource_manager as rm

    state, root, orphan_id = _isolated_orphan(tmp_path, [f"pid={_dead_pid()}"])
    refused = asyncio.run(rm._resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="本机 /proc 里没有，应该是死了"))

    assert refused["status"] == "error"
    assert "无法机械探活" in refused["error"]
    assert refused["process_probes"][0]["state"] == "unavailable"
    assert "没有写出主机" in refused["process_probes"][0]["detail"]
    # 拒绝必须指明正确写法与理由，并给出可执行的另一条路。
    assert "pid=<数字>@<主机名>" in refused["next_action"]
    assert "output_paths" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert rm._active_output_conflicts(state, [str(root)])
    # 裸数字（连 pid= 前缀都没有）同样拒。
    assert rm._probe_observed_process_liveness("4194304")["state"] == "unavailable"


def test_resolve_unknown_orphan_refuses_pid_with_trailing_free_text(tmp_path):
    """尾随文字里写着的主机不得被静默丢弃：截到第一个空格就是误放行。"""
    from nodes.experiment.tools import resource_manager as rm

    state, root, orphan_id = _isolated_orphan(
        tmp_path, [f"pid={_dead_pid()} (python train.py) on spr-cu04"])
    refused = asyncio.run(rm._resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="自述里写了 spr-cu04，但本机查不到"))

    assert refused["status"] == "error"
    assert refused["process_probes"][0]["state"] == "unavailable"
    assert "还有别的文字" in refused["process_probes"][0]["detail"]
    assert "pid=<数字>@<主机名>" in refused["process_probes"][0]["detail"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert rm._active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_pid_with_empty_host(tmp_path):
    """pid=<n>@ 有 @ 但主机为空：不得回退到 recorded_on_host。"""
    from nodes.experiment.tools import resource_manager as rm

    state, root, orphan_id = _isolated_orphan(tmp_path, [f"pid={_dead_pid()}@"])
    refused = asyncio.run(rm._resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="@ 后面忘了写主机"))

    assert refused["status"] == "error"
    assert refused["process_probes"][0]["state"] == "unavailable"
    assert "没有写出主机" in refused["process_probes"][0]["detail"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert rm._active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_non_ascii_digit_pid_without_raising(tmp_path):
    """'²'.isdigit() 为真但 int('²') 抛 ValueError：结构化拒绝，不得抛异常。"""
    from nodes.experiment.tools import resource_manager as rm

    state, root, orphan_id = _isolated_orphan(tmp_path, ["pid=²@" + socket.gethostname()])
    refused = asyncio.run(rm._resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="从渲染过的日志里抄来的 pid"))

    assert refused["status"] == "error"
    assert refused["process_probes"][0]["state"] == "unavailable"
    assert "不是十进制数字" in refused["process_probes"][0]["detail"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert rm._active_output_conflicts(state, [str(root)])
    # 全角数字是十进制，仍按老行为归一成 ASCII pid 后再查 /proc。
    probe = rm._probe_observed_process_liveness(
        "pid=１２３４５@" + socket.gethostname())
    assert probe["pid"] == "12345"
    assert probe["state"] in {"alive", "dead"}




def test_resolve_unknown_orphan_normalizes_zero_padded_pid(tmp_path):
    """/proc/00001 不存在，但 pid 1 可能活着：零填充自述不得伪造死亡证明。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [_local_pid_reference(f"{os.getpid():09d}")])
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="从 ps 的定宽列里抄来的 pid"))

    assert refused["status"] == "error"
    assert "存活" in refused["error"]
    assert refused["process_probes"][0]["pid"] == str(os.getpid())
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_refuses_when_container_probe_is_unavailable(
        tmp_path, monkeypatch):
    from core import sandbox
    from nodes.experiment.tools.resource_manager import _resolve_unknown_orphan

    monkeypatch.setattr(
        sandbox, "availability",
        lambda refresh=False: (False, "native isolation backend offline"))
    state, _root, orphan_id = _isolated_orphan(tmp_path, ["container=deadbeef"])
    unavailable = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="容器早就没了"))
    assert unavailable["status"] == "error"
    assert unavailable["process_probes"][0]["state"] == "unavailable"
    assert "本地作业隔离层不可用" in unavailable["process_probes"][0]["detail"]
    assert "native isolation backend offline" in unavailable["process_probes"][0]["detail"]
    assert "等待本地作业隔离层恢复后再试" in unavailable["next_action"]
    assert "恢复 docker" not in unavailable["next_action"]

    monkeypatch.setattr(sandbox, "availability", lambda refresh=False: (True, "ok"))
    monkeypatch.setattr(sandbox, "inspect_container", lambda ref: {"error": "daemon 无响应"})
    probe_failed = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="容器早就没了"))
    assert probe_failed["status"] == "error"
    assert probe_failed["process_probes"][0]["state"] == "unavailable"
    assert state.list_artifacts("unknown_orphan_resolution") == []


def test_resolve_unknown_orphan_refuses_when_output_root_is_gone(tmp_path):
    """零证据不是静默证据：根被删掉/改名时无从核验，隔离保持。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [])
    (root / "features.csv").unlink()
    root.rmdir()
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="目录都没了，肯定没人写"))

    assert refused["status"] == "error"
    assert "静默核验未通过" in refused["error"]
    assert refused["silence_probe"]["state"] == "unavailable"
    assert os.path.realpath(root) in refused["silence_probe"]["missing_roots"]
    assert "output_paths" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def _orphan_with_prepared_root(tmp_path: Path, build):
    """Record an orphan over a root whose layout ``build`` prepares beforehand.

    ``build`` runs before登记，且它造的每个条目都被回拨到 recorded_at 之前 ——
    复核者的实证形态：链接在隔离开始前就在那儿，链接自身此后再不变。
    """
    from nodes.experiment.tools.resource_manager import _record_unknown_orphan

    root = tmp_path / "old-run"
    root.mkdir(parents=True, exist_ok=True)
    build(root)
    stale = time.time() - 600
    for path in [root, *root.rglob("*")]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    state = State.new("experiment", tmp_path)
    recorded = asyncio.run(_record_unknown_orphan(state, [str(root)], []))
    return state, root, recorded["artifact_id"]


def test_resolve_unknown_orphan_follows_symlinked_subdirectory_to_its_target(tmp_path):
    """树内部的符号链接子目录被跟进：写者经它写到别处，照样量得到。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state, root, orphan_id = _orphan_with_prepared_root(
        tmp_path, lambda r: os.symlink(elsewhere, r / "linked"))
    # 登记后写者继续往链接的真实目标写：链接自身的 mtime 不会因此改变。
    (elsewhere / "features.csv").write_text("live\n", encoding="utf-8")

    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="扫了一圈没看到新文件"))

    assert refused["status"] == "error"
    # 跟进目标后这是实打实的"隔离期内被写过"，不再是"看不见所以不敢放"。
    assert refused["silence_probe"]["state"] == "written"
    assert any(str(root / "linked") in item and str(elsewhere) in item
               for item in refused["suspicious_files"])
    # 拒绝文案绝不能指使去动这些条目：删/改链接会抬高父目录 mtime，把解除通道
    # 永久关死（这正是本提交要修的"锁的锁"）。也不能让调用方去等一个不会到来的
    # 状态：mtime 只增不减，写过就再也核验不成静默，出路只有换目录。
    assert "先别动这些根里的任何条目" in refused["next_action"]
    assert "mtime 只增不减" in refused["next_action"]
    assert "唯一可执行的下一步是换目录" in refused["next_action"]
    assert "重新调用" not in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_follows_symlinked_file_to_its_target(tmp_path):
    """树内部的符号链接文件同样跟进：真实目标被改写就判 written。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    target = tmp_path / "real_features.csv"
    target.write_text("x,y\n", encoding="utf-8")
    state, root, orphan_id = _orphan_with_prepared_root(
        tmp_path, lambda r: os.symlink(target, r / "features.csv"))
    target.write_text("x,y\n1,2\n", encoding="utf-8")

    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="mtime 都很老，应该没人写"))

    assert refused["status"] == "error"
    assert refused["silence_probe"]["state"] == "written"
    assert any(str(root / "features.csv") in item and str(target) in item
               for item in refused["suspicious_files"])
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_releases_benign_in_root_symlink(tmp_path):
    """不误伤：latest -> results 这类根内良性链接，目标静默时照常解除。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    def build(root: Path) -> None:
        (root / "results").mkdir()
        (root / "results" / "features.csv").write_text("x,y\n", encoding="utf-8")
        os.symlink("results", root / "latest")

    state, root, orphan_id = _orphan_with_prepared_root(tmp_path, build)
    released = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="属主确认已停机，链接目标也一并核对过"))

    assert released["status"] == "success"
    assert released["silence_probe"]["state"] == "silent"
    # root / results / features.csv 三个 inode；latest 与 results 同 inode，只量一次。
    assert released["silence_probe"]["entries_checked"] == 3
    assert _active_output_conflicts(state, [str(root)]) == []


def test_resolve_unknown_orphan_survives_symlink_cycle(tmp_path):
    """自指链接不得把跟进变成无限遍历：(st_dev, st_ino) 去重后照常出结论。"""
    from nodes.experiment.tools.resource_manager import _resolve_unknown_orphan

    def build(root: Path) -> None:
        (root / "features.csv").write_text("x,y\n", encoding="utf-8")
        os.symlink(".", root / "loop")

    state, _root, orphan_id = _orphan_with_prepared_root(tmp_path, build)
    released = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="核对过全部条目，隔离期内无写入"))

    assert released["status"] == "success"
    assert released["silence_probe"]["entries_checked"] == 2


def test_resolve_unknown_orphan_refuses_broken_symlink_target(tmp_path):
    """只有目标真的够不着才 unavailable，且文案不得指使删掉它。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _orphan_with_prepared_root(
        tmp_path, lambda r: os.symlink(tmp_path / "never-existed", r / "dangling"))
    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="断链应该不算数吧"))

    assert refused["status"] == "error"
    assert refused["silence_probe"]["state"] == "unavailable"
    assert any(str(root / "dangling") in item and "符号链接目标够不着" in item
               for item in refused["silence_probe"]["unreadable"])
    # 可读性问题给出的补救是 chmod（不动 mtime，实测可重试）；断链只能换目录 ——
    # 两条都不指使任何会抬高 mtime、把解除通道关死的动作。
    assert "chmod" in refused["next_action"]
    assert "先别动这些根里的任何条目" in refused["next_action"]
    assert "output_paths" in refused["next_action"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_resolve_unknown_orphan_still_releases_plain_regular_tree(tmp_path):
    """不误伤：嵌套的纯常规文件全部早于 recorded_at 时仍能正常解除。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    def build(root: Path) -> None:
        nested = root / "results" / "step1"
        nested.mkdir(parents=True)
        (root / "features.csv").write_text("x,y\n", encoding="utf-8")
        (nested / "match_log.json").write_text("{}", encoding="utf-8")

    state, root, orphan_id = _orphan_with_prepared_root(tmp_path, build)
    released = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="属主确认已停机，逐个文件核对过"))

    assert released["status"] == "success"
    assert released["silence_probe"]["state"] == "silent"
    # 3 目录 + 2 文件；符号链接判定不得把常规条目挡在核验之外。
    assert released["silence_probe"]["entries_checked"] == 5
    assert _active_output_conflicts(state, [str(root)]) == []


def test_resolve_unknown_orphan_refuses_when_root_itself_became_symlink(tmp_path):
    """既有行为回归：根本身是符号链接时，真正被写的是别处，隔离保持。"""
    from nodes.experiment.tools.resource_manager import (
        _active_output_conflicts, _resolve_unknown_orphan,
    )

    state, root, orphan_id = _isolated_orphan(tmp_path, [])
    recorded_root = os.path.realpath(root)  # 登记时的真实根，之后才被换成链接
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "features.csv").unlink()
    root.rmdir()
    os.symlink(elsewhere, root)

    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="路径还在，应该没人写"))

    assert refused["status"] == "error"
    assert refused["silence_probe"]["state"] == "unavailable"
    assert refused["silence_probe"]["missing_roots"] == []
    assert any(recorded_root in item
               for item in refused["silence_probe"]["unreadable"])
    assert "符号链接" in refused["silence_probe"]["detail"]
    assert state.list_artifacts("unknown_orphan_resolution") == []
    assert _active_output_conflicts(state, [str(root)])


def test_silence_probe_puts_no_entry_cap_on_a_plain_symlink_free_tree(tmp_path):
    """回归：普通 MPI 输出布局（640 rank × 40 文件）不得被条目上限永久锁死。

    上一版对**所有**条目设 20000 上限，于是一棵完全没有符号链接的 26k 条目输出树
    直接判 unavailable —— 而 mtime 单调、上限是编译期常量，这个 orphan 再没有任何
    动作序列能解除，恰恰又是一把"修复锁的锁"，纯靠输出树大小就能触发。
    output_roots 登记时已被 realpath，根内遍历按构造必然终止，26k 条目 0.15s 量完。
    """
    from nodes.experiment.tools.resource_manager import (
        _SILENCE_MAX_CROSSED_ENTRIES, _probe_output_roots_silent,
    )

    root = tmp_path / "old-run"
    root.mkdir()
    ranks, per_rank = 640, 40
    stale = time.time() - 3600
    for rank in range(ranks):
        rank_dir = root / f"rank{rank:04d}"
        rank_dir.mkdir()
        for index in range(per_rank):
            entry = rank_dir / f"out{index:03d}.nc"
            entry.write_bytes(b"")
            os.utime(entry, (stale, stale))
        os.utime(rank_dir, (stale, stale))
    os.utime(root, (stale, stale))
    total = 1 + ranks + ranks * per_rank
    assert total > _SILENCE_MAX_CROSSED_ENTRIES

    probe = _probe_output_roots_silent(
        [str(root)], datetime.now(timezone.utc).isoformat())

    assert probe["state"] == "silent"
    assert probe["entries_checked"] == total


def test_silence_probe_budget_counts_only_cross_symlink_descent(tmp_path, monkeypatch):
    """预算只对跨出登记根的下降计数，且超限拒绝必须指名是哪条链接撑爆的。"""
    from nodes.experiment.tools import resource_manager as rm

    root = tmp_path / "old-run"
    (root / "results").mkdir(parents=True)
    huge = tmp_path / "huge"
    huge.mkdir()
    for index in range(8):
        (root / "results" / f"g{index}").write_bytes(b"")
        (huge / f"f{index}").write_bytes(b"")
    link = root / "linked"
    os.symlink(huge, link)
    stale = time.time() - 3600
    for path in [root, huge, *root.rglob("*"), *huge.rglob("*")]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    # 预算调小到远小于根内条目数：根内遍历若也吃预算，下面第二段必然一起挂掉。
    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 3)
    recorded_at = datetime.now(timezone.utc).isoformat()

    overflowed = rm._probe_output_roots_silent([str(root)], recorded_at)
    assert overflowed["state"] == "unavailable"
    assert overflowed["overflow_source"] == f"{link} -> {huge}"
    assert str(link) in overflowed["detail"] and str(huge) in overflowed["detail"]

    # 同一棵根去掉那条链接（unlink 抬高根 mtime，回拨后再核）：10 个根内条目
    # 远超预算 3，仍必须判静默 —— 上限对根内遍历不生效。
    os.unlink(link)
    os.utime(root, (stale, stale))
    plain = rm._probe_output_roots_silent([str(root)], recorded_at)
    assert plain["state"] == "silent"
    assert plain["entries_checked"] == 10


def test_sibling_output_root_link_never_eats_the_budget(tmp_path, monkeypatch):
    """指向同一条 orphan 另一个登记根的链接不算跨出（MOM6 OUTPUT/RESTART -> ../RESTART）。

    跨出判定若只跟当前这一个根比，兄弟根整棵树会被当成"根外"吃预算，超限即永久
    锁死；而根名字典序决定谁先被遍历，结论还会随改名而变。
    """
    from nodes.experiment.tools import resource_manager as rm

    output = tmp_path / "OUTPUT"
    restart = tmp_path / "RESTART"
    output.mkdir()
    restart.mkdir()
    for index in range(8):
        (output / f"o{index}.nc").write_bytes(b"")
        (restart / f"r{index}.res").write_bytes(b"")
    os.symlink(restart, output / "RESTART")
    stale = time.time() - 3600
    for path in [output, restart, *output.rglob("*"), *restart.rglob("*")]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    # 预算远小于兄弟根条目数：只要兄弟根被误判成"根外"就必然 overflow。
    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 3)
    recorded_at = datetime.now(timezone.utc).isoformat()

    for roots in ([str(output), str(restart)], [str(restart), str(output)]):
        probed = rm._probe_output_roots_silent(roots, recorded_at)
        assert probed["state"] == "silent", probed
        assert not probed.get("overflow_source")

    # 对照：真跑到全部登记根之外的链接仍 fail-closed 且指名。
    outside = tmp_path / "outside"
    outside.mkdir()
    for index in range(8):
        (outside / f"x{index}").write_bytes(b"")
    escape = output / "escape"
    os.symlink(outside, escape)
    for path in [output, outside, *outside.rglob("*")]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    escaped = rm._probe_output_roots_silent([str(output), str(restart)], recorded_at)
    assert escaped["state"] == "unavailable"
    assert escaped["overflow_source"] == f"{escape} -> {outside}"


def test_overflow_refusal_takes_its_own_next_action_branch(tmp_path, monkeypatch):
    """overflow 必须先于 unreadable 判，且不许诺一条走不通的 chmod。"""
    from nodes.experiment.tools import resource_manager as rm
    from nodes.experiment.tools.resource_manager import (
        _resolve_unknown_orphan, _unknown_orphan_silence_next_action,
    )

    huge = tmp_path / "huge"
    huge.mkdir()
    for index in range(8):
        (huge / f"f{index}").write_bytes(b"")

    def build(root: Path) -> None:
        os.symlink(huge, root / "linked")
        os.symlink(tmp_path / "never-existed", root / "dangling")

    state, root, orphan_id = _orphan_with_prepared_root(tmp_path, build)
    stale = time.time() - 600
    for path in [huge, *huge.iterdir()]:
        os.utime(path, (stale, stale))
    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 2)

    refused = asyncio.run(_resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="链接指向一棵巨树，先试试"))
    assert refused["status"] == "error"
    assert refused["silence_probe"]["overflow_source"].startswith(str(root / "linked"))
    assert str(root / "linked") in refused["next_action"]
    # chmod 在这里永远无效：无论权限如何，下一次核验照样在同一条链接上撑爆预算。
    assert "chmod" not in refused["next_action"]
    assert "output_paths" in refused["next_action"]

    # 路由本身钉死：unreadable 非空时（预算耗尽前攒下的 EACCES／断链条目）
    # 也必须走 overflow 分支，而不是那条 chmod 建议。
    routed = _unknown_orphan_silence_next_action(
        orphan_id, [str(root)],
        {"state": "unavailable", "unreadable": [f"{root}/x: Permission denied"],
         "missing_roots": [], "overflow_source": f"{root}/linked -> {huge}"})
    assert "chmod" not in routed
    assert str(huge) in routed


def test_in_root_symlink_never_eats_the_cross_root_budget(tmp_path, monkeypatch):
    """根内良性链接（latest -> results）不得吃预算，且结论不许取决于目录顺序。

    上一版的判据是"是不是符号链接"。遍历是 LIFO 栈 + (dev,ino) 去重：链接先出栈，
    整棵真实子树就被记成"跨链接到达"、吃满 20000 预算判 unavailable；真实目录先出栈，
    链接命中去重、一条都不吃、判 silent —— 差别纯粹是 ``os.listdir`` 的返回顺序，同一
    棵 26k 条目的树换个链接名就翻面（实测六个名字 2 触发 4 不触发）。这里把 listdir
    排序、链接命名 ``zz-latest``（pop 取末位 ⇒ 链接必先走），确定性复现那个最坏顺序。
    判据换成"realpath 之后还在不在这个根里"之后，目标在根内就是这批输出本身，不计预算。
    """
    from nodes.experiment.tools import resource_manager as rm

    real_listdir = os.listdir
    monkeypatch.setattr(rm.os, "listdir", lambda path: sorted(real_listdir(path)))

    root = tmp_path / "old-run"
    results = root / "results"
    results.mkdir(parents=True)
    ranks, per_rank = 640, 40
    for rank in range(ranks):
        rank_dir = results / f"rank{rank:04d}"
        rank_dir.mkdir()
        for index in range(per_rank):
            (rank_dir / f"out{index:03d}.nc").write_bytes(b"")
    os.symlink(results, root / "zz-latest")
    stale = time.time() - 3600
    for path in [root, *root.rglob("*")]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    recorded_at = datetime.now(timezone.utc).isoformat()

    probe = rm._probe_output_roots_silent([str(root)], recorded_at)

    assert probe["state"] == "silent"
    assert "overflow_source" not in probe
    # root + results（经 zz-latest 到达，同 inode 只计一次）+ 640 rank + 25600 文件。
    assert probe["entries_checked"] == 2 + ranks + ranks * per_rank

    # 唯一的差别是链接指到根外：那才是真正跨出登记根，仍然 fail-closed 并指名它。
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.nc").write_bytes(b"")
    os.symlink(outside, root / "zz-outside")
    for path in [root, outside, *outside.iterdir()]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 1)

    crossed = rm._probe_output_roots_silent([str(root)], recorded_at)
    assert crossed["state"] == "unavailable"
    assert crossed["overflow_source"] == f"{root / 'zz-outside'} -> {outside}"


def test_nested_mount_inside_the_root_does_not_eat_the_budget(tmp_path, monkeypatch):
    """无任何符号链接、只是 st_dev 变了（嵌套挂载）也曾确定性锁死。

    HPC 上的输出根底下常有节点本地 scratch bind mount、autofs／NFS submount、btrfs
    subvolume、overlayfs：挂载点仍在登记根内，那底下也是这批输出。上一版"st_dev 变了
    就算跨出根"于是把它们全部计入预算，超 20000 即永久锁死，全程不需要一条链接
    （复核者实测 ``_probe_output_roots_silent(['/run/user'], ...)`` 预算 3 即 unavailable）。
    """
    from nodes.experiment.tools import resource_manager as rm

    root = tmp_path / "old-run"
    nested = root / "scratch"
    nested.mkdir(parents=True)
    for index in range(8):
        (nested / f"f{index}.nc").write_bytes(b"")
    stale = time.time() - 3600
    for path in [root, *root.rglob("*")]:
        os.utime(path, (stale, stale))
    assert not any(path.is_symlink() for path in root.rglob("*"))

    real_stat = os.stat

    def faked_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path).startswith(str(nested)):  # 伪造挂载点以下的 st_dev
            fields = list(info)
            fields[2] = info.st_dev + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 3)
    monkeypatch.setattr(rm.os, "stat", faked_stat)
    probe = rm._probe_output_roots_silent(
        [str(root)], datetime.now(timezone.utc).isoformat())
    monkeypatch.undo()

    assert probe["state"] == "silent"
    assert "overflow_source" not in probe
    assert probe["entries_checked"] == 10  # root + scratch + 8 文件


def test_overflow_still_registers_the_remaining_output_roots(tmp_path, monkeypatch):
    """overflow 不得跳过其余根：否则第二个根早就没了要等下一轮才发现，白跑一轮。"""
    from nodes.experiment.tools import resource_manager as rm

    huge = tmp_path / "huge"
    huge.mkdir()
    for index in range(8):
        (huge / f"f{index}").write_bytes(b"")
    first = tmp_path / "run-a"
    first.mkdir()
    os.symlink(huge, first / "linked")
    present = tmp_path / "run-b"
    present.mkdir()
    (present / "keep.nc").write_bytes(b"")
    gone = tmp_path / "run-c"
    stale = time.time() - 3600
    for path in [huge, *huge.iterdir(), first, present, *present.iterdir()]:
        os.utime(path, (stale, stale), follow_symlinks=False)
    monkeypatch.setattr(rm, "_SILENCE_MAX_CROSSED_ENTRIES", 2)
    recorded_at = datetime.now(timezone.utc).isoformat()

    probe = rm._probe_output_roots_silent([str(first), str(present)], recorded_at)
    assert probe["state"] == "unavailable"
    assert probe["overflow_source"].startswith(str(first / "linked"))
    assert probe["deferred_roots"] == [str(present)]
    assert str(present) in probe["detail"]
    action = rm._unknown_orphan_silence_next_action("orphan-1", [str(first)], probe)
    assert str(present) in action
    assert "chmod" not in action

    # 第二个根已经消失：overflow 之后也必须当场报出来。
    vanished = rm._probe_output_roots_silent([str(first), str(gone)], recorded_at)
    assert vanished["missing_roots"] == [str(gone)]


def test_overflow_next_action_no_longer_blames_the_output_size(tmp_path):
    """文案必须与真实成因一致：撑爆预算的是根外那棵树，不是"跟输出规模无关"。"""
    from nodes.experiment.tools.resource_manager import (
        _unknown_orphan_silence_next_action,
    )

    action = _unknown_orphan_silence_next_action(
        "orphan-1", ["/data/old-run"],
        {"state": "unavailable", "unreadable": [], "missing_roots": [],
         "overflow_source": "/data/old-run/latest -> /scratch/huge"})

    assert "跨出了登记的输出根" not in action
    assert "跟输出规模、跟权限都无关" not in action
    assert "realpath" in action and "/scratch/huge" in action


def test_unreadable_directory_is_recoverable_with_the_advertised_chmod(tmp_path):
    """chmod 建议的模式位必须真能解开：不可读目录光有 +r 不够，os.stat 子项要 +x。"""
    from nodes.experiment.tools.resource_manager import _resolve_unknown_orphan

    if os.geteuid() == 0:
        pytest.skip("root 无视权限位，这条补救无法在 root 下验证")

    def build(root: Path) -> None:
        sub = root / "results"
        sub.mkdir()
        (sub / "features.csv").write_text("x,y\n", encoding="utf-8")

    state, root, orphan_id = _orphan_with_prepared_root(tmp_path, build)
    sub = root / "results"
    os.chmod(sub, 0o000)
    try:
        refused = asyncio.run(_resolve_unknown_orphan(
            state, artifact_id=orphan_id, reason="目录读不了，先看看怎么办"))
        assert refused["status"] == "error"
        assert refused["silence_probe"]["unreadable"]
        assert "chmod u+rX" in refused["next_action"]

        # 只给 +r 仍然过不去：agent 照"chmod +r"的字面做会白烧一轮。
        os.chmod(sub, 0o444)
        still = asyncio.run(_resolve_unknown_orphan(
            state, artifact_id=orphan_id, reason="只补了读权限"))
        assert still["status"] == "error"
        assert still["silence_probe"]["unreadable"]

        # u+rX 等价的 0o555 才真的解得开，且 chmod 不改 mtime、不毁掉静默证据。
        os.chmod(sub, 0o555)
        released = asyncio.run(_resolve_unknown_orphan(
            state, artifact_id=orphan_id, reason="权限修好后逐个条目复核过"))
        assert released["status"] == "success"
        assert released["silence_probe"]["state"] == "silent"
    finally:
        os.chmod(sub, 0o755)


def test_pid_host_match_refuses_same_short_name_in_a_different_domain(monkeypatch):
    """FQDN 不得截断比较：异域同短名是两台机器，不能拿本机 /proc 替它下结论。"""
    from nodes.experiment.tools import resource_manager as rm

    monkeypatch.setattr(rm.socket, "gethostname", lambda: "spr-cu01.dc1.internal")
    alive = os.getpid()

    foreign = rm._probe_observed_process_liveness(f"pid={alive}@spr-cu01.dc2.internal")
    assert foreign["state"] == "unavailable"
    assert "dc2.internal" in foreign["detail"]
    # 换成死 pid 更要命：截断比较会给另一台机器上的进程开死亡证明。
    dead = rm._probe_observed_process_liveness(f"pid={_dead_pid()}@spr-cu01.dc2.internal")
    assert dead["state"] == "unavailable"

    # 同域全限定、或任一方没写域（本集群自述常写短名）仍按同一台机器处理。
    assert rm._probe_observed_process_liveness(
        f"pid={alive}@spr-cu01.dc1.internal")["state"] == "alive"
    assert rm._probe_observed_process_liveness(f"pid={alive}@SPR-CU01")["state"] == "alive"
    monkeypatch.setattr(rm.socket, "gethostname", lambda: "spr-cu01")
    assert rm._probe_observed_process_liveness(
        f"pid={alive}@spr-cu01.dc1.internal")["state"] == "alive"


def test_submit_job_stops_reporting_orphan_conflict_after_resolution(
    tmp_path, monkeypatch,
):
    """端到端：解除前 submit_job 撞冲突 blocker，解除后同一提交越过该门。"""
    from shared.lib import dangerous_commands as danger
    from nodes.experiment.tools import execution_route, resource_manager as rm
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    # 本用例只验证冲突门；物理提交用桩观测，绝不启动真实作业。
    danger.set_bypass_mode(False)
    submitted: list[str] = []

    def fake_submit(*_args, **_kwargs):
        submitted.append("physical-submit")
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job", "job_id": "orphan-released",
            "submission_nonce": "orphan-released-nonce",
        }

    monkeypatch.setattr(rm, "_submit_sync", fake_submit)
    state = State.new("experiment", tmp_path)
    runtime = Path(experiment_output_dir(state, "runtime", create=True))
    root = runtime / "legacy"
    root.mkdir(parents=True)
    stale = root / "features.csv"
    stale.write_text("x,y\n", encoding="utf-8")
    old = time.time() - 600
    for path in (stale, root):
        os.utime(path, (old, old))
    state.hook_state["node_inputs"] = {"experiment_focus": "回归：核验 unknown_orphan 解除后 submit_job 不再撞输出根冲突。"}
    assert asyncio.run(_classify_experiment_scope(
        state, scope="operation",
        reason="回归：核验 unknown_orphan 解除后 submit_job 不再撞输出根冲突。",
    ))["status"] == "success"
    assert asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2, "goal": "unknown_orphan 冲突门回归",
        "evidence_refs": ["test:unknown-orphan-resolution"],
        "steps": [{"id": "submit_echo", "goal": "受管提交", "after": [],
                   "action": {"tool": "submit_job", "program": "echo"},
                   "effects": ["workspace_write", "process_tree", "external_job"],
                   "workdir_role": "run_root", "expected_outputs": []}],
    }))["status"] == "success"
    orphan_id = asyncio.run(
        rm._record_unknown_orphan(state, [str(root)], []))["artifact_id"]
    submission = dict(command="echo hi", scheduler="local", workdir=str(runtime),
                      output_paths=[str(root / "results")], dry_run=False,
                      route_step_id="submit_echo")

    blocked = asyncio.run(rm._submit_job(state, **submission))
    assert blocked["status"] == "error"
    assert blocked["blocker"]["kind"] == "active_external_job_output_conflict"
    assert blocked["blocker"]["conflicts"][0]["kind"] == "unknown_orphan"
    assert "resolve_unknown_orphan" in blocked["error"]

    released = asyncio.run(rm._resolve_unknown_orphan(
        state, artifact_id=orphan_id, reason="核查过这批遗留输出，隔离期内无写入"))
    assert released["status"] == "success"

    # 同一份参数再提交：冲突门放行，普通本地提交直达同一次物理提交。
    assert submitted == []
    retried = asyncio.run(rm._submit_job(state, **submission))
    assert retried["status"] == "success", retried
    assert "blocker" not in retried
    assert submitted == ["physical-submit"]


@requires_sandbox
def test_finalize_requires_frozen_analysis_log_and_closes_workflow(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _finalize_external_job, _submit_sync, _wait_for_external_job, unresolved_external_workflows,
    )

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    result = _submit_sync(
        runtime, "local", "printf complete", "finalize-check",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert result["status"] == "success"
    assert len(result["container_runtime_id"]) == 64
    try:
        state.save_artifact("job_submission", "job_submission_finalize", json.dumps(result))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            from nodes.experiment.tools.resource_manager import _job_status_sync
            status = _job_status_sync(
                "local", result["job_id"], None,
                container_runtime_id=result["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)
        waited = asyncio.run(_wait_for_external_job(
            state, "local", result["job_id"],
            max_wait_s=30))
        assert waited["wait_outcome"] == "scheduler_terminal"
        hooks.external_job_handoff_on_end(_ctx(state), SimpleNamespace(final_text=""))
        tasks = TaskList(state.project_root / "tasks").list_all()
        assert len(tasks) == 1 and tasks[0].status == "pending"
        log = _save_frozen(
            state, "experiment_log", "terminal_job_analysis",
            f"job_id={result['job_id']}\nverdict: inconclusive\noutputs inspected",
            metadata={"external_job_refs": [
                _external_job_ref(result),
            ]},
        )
        finalized = asyncio.run(_finalize_external_job(
            state, "local", result["job_id"], log["id"], "analyzed_inconclusive"))
        assert finalized["status"] == "success"
        assert not unresolved_external_workflows(state)
        assert TaskList(state.project_root / "tasks").list_all()[0].status == "completed"
        assert finalized["cleanup"]["container_runtime_id"] == result["container_runtime_id"]
        after = _job_status_sync(
            "local", result["job_id"], None,
            container_runtime_id=result["container_runtime_id"],
        )
        assert after["raw"]["stdout"] == "NOT_RUNNING"
        assert after["raw"]["sandbox_state"]["exists"] is False
        assert not Path(result["sandbox_control_dir"]).exists()
    finally:
        _cleanup_local_submission(result)


def test_finalize_rejects_substring_only_job_id_reference(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    state.save_artifact("job_submission", "submitted_job", json.dumps({
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": "123", "container_runtime_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }))
    log = _save_frozen(
        state, "experiment_log", "wrong_job_reference",
        "job_id=1234\noutputs inspected",
        metadata={"external_job_refs": [{
            "scheduler": "local", "job_id": "1234",
        }]},
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "scheduler_phase": "terminal",
    })

    finalized = asyncio.run(manager._finalize_external_job(
        state, "local", "123", log["id"], "analyzed_inconclusive"))

    assert finalized["status"] == "error"
    assert "metadata.external_job_refs" in finalized["error"]


def test_finalize_reports_exact_identity_recovery_for_missing_nonce_and_runtime(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    runtime_id = "a" * 64
    state = State.new("experiment", tmp_path)
    state.save_artifact("job_submission", "submitted_job", json.dumps({
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": "123", "submission_nonce": "route-immutable-nonce",
        "container_runtime_id": runtime_id,
    }))
    log = _save_frozen(
        state, "experiment_log", "incomplete_identity_log", "outputs inspected",
        metadata={"external_job_refs": [{
            "scheduler": "local", "job_id": "123",
        }]},
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "scheduler_phase": "terminal",
    })
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(
        manager, "_cleanup_local_job_for_finalization",
        lambda record: cleanup_calls.append(record) or {"status": "success"},
    )

    finalized = asyncio.run(manager._finalize_external_job(
        state, "local", "123", log["id"], "analyzed_inconclusive"))

    assert finalized["status"] == "error"
    assert finalized["error_code"] == "external_job_evidence_identity_mismatch"
    assert finalized["missing_identity_fields"] == [
        "submission_nonce", "container_runtime_id",
    ]
    assert finalized["required_external_job_ref"]["submission_nonce"] == "route-immutable-nonce"
    assert finalized["required_external_job_ref"]["container_runtime_id"] == runtime_id
    assert "external_job_workflow" in finalized["recovery"]
    assert cleanup_calls == []
    assert state.list_artifacts("external_job_lifecycle") == []


def test_local_finalize_cleanup_failure_blocks_closure_and_retry_is_idempotent(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import execution_route
    from nodes.experiment.tools import resource_manager as manager

    runtime_id = "c" * 64
    submission = {
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": "hf-finalize-retry", "submission_nonce": "nonce-retry",
        "container_runtime_id": runtime_id,
        "sandbox_control_dir": "/tmp/harness-sandbox-finalize-retry",
    }
    state = State.new("experiment", tmp_path)
    state.save_artifact(
        "job_submission", "finalize_retry_submission", json.dumps(submission),
    )
    log = _save_frozen(
        state, "experiment_log", "finalize_retry_log", "terminal output analyzed",
        metadata={
            "external_job_refs": [_external_job_ref(submission)],
        },
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal",
    })
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None,
    )
    lifecycle_calls = []
    completed_calls = []
    monkeypatch.setattr(
        manager, "_record_job_lifecycle",
        lambda *args, **kwargs: lifecycle_calls.append((args, kwargs)) or kwargs,
    )
    monkeypatch.setattr(
        manager, "_complete_handoff_tasks",
        lambda *args, **kwargs: (
            completed_calls.append((args, kwargs))
            or {"status": "success", "completed_task_ids": []}
        ),
    )
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {"status": "success"},
    )
    stop_calls = []
    release_calls = []
    container_exists = {"value": True}

    def inspect_container(name):
        assert name in {submission["job_id"], runtime_id}
        if not container_exists["value"]:
            return {"exists": False}
        return {
            "exists": True,
            "id": runtime_id,
            "managed": True,
            "kind": "job",
            "namespace": sandbox.sandbox_namespace(),
            "running": False,
            "name": submission["job_id"],
        }

    def stop_container(name, *, remove, expected_container_id):
        stop_calls.append((name, remove, expected_container_id))
        container_exists["value"] = False

    monkeypatch.setattr(sandbox, "inspect_container", inspect_container)
    monkeypatch.setattr(sandbox, "stop_container", stop_container)
    monkeypatch.setattr(
        sandbox, "release_reservation",
        lambda name: release_calls.append(str(name)),
    )
    cleanup_attempts = []

    def cleanup_once_then_succeed(path):
        cleanup_attempts.append(str(path))
        if len(cleanup_attempts) == 1:
            raise OSError("control cleanup unavailable")

    monkeypatch.setattr(sandbox, "cleanup_control_dir", cleanup_once_then_succeed)

    first = asyncio.run(manager._finalize_external_job(
        state, "local", submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert first["status"] == "finalized_needs_cleanup"
    assert first["workflow_status"] == "awaiting_cleanup"
    assert first["do_not_resubmit"] is True
    assert first["cleanup"]["container_runtime_id"] == runtime_id
    assert lifecycle_calls == []
    assert completed_calls == []
    blocker = first["blocker"]
    assert blocker["reason"] == "finalized_needs_cleanup"
    assert blocker["container_runtime_id"] == runtime_id
    assert blocker["submission_nonce"] == "nonce-retry"
    assert "native local-job/control-dir cleanup capability" in blocker["requested_action"]
    assert "Docker/control-dir cleanup" not in blocker["requested_action"]
    assert blocker in state.hook_state["blockers"]

    second = asyncio.run(manager._finalize_external_job(
        state, "local", submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert second["status"] == "success"
    assert second["cleanup"]["status"] == "success"
    assert len(lifecycle_calls) == 1
    assert len(completed_calls) == 1
    assert stop_calls == [
        (submission["job_id"], True, runtime_id),
    ]
    assert release_calls == [submission["job_id"]]
    assert cleanup_attempts == [submission["sandbox_control_dir"]] * 2
    assert not any(
        str(item.get("reported_by") or "").startswith(
            manager._FINALIZED_NEEDS_CLEANUP_PREFIX
        )
        for item in state.hook_state.get("blockers", [])
    )


def test_local_cleanup_inspect_error_fails_closed(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as manager

    record = {
        "scheduler": "local",
        "job_id": "hf-cleanup-inspect-error",
        "submission_nonce": "nonce-inspect-error",
        "container_runtime_id": "d" * 64,
        "sandbox_control_dir": str(tmp_path / "harness-sandbox-inspect-error"),
    }
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda name: {"exists": False, "error": "inspect_timeout"},
    )
    monkeypatch.setattr(
        sandbox, "stop_container",
        lambda *_a, **_k: pytest.fail("inspect error must block before stop"),
    )
    monkeypatch.setattr(
        sandbox, "cleanup_control_dir",
        lambda *_a, **_k: pytest.fail("inspect error must preserve control state"),
    )
    monkeypatch.setattr(
        sandbox, "release_reservation",
        lambda *_a, **_k: pytest.fail("inspect error must preserve reservation"),
    )

    result = manager._cleanup_local_job_for_finalization(record)

    assert result["status"] == "error"
    assert result["container_runtime_id"] == record["container_runtime_id"]
    assert result["sandbox_control_dir"] == record["sandbox_control_dir"]


def test_local_cleanup_silent_control_dir_noop_fails_closed(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as manager

    control_dir = tmp_path / "harness-sandbox-silent-noop"
    control_dir.mkdir()
    record = {
        "scheduler": "local",
        "job_id": "hf-cleanup-silent-noop",
        "submission_nonce": "nonce-silent-noop",
        "container_runtime_id": "e" * 64,
        "sandbox_control_dir": str(control_dir),
    }
    cleanup_calls = []
    release_calls = []
    monkeypatch.setattr(
        sandbox, "inspect_container", lambda name: {"exists": False},
    )
    monkeypatch.setattr(
        sandbox, "stop_container",
        lambda *_a, **_k: pytest.fail("absent exact container must not be stopped"),
    )
    monkeypatch.setattr(
        sandbox, "release_reservation",
        lambda name: release_calls.append(str(name)),
    )
    monkeypatch.setattr(
        sandbox, "cleanup_control_dir",
        lambda path: cleanup_calls.append(str(path)),
    )

    result = manager._cleanup_local_job_for_finalization(record)

    assert result["status"] == "error"
    assert control_dir.is_dir()
    assert release_calls == [record["job_id"]]
    assert cleanup_calls == [str(control_dir)]


def test_route_projection_failure_blocks_before_lifecycle_and_retry_resolves(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route
    from nodes.experiment.tools import resource_manager as manager

    submission = {
        "status": "success", "dry_run": False, "scheduler": "slurm",
        "job_id": "route-before-finalize",
        "launch_host": "login-a", "scheduler_cluster": "cluster-a",
        "submission_nonce": "nonce-route-before-finalize",
    }
    state = State.new("experiment", tmp_path)
    state.hook_state["blockers"] = {
        "reported_by": "legacy:malformed-blocker-container",
    }
    state.save_artifact(
        "job_submission", "route_before_finalize_submission",
        json.dumps(submission),
    )
    log = _save_frozen(
        state, "experiment_log", "route_before_finalize_log",
        "terminal output analyzed",
        metadata={
            "external_job_refs": [_external_job_ref(submission)],
        },
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal",
    })
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None,
    )
    order = []
    original_record_lifecycle = manager._record_job_lifecycle

    def record_lifecycle(*args, **kwargs):
        order.append("lifecycle")
        return original_record_lifecycle(*args, **kwargs)

    monkeypatch.setattr(manager, "_record_job_lifecycle", record_lifecycle)
    monkeypatch.setattr(
        manager, "_complete_handoff_tasks",
        lambda *_a, **_k: (
            order.append("task")
            or {"status": "success", "completed_task_ids": []}
        ),
    )
    route_results = iter([
        {"status": "error", "reason": "route ledger unavailable"},
        {"status": "success", "attempt_id": "attempt-1"},
    ])

    def project_route(*_args, **_kwargs):
        order.append("route")
        return next(route_results)

    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project_route,
    )

    first = asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert first["status"] == "finalized_needs_route_reconciliation"
    assert first["workflow_status"] == "awaiting_route_projection"
    assert order == ["route"]
    assert state.list_artifacts("external_job_lifecycle") == []
    assert isinstance(state.hook_state["blockers"], list)
    route_blockers = [
        item for item in state.hook_state["blockers"]
        if str(item.get("reported_by") or "").startswith(
            manager._FINALIZED_NEEDS_ROUTE_PREFIX
        )
    ]
    assert len(route_blockers) == 1
    assert route_blockers[0]["submission_nonce"] == submission["submission_nonce"]

    second = asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert second["status"] == "success"
    assert order == ["route", "route", "task", "lifecycle"]
    assert not any(
        str(item.get("reported_by") or "").startswith(
            manager._FINALIZED_NEEDS_ROUTE_PREFIX
        )
        for item in state.hook_state["blockers"]
    )


def test_finalize_task_failure_retries_before_lifecycle_and_resolves_exact_blocker(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route
    from nodes.experiment.tools import resource_manager as manager

    submission = {
        "status": "success", "dry_run": False, "scheduler": "slurm",
        "job_id": "task-before-finalize",
        "launch_host": "login-a", "scheduler_cluster": "cluster-a",
        "submission_nonce": "nonce-task-before-finalize",
    }
    state = State.new("experiment", tmp_path)
    unrelated = {
        "reported_by": manager._EXTERNAL_JOB_NEEDS_TASK_PREFIX + "unrelated",
        "reason": "external_job_needs_task_reconciliation",
        "job_id": "other-job",
    }
    state.hook_state["blockers"] = [unrelated]
    state.save_artifact(
        "job_submission", "task_before_finalize_submission",
        json.dumps(submission),
    )
    log = _save_frozen(
        state, "experiment_log", "task_before_finalize_log",
        "terminal output analyzed",
        metadata={
            "external_job_refs": [_external_job_ref(submission)],
        },
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal",
    })
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None,
    )
    order = []
    original_record_lifecycle = manager._record_job_lifecycle

    def record_lifecycle(*args, **kwargs):
        order.append("lifecycle")
        return original_record_lifecycle(*args, **kwargs)

    task_results = iter([
        {
            "status": "error",
            "reason": "handoff_task_completion_failed",
            "error_type": "OSError",
            "error": "task ledger unavailable",
            "completed_task_ids": [],
        },
        {"status": "success", "completed_task_ids": ["task-a"]},
    ])

    def complete_tasks(*_args, **_kwargs):
        order.append("task")
        return next(task_results)

    def project_route(*_args, **_kwargs):
        order.append("route")
        return {"status": "success", "attempt_id": "attempt-task"}

    monkeypatch.setattr(manager, "_record_job_lifecycle", record_lifecycle)
    monkeypatch.setattr(manager, "_complete_handoff_tasks", complete_tasks)
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization", project_route,
    )

    first = asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    exact_marker = manager._external_job_needs_task_reported_by(submission)
    assert first["status"] == "finalized_needs_task_reconciliation"
    assert first["do_not_resubmit"] is True
    assert first["task_completion"]["status"] == "error"
    assert order == ["route", "task"]
    assert state.list_artifacts("external_job_lifecycle") == []
    exact_blockers = [
        item for item in state.hook_state["blockers"]
        if item.get("reported_by") == exact_marker
    ]
    assert len(exact_blockers) == 1
    assert exact_blockers[0]["submission_nonce"] == submission["submission_nonce"]
    assert exact_blockers[0]["closure_kind"] == "finalize"

    second = asyncio.run(manager._finalize_external_job(
        state, submission["scheduler"], submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert second["status"] == "success"
    assert order == ["route", "task", "route", "task", "lifecycle"]
    assert manager.lifecycle_for_submission(state, submission)["status"] == "finalized"
    assert not any(
        item.get("reported_by") == exact_marker
        for item in state.hook_state["blockers"]
    )
    assert unrelated in state.hook_state["blockers"]


def test_successful_finalize_clears_only_exact_handoff_blocker(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route
    from nodes.experiment.tools import resource_manager as manager

    submissions = [
        {
            "status": "success", "dry_run": False, "scheduler": "slurm",
            "job_id": "job-a", "launch_host": "login-a",
            "scheduler_cluster": "cluster-a", "submission_nonce": "nonce-a",
        },
        {
            "status": "success", "dry_run": False, "scheduler": "slurm",
            "job_id": "job-b", "launch_host": "login-a",
            "scheduler_cluster": "cluster-a", "submission_nonce": "nonce-b",
        },
    ]
    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    for index, submission in enumerate(submissions):
        state.save_artifact(
            "job_submission", f"selective_handoff_{index}",
            json.dumps(submission),
        )
    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _row: "running")
    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)

    expected_markers = {
        manager._external_job_handoff_reported_by(item) for item in submissions
    }
    assert {
        item.get("reported_by") for item in state.hook_state["blockers"]
        if str(item.get("reported_by") or "").startswith(
            manager._EXTERNAL_JOB_HANDOFF_BLOCKER_PREFIX
        )
    } == expected_markers

    log = _save_frozen(
        state, "experiment_log", "selective_handoff_log", "job-a output analyzed",
        metadata={
            "external_job_refs": [_external_job_ref(submissions[0])],
        },
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal",
    })
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        execution_route, "record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "attempt-a"},
    )

    finalized = asyncio.run(manager._finalize_external_job(
        state, submissions[0]["scheduler"], submissions[0]["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert finalized["status"] == "success"
    remaining_markers = {
        item.get("reported_by") for item in state.hook_state["blockers"]
        if str(item.get("reported_by") or "").startswith(
            manager._EXTERNAL_JOB_HANDOFF_BLOCKER_PREFIX
        )
    }
    assert remaining_markers == {
        manager._external_job_handoff_reported_by(submissions[1])
    }


def test_local_finalize_missing_runtime_id_never_stops_mutable_name(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as manager

    submission = {
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": "hf-finalize-no-runtime", "submission_nonce": "nonce-missing",
        "sandbox_control_dir": "/tmp/harness-sandbox-finalize-missing",
    }
    state = State.new("experiment", tmp_path)
    state.save_artifact(
        "job_submission", "finalize_missing_runtime", json.dumps(submission),
    )
    log = _save_frozen(
        state, "experiment_log", "finalize_missing_runtime_log", "terminal output analyzed",
        metadata={
            "external_job_refs": [_external_job_ref(submission)],
        },
    )
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success", "scheduler_phase": "terminal",
    })
    monkeypatch.setattr(
        manager, "_persist_execution_environment_evidence", lambda *_a, **_k: None,
    )
    stop_calls = []
    monkeypatch.setattr(
        sandbox, "stop_container", lambda *args, **kwargs: stop_calls.append(
            (args, kwargs)
        ),
    )
    monkeypatch.setattr(
        manager, "_record_job_lifecycle",
        lambda *_a, **_k: pytest.fail("lifecycle closed before local cleanup"),
    )
    monkeypatch.setattr(
        manager, "_complete_handoff_tasks",
        lambda *_a, **_k: pytest.fail("task closed before local cleanup"),
    )

    result = asyncio.run(manager._finalize_external_job(
        state, "local", submission["job_id"], log["id"],
        "analyzed_inconclusive",
    ))

    assert result["status"] == "finalized_needs_cleanup"
    assert result["cleanup"]["error_type"] == "missing_immutable_container_runtime_id"
    assert result["blocker"]["container_runtime_id"] is None
    assert stop_calls == []


def test_namespace_scopes_lifecycle_and_unresolved_workflows_independently(
        tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    for namespace in ("alpha", "beta"):
        state.save_artifact("job_submission", f"job_{namespace}", json.dumps({
            "status": "success", "dry_run": False, "scheduler": "kubernetes",
            "job_id": "shared-name", "namespace": namespace,
        }))
    state.save_artifact("external_job_lifecycle", "alpha_finalized", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name",
        "namespace": "alpha", "lifecycle_status": "finalized",
    }))
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "workflow_status": "awaiting_external_job",
    })

    unresolved = manager.unresolved_external_workflows(state)

    assert [row["namespace"] for row in unresolved] == ["beta"]

    handoff_state = State.new("experiment", tmp_path / "handoff")
    handoff_state.project_root = tmp_path / "handoff-project"
    for namespace in ("alpha", "beta"):
        assert hooks.persist_external_job_workflow(handoff_state, {
            "status": "success", "dry_run": False, "scheduler": "kubernetes",
            "job_id": "shared-name", "namespace": namespace,
        }) is not None
    assert len(TaskList(handoff_state.project_root / "tasks").list_all()) == 2
    assert len(handoff_state.list_artifacts("external_job_workflow")) == 2


def test_known_submission_receipt_damage_blocks_core_finalization(tmp_path):
    """A ledger-declared receipt may never disappear through tolerant listing."""
    from core.agent_loop import LoopResult
    from core.executor import finalize_run
    from core.harness import NodeHarness
    from core.loop_hooks import run_on_end

    state = State.new("experiment", tmp_path)
    artifact = state.save_artifact("job_submission", "receipt_under_test", json.dumps({
        "status": "success", "dry_run": False, "scheduler": "local", "job_id": "4242",
    }))
    path = state.find_artifact_path(artifact["id"])
    assert path is not None
    path.write_text("{not valid JSON", encoding="utf-8")

    loop_result = LoopResult(final_text="model claimed completion", turns=1,
                             tool_calls=[], messages=[], status="completed")
    asyncio.run(run_on_end([hooks.external_job_handoff], _ctx(state), loop_result))

    assert any(item.get("blocker_id") == "experiment_job_submission_read_error"
               for item in state.hook_state["blockers"])
    assert artifact["id"] in state.hook_state["experiment_downstream_blocked"]["reason"]
    summary = asyncio.run(finalize_run(
        state, NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result, llm=None))
    assert summary["status"] == "blocked"


@pytest.mark.parametrize(("kind", "artifact_type"), [
    ("content", "job_submission"),
    ("missing", "job_submission"),
    ("content", "external_job_submission_recovery"),
])
def test_strict_submission_reader_rejects_content_and_missing_receipts(
        tmp_path, kind, artifact_type):
    state = State.new("experiment", tmp_path)
    artifact = state.save_artifact(artifact_type, "receipt_under_test", json.dumps({
        "status": "success", "dry_run": False, "scheduler": "local", "job_id": "4242",
    }))
    path = state.find_artifact_path(artifact["id"])
    assert path is not None
    if kind == "content":
        # 正文就是盘上的文件：直接写坏。
        path.write_text("{not valid JSON", encoding="utf-8")
    else:
        path.unlink()

    with pytest.raises(hooks.SubmissionLedgerError, match=artifact["id"]):
        hooks._job_submission_records(state)


def test_submission_ledger_blocker_survives_transcript_write_failure(tmp_path):
    state = State.new("experiment", tmp_path)
    artifact = state.save_artifact("job_submission", "corrupt_receipt", json.dumps({
        "status": "success", "dry_run": False, "scheduler": "local", "job_id": "4242",
    }))
    path = state.find_artifact_path(artifact["id"])
    assert path is not None
    path.write_text("{not valid JSON", encoding="utf-8")
    state.append_transcript = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("transcript unavailable"))

    loop_result = SimpleNamespace(status="completed", final_text="")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)

    assert any(item.get("blocker_id") == "experiment_job_submission_read_error"
               for item in state.hook_state["blockers"])
    assert loop_result.status == "blocked"


def test_no_submission_receipt_keeps_normal_completion_path(tmp_path):
    state = State.new("experiment", tmp_path)
    loop_result = SimpleNamespace(status="completed", final_text="")

    hooks.external_job_handoff_on_end(_ctx(state), loop_result)

    assert state.hook_state.get("blockers", []) == []
    assert loop_result.status == "completed"


def _save_submission(state, name, *, scheduler="kubernetes", job_id="shared-name",
                     namespace=None, launch_host=None):
    return state.save_artifact("job_submission", name, json.dumps({
        "status": "success", "dry_run": False, "scheduler": scheduler,
        "job_id": job_id, "namespace": namespace, "launch_host": launch_host,
        "output_roots": [str(state.root / "outputs")],
    }))


def test_unique_legacy_lifecycle_is_read_for_one_scoped_submission(tmp_path):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    _save_submission(state, "alpha", namespace="alpha")
    state.save_artifact("external_job_lifecycle", "legacy_finalized", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name",
        "lifecycle_status": "finalized",
    }))

    submission = manager._submission_payloads(state)[0]
    resolution = manager.lifecycle_for_submission(state, submission)

    assert resolution["status"] == "finalized"
    assert resolution["resolution"] == "unique_legacy"
    assert manager.unresolved_external_workflows(state) == []


def test_ambiguous_legacy_lifecycle_stays_open_and_blocks_cancellation(
        tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    _save_submission(state, "alpha", namespace="alpha")
    _save_submission(state, "beta", namespace="beta")
    state.save_artifact("external_job_lifecycle", "legacy_finalized", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name",
        "lifecycle_status": "finalized",
    }))
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "workflow_status": "awaiting_external_job",
    })
    monkeypatch.setattr(manager, "_external_job_is_active", lambda *_args, **_kwargs: True)

    submissions = manager._submission_payloads(state)
    assert {manager.lifecycle_for_submission(state, row)["resolution"]
            for row in submissions} == {"legacy_lifecycle_scope_ambiguous"}
    unresolved = manager.unresolved_external_workflows(state)
    assert {row["namespace"] for row in unresolved} == {"alpha", "beta"}
    assert {row["workflow_status"] for row in unresolved} == {
        "legacy_lifecycle_scope_ambiguous"}
    conflicts = manager._active_output_conflicts(state, [str(state.root / "outputs")])
    assert {row["kind"] for row in conflicts} == {"legacy_lifecycle_scope_ambiguous"}
    cancelled = asyncio.run(manager._cancel_job(
        state, "kubernetes", "shared-name", namespace="alpha", reason="test"))
    assert cancelled["status"] == "error"
    assert "legacy_lifecycle_scope_ambiguous" in cancelled["error"]


def test_exact_scoped_lifecycle_beats_legacy_and_unscoped_history_is_unchanged(tmp_path):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    _save_submission(state, "alpha", namespace="alpha")
    state.save_artifact("external_job_lifecycle", "legacy_finalized", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name",
        "lifecycle_status": "finalized",
    }))
    state.save_artifact("external_job_lifecycle", "exact_cancelled", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name", "namespace": "alpha",
        "lifecycle_status": "cancelled",
    }))
    exact = manager.lifecycle_for_submission(state, manager._submission_payloads(state)[0])
    assert exact["status"] == "cancelled"
    assert exact["resolution"] == "exact"

    legacy = State.new("experiment", tmp_path / "legacy")
    _save_submission(legacy, "local", scheduler="local", job_id="4242")
    legacy.save_artifact("external_job_lifecycle", "old_local", json.dumps({
        "scheduler": "local", "job_id": "4242", "lifecycle_status": "finalized",
    }))
    unscoped = manager.lifecycle_for_submission(
        legacy, manager._submission_payloads(legacy)[0])
    assert unscoped["status"] == "finalized"
    assert unscoped["resolution"] == "exact"


def test_kubernetes_uid_prevents_recreated_job_lifecycle_aliasing(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path)
    for name, uid in (("old", "uid-old"), ("new", "uid-new")):
        state.save_artifact("job_submission", name, json.dumps({
            "status": "success", "dry_run": False, "scheduler": "kubernetes",
            "job_id": "shared-name", "namespace": "alpha", "resource_uid": uid,
        }))
    state.save_artifact("external_job_lifecycle", "old_finalized", json.dumps({
        "scheduler": "kubernetes", "job_id": "shared-name", "namespace": "alpha",
        "resource_uid": "uid-old", "lifecycle_status": "finalized",
    }))
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "workflow_status": "awaiting_external_job",
    })

    unresolved = manager.unresolved_external_workflows(state)

    assert [row["resource_uid"] for row in unresolved] == ["uid-new"]


def test_submit_persists_workflow_task_before_on_end(tmp_path):
    state = _State(tmp_path, [_submission()])
    task_id = hooks.persist_external_job_workflow(state, _submission())

    assert task_id == "T01"
    tasks = TaskList(state.project_root / "tasks").list_all()
    assert len(tasks) == 1
    assert "experiment_workflow_status=awaiting_external_job" in tasks[0].description
    assert state.hook_state["external_job_waiting"]["review_eligibility"] is False
    assert any(event["event"] == "external_job_workflow_persisted" for event in state.events)


def test_stop_with_running_job_is_vetoed_and_points_at_managed_wait(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    hooks.persist_external_job_workflow(state, _submission())
    monkeypatch.setattr(hooks, "_unresolved_external_workflows", lambda _: [{
        "scheduler": "local", "job_id": "4242",
        "workflow_status": "awaiting_external_job",
        "health": {"scheduler_phase": "running", "expected_duration_s": 30},
    }])

    messages = _run_finish_gate(state)

    assert messages, "收尾闸必须否决本次收尾"
    text = messages[0].content
    assert "wait_for_external_job" in text
    assert "max_wait_s=30" in text
    assert "scheduler_phase=running" in text
    assert any(event["event"] == "external_job_closure_gate" for event in state.events)
    assert any(event.get("action") == "veto_finish" for event in state.events)


def test_stop_with_terminal_job_is_vetoed_and_points_at_finalize(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    monkeypatch.setattr(hooks, "_unresolved_external_workflows", lambda _: [{
        "scheduler": "local", "job_id": "4242",
        "workflow_status": "awaiting_analysis",
        "health": {"scheduler_phase": "terminal", "health_state": "failure_signal"},
    }])

    messages = _run_finish_gate(state)

    assert messages
    text = messages[0].content
    assert "finalize_external_job" in text
    assert "scheduler_phase=terminal" in text
    assert "health_state=failure_signal" in text
    # 出口要说全：finalize 拒绝时按拒绝走，没有出口时报 blocker，不许改用 cancel 绕开
    assert "report_blocker" in text
    assert "cancel_job" in text
    # 一次性限制必须对模型明说，不能再宣称"强制"
    assert "只拦一次" in text


def test_finish_gate_does_not_teach_an_order_forked_by_run_mode(tmp_path, monkeypatch):
    """同一终态作业，operational 与 scientific 两种分类下拿到同一段指引。

    旧文案按 run 模式分叉，在 operational 分支里教"先 record_operation_completion
    再 finalize"，而那是 run 级单向门；2026-09-08 活体里模型正是被这条顺序推进死路。
    """
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    row = {"scheduler": "local", "job_id": "4242",
           "workflow_status": "awaiting_analysis",
           "health": {"scheduler_phase": "terminal"}}

    operational = _State(tmp_path / "op", [_submission()])
    _bind_operation_inputs(operational)
    asyncio.run(_classify_experiment_scope(
        operational, scope="operation", operation_category="job_observation",
        reason="Observe an existing managed scheduler job without scientific analysis.",
    ))
    scientific = _State(tmp_path / "sci", [_submission()])
    monkeypatch.setattr(hooks, "_unresolved_external_workflows", lambda _: [row])

    op_text = _run_finish_gate(operational)[0].content
    sci_text = _run_finish_gate(scientific)[0].content

    assert op_text == sci_text
    assert "record_operation_completion" not in op_text




@requires_sandbox
def test_operational_job_closes_with_compact_experiment_log(tmp_path):
    from nodes.experiment.tools.resource_manager import (
        _finalize_external_job, _job_status_sync, _submit_sync, _wait_for_external_job,
        unresolved_external_workflows,
    )
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Run and verify a managed toolchain smoke job only.",
    ))
    result = _submit_sync(
        runtime, "local", "printf complete", "operation-finalize-check",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert result["status"] == "success"
    assert len(result["container_runtime_id"]) == 64
    try:
        state.save_artifact("job_submission", "operation_job_submission", json.dumps(result))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = _job_status_sync(
                "local", result["job_id"], None,
                container_runtime_id=result["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)
        waited = asyncio.run(_wait_for_external_job(
            state, "local", result["job_id"],
            max_wait_s=30))
        assert waited["wait_outcome"] == "scheduler_terminal"
        log = _save_frozen(
            state, "experiment_log", "operation_log",
            ("## Execution\njob_id: {}\ncommand: printf complete\n".format(result["job_id"]) +
            "## Verification\nscheduler_terminal: passed\n## Result\nstatus: completed\n"),
            metadata={"analysis_eligible": False,
                      "record_kind": "operation", "execution_scope": "operation",
                      "external_job_refs": [_external_job_ref(result)]},
        )
        finalized = asyncio.run(_finalize_external_job(
            state, "local", result["job_id"], log["id"], "operation_completed"))

        assert finalized["status"] == "success", finalized
        assert "execution_mode" not in finalized, "作业层不回显 run 身份"
        assert not unresolved_external_workflows(state)
    finally:
        _cleanup_local_submission(result)


@requires_sandbox
def test_running_managed_job_cannot_be_recorded_as_successful_operation(tmp_path):
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    from nodes.experiment.tools.resource_manager import _cancel_sync, _submit_sync
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Run a managed operation and verify it only after terminal state.",
    ))
    submission = _submit_sync(
        runtime, "local", "sleep 5", "operation-running-check",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert submission["status"] == "success"
    assert len(submission["container_runtime_id"]) == 64
    try:
        state.save_artifact(
            "job_submission", "operation_running_submission", json.dumps(submission))

        completion = asyncio.run(_record_operation_completion(
            state, task_kind="external_job",
            objective="wait for the managed operation to finish",
            outcome="success", job_ids=[submission["job_id"]],
        ))

        assert completion["status"] == "error"
        assert completion["error_code"] == "external_jobs_not_terminal"
        assert completion["checks"][0]["name"] == "external_jobs_terminal"
        assert completion["checks"][0]["passed"] is False
    finally:
        _cleanup_local_submission(submission)


@requires_sandbox
def test_terminal_operation_job_closes_through_the_unique_completion_writer(tmp_path):
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion, audit_operation_completion,
    )
    from nodes.experiment.tools.resource_manager import (
        _finalize_external_job, _job_status_sync, _submit_sync,
        unresolved_external_workflows,
    )
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="job_observation",
        reason="Run and close one managed external-job observation.",
    ))
    submission = _submit_sync(
        runtime, "local", "printf complete", "operation-composed-close",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert submission["status"] == "success"
    assert len(submission["container_runtime_id"]) == 64
    try:
        state.save_artifact(
            "job_submission", "operation_composed_submission", json.dumps(submission))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = _job_status_sync(
                "local", submission["job_id"], None,
                container_runtime_id=submission["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)

        completion = asyncio.run(_record_operation_completion(
            state, task_kind="external_job",
            objective="verify and close the managed operation",
            outcome="success", job_ids=[submission["job_id"]],
        ))
        assert completion["status"] == "success", completion
        success_check = next(
            item for item in completion["checks"]
            if item["name"] == "external_jobs_successful"
        )
        assert success_check["passed"] is True
        assert success_check["evidence"]["health"][0]["success_evidence"]["returncode"] == 0

        log = state.read_artifact(completion["experiment_log_artifact_id"])
        refs = (log.get("metadata") or {}).get("external_job_refs")
        assert refs and refs[0]["scheduler"] == "local"
        assert refs[0]["job_id"] == submission["job_id"]

        finalized = asyncio.run(_finalize_external_job(
            state, "local", submission["job_id"],
            completion["experiment_log_artifact_id"], "operation_completed",
        ))

        assert finalized["status"] == "success", finalized
        assert audit_operation_completion(state)["passed"] is True
        assert not unresolved_external_workflows(state)
        assert len(state.list_artifacts("raw_results", own_only=True)) == 1
        assert len(state.list_artifacts("clean_results", own_only=True)) == 1
        assert len(state.list_artifacts("experiment_log", own_only=True)) == 1
    finally:
        _cleanup_local_submission(submission)


@requires_sandbox
def test_terminal_toolchain_build_refs_close_through_unique_completion_writer(tmp_path):
    """A managed toolchain build freezes exact refs before local cleanup."""
    from core.sandbox import inspect_container
    from nodes.experiment.tools import execution_route
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion, audit_operation_completion,
    )
    from nodes.experiment.tools.resource_manager import (
        _finalize_external_job, _job_status_sync, _submit_job,
        unresolved_external_workflows,
    )
    from nodes.experiment.tools.run_contract import _classify_experiment_scope
    from shared.lib import dangerous_commands as danger

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    output = runtime / "toolchain-complete.txt"
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Build and close one managed toolchain operation with exact refs.",
    ))
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route={
            "schema_version": 2,
            "goal": "Build and verify one managed toolchain output.",
            "evidence_refs": ["test:toolchain-build-composed-close"],
            "steps": [{
                "id": "build",
                "goal": "Write the bounded build sentinel in a managed job.",
                "after": [],
                "action": {"tool": "submit_job", "program": "printf"},
                "effects": ["external_job", "process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": ["toolchain-complete.txt"],
            }],
        },
    ))
    assert declared["status"] == "success", declared
    danger.set_bypass_mode(True)
    try:
        submission = asyncio.run(_submit_job(
            state,
            command="printf complete > toolchain-complete.txt",
            scheduler="local",
            job_name="toolchain-build-composed-close",
            mpi_ranks=1,
            cpus_per_rank=1,
            gpus=0,
            memory_gb=1.0,
            storage_gb=8.0,
            walltime_minutes=1,
            workdir=str(runtime),
            dry_run=False,
            output_paths=[str(runtime)],
            expected_duration_s=60,
            route_step_id="build",
            execution_params={"case": "toolchain-build-composed-close"},
        ))
    finally:
        danger.set_bypass_mode(False)
    assert submission["status"] == "success"
    assert len(submission["container_runtime_id"]) == 64
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = _job_status_sync(
                "local", submission["job_id"], None,
                container_runtime_id=submission["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)
        assert output.read_text(encoding="utf-8") == "complete"

        completion = asyncio.run(_record_operation_completion(
            state,
            task_kind="toolchain_build",
            objective="verify and close the managed toolchain build",
            outcome="success",
            artifact_paths=[str(output)],
            external_job_refs=[_external_job_ref(submission)],
        ))
        assert completion["status"] == "success", completion
        success_check = next(
            item for item in completion["checks"]
            if item["name"] == "external_jobs_successful"
        )
        assert success_check["passed"] is True
        assert success_check["evidence"]["health"][0]["success_evidence"]["returncode"] == 0

        log = state.read_artifact(completion["experiment_log_artifact_id"])
        refs = (log.get("metadata") or {}).get("external_job_refs")
        assert refs and refs[0]["scheduler"] == "local"
        assert refs[0]["job_id"] == submission["job_id"]
        assert refs[0]["container_runtime_id"] == submission["container_runtime_id"]
        clean = json.loads(state.read_artifact(
            completion["clean_results_artifact_id"])["content"])
        assert clean["task_kind"] == "build"

        finalized = asyncio.run(_finalize_external_job(
            state, "local", submission["job_id"],
            completion["experiment_log_artifact_id"], "operation_completed",
        ))

        assert finalized["status"] == "success", finalized
        assert inspect_container(submission["container_runtime_id"])["exists"] is False
        assert audit_operation_completion(state)["passed"] is True
        assert not unresolved_external_workflows(state)
        assert len(state.list_artifacts("raw_results", own_only=True)) == 1
        assert len(state.list_artifacts("clean_results", own_only=True)) == 1
        assert len(state.list_artifacts("experiment_log", own_only=True)) == 1
    finally:
        _cleanup_local_submission(submission)


@requires_sandbox
def test_terminal_local_exit7_cannot_be_recorded_as_success(tmp_path):
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    from nodes.experiment.tools.resource_manager import _cancel_sync, _job_status_sync, _submit_sync
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Verify that a terminal non-zero local job is not success.",
    ))
    submission = _submit_sync(
        runtime, "local", "exit 7", "operation-exit-seven",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert submission["status"] == "success"
    assert len(submission["container_runtime_id"]) == 64
    try:
        state.save_artifact(
            "job_submission", "operation_exit_seven", json.dumps(submission))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = _job_status_sync(
                "local", submission["job_id"], None,
                container_runtime_id=submission["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)

        completion = asyncio.run(_record_operation_completion(
            state, task_kind="external_job",
            objective="reject the failed managed operation",
            outcome="success", job_ids=[submission["job_id"]],
        ))

        assert completion["status"] == "error"
        assert completion["error_code"] == "external_job_success_unverified"
        success_check = completion["checks"][1]
        assert success_check["name"] == "external_jobs_successful"
        evidence = success_check["evidence"]["health"][0]["success_evidence"]
        assert evidence["verified"] is True
        assert evidence["succeeded"] is False
        assert evidence["returncode"] == 7
        assert not state.list_artifacts("raw_results")
    finally:
        cleanup = _cleanup_local_submission(submission)
        assert cleanup and cleanup["ok"] is True, cleanup


@requires_sandbox
def test_cancelled_local_job_cannot_be_recorded_as_success(tmp_path):
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    from nodes.experiment.tools.resource_manager import (
        _cancel_sync, _job_status_sync, _record_job_lifecycle, _submit_sync,
    )
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    runtime = experiment_output_dir(state, "runtime", create=True)
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build",
        reason="Verify that cancellation is a terminal failure, not success.",
    ))
    submission = _submit_sync(
        runtime, "local", "sleep 5", "operation-cancelled",
        1, 1, 0, 1.0, 8.0, 1, None, None, None, str(runtime), False, None,
        output_paths=[str(runtime)], expected_duration_s=60,
        stage_in=None,
        state=state,
    )
    assert submission["status"] == "success"
    assert len(submission["container_runtime_id"]) == 64
    try:
        state.save_artifact(
            "job_submission", "operation_cancelled", json.dumps(submission))
        assert _cancel_sync(
            "local", submission["job_id"], None,
            container_runtime_id=submission["container_runtime_id"],
        )["ok"] is True
        _record_job_lifecycle(
            state, scheduler="local", job_id=submission["job_id"],
            lifecycle_status="cancelled", reason="test cancellation",
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = _job_status_sync(
                "local", submission["job_id"], None,
                container_runtime_id=submission["container_runtime_id"],
            )
            if status["raw"]["stdout"] == "NOT_RUNNING":
                break
            time.sleep(0.02)

        completion = asyncio.run(_record_operation_completion(
            state, task_kind="external_job",
            objective="reject the cancelled managed operation",
            outcome="success", job_ids=[submission["job_id"]],
        ))

        assert completion["status"] == "error"
        assert completion["error_code"] == "external_job_success_unverified"
        evidence = completion["checks"][1]["evidence"]["health"][0]["success_evidence"]
        assert evidence["reason"] == "lifecycle_cancelled"
    finally:
        _cleanup_local_submission(submission)


def test_unknown_managed_job_state_cannot_be_recorded_as_success(
        tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_manager as manager
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="job_observation",
        reason="An unknown scheduler state must remain unresolved.",
    ))
    state.save_artifact("job_submission", "unknown_operation_job", json.dumps({
        "status": "success", "scheduler": "local", "job_id": "424242",
        "container_runtime_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "submission_nonce": "unknown-test",
        "bootstrap_log_dir": str(tmp_path),
        "execution_status_path": str(tmp_path / "missing-exit-status.json"),
    }))
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_args, **_kwargs: {
        "status": "success", "scheduler_phase": "unknown",
        "health_state": "unknown", "error_evidence": [],
    })

    completion = asyncio.run(_record_operation_completion(
        state, task_kind="external_job",
        objective="reject an unknown managed operation",
        outcome="success", job_ids=["424242"],
    ))

    assert completion["status"] == "error"
    assert completion["error_code"] == "external_jobs_not_terminal"
    assert completion["checks"][0]["passed"] is False


def test_terminal_operational_job_is_vetoed_without_a_mode_specific_order(tmp_path, monkeypatch):
    state = _State(tmp_path, [_submission()])
    from nodes.experiment.tools.run_contract import _classify_experiment_scope
    _bind_operation_inputs(state)
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="job_observation",
        reason="Observe an existing managed scheduler job without scientific analysis.",
    ))
    monkeypatch.setattr(hooks, "_unresolved_external_workflows", lambda _: [{
        "scheduler": "local", "job_id": "4242", "workflow_status": "awaiting_analysis",
        "health": {"scheduler_phase": "terminal"},
    }])

    messages = _run_finish_gate(state)

    assert messages
    text = messages[0].content
    assert "finalize_external_job" in text
    # outcome 词表归 finalize 的 schema，本闸只说按 execution_class 选，不复制那套判断
    assert "execution_class" in text
    assert "operation_receipt" not in text


def test_unresolved_local_workflow_projects_exact_handoff_identity(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    submission = _submission(
        job_id="hf-projected-ready",
        submission_nonce="nonce-projected",
        container_runtime_id="d" * 64,
    )
    state = _State(tmp_path, [submission])
    monkeypatch.setattr(manager, "probe_external_job_health", lambda *_a, **_k: {
        "status": "success",
        "scheduler_phase": "running",
        "workflow_status": "awaiting_external_job",
    })
    running = manager.unresolved_external_workflows(state)
    state.hook_state["_external_job_handoff_ready"] = {
        "scheduler": "local",
        "job_id": submission["job_id"],
        "submission_nonce": submission["submission_nonce"],
        "container_runtime_id": submission["container_runtime_id"],
    }

    assert running[0]["submission_nonce"] == submission["submission_nonce"]
    assert running[0]["container_runtime_id"] == submission["container_runtime_id"]
    assert hooks._running_external_jobs_are_handoff_ready(state, running) is True


def test_healthy_wait_permit_allows_durable_handoff(tmp_path, monkeypatch):
    runtime_id = "a" * 64
    submission = _submission(
        job_id="hf-permit-ready",
        submission_nonce="nonce-ready",
        container_runtime_id=runtime_id,
    )
    state = _State(tmp_path, [submission])
    state.hook_state["_external_job_handoff_ready"] = {
        "scheduler": "local", "job_id": submission["job_id"],
        "submission_nonce": submission["submission_nonce"],
        "container_runtime_id": runtime_id,
    }
    workflows = [{
        "scheduler": "local", "job_id": submission["job_id"],
        "submission_nonce": submission["submission_nonce"],
        "container_runtime_id": runtime_id,
        "workflow_status": "awaiting_external_job",
    }]
    monkeypatch.setattr(hooks, "_unresolved_external_workflows", lambda _: workflows)

    assert _run_finish_gate(state) == [], "已持久化交接的在跑作业不再被拦第二次"
    assert any(event.get("action") == "allow_persistent_handoff" for event in state.events)

    monkeypatch.setattr(hooks, "_classify_external_job_status", lambda _: "running")
    loop_result = SimpleNamespace(status="completed", final_text="handoff")
    hooks.external_job_handoff_on_end(_ctx(state), loop_result)

    assert loop_result.status == "blocked"
    assert state.hook_state["external_job_waiting"]["review_eligibility"] is False
    assert any(item.get("category") == "external_job"
               for item in state.hook_state["blockers"])


@pytest.mark.parametrize(
    ("permit_fields", "row_fields"),
    [
        ({}, {"submission_nonce": "new", "container_runtime_id": "a" * 64}),
        (
            {"container_runtime_id": "a" * 64},
            {"container_runtime_id": "a" * 64},
        ),
        (
            {"submission_nonce": "new", "container_runtime_id": "a" * 64},
            {"submission_nonce": "new", "container_runtime_id": "b" * 64},
        ),
        (
            {"submission_nonce": "old", "container_runtime_id": "a" * 64},
            {"submission_nonce": "new", "container_runtime_id": "a" * 64},
        ),
    ],
)
def test_local_handoff_permit_rejects_legacy_or_changed_container_identity(
    tmp_path, permit_fields, row_fields,
):
    state = _State(tmp_path, [])
    state.hook_state["_external_job_handoff_ready"] = {
        "scheduler": "local", "job_id": "hf-reused-name", **permit_fields,
    }
    running = [{
        "scheduler": "local", "job_id": "hf-reused-name",
        "workflow_status": "awaiting_external_job", **row_fields,
    }]

    assert hooks._running_external_jobs_are_handoff_ready(state, running) is False


def test_legacy_remote_handoff_permit_retains_scoped_identity_compatibility(tmp_path):
    state = _State(tmp_path, [])
    state.hook_state["_external_job_handoff_ready"] = {
        "scheduler": "slurm", "job_id": "7654", "namespace": "science",
        "launch_host": "login-a", "scheduler_cluster": "cluster-a",
        "resource_uid": "allocation-9",
    }
    running = [{
        "scheduler": "slurm", "job_id": "7654", "namespace": "science",
        "launch_host": "login-a", "scheduler_cluster": "cluster-a",
        "resource_uid": "allocation-9", "submission_nonce": "new-nonce",
        "workflow_status": "awaiting_external_job",
    }]

    assert hooks._running_external_jobs_are_handoff_ready(state, running) is True


def test_local_handoff_permit_writer_requires_immutable_identity(tmp_path):
    from nodes.experiment.tools import resource_manager as manager

    state = _State(tmp_path, [])
    state.hook_state["_external_job_handoff_ready"] = {
        "scheduler": "local", "job_id": "hf-stale",
        "container_runtime_id": "a" * 64,
    }

    assert manager._mark_external_job_handoff_ready(
        state, "local", "hf-new", submission_nonce="nonce-new",
    ) is False
    assert "_external_job_handoff_ready" not in state.hook_state

    assert manager._mark_external_job_handoff_ready(
        state, "local", "hf-new", container_runtime_id="b" * 64,
    ) is False
    assert "_external_job_handoff_ready" not in state.hook_state

    assert manager._mark_external_job_handoff_ready(
        state, "local", "hf-new", submission_nonce="nonce-new",
        container_runtime_id="b" * 64,
    ) is True
    permit = state.hook_state["_external_job_handoff_ready"]
    assert permit["submission_nonce"] == "nonce-new"
    assert permit["container_runtime_id"] == "b" * 64


def test_handoff_permit_remains_valid_when_transcript_write_fails(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    state = _State(tmp_path, [])
    monkeypatch.setattr(
        state, "append_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("transcript unavailable")
        ),
    )

    assert manager._mark_external_job_handoff_ready(
        state, "local", "hf-durable-permit",
        submission_nonce="nonce-durable", container_runtime_id="d" * 64,
    ) is True
    assert state.hook_state["_external_job_handoff_ready"] == {
        "scheduler": "local",
        "job_id": "hf-durable-permit",
        "namespace": None,
        "launch_host": None,
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": "nonce-durable",
        "container_runtime_id": "d" * 64,
        "recorded_at": state.hook_state["_external_job_handoff_ready"]["recorded_at"],
    }


def test_submission_ledger_failure_blocks_before_core_finalization(
        tmp_path, monkeypatch):
    """A failed authoritative submission read must not silently complete a run."""
    from core.agent_loop import LoopResult
    from core.executor import finalize_run
    from core.harness import NodeHarness
    from core.loop_hooks import run_on_end

    state = State.new("experiment", tmp_path)
    # 记录账本（run 本地：<run>/records.jsonl）不可读：读方必须把它当坏账。
    ledger_path = state.root / "records.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()
    ledger_path.mkdir()
    loop_result = LoopResult(final_text="model claimed completion", turns=1,
                             tool_calls=[], messages=[], status="completed")

    asyncio.run(run_on_end([hooks.external_job_handoff], _ctx(state), loop_result))
    ledger_path.rmdir()
    ledger_path.touch()

    assert any(item.get("blocker_id") == "experiment_job_submission_read_error"
               for item in state.hook_state["blockers"])
    summary = asyncio.run(finalize_run(
        state, NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result, llm=None))
    assert summary["status"] == "blocked"


def test_external_wait_cancels_while_scheduler_probe_is_still_running(monkeypatch):
    from core.cancellation import RunCancelled
    from nodes.experiment.tools import resource_manager as manager

    started = threading.Event()
    release = threading.Event()

    def blocking_probe(*_args, **_kwargs):
        started.set()
        release.wait(timeout=2)
        return {"status": "error", "error": "late scheduler response"}

    class WaitState:
        def __init__(self):
            self.hook_state = {}
            self.kill_event = asyncio.Event()

    async def scenario():
        state = WaitState()
        monkeypatch.setattr(manager, "probe_external_job_health", blocking_probe)
        task = asyncio.create_task(manager._wait_for_external_job(
            state, "slurm", "123", max_wait_s=30))
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        state.kill_event.set()
        with pytest.raises(RunCancelled):
            await asyncio.wait_for(task, timeout=1.5)
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

@pytest.mark.parametrize("stdout, expected", [
    ("NOT_RUNNING", "finished_or_unavailable"),
    ("NOT_RUNNING\n", "finished_or_unavailable"),
    ("RUNNING", "running"),
    ("RUNNING\n", "running"),
    ("", "unknown"),
])
def test_local_job_status_is_parsed_by_exact_match(monkeypatch, stdout, expected):
    """本地作业状态必须精确匹配，不能用子串。

    「RUNNING」是「NOT_RUNNING」的子串。原实现先判 `"RUNNING" in stdout`，
    于是每个已结束的本地作业都被判成 running，run 收尾时交接账本写下「作业仍在
    运行」的假事实（2026-09-10 验收 2 活体实测）。

    这条测试**不替换分类器本身**，只替换它的输入 _job_status_sync，让真实解析
    逻辑跑在真实 stdout 上。本文件里其它用例都直接把分类器换成常量，所以这个
    bug 此前从未被任何测试触及。
    """
    import sys

    def fake_status(*_a, **_k):
        return {"raw": {"ok": True, "stdout": stdout}}

    patched = 0
    for name in ("tools.resource_manager", "nodes.experiment.tools.resource_manager"):
        module = sys.modules.get(name)
        if module is None:
            try:
                module = __import__(name, fromlist=["_job_status_sync"])
            except ImportError:
                continue
        if hasattr(module, "_job_status_sync"):
            monkeypatch.setattr(module, "_job_status_sync", fake_status)
            patched += 1
    assert patched, "找不到 _job_status_sync，测试替身没挂上"

    submission = {"scheduler": "local", "job_id": "hf-job-exact-match",
                  "container_runtime_id": "a" * 64}
    assert hooks._classify_external_job_status(submission) == expected
