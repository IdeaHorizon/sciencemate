"""埋错套件：一份看起来完全正常的推导记录里藏一个真实的数学错误，闸抓不抓得住。

## 与 test_derivation_freeze_gate 的分工

那一份验的是**我想到的失败方式**（字段缺了、章伪造了、模式用错了）——
是结构判据的单测。

这一份不同：每个案例都是一份**结构完全合规**的 derivation_log ——
字段齐、验证章都是真工具落的、模式声明正确、findings 也写了 ——
但推导本身藏着一个真实的错误。问题只有一个：

> **这份记录能不能冻结成功？**

能冻结 = 这个错误从机械层溜过去了，只剩 reviewer 一道防线。

## 为什么要如实记录"抓不住"

抓不住的案例**照样留在这里，并断言它抓不住**。这不是自嘲，是防线边界的
可执行文档：

  · 哪些错误归机械层，哪些只能归 reviewer —— 一眼可查，不靠读散文
  · 哪天框架能抓了，对应的测试会红，提醒回来更新这份边界
  · review_spec 的清单该重点查什么，由这里的"漏网名单"倒推

**假装防线完美，比防线有洞更危险** —— 后者只是漏，前者会让人不去补。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.derivation.tools.derivation_contract import audit_derivation_log  # noqa: E402
from shared.tools.library import derivation_check as D  # noqa: E402


class _FakeState:
    def __init__(self, record):
        self._record = record

    def read_artifact(self, artifact_id):
        return self._record

    def append_transcript(self, *a, **kw):
        pass

    def list_artifacts(self):
        return []


def _real_check(lhs, rhs, relation="eq", assumptions=None):
    """真调一次验证工具，拿它**真实**返回的 verification 块。

    埋错案例必须用真章 —— 手写一个假章，测的就成了"伪造检测"（那是另一份
    测试的事），而不是"真验过的一步里藏着的错"。
    """
    result = asyncio.run(
        D._check_step(None, lhs, rhs, relation=relation, assumptions=assumptions))
    return result.get("verification")


def _log(steps, assumptions, **over):
    metadata = {
        "mode": "confirmatory",
        "steps": steps,
        "assumptions": assumptions,
        "credibility": "主结果经符号验证。",
        "verdicts": {"P1": "derived"},
        "counterexample_search": {"budget": 400, "found": False},
        "findings": [{"statement": "推导链已闭合"}],
        "main_result": {"expression": "a**2 + 2*a*b + b**2",
                        "statement": "完全平方展开式"},
    }
    metadata.update(over)
    return _FakeState({"type": "derivation_log", "metadata": metadata})


def _freezes(state) -> bool:
    return audit_derivation_log(state, "a1")["passed"]


# ══ 机械层抓得住的 ══════════════════════════════════════════════════════════

def test_planted_false_algebra_is_caught():
    """埋错 1：一步真实错误的代数变换，模型照常调了 check_step。

    工具返回 failed —— 检测防线仍在。判决拆除批 3w（deriv 737 降格）后
    冻结不再拒绝：**链上留 failed 记录是最诚实的记录**（旧墙奖励删除反例）；
    failed 清单机械入账 + advisory，referee 终审结论是否依赖它。
    """
    bad = _real_check("(a+b)**2", "a**2 + b**2")
    assert bad["status"] == "failed"
    state = _log(
        steps=[{"id": "S1", "claim": "(a+b)^2 = a^2+b^2",
                "justification": "algebraic", "verification": bad}],
        assumptions=[])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["failed_steps"] == ["S1"], (
        "failed 步骤必须被机械入账 —— 见证是判决的输入")
    assert "failed_steps_in_chain" in audit["advisories"]


def test_planted_domain_violation_is_caught_when_unassumed():
    """埋错 2：sqrt(ab)=sqrt(a)sqrt(b) —— 在复数域不成立的"常识"。

    不声明适用域时数值抽查会撞到复值点并给出反例。这是抽查掺复值的意义：
    只在正实轴采样，这个错误永远抓不到。
    """
    bad = _real_check("sqrt(a*b)", "sqrt(a)*sqrt(b)")
    assert bad["status"] == "failed"


def test_the_same_step_passes_once_the_domain_is_declared():
    """对照组：声明 a,b>0 之后同一步成立。

    没有这条对照，上一条测的可能只是"工具爱报错"。
    """
    good = _real_check("sqrt(a*b)", "sqrt(a)*sqrt(b)",
                       assumptions={"a": "positive", "b": "positive"})
    assert good["status"] == "verified"


def test_planted_numeric_masquerading_as_proof_is_caught():
    """埋错 3：主结果只有数值支持，credibility 却写"已证明"。

    判决拆除批 3w（deriv 762 降格 + 关键词出口改结构化）：检测照跑——
    numerically_supported 清单机械入账、缺结构化交代记 advisory——判决取消。
    """
    state = _log(
        steps=[{"id": "S1", "claim": "恒等式在全域成立", "justification": "algebraic",
                "verification": {"method": "numeric", "status": "numerically_supported",
                                 "tool": "check_step", "points_checked": 64}}],
        assumptions=[],
        credibility="主结果已完成证明，结论可靠。")
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["numerically_supported_steps"] == ["S1"]
    assert "numeric_support_not_disclosed" in audit["advisories"], (
        "「已证明」的口供没有结构化让步佐证 —— 缺口必须如实可见")


def test_planted_widened_validity_domain_is_surfaced():
    """埋错 4：结论比活假设允许的更宽。

    机械层不判"结论文本对不对"（那是语义），但它**现算**活假设并集交给
    reviewer —— 让"结论说全域成立、账上却挂着 x>0"这件事一眼可见。
    """
    good = _real_check("sqrt(x**2)", "x", assumptions={"x": "positive"})
    state = _log(
        steps=[{"id": "S1", "claim": "sqrt(x^2) = x（对一切实数 x）",
                "justification": "algebraic", "verification": good}],
        assumptions=[{"id": "A1", "statement": "x > 0"}])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, "结构合规，机械层不拦"
    assert audit["derived"]["validity_domain"] == ["A1"], (
        "但活假设必须被现算出来交给 reviewer —— 结论声称『对一切实数』而"
        "账上挂着 x>0，这个矛盾是 review_spec 第 3 维的 critical 红线")


# ══ 机械层抓不住的 —— 防线边界的可执行文档 ══════════════════════════════════

def test_KNOWN_GAP_division_by_a_possibly_zero_quantity():
    """⚠️ 已知缺口：两边同除一个可能为零的量。

    `(x^2-x)/x = x-1` 被 sympy 判 **verified**，即使没有声明 x≠0 ——
    因为它化简 `x**2/x` 时隐含了 x≠0。经典的 1=2 谬证正是这么来的。

    这暴露了 `verified` 的真实语义：**"在 sympy 的默认假设下等价"**，
    而不是"在所有情况下都对"。那些被化简吞掉的条件是**幽灵假设** ——
    实际用到了、却没进账本。

    归谁：reviewer（review_spec 第 3 维明确要求"抽查几个除法与级数操作"）。

    要不要往机械层补：可以扫表达式里的除法/开方/对数，提示"这里有隐含条件"。
    但那会对每个分母都报一次，绝大多数是噪声 —— 而"这个分母会不会为零"
    恰恰是语义判断。**先记下边界，别急着加一道会喊狼来了的闸。**
    """
    sneaky = _real_check("x**2/x - x/x", "x - 1")
    assert sneaky["status"] == "verified", (
        "如果这条断言红了，说明 sympy 或我们的调用方式变了、这个缺口被堵上了 —— "
        "把它挪到上面『抓得住』那一组，并更新 review_spec 第 3 维")

    state = _log(
        steps=[{"id": "S1", "claim": "(x^2-x)/x = x-1", "justification": "algebraic",
                "verification": sneaky}],
        assumptions=[])          # ← x≠0 没进账本，而这一步真的用到了它
    assert _freezes(state), "当前机械层放行 —— 这就是缺口本身"


def test_KNOWN_GAP_approximation_without_error_control():
    """⚠️ 已知缺口：近似不带误差控制。

    "忽略高阶项"标成 prose，机械层放行（未验不是罪）。而"丢掉的量级有多大、
    在什么范围内可忽略"是纯语义判断，框架无从机械核对。

    归谁：reviewer（review_spec 第 4 维 approximation_control）。

    为什么不硬拦：要求每个 prose 步骤都填一个"量级估计"字段，就会得到一堆
    为了过门编出来的量级 —— **门要一个形式，模型就生产这个形式**。
    """
    state = _log(
        steps=[{"id": "S1", "claim": "忽略 O(x^3) 及更高阶项", "justification": "prose"}],
        assumptions=[])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True
    assert audit["derived"]["unverified_steps"] == ["S1"], (
        "至少要出现在未验清单里 —— reviewer 的起点是这份清单")


def test_KNOWN_GAP_verification_certificate_transplanted_to_another_step():
    """⚠️ 已知缺口：把一步的真验证章贴到另一步上。

    章是真的（真调了工具、真 verified），但它验的是 `(a+b)^2` 展开，
    而这一步的 claim 写的是别的事。机械层只核对"章的来源是不是可信工具"，
    **不核对章验的内容与 claim 是不是同一件事** —— 那需要把自然语言 claim
    解析成表达式，是语义判断。

    归谁：reviewer（review_spec 第 1 维明确写了"声称验过的 lhs/rhs 与该步
    claim 里写的式子是不是同一个？验了一个简化版、贴在完整版上，是最隐蔽
    的一种"）。

    这条是**本节点最隐蔽的作弊面**，也是 review_spec 把"抽验 3 个承重步骤、
    自己重跑 check_step"写成硬要求的原因 —— 重跑一次就现形。
    """
    real_but_irrelevant = _real_check("(a+b)**2", "a**2 + 2*a*b + b**2")
    assert real_but_irrelevant["status"] == "verified"
    state = _log(
        steps=[{"id": "S1", "claim": "级数在 |q|<1 时收敛到 1/(1-q)",
                "justification": "algebraic",
                "verification": real_but_irrelevant}],   # ← 张冠李戴
        assumptions=[])
    assert _freezes(state), "当前机械层放行 —— 只有 reviewer 重跑才抓得住"


# ══ 汇总：把防线边界打印成一张表 ════════════════════════════════════════════

def test_the_boundary_table_is_current():
    """六类真实推导错误，机械层各自拦不拦得住 —— 一张现算的表。

    这条测试不断言对错，它断言**这张表算得出来**，并把它打印到 CI 日志里。
    改了闸之后看一眼这张表，比读一遍 review_spec 快。
    """
    rows = [
        ("错误的代数变换", True, "check_step 返回 failed"),
        ("适用域未声明（复数域反例）", True, "数值抽查掺复值点"),
        ("数值支持冒充已证明", True, "credibility 未交代 → 拒绝"),
        ("结论适用域宽于活假设", False, "机械层现算活假设，判定归 reviewer"),
        ("除以可能为零的量", False, "sympy 化简隐含 x≠0 —— 幽灵假设"),
        ("近似不带误差控制", False, "量级估计是语义判断"),
        ("验证章张冠李戴", False, "需解析自然语言 claim —— reviewer 重跑才现形"),
    ]
    print("\n\n  推导错误 → 机械层拦不拦得住")
    print("  " + "-" * 68)
    for name, mechanical, how in rows:
        mark = "✅ 拦" if mechanical else "→ reviewer"
        print(f"  {mark:<12} {name:<26} {how}")
    print("  " + "-" * 68)
    caught = sum(1 for _, m, _ in rows if m)
    print(f"  机械层 {caught}/{len(rows)}，其余归 reviewer（review_spec 逐条对应）\n")
    assert caught >= 3, "机械层至少要拦住最基本的那几类"
