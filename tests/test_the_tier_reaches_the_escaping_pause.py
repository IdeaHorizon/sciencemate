"""pause 的归宿只在 worker 的操作出口判一次：给人，还是按档位在进程内消化。

## 现场（2026-09-09 node20，qinp 的课题）

09:23 切成「连续」，worker 当场收到 `["*"]`。此后 09:38、09:47、16:04、16:07 每一张
post-node 决策卡照样停下问人。`auto_resume_pauses`（连续档就地消化非高危 pause）
散在三处才有意义的地方（等后台子节点、turn 逃逸、answer 逃逸），只接通了第一处。
散着判就一定漏：正解是每一种操作都从同一个出口（`_operation_end`）出去，在那里判一次。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

import platform_runtime
from core import session_driver

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _calls_in(fn: ast.AST) -> set[str]:
    return {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }


def _method(cls_name: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_ROOT / "platform_runtime.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
    return next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_the_single_operation_exit_consults_the_tier() -> None:
    assert "_auto_resume_pauses" in _calls_in(_method("PlatformSession", "_operation_end"))


def test_no_operation_consults_the_tier_on_its_own() -> None:
    """turn / answer / run_unattended / rejoin 都不许各自再判一遍 —— 判一次的地方只有出口。"""
    for name in ("turn", "answer", "run_unattended", "rejoin"):
        try:
            method = _method("PlatformSession", name)
        except StopIteration:
            continue
        assert "_auto_resume_pauses" not in _calls_in(method), f"{name}() 自己又判了一遍"
    tree = ast.parse((_ROOT / "core" / "session_driver.py").read_text(encoding="utf-8"))
    run_turn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_turn")
    assert "auto_resume_pauses" not in _calls_in(run_turn), "脊柱里又长出了一处散着的判断"


def _session(monkeypatch, resumed):
    import chat

    session = platform_runtime.PlatformSession.__new__(platform_runtime.PlatformSession)
    session.state = SimpleNamespace(
        append_transcript=lambda *a, **k: None, run_id="r", root=None, hook_state={}
    )
    session.messages = []
    session._tailer = None
    session._activity = None
    session._suppress_terminal_result = False
    session._operation_active = True
    session.request_id = "req-1"
    emitted: list[tuple] = []
    session.emit = lambda *a, **k: emitted.append((a, k))
    seen: dict = {}

    async def fake_auto():
        seen["asked"] = True
        return resumed

    session._auto_resume_pauses = fake_auto
    session._result = lambda **kw: {
        "status": kw["status"], "final_text": kw["final_text"],
        "pause_event": {"question": "q"}, "pause_id": "p", "pause_pending_path": None,
    }
    monkeypatch.setattr(chat, "_save_conversation", lambda *a, **k: None)
    monkeypatch.setattr("core.project_workspace.request_completion_checkpoint", lambda *a, **k: None)
    return session, seen, emitted


@pytest.mark.asyncio
async def test_a_paused_result_is_consumed_before_it_leaves_the_worker(monkeypatch) -> None:
    session, seen, _emitted = _session(monkeypatch, resumed=("completed", "auto-answered"))
    result = await session._operation_end(
        operation="answer", status="paused", final_text="", before_artifacts={}, tokens_used_before=0
    )
    assert seen.get("asked") is True
    assert (result["status"], result["final_text"]) == ("completed", "auto-answered")


@pytest.mark.asyncio
async def test_outside_continuous_the_pause_still_leaves_for_the_human(monkeypatch) -> None:
    session, seen, _emitted = _session(monkeypatch, resumed=None)
    result = await session._operation_end(
        operation="turn", status="paused", final_text="", before_artifacts={}, tokens_used_before=0
    )
    assert seen.get("asked") is True
    assert result["status"] == "paused"


def test_the_platform_no_longer_ships_a_dead_environment_switch() -> None:
    text = (_ROOT / "platform" / "backend" / "app" / "services" / "harness_sessions.py").read_text(
        encoding="utf-8"
    )
    assert 'child_env["HARNESS_AUTO_APPROVE"]' not in text


def test_auto_resume_lives_in_the_shared_spine() -> None:
    assert callable(session_driver.auto_resume_pauses)
