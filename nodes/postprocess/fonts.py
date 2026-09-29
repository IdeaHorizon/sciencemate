"""渲染字体栈的唯一真相源（按字形覆盖扫盘，不写名单）。

## 为什么有这个模块

在此之前，同一份 CJK 字体名单在 postprocess 里写了五遍
（`tools/render.py`、`v2/rendering.py` ×3、`autoplot_engine/chart_designer.py`），
而且已经开始分叉：有的用 `font.sans-serif`、有的用 `font.family`，
有的带 `PingFang HK`、有的不带，`DejaVu Sans` 在有的地方排第一位。

更要命的是名单里列的全是**开发机（macOS）**上的字体 ——
`Hiragino Sans GB` / `Arial Unicode MS` / `PingFang HK`。Linux 部署机上一个
都没有，于是必然落到 `DejaVu Sans`，而它**没有 CJK 字形**：中文标题/轴标签
渲染成豆腐块，且不报错、只是降级。node20 实测：现行名单 11 条缺字警告，
换成机器上已装的 `WenQuanYi Zen Hei` 后 0 条。

病根是"写名单"而不是"扫盘"：名单里没有的字体等于不存在，换台机器整条落空，
落空时还静默。

## 这里怎么做

按**字形覆盖**扫当前机器真正装了什么（`fontTools` 读 cmap，看有没有代表性
汉字），`PREFERRED_ORDER` 只用来给扫出来的结果**排序**，不再决定有无。
所以少写一个名字最多是排序不理想，不会变成豆腐块；反过来，机器上装了任何
一个能写中文的字体都会被用上。

扫描每进程一次（`lru_cache`）。实测 465 个字体文件全扫 1.78s（macOS 开发机，
上限），Linux 容器几十个字体在 0.2s 量级。
"""
from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# 判定用的代表字：常用汉字 + 一个次常用字。只覆盖第一个不算数 ——
# 有些字体只补了极少数汉字（例如某些 emoji/符号字体）。
_PROBE_CODEPOINTS = (0x4E2D, 0x6BD4, 0x56FE)   # 中 比 图

# 只影响**顺序**，不影响有无。排前面的是排版质量更好的正文黑体。
PREFERRED_ORDER = (
    "Noto Sans CJK SC",
    "Noto Sans CJK TC",
    "Source Han Sans SC",
    "PingFang SC",
    "PingFang HK",
    "Hiragino Sans GB",
    "Microsoft YaHei",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "Droid Sans Fallback",
    "AR PL UMing CN",
    "Arial Unicode MS",
)

# 兜底：没有 CJK 字形，但拉丁字形质量好，必须留在最后一位。
LATIN_FALLBACK = "DejaVu Sans"

# 唯一的排除项，且理由不是"名单里没有"，而是**它不是一款正文字体**：
# Last Resort 的设计目的就是覆盖全部码位并画出占位方框（告诉用户"这里缺字"）。
# 它因此天然骗过"字形覆盖"这个判据 —— 一台只装了它的机器会被判成"有 CJK 字体"，
# 于是下面那条 warning 被吞掉，人看到的还是方框却再也没有提示。
_NOT_A_REAL_TYPEFACE = ("last resort",)


def _covers_cjk(path: str) -> bool:
    """这个字体文件真的能画出汉字吗 —— 读 cmap，不看名字。"""
    from fontTools.ttLib import TTCollection, TTFont

    font = None
    try:
        if path.lower().endswith((".ttc", ".otc")):
            font = TTCollection(path, lazy=True).fonts[0]
        else:
            font = TTFont(path, lazy=True, fontNumber=0)
        cmap = font.getBestCmap()
        return all(cp in cmap for cp in _PROBE_CODEPOINTS)
    except Exception:
        # 坏字体/不认识的格式不是错误，只是不算候选。
        return False
    finally:
        try:
            if font is not None:
                font.close()
        except Exception:
            pass


@lru_cache(maxsize=1)
def cjk_families() -> tuple[str, ...]:
    """当前机器上**字形真的覆盖汉字**的字体族，按排版质量排序。

    找不到任何一个时返回空元组，并 warning 一次 —— 这时中文一定是豆腐块，
    该让人看见，而不是继续静默降级。
    """
    import matplotlib.font_manager as fm

    found: set[str] = set()
    for entry in fm.fontManager.ttflist:
        name = entry.name
        # 点开头的是 macOS 的内部字体（.Aqua Kana 之类），matplotlib 选不稳。
        if not name or name.startswith("."):
            continue
        if any(tag in name.lower() for tag in _NOT_A_REAL_TYPEFACE):
            continue
        if name in found:
            continue
        if _covers_cjk(entry.fname):
            found.add(name)

    ordered = [n for n in PREFERRED_ORDER if n in found]
    ordered += sorted(found - set(ordered))

    if not ordered:
        logger.warning(
            "本机没有任何覆盖 CJK 字形的字体，中文标题/轴标签会渲染成方块。"
            "Linux 上装 fonts-wqy-zenhei 或 fonts-noto-cjk 即可。"
        )
    return tuple(ordered)


def sans_stack(*preferred: str | None) -> list[str]:
    """给 matplotlib 的字体栈：调用方偏好 → 本机 CJK 字体 → 拉丁兜底。

    `preferred` 是调用方自己的样式令牌（例如 style_profile 里的 font_family），
    放最前面；后面接上**扫出来**的 CJK 字体，最后是 DejaVu Sans。
    去重且保序。
    """
    stack: list[str] = []
    for name in (*preferred, *cjk_families(), LATIN_FALLBACK):
        if name and name not in stack:
            stack.append(name)
    return stack


@lru_cache(maxsize=64)
def _covered_codepoints(family: str) -> frozenset[int]:
    """这个字体族**真的**能画出哪些码位 —— 还是读 cmap，不看名字。"""
    import matplotlib.font_manager as fm
    from fontTools.ttLib import TTCollection, TTFont

    covered: set[int] = set()
    for entry in fm.fontManager.ttflist:
        if entry.name != family:
            continue
        font = None
        try:
            if entry.fname.lower().endswith((".ttc", ".otc")):
                font = TTCollection(entry.fname, lazy=True).fonts[0]
            else:
                font = TTFont(entry.fname, lazy=True, fontNumber=0)
            covered.update(font.getBestCmap())
        except Exception:
            continue
        finally:
            try:
                if font is not None:
                    font.close()
            except Exception:
                pass
    return frozenset(covered)


def missing_glyphs(text: str, stack: tuple[str, ...] | list[str]) -> list[str]:
    """这段文字里，**整条字体栈都画不出来**的字符。

    为什么要有它：合同里写了「① 单台服务器内部拓扑」，TikZ 出来的图上只有
    「单台服务器内部拓扑」—— 衬线字体没有 ① 的字形，字就没了，没有任何报错
    （2026-09-17 实测）。这正是这套图合同要拦的那类事：**合同里写了、图上
    没有、没人报**。cjk_families() 只探 3 个代表字（中/比/图），代表字过了
    不代表这张图要画的每个字都过。

    判据落在「这张图真要画的字」上，不是一张字符名单 ——
    同 [[feedback_guardrails_must_scan_not_list]]。
    """
    wanted = {ord(ch) for ch in text if ch.strip() and ord(ch) > 0x7E}
    if not wanted:
        return []
    for family in stack:
        wanted -= _covered_codepoints(family)
        if not wanted:
            return []
    return sorted(chr(cp) for cp in wanted)
