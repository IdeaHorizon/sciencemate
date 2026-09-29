"""`shared.lib.hostinfo`：主机事实的契约，两平台同一份。

内存 / 磁盘的**具体数字**不能写死（机器各异），但可以断言它们是**观测出来的真值**：
总量为正、可用量在 [0, 总量] 内、和 psutil 直接读的一致。
"""
from __future__ import annotations

import os

import psutil
import pytest

from shared.lib import hostinfo


def test_memory_is_observed_not_guessed():
    mem = hostinfo.memory()
    assert mem.known is True
    assert mem.total_bytes and mem.total_bytes > 0
    assert 0 <= mem.available_bytes <= mem.total_bytes
    # 和 psutil 直接读的对齐（同一次观测会有微小漂移，给 20% 容差只为挡住"读错字段"）
    vm = psutil.virtual_memory()
    assert abs(mem.total_bytes - vm.total) < vm.total * 0.2


def test_logical_cpus_matches_stdlib():
    assert hostinfo.logical_cpus() == os.cpu_count()
    assert hostinfo.logical_cpus() >= 1


def test_disk_is_observed_for_an_existing_path(tmp_path):
    d = hostinfo.disk(tmp_path)
    assert d.known is True
    assert d.total_bytes and d.total_bytes > 0
    assert 0 <= d.free_bytes <= d.total_bytes


def test_disk_on_a_missing_path_is_unknown_not_a_crash():
    d = hostinfo.disk("/no/such/path/anywhere/hostinfo-test")
    assert d.known is False
    assert d.total_bytes is None and d.free_bytes is None


def test_os_description_is_nonempty():
    desc = hostinfo.os_description()
    assert isinstance(desc, str) and desc and desc != "unknown"


def test_memory_unknown_when_psutil_cannot_read(monkeypatch):
    """psutil 抛了（受限环境）→ 如实说不知道，不崩、不伪造。"""
    def boom():
        raise RuntimeError("no /proc here")

    monkeypatch.setattr(hostinfo.psutil, "virtual_memory", boom)
    mem = hostinfo.memory()
    assert mem.known is False
    assert mem.total_bytes is None and mem.available_bytes is None
