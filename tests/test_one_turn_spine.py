"""一轮会话只有一副脊柱 —— CLI 与平台都必须真的走 session_driver.run_turn。

## 现场

`chat.py` 与 `platform_runtime.py` 曾各写一遍同一副 turn 脊柱（第三份在
一次性桥接 run_platform_request 里）。分叉的代价反复兑现：无人值守能力只长
在 CLI 那份上、auto-approve 在平台上读都没被读过（E2E v17）、记忆链的平台
分支断了三个月没人发现。

这组测试钉两件事：

  1. **脊柱本身的行为**（run_turn 对三个前端问题的处理）；
  2. **两个入口真的接在脊柱上**（wiring —— 不接上的话，脊柱再对也只是
     又一份没人调的机制）。
"""
from __future__ import annotations

import pathlib

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.session_driver import (
    PausePendingError,
    SessionFrontend,
    TurnOutcome,
    run_turn,
)


# ── 假件 ──────────────────────────────────────────────────────────────────

@dataclass
class _FakeState:
    run_id: str = "run-1"
    hook_state: dict = field(default_factory=dict)
    transcript: list = field(default_factory=list)

    def append_transcript(self, kind: str, **fields: Any) -> None:
        self.transcript.append((kind, fields))


@dataclass
class _FakeLoopResult:
    status: str | None = None
    final_text: str = ""


class _Frontend(SessionFrontend):
    def __init__(self, *, ask_pause=None, waits_for_background=False):
        self.ask_pause = ask_pause
        self.waits_for_background = waits_for_background
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.fixture
def spine(monkeypatch):
    """把 chat 的既有实现替成可观测的假件；返回观测记录。"""
    import chat as chat_mod

    seen: dict[str, Any] = {"raw_calls": [], "post_calls": [], "saved": 0}

    async def _raw(state, harness, messages, llm, user_text):
        seen["raw_calls"].append(user_text)
        return seen["loop_result"]

    async def _post(result, harness, state, messages, llm, *, ask_pause=None):
        seen["post_calls"].append(ask_pause)
        return "整形后的回复"

    monkeypatch.setattr(chat_mod, "_run_one_turn_raw", _raw)
    monkeypatch.setattr(chat_mod, "_post_loop_reply", _post)
    monkeypatch.setattr(chat_mod, "_save_conversation",
                        lambda state, messages: seen.__setitem__(
                            "saved", seen["saved"] + 1))
    import core.pause as pause_mod
    monkeypatch.setattr(pause_mod, "get_deepest_paused", lambda: None)
    seen["loop_result"] = _FakeLoopResult(status="completed")
    return seen


def _run(coro):
    return asyncio.run(coro)


# ── 1. 脊柱行为 ───────────────────────────────────────────────────────────

def test_completed_turn_flows_through_reply_shaping(spine):
    outcome = _run(run_turn(_FakeState(), None, [], None, "你好",
                            frontend=_Frontend()))
    assert outcome == TurnOutcome(status="completed", reply="整形后的回复")
    assert spine["raw_calls"] == ["你好"]


def test_pause_escapes_when_nobody_drives_in_process(spine):
    """平台形前端（ask_pause=None）：paused 结果不许走回复整形 ——
    走了就会撞 _post_loop_reply 的接线检查，或更糟：把 pause 吞成一句空话。"""
    spine["loop_result"] = _FakeLoopResult(status="paused")
    outcome = _run(run_turn(_FakeState(), None, [], None, "msg",
                            frontend=_Frontend()))
    assert outcome.status == "paused"
    assert outcome.reply == ""
    assert spine["post_calls"] == [], "pause 逃逸时不该碰 _post_loop_reply"


def test_pause_is_driven_in_process_when_frontend_can(spine):
    """CLI 形前端：paused 结果就地驱动（问答函数透传给回复整形层），
    turn 不以 paused 结束。"""
    async def _ask(pe):
        return "继续"

    spine["loop_result"] = _FakeLoopResult(status="paused")
    outcome = _run(run_turn(_FakeState(), None, [], None, "msg",
                            frontend=_Frontend(ask_pause=_ask)))
    assert outcome.status == "completed"
    assert outcome.reply == "整形后的回复"
    assert spine["post_calls"] == [_ask], "问答函数必须原样到达 drive 层"


def test_pending_pause_blocks_new_turn_only_for_escape_frontends(
        spine, monkeypatch):
    """pause 挂着时：进程外作答的前端开新轮=两个驱动者抢一个 pause，必须拒；
    进程内驱动的前端（人就是唯一驱动者）不受限。"""
    import core.pause as pause_mod
    monkeypatch.setattr(pause_mod, "get_deepest_paused", lambda: object())

    with pytest.raises(PausePendingError):
        _run(run_turn(_FakeState(), None, [], None, "msg",
                      frontend=_Frontend()))

    async def _ask(pe):
        return "答"
    # 有进程内驱动者：守卫放行，正常跑完
    outcome = _run(run_turn(_FakeState(), None, [], None, "msg",
                            frontend=_Frontend(ask_pause=_ask)))
    assert outcome.reply == "整形后的回复"


def test_error_path_checkpoints_and_saves_before_reraising(spine, monkeypatch):
    """异常不是"这轮没发生"：工作区可能改了一半。checkpoint + 存盘再抛 ——
    这层此前只有平台有，CLI 跑挂就丢。"""
    import chat as chat_mod

    async def _boom(*a, **kw):
        raise RuntimeError("炸")
    monkeypatch.setattr(chat_mod, "_run_one_turn_raw", _boom)

    requested: list = []
    import core.project_workspace as pw
    monkeypatch.setattr(pw, "request_completion_checkpoint",
                        lambda state, status: requested.append(status))

    with pytest.raises(RuntimeError, match="炸"):
        _run(run_turn(_FakeState(), None, [], None, "msg",
                      frontend=_Frontend()))
    assert requested == ["failed"]
    assert spine["saved"] == 1, "崩溃后重开会话要能接着聊 —— 对话必须已存盘"


def test_background_children_are_awaited_only_when_the_frontend_says_so(spine):
    """turn 的边界因入口而异：平台等后台（RPC 结果带 artifact delta），
    CLI 不等（提示符要还给人）。"""
    from shared.tools import run_node as run_node_module

    async def _one_turn(waits: bool) -> list:
        fe = _Frontend(waits_for_background=waits)
        task = asyncio.ensure_future(asyncio.sleep(0.01))
        run_node_module._BACKGROUND_TASKS.add(task)
        try:
            await run_turn(_FakeState(), None, [], None, "msg", frontend=fe)
        finally:
            run_node_module._BACKGROUND_TASKS.discard(task)
            if not task.done():
                task.cancel()
        return [(e, f) for e, f in fe.events if e == "background_wait"]

    waited = _run(_one_turn(True))
    assert waited and waited[0][1]["task_count"] == 1
    assert _run(_one_turn(False)) == [], "CLI 形前端不该等后台子节点"


# ── 2. 两个入口真的接在脊柱上 ─────────────────────────────────────────────

class _SpineUsed(Exception):
    pass


def _sentinel_run_turn(monkeypatch):
    import core.session_driver as sd

    async def _mark(*a, **kw):
        raise _SpineUsed
    monkeypatch.setattr(sd, "run_turn", _mark)


def test_cli_turn_runs_on_the_shared_spine(monkeypatch):
    import chat as chat_mod

    _sentinel_run_turn(monkeypatch)
    with pytest.raises(_SpineUsed):
        _run(chat_mod._run_one_turn(
            _FakeState(), None, [], None, "hi", chat_mod.ChatState()))


def test_platform_turn_runs_on_the_shared_spine(monkeypatch):
    """PlatformSession.turn 除 RPC 协议件外没有自己的脊柱。"""
    import platform_runtime as pr

    _sentinel_run_turn(monkeypatch)
    # 走**真的构造器**，只把它管不到的那几样（state/harness/messages/client，
    # 平时由 `start()` 装上）换成假的。
    #
    # 原来这里是 `__new__` + 手工补七八个字段 —— 那是一份"会话长什么样"的
    # 抄件。P1 给会话加了邮箱之后它当场 AttributeError，而红的原因跟被测的
    # 性质（turn 没有自己的脊柱）毫无关系。「子集按构造方选，不按改动文件选」。
    session = pr.PlatformSession(
        {
            "request_id": "req-0", "tenant_id": "t", "project_id": "p",
            "session_id": "s", "home_dir": pathlib.Path("/tmp"),
        },
        lambda *a, **kw: None,
    )
    session.state = _FakeState()
    session.harness = None
    session.messages = []
    session.client = None
    session.emit = lambda *a, **kw: None
    session._operation_start = lambda request_id, operation: ({}, 0)

    import core.pause as pause_mod
    monkeypatch.setattr(pause_mod, "get_deepest_paused", lambda: None)
    with pytest.raises(_SpineUsed):
        _run(session.turn("req-1", "hi"))


def test_cli_frontend_answers_the_three_questions():
    """CLI：进程内驱动 pause、不等后台。改这两个答案 = 改会话语义，
    必须是有意的（并说得出"这真的因入口而异吗"）。"""
    import chat as chat_mod

    fe = chat_mod._CliFrontend(chat_mod.ChatState())
    assert fe.ask_pause is not None
    assert fe.waits_for_background is False


def test_platform_frontend_answers_the_three_questions():
    """平台：pause 逃逸（人在 HTTP 那头）、等后台、事件带 request_id。"""
    import platform_runtime as pr

    class _S:
        request_id = "req-9"
        events: list = []

        def emit(self, event, **fields):
            self.events.append((event, fields))

    s = _S()
    # 走生产同一条构造路径（类是延迟建的，见 platform_runtime._frontend_classes）
    fe = pr._platform_frontend(s)
    assert fe.ask_pause is None
    assert fe.waits_for_background is True
    fe.emit("background_wait", task_count=2)
    assert s.events == [("background_wait",
                         {"request_id": "req-9", "task_count": 2})]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
