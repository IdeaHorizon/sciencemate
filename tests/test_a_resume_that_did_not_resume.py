"""请求续跑、没续上、换了个新 run —— 这件事必须到达调用方（#1081）。

`execute_node(resume_run_id=X)` 里 `State.reopen` 抛 `FileNotFoundError` /
`OSError` 时会改用 `State.new` 接着跑。这是对的：续跑失败不该让整次派发死掉。
但此前它只留一行 `log.warning`：

  - 返回的是新 run_id 的正常 summary（`status=completed`），没有字段说明换过 run；
  - 新 run 的 transcript 里既没有 X，也没有"曾请求续跑"的记录；
  - 只能把入参里的 X 和返回的 `child_run_id` 人工比一遍才看得出来。

于是"接着上一轮"和"从头来过"在调用方眼里逐字段相同，而两者的上下文、进度和
花费完全不同 —— 正是判例「[[feedback_absent_check_looks_like_passed_check]]」
的形状：没执行的那一侧长得和执行过一模一样。
"""
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


def test_a_resume_that_silently_started_fresh_says_so(tmp_path: Path) -> None:
    bootstrap()
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    captured = _run_capturing_state(runs, "run_that_was_never_written")
    state = captured["state"]

    assert captured["run_id"] != "run_that_was_never_written", (
        "前提没成立：这条用例要的是 reopen 失败后退回新开的那一支"
    )
    outcome = state.hook_state.get("_resume_outcome")
    assert outcome, "换了 run 却什么都没记下来"
    assert outcome["requested_run_id"] == "run_that_was_never_written"
    assert outcome["resumed"] is False
    assert outcome["reason"], "说不出为什么没续上，等于只说了『换了』"

    import json

    events = [
        json.loads(line)["event"]
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "resume_failed_started_fresh" in events, (
        f"新 run 的 transcript 里没有『曾请求续跑』：{events}"
    )


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


def test_the_caller_facing_result_carries_the_failed_resume() -> None:
    """summary 里记下了还不够 —— run_node 给调用方的那份精简返回要点它的名。

    那张返回是**按名点收**的：不点名，summary 里写得再清楚也到不了调用方。
    """
    import inspect

    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod)
    assert '"resume_failed"' in src, (
        "run_node 的返回里没有 resume_failed —— 事实停在 summary.json 里，"
        "而做决定的是拿到返回值的那一方"
    )

    import core.executor as executor_mod

    assert '"resume": dict(state.hook_state.get("_resume_outcome")' in \
        inspect.getsource(executor_mod), "summary 没带上续跑结果"
