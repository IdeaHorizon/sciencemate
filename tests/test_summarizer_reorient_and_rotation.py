"""P4 压后再定向 + P6 机械换届保险丝。

P4：压缩后注入当刻扫盘快照，旧定向注入被**取代**而不是并排堆着
（notice 累积 bug 的同款纪律）。事实以重扫为准，账本只管叙事。
P6：压缩已证明压不回窗口时 fail-loud —— 结构化事件 + 收尾建议注入，
不自动 publish（那是带科学门语义的平台决定）。v20 实测这个事实被沉默
吞掉的代价是 orchestrator 白磨 5 小时。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import summarizer as sm
from core.harness import SummarizerConfig
from core.llm import LLMMessage
from core.llm import framework_notice_body
from core.loop_hooks_builtin import ORIENTATION_PREFIX


class _State:
    def __init__(self, worktree=None):
        self.hook_state: dict = {}
        self.tokens_used = 0
        self.project_worktree = worktree
        self.transcript: list = []

    def append_transcript(self, event, **kw):
        self.transcript.append((event, kw))

    def save_artifact(self, **kw):
        return {"id": "x"}


def _worktree(tmp_path: Path) -> Path:
    """一份 experiment 记录在工作区账本上 —— 重扫读的是账本，不是目录。"""
    from core.ledger import write_record

    root = tmp_path / "wt"
    root.mkdir()
    write_record(root, artifact_type="clean_results", name="demo", content="{}",
                 directory="experiments",
                 produced_by_node_type="experiment", produced_by_run_id="r-exp")
    return root


def _msgs(n=8, chars=9_000):
    out = [LLMMessage(role="system", content="SYS"),
           LLMMessage(role="system", content=f"{ORIENTATION_PREFIX}（机械扫描）** 旧的开局定向"),
           LLMMessage(role="user", content="go")]
    for i in range(n):
        out.append(LLMMessage(role="assistant", content=f"想法 {i} " + "y" * chars))
        out.append(LLMMessage(role="user", content=f"回 {i}"))
    return out


def _harness(window=10_000, strategy="drop_tool_results"):
    class _H:
        node_type = "_test"
        max_context_tokens = window
        summarizer = SummarizerConfig(strategy=strategy)
    return _H()


def _compress(state, msgs, window=10_000):
    return asyncio.run(sm.run_summarizer(
        _harness(window), state, msgs, llm=None, turn=5,
        estimated_tokens=sm.estimate_tokens(msgs)))


# ── P4 ───────────────────────────────────────────────────────────────────
def test_fresh_orientation_replaces_the_old_one(tmp_path: Path) -> None:
    state = _State(worktree=_worktree(tmp_path))
    out = _compress(state, _msgs())
    orients = [m for m in out if framework_notice_body(m).startswith(ORIENTATION_PREFIX)]
    assert len(orients) == 1, "旧定向必须被取代，不是并排堆着"
    assert "压缩后重扫" in orients[0].content
    assert "clean_results__demo" in orients[0].content, "快照必须是当刻扫盘"
    assert ("orientation_refreshed_after_compress" in
            [e for e, _ in state.transcript])


def test_orientation_does_not_accumulate_across_compressions(tmp_path: Path) -> None:
    state = _State(worktree=_worktree(tmp_path))
    out = _compress(state, _msgs())
    out2 = _compress(state, out + [
        LLMMessage(role="assistant", content="新工作 " + "z" * 30_000)])
    orients = [m for m in out2 if framework_notice_body(m).startswith(ORIENTATION_PREFIX)]
    assert len(orients) == 1


def test_no_worktree_no_orientation_refresh(tmp_path: Path) -> None:
    state = _State(worktree=None)
    out = _compress(state, _msgs())
    assert not any("压缩后重扫" in (m.content or "") for m in out
                   if framework_notice_body(m).startswith(ORIENTATION_PREFIX))


# ── P6 ───────────────────────────────────────────────────────────────────
def test_rotation_advised_when_compression_cannot_win(tmp_path: Path) -> None:
    """窗口小到压完仍超 90% → 必须亮牌，且只亮一次。"""
    state = _State()
    msgs = _msgs(n=12)
    out = _compress(state, msgs, window=1_000)   # 压完必然还是远超 1k
    advisory = [m for m in out if "压缩已到极限" in (m.content or "")]
    assert len(advisory) == 1
    assert "publish" in advisory[0].content
    assert ("session_rotation_advised" in [e for e, _ in state.transcript])

    out2 = _compress(state, out, window=1_000)
    assert sum(1 for m in out2 if "压缩已到极限" in (m.content or "")) == 1, \
        "同一 run 只建议一次，不刷屏"


def test_no_rotation_noise_when_compression_succeeds(tmp_path: Path) -> None:
    state = _State()
    out = _compress(state, _msgs(), window=200_000)
    assert not any("压缩已到极限" in (m.content or "") for m in out)
    assert "session_rotation_advised" not in [e for e, _ in state.transcript]
