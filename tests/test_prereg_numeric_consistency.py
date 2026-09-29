"""协议里声明的量必须跟它自己写的定义公式对得上。

E2E v18 现场：research_plan 的表上方写着 `γ = 0.9 / (n_steps × 0.001)`，同一张
表的 n_steps 却整列比公式要求的小 1000 倍（G1: γ=10⁻² 配 n_steps=90，反推应是
90,000）。run_experiments.py 直接用了错的那列 —— G1 相当于 90 步内从 T=1.0 降到
0.1，系统来不及弛豫，不是玻璃化转变而是淬火假象。

冻结门禁此前查"判据能不能解析""资源有没有声明"，不查数字之间自不自洽。
这一类是纯算术，不该留给模型自觉。

**误报的门比没有门更糟**（它教人绕过），所以：按表头定位列、按分隔行切表、
拿不准一律沉默。
"""

from core.prereg_numeric_consistency import _num, formula_violations

PLAN = """
γ = 0.9 / (n_steps × 0.001) 严格不同。

| 组别 | γ (LJ units) | T_start | T_end | n_steps | 重复次数 |
|------|-------------|---------|-------|---------|---------|
| G1 | 10⁻² | 1.0 | 0.1 | 90 | 3 |
| G5 | 10⁻⁵ | 1.0 | 0.1 | 90,000 | 3 |
"""

GOOD = """
γ = 0.9 / (n_steps × 0.001)

| 组别 | γ | n_steps |
|------|---|---------|
| G1 | 10 | 90 |
| G5 | 0.01 | 90000 |
"""


def test_catches_the_thousandfold_error():
    out = formula_violations(PLAN)
    assert len(out) == 2
    assert all("1e+03" in v or "1000" in v for v in out)


def test_consistent_table_is_silent():
    assert formula_violations(GOOD) == []


def test_no_formula_means_no_judgement():
    """没写定义公式就没有判据 —— 不猜。"""
    assert formula_violations("| a | b |\n|---|---|\n| 1 | 2 |") == []


def test_citation_years_are_not_mistaken_for_steps():
    """第一版把文献行的年份 1996 当成步数、温度 0.1 当成 γ，误报一片。"""
    text = """
γ = 0.9 / (n_steps × 0.001)

| 组别 | γ | n_steps |
|------|---|---------|
| G1 | 10 | 90 |

| 文献 | 体系 | N | γ 范围 | Tg |
|------|------|---|--------|-----|
| Vollmayr et al. (1996) | 二元 LJ | 1000 | ~10⁻⁴–10⁻² | ~0.3–0.5 |
"""
    assert formula_violations(text) == []


def test_other_tables_do_not_borrow_the_first_tables_columns():
    """不切表的话，第一张表的列序会被后面另一张表的行套用。"""
    text = PLAN + """

| 体系 | N | γ | T_start | T_end | n_steps | 重复 |
|------|---|---|---------|-------|---------|------|
| N1000 | 1000 | 1 | 1.0 | 0.1 | 900 | 3 |
"""
    out = formula_violations(text)
    # 只有前两行违规；N1000 那行 γ=1 与 n_steps=900 是自洽的
    assert len(out) == 2


def test_superscript_and_scientific_forms_both_parse():
    assert _num("10⁻²") == 0.01
    assert _num("3×10⁻⁵") == 3e-5
    assert _num("90,000") == 90000
    assert _num("1996") == 1996
