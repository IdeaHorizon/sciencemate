"""read_file 的输出必须有界 —— 与文件大小无关。

2026-08-04 实测事故（e2e8 _reviewer run 1785834100-7847bb）：
  materials 里的 `episodes_master.json` = 1,999,210 bytes，**0 个换行**。
  模型很克制地调了 `read_file(offset=0, limit=5)` —— 它只要 5 行。
  旧实现的字节护栏写成 `if size > MAX and offset == 0 and limit is None`，
  给了 limit 就整个关掉；而 limit 是按行的，单行文件穿过去了。
  返回 2MB → context 625k → 压缩把 85 条消息压成 13 条却只降到 610k
  （体积全在一条消息里，压缩只能删消息不能切消息）→ HTTP 400，run 死。

所以这里锁的是**原则**，不是那一个 if：无论调用方怎么传参，content 的
字节数都不许超过上限。变异测试就针对"把上限改回有条件生效"。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from shared.tools.builtin import _MAX_READ_BYTES, _read_file


class _FakeState:
    def __init__(self, root):
        self.root = root
        self.files_read: set[str] = set()
        self.tokens_used = 0
        self.tokens_limit = 0

    def append_transcript(self, *a, **k):
        pass


@pytest.fixture()
def state(tmp_path):
    return _FakeState(tmp_path)


def _read(state, path, **kw) -> dict:
    return asyncio.run(_read_file(state, str(path), **kw))


def _content_bytes(res: dict) -> int:
    return len(res["content"].encode("utf-8"))


# ── 事故本体：单行巨型文件 ───────────────────────────────────────────────

@pytest.fixture()
def giant_one_liner(tmp_path):
    """复刻 episodes_master.json：~2MB，一行，无换行符。"""
    p = tmp_path / "episodes_master.json"
    p.write_text(json.dumps([{"arm": "B0", "env": "airline", "i": i} for i in range(30_000)]))
    assert p.stat().st_size > 1_000_000
    assert p.read_text().count("\n") == 0
    return p


def test_single_giant_line_with_limit_is_still_bounded(state, giant_one_liner):
    """事故复现路径：limit=5 + 单行 2MB。旧实现返回全文。"""
    res = _read(state, giant_one_liner, offset=0, limit=5)
    assert res["status"] == "success"
    assert _content_bytes(res) <= _MAX_READ_BYTES


def test_single_giant_line_without_limit_is_bounded_not_error(state, giant_one_liner):
    """不给 limit 也要有界 —— 而且是返回内容，不是报错拒读。"""
    res = _read(state, giant_one_liner, offset=0)
    assert res["status"] == "success"
    assert _content_bytes(res) <= _MAX_READ_BYTES


def test_giant_line_reports_partial_line_not_silent(state, giant_one_liner):
    """截断必须自报：模型要能分辨"读完了"和"读到上限了"。"""
    res = _read(state, giant_one_liner, limit=5)
    assert res["truncated"] is True
    assert res["bytes_capped"] is True
    assert res["partial_line"] is True
    assert "note" in res


def test_giant_line_note_names_the_way_out(state, giant_one_liner):
    """行寻址翻不动单行巨файл —— note 得说清用什么替代，别让模型拿 next_offset 空转。"""
    res = _read(state, giant_one_liner, limit=5)
    assert any(k in res["note"] for k in ("jq", "python", "run_bash"))


def test_giant_line_next_offset_does_not_promise_progress(state, giant_one_liner):
    """整个文件只有 1 行且没读完时，next_offset 不该指向"下一行"（那是空转）。"""
    res = _read(state, giant_one_liner, limit=5)
    assert res["total_lines"] == 1
    assert res["next_offset"] is None


def test_partial_line_says_remainder_is_lost(state, giant_one_liner):
    """截断单行时若不说"后半再也拿不到"，模型会以为 next_offset 能补齐 ——
    那是静默丢内容，比报错更坏。"""
    res = _read(state, giant_one_liner, limit=5)
    assert "剩余部分不会出现" in res["note"]


# ── 巨行夹在多行文件中间 ────────────────────────────────────────────────

@pytest.fixture()
def giant_line_in_middle(tmp_path):
    p = tmp_path / "mixed.jsonl"
    p.write_text("\n".join(["small-a", "small-b", "X" * 900_000, "small-c", "small-d"]))
    return p


def test_giant_line_in_middle_still_bounded(state, giant_line_in_middle):
    res = _read(state, giant_line_in_middle, offset=2)
    assert _content_bytes(res) <= _MAX_READ_BYTES
    assert res["partial_line"] is True


def test_small_lines_before_giant_are_not_cut(state, giant_line_in_middle):
    """巨行前面的整行不该被牺牲：先返回它们，把巨行留给下一页。"""
    res = _read(state, giant_line_in_middle, offset=0)
    assert res["lines_returned"] == 2
    assert res["partial_line"] is False
    assert res["bytes_capped"] is True
    assert res["next_offset"] == 2


def test_paging_past_giant_line_makes_progress(state, giant_line_in_middle):
    """卡在巨行上不动 = 死循环。翻页必须能越过它。"""
    res = _read(state, giant_line_in_middle, offset=2)
    assert res["next_offset"] == 3
    tail = _read(state, giant_line_in_middle, offset=3)
    assert tail["content"] == "4\tsmall-c\n5\tsmall-d"


# ── 多行大文件 ──────────────────────────────────────────────────────────

@pytest.fixture()
def many_big_lines(tmp_path):
    """1000 行 × 1KB ≈ 1MB，行寻址能翻页。"""
    p = tmp_path / "steps_master.jsonl"
    p.write_text("\n".join(json.dumps({"i": i, "pad": "x" * 1000}) for i in range(1000)))
    return p


def test_many_lines_output_bounded(state, many_big_lines):
    res = _read(state, many_big_lines)
    assert _content_bytes(res) <= _MAX_READ_BYTES


def test_many_lines_paging_actually_advances(state, many_big_lines):
    """有界之后必须能接着读 —— 否则等于读不到。"""
    first = _read(state, many_big_lines)
    assert first["bytes_capped"] is True
    assert first["partial_line"] is False
    nxt = first["next_offset"]
    assert nxt is not None and nxt > 0
    second = _read(state, many_big_lines, offset=nxt)
    assert second["content"].startswith(f"{nxt + 1}\t")
    assert _content_bytes(second) <= _MAX_READ_BYTES


def test_many_short_lines_separators_counted(state, tmp_path):
    """行多且短时，join 用的 "\\n" 本身就是可观的体积。

    变异测试逼出来的：只用 1KB 长行测不到这个 —— 250 行才多 249 字节，
    被余量吸收了。短行才让分隔符占比上来（2 万行 = 2 万字节）。
    """
    p = tmp_path / "short_lines.txt"
    p.write_text("\n".join("x" * 20 for _ in range(20_000)))
    res = _read(state, p, limit=10_000)
    assert _content_bytes(res) <= _MAX_READ_BYTES


def test_paging_eventually_reaches_the_end(state, many_big_lines):
    off, seen, guard = 0, 0, 0
    while True:
        guard += 1
        assert guard < 50, "翻页不收敛"
        res = _read(state, many_big_lines, offset=off)
        seen += res["lines_returned"]
        if res["next_offset"] is None:
            break
        off = res["next_offset"]
    assert seen == 1000


# ── 小文件不受影响 ──────────────────────────────────────────────────────

def test_small_file_unchanged(state, tmp_path):
    p = tmp_path / "small.txt"
    p.write_text("a\nb\nc\n")
    res = _read(state, p)
    assert res["content"] == "1\ta\n2\tb\n3\tc"
    assert res["truncated"] is False
    assert "bytes_capped" not in res
    assert res["total_lines"] == 3


def test_offset_and_limit_still_work(state, tmp_path):
    p = tmp_path / "small.txt"
    p.write_text("\n".join(f"line{i}" for i in range(10)))
    res = _read(state, p, offset=3, limit=2)
    assert res["content"] == "4\tline3\n5\tline4"
    assert res["lines_returned"] == 2
    assert res["next_offset"] == 5
    assert res["truncated"] is True


def test_default_limit_lines_is_actually_applied(state, tmp_path):
    """旧实现定义了 _DEFAULT_LIMIT_LINES=2000 却从没用过，描述里的
    "默认返回前 2000 行"是假的 —— 实际返回全文。"""
    from shared.tools.builtin import _DEFAULT_LIMIT_LINES
    p = tmp_path / "many.txt"
    p.write_text("\n".join(f"L{i}" for i in range(_DEFAULT_LIMIT_LINES + 500)))
    res = _read(state, p)
    assert res["lines_returned"] == _DEFAULT_LIMIT_LINES
    assert res["next_offset"] == _DEFAULT_LIMIT_LINES


def test_files_read_marked_even_when_capped(state, giant_one_liner):
    """截断了也算读过 —— 否则 write/edit 的"必须先读"门禁把人锁死。"""
    _read(state, giant_one_liner, limit=5)
    assert str(giant_one_liner.resolve()) in {
        str(x) for x in state.files_read
    } or str(giant_one_liner) in state.files_read


def test_size_bytes_reported(state, giant_one_liner):
    """模型要能一眼看出"这文件本来多大"，才知道自己拿到的是几分之一。"""
    res = _read(state, giant_one_liner, limit=5)
    assert res["size_bytes"] == giant_one_liner.stat().st_size


# ── 不许把大文件整个读进内存 ────────────────────────────────────────────

def test_does_not_slurp_whole_file(state, many_big_lines, monkeypatch):
    """旧实现 read_text() 把整个文件吞进内存再切。18MB 的 jsonl 就是这么来的。"""
    from pathlib import Path
    called = []
    orig = Path.read_text

    def _spy(self, *a, **k):
        called.append(str(self))
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _spy)
    _read(state, many_big_lines)
    assert not any(str(many_big_lines) in c for c in called)
