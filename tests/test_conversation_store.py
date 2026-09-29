"""conversation_store：chat.py 与 run_e2e_dogfood.py 共用的对话持久化（2026-07-08）。

根因回归：两个入口各自维护 conversation.json 读写，dogfood 写 list、chat 写
dict 且读取假定 dict —— 换入口续连直接 `'list' object has no attribute 'get'`
崩溃；且 list 格式丢 scratchpad/hook_state，导致 first-turn 引导重复注入。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.conversation_store import (
    conversation_path,
    load_conversation,
    save_conversation,
)
from core.llm import LLMMessage
from core.state import State


def _make_state(tmp_path: Path) -> State:
    root = tmp_path / "orchestrator__test-proj"
    root.mkdir(parents=True)
    return State(run_id="orchestrator__test-proj", node_type="_orchestrator",
                 root=root, project_id="test-proj", project_root=None)


def test_round_trip_preserves_messages_and_metadata(tmp_path):
    state = _make_state(tmp_path)
    state.tokens_used = 12345
    state.tool_calls_made = 7
    state.scratchpad = "note 1\nnote 2"
    state.hook_state["auto_approve_enabled"] = True
    msgs = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="你好"),
        LLMMessage(role="assistant", content="回复",
                    reasoning_content="思考过程"),
        LLMMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "type": "function",
                                  "function": {"name": "f", "arguments": "{}"}}]),
        LLMMessage(role="tool", content="result", tool_call_id="c1", name="f"),
    ]
    save_conversation(state, msgs)

    state2 = _make_state(tmp_path / "reload")
    state2.root = state.root   # 指向同一存储目录
    loaded = load_conversation(state2)
    assert loaded is not None and len(loaded) == 5
    assert loaded[1].content == "你好"
    assert loaded[2].reasoning_content == "思考过程"
    assert loaded[3].tool_calls[0]["id"] == "c1"
    assert loaded[4].tool_call_id == "c1" and loaded[4].name == "f"
    # 元数据恢复（之前 dogfood 的 list 格式把这些全丢了）
    assert state2.tokens_used == 12345
    assert state2.tool_calls_made == 7
    assert state2.scratchpad == "note 1\nnote 2"
    assert state2.hook_state.get("auto_approve_enabled") is True


def test_legacy_list_format_still_loads(tmp_path):
    """旧 dogfood 写的裸 list 格式必须能读（换入口续连不再崩）。"""
    state = _make_state(tmp_path)
    legacy = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi", "tool_call_id": None,
         "name": None, "tool_calls": None},
        {"role": "assistant", "content": "yo"},
    ]
    conversation_path(state).write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    loaded = load_conversation(state)
    assert loaded is not None and len(loaded) == 3
    assert loaded[2].content == "yo"
    # 元数据在旧格式里不存在 → 保持默认值，不崩
    assert state.tokens_used == 0


def test_missing_and_corrupt_files(tmp_path):
    state = _make_state(tmp_path)
    assert load_conversation(state) is None          # 不存在
    conversation_path(state).write_text("{not json", encoding="utf-8")
    assert load_conversation(state) is None          # 损坏
    conversation_path(state).write_text('"just a string"', encoding="utf-8")
    assert load_conversation(state) is None          # 非 dict 非 list


def test_hook_state_unjsonable_values_dropped(tmp_path):
    state = _make_state(tmp_path)
    state.hook_state["good"] = {"a": 1}
    state.hook_state["bad"] = {1, 2, 3}      # set 不可 json
    save_conversation(state, [LLMMessage(role="user", content="x")])
    data = json.loads(conversation_path(state).read_text(encoding="utf-8"))
    assert "good" in data["hook_state"]
    assert "bad" not in data["hook_state"]


def test_chat_and_dogfood_share_the_store():
    """两个入口必须 import 同一实现，不得自带副本（回归护栏）。"""
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    for fname in ("chat.py", "scripts/run_e2e_dogfood.py"):
        src = (root / fname).read_text(encoding="utf-8")
        assert "core.conversation_store" in src, f"{fname} 未使用共享 conversation_store"
        # 不允许再出现本地私有的 conversation.json 写入逻辑
        assert src.count('json.dumps(payload') == 0, f"{fname} 仍有自带的持久化副本"
