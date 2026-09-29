"""Regression tests for chat.py's queue-backed auto-approve path."""
from __future__ import annotations

import asyncio

import pytest

import chat as chat_mod
from core import pause_driver
from core.pause import PauseEvent


@pytest.fixture(autouse=True)
def _reset_auto_approve():
    pause_driver.set_auto_approve(False)
    yield
    pause_driver.set_auto_approve(False)


def _decision(recommended_index: int = 1) -> PauseEvent:
    return PauseEvent(
        question="Post-node decision",
        options=["PROCEED", "REVISE", "REDIRECT", "ABORT", "EDIT"],
        metadata={
            "type": "decision_package",
            "recommended_option_index": recommended_index,
        },
    )


@pytest.mark.asyncio
async def test_chat_queue_auto_approves_recommended_decision(monkeypatch):
    """Queue-backed chat pauses must honor the reviewer's recommendation."""
    pause_driver.set_auto_approve(True)
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_COUNTDOWN_SEC", 0.01)
    chat_state = chat_mod.ChatState()

    answer = await chat_mod._ask_pause_via_queue(_decision(), chat_state)

    assert answer == "2"
    assert not chat_state.paused.is_set()


@pytest.mark.asyncio
async def test_chat_queue_user_can_override_during_countdown(monkeypatch):
    """A user answer arriving before the timeout overrides auto-approve."""
    pause_driver.set_auto_approve(True)
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_COUNTDOWN_SEC", 1)
    chat_state = chat_mod.ChatState()

    task = asyncio.create_task(
        chat_mod._ask_pause_via_queue(_decision(), chat_state)
    )
    await asyncio.sleep(0)
    assert chat_state.paused.is_set()
    await chat_state.pause_answer_queue.put("1")

    assert await task == "1"
    assert not chat_state.paused.is_set()


@pytest.mark.asyncio
async def test_chat_queue_manual_mode_still_waits_for_user():
    pause_driver.set_auto_approve(False)
    chat_state = chat_mod.ChatState()

    task = asyncio.create_task(
        chat_mod._ask_pause_via_queue(_decision(), chat_state)
    )
    await asyncio.sleep(0)
    assert chat_state.paused.is_set()
    assert not task.done()
    await chat_state.pause_answer_queue.put("3")

    assert await task == "3"
    assert not chat_state.paused.is_set()


@pytest.mark.asyncio
async def test_chat_queue_answers_generic_pause_without_waiting():
    """无人值守下**立即返回**，不等人 —— 但没有推荐项时不许替它挑选项。

    旧行为是"选第一个"。那是任意值：实测选中过 "A: 等平台修复合约门"，
    autonomous 于是自己选择了停摆（台账 #3）。现在把"你没给推荐项"这个事实
    如实回给提问方，让它带 recommended_option_index 重问。
    不等待这一点没变 —— 那才是死锁的来源。
    """
    pause_driver.set_auto_approve(True)
    chat_state = chat_mod.ChatState()
    pause = PauseEvent(question="Pick one", options=["safe", "risky"])

    answer = await chat_mod._ask_pause_via_queue(pause, chat_state)

    assert answer not in {"safe", "risky"}, "不许在没有推荐项时替它挑一个"
    assert "recommended_option_index" in answer
    assert not chat_state.paused.is_set(), "无人值守不许等人"


@pytest.mark.asyncio
async def test_chat_queue_uses_the_recommendation_when_given():
    pause_driver.set_auto_approve(True)
    chat_state = chat_mod.ChatState()
    pause = PauseEvent(
        question="Pick one", options=["safe", "risky"],
        metadata={"type": "structured_question", "recommended_option_index": 1},
    )

    answer = await chat_mod._ask_pause_via_queue(pause, chat_state)

    assert answer == "2", "结构化提问按推荐项作答（1-indexed）"


def test_auto_approve_clamps_invalid_recommended_index():
    pause = _decision(recommended_index=99)
    assert pause_driver.auto_approve_answer(pause) == "5"
