"""停靠的理由必须是停靠的理由（#1083 第 3 条）。

`worker_parked.why` 取的是 `SessionAction.reason`，而那一路传的是**上一轮怎么
结束的**（`reason=status or "completed"`）。于是真实现场是「因 blocked 停靠、
30 分钟后复查」，公开事件上写着 `why="completed"`。

理由字段说反话比没有理由更坏：读的人据此去查一个不存在的完成。活体
（benchmark pipeline_stop_point）里那条 `worker_parked` 写的就是 `why="completed"`。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State


def _blocked_state() -> State:
    bootstrap()
    st = State.new(node_type="project_chat", base_dir=Path(tempfile.mkdtemp()), project_id=None)
    st.hook_state["continuous_running"] = True
    return st


def test_parking_publishes_why_it_parked() -> None:
    import chat as _chat

    st = _blocked_state()

    class _NoWork:
        blocking_obligations: list = []

    prompt, delay = _chat._park_blocked(st, "CONTINUOUS_STATUS: blocked", _NoWork())
    assert prompt and delay > 0

    park = st.hook_state.get("continuous_park")
    assert park, "停靠了却没把理由交给上层 —— platform 侧只能沿用上一轮的状态"
    assert park["reason"] == "blocked"
    assert park["probe"] == 1
    assert park["next_probe_at_epoch"] > 0, "说得出「在睡」，说不出「睡到几点」"


def test_resuming_clears_the_park_fact() -> None:
    """解除之后不能还报着停靠 —— 会被读成「一直卡着」。"""
    import chat as _chat

    st = _blocked_state()

    class _NoWork:
        blocking_obligations: list = []

    _chat._park_blocked(st, "CONTINUOUS_STATUS: blocked", _NoWork())
    _chat._unpark_if_blocked(st)
    assert st.hook_state.get("continuous_park") is None


@pytest.mark.asyncio
async def test_the_session_action_carries_the_park_reason(monkeypatch) -> None:
    """`next_action` 交给 worker 的 reason 必须是停靠的理由，不是上一轮的状态。"""
    import chat as _chat
    from core import session_driver

    st = _blocked_state()

    def _fake_followup(state, reply, *, reason):
        state.hook_state["continuous_park"] = {
            "reason": "blocked", "probe": 2,
            "next_probe_seconds": 1800, "next_probe_at_epoch": 4102444800,
            "agent_set_interval": False, "stated": "缺标定文件",
        }
        return "下一轮", 1800.0

    monkeypatch.setattr(_chat, "_continuous_followup", _fake_followup)
    monkeypatch.setattr(_chat, "_continuous_running", lambda s: True)
    monkeypatch.setattr(session_driver, "_chat", _chat, raising=False)

    action = await session_driver.next_action(st, "reply", reason="completed")

    assert action.kind == "prompt"
    assert action.reason == "parked_blocked", (
        f"停靠的理由被写成了上一轮的状态：{action.reason!r} —— 活体里写的正是 'completed'")
    assert action.extra.get("probe") == 2
    assert action.extra.get("next_probe_at_epoch") == 4102444800
    assert "stated" not in action.extra, "呼救正文不该跟着事件外发"


def test_the_worker_publishes_the_structured_park_facts() -> None:
    """worker 落 `worker_parked` 时要把结构化事实带上，平台 ingest 也要收。"""
    import inspect

    import platform_runtime as pr

    src = inspect.getsource(pr)
    head = src.index('"worker_parked"')
    assert "park" in src[head:head + 400], (
        "worker_parked 只有一个 why —— 读的人还得去翻 worker 的 transcript")

    ingest = Path(__file__).resolve().parents[1] / (
        "platform/backend/app/services/execution_ingest.py")
    body = ingest.read_text(encoding="utf-8")
    at = body.index('"run.parked"')
    assert '"park"' in body[at:at + 900], "平台把停靠事实丢在了摄取口"
