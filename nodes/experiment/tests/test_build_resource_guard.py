"""构建资源守卫：推导、路由、运行时健康和 Python 绕过回归。"""
from __future__ import annotations

import asyncio
import os
import signal
import threading
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.state import State
from nodes.experiment.tools import build_resource_guard as guard
from nodes.experiment.tools import execution_route, resource_manager, safe_bash
from nodes.experiment.tools.path_roles import (
    default_stage_workdir,
    experiment_output_dir,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope
from shared.lib import dangerous_commands


# ── 环境能力探针 ────────────────────────────────────────────────────────────
#
# 下面四条测试练的是**宿主监督机械**，它们各依赖一种容器里常缺的 OS 能力。
# 探测的是能力本身，不是「是不是 CI」—— 在有能力的容器里照样跑，
# 在没能力的宿主上照样跳。2026-09-04 用 python:3.13-slim 容器逐条复现定位：
#
# · strong_guard 的收尾要向 systemd 证明 cgroup 已清空
#   （wait_for_cgroup_quiescence → cgroup_unit_snapshot → systemctl show）。
#   没有 systemd 时 snapshot 恒 None → 判 unknown → fail-closed 报错。
#   **那是 A 类熔断在正确工作**（unknown 不得当成功证据），只是测试断言的
#   是有 systemd 的幸福路径。
# · 取消/收尾断言「进程组消亡」，前提是孤儿进程会被 init 收割。CI 容器里
#   PID 1 是 pytest 自己，不收割 → 僵尸把 pgid 撑活 → killpg(0) 永远成功。
#   实证：docker run 加 --init（tini 收割）后同样的两条测试全绿。

import functools
import subprocess
import time as _time


@functools.lru_cache(maxsize=1)
def _systemd_answers_unit_queries() -> bool:
    """systemd 能回答 unit 状态吗（哪怕答案是 not-found）？"""
    try:
        return guard.cgroup_unit_snapshot("hf-capability-probe.scope") is not None
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _orphans_get_reaped() -> bool:
    """孤儿化的后台进程退出后，会有 init 把僵尸收走吗？"""
    try:
        probe = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 0.05 </dev/null >/dev/null 2>&1 &"],
            start_new_session=True)
        pgid = probe.pid
        probe.wait()          # 收割 sh 本身；后台 sleep 孤儿化给 PID 1
    except Exception:
        return False
    deadline = _time.monotonic() + 2.0
    while _time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True       # sleep 已退出且被收割 → 进程组真的消亡
        _time.sleep(0.05)
    return False              # 僵尸把进程组撑着 —— 没有收割者


needs_systemd = pytest.mark.skipif(
    not _systemd_answers_unit_queries(),
    reason="strong_guard 的 quiescence 证明需要 systemd 回答 unit 状态；"
           "此环境没有 systemd，熔断会如设计地 fail-closed，幸福路径无法练到",
)
needs_reaping_init = pytest.mark.skipif(
    not _orphans_get_reaped(),
    reason="断言「进程组消亡」需要 init 收割孤儿僵尸；此环境 PID 1 不收割"
           "（容器内给 docker 加 --init 即可跑）",
)


class _State:
    def __init__(self, root: Path):
        self.root = root
        self.hook_state = {}
        self.events = []
        self.kill_event = asyncio.Event()

    def append_transcript(self, event: str, **payload):
        self.events.append({"event": event, **payload})


def _fixed_resource_plan(
    *,
    cpus: int = 2,
    memory_gb: float = 2,
    walltime_minutes: int = 5,
) -> dict:
    return {
        "status": "success",
        "decision": "build_mode_feasible",
        "runtime_resource_policy": "fixed",
        "artifact_id": "build_resource_plan__test",
        "requested_resources": {
            "total_cpus": cpus,
            "memory_gb": memory_gb,
            "walltime_minutes": walltime_minutes,
        },
    }


def _classify_bound_operation(
    state: State,
    *,
    operation_category: str,
    reason: str,
) -> dict:
    state.hook_state.setdefault("node_inputs", {
        "fixture": "build_resource_guard",
        "requested_work": "验证受管构建、Python 与未知入口的资源守卫。",
    })
    return asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category=operation_category,
        reason=reason,
    ))


def test_wrf_official_compile_and_parallelism_are_recognized() -> None:
    assert safe_bash._is_major_build("./compile em_real -j 16") is True
    assert safe_bash._is_major_build("./compile --help") is False
    assert guard.command_parallelism("./compile em_real -j 16") == 16
    assert guard.command_parallelism("make -j") == 256
    assert guard.command_parallelism("MAKEFLAGS='-j 16' make") == 16
    assert guard.command_parallelism("env MFLAGS=-j64 make") == 64


def test_exec_preflight_blocks_missing_script_interpreter(tmp_path) -> None:
    script = tmp_path / "compile"
    script.write_text("#!/definitely/missing/csh -f\necho unreachable\n",
                      encoding="utf-8")
    script.chmod(0o755)

    result = safe_bash._exec_preflight_bash(str(script))

    assert result is not None
    assert result["blocker"]["kind"] == "script_interpreter_unavailable"
    assert result["blocker"]["script"] == str(script)
    assert result["blocker"]["interpreter"] == "/definitely/missing/csh"
    assert "不得把官方脚本静默回退为裸 `make`" in result["error"]


def test_exec_preflight_accepts_script_with_available_interpreter(tmp_path) -> None:
    script = tmp_path / "compile"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    assert safe_bash._exec_preflight_bash(str(script)) is None


def test_limits_are_adaptive_but_bounded(monkeypatch) -> None:
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 64 * 1024**3)
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * 1024**3))
    state = SimpleNamespace(hook_state={"build_resource_preflight": {
        "requested_resources": {"memory_gb": 8},
    }})

    limits = guard.derive_build_limits(
        state, "./compile em_real -j 16", timeout_s=900)

    assert limits.parallelism == 16
    assert limits.tasks_max == 160
    assert limits.memory_max_bytes == 10 * 1024**3
    assert limits.memory_reservation_bytes == 8 * 1024**3
    assert limits.memory_request_source == "build_resource_plan"
    assert limits.memory_high_bytes == 10 * 1024**3
    assert limits.timeout_s == 900
    assert limits.warning_ratio < limits.stop_ratio < 1
    assert limits.disk_warning_free_bytes == 1024**4 // 20
    assert limits.disk_stop_free_bytes == 1024**4 // 100


def test_fixed_plan_tasks_envelope_covers_declared_cpu_parallelism(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 128 * 1024**3)
    state = SimpleNamespace(hook_state={
        "build_resource_preflight": _fixed_resource_plan(cpus=64),
    })

    implicit_parallel = guard.derive_build_limits(
        state, "cmake --build .", timeout_s=600
    )

    assert implicit_parallel.parallelism == 1
    assert implicit_parallel.cpu_quota_percent == 6400
    assert implicit_parallel.tasks_max == 256
    assert implicit_parallel.tasks_max > 64

    state.hook_state["build_resource_preflight"] = _fixed_resource_plan(cpus=1)
    serial = guard.derive_build_limits(
        state, "cmake --build .", timeout_s=600
    )

    assert serial.cpu_quota_percent == 100
    assert serial.tasks_max == 64


def test_fixed_plan_maps_exact_cpu_memory_and_walltime_to_cgroup(
    monkeypatch,
    tmp_path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "systemd").mkdir(parents=True)
    (runtime / "systemd" / "private").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        guard.shutil, "which", lambda name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        guard, "_host_available_memory", lambda: 64 * 1024**3
    )
    state = SimpleNamespace(hook_state={
        "build_resource_preflight": _fixed_resource_plan(),
    })

    limits = guard.derive_build_limits(
        state, "cmake --build . --parallel 2", timeout_s=600
    )

    assert limits.cpu_quota_percent == 200
    assert limits.memory_max_bytes == 2 * 1024**3
    assert limits.timeout_s == 300
    assert limits.resource_policy == "fixed"
    assert limits.resource_plan_artifact_id == "build_resource_plan__test"
    argv, reason, _unit = guard.wrap_with_cgroup(["cmake", "--build", "."], limits)
    assert reason is None
    rendered = " ".join(argv or [])
    assert "CPUQuota=200%" in rendered
    assert f"MemoryMax={2 * 1024**3}" in rendered
    assert "RuntimeMaxSec=300" in rendered


def test_managed_local_guard_omits_runtime_max_without_hard_deadline(
    monkeypatch,
    tmp_path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "systemd").mkdir(parents=True)
    (runtime / "systemd" / "private").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        guard.shutil, "which", lambda name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        guard, "_host_available_memory", lambda: 64 * 1024**3
    )
    state = SimpleNamespace(hook_state={
        "build_resource_preflight": _fixed_resource_plan(),
    })

    limits = guard.derive_build_limits(
        state, "cmake --build . --parallel 2", timeout_s=None,
    )
    argv, reason, _unit = guard.wrap_with_cgroup(
        ["cmake", "--build", "."], limits,
    )

    assert reason is None
    assert limits.timeout_s is None
    assert "RuntimeMaxSec" not in " ".join(argv or [])


def test_resource_plan_gate_rejects_missing_unapproved_and_overparallel() -> None:
    state = SimpleNamespace(hook_state={})
    missing = guard.build_resource_plan_block(state, "make -j2")
    assert missing and missing["error_code"] == "build_resource_preflight_required"

    state.hook_state["build_resource_preflight"] = {
        **_fixed_resource_plan(),
        "status": "pause",
    }
    paused = guard.build_resource_plan_block(state, "make -j2")
    assert paused and paused["error_code"] == "build_resource_preflight_not_approved"

    state.hook_state["build_resource_preflight"] = _fixed_resource_plan(cpus=2)
    over = guard.build_resource_plan_block(state, "make -j16")
    assert over and over["error_code"] == "build_parallelism_exceeds_plan"
    assert over["declared_total_cpus"] == 2
    assert guard.build_resource_plan_block(state, "make -j2") is None


def test_disk_reserves_adapt_to_filesystem_without_per_build_log_cap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 64 * 1024**3)
    state = SimpleNamespace(hook_state={})

    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (20 * 1024**3, 10 * 1024**3))
    small = guard.derive_build_limits(state, "make -j 4", timeout_s=600)
    assert small.disk_warning_free_bytes == 2 * 1024**3
    assert small.disk_stop_free_bytes == 512 * 1024**2

    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (15 * 1024**4, 10 * 1024**4))
    large = guard.derive_build_limits(state, "make -j 4", timeout_s=3600)
    assert large.disk_warning_free_bytes == 100 * 1024**3
    assert large.disk_stop_free_bytes == 20 * 1024**3
    assert not hasattr(large, "log_max_bytes")


def test_host_memory_reserves_are_adaptive_and_ordered() -> None:
    gib = 1024**3

    laptop_warning, laptop_stop = guard._host_memory_reserve_limits(8 * gib)
    small_warning, small_stop = guard._host_memory_reserve_limits(16 * gib)
    large_warning, large_stop = guard._host_memory_reserve_limits(2 * 1024 * gib)

    assert 0 < laptop_stop < laptop_warning < 8 * gib
    assert laptop_warning == 8 * gib // 10
    assert laptop_stop == 8 * gib // 20
    assert small_warning == 16 * gib // 10
    assert small_stop == 16 * gib // 20
    assert large_warning == 64 * gib
    assert large_stop == 32 * gib


def test_host_memory_admission_uses_live_available_memory(monkeypatch) -> None:
    limits = guard.BuildLimits(
        parallelism=1,
        tasks_max=64,
        memory_high_bytes=400,
        memory_max_bytes=400,
        memory_swap_max_bytes=100,
        timeout_s=30,
        disk_warning_free_bytes=1_000,
        disk_stop_free_bytes=100,
        memory_reservation_bytes=400,
        startup_headroom_bytes=40,
        host_memory_warning_free_bytes=100,
        host_memory_stop_free_bytes=50,
    )

    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 1_000,
        "available_bytes": 89,
    })
    blocked = guard.host_memory_admission_block(limits)
    assert blocked is not None
    assert blocked["reason"] == "build_host_memory_admission_denied"
    assert blocked["failure_class"] == "transient_host_pressure"
    assert blocked["current"] == 89
    assert blocked["required"] == 90
    assert blocked["deficit_bytes"] == 1
    assert blocked["startup_headroom_bytes"] == 40
    assert blocked["max_admissible_commitment_bytes"] == 950
    assert blocked["memory_hard_limit_bytes"] == 400
    assert blocked["retryable"] is True
    assert blocked["suggested_actions"] == [
        "wait_for_host_memory",
        "submit_to_scheduler",
    ]

    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 1_000,
        "available_bytes": 90,
    })
    assert guard.host_memory_admission_block(limits) is None

    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: None)
    unavailable = guard.host_memory_admission_block(limits)
    assert unavailable is not None
    assert unavailable["reason"] == "build_host_memory_probe_unavailable"


def test_fixed_limit_is_exact_and_independent_of_transient_available(monkeypatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 8 * gib)
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 64 * gib,
        "available_bytes": 8 * gib,
    })
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * gib))
    state = SimpleNamespace(hook_state={
        "build_resource_preflight": _fixed_resource_plan(memory_gb=16),
    })

    limits = guard.derive_build_limits(state, "make -j2", timeout_s=300)

    assert limits.memory_max_bytes == 16 * gib
    assert limits.memory_reservation_bytes == 16 * gib
    assert limits.startup_headroom_bytes == 64 * gib // 100
    assert guard.host_memory_admission_block(limits) is None


def test_flexible_plan_separates_admission_commitment_from_burst_limit(
    monkeypatch,
) -> None:
    gib = 1024**3
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 64 * gib)
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 128 * gib,
        "available_bytes": 64 * gib,
    })
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * gib))
    plan = _fixed_resource_plan(memory_gb=8)
    plan["runtime_resource_policy"] = "flexible"
    state = SimpleNamespace(hook_state={"build_resource_preflight": plan})

    limits = guard.derive_build_limits(state, "make -j2", timeout_s=300)

    assert limits.memory_reservation_bytes == 8 * gib
    assert limits.memory_max_bytes == 10 * gib
    assert limits.memory_request_source == "build_resource_plan"


def test_low_memory_host_admits_bounded_automatic_local_guard(monkeypatch) -> None:
    gib = 1024**3
    available = int(7.6 * gib)
    monkeypatch.setattr(guard, "_host_available_memory", lambda: available)
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": int(15.1 * gib),
        "available_bytes": available,
    })
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * gib))
    state = SimpleNamespace(hook_state={})

    limits = guard.derive_build_limits(state, "printf ok", timeout_s=30)

    assert limits.memory_reservation_bytes == 2 * gib
    assert limits.memory_max_bytes == 2 * gib
    assert limits.memory_request_source == "automatic_local_guard"
    assert guard.host_memory_admission_block(limits) is None


def test_eight_gib_host_admits_flexible_python_with_guarded_cap(
    monkeypatch,
) -> None:
    gib = 1024**3
    mib = 1024**2
    available = 700 * mib
    monkeypatch.setattr(guard, "_host_available_memory", lambda: available)
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 8 * gib,
        "available_bytes": available,
    })
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * gib))

    limits = guard.derive_build_limits(
        SimpleNamespace(hook_state={}),
        "python -c pass",
        timeout_s=30,
        requested_memory_gb=4,
        memory_request_source="runtime_request",
    )

    assert limits.memory_reservation_bytes == 4 * gib
    assert limits.memory_max_bytes == 5 * gib
    assert limits.startup_headroom_bytes == 256 * mib
    assert available < limits.host_memory_warning_free_bytes
    assert available > (
        limits.host_memory_stop_free_bytes + limits.startup_headroom_bytes
    )
    assert guard.host_memory_admission_block(limits) is None


def test_cgroup_wrapper_contains_all_hard_limits(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "systemd").mkdir(parents=True)
    (runtime / "systemd" / "private").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(guard.shutil, "which", lambda name: "/usr/bin/systemd-run")
    limits = guard.BuildLimits(
        16, 160, 8_000, 10_000, 2_000, 900, 20_000, 2_000)

    argv, reason, unit = guard.wrap_with_cgroup(["make", "-j16"], limits)

    assert reason is None and unit and unit.endswith(".scope")
    rendered = " ".join(argv or [])
    for value in (
        "OOMPolicy=continue",
        "TasksMax=160", "MemoryHigh=8000", "MemoryMax=10000",
        "MemorySwapMax=2000", "RuntimeMaxSec=900",
    ):
        assert value in rendered
    assert "payload-with-cgroup-events" in (argv or [])


def test_cgroup_files_snapshot_reads_swap_psi_and_live_events(
    tmp_path,
) -> None:
    scope = tmp_path / "user.slice" / "build.scope"
    scope.mkdir(parents=True)
    (scope / "memory.swap.current").write_text("123\n", encoding="utf-8")
    (scope / "pids.events").write_text("max 2\n", encoding="utf-8")
    (scope / "memory.events").write_text(
        "oom 1\noom_kill 1\n", encoding="utf-8")
    (scope / "memory.swap.events").write_text(
        "max 3\nfail 0\n", encoding="utf-8")
    (scope / "memory.pressure").write_text(
        "some avg10=12.50 avg60=1.00 avg300=0.10 total=1\n"
        "full avg10=2.00 avg60=0.20 avg300=0.02 total=1\n",
        encoding="utf-8",
    )

    snapshot = guard._cgroup_files_snapshot(
        "/user.slice/build.scope", tmp_path,
    )

    assert snapshot["MemorySwapCurrent"] == 123
    assert snapshot["CgroupEvents"] == {
        "pids.max": 2,
        "memory.oom": 1,
        "memory.oom_kill": 1,
        "memory.swap.max": 3,
        "memory.swap.fail": 0,
    }
    assert snapshot["MemoryPSI"] == {
        "some_avg10": 12.5,
        "full_avg10": 2.0,
    }


def test_cgroup_unit_snapshot_preserves_lifecycle_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        guard, "_cgroup_files_snapshot",
        lambda path: {
            "MemorySwapCurrent": 7,
            "CgroupEvents": {"pids.max": 0},
        } if path == "/user.slice/build.scope" else {},
    )
    monkeypatch.setattr(guard.subprocess, "run", lambda *_args, **_kwargs: (
        SimpleNamespace(
            returncode=0,
            stdout=(
                "MemoryCurrent=123\n"
                "TasksCurrent=4\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "ControlGroup=/user.slice/build.scope\n"
            ),
            stderr="",
        )
    ))

    snapshot = guard.cgroup_unit_snapshot("build.scope")

    assert snapshot == {
        "MemoryCurrent": 123,
        "TasksCurrent": 4,
        "LoadState": "loaded",
        "ActiveState": "active",
        "ControlGroup": "/user.slice/build.scope",
        "MemorySwapCurrent": 7,
        "CgroupEvents": {"pids.max": 0},
    }


def test_cgroup_quiescence_accepts_collected_or_drained_unit(monkeypatch) -> None:
    snapshots = iter([
        {
            "MemoryCurrent": 0,
            "TasksCurrent": 2,
            "LoadState": "loaded",
            "ActiveState": "active",
        },
        {
            "MemoryCurrent": None,
            "TasksCurrent": None,
            "LoadState": "not-found",
            "ActiveState": "inactive",
        },
    ])
    monkeypatch.setattr(
        guard, "cgroup_unit_snapshot", lambda _unit: next(snapshots))
    monkeypatch.setattr(guard.time, "sleep", lambda _seconds: None)

    result = guard.wait_for_cgroup_quiescence(
        "build.scope", grace_s=0.5, poll_interval_s=0.01)

    assert result["status"] == "quiet"
    assert result["snapshot"]["LoadState"] == "not-found"


def test_cgroup_quiescence_fails_closed_for_busy_or_unknown_unit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guard, "cgroup_unit_snapshot", lambda _unit: {
        "MemoryCurrent": 10,
        "TasksCurrent": 3,
        "LoadState": "loaded",
        "ActiveState": "active",
    })
    busy = guard.wait_for_cgroup_quiescence("build.scope", grace_s=0)
    assert busy["status"] == "busy"
    assert busy["tasks_current"] == 3

    monkeypatch.setattr(guard, "cgroup_unit_snapshot", lambda _unit: None)
    unknown = guard.wait_for_cgroup_quiescence("build.scope", grace_s=0)
    assert unknown["status"] == "unknown"


def test_only_contract_invalidating_cgroup_events_are_terminal() -> None:
    assert guard._resource_event_delta(
        {"pids.max": 0, "memory.oom": 0},
        {"pids.max": 1, "memory.oom": 0},
    ) == {
        "reason": "build_pids_limit_exhausted",
        "resource": "pids",
        "counter": 1,
        "failure_class": "resource_exhaustion",
    }
    assert guard._resource_event_delta(
        {"pids.max": 0, "memory.oom": 2},
        {"pids.max": 0, "memory.oom": 3},
    )["reason"] == "build_memory_limit_exhausted"
    assert guard._resource_event_delta(
        {"memory.swap.max": 0},
        {"memory.swap.max": 1},
    ) is None


def test_cgroup_event_deltas_ignore_cumulative_history() -> None:
    assert guard._cgroup_event_deltas(
        {"memory.swap.max": 3, "memory.swap.fail": 1},
        {"memory.swap.max": 5, "memory.swap.fail": 1},
    ) == {"memory.swap.max": 2}
    assert guard._cgroup_event_deltas(
        {"memory.swap.max": 5},
        {"memory.swap.max": 5},
    ) == {}


def test_resource_health_keeps_running_at_95_percent_without_hard_event() -> None:
    limits = guard.BuildLimits(
        1, 64, 10_000, 10_000, 2_000, 30, 20_000, 2_000,
        host_memory_warning_free_bytes=100,
        host_memory_stop_free_bytes=50,
    )

    health = guard.classify_resource_health(
        limits,
        usage={"MemoryCurrent": 9_600, "TasksCurrent": 61},
        disk_free_bytes=30_000,
        host_snapshot={
            "total_bytes": 1_000,
            "available_bytes": 500,
            "swap_total_bytes": 0,
            "swap_free_bytes": 0,
        },
    )

    assert health["resource_health"] == "critical"
    assert health["decision"] == "continue_with_fast_sampling"
    assert health["hard_stop"] is None
    assert set(health["active_warnings"]) == {"memory_bytes", "pids"}


def test_resource_health_hard_stops_only_on_authoritative_cgroup_event() -> None:
    limits = guard.BuildLimits(
        1, 64, 10_000, 10_000, 2_000, 30, 20_000, 2_000,
    )

    health = guard.classify_resource_health(
        limits,
        usage={
            "MemoryCurrent": 1_000,
            "TasksCurrent": 2,
            "CgroupEvents": {"pids.max": 1},
        },
        disk_free_bytes=30_000,
        host_snapshot=None,
    )

    assert health["resource_health"] == "exhausted"
    assert health["decision"] == "emergency_stop"
    assert health["hard_stop"]["reason"] == "build_pids_limit_exhausted"
    assert health["hard_stop"]["failure_class"] == "resource_exhaustion"


def test_resource_health_uses_swap_and_psi_as_pressure_not_kill() -> None:
    limits = guard.BuildLimits(
        1, 64, 10_000, 10_000, 2_000, 30, 20_000, 2_000,
    )

    health = guard.classify_resource_health(
        limits,
        usage={
            "MemoryCurrent": 2_000,
            "MemorySwapCurrent": 1_920,
            "TasksCurrent": 2,
            "MemoryPSI": {"some_avg10": 55.0, "full_avg10": 2.0},
        },
        disk_free_bytes=30_000,
        host_snapshot={
            "total_bytes": 16_000,
            "available_bytes": 8_000,
            "swap_total_bytes": 10_000,
            "swap_free_bytes": 400,
            "memory_psi": {"some_avg10": 12.0, "full_avg10": 0.0},
        },
    )

    assert health["resource_health"] == "critical"
    assert health["decision"] == "continue_with_fast_sampling"
    assert health["hard_stop"] is None
    assert "swap_bytes" in health["active_warnings"]
    assert "host_swap_free_bytes" in health["active_warnings"]
    assert "cgroup_memory_psi" in health["active_warnings"]


def test_swap_rejection_is_recoverable_critical_evidence() -> None:
    limits = guard.BuildLimits(
        1, 64, 10_000, 10_000, 2_000, 30, 20_000, 2_000,
    )

    critical = guard.classify_resource_health(
        limits,
        usage={
            "MemoryCurrent": 2_000,
            "MemorySwapCurrent": 200,
            "TasksCurrent": 2,
            "CgroupEvents": {"memory.swap.max": 1},
            "CgroupEventDeltas": {"memory.swap.max": 1},
        },
        disk_free_bytes=30_000,
        host_snapshot=None,
    )
    recovered = guard.classify_resource_health(
        limits,
        usage={
            "MemoryCurrent": 2_000,
            "MemorySwapCurrent": 200,
            "TasksCurrent": 2,
            "CgroupEvents": {"memory.swap.max": 1},
            "CgroupEventDeltas": {},
        },
        disk_free_bytes=30_000,
        host_snapshot=None,
    )

    assert critical["resource_health"] == "critical"
    assert critical["decision"] == "continue_with_fast_sampling"
    assert critical["decision_reasons"] == ["swap_allocation_denied"]
    assert critical["active_warnings"] == ["swap_bytes"]
    assert critical["hard_stop"] is None
    assert recovered["resource_health"] == "healthy"
    assert recovered["decision"] == "continue"
    assert recovered["active_warnings"] == []


def test_runtime_probe_failure_degrades_visibility_without_stopping() -> None:
    limits = guard.BuildLimits(
        1, 64, 10_000, 10_000, 2_000, 30, 20_000, 2_000,
    )

    transient = guard.classify_resource_health(
        limits,
        usage={},
        disk_free_bytes=None,
        host_snapshot=None,
        probe_failures={"cgroup": 1, "disk": 1, "host_memory": 1},
    )
    degraded = guard.classify_resource_health(
        limits,
        usage={},
        disk_free_bytes=None,
        host_snapshot=None,
        probe_failures={"cgroup": 3, "disk": 3, "host_memory": 3},
    )

    assert transient["resource_health"] == "healthy"
    assert transient["hard_stop"] is None
    assert degraded["resource_health"] == "unknown"
    assert degraded["decision"] == "continue_with_fast_sampling"
    assert degraded["hard_stop"] is None

    partially_observed = guard.classify_resource_health(
        limits,
        usage={"MemoryCurrent": 9_600},
        disk_free_bytes=None,
        host_snapshot=None,
        probe_failures={"cgroup": 3, "disk": 3},
    )
    assert partially_observed["resource_health"] == "critical"
    assert "cgroup_telemetry_unavailable" in (
        partially_observed["active_warnings"]
    )


def test_payload_wrapper_turns_pids_event_into_error(monkeypatch, capsys) -> None:
    snapshots = iter([{"pids.max": 0}, {"pids.max": 1}])
    monkeypatch.setattr(guard, "_self_cgroup_event_counts", lambda: next(snapshots))

    assert guard._payload_with_cgroup_events(["/bin/true"]) == 125

    event = guard.parse_build_resource_event(capsys.readouterr().err)
    assert event is not None
    assert event["reason"] == "build_pids_limit_exhausted"


def test_payload_wrapper_preserves_success_after_swap_rejection(
    monkeypatch,
) -> None:
    snapshots = iter([
        {"memory.swap.max": 0},
        {"memory.swap.max": 1},
    ])
    monkeypatch.setattr(
        guard, "_self_cgroup_event_counts", lambda: next(snapshots),
    )

    assert guard._payload_with_cgroup_events(["/bin/true"]) == 0


def test_foreground_wait_surfaces_payload_resource_event(monkeypatch, tmp_path) -> None:
    state = _State(tmp_path)
    monkeypatch.setattr(safe_bash, "experiment_output_dir",
                        lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(safe_bash, "kill_guarded_tree",
                        lambda _unit, fallback: fallback())
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=1, disk_stop_free_bytes=0)

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", (
                "import sys; "
                "sys.stderr.write('HARNESS_BUILD_RESOURCE_EVENT "
                "reason=build_pids_limit_exhausted resource=pids counter=1\\n'); "
                "raise SystemExit(125)"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        return await safe_bash._bounded_build_wait(
            proc, state=state, cmd="make -j 1", timeout=10,
            limits=limits, unit="fake.scope")

    result = asyncio.run(run())

    assert result["status"] == "error"
    assert result["reason"] == "build_pids_limit_exhausted"
    assert result["blocker"]["kind"] == "build_resource_pressure"
    assert result["resource_health"] == "exhausted"
    assert result["resource_decision"] == "emergency_stop"


def test_async_executor_delegates_resource_enforcement_to_run_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    from core.sandbox import limits_for_profile

    state = State.new("experiment", tmp_path)
    run_root = experiment_output_dir(state, "runtime", create=True)
    calls = []

    async def managed_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return "done", 0, b"ok\n", b""

    monkeypatch.setattr(safe_bash, "spawn_and_wait", managed_spawn)
    monkeypatch.setattr(
        safe_bash, "_ensure_hardened_attempt_manifest", lambda _state: object())
    result = asyncio.run(safe_bash._exec_and_log(
        state,
        "echo must-run-in-attempt",
        timeout=37,
        cwd=str(run_root),
        resource_profile="large",
    ))

    assert result["status"] == "success"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (
        "/bin/bash", "-o", "pipefail", "-c",
        "echo must-run-in-attempt",
    )
    assert kwargs["state"] is state
    assert kwargs["timeout"] == 37
    assert kwargs["cwd"] == str(run_root)
    assert run_root.resolve() in kwargs["writable_roots"]
    assert Path(state.root).resolve() not in kwargs["writable_roots"]
    assert Path(state.root).resolve() in kwargs["readonly_roots"]
    assert kwargs["sandbox_limits"] == limits_for_profile(
        "large", walltime_seconds=37
    )


def test_async_wait_rejects_zero_exit_with_live_cgroup_descendants(
    monkeypatch,
    tmp_path,
) -> None:
    state = _State(tmp_path)
    limits = guard.BuildLimits(
        1,
        64,
        1_000,
        1_000,
        100,
        10,
        1_000,
        100,
        cgroup_quiescence_grace_s=0,
    )
    monkeypatch.setattr(
        safe_bash, "experiment_output_dir",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        safe_bash,
        "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "busy",
            "tasks_current": 2,
            "snapshot": {"TasksCurrent": 2, "ActiveState": "active"},
        },
    )
    killed = []
    monkeypatch.setattr(
        safe_bash,
        "kill_guarded_tree",
        lambda unit, _fallback: killed.append(unit),
    )

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            "raise SystemExit(0)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return await safe_bash._bounded_build_wait(
            proc,
            state=state,
            cmd="make",
            timeout=10,
            limits=limits,
            unit="fake.scope",
        )

    result = asyncio.run(run())

    assert result["status"] == "error"
    assert result["reason"] == "build_cgroup_not_quiescent"
    assert result["blocker"]["kind"] == "build_process_tree_not_quiescent"
    assert result["blocker"]["resource"] == "pids"
    assert result["blocker"]["current"] == 2
    assert killed == ["fake.scope"]


def test_foreground_supervisor_keeps_running_at_critical_memory_ratio(
    monkeypatch, tmp_path,
) -> None:
    state = _State(tmp_path)
    monkeypatch.setattr(
        safe_bash, "experiment_output_dir",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        safe_bash, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        },
    )
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=1, disk_stop_free_bytes=0,
    )
    real_create = asyncio.create_subprocess_exec

    class _Probe:
        returncode = 0

        async def communicate(self):
            return b"MemoryCurrent=9600\nTasksCurrent=1\n", b""

    async def run():
        proc = await real_create(
            "python3", "-c", "import time; time.sleep(0.2)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def create_probe(*args, **kwargs):
            assert args[:4] == (
                "systemctl", "--user", "show", "fake.scope",
            )
            return _Probe()

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", create_probe,
        )
        return await safe_bash._bounded_build_wait(
            proc, state=state, cmd="make -j 1", timeout=10,
            limits=limits, unit="fake.scope",
        )

    result = asyncio.run(run())

    assert result["status"] == "done"
    assert result["resource_health"] == "critical"
    assert result["resource_decision"] == "continue_with_fast_sampling"
    assert result["resource_guard"]["active_warnings"] == ["memory_bytes"]


def test_foreground_supervisor_swap_rejection_recovers_without_kill(
    monkeypatch, tmp_path,
) -> None:
    state = _State(tmp_path)
    monkeypatch.setattr(
        safe_bash, "experiment_output_dir",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        safe_bash, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        },
    )
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=1, disk_stop_free_bytes=0,
    )
    real_create = asyncio.create_subprocess_exec

    class _Probe:
        returncode = 0

        async def communicate(self):
            return (
                b"MemoryCurrent=1000\nTasksCurrent=1\n"
                b"ControlGroup=/fake.scope\n",
                b"",
            )

    monkeypatch.setattr(
        safe_bash, "_cgroup_files_snapshot",
        lambda _path: {
            "MemorySwapCurrent": 200,
            "CgroupEvents": {"memory.swap.max": 1},
        },
    )

    async def run():
        proc = await real_create(
            "python3", "-c", "import time; time.sleep(0.65)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def create_probe(*args, **kwargs):
            assert args[:4] == (
                "systemctl", "--user", "show", "fake.scope",
            )
            return _Probe()

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", create_probe,
        )
        return await safe_bash._bounded_build_wait(
            proc, state=state, cmd="make -j 1", timeout=10,
            limits=limits, unit="fake.scope",
        )

    result = asyncio.run(run())

    assert result["status"] == "done"
    assert result["resource_health"] == "healthy"
    assert result["resource_decision"] == "continue"
    assert result["resource_guard"]["warnings"] == ["swap_bytes"]
    assert result["resource_guard"]["active_warnings"] == []
    assert any(
        event["event"] == "build_resource_pressure_warning"
        and event["resource"] == "swap_bytes"
        for event in state.events
    )
    assert any(
        event["event"] == "build_resource_pressure_recovered"
        and event["resource"] == "swap_bytes"
        for event in state.events
    )


def test_local_supervisor_argv_keeps_options_before_remainder(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        1, 64, 8_000, 10_000, 2_000, 30, 20_000, 2_000)
    called = {}
    monkeypatch.setattr(
        guard, "supervise_local_build",
        lambda payload, **kwargs: called.update(payload=payload, **kwargs) or 0)

    argv = guard.local_build_supervisor_argv(
        ["make", "-j1"], stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err", status_path=tmp_path / "status",
        limits=limits)

    mode_index = argv.index("supervise-local-build")
    assert argv.index("--limits-json") < mode_index < argv.index("--")
    assert guard._main(argv[2:]) == 0
    assert called["payload"] == ["make", "-j1"]


@needs_systemd
def test_build_wait_warns_on_low_disk_without_truncating_or_stopping(
    monkeypatch, tmp_path,
) -> None:
    state = _State(tmp_path)
    monkeypatch.setattr(safe_bash, "experiment_output_dir",
                        lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(
        safe_bash, "kill_guarded_tree",
        lambda _unit, fallback: fallback())
    monkeypatch.setattr(safe_bash, "filesystem_free_bytes", lambda _path: 500)
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=1000, disk_stop_free_bytes=100)

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", "print('x' * 1000000)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        return await safe_bash._bounded_build_wait(
            proc, state=state, cmd="make -j 1", timeout=10,
            limits=limits, unit="fake.scope")

    result = asyncio.run(run())

    assert result["status"] == "done"
    assert result["log_bytes"] > 256 * 1024
    assert result["truncated"] is False
    assert "disk_free_bytes" in result["resource_guard"]["warnings"]
    assert any(event["event"] == "build_resource_pressure_warning"
               for event in state.events)


@needs_systemd
def test_local_supervisor_warns_on_low_disk_and_keeps_full_log(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=1000, disk_stop_free_bytes=100)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(guard, "_unit_usage", lambda _unit: {})
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _path: 500)
    monkeypatch.setattr(
        guard, "kill_guarded_tree",
        lambda _unit, fallback: fallback())
    stdout = tmp_path / "build.out"
    stderr = tmp_path / "build.err"
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "print('x' * 100000)"],
        stdout_path=stdout, stderr_path=stderr,
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 0
    assert stdout.stat().st_size + stderr.stat().st_size > 4096
    assert "disk_free_bytes" in payload["warnings"]
    assert "average_log_bytes_per_second" in payload["behavior_evidence"]
    assert "pid_growth_per_second" in payload["behavior_evidence"]
    assert payload["required_action"] == "analyze_build_behavior"
    assert "HARNESS_BUILD_RESOURCE_" in stderr.read_text(encoding="utf-8")


def test_local_supervisor_refuses_start_below_emergency_disk_reserve(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=1000, disk_stop_free_bytes=100)
    spawned = []
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _path: 50)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda *_args, **_kwargs: spawned.append(True))
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["echo", "must-not-run"], stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err", status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 125
    assert spawned == []
    assert payload["reason"] == "build_disk_reserve_exhausted"
    assert payload["current"] == 50



def test_local_supervisor_refuses_low_host_memory_before_payload(
    monkeypatch,
    tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1,
        tasks_max=64,
        memory_high_bytes=40,
        memory_max_bytes=40,
        memory_swap_max_bytes=10,
        timeout_s=10,
        disk_warning_free_bytes=1_000,
        disk_stop_free_bytes=100,
        memory_reservation_bytes=40,
        startup_headroom_bytes=40,
        host_memory_warning_free_bytes=100,
        host_memory_stop_free_bytes=50,
    )
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 1_000,
        "available_bytes": 89,
    })
    spawned = []
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda *_args, **_kwargs: spawned.append(True),
    )
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["echo", "must-not-run"],
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
        status_path=status,
        limits=limits,
    )

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 125
    assert spawned == []
    assert payload["reason"] == "build_host_memory_admission_denied"


def test_local_supervisor_stops_when_host_reserve_is_exhausted(
    monkeypatch,
    tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1,
        tasks_max=64,
        memory_high_bytes=1_000,
        memory_max_bytes=1_000,
        memory_swap_max_bytes=100,
        timeout_s=10,
        disk_warning_free_bytes=1_000,
        disk_stop_free_bytes=100,
        memory_reservation_bytes=10,
        host_memory_warning_free_bytes=100,
        host_memory_stop_free_bytes=50,
    )
    probes = 0

    def memory_snapshot():
        nonlocal probes
        probes += 1
        return {
            "total_bytes": 1_000,
            "available_bytes": 1_000 if probes == 1 else 49,
        }

    monkeypatch.setattr(guard, "_host_memory_snapshot", memory_snapshot)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(
        guard, "_unit_usage",
        lambda _unit: {"TasksCurrent": 1, "MemoryCurrent": 1})
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _path: 10_000)
    monkeypatch.setattr(
        guard, "kill_guarded_tree",
        lambda _unit, fallback: fallback())
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
        status_path=status,
        limits=limits,
    )

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 125
    assert payload["reason"] == "build_host_memory_reserve_exhausted"
    assert payload["resource"] == "host_memory_available_bytes"
    assert payload["behavior_evidence"]["host_memory_available_bytes"] == 49


def test_local_supervisor_warns_but_does_not_stop_above_host_emergency_line(
    monkeypatch,
    tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1,
        tasks_max=64,
        memory_high_bytes=1_000,
        memory_max_bytes=1_000,
        memory_swap_max_bytes=100,
        timeout_s=10,
        disk_warning_free_bytes=1_000,
        disk_stop_free_bytes=100,
        memory_reservation_bytes=10,
        host_memory_warning_free_bytes=100,
        host_memory_stop_free_bytes=50,
    )
    probes = 0

    def memory_snapshot():
        nonlocal probes
        probes += 1
        return {
            "total_bytes": 1_000,
            "available_bytes": 1_000 if probes == 1 else 80,
        }

    monkeypatch.setattr(guard, "_host_memory_snapshot", memory_snapshot)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(
        guard, "_unit_usage",
        lambda _unit: {"TasksCurrent": 1, "MemoryCurrent": 1})
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _path: 10_000)
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        })
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(0.2)"],
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
        status_path=status,
        limits=limits,
    )

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 0
    assert "host_memory_available_bytes" in payload["warnings"]
    assert payload["required_action"] == "wait_or_reschedule_host_memory"


def test_local_supervisor_rejects_zero_exit_with_live_cgroup_descendants(
    monkeypatch,
    tmp_path,
) -> None:
    limits = guard.BuildLimits(
        1, 64, 1_000, 1_000, 100, 10, 1_000, 100,
        cgroup_quiescence_grace_s=0,
    )
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(guard, "_unit_usage", lambda _unit: {})
    monkeypatch.setattr(guard, "filesystem_free_bytes", lambda _path: 10_000)
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "busy",
            "tasks_current": 2,
            "snapshot": {"TasksCurrent": 2, "ActiveState": "active"},
        })
    killed = []
    monkeypatch.setattr(
        guard, "kill_guarded_tree",
        lambda unit, _fallback: killed.append(unit))
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "raise SystemExit(0)"],
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
        status_path=status,
        limits=limits,
    )

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 125
    assert killed == ["fake.scope"]
    assert payload["reason"] == "build_cgroup_not_quiescent"
    assert payload["resource"] == "pids"
    assert payload["current"] == 2

def test_local_supervisor_keeps_running_at_critical_pid_ratio(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=20_000, disk_stop_free_bytes=2_000)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(
        guard, "_unit_usage",
        lambda _unit: {"TasksCurrent": 61, "MemoryCurrent": 1024})
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        })
    stdout = tmp_path / "build.out"
    stderr = tmp_path / "build.err"
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(0.2)"],
        stdout_path=stdout, stderr_path=stderr,
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["resource_health"] == "critical"
    assert payload["decision"] == "continue_with_fast_sampling"
    assert payload["active_warnings"] == ["pids"]
    assert "build_pids_limit_approaching" not in stderr.read_text(
        encoding="utf-8")


def test_local_supervisor_keeps_running_at_critical_memory_ratio(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=20_000, disk_stop_free_bytes=2_000)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(
        guard, "_unit_usage",
        lambda _unit: {"TasksCurrent": 1, "MemoryCurrent": 9_600})
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        })
    stdout = tmp_path / "build.out"
    stderr = tmp_path / "build.err"
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(0.2)"],
        stdout_path=stdout, stderr_path=stderr,
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["resource_health"] == "critical"
    assert payload["decision"] == "continue_with_fast_sampling"
    assert payload["active_warnings"] == ["memory_bytes"]
    assert "build_memory_bytes_limit_approaching" not in stderr.read_text(
        encoding="utf-8")


def test_local_supervisor_warning_recovers_and_sampling_state_clears(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=20_000, disk_stop_free_bytes=2_000)
    usages = iter([
        {"TasksCurrent": 1, "MemoryCurrent": 8_500},
        {"TasksCurrent": 1, "MemoryCurrent": 1_000},
    ])

    def next_usage(_unit):
        try:
            return next(usages)
        except StopIteration:
            return {"TasksCurrent": 1, "MemoryCurrent": 1_000}

    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(guard, "_unit_usage", next_usage)
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        })
    status = tmp_path / "guard.json"
    stderr = tmp_path / "err"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(1.1)"],
        stdout_path=tmp_path / "out", stderr_path=stderr,
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    log = stderr.read_text(encoding="utf-8")
    assert rc == 0
    assert payload["warnings"] == ["memory_bytes"]
    assert payload["active_warnings"] == []
    assert payload["resource_health"] == "healthy"
    assert payload["decision"] == "continue"
    assert "HARNESS_BUILD_RESOURCE_RECOVERED resource=memory_bytes" in log


def test_local_supervisor_swap_rejection_warns_recovers_and_completes(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=20_000, disk_stop_free_bytes=2_000)
    usages = iter([
        {
            "TasksCurrent": 1,
            "MemoryCurrent": 1_000,
            "MemorySwapCurrent": 200,
            "CgroupEvents": {"memory.swap.max": 1},
        },
        {
            "TasksCurrent": 1,
            "MemoryCurrent": 1_000,
            "MemorySwapCurrent": 200,
            "CgroupEvents": {"memory.swap.max": 1},
        },
    ])

    def next_usage(_unit):
        try:
            return next(usages)
        except StopIteration:
            return {
                "TasksCurrent": 1,
                "MemoryCurrent": 1_000,
                "MemorySwapCurrent": 200,
                "CgroupEvents": {"memory.swap.max": 1},
            }

    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(guard, "_unit_usage", next_usage)
    monkeypatch.setattr(
        guard, "wait_for_cgroup_quiescence",
        lambda *_args, **_kwargs: {
            "status": "quiet", "tasks_current": 0, "snapshot": {},
        })
    status = tmp_path / "guard.json"
    stderr = tmp_path / "err"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(1.1)"],
        stdout_path=tmp_path / "out", stderr_path=stderr,
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    log = stderr.read_text(encoding="utf-8")
    assert rc == 0
    assert payload["warnings"] == ["swap_bytes"]
    assert payload["active_warnings"] == []
    assert payload["resource_health"] == "healthy"
    assert payload["decision"] == "continue"
    assert payload["behavior_evidence"]["cgroup_event_deltas"] == {}
    assert "HARNESS_BUILD_RESOURCE_WARNING resource=swap_bytes" in log
    assert "HARNESS_BUILD_RESOURCE_RECOVERED resource=swap_bytes" in log


def test_local_supervisor_stops_live_cgroup_exhaustion_event(
    monkeypatch, tmp_path,
) -> None:
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64,
        memory_high_bytes=10_000, memory_max_bytes=10_000,
        memory_swap_max_bytes=2_000, timeout_s=10,
        disk_warning_free_bytes=20_000, disk_stop_free_bytes=2_000)
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda argv, _limits: (argv, None, "fake.scope"))
    monkeypatch.setattr(
        guard, "_unit_usage",
        lambda _unit: {
            "TasksCurrent": 2,
            "MemoryCurrent": 1_000,
            "CgroupEvents": {"pids.max": 1},
        })
    monkeypatch.setattr(
        guard, "kill_guarded_tree",
        lambda _unit, fallback: fallback())
    status = tmp_path / "guard.json"

    rc = guard.supervise_local_build(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
        status_path=status, limits=limits)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert rc == 125
    assert payload["reason"] == "build_pids_limit_exhausted"
    assert payload["failure_class"] == "resource_exhaustion"
    assert payload["resource_health"] == "exhausted"


def test_local_supervisor_does_not_spawn_when_log_is_unavailable(
    monkeypatch, tmp_path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("x", encoding="utf-8")
    status = tmp_path / "guard.json"
    spawned = []
    monkeypatch.setattr(
        guard, "wrap_with_cgroup",
        lambda *_args, **_kwargs: spawned.append(True))
    limits = guard.BuildLimits(
        1, 64, 8_000, 10_000, 2_000, 30, 20_000, 2_000)

    rc = guard.supervise_local_build(
        ["echo", "must-not-run"], stdout_path=blocked_parent / "out",
        stderr_path=tmp_path / "err", status_path=status, limits=limits)

    assert rc == 126
    assert spawned == []
    assert json.loads(status.read_text(encoding="utf-8"))["reason"] == (
        "build_log_unavailable")


def test_foreground_build_kills_cgroup_when_log_cannot_be_created(
    monkeypatch, tmp_path,
) -> None:
    state = _State(tmp_path)
    killed = []
    monkeypatch.setattr(
        safe_bash, "experiment_output_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(
        safe_bash, "kill_guarded_tree",
        lambda _unit, fallback: (killed.append(True), fallback()))
    limits = guard.BuildLimits(
        1, 64, 8_000, 10_000, 2_000, 30, 20_000, 2_000)

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        return await safe_bash._bounded_build_wait(
            proc, state=state, cmd="make", timeout=30,
            limits=limits, unit="fake.scope")

    result = asyncio.run(run())

    assert result["reason"] == "build_log_unavailable"
    assert killed == [True]


def test_python_process_launches_cannot_bypass_managed_build_route() -> None:
    assert safe_bash._python_process_launches(
        "import subprocess as sp\nsp.Popen(['make', '-j', '16'])") == [
            "subprocess.Popen"]
    assert safe_bash._python_process_launches(
        "from subprocess import run as launch\nlaunch(['make'])") == [
            "subprocess.run"]
    assert safe_bash._python_process_launches(
        "import subprocess\nlaunch = subprocess.Popen\nlaunch(['make'])") == [
            "subprocess.Popen"]
    assert safe_bash._python_process_launches(
        "import os\ngetattr(os, 'system')('make')") == ["os.system"]
    assert safe_bash._python_process_launches(
        "import os\nos.execvp('make', ['make'])") == ["os.execvp"]
    assert safe_bash._python_process_launches(
        "import pty\npty.spawn(['/bin/sh'])") == ["pty.spawn"]
    assert safe_bash._python_process_launches("print('pure python')") == []


def test_all_safe_python_uses_core_run_attempt_with_per_call_contract(
    tmp_path, monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="other",
        reason="验证轻量 Python 也有 PID、内存、时间硬边界。",
    )
    assert classified["status"] == "success"
    run_root = experiment_output_dir(state, "runtime", create=True)
    calls = []
    monkeypatch.setenv("SECRET_SENTINEL", "must-not-enter-payload")
    parent_env = dict(os.environ)

    async def managed_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return "done", 0, b"ok\n", b""

    monkeypatch.setattr(safe_bash, "spawn_and_wait", managed_spawn)
    monkeypatch.setattr(
        safe_bash, "_ensure_hardened_attempt_manifest", lambda _state: object())
    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('ok')", cwd=str(run_root), timeout=41))

    assert result["status"] == "success"
    assert result["workspace"] == str(run_root)
    assert result["python_thread_limit"] == 4
    assert result["python_pid_limit"] == 64
    assert result["python_memory_max_bytes"] == int(5 * 1024**3)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:4] == ("/bin/bash", "-o", "pipefail", "-c")
    payload = args[4]
    assert "/usr/bin/env -i" in payload
    assert "SECRET_SENTINEL" not in payload
    assert "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]) in payload
    assert "OMP_NUM_THREADS=4" in payload
    assert "PYTHONPYCACHEPREFIX=" in payload
    assert "_hf_sys.addaudithook" in payload
    assert "print(" in payload and "ok" in payload
    assert kwargs["timeout"] == 41
    assert kwargs["cwd"] == str(run_root)
    assert run_root.resolve() in kwargs["writable_roots"]
    assert Path(state.root).resolve() not in kwargs["writable_roots"]
    assert Path(state.root).resolve() in kwargs["readonly_roots"]
    assert Path(__file__).resolve().parents[3] in kwargs["readonly_roots"]
    assert kwargs["sandbox_limits"].memory_bytes == int(5 * 1024**3)
    assert kwargs["sandbox_limits"].pids == 64
    assert kwargs["sandbox_limits"].walltime_seconds == 41
    assert dict(os.environ) == parent_env


def test_python_runattempt_rejects_portable_profile_before_spawn(
    tmp_path, monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    run_root = experiment_output_dir(state, "runtime", create=True)

    def portable(_state):
        raise RuntimeError(
            "hardened_sandbox_profile_required: portable keeps broad mounts writable")

    async def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("portable profile must fail before spawn")

    monkeypatch.setattr(
        safe_bash, "_ensure_hardened_attempt_manifest", portable)
    monkeypatch.setattr(safe_bash, "spawn_and_wait", unexpected_spawn)
    result = asyncio.run(safe_bash._exec_and_log(
        state, "/usr/bin/python3 -c pass", timeout=5, cwd=str(run_root),
        sandbox_profile="python",
    ))

    assert result["status"] == "error"
    assert result["reason"] == "hardened_sandbox_profile_required"
    assert result["blocker"]["suggested_owner"] == "framework"


def test_legacy_python_requirements_is_rejected_before_executor(monkeypatch) -> None:
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("requirements must not reach the executor")

    monkeypatch.setattr(safe_bash, "_exec_and_log", unexpected)
    result = asyncio.run(safe_bash._safe_execute_python(
        SimpleNamespace(), "print(1)", requirements=["numpy"]))

    assert result["reason"] == "managed_python_workload_required"
    assert result["blocker"]["node_action"] == (
        "move_workload_to_managed_bash_or_submission")


def test_python_resource_terminal_labels_are_not_reported_as_build() -> None:
    result = safe_bash._python_resource_envelope({
        "status": "error",
        "reason": "build_pids_limit_exhausted",
        "blocker": {
            "kind": "build_resource_pressure",
            "reason": "build_pids_limit_exhausted",
        },
        "error": "构建资源接近硬上限",
        "required_action": "analyze_build_behavior",
    })

    assert result["reason"] == "python_pids_limit_exhausted"
    assert result["blocker"]["kind"] == "python_resource_pressure"
    assert result["blocker"]["reason"] == "python_pids_limit_exhausted"
    assert result["required_action"] == "analyze_python_behavior"
    assert "Python 资源" in result["error"]


def test_generic_setup_and_acquire_actions_install_process_guard_without_probe_noise() -> None:
    managed = [
        "pip install torch",
        "python -m pip install numpy",
        "conda install pytorch",
        "spack install wrf",
        "./configure --prefix=/tmp/example",
        "tar xf source.tar.gz",
        "git clone https://example.invalid/source.git",
    ]
    for command in managed:
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert "process_tree" in action["observed_effects"], command

    for probe in [
        "pip --version",
        "./configure --help",
        "tar -tf source.tar.gz",
        "mpirun --version",
    ]:
        action = safe_bash._bash_route_action(
            probe, execution_stage="diagnostic")
        assert "process_tree" not in action["observed_effects"], probe

    assert safe_bash._is_major_build_or_run("mpirun --version") is False


def test_unknown_benchmark_capability_defaults_on_in_group_d(monkeypatch) -> None:
    monkeypatch.setenv("EXPERIMENT_BENCH_GROUP", "D")
    assert safe_bash.bench_enabled("future_safety_capability") is True


def test_unrouted_package_install_is_blocked_before_any_process(tmp_path, monkeypatch) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="package_install",
        reason="本测试只验证软件包安装路线门，不产生可分析的科学结果。",
    )
    assert classified["status"] == "success"
    spawned = False

    async def forbidden(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    monkeypatch.setattr(safe_bash, "_orig_run_bash", forbidden)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "pip install example-package"))

    assert result["status"] == "error"
    # 2026-09-12 效应层拆词：装包仍然必须先有路线步骤（高后果、进程没起），
    # 但它是有界本地动作，不再被逼成外部作业，所以理由是"需要路线"而非"需要受管生命周期"。
    assert result["reason"] == "execution_route_required"
    assert spawned is False


def test_unclassified_python_write_stops_before_runtime_materialization(
    tmp_path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    runtime_root = experiment_output_dir(state, "runtime", create=False)
    executed = False

    async def forbidden(*_args, **_kwargs):
        nonlocal executed
        executed = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state, "open('sentinel', 'w').write('bad')"))

    assert result["reason"] == "experiment_scope_classification_required"
    assert "classify_experiment_scope" in result["error"]
    assert "safe_run_bash" in result["error"]
    assert executed is False
    assert runtime_root.exists() is False



def test_unknown_wrapper_requires_route_before_spawn_or_directory(
    tmp_path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="toolchain_build",
        reason="未知软件入口不能因正则未识别而绕过路线与资源守卫。",
    )
    assert classified["status"] == "success"
    runtime_root = experiment_output_dir(state, "runtime", create=False)
    spawned = False

    async def forbidden(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    monkeypatch.setattr(safe_bash, "_orig_run_bash", forbidden)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./vendor_runner --case smoke"))

    assert result["reason"] == "execution_route_required"
    assert result["blocker"]["resolver_decision"] == "route_unavailable"
    assert spawned is False
    assert runtime_root.exists() is False


def test_path_override_cannot_obtain_read_only_fast_path() -> None:
    command = "PATH=.:$PATH ls"
    action = safe_bash._bash_route_action(
        command, execution_stage="diagnostic")

    assert safe_bash._is_read_only_shell_command(command) is False
    assert action["read_only"] is False
    assert "unknown_executable" in action["observed_effects"]


def test_wrapper_local_path_override_is_unknown() -> None:
    command = "env -i PATH=.:/usr/bin tee output"
    action = safe_bash._bash_route_action(
        command, execution_stage="diagnostic")

    assert action["read_only"] is False
    assert "unknown_executable" in action["observed_effects"]


def test_bounded_program_from_untrusted_path_is_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    fake_tee = tmp_path / "tee"
    fake_tee.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_tee.chmod(0o755)
    monkeypatch.setattr(safe_bash.shutil, "which", lambda _name: str(fake_tee))

    action = safe_bash._bash_route_action(
        "tee output", execution_stage="diagnostic")

    assert action["read_only"] is False
    assert "unknown_executable" in action["observed_effects"]


def test_known_bounded_file_operation_stays_low_risk() -> None:
    action = safe_bash._bash_route_action(
        "mkdir -p output", execution_stage="diagnostic")

    assert action["read_only"] is False
    assert action["observed_effects"] == ["workspace_write"]


def test_unknown_route_entry_still_uses_run_attempt_if_effect_is_underdeclared(
    tmp_path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="other",
        reason="验证未知 operation wrapper 即使漏标 process_tree 仍进入强守卫。",
    )
    assert classified["status"] == "success"
    run_root = experiment_output_dir(state, "runtime", create=True)
    entry = run_root / "vendor_runner"
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route={
            "schema_version": 2,
            "goal": "运行未知名称的官方最小验证入口",
            "evidence_refs": ["test:vendor-runner"],
            "steps": [{
                "id": "smoke",
                "goal": "运行官方 smoke",
                "after": [],
                "action": {
                    "tool": "safe_run_bash",
                    "program": "./vendor_runner",
                },
                "effects": ["workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            }],
        },
    ))
    assert declared["status"] == "success"
    calls = []

    async def fake_exec(
        _state, cmd, timeout=600, cwd=None, resource_profile=None,
        **sandbox_kwargs,
    ):
        calls.append({
            "cmd": cmd,
            "cwd": cwd,
            "resource_profile": resource_profile,
            **sandbox_kwargs,
        })
        return {"status": "success", "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./vendor_runner", route_step_id="smoke",
        resource_profile="large"))

    assert result["status"] == "success", result
    assert len(calls) == 1
    call = calls[0]
    assert call["cmd"] == "./vendor_runner"
    assert call["cwd"] == str(run_root)
    assert call["resource_profile"] == "large"
    assert call["sandbox_profile"] == "bash"
    assert call["sandbox_write_targets"] == []
    writable, readonly = call["sandbox_roots"]
    assert run_root.resolve() in writable
    assert Path(state.root).resolve() in readonly
    assert Path(state.root).resolve() not in writable


def test_unknown_wrapper_resolver_failure_stops_before_spawn_or_directory(
    tmp_path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="toolchain_build",
        reason="本测试验证未知官方构建入口在路线解析故障时不会启动。",
    )
    assert classified["status"] == "success"
    runtime_root = experiment_output_dir(state, "runtime", create=False)
    spawned = False

    async def forbidden(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(
        execution_route,
        "build_route_snapshot",
        lambda _state: (_ for _ in ()).throw(RuntimeError("resolver down")),
    )
    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    monkeypatch.setattr(safe_bash, "_orig_run_bash", forbidden)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./unknown-official-wrapper"))

    assert result["reason"] == "route_event_history_invalid"
    assert result["blocker"]["kind"] == "route_event_history_invalid"
    assert spawned is False
    assert runtime_root.exists() is False


def test_safe_execute_python_rejects_unmanaged_process_before_executor(
    tmp_path,
) -> None:
    state = State.new("experiment", tmp_path)
    runtime_root = experiment_output_dir(state, "runtime", create=False)
    classified = _classify_bound_operation(
        state,
        operation_category="toolchain_build",
        reason="本测试只验证 Python 不得绕过受管构建入口派生外部进程。",
    )
    assert classified["status"] == "success"

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "import subprocess; subprocess.Popen(['make'])"))

    assert result["status"] == "error"
    assert result["reason"] == "unmanaged_python_process_launch"
    assert result["blocker"]["calls"] == ["subprocess.Popen"]
    assert runtime_root.exists() is False


def test_safe_run_bash_rejects_declared_toolchain_build_before_spawn(
    monkeypatch, tmp_path,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="toolchain_build",
        reason="本测试只验证工具链构建资源门，不产生可分析的科学结果。",
    )
    assert classified["status"] == "success"
    calls = []
    build_dir = default_stage_workdir(state, "toolchain_build", create=True)
    compile_entry = build_dir / "vendor-build"
    compile_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    compile_entry.chmod(0o755)
    declared = asyncio.run(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "通过项目官方 wrapper 完成构建",
        "evidence_refs": ["test:vendor-build-entry"],
        "steps": [{
            "id": "build",
            "goal": "运行官方构建入口",
            "after": [],
            "action": {"tool": "submit_job", "program": "vendor-build"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "build_root",
            "expected_outputs": [],
        }],
    }))
    assert declared["status"] == "success"

    async def fake_exec(
        _state, cmd, timeout=600, cwd=None, resource_profile=None,
    ):
        calls.append({
            "cmd": cmd,
            "resource_profile": resource_profile,
            "cwd": cwd,
        })
        return {"status": "success", "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    monkeypatch.setattr(safe_bash, "bench_enabled", lambda _cap: False)
    monkeypatch.setattr(safe_bash._te, "bash_analyzer_unavailable_reason", lambda: None)
    monkeypatch.setattr(safe_bash._te, "looks_backgrounded", lambda _cmd: False)

    blocked = asyncio.run(safe_bash._safe_run_bash(
        state, "./vendor-build", stage="toolchain_build"))

    assert blocked["status"] == "error"
    assert blocked["reason"] == "execution_route_required"
    assert calls == []
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any(item.get("event") == "route_step_bound" for item in events)

def test_submit_job_expected_duration_does_not_become_local_hard_deadline(
    tmp_path,
) -> None:
    state = State.new("experiment", tmp_path)

    result = asyncio.run(resource_manager._submit_job(
        state, command="./compile em_real -j 16", scheduler="local",
        stage="toolchain_build", memory_gb=8, expected_duration_s=900,
        dry_run=True))

    assert result["status"] == "success"
    resource_guard = result["resource_guard"]
    assert resource_guard["tasks_max"] == 160
    assert resource_guard["timeout_s"] is None
    assert resource_guard["hard_deadline_enforced"] is False
    # 不再钉死一个常量字符串：这条记账必须来自隔离层的实际回答（见
    # resource_manager._local_enforcement_account 的 docstring —— 旧值在守不住的
    # 宿主上照样宣称有 cgroup 监护，issue #849 实测）。
    assert resource_guard["enforcement"] in {"native_managed_job", "unknown"}
    if resource_guard["enforcement"] == "native_managed_job":
        assert isinstance(resource_guard["enforced_invariants"], list)
        assert isinstance(resource_guard["missing_for_unattended"], list)
    else:
        assert resource_guard["enforcement_unknown_reason"]

    hard = asyncio.run(resource_manager._submit_job(
        state, command="./compile em_real -j 16", scheduler="local",
        stage="toolchain_build", memory_gb=8, expected_duration_s=900,
        hard_deadline_s=1200, dry_run=True))

    assert hard["status"] == "success"
    assert hard["resource_guard"]["timeout_s"] == 1200
    assert hard["resource_guard"]["hard_deadline_enforced"] is True


def test_external_build_evidence_does_not_claim_platform_pid_or_disk_guards(
    tmp_path,
) -> None:
    state = SimpleNamespace(hook_state={})

    result = resource_manager._submit_sync(
        tmp_path, "slurm", "make -j 8", "build", 1, 8, 0, 12.0, 8.0, 30,
        None, None, None, None, True, None,
        execution_class="toolchain_build", stage_in=None, state=state)

    resource_guard = result["resource_guard"]
    assert resource_guard["enforcement"] == "scheduler_memory_time_contract"
    assert resource_guard["platform_verification_required"] == [
        "pids", "disk_pressure_monitoring", "filesystem_quota"]
    assert "tasks_max" not in resource_guard


def test_kubernetes_build_script_uses_only_explicit_hard_deadline() -> None:
    advisory_only = resource_manager._script_for(
        "kubernetes", "make -j 4", "build", 1, 4, 0, 8.0, 7,
        None, None, "ubuntu:24.04", "/work", "/work/logs", None,
        "/tmp/job")
    explicit_hard = resource_manager._script_for(
        "kubernetes", "make -j 4", "build", 1, 4, 0, 8.0, None,
        None, None, "ubuntu:24.04", "/work", "/work/logs", None,
        "/tmp/job", hard_deadline_s=420)

    assert "activeDeadlineSeconds" not in advisory_only
    assert "activeDeadlineSeconds: 420" in explicit_hard


@pytest.mark.parametrize(
    ("scheduler", "directive"),
    (("slurm", "#SBATCH --time="), ("pbs", "#PBS -l walltime=")),
)
def test_scheduler_script_omits_walltime_until_explicit(
    scheduler,
    directive,
) -> None:
    omitted = resource_manager._script_for(
        scheduler, "make -j 4", "build", 1, 4, 0, 8.0, None,
        None, None, None, "/work", "/work/logs",
    )
    explicit = resource_manager._script_for(
        scheduler, "make -j 4", "build", 1, 4, 0, 8.0, 7,
        None, None, None, "/work", "/work/logs",
    )

    assert directive not in omitted
    assert directive in explicit


def test_explicit_local_submission_contract_is_exact(monkeypatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(guard, "_host_available_memory", lambda: 64 * gib)
    monkeypatch.setattr(guard, "_host_memory_snapshot", lambda: {
        "total_bytes": 128 * gib, "available_bytes": 64 * gib,
    })
    monkeypatch.setattr(
        guard, "_host_disk_usage", lambda _state: (1024**4, 800 * gib),
    )
    state = SimpleNamespace(hook_state={})

    limits = guard.derive_build_limits(
        state,
        "bash job.sh",
        timeout_s=420,
        requested_memory_gb=3.5,
        requested_total_cpus=6,
        exact_resource_contract=True,
    )

    assert limits.memory_max_bytes == int(3.5 * gib)
    assert limits.memory_reservation_bytes == int(3.5 * gib)
    assert limits.memory_swap_max_bytes == 0
    assert limits.cpu_quota_percent == 600
    assert limits.timeout_s == 420
    assert limits.resource_policy == "fixed_submission_contract"
    assert limits.memory_request_source == "runtime_request"


@needs_reaping_init
def test_external_task_cancel_reaps_process_group_and_closes_log(
    tmp_path,
) -> None:
    async def scenario() -> None:
        state = _State(tmp_path)
        limits = guard.derive_build_limits(
            state, "sleep 20", timeout_s=30,
        )
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        baseline_tasks = set(asyncio.all_tasks())
        task = asyncio.create_task(safe_bash._bounded_process_wait(
            proc,
            state=state,
            cmd="sleep 20",
            timeout=30,
            limits=limits,
            unit=None,
            strong_guard=False,
        ))
        log_path = None
        try:
            for _ in range(100):
                candidates = list(
                    experiment_output_dir(
                        state, "runtime/logs", create=True
                    ).glob("*.log")
                )
                if candidates:
                    log_path = candidates[0]
                    break
                await asyncio.sleep(0.01)
            assert log_path is not None

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("supervisor 必须继续传播外部取消")

            for _ in range(100):
                if proc.returncode is not None:
                    try:
                        os.killpg(proc.pid, 0)
                    except ProcessLookupError:
                        break
                await asyncio.sleep(0.01)
            assert proc.returncode is not None
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("取消返回后 payload 进程组仍存活")

            log_target = log_path.resolve()
            open_targets = []
            for descriptor in Path("/proc/self/fd").iterdir():
                try:
                    open_targets.append(
                        Path(os.readlink(descriptor)).resolve()
                    )
                except (FileNotFoundError, OSError):
                    continue
            assert log_target not in open_targets
            assert any(
                event["event"] == "execution_cancel_cleanup"
                and event["process_returncode"] is not None
                for event in state.events
            )

            await asyncio.sleep(0)
            leaked = [
                pending for pending in asyncio.all_tasks() - baseline_tasks
                if not pending.done()
            ]
            assert leaked == []
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()

    asyncio.run(scenario())


def test_external_task_cancel_uses_cgroup_kill_before_propagation(
    tmp_path, monkeypatch,
) -> None:
    async def scenario() -> None:
        state = _State(tmp_path)
        limits = guard.derive_build_limits(
            state, "sleep 20", timeout_s=30,
        )
        kill_calls = []
        quiet_calls = []

        def fake_kill(unit, fallback_kill):
            kill_calls.append(unit)
            fallback_kill()

        def fake_quiet(unit, *, grace_s, poll_interval_s=0.1):
            quiet_calls.append((unit, grace_s))
            return {"status": "quiet", "tasks_current": 0}

        monkeypatch.setattr(safe_bash, "kill_guarded_tree", fake_kill)
        monkeypatch.setattr(
            safe_bash, "wait_for_cgroup_quiescence", fake_quiet,
        )
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        task = asyncio.create_task(safe_bash._bounded_process_wait(
            proc,
            state=state,
            cmd="sleep 20",
            timeout=30,
            limits=limits,
            unit="hf-cancel-test.scope",
            strong_guard=True,
        ))
        try:
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("supervisor 必须继续传播外部取消")

            assert kill_calls == ["hf-cancel-test.scope"]
            assert quiet_calls and quiet_calls[0][0] == (
                "hf-cancel-test.scope"
            )
            assert proc.returncode is not None
            cleanup_events = [
                event for event in state.events
                if event["event"] == "execution_cancel_cleanup"
            ]
            assert cleanup_events
            assert cleanup_events[-1]["cgroup_quiescence"]["status"] == (
                "quiet"
            )
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()

    asyncio.run(scenario())


@needs_reaping_init
def test_cancel_during_quiescence_reaps_detached_process_group(
    tmp_path, monkeypatch,
) -> None:
    async def scenario() -> None:
        state = _State(tmp_path)
        limits = guard.derive_build_limits(
            state, "sleep 20 &", timeout_s=30,
        )
        entered_quiescence = threading.Event()
        release_first_probe = threading.Event()
        probe_calls = []
        kill_calls = []

        def fake_quiescence(unit, *, grace_s, poll_interval_s=0.1):
            probe_calls.append(unit)
            if len(probe_calls) == 1:
                entered_quiescence.set()
                release_first_probe.wait(timeout=5)
            return {"status": "quiet", "tasks_current": 0}

        def fake_kill(unit, fallback_kill):
            kill_calls.append(unit)
            fallback_kill()

        monkeypatch.setattr(
            safe_bash, "wait_for_cgroup_quiescence", fake_quiescence,
        )
        monkeypatch.setattr(safe_bash, "kill_guarded_tree", fake_kill)
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c",
            "sleep 20 </dev/null >/dev/null 2>&1 &",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        task = asyncio.create_task(safe_bash._bounded_process_wait(
            proc,
            state=state,
            cmd="sleep 20 &",
            timeout=30,
            limits=limits,
            unit="hf-quiescence-cancel.scope",
            strong_guard=True,
        ))
        try:
            for _ in range(200):
                if entered_quiescence.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered_quiescence.is_set()
            assert proc.returncode == 0

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("quiescence 阶段取消必须继续向上层传播")

            assert kill_calls == ["hf-quiescence-cancel.scope"]
            for _ in range(100):
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("顶层退出后的后台进程组仍存活")
            assert any(
                event["event"] == "execution_cancel_cleanup"
                and event["process_returncode"] == 0
                for event in state.events
            )
        finally:
            release_first_probe.set()
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    asyncio.run(scenario())


def test_internal_probe_timeout_kills_and_waits_probe() -> None:
    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                await safe_bash._communicate_bounded_probe(
                    proc, timeout=0.02,
                )
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("阻塞探针必须触发有界超时")
            assert proc.returncode is not None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(scenario())


def test_internal_probe_task_cancel_kills_and_waits_probe() -> None:
    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task = asyncio.create_task(
            safe_bash._communicate_bounded_probe(proc, timeout=30)
        )
        try:
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("探针取消必须继续向上层传播")
            assert proc.returncode is not None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(scenario())


def test_supervisor_cancel_reaps_payload_and_active_systemctl_probe(
    tmp_path, monkeypatch,
) -> None:
    async def scenario() -> None:
        state = _State(tmp_path)
        limits = guard.derive_build_limits(
            state, "sleep 20", timeout_s=30,
        )
        original_spawn = asyncio.create_subprocess_exec
        payload = await original_spawn(
            "/bin/sh", "-c", "sleep 20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        probe_started = asyncio.Event()
        probes = []

        async def tracked_spawn(*argv, **kwargs):
            if argv and argv[0] == "systemctl":
                probe = await original_spawn(
                    "/bin/sh", "-c", "sleep 20",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                probes.append(probe)
                probe_started.set()
                return probe
            return await original_spawn(*argv, **kwargs)

        monkeypatch.setattr(
            safe_bash.asyncio,
            "create_subprocess_exec",
            tracked_spawn,
        )
        monkeypatch.setattr(
            safe_bash,
            "kill_guarded_tree",
            lambda _unit, fallback_kill: fallback_kill(),
        )
        monkeypatch.setattr(
            safe_bash,
            "wait_for_cgroup_quiescence",
            lambda *_args, **_kwargs: {
                "status": "quiet", "tasks_current": 0,
            },
        )
        supervisor = asyncio.create_task(
            safe_bash._bounded_process_wait(
                payload,
                state=state,
                cmd="sleep 20",
                timeout=30,
                limits=limits,
                unit="hf-probe-cancel.scope",
                strong_guard=True,
            )
        )
        try:
            await asyncio.wait_for(probe_started.wait(), timeout=2)
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("supervisor 取消必须继续向上层传播")
            assert payload.returncode is not None
            assert probes and all(
                probe.returncode is not None for probe in probes
            )
        finally:
            if payload.returncode is None:
                try:
                    os.killpg(payload.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await payload.wait()
            for probe in probes:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()

    asyncio.run(scenario())


def test_build_gate_exception_blocks_payload_even_with_bypass(
    tmp_path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    classified = _classify_bound_operation(
        state,
        operation_category="other",
        reason="验证 build gate 异常即使 bypass 也必须阻断 payload。",
    )
    assert classified["status"] == "success", classified
    runtime = experiment_output_dir(state, "runtime", create=True)
    spawned = []

    def broken_gate(*_args, **_kwargs):
        raise RuntimeError("build gate crashed")

    async def forbidden_executor(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("payload must not start after build gate failure")

    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    monkeypatch.setattr(safe_bash, "_build_gate", broken_gate)
    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_executor)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "echo build-gate-check", cwd=str(runtime)))

    assert result["status"] == "error"
    assert result["reason"] == "build_gate_unavailable"
    assert result["blocker"]["guard"] == "build_gate"
    assert result["blocker"]["source"] == "build_gate"
    assert result["blocker"]["bypass_allowed"] is False
    assert spawned == []
