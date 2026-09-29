"""execute_python 用到 matplotlib 时，默认设好 CJK 字体栈 —— 否则中文豆腐块。

## 根因（E2E v27/v28/v29）

模型自己写 execute_python 脚本画图，用 matplotlib 默认 `DejaVu Sans`（无 CJK 字形）
→ 中文标题/轴标签渲染成方块。postprocess 自己的画图工具会先设 `font.sans-serif`，
但模型手写脚本不走那条路。v29 靠每轮跑字体搜索脚本自愈才画对 —— 不跨 run、每次
重来、还触发高危审批。把默认应用挪到环境层：任何用 matplotlib 的脚本自动拿到栈。
"""
from __future__ import annotations

from shared.tools.library.python_exec import _with_matplotlib_font_preamble


def test_non_plotting_code_is_left_untouched():
    """不提 matplotlib 的代码原样返回 —— 不强制 import、不拖慢。"""
    code = "total = sum(range(10))\nprint(total)\n"
    assert _with_matplotlib_font_preamble(code) == code


def test_plotting_code_gets_the_cjk_stack_prepended():
    code = "import matplotlib.pyplot as plt\nplt.title('中文')\n"
    out = _with_matplotlib_font_preamble(code)
    assert out != code
    assert "font.sans-serif" in out
    assert "axes.unicode_minus" in out
    assert out.endswith(code), "前置，不改动用户代码本身"


def test_pyplot_and_seaborn_and_bare_plt_are_detected():
    for snippet in ("import seaborn as sns\nsns.histplot(x)\n",
                    "plt.plot([1,2])\n",
                    "from matplotlib import pyplot\n"):
        assert "font.sans-serif" in _with_matplotlib_font_preamble(snippet), snippet


def test_stack_comes_from_the_single_source_and_has_latin_fallback():
    """字体名单取 fonts.PREFERRED_ORDER（单一真相源），且拉丁兜底在内。"""
    from nodes.postprocess.fonts import LATIN_FALLBACK, PREFERRED_ORDER

    out = _with_matplotlib_font_preamble("import matplotlib.pyplot as plt\n")
    assert PREFERRED_ORDER[0] in out, "该用 fonts.py 的首选 CJK 族"
    assert LATIN_FALLBACK in out, "拉丁兜底必须在栈里（没装 CJK 时不至于崩）"


def test_user_font_choice_can_still_win():
    """前置的是默认；用户代码后设 font 会覆盖 —— 前置在用户代码之前。"""
    code = "import matplotlib.pyplot as plt\nplt.rcParams['font.sans-serif']=['MyFont']\n"
    out = _with_matplotlib_font_preamble(code)
    # 用户那行在前置之后（前置提供默认，用户覆盖）
    assert out.index("font.sans-serif'] = [") < out.index("['MyFont']")


def test_cjk_renders_without_missing_glyph_when_a_cjk_font_exists():
    """端到端：真跑一段画中文的代码，本机有 CJK 字体时不应有 missing-glyph 警告。

    没有 CJK 字体的机器（如未装 fonts-noto-cjk 的 Linux）跳过 —— 那是字体**安装**
    问题，与本注入正交。
    """
    import subprocess
    import sys
    import tempfile

    from nodes.postprocess.fonts import cjk_families

    if not cjk_families():
        import pytest

        pytest.skip("本机无 CJK 字体（字体安装问题，与注入正交）")

    tmp = tempfile.mkdtemp()
    code = (f"import matplotlib.pyplot as plt\n"
            f"plt.title('中文标题比图')\nplt.xlabel('样本量')\n"
            f"plt.savefig(r'{tmp}/t.png')\n")
    wrapped = _with_matplotlib_font_preamble(code)
    r = subprocess.run([sys.executable, "-c", wrapped],
                       capture_output=True, text=True, cwd=tmp)
    assert "missing from font" not in (r.stderr + r.stdout), (
        f"注入了 CJK 栈却仍豆腐块：{r.stderr[-300:]}"
    )
