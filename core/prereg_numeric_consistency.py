"""预注册里声明的量，必须跟它自己写的定义公式对得上。

E2E v18 现场：research_plan 的表里明写 `γ = 0.9 / (n_steps × 0.001)`，同一张表
的 n_steps 却整列比公式要求的小 1000 倍（G1: γ=10⁻² 配 n_steps=90，公式反推
应是 90,000）。`run_experiments.py` 直接用了错的那列。

相对排序还在，所以 H1/H2 形式上照样"可判定"——但论文里每个 γ 数值都会错三个
数量级，而 G1 相当于 90 步内从 T=1.0 降到 0.1，系统根本来不及弛豫，那不是
玻璃化转变而是淬火假象。

冻结门禁此前查"判据能不能解析""资源有没有声明"，**不查数字之间自不自洽**。
这跟 reviewer 的 7 个维度里没有"设计在物理上做得出来吗"是同一个缺口：查字段
齐全，不查实质。而这一类是纯算术，不该留给模型自觉。

**判据必须按表头定位列**。第一版按"行里随便挑一个小数和一个大数"配对，把文献
引用行的年份 1996 当成步数、温度 0.1 当成 γ，误报一片 —— 会误伤的门比没有门
更糟，它教人绕过。
"""

from __future__ import annotations

import re

# 正文里的显式定义式：`X = A / (B × C)`
_FORMULA = re.compile(
    r"(?P<lhs>[A-Za-zΓγ_][\w]*)\s*=\s*(?P<num>[\d.eE+-]+)\s*/\s*\(\s*"
    r"(?P<var>[A-Za-z_]\w*)\s*[×*x]\s*(?P<factor>[\d.eE+-]+)\s*\)"
)

_TOL = 0.05          # 5% 相对容差
_SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
        "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}


def _num(text: str) -> float | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"[⁻−–]", "-", t)
    t = "".join(_SUP.get(ch, ch) for ch in t)
    t = t.replace("^", "").replace(",", "")
    t = re.sub(r"\s+", "", t)
    # `3×10-5` → 3e-5 ；裸的 `10-5`（即 10⁻⁵）→ 1e-5。
    # 只认 ×10 / ·10 的写法，不去猜"10-5"是不是减法 —— 上一版漏掉裸上标，
    # 5 行里只抓到 1 行，覆盖太窄。
    t = re.sub(r"(?:[×·x])10(-?\d+)", r"e\1", t)
    t = re.sub(r"^10(-\d+)$", r"1e\1", t)
    m = re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", t)
    if not m:
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)e(-?\d+)", t)
        if not m:
            return None
    try:
        return float(t)
    except ValueError:
        return None


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _tables(content: str) -> list[tuple[list[str], list[list[str]]]]:
    """按"连续表格行"成块切表，块内第二行是分隔符则认表头。

    不切表的话，第一张表定位到的列序会被**后面另一张表**的行套用 —— 实测把
    N1000 那张表的粒子数当成 γ、温度当成步数，误报两条。
    """
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|") and len(line) > 2:
            current.append([c.strip() for c in line[1:-1].split("|")])
        else:
            if current:
                blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    tables: list[tuple[list[str], list[list[str]]]] = []
    for block in blocks:
        if len(block) >= 3 and _is_separator(block[1]):
            body = [r for r in block[2:] if not _is_separator(r)]
            if body:
                tables.append((block[0], body))
    return tables


def _column_index(header: list[str], names: tuple[str, ...]) -> int | None:
    for i, cell in enumerate(header):
        low = cell.lower()
        if any(n.lower() in low for n in names):
            return i
    return None


def formula_violations(content: str) -> list[str]:
    """表格里声明的量与正文定义公式对不上 → 返回清单。空 = 没发现矛盾。

    只在**表头能定位到两列**时才判，否则一律沉默：宁可漏报，不可误伤。
    """
    if not content:
        return []
    formula = _FORMULA.search(content)
    if not formula:
        return []
    lhs, var = formula.group("lhs"), formula.group("var")
    numerator, factor = _num(formula.group("num")), _num(formula.group("factor"))
    if not numerator or not factor:
        return []

    violations: list[str] = []
    for header, body in _tables(content):
        lhs_col = _column_index(header, (lhs, "γ", "gamma", "冷却速率", "rate"))
        var_col = _column_index(header, (var, "n_steps", "步数", "steps"))
        if lhs_col is None or var_col is None or lhs_col == var_col:
            continue
        for row in body:
            if len(row) <= max(lhs_col, var_col):
                continue
            declared, steps = _num(row[lhs_col]), _num(row[var_col])
            if declared is None or steps is None or steps <= 0 or declared == 0:
                continue
            implied = numerator / (steps * factor)
            if abs(implied - declared) / abs(declared) <= _TOL:
                continue
            violations.append(
                f"{lhs}={row[lhs_col]} 与 {var}={row[var_col]} 不自洽："
                f"按正文公式 {lhs} = {numerator:g}/({var}×{factor:g}) 应为 {implied:g}"
                f"（相差 {implied / declared:.3g} 倍）。整行：{' | '.join(row)[:110]}"
            )
    return violations
