"""Explicit resume retains the existing run directory and saved messages."""
from __future__ import annotations

import json
from pathlib import Path

from core.state import State


def _checkpoint(run_dir: Path, *, turn: int, texts: list[str]) -> None:
    (run_dir / "messages_checkpoint.json").write_text(json.dumps({
        "turn": turn,
        "written_at": "2026-08-18T00:00:00Z",
        "messages": [{"role": "system", "content": "旧的 system prompt"}]
        + [{"role": "assistant", "content": t} for t in texts],
    }, ensure_ascii=False), encoding="utf-8")








def test_reopen_keeps_the_same_run_id_and_appends_to_the_same_transcript(
    tmp_path: Path,
) -> None:
    """续跑不是新开：同 run_id、同目录、transcript 继续追加。"""
    from core.state import State

    first = State.new(node_type="_curator", base_dir=tmp_path / "runs")
    first.append_transcript("run_start", node_type="_curator")
    again = State.reopen(
        node_type="_curator", base_dir=tmp_path / "runs", run_id=first.run_id
    )
    assert again.run_id == first.run_id
    assert again.root == first.root
    again.append_transcript("run_resumed_after_interruption", resumed_from_turn=12)
    events = [
        json.loads(line)["event"]
        for line in first.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events == ["run_start", "run_resumed_after_interruption"]


def test_executor_restores_checkpoint_messages_on_resume(tmp_path: Path) -> None:
    """executor 的 resume 分支：system 用当前的，历史接 checkpoint，并追加续跑指令。"""
    import asyncio
    from unittest.mock import patch

    from core.bootstrap import bootstrap
    from core.state import State

    bootstrap()
    runs = tmp_path / "runs"
    seed = State.new(node_type="_curator", base_dir=runs)
    seed.append_transcript("run_start", node_type="_curator")
    _checkpoint(seed.root, turn=7, texts=["我已经把 Q1 命题注册进 KB 了"])

    captured: dict = {}

    async def _fake_run_loop(harness, state, messages, llm):
        captured["messages"] = messages
        captured["run_id"] = state.run_id
        raise RuntimeError("stop here — 只验证入口装配")

    with patch("core.executor.run_loop", _fake_run_loop):
        try:
            asyncio.run(_execute(runs, seed.run_id))
        except Exception:
            pass

    assert captured.get("run_id") == seed.run_id, "续跑必须是同一个 run"
    texts = [m.content or "" for m in captured["messages"]]
    assert any("我已经把 Q1 命题注册进 KB 了" in t for t in texts), "没接上 checkpoint 历史"
    assert any("接着往下做" in t for t in texts), "没给出续跑指令"
    assert any("不幂等" in t for t in texts), "没警示重复副作用"


async def _execute(runs: Path, run_id: str):
    from core.executor import execute_node

    return await execute_node(
        "_curator", state_dir=runs, project_id=None, resume_run_id=run_id,
    )
