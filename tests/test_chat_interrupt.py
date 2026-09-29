"""chat.py 异步打断 + fork-mode orchestrator decision 行为测试。

不跑真主循环（涉及 stdin）；测核心函数 `_handle_interrupt`：用 stub LLM 模拟
orchestrator 的 fork-mode 决策，验证不同 LLM 决策的应用结果。
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
    ActiveRunInfo, clear_all, find_child_state, register_active,
)
from core.state import State


def _load_chat_module():
    spec = importlib.util.spec_from_file_location(
        "_chat_under_test", Path(__file__).parent.parent / "chat.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


class _MockLLM:
    """支持 forked 单轮调用 + 工具调用模拟。"""
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.call_count = 0
        self.seen_systems: list[str] = []

    async def chat(self, messages, **kw):
        self.call_count += 1
        # 收集 system prompt 内容方便 assert
        for m in messages:
            if m.role == "system":
                self.seen_systems.append(m.content or "")
        if not self.responses:
            return LLMResponse(content="", tool_calls=[],
                                finish_reason="stop", usage={})
        return self.responses.pop(0)


def test_chat_imports_and_has_async_helpers():
    chat = _load_chat_module()
    for fn in ["_handle_interrupt", "_run_one_turn", "_stdin_listener",
                "ChatState", "_print_pause_prompt"]:
        assert hasattr(chat, fn), f"chat.py 缺 {fn}"


def test_chat_imports_readline_for_wide_char_line_editing():
    """回归护栏：chat.py 必须 import readline，否则 input() 走 TTY cooked 模式，
    退格对双列宽字符（中文/emoji）只擦一列 → 屏幕残留"删不掉"的鬼影列
    （用户实测 bug，2026-07）。别把这个 import 删了。"""
    import sys
    _load_chat_module()
    assert "readline" in sys.modules, "chat.py 应 import readline 修宽字符退格鬼影"


def test_sanitize_reply_strips_think_residue_and_control_tokens():
    """2026-07-09 重构（P0-2/3）：_sanitize_reply 复用 core.llm 输出防火墙做
    display 层幂等兜底——剥 </think> 残渣 + 控制标记（DSML/chatml/孤立 scaffold
    标签）。

    scaffold **复读** 的剥离已上移到源头（core.llm.chat() 带 injected_texts 佐证），
    不再靠显示层硬编码某句哨兵——那种哨兵改一次引导文案就静默失效（正是本次修的
    A3 bug）。复读剥离的覆盖见 tests/test_output_firewall.py。"""
    chat = _load_chat_module()
    s = chat._sanitize_reply

    # </think> 残渣
    assert s("</think>进步了！产出了 4 件 artifact") == "进步了！产出了 4 件 artifact"
    # DSML / chatml 控制标记残留
    assert s("好，现在汇报进展。</｜DSML｜tool_calls>") == "好，现在汇报进展。"
    assert s("用户选了 **[1] PROCEED**<|im_end|>") == "用户选了 **[1] PROCEED**"
    # 干净回复原样通过
    assert s("正常回复，不动。") == "正常回复，不动。"
    # 全是控制标记、剥完为空 → 退回原文（别显示空白）
    only_token = "</｜DSML｜tool_calls>"
    assert s(only_token) == only_token


@pytest.mark.asyncio
async def test_handle_interrupt_with_no_children_delivers_to_the_main_turn(
    tmp_path: Path, capsys,
):
    """没有活跃子节点 = 调度器在自己干活 → 这句话进主对话，**不丢弃**。

    2026-08-18 前这里直接"已忽略" —— 用户在调度器自己干活时说的每一句都蒸发。
    wangd：「应该能跟调度器非常非常顺畅地讨论」，第一步是别把话扔了。
    """
    chat = _load_chat_module()
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    llm = _MockLLM([])
    await chat._handle_interrupt(state, harness, llm, "把重点放在营养那条线")
    # 主对话必须拿到它 —— agent_loop 下一轮开头机械 drain
    injected = state.hook_state.get("injected_messages") or []
    assert len(injected) == 1
    assert "把重点放在营养那条线" in injected[0]["content"]
    assert injected[0]["source"] == "user_interject"
    # 没有子节点可问，不必跑待命轮
    assert llm.call_count == 0
    assert "下一轮" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_handle_interrupt_calls_inject_tool(tmp_path: Path):
    """forked LLM 返 inject_into_node tool call → 应用到 child hook_state。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    inject_call = {
        "id": "tc_1", "type": "function",
        "function": {
            "name": "runtime_control",
            "arguments": json.dumps({
                "action": "inject",
                "child_run_id": child.run_id,
                "content": "用 BAOAB 不用 Verlet",
                "source": "orchestrator_relay",
            }),
        },
    }
    llm = _MockLLM([
        LLMResponse(content=None, tool_calls=[inject_call],
                     finish_reason="tool_calls", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=["runtime_control"],
                            max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm,
                                    "改成 BAOAB 算法")
    # child hook_state 拿到 inject
    queue = child.hook_state.get("injected_messages") or []
    assert len(queue) == 1
    assert "BAOAB" in queue[0]["content"]
    # 待命轮的 system prompt：明说自己在待命 + 带 active child 信息
    assert any("待命" in s for s in llm.seen_systems)
    assert any(child.run_id in s for s in llm.seen_systems)
    # 交流记录回主对话，主循环才不会把答过的再答一遍
    parent_injected = parent.hook_state.get("injected_messages") or []
    assert parent_injected and "BAOAB" in parent_injected[0]["content"]


@pytest.mark.asyncio
async def test_handle_interrupt_calls_cancel_tool(tmp_path: Path):
    """forked LLM 返 cancel_node tool call → 写 child kill_signal。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    cancel_call = {
        "id": "tc_1", "type": "function",
        "function": {
            "name": "runtime_control",
            "arguments": json.dumps({
                "action": "cancel",
                "child_run_id": child.run_id,
                "reasoning": "user said stop now please",
            }),
        },
    }
    llm = _MockLLM([
        LLMResponse(content=None, tool_calls=[cancel_call],
                     finish_reason="tool_calls", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=["runtime_control"], max_turns=2,
                            max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "停了吧")
    sig = child.hook_state.get("kill_signal")
    assert sig is not None
    assert "user said stop" in sig["reason"]


@pytest.mark.asyncio
async def test_handle_interrupt_no_tool_call_prints_explanation(
    tmp_path: Path, capsys,
):
    """LLM 拿不准 → 不调工具，输出文本 → 直接打给 user。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    llm = _MockLLM([
        LLMResponse(content="跟当前节点工作无关，先继续。",
                     tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "什么时候吃饭")
    out = capsys.readouterr().out
    assert "跟当前节点工作无关" in out
    # child 没收到信号
    assert not child.hook_state.get("kill_signal")
    assert not child.hook_state.get("injected_messages")


@pytest.mark.asyncio
async def test_handle_interrupt_llm_error_does_not_crash(tmp_path: Path,
                                                          capsys):
    """LLM 抛异常 → 打印错误，不挂掉主循环。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))

    class _BadLLM:
        async def chat(self, *a, **kw):
            raise RuntimeError("LLM exploded")

    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, _BadLLM(), "anything")
    out = capsys.readouterr().out
    assert "待命轮出错" in out
    assert "RuntimeError" in out
    # 报错也不许丢话：主对话仍要拿到它
    assert parent.hook_state.get("injected_messages")


@pytest.mark.asyncio
async def test_chat_state_paused_event_routing(tmp_path: Path):
    """ChatState.paused event 在 pause_driver 进出 ask_fn 时 set/clear。"""
    chat = _load_chat_module()
    cs = chat.ChatState()
    # 初始未 set
    assert not cs.paused.is_set()
    # 模拟 ask 函数 set 然后 await answer 然后 clear（参考 _run_one_turn 内 ask_via_queue）
    from core.pause import PauseEvent

    async def driver():
        cs.paused.set()
        try:
            return await cs.pause_answer_queue.get()
        finally:
            cs.paused.clear()

    drive_task = asyncio.create_task(driver())
    # 等 set
    await asyncio.sleep(0)
    assert cs.paused.is_set()
    await cs.pause_answer_queue.put("my answer")
    answer = await drive_task
    assert answer == "my answer"
    assert not cs.paused.is_set()


@pytest.mark.asyncio
async def test_handle_interrupt_logs_to_transcript(tmp_path: Path):
    """interrupt 决策应写 transcript 事件 'interrupt_decision'。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    llm = _MockLLM([
        LLMResponse(content="no action", tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "user msg")
    # transcript 应含 interrupt_decision
    transcript = parent.transcript_path.read_text(encoding="utf-8")
    events = []
    for line in transcript.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line).get("event"))
    assert "interrupt_decision" in events


# ── 机械停止信号（2026-08-17 平台停止按钮与 CLI /stop 共用一份）──────────────

def test_do_stop_signal_writes_kill_signal_to_top_and_children(tmp_path: Path):
    """停止不经过模型：直接写 kill_signal（顶层 + 每个活跃子节点），关 continuous。"""
    from core.pause import ActiveRunInfo, register_active, unregister_active

    chat = _load_chat_module()
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    state.hook_state["continuous_mode"] = True
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child))
    try:
        n = chat._do_stop_signal(state, reason="平台停止按钮", requested_by="user_stop:u1")
    finally:
        unregister_active(child.run_id)

    assert n == 1
    assert state.hook_state["kill_signal"]["requested_by"] == "user_stop:u1"
    assert child.hook_state["kill_signal"]["requested_by"] == "user_stop:u1"
    assert not chat._continuous_loop(state), \
        "continuous 不关 → 本轮刚停 driver 立刻又排下一轮，看起来像停不下来"


@pytest.mark.asyncio
async def test_handle_interrupt_reply_reaches_transcript(tmp_path: Path, capsys):
    """决策轮给用户的答复必须落 transcript（interrupt_reply）。

    2026-08-17 实测：以前只走 _print_speaker/_emit_above_prompt（CLI 打印），
    平台上用户问"跑的怎么样了"，回答写进 stdout 没人看得见 —— 插话像对着
    空气说话。transcript 是给两个前端的；打印只是 CLI 的显示方式。
    """
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    llm = _MockLLM([
        LLMResponse(content="文献检索还在第 4 轮，已入索引 17 篇。",
                     tool_calls=[],
                     finish_reason="stop", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "跑的怎么样了？")
    records = [
        json.loads(line) for line in
        parent.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    replies = [r for r in records if r.get("event") == "interrupt_reply"]
    assert replies, f"没有 interrupt_reply 记录，只有 {[r.get('event') for r in records]}"
    assert "已入索引 17 篇" in replies[0]["text"]


@pytest.mark.asyncio
async def test_handle_interrupt_deferred_note_reaches_transcript(tmp_path: Path, capsys):
    """没有活跃子节点时，"已排队"的事实同样要进 transcript ——
    但作为**结构化回执**（interrupt_deferred），不是框架替调度器编的台词
    （wangd 2026-08-18：机械事实归框架、措辞归显示层，谁也不冒充谁）。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path)

    class _NeverCalledLLM:
        async def chat(self, *a, **kw):
            raise AssertionError("no active children → LLM must not be called")

    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=[], max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, _NeverCalledLLM(), "顺便再看下 X",
                                 reply_to_message_id="msg-1")
    records = [
        json.loads(line) for line in
        parent.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    deferred = [r for r in records if r.get("event") == "interrupt_deferred"]
    assert deferred and deferred[0]["replies_to_message_id"] == "msg-1"
    # 框架的台词不再冒充调度器发言。
    assert not [r for r in records if r.get("event") == "interrupt_reply"]
    # CLI 观众仍然有一行人话提示（打印面不变）。
    assert "下一轮" in capsys.readouterr().out


# ── 待命轮：子节点跑着时也能顺畅对话（wangd 2026-08-18）────────────────────

@pytest.mark.asyncio
async def test_standby_turn_gets_read_tools_but_never_write_tools(tmp_path: Path):
    """子节点持有工作区写入权 → 待命轮**只**拿只读工具 + runtime_control。

    判据是 `replayable_read` 这个声明属性（机械扫盘），不是工具名单 ——
    名单对新加的工具默认漏过，而漏过的方向是"给了写权限"。
    """
    from core.bootstrap import bootstrap
    from core.loader import load_harness
    bootstrap()

    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    seen_tools: list[list[str]] = []

    class _ToolSpyLLM:
        call_count = 0
        seen_systems: list[str] = []

        async def chat(self, messages, tools=None, **kw):
            seen_tools.append([t["function"]["name"] for t in (tools or [])])
            return LLMResponse(content="文献那边刚跑完第 4 轮检索。",
                                tool_calls=[], finish_reason="stop", usage={})

    harness = load_harness("_orchestrator")
    await chat._handle_interrupt(parent, harness, _ToolSpyLLM(), "进展怎么样？")

    assert seen_tools, "待命轮没有发起 LLM 调用"
    offered = set(seen_tools[0])
    assert "read_artifact" in offered and "search_kb" in offered, offered
    assert "runtime_control" in offered
    # 写工具一个都不许出现（子节点正持有 Git mutation lane）
    for forbidden in ("save_artifact", "run_node", "freeze_artifact",
                       "write_scratchpad", "task"):
        assert forbidden not in offered, f"{forbidden} 不该出现在待命轮"


@pytest.mark.asyncio
async def test_standby_turn_can_look_things_up_before_answering(tmp_path: Path):
    """多个来回：先调只读工具查，再用查到的东西回答。"""
    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    progress_call = {
        "id": "tc_p", "type": "function",
        "function": {
            "name": "runtime_control",
            "arguments": json.dumps({
                "action": "progress", "child_run_id": child.run_id,
            }),
        },
    }
    llm = _MockLLM([
        LLMResponse(content=None, tool_calls=[progress_call],
                     finish_reason="tool_calls", usage={}),
        LLMResponse(content="它在跑第 4 组检索，已入索引 17 篇。",
                     tool_calls=[], finish_reason="stop", usage={}),
    ])
    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=["runtime_control"],
                            max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, llm, "跑到哪了？")
    assert llm.call_count == 2, "查完之后必须再回来把结果讲成人话"
    records = [
        json.loads(line) for line in
        parent.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    replies = [r["text"] for r in records if r.get("event") == "interrupt_reply"]
    assert any("已入索引 17 篇" in t for t in replies)


@pytest.mark.asyncio
async def test_standby_turn_carries_the_conversation_context(tmp_path: Path):
    """待命轮带调度器自己的对话尾巴 —— 没有语境答不了"之前说的那个方案"。"""
    from core.conversation_store import save_conversation
    from core.llm import LLMMessage

    chat = _load_chat_module()
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "p")
    child = State.new(node_type="literature", base_dir=tmp_path / "c")
    register_active(ActiveRunInfo(
        run_id=child.run_id, node_type="literature", state=child,
    ))
    save_conversation(parent, [
        LLMMessage(role="system", content="orchestrator system"),
        LLMMessage(role="user", content="先做英国饮食文化的开题"),
        LLMMessage(role="assistant", content="已冻结预注册，四个研究问题。"),
    ])

    seen_user_texts: list[list[str]] = []

    class _ContextSpyLLM:
        call_count = 0
        seen_systems: list[str] = []

        async def chat(self, messages, tools=None, **kw):
            seen_user_texts.append([
                (m.content or "") for m in messages if m.role in ("user", "assistant")
            ])
            return LLMResponse(content="四个研究问题都在预注册里冻结了。",
                                tool_calls=[], finish_reason="stop", usage={})

    harness = NodeHarness(node_type="_orchestrator", system_prompt="o",
                            tools=["runtime_control"],
                            max_turns=2, max_output_tokens=2048)
    await chat._handle_interrupt(parent, harness, _ContextSpyLLM(),
                                  "刚才冻结的那几个研究问题是啥来着？")
    assert seen_user_texts
    joined = " ".join(seen_user_texts[0])
    assert "已冻结预注册" in joined, "待命轮没带上之前的对话"
    assert "刚才冻结的那几个研究问题" in joined
