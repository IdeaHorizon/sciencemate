"""无人认领的 pause 不得堵死平台路径（台账 #1 / #7 / #8）。

死锁闭环（2026-08-09 实测，19 个 session、最久 63.9 小时）：
  pause 登记了 → 创建它的 attempt 结束 → 没有任何人在等答复
  → answer() 因 "attempt 已关闭" 失败
  → turn() 因 "pause_pending" 被拒
  → 两条路同时堵死，平台自己看不见

CLI 早在 E2E-5a（82 分钟死锁）之后就修了这条，平台侧从来没接。
本文件锁住"两条路径共用同一份判据与处理"。
"""
from __future__ import annotations

import asyncio

import pytest

from core import pause as pause_registry
from core import pause_driver


class _Ctx:
    """最小 PausedRunContext 替身：注册表只用到 run_id / pause_event。"""

    def __init__(self, run_id: str, meta: dict | None = None):
        self.run_id = run_id
        self.pause_event = type("PE", (), {
            "metadata": meta or {},
            "options": ["继续", "中止"],
            "question": "q",
            "asking_run_id": run_id,
        })()


class _State:
    def __init__(self):
        self.events: list[tuple] = []
        self.hook_state: dict = {}

    def append_transcript(self, event, **kw):
        self.events.append((event, kw))


@pytest.fixture(autouse=True)
def _clean_registry():
    for ctx in list(pause_registry.list_paused()):
        pause_registry.clear_pause(ctx.run_id)
    yield
    for ctx in list(pause_registry.list_paused()):
        pause_registry.clear_pause(ctx.run_id)


def test_undriven_now_reports_orphans_only_when_nobody_drives() -> None:
    pause_registry.register_pause(_Ctx("r1"))
    assert [c.run_id for c in pause_driver.undriven_now()] == ["r1"]

    # 只要还有**一个**有人管，就不许抢方向盘（false-PROCEED 事故的第一环）
    pause_registry.claim_driver("r1")
    assert pause_driver.undriven_now() == []
    pause_registry.release_driver("r1")
    assert [c.run_id for c in pause_driver.undriven_now()] == ["r1"]


def test_resolver_always_clears_the_registry_even_on_failure(monkeypatch) -> None:
    """防死锁的机制自己挂住 = 全白做。失败必须清注册表放行。"""
    pause_registry.register_pause(_Ctx("r_boom"))

    async def _explode(**kw):
        raise RuntimeError("resume 炸了")

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _explode)
    state = _State()
    handled = asyncio.run(
        pause_driver.resolve_orphan_pauses(state, pause_driver.undriven_now()))

    assert handled is True
    assert pause_registry.list_paused() == [], "失败也必须清干净，否则下轮又被同一批卡住"
    assert any(e == "orphan_pause_resolve_failed" for e, _ in state.events)


def test_resolver_is_bounded(monkeypatch) -> None:
    """有界：resume 会真跑 LLM，可能永不返回。"""
    async def _hang(**kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _hang)
    monkeypatch.setattr(pause_driver, "ORPHAN_PAUSE_RESOLVE_MAX_S", 0.05)
    pause_registry.register_pause(_Ctx("r_hang"))
    state = _State()

    asyncio.run(pause_driver.resolve_orphan_pauses(state, pause_driver.undriven_now()))

    assert pause_registry.list_paused() == []


def test_platform_does_not_steal_pauses_from_the_human() -> None:
    """平台 turn() **不许**套用 CLI 的 undriven 判据。

    CLI 里"有人管"= 有人在 await stdin；平台里 pause 通过 HTTP 返回给人，
    进程内永远没人 await（claim_driver 只在 answer() 执行期间持有）。于是
    undriven_now() 在平台上恒为真 —— 照搬会把人正要在 UI 上回答的 pause
    自动答掉，正是 CLI 注释警告的"抢方向盘造出第二个控制平面"。
    （我第一版就是这么写的，被 test_serve_resumes_pause... 当场抓住。）

    平台侧真正的孤儿判据在 App Server：pause 活着但 attempt 已关闭 ——
    见 test_pause_outliving_attempt_is_recoverable.py。
    """
    import inspect

    import platform_runtime

    src = inspect.getsource(platform_runtime)
    start = src.index("async def turn(")
    turn_src = src[start:start + 2500]
    # 只看**可执行代码** —— 注释里正解释着"为什么不能这么用"，按整段文本
    # 匹配会把解释本身当成违规（判据要认语义，不是认字符串出现过）。
    code = "\n".join(
        line for line in turn_src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "undriven_now" not in code, "平台不能用 CLI 的 undriven 判据"
    assert "resolve_orphan_pauses" not in code, "平台不能在这里自动答 pause"
    assert "pause_pending" in code, "有 pause 挂着时仍要拒绝新 turn"


def test_cli_and_platform_share_one_implementation() -> None:
    """两条路径不许各写一份无人值守语义。"""
    import pathlib

    chat_src = pathlib.Path("chat.py").read_text(encoding="utf-8")
    assert "from core.pause_driver import resolve_orphan_pauses" in chat_src
    # CLI 里不许再有私有实现
    assert "drive_pause_chain(" not in chat_src.split("_queue_continuous_followup")[0][-3000:], \
        "CLI 应转发 core，不再自带一份 orphan resolve"
