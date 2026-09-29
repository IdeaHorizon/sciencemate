"""计划内取消（D07，design_planned_cancel_d07_0915.md）。

任务要求作业中途停下（常驻服务用完关掉、按判据停、检查点重启测试）时，原先取消之后路线步骤
记 cancelled、整条路线 blocked，唯一的成功出口是重跑一遍；模型因此记下「不要用 cancel_job」。
现在提交时在 expected_termination 里声明 planned_stop（引文逐字出自任务原文），取消确认、
不是替换的本地作业按计划停止记账：步骤按 expected_outputs 核对，收尾可记 success 并强制附
external_job_stopped_as_planned。没声明的取消不追认。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_expected_termination import _seed
from test_external_cancel_transaction import (
    _REAL_OBSERVED_JOB_END,
    _RUNTIME_ID_A,
    requires_sandbox,
)
from test_external_route_projection import _bound_state, _route, _state, _submitted_route

from core import sandbox
from core.state import State
from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as manager
from shared.lib import process_control

_QUOTE = "服务用完就把它停掉"
_PLANNED = {"planned_stop": True, "task_quote": _QUOTE}
_STOP_CONFIRM_TIMEOUT_S = 3.0 + 5.0
_STOP_POLL_INTERVAL_S = 0.05
_LINUX_PROC_IDENTITY_AVAILABLE = Path("/proc/self/stat").is_file()

_IdentityReader = Callable[[int], dict[str, object]]
_AliveReader = Callable[[int], bool]


def _read_linux_process_identity(pid: int) -> dict[str, object]:
    """Independent test oracle for one Linux PID slot; never a product fact source."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return {"presence": "absent", "state": None, "start_ticks": None}
    except (OSError, UnicodeError) as exc:
        return {
            "presence": "unknown",
            "state": None,
            "start_ticks": None,
            "error": type(exc).__name__,
        }

    _prefix, separator, tail = raw.rpartition(")")
    fields = tail.strip().split()
    if not separator or len(fields) <= 19:
        return {
            "presence": "unknown",
            "state": None,
            "start_ticks": None,
            "error": "malformed_proc_stat",
        }
    return {
        "presence": "present",
        "state": fields[0],
        "start_ticks": fields[19],
    }


def _observe_original_process(
    pid: int,
    expected_start_ticks: str,
    *,
    identity_reader: _IdentityReader = _read_linux_process_identity,
    alive_reader: _AliveReader = process_control.alive,
) -> dict[str, object]:
    """Classify the original (pid, start_ticks) across an alive() probe.

    The two identity reads close the common stat/alive race: if the PID is reused
    between them, the old process is stopped even though the new occupant is alive.
    Unknown reads stay unknown and can never be promoted to a successful stop.
    """
    before = identity_reader(pid)
    logical_alive = bool(alive_reader(pid))
    after = identity_reader(pid)
    snapshots = (before, after)
    presences = {str(item.get("presence") or "unknown") for item in snapshots}

    if "unknown" in presences:
        identity_status = "unknown"
    elif "absent" in presences:
        identity_status = "absent"
    else:
        observed_ticks = [str(item.get("start_ticks") or "") for item in snapshots]
        if not all(observed_ticks):
            identity_status = "unknown"
        elif any(value != str(expected_start_ticks) for value in observed_ticks):
            identity_status = "reused"
        else:
            identity_status = "same"

    stopped = identity_status in {"absent", "reused"} or (
        identity_status == "same" and not logical_alive
    )
    return {
        "pid": pid,
        "expected_start_ticks": str(expected_start_ticks),
        "identity_status": identity_status,
        "logical_alive": logical_alive,
        "proc_state": after.get("state") or before.get("state"),
        "before": before,
        "after": after,
        "stopped": stopped,
    }


def _wait_for_original_process_to_stop(
    pid: int,
    expected_start_ticks: str,
    *,
    identity_reader: _IdentityReader = _read_linux_process_identity,
    alive_reader: _AliveReader = process_control.alive,
    timeout_s: float = _STOP_CONFIRM_TIMEOUT_S,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while True:
        last = _observe_original_process(
            pid,
            expected_start_ticks,
            identity_reader=identity_reader,
            alive_reader=alive_reader,
        )
        if last["stopped"] is True:
            return last
        if time.monotonic() >= deadline:
            pytest.fail(
                "original managed process did not stop within Core 3+5 second "
                f"confirmation window: {json.dumps(last, sort_keys=True)}"
            )
        time.sleep(_STOP_POLL_INTERVAL_S)


def _pid_slot_exists(pid: int) -> bool | None:
    """Diagnostic only: unlike the oracle above, this is not process identity."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True



# ── 声明与锚点 ────────────────────────────────────────────────────────────────


def test_a_planned_stop_alone_is_anchored_without_exit_codes(tmp_path):
    state = State.new("experiment", tmp_path)
    _seed(state, f"起一个推理服务做评测，{_QUOTE}。")

    normalized, refusal = manager._anchored_expected_termination(state, dict(_PLANNED))

    assert refusal is None, refusal
    assert normalized == {"planned_stop": True, "task_quote": _QUOTE,
                          "anchor": "experiment_spec"}
    # 退出码判定不受影响：只声明计划内停止的作业，自己跑完照常按退出码收尾。
    assert manager._termination_verdict({"expected_termination": normalized}, {}) == {
        "declared": False, "termination_matched": None}


@pytest.mark.parametrize("declaration", [
    {"planned_stop": True, "task_quote": "任务原文里没有这句话"},
    {"task_quote": _QUOTE},
    {"planned_stop": False, "task_quote": _QUOTE},
], ids=["quote_not_in_task", "neither_codes_nor_planned_stop", "planned_stop_false"])
def test_a_planned_stop_needs_a_verbatim_quote_and_the_flag(tmp_path, declaration):
    state = State.new("experiment", tmp_path)
    _seed(state, f"起一个推理服务做评测，{_QUOTE}。")

    normalized, refusal = manager._anchored_expected_termination(state, declaration)

    assert normalized is None
    assert refusal["error_code"] == "expected_termination_not_anchored", refusal


# ── 走真实路线的取消（作业本身是替身）─────────────────────────────────────────


def _running(*_args, **_kwargs):
    return {"ok": True, "returncode": 0, "stdout": "RUNNING", "stderr": "",
            "sandbox_state": {"exists": True, "running": True, "id": _RUNTIME_ID_A}}


def _routed_local_job(tmp_path, monkeypatch, *, planned: bool, route: dict | None = None,
                      stopped: bool = True, bound: bool = False):
    # bound：收尾要求当前 v1 意图绑定（见 test_external_route_projection._bound_state）。
    state = _bound_state(tmp_path) if bound else _state(tmp_path)
    binding, reference = _submitted_route(state, route or _route(), local=True)
    payload = {
        "status": "success", "dry_run": False,
        **{key: reference[key] for key in (
            "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
            "resource_uid", "submission_nonce", "process_group_id",
            "process_start_ticks", "container_runtime_id")},
        "route_attempt_id": reference["route_attempt_id"],
        "output_roots": [str(state.root / "outputs")],
        **({"expected_termination": dict(_PLANNED, anchor="experiment_spec")}
           if planned else {}),
    }
    state.save_artifact("job_submission", "planned_stop_submission", json.dumps(payload))

    async def unknown(*_a, **_k):
        return None

    monkeypatch.setattr(manager, "_observed_job_end", unknown)
    monkeypatch.setattr(manager, "_local_container_status", _running)
    monkeypatch.setattr("core.sandbox.stop_container", lambda *_a, **_k: stopped)
    monkeypatch.setattr(manager, "_cleanup_local_job_for_finalization",
                        lambda _record: {"status": "success"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    return state, reference, payload


def _cancel(state, reference, **kwargs):
    return asyncio.run(manager._cancel_job(
        state, "local", reference["job_id"], reason="任务要求停下", **kwargs))


def test_a_declared_stop_completes_the_step_and_verifies_the_job(tmp_path, monkeypatch):
    state, reference, payload = _routed_local_job(tmp_path, monkeypatch, planned=True)

    cancelled = _cancel(state, reference)

    assert cancelled["status"] == "success", cancelled
    assert cancelled["stopped_as_planned"] is True
    snapshot = er.build_route_snapshot(state)
    assert snapshot["route_state"] == "complete", snapshot
    assert manager.lifecycle_for_submission(state, payload)["status"] == "cancelled"
    verification = oc._managed_external_job_verification(state, None, [reference])
    assert verification["successful"] is True, verification
    evidence = verification["health"][0]["success_evidence"]
    assert evidence["source"] == "planned_stop_cancellation"
    assert evidence["succeeded"] is False and evidence["termination_matched"] is True
    assert evidence["task_quote"] == _QUOTE


def test_after_a_planned_kill_the_resume_step_is_ready(tmp_path, monkeypatch):
    route = _route()
    route["steps"].append({
        "id": "resume", "goal": "从 checkpoint 续跑", "after": ["run"],
        # 程序名与第一步不同：夹具提交时不带 route_step_id，同名会匹配成歧义。
        "action": {"tool": "submit_job", "program": "restart_solver"},
        "effects": ["external_job", "process_tree", "workspace_write"],
        "workdir_role": "run_root", "expected_outputs": [],
    })
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, route=route)

    _cancel(state, reference)

    snapshot = er.build_route_snapshot(state)
    assert snapshot["steps"]["run"]["state"] == "verified", snapshot["steps"]
    assert snapshot["ready_step_ids"] == ["resume"], snapshot


def test_a_declared_stop_still_checks_expected_outputs(tmp_path, monkeypatch):
    route = _route()
    route["steps"][0]["expected_outputs"] = ["heartbeat.log"]
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, route=route)

    _cancel(state, reference)

    step = er.build_route_snapshot(state)["steps"]["run"]
    assert step["state"] != "verified", step
    assert "expected_outputs" in str(step.get("reason")), step


def test_an_undeclared_cancel_is_not_counted_and_names_the_honest_exit(tmp_path, monkeypatch):
    state, reference, _payload = _routed_local_job(tmp_path, monkeypatch, planned=False)

    cancelled = _cancel(state, reference)

    assert cancelled["status"] == "success" and cancelled["stopped_as_planned"] is False
    assert "不追认" in cancelled["message"]
    assert er.build_route_snapshot(state)["route_state"] == "blocked"
    completion = oc._route_completion_verification(state)
    assert completion["cancelled_steps"] == ["run"], completion
    assert any("planned_stop" in step for step in completion["next_actions"]), completion
    assert oc._managed_external_job_verification(state, None, [reference])["successful"] is False


def test_a_replacement_or_an_unconfirmed_stop_is_not_a_planned_stop(tmp_path, monkeypatch):
    state, reference, payload = _routed_local_job(tmp_path, monkeypatch, planned=True)
    superseded = _cancel(state, reference, superseded_by="hf-harness-route-local-v2")
    assert superseded["stopped_as_planned"] is False, superseded
    assert manager.planned_stop_cancellation(state, payload) is None

    state2, reference2, payload2 = _routed_local_job(
        tmp_path / "unconfirmed", monkeypatch, planned=True, stopped=False)
    unconfirmed = _cancel(state2, reference2)
    assert unconfirmed["status"] == "cancellation_outcome_unknown", unconfirmed
    assert manager.planned_stop_cancellation(state2, payload2) is None


@pytest.mark.skipif(not _LINUX_PROC_IDENTITY_AVAILABLE, reason="Linux proc identity is unavailable")
def test_identity_aware_stop_oracle_treats_a_zombie_as_stopped_while_kill_zero_does_not():
    """A zombie has exited, although the old PID-only assertion still sees its slot."""
    proc = subprocess.Popen([sys.executable, "-c", "import os; os._exit(0)"])
    try:
        deadline = time.monotonic() + _STOP_CONFIRM_TIMEOUT_S
        while True:
            snapshot = _read_linux_process_identity(proc.pid)
            if snapshot.get("presence") == "present" and snapshot.get("state") == "Z":
                break
            if time.monotonic() >= deadline:
                pytest.fail(f"child never became a zombie: {snapshot}")
            time.sleep(_STOP_POLL_INTERVAL_S)

        expected_start_ticks = str(snapshot["start_ticks"])
        assert _pid_slot_exists(proc.pid) is True  # The replaced assertion would fail here.
        observed = _observe_original_process(proc.pid, expected_start_ticks)

        assert observed["identity_status"] == "same", observed
        assert observed["proc_state"] == "Z", observed
        assert observed["logical_alive"] is False, observed
        assert observed["stopped"] is True, observed
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_identity_aware_stop_oracle_does_not_treat_a_reused_pid_as_the_original_process():
    samples = iter(
        [
            {"presence": "present", "state": "S", "start_ticks": "100"},
            {"presence": "present", "state": "S", "start_ticks": "200"},
        ]
    )

    observed = _observe_original_process(
        4242, "100", identity_reader=lambda _pid: next(samples), alive_reader=lambda _pid: True
    )

    assert observed["identity_status"] == "reused", observed
    assert observed["logical_alive"] is True, observed
    assert observed["stopped"] is True, observed

    same_process = _observe_original_process(
        4242,
        "100",
        identity_reader=lambda _pid: {"presence": "present", "state": "S", "start_ticks": "100"},
        alive_reader=lambda _pid: True,
    )
    assert same_process["identity_status"] == "same", same_process
    assert same_process["stopped"] is False, same_process


def test_identity_aware_stop_oracle_never_promotes_an_unknown_read_to_stopped():
    unknown = _observe_original_process(
        4242,
        "100",
        identity_reader=lambda _pid: {"presence": "unknown", "state": None, "start_ticks": None},
        alive_reader=lambda _pid: False,
    )

    assert unknown["identity_status"] == "unknown", unknown
    assert unknown["stopped"] is False, unknown


# ── 真实本地作业 ──────────────────────────────────────────────────────────────


@requires_sandbox
def test_a_real_long_job_declared_as_planned_stop_really_stops_and_verifies(
    tmp_path, monkeypatch,
):
    """L237：真实受管进程；负向探测落在 OS 上——取消后进程确实不在了。"""
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(manager, "_observed_job_end", _REAL_OBSERVED_JOB_END)
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)
    state = State.new("experiment", tmp_path / "run")
    runtime = manager.experiment_output_dir(state, "runtime", create=True).resolve()
    submission = manager._submit_sync(
        runtime_root=runtime, scheduler="local", command="sleep 30",
        job_name="planned-stop", mpi_ranks=1, cpus_per_rank=1, gpus=0,
        memory_gb=1.0, storage_gb=1.0, walltime_minutes=1, queue=None,
        nodelist=None, image=None, workdir=str(runtime), dry_run=False,
        namespace=None, output_paths=[str(runtime)], stage_in=None, state=state,
        submission_nonce="planned-stop", route_attempt_id="planned-stop", hard_deadline_s=45,
    )
    assert submission["status"] == "success", submission
    submission["expected_termination"] = dict(_PLANNED, anchor="experiment_spec")
    state.save_artifact("job_submission", "planned_stop", json.dumps(submission))
    try:
        observed = sandbox.inspect_container(submission["container_runtime_id"])
        assert observed["running"] is True, observed
        pid = int(observed["pid"])
        expected_start_ticks: str | None = None
        if _LINUX_PROC_IDENTITY_AVAILABLE:
            initial_identity = _read_linux_process_identity(pid)
            assert initial_identity["presence"] == "present", initial_identity
            expected_start_ticks = str(initial_identity["start_ticks"])

        cancelled = asyncio.run(manager._cancel_job(
            state, "local", submission["job_id"], reason="任务要求停下"))

        assert cancelled["status"] == "success", cancelled
        assert cancelled["stopped_as_planned"] is True
        if expected_start_ticks is not None:
            process_diagnostic = _wait_for_original_process_to_stop(pid, expected_start_ticks)
            process_diagnostic["identity_oracle"] = "linux_proc_start_ticks"
        else:
            deadline = time.monotonic() + _STOP_CONFIRM_TIMEOUT_S
            while process_control.alive(pid) and time.monotonic() < deadline:
                time.sleep(_STOP_POLL_INTERVAL_S)
            assert not process_control.alive(pid), (
                f"managed process {pid} remained alive after cancellation")
            process_diagnostic = {
                "pid": pid,
                "identity_status": "unsupported",
                "logical_alive": False,
                "stopped": True,
                "identity_oracle": "logical_alive_only",
            }
        process_diagnostic["pid_slot_exists"] = _pid_slot_exists(pid)
        print(
            "planned_stop_process_diagnostic="
            + json.dumps(process_diagnostic, sort_keys=True),
            flush=True,
        )
        assert manager.planned_stop_cancellation(state, submission) is not None
        verification = oc._managed_external_job_verification(state, [submission["job_id"]], None)
        assert verification["successful"] is True, verification
    finally:
        runtime_id = submission.get("container_runtime_id")
        if runtime_id:
            sandbox.stop_container(str(submission["job_id"]), remove=True,
                                   expected_container_id=str(runtime_id))


def test_operation_success_records_that_the_job_stopped_as_planned(tmp_path, monkeypatch):
    """收尾能记 success，但必须如实附一条：这个作业是按计划停下的，不是自己跑完的。"""
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, bound=True)
    _cancel(state, reference)

    completion = asyncio.run(oc._record_operation_completion(
        state, task_kind="external_job", objective="起服务做评测，用完停掉",
        outcome="success", external_job_refs=[reference]))

    assert completion["status"] == "success", completion
    checks = {item["name"]: item for item in completion["checks"]}
    assert checks["external_job_stopped_as_planned"]["passed"] is True, checks
    job = checks["external_job_stopped_as_planned"]["evidence"]["jobs"][0]
    assert job["task_quote"] == _QUOTE and job["cancellation_outcome_artifact_id"]


# ── 第三会话复审 D07（review_d07_0915_third.md，探针 probe_d07）───────────────


@pytest.mark.parametrize("task_kind", ["external_job", "generic"])
def test_closing_without_refs_still_verifies_and_discloses_the_planned_stop(
    tmp_path, monkeypatch, task_kind,
):
    """P2：收尾不传 external_job_refs 时，refs 从本 run 提交账本推导；按计划停止的作业原先
    因 lifecycle=cancelled 被排除，external_job 收尾降为 partial，generic 收尾直接 success 且
    不带披露。"""
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, bound=True)
    _cancel(state, reference)
    evidence = tmp_path / "eval.log"
    evidence.write_text("ok\n", encoding="utf-8")

    completion = asyncio.run(oc._record_operation_completion(
        state, task_kind=task_kind, objective="起服务做评测，用完停掉", outcome="success",
        **({"artifact_paths": [str(evidence)]} if task_kind == "generic" else {})))

    assert completion["status"] == "success", completion
    assert completion.get("outcome") == "success", completion
    checks = {item["name"]: item["passed"] for item in completion["checks"]}
    assert checks.get("external_job_stopped_as_planned") is True, checks


def test_verifying_a_planned_stop_reports_success_not_a_missing_receipt(tmp_path, monkeypatch):
    """P3：取消已写下路线终态事实，verify_external_job_execution 原先报
    external_route_projection_receipt_missing，误导模型。"""
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, bound=True)
    _cancel(state, reference)

    verified = asyncio.run(oc._verify_external_job_execution(
        state, scheduler="local", job_id=reference["job_id"]))

    assert verified["status"] == "success", verified
    assert verified["already_projected"] is True


def test_a_cancelled_lifecycle_without_a_confirmed_cancel_is_not_projected_as_planned(
    tmp_path, monkeypatch,
):
    """P3：路线投影与 ROC 共用 planned_stop_cancellation。lifecycle 直接是 cancelled、没有确认过的
    取消事务时，cancel_job 重放原先仍把路线投影成按计划停止（complete），ROC 却不认。"""
    state, reference, payload = _routed_local_job(
        tmp_path, monkeypatch, planned=True, bound=True)
    manager._record_job_lifecycle(
        state, scheduler=payload["scheduler"], job_id=payload["job_id"],
        namespace=payload.get("namespace"), launch_host=payload.get("launch_host"),
        scheduler_cluster=payload.get("scheduler_cluster"),
        resource_uid=payload.get("resource_uid"),
        lifecycle_status="cancelled", reason="legacy_fixture")

    replay = _cancel(state, reference)

    assert replay.get("stopped_as_planned") is not True, replay
    assert er.build_route_snapshot(state)["route_state"] != "complete"


def test_verifying_a_job_that_finalize_already_projected_reports_success(tmp_path, monkeypatch):
    """同一处改动对普通作业也成立：先 finalize 记 operation_completed（写下路线终态事实），再调
    verify_external_job_execution——投影被跳过，原先同样报 external_route_projection_receipt_missing。"""
    from test_external_route_projection import _success_verification

    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=False, bound=True)
    finalized = er.record_external_route_finalization(
        state, **{key: reference[key] for key in (
            "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
            "resource_uid", "submission_nonce", "process_group_id",
            "process_start_ticks", "container_runtime_id")},
        domain_outcome="operation_completed",
        evidence_artifact_id="external_job_operation_closure__receipt")
    assert finalized["status"] == "success", finalized
    monkeypatch.setattr(oc, "_managed_external_job_verification",
                        lambda _state, _ids, refs: _success_verification(refs[0]))

    verified = asyncio.run(oc._verify_external_job_execution(
        state, scheduler="local", job_id=reference["job_id"]))

    assert verified["status"] == "success", verified
    assert verified["already_projected"] is True


def test_verifying_a_job_whose_finalize_recorded_missing_outputs_reports_the_failure(
    tmp_path, monkeypatch,
):
    """第三会话复审 307e4079 P2（探针 probe_d07 test_zz_probe_d07_verify_failed 的 outputs_missing）：
    finalize 缺预期产物时也写收尾事件（failure_class=expected_outputs_missing）再返回 error，
    verify_external_job_execution 却按「已投影」报 success，而步骤实际 failed、路线 blocked。"""
    from test_external_route_projection import _success_verification

    route = _route()
    route["steps"][0]["expected_outputs"] = ["heartbeat.log"]
    state, reference, _payload = _routed_local_job(
        tmp_path, monkeypatch, planned=False, bound=True, route=route)
    finalized = er.record_external_route_finalization(
        state, **{key: reference[key] for key in (
            "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
            "resource_uid", "submission_nonce", "process_group_id",
            "process_start_ticks", "container_runtime_id")},
        domain_outcome="operation_completed",
        evidence_artifact_id="external_job_operation_closure__receipt")
    assert finalized["reason"] == "route_expected_outputs_missing", finalized
    monkeypatch.setattr(oc, "_managed_external_job_verification",
                        lambda _state, _ids, refs: _success_verification(refs[0]))

    verified = asyncio.run(oc._verify_external_job_execution(
        state, scheduler="local", job_id=reference["job_id"]))

    assert verified["status"] == "error", verified
    assert verified["error_code"] == "route_expected_outputs_missing"
    assert verified["missing_expected_outputs"] == ["heartbeat.log"]
    assert any("recovery_basis" in step for step in verified["next_actions"])
    assert er.build_route_snapshot(state)["route_state"] == "blocked"
