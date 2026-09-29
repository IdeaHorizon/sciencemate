"""框架的等待心跳不是用户指令（issue #744）。

## 现场（lujy 实测，一个项目被压死）

`research_intake.json` 里 118 条"用户后续指令"共 33 万字符 —— 真实用户输入只有
143 字符：

    来源                条数    字符      占比
    框架自己的等待心跳   114    329200    99%
    真实用户输入           4       951     0%

心跳每 2 分钟一条、2890 字符：「⏱️ 框架已代你等待 120.1s（在飞子节点：
literature）…」。而 intake 被 `render_intake_section()` **逐字注入每个节点的
system prompt**，还盖着"这是研究目标的唯一权威表述，与本段冲突时以本段为准"。
于是任何节点、任何入口开局就 33 万字符、超窗口 8 万。

## 根因是一处

`chat._is_continuous_turn` 原本判 `lstrip().startswith(哨兵)`，而
`core/session_driver.py` 交出 prompt 前往**前面**贴装饰。贴一次哨兵就不在句首，
判据翻面 → `chat.py` 的 intake 闸放行。文本去重（`research_intake.py:76`）挡不住
——每条秒数都不同，永远不重复。

## 判据落在真入口上

用 `_child_wait_note` 真生成装饰、过那道闸、看 intake 落盘结果。只断言
`_is_continuous_turn("…")` 对手写字符串返回 True 是不够的 —— 那正是原来能全绿
的写法。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import chat as chat_mod
from core.research_intake import (
    load_intake,
    record_intake,
    render_intake_section,
)
from core.state import State

_ORIG = "研究东亚季风与气溶胶的相互作用，用 WRF-Chem 做敏感性实验。"


def _project() -> Path:
    return Path(tempfile.mkdtemp())


def _heartbeat_prompt(waited_s: float) -> str:
    """完全照 session_driver 那两行来：心跳前置到续轮 prompt 上。"""
    base = f"{chat_mod._CONTINUOUS_INTERNAL_PREFIX}\n这是框架自动生成的持续运行续轮。"
    note = chat_mod._child_wait_note({
        "waited_seconds": waited_s, "children": ["literature"],
        "woke_on": "check_in_elapsed", "child_quiet_seconds": None,
        "check_in_set_by_agent": True,
    })
    return f"{note}\n\n{base}"


def _record_if_user_turn(root: Path, text: str) -> None:
    """复刻 chat.py:2117 那道闸 —— 同一个判据函数、同一个调用形状。"""
    if not chat_mod._is_continuous_turn(text):
        record_intake(root, text, source="user")


def test_decorated_continuation_is_still_a_framework_turn():
    decorated = _heartbeat_prompt(120.1)
    assert not decorated.lstrip().startswith(chat_mod._CONTINUOUS_INTERNAL_PREFIX), (
        "前提没成立：哨兵还在句首，那就测不到本 issue 的形状"
    )
    assert chat_mod._is_continuous_turn(decorated)


def test_real_user_text_is_still_a_user_turn():
    """不能矫枉过正。"""
    assert not chat_mod._is_continuous_turn(_ORIG)
    assert not chat_mod._is_continuous_turn("1. 补完 uwnd 数据")


def test_heartbeats_never_reach_research_intake():
    """114 条秒数各异的心跳，一条都不许进 —— 且注入段不许因此膨胀。"""
    root = _project()
    _record_if_user_turn(root, _ORIG)
    for i in range(114):
        _record_if_user_turn(root, _heartbeat_prompt(120.0 + i * 0.1))

    rec = load_intake(root)
    assert rec["original_text"] == _ORIG
    assert rec.get("amendments") == []

    section = render_intake_section(root)
    assert "框架已代你等待" not in section
    assert len(section) < 2000, f"注入段 {len(section)} 字符"


def test_real_followup_instructions_still_recorded():
    root = _project()
    _record_if_user_turn(root, _ORIG)
    _record_if_user_turn(root, _heartbeat_prompt(120.1))
    _record_if_user_turn(root, "改成只做 2015-2020 这一段")

    assert [a["text"] for a in load_intake(root)["amendments"]] == [
        "改成只做 2015-2020 这一段"]


@pytest.mark.asyncio
async def test_driver_output_survives_every_decoration(monkeypatch):
    """走 session_driver 真路径：两层装饰都贴上，交出来的仍是"框架轮"。"""
    from core import session_driver

    class _St:
        hook_state: dict = {"continuous_check_in_note": "检查间隔已收到（3600s）"}
        events: list = []

        def append_transcript(self, event, **kw):
            self.events.append((event, kw))

    async def _wait(_st):
        return {"waited_seconds": 120.1, "children": ["literature"],
                "woke_on": "check_in_elapsed", "child_quiet_seconds": None,
                "check_in_set_by_agent": True}

    monkeypatch.setattr(chat_mod, "_continuous_running", lambda _s: True)
    monkeypatch.setattr(chat_mod, "_wait_for_child_progress", _wait)
    monkeypatch.setattr(chat_mod, "_continuous_check_in", lambda _r: (3600, None))
    monkeypatch.setattr(
        chat_mod, "_continuous_followup",
        lambda _s, _r, *, reason: (
            f"{chat_mod._CONTINUOUS_INTERNAL_PREFIX}\n续轮正文", 0.0))

    action = await session_driver.next_action(
        _St(), "CONTINUOUS_STATUS: continue check_in=3600", reason="check_in")

    assert action.kind == "prompt"
    assert "框架已代你等待" in action.prompt          # 装饰真贴上了（前提）
    assert chat_mod._is_continuous_turn(action.prompt)  # 仍然认得出（结论）
