"""031 B-3：有就绪 Python 步骤时，只读诊断该走哪条路。

safe_execute_python 带 route_step_id 跑一次 print，会把真实计算步骤记成已完成
（路线变 complete）；safe_run_bash 的只读诊断不带 id 放行，且不消耗该步骤。
文案应把模型引向后者，而不是“诊断也必须传 route_step_id”。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from nodes.experiment.tests.test_route_shadow_wiring import _single_step_route, _state
from nodes.experiment.tools import execution_route, safe_bash


def _ready_python_step(tmp_path: Path, monkeypatch):
    async def fake_exec(*_args, **_kwargs):
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    state = _state(tmp_path)
    declared = asyncio.run(
        execution_route._declare_execution_route(
            state,
            route=_single_step_route(
                tool="safe_execute_python",
                program="python",
                role="run_root",
                effects=["workspace_write"],
            ),
        )
    )
    assert declared["status"] == "success", declared
    return state


def test_bash_read_only_diagnosis_leaves_ready_python_step_untouched(
    tmp_path,
    monkeypatch,
):
    state = _ready_python_step(tmp_path, monkeypatch)
    before = execution_route.build_route_snapshot(state)["route_state"]
    for cmd in ("ls", "cat /dev/null"):
        result = asyncio.run(safe_bash._safe_run_bash(state, cmd))
        assert result["status"] == "success", (cmd, result)
    assert execution_route.build_route_snapshot(state)["route_state"] == before != "complete"


def test_python_diagnosis_bound_to_ready_step_consumes_it(tmp_path, monkeypatch):
    """旧行为（另开包修）：这里只钉成事实，文案不得引导模型走这条路。"""
    state = _ready_python_step(tmp_path, monkeypatch)
    result = asyncio.run(
        safe_bash._safe_execute_python(
            state,
            "print('diag')",
            route_step_id="execute",
        )
    )
    assert result["status"] == "success", result
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"
