"""chat.py REPL 的单元 smoke test。不联网，不调真实 LLM。

测试点：
  1. orchestrator state 创建 + 持久化目录
  2. 对话存盘 + 加载 round-trip
  3. /reset 清空 messages
  4. /btw 注入登记 + 下一轮消费
  5. orchestrator harness 加载（_orchestrator）
"""
from __future__ import annotations

import json
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.llm import LLMMessage
from core.loader import load_harness

bootstrap()

import chat as chat_mod   # noqa: E402


def test_orchestrator_state_creation():
    """临时项目（无 project_id）应生成 anon run_id。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        assert state.run_id.startswith("orchestrator__anon-")
        assert state.root.exists()
        assert state.project_id is None
        assert state.project_root is None
        print(f"  ✓ anon state: {state.run_id}")


def test_orchestrator_state_with_project():
    """带 project_id 的 state 应固定 run_id 并指向 project_root。"""
    import os
    with tempfile.TemporaryDirectory() as td:
        os.environ["HARNESS_FRAMEWORK_HOME"] = str(Path(td) / "home")
        state = chat_mod._make_or_load_orchestrator_state("proj-test", Path(td))
        assert state.run_id == "orchestrator__proj-test"
        assert state.project_id == "proj-test"
        assert state.project_root is not None
        assert state.project_root.exists()
        print(f"  ✓ project state: {state.run_id} → {state.project_root}")


def test_conversation_roundtrip():
    """对话存盘 + 加载应该 round-trip。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        messages = [
            LLMMessage(role="system", content="sys prompt"),
            LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi", tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "list_artifacts", "arguments": "{}"}},
            ]),
            LLMMessage(role="tool", tool_call_id="c1", name="list_artifacts",
                       content='{"artifacts":[]}'),
        ]
        state.tokens_used = 1234
        state.scratchpad = "H1 已冻结；下一步跑 experiment"
        state.hook_state["key"] = "value"
        chat_mod._save_conversation(state, messages)

        # 新建另一个 state 然后加载
        state2 = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state2.run_id = state.run_id    # 模拟续连同一个 run
        state2.root = state.root
        loaded = chat_mod._load_conversation(state2)
        assert loaded is not None
        assert len(loaded) == 4
        assert loaded[0].role == "system"
        assert loaded[2].tool_calls and loaded[2].tool_calls[0]["function"]["name"] == "list_artifacts"
        assert state2.tokens_used == 1234
        assert state2.scratchpad == "H1 已冻结；下一步跑 experiment"
        assert state2.hook_state.get("key") == "value"
        print(f"  ✓ conversation roundtrip: {len(loaded)} messages，state metadata 保留")


def test_cmd_reset():
    """/reset 清空 messages + hook_state + scratchpad，不动 artifact/memory。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.save_artifact("plan", "p1", "body")
        state.save_memory(kind="observation", text="important", tags=["test"])
        state.hook_state["x"] = 1
        state.scratchpad = "板上写着点什么"
        messages = [LLMMessage(role="user", content="hi"), LLMMessage(role="assistant", content="hello")]
        out = chat_mod._cmd_reset(state, messages)
        assert len(messages) == 0
        # 换届清掉的是"聊到哪了"，"怎么跑"要原样带过去 —— 于是 hook_state 里
        # 只剩档位那一份声明（这里是协作档 = 空授权）。
        assert state.hook_state == {"autonomy_mode": "assisted", "authorized_risk_classes": []}
        assert state.scratchpad == ""
        # artifact / memory 不动
        assert len(state.list_artifacts()) == 1
        assert len(state.search_memory()) == 1
        assert "memory + KB 保留" in out
        print("  ✓ /reset 清空 messages/hook_state/scratchpad，保留 artifact/memory")


def test_cmd_reset_preserves_continuous_runtime_intent():
    from core import pause_driver
    from shared.lib import dangerous_commands as dc

    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        old_auto = pause_driver.AUTO_APPROVE_ENABLED
        old_bypass = dc.bypass_enabled()
        try:
            chat_mod._set_autonomy_mode(state, "continuous")
            chat_mod._set_continuous_loop(state, True)
            state.hook_state["temporary"] = "drop-me"
            messages = [LLMMessage(role="user", content="hi")]
            chat_mod._cmd_reset(state, messages)
            assert messages == []
            assert chat_mod._continuous_running(state) is True
            assert "temporary" not in state.hook_state
        finally:
            pause_driver.set_auto_approve(old_auto)
            dc.set_bypass_mode(old_bypass)


def test_btw_queueing():
    """/btw 命令把 payload 登记到 state.hook_state['pending_btw_injection']。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        # 模拟 REPL 主循环中 /btw 处理逻辑
        payload = "hey 我希望多关注 baseline"
        queue = state.hook_state.setdefault("pending_btw_injection", "")
        state.hook_state["pending_btw_injection"] = (
            queue + ("\n" if queue else "") + payload
        )
        # 再来一条
        payload2 = "另外注意 timeline"
        queue = state.hook_state.setdefault("pending_btw_injection", "")
        state.hook_state["pending_btw_injection"] = (
            queue + ("\n" if queue else "") + payload2
        )
        text = state.hook_state["pending_btw_injection"]
        assert payload in text and payload2 in text
        assert text.count("\n") == 1   # 两条之间一个换行
        print(f"  ✓ /btw 累积两条注入：{repr(text)[:80]}")


def test_load_orchestrator_harness():
    """orchestrator harness 能正确加载，含 callable_nodes=['*']。"""
    h = load_harness("_orchestrator")
    assert h.node_type == "_orchestrator"
    assert "*" in h.callable_nodes
    assert "run_node" in h.tools
    assert "query_project_status" in h.tools
    assert h.summarizer.enabled
    print(f"  ✓ orchestrator harness: tools={len(h.tools)}, callable=['*'], summarizer enabled")


def test_continuous_mode_includes_auto_approve_and_bypass():
    """`/continuous on` 必须机械包含"不停下来问人"，不能只靠启动说明。

    2026-08-23 之后这是**两件事**：档位（`_set_autonomy_mode`）决定要不要停，
    续轮（`_set_continuous_loop`）决定跑完一轮要不要接着跑。`/continuous on`
    在终端里的意思是两件都要，所以它两个都调 —— 但两件事各有各的名字，
    平台侧的 `run_unattended` 只碰后者。
    """
    from core import pause_driver
    from shared.lib import dangerous_commands as dc

    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        old_auto = pause_driver.AUTO_APPROVE_ENABLED
        old_bypass = dc.bypass_enabled()
        try:
            pause_driver.set_auto_approve(False)
            dc.set_bypass_mode(False)
            chat_mod._set_autonomy_mode(state, "continuous")
            chat_mod._set_continuous_loop(state, True)
            assert chat_mod._continuous_loop(state) is True
            assert chat_mod._continuous_running(state) is True
            assert pause_driver.AUTO_APPROVE_ENABLED is True
            assert dc.bypass_enabled() is True

            chat_mod._set_continuous_loop(state, False)
            assert chat_mod._continuous_loop(state) is False
            # 关掉续轮不暗中改档位：那是 /autonomy 的事。
            assert pause_driver.AUTO_APPROVE_ENABLED is True
            assert dc.bypass_enabled() is True
        finally:
            pause_driver.set_auto_approve(old_auto)
            dc.set_bypass_mode(old_bypass)


def test_continuous_status_protocol_and_default_continue():
    assert chat_mod._continuous_status("CONTINUOUS_STATUS: continue") == "continue"
    assert chat_mod._continuous_status("done\nCONTINUOUS_STATUS: complete") == "complete"
    assert chat_mod._continuous_status("CONTINUOUS_STATUS: blocked") == "blocked"
    assert chat_mod._continuous_status("要我继续吗？") is None
    assert "CONTINUOUS_STATUS" not in chat_mod._strip_continuous_status(
        "结果正文\nCONTINUOUS_STATUS: complete"
    )


def test_continuous_followup_keeps_running_without_marker():
    """普通问句/汇报没有状态行时必须自动续轮；complete/blocked 才停。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")

        prompt, delay = chat_mod._continuous_followup(
            state, "要我按 P1 到 P5 启动吗？", reason="turn_finished"
        )
        assert prompt is not None
        assert prompt.startswith(chat_mod._CONTINUOUS_INTERNAL_PREFIX)
        assert "不要用“要我继续吗" in prompt
        assert delay >= 0

        prompt, _ = chat_mod._continuous_followup(
            state, "全部交付完成\nCONTINUOUS_STATUS: complete", reason="turn_finished"
        )
        assert prompt is None
        assert state.hook_state["continuous_phase"] == "complete"

        # 显式重新打开后可以开始下一个目标。
        # blocked 语义变更（E2E-5b 12.7h 停摆）：不再是终态 —— 停靠 + 复查。
        # 见 tests/test_blocked_parking.py。
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        prompt, delay = chat_mod._continuous_followup(
            state, "只剩真人实验\nCONTINUOUS_STATUS: blocked", reason="turn_finished"
        )
        assert prompt is not None and "复查" in prompt
        assert state.hook_state["continuous_phase"] == "running"
        assert delay >= chat_mod._BLOCKED_PROBE_BASE_S


def test_continuous_session_resume_retries_stale_blocker():
    """进程重启可能意味着外部修复已部署，恢复轮应先重试原失败动作。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        prompt, _ = chat_mod._continuous_followup(
            state,
            "CONTINUOUS_STATUS: continue",
            reason="session_start",
        )
        assert prompt is not None
        assert "先原样重试" in prompt
        assert "旧 summary" in prompt


def test_continuous_rejects_terminal_status_when_synthesis_has_runnable_steps():
    """iterate 已给 target_node 时，模型误报 complete/blocked 也必须继续。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.save_artifact(
            "review_critique",
            "project_synthesis_gate",
            "iterate",
            metadata={
                "scope": "project_synthesis",
                "project_verdict": "iterate",
                "actionable_next_steps": [{
                    "action": "redo_experiment",
                    "target_node": "experiment",
                    "target_artifact": None,
                    "how": "运行真实 WebArena 对照实验",
                    "blocks_writing": True,
                }],
            },
            provenance=forwarded(
                produced("_reviewer", "review-run"),
                via_node_type=state.node_type,
                via_run_id=state.run_id,
            ),
        )
        state.hook_state.update(continuous_loop=True, continuous_phase="running")

        prompt, _ = chat_mod._continuous_followup(
            state,
            "需要实验，所以停止\nCONTINUOUS_STATUS: blocked",
            reason="turn_finished",
        )
        assert prompt is not None
        assert state.hook_state["continuous_phase"] == "running"
        assert "experiment" in prompt
        assert "WebArena" in prompt


def test_continuous_reads_legacy_project_steps_from_json_content():
    """Continuous mode must share writing-gate's legacy typed-content fallback."""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.save_artifact(
            "review_critique",
            "project_synthesis_legacy_content",
            json.dumps({
                "scope": "project_synthesis",
                "project_verdict": "iterate",
                "actionable_next_steps": [{
                    "action": "fix_evidence_gap",
                    "target_node": "literature",
                    "target_artifact": None,
                    "how": "补齐官网参数证据",
                    "blocks_writing": True,
                }],
            }),
            metadata={"source_node_type": "_project"},
            provenance=forwarded(
                produced("_reviewer", "review-run"),
                via_node_type=state.node_type,
                via_run_id=state.run_id,
            ),
        )

        assert chat_mod._continuous_project_actions(state) == [{
            "action": "fix_evidence_gap",
            "target_node": "literature",
            "target_artifact": None,
            "how": "补齐官网参数证据",
            "blocks_writing": True,
        }]


def test_continuous_rejects_terminal_status_while_formal_decision_is_pending():
    """A persisted decision package outranks an LLM completion/block marker."""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "writing",
            "producing_run_id": "writing-reviewed",
            "artifact_ids": ["manuscript__draft"],
            "review_state": "done",
            "curator_state": "done",
            "decision_state": "awaiting_human",
            "decision_recommended_action": "revise",
        }]

        for reported in ("complete", "blocked"):
            prompt, _ = chat_mod._continuous_followup(
                state,
                f"用户选择了 PROCEED\nCONTINUOUS_STATUS: {reported}",
                reason="turn_finished",
            )
            assert prompt is not None
            assert state.hook_state["continuous_phase"] == "running"
            assert "present_decision_package" in prompt
            assert "writing-reviewed" in prompt
            assert "revise" in prompt

        events = [
            json.loads(line) for line in state.transcript_path.read_text().splitlines()
            if json.loads(line).get("event") == "continuous_terminal_status_rejected"
        ]
        assert {event["reported_status"] for event in events} >= {"complete", "blocked"}
        assert all(event["reason"] == "pending_post_node_flow" for event in events[-2:])


def test_continuous_does_not_queue_second_turn_while_pause_is_live():
    """The pause driver exclusively owns control until the formal pause resolves."""
    from core.pause import (
        PauseEvent, PausedRunContext, claim_driver, clear_all, register_pause,
    )

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
            state.hook_state.update(continuous_loop=True, continuous_phase="running")
            register_pause(PausedRunContext(
                run_id="paused-decision",
                state=state,
                messages=[],
                harness=load_harness("_orchestrator"),
                llm=object(),
                pending_tool_call_id="call-1",
                pause_event=PauseEvent(
                    question="decision",
                    metadata={"type": "decision_package"},
                ),
            ))
            # "live" 现在是注册表里的事实，不是假设：有人在等答复才算 live。
            # 没人等的 pause 是孤儿，会被 auto-approve 自行收拾（见
            # tests/test_orphan_pause.py：E2E-5a 82 分钟死锁）。
            claim_driver("paused-decision")
            chat_state = chat_mod.ChatState()
            try:
                queued = await chat_mod._queue_continuous_followup(
                    state,
                    chat_state,
                    "CONTINUOUS_STATUS: continue",
                    reason="turn_finished",
                )
                assert queued is False
                assert chat_state.input_queue.empty()
                events = [
                    json.loads(line)
                    for line in state.transcript_path.read_text().splitlines()
                ]
                assert any(
                    event.get("event") == "continuous_followup_suppressed"
                    and event.get("reason") == "live_pause_pending"
                    for event in events
                )
            finally:
                clear_all()

    asyncio.run(scenario())


def test_continuous_rejects_complete_while_latest_producer_is_incomplete():
    """A synthesis/readback cannot launder a QC-failed producer into complete."""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        failed_run = state.root.parent / "writing-failed"
        failed_run.mkdir()
        (failed_run / "summary.json").write_text(
            json.dumps({
                "run_id": "writing-failed",
                "node_type": "writing",
                "status": "incomplete",
                "missing_required_outputs": ["manuscript"],
            }),
            encoding="utf-8",
        )
        state.append_transcript(
            "subagent_call_end",
            child_node_type="writing",
            child_run_id="writing-failed",
            child_status="incomplete",
        )

        prompt, _ = chat_mod._continuous_followup(
            state,
            "PDF 已经生成\nCONTINUOUS_STATUS: complete",
            reason="turn_finished",
        )

        assert prompt is not None
        assert state.hook_state["continuous_phase"] == "running"
        assert "writing-failed" in prompt
        assert "manuscript" in prompt
        assert "不得用 project_synthesis" in prompt

        completed_run = state.root.parent / "writing-completed"
        completed_run.mkdir()
        (completed_run / "summary.json").write_text(
            json.dumps({
                "run_id": "writing-completed",
                "node_type": "writing",
                "status": "completed",
            }),
            encoding="utf-8",
        )
        state.append_transcript(
            "subagent_call_end",
            child_node_type="writing",
            child_run_id="writing-completed",
            child_status="completed",
        )
        prompt, _ = chat_mod._continuous_followup(
            state,
            "真正完成\nCONTINUOUS_STATUS: complete",
            reason="turn_finished",
        )
        assert prompt is None
        assert state.hook_state["continuous_phase"] == "complete"


def test_continuous_terminal_guard_only_blocks_invalid_final_writing():
    """Superseded upstream attempts and real human blockers remain terminal-safe."""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        state.append_transcript(
            "subagent_call_end",
            child_node_type="data",
            child_run_id="abandoned-data",
            child_status="cancelled",
        )
        prompt, _ = chat_mod._continuous_followup(
            state,
            "研究目标已完成\nCONTINUOUS_STATUS: complete",
            reason="turn_finished",
        )
        assert prompt is None
        assert state.hook_state["continuous_phase"] == "complete"

        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        state.append_transcript(
            "subagent_call_end",
            child_node_type="writing",
            child_run_id="writing-needs-human",
            child_status="incomplete",
        )
        prompt, delay = chat_mod._continuous_followup(
            state,
            "需要人工取得受限数据后才能继续\nCONTINUOUS_STATUS: blocked",
            reason="turn_finished",
        )
        # blocked 语义变更：终态门禁放行它之后进入**停靠**（复查+呼救），不熄火。
        assert prompt is not None and "复查" in prompt
        assert state.hook_state["continuous_phase"] == "running"
        assert delay >= chat_mod._BLOCKED_PROBE_BASE_S


def test_continuous_repairs_dangling_and_orphan_tool_messages():
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "run_node", "arguments": "{}"}},
        ]),
        LLMMessage(role="tool", tool_call_id="c1", name="read_file", content="ok"),
        # c2 missing；下一条非-tool 之前必须补 deferred。
        LLMMessage(role="system", content="next"),
        # 没有紧邻 assistant call 的 orphan 要隔离。
        LLMMessage(role="tool", tool_call_id="orphan", name="x", content="late"),
        LLMMessage(role="user", content="continue"),
    ]
    changed = chat_mod._repair_message_tool_protocol(messages)
    assert changed == 2
    assert [m.role for m in messages] == [
        "system", "assistant", "tool", "tool", "system", "user"
    ]
    assert messages[2].tool_call_id == "c1"
    assert messages[3].tool_call_id == "c2"
    assert "deferred" in (messages[3].content or "")


def test_orchestrator_messages_initial():
    """orchestrator 的初始 build_messages 应该至少包含 system + user。"""
    from core.context_engine import build_messages
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        h = load_harness("_orchestrator")
        messages = build_messages(h, state, node_inputs={})
        assert messages[0].role == "system"
        assert "对话主 agent" in (messages[0].content or "")
        assert messages[1].role == "user"
        print(f"  ✓ orchestrator initial messages: {len(messages)} 条")


class _FakeRealStream:
    """录制 write() 调用，冒充真实 sys.stderr 给 _PromptSafeStderr 包。"""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, s: str) -> int:
        self.writes.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def test_prompt_safe_stderr_buffers_until_full_line():
    """半行不触发清行/重画；凑齐一整行才落地，且落地内容干净无残留。

    复现场景（issue：终端花屏）：node hook 直接 print(msg, file=sys.stderr,
    flush=True) 会把消息拆成多次 write（内容 + 换行符），如果不按行缓冲，
    半行也会触发一次清行+重画，视觉上提示符被反复顶出来。
    """
    chat_mod._PROMPT_LIVE["on"] = True
    chat_mod._IS_TTY = True
    # 清行/重画本身写真实 sys.stdout（跟 _emit_above_prompt 同款协议）——
    # 测试只关心 _PromptSafeStderr 有没有正确按行缓冲、转发给被包的流，
    # 不关心提示符重画的具体 ANSI 输出，摘掉避免污染 pytest 自己的输出。
    saved_clear = chat_mod._clear_input_line
    saved_redraw = chat_mod._redraw_prompt
    chat_mod._clear_input_line = lambda: None
    chat_mod._redraw_prompt = lambda: None
    try:
        real = _FakeRealStream()
        wrapped = chat_mod._PromptSafeStderr(real)

        # print() 典型地拆成两次 write：消息体 + 换行
        wrapped.write("[experiment 14:46:02] tool_done: safe_execute_python")
        assert real.writes == [], "半行不该提前落地"
        wrapped.write("\n")
        assert real.writes == ["[experiment 14:46:02] tool_done: safe_execute_python\n"]

        wrapped.write("[experiment 14:46:27] llm_tool_calls: safe_execute_python\n")
        assert real.writes[-1] == "[experiment 14:46:27] llm_tool_calls: safe_execute_python\n"
        assert len(real.writes) == 2, "两条完整行 → 恰好两次真实落地写"
    finally:
        chat_mod._clear_input_line = saved_clear
        chat_mod._redraw_prompt = saved_redraw
        chat_mod._PROMPT_LIVE["on"] = False


def test_prompt_safe_stderr_passthrough_when_prompt_not_live():
    """提示符还没接管（启动阶段）/非 tty 时，直接透传，不缓冲。"""
    chat_mod._PROMPT_LIVE["on"] = False
    real = _FakeRealStream()
    wrapped = chat_mod._PromptSafeStderr(real)
    wrapped.write("early startup log\n")
    assert real.writes == ["early startup log\n"]


if __name__ == "__main__":
    print("== chat.py smoke tests ==")
    tests = [
        test_orchestrator_state_creation,
        test_orchestrator_state_with_project,
        test_conversation_roundtrip,
        test_cmd_reset,
        test_btw_queueing,
        test_load_orchestrator_harness,
        test_orchestrator_messages_initial,
        test_prompt_safe_stderr_buffers_until_full_line,
        test_prompt_safe_stderr_passthrough_when_prompt_not_live,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print("\nALL PASS ✓")


def test_continuous_aborts_after_consecutive_errors():
    """v3.3 熔断：连续 turn_error 达阈值后停机（phase=aborted），不再自动续轮。
    这是 atomic-agents E2E 五小时 393 次崩溃循环的直接止血。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        n = chat_mod._CONTINUOUS_MAX_CONSECUTIVE_ERRORS
        assert n > 0
        # 前 n-1 轮 turn_error：仍续轮
        for i in range(n - 1):
            prompt, _ = chat_mod._continuous_followup(
                state, "又挂了", reason="turn_error"
            )
            assert prompt is not None, f"第 {i+1} 次 error 不应停机"
        # 第 n 轮：达阈值 → 停机
        prompt, delay = chat_mod._continuous_followup(
            state, "又挂了", reason="turn_error"
        )
        assert prompt is None
        assert delay == 0.0
        assert state.hook_state["continuous_phase"] == "aborted"
        assert state.hook_state.get("continuous_abort_reason")
        # aborted 后 _continuous_running=False（本会话不再续轮）
        assert not chat_mod._continuous_running(state)


def test_continuous_error_streak_resets_on_success():
    """一次正常轮（非 turn_error）应清零 error streak —— 偶发抖动不该累积到熔断。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(continuous_loop=True, continuous_phase="running")
        n = chat_mod._CONTINUOUS_MAX_CONSECUTIVE_ERRORS
        for _ in range(n - 1):
            chat_mod._continuous_followup(state, "挂", reason="turn_error")
        # 一次正常续轮 → 清零
        chat_mod._continuous_followup(state, "进展中", reason="turn_finished")
        assert state.hook_state.get("continuous_error_turns", 0) == 0
        # 再来一次 error 不应立即熔断（streak 已重置）
        prompt, _ = chat_mod._continuous_followup(state, "又挂", reason="turn_error")
        assert prompt is not None
        assert state.hook_state["continuous_phase"] == "running"


def test_continuous_resume_clears_abort_and_counters():
    """人工 /continuous on（reset_phase=True）应清熔断计数 + 原因，允许重新开始。"""
    with tempfile.TemporaryDirectory() as td:
        state = chat_mod._make_or_load_orchestrator_state(None, Path(td))
        state.hook_state.update(
            continuous_loop=True, continuous_phase="aborted",
            continuous_error_turns=99, continuous_abort_reason="whatever",
        )
        chat_mod._set_continuous_loop(state, True)   # 默认 reset_phase=True
        assert state.hook_state["continuous_phase"] == "running"
        assert state.hook_state["continuous_error_turns"] == 0
        assert "continuous_abort_reason" not in state.hook_state
