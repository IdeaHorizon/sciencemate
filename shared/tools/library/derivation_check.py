"""推导验证工具：把「验证比推导便宜」这条不对称性做成工具面。

## 为什么是工具，不是 prompt

裸模型长推导的死因是**复利崩塌**：每步 99% 正确，100 步后成功率 36.6%。
且实测（"Illusion of Diminishing Returns", 2509.09677）长程失败是执行失败 ——
单步正确率还随 context 增长而衰减，后面的步骤哪怕单独看很简单也会错。
所以"让模型一次生成 50 步漂亮推导"是死路，与模型多强无关。

正路利用数学的根本不对称性：**验证远比推导便宜**。Schwartz–Zippel 引理保证
一个非零的多项式恒等式在随机点上"碰巧成立"的概率趋近于零 —— 于是几次随机
代入就能以压倒性把握否定一个错误的等式，而找出正确的等式要难得多。

框架的工作就是把这个不对称性组织起来：模型只负责**提出下一步**，工具机械地
**验证这一步**。错误在发生的那一步被抓住，而不是 30 步之后；回退成本是一步，
而不是整条链。这是把定理证明器的 proof kernel 模式搬到非正式推导上。

## 四值，永远不是布尔

`simplify(lhs - rhs)` 没化到 0，**只证明 sympy 没化出来**，不证明两边不等。
把它当 false 返回，就是把"我不知道"讲成"这是错的" —— 模型会据此推翻正确的
步骤，去找一个不存在的 bug。所以判定有四个值：

    verified               符号层面确证等价
    numerically_supported  符号没结论，但 N 个随机点上高精度成立
    inconclusive           两条路都没结论（不是"错"）
    failed                 找到了反例（这是**证否**，强结论）

`failed` 与 `inconclusive` 的区别是本模块最重要的语义：前者是发现，后者是无知。

## 验证章的所有权

这些工具返回的 `verification` 块带 `tool` 与 `checked_at`，是 derivation_log
里每一步唯一合法的验证来源 —— 模型自己写一句 "verified: true" 进不了冻结门
（见 nodes/derivation/tools/derivation_contract.py）。**报告不是事实。**

## 依赖

只用 sympy + mpmath（环境里已有，不新增重依赖）。数值反例搜索用 sympy 自带的
随机代入 + 可选 scipy 优化；不引 hypothesis / pint —— 量纲用
`sympy.physics.units`，够用且零新装。
"""
from __future__ import annotations

import random
import re
from typing import Any

from core.tool_registry import (
    ToolDefinition,
    register_capability_gated_tool,
    register_tool,
)
from shared.lib import derivation_ledger as _ledger
from shared.lib.verifier_registry import register_verifier


def seal_verification(state: Any, verification: dict[str, Any], *, tool: str,
                      lhs: str, rhs: str, relation: str = "eq",
                      assumptions: dict | None = None) -> dict[str, Any]:
    """给验证章盖上式子指纹，并记进本 run 的账本。

    指纹是"这一步真调过工具"的凭据：冻结门拿它反查账本，对不上就不认。
    见 shared/lib/derivation_ledger —— 判据从"查工具名"升级成"查真调过"。

    ⚠️ 2026-08-23 之前这段是 `_check_step` 的内部闭包，`tool` 硬编码成
    `"check_step"`。后果：**另外三个验证工具根本不产出验证章** ——
    它们在 TRUSTED_VERIFIERS 白名单里，却没有任何合法方式把"我验过量纲了"
    记进推导链。模型只剩两条路：手写一个章（缺 probe，被门拒），
    或者把明明验过的一步标成 `prose`（说自己没验）。
    **闸反过来制造它要防的行为。** 提成公用之后，谁验的谁署名。
    """
    verification["probe"] = _ledger.probe_id(lhs, rhs, relation)
    verification.setdefault("tool", tool)
    _ledger.record(
        state, probe=verification["probe"], tool=tool,
        status=str(verification.get("status") or ""),
        lhs=lhs, rhs=rhs, relation=relation, assumptions=assumptions)
    return verification

#: 数值抽查的默认工作精度（十进制位）。50 位远超 float64，能把"看起来相等"
#: 与"真的相等"分开 —— 差在 1e-30 量级的两个表达式，float64 会报告相等。
_DEFAULT_PRECISION = 50
#: 默认抽查点数。Schwartz–Zippel 下，多项式恒等式每多一个随机点，假阳性概率
#: 就再乘一个小因子；32 点对研究场景是压倒性的。
_DEFAULT_POINTS = 32

_RELATIONS = ("eq", "lt", "le", "gt", "ge")


def _require_sympy() -> tuple[Any, Any] | dict:
    """返回 (sympy, mpmath) 或一个给模型看的错误 dict。"""
    try:
        import sympy
        import mpmath
    except ImportError as exc:  # pragma: no cover —— 环境缺件时的合法出口
        return {
            "status": "error",
            "error": (
                f"推导验证需要 sympy + mpmath，当前环境缺件：{exc}。"
                "这不是你的推导有问题 —— 把这条作为 blocker 报上去，"
                "或在本节点目录建 venv 装齐后重试。"
            ),
        }
    return sympy, mpmath


#: `名字(` 的形状 —— 带括号的才是函数调用。
_CALL_LIKE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(")
#: 任意标识符。
_IDENT_LIKE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\b")
#: 不带括号时**仍然**按 sympy 原义解析的名字：数学常数。
#: 其余一律当符号 —— 见 `_parse` 的说明。
_RESERVED_CONSTANTS = frozenset({
    "pi", "E", "I", "oo", "zoo", "nan", "true", "false", "S",
})


def _parse(sympy: Any, text: str, symbols: dict[str, Any]) -> Any:
    """把模型给的表达式串解析成 sympy 表达式。

    ⚠️ `implicit_multiplication_application`（为了让 `2x` 解析成 `2*x`）会把
    **它不认识的多字母标识符拆成单字母乘积**。2026-08-22 拿哥德尔第一不完备
    定理的步骤实测：

        Prov(gn(A))  →  P*r*o*v*(g*n*A)

    于是 `simplify(lhs-rhs)` 得到一坨单字母乘积、`equals()` 返回 False，
    这一步被判 **failed（找到反例，强结论）** —— 而它是一条正确的元数学命题。
    模型若信了，就会去"修"一个没错的东西。

    修在解析层：先扫出所有 `名字(` 形状的标识符，**凡是 sympy 不认识的都注册
    成 `Function`**，再交给 parse_expr。这样 `Prov(...)` 成为 AppliedUndef，
    下游 `_numerics_are_trustworthy` 才认得出"这不是我能数值判定的东西"。

    影响面远不止哥德尔：任何模型自定义的多字母函数名（`Psi(x)`、`Ham(q,p)`、
    `Prob(A)`）以前都会被静默拆开，然后给出无意义的判定。

    ## 不带括号的标识符一律当符号

    数学记法的通例：`gamma` 是比热比，`gamma(x)` 才是 Gamma 函数。
    不这么分，`T*V**(gamma-1)` 会因为 `FunctionClass - One` 直接解析失败 ——
    而 γ / β / ζ 是热力学、统计物理、统计学最常用的一批符号。
    只有数学常数（pi / E / I / oo）保留 sympy 原义。
    """
    from sympy.parsing.sympy_parser import (
        parse_expr, standard_transformations, implicit_multiplication_application,
        convert_xor,
    )

    text = str(text)
    called = set(_CALL_LIKE.findall(text))
    table = dict(symbols)

    # ① 带括号的自定义名字 → Function（哥德尔那轮：Prov(gn(A)) 不能被拆成乘积）
    for name in called:
        if name in table:
            continue
        known = getattr(sympy, name, None)
        if known is not None and callable(known):
            continue          # sin / exp / Sum / Integral / Rational …… 交给 sympy
        table[name] = sympy.Function(name)

    # ② 不带括号的标识符 → Symbol，**哪怕 sympy 里有同名函数**。
    #
    # 数学记法的通例：`gamma` 是比热比，`gamma(x)` 才是 Gamma 函数。
    # 不这么分，物理与统计里最常用的一批符号全部解析失败 ——
    # 2026-08-22 benchmark 实测：
    #
    #     check_step("T*V**(gamma-1)", "T*V**(gamma-1)")
    #       → 表达式解析失败：unsupported operand type(s) for -:
    #         'FunctionClass' and 'One'
    #
    # γ（比热比/阻尼系数/洛伦兹因子）、β（1/kT、回归系数）、ζ、Γ 全中招，
    # 而这些恰恰是热力学、统计物理、统计学最常见的符号。单测里我用的是
    # a/b/x/y，一个都碰不到 —— 真跑第一批就撞上了。
    for name in set(_IDENT_LIKE.findall(text)):
        if name in table or name in called or name in _RESERVED_CONSTANTS:
            continue
        table[name] = sympy.Symbol(name)

    transformations = standard_transformations + (
        implicit_multiplication_application, convert_xor)
    return parse_expr(str(text), local_dict=table,
                      transformations=transformations, evaluate=True)


def _free_symbols(sympy: Any, *exprs: Any) -> list[Any]:
    seen: dict[str, Any] = {}
    for expr in exprs:
        for sym in getattr(expr, "free_symbols", ()):
            seen.setdefault(sym.name, sym)
    return [seen[k] for k in sorted(seen)]


def _sample_points(
    sympy: Any, mpmath: Any, symbols: list[Any], n: int, seed: int,
    assumptions: dict[str, Any] | None,
) -> list[dict[Any, Any]]:
    """生成抽查点。

    刻意覆盖三个量级带（~1、~1e6、~1e-6）与复值 —— 只在 [0,1] 上采样会漏掉
    只在大参数或近奇点处才破裂的等式，而那正是研究里最常见的错误形态。

    **声明过的约束必须被尊重** —— 在 x>0 下成立的等式，拿负数去否定它是
    闸自己制造的假反例，而假反例是最坏的一类错误：它让模型去推翻一个正确
    的步骤、找一个不存在的 bug。

    2026-08-22 真跑抓到：`integer` 只进了 sympy 的符号假设、没进采样器。
    模型验有限几何级数求和（N∈ℤ⁺ 是求和上界），采样器给 N 发了 1.102，
    于是一个**教科书正确**的公式被判 failed。模型自己诊断出是工具的锅
    并绕了道 —— 但它本不该需要绕道。
    """
    rng = random.Random(seed)

    def _flags(name: str) -> set[str]:
        raw = str((assumptions or {}).get(name, "")).strip().lower()
        return {raw} if raw else set()

    positive_names = {n for n in (s.name for s in symbols)
                      if _flags(n) & {"positive", "pos", ">0"}}
    integer_names = {n for n in (s.name for s in symbols)
                     if _flags(n) & {"integer", "int"}}
    real_names = {n for n in (s.name for s in symbols)
                  if _flags(n) & {"real", "nonzero", "!=0"}}

    # ── 病态点先跑 ──────────────────────────────────────────────────────────
    #
    # 随机采样天然采不到边界：±1、极小量、整数 1。而错误的等式**最常在这里
    # 破裂** —— 一个只在 n=1 退化的递推式、一个在 x→0 才暴露的展开，
    # 随机点几乎必然放过。借鉴 math-rigor 的 pathological cases 要求
    # （零/奇异、n=1、复数值），但**判据不一样**：
    #
    # ⚠️ 病态点上的"反例"不能直接算数。灾难性相消正是在这些点上发生的 ——
    # 2026-08-22 实测：无穷几何级数在 x≈1.5e-7 处两边相对差 1.15e-10，
    # 一个教科书公式被判 failed。所以病态点上一旦 broken，**要用更高精度
    # 复验一次**（见 `_numeric_probe`）：真不等的话差值不随精度消失，
    # 相消误差会。用精度提升区分"真不等"和"算不准"，是机械的判据。
    pathological: list[dict[Any, Any]] = []
    for magnitudes in ((1, ), (-1, ), (mpmath.mpf(10) ** -12, )):
        point = {}
        for sym in symbols:
            value = mpmath.mpf(magnitudes[0])
            if sym.name in positive_names and value <= 0:
                value = abs(value) or mpmath.mpf(1)
            if sym.name in integer_names:
                value = mpmath.mpf(int(value) or 1)
            point[sym] = value
        pathological.append(point)

    scales = (1.0, 1e6, 1e-6)
    points: list[dict[Any, Any]] = list(pathological)
    for i in range(max(0, n - len(pathological))):
        scale = scales[i % len(scales)]
        point: dict[Any, Any] = {}
        for sym in symbols:
            name = sym.name
            if name in integer_names:
                # 整数符号常常是求和/乘积的上界或幂次 —— 给它浮点数，
                # 表达式本身就失去意义。量级也压住：e^(1e6) 直接溢出。
                low = 1 if name in positive_names else -12
                point[sym] = sympy.Integer(rng.randint(low, 12))
                continue
            magnitude = mpmath.mpf(rng.uniform(0.1, 2.0)) * scale
            if name in positive_names:
                point[sym] = magnitude
            elif name in real_names or i % 4 != 3:
                point[sym] = magnitude * (1 if rng.random() < 0.5 else -1)
            else:
                # 每四点掺一个复值：实轴上成立、复平面上不成立的等式确实存在。
                # 声明了 real 的符号不掺 —— 那同样是自造假反例。
                point[sym] = mpmath.mpc(
                    rng.uniform(-2, 2) * scale, rng.uniform(-2, 2) * scale)
        points.append(point)
    return points


def _evaluate_at(sympy: Any, mpmath: Any, expr: Any, point: dict, precision: int):
    """在一个点上求值，返回 mpmath 复数；求不出来返回 None（**不是反例**）。

    三件事，每一件都是被真跑教出来的：

    1. `Sum` / `Integral` / `Product` / `Derivative` 代入具体值后仍是未求值的
       符号对象，必须先 `doit()`；doit 之后仍未求值（上界还是符号）就放弃。

    2. **绝不降级成 float64。** 第一版用 `complex(sympy.N(value, precision))`
       —— 声称 50 位精度，结果被压成 16 位有效数字，而容差还按 50 位设。
       **判据比数据本身精确，就必然造出假反例。**
       2026-08-22 e2e 实测：无穷几何级数在 x≈1.5e-7 处两边相对差 1.15e-10
       （病态点上的灾难性相消），被判 failed —— 而那是教科书公式。

    3. 方向是死的：**算不出来就说算不出来（None → 跳过该点），
       永远不要因为"我算不动"而报"这是错的"。**
    """
    # ⚠️ mpmath 的工作精度是**全局**的。不在这里按本次 precision 设一遍，
    # `_stable_value` 那两次"不同精度"的计算就会用同一个 dps —— 自校验
    # 形同虚设（实测：两次结果一模一样，病态点一个都拦不住）。
    saved_dps = mpmath.mp.dps
    mpmath.mp.dps = max(int(precision), 15) + 10      # 留 10 位护栏位
    try:
        value = expr.subs(point)
        unevaluated = (sympy.Sum, sympy.Integral, sympy.Product, sympy.Derivative)
        if value.has(*unevaluated):
            try:
                value = value.doit()
            except Exception:
                return None
        if value.has(*unevaluated):
            return None      # doit 之后仍未求值（如上界仍是符号）
        try:
            number = sympy.N(value, precision)
        except Exception:
            return None
        if not getattr(number, "is_number", False):
            return None
        # 走字符串转 mpmath —— complex() 会把结果压回 float64（16 位），
        # 而容差是按 precision 设的。判据比数据精确 = 假反例工厂。
        try:
            real = mpmath.mpf(str(sympy.re(number).evalf(precision)))
            imag = mpmath.mpf(str(sympy.im(number).evalf(precision)))
        except Exception:
            return None
        return mpmath.mpc(real, imag)
    finally:
        mpmath.mp.dps = saved_dps


def _stable_value(sympy: Any, mpmath: Any, expr: Any, point: dict, precision: int):
    """同一点上用两种精度各算一次；两次对不上说明**这个点数值不稳定**。

    这是数值分析的标准自校验：不去猜"哪些点危险"（1/(1-exp(-x)) 在 x→0
    处的灾难性相消不是我能一一列举的），而是让计算自己报告它靠不靠谱。
    不稳定的点直接跳过 —— 不算证据，**更不算反例**。
    """
    low = _evaluate_at(sympy, mpmath, expr, point, precision)
    if low is None:
        return None
    high = _evaluate_at(sympy, mpmath, expr, point, precision * 2)
    if high is None:
        return None
    scale = max(abs(low), abs(high), mpmath.mpf(1))
    if abs(low - high) > scale * mpmath.mpf(10) ** (-(precision // 2)):
        return None          # 两次精度结果自己就不一致 → 这个点不可信
    return high


def _numerics_are_trustworthy(sympy: Any, *exprs: Any) -> bool:
    """这些表达式的数值路径可不可信。

    判据：**符号层面 `doit()` 能不能求出闭式**。求不出来的，代入具体数值后
    sympy 会走数值近似路径 —— 而那条路径在这里不可信。

    2026-08-22 e2e 的决定性证据（x≈1.549e-7，Σ_{n≥0} e^{-xn} vs 1/(1-e^{-x})，
    一个精确恒等式）：

        mpmath 独立求和   6454064.99257116773260515068604964236722277911
        闭式             6454064.99257116773260515068604964236722277911   ← 一致
        sympy Sum.doit()  6454064.99331501434701368416249975790673123583   ← 错

    更精确的根因由**跑这一趟的模型自己查出来**（它用 execute_python 看了
    lambdify 的生成代码，比我诊断得准）：sympy 对 `Sum(..., oo)` 生成
    `builtins.sum(exp(-n*x) for n in range(0, inf+1))` —— `inf` 不是整数，
    迭代无效，于是**求值结果退化成首项**（实测 lhs_value = 1 = e^{-x·0}）。

    这也解释了为什么双精度自校验拦不住它：取首项跟工作精度无关，
    50 位和 100 位下**稳定地给出同一个错值**。

    所以这道判据落在**表达式层面**，不落在点上：符号求不出闭式 → 整条
    数值抽查放弃，判 inconclusive。**算不出来就说算不出来，
    永远不要因为"我算不动"而报"这是错的"。**

    注意边界：有限求和、定积分、等差求和的 `doit()` 都求得出（哪怕结果是
    Piecewise），它们的数值路径照常可信 —— 这道闸只挡真正求不出来的那些。
    """
    # ① 未定义函数（`Prov(x)`、`f(x)` 这种）：数值代入毫无意义 —— 随机数字
    #    喂给一个语义未知的符号，两边"不相等"是必然的，不是反例。
    #
    #    2026-08-22 拿哥德尔第一不完备定理的真实步骤打了一遍，实测：
    #    `Prov(gn(A))` vs `Provable(A)` 被判 **failed（找到反例，强结论）**。
    #    模型若信了这条，就会去"修"一个正确的元数学命题。
    #
    #    ⚠️ 只拦数值路径，符号路径照走 —— `diff(f*g) = f*g' + g*f'`（乘积法则）
    #    含未定义函数，但 sympy 符号上验得出 verified。一刀切会把整类
    #    "关于一般函数的恒等式"变成永远的 inconclusive。
    from sympy.core.function import AppliedUndef

    for expr in exprs:
        if expr.atoms(AppliedUndef):
            return False

    # ② 符号层面求不出闭式的求和/积分/连乘：sympy 代入后走数值近似，不可信。
    unresolved = (sympy.Sum, sympy.Integral, sympy.Product)
    for expr in exprs:
        if not expr.has(*unresolved):
            continue
        try:
            if expr.doit().has(*unresolved):
                return False
        except Exception:
            return False
    return True


def _numeric_probe(
    sympy: Any, mpmath: Any, lhs: Any, rhs: Any, relation: str,
    symbols: list[Any], points: int, precision: int, seed: int,
    assumptions: dict[str, Any] | None,
) -> dict[str, Any]:
    """在随机点上比较两侧。返回 {verdict, checked, counterexample?}。"""
    if not _numerics_are_trustworthy(sympy, lhs, rhs):
        # 符号层面求不出闭式 → sympy 的数值近似不可信 → 不做抽查，判无知。
        return {"verdict": "inconclusive", "checked": 0,
                "untrustworthy_numerics": True}
    mpmath.mp.dps = precision
    tolerance = mpmath.mpf(10) ** (-(precision // 2))
    sample = _sample_points(sympy, mpmath, symbols, points, seed, assumptions)
    #: `_sample_points` 把病态点排在最前面（±1、1e-12）。它们最容易触发
    #: 灾难性相消，所以在这些点上判 broken 之前要加验一道。
    pathological_upto = 3
    checked = 0
    for index, point in enumerate(sample):
        try:
            left = _stable_value(sympy, mpmath, lhs, point, precision)
            right = _stable_value(sympy, mpmath, rhs, point, precision)
        except (TypeError, ValueError, ZeroDivisionError):
            continue        # 奇点/定义域外：跳过，不算证据也不算反例
        if left is None or right is None:
            continue        # 求不出数、或这个点数值不稳定：**不算反例**
        if any(v != v for v in (mpmath.re(left), mpmath.re(right))):  # NaN
            continue
        checked += 1
        scale = max(abs(left), abs(right), mpmath.mpf(1))
        if relation == "eq":
            broken = abs(left - right) > tolerance * scale
        else:
            if abs(mpmath.im(left)) > 1e-9 or abs(mpmath.im(right)) > 1e-9:
                continue    # 不等式只在实数上有意义
            a, b = mpmath.re(left), mpmath.re(right)
            broken = {
                "lt": lambda: not a < b, "le": lambda: not a <= b,
                "gt": lambda: not a > b, "ge": lambda: not a >= b,
            }[relation]()
        if broken and index < pathological_upto:
            # 病态点上的 broken 先当嫌疑，不当结论：用 4 倍精度复算一次。
            # 真的不等，差值不会随精度消失；灾难性相消会。
            # **方向是死的**：宁可漏报一个真反例，也不能造一个假反例 ——
            # 假反例让模型去推翻一个正确的步骤、找一个不存在的 bug。
            deep = precision * 4
            mpmath.mp.dps = deep
            try:
                left_deep = _stable_value(sympy, mpmath, lhs, point, deep)
                right_deep = _stable_value(sympy, mpmath, rhs, point, deep)
            except (TypeError, ValueError, ZeroDivisionError):
                left_deep = right_deep = None
            mpmath.mp.dps = precision
            if left_deep is None or right_deep is None:
                continue
            deep_scale = max(abs(left_deep), abs(right_deep), mpmath.mpf(1))
            deep_tolerance = mpmath.mpf(10) ** (-(deep // 2))
            if relation == "eq":
                broken = abs(left_deep - right_deep) > deep_tolerance * deep_scale
            if not broken:
                continue        # 高精度下差值消失了 —— 那是相消误差，不是反例
            left, right = left_deep, right_deep

        if broken:
            return {
                "verdict": "failed",
                "checked": checked,
                "counterexample": {
                    "point": {s.name: str(v) for s, v in point.items()},
                    "lhs_value": str(left), "rhs_value": str(right),
                    "pathological": index < pathological_upto,
                },
            }
    return {"verdict": "numerically_supported" if checked else "inconclusive",
            "checked": checked}


async def _check_step(
    state: Any,
    lhs: str,
    rhs: str,
    relation: str = "eq",
    assumptions: dict | None = None,
    symbols: list | None = None,
    points: int = _DEFAULT_POINTS,
    precision: int = _DEFAULT_PRECISION,
    seed: int = 20260822,
    **_: Any,
) -> dict:
    loaded = _require_sympy()
    if isinstance(loaded, dict):
        return loaded
    sympy, mpmath = loaded

    # relation 的枚举由 parameters_schema 声明（enum=_RELATIONS），派发口核一次。
    relation = str(relation or "eq").strip().lower()

    # 显式声明的符号带上假设 —— sympy 的化简对 positive/real 敏感，
    # 而"在什么条件下成立"正是推导里最容易丢的东西。
    table: dict[str, Any] = {}
    for name in (symbols or []):
        table[str(name)] = sympy.Symbol(str(name))
    for name, spec in (assumptions or {}).items():
        flag = str(spec).strip().lower()
        kwargs = {}
        if flag in ("positive", "pos", ">0"):
            kwargs["positive"] = True
        elif flag in ("real",):
            kwargs["real"] = True
        elif flag in ("integer", "int"):
            kwargs["integer"] = True
        elif flag in ("nonzero", "!=0"):
            kwargs["nonzero"] = True
        table[str(name)] = sympy.Symbol(str(name), **kwargs)

    try:
        left = _parse(sympy, lhs, table)
        right = _parse(sympy, rhs, table)
    except Exception as exc:
        return {"status": "error",
                "error": (f"表达式解析失败：{exc}。用 Python/sympy 语法："
                          "乘号写 `*`、幂写 `**`（`^` 也认）、函数用 sqrt/exp/log/sin。")}

    free = _free_symbols(sympy, left, right)
    probe = _ledger.probe_id(lhs, rhs, relation)
    result: dict[str, Any] = {
        "status": "success",
        "lhs": str(left), "rhs": str(right), "relation": relation,
        "symbols": [s.name for s in free],
    }

    def _seal(verification: dict[str, Any]) -> dict[str, Any]:
        return seal_verification(
            state, verification, tool="check_step", lhs=lhs, rhs=rhs,
            relation=relation, assumptions=assumptions)

    # ── 第一路：符号 ────────────────────────────────────────────────────────
    symbolic = None
    if relation == "eq":
        try:
            difference = sympy.simplify(left - right)
            if difference == 0:
                symbolic = "verified"
            else:
                # equals() 内部会做数值探测，比 simplify 更强；它明确返回 False
                # 才算证否 —— None 是"不知道"。
                verdict = left.equals(right)
                if verdict is True:
                    symbolic = "verified"
                elif verdict is False:
                    symbolic = "refuted"
        except Exception:
            symbolic = None
    else:
        try:
            relation_expr = {"lt": sympy.Lt, "le": sympy.Le,
                             "gt": sympy.Gt, "ge": sympy.Ge}[relation](left, right)
            simplified = sympy.simplify(relation_expr)
            if simplified is sympy.true:
                symbolic = "verified"
            elif simplified is sympy.false:
                symbolic = "refuted"
        except Exception:
            symbolic = None

    if symbolic == "verified":
        result["verification"] = _seal({
            "method": "symbolic", "status": "verified",
            "tool": "check_step", "assumptions": dict(assumptions or {}),
        })
        result["note"] = "符号层面确证。"
        return result

    # ── 第二路：数值抽查（也用来把 symbolic=refuted 落成具体反例）────────────
    numeric = _numeric_probe(sympy, mpmath, left, right, relation, free,
                             int(points), int(precision), int(seed), assumptions)
    verdict = numeric["verdict"]
    if not free:
        # 无自由符号：数值就是全部真相，没抽到点说明两边都是常数且相等
        verdict = "verified" if verdict == "numerically_supported" else verdict

    if verdict == "failed":
        result["verification"] = _seal({
            "method": "numeric", "status": "failed", "tool": "check_step",
            "points_checked": numeric["checked"], "precision": int(precision),
            "seed": int(seed), "assumptions": dict(assumptions or {}),
        })
        result["counterexample"] = numeric["counterexample"]
        result["note"] = (
            "找到反例 —— 这一步不成立。**这是强结论**：不要改写措辞绕过它，"
            "回到上一步找出哪里错了。若你认为反例点在适用域外，"
            "把该约束写进 assumptions 重跑（并把它记进假设账本）。")
        return result

    result["verification"] = _seal({
        "method": "numeric" if verdict == "numerically_supported" else "none",
        "status": verdict, "tool": "check_step",
        "points_checked": numeric["checked"], "precision": int(precision),
        "seed": int(seed), "assumptions": dict(assumptions or {}),
    })
    result["note"] = (
        "符号没化出结论，但 %d 个随机点（%d 位精度）上都成立 —— 这是**支持**，"
        "不是证明。若这一步承重，请补一个演绎论证，或把它降级成待证引理。"
        % (numeric["checked"], int(precision))
        if verdict == "numerically_supported" else
        "两条路都没有结论（**不等于这一步是错的**）。\n"
        "若式子里有**未定义函数/谓词**（`Prov(x)`、`f(x)` 这类），数值抽查对它们"
        "本就无意义 —— 这类命题（逻辑推理、元数学、关于一般函数的断言）"
        "超出 CAS 能机械判定的范围，把这一步标成 `cited_theorem`（带外部锚点）"
        "或 `prose` 并写清论证，是**正当出口**，不是失败。\n"
        "否则可试：缩小适用域后重试、把表达式拆成更小的步骤、"
        "或换 find_counterexample 主动找反例。"
    )
    return result


async def _find_counterexample(
    state: Any,
    claim_lhs: str,
    claim_rhs: str,
    relation: str = "eq",
    assumptions: dict | None = None,
    budget: int = 400,
    precision: int = 30,
    seed: int = 20260822,
    **_: Any,
) -> dict:
    """主动找反例。找不到是结论，没找不是。"""
    loaded = _require_sympy()
    if isinstance(loaded, dict):
        return loaded

    budget = max(1, min(int(budget), 5000))
    # 多轮不同 seed 的抽查 —— 单次 check_step 的 32 点是"顺手看一眼"，
    # 这里是"专门去砸"，量级不同。
    rounds = max(1, budget // _DEFAULT_POINTS)
    for index in range(rounds):
        probe = await _check_step(
            state, claim_lhs, claim_rhs, relation=relation,
            assumptions=assumptions, points=_DEFAULT_POINTS,
            precision=int(precision), seed=int(seed) + index * 7919)
        if probe.get("status") != "success":
            return probe
        if probe.get("verification", {}).get("status") == "failed":
            return {
                "status": "success", "found": True,
                "counterexample": probe.get("counterexample"),
                "searched_points": (index + 1) * _DEFAULT_POINTS,
                # 章由内部那次 check_step 盖好、也已入账（同一个 probe 空间）——
                # 原样带出来给模型贴进 step.verification。此前这里把它扔了，
                # 于是"找到反例"这个**最强的结论**反而没有合法的记录方式。
                "verification": probe.get("verification"),
                "note": ("找到反例 —— 这条命题不成立（至少在你声明的假设下不成立）。"
                         "把 verification 原样贴进对应 step，或撤掉这条路线。"),
            }
        if probe.get("verification", {}).get("status") == "verified":
            return {
                "status": "success", "found": False, "symbolically_verified": True,
                "searched_points": (index + 1) * _DEFAULT_POINTS,
                "verification": probe.get("verification"),
                "note": "符号层面已确证成立，反例搜索无意义。",
            }
    return {
        "status": "success", "found": False,
        "searched_points": rounds * _DEFAULT_POINTS,
        "precision": int(precision),
        "counterexample_search": {
            "budget": rounds * _DEFAULT_POINTS, "found": False,
            "precision": int(precision), "seed": int(seed), "tool": "find_counterexample",
        },
        "note": ("在 %d 个点上没找到反例。这是**支持性证据**，不是证明 —— "
                 "把这块 counterexample_search 原样写进 derivation_log 的 metadata。"
                 % (rounds * _DEFAULT_POINTS)),
    }


async def _dimensional_check(
    state: Any,
    expression: str,
    units: dict | None = None,
    expected: str | None = None,
    **_: Any,
) -> dict:
    """量纲一致性：加减两侧同量纲、等式两侧同量纲。

    用 sympy.physics.units（环境里已有），不引 pint。
    """
    loaded = _require_sympy()
    if isinstance(loaded, dict):
        return loaded
    sympy, _mp = loaded
    try:
        from sympy.physics import units as u
        from sympy.physics.units.systems.si import SI
    except ImportError as exc:      # pragma: no cover
        return {"status": "error", "error": f"sympy.physics.units 不可用：{exc}"}

    unit_names = {k: getattr(u, k) for k in dir(u) if not k.startswith("_")}
    table: dict[str, Any] = {}
    unknown: list[str] = []
    for name, unit_text in (units or {}).items():
        try:
            parsed = _parse(sympy, str(unit_text), dict(unit_names))
        except Exception:
            unknown.append(f"{name}={unit_text}")
            continue
        # ⚠️ `parse_expr` 对不认识的名字**自动造一个 Symbol**，于是拼错的单位名
        # 解析"成功"，然后被当成一个无量纲自由变量算进去 —— 量纲检查静默给出
        # 错误答案。实测 `furlongs_per_fortnight` 就这样一路通过。
        # 判据：单位表达式解析完不许剩下自由符号（真单位都是 Quantity）。
        leftover = sorted(s.name for s in getattr(parsed, "free_symbols", ()))
        if leftover:
            unknown.append(f"{name}={unit_text}（不认识：{'、'.join(leftover)}）")
            continue
        table[str(name)] = parsed
    if unknown:
        return {"status": "error",
                "error": (f"这些单位解析不了：{'、'.join(unknown)}。"
                          "用 sympy.physics.units 的名字，如 meter / second / kilogram / "
                          "joule / kelvin，组合写 `kilogram*meter**2/second**2`。")}

    try:
        expr = _parse(sympy, expression, dict(table))
        substituted = expr.subs(table)
    except Exception as exc:
        return {"status": "error", "error": f"表达式解析失败：{exc}"}

    # ⚠️ 不用 `SI.get_dimensional_expr`：它对量纲**不一致**的加法静默返回第一项
    # 的量纲。实测 `kilogram + meter/second` 得到 `length/time` —— 一个错误答案，
    # 比不检查更坏（"检查过了"的假象）。而抓这类错正是本工具存在的理由。
    #
    # `_collect_factor_and_dimension` 会逐项核对并在不一致时抛 ValueError。
    # 判据是**这个 API 成功还是抛 ValueError**（它表达不一致的方式），
    # 不是去看错误消息长什么样。
    try:
        _factor, dimension = SI._collect_factor_and_dimension(substituted)
    except ValueError as exc:
        return {
            "status": "success", "expression": str(expr),
            "consistent": False,
            "checks": {"dimensional": {
                "tool": "dimensional_check", "consistent": False,
                "conflict": str(exc),
                "units": {k: str(v) for k, v in (units or {}).items()}}},
            "note": ("⛔ **量纲不一致** —— 把不同量纲的量加/减到了一起："
                     f"{exc}。量纲错就是式子错，别往下推，回去找哪一步出的问题。"),
        }
    except Exception as exc:
        return {"status": "error",
                "error": (f"量纲推导失败（工具侧，不是你的式子的问题）："
                          f"{type(exc).__name__}: {exc}")}
    def _basis(dim: Any) -> dict[str, int] | None:
        """把量纲规约成基本量纲的幂次 —— 判"两个量纲相不相等"的唯一可靠判据。

        表达式长相不能当判据：`joule` 是 `Dimension(energy, E)`，而
        `kilogram*meter**2/second**2` 是 `Dimension(length**2*mass/time**2)`，
        字面上天差地别，展开后同为 {mass:1, length:2, time:-2}。
        key 也要取 `.name`：同一个基本量纲在两条路径上一个带符号一个不带
        （`Dimension(mass)` vs `Dimension(mass, M)`），直接比字典会假不等。
        """
        try:
            deps = SI.get_dimension_system().get_dimensional_dependencies(dim)
            return {str(getattr(k, "name", k)): int(v) for k, v in deps.items()}
        except Exception:
            return None

    dimension_basis = _basis(dimension)
    dimension = sympy.simplify(
        dimension.name if hasattr(dimension, "name") else dimension)

    result: dict[str, Any] = {
        "status": "success", "expression": str(expr), "dimension": str(dimension),
        "consistent": True,
        "checks": {"dimensional": {
            "tool": "dimensional_check", "consistent": True,
            "dimension": str(dimension),
            "units": {k: str(v) for k, v in (units or {}).items()}}},
    }
    if expected:
        try:
            _wf, want_dim = SI._collect_factor_and_dimension(
                _parse(sympy, str(expected), {
                    k: getattr(u, k) for k in dir(u) if not k.startswith("_")}))
            want = sympy.simplify(
                want_dim.name if hasattr(want_dim, "name") else want_dim)
            want_basis = _basis(want_dim)
            matches = (None if (dimension_basis is None or want_basis is None)
                       else dimension_basis == want_basis)
        except Exception:
            want, matches = expected, None
        result["expected"] = str(expected)
        result["matches_expected"] = matches
        result["checks"]["dimensional"]["expected"] = str(expected)
        result["checks"]["dimensional"]["matches"] = matches
        if matches is False:
            result["note"] = ("⛔ 量纲对不上：推出来的是 %s，声明的是 %s。"
                              "量纲错就是式子错，先别往下推。" % (dimension, want))
            # ── 证否才盖章（见本文件末尾「谁能证成、谁只能证否」）────────────
            # 量纲**对**不构成对这一步的验证：它是必要条件不是充分条件，
            # 一个量纲正确、系数错了一倍的式子照样通过。盖成 verified 就是
            # 语义膨胀 —— 模型会拿它当"这步验过了"。
            # 量纲**错**是强结论：量纲错就是式子错，这一维足以证否整步。
            result["verification"] = seal_verification(
                state, {"status": "failed", "method": "symbolic",
                        "detail": f"量纲 {dimension} ≠ 期望 {want}"},
                tool="dimensional_check", lhs=str(expr), rhs=str(expected),
                relation="dim_eq")
    return result


async def _limit_check(
    state: Any,
    expression: str,
    variable: str,
    approaching: str,
    expected: str | None = None,
    direction: str = "+",
    **_: Any,
) -> dict:
    """极限/特例回归：结果在声明的极限下退化成已知答案吗。

    物理推导最有力的自查：v→0 回牛顿、T→∞ 回经典、耦合→0 回自由理论。
    """
    loaded = _require_sympy()
    if isinstance(loaded, dict):
        return loaded
    sympy, _mp = loaded
    try:
        symbol = sympy.Symbol(str(variable))
        expr = _parse(sympy, expression, {str(variable): symbol})
        target = _parse(sympy, str(approaching), {str(variable): symbol})
        limit = sympy.limit(expr, symbol, target,
                            dir=direction if direction in ("+", "-") else "+")
    except Exception as exc:
        return {"status": "error", "error": f"极限计算失败：{exc}"}

    out: dict[str, Any] = {
        "status": "success", "limit": str(limit),
        "variable": str(variable), "approaching": str(approaching),
    }
    if expected is not None:
        try:
            want = _parse(sympy, str(expected), {str(variable): symbol})
            matches = sympy.simplify(limit - want) == 0
        except Exception:
            matches = None
        out["expected"] = str(expected)
        out["matches_expected"] = matches
        out["checks"] = {"limit": {
            "tool": "limit_check", "variable": str(variable),
            "approaching": str(approaching), "limit": str(limit),
            "expected": str(expected), "matches": matches}}
        if matches is False:
            out["note"] = ("⛔ 极限对不上：%s → %s 时得到 %s，而已知答案是 %s。"
                           "这是个强信号 —— 极限不对，多半是前面某一步错了。"
                           % (variable, approaching, limit, want))
            # 同 dimensional_check：极限**对**只说明一个边界情形吻合，
            # 不构成对整步的验证；极限**错**足以证否。只在证否侧盖章。
            out["verification"] = seal_verification(
                state, {"status": "failed", "method": "symbolic",
                        "detail": f"{variable}→{approaching} 时极限为 {limit}，"
                                  f"期望 {want}"},
                tool="limit_check",
                lhs=f"limit({expression}, {variable}->{approaching})",
                rhs=str(expected), relation="eq")
    return out


# ── 注册：共享工具面 ────────────────────────────────────────────────────────
#
# allowed_node_types 留空 = 任何节点白名单里写了就能用。这是刻意的：
# observation 合并效应量、experiment 核对理论预测、hypothesis 定阈值时验一下
# 量纲，都是"顺手算一下"，不该为此起一个 producing run。
# 证据所有权（谁能产 derivation_log）由 artifact_policy 独占管，与工具面无关 ——
# **工具共享，证据所有权独占**。

register_tool(
    ToolDefinition(
        name="check_step",
        description=(
            "机械验证推导的一步：lhs 与 rhs 在给定关系下是否成立。"
            "先走 sympy 符号化简，没结论再退到高精度随机点数值抽查（默认 32 点 / 50 位，"
            "覆盖多个量级与复值）。"
            "**四值返回**：verified（符号确证）/ numerically_supported（数值支持，非证明）/ "
            "inconclusive（两条路都没结论，**不等于错**）/ failed（找到反例，强结论）。"
            "⚠️ verified 的语义是「**在 CAS 默认假设下**等价」，不是「在所有情况下都对」："
            "`check_step('x**2/x - x/x', 'x - 1')` 返回 verified，哪怕你没声明 x≠0 —— "
            "化简时隐含了它。做除法/开方/对数/级数/换序之后，"
            "主动把用到的条件记进你的假设账本。"
            "返回的 verification 块是 derivation_log 每一步唯一合法的验证来源 —— "
            "自己写 verified 进不了冻结门。"
            "assumptions 形如 {'x': 'positive', 'n': 'integer'}：既影响符号化简，"
            "也让数值抽查避开适用域外的点。\n"
            "**数值核对（闭式 vs 已知值/实验值）用不等式形式**，不要直接 eq —— "
            "`2/log(1+sqrt(2))` 与 `2.269185314` 差 2e-10，判 eq 会得到 failed。"
            "正确写法：lhs=`Abs(你的闭式 - 已知值)`、rhs=`1e-9`、relation=`lt`。"
            "**容差必须你来定**：多少算一致是科学判断（测量精度？理论截断？），"
            "框架只负责机械比较。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "lhs": {"type": "string", "description": "左边表达式（Python/sympy 语法）。"},
                "rhs": {"type": "string", "description": "右边表达式。"},
                "relation": {"type": "string", "enum": list(_RELATIONS), "default": "eq",
                             "description": "两侧的关系。默认 eq（相等）。"},
                "assumptions": {"type": "object",
                                "description": "符号约束，如 {'x': 'positive'}。合法值："
                                               "positive / real / integer / nonzero。"},
                "symbols": {"type": "array", "items": {"type": "string"},
                            "description": "可选：显式列出自由符号名。"},
                "points": {"type": "integer", "default": _DEFAULT_POINTS,
                           "description": "数值抽查点数。"},
                "precision": {"type": "integer", "default": _DEFAULT_PRECISION,
                              "description": "十进制工作精度位数。"},
                "seed": {"type": "integer", "default": 20260822,
                         "description": "随机种子（同种子可复现）。"},
            },
            "required": ["lhs", "rhs"],
        },
        risk_level="low",
    ),
    _check_step,
)

register_tool(
    ToolDefinition(
        name="find_counterexample",
        description=(
            "主动去砸一条命题：在大量随机点上找反例。"
            "**找不到反例是结论，没找过不是** —— 两者在结果上都表现为「没有反例」，"
            "只有搜索记录能区分。返回的 counterexample_search 块直接写进 "
            "derivation_log 的 metadata。"
            "当前 LLM 找反例的能力强于构造证明，这是你最便宜的一记重拳。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "claim_lhs": {"type": "string"},
                "claim_rhs": {"type": "string"},
                "relation": {"type": "string", "enum": list(_RELATIONS), "default": "eq"},
                "assumptions": {"type": "object",
                                "description": "适用域约束，如 {'x': 'positive'}。"},
                "budget": {"type": "integer", "default": 400,
                           "description": "抽查点预算（上限 5000）。"},
                "precision": {"type": "integer", "default": 30},
                "seed": {"type": "integer", "default": 20260822},
            },
            "required": ["claim_lhs", "claim_rhs"],
        },
        risk_level="low",
    ),
    _find_counterexample,
)

register_tool(
    ToolDefinition(
        name="dimensional_check",
        description=(
            "量纲一致性检查（sympy.physics.units）。给每个符号指定单位，"
            "推出整个表达式的量纲；给了 expected 就核对。"
            "**量纲错就是式子错** —— 这是物理推导最便宜的一道体检，"
            "在往下推之前先过一遍。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "要检查的表达式。"},
                "units": {"type": "object",
                          "description": "符号 → 单位，如 {'m': 'kilogram', 'v': 'meter/second'}。"},
                "expected": {"type": "string",
                             "description": "可选：期望量纲，如 'kilogram*meter**2/second**2'。"},
            },
            "required": ["expression"],
        },
        risk_level="low",
    ),
    _dimensional_check,
)

register_tool(
    ToolDefinition(
        name="limit_check",
        description=(
            "极限 / 特例回归：表达式在某个极限下退化成什么，与已知答案对不对得上。"
            "v→0 回牛顿、T→∞ 回经典、耦合→0 回自由理论 —— "
            "极限对不上是个强信号，多半前面某步就错了。"
            "挑**有判别力**的极限，不是最好算的那个。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
                "variable": {"type": "string", "description": "取极限的变量名。"},
                "approaching": {"type": "string",
                                "description": "趋向的值：0 / oo / -oo / 具体表达式。"},
                "expected": {"type": "string", "description": "可选：期望的极限值。"},
                "direction": {"type": "string", "enum": ["+", "-"], "default": "+"},
            },
            "required": ["expression", "variable", "approaching"],
        },
        risk_level="low",
    ),
    _limit_check,
)


# ── 谁能证成、谁只能证否 ────────────────────────────────────────────────────
#
# 这张表是本模块的架构，2026-08-23 定：
#
# | 工具 | 证成 | 证否 | method | 严格性 |
# |---|---|---|---|---|
# | check_step          | ✅ CAS 默认假设下 | ✅ | symbolic / numeric | 强 / 抽样 |
# | dimensional_check   | ❌ | ✅ | symbolic | 严格（量纲错=式子错）|
# | limit_check         | ❌ | ✅ | symbolic | 严格 |
# | find_counterexample | ❌ | ✅ | numeric  | 严格（给出具体反例点）|
# | interval_check      | ❌ | ✅ | interval | **严格**（区间外包围）|
# | check_lean          | ✅ | —  | formal   | **最强**（形式化内核）|
#
# 中间四个**只在证否侧盖章**。量纲对、极限对、没找到反例、区间含 0 —— 这些
# 全都是必要条件而非充分条件，盖成 verified 就是语义膨胀：模型会拿"量纲对了"
# 当"这步验过了"。而它们证否的时候是**强结论**，足以否掉整步。
#
# 这与「验证不对称性」是同一件事的两面：证否便宜且严格，证成贵。


def _flint():
    """python-flint（Arb ball arithmetic）。没装就返回 None —— 能力探测。"""
    try:
        import flint                                    # type: ignore
        return flint
    except Exception:
        return None


async def _interval_check(
    state: Any,
    lhs: str,
    rhs: str,
    domain: dict | None = None,
    precision_bits: int = 256,
    **_: Any,
) -> dict[str, Any]:
    """区间算术（ball arithmetic）严格证否：在**整个区间**上验 lhs = rhs。

    ## 它能断言什么、不能断言什么

    区间算术算的是**外包围**（over-approximation）：真值一定落在算出的区间里，
    但算出的区间通常比真值集合宽（包裹效应）。因此：

      · 差值区间**不含 0** → **严格证否**。整个输入区间上 lhs ≠ rhs，
        不是"我抽的这 32 个点不等"，是"这个区间里没有一个点相等"。
        这比随机抽样强一个量级。
      · 差值区间**含 0** → **什么都证明不了**。可能真的恒等，也可能只是
        包裹效应太宽。实测：sin²+cos²−1 在 [0.4,1.0] 上算出 [±0.732]，
        而它数学上恒等于 0。

    所以本工具**只在证否侧盖章**。把 contains(0) 当成"验证通过"，
    就是拿一个不严格的判据冒充严格 —— 那正是这个节点的原罪。
    """
    flint = _flint()
    if flint is None:
        return {"status": "error",
                "error": "本环境没有 python-flint，区间验证不可用。"}
    loaded = _require_sympy()
    if isinstance(loaded, dict):
        return loaded
    sympy, _mpmath = loaded

    domain = domain if isinstance(domain, dict) else {}
    flint.ctx.prec = max(64, min(int(precision_bits), 4096))

    table: dict[str, Any] = {}
    try:
        left = _parse(sympy, lhs, table)
        right = _parse(sympy, rhs, table)
    except Exception as exc:
        return {"status": "error", "error": f"表达式解析失败：{exc}"}

    free = sorted(_free_symbols(sympy, left, right), key=lambda s: s.name)
    missing = [s.name for s in free if s.name not in domain]
    if missing:
        return {
            "status": "error",
            "error": (f"这些变量没给区间：{', '.join(missing)}。"
                      f"domain 形如 {{'x': [0.5, 2.0]}} —— 区间验证必须知道"
                      f"在**哪个范围**上验，否则它退化成一次点估值。"),
        }

    def _ball(bounds: Any):
        lo, hi = (float(bounds[0]), float(bounds[1])) if isinstance(
            bounds, (list, tuple)) and len(bounds) == 2 else (float(bounds), float(bounds))
        mid, rad = (lo + hi) / 2.0, abs(hi - lo) / 2.0
        return flint.arb(mid, rad)

    try:
        env = {s.name: _ball(domain[s.name]) for s in free}
        difference = _eval_arb(flint, sympy, left - right, env)
    except Exception as exc:
        return {"status": "error",
                "error": (f"区间求值失败：{exc}。"
                          f"本工具只支持 Arb 有实现的函数"
                          f"（四则/幂/exp/log/sin/cos/tan/sqrt/atan/sinh/cosh）。")}

    contains_zero = bool(difference.contains(0))
    result: dict[str, Any] = {
        "status": "success",
        "difference_enclosure": str(difference),
        "contains_zero": contains_zero,
        "domain": {k: list(v) if isinstance(v, (list, tuple)) else v
                   for k, v in domain.items()},
        "precision_bits": flint.ctx.prec,
    }
    if not contains_zero:
        result["verification"] = seal_verification(
            state, {"status": "failed", "method": "interval",
                    "detail": (f"差值的区间外包围 {difference} 严格不含 0 —— "
                               f"在整个区间上 lhs ≠ rhs")},
            tool="interval_check", lhs=lhs, rhs=rhs, relation="eq")
        result["note"] = (
            "⛔ **严格证否**：差值区间不含 0，整个 domain 上都不相等。"
            "这不是抽样结论 —— 把 verification 贴进对应 step，或撤掉这条路线。")
    else:
        result["note"] = (
            "区间含 0 —— **这什么都没证明**。区间算术是外包围，含 0 既可能是"
            "真的恒等，也可能只是包裹效应太宽（实测 sin²+cos²−1 在 [0.4,1] 上"
            "算出 [±0.732]）。要证成请用 check_step 的符号路，或 check_lean。\n"
            "如果你**本来是想证否**：多半是 domain 给太宽了。同一个变量在式子里"
            "出现多次时，区间算术把每次出现当独立变量处理（依赖问题），区间会被"
            "严重放大 —— 实测 exp(x+y) vs exp(x)+exp(y)：x,y∈[1,2] 算出 [±49.2]"
            "（含 0，证否失败），收窄到 [1,1.01] 就变成 [2±0.103]（严格证否）。"
            "**分段收窄再逐段验**是标准做法。")
    return result


def _eval_arb(flint, sympy, expr, env: dict):
    """把 sympy 表达式在 Arb ball 上求值（递归，只认有严格实现的函数）。"""
    if expr.is_Symbol:
        return env[expr.name]
    if expr.is_Integer:
        return flint.arb(int(expr))
    if expr.is_Rational:
        return flint.arb(int(expr.p)) / flint.arb(int(expr.q))
    if expr.is_Float:
        return flint.arb(str(expr))
    if expr is sympy.pi:
        return flint.arb.pi()
    if expr is sympy.E:
        return flint.arb(1).exp()
    if expr.is_Add:
        out = flint.arb(0)
        for term in expr.args:
            out = out + _eval_arb(flint, sympy, term, env)
        return out
    if expr.is_Mul:
        out = flint.arb(1)
        for factor in expr.args:
            out = out * _eval_arb(flint, sympy, factor, env)
        return out
    if expr.is_Pow:
        base = _eval_arb(flint, sympy, expr.base, env)
        exponent = expr.exp
        if exponent.is_Integer:
            return base ** int(exponent)
        return base ** _eval_arb(flint, sympy, exponent, env)
    unary = {
        sympy.exp: "exp", sympy.log: "log", sympy.sin: "sin", sympy.cos: "cos",
        sympy.tan: "tan", sympy.atan: "atan", sympy.sinh: "sinh",
        sympy.cosh: "cosh", sympy.tanh: "tanh", sympy.asin: "asin",
        sympy.acos: "acos", sympy.asinh: "asinh", sympy.acosh: "acosh",
    }
    for func, method in unary.items():
        if expr.func is func:
            return getattr(_eval_arb(flint, sympy, expr.args[0], env), method)()
    if expr.func is sympy.sqrt or (expr.is_Pow and expr.exp == sympy.Rational(1, 2)):
        return _eval_arb(flint, sympy, expr.args[0], env).sqrt()
    raise ValueError(f"Arb 没有 {expr.func} 的严格实现")


# ── 形式化后端：Lean 4 ──────────────────────────────────────────────────────

def _lean_binary() -> str | None:
    """本机的 lean 可执行文件。没有就返回 None —— 能力探测。

    「能力缺席就不给工具」：探不到 Lean 就**不注册** check_lean，
    于是它的名字也不在 trusted_verifiers 里。模型手写一个
    `tool: "check_lean"` 的章会被当场拒掉，而不是溜过第一道判据 ——
    2026-08-23 之前 check_lean 只是 TRUSTED_VERIFIERS 里的一个字符串，
    那正是一条只在账本失灵时生效的近路。
    """
    import os
    import shutil

    found = shutil.which("lean")
    if found:
        return found
    fallback = os.path.expanduser("~/.elan/bin/lean")
    return fallback if os.path.exists(fallback) else None


async def _check_lean(
    state: Any,
    statement: str,
    proof: str = "",
    preamble: str = "",
    timeout_s: int = 120,
    **_: Any,
) -> dict[str, Any]:
    """把一步交给 Lean 4 内核裁决 —— 本节点唯一的**严格证成**手段。

    ## ⚠️ 信任边界在陈述，不在证明

    Lean 证明的是**你写下的那个形式化命题**，不是你以为的那个自然语言命题。
    翻译错了，Lean 全绿也毫无意义 —— 而且它绿得非常有说服力，
    这使得翻译错误比一般的推导错误**更危险**。

    所以本工具返回的章里一定带 `formal_statement` 原文：审阅的人要核对的是
    那一行，不是"Lean 说对了"这句话。这与 audit 模式要求 audit_target 带
    content_hash 是同一条原则（审了一份、报告贴的是另一份）。

    ## 没有 mathlib

    本机装的是裸 Lean 4 工具链，没有 mathlib。能验的是命题逻辑、基本算术、
    结构归纳这一档；`Nat.add_comm` 这类核心库引理可用，但实分析、测度论、
    群论的成套引理不在。要用它们得另装 mathlib（数 GB，另说）。
    验不了不是罪 —— 报 error 让模型换 check_step，别硬凑。
    """
    import tempfile
    from pathlib import Path

    binary = _lean_binary()
    if binary is None:
        return {"status": "error",
                "error": "本环境没有 Lean 工具链，形式化验证不可用。"}
    # statement 非空由 parameters_schema 声明（minLength:1），派发口核一次。
    body = str(proof).strip() or "by sorry"
    source = "\n".join([
        str(preamble).strip(),
        f"theorem _harness_goal : {str(statement).strip()} := {body}",
        "",
    ]).strip() + "\n"

    from core.sandbox import SandboxLimits
    from shared.lib.cancellable_subprocess import spawn_and_wait

    budget = max(5, min(int(timeout_s), 600))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Goal.lean"
        path.write_text(source, encoding="utf-8")
        # ── 走唯一咽喉：模型写的代码只能在墙内跑 ────────────────────────────
        #
        # 这里编译的是**模型写的 .lean 源码**，而 Lean 4 的 `#eval` 可以执行任意 IO
        # —— 也就是说这条路上跑的是模型的代码，只是 argv 长得像"固定程序 + 一个文件"。
        # 它此前用 `create_subprocess_exec` 直接在宿主上起，绕开了咽喉：没有写边界、
        # 没有断网、没有 kill_event、没有整组清扫、也不进 isolation 记账。
        # 全仓最后一条 `model_command` 债（framework_exemptions.yaml，deadline
        # 2026-10-15）就是它；收进来之后那条登记随之删除。
        #
        # 形状照抄 latex.py::_run_process —— 那条已经验证过：**原样交回 spawn 的
        # status**，不把 timeout / spawn_failed / 编译错压成同一个"退出码非零"。
        # 三件事在界面上是同一句话的代价已经付过一次（三轮 `execvp latexmk` 被显示成
        # "LaTeX 编译失败"，模型三轮都在查一份完全正确的稿子）。
        status, returncode, raw, _stderr = await spawn_and_wait(
            binary, str(path),
            state=state,
            timeout=float(budget),
            cwd=tmp,
            writable_roots=[tmp],
            sandbox_limits=SandboxLimits(
                memory_bytes=4 * 1024**3,
                cpus=2,
                pids=128,
                walltime_seconds=budget,
                storage_bytes=4 * 1024**3,
                output_bytes=16 * 1024**2,
            ),
        )

    if status == "timeout":
        return {"status": "error",
                "error": f"Lean 编译超过 {timeout_s}s。"
                         f"证明太重，或者卡在一个搜索型 tactic 上。"}
    if status == "spawn_failed":
        return {"status": "error",
                "error": "Lean 工具链起不来（不是稿子的问题）："
                         f"{(raw or b'').decode('utf-8', 'replace')[:400]}"}
    if status == "cancelled":
        return {"status": "error", "error": "Lean 编译被取消。"}

    output = (raw or b"").decode("utf-8", "replace").strip()
    result: dict[str, Any] = {
        "status": "success",
        "formal_statement": str(statement).strip(),
        "lean_output": output[:4000],
        "exit_code": returncode,
    }

    # `sorry` 会让 Lean 以 0 退出但发一条警告 —— 那是**没证**，不是证了。
    used_sorry = "declaration uses 'sorry'" in output or "sorry" in body
    if returncode == 0 and not used_sorry:
        result["verification"] = seal_verification(
            state, {"status": "verified", "method": "formal",
                    "formal_statement": str(statement).strip(),
                    "detail": "Lean 4 内核接受了这个证明"},
            tool="check_lean", lhs=str(statement).strip(), rhs="True",
            relation="lean")
        result["note"] = (
            "✅ Lean 内核接受。⚠️ **它证的是上面 formal_statement 那一行** —— "
            "核对它是不是你要证的那个命题；翻译错了这个绿灯毫无意义。")
    elif used_sorry:
        result["verified"] = False
        result["note"] = ("⚠️ 证明里有 `sorry` —— 那是占位符，等于**没证**。"
                          "Lean 以 0 退出只说明文件语法合法。")
    else:
        result["verified"] = False
        result["note"] = ("Lean 拒绝了这个证明。**这不等于命题是错的** —— "
                          "多半是证明写法或缺引理（本机无 mathlib）。"
                          "证否要靠 check_step / find_counterexample / interval_check。")
    return result


# ── 注册：工具面 + 验证器身份，两件事同生共死 ───────────────────────────────
#
# `register_verifier` 与 `register_tool` 必须写在一起：工具没注册模型就调不到，
# 验证器没注册它的章就不被门认。分开写迟早分叉 —— 2026-08-23 的 check_lean
# 就是只在门的白名单里留了个名字，工具从来没实现过。

register_verifier("check_step", ("symbolic", "numeric"),
                  "符号化简 + 高精度随机点抽查。唯一能在 CAS 默认假设下证成的工具。")
register_verifier("dimensional_check", ("symbolic",),
                  "量纲一致性。只证否：量纲错=式子错；量纲对不构成验证。")
register_verifier("limit_check", ("symbolic",),
                  "极限/特例回归。只证否：极限对不上是强信号。")
register_verifier("find_counterexample", ("numeric",),
                  "反例搜索。只证否：给出具体反例点。")

# ── 区间算术：需要 python-flint ─────────────────────────────────────────────
#
# 探不到 flint 就**不给 agent 这个工具**（那条规矩不变），但定义照样登记 ——
# 文档站读的是目录，于是站的内容不再取决于 build 那台机器装没装 flint。
# 见 core.tool_registry.register_capability_gated_tool。
_HAS_FLINT = _flint() is not None
if _HAS_FLINT:
    register_verifier("interval_check", ("interval",),
                      "Arb ball arithmetic。只证否，但证否是**严格**的："
                      "差值区间不含 0 = 整个区间上都不相等。")
register_capability_gated_tool(
    ToolDefinition(
        name="interval_check",
        description=(
            "⛔ **这是证否器，不是验证器** —— 用它来**排除**一条等式，"
            "不要用它来确认一条等式成立。\n"
            "区间算术（Arb ball arithmetic）在**整个区间**上算差值的外包围："
            "不含 0 → **严格证否**（这个区间里没有一个点相等，比随机抽样强"
            "一个量级）；含 0 → **什么都没证明**，既可能真恒等、也可能只是"
            "包裹效应太宽。\n"
            "**要证成一条恒等式，用 `check_step` 的符号路或 `check_lean`。**"
            "（2026-08-23 真跑实测：模型拿它去证 x²eˣ/(eˣ−1)² = (x/2)²/sinh²(x/2)，"
            "domain 跨 500 倍且分母趋零，区间炸到 [±6.31e+5] —— "
            "注定失败的用法，不是这个工具的活。）\n"
            "⚠️ **依赖问题**：同一个变量出现多次时，区间算术把每次出现当独立"
            "变量，区间被严重放大。实测 exp(x+y) vs exp(x)+exp(y)："
            "x,y∈[1,2] → [±49.2]（含 0，证否失败）；收窄到 [1,1.01] → "
            "[2±0.103]（严格证否）。**证否失败先怀疑区间太宽，分段收窄再逐段验。**\n"
            "domain 必填，形如 {'x': [0.5, 2.0]} —— 不给范围它就退化成点估值。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "lhs": {"type": "string"},
                "rhs": {"type": "string"},
                "domain": {
                    "type": "object",
                    "description": "每个自由变量的闭区间：{'x': [下界, 上界]}。",
                },
                "precision_bits": {"type": "integer", "default": 256},
            },
            "required": ["lhs", "rhs", "domain"],
        },
        risk_level="low",
    ),
    _interval_check,
    capability="python-flint",
    available=_HAS_FLINT,
)

# ── 形式化：需要 Lean 工具链 ────────────────────────────────────────────────
#
# 同上：探不到 lean 就不给 agent 这个工具，但定义照样登记给文档站。
_HAS_LEAN = _lean_binary() is not None
if _HAS_LEAN:
    register_verifier("check_lean", ("formal",),
                      "Lean 4 内核。唯一的严格证成手段；信任边界在**陈述**不在证明。")
register_capability_gated_tool(
    ToolDefinition(
        name="check_lean",
        description=(
            "把一步交给 Lean 4 内核裁决 —— 本平台唯一的**严格证成**手段。\n"
            "⚠️ **信任边界在陈述，不在证明**：Lean 证的是你写下的那个形式化"
            "命题，不是你以为的那个自然语言命题。翻译错了它照样全绿，"
            "而且绿得很有说服力 —— 所以返回的章里带 formal_statement 原文，"
            "**要核对的是那一行**。\n"
            "⚠️ 本机是裸 Lean 4，**没有 mathlib**：命题逻辑、基本算术、"
            "结构归纳、Nat/List 核心引理可用；实分析/测度论/群论的成套引理不在。"
            "验不了就报错换 check_step，别硬凑。\n"
            "证明里写 `sorry` 等于没证 —— 工具会识别出来并拒绝盖章。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "statement": {
                    "type": "string", "minLength": 1,
                    "description": "Lean 4 语法的命题，如 `∀ n : Nat, n + 0 = n`。",
                },
                "proof": {
                    "type": "string",
                    "description": "证明项或 tactic 块，如 `by simp` / `by induction n <;> simp`。",
                },
                "preamble": {
                    "type": "string",
                    "description": "可选：import / 辅助定义，放在 theorem 之前。",
                },
                "timeout_s": {"type": "integer", "default": 120},
            },
            "required": ["statement"],
        },
        risk_level="low",
    ),
    _check_lean,
    capability="Lean 4 工具链",
    available=_HAS_LEAN,
)
