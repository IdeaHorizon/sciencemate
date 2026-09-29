"""推导验证四件套的判定语义。

## 判据不是"能算"，是"四个值分得开"

最贵的错误方向是把 **inconclusive 讲成 failed**：sympy 没化出来只说明它没化
出来，据此告诉模型"这一步错了"，模型会去推翻一个正确的步骤，找一个不存在的
bug。所以本文件的重心是**阴性对照**：那些不该被判死的输入。

第二贵的是**静默给错答案**。开发时实测：`SI.get_dimensional_expr` 对
`kilogram + meter/second` 返回 `length/time`（只取第一项）—— 一个"检查过了"
的假象，而抓这类错正是量纲工具存在的理由。改用
`_collect_factor_and_dimension` 后才真的拦得住，那一条在下面钉死。
"""
from __future__ import annotations

import pytest

from shared.tools.library import derivation_check as D


def _status(result: dict) -> str:
    return (result.get("verification") or {}).get("status") or result.get("status")


# ── check_step：四值必须分得开 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_true_identity_is_verified():
    r = await D._check_step(None, "(a+b)**2", "a**2 + 2*a*b + b**2")
    assert _status(r) == "verified"
    assert r["verification"]["method"] == "symbolic"


@pytest.mark.asyncio
async def test_false_identity_fails_with_a_concrete_counterexample():
    r = await D._check_step(None, "(a+b)**2", "a**2 + b**2")
    assert _status(r) == "failed"
    # 反例必须是具体的点，不是一句"不成立" —— 模型要拿它去定位错在哪
    assert r["counterexample"]["point"]
    assert "lhs_value" in r["counterexample"]


@pytest.mark.asyncio
async def test_assumptions_change_the_verdict():
    """sqrt(x**2) == x 只在 x>=0 成立。

    这一对是整个假设账本机制的缩影：同一个式子，声明与不声明适用域，
    判定必须不同 —— 否则"适用域"在系统里就不是一个真实存在的东西。
    """
    without = await D._check_step(None, "sqrt(x**2)", "x")
    assert _status(without) == "failed"

    with_positive = await D._check_step(
        None, "sqrt(x**2)", "x", assumptions={"x": "positive"})
    assert _status(with_positive) == "verified"
    assert with_positive["verification"]["assumptions"] == {"x": "positive"}


@pytest.mark.asyncio
async def test_inequality_over_reals_is_not_falsified_by_complex_sampling():
    """x**2 >= 0 在实数上恒真。

    数值抽查会掺复值点（实轴上成立、复平面上不成立的等式确实存在），
    但不等式在复数上无意义 —— 拿复值点否定一个实数不等式，
    是闸自己制造的假反例。
    """
    r = await D._check_step(None, "x**2", "0", relation="ge",
                            assumptions={"x": "real"})
    assert _status(r) != "failed"


@pytest.mark.asyncio
async def test_unparseable_input_is_an_error_not_a_verdict():
    """解析不了 ≠ 这一步是错的。报错要带语法指导（契约必须送到调用方）。"""
    r = await D._check_step(None, r"\frac{1}{2}", "0.5")
    assert r["status"] == "error"
    assert "语法" in r["error"]


@pytest.mark.asyncio
async def test_bad_relation_lists_the_legal_values(tmp_path):
    """契约归 schema：relation 的 enum 在 parameters_schema，派发口核一次并把
    合法值列进报错 —— 工具体内不再手写，所以走 execute。"""
    from core.bootstrap import bootstrap
    from core.state import State
    from core.tool_registry import execute

    bootstrap()
    st = State.new(node_type="derivation", base_dir=tmp_path)
    r = await execute("check_step", st, lhs="x", rhs="x", relation="approximately")
    assert r["status"] == "error"
    assert r["parameter_violations"]
    for legal in ("eq", "lt", "le", "gt", "ge"):
        assert legal in r["error"]


@pytest.mark.asyncio
async def test_numerically_supported_is_never_reported_as_verified():
    """数值支持不能冒充符号确证 —— 这个区分是「数值证据关不掉演绎命题」的载体。"""
    r = await D._check_step(None, "sin(x)**2 + cos(x)**2", "1")
    assert _status(r) in ("verified", "numerically_supported")
    if _status(r) == "numerically_supported":
        assert r["verification"]["method"] == "numeric"
        assert r["verification"]["points_checked"] > 0


# ── find_counterexample：找不到是结论，没找不是 ─────────────────────────────

@pytest.mark.asyncio
async def test_counterexample_search_finds_a_real_one():
    r = await D._find_counterexample(None, "exp(x+y)", "exp(x)+exp(y)", budget=64)
    assert r["found"] is True
    assert r["counterexample"]


@pytest.mark.asyncio
async def test_counterexample_search_records_the_budget_when_it_finds_nothing():
    """没找到时必须留下**搜索记录** —— 否则"找过了没有"与"没找过"无法区分。"""
    r = await D._find_counterexample(None, "(a+b)**2", "a**2+2*a*b+b**2", budget=64)
    assert r["found"] is False
    block = r.get("counterexample_search") or {}
    if block:                      # 符号确证时会走 early return，那条也合法
        assert block["budget"] > 0
        assert block["found"] is False


# ── dimensional_check：不一致必须抓得住 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_inconsistent_dimensions_are_caught():
    """开发时的真 bug：get_dimensional_expr 静默返回第一项的量纲。

    这条钉住的不是"能算量纲"，是"**加了不同量纲的东西会被抓**"。
    判据反过来（返回 consistent=True）时，量纲工具就退化成一句装饰。
    """
    r = await D._dimensional_check(
        None, "m + v", units={"m": "kilogram", "v": "meter/second"})
    assert r["consistent"] is False
    assert r["checks"]["dimensional"]["consistent"] is False


@pytest.mark.asyncio
async def test_same_dimension_written_differently_still_matches():
    """joule vs kilogram*meter**2/second**2 —— 判据是基本量纲幂次，不是长相。"""
    r = await D._dimensional_check(
        None, "m*v**2/2", units={"m": "kilogram", "v": "meter/second"},
        expected="joule")
    assert r["consistent"] is True
    assert r["matches_expected"] is True


@pytest.mark.asyncio
async def test_wrong_dimension_does_not_match():
    r = await D._dimensional_check(
        None, "m*v", units={"m": "kilogram", "v": "meter/second"},
        expected="joule")
    assert r["matches_expected"] is False


@pytest.mark.asyncio
async def test_unknown_unit_lists_how_to_write_it():
    r = await D._dimensional_check(None, "m", units={"m": "furlongs_per_fortnight"})
    assert r["status"] == "error"
    assert "meter" in r["error"]          # 报错要给合法写法，不只说"不认识"


# ── limit_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_matches_known_answer():
    r = await D._limit_check(None, "sin(x)/x", "x", "0", expected="1")
    assert r["matches_expected"] is True


@pytest.mark.asyncio
async def test_relativistic_kinetic_energy_reduces_at_low_speed():
    """v→0 时相对论动能 - 静能 → 0：物理推导最有力的一类自查。"""
    r = await D._limit_check(
        None, "m*c**2/sqrt(1-v**2/c**2) - m*c**2", "v", "0", expected="0")
    assert r["matches_expected"] is True


@pytest.mark.asyncio
async def test_limit_mismatch_is_flagged_loudly():
    r = await D._limit_check(None, "sin(x)/x", "x", "0", expected="0")
    assert r["matches_expected"] is False
    assert "⛔" in (r.get("note") or "")


# ── 工具面所有权：共享，不独占 ──────────────────────────────────────────────

def test_verification_tools_are_shared_across_nodes():
    """工具共享、证据所有权独占 —— 四件工具不许绑死在 derivation 上。

    observation 合并效应量、experiment 核对理论预测、hypothesis 验量纲，
    都该顺手能用，不必为此起一个 producing run。
    """
    from core.bootstrap import bootstrap
    from core.tool_registry import get_tool

    bootstrap()
    for name in ("check_step", "find_counterexample",
                 "dimensional_check", "limit_check"):
        tool = get_tool(name)
        assert tool is not None, f"{name} 没注册"
        assert tool.allowed_node_types is None, (
            f"{name} 绑死在 {tool.allowed_node_types} 上了 —— "
            "验证工具是共享面，独占的只有 derivation_log 的所有权")


# ── 假反例：真跑（2026-08-22 e2e）抓到的一类，单测全想不到 ──────────────────
#
# 单测里我用的全是初等表达式（多项式、三角、指数），从没测过 Sum/Integral
# 这类**未求值符号对象**，也没测过 integer 约束。真跑第一趟就撞上了：
#
#   模型验有限几何级数求和 Σ_{n=0}^{N} e^{-x(n+1/2)} = e^{-x/2}(1-e^{-(N+1)x})/(1-e^{-x})
#   —— 教科书正确 —— 工具判了 failed。
#
# 两个根因叠加：① `integer` 只进了 sympy 的符号假设、没进采样器，N 被发了
# 1.102；② `Sum(...)` 代入具体值后仍是未求值对象，complex() 给不出有意义的数。
#
# **假反例是最坏的一类错误**：整套设计反复告诉模型"failed 是强结论"，
# 于是它会去推翻一个正确的步骤、找一个不存在的 bug。
# （那一趟模型自己识破了是工具的锅并绕了道 —— 但它本不该需要绕道。）

@pytest.mark.asyncio
async def test_the_e2e_false_counterexample_stays_fixed():
    """★ 回归：e2e 现场那条被误判的式子。"""
    r = await D._check_step(
        None,
        "Sum(exp(-x*(n+Rational(1,2))), (n, 0, N))",
        "exp(-x/2)*(1 - exp(-(N+1)*x))/(1 - exp(-x))",
        assumptions={"x": "positive", "N": "integer"}, symbols=["x", "N"])
    assert _status(r) != "failed", (
        "有限几何级数求和是教科书正确的公式 —— 判它 failed 是假反例")


@pytest.mark.asyncio
async def test_integer_assumption_reaches_the_sampler():
    """`integer` 不能只进 sympy 的符号假设，必须也进采样器。

    整数符号常常是求和/乘积的上界或幂次 —— 给它浮点数，表达式本身就失去意义。
    """
    r = await D._check_step(None, "Sum(n, (n, 0, N))", "N*(N+1)/2",
                            assumptions={"N": "integer"}, symbols=["N"])
    assert _status(r) in ("verified", "numerically_supported")


@pytest.mark.asyncio
async def test_unevaluated_objects_are_evaluated_before_comparing():
    """Sum / Integral 代入后要 doit，否则比较的是两个符号对象。"""
    r = await D._check_step(None, "Integral(x**2, (x, 0, a))", "a**3/3",
                            assumptions={"a": "positive"}, symbols=["a"])
    assert _status(r) in ("verified", "numerically_supported")


@pytest.mark.asyncio
async def test_a_genuinely_wrong_summation_is_still_caught():
    """★ 阴性对照：修完之后闸不能修瞎了。

    没有这一条，上面三条都可以靠"永远不报 failed"来通过。
    """
    r = await D._check_step(None, "Sum(n, (n, 0, N))", "N**2",
                            assumptions={"N": "integer"}, symbols=["N"])
    assert _status(r) == "failed"


@pytest.mark.asyncio
async def test_declaring_real_keeps_complex_points_out():
    """声明了 real 就不该拿复值点去否定它 —— 同样是自造假反例。"""
    r = await D._check_step(None, "sqrt(x**2)", "Abs(x)",
                            assumptions={"x": "real"}, symbols=["x"])
    assert _status(r) != "failed"


@pytest.mark.asyncio
async def test_infinite_series_that_sympy_cannot_close_is_inconclusive_not_failed():
    """★ 最贵的那条：sympy 稳定地算错，双精度自校验也拦不住。

    e2e 决定性证据（x≈1.549e-7，Σ_{n≥0} e^{-xn} vs 1/(1-e^{-x})，精确恒等式）：

        mpmath 独立求和   6454064.992571167732605150686049642367222779
        闭式             6454064.992571167732605150686049642367222779  ← 一致
        sympy Sum.doit()  6454064.993315014347013684162499757906731235  ← 错

    级数在这个 x 下要 ~6.5e6 项才收敛，sympy 的数值求和截断了 ——
    而且它在 50 位和 100 位下**给出同样错误的结果**，所以靠"两种精度对一下"
    的自校验发现不了。

    判据只能落在表达式层面：**符号 doit() 求不出闭式 → 数值路径整个不可信**。
    """
    for lhs, rhs in [
        ("Sum(exp(-x*n), (n, 0, oo))*exp(-x/2)", "exp(-x/2)/(1 - exp(-x))"),
        ("summation(exp(-x*n), (n, 0, oo))", "1/(1 - exp(-x))"),
    ]:
        r = await D._check_step(None, lhs, rhs, assumptions={"x": "positive"})
        assert _status(r) == "inconclusive", (
            f"{lhs} 是精确恒等式；sympy 算不动它就该说算不动，不能报 failed")


@pytest.mark.asyncio
async def test_the_trust_gate_does_not_silence_resolvable_sums():
    """阴性对照：这道闸只挡真正求不出闭式的。

    有限求和、定积分、等差求和的 doit() 都求得出（哪怕结果是 Piecewise），
    它们的数值抽查照常进行 —— 否则这道闸就是把整类表达式变成永远的
    inconclusive，那是另一种失效。
    """
    ok = await D._check_step(None, "Sum(n, (n, 0, N))", "N*(N+1)/2",
                             assumptions={"N": "integer"})
    assert _status(ok) in ("verified", "numerically_supported")

    wrong = await D._check_step(None, "Sum(n, (n, 0, N))", "N**2",
                                assumptions={"N": "integer"})
    assert _status(wrong) == "failed", "可求闭式的错误求和仍必须被抓住"


# ── 逻辑/元数学：CAS 判不了的东西，必须诚实说判不了 ─────────────────────────
#
# 2026-08-22 wangd 问「能不能从基础逻辑推出哥德尔不完备定理」。拿它证明里的
# 真实步骤打了一遍工具，抓到两层缺陷：
#
# 1. **解析层**：`implicit_multiplication_application`（为了让 2x → 2*x）
#    把不认识的多字母标识符拆成单字母乘积 ——
#        Prov(gn(A))  →  P*r*o*v*(g*n*A)
#    于是 `equals()` 返回 False，这一步被判 **failed（强结论）**，
#    而它是一条正确的元数学命题。影响面远不止哥德尔：任何模型自定义的
#    多字母函数名（Psi / Ham / Prob）以前都会被静默拆开。
#
# 2. **判定层**：含未定义函数/谓词的表达式，数值代入本就无意义 ——
#    随机数字喂给一个语义未知的符号，两边"不相等"是必然的，不是反例。
#
# 这类命题（逻辑推理、元数学）**超出 CAS 能机械判定的范围**。
# 正确的行为是判 inconclusive 并在 note 里指出正当出口（cited_theorem /
# prose），而不是报 failed 让模型去修一个没错的东西。

@pytest.mark.asyncio
async def test_godel_style_metamathematics_is_inconclusive_not_failed():
    """★ 哥德尔证明里的真实步骤：判不了就说判不了。"""
    for lhs, rhs in [
        ("Prov(gn(A))", "Provable(A)"),
        ("G", "Not(Prov(godel_number(G)))"),
        ("Consistent(T)", "Not(Provable(Con(T)))"),
    ]:
        r = await D._check_step(None, lhs, rhs)
        assert _status(r) == "inconclusive", (
            f"{lhs} = {rhs} 是元数学命题，CAS 判不了 —— "
            "判 failed 会让模型去推翻一条正确的命题")
        assert "cited_theorem" in (r.get("note") or ""), "报错要指出正当出口"


@pytest.mark.asyncio
async def test_multi_letter_function_names_are_not_split_into_products():
    """★ 解析层：`Psi(x)` 不能变成 P*s*i*x。"""
    r = await D._check_step(None, "Psi(x)+Psi(y)", "Psi(y)+Psi(x)")
    assert _status(r) == "verified", "自定义多字母函数名被拆了"


@pytest.mark.asyncio
async def test_symbolic_path_still_works_for_undefined_functions():
    """阴性对照：含未定义函数**不等于**不能验。

    乘积法则 / 链式法则都含一般函数 f、g，sympy 符号上验得出 verified。
    一刀切拦掉数值路径可以，拦掉符号路径就会把整类「关于一般函数的恒等式」
    变成永远的 inconclusive。
    """
    r = await D._check_step(
        None, "diff(f(x)*g(x),x)", "f(x)*diff(g(x),x)+g(x)*diff(f(x),x)")
    assert _status(r) == "verified"


@pytest.mark.asyncio
async def test_builtin_functions_and_implicit_multiplication_still_work():
    """阴性对照：修解析层不能碰坏 sympy 内建与隐式乘法。"""
    assert _status(await D._check_step(None, "sin(x)**2+cos(x)**2", "1")) != "failed"
    assert _status(await D._check_step(None, "2x+3x", "5x")) == "verified"


@pytest.mark.asyncio
async def test_numeric_agreement_is_expressible_without_a_new_relation():
    """闭式与已知值的对账：现有 relation 够用，不需要加 approx。

    2026-08-22 准备 Ising 课题时抓到：`2/log(1+sqrt(2))` 与 `2.269185314`
    判 eq 得到 failed（差 2e-10，符号上确实不等）。技术上没错，但模型想问的
    是"误差内一致吗"，被答成"这一步不成立"。

    修法是**把契约送到调用方**（工具 description + 节点 prompt 写清写法），
    不是加一个 approx relation —— 现有机制表达得了，就不加新概念。

    这个形式还更好：**容差必须显式写出来**。多少算一致取决于测量精度与
    截断阶数，是科学判断（归模型）；数值比较是机械的（归框架）。
    approx 的隐式默认容差会把这个判断藏起来。
    """
    ok = await D._check_step(
        None, "Abs(2/log(1+sqrt(2)) - 2.269185314213022)", "1e-9", relation="lt")
    assert _status(ok) == "verified"

    bad = await D._check_step(
        None, "Abs(2/log(1+sqrt(2)) - 2.5)", "1e-9", relation="lt")
    assert _status(bad) == "failed", "与错值比对必须判否，否则这个写法就成了万能通行证"


@pytest.mark.asyncio
async def test_the_ising_self_duality_step_is_verifiable():
    """T 线前置：Onsager T_c 推导链的承重步骤机械验得了。"""
    r = await D._check_step(None, "sinh(2*log(1+sqrt(2))/2)", "1")
    assert _status(r) == "verified"


# ── 符号名撞上 sympy 内建函数（benchmark 真跑抓到，影响面极大）─────────────
#
# `gamma` 在 sympy 里是 Gamma 函数。于是：
#
#     check_step("T*V**(gamma-1)", "T*V**(gamma-1)")
#       → 表达式解析失败：unsupported operand type(s) for -:
#         'FunctionClass' and 'One'
#
# **连它和自己都判不了。** γ（比热比/阻尼系数/洛伦兹因子）、β（1/kT、回归
# 系数）、ζ、Γ 全中招 —— 而这些恰恰是热力学、统计物理、统计学最常用的符号。
# 单测里我用的是 a/b/x/y，一个都碰不到；benchmark 第一批真题就撞上了。
#
# 判据取数学记法的通例：**带括号才是函数，不带括号就是符号**，
# 只有数学常数（pi/E/I/oo）保留 sympy 原义。

@pytest.mark.asyncio
async def test_greek_letter_names_that_shadow_sympy_functions_are_symbols():
    """★ gamma / beta / zeta 当变量名用 —— 必须能验。"""
    assert _status(await D._check_step(
        None, "T*V**(gamma-1)", "T*V**gamma/V")) == "verified"
    assert _status(await D._check_step(
        None, "exp(-beta*E)", "1/exp(beta*E)")) == "verified"
    assert _status(await D._check_step(None, "zeta*x", "x*zeta")) == "verified"


@pytest.mark.asyncio
async def test_the_same_names_are_still_functions_when_called():
    """`gamma(x)` 带括号 → 仍是 Gamma 函数，递推关系验得出来。"""
    assert _status(await D._check_step(
        None, "gamma(x+1)", "x*gamma(x)")) != "failed"


@pytest.mark.asyncio
async def test_math_constants_keep_their_meaning():
    """pi / E / oo 不能被当成自由符号 —— 那会让 sin(pi)=0 判不出来。"""
    assert _status(await D._check_step(None, "sin(pi)", "0")) != "failed"
    assert _status(await D._check_step(None, "log(E)", "1")) != "failed"


@pytest.mark.asyncio
async def test_a_genuinely_wrong_identity_with_greek_symbols_still_fails():
    """阴性对照：修完不能把闸修瞎。"""
    assert _status(await D._check_step(
        None, "T*V**(gamma-1)", "T*V**gamma")) == "failed"
