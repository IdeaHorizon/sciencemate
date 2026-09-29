"""被杀的 worker 只丢**当前 turn 内**那一小段（2026-08-19 实证）。

## 为什么要有这条

`reap_orphaned_session_worker` 里曾有一句注释："给它 2 秒把 conversation.json
存盘"。这个前提是**假的** —— worker 没有 SIGTERM handler，Python 默认直接终止，
finally / atexit 都不跑。误导在于它让读代码的人以为退出路径上有一层保护，于是
下一个人会去"修复"那层根本不存在的东西（我 2026-08-19 差点就去加 handler）。

真正兜住工作的不是退出时保存，是**边跑边落**：

    agent_loop 每个 turn 末      → messages_checkpoint.json
    conversation_store.load_conversation → 取两份里**更新**的那份

所以任何死法（SIGTERM / SIGKILL / OOM / 断电）损失的都只是当前 turn 内尚未
落到 checkpoint 的那一段。这条测试钉住这个不变量本身 —— 谁把 checkpoint 停写、
或者把恢复改回只读 conversation.json，这里当场红。
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

from core.conversation_store import (
    checkpoint_path,
    conversation_path,
    load_conversation,
    save_conversation,
)
from core.llm import LLMMessage


def _state(root):
    return SimpleNamespace(
        root=root, hook_state={}, tokens_used=0, tool_calls_made=0,
        scratchpad="", scratchpad_revision=0, scratchpad_revised_turn=0,
        run_id="r1", node_type="_orchestrator", tenant_id="t", project_id="p",
        session_id="s",
    )


def _write_checkpoint(root, messages, turn):
    path = checkpoint_path(SimpleNamespace(root=root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "turn": turn, "written_at": "2026-08-19T00:00:00Z",
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_recovery_takes_the_newer_record(tmp_path):
    """**这就是被杀之后还剩什么**：turn 级 checkpoint 比上一轮的快照新，就用它。

    实测过一个真被 kill 的会话：conversation.json 19 条（18:18），
    checkpoint 39 条（20:20）—— 恢复拿到 39 条。
    """
    state = _state(tmp_path)
    save_conversation(state, [LLMMessage(role="user", content="第一轮")])
    time.sleep(0.01)
    _write_checkpoint(tmp_path, [
        LLMMessage(role="user", content="第一轮"),
        LLMMessage(role="assistant", content="turn 3 干到一半"),
    ], turn=3)

    restored = _state(tmp_path)
    msgs = load_conversation(restored)
    assert msgs is not None and len(msgs) == 2
    assert msgs[-1].content == "turn 3 干到一半"
    assert restored.hook_state.get("_resumed_from_turn_checkpoint") is True


def test_a_finished_turn_keeps_its_richer_snapshot(tmp_path):
    """反向：正常跑完的那一轮，conversation.json 更新且带元数据 —— 用它。

    少了这条，"总是用 checkpoint" 会把 scratchpad / hook_state 这些只存在于
    conversation.json 的元数据丢掉（本机真实数据里正常结束的会话恰恰是
    conversation 比 checkpoint 多几条）。
    """
    state = _state(tmp_path)
    _write_checkpoint(tmp_path, [LLMMessage(role="user", content="旧")], turn=1)
    time.sleep(0.01)
    state.scratchpad = "白板内容"
    save_conversation(state, [
        LLMMessage(role="user", content="旧"),
        LLMMessage(role="assistant", content="收尾那句"),
    ])

    restored = _state(tmp_path)
    msgs = load_conversation(restored)
    assert msgs is not None and len(msgs) == 2
    assert msgs[-1].content == "收尾那句"
    assert restored.hook_state.get("_resumed_from_turn_checkpoint") is not True
    assert restored.scratchpad == "白板内容", "元数据只在 conversation.json 里"


def test_the_reaper_does_not_claim_sigterm_saves_anything():
    """那句假注释不许回来。

    判据针对**这个断言本身**：'SIGTERM 会让它存盘' 是错的，写在注释里会让
    下一个人去修一层不存在的保护。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "platform" / "backend" / "app" / "services"
           / "harness_sessions.py").read_text(encoding="utf-8")
    assert "给它 2 秒把 conversation.json 存盘" not in src, (
        "这句注释断言 SIGTERM 会触发存盘 —— worker 没有 SIGTERM handler，"
        "Python 默认直接终止。工作靠 turn 级 checkpoint 兜，不靠退出路径。"
    )
