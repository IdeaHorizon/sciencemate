"""console_box：CJK/emoji 宽度感知的终端框线排版（2026-07 banner 重设计）。"""
from __future__ import annotations

from shared.lib.console_box import display_width, draw_box, pad_display


def test_display_width_ascii():
    assert display_width("hello") == 5
    assert display_width("") == 0


def test_display_width_cjk_double():
    # 中文全角每字 2 列
    assert display_width("研究平台") == 8
    assert display_width("a研b") == 4  # 1+2+1


def test_display_width_skips_ansi():
    colored = "\033[1m研究\033[0m"
    assert display_width(colored) == 4  # ANSI 转义不计宽


def test_display_width_zero_width_combining():
    # 变体选择符 / ZWJ 记 0 宽
    assert display_width("a️") == 1


def test_pad_display_aligns_by_columns():
    # "研究"=4 列，补到 8 列 → 加 4 个空格
    padded = pad_display("研究", 8)
    assert padded == "研究    "
    assert display_width(padded) == 8


def test_pad_display_no_shrink():
    assert pad_display("hello", 3) == "hello"  # 已超宽不截


def test_draw_box_all_content_lines_same_visual_width():
    lines = ["AI4Science 研究平台", "项目  demo", "模型  deepseek-v4-flash"]
    box = draw_box(lines, min_inner_width=30)
    rows = box.splitlines()
    # 每行（含边框）显示宽度必须一致 —— 这是"对齐"的机器可判定义
    widths = {display_width(r) for r in rows}
    assert len(widths) == 1, f"框行宽度不齐：{widths}"
    # 圆角字符在四角
    assert rows[0].startswith("╭") and rows[0].endswith("╮")
    assert rows[-1].startswith("╰") and rows[-1].endswith("╯")


def test_draw_box_empty_line_becomes_separator():
    box = draw_box(["a", "", "b"])
    rows = box.splitlines()
    sep = [r for r in rows if r.startswith("├")]
    assert len(sep) == 1
    assert sep[0].endswith("┤")


def test_draw_box_with_emoji_stays_aligned():
    lines = ["🔬  研究平台 · Orchestrator", "记忆  💤 从未整理过"]
    box = draw_box(lines, min_inner_width=40)
    rows = box.splitlines()
    widths = {display_width(r) for r in rows}
    assert len(widths) == 1, f"含 emoji 时框行宽度不齐：{widths}"


def test_draw_box_border_style_does_not_break_alignment():
    dim = lambda s: f"\033[2m{s}\033[0m"  # noqa: E731
    box = draw_box(["research", "平台"], border_style=dim, min_inner_width=20)
    rows = box.splitlines()
    # 边框上色（ANSI）不应影响对齐
    widths = {display_width(r) for r in rows}
    assert len(widths) == 1
