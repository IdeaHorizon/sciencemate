"""v0.7：external signal mechanism 测试。

3 个 primitive：
  - core.signal.write_signal / read_signal / clear_signal
  - core.loop_hooks_builtin external_signal_check hook
  - core.agent_loop _persist_messages_checkpoint / load_messages_checkpoint
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.signal import (
    read_signal, write_signal, clear_signal,
)
from core.state import State


# ─────────────────────────────────────────────────────────────────────────────
# 1. signal primitive
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_write_read_clear(tmp_path, monkeypatch):
    bootstrap()
    # signal 用 paths.project_dir，自动跟 conftest 的 HARNESS_FRAMEWORK_HOME 走
    sig = write_signal("p1", "inject", "hello")
    assert sig["action"] == "inject"
    assert sig["content"] == "hello"
    assert "written_at" in sig

    got = read_signal("p1")
    assert got["action"] == "inject"
    assert got["content"] == "hello"

    assert clear_signal("p1") is True
    assert read_signal("p1") is None
    assert clear_signal("p1") is False  # 已删 → False


def test_signal_action_pause_expands_template(tmp_path):
    bootstrap()
    sig = write_signal("p_pause", "pause")
    assert sig["action"] == "pause"
    # pause action 自动展成 request_human_input 提示模板
    assert "request_human_input" in sig["content"]
    assert sig["raw_user_content"] == ""    # user 没传 content


def test_signal_action_abort_expands_template(tmp_path):
    bootstrap()
    sig = write_signal("p_abort", "abort")
    assert sig["action"] == "abort"
    assert "wrap up" in sig["content"].lower()
    assert "scale" in sig["content"].lower() or "不要继续" in sig["content"]


def test_signal_inject_requires_content(tmp_path):
    bootstrap()
    with pytest.raises(ValueError, match="非空 content"):
        write_signal("p_no_content", "inject", "")


def test_signal_unknown_action_rejected(tmp_path):
    bootstrap()
    with pytest.raises(ValueError, match="action 必须"):
        write_signal("p_bad", "kill")    # 不存在的 action


def test_signal_overwrite_protection(tmp_path):
    bootstrap()
    write_signal("p_dup", "inject", "first")
    with pytest.raises(FileExistsError, match="已有 pending signal"):
        write_signal("p_dup", "inject", "second")
    # overwrite=True 应通过
    sig = write_signal("p_dup", "inject", "override", overwrite=True)
    assert sig["content"] == "override"


def test_signal_no_project_id_no_signal(tmp_path):
    bootstrap()
    # 不应崩 —— signal 系统对没 project_id 的 state 没意义但应 graceful
    got = read_signal("nonexistent_project_xyz")
    assert got is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. external_signal_check hook
# ─────────────────────────────────────────────────────────────────────────────

def test_hook_no_signal_no_inject(tmp_path):
    """没 signal 文件时 hook 不注入任何东西。"""
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _external_signal_on_turn_start
    from core.loop_hooks import HookContext

    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_nosignal")
    ctx = HookContext(
        harness=NodeHarness(node_type="_orchestrator"),
        state=state, messages=[], turn=1,
    )
    result = _external_signal_on_turn_start(ctx)
    assert result is None


def test_hook_inject_signal_becomes_system_message(tmp_path):
    """有 inject signal → hook 返一条 system message 含 content + 消费 signal 文件。"""
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _external_signal_on_turn_start
    from core.loop_hooks import HookContext

    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_inj")
    write_signal("p_inj", "inject", "请立刻 wrap up")

    ctx = HookContext(
        harness=NodeHarness(node_type="_orchestrator"),
        state=state, messages=[], turn=5,
    )
    result = _external_signal_on_turn_start(ctx)
    assert result is not None and len(result) == 1
    msg = result[0]
    assert msg.role == "system"
    assert "外部 signal 注入" in msg.content
    assert "请立刻 wrap up" in msg.content
    assert "inject" in msg.content

    # 消费后 signal 文件应消失
    assert read_signal("p_inj") is None


def test_hook_pause_signal_injects_request_human_input_template(tmp_path):
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _external_signal_on_turn_start
    from core.loop_hooks import HookContext

    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_pause_hook")
    write_signal("p_pause_hook", "pause")

    ctx = HookContext(
        harness=NodeHarness(node_type="_orchestrator"),
        state=state, messages=[], turn=3,
    )
    result = _external_signal_on_turn_start(ctx)
    assert result and "request_human_input" in result[0].content


def test_hook_abort_signal_injects_wrap_up_template(tmp_path):
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _external_signal_on_turn_start
    from core.loop_hooks import HookContext

    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_abort_hook")
    write_signal("p_abort_hook", "abort")

    ctx = HookContext(
        harness=NodeHarness(node_type="_orchestrator"),
        state=state, messages=[], turn=7,
    )
    result = _external_signal_on_turn_start(ctx)
    assert result and "wrap up" in result[0].content.lower()


def test_orchestrator_yaml_has_external_signal_check_enabled():
    """_orchestrator/harness.yaml 默认必须启用 external_signal_check。"""
    from core.loader import load_harness
    h = load_harness("_orchestrator")
    assert "external_signal_check" in h.loop_hooks, (
        "_orchestrator 必须在 loop_hooks 含 external_signal_check —— "
        "否则 v0.7 外部 signal 机制对 orchestrator 不生效。"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. messages checkpointing
# ─────────────────────────────────────────────────────────────────────────────

def test_checkpoint_persist_and_load(tmp_path):
    bootstrap()
    from core.agent_loop import (
        _persist_messages_checkpoint, load_messages_checkpoint,
    )
    from core.llm import LLMMessage

    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_ckpt")
    msgs = [
        LLMMessage(role="system", content="system prompt"),
        LLMMessage(role="user", content="hello"),
        LLMMessage(role="assistant", content="hi", tool_calls=[{"id": "tc1"}]),
        LLMMessage(role="tool", tool_call_id="tc1", name="some_tool",
                    content="result"),
    ]
    _persist_messages_checkpoint(state, msgs, turn=3)
    loaded = load_messages_checkpoint(state)
    assert loaded is not None
    loaded_msgs, last_turn = loaded
    assert last_turn == 3
    assert len(loaded_msgs) == 4
    assert loaded_msgs[0].role == "system"
    assert loaded_msgs[2].tool_calls == [{"id": "tc1"}]
    assert loaded_msgs[3].tool_call_id == "tc1"


def test_checkpoint_missing_returns_none(tmp_path):
    bootstrap()
    from core.agent_loop import load_messages_checkpoint
    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_nockpt")
    assert load_messages_checkpoint(state) is None


def test_checkpoint_corrupt_returns_none(tmp_path):
    bootstrap()
    from core.agent_loop import load_messages_checkpoint
    state = State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id="p_corrupt")
    (state.root / "messages_checkpoint.json").write_text("not json {",
                                                          encoding="utf-8")
    assert load_messages_checkpoint(state) is None


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_show_no_signal(capsys, tmp_path):
    bootstrap()
    from core.signal import _cli_main
    rc = _cli_main(["signal", "show", "no_project"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no pending signal" in out


def test_cli_inject_writes_signal(capsys, tmp_path):
    bootstrap()
    from core.signal import _cli_main
    rc = _cli_main(["signal", "inject", "p_cli", "test msg"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote signal" in out
    assert read_signal("p_cli")["content"] == "test msg"
