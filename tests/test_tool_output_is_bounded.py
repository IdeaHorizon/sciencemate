"""工具结果的出口有界：太大的落盘，进上下文的只是一个指针。

## 事故来源（2026-08-22，632 个真实 checkpoint 实测）

    read_artifact             8 条 > 20 万字符，最大 **1,865,703 字符**
    read_producer_transcript  2 条 > 20 万字符

那条 186 万字符是一张 `clean_results` 的 JSON 平表，约 58 万 tokens ——
**是默认上下文窗口（256k）的 2.3 倍**。632 个 checkpoint 里有 9 个超窗口，
全部是被**单条**结果撑爆的，不是"读了太多次"。

工作集的「至多一份活副本」对它无效（本来就只有一份）；压缩也救不了
（它自己比 context 还大）。唯一的解是**不让它进来**。

判据集中在两点：进上下文的东西必须有界；而"有界"不许靠丢答案换。
"""
from __future__ import annotations

import json

import pytest

import shared.tools  # noqa: F401
from core import bounded_output as bo


class _FakeState:
    def __init__(self, tmp_path):
        self.hook_state: dict = {}
        self.root = tmp_path


def _huge(n: int = bo.MAX_RESULT_CHARS * 2) -> dict:
    return {"status": "success", "total_matched": 3, "passed": True,
            "artifact": {"content": "x" * n}}


# ── 有界 ────────────────────────────────────────────────────────────────────

def test_a_huge_result_never_enters_the_context_whole(tmp_path):
    s = _FakeState(tmp_path)
    out = bo.bound(s, "read_artifact", _huge())
    assert out["oversized"] is True
    assert len(json.dumps(out, ensure_ascii=False)) < bo.MAX_RESULT_CHARS, \
        "指针本身还是超上限了"


def test_a_normal_result_is_untouched(tmp_path):
    s = _FakeState(tmp_path)
    res = {"status": "success", "items": ["a", "b"], "total_matched": 2}
    assert bo.bound(s, "search_kb", res) is res, "正常结果被动了"


def test_a_full_page_of_read_file_is_not_cut_twice(tmp_path):
    """`read_file` 自己有分页（offset/limit + 字节上限），满页返回不该被再截一次。

    这就是本闸阈值取得比 `_MAX_READ_BYTES` 大一档的原因。
    """
    from shared.tools.builtin import _MAX_READ_BYTES

    s = _FakeState(tmp_path)
    full_page = {"status": "success", "content": "x" * _MAX_READ_BYTES,
                 "next_offset": 2000, "bytes_capped": True}
    assert "oversized" not in bo.bound(s, "read_file", full_page)


# ── 有界不许靠丢答案换 ──────────────────────────────────────────────────────

def test_the_answer_scalars_survive(tmp_path):
    """`total_matched` / `passed` 这类顶层标量往往**就是答案**，砍掉等于白跑。"""
    s = _FakeState(tmp_path)
    out = bo.bound(s, "read_artifact", _huge())
    assert out["total_matched"] == 3
    assert out["passed"] is True


def test_the_status_is_inherited_not_stamped(tmp_path):
    """status 是产生方盖的章，本层只截体积、不改判（同工作集的宪法）。"""
    s = _FakeState(tmp_path)
    bad = {"status": "error", "error": "boom", "detail": "y" * (bo.MAX_RESULT_CHARS * 2)}
    out = bo.bound(s, "run_bash", bad)
    assert out["status"] == "error", "失败被截断层改判成了成功"


def test_the_original_is_saved_verbatim(tmp_path):
    s = _FakeState(tmp_path)
    src = _huge()
    out = bo.bound(s, "read_artifact", src)
    saved = json.loads((tmp_path / bo.OVERSIZED_DIRNAME).glob("*.json").__next__()
                       .read_text(encoding="utf-8"))
    assert saved == src, "落盘的不是原样内容"
    assert out["saved_to"].endswith(".json")


def test_the_pointer_says_what_to_do_next(tmp_path):
    """报错/截断必须给出可执行的下一步 —— 对一张大表，筛比通读有用。"""
    s = _FakeState(tmp_path)
    note = bo.bound(s, "read_artifact", _huge())["note"]
    assert "read_file" in note and "run_bash" in note
    assert "grep" in note or "jq" in note
    assert "别再原样重调" in note
    assert "preview" in note


# ── 落盘失败也必须截断 ──────────────────────────────────────────────────────

def test_it_still_truncates_when_it_cannot_save(tmp_path):
    """写不进盘时**不放行原文** —— 放行一次就是一轮必然的上下文崩溃。"""
    class _NoRoot:
        hook_state: dict = {}

    out = bo.bound(_NoRoot(), "read_artifact", _huge())
    assert out["oversized"] is True
    assert "saved_to" not in out
    assert "没能存盘" in out["note"], "没如实说明完整内容丢了"
    assert len(json.dumps(out, ensure_ascii=False)) < bo.MAX_RESULT_CHARS


# ── 接线：真的挂在唯一派发口上（不是我以为挂上了）────────────────────────────

@pytest.mark.asyncio
async def test_the_gate_is_wired_into_the_only_dispatch_point(tmp_path, monkeypatch):
    from core import tool_registry
    from core.state import State

    async def _flood(*, state, **kwargs):
        return {"status": "success", "content": "巨" * (bo.MAX_RESULT_CHARS)}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_artifact", _flood)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_artifact",
                        tool_registry.ToolDefinition(
                            name="read_artifact", description="d",
                            parameters_schema={"type": "object", "properties": {}}))

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_bound")
    out = await tool_registry.execute("read_artifact", state)
    assert out.get("oversized") is True, "闸没挂在派发口上"
    assert len(json.dumps(out, ensure_ascii=False)) < bo.MAX_RESULT_CHARS


@pytest.mark.asyncio
async def test_secrets_are_redacted_before_the_original_hits_disk(tmp_path, monkeypatch):
    """顺序不变量：**先脱敏、后落盘**。

    反过来的话，凭据会被原样写进一个长期留在 run 目录里的文件 —— 而脱敏这道
    门存在的全部意义就是不让它离开进程。
    """
    from core import tool_registry
    from core.state import State

    monkeypatch.setenv("MY_SECRET_TOKEN", "sk-super-secret-value-123456")

    async def _leak(*, state, **kwargs):
        return {"status": "success",
                "content": "sk-super-secret-value-123456 " + "z" * bo.MAX_RESULT_CHARS}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_artifact", _leak)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_artifact",
                        tool_registry.ToolDefinition(
                            name="read_artifact", description="d",
                            parameters_schema={"type": "object", "properties": {}}))

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_sec")
    await tool_registry.execute("read_artifact", state)

    for f in (state.root / bo.OVERSIZED_DIRNAME).glob("*.json"):
        assert "sk-super-secret-value-123456" not in f.read_text(encoding="utf-8"), \
            "秘密在脱敏之前就被写进了落盘文件"
