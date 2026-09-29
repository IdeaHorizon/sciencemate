"""终态三分型（#735）+ 转轮一等字段（#733 第一步）。

判据全部落在**真 run_loop** 的出口与 transcript 上，不落在文案上：
  - 每条终止路径的 LoopResult 带 terminal_kind/cause，且 status 是表的投影
  - 每次 run_loop 返回留一条 loop_finished
  - 「这一轮为什么没停」有 turn_transition 事件可断言

（教训来源：右判决错路径 —— 只断言 status 分不清"我要测的那条对"和
"另一条碰巧同答案"。cause 是路径的名字。）
"""
from __future__ import annotations

import json

import pytest

from core.agent_loop import (
    _TERMINAL_CAUSES, LoopResult, TERMINAL_CANCELLED, TERMINAL_FAILURE,
    TERMINAL_STOP, TERMINAL_SUSPENDED, run_loop,
)
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


def _events(state, name=None):
    lines = [json.loads(ln) for ln in
             state.transcript_path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return [e for e in lines if name is None or e.get("event") == name]


def _tool_call(i):
    return {
        "id": f"c{i}", "type": "function",
        "function": {"name": "list_artifacts",
                     "arguments": f'{{"artifact_type": "t{i}"}}'},
    }


class _FinishesAfter:
    """先干 n 轮活，然后自然收尾。"""

    def __init__(self, n):
        self.n = n
        self.turn = 0

    async def chat(self, messages, **kw):
        self.turn += 1
        if self.turn > self.n:
            return LLMResponse(content="做完了。", tool_calls=[],
                               finish_reason="stop", usage={})
        return LLMResponse(content=f"第 {self.turn} 步",
                           tool_calls=[_tool_call(self.turn)],
                           finish_reason="tool_calls", usage={})


def _harness(**kw):
    kw.setdefault("node_type", "literature")
    kw.setdefault("system_prompt", "t")
    kw.setdefault("tools", ["list_artifacts"])
    return NodeHarness(**kw)


# ─────────────────────────────────────────────────────────────────────────────
# 投影表本身
# ─────────────────────────────────────────────────────────────────────────────


def test_every_cause_maps_to_a_known_kind():
    kinds = {TERMINAL_STOP, TERMINAL_CANCELLED, TERMINAL_FAILURE,
             TERMINAL_SUSPENDED}
    for cause, (kind, status) in _TERMINAL_CAUSES.items():
        assert kind in kinds, cause
        assert status in {"completed", "cancelled", "failed", "void", "paused"}


def test_status_is_a_projection_not_a_second_opinion():
    """显式 status 与表不一致必须当场炸，不允许静默分叉。"""
    with pytest.raises(ValueError):
        LoopResult(final_text="", turns=1, status="failed",
                   terminal_cause="model_finished")
    with pytest.raises(KeyError):
        LoopResult(final_text="", turns=1, terminal_cause="not_registered")


def test_unstamped_legacy_construction_still_works():
    r = LoopResult(final_text="", turns=0)
    assert r.terminal_kind == "" and r.terminal_cause == ""


# ─────────────────────────────────────────────────────────────────────────────
# 真 run_loop：各终止路径的盖章
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_finished_is_stop(tmp_path):
    state = State.new("literature", tmp_path / "r1", project_id="p")
    result = await run_loop(_harness(max_turns=20), state,
                            [LLMMessage(role="user", content="go")],
                            _FinishesAfter(2))
    assert result.status == "completed"
    assert result.terminal_kind == TERMINAL_STOP
    assert result.terminal_cause == "model_finished"

    fin = _events(state, "loop_finished")
    assert len(fin) == 1
    assert fin[0]["terminal_cause"] == "model_finished"

    # 干活的两轮各留一条正常转轮记录，且 n_tool_calls 对得上
    trans = _events(state, "turn_transition")
    assert [t["reason"] for t in trans] == ["tool_calls_executed"] * 2
    assert all(t["n_tool_calls"] == 1 for t in trans)


@pytest.mark.asyncio
async def test_turn_cap_is_cancelled_not_stop(tmp_path):
    """#735 的核心病例：max_turns 截断在旧词表里叫 completed。
    旧字节冻结不变（老读者不迁移不改），但 kind 必须说真话。"""
    state = State.new("literature", tmp_path / "r2", project_id="p")
    result = await run_loop(_harness(max_turns=2), state,
                            [LLMMessage(role="user", content="go")],
                            _FinishesAfter(99))
    assert result.status == "completed"          # 旧词表投影，字节不变
    assert result.terminal_kind == TERMINAL_CANCELLED
    assert result.terminal_cause == "turn_cap"
    fin = _events(state, "loop_finished")
    assert fin and fin[0]["terminal_kind"] == TERMINAL_CANCELLED


@pytest.mark.asyncio
async def test_external_kill_is_cancelled(tmp_path):
    state = State.new("literature", tmp_path / "r3", project_id="p")
    state.hook_state["kill_signal"] = {"reason": "test", "requested_by": "t"}
    result = await run_loop(_harness(max_turns=5), state,
                            [LLMMessage(role="user", content="go")],
                            _FinishesAfter(99))
    assert result.status == "cancelled"
    assert result.terminal_cause == "external_kill"
    assert _events(state, "loop_finished")[0]["terminal_kind"] \
        == TERMINAL_CANCELLED


@pytest.mark.asyncio
async def test_truncation_recovery_transition_is_recorded(tmp_path):
    """恢复路径触发过 —— 从此是一条可断言的事件，不是翻正文猜。"""

    class _TruncatedOnce:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                # 撞输出上限且零调用：内容非复读（否则走退化熔断那条腿）
                text = " ".join(f"独立片段{i}词汇各不相同" for i in range(60))
                return LLMResponse(content=text, tool_calls=[],
                                   finish_reason="length",
                                   usage={"completion_tokens": 999})
            return LLMResponse(content="收尾。", tool_calls=[],
                               finish_reason="stop", usage={})

    state = State.new("literature", tmp_path / "r4", project_id="p")
    result = await run_loop(_harness(max_turns=10), state,
                            [LLMMessage(role="user", content="go")],
                            _TruncatedOnce())
    assert result.terminal_cause == "model_finished"
    reasons = [t["reason"] for t in _events(state, "turn_transition")]
    assert "truncation_recovery" in reasons


@pytest.mark.asyncio
async def test_progress_break_is_failure(tmp_path):
    class _BrokenRecord:
        async def chat(self, messages, **kw):
            return LLMResponse(content="同一句话。",
                               tool_calls=[{
                                   "id": "call_same", "type": "function",
                                   "function": {"name": "list_artifacts",
                                                "arguments": "{}"},
                               }],
                               finish_reason="tool_calls", usage={})

    state = State.new("literature", tmp_path / "r5", project_id="p")
    result = await run_loop(_harness(max_turns=60), state,
                            [LLMMessage(role="user", content="go")],
                            _BrokenRecord())
    assert result.status == "failed"
    assert result.terminal_kind == TERMINAL_FAILURE
    assert result.terminal_cause == "progress_circuit_break"
