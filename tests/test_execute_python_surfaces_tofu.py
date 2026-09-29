"""图里有豆腐块时，execute_python 必须把 matplotlib 的缺字形告警抬到醒目位置。

## 根因（E2E v27/v28）

matplotlib 渲染缺字形时**必然**往 stderr 打 "Glyph NNN missing from font(s) …"，
但它埋在一堆输出里、returncode 还是 0 —— v27/v28 就是这么把满屏方块的中文图静默
交付出去的（谁都没看那行）。#651 已把默认 CJK 字体栈补上；这里管**残留**：某字形
连 CJK 字体也没有 / 部署机没装字体时，把信号从 stderr 捞到 envelope 顶层，让写这段
画图代码的 agent 当场看见、当场改。
"""
from __future__ import annotations

from shared.tools.library.python_exec import _missing_glyph_summary


def test_missing_glyph_line_is_detected():
    """matplotlib 真实缺字形那行 —— 必须被捞出来。"""
    out = (
        "some normal output\n"
        "/x/mpl.py:123: UserWarning: Glyph 20013 (\\N{CJK UNIFIED IDEOGRAPH-4E2D}) "
        "missing from font(s) DejaVu Sans.\n"
        "  fig.savefig(...)\n"
    )
    s = _missing_glyph_summary(out)
    assert s is not None
    assert "豆腐块" in s
    assert "font.sans-serif" in s, "告警要给出可操作的改法，不能只说不行"


def test_many_glyph_lines_are_counted_not_dumped():
    """一张中文图能刷几百条同样的行 —— 数条数，不逐条堆。"""
    out = "\n".join(
        f"Glyph {20000+i} missing from font(s) DejaVu Sans." for i in range(300))
    s = _missing_glyph_summary(out)
    assert s is not None and "300 处" in s
    assert len(s) < 600, "告警本身要短，不能把 300 行灌进去"


def test_findfont_fallback_is_detected():
    out = "findfont: Font family 'Noto Sans CJK SC' not found.\n"
    s = _missing_glyph_summary(out)
    assert s is not None and "findfont" in s


def test_clean_output_does_not_false_fire():
    """正常渲染（无缺字形）不该报告豆腐块。"""
    out = "saved figure to /x/fig.png\nElapsed 1.2s\nfindfont: score(...) = 0.05\n"
    assert _missing_glyph_summary(out) is None


def test_empty_output():
    assert _missing_glyph_summary("") is None


def test_end_to_end_result_carries_the_warning(tmp_path, monkeypatch):
    """走真工具：故意用只有拉丁字形的字体画中文 → 结果里必须有 figure_glyph_warning。"""
    import asyncio

    from nodes.postprocess.fonts import cjk_families

    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_RUNS_ROOT", raising=False)
    from core.bootstrap import bootstrap
    from core.state import State
    from core.tool_registry import execute

    bootstrap()
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                   project_id="tofu-test")
    # 强制用 DejaVu Sans（无 CJK 字形）画中文 —— 必出缺字形告警，且不受 #651 的
    # 默认栈影响（用户代码显式覆盖 font.sans-serif）。
    code = (
        "import matplotlib; matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.rcParams['font.sans-serif'] = ['DejaVu Sans']\n"
        "fig, ax = plt.subplots()\n"
        "ax.set_title('中文标题会变豆腐块')\n"
        "fig.savefig('out.png')\n"
    )
    res = asyncio.run(execute("execute_python", st, code=code))
    assert res.get("returncode") == 0, res
    assert "figure_glyph_warning" in res, (
        f"用 DejaVu 画中文却没报豆腐块告警：{res.get('stderr_tail','')[-200:]}"
    )
    assert "豆腐块" in res["figure_glyph_warning"]
