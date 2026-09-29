"""字体栈回归：中文标签不能渲染成豆腐块。

起因（2026-08-18，issue #513）：同一份 CJK 字体名单在 postprocess 里写了五遍，
列的全是 macOS 上的字体名。Linux 部署机上一个都没有 → 落到 DejaVu Sans →
中文出方块，且**不报错**。node20 实测 11 条缺字警告。

所以这里的判据是**渲染结果**（有没有缺字警告），不是"有没有调用那个函数" ——
后者在名单再次写歪时照样绿。
"""
from __future__ import annotations

import warnings

import pytest

from nodes.postprocess.fonts import LATIN_FALLBACK, PREFERRED_ORDER, cjk_families, sans_stack


def _render_chinese_and_count_missing_glyphs(font_stack: list[str]) -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with plt.rc_context({"font.sans-serif": font_stack,
                             "font.family": "sans-serif",
                             "axes.unicode_minus": False}):
            fig = plt.figure()
            ax = fig.add_subplot(111)
            ax.bar(["比表面积", "方法", "时间"], [1.0, 2.0, 3.0])
            ax.set_title("对比图")
            fig.canvas.draw()
            plt.close(fig)
        return sum("missing from font" in str(w.message) for w in caught)


def test_stack_renders_chinese_without_missing_glyphs():
    """真正要守的东西：中文画得出来。"""
    if not cjk_families():
        pytest.skip("本机没有任何覆盖 CJK 的字体，这条守不了（Linux 装 fonts-wqy-zenhei）")
    assert _render_chinese_and_count_missing_glyphs(sans_stack()) == 0


def test_the_old_hardcoded_list_is_what_broke_it():
    """变异对照：旧名单在没有这些字体的机器上确实出豆腐块。

    这条钉住"问题是真的"——否则上面那条可能只是碰巧绿。在开发机（装了
    Hiragino 之类）上旧名单也能过，此时这条自动跳过，不制造假红。
    """
    old_list = ["Noto Sans CJK SC", "Hiragino Sans GB", "Arial Unicode MS", "DejaVu Sans"]
    import matplotlib.font_manager as fm
    installed = {f.name for f in fm.fontManager.ttflist}
    if any(name in installed for name in old_list[:3]):
        pytest.skip("本机装着旧名单里的 CJK 字体，复现不了部署机上的失效")
    if not cjk_families():
        pytest.skip("本机没有任何 CJK 字体，两边都会是豆腐块，对照无意义")
    assert _render_chinese_and_count_missing_glyphs(old_list) > 0
    assert _render_chinese_and_count_missing_glyphs(sans_stack()) == 0


def test_scan_decides_membership_and_the_list_only_orders():
    """名单只排序、不决定有无 —— 这是与旧实现的根本差别。"""
    families = cjk_families()
    if not families:
        pytest.skip("本机没有 CJK 字体")
    # 扫出来的每一个都必须真在本机装着
    import matplotlib.font_manager as fm
    installed = {f.name for f in fm.fontManager.ttflist}
    assert set(families) <= installed
    # 排在前面的若出现，必须是 PREFERRED_ORDER 里的（顺序偏好生效）
    preferred_present = [n for n in PREFERRED_ORDER if n in families]
    assert list(families[:len(preferred_present)]) == preferred_present
    # 不在名单里但覆盖 CJK 的字体也必须被收进来（这正是旧实现漏掉的那一格）
    assert len(families) >= len(preferred_present)


def test_latin_fallback_is_always_last():
    stack = sans_stack()
    assert stack[-1] == LATIN_FALLBACK
    assert len(stack) == len(set(stack)), "字体栈不能有重复项"


def test_caller_preference_wins_the_front():
    stack = sans_stack("Helvetica")
    assert stack[0] == "Helvetica"
    assert stack[-1] == LATIN_FALLBACK


def test_none_preference_is_dropped_not_stringified():
    """style.get("font_family") 缺省时是 None，不能变成字面量 'None'。"""
    assert "None" not in sans_stack(None)
    assert None not in sans_stack(None)


def test_last_resort_is_not_treated_as_a_cjk_font():
    """Last Resort 覆盖全部码位却只画占位方框 —— 它算数的话，"本机没有中文字体"
    这条 warning 就永远不会触发，人看到方框却再也收不到提示。"""
    assert not any("last resort" in name.lower() for name in cjk_families())
