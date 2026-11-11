"""请求可重建性（#734，抄 DSH「模型看到的一切都在日志里」）。

不变量：无压缩、无驱逐的 run，**每一轮请求的 messages 仅凭 transcript
逐字节重建**——重建结果的 digest 必须等于 llm_request 当场记下的 digest。

这不是"事件都在"的弱断言：任何一条注入丢了全文、任何一条 append 绕过了
留痕、序列化有第二份近似，digest 都对不上。注入类事故（PR#445/#462、
probe 缺 system prompt）从此是 grep + 重算，不用抓真请求。
"""
from __future__ import annotations

import json

import pytest

from core.agent_loop import digest_messages, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse, framework_notice
from core.state import State
from core.conversation_store import apply_context_patch, jsonable_to_msg


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


def _events(state):
    return [json.loads(ln) for ln in
            state.transcript_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _msg_from_dict(d):
    return jsonable_to_msg(d)


def _rebuild_and_check(state):
    """按事件顺序重放 transcript，在每个 llm_request 处与记录的 digest 对账。
    返回对账过的请求数。

    assistant 采用**挂起再落定**语义：真实循环里，撞输出上限且零调用的那轮
    会 `continue` 重来，assistant 消息从未 append —— 重建按同一规则走：
    llm_response 先挂起；见到 truncation_recovery notice 就丢弃挂起（那轮
    没落进历史），其余已知事件先冲洗挂起再处理。"""
    rebuilt: list[LLMMessage] = []
    pending_assistant: list[LLMMessage] = []
    checked = 0

    def _flush():
        rebuilt.extend(pending_assistant)
        pending_assistant.clear()

    for e in _events(state):
        ev = e.get("event")
        if ev == "loop_seed":
            # A new seed replaces the whole request view. A response pending
            # when the previous worker died is already represented by this seed.
            pending_assistant.clear()
            rebuilt = [_msg_from_dict(d) for d in e["messages"]]
        elif ev == "context_rendered":
            _flush()
            rebuilt = apply_context_patch(rebuilt, e["splices"])
        elif ev == "hook_injection" or ev == "finish_gate_blocked":
            _flush()
            if "messages" in e:
                rebuilt.extend(jsonable_to_msg(d) for d in e["messages"])
            else:
                for role, content in zip(e["roles"], e["contents"]):
                    rebuilt.append(LLMMessage(role=role, content=content))
        elif ev == "framework_notice_injected":
            if e.get("source") == "truncation_recovery":
                pending_assistant.clear()     # 那轮的 assistant 从未落进历史
            else:
                _flush()
            rebuilt.append(framework_notice(e["text"]))
        elif ev == "user_message_injected":
            _flush()
            for txt in e["notice_texts"]:
                rebuilt.append(framework_notice(txt))
        elif ev == "llm_request":
            _flush()
            _, digest = digest_messages(rebuilt)
            assert digest == e["request_digest"], (
                f"turn {e['turn']}: 重建 digest 与请求时不符 —— "
                f"有消息变动没进日志（n_messages 记录 {e['n_messages']}，"
                f"重建 {len(rebuilt)}）")
            assert len(rebuilt) == e["n_messages"]
            checked += 1
        elif ev == "llm_response":
            pending_assistant.append(LLMMessage(
                role="assistant", content=e["content"],
                tool_calls=e["tool_calls_full"] or None,
                reasoning_content=e.get("reasoning_content")))
        elif ev == "tool_result":
            _flush()
            rebuilt.append(LLMMessage(
                role="tool", tool_call_id=e["tool_call_id"],
                name=e["name"], content=e["content"]))
    return checked


def _call(name, key, cid):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps({"artifact_type": key})}}


class _Scripted:
    """3 轮：干活（2 个不同参调用）→ 干活（1 个）→ 收尾。"""

    def __init__(self):
        self.turn = 0

    async def chat(self, messages, **kw):
        self.turn += 1
        if self.turn == 1:
            return LLMResponse(
                content="先看一圈",
                tool_calls=[_call("list_artifacts", "ta", "c1"),
                            _call("list_artifacts", "tb", "c2")],
                finish_reason="tool_calls", usage={})
        if self.turn == 2:
            return LLMResponse(
                content="再看一眼", reasoning_content="（思考链示例）",
                tool_calls=[_call("list_artifacts", "tc", "c3")],
                finish_reason="tool_calls", usage={})
        return LLMResponse(content="做完了。", tool_calls=[],
                           finish_reason="stop", usage={})


@pytest.mark.asyncio
async def test_every_request_rebuilds_from_transcript_alone(tmp_path):
    harness = NodeHarness(node_type="literature", system_prompt="系统章程",
                          tools=["list_artifacts"], max_turns=10)
    state = State.new("literature", tmp_path / "r1", project_id="p")
    result = await run_loop(
        harness, state,
        [LLMMessage(role="system", content="系统章程"),
         LLMMessage(role="user", content="go")],
        _Scripted())
    assert result.status == "completed"
    assert _rebuild_and_check(state) == 3     # 三轮请求全部对上


@pytest.mark.asyncio
async def test_seed_carries_the_system_prompt_verbatim(tmp_path):
    """probe 事故的教训：「system 在不在场」必须能从日志直接回答。"""
    harness = NodeHarness(node_type="literature", system_prompt="t",
                          tools=["list_artifacts"], max_turns=4)
    state = State.new("literature", tmp_path / "r2", project_id="p")
    sysmsg = "章程逐字：不许转述"
    await run_loop(harness, state,
                   [LLMMessage(role="system", content=sysmsg),
                    LLMMessage(role="user", content="go")],
                   _Scripted())
    seeds = [e for e in _events(state) if e.get("event") == "loop_seed"]
    assert len(seeds) == 1
    assert seeds[0]["messages"][0]["role"] == "system"
    assert seeds[0]["messages"][0]["content"] == sysmsg


@pytest.mark.asyncio
async def test_mid_loop_notice_reaches_the_log_in_full(tmp_path):
    """中途注入走唯一入口：调度器插话的全文必须在日志里且参与重建。"""
    harness = NodeHarness(node_type="literature", system_prompt="t",
                          tools=["list_artifacts"], max_turns=10)
    state = State.new("literature", tmp_path / "r3", project_id="p")
    long_note = "插话正文 " * 100          # 800+ 字，preview 截不下
    state.hook_state["injected_messages"] = [
        {"content": long_note, "source": "orchestrator"}]
    result = await run_loop(harness, state,
                            [LLMMessage(role="user", content="go")],
                            _Scripted())
    assert result.status == "completed"
    inj = [e for e in _events(state) if e.get("event") == "user_message_injected"]
    assert inj and long_note in inj[0]["notice_texts"][0]
    assert _rebuild_and_check(state) == 3


@pytest.mark.asyncio
async def test_recovery_notice_participates_in_rebuild(tmp_path):
    """框架自己的插话（截断恢复 notice，走 _inject_notice）也必须全文入日志
    并参与重建 —— 只测调度器注入会漏掉这条路径（变异 Mu1 抓出的盲区）。"""

    class _TruncatedOnce:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                text = " ".join(f"独立片段{i}词汇各不相同" for i in range(60))
                return LLMResponse(content=text, tool_calls=[],
                                   finish_reason="length",
                                   usage={"completion_tokens": 999})
            return LLMResponse(content="收尾。", tool_calls=[],
                               finish_reason="stop", usage={})

    harness = NodeHarness(node_type="literature", system_prompt="t",
                          tools=["list_artifacts"], max_turns=10)
    state = State.new("literature", tmp_path / "r4", project_id="p")
    result = await run_loop(harness, state,
                            [LLMMessage(role="user", content="go")],
                            _TruncatedOnce())
    assert result.terminal_cause == "model_finished"
    notices = [e for e in _events(state)
               if e.get("event") == "framework_notice_injected"]
    assert notices and notices[0]["source"] == "truncation_recovery"
    assert len(notices[0]["text"]) > 100      # 全文，不是截样
    assert _rebuild_and_check(state) == 2     # 第二轮请求含 notice，digest 对上


def test_request_digest_tracks_provider_fields_not_local_rendering_identity():
    from dataclasses import replace
    from core.llm import _msg_to_dict

    plain = framework_notice("current snapshot")
    local = replace(plain, context_slot="snapshot")
    assert _msg_to_dict(plain) == _msg_to_dict(local)
    assert digest_messages([plain]) == digest_messages([local])


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("assistant_content", [None, ""])
async def test_snapshot_replacement_and_empty_view_rebuild_every_request(
        tmp_path, monkeypatch, restored, assistant_content):
    from dataclasses import replace
    from core import agent_loop
    from core.loop_hooks import LoopHook, register_loop_hook

    name = "replay_snapshot"
    register_loop_hook(LoopHook(name=name, context_slot=name,
        on_turn_start=lambda ctx: [framework_notice(f"snapshot {ctx.turn}")] if ctx.turn < 3 else None))
    monkeypatch.setattr(agent_loop, "resolve_enabled_hook_names", lambda harness: [name])
    state = State.new("_test", tmp_path / "snapshots")
    messages = [LLMMessage(role="system", content="system")]
    if restored:
        messages.append(replace(framework_notice("previous process"), context_slot=name))
        agent_loop._persist_messages_checkpoint(state, messages, turn=9)
        messages, _ = agent_loop.load_messages_checkpoint(state)
    class SilentToolTurn(_Scripted):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            if self.turn == 1:
                response.content = assistant_content
            return response

    result = await run_loop(NodeHarness(node_type="_test", tools=["list_artifacts"], max_turns=4),
                            state, messages, SilentToolTurn())
    assert result.status == "completed"
    assert _rebuild_and_check(state) == 3
    assert not any(m.context_slot == name for m in result.messages)


@pytest.mark.asyncio
async def test_snapshot_rollback_rebuilds_the_next_request(tmp_path, monkeypatch):
    from core import agent_loop
    from core.loop_hooks import LoopHook, register_loop_hook

    name = "replay_snapshot_rollback"
    register_loop_hook(LoopHook(name=name, context_slot=name,
        on_turn_start=lambda ctx: [framework_notice(f"snapshot {ctx.turn}")]))
    monkeypatch.setattr(agent_loop, "resolve_enabled_hook_names", lambda harness: [name])
    monkeypatch.setattr(agent_loop, "_VOID_RETRY_DELAYS", (0,))

    class VoidThenDone:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return LLMResponse(content="" if self.calls == 1 else "done", tool_calls=[],
                usage={"completion_tokens": 1 if self.calls == 1 else 10},
                finish_reason="stop", protocol_leak=self.calls == 1,
                leak_kind="empty" if self.calls == 1 else None)

    state = State.new("_test", tmp_path / "rollback")
    result = await run_loop(NodeHarness(node_type="_test", max_turns=3), state,
                            [LLMMessage(role="system", content="system")], VoidThenDone())
    assert result.status == "completed"
    assert _rebuild_and_check(state) == 2
