"""终端框线绘制 + CJK/emoji 宽度感知的排版工具（2026-07）。

为啥要它：chat.py 启动 banner 之前是一堆散 print()，中文是**双列宽**字符但
代码按单列算，终端一换行就错位、观感差。这里提供：
  - display_width(s)：算字符串真实**显示列宽**（CJK 全角 = 2，emoji = 2，
    ANSI 转义 / 零宽 = 0）
  - pad_display(s, width)：按显示宽度右侧补空格对齐
  - draw_box(...)：画圆角框，内容行自动按显示宽度对齐右边框

纯 stdlib，无第三方依赖。color 由调用方决定（传已上色的字符串也能对齐，因为
display_width 会跳过 ANSI 转义序列）。
"""
from __future__ import annotations

import re
import unicodedata

# ANSI 转义序列（颜色 / 样式），显示宽度按 0 算
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _char_width(ch: str) -> int:
    o = ord(ch)
    # 零宽：ZWJ、变体选择符、组合记号
    if o == 0x200D or 0xFE00 <= o <= 0xFE0F or unicodedata.combining(ch):
        return 0
    # East Asian Wide / Fullwidth → 2 列
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    # 常见 emoji / 符号区（多数终端按 2 列渲染）
    if (
        0x1F300 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0x1F000 <= o <= 0x1F2FF
        or o in (0x2B50, 0x2B55)
    ):
        return 2
    return 1


def display_width(s: str) -> int:
    """字符串在等宽终端里的显示列宽（跳过 ANSI 转义；CJK/emoji 记 2）。"""
    s = _ANSI_RE.sub("", s)
    return sum(_char_width(c) for c in s)


def pad_display(s: str, width: int) -> str:
    """按显示宽度把 s 右补空格到 width 列（已够宽则原样返回）。"""
    gap = width - display_width(s)
    return s + " " * gap if gap > 0 else s


def draw_box(
    lines: list[str],
    *,
    min_inner_width: int = 48,
    pad_x: int = 2,
    border: str = "rounded",
    border_style: "Callable[[str], str] | None" = None,  # noqa: F821
) -> str:
    """把若干内容行画进一个框。

    lines 里的空字符串 '' 渲染成一条内部分隔线（├──┤）。内容行按最长显示宽度
    对齐右边框。border_style 可选：对边框字符上色的函数（如 dim）；不影响对齐
    （display_width 跳过 ANSI）。

    返回整块多行字符串（不含末尾换行）。
    """
    chars = {
        "rounded": ("╭", "╮", "╰", "╯", "─", "│", "├", "┤"),
        "square": ("┌", "┐", "└", "┘", "─", "│", "├", "┤"),
    }[border]
    tl, tr, bl, br, h, v, ml, mr = chars

    content_w = max((display_width(ln) for ln in lines), default=0)
    inner = max(content_w, min_inner_width) + pad_x * 2

    def _b(s: str) -> str:
        return border_style(s) if border_style else s

    out: list[str] = [_b(tl + h * inner + tr)]
    for ln in lines:
        if ln == "":
            out.append(_b(ml + h * inner + mr))
            continue
        padded = " " * pad_x + pad_display(ln, inner - pad_x * 2) + " " * pad_x
        out.append(_b(v) + padded + _b(v))
    out.append(_b(bl + h * inner + br))
    return "\n".join(out)
