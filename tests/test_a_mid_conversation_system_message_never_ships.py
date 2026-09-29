"""会话中段的 system 消息不许上线 —— 模型会复读它，严格的网关直接 400。

2026-09-15 同一天两笔代价各兑现一次：

- 模型侧：中段 system 在训练分布里几乎不存在，模型归属不了说话人，把它当成
  "自己没说完的话"接着写（`core/llm.py` 里那段实测：13/14 复读，输出 191 vs 61 token）。
- 协议侧：yuankk 的 `qwen3.8-27b` 网关回
  `{"message":"System message must be at the beginning."}` —— HTTP 400，一整轮没了。
  真身是 `chat.py` 往消息尾巴上 append 的四条 system（项目计数快照 / 空回复 nudge /
  长粘贴防注入框 / 多意图提醒）。

`opening_system_prompt` 的文档写着"存在这个函数是为了让护栏能扫盘"，**而那道护栏
一直没写**（不在场的检查和通过的检查长得一样）。这个文件就是它。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.llm import (
    FRAMEWORK_NOTICE_OPEN,
    LLMMessage,
    _only_an_opening_system_message,
    framework_notice,
    opening_system_prompt,
)


def _roles(messages):
    return [m.role for m in messages]


def test_an_opening_system_prompt_is_left_alone():
    messages = [opening_system_prompt("你是主 agent"), LLMMessage(role="user", content="hi")]
    assert _only_an_opening_system_message(messages) is messages


def test_a_system_message_in_the_middle_becomes_a_framework_notice():
    """yuankk 那条对话的真实形状：[system, user, system(计数快照), user]。"""
    messages = [
        opening_system_prompt("你是主 agent"),
        LLMMessage(role="user", content="（没有上游上下文）"),
        LLMMessage(role="system", content="📊 项目真实计数快照：artifacts=0"),
        LLMMessage(role="user", content="hi"),
    ]

    out = _only_an_opening_system_message(messages)

    assert _roles(out) == ["system", "user", "user", "user"], "开篇那条留着，中段那条变 user"
    assert FRAMEWORK_NOTICE_OPEN in (out[2].content or ""), "要带归属信封，不能和用户发言混在一起"
    assert "artifacts=0" in (out[2].content or ""), "内容一个字不能丢"
    assert messages[2].role == "system", "不就地改调用方的列表"


def test_a_system_message_that_is_not_even_first_is_also_caught():
    messages = [LLMMessage(role="user", content="hi"), LLMMessage(role="system", content="x")]
    assert _roles(_only_an_opening_system_message(messages)) == ["user", "user"]


def test_chat_py_speaks_through_the_two_helpers_and_never_builds_a_system_message():
    """这一层是网不是拐杖：发声侧照旧该直接用 framework_notice。

    判据按文件扫：chat.py 是调度器自己拼消息的地方，合法的 system 只有"一次调用的
    第一条"（`opening_system_prompt`）。节点 hook 里的 `role="system"` 不在此列 ——
    它们由 `loop_hooks._as_framework_notices` 在咽喉统一转成 user。
    """
    source = Path(__file__).resolve().parents[1] / "chat.py"
    text = source.read_text(encoding="utf-8")
    offenders = [line.strip() for line in text.splitlines()
                 if 'LLMMessage(role="system"' in line]
    assert offenders == [], f"chat.py 里手搓 system 消息的地方：{offenders}"
    assert "framework_notice(" in text
    assert "opening_system_prompt(" in text


@pytest.mark.asyncio
async def test_the_request_that_actually_leaves_carries_no_mid_conversation_system(monkeypatch):
    """接线，不是函数本身：判据落在**真正发出去的 payload** 上。

    只测 `_only_an_opening_system_message` 的话，把它从 `chat()` 里摘掉不会转红
    —— 那正是"修复落在没人走的路上"。
    """
    from core import llm as llm_mod
    from core.llm import LLMClient

    sent: list[dict] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):  # noqa: A002
            sent.append(json)
            return _Resp()

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = ""

        @property
        def is_success(self):
            return True

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setenv("LLM_STREAM", "0")
    monkeypatch.setenv("HARNESS_LLM_CACHE", "off")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)

    await LLMClient(api_key="k", model="m", base_url="http://x", max_retries=0).chat([
        opening_system_prompt("你是主 agent"),
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="system", content="📊 项目真实计数快照：artifacts=0"),
    ])

    roles = [m["role"] for m in sent[0]["messages"]]
    assert roles == ["system", "user", "user"], f"发出去的是 {roles}"
    assert "artifacts=0" in sent[0]["messages"][2]["content"]
