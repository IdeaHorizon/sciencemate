"""续连必须看得见 turn 级 checkpoint。

2026-08-04 实测事故（e2e8）：orchestrator 从 15:05 一直跑到被 API 打死，
第一轮 REPL 对话从未结束。重启时 banner 显示"新对话"而不是"续连"——
一小时四十五分钟的编排工作全丢。

而盘上明明躺着：

    conversation.json          不存在（chat.py 在 REPL 轮末才写）
    messages_checkpoint.json   58 条消息，16:40 写的，完整

`_persist_messages_checkpoint` 的 docstring 原文就是"kill 后能从这里
resume"，`load_messages_checkpoint` 也早就写好了 —— 但只有
scripts/run_e2e_dogfood.py 在用，chat.py 的续连路径从来没接上。

第 9 次同一个模式：机制存在，只是写它的人和读它的人没接上。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from core.conversation_store import (
    checkpoint_path,
    conversation_path,
    load_conversation,
    save_conversation,
)
from core.llm import LLMMessage
from core.state import State


@pytest.fixture()
def state(tmp_path) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p1")


def _write_checkpoint(st: State, texts: list[str], *, turn: int = 3) -> None:
    p = checkpoint_path(st)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "turn": turn,
        "messages": [{"role": "user", "content": t} for t in texts],
    }, ensure_ascii=False), encoding="utf-8")


def _touch_newer(path, base) -> None:
    """把 path 的 mtime 明确推到 base 之后（避免同秒竞态）。"""
    t = os.path.getmtime(base) + 10
    os.utime(path, (t, t))


# ── 事故本体 ────────────────────────────────────────────────────────────

def test_resumes_from_checkpoint_when_no_conversation_json(state):
    """第一轮 REPL 没跑完就重启 —— 此前直接冷启动。"""
    _write_checkpoint(state, ["把材料写成论文", "好的，我先读 README"])
    assert not conversation_path(state).exists()

    msgs = load_conversation(state)
    assert msgs is not None, "盘上有完整 checkpoint 却当成新对话"
    assert [m.content for m in msgs] == ["把材料写成论文", "好的，我先读 README"]


def test_resume_source_is_recorded_not_silent(state):
    """从哪份恢复的必须留痕 —— 元数据缺失（scratchpad/hook_state）是有代价的，
    不能让人事后查不出来。"""
    _write_checkpoint(state, ["x"])
    load_conversation(state)
    assert state.hook_state.get("_resumed_from_turn_checkpoint") is True


def test_checkpoint_wins_when_strictly_newer(state):
    """上一轮 REPL 存过盘，之后又跑了几个 turn 才崩 —— 该用 checkpoint。"""
    save_conversation(state, [LLMMessage(role="user", content="旧的")])
    _write_checkpoint(state, ["旧的", "新的第 2 轮", "新的第 3 轮"])
    _touch_newer(checkpoint_path(state), conversation_path(state))

    msgs = load_conversation(state)
    assert [m.content for m in msgs] == ["旧的", "新的第 2 轮", "新的第 3 轮"]


def test_conversation_wins_when_not_older(state):
    """conversation.json 带元数据（scratchpad/hook_state），同样新时优先它。"""
    _write_checkpoint(state, ["checkpoint 版"])
    state.scratchpad = "一块板子"
    save_conversation(state, [LLMMessage(role="user", content="conversation 版")])
    _touch_newer(conversation_path(state), checkpoint_path(state))

    fresh = State.new(node_type="_orchestrator",
                      base_dir=state.root.parent.parent, project_id="p1")
    fresh.root = state.root
    msgs = load_conversation(fresh)
    assert [m.content for m in msgs] == ["conversation 版"]
    assert fresh.scratchpad == "一块板子"
    assert not fresh.hook_state.get("_resumed_from_turn_checkpoint")


def test_corrupt_conversation_falls_back_to_checkpoint(state):
    """一个坏文件不该把好的那份一起埋了。"""
    _write_checkpoint(state, ["还活着的历史"])
    p = conversation_path(state)
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    _touch_newer(p, checkpoint_path(state))

    msgs = load_conversation(state)
    assert msgs is not None
    assert [m.content for m in msgs] == ["还活着的历史"]


# ── 不许过度触发 ────────────────────────────────────────────────────────

def test_no_files_still_returns_none(state):
    """真·新项目还是冷启动，不许凭空造历史。"""
    assert load_conversation(state) is None


def test_empty_checkpoint_is_not_a_resume(state):
    """空 checkpoint = 没历史，不是"续连 0 条消息"。"""
    checkpoint_path(state).parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path(state).write_text(json.dumps({"turn": 0, "messages": []}))
    assert load_conversation(state) is None


def test_checkpoint_reader_itself_returns_none_on_empty(state):
    """直接钉里层契约。

    变异测试逼出来的：外层 `if msgs:` 会把空列表兜住，所以只测
    load_conversation 时，里层那道"空 = 没历史"的判断坏掉也没人发现。
    两层防御是好事，但每一层都得自己被测到，否则它就是死代码。
    """
    from core.conversation_store import _load_checkpoint_messages
    checkpoint_path(state).parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path(state).write_text(json.dumps({"turn": 0, "messages": []}))
    assert _load_checkpoint_messages(state) is None


def test_corrupt_checkpoint_does_not_break_normal_resume(state):
    """checkpoint 坏了不影响正常的 conversation.json 续连。"""
    save_conversation(state, [LLMMessage(role="user", content="正常历史")])
    checkpoint_path(state).write_text("{{{ 坏的", encoding="utf-8")
    _touch_newer(checkpoint_path(state), conversation_path(state))

    msgs = load_conversation(state)
    assert [m.content for m in msgs] == ["正常历史"]


def test_metadata_still_restored_on_normal_path(state):
    """老路径行为不变。"""
    state.tokens_used = 1234
    state.scratchpad = "note"
    state.hook_state["k"] = "v"
    save_conversation(state, [LLMMessage(role="user", content="hi")])

    fresh = State.new(node_type="_orchestrator",
                      base_dir=state.root.parent.parent, project_id="p1")
    fresh.root = state.root
    load_conversation(fresh)
    assert fresh.tokens_used == 1234
    assert fresh.scratchpad == "note"
    assert fresh.hook_state["k"] == "v"


def test_checkpoint_written_by_agent_loop_is_readable(tmp_path):
    """路径契约：agent_loop 写的位置就是这里读的位置。写的人和读的人必须
    指向同一个文件 —— 这次事故的根子就是两边没接上。"""
    from core.agent_loop import _checkpoint_path
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p1")
    assert _checkpoint_path(st) == checkpoint_path(st)
