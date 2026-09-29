"""出版制图的薄 helper —— 普通库函数，无任何拒绝分支（判决拆除 B 刀）。

这是 DSL 降为技能库后**唯一**保留的代码化领域知识：期刊物理尺寸、色盲
安全色板、字体栈。skills（nodes/postprocess/skills/）里的代码片段可以
import 它；agent 也可以完全不用 —— 它只是省事，不是通道。

    from nodes.postprocess.figure_helpers import (
        COLORBLIND_SAFE, figsize_mm, journal_width_mm, sans_font_stack,
    )
    fig, ax = plt.subplots(figsize=figsize_mm(journal_width_mm("nature"), 60),
                           dpi=300)
"""

from __future__ import annotations

MM_PER_INCH = 25.4

#: 常见期刊族的栏宽（mm）。不认识的 venue 用通用值 —— 不猜 house style。
JOURNAL_COLUMN_WIDTHS_MM: dict[str, dict[str, float]] = {
    "nature": {"single": 89.0, "double": 183.0},
    "science": {"single": 55.0, "double": 175.0},
    "plos": {"single": 83.0, "double": 173.5},
    "ieee": {"single": 88.9, "double": 181.0},
    "generic": {"single": 89.0, "double": 183.0},
}

#: 出版位图的通行下限（DPI）。
PUBLICATION_MIN_RASTER_DPI = 300

#: Okabe-Ito 色盲安全色板（8 色，含黑）。分组身份仍应叠加 marker/线型冗余编码。
COLORBLIND_SAFE: list[str] = [
    "#000000", "#E69F00", "#56B4E9", "#009E73",
    "#F0E442", "#0072B2", "#D55E00", "#CC79A7",
]


def mm_to_inch(mm: float) -> float:
    return float(mm) / MM_PER_INCH


def figsize_mm(width_mm: float, height_mm: float) -> tuple[float, float]:
    """matplotlib ``figsize`` 用英寸；期刊约束用毫米 —— 换算收在一处。"""
    return (mm_to_inch(width_mm), mm_to_inch(height_mm))


def journal_width_mm(venue: str | None, column: str = "double") -> float:
    """按期刊族给栏宽；不认识的 venue 落到 generic（不猜 house style）。"""
    family = (venue or "generic").strip().lower()
    for key, widths in JOURNAL_COLUMN_WIDTHS_MM.items():
        if key in family:
            return widths.get(column, widths["double"])
    return JOURNAL_COLUMN_WIDTHS_MM["generic"].get(
        column, JOURNAL_COLUMN_WIDTHS_MM["generic"]["double"]
    )


def sans_font_stack() -> list[str]:
    """CJK 安全的 sans 字体栈（单一真相源在 fonts.py）。

    execute_python 已默认注入同一份栈；这里给需要显式设置的代码用。
    """
    try:
        from nodes.postprocess.fonts import LATIN_FALLBACK, PREFERRED_ORDER

        return [*PREFERRED_ORDER, LATIN_FALLBACK]
    except Exception:
        return ["Noto Sans CJK SC", "PingFang SC", "Arial Unicode MS", "DejaVu Sans"]


def apply_publication_defaults(plt, *, font_size_pt: float = 8.0) -> None:
    """一把设好克制的出版默认：字体栈/字号/线宽/去装饰。纯便利，可覆盖。"""
    plt.rcParams.update(
        {
            "font.sans-serif": sans_font_stack(),
            "axes.unicode_minus": False,
            "font.size": font_size_pt,
            "axes.titlesize": font_size_pt + 1.0,
            "axes.labelsize": font_size_pt,
            "legend.fontsize": max(5.0, font_size_pt - 1.0),
            "xtick.labelsize": max(5.0, font_size_pt - 1.0),
            "ytick.labelsize": max(5.0, font_size_pt - 1.0),
            "axes.linewidth": 0.6,
            "lines.linewidth": 1.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": False,
            "figure.dpi": 150,
            "savefig.dpi": PUBLICATION_MIN_RASTER_DPI,
        }
    )
