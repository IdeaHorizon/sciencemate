"""An explicit resume must retain its run; inability to reopen cannot create work."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from core.bootstrap import bootstrap
from core.state import State


async def _execute(runs: Path, run_id: str | None):
    from core.executor import execute_node

    return await execute_node(
        "_curator", state_dir=runs, project_id=None, resume_run_id=run_id,
    )


def _run_capturing_state(runs: Path, resume_run_id: str | None) -> dict:
    captured: dict = {}

    async def _fake_run_loop(harness, state, messages, llm):
        captured["state"] = state
        captured["run_id"] = state.run_id
        raise RuntimeError("stop here — 只验证入口装配")

    with patch("core.executor.run_loop", _fake_run_loop):
        try:
            asyncio.run(_execute(runs, resume_run_id))
        except Exception:
            pass
    return captured




def test_a_run_that_never_asked_to_resume_still_answers_the_question(tmp_path: Path) -> None:
    """对照：没请求续跑也照写。缺字段的话，"没请求"和"请求了但记丢了"又分不开。"""
    bootstrap()
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    state = _run_capturing_state(runs, None)["state"]
    outcome = state.hook_state.get("_resume_outcome")
    assert outcome == {"requested_run_id": None, "resumed": False, "reason": None}


def test_a_real_resume_is_marked_as_resumed(tmp_path: Path) -> None:
    """对照：真的续上时 resumed=True —— 否则把常量 False 写死也能全绿。"""
    bootstrap()
    runs = tmp_path / "runs"
    seed = State.new(node_type="_curator", base_dir=runs)
    seed.append_transcript("run_start", node_type="_curator")

    captured = _run_capturing_state(runs, seed.run_id)
    assert captured["run_id"] == seed.run_id
    outcome = captured["state"].hook_state.get("_resume_outcome")
    assert outcome["resumed"] is True
    assert outcome["requested_run_id"] == seed.run_id
    assert outcome["reason"] is None




def test_missing_resume_target_never_creates_a_replacement(tmp_path):
    import pytest
    bootstrap()
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(FileNotFoundError):
        asyncio.run(_execute(runs, "missing-original"))
    assert list(runs.iterdir()) == []
