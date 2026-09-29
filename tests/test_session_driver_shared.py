"""会话驱动策略：CLI 与平台共用一份（UI 缺口盘点 A 组）。

背景：无人值守的全部能力此前只长在 chat.py。平台只有 turn()/answer()，
是"被推一下走一步"的模型 —— UI 上的 autonomous 名不副实，只能靠外挂脚本推，
而外挂脚本按 DB 缓存态判断，会误判僵死、会误伤正在干活的 run（实测两样都发生）。
"""
from __future__ import annotations

import asyncio

import pytest

from core import pause as pause_registry
from core.session_driver import SessionAction, next_action


class _State:
    def __init__(self):
        self.hook_state: dict = {}
        self.events: list = []

    def append_transcript(self, event, **kw):
        self.events.append((event, kw))


@pytest.fixture(autouse=True)
def _clean():
    for ctx in list(pause_registry.list_paused()):
        pause_registry.clear_pause(ctx.run_id)
    yield
    for ctx in list(pause_registry.list_paused()):
        pause_registry.clear_pause(ctx.run_id)


def _run(state, reply="", reason="completed", **kw):
    return asyncio.run(next_action(state, reply, reason=reason, **kw))


def test_stops_when_continuous_is_off() -> None:
    action = _run(_State())
    assert action.kind == "stop" and action.reason == "continuous_not_running"


def test_does_not_grab_the_wheel_from_a_driven_pause(monkeypatch) -> None:
    """有人在管的 pause = 控制权在别人手上，抢了会造出第二个控制平面。"""
    import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_continuous_running", lambda s: True)

    class _Ctx:
        run_id = "r1"
        pause_event = type("PE", (), {"metadata": {}, "options": [], "question": "q",
                                      "asking_run_id": "r1"})()

    pause_registry.register_pause(_Ctx())
    pause_registry.claim_driver("r1")

    action = _run(_State())
    assert action.kind == "stop" and action.reason == "live_pause_pending"


def test_waits_before_computing_the_prompt(monkeypatch) -> None:
    """顺序不可换：先机械等，醒来后进度指纹已变，症状层检测才拿到等待之后的世界。

    反过来会让 stall 计数和"一字不差重复"检测咬到忙等自己的尾巴
    （E2E-4：writing 5 分钟就跑完了，项目空转 21.9 小时）。
    """
    import chat as chat_mod

    order: list[str] = []

    async def _wait(state):
        order.append("wait")
        return {"finished": ["child-1"]}

    def _followup(state, reply, *, reason):
        order.append("followup")
        return "继续", 0.0

    monkeypatch.setattr(chat_mod, "_continuous_running", lambda s: True)
    monkeypatch.setattr(chat_mod, "_wait_for_child_progress", _wait)
    monkeypatch.setattr(chat_mod, "_continuous_followup", _followup)
    monkeypatch.setattr(chat_mod, "_child_wait_note", lambda w: "[子节点结束]")

    action = _run(_State())
    assert order == ["wait", "followup"], "必须先等再算"
    assert action.kind == "prompt"
    assert action.prompt.startswith("[子节点结束]"), "等到的事件要带给模型"


def test_check_in_takes_effect_in_the_same_round(monkeypatch) -> None:
    """agent 说"一小时后叫我"，这一轮就得生效 —— 晚一拍等于没听。"""
    import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_continuous_running", lambda s: True)
    monkeypatch.setattr(chat_mod, "_continuous_check_in", lambda r: (3600.0, "已截断到 1h"))
    monkeypatch.setattr(chat_mod, "_wait_for_child_progress",
                        lambda s: asyncio.sleep(0, result=None))
    monkeypatch.setattr(chat_mod, "_continuous_followup",
                        lambda s, r, *, reason: ("继续", 30.0))

    state = _State()
    action = _run(state)
    assert state.hook_state["continuous_check_in_s"] == 3600.0
    assert "已截断到 1h" in action.prompt
    assert action.delay_s == 0.0, "有 check-in 提示时不再额外退避"


def test_void_replay_prompt_is_never_decorated(monkeypatch) -> None:
    """空轮重放靠**逐字节等值**识别，任何装饰都会破坏驻定性。"""
    import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_continuous_running", lambda s: True)
    monkeypatch.setattr(chat_mod, "_continuous_check_in", lambda r: (None, "注记"))
    monkeypatch.setattr(chat_mod, "_wait_for_child_progress",
                        lambda s: asyncio.sleep(0, result={"finished": ["c"]}))
    monkeypatch.setattr(chat_mod, "_continuous_followup",
                        lambda s, r, *, reason: (chat_mod._VOID_RETRY_PROMPT, 5.0))

    action = _run(_State(), reason="void_turn")
    assert action.prompt == chat_mod._VOID_RETRY_PROMPT


def test_platform_has_an_unattended_loop() -> None:
    """平台必须能自己跑完，而不是等外挂脚本推。"""
    import inspect

    import platform_runtime

    src = inspect.getsource(platform_runtime)
    assert "async def run_unattended(" in src
    loop = src[src.index("async def run_unattended("):]
    loop = loop[:loop.index("async def answer(")]
    code = "\n".join(l for l in loop.splitlines() if not l.lstrip().startswith("#"))
    assert "next_action" in code, "续轮策略必须用共用的那份"
    assert "max_turns" in code, "循环必须有界 —— 防死锁的东西自己变死循环就全白做"
    assert 'status == "paused"' in code, "需要人的时候必须交还给人"
