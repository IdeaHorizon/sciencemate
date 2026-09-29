"""轮次用尽的收尾宽限调用，以及 MEMORY 索引超限的 fail-loud。

共同的道理：**别让"没做完"和"什么都没有"长得一样**。
"""
from __future__ import annotations

import asyncio

import pytest

from core.agent_loop import _GRACE_PROMPT, _grace_wrapup
from core.llm import LLMMessage


class _Resp:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []
        self.usage = {}


class _LLM:
    """记录收到的调用，好断言"没带工具"。"""

    def __init__(self, content="做完了 A 和 B，产物在 out/x.csv；差 C。"):
        self.calls: list[dict] = []
        self._content = content

    async def chat(self, messages, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools})
        return _Resp(self._content)


class _State:
    def __init__(self):
        self.events: list[tuple] = []

    def append_transcript(self, kind, **kw):
        self.events.append((kind, kw))


def test_grace_call_carries_no_tools():
    """要点：不给工具。带着工具它会接着干活，那是偷加一轮不是收尾。"""
    llm = _LLM()
    st = _State()
    text = asyncio.run(_grace_wrapup(
        llm=llm, messages=[LLMMessage(role="user", content="干活")],
        state=st, turn=40))

    assert text.startswith("做完了")
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None, "收尾调用不得带工具"


def test_grace_prompt_asks_for_handoff_not_apology():
    """交接三件事：做完了什么、卡在哪、从哪续上。"""
    assert "不要再调用任何工具" in _GRACE_PROMPT
    assert "落盘" in _GRACE_PROMPT or "产物" in _GRACE_PROMPT
    assert "续上" in _GRACE_PROMPT
    assert "不要道歉" in _GRACE_PROMPT


def test_grace_call_appends_to_history_without_mutating_it():
    """不能把收尾问句塞进真实历史 —— 那会污染 resume 后的重放。"""
    llm = _LLM()
    msgs = [LLMMessage(role="user", content="干活")]
    before = len(msgs)
    asyncio.run(_grace_wrapup(llm=llm, messages=msgs, state=_State(), turn=9))
    assert len(msgs) == before
    assert len(llm.calls[0]["messages"]) == before + 1


def test_grace_call_records_transcript_event():
    st = _State()
    asyncio.run(_grace_wrapup(llm=_LLM(), messages=[], state=st, turn=40))
    assert any(k == "grace_wrapup" for k, _ in st.events)


def test_grace_failure_falls_back_silently():
    """收尾是锦上添花，不能让它把 run 弄挂。"""
    class _Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("provider down")

    assert asyncio.run(_grace_wrapup(
        llm=_Boom(), messages=[], state=_State(), turn=1)) == ""


def test_grace_empty_response_falls_back():
    assert asyncio.run(_grace_wrapup(
        llm=_LLM(content="   "), messages=[], state=_State(), turn=1)) == ""


# ── MEMORY 容量 fail-loud ──────────────────────────────────────────────────
#
# 2026-08-21 删掉三条：`test_index_overflow_writes_then_raises` /
# `test_index_under_cap_is_silent` / `test_non_strict_keeps_old_behavior`。
#
# 它们测的是 `MemoryV2.write_index(strict=)` + `_MEMORY_MD_BYTE_CAP` ——
# 那套机制随记忆重建整体退场（判据见 tests/test_memory_rebuild_tombstones.py）。
# 继任者就在下面这条：容量约束从"重生成索引时"挪到了**写入端**，CLI 和平台
# 两种模式共用一条路径 —— 覆盖面比被删的那三条更大，不是更小。
#
# 删而不是改写：被测的函数不存在了，改写只能改成测继任者，那就是重复。

def test_constitution_overflow_is_refused_loudly_with_a_way_out(tmp_path,
                                                                mem_worktree):
    """接线检查：fail-loud 必须真的走到工具返回值里，而且报错要有出路。

    前身是 `regenerate_index(strict=True)` —— 那套机制只在 CLI 模式生效
    （worktree 分支下 regenerate 整个短路直接 return success），是"机制在、
    路径没接上"的典型。现在容量约束在**写入端**，两种模式共用一条路径。

    另一半同样重要：硬墙必须指出该往哪放。上一代的溢出文案让 curator
    "归档旧 episodes"—— 而那个动作没有工具、没有 API，是一条不存在的出路。
    """
    import asyncio

    from core import memory as M
    from core.state import State
    from core.tool_registry import execute as run_tool

    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                   project_id="overflow", project_worktree=mem_worktree)
    M.ensure_skeleton(st)
    st.hook_state["_user_utterances"] = ["这条规矩你记一下"]

    huge = "铁" * M.CONSTITUTION_BYTE_CAP
    res = asyncio.run(run_tool("memory_write", st, section="law",
                               text=huge, derived_from=["这条规矩你记一下"]))
    assert res["status"] == "error"
    assert res["code"] == "refused"
    # 出路必须指名道姓，且指向真实存在的去处
    assert "撤掉" in res["error"] and "memory_note" in res["error"]
    assert M.laws(st) == [], "拒了却还是写进去了"
