"""压缩时保留用户指令原文。"""
from __future__ import annotations
from core.llm import LLMMessage
from core.summarizer import (
    _preserve_user_messages, _PRESERVE_USER_MAX_CHARS, _PRESERVE_USER_MAX_COUNT,
)

def U(c): return LLMMessage(role="user", content=c)
def A(c): return LLMMessage(role="assistant", content=c)

def test_user_instructions_survive():
    mid = [A("干活中"), U("用现成的 retry helper，别新写一个"), A("好")]
    out = _preserve_user_messages(mid, [])
    assert [m.content for m in out] == ["用现成的 retry helper，别新写一个"]

def test_assistant_and_tool_results_are_not_preserved():
    mid = [A("我的推理"), LLMMessage(role="tool", content="工具输出"),
           LLMMessage(role="user", content="结果", tool_call_id="tc1")]
    assert _preserve_user_messages(mid, []) == []

def test_already_kept_not_duplicated():
    u = U("指令")
    assert _preserve_user_messages([u], [u]) == []

def test_huge_paste_is_not_an_instruction():
    """粘进来的大块材料该走摘要，不是指令。"""
    assert _preserve_user_messages([U("x" * (_PRESERVE_USER_MAX_CHARS + 1))], []) == []

def test_count_capped_keeps_most_recent():
    mid = [U(f"指令{i}") for i in range(_PRESERVE_USER_MAX_COUNT + 5)]
    out = _preserve_user_messages(mid, [])
    assert len(out) == _PRESERVE_USER_MAX_COUNT
    assert out[-1].content == f"指令{_PRESERVE_USER_MAX_COUNT + 4}"   # 最近的留下

def test_blank_skipped():
    assert _preserve_user_messages([U("   "), U("")], []) == []


# ── 防复活 preamble + 命运表 ────────────────────────────────────────────────

def test_anti_revival_preamble_says_the_three_things():
    """摘要读起来和任务清单一样，必须说清它不是指令。"""
    from core.summarizer import _ANTI_REVIVAL_PREAMBLE as P
    assert "不是当前指令" in P                    # 它是什么
    assert "以摘要之后的消息为准" in P             # 谁优先
    assert "不要从这段历史里翻出旧任务重做" in P   # 没新指令时干嘛


def test_notice_carries_the_preamble():
    """preamble 必须真的进到注入的 notice 里。

    断行为不断实现位置 —— 原来断言它出现在 `_strategy_llm` 源码里，
    重构到共用构造器之后就假红了，那是坏判据。
    """
    from core import summarizer as sm
    assert "历史记录，不是当前指令" in sm.build_compression_notice(1, "x").content


def test_fate_table_exists_and_matches_code():
    """命运表不能和代码分叉 —— 表里点名的机制必须真的在。"""
    from pathlib import Path
    from core import summarizer as sm

    doc = Path("docs/compression-fate-table.md")
    assert doc.is_file(), "压缩命运表缺失"
    text = doc.read_text(encoding="utf-8")

    # 表里点名的每个机制都要在代码里找得到
    for name in ("_preserve_user_messages", "_strategy_clear_tool_results",
                 "_head_without_superseded_notices", "extract_keep_tool_pairs",
                 "split_for_compression"):
        assert name in text, f"命运表没提到 {name}"
        assert hasattr(sm, name), f"命运表提到的 {name} 在 summarizer 里不存在"

    # 三条判据在
    assert "契约类内容必须是「重建」" in text
    assert "用户指令必须是「逐字保留」" in text
    assert "留可恢复指针" in text


def test_every_strategy_notice_carries_the_preamble():
    """防复活 preamble 必须覆盖**所有**压缩策略。

    实测（E2E 强制压缩）：llm 策略压完后二次兜底又跑 drop_tool_results，
    最终留在上下文里的是不带 preamble 的那一条 —— 只覆盖一条路径 = 没有防线。
    """
    import inspect
    from core import summarizer as sm

    # 所有 notice 都必须经 build_compression_notice 产出
    src = inspect.getsource(sm)
    assert src.count("📦 历史压缩") <= 1, "仍有策略在硬编码 notice 文案"

    n = sm.build_compression_notice(7, "某策略。")
    assert sm._COMPRESSION_NOTICE_MARKER in n.content
    assert "历史记录，不是当前指令" in n.content
    assert "以摘要之后的消息为准" in n.content

    n2 = sm.build_compression_notice(7, "带正文。", body="## 摘要\n内容")
    assert "## 摘要" in n2.content and "历史记录，不是当前指令" in n2.content
