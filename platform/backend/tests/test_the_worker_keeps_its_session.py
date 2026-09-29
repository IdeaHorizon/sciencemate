"""worker / 启动探针的子环境必须带着"能不能起资源墙"那两个变量（#849）。

这条判据住在**平台侧**的测试目录，不在根 tests/ —— 根 venv 里没有 pydantic_settings，
放那边它会 skip，而 skip 掉的防线和不存在没有区别
（[[feedback_absent_check_looks_like_passed_check]]）。

病例与全部背景见 tests/test_the_child_keeps_the_session_that_makes_the_walls.py。
一句话：UI 路径下 188 个进程不受任何 cgroup 约束地跑完（CLI 下 1.4 秒被拦），根因是
worker 的 environ 里 XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS 数量 = 0。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def _session(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")


def test_the_worker_environment_carries_the_user_session(_session):
    """真走 worker 与启动探针共用的那条构造函数，不是只验名单常量。

    [[feedback_grep_for_a_name_is_not_wiring]]：名字出现在名单里不等于接上了。
    """
    from app.services.harness_runtime import harness_subprocess_env

    env = harness_subprocess_env(ROOT, passthrough=("HARNESS_EXECUTOR",))
    assert env.get("XDG_RUNTIME_DIR") == "/run/user/1000", (
        "worker / 启动探针的子环境丢掉了 XDG_RUNTIME_DIR —— systemd-run --user 连不上 "
        "user session，mem_cap/pids_cap 在 UI 路径下整片消失（#849）"
    )
    assert env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/1000/bus"


def test_the_harness_session_spawn_uses_the_same_answer(_session):
    """worker 那条链（harness_sessions）与探针那条链（main）用的是同一份名单。

    #849 的直接原因是同一份系统变量白名单被抄了两遍、两遍漏了同样的东西。
    """
    from app.services.harness_runtime import harness_subprocess_env
    from shared.lib.platform_env import system_env_passthrough

    env = harness_subprocess_env(ROOT, passthrough=())
    for name, value in system_env_passthrough().items():
        assert env.get(name) == value, f"{name} 没穿过 harness_subprocess_env"
