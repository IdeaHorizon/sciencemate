"""Experiment's resource plans and compatibility metadata.

Execution, cgroup construction, limits, cancellation and process cleanup belong
only to Core isolation/spawn_and_wait. This module reads planning observations;
it cannot start, stop or supervise a command. Serialized BuildLimits fields stay
readable for existing plan artifacts; they do not assert live enforcement.
"""
from __future__ import annotations

import os
import re
import shutil
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
        # 实际执行准入由 Core 资源/隔离契约负责；此处只保存领域计划。
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


if __name__ == "__main__":
    raise SystemExit("This module exposes resource plans only; execute through Core managed tools.")
