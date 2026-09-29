"""safe_run_bash 返回起止时间与耗时（收敛任务书 K12、缺陷 #9）。

PID 不返回：真实执行经 shared.lib.cancellable_subprocess.spawn_and_wait 起在受管沙箱里，
它只交回 (status, returncode, stdout, stderr)，节点侧拿不到进程号（归 shared owner）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from nodes.experiment.tools import safe_bash as sb
from test_timeout_escalation import requires_sandbox, runattempt_state  # noqa: F401


class _Clock:
    """只替换本模块看到的 time.monotonic；其余属性照旧转给真实 time。"""

    def __init__(self, monotonic: float) -> None:
        self.value = monotonic

    def monotonic(self) -> float:
        return self.value

    def __getattr__(self, name):
        return getattr(time, name)


def test_timing_reports_utc_wall_times_and_the_monotonic_elapsed(monkeypatch):
    monkeypatch.setattr(sb, "time", _Clock(502.25))

    timing = sb._execution_timing(1_789_000_000.0, 500.0)

    started = datetime.fromisoformat(timing["started_at"])
    finished = datetime.fromisoformat(timing["finished_at"])
    assert timing["elapsed_seconds"] == 2.25
    assert started == datetime.fromtimestamp(1_789_000_000.0, timezone.utc)
    assert started.utcoffset() == timedelta(0)
    assert (finished - started).total_seconds() == 2.25


def test_a_clock_that_goes_backwards_never_reports_a_negative_duration(monkeypatch):
    monkeypatch.setattr(sb, "time", _Clock(499.0))

    timing = sb._execution_timing(1_789_000_000.0, 500.0)

    assert timing["elapsed_seconds"] == 0.0
    assert timing["finished_at"] == timing["started_at"]


@requires_sandbox
def test_a_real_command_reports_when_it_ran_and_for_how_long(runattempt_state):  # noqa: F811
    state = runattempt_state
    run_root = sb.experiment_output_dir(state, "runtime", create=True).resolve()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}
    before = datetime.now(timezone.utc)

    result = asyncio.run(sb._exec_and_log(
        state, "sleep 0.3; printf ok", cwd=str(run_root), timeout=30, sandbox_profile="bash"))

    after = datetime.now(timezone.utc)
    assert result["status"] == "success", result
    started = datetime.fromisoformat(result["started_at"])
    finished = datetime.fromisoformat(result["finished_at"])
    assert result["elapsed_seconds"] >= 0.3
    assert before <= started <= finished <= after + timedelta(seconds=1)
