"""主机事实（CPU / 内存 / 磁盘 / OS）—— 一处回答，跨平台。

散在几处的探针各读各的，而且各有一种非 Linux 的静默退化：

* ``compute_inventory`` 用 ``os.sysconf`` 读内存 —— **Windows 没有 sysconf**，于是 catch
  住返回 ``unknown``：明明读得到，账面却说不知道。
* ``resource_manager._local_resources`` 读 ``/proc/meminfo`` —— 非 Linux 直接静默成 None。

收成一处：内存走 ``psutil.virtual_memory``（Linux 读 /proc、macOS sysctl、Windows
``GlobalMemoryStatusEx``——psutil 内部按平台分），CPU 用 ``os.cpu_count``（本就跨平台），
磁盘用 ``shutil.disk_usage``（本就跨平台）。都是**机械观测**：读不到就如实说不知道，不估。

注意：``build_resource_guard`` 里的 ``/proc/meminfo`` 问的是**另一个问题**——"这个 cgroup
分到多少内存"，是 Linux 沙箱内的度量，不是主机事实，不收在这里。
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass

import psutil

__all__ = ["Memory", "Disk", "logical_cpus", "memory", "disk", "os_description"]


@dataclass(frozen=True)
class Memory:
    """物理内存。读不到时两个字段都是 None（``known`` 为假）。"""

    total_bytes: int | None
    available_bytes: int | None

    @property
    def known(self) -> bool:
        return self.total_bytes is not None


@dataclass(frozen=True)
class Disk:
    total_bytes: int | None
    free_bytes: int | None

    @property
    def known(self) -> bool:
        return self.total_bytes is not None


def logical_cpus() -> int | None:
    """逻辑核数；数不出来（极少见）返回 None。"""
    return os.cpu_count()


def memory() -> Memory:
    try:
        vm = psutil.virtual_memory()
    except Exception:  # psutil 在某些受限环境里会抛 —— 读不到就说不知道
        return Memory(None, None)
    total = int(vm.total)
    available = int(vm.available)
    if total <= 0 or available < 0:
        return Memory(None, None)
    return Memory(total, available)


def disk(path: os.PathLike | str) -> Disk:
    try:
        usage = shutil.disk_usage(os.fspath(path))
    except OSError:
        return Disk(None, None)
    return Disk(int(usage.total), int(usage.free))


def os_description() -> str:
    """``platform.platform()``（含内核版本），退回 ``system()``，再退回 unknown。"""
    return platform.platform() or platform.system() or "unknown"
