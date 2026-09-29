"""run_node.py CLI 行为：argparse + --no-interactive + sandbox + 异常分支。

不调真 LLM —— monkeypatch execute_node 返预设 summary。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


# 测试模块加载（不调 main）
def test_run_node_module_imports():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_run_node_under_test",
        Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 关键 attrs 存在
    assert hasattr(mod, "_main")
    assert hasattr(mod, "_drive_with_interaction")
    assert hasattr(mod, "_fake_orchestrator_handle_interrupt")
    assert hasattr(mod, "_CANCEL_KEYWORDS")


def test_cancel_keywords_match_chinese_and_english():
    """fake-orchestrator heuristic 关键词检测覆盖中英文常见说法。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cancel_phrases = [
        "停了吧", "算了", "取消", "别干这个了", "stop", "cancel",
        "Cancel this", "halt", "abort it", "kill",
    ]
    not_cancel = [
        "改成密度 0.95", "应该用 BAOAB", "再加一组", "用 NVT ensemble",
        "继续",  # don't include stop in 继续
    ]
    for p in cancel_phrases:
        assert mod._CANCEL_KEYWORDS.search(p), f"应识别 cancel：{p!r}"
    for p in not_cancel:
        assert not mod._CANCEL_KEYWORDS.search(p), f"误判 cancel：{p!r}"


@pytest.mark.asyncio
async def test_run_node_no_interactive_with_paused_summary_returns_quickly(
    tmp_path: Path, monkeypatch,
):
    """--no-interactive 模式下，paused summary 不进 driver，直接返。"""
    # 导入模块
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    paused_summary = {
        "run_id": "fake_run", "node_type": "literature",
        "status": "paused", "state_dir": str(tmp_path),
        "paused_run_id": "fake_run",
        "missing_required_outputs": [], "turns": 1,
        "tool_call_count": 0, "artifacts": [],
        "final_text_preview": "",
        "pause_event": {},
    }
    # _drive_with_interaction with no_interactive=True 直接返
    result = await mod._drive_with_interaction(paused_summary, no_interactive=True)
    assert result["status"] == "paused"


@pytest.mark.asyncio
async def test_run_node_no_interactive_completed_passes_through(tmp_path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    completed_summary = {
        "run_id": "x", "node_type": "literature", "status": "completed",
        "state_dir": str(tmp_path), "missing_required_outputs": [],
        "turns": 1, "tool_call_count": 0, "artifacts": [],
        "final_text_preview": "ok",
    }
    result = await mod._drive_with_interaction(
        completed_summary, no_interactive=False,
    )
    # 已完成 → 直接返不进 driver
    assert result == completed_summary


@pytest.mark.asyncio
async def test_fake_orchestrator_routes_pause_answer(tmp_path: Path):
    """paused state → user 输入路由到 pause_answer_queue（不被识别为 cancel）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    paused_event = asyncio.Event()
    paused_event.set()
    queue: asyncio.Queue[str] = asyncio.Queue()
    await mod._fake_orchestrator_handle_interrupt(
        "我的答复", paused_event=paused_event, pause_answer_queue=queue,
    )
    got = await queue.get()
    assert got == "我的答复"


@pytest.mark.asyncio
async def test_fake_orchestrator_inject_branch(tmp_path: Path):
    """非 cancel 关键词 → inject_into_node 工具被调用，写 child hook_state。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import (
        ActiveRunInfo, clear_all, find_child_state, register_active,
    )
    from core.state import State

    bootstrap()
    clear_all()
    try:
        s = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=s.run_id, node_type="literature", state=s,
        ))
        paused_event = asyncio.Event()  # 未 set
        queue: asyncio.Queue[str] = asyncio.Queue()
        await mod._fake_orchestrator_handle_interrupt(
            "网格再加密一倍",
            paused_event=paused_event, pause_answer_queue=queue,
        )
        # 没路由到 pause queue
        assert queue.empty()
        # inject 进了 child hook_state
        injected = s.hook_state.get("injected_messages") or []
        assert len(injected) == 1
        assert "网格再加密一倍" in injected[0]["content"]
        assert injected[0]["source"] == "test_fake_orchestrator"
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_fake_orchestrator_wraps_raw_user_text_into_directive(tmp_path: Path):
    """★ 验证 _fake_orchestrator 把 raw user 原话包装成"权威指令"形态。

    背景：v2.x dogfood 实测 raw user 原话直接 inject 给 reasoning model 会被
    "noted but continue plan" 无视（截图 run 1780910300-3282c5）。生产 chat.py
    路径靠 orchestrator LLM 实时翻译；run_node.py 调试模式没 LLM，靜態包裝
    一层"必须立即响应 + 处理要求"模拟同样效果。framework agent_loop 不再二次
    包装，保持 caller 责任原则。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import ActiveRunInfo, clear_all, register_active
    from core.state import State

    bootstrap()
    clear_all()
    try:
        s = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=s.run_id, node_type="literature", state=s,
        ))
        await mod._fake_orchestrator_handle_interrupt(
            "是不是有之前编译残留？",  # ← 截图里 user 实际打的话
            paused_event=asyncio.Event(),
            pause_answer_queue=asyncio.Queue(),
        )

        injected = s.hook_state.get("injected_messages") or []
        assert len(injected) == 1
        content = injected[0]["content"]

        # ① 原文必须保留（不能被包装吞掉）
        assert "是不是有之前编译残留？" in content

        # ② 必须有"权威指令"包装层：强 wording 标记
        assert "中途打断" in content or "必须立即响应" in content, (
            f"应有'中途打断/必须立即响应'强 wording 让 LLM 当 user command 而非 metadata"
            f"\n实际: {content[:300]}"
        )

        # ③ 必须有处理指引（让 LLM 知道这条不能 'noted' 跳过）
        assert "不允许" in content or "处理要求" in content, (
            f"应有处理指引 + 禁止 noted 跳过\n实际: {content[:300]}"
        )

        # ④ 原话应该被显式 labelled 为"用户原话"（让 LLM 区分 directive 跟 原文）
        assert "用户原话" in content, (
            f"原话该有'用户原话：'标签让 LLM 看清是 user input\n实际: {content[:300]}"
        )

        # ⑤ source 标记不变
        assert injected[0]["source"] == "test_fake_orchestrator"
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_fake_orchestrator_wrapping_is_run_node_only_not_framework(tmp_path: Path):
    """★ framework agent_loop 不二次包装 —— 直接 inject 不经过 _fake_orchestrator
    的 content 应该保持原样（caller 责任：传"权威指令"形态）。

    防止以后误把包装层挪进 core/agent_loop.py 影响生产 chat.py 路径
    （orchestrator 已经包装过，再二次包装会臃肿）。
    """
    from core.bootstrap import bootstrap
    from core.harness import NodeHarness
    from core.agent_loop import run_loop
    from core.llm import LLMResponse, LLMMessage, is_framework_notice
    from core.state import State

    bootstrap()
    state = State.new(node_type="literature", base_dir=tmp_path)
    harness = NodeHarness(node_type="literature", system_prompt="t",
                           tools=[], max_turns=2)

    class _StubLLM:
        def __init__(self):
            self.calls = []
        async def chat(self, messages, **kw):
            self.calls.append(list(messages))
            return LLMResponse(content="done", tool_calls=[],
                                 finish_reason="stop", usage={})

    # 模拟 chat.py 里 orchestrator 已经包装过的 inject content
    pre_wrapped = "[orchestrator]: 用户要求改用 BAOAB 算法重跑实验。"
    state.hook_state["injected_messages"] = [
        {"content": pre_wrapped, "source": "orchestrator"},
    ]
    llm = _StubLLM()
    await run_loop(harness, state, [], llm)

    # 检查 LLM 看到的 messages：framework 只加 framework-notice 信封 +
    # "📨 调度器中途注入" 前缀，不应改写 caller 给的 content
    msgs = llm.calls[0]
    inject_msgs = [m for m in msgs
                   if is_framework_notice(m) and "调度器中途注入" in (m.content or "")]
    assert len(inject_msgs) == 1
    # caller 的原 content 应原样出现（framework 不改写 / 不二次包装）
    assert pre_wrapped in inject_msgs[0].content, (
        "framework agent_loop 不应改写 caller 给的 content。"
        "如果你想加包装，做在 caller 层（_fake_orchestrator 或 chat.py 的 orchestrator）。"
    )


@pytest.mark.asyncio
async def test_fake_orchestrator_cancel_branch(tmp_path: Path):
    """cancel 关键词 → cancel_node 工具被调用，写 child kill_signal。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import ActiveRunInfo, clear_all, register_active
    from core.state import State

    bootstrap()
    clear_all()
    try:
        s = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=s.run_id, node_type="literature", state=s,
        ))
        paused_event = asyncio.Event()
        queue: asyncio.Queue[str] = asyncio.Queue()
        await mod._fake_orchestrator_handle_interrupt(
            "停了吧 思路不对",
            paused_event=paused_event, pause_answer_queue=queue,
        )
        kill = s.hook_state.get("kill_signal")
        assert kill is not None
        assert "停了吧" in kill["reason"]
        assert "test-mode" in kill["reason"]
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_race_main_task_routes_input_to_inject_while_running(tmp_path: Path):
    """★ 修 #N regression：execute_node 跑中 user 输入 → inject 进 child hook_state。

    旧 bug：execute_node 是 await 阻塞跑完才进 _drive_with_interaction，
    stdin listener 没机会监听 → 节点跑中 user 输入完全被吞。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import (
        ActiveRunInfo, clear_all, register_active,
    )
    from core.state import State

    bootstrap()
    clear_all()
    try:
        # 模拟 child 在跑（注册到 active runs registry）
        child_state = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=child_state.run_id, node_type="literature",
            state=child_state,
        ))

        # 模拟一个慢跑的 execute_node task
        async def slow_execute():
            await asyncio.sleep(0.2)
            return {"status": "completed"}
        main_task = asyncio.create_task(slow_execute())

        # 准备共享 queues
        input_queue: asyncio.Queue[str] = asyncio.Queue()
        paused_event = asyncio.Event()
        pause_answer_queue: asyncio.Queue[str] = asyncio.Queue()

        # 在 race 跑起来后异步往 queue 注两条 user 输入
        async def feed_inputs():
            await asyncio.sleep(0.05)
            await input_queue.put("改用 BAOAB 算法")
            await asyncio.sleep(0.02)
            await input_queue.put("再加密网格 2x")
        feeder = asyncio.create_task(feed_inputs())

        await mod._race_main_task_with_input(
            main_task, input_queue,
            paused_event=paused_event,
            pause_answer_queue=pause_answer_queue,
        )
        await feeder

        # main task 跑完
        assert main_task.done()
        result = main_task.result()
        assert result == {"status": "completed"}

        # 两条 user 输入都 inject 进了 child hook_state（不被吞）
        injected = child_state.hook_state.get("injected_messages") or []
        assert len(injected) == 2, f"应有 2 条 inject，实际 {len(injected)}"
        assert "BAOAB" in injected[0]["content"]
        assert "网格" in injected[1]["content"]
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_race_main_task_routes_cancel_keyword_to_kill_signal(tmp_path: Path):
    """★ 修 #N regression：execute_node 跑中 user 输入 cancel 词 → 写 kill_signal。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import ActiveRunInfo, clear_all, register_active
    from core.state import State

    bootstrap()
    clear_all()
    try:
        child_state = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=child_state.run_id, node_type="literature",
            state=child_state,
        ))

        async def slow_execute():
            await asyncio.sleep(0.15)
            return {"status": "completed"}
        main_task = asyncio.create_task(slow_execute())

        input_queue: asyncio.Queue[str] = asyncio.Queue()
        paused_event = asyncio.Event()
        pause_answer_queue: asyncio.Queue[str] = asyncio.Queue()

        async def feed():
            await asyncio.sleep(0.03)
            await input_queue.put("停了吧 思路不对")
        feeder = asyncio.create_task(feed())

        await mod._race_main_task_with_input(
            main_task, input_queue,
            paused_event=paused_event,
            pause_answer_queue=pause_answer_queue,
        )
        await feeder

        kill = child_state.hook_state.get("kill_signal")
        assert kill is not None
        assert "停了吧" in kill["reason"]
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_race_main_task_interrupt_sentinel_ignored(tmp_path: Path):
    """__INTERRUPT__ sentinel（Ctrl-C / EOF）不应触发 inject，只打提示。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from core.bootstrap import bootstrap
    from core.pause import ActiveRunInfo, clear_all, register_active
    from core.state import State

    bootstrap()
    clear_all()
    try:
        child_state = State.new(node_type="literature", base_dir=tmp_path)
        register_active(ActiveRunInfo(
            run_id=child_state.run_id, node_type="literature",
            state=child_state,
        ))

        async def slow_execute():
            await asyncio.sleep(0.1)
            return {"status": "completed"}
        main_task = asyncio.create_task(slow_execute())

        input_queue: asyncio.Queue[str] = asyncio.Queue()
        paused_event = asyncio.Event()
        pause_answer_queue: asyncio.Queue[str] = asyncio.Queue()

        async def feed():
            await asyncio.sleep(0.02)
            await input_queue.put("__INTERRUPT__")
        feeder = asyncio.create_task(feed())

        await mod._race_main_task_with_input(
            main_task, input_queue,
            paused_event=paused_event,
            pause_answer_queue=pause_answer_queue,
        )
        await feeder

        # __INTERRUPT__ 不应被当作 user 输入 inject
        assert not child_state.hook_state.get("injected_messages")
        assert not child_state.hook_state.get("kill_signal")
    finally:
        clear_all()


@pytest.mark.asyncio
async def test_fake_orchestrator_empty_input_ignored(tmp_path: Path):
    """空输入不触发任何动作。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rn", Path(__file__).parent.parent / "run_node.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    paused_event = asyncio.Event()
    paused_event.set()
    queue: asyncio.Queue[str] = asyncio.Queue()
    # 空 / 空格
    for empty in ["", "   ", "\t\n"]:
        await mod._fake_orchestrator_handle_interrupt(
            empty, paused_event=paused_event, pause_answer_queue=queue,
        )
    assert queue.empty()
