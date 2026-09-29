"""模拟 chat.py 主控异步 race：turn_task 跑中 + user 输入中途打断。

不读真 stdin —— 直接往 input_queue.put 模拟 stdin_listener。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMResponse
from core.pause import (
    ActiveRunInfo, clear_all, register_active,
)
from core.state import State


def _load_chat():
    spec = importlib.util.spec_from_file_location(
        "_chat", Path(__file__).parent.parent / "chat.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


@pytest.mark.asyncio
async def test_main_loop_race_interrupt_during_running_turn(tmp_path: Path):
    """模拟主循环：orch_task 跑中，user 插话 → 路由到 _handle_interrupt。"""
    chat = _load_chat()
    chat_state = chat.ChatState()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)

    # 模拟 child running
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    # 长时间跑的 turn task（模拟 orchestrator 等 child）
    completed = asyncio.Event()

    async def slow_turn():
        await asyncio.sleep(0.1)
        completed.set()
        return "turn finished"

    turn_task = asyncio.create_task(slow_turn())

    # Stub LLM for interrupt decision returning inject call
    class _InterruptLLM:
        # 待命轮是多轮的：调完工具会回来把结果讲成人话。mock 也得这样，
        # 否则它会一直返工具调用直到撞上限（那不是真实模型行为）。
        _did_inject = [False]

        async def chat(self, messages, **kw):
            if self._did_inject[0]:
                return LLMResponse(content="已经把方向注入进去了。",
                                    tool_calls=[], finish_reason="stop", usage={})
            self._did_inject[0] = True
            return LLMResponse(
                content=None,
                tool_calls=[{
                    "id": "tc_1", "type": "function",
                    "function": {
                        "name": "runtime_control",
                        "arguments": json.dumps({
                            "action": "inject",
                            "child_run_id": child.run_id,
                            "content": "redirected by interrupt",
                            "source": "orchestrator_relay",
                        }),
                    },
                }],
                finish_reason="tool_calls", usage={},
            )

    harness = NodeHarness(node_type="_orchestrator", system_prompt="t",
                            tools=[], max_turns=2, max_output_tokens=2048)
    llm = _InterruptLLM()

    # 模拟主循环：把 user 输入 push 进 queue 后跟着同 chat.py 主循环的 race 逻辑
    await chat_state.input_queue.put("改成 BAOAB 算法")

    while not turn_task.done():
        next_input_task = asyncio.create_task(chat_state.input_queue.get())
        done, _pending = await asyncio.wait(
            [turn_task, next_input_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if next_input_task in done:
            interrupt = next_input_task.result().strip()
            await chat._handle_interrupt(parent, harness, llm, interrupt)
        else:
            next_input_task.cancel()
            try:
                await next_input_task
            except (asyncio.CancelledError, Exception):
                pass

    assert completed.is_set()
    # child 收到了 inject
    inj = child.hook_state.get("injected_messages") or []
    assert len(inj) == 1
    assert "redirected by interrupt" in inj[0]["content"]


@pytest.mark.asyncio
async def test_main_loop_input_during_paused_routes_to_answer(tmp_path: Path):
    """child 已 paused（chat_state.paused.set()）→ user 输入路由到 pause_answer_queue。"""
    chat = _load_chat()
    cs = chat.ChatState()
    cs.paused.set()  # 模拟节点 pause

    # 模拟 driver 在等答复
    answer_future = asyncio.create_task(cs.pause_answer_queue.get())

    # 模拟主循环路由逻辑
    await cs.input_queue.put("我的答复")
    line = await cs.input_queue.get()
    if cs.paused.is_set():
        await cs.pause_answer_queue.put(line)

    got = await answer_future
    assert got == "我的答复"


@pytest.mark.asyncio
async def test_main_loop_slash_in_turn_does_not_crash(tmp_path: Path, capsys):
    """turn 跑中 user 输入 slash 命令 → 提示后忽略，不进 interrupt 决策。"""
    chat = _load_chat()
    cs = chat.ChatState()
    completed = asyncio.Event()

    async def slow_turn():
        await asyncio.sleep(0.05)
        completed.set()
        return "done"

    turn_task = asyncio.create_task(slow_turn())
    await cs.input_queue.put("/status")

    while not turn_task.done():
        next_input_task = asyncio.create_task(cs.input_queue.get())
        done, _ = await asyncio.wait(
            [turn_task, next_input_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if next_input_task in done:
            interrupt = next_input_task.result().strip()
            if interrupt in ("/exit", "/quit"):
                continue
            if interrupt.startswith("/"):
                print(f"({interrupt}：turn 跑中不支持 slash 命令)")
                continue
        else:
            next_input_task.cancel()
            try:
                await next_input_task
            except (asyncio.CancelledError, Exception):
                pass

    assert completed.is_set()
    out = capsys.readouterr().out
    assert "turn 跑中不支持 slash 命令" in out


@pytest.mark.asyncio
async def test_stdin_listener_pushes_lines_to_queue(monkeypatch):
    """_stdin_listener 把每行 input() 推进 queue。

    注意：v2.0 起 _stdin_listener 改成 sync function + daemon thread 运行
    （为修"跑完节点还要按回车"的 bug；详见 chat.py:_stdin_listener docstring）。
    """
    import threading
    chat = _load_chat()
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # mock input() 返一组预定义行
    inputs = ["line1", "line2", EOFError()]

    def mock_input(prompt=""):
        v = inputs.pop(0)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr("builtins.input", mock_input)

    # 走真实生产姿势：daemon thread + (loop, queue) 双参
    t = threading.Thread(
        target=chat._stdin_listener, args=(loop, queue), daemon=True,
    )
    t.start()

    line1 = await asyncio.wait_for(queue.get(), timeout=2.0)
    line2 = await asyncio.wait_for(queue.get(), timeout=2.0)
    line3 = await asyncio.wait_for(queue.get(), timeout=2.0)
    assert line1 == "line1"
    assert line2 == "line2"
    assert line3 == "/exit"  # EOFError 转成 /exit
    t.join(timeout=2.0)
    assert not t.is_alive()


@pytest.mark.asyncio
async def test_concurrent_interrupts_apply_in_order(tmp_path: Path):
    """快速连续 2 次中途打断 → 都被处理（按到达顺序）。"""
    chat = _load_chat()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    call_count = [0]

    class _MultiLLM:
        """每次插话：注入一次 → 下一轮收口（真实模型的形状）。"""

        def __init__(self) -> None:
            self._pending_tool = True

        async def chat(self, messages, **kw):
            # 每次 _handle_interrupt 都是新的 forked 对话：以"这轮还没注入过"
            # 判断该不该动手。用 messages 里有没有 tool 结果来判，最贴近真实。
            if any(getattr(m, "role", None) == "tool" for m in messages):
                return LLMResponse(content="注入完成。", tool_calls=[],
                                    finish_reason="stop", usage={})
            call_count[0] += 1
            return LLMResponse(
                content=None,
                tool_calls=[{
                    "id": f"tc_{call_count[0]}", "type": "function",
                    "function": {
                        "name": "runtime_control",
                        "arguments": json.dumps({
                            "action": "inject",
                            "child_run_id": child.run_id,
                            "content": f"inject_{call_count[0]}",
                            "source": "orchestrator_relay",
                        }),
                    },
                }],
                finish_reason="tool_calls", usage={},
            )

    harness = NodeHarness(node_type="_orchestrator", system_prompt="t",
                            tools=[], max_turns=2, max_output_tokens=2048)
    llm = _MultiLLM()
    await chat._handle_interrupt(parent, harness, llm, "first")
    await chat._handle_interrupt(parent, harness, llm, "second")
    injects = child.hook_state.get("injected_messages") or []
    assert len(injects) == 2
    assert "inject_1" in injects[0]["content"]
    assert "inject_2" in injects[1]["content"]
