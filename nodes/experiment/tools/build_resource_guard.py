"""Experiment 节点的本机构建资源守卫。

这里只负责把已经通过现有路径/高危/build gate 的构建进程关进 cgroup。
它不判断软件该怎样编译，也不替代官方构建入口。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_MIN_MEMORY_BYTES = 2 * 1024**3
_MAX_DEFAULT_MEMORY_BYTES = 32 * 1024**3
_HOST_MEMORY_WARNING_MIN_BYTES = 512 * 1024**2
_HOST_MEMORY_WARNING_MAX_BYTES = 64 * 1024**3
_HOST_MEMORY_STOP_MIN_BYTES = 256 * 1024**2
_HOST_MEMORY_STOP_MAX_BYTES = 32 * 1024**3
_STARTUP_HEADROOM_MIN_BYTES = 256 * 1024**2
_STARTUP_HEADROOM_MAX_BYTES = 1024**3
_DISK_WARNING_MIN_BYTES = 2 * 1024**3
_DISK_WARNING_MAX_BYTES = 100 * 1024**3
_DISK_STOP_MIN_BYTES = 512 * 1024**2
_DISK_STOP_MAX_BYTES = 20 * 1024**3

_RESOURCE_EVENT_PREFIX = "HARNESS_BUILD_RESOURCE_EVENT"
_RESOURCE_EVENT_RE = re.compile(
    rf"{_RESOURCE_EVENT_PREFIX}\s+reason=(?P<reason>\S+)\s+"
    r"resource=(?P<resource>\S+)\s+counter=(?P<counter>\d+)")


@dataclass(frozen=True)
class BuildLimits:
    parallelism: int
    tasks_max: int
    memory_high_bytes: int
    memory_max_bytes: int
    memory_swap_max_bytes: int
    # None means this managed job has no Experiment-owned wall-clock kill.
    # PID/memory/disk/cancellation enforcement remains active.  A numeric value
    # is reserved for an explicit hard deadline (or a synchronous diagnostic
    # timeout whose API already promises destructive timeout semantics).
    timeout_s: int | None
    disk_warning_free_bytes: int
    disk_stop_free_bytes: int
    warning_ratio: float = 0.80
    stop_ratio: float = 0.95
    cpu_quota_percent: int | None = None
    resource_policy: str | None = None
    resource_plan_artifact_id: str | None = None
    # 资源请求/计划值用于保留调用方意图与判断宿主是否确定无法容纳，
    # 不再表示“payload 启动前必须立即空出的全部内存”。
    memory_reservation_bytes: int | None = None
    memory_request_source: str | None = None
    # 启动余量只回答当前能否安全创建 supervisor/payload；运行中的
    # 内存包络由 MemoryMax 与宿主紧急保留线独立约束。None 仅供旧序列化
    # BuildLimits 兼容，准入时会回退到旧的全额承诺语义。
    startup_headroom_bytes: int | None = None
    host_memory_warning_free_bytes: int = 0
    host_memory_stop_free_bytes: int = 0
    cgroup_quiescence_grace_s: float = 2.0

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _pressure_snapshot(path: Path) -> dict[str, float] | None:
    """读取 Linux PSI 的 avg10；不可用时返回 None，不伪造零压力。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    result: dict[str, float] = {}
    try:
        for line in lines:
            kind, *parts = line.split()
            if kind not in {"some", "full"}:
                continue
            fields = dict(part.split("=", 1) for part in parts if "=" in part)
            if "avg10" in fields:
                result[f"{kind}_avg10"] = float(fields["avg10"])
    except (TypeError, ValueError):
        return None
    return result or None


def _host_memory_snapshot() -> dict[str, Any] | None:
    """读取宿主内存、Swap 与 PSI；核心内存字段不可读时不伪造容量。"""
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as stream:
            for line in stream:
                key, separator, raw = line.partition(":")
                if separator and key in {
                    "MemTotal", "MemAvailable", "SwapTotal", "SwapFree",
                }:
                    values[key] = int(raw.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", -1)
    if total <= 0 or available < 0:
        return None
    result: dict[str, Any] = {
        "total_bytes": total,
        "available_bytes": min(available, total),
    }
    swap_total = max(0, values.get("SwapTotal", 0))
    swap_free = max(0, values.get("SwapFree", 0))
    result.update(
        swap_total_bytes=swap_total,
        swap_free_bytes=min(swap_free, swap_total),
    )
    pressure = _pressure_snapshot(Path("/proc/pressure/memory"))
    if pressure is not None:
        result["memory_psi"] = pressure
    return result


def _host_available_memory() -> int:
    """兼容限制推导入口；真正准入使用严格的 snapshot。"""
    snapshot = _host_memory_snapshot()
    if snapshot is None:
        return 8 * 1024**3
    return snapshot["available_bytes"]


def _host_memory_reserve_limits(total_bytes: int) -> tuple[int, int]:
    """按宿主容量推导新任务预留线与运行中紧急停止线。"""
    total = max(1, int(total_bytes))
    warning = min(
        _HOST_MEMORY_WARNING_MAX_BYTES,
        max(_HOST_MEMORY_WARNING_MIN_BYTES, total // 10),
        max(1, total // 2),
    )
    stop = min(
        _HOST_MEMORY_STOP_MAX_BYTES,
        max(_HOST_MEMORY_STOP_MIN_BYTES, total // 20),
        max(1, warning - 1),
    )
    return warning, min(stop, max(1, warning - 1))


def _startup_headroom_limit(total_bytes: int) -> int:
    """推导与任务规模无关的最低启动余量。

    这不是资源预留或软上限；它只避免在宿主已无法安全创建
    supervisor/payload 时继续入场。运行期增长仍由 cgroup 和宿主紧急线守护。
    """
    total = max(1, int(total_bytes))
    return min(
        _STARTUP_HEADROOM_MAX_BYTES,
        max(_STARTUP_HEADROOM_MIN_BYTES, total // 100),
        max(1, total // 4),
    )


def host_memory_admission_block(
    limits: BuildLimits,
) -> dict[str, Any] | None:
    """在 payload 启动前只判断请求可行性与最低启动余量。

    这是单任务 best-effort 准入，不是多用户原子预留；跨任务聚合仍应由
    平台父 cgroup 或调度器负责。
    """
    warning = max(0, int(limits.host_memory_warning_free_bytes))
    stop = max(0, int(limits.host_memory_stop_free_bytes))
    if warning == 0 and stop == 0:
        return None
    snapshot = _host_memory_snapshot()
    if snapshot is None:
        return {
            "status": "error",
            "reason": "build_host_memory_probe_unavailable",
            "resource": "host_memory_available_bytes",
            "error": "无法读取实时 MemAvailable，已在 payload 启动前拒绝执行。",
            "resource_guard": limits.public(),
        }
    request = max(
        1,
        int(limits.memory_reservation_bytes or limits.memory_max_bytes),
    )
    safe_capacity = max(0, snapshot["total_bytes"] - stop)
    if request > safe_capacity:
        return {
            "status": "error",
            "reason": "build_host_memory_request_infeasible",
            "failure_class": "resource_request_infeasible",
            "resource": "host_memory_total_bytes",
            "current": snapshot["total_bytes"],
            "available_bytes": snapshot["available_bytes"],
            "required": request + stop,
            "deficit_bytes": request - safe_capacity,
            "max_admissible_commitment_bytes": safe_capacity,
            "memory_reservation_bytes": request,
            "memory_hard_limit_bytes": limits.memory_max_bytes,
            "memory_request_source": limits.memory_request_source,
            "warning_reserve_bytes": warning,
            "emergency_reserve_bytes": stop,
            "retryable": False,
            "suggested_actions": [
                "use_an_approved_smaller_resource_plan",
                "submit_to_scheduler",
            ],
            "error": (
                "本任务内存请求超过宿主在保留紧急余量后的最大可行容量；"
                "请使用经批准的更小资源计划或改用调度器。"
            ),
            "resource_guard": limits.public(),
        }
    # None 只可能来自旧序列化 BuildLimits，安全地保留旧的全额准入语义。
    startup_headroom = max(
        1,
        int(limits.startup_headroom_bytes or request),
    )
    required = stop + startup_headroom
    available = snapshot["available_bytes"]
    if available >= required:
        return None
    return {
        "status": "error",
        "reason": "build_host_memory_admission_denied",
        "failure_class": "transient_host_pressure",
        "resource": "host_memory_available_bytes",
        "current": available,
        "available_bytes": available,
        "required": required,
        "deficit_bytes": required - available,
        "max_admissible_commitment_bytes": safe_capacity,
        "memory_reservation_bytes": request,
        "startup_headroom_bytes": startup_headroom,
        "memory_hard_limit_bytes": limits.memory_max_bytes,
        "memory_request_source": limits.memory_request_source,
        "warning_reserve_bytes": warning,
        "emergency_reserve_bytes": stop,
        "retryable": True,
        "suggested_actions": [
            "wait_for_host_memory",
            "submit_to_scheduler",
        ],
        "error": (
            "实时 MemAvailable 已不足以同时保留宿主紧急余量并安全启动"
            "supervisor/payload；这是可重试的瞬时压力，请等待内存释放或改用调度器。"
        ),
        "resource_guard": limits.public(),
    }


def _host_disk_usage(state: Any) -> tuple[int, int]:
    """返回本次运行所在文件系统的总容量与可用字节数。"""
    candidate = Path(getattr(state, "root", None) or os.getcwd()).expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
        return int(usage.total), int(usage.free)
    except OSError:
        return 64 * 1024**3, 64 * 1024**3


def _disk_reserve_limits(total_bytes: int) -> tuple[int, int]:
    """从文件系统容量推导预警线和宿主机紧急保护线。"""
    total = max(1, int(total_bytes))
    warning = min(
        _DISK_WARNING_MAX_BYTES,
        max(_DISK_WARNING_MIN_BYTES, total // 20),
        max(1, total // 2),
    )
    stop = min(
        _DISK_STOP_MAX_BYTES,
        max(_DISK_STOP_MIN_BYTES, total // 100),
        max(1, total // 4),
    )
    return warning, min(stop, max(1, warning - 1))


def filesystem_free_bytes(path: Path) -> int | None:
    """读取日志实际所在文件系统的剩余空间。"""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def command_parallelism(command: str) -> int:
    """提取常见构建并行度；无显式值时保守使用 1，而非猜满整机 CPU。"""
    patterns = (
        r"(?:^|\s)-j\s*(\d+)(?=\s|$)",
        r"(?:^|\s)--jobs(?:=|\s+)(\d+)(?=\s|$)",
        r"(?:^|\s)--parallel(?:=|\s+)(\d+)(?=\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, command or "")
        if match:
            return max(1, min(int(match.group(1)), 256))
    if re.search(r"(?:^|\s)-j(?=\s|$)", command or ""):
        # 裸 -j 表示无限并行。不要按 1 估算，否则 PID 上限反而过紧且证据失真。
        return 256
    # GNU make 常从环境中的 MAKEFLAGS/MFLAGS 取得并行度。若只看 argv，
    # ``MAKEFLAGS=-j64 make`` 会被误估为单线程并得到 TasksMax=64，正常
    # 并行编译反而可能被守卫误伤。显式 argv 的 -j 已在上方优先处理。
    for match in re.finditer(
        r"(?:^|\s)(?:MAKEFLAGS|MFLAGS)=(?:\"([^\"]*)\"|'([^']*)'|(\S+))",
        command or "",
    ):
        flags = next((part for part in match.groups() if part is not None), "")
        for pattern in patterns:
            nested = re.search(pattern, flags)
            if nested:
                return max(1, min(int(nested.group(1)), 256))
        if re.search(r"(?:^|\s)-j(?=\s|$)", flags):
            return 256
    return 1


def active_build_resource_plan(state: Any) -> dict[str, Any]:
    """返回现有 preflight 的同一份结构化计划；不从自然语言猜预算。"""
    try:
        plan = state.hook_state.get("build_resource_preflight") or {}
    except AttributeError:
        return {}
    return dict(plan) if isinstance(plan, dict) else {}


def build_resource_plan_block(
    state: Any,
    command: str,
) -> dict[str, Any] | None:
    """canonical configure/build 的事前资源承诺门。"""
    plan = active_build_resource_plan(state)
    if not plan:
        return {
            "status": "error",
            "error_code": "build_resource_preflight_required",
            "reason": "build_resource_preflight_required",
            "error": (
                "canonical configure/build 在启动前必须调用 "
                "preflight_build_resources，把 CPU、内存和 walltime 形成"
                "结构化计划；自然语言预算不能替代机械资源契约。"
            ),
            "blocker": {
                "kind": "build_resource_preflight_required",
                "suggested_owner": "experiment",
                "required_tool": "preflight_build_resources",
            },
        }
    status = str(plan.get("status") or "")
    if status != "success":
        return {
            "status": "error",
            "error_code": "build_resource_preflight_not_approved",
            "reason": "build_resource_preflight_not_approved",
            "error": (
                "最近一次 build_resource_plan 未获准执行；必须先解决其中的"
                "环境/容量问题或取得新的上游资源契约，不能绕过后继续构建。"
            ),
            "resource_plan_status": status or "invalid",
            "resource_plan_decision": plan.get("decision"),
            "blocker": {
                "kind": "build_resource_preflight_not_approved",
                "suggested_owner": "experiment",
                "resource_plan_status": status or "invalid",
            },
        }
    requested = plan.get("requested_resources") or {}
    try:
        total_cpus = int(requested["total_cpus"])
        memory_gb = float(requested["memory_gb"])
        walltime_minutes = int(requested["walltime_minutes"])
    except (KeyError, TypeError, ValueError):
        return {
            "status": "error",
            "error_code": "build_resource_plan_invalid",
            "reason": "build_resource_plan_invalid",
            "error": (
                "build_resource_plan 缺少有效的 total_cpus、memory_gb 或 "
                "walltime_minutes；请重新运行 preflight_build_resources。"
            ),
            "blocker": {
                "kind": "build_resource_plan_invalid",
                "suggested_owner": "experiment",
            },
        }
    if total_cpus < 1 or memory_gb <= 0 or walltime_minutes < 1:
        return {
            "status": "error",
            "error_code": "build_resource_plan_invalid",
            "reason": "build_resource_plan_invalid",
            "error": "build_resource_plan 的 CPU、内存和 walltime 必须为正值。",
            "blocker": {
                "kind": "build_resource_plan_invalid",
                "suggested_owner": "experiment",
            },
        }
    policy = str(plan.get("runtime_resource_policy") or "fixed")
    observed_parallelism = command_parallelism(command)
    if policy == "fixed" and observed_parallelism > total_cpus:
        return {
            "status": "error",
            "error_code": "build_parallelism_exceeds_plan",
            "reason": "build_parallelism_exceeds_plan",
            "error": (
                f"命令并行度 {observed_parallelism} 超过 fixed 计划的 "
                f"{total_cpus} CPU；请降低 -j/--parallel，或先形成新的资源计划。"
            ),
            "declared_total_cpus": total_cpus,
            "observed_parallelism": observed_parallelism,
            "blocker": {
                "kind": "build_parallelism_exceeds_plan",
                "suggested_owner": "experiment",
            },
        }
    return None


def _planned_memory_bytes(state: Any) -> int | None:
    try:
        requested = active_build_resource_plan(state).get(
            "requested_resources"
        ) or {}
        value = requested.get("memory_gb")
        if value is not None and float(value) > 0:
            return int(float(value) * 1024**3)
    except (TypeError, ValueError):
        pass
    return None


def derive_build_limits(
    state: Any,
    command: str,
    timeout_s: int | None,
    requested_memory_gb: float | None = None,
    requested_total_cpus: int | None = None,
    exact_resource_contract: bool = False,
    memory_request_source: str | None = None,
) -> BuildLimits:
    """从本次资源计划和命令并行度推导弹性上限。"""
    parallelism = command_parallelism(command)
    fallback_available = max(_MIN_MEMORY_BYTES, _host_available_memory())
    host_snapshot = _host_memory_snapshot()
    host_total = (
        host_snapshot["total_bytes"]
        if host_snapshot is not None
        else max(8 * 1024**3, fallback_available)
    )
    host_memory_warning, host_memory_stop = _host_memory_reserve_limits(
        host_total
    )
    safe_host_capacity = max(1, host_total - host_memory_stop)
    plan = active_build_resource_plan(state)
    plan_status = str(plan.get("status") or "")
    policy = str(plan.get("runtime_resource_policy") or "flexible")
    requested = plan.get("requested_resources") or {}
    planned = (int(float(requested_memory_gb) * 1024**3)
               if requested_memory_gb is not None else _planned_memory_bytes(state))
    fixed_plan = (
        exact_resource_contract
        or (requested_memory_gb is None
            and plan_status == "success" and policy == "fixed")
    )
    if memory_request_source is None:
        if requested_memory_gb is not None:
            memory_request_source = "runtime_request"
        elif planned is not None:
            memory_request_source = "build_resource_plan"
        else:
            memory_request_source = "automatic_local_guard"
    if exact_resource_contract:
        effective_resource_policy = "fixed_submission_contract"
    elif memory_request_source == "build_resource_plan" and plan_status == "success":
        effective_resource_policy = policy
    elif requested_memory_gb is not None:
        effective_resource_policy = "flexible"
    elif plan_status == "success":
        effective_resource_policy = policy
    else:
        effective_resource_policy = None
    if planned is None:
        # automatic 包络只是运行时硬上限，不是启动前的实时预留。
        # 基于宿主总容量而非瞬时 MemAvailable，避免同一任务的
        # MemoryMax 随其他用户的短期波动改变。
        memory_max = min(
            safe_host_capacity,
            _MAX_DEFAULT_MEMORY_BYTES,
            max(_MIN_MEMORY_BYTES, host_total // 8),
        )
        memory_reservation = memory_max
    elif fixed_plan:
        # fixed 请求与硬上限必须精确相等；是否超过宿主安全
        # 总容量由 host_memory_admission_block 给出“确定不可行”证据。
        memory_reservation = max(1, planned)
        memory_max = memory_reservation
    else:
        # flexible 计划保留请求值与 25% 受限突发包络。入口只检查
        # 请求是否确定超过宿主总能力，不要求当前立即空出整个计划值。
        memory_reservation = max(1, planned)
        burst_limit = max(1, int(planned * 1.25))
        memory_max = max(1, min(burst_limit, safe_host_capacity))
    # systemd 的 MemoryHigh 是内核节流线，不是观察型预警线。若设为 80%，
    # 编译/链接会在本应只是记录证据的区间被显著减速。80%/95% 由监督器
    # 解释为压力/临界证据并加密采样；MemoryMax 与 cgroup events 才提供
    # 权威硬兜底，故将 MemoryHigh 对齐到 MemoryMax 而非提前节流。
    memory_high = memory_max
    bounded_timeout = (
        max(1, int(timeout_s)) if timeout_s is not None else None
    )
    cpu_quota_percent = None
    task_parallelism = parallelism
    if fixed_plan:
        if exact_resource_contract:
            try:
                total_cpus = max(1, int(requested_total_cpus or parallelism))
            except (TypeError, ValueError):
                total_cpus = max(1, parallelism)
            walltime_s = bounded_timeout
        else:
            try:
                total_cpus = max(1, int(requested["total_cpus"]))
                walltime_s = max(60, int(requested["walltime_minutes"]) * 60)
            except (KeyError, TypeError, ValueError):
                total_cpus = 1
                walltime_s = bounded_timeout
        # 固定资源计划是执行器的并发能力上限。即使命令本身没有显式
        # ``-j``（例如 ``cmake --build`` 交给 Ninja 自行取并行度），
        # cgroup 的 PID 包络也必须覆盖计划允许的总 CPU 数，不能只按
        # 命令文本推导成单并发而误杀正常编译。
        task_parallelism = max(task_parallelism, total_cpus)
        cpu_quota_percent = total_cpus * 100
        if bounded_timeout is not None and walltime_s is not None:
            bounded_timeout = min(bounded_timeout, walltime_s)
    tasks_max = max(64, min(256, task_parallelism * 8 + 32))
    disk_total, _disk_free = _host_disk_usage(state)
    disk_warning, disk_stop = _disk_reserve_limits(disk_total)
    return BuildLimits(
        parallelism=parallelism,
        tasks_max=tasks_max,
        memory_high_bytes=memory_high,
        memory_max_bytes=memory_max,
        memory_swap_max_bytes=(
            0 if fixed_plan else max(512 * 1024**2, memory_max // 4)
        ),
        timeout_s=bounded_timeout,
        disk_warning_free_bytes=disk_warning,
        disk_stop_free_bytes=disk_stop,
        cpu_quota_percent=cpu_quota_percent,
        resource_policy=effective_resource_policy,
        resource_plan_artifact_id=(
            str(plan.get("artifact_id"))
            if (
                memory_request_source == "build_resource_plan"
                and plan.get("artifact_id")
            )
            else None
        ),
        memory_reservation_bytes=memory_reservation,
        memory_request_source=memory_request_source,
        startup_headroom_bytes=_startup_headroom_limit(host_total),
        host_memory_warning_free_bytes=host_memory_warning,
        host_memory_stop_free_bytes=host_memory_stop,
        cgroup_quiescence_grace_s=2.0,
    )


def systemd_guard_available() -> tuple[bool, str | None]:
    if not shutil.which("systemd-run"):
        return False, "systemd-run 不存在"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return False, "XDG_RUNTIME_DIR 未设置，无法连接用户级 systemd"
    if not os.path.exists(os.path.join(runtime, "systemd", "private")):
        return False, "用户级 systemd bus 不可用"
    return True, None


def wrap_with_cgroup(
    argv: list[str], limits: BuildLimits,
) -> tuple[list[str] | None, str | None, str | None]:
    """返回 systemd scope argv；守卫不可用时 fail-closed。"""
    available, reason = systemd_guard_available()
    if not available:
        return None, reason, None
    unit = f"hf-experiment-build-{os.getpid()}-{uuid.uuid4().hex[:10]}.scope"
    # 外部监督器按秒采样 TasksCurrent，极快的 make -i 递归可能在下一次采样
    # 前就因 pids.max 被拒绝并以 0 退出。这个极小 wrapper 本身处于同一
    # cgroup，能在 payload 退出后读取 pids.events / memory.events 的增量，
    # 把被构建系统吞掉的资源拒绝转成结构化失败；它不理解任何应用构建语义。
    guarded_payload = [
        sys.executable, str(Path(__file__).resolve()),
        "payload-with-cgroup-events", "--", *argv,
    ]
    properties = [
        # payload OOM 时保留同 scope 内的轻量事件包装器；否则 systemd 默认
        # OOMPolicy 可能停止整个 scope，外层只能看到裸 SIGTERM，读不到
        # memory.events。硬上限不变，包装器随后把 OOM 归类并退出 125。
        "-p", "OOMPolicy=continue",
        "-p", f"TasksMax={limits.tasks_max}",
        "-p", f"MemoryHigh={limits.memory_high_bytes}",
        "-p", f"MemoryMax={limits.memory_max_bytes}",
        "-p", f"MemorySwapMax={limits.memory_swap_max_bytes}",
    ]
    if limits.timeout_s is not None:
        properties.extend([
            "-p", f"RuntimeMaxSec={limits.timeout_s}",
        ])
    if limits.cpu_quota_percent is not None:
        properties.extend([
            "-p", f"CPUQuota={limits.cpu_quota_percent}%",
        ])
    wrapped = [
        "systemd-run", "--user", "--scope", "--collect", "--quiet",
        f"--unit={unit}", *properties, "--", *guarded_payload,
    ]
    return wrapped, None, unit


def _self_cgroup_event_counts() -> dict[str, int] | None:
    """读取当前进程所在 cgroup 的硬资源拒绝计数（cgroup v2）。"""
    relative: str | None = None
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
                relative = parts[2]
                break
    except OSError:
        return None
    if relative is None:
        return None
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    counters: dict[str, int] = {}
    readable = False
    for filename, prefix in (("pids.events", "pids"),
                             ("memory.events", "memory"),
                             ("memory.swap.events", "memory.swap")):
        try:
            lines = (root / filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        readable = True
        for line in lines:
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                counters[f"{prefix}.{key}"] = int(value.strip())
    return counters if readable else None


def _resource_event_delta(
    before: dict[str, int] | None, after: dict[str, int] | None,
) -> dict[str, Any] | None:
    """把足以破坏本次执行完整性的 cgroup 增量归类为终态失败。"""
    deltas = _cgroup_event_deltas(before, after)
    if deltas is None:
        return None
    pids_denied = deltas.get("pids.max", 0)
    if pids_denied:
        return {
            "reason": "build_pids_limit_exhausted",
            "resource": "pids",
            "counter": pids_denied,
            "failure_class": "resource_exhaustion",
        }
    memory_oom = max(
        deltas.get("memory.oom", 0),
        deltas.get("memory.oom_kill", 0),
        deltas.get("memory.oom_group_kill", 0),
    )
    if memory_oom:
        return {
            "reason": "build_memory_limit_exhausted",
            "resource": "memory_bytes",
            "counter": memory_oom,
            "failure_class": "resource_exhaustion",
        }
    return None


def _cgroup_event_deltas(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
) -> dict[str, int] | None:
    """返回两次 cgroup 采样间新增的事件，不把累计值误作当前压力。"""
    if before is None or after is None:
        return None
    deltas: dict[str, int] = {}
    for key, current in after.items():
        if not isinstance(current, int):
            continue
        delta = max(0, current - int(before.get(key, 0)))
        if delta:
            deltas[key] = delta
    return deltas


def classify_resource_health(
    limits: BuildLimits,
    *,
    usage: dict[str, Any],
    disk_free_bytes: int | None,
    host_snapshot: dict[str, Any] | None,
    probe_failures: dict[str, int] | None = None,
    memory_growth_per_second: float = 0.0,
) -> dict[str, Any]:
    """仅凭机械资源证据分类健康度，不猜应用语义或进度。

    80%/95% 是加密采样的压力等级，不是终止线。只有 PID 硬拒绝、
    内存 OOM、宿主紧急余量或磁盘紧急余量等确定事实才请求整树终止。
    """
    severity = 0
    reasons: list[str] = []
    warnings: set[str] = set()
    hard_stop: dict[str, Any] | None = None

    def mark(reason: str, level: int, warning: str | None = None) -> None:
        nonlocal severity
        severity = max(severity, level)
        if reason not in reasons:
            reasons.append(reason)
        if warning:
            warnings.add(warning)

    events = usage.get("CgroupEvents")
    if isinstance(events, dict):
        hard_stop = _resource_event_delta({}, events)
    event_deltas = usage.get("CgroupEventDeltas")
    if isinstance(event_deltas, dict):
        swap_denied = max(
            int(event_deltas.get("memory.swap.max", 0)),
            int(event_deltas.get("memory.swap.fail", 0)),
        )
        if swap_denied > 0:
            # Swap 拒绝只证明本次换出/分配失败；只要还能回收其它内存，
            # payload 仍可能正确推进。它是强压力证据，不是任务终态。
            mark("swap_allocation_denied", 2, "swap_bytes")
    if hard_stop is None and disk_free_bytes is not None:
        if disk_free_bytes <= limits.disk_stop_free_bytes:
            hard_stop = {
                "reason": "build_disk_reserve_exhausted",
                "resource": "disk_free_bytes",
                "current": disk_free_bytes,
                "maximum": limits.disk_stop_free_bytes,
                "failure_class": "resource_exhaustion",
            }
        elif disk_free_bytes <= limits.disk_warning_free_bytes:
            mark("disk_space_low", 1, "disk_free_bytes")

    if host_snapshot is not None:
        available = host_snapshot.get("available_bytes")
        if isinstance(available, int):
            if (
                hard_stop is None
                and available <= limits.host_memory_stop_free_bytes
            ):
                hard_stop = {
                    "reason": "build_host_memory_reserve_exhausted",
                    "resource": "host_memory_available_bytes",
                    "current": available,
                    "maximum": limits.host_memory_stop_free_bytes,
                    "failure_class": "host_resource_exhaustion",
                }
            elif available <= limits.host_memory_warning_free_bytes:
                mark(
                    "host_memory_low", 1,
                    "host_memory_available_bytes",
                )
        swap_total = host_snapshot.get("swap_total_bytes")
        swap_free = host_snapshot.get("swap_free_bytes")
        if (
            isinstance(swap_total, int) and swap_total > 0
            and isinstance(swap_free, int)
        ):
            free_ratio = swap_free / swap_total
            if free_ratio <= 0.05:
                mark("host_swap_critical", 2, "host_swap_free_bytes")
            elif free_ratio <= 0.10:
                mark("host_swap_low", 1, "host_swap_free_bytes")

    def observe_ratio(
        name: str,
        current: Any,
        maximum: int,
    ) -> None:
        if not isinstance(current, int) or maximum <= 0:
            return
        ratio = current / maximum
        if ratio >= limits.stop_ratio:
            mark(f"{name}_critical", 2, name)
        elif ratio >= limits.warning_ratio:
            mark(f"{name}_pressure", 1, name)

    observe_ratio(
        "memory_bytes", usage.get("MemoryCurrent"),
        limits.memory_max_bytes,
    )
    observe_ratio("pids", usage.get("TasksCurrent"), limits.tasks_max)
    if limits.memory_swap_max_bytes > 0:
        observe_ratio(
            "swap_bytes", usage.get("MemorySwapCurrent"),
            limits.memory_swap_max_bytes,
        )

    current_memory = usage.get("MemoryCurrent")
    if (
        isinstance(current_memory, int)
        and limits.memory_max_bytes > 0
        and current_memory / limits.memory_max_bytes >= limits.warning_ratio
        and memory_growth_per_second
        >= limits.memory_max_bytes * 0.10
    ):
        mark("memory_rapid_growth", 2, "memory_growth_per_second")

    def observe_psi(prefix: str, pressure: Any) -> None:
        if not isinstance(pressure, dict):
            return
        some = pressure.get("some_avg10")
        full = pressure.get("full_avg10")
        if (
            isinstance(some, int | float) and some >= 50.0
        ) or (
            isinstance(full, int | float) and full >= 10.0
        ):
            mark(f"{prefix}_psi_critical", 2, f"{prefix}_memory_psi")
        elif (
            isinstance(some, int | float) and some >= 10.0
        ) or (
            isinstance(full, int | float) and full >= 1.0
        ):
            mark(f"{prefix}_psi_pressure", 1, f"{prefix}_memory_psi")

    observe_psi("cgroup", usage.get("MemoryPSI"))
    if host_snapshot is not None:
        observe_psi("host", host_snapshot.get("memory_psi"))

    degraded = [
        name for name, count in (probe_failures or {}).items()
        if int(count) >= 3
    ]
    for name in sorted(degraded):
        mark(
            f"{name}_telemetry_unavailable",
            3 if severity == 0 else severity,
            f"{name}_telemetry_unavailable",
        )

    if hard_stop is not None:
        return {
            "resource_health": "exhausted",
            "decision": "emergency_stop",
            "decision_reasons": [str(hard_stop.get("reason"))],
            "active_warnings": sorted(
                warnings | {str(hard_stop.get("resource") or "resource")}
            ),
            "hard_stop": hard_stop,
        }
    health = {
        0: "healthy",
        1: "pressure",
        2: "critical",
        3: "unknown",
    }[severity]
    return {
        "resource_health": health,
        "decision": (
            "continue"
            if health == "healthy"
            else "continue_with_fast_sampling"
        ),
        "decision_reasons": reasons,
        "active_warnings": sorted(warnings),
        "hard_stop": None,
    }


def parse_build_resource_event(text: str) -> dict[str, Any] | None:
    """解析本模块 payload wrapper 发出的稳定终态事件行。"""
    match = _RESOURCE_EVENT_RE.search(text or "")
    if match is None:
        return None
    return {
        "reason": match.group("reason"),
        "resource": match.group("resource"),
        "counter": int(match.group("counter")),
        "failure_class": "resource_exhaustion",
    }


def _payload_with_cgroup_events(payload: list[str]) -> int:
    """在受限 cgroup 内运行 payload，并保留破坏执行完整性的终态事实。"""
    if payload and payload[0] == "--":
        payload = payload[1:]
    if not payload:
        return 2
    before = _self_cgroup_event_counts()
    try:
        proc = subprocess.Popen(payload, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"{_RESOURCE_EVENT_PREFIX} reason=build_spawn_failed "
              f"resource=process counter=1 detail={type(exc).__name__}",
              file=sys.stderr, flush=True)
        return 126
    returncode = proc.wait()
    event = _resource_event_delta(before, _self_cgroup_event_counts())
    if event is not None:
        print(f"{_RESOURCE_EVENT_PREFIX} reason={event['reason']} "
              f"resource={event['resource']} counter={event['counter']}",
              file=sys.stderr, flush=True)
        return 125
    return int(returncode)


def kill_guarded_tree(unit: str | None, fallback_kill: Any) -> None:
    """优先终止整个 cgroup；控制面异常时再终止启动进程组。"""
    if unit:
        try:
            subprocess.run(
                ["systemctl", "--user", "kill", "--kill-whom=all",
                 "--signal=SIGKILL", unit],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    fallback_kill()


def local_build_supervisor_argv(
    payload_argv: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    status_path: Path,
    limits: BuildLimits,
) -> list[str]:
    """生成框架拥有的本地构建监督器 argv；模型不能直接控制监督参数。"""
    return [
        sys.executable, str(Path(__file__).resolve()),
        "--stdout", str(stdout_path), "--stderr", str(stderr_path),
        "--status", str(status_path),
        "--limits-json", json.dumps(limits.public(), separators=(",", ":")),
        "supervise-local-build", "--", *payload_argv,
    ]


def _cgroup_files_snapshot(
    control_group: str,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    """读取指定受管 scope 的 cgroup v2 实测值；路径必须留在 cgroup 根内。"""
    root = cgroup_root.resolve()
    candidate = (root / control_group.lstrip("/")).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        return {}
    result: dict[str, Any] = {}
    try:
        raw_swap = (candidate / "memory.swap.current").read_text(
            encoding="utf-8").strip()
        if raw_swap.isdigit():
            result["MemorySwapCurrent"] = int(raw_swap)
    except OSError:
        pass
    counters: dict[str, int] = {}
    for filename, prefix in (
        ("pids.events", "pids"),
        ("memory.events", "memory"),
        ("memory.swap.events", "memory.swap"),
    ):
        try:
            lines = (candidate / filename).read_text(
                encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                counters[f"{prefix}.{key}"] = int(value.strip())
    if counters:
        result["CgroupEvents"] = counters
    pressure = _pressure_snapshot(candidate / "memory.pressure")
    if pressure is not None:
        result["MemoryPSI"] = pressure
    return result


def cgroup_unit_snapshot(unit: str) -> dict[str, Any] | None:
    """一次读取 unit 的资源、生命周期与实时 cgroup 证据。"""
    try:
        probe = subprocess.run(
            [
                "systemctl", "--user", "show", unit,
                "-p", "MemoryCurrent", "-p", "TasksCurrent",
                "-p", "LoadState", "-p", "ActiveState",
                "-p", "ControlGroup",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=2,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    result: dict[str, Any] = {}
    for line in probe.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in {"MemoryCurrent", "TasksCurrent"}:
            result[key] = int(value) if value.isdigit() else None
        elif key in {"LoadState", "ActiveState", "ControlGroup"}:
            result[key] = value
    if not result:
        return None
    control_group = str(result.get("ControlGroup") or "")
    if control_group:
        result.update(_cgroup_files_snapshot(control_group))
    return result


def _unit_usage(unit: str) -> dict[str, Any]:
    snapshot = cgroup_unit_snapshot(unit)
    if snapshot is None:
        return {}
    return {
        key: value
        for key in (
            "MemoryCurrent", "TasksCurrent", "MemorySwapCurrent",
            "CgroupEvents", "MemoryPSI", "ControlGroup",
        )
        if (value := snapshot.get(key)) is not None
    }


def wait_for_cgroup_quiescence(
    unit: str,
    *,
    grace_s: float,
    poll_interval_s: float = 0.1,
) -> dict[str, Any]:
    """等待 scope 清空；busy/unknown 都不能成为成功证据。"""
    deadline = time.monotonic() + max(0.0, float(grace_s))
    while True:
        snapshot = cgroup_unit_snapshot(unit)
        if snapshot is not None:
            load_state = str(snapshot.get("LoadState") or "")
            active_state = str(snapshot.get("ActiveState") or "")
            tasks = snapshot.get("TasksCurrent")
            if (
                load_state == "not-found"
                or tasks == 0
                or (active_state == "inactive" and tasks is None)
            ):
                return {
                    "status": "quiet",
                    "tasks_current": 0,
                    "snapshot": snapshot,
                }
        now = time.monotonic()
        if now >= deadline:
            if snapshot is None:
                return {
                    "status": "unknown",
                    "tasks_current": None,
                    "snapshot": None,
                }
            tasks = snapshot.get("TasksCurrent")
            return {
                "status": (
                    "busy"
                    if isinstance(tasks, int) and tasks > 0
                    else "unknown"
                ),
                "tasks_current": tasks if isinstance(tasks, int) else None,
                "snapshot": snapshot,
            }
        time.sleep(
            min(max(0.01, poll_interval_s), max(0.01, deadline - now)))


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def supervise_local_build(
    payload_argv: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    status_path: Path,
    limits: BuildLimits,
) -> int:
    """监督已交接的本地构建，后端无需保持 asyncio 任务存活。"""
    try:
        for parent in {
            stdout_path.parent, stderr_path.parent, status_path.parent,
        }:
            parent.mkdir(parents=True, exist_ok=True)
        # 可追溯日志是安全契约；先验证可创建，再启动任何 payload。
        with stdout_path.open("wb"), stderr_path.open("wb"):
            pass
    except OSError as exc:
        _write_status(status_path, {
            "status": "error", "reason": "build_log_unavailable",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return 126

    admission_block = host_memory_admission_block(limits)
    if admission_block is not None:
        _write_status(status_path, admission_block)
        if admission_block.get("reason") == "build_host_memory_probe_unavailable":
            return 126
        return 125

    initial_disk_free = filesystem_free_bytes(stdout_path.parent)
    if initial_disk_free is None:
        _write_status(status_path, {
            "status": "error", "reason": "build_disk_probe_unavailable",
            "detail": f"无法读取日志文件系统剩余空间：{stdout_path.parent}",
        })
        return 126
    if initial_disk_free <= limits.disk_stop_free_bytes:
        _write_status(status_path, {
            "status": "error", "reason": "build_disk_reserve_exhausted",
            "resource": "disk_free_bytes", "current": initial_disk_free,
            "maximum": limits.disk_stop_free_bytes,
            "resource_guard": limits.public(),
        })
        return 125

    wrapped, reason, unit = wrap_with_cgroup(payload_argv, limits)
    if wrapped is None or unit is None:
        _write_status(status_path, {
            "status": "error", "reason": "build_resource_guard_unavailable",
            "detail": reason,
        })
        return 126

    try:
        from shared.lib import process_control

        proc = subprocess.Popen(
            wrapped, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **process_control.group_spawn_kwargs())
    except OSError as exc:
        _write_status(status_path, {
            "status": "error", "reason": "build_spawn_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return 126

    def fallback_kill() -> None:
        _kill_process_group(proc.pid)

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    started = time.monotonic()
    next_probe = started
    written = 0
    warning_history: set[str] = set()
    active_warnings: set[str] = set()
    resource_health: dict[str, Any] = {
        "resource_health": "healthy",
        "decision": "continue",
        "decision_reasons": [],
        "active_warnings": [],
        "hard_stop": None,
    }
    probe_failures = {
        "cgroup": 0, "disk": 0, "host_memory": 0,
    }
    stop: dict[str, Any] = {}
    resource_event_tail = bytearray()
    last_evidence: dict[str, Any] = {
        "elapsed_seconds": 0,
        "pids": 0,
        "memory_bytes": 0,
        "memory_growth_per_second": 0.0,
        "swap_bytes": 0,
        "host_swap_free_bytes": None,
        "host_swap_total_bytes": None,
        "cgroup_memory_psi": None,
        "host_memory_psi": None,
        "cgroup_events": {},
        "cgroup_event_deltas": {},
        "log_bytes": 0,
        "average_log_bytes_per_second": 0,
        "pid_growth_per_second": 0.0,
        "disk_free_bytes": initial_disk_free,
        "host_memory_available_bytes": None,
    }
    previous_probe_at = started
    previous_pids = 0
    previous_memory: int | None = None
    previous_cgroup_events: dict[str, int] = {}
    last_status_write = 0.0
    signal_number = 0
    quiescence_checked = False

    def _on_signal(signum: int, _frame: Any) -> None:
        nonlocal signal_number
        signal_number = signum

    previous_handlers = {
        signum: signal.signal(signum, _on_signal)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            files = {"stdout": stdout_file, "stderr": stderr_file}

            def emit(message: str) -> None:
                nonlocal written
                data = (message.rstrip() + "\n").encode("utf-8", errors="replace")
                try:
                    stderr_file.write(data)
                    stderr_file.flush()
                    written += len(data)
                except OSError as exc:
                    if not stop:
                        stop.update(
                            reason="build_log_write_failed",
                            resource="disk_free_bytes",
                            detail=f"{type(exc).__name__}: {exc}")

            def apply_resource_health(health: dict[str, Any]) -> bool:
                nonlocal active_warnings, resource_health, last_status_write
                current = set(health.get("active_warnings") or [])
                added = current - active_warnings
                recovered = active_warnings - current
                for name in sorted(added):
                    emit(
                        "HARNESS_BUILD_RESOURCE_WARNING "
                        f"resource={name} "
                        "action=continue_with_fast_sampling"
                    )
                for name in sorted(recovered):
                    emit(
                        "HARNESS_BUILD_RESOURCE_RECOVERED "
                        f"resource={name} action=normal_sampling"
                    )
                warning_history.update(added)
                changed = (
                    current != active_warnings
                    or health.get("resource_health")
                    != resource_health.get("resource_health")
                    or health.get("decision") != resource_health.get("decision")
                    or health.get("decision_reasons")
                    != resource_health.get("decision_reasons")
                )
                active_warnings = current
                resource_health = health
                hard_stop = health.get("hard_stop")
                if isinstance(hard_stop, dict) and hard_stop and not stop:
                    stop.update(hard_stop)
                now = time.monotonic()
                if changed or now - last_status_write >= 5.0:
                    required_action = None
                    if "disk_free_bytes" in active_warnings:
                        required_action = "analyze_build_behavior"
                    elif active_warnings & {
                        "host_memory_available_bytes",
                        "host_swap_free_bytes",
                        "host_memory_psi",
                    }:
                        required_action = "wait_or_reschedule_host_memory"
                    payload = {
                        "status": (
                            "running"
                            if not active_warnings
                            and health.get("resource_health") == "healthy"
                            else "running_warning"
                        ),
                        "resource_guard": limits.public(),
                        "warnings": sorted(warning_history),
                        "active_warnings": sorted(active_warnings),
                        "resource_health": health.get("resource_health"),
                        "decision": health.get("decision"),
                        "decision_reasons": health.get("decision_reasons") or [],
                        "behavior_evidence": dict(last_evidence),
                    }
                    if required_action is not None:
                        payload["required_action"] = required_action
                    _write_status(status_path, payload)
                    last_status_write = now
                return health.get("decision") != "continue"

            def observe_quiescence() -> None:
                evidence = wait_for_cgroup_quiescence(
                    unit,
                    grace_s=limits.cgroup_quiescence_grace_s,
                )
                last_evidence["cgroup_quiescence"] = evidence
                if evidence.get("status") == "quiet" or stop:
                    return
                tasks = evidence.get("tasks_current")
                if evidence.get("status") == "busy":
                    stop.update(
                        reason="build_cgroup_not_quiescent",
                        resource="pids",
                        current=tasks,
                    )
                    return
                stop.update(
                    reason="build_cgroup_quiescence_unavailable",
                    resource="cgroup_lifecycle",
                    detail="无法确认受管 cgroup 已清空，不能把顶层退出视为成功。",
                )

            apply_resource_health(classify_resource_health(
                limits, usage={}, disk_free_bytes=initial_disk_free,
                host_snapshot=None, probe_failures=probe_failures,
            ))

            while selector.get_map():
                for key, _ in selector.select(timeout=0.5):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    try:
                        files[key.data].write(chunk)
                        files[key.data].flush()
                        written += len(chunk)
                    except OSError as exc:
                        if not stop:
                            stop.update(
                                reason="build_log_write_failed",
                                resource="disk_free_bytes",
                                detail=f"{type(exc).__name__}: {exc}")
                    if key.data == "stderr":
                        resource_event_tail.extend(chunk)
                        if len(resource_event_tail) > 4096:
                            del resource_event_tail[:-4096]
                        event = parse_build_resource_event(
                            resource_event_tail.decode("utf-8", errors="replace"))
                        if event is not None and not stop:
                            stop.update(event)

                now = time.monotonic()
                if now >= next_probe:
                    usage = _unit_usage(unit)
                    disk_free = filesystem_free_bytes(stdout_path.parent)
                    host_probe_enabled = (
                        limits.host_memory_warning_free_bytes > 0
                        or limits.host_memory_stop_free_bytes > 0
                    )
                    host_snapshot = (
                        _host_memory_snapshot()
                        if host_probe_enabled
                        else None
                    )
                    cgroup_ok = any(
                        isinstance(usage.get(key), int)
                        for key in ("MemoryCurrent", "TasksCurrent")
                    )
                    probe_failures["cgroup"] = (
                        0 if cgroup_ok
                        else probe_failures["cgroup"] + 1
                    )
                    probe_failures["disk"] = (
                        0 if disk_free is not None
                        else probe_failures["disk"] + 1
                    )
                    probe_failures["host_memory"] = (
                        0
                        if not host_probe_enabled or host_snapshot is not None
                        else probe_failures["host_memory"] + 1
                    )
                    elapsed = max(0.001, now - started)
                    current_pids = usage.get("TasksCurrent")
                    current_memory = usage.get("MemoryCurrent")
                    current_cgroup_events = usage.get("CgroupEvents")
                    cgroup_event_deltas = (
                        _cgroup_event_deltas(
                            previous_cgroup_events, current_cgroup_events,
                        )
                        if isinstance(current_cgroup_events, dict)
                        else None
                    )
                    if cgroup_event_deltas is not None:
                        usage["CgroupEventDeltas"] = cgroup_event_deltas
                    probe_interval = max(0.001, now - previous_probe_at)
                    pid_growth = (
                        (current_pids - previous_pids) / probe_interval
                        if isinstance(current_pids, int)
                        else 0.0
                    )
                    memory_growth = (
                        (current_memory - previous_memory) / probe_interval
                        if isinstance(current_memory, int)
                        and isinstance(previous_memory, int)
                        else 0.0
                    )
                    last_evidence.update(
                        elapsed_seconds=round(elapsed, 3),
                        pids=(
                            current_pids
                            if isinstance(current_pids, int)
                            else last_evidence["pids"]
                        ),
                        memory_bytes=(
                            current_memory
                            if isinstance(current_memory, int)
                            else last_evidence["memory_bytes"]
                        ),
                        memory_growth_per_second=round(memory_growth, 3),
                        swap_bytes=usage.get("MemorySwapCurrent", 0),
                        log_bytes=written,
                        average_log_bytes_per_second=int(written / elapsed),
                        pid_growth_per_second=round(pid_growth, 3),
                        disk_free_bytes=disk_free,
                        cgroup_memory_psi=usage.get("MemoryPSI"),
                        cgroup_events=usage.get("CgroupEvents") or {},
                        cgroup_event_deltas=cgroup_event_deltas or {},
                    )
                    if host_snapshot is not None:
                        last_evidence.update(
                            host_memory_available_bytes=host_snapshot.get(
                                "available_bytes"),
                            host_swap_free_bytes=host_snapshot.get(
                                "swap_free_bytes"),
                            host_swap_total_bytes=host_snapshot.get(
                                "swap_total_bytes"),
                            host_memory_psi=host_snapshot.get("memory_psi"),
                        )
                    health = classify_resource_health(
                        limits,
                        usage=usage,
                        disk_free_bytes=disk_free,
                        host_snapshot=host_snapshot,
                        probe_failures=probe_failures,
                        memory_growth_per_second=memory_growth,
                    )
                    high_pressure = apply_resource_health(health)
                    previous_probe_at = now
                    if isinstance(current_pids, int):
                        previous_pids = current_pids
                    if isinstance(current_memory, int):
                        previous_memory = current_memory
                    if isinstance(current_cgroup_events, dict):
                        previous_cgroup_events = dict(current_cgroup_events)
                    # 压力/临界/遥测退化时提高到 4 Hz；恢复后自动回到 1 Hz。
                    next_probe = now + (0.25 if high_pressure else 1.0)
                if (
                    limits.timeout_s is not None
                    and now - started >= limits.timeout_s
                    and not stop
                ):
                    stop.update(
                        reason="build_time_limit_reached", resource="runtime_seconds",
                        current=int(now - started), maximum=limits.timeout_s)
                if signal_number and not stop:
                    stop.update(reason="build_cancelled", resource="signal",
                                current=signal_number, maximum=signal_number)
                if proc.poll() is not None and not quiescence_checked:
                    quiescence_checked = True
                    observe_quiescence()
                if stop:
                    emit("HARNESS_BUILD_RESOURCE_STOP " + " ".join(
                        f"{key}={value}" for key, value in stop.items()))
                    break
                if proc.poll() is not None and not selector.get_map():
                    break

            if stop:
                kill_guarded_tree(unit, fallback_kill)
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_guarded_tree(unit, fallback_kill)
                returncode = 125
            if not stop and not quiescence_checked:
                quiescence_checked = True
                observe_quiescence()
                if stop:
                    emit("HARNESS_BUILD_RESOURCE_STOP " + " ".join(
                        f"{key}={value}" for key, value in stop.items()))
                    kill_guarded_tree(unit, fallback_kill)
    finally:
        selector.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if (
        stop.get("failure_class") == "resource_exhaustion"
        and resource_health.get("resource_health") != "exhausted"
    ):
        resource_health = {
            "resource_health": "exhausted",
            "decision": "emergency_stop",
            "decision_reasons": [str(stop.get("reason"))],
            "active_warnings": sorted(
                active_warnings
                | {str(stop.get("resource") or "resource")}
            ),
            "hard_stop": dict(stop),
        }
        active_warnings = set(resource_health["active_warnings"])
        warning_history.update(active_warnings)
    status = {
        "status": "error" if stop or returncode else "success",
        "returncode": returncode,
        "resource_guard": limits.public(),
        "warnings": sorted(warning_history),
        "active_warnings": sorted(active_warnings),
        "resource_health": resource_health.get("resource_health"),
        "decision": resource_health.get("decision"),
        "decision_reasons": resource_health.get("decision_reasons") or [],
        "log_bytes": written,
        "behavior_evidence": last_evidence,
        **stop,
    }
    if "disk_free_bytes" in active_warnings:
        status["required_action"] = "analyze_build_behavior"
    elif active_warnings & {
        "host_memory_available_bytes",
        "host_swap_free_bytes",
        "host_memory_psi",
    }:
        status["required_action"] = "wait_or_reschedule_host_memory"
    _write_status(status_path, status)
    if stop.get("reason") == "build_time_limit_reached":
        return 124
    if stop:
        return 125
    return int(returncode or 0)


def _kill_process_group(pgid: int) -> None:
    # 整组杀的机制在 shared.lib.process_control（POSIX 进程组 / Windows Job）。
    from shared.lib import process_control

    process_control.Group.of(pgid).kill()


def _main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "payload-with-cgroup-events":
        return _payload_with_cgroup_events(argv[1:])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode")
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--limits-json", required=True)
    parser.add_argument("payload", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode != "supervise-local-build" or not args.payload:
        return 2
    payload = args.payload[1:] if args.payload[0] == "--" else args.payload
    try:
        limits = BuildLimits(**json.loads(args.limits_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 2
    return supervise_local_build(
        payload, stdout_path=Path(args.stdout), stderr_path=Path(args.stderr),
        status_path=Path(args.status), limits=limits)


if __name__ == "__main__":
    raise SystemExit(_main())
