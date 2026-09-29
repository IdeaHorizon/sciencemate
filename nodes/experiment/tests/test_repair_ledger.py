"""软件修复台账的结构化持久化。

背景：编译/运行 blocker 此前只活在 `hook_state` 的内存台账里，run 结束即消失，
复盘只能靠 scratchpad 散文，于是"失败、延期与 dead-end 是否复查过"无从核对。
本文件锁住：字段完整、status 由构建证据推导、截断如实自报、
以及不替 agent 推断 dead_end。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from core.state import State
from nodes.experiment import hooks


def _state(tmp_path: Path) -> State:
    return State.new("experiment", tmp_path)


def _ctx(state: State, turn: int = 1, records: list | None = None):
    return SimpleNamespace(state=state, turn=turn, messages=[],
                           harness=None, tool_call_records=records or [])


def _bash(cmd: str, *, returncode: int = 0, log_path: str | None = None,
          status: str = "success") -> dict:
    return {
        "name": "safe_run_bash",
        "args": {"cmd": cmd},
        "result": {"cmd": cmd, "returncode": returncode,
                   "status": status, "log_path": log_path,
                   "stderr_tail": "undefined reference to `foo_'"},
    }


def _ledger(state: State) -> dict:
    records = state.list_artifacts("repair_ledger")
    assert len(records) == 1, f"应恰好 1 份台账，实际 {len(records)}"
    return json.loads(state.read_artifact(records[0]["id"])["content"])


def test_no_ledger_written_when_nothing_happened(tmp_path: Path) -> None:
    state = _state(tmp_path)
    hooks._execution_control_on_end(_ctx(state), None)
    assert state.list_artifacts("repair_ledger") == []


def test_ledger_records_required_fields(tmp_path: Path) -> None:
    state = _state(tmp_path)
    hooks._execution_control_on_turn_end(
        _ctx(state, turn=3, records=[
            _bash("make -j8 spio", returncode=2, status="error",
                  log_path="outputs/experiment/runtime/logs/0003_x.log")]))
    hooks._execution_control_on_end(_ctx(state, turn=3), None)

    payload = _ledger(state)
    issue = payload["issues"][0]
    for field in ("component", "latest_error", "attempted_fix", "status",
                  "log_path", "turn"):
        assert field in issue, field
    assert issue["status"] == "open"
    assert payload["truncated"] is False


def test_attempted_fix_records_only_observed_commands(tmp_path: Path) -> None:
    """attempted_fix 来自实际跑过的同组件命令，不是 agent 自述。"""
    state = _state(tmp_path)
    fail = _bash("make -j8 spio", returncode=2, status="error",
                 log_path="logs/spio.log")
    hooks._execution_control_on_turn_end(_ctx(state, turn=1, records=[fail]))
    retry = _bash("make -j8 spio", returncode=0, log_path="logs/spio.log")
    hooks._execution_control_on_turn_end(_ctx(state, turn=2, records=[retry]))
    hooks._execution_control_on_end(_ctx(state, turn=2), None)

    issue = _ledger(state)["issues"][0]
    assert issue["attempted_fix"], "同组件的后续命令应被记为修复尝试"
    assert issue["attempted_fix"][-1]["outcome"] == "ok"


def test_status_derived_from_later_build_evidence(tmp_path: Path) -> None:
    """同组件后来通过构建 → status 推导为 passed，并记录解决轮次。"""
    state = _state(tmp_path)
    hooks._execution_control_on_turn_end(_ctx(state, turn=1, records=[
        _bash("make -j8 spio", returncode=2, status="error",
              log_path="logs/spio.log")]))
    hooks._execution_control_on_turn_end(_ctx(state, turn=5, records=[
        _bash("make -j8 spio", returncode=0, log_path="logs/spio.log")]))
    hooks._execution_control_on_end(_ctx(state, turn=5), None)

    issue = _ledger(state)["issues"][0]
    assert issue["status"] == "passed"
    assert issue["resolved_at_turn"] == 5
    assert issue["status_source"] == "derived_from_build_evidence"


def test_ledger_reports_truncation_honestly(tmp_path: Path) -> None:
    """环形缓冲只留 20 条；一份看起来完整的台账比标注了截断的更危险。"""
    state = _state(tmp_path)
    for turn in range(1, 26):
        hooks._execution_control_on_turn_end(_ctx(state, turn=turn, records=[
            _bash(f"make target{turn}", returncode=2, status="error",
                  log_path=f"logs/c{turn}.log")]))
    hooks._execution_control_on_end(_ctx(state, turn=25), None)

    payload = _ledger(state)
    assert payload["issues_seen"] == 25
    assert payload["issues_recorded"] == 20
    assert payload["truncated"] is True


def test_ledger_does_not_invent_dead_end(tmp_path: Path) -> None:
    """dead_end/deferred 需 agent 显式声明，推断出来的会污染跨项目复利。"""
    state = _state(tmp_path)
    hooks._execution_control_on_turn_end(_ctx(state, turn=1, records=[
        _bash("make -j8 spio", returncode=2, status="error",
              log_path="logs/spio.log")]))
    hooks._execution_control_on_end(_ctx(state, turn=1), None)

    payload = _ledger(state)
    assert {i["status"] for i in payload["issues"]} <= {"passed", "open"}
    assert "dead_end" in payload["note"]
