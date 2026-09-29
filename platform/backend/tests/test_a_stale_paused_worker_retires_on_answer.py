"""停在问题上的旧一代 worker，在人作答那一刻换代 —— 答复那条路与开新一轮同一条规矩。

## 现场（2026-09-09 node20，qinp 的课题）

worker 09-08 15:33 起，之后部署了三次，它一次都没跟上：新一轮走 `_get_or_create`，
接管来的会话指纹为空 → respawn；可答复那条路直接把答复送给停着的旧 worker，只要
人一直点卡、它就一直活着跑旧代码。修复上线了，对它不生效。

换代不是新机制：respawn 已经对停靠 worker 成立（pause 由 stale-binding 钩子标成
可恢复，checkpoint 接续）。这里只是让答复那条路也走它，并把人的那一下点击化成一句
话交给新 worker（它的流程账本记着这个决定点，会重新呈递；连续档下当场自动放行）。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from app.api.v1.chat import ChoiceAnswer, _choice_as_words
from app.services.harness_sessions import AppRunBinding, HarnessSessionManager

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _manager_with(session) -> HarnessSessionManager:
    manager = HarnessSessionManager()
    manager._sessions[manager._key("p", "s")] = session
    return manager


def _paused_session(*, fingerprint: str, occupied: bool = False):
    terminated: list[str] = []

    async def terminate():
        terminated.append("yes")

    session = SimpleNamespace(
        alive=True, paused=True, backend_fingerprint=fingerprint,
        binding=AppRunBinding("u", "s", "run-x", "s", "att", None, ""),
        conversation_in_flight=occupied, project_id="p", session_id="s",
        terminate=terminate,
    )
    return session, terminated


def test_an_adopted_paused_worker_is_stale_and_a_native_one_is_not(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.harness_sessions.read_worker_activity", lambda *_a: None
    )
    adopted, _ = _paused_session(fingerprint="")
    assert _manager_with(adopted).paused_worker_is_stale("p", "s") is True
    native, _ = _paused_session(fingerprint="backend-fp")
    assert _manager_with(native).paused_worker_is_stale("p", "s") is False


def test_a_worker_with_a_turn_in_flight_is_never_retired(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.harness_sessions.read_worker_activity", lambda *_a: None
    )
    busy, _ = _paused_session(fingerprint="", occupied=True)
    assert _manager_with(busy).paused_worker_is_stale("p", "s") is False


@pytest.mark.asyncio
async def test_retiring_terminates_the_worker_and_marks_its_pause_recoverable() -> None:
    session, terminated = _paused_session(fingerprint="")
    manager = _manager_with(session)
    orphaned: list[AppRunBinding] = []

    async def handler(binding):
        orphaned.append(binding)

    manager.set_stale_binding_handler(handler)
    binding = await manager.retire_paused_worker("p", "s")
    assert terminated == ["yes"]
    assert binding is not None and binding.run_id == "run-x"
    assert orphaned == [binding], "送走的 pause 必须走 respawn 同一条标记路"
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_the_click_becomes_the_label_the_card_showed() -> None:
    decision = SimpleNamespace(choices=[
        {"choiceId": "proceed", "label": "PROCEED to next stage"},
        {"choiceId": "abort", "label": "ABORT pipeline"},
    ])

    class _DB:
        async def scalar(self, _stmt):
            return decision

    answer = ChoiceAnswer(kind="choice", offer_id="a:b:c", choice_id="proceed", note="继续不要停")
    assert await _choice_as_words(_DB(), "run-x", answer) == "PROCEED to next stage 继续不要停"
    bare = ChoiceAnswer(kind="choice", offer_id="a:b:c", choice_id="revise", note="")
    assert await _choice_as_words(_DB(), "run-x", bare) == "revise", "找不到 label 就用 id，不编"


def test_the_chat_entry_retires_before_it_gates_on_a_live_pause() -> None:
    """AST：`retire_paused_worker` 的调用必须排在 `no_pause_to_answer` 那道闸之前。"""
    source = (_BACKEND / "app" / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    assert source.index("retire_paused_worker(") < source.index('"no_pause_to_answer"')
    tree = ast.parse(source)
    calls = {
        getattr(node.func, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"paused_worker_is_stale", "retire_paused_worker"} <= calls
