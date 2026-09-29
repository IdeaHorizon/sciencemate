"""作业是否已结束：读不出时不装作读得出（第 3 步，用户 2026-09-13 定）。

- 缺陷 1：终态收据枚举失败 ≠ 没有收据（C1 后回退扫 artifacts_dir 恒为空，会重铸收据）。
- 缺陷 2：本地作业账本里没有记录 ≠ 取消成功（原先没发信号就记 cancelled）。
- 缺陷 3：发信号前最后一读看到作业已结束，如实记 not_sent（原先记 rejected，再调取消停在
  一句不实的话上）；被拒之后作业已结束，也要指回收尾。
- 收据复用扩到全部调度器；作业状态读不出（本地记录缺失 / 远端调度器已遗忘）统一给诚实
  出口：不宣称结束、不宣称取消，report_blocker 后 record_operation_completion(blocked)。
- 调度器记账查询不卡事件循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from core import sandbox
from core.state import State
from nodes.experiment.tools import resource_manager as manager
from test_external_cancel_transaction import (
    _REAL_OBSERVED_JOB_END, _RUNTIME_ID_A, _cleanup, _save_submission, _submit_local,
    requires_sandbox,
)
from test_operation_job_finalization import (
    _finalize, _health, _local_submission, _operation_receipt_payload, _patch_pipeline,
    _prepared_state, _save_operation_receipt, _submission,
)


async def _guard_reads_nothing(*_a, **_k):
    return None


def _honest_exit_offered(result: dict) -> bool:
    return any('outcome="blocked"' in step for step in (result.get("next_actions") or []))


def test_a_receipt_ledger_that_cannot_be_enumerated_is_not_an_absent_receipt(tmp_path, monkeypatch):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    real_list = state.list_artifacts

    def unreadable(artifact_type=None, own_only=False):
        if artifact_type == manager._EXTERNAL_JOB_OPERATION_CLOSURE_TYPE:
            raise OSError("records ledger unreadable")
        return real_list(artifact_type, own_only=own_only)

    monkeypatch.setattr(state, "list_artifacts", unreadable)

    resolved = manager._operation_closure_receipt(state, submission, "operation_completed")

    assert resolved["status"] == "error", resolved
    assert resolved["error_code"] == "operation_closure_receipt_audit_failed"
    assert resolved["unreadable_stage"] == "enumeration"


def _local_payload(state: State) -> dict:
    payload = {"status": "success", "dry_run": False, "scheduler": "local",
               "job_id": "hf-job-0123456789abcdef", "submission_nonce": "n",
               "container_runtime_id": _RUNTIME_ID_A,
               "output_roots": [str(state.root / "outputs")]}
    state.save_artifact("job_submission", "managed_submission", json.dumps(payload))
    return payload


def test_cancelling_a_job_whose_local_record_is_gone_records_no_cancellation(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    payload = _local_payload(state)
    monkeypatch.setattr(manager, "_observed_job_end", _guard_reads_nothing)
    monkeypatch.setattr(manager, "_local_container_status", lambda *_a, **_k: {
        "ok": True, "returncode": 0, "stdout": "NOT_RUNNING", "stderr": "",
        "sandbox_state": {"exists": False}})
    stops: list = []
    monkeypatch.setattr("core.sandbox.stop_container", lambda *a, **k: stops.append(a) or True)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(state, "local", payload["job_id"], reason="stop"))

    assert result["status"] == "cancellation_outcome_unknown", result
    assert stops == []
    assert manager.lifecycle_for_submission(state, payload)["status"] != "cancelled"
    assert _honest_exit_offered(result), result
    # 物理进程事实归 Core，与收尾出口的 owner 一致（第三会话复审 P3）。
    assert result["blocker"]["suggested_owner"] == "core", result


@requires_sandbox
def test_a_running_job_whose_record_vanished_is_neither_signalled_nor_recorded_cancelled(
    tmp_path, monkeypatch,
):
    """L237：真实受管进程；负向探测落在 OS 上——进程仍然活着，没有被信号杀掉。"""
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    state = State.new("experiment", tmp_path / "run")
    submission: dict = {}
    control: Path | None = None
    hidden = tmp_path / "hidden-record"
    try:
        submission = _submit_local(state, tmp_path, "sleep 30", "vanished-record")
        observed = sandbox.inspect_container(submission["container_runtime_id"])
        assert observed["running"] is True, observed
        pid = int(observed["pid"])
        control = Path(submission["sandbox_control_dir"])
        shutil.move(str(control), str(hidden))
        assert sandbox.inspect_container(submission["container_runtime_id"])["exists"] is False

        result = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="planned stop"))

        assert result["status"] == "cancellation_outcome_unknown", result
        os.kill(pid, 0)  # 进程仍在：没有发出信号
        assert manager.lifecycle_for_submission(state, submission)["status"] != "cancelled"
    finally:
        if control is not None and hidden.exists() and not control.exists():
            shutil.move(str(hidden), str(control))
        _cleanup(submission)


def test_a_rejected_cancellation_points_to_finalize_once_the_job_is_seen_to_have_ended(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _save_submission(state)  # slurm 4242
    monkeypatch.setattr(manager, "_observed_job_end", _guard_reads_nothing)
    monkeypatch.setattr(manager, "_cancel_sync", lambda *_a, **_k: {
        "ok": False, "action": "scheduler_cancel",
        "result": {"ok": False, "returncode": 1, "stderr": "scancel: refused"}})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    first = asyncio.run(manager._cancel_job(state, "slurm", "4242", reason="stop"))
    assert first["status"] == "error", first

    async def ended(*_a, **_k):
        return {"source": "slurm_accounting", "states": ["COMPLETED"]}

    monkeypatch.setattr(manager, "_observed_job_end", ended)
    second = asyncio.run(manager._cancel_job(state, "slurm", "4242", reason="stop"))

    assert second["status"] == "already_ended", second
    assert second["next_tool"]["name"] == "finalize_external_job"
    assert second["previous_cancel_result"]["action"] == "scheduler_cancel"


def test_a_remote_job_forgotten_by_its_scheduler_is_closed_from_its_receipt(tmp_path, monkeypatch):
    submission = _submission()  # slurm
    state = _prepared_state(tmp_path, submission)
    _save_operation_receipt(state, submission, "prior_receipt",
                            _operation_receipt_payload(state, submission))
    _patch_pipeline(monkeypatch, _health(scheduler_phase="unknown"))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "success", result
    assert len(state.list_artifacts("external_job_operation_closure")) == 1


def test_finalizing_a_remote_job_whose_state_cannot_be_read_offers_the_honest_exit(
    tmp_path, monkeypatch,
):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(scheduler_phase="unknown"))

    result = _finalize(state, submission, "operation_completed")

    assert result.get("error_code") == "external_job_state_unknown", result
    assert result["lifecycle_written"] is False
    assert result["blocker"]["suggested_owner"] == "run_owner"
    assert result["observation"] == "state_unreadable", result
    assert result["retry_first"] is True
    assert "永久封口" in result["error"]
    assert _honest_exit_offered(result), result
    assert state.list_artifacts("external_job_operation_closure") == []


def test_finalizing_a_local_job_whose_record_is_gone_is_not_treated_as_ended(tmp_path, monkeypatch):
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(
        scheduler_result={"raw": {"ok": True, "stdout": "NOT_RUNNING",
                                  "sandbox_state": {"exists": False}}}))
    monkeypatch.setattr(manager, "_cleanup_local_job_for_finalization",
                        lambda _r: {"status": "success"})

    result = _finalize(state, submission, "operation_failed")

    assert result.get("error_code") == "external_job_state_unknown", result
    assert result["observation"] == "local_record_missing", result
    assert "没有这个作业的记录" in result["error"]
    assert result["retry_first"] is False
    assert result["blocker"]["orphan_risk"] is True
    assert result["blocker"]["suggested_owner"] == "core"
    assert state.list_artifacts("external_job_operation_closure") == []


#: _local_container_status 自己会返回的形状——记录其实都在，只是身份核对不过或读取出错。
_LOCAL_STATUS_WHEN_THE_RECORD_IS_STILL_THERE = {
    "container_name_reused": {
        "ok": False, "returncode": None, "stdout": "",
        "stderr": "容器名已被其他实例复用；不可变 container ID 不匹配",
        "sandbox_state": {"exists": True, "managed": True, "id": "b" * 64}},
    "other_namespace": {
        "ok": False, "returncode": None, "stdout": "",
        "stderr": "受管作业不属于当前 sandbox namespace；拒绝读取或操作",
        "sandbox_state": {"exists": True, "managed": True}},
    "inspect_error": {
        "ok": False, "returncode": None, "stdout": "",
        "stderr": "backend unavailable", "sandbox_state": {"error": "backend unavailable"}},
}


@pytest.mark.parametrize("shape", sorted(_LOCAL_STATUS_WHEN_THE_RECORD_IS_STILL_THERE))
def test_a_local_job_that_cannot_be_read_is_not_said_to_have_lost_its_record(
    tmp_path, monkeypatch, shape,
):
    """记录其实都在时只能说「这次读不出」、先重读；不能说「账本里没有记录」，再把模型
    引向会永久封口 run 的收尾（2026-09-14 第三会话复审 P2）。走真实健康探针。"""
    status = _LOCAL_STATUS_WHEN_THE_RECORD_IS_STILL_THERE[shape]
    submission = _local_submission()
    state = _prepared_state(tmp_path, submission)
    monkeypatch.setattr(manager, "_local_container_status", lambda *_a, **_k: dict(status))
    monkeypatch.setattr(manager, "_persist_execution_environment_evidence",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success"})
    monkeypatch.setattr(manager, "_cleanup_local_job_for_finalization",
                        lambda _r: {"status": "success"})

    result = _finalize(state, submission, "operation_failed")

    assert result.get("error_code") == "external_job_state_unknown", result
    assert result["observation"] == "state_unreadable", result
    assert "没有这个作业的记录" not in result["error"]
    assert status["stderr"] in result["error"]
    assert result["retry_first"] is True
    assert "重读" in result["next_actions"][0]
    assert result["blocker"]["orphan_risk"] is None
    assert state.list_artifacts("external_job_operation_closure") == []


@pytest.mark.parametrize("stderr, returncode, observation", [
    ("slurm_load_jobs error: Unable to contact slurm controller (connect failure)", 1,
     "state_unreadable"),
    ("not found", None, "state_unreadable"),  # squeue 没装：_run 的 FileNotFoundError 形状
    ("slurm_load_jobs error: Invalid job id specified", 1, "scheduler_reports_job_unknown"),
], ids=["controller_unreachable", "squeue_missing", "scheduler_says_unknown"])
def test_a_remote_job_is_said_to_be_unknown_only_when_the_scheduler_says_so(
    tmp_path, monkeypatch, stderr, returncode, observation,
):
    submission = _submission()  # slurm
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(scheduler_phase="unknown", scheduler_result={
        "raw": {"ok": False, "returncode": returncode, "stdout": "", "stderr": stderr}}))

    result = _finalize(state, submission, "operation_completed")

    assert result.get("error_code") == "external_job_state_unknown", result
    assert result["observation"] == observation, result
    assert ("查不到" in result["error"]) is (observation == "scheduler_reports_job_unknown")
    if observation == "scheduler_reports_job_unknown":
        # squeue 的 Invalid job id 在 PrivateData 隐藏或查错集群时也会出现（第三会话复审 P3）。
        assert "不可见" in result["error"]
    assert result["retry_first"] is (observation == "state_unreadable")
    assert "永久封口" in result["error"]


def test_finalizing_a_running_job_says_it_is_still_running(tmp_path, monkeypatch):
    submission = _submission()
    state = _prepared_state(tmp_path, submission)
    _patch_pipeline(monkeypatch, _health(scheduler_phase="running"))

    result = _finalize(state, submission, "operation_completed")

    assert result["status"] == "error", result
    assert "仍在运行" in result["error"]
    assert result.get("error_code") != "external_job_state_unknown"


def test_the_accounting_lookup_does_not_block_the_event_loop(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path)
    record = _save_submission(state)  # slurm 4242
    monkeypatch.setattr(manager, "_job_status_sync", lambda *_a, **_k: {
        "status": "success", "raw": {"ok": True, "stdout": "", "stderr": ""}})

    def slow_accounting(argv, timeout=None, **_k):
        time.sleep(1.0)
        return {"ok": True, "stdout": "4242|COMPLETED|0:0||||||\n", "stderr": ""}

    monkeypatch.setattr(manager, "_run", slow_accounting)

    async def main():
        gaps: list[float] = []

        async def ticker():
            last = time.monotonic()
            for _ in range(24):
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        # 先让计时协程真的跑起来、挂在两次 tick 之间，阻塞才会压在它的计时器上被量到；
        # 否则同步阻塞可能整段发生在它第一次运行之前，什么也量不出来（实测如此）。
        await asyncio.sleep(0.12)
        ended = await manager._observed_job_end(state, record)
        await tick
        return ended, max(gaps)

    ended, worst_gap = asyncio.run(main())

    assert ended and ended["source"] == "slurm_accounting", ended
    assert worst_gap < 0.5, worst_gap
