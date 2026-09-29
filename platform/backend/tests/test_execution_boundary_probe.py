"""启动闸只问一件事：这台机器上有没有后端能守写边界（I1）。

原来的闸问的是「Docker 在不在」，个人机器没装 Docker 连只读功能都起不来
（RFC_EXECUTOR_TIERS §5 拆除清单第一条）。现在探针把实际守到的不变量带回
``app.state.execution_boundary``，``/health/ready`` 原样交出去。Docker 与它的容器
reaper 已随 PR C 删除。
"""
from __future__ import annotations

import json
import sys

import pytest

from app.config import settings


def _probe_returning(stdout: bytes, returncode: int = 0):
    class Probe:
        async def communicate(self):
            return stdout, b""

    Probe.returncode = returncode

    async def spawn(*argv, **kwargs):
        return Probe()

    return spawn


@pytest.fixture()
def _bridge(tmp_path, monkeypatch):
    root = tmp_path / "harness"
    root.mkdir()
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(root))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    from app import main as app_main

    app_main.app.state.execution_boundary = None
    yield app_main
    app_main.app.state.execution_boundary = None


@pytest.mark.asyncio
async def test_probe_records_the_enforcement_facts_instead_of_demanding_docker(_bridge, monkeypatch):
    record = {"backend": "darwin", "policy": "personal",
              "enforced": ["git_unwritable", "net_deny", "write_boundary", "walltime", "group_kill"],
              "missing_for_unattended": ["disk_cap", "mem_cap", "pids_cap"],
              "missing_for_attended": []}
    monkeypatch.setattr(_bridge.asyncio, "create_subprocess_exec",
                        _probe_returning(json.dumps(record).encode()))

    await _bridge._record_what_this_machine_enforces()

    assert _bridge.app.state.execution_boundary == record


@pytest.mark.asyncio
async def test_a_host_without_a_boundary_still_serves_and_says_so(_bridge, monkeypatch):
    """守不住写边界 = 一个答案，不是一次拒绝（2026-09-05 WP-02 / RFC X6）。

    从前这里 `pytest.raises(RuntimeError)`：一台没装 bwrap 的机器上，连「打开
    界面看看以前的项目」都做不到。现在它照常起，并且在 `/health/ready` 上如实
    说自己守不住什么；要不要真跑作业由派发那一刻决定。
    """
    monkeypatch.setattr(_bridge.asyncio, "create_subprocess_exec",
                        _probe_returning(b"the linux backend cannot enforce the write boundary "
                                         b"(I1) on this host: bwrap: not installed",
                                         returncode=1))

    await _bridge._record_what_this_machine_enforces()  # 不抛

    record = _bridge.app.state.execution_boundary
    assert record["backend"] is None
    assert "bwrap: not installed" in record["unavailable_reason"], (
        "如实显示的意思是把原因带出来，不是只说一句「没有」"
    )
    assert _bridge.app.state.sandbox_readiness.startswith("none: "), (
        _bridge.app.state.sandbox_readiness
    )


@pytest.mark.asyncio
async def test_a_probe_that_times_out_is_also_an_answer(_bridge, monkeypatch):
    class Hanging:
        returncode = None

        def communicate(self):
            # 故意不是 async：wait_for 被换掉了，这个协程不会有人 await
            return None

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def spawn(*argv, **kwargs):
        return Hanging()

    monkeypatch.setattr(_bridge.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(_bridge.asyncio, "wait_for",
                        lambda coro, timeout: (_ for _ in ()).throw(TimeoutError()))

    await _bridge._record_what_this_machine_enforces()

    assert "timed out" in _bridge.app.state.execution_boundary["unavailable_reason"]


def test_the_probe_script_asks_the_isolation_layer_not_docker(_bridge):
    """探针脚本本身：走 core.isolation，不再 import core.sandbox.availability。"""
    script = _bridge._EXECUTION_BOUNDARY_PROBE
    assert "isolation.select_backend" in script
    assert "enforcement_record" in script
    assert "core.sandbox" not in script
    assert "SystemExit" not in script, (
        "探针不许以「进程失败」的形式回答「这台机器没有沙箱」—— 那是把记账"
        "变回启动条件"
    )


def test_the_backend_no_longer_knows_a_container_reaper(_bridge):
    """Docker 删了：容器对账循环、image 后端判断都不该再存在。"""
    from app.services import harness_sessions

    assert not hasattr(harness_sessions, "reap_sandbox_instances")
    assert not hasattr(_bridge, "_image_backend_active")
    assert "docker" not in _bridge._EXECUTION_BOUNDARY_PROBE.lower()


@pytest.mark.asyncio
async def test_a_probe_that_cannot_even_start_is_also_an_answer(_bridge, monkeypatch):
    """解释器不在、harness 根不在 —— spawn 自己就 FileNotFoundError。

    2026-09-05 真机点验抓到的：前一版只把 `communicate()` 放进 try，spawn 在
    try 之外，于是「探针不再抛」只覆盖了一半的路，服务照样 `Application
    startup failed. Exiting.`。判据只在单测里走过的那半条路上成立。
    """
    async def spawn(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(_bridge.asyncio, "create_subprocess_exec", spawn)

    await _bridge._record_what_this_machine_enforces()

    record = _bridge.app.state.execution_boundary
    assert record["backend"] is None
    assert "could not run the execution boundary probe" in record["unavailable_reason"]


@pytest.mark.asyncio
async def test_a_named_backend_that_enforces_nothing_is_not_ok(_bridge, monkeypatch):
    """「有个后端」≠「守得住写边界」。

    Linux 后端在既没有 bwrap 也没有 Landlock 的机器上仍然叫 `linux`，
    `capabilities()` 却是空集。按后端名字判就会在这里说一句 ok —— 一个守不住
    任何东西的机器被显示成守得住。判据落在不变量上，不落在名字上。
    """
    record = {"backend": "linux", "policy": "personal", "enforced": [],
              "missing_for_unattended": ["write_boundary"], "missing_for_attended": ["write_boundary"]}
    monkeypatch.setattr(_bridge.asyncio, "create_subprocess_exec",
                        _probe_returning(json.dumps(record).encode()))

    await _bridge._record_what_this_machine_enforces()

    assert _bridge.app.state.sandbox_readiness.startswith("none: "), (
        _bridge.app.state.sandbox_readiness
    )
