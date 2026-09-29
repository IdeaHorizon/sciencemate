"""可逆清除层：压缩的第一层，无损（内容可凭指针恢复）。

真实回放数据（scripts/replay_summarizer_on_checkpoints.py，12 个最大 checkpoint）：
8 个 47-51 万 tokens 的 checkpoint 全部清回 12 万窗口内，销毁事实 0；
旧 drop_tool_results 在其中 4 个上完全无效（mid=0 退化），另 2 个无指针销毁
41/123 个事实。这里把回放中撞出来的每个坑固化成合成用例。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import summarizer as sm
from core.harness import SummarizerConfig
from core.llm import LLMMessage
from core.tool_registry import ToolDefinition, _REGISTRY, register_tool


class _H:
    node_type = "_test"
    max_context_tokens = 10_000
    summarizer = SummarizerConfig()


@pytest.fixture(autouse=True)
def _tools():
    """注册测试工具：一个可重读、一个带 compactor、一个什么都没声明。"""
    async def _noop(state=None, **kw):  # pragma: no cover
        return {}

    for name, kwargs in (
        ("t_read", {"replayable_read": True}),
        ("t_compact", {"result_compactor": lambda c: f"[压缩的 run_node 结果 digest] {c[:30]}"}),
        ("t_opaque", {}),
    ):
        register_tool(ToolDefinition(name=name, description="x",
                                     parameters_schema={"type": "object"}, **kwargs), _noop)
    yield
    for name in ("t_read", "t_compact", "t_opaque"):
        _REGISTRY.tools.pop(name, None)
        _REGISTRY.executors.pop(name, None)


def _msgs(*, big_tool: str, n_turns: int = 6, big_chars: int = 200_000) -> list[LLMMessage]:
    out = [LLMMessage(role="system", content="SYS"), LLMMessage(role="user", content="go")]
    for i in range(n_turns):
        out.append(LLMMessage(role="assistant", content=f"turn {i}",
                              tool_calls=[{"id": f"c{i}", "function": {"name": big_tool, "arguments": "{}"}}]))
        out.append(LLMMessage(role="tool", name=big_tool, tool_call_id=f"c{i}",
                              content=f"payload-{i} " + "x" * big_chars))
    return out


def _run(msgs, harness=None):
    ctx = sm.SummarizerContext(harness=harness or _H(), state=None, messages=msgs,
                               estimated_tokens=sm.estimate_tokens(msgs), llm=None, turn=9)
    return asyncio.run(sm._strategy_clear_tool_results(ctx))


def test_replayable_results_get_cleared_with_pointer() -> None:
    msgs = _msgs(big_tool="t_read")
    out = _run(msgs)
    cleared = [m for m in out if (m.content or "").startswith(sm._CLEARED_MARKER)]
    assert cleared, "可重读结果必须被清"
    # 判据抓语义不抓逐字：墓碑必须说明"原文还在 + 怎么拿回来"。
    assert "没有丢" in cleared[0].content and "再调用一次" in cleared[0].content
    assert sm.estimate_tokens(out) < sm.estimate_tokens(msgs) / 3


def test_structure_and_prefix_are_untouched() -> None:
    """只原位换 content：条数、顺序、tool_call_id 不变；首条被清消息之前逐字节相同。"""
    msgs = _msgs(big_tool="t_read")
    out = _run(msgs)
    assert len(out) == len(msgs)
    assert [m.role for m in out] == [m.role for m in msgs]
    assert [m.tool_call_id for m in out] == [m.tool_call_id for m in msgs]
    first_changed = next(i for i, (a, b) in enumerate(zip(msgs, out)) if a.content != b.content)
    for a, b in zip(msgs[:first_changed], out[:first_changed]):
        assert a.content == b.content and a.tool_calls == b.tool_calls


def test_undeclared_tools_are_never_touched() -> None:
    """没声明恢复方式的工具 fail-closed —— 不清，宁可交给下一层。"""
    msgs = _msgs(big_tool="t_opaque")
    out = _run(msgs)
    assert all((m.content or "") == (n.content or "") for m, n in zip(msgs, out))


def test_error_results_are_preserved() -> None:
    """失败是自我修正的信号，不清（重复失败熔断也依赖它）。"""
    msgs = _msgs(big_tool="t_read")
    err = json.dumps({"status": "error", "error": "boom " + "e" * 50_000})
    msgs[4] = LLMMessage(role="tool", name="t_read", tool_call_id="c1", content=err)
    out = _run(msgs)
    assert out[4].content == err


def test_giant_result_is_not_shielded_by_recency() -> None:
    """回放实测的坑：466k 的单条结果恰好是'该工具最近一条'，按近因保护它
    等于整个策略失效（四个 checkpoint 一条都清不动）。巨物永不受保护。"""
    msgs = _msgs(big_tool="t_read", n_turns=1, big_chars=400_000)
    out = _run(msgs)
    assert any((m.content or "").startswith(sm._CLEARED_MARKER) for m in out)


def test_degenerate_few_turns_still_compresses() -> None:
    """回放实测的坑：498k 的 checkpoint 只有 ≤3 条 assistant —— 按轮保护会把
    全部内容罩住（旧 drop 在它上面 0 压缩）。按 token 保护没有这个退化。"""
    msgs = _msgs(big_tool="t_read", n_turns=2, big_chars=300_000)
    out = _run(msgs)
    assert sm.estimate_tokens(out) < sm.estimate_tokens(msgs) / 3


def test_compactor_digest_is_used() -> None:
    msgs = _msgs(big_tool="t_compact")
    out = _run(msgs)
    assert any((m.content or "").startswith("[压缩的 run_node 结果") for m in out)


def test_idempotent_second_pass_clears_nothing_more() -> None:
    msgs = _msgs(big_tool="t_read")
    once = _run(msgs)
    twice = _run(once)
    assert [m.content for m in twice] == [m.content for m in once]


def test_escalate_skips_llm_when_clearing_suffices() -> None:
    """清够了就不该调 LLM —— llm=None，真调会炸，不炸即证明。"""
    msgs = _msgs(big_tool="t_read")
    ctx = sm.SummarizerContext(harness=_H(), state=None, messages=msgs,
                               estimated_tokens=sm.estimate_tokens(msgs), llm=None, turn=9)
    out = asyncio.run(sm._strategy_escalate(ctx))
    assert sm.estimate_tokens(out) < sm.estimate_tokens(msgs)


def test_run_node_compactor_keeps_decision_facts() -> None:
    from shared.tools.run_node import _compact_run_node_result

    payload = json.dumps({
        "status": "completed", "child_run_id": "1786214798-3781e0",
        "child_node_type": "postprocess",
        "produced_artifacts": [{"id": "figure__x"}, {"id": "figure__y"}],
        "huge": "z" * 50_000,
    })
    digest = _compact_run_node_result(payload)
    assert len(digest) < 700
    for fact in ("completed", "1786214798-3781e0", "figure__x"):
        assert fact in digest


def test_clearing_runs_even_when_harness_pins_a_lossy_strategy(tmp_path) -> None:
    """无损清除不受 harness 的 strategy 影响 —— v22 实测 `_orchestrator` 硬编码
    `strategy: llm`，最需要清除层的节点（中段 93% run_node 结果）完全用不上。
    做成选项 = 名单式护栏，不写就默认漏过。"""
    from core.harness import SummarizerConfig

    class _State:
        def __init__(self):
            self.hook_state = {}
            self.tokens_used = 0
            self.project_worktree = None
            self.events = []
        def append_transcript(self, e, **k): self.events.append(e)
        def save_artifact(self, **k): return {"id": "x"}

    class _Hpinned:
        node_type = "_orchestrator"
        max_context_tokens = 40_000
        summarizer = SummarizerConfig(strategy="llm", trigger_threshold=0.7)

    msgs = _msgs(big_tool="t_read", n_turns=6, big_chars=120_000)
    state = _State()
    out = asyncio.run(sm.run_summarizer(
        _Hpinned(), state, msgs, llm=None, turn=7,
        estimated_tokens=sm.estimate_tokens(msgs)))

    assert any((m.content or "").startswith(sm._CLEARED_MARKER) for m in out), \
        "strategy=llm 也必须先走无损清除"
    # 清够了就不该进有损层（llm=None，真进去会走 fallback 而不是 clear_sufficed）
    assert "summarizer_clear_sufficed" in state.events
    assert sm.estimate_tokens(out) < sm.estimate_tokens(msgs) / 3
