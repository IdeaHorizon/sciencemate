"""自主循环停机这条事件，必须变成用户看得见的东西。

worker 一直老老实实写 `unattended_loop_stopped`，平台侧**零消费方** —— 写了没人
读，和没写一样，而且更难查：代码里明明有 append_transcript。yuankk 2026-09-17
从 8:30 起没有新迭代，界面上一句说明都没有，只剩一张永远转着的卡。

判据落在"用户收到没有"上，不落在"事件发出去没有"上。
"""
from __future__ import annotations

import pytest

from app.services.execution_ingest import TranscriptAdapter
from app.services.say import spoken

from .test_execution_foundation import AT, _context


def _draft(raw: dict):
    """走 adapter 的真入口（`adapt`），不另造一条只有测试走得到的路。"""
    return TranscriptAdapter().adapt(
        {"at": AT, **raw}, context=_context(), adapter_state={}, event_id="e1")


def _said(draft, lang: str) -> str:
    """消息两种语言都存着（写的时候不知道谁来读）；这一屏读到的是哪一句。"""
    return spoken(draft.payload["content"], lang)


def test_an_aborted_loop_becomes_a_message_carrying_its_reason():
    draft = _draft({
        "event": "unattended_loop_stopped", "turn": 41, "reason": "loop_aborted",
        "detail": "连续输出一字不差且无新的机械 delta 可注入。",
    })
    assert draft is not None, "平台不认这条事件 —— 循环停了而界面上什么都没有"
    assert draft.kind == "session.message"
    assert "停止" in _said(draft, "zh")
    assert "一字不差" in _said(draft, "zh"), (
        "没带理由的「已停止」只会换来一句「为什么」"
    )
    assert _said(draft, "en").startswith("🛑 Autonomous research has stopped: several attempts")


def test_a_completed_loop_does_not_read_like_a_failure():
    draft = _draft({
        "event": "unattended_loop_stopped", "turn": 12, "reason": "research_complete",
        "detail": "",
    })
    assert draft is not None and draft.kind == "session.message"
    assert "完成" in _said(draft, "zh")
    assert "🛑" not in _said(draft, "zh"), "做完了不该显示成停机"
    assert _said(draft, "en") == "✅ Autonomous research is complete."


def test_a_reasonless_stop_still_says_something():
    """detail 缺失（老 worker / 未知路径）也不能退化成一条空消息 —— 停机码说成人话。"""
    draft = _draft({"event": "unattended_loop_stopped", "reason": "live_pause_pending"})
    assert draft is not None
    assert _said(draft, "zh") == "🛑 自主研究已停止：有一个决策在等你处理。"
    assert _said(draft, "en") == "🛑 Autonomous research has stopped: a decision is waiting for you."


def test_a_stop_code_nobody_knows_is_still_named():
    """认不出的码原样说出来，不编一句理由。"""
    draft = _draft({"event": "unattended_loop_stopped", "reason": "something_new"})
    assert "something_new" in _said(draft, "zh") and "something_new" in _said(draft, "en")
