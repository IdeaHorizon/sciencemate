"""空转熔断（run_node._MAX_ACTION_ATTEMPTS）数的是**同一个失败信号**，不是裸次数。

一审保留这道 A 类熔断，附修正「action_last_failure 变则断链」；二审核出修正没落，
仍是裸计数器（`_attempt > _MAX_ACTION_ATTEMPTS`）。第三波落地：上次失败原因
与本条 streak 的签名不同 = 有东西变过 → 计数归零重数；相同（或「无记录 —— 子 run
完成了但 flow 没闭合」那种恒空签名）才累计到熔断。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.bootstrap import bootstrap
from core.state import State
from shared.tools.run_node import _MAX_ACTION_ATTEMPTS, _run_node_tool


@pytest.fixture(autouse=True)
def _boot():
    bootstrap()


def _entry(**over) -> dict:
    base = {
        "producing_node": "experiment", "producing_run_id": "r-exp-1",
        "review_state": "done", "review_critique_artifact_id": "review_critique__x",
        "curator_state": "done", "decision_state": "action_authorized",
        "authorized_action": "revise", "authorized_target_node": "experiment",
        "action_attempt_count": _MAX_ACTION_ATTEMPTS,
    }
    base.update(over)
    return base


def _state(tmp_path, entry: dict) -> State:
    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="pb")
    st.hook_state["_callable_nodes"] = ["experiment"]
    st.hook_state["pending_post_node_flow"] = [entry]
    return st


def _dispatch(st: State, monkeypatch) -> dict:
    async def fake_execute_node(**kw):
        return {
            "run_id": "fake", "node_type": kw["node_type"], "project_id": "pb",
            "status": "completed", "missing_required_outputs": [], "turns": 1,
            "tool_call_count": 0, "artifacts": [], "final_text_preview": "",
            "state_dir": str(Path(tempfile.mkdtemp())), "project_root": None,
            "depth": 1, "sub_run_id": "t",
        }

    async def no_flow(*a, **k):
        return None

    import core.executor
    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    monkeypatch.setattr("shared.tools.run_node._run_post_producing_flow", no_flow)
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())
    return asyncio.run(_run_node_tool(
        st, node_type="experiment", node_inputs={"experiment_spec": "x"}, user_note="测试派发"))


def test_the_same_signal_repeated_still_trips_the_breaker(tmp_path, monkeypatch):
    entry = _entry(action_last_failure="qc: missing_units",
                   action_streak_signature="qc: missing_units")
    res = _dispatch(_state(tmp_path, entry), monkeypatch)
    assert res["status"] == "error" and "空转" in res["error"], res
    assert res["flow_entry"]["action_attempt_count"] == _MAX_ACTION_ATTEMPTS


def test_a_changed_signal_breaks_the_streak_and_the_dispatch_goes_through(tmp_path, monkeypatch):
    """上次失败原因变了 = 有东西变过 —— 不是空转，计数从头。修正没落地这条转红。"""
    entry = _entry(action_last_failure="qc: figure_missing",
                   action_streak_signature="qc: missing_units")
    st = _state(tmp_path, entry)
    res = _dispatch(st, monkeypatch)
    assert res.get("status") != "error" or "空转" not in res.get("error", ""), res
    assert entry["action_streak_signature"] == "qc: figure_missing"
    assert entry["action_attempt_count"] == 1
    events = [json.loads(l) for l in st.transcript_path.read_text(encoding="utf-8").splitlines()]
    reset = [e for e in events if e.get("event") == "decision_action_streak_reset"]
    assert reset and reset[-1]["attempts_before_reset"] == _MAX_ACTION_ATTEMPTS


def test_no_failure_record_at_all_is_the_idling_the_breaker_exists_for(tmp_path, monkeypatch):
    """子 run 完成了但 flow 没闭合：签名恒空，累计照旧熔断（实测 11 圈 5.5h 的形状）。"""
    entry = _entry(action_last_failure="", action_streak_signature="")
    res = _dispatch(_state(tmp_path, entry), monkeypatch)
    assert res["status"] == "error" and "空转" in res["error"], res
