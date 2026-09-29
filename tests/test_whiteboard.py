"""白板：覆盖语义 + 容量硬上限。

回放的是 2026-08-13 v26 那条 run：append 语义的 scratchpad 涨到 639 条 / 50KB，
每轮全量注入，模型连着 591 轮只写笔记不干活（1482 轮 / 117M tokens）。
"""
from __future__ import annotations

import json

import pytest

from core import whiteboard
from core.state import State


def _state(tmp_path):
    return State.new("hypothesis", tmp_path / "rt", project_id="p")


def test_write_replaces_the_board_instead_of_appending(tmp_path):
    """核心语义：一块板子，不是一本日志。"""
    state = _state(tmp_path)
    whiteboard.write(state, "第一版：正在设计 prereg", turn=1)
    whiteboard.write(state, "第二版：prereg 已冻结，下一步 experiment", turn=2)

    assert state.scratchpad == "第二版：prereg 已冻结，下一步 experiment"
    assert "第一版" not in state.scratchpad
    assert state.scratchpad_revision == 2
    assert state.scratchpad_revised_turn == 2


def test_board_cannot_grow_without_bound(tmp_path):
    """写 1000 次，板子仍然只有一块的大小 —— 由构造保证，不靠模型自觉。"""
    state = _state(tmp_path)
    for i in range(1000):
        whiteboard.write(state, f"状态 {i}：在跑 L={i} 的模拟", turn=i)
    assert whiteboard.measure(state.scratchpad) <= whiteboard.capacity_tokens()
    assert state.scratchpad_revision == 1000        # 写了 1000 次
    assert len(state.scratchpad) < 200              # 板子还是一行


def test_oversized_write_is_refused_and_the_old_board_survives(tmp_path):
    """超容量**拒绝写入且不动原板** —— 删什么是语义判断，留给模型。"""
    state = _state(tmp_path)
    whiteboard.write(state, "有用的现状：H1 待验证", turn=1)
    huge = "废话。" * 4000

    result = whiteboard.write(state, huge, turn=2)

    assert result["status"] == "error"
    assert result["board_unchanged"] is True
    assert state.scratchpad == "有用的现状：H1 待验证"     # 原板毫发无伤
    assert state.scratchpad_revision == 1                # 没算一次改写
    assert "容量" in result["error"]
    # 报错必须说清"该往哪儿放"，否则模型只能重试同一条路
    assert "artifact" in result["error"]


def test_empty_write_wipes_the_board(tmp_path):
    """判决拆除（whiteboard:97）：空 content = 擦板，不是「交白卷」被拒。

    从前这里返回 error；现在照常 +1 revision、账上记一次改写、render 返 None。
    把拒绝加回去这条必转红。
    """
    state = _state(tmp_path)
    whiteboard.write(state, "有内容的板子", turn=1)

    result = whiteboard.write(state, "   ", turn=2)

    assert result["status"] == "success"
    assert result["board"] == "" and result["tokens_used"] == 0
    assert state.scratchpad == "" and state.scratchpad_revision == 2
    assert whiteboard.render(state, turn=3) is None
    events = [json.loads(line) for line in
              state.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    revisions = [e for e in events if e.get("event") == "whiteboard_revised"]
    assert revisions and revisions[-1]["revision"] == 2   # 擦板也如实记一次改写


def test_result_shows_remaining_space_not_a_growing_counter(tmp_path):
    """旧的 {"note_count": 639} 是只涨的计数器，读起来像进展。

    新返回值让模型每次都看见自己在管理一块**有限**的资源。
    """
    state = _state(tmp_path)
    result = whiteboard.write(state, "当前状态", turn=1)
    assert "note_count" not in result
    assert result["tokens_remaining"] == result["capacity_tokens"] - result["tokens_used"]
    assert result["tokens_remaining"] > 0
    assert result["board"] == "当前状态"


def test_capacity_is_configurable_but_never_unbounded(monkeypatch):
    monkeypatch.setenv("HARNESS_WHITEBOARD_MAX_TOKENS", "2500")
    assert whiteboard.capacity_tokens() == 2500
    for bad in ("0", "-1", "unlimited", ""):
        monkeypatch.setenv("HARNESS_WHITEBOARD_MAX_TOKENS", bad)
        assert whiteboard.capacity_tokens() == 1000      # 落回默认，永远有限


def test_legacy_append_log_is_adopted_not_dropped():
    """续连旧 checkpoint：639 条笔记读成一块板子，内容不丢。"""
    legacy = [f"笔记 {i}" for i in range(639)]
    board = whiteboard.adopt_legacy(legacy)
    assert board.startswith("笔记 0")
    assert board.endswith("笔记 638")
    assert whiteboard.adopt_legacy(None) == ""
    assert whiteboard.adopt_legacy("已经是板子") == "已经是板子"


def test_oversized_legacy_board_says_so_instead_of_silently_truncating(tmp_path):
    """旧板超容量：注入侧**说清楚**，不偷偷截断（截断哪段是语义判断）。"""
    state = _state(tmp_path)
    state.scratchpad = whiteboard.adopt_legacy([f"第 {i} 条笔记，内容若干。" for i in range(639)])
    rendered = whiteboard.render(state, turn=5)
    assert rendered and "超出容量" in rendered


def test_render_states_the_age_as_fact_without_a_verdict(tmp_path):
    state = _state(tmp_path)
    whiteboard.write(state, "卡在 LAMMPS 编译", turn=10)
    rendered = whiteboard.render(state, turn=45)
    assert "turn 10" in rendered and "35 轮" in rendered
    # 事实层不下判决 —— 判决归 progress_breaker
    assert "熔断" not in rendered and "停机" not in rendered


def test_render_returns_none_for_an_empty_board(tmp_path):
    assert whiteboard.render(_state(tmp_path), turn=3) is None


@pytest.mark.asyncio
async def test_tool_overwrites_and_framework_reminders_do_not_touch_the_board(tmp_path):
    """`note=` 是框架合成调用的入口（experiment 的 empty_stop_guard 等）。

    那两处要的是"这一轮不空 + 提醒送到模型眼前"，提醒本身就在 tool_call 参数里 ——
    所以它不该动模型的板子。板子是模型的。
    """
    from shared.tools.builtin import _write_scratchpad

    state = _state(tmp_path)
    await _write_scratchpad(state, content="我的板子")

    out = await _write_scratchpad(state, note="[empty_stop_guard] 不要停，继续调工具")
    assert out["status"] == "success"
    assert out["board_unchanged"] is True
    assert state.scratchpad == "我的板子"          # 没被框架提醒覆盖掉
    assert state.scratchpad_revision == 1


def test_tool_schema_only_advertises_overwrite(tmp_path):
    """模型看得见的只有 content。`note` 是框架内部入口，不进 schema。"""
    from core.tool_registry import get_tool

    schema = get_tool("write_scratchpad").parameters_schema
    assert schema["required"] == ["content"]
    assert "note" not in schema["properties"]
    assert "整块覆盖" in get_tool("write_scratchpad").description
