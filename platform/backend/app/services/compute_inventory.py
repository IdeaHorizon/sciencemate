"""Mechanical, non-estimated inventory for the local development host."""

import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.config import DataRootError, data_root, settings
from app.services.harness_imports import HarnessNotImportable, harness_module
from app.services.harness_sessions import harness_session_manager


def _cpu_inventory() -> dict:
    logical_cores = os.cpu_count()
    return {
        "status": "online" if logical_cores is not None else "unknown",
        "logical_cores": logical_cores,
    }


def _memory_inventory() -> dict:
    # 主机内存怎么读，一处回答（`shared.lib.hostinfo`：psutil，跨平台）。旧写法用
    # `os.sysconf` —— Windows 没有它，于是账面静默报 unknown。harness 找不到就退回
    # unknown，与旧的读不到时一致。
    try:
        mem = harness_module("shared.lib.hostinfo").memory()
        total_bytes, available_bytes = mem.total_bytes, mem.available_bytes
    except (HarnessNotImportable, ImportError):
        total_bytes = available_bytes = None
    if total_bytes is None or available_bytes is None:
        return {"status": "unknown", "total_bytes": None, "available_bytes": None}
    return {
        "status": "online",
        "total_bytes": total_bytes,
        "available_bytes": available_bytes,
    }


def _storage_inventory() -> dict:
    try:
        configured = data_root("state")
    except DataRootError:
        configured = None
    probe_root = configured if configured and configured.exists() else Path.cwd()
    try:
        usage = shutil.disk_usage(probe_root)
    except OSError:
        return {"status": "offline", "total_bytes": None, "free_bytes": None}
    return {
        "status": "online",
        "total_bytes": usage.total,
        "free_bytes": usage.free,
    }


def _gpu_inventory() -> dict:
    """Read one point-in-time NVIDIA inventory without estimating capacity."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"status": "unknown", "count": None, "devices": []}
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "offline", "count": None, "devices": []}

    devices = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        index, name, total_mib, available_mib, utilization = parts
        try:
            devices.append(
                {
                    "id": f"gpu-{int(index)}",
                    "name": name,
                    "memory_total_bytes": int(total_mib) * 1024 * 1024,
                    "memory_available_bytes": int(available_mib) * 1024 * 1024,
                    "utilization_percent": int(utilization),
                }
            )
        except ValueError:
            continue
    return {
        "status": "online" if devices else "unknown",
        "count": len(devices) if devices else None,
        "devices": devices,
    }


def local_compute_inventory() -> dict:
    """Return only values observed locally; unsupported dimensions stay unknown."""
    cpu = _cpu_inventory()
    memory = _memory_inventory()
    storage = _storage_inventory()
    gpu = _gpu_inventory()
    checks = [
        {
            "name": "app_server_process",
            "status": "online",
            "detail": "This response positively observed the local App Server process.",
        },
        {
            "name": "cpu_inventory",
            "status": cpu["status"],
            "detail": (
                "Logical CPU capacity was observed."
                if cpu["status"] == "online"
                else "Logical CPU capacity could not be observed."
            ),
        },
        {
            "name": "memory_inventory",
            "status": memory["status"],
            "detail": (
                "Memory capacity was observed."
                if memory["status"] == "online"
                else "Memory probing is unavailable on this host."
            ),
        },
        {
            "name": "storage_inventory",
            "status": storage["status"],
            "detail": (
                "Filesystem capacity was observed."
                if storage["status"] == "online"
                else "The local filesystem capacity probe failed."
            ),
        },
        {
            "name": "gpu_inventory",
            "status": gpu["status"],
            "detail": (
                "NVIDIA device and memory availability were observed with nvidia-smi."
                if gpu["status"] == "online"
                else "No working NVIDIA GPU probe is available; no GPU claim is made."
            ),
        },
    ]
    capacity_status = (
        "online"
        if all(item["status"] == "online" for item in (cpu, memory, storage))
        and gpu["status"] == "online"
        else "offline"
        if any(item["status"] == "offline" for item in (cpu, memory, storage))
        else "unknown"
    )
    return {
        "scope": "local_development",
        "observed_at": datetime.now(UTC),
        "health": {"status": "online", "checks": checks},
        "nodes": [
            {
                "id": "local-app-server",
                "name": "Local App Server",
                "kind": "local",
                "status": "online",
                "operating_system": platform.system() or "unknown",
                "architecture": platform.machine() or "unknown",
                "cpu": cpu,
                "memory": memory,
                "storage": storage,
                "gpu": gpu,
            }
        ],
        "schedulers": [
            {
                "id": "local-process",
                "kind": "in_process",
                "status": "online",
                "supports_queue": False,
                "queue_depth": None,
                "active_sessions": harness_session_manager.active_count,
            }
        ],
        "capacity": {
            "status": capacity_status,
            "cpu_logical_cores": cpu["logical_cores"],
            "memory_total_bytes": memory["total_bytes"],
            "memory_available_bytes": memory["available_bytes"],
            "storage_total_bytes": storage["total_bytes"],
            "storage_free_bytes": storage["free_bytes"],
            "gpu_count": gpu["count"],
            "gpu_memory_total_bytes": (
                sum(device["memory_total_bytes"] for device in gpu["devices"])
                if gpu["devices"]
                else None
            ),
            "gpu_memory_available_bytes": (
                sum(device["memory_available_bytes"] for device in gpu["devices"])
                if gpu["devices"]
                else None
            ),
        },
        "recent_jobs": {"supported": False, "items": []},
        "limitations": [
            "Project resource registrations are logical bindings, not allocation claims.",
            "The local process runner has no durable queue or recent-job inventory.",
            (
                "GPU availability is a point-in-time device observation, not a Project allocation."
                if gpu["status"] == "online"
                else "GPU capacity and monetary cost are not probed or estimated."
            ),
        ],
    }
