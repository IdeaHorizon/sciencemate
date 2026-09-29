"""reflection hook + /btw 注入单元 smoke test。不联网。

测试点：
  1. reflection hook 在 turn % N == 0 时注入消息
  2. reflection hook 在其它 turn 不注入
  3. 自定义 prompt 被 format
  4. /btw 注入逻辑：_run_one_turn 消费 pending_btw_injection 后清空
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap

bootstrap()

from core.harness import NodeHarness  # noqa: E402
from core.llm import LLMMessage  # noqa: E402
from core.loop_hooks import HookContext, get_loop_hook  # noqa: E402
from core.state import State  # noqa: E402


def make_state(td: Path) -> State:
    return State.new(node_type="test", base_dir=td)


def test_reflection_fires_at_n():
    """turn=4 触发；turn=3 / turn=5 不触发（默认 N=4）。"""
    hook = get_loop_hook("reflection")
    assert hook is not None and hook.on_turn_end is not None

    harness = NodeHarness(node_type="test", max_turns=20)
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        for turn, expected in [(3, False), (4, True), (5, False), (8, True), (20, False)]:
            ctx = HookContext(harness=harness, state=state, messages=[], turn=turn)
            result = hook.on_turn_end(ctx)
            got = bool(result)
            assert got == expected, f"turn={turn} 期望={expected} 但得到={got}"
            if got:
                msg = result[0]
                assert "反思检查" in msg.content
                assert f"已经跑了 {turn}" in msg.content
        print("  ✓ reflection 在 turn % 4 == 0 触发（且不在 max_turns 上触发）")


def test_reflection_custom_n():
    """hook_config.reflection.every_n_turns=2 → 偶数 turn 触发。"""
    hook = get_loop_hook("reflection")
    harness = NodeHarness(
        node_type="test", max_turns=20,
        hook_config={"reflection": {"every_n_turns": 2}},
    )
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        for turn, expected in [(1, False), (2, True), (3, False), (4, True)]:
            ctx = HookContext(harness=harness, state=state, messages=[], turn=turn)
            result = hook.on_turn_end(ctx)
            assert bool(result) == expected
        print("  ✓ every_n_turns=2 起作用")


def test_reflection_custom_prompt():
    """自定义 prompt 含 {turn} 占位。"""
    hook = get_loop_hook("reflection")
    harness = NodeHarness(
        node_type="test", max_turns=20,
        hook_config={
            "reflection": {
                "every_n_turns": 1,
                "prompt": "我的自定义反思：现在是 turn {turn}（窗口 {window}）。",
            },
        },
    )
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        ctx = HookContext(harness=harness, state=state, messages=[], turn=3)
        result = hook.on_turn_end(ctx)
        assert result
        assert "我的自定义反思" in result[0].content
        assert "turn 3" in result[0].content
        print("  ✓ 自定义 prompt 被 format 渲染")


def test_reflection_disabled_when_n_zero():
    """every_n_turns=0 完全不触发。"""
    hook = get_loop_hook("reflection")
    harness = NodeHarness(
        node_type="test", max_turns=20,
        hook_config={"reflection": {"every_n_turns": 0}},
    )
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        for turn in [1, 4, 8, 16]:
            ctx = HookContext(harness=harness, state=state, messages=[], turn=turn)
            result = hook.on_turn_end(ctx)
            assert not result
        print("  ✓ every_n_turns=0 完全禁用")


def test_btw_consumed_in_run_one_turn():
    """模拟 _run_one_turn 的 /btw 消费逻辑（不调真实 LLM，只验证 pending 被弹出）。"""
    import chat as chat_mod

    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state["pending_btw_injection"] = "注意 X 主题"

        # 模拟 _run_one_turn 的前半段（只到注入 + user 加入，不跑 run_loop）
        messages: list[LLMMessage] = []
        pending_btw = state.hook_state.pop("pending_btw_injection", None)
        assert pending_btw == "注意 X 主题"
        if pending_btw:
            messages.append(LLMMessage(
                role="system",
                content=f"📨 用户额外提示（/btw）：{pending_btw}",
            ))
        messages.append(LLMMessage(role="user", content="正常 user 输入"))

        # 验证：注入了 system + user 共 2 条；pending 被消费
        assert len(messages) == 2
        assert messages[0].role == "system" and "/btw" in messages[0].content
        assert "pending_btw_injection" not in state.hook_state
        print("  ✓ /btw payload 在下一轮被消费 + 清空 pending")


def test_btw_no_double_inject_if_empty():
    """没有 pending_btw 时不注入系统消息。"""
    import chat as chat_mod

    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        messages: list[LLMMessage] = []
        pending_btw = state.hook_state.pop("pending_btw_injection", None)
        assert pending_btw is None
        if pending_btw:
            messages.append(LLMMessage(role="system", content="不该被加进来"))
        messages.append(LLMMessage(role="user", content="hi"))
        assert len(messages) == 1
        print("  ✓ 没有 pending 时不注入")


if __name__ == "__main__":
    print("== reflection + /btw smoke tests ==")
    tests = [
        test_reflection_fires_at_n,
        test_reflection_custom_n,
        test_reflection_custom_prompt,
        test_reflection_disabled_when_n_zero,
        test_btw_consumed_in_run_one_turn,
        test_btw_no_double_inject_if_empty,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print("\nALL PASS ✓")
