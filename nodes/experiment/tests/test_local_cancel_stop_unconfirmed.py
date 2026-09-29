"""本地作业取消：进程没被停下时不记 cancelled（合入 origin/main d852a6f5 后的小修）。

main 起 core.sandbox.stop_container 在进程组没有按期退出时返回 False；_cancel_sync 本地分支原先
不看返回值、一律 ok=True，取消事务据此写 lifecycle=cancelled——进程其实还在跑。现在返回 False
时按 outcome_unknown 处理，走取消结果未知的对账出口。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import resource_manager as manager
from test_job_end_honest_exits import (
    _RUNTIME_ID_A, _guard_reads_nothing, _honest_exit_offered, _local_payload,
)


def _running(*_args, **_kwargs):
    return {"ok": True, "returncode": 0, "stdout": "RUNNING", "stderr": "",
            "sandbox_state": {"exists": True, "running": True, "id": _RUNTIME_ID_A}}


def test_a_stop_that_leaves_the_process_running_is_an_unknown_outcome(monkeypatch):
    monkeypatch.setattr(manager, "_local_container_status", _running)
    monkeypatch.setattr("core.sandbox.stop_container", lambda *_a, **_k: False)

    result = manager._cancel_sync(
        "local", "hf-job-0123456789abcdef", None, container_runtime_id=_RUNTIME_ID_A,
        refuse_if_ended=True, absent_is_unknown=True)

    assert result["ok"] is False and result["outcome_unknown"] is True, result


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "still_running"])
def test_only_a_confirmed_stop_is_recorded_as_cancelled(tmp_path, monkeypatch, stopped):
    state = State.new("experiment", tmp_path)
    payload = _local_payload(state)
    stops: list = []
    monkeypatch.setattr(manager, "_observed_job_end", _guard_reads_nothing)
    monkeypatch.setattr(manager, "_local_container_status", _running)
    monkeypatch.setattr("core.sandbox.stop_container",
                        lambda *args, **_k: stops.append(args) or stopped)
    monkeypatch.setattr(
        "nodes.experiment.tools.execution_route.record_external_route_finalization",
        lambda *_a, **_k: {"status": "success", "attempt_id": "a"})
    monkeypatch.setattr("shared.lib.dangerous_commands.bypass_enabled", lambda: True)

    result = asyncio.run(manager._cancel_job(state, "local", payload["job_id"], reason="stop"))

    assert len(stops) == 1
    lifecycle = manager.lifecycle_for_submission(state, payload)["status"]
    if stopped:
        assert lifecycle == "cancelled", result
    else:
        assert result["status"] == "cancellation_outcome_unknown", result
        assert lifecycle != "cancelled"
        assert _honest_exit_offered(result), result
