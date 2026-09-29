"""serve 模式下 autonomous 必须就地消化非高风险暂停，高风险照样抛回给人。

E2E v17 实测：子进程环境里 HARNESS_AUTO_APPROVE=1 明明设上了，决策包照样
把 run 变成 waiting_human —— 因为 serve 模式的 turn 一遇到 pause 就直接返回
`status=paused`，**从不经过 pause_driver**，AUTO_APPROVE_ENABLED 读都没被读过。

平台此前根本没有让 auto-approve 生效的路径（UI 的 "Autonomous" 只影响
自动 publish 版本）。这条测试锁住接通之后的两端行为。
"""

import asyncio
import types

import pytest

import platform_runtime
from core import pause_driver


def _Pause(pause_type):
    """用**真的** PauseEvent。

    这里原本是个手工桩（只有 metadata / options / question / context /
    asking_*）。桩不会跟着真类演化 —— `PauseEvent` 后来长出 `payload` 与
    `recommended_index()`，桩上没有，于是这条测试测的是一个现实中不存在的形状。
    """
    from core.pause import PauseEvent

    return PauseEvent(
        question="q",
        context="",
        options=["PROCEED", "REVISE"],
        asking_node_type="hypothesis",
        asking_run_id="r1",
        metadata={"type": pause_type},
        payload={"recommended_option_index": 0},
    )


class _Pending:
    def __init__(self, pause_type):
        self.pause_event = _Pause(pause_type)


def _session():
    return platform_runtime.PlatformSession.__new__(platform_runtime.PlatformSession)


@pytest.mark.asyncio
async def test_returns_none_when_auto_approve_is_off(monkeypatch):
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", False)
    monkeypatch.setattr("core.pause.get_deepest_paused", lambda: _Pending("decision_package"))
    assert await _session()._auto_resume_pauses() is None


@pytest.mark.asyncio
async def test_high_risk_pause_is_never_consumed(monkeypatch):
    """批量写入 / 外部作业提交必须抛回给人，autonomous 也不例外。"""
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    for pause_type in ("highrisk_confirm", "permission"):
        monkeypatch.setattr("core.pause.get_deepest_paused", lambda t=pause_type: _Pending(t))
        assert await _session()._auto_resume_pauses() is None, pause_type


@pytest.mark.asyncio
async def test_decision_package_is_consumed_and_run_continues(monkeypatch):
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    calls = {"n": 0}

    def _pending():
        # 第一次有暂停，被消化后就没有了
        return _Pending("decision_package") if calls["n"] == 0 else None

    monkeypatch.setattr("core.pause.get_deepest_paused", _pending)

    async def _drive(*, ask_fn, finalize_fn):
        calls["n"] += 1
        await ask_fn(_Pause("decision_package"))
        return "done"

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _drive)

    result = await _session()._auto_resume_pauses()
    assert result == ("completed", "done")


@pytest.mark.asyncio
async def test_remaining_pause_keeps_the_run_paused(monkeypatch):
    """链条消化完还剩暂停（例如里面套了个高风险）→ 保持 paused，别谎报完成。"""
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    monkeypatch.setattr("core.pause.get_deepest_paused", lambda: _Pending("decision_package"))

    async def _drive(*, ask_fn, finalize_fn):
        return "partial"

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _drive)
    assert await _session()._auto_resume_pauses() is None
