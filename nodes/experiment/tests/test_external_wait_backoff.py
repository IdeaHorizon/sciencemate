"""submit_job 前台等待改为短退避探测（收敛任务书 K13、缺陷 #13）。

原先首探之后第二次探测要等 min(poll_interval_s, max_wait_s)，默认 180 秒：几秒就结束的短作业
也要干等三分钟才被看到。调用方没指定 poll_interval_s 时，前几次按 0.5、1、2、5、10、30、60 秒
探测（每次不超过声明的间隔），之后回到声明的间隔；显式指定 poll_interval_s 时照旧。
"""
from __future__ import annotations

import asyncio
import time

from nodes.experiment.tools import resource_manager as manager

_RUNNING = {"status": "success", "workflow_status": "awaiting_external_job",
            "health_state": "healthy", "decision": "wait"}
_TERMINAL = {"status": "success", "workflow_status": "awaiting_analysis",
             "scheduler_phase": "terminal"}


class _Clock:
    """只替换本模块看到的 time.monotonic；其余属性照旧转给真实 time。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class _WaitState:
    def __init__(self) -> None:
        self.hook_state: dict = {}
        self.kill_event = asyncio.Event()


def _probe_offsets(monkeypatch, *, running_probes: int, poll_interval_s=None,
                   configured: int = 180, max_wait_s: int = 300):
    clock = _Clock()
    offsets: list[float] = []

    async def probe(_state, _scheduler, _job_id, _namespace):
        offsets.append(round(clock.now - 1000.0, 3))
        return dict(_RUNNING if len(offsets) <= running_probes else _TERMINAL)

    async def sleep(_state, seconds):
        clock.now += seconds

    monkeypatch.setattr(manager, "time", clock)
    monkeypatch.setattr(manager, "_probe_external_job_health_cancellable", probe)
    monkeypatch.setattr(manager, "_sleep_with_external_wait_cancellation", sleep)
    monkeypatch.setattr(manager, "_external_job_record", lambda *_a, **_k: {
        "health_contract": {"poll_interval_s": configured}})
    waited = asyncio.run(manager._wait_for_external_job(
        _WaitState(), "slurm", "42", max_wait_s=max_wait_s, poll_interval_s=poll_interval_s))
    return waited, offsets


def test_a_job_that_ends_in_seconds_is_seen_in_seconds(monkeypatch):
    waited, offsets = _probe_offsets(monkeypatch, running_probes=3)

    assert waited["wait_outcome"] == "scheduler_terminal", waited
    assert offsets == [0.0, 0.5, 1.5, 3.5], offsets


def test_after_the_backoff_the_declared_interval_applies_and_caps_every_gap(monkeypatch):
    _waited, offsets = _probe_offsets(monkeypatch, running_probes=9, configured=30)

    gaps = [round(b - a, 3) for a, b in zip(offsets, offsets[1:])]
    assert gaps == [0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0, 30.0], gaps


def test_an_explicit_poll_interval_keeps_the_fixed_schedule(monkeypatch):
    _waited, offsets = _probe_offsets(monkeypatch, running_probes=2, poll_interval_s=45)

    assert offsets == [0.0, 45.0, 90.0], offsets
