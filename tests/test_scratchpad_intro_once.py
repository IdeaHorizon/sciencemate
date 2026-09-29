"""白板 hook：引导每个 state 生命周期只注入一次（P0-4），之后注入板子本身。

引导原来用 `ctx.turn == 1` 触发。producing 节点每次 run 是 fresh state（turn 从 1
起，引导正好一次），没问题。但 _orchestrator 是**长 session、单一 state**，
chat.py 每条用户消息都新起一次 run_loop → ctx.turn 每条消息都从 1 重置，而白板
一直空（对话场景很少写），于是引导**每条消息都重注入一次**（实测：叠加端点复读，
引导全文被逐字抄进回复三次）。改用 state 级持久 flag。
"""
from __future__ import annotations

from pathlib import Path

from core.harness import NodeHarness
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import _scratchpad_on_turn_start
from core.state import State


def _ctx(state: State, turn: int) -> HookContext:
    return HookContext(harness=NodeHarness(node_type="_orchestrator"),
                       state=state, messages=[], turn=turn)


def _state(tmp_path: Path, name: str = "rt") -> State:
    return State.new("literature", tmp_path / name, project_id="p")


def test_intro_injected_once_per_state(tmp_path):
    state = _state(tmp_path)
    first = _scratchpad_on_turn_start(_ctx(state, turn=1))
    assert first and "白板" in first[0].content
    # 后续消息（还是 turn=1，白板仍空）→ 不再重注入引导
    for _ in range(3):
        again = _scratchpad_on_turn_start(_ctx(state, turn=1))
        assert again is None or "开场引导" not in (again[0].content or "")


def test_board_injected_after_intro(tmp_path):
    state = _state(tmp_path)
    _scratchpad_on_turn_start(_ctx(state, turn=1))          # 消费掉 intro
    state.scratchpad = "记住：H1 用 synthetic 数据验证协议"
    state.scratchpad_revised_turn = 2
    out = _scratchpad_on_turn_start(_ctx(state, turn=2))
    assert out and "synthetic 数据验证协议" in out[0].content


def test_board_injection_carries_its_age(tmp_path):
    """注入带"上次改写在第几轮" —— 只陈述事实，判决归进展熔断。"""
    state = _state(tmp_path)
    state.scratchpad = "卡在 LAMMPS 编译"
    state.scratchpad_revised_turn = 10
    out = _scratchpad_on_turn_start(_ctx(state, turn=42))
    assert out
    assert "turn 10" in out[0].content
    assert "32 轮" in out[0].content


def test_each_state_gets_its_own_intro(tmp_path):
    s1, s2 = _state(tmp_path, "a"), _state(tmp_path, "b")
    assert _scratchpad_on_turn_start(_ctx(s1, turn=1)) is not None
    assert _scratchpad_on_turn_start(_ctx(s2, turn=1)) is not None


def test_injection_size_is_bounded_by_capacity(tmp_path):
    """这是整改的核心不变量：注入体积**与写了多少次无关**。

    旧设计（append 日志 + 每轮全量注入）实测涨到 639 条 / 50KB，把上下文挤爆，
    模型连着 591 轮只写笔记不干活。现在容量是硬上限，写入口就拒绝，所以注入
    体积由构造有界。
    """
    from core import whiteboard

    state = _state(tmp_path)
    for i in range(200):                       # 写 200 次
        whiteboard.write(state, f"当前状态：第 {i} 次改写；下一步跑 experiment", turn=i)
    out = _scratchpad_on_turn_start(_ctx(state, turn=201))
    assert out
    assert whiteboard.measure(out[0].content) <= whiteboard.capacity_tokens() + 120
