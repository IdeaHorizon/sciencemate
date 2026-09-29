"""判分器：把「harness 比裸 agent 强吗」变成四个数字。

## 为什么判分器自己必须机械

judge 判错的方向是不对称的：**误判成"harness 赢"是灾难性的** —— 它会让
整套设计建立在一个假结论上，而且没人会去复核一个"符合预期"的结果。
所以：

1. **机械优先**：表达式比对走 sympy 规范化，不问长相；假设匹配走预置
   pattern（一条假设给多个别名），不靠 LLM 猜。
2. **LLM 只当检测器**：机械判不了的（语义等价但措辞不同）标成
   `needs_review`，**不自动计分** —— QC 那一轮的教训是"能失败但没有后果的
   判定不是保护，是噪音"，这里的形态是"能判错但没人复核的分数不是证据"。
3. **两臂同一套代码**：A0 与 A1 的 submission 是同一形状，judge 不知道
   自己在判哪一臂（结构上无法偏袒）。

## 四个指标

| 指标 | 判据 |
|---|---|
| `correctness` | final_expression 与 ground truth 规范化等价 |
| `false_detection` | 假命题任务：verdict 是不是 refuted |
| `assumption_recall` | required_assumptions 命中率 |
| `disclosure` | 有没有交代未验步骤 |
| `verifiability` | **验证声称有没有独立证据** —— harness 最核心的主张 |

## 关于 verifiability 这一维

首跑（4 题探路）暴露：裸模型被直接要求列假设时列得**比节点还多**
（8-10 条 vs 4 条），未验步骤也如实报了。若只看前四个指标，
harness 看起来没在付钱。

但两者的**可核验性根本不同**：

  A0  "正则系综密度算符…是基本结论，未在此重新推导"  ← 自述，无法定位、无法查
  A1  unverified_steps = ["S1","S8"]                ← 框架从账本现算，虚报不了

前者是免责声明，后者是可审计事实。**"报告不是事实"是整套设计的地基，
而我第一版 judge 没测它** —— 只判了 unverified_steps 是不是个 list。

`verifiability` 测的是：提交里声称"已验证"的步骤，有几条能反查到独立证据
（本 run 账本里的式子指纹）。

⚠️ **这不是"A0 不会验证"，是"A0 的声称无法被独立核验"** —— 结构性事实，
不是判分偏袒。给 A0 留了公平通道：它若给出可复现的验证方式（脚本/命令），
挂 needs_review 由人判，不直接记 0。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ── 表达式等价：规范化比对，不问长相 ────────────────────────────────────────

def expressions_equivalent(got: str, want: str,
                           definitions: dict | None = None,
                           aliases: dict | None = None) -> tuple[bool, str]:
    """两个表达式数学上等价吗。返回 (等价, 说明)。

    判据是 sympy 化简后差为 0 —— `k_B*x**2*exp(x)/(exp(x)-1)**2` 与
    `k_B*x**2/(4*sinh(x/2)**2)` 是同一个东西，字面比对会把对的判成错的。

    ## definitions / aliases：判分器不该不知道题目自己的符号约定

    2026-08-22 首轮全量实测，5 个"判错"里 4 个是判分器的锅：

        got `gamma`                     want `omega_0/Q`    Q ≡ ω₀/γ 是定义
        got `1/lam**2`                  want `1/lambda**2`  缩写
        got `sigma**2*Inverse(X.T*X)`   want `sigma2*(X_t_X)**(-1)`  矩阵记法
        got `kB*(1-(hbar*omega/(kB*T))**2/12)`  want `k_B*(1-x**2/12)`  展开了 x

    全部数学等价。**judge 把对的判成错的，方向上是在低估两臂** —— 比误判成
    "harness 赢"温和，但同样让结论不可信：我差点报告 A1 正确率 62%，
    而真实值接近全对。

    修法不是让 judge 更聪明地猜，是**让题目把自己的符号约定声明出来**
    （`ground_truth.symbol_definitions` / `accepted_aliases`），
    judge 比对前按它归一。每题的符号不同 —— 硬编码一张全局缩写表迟早误伤。
    """
    if not got or not want:
        return (False, "空表达式")
    try:
        import sympy as sp

        # **复用节点的解析器，不自己写一套。**
        # 两套解析必然各自演化，而且分叉时两边都不报错 —— judge 判"不等价"
        # 与工具判"failed"会给出互相矛盾的结论，没人知道该信哪个。
        # （2026-08-22 实测：judge 自己那套栽在 `gamma-1` 上 ——
        #  sympy 的 gamma 是 Gamma 函数，`FunctionClass - One` 直接崩，
        #  于是 `T*V**(gamma-1)` 与它自己都判"不等价"。）
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from shared.tools.library.derivation_check import _parse

        # 记法归一只做 judge 这一侧需要的：模型与 ground truth 可能用不同
        # 写法指同一个量（ħ/hbar、σ²/sigma2）。这是**判分的宽容度**，
        # 不是解析规则 —— 所以留在这里，不下沉到工具。
        def norm(text: str) -> str:
            text = str(text).strip().strip("$").replace("\\", "")
            for a, b in (("ħ", "hbar"), ("ω", "omega"), ("σ", "sigma"),
                         ("μ", "mu"), ("β", "beta"), ("γ", "gamma"),
                         ("λ", "lamda"), ("θ", "theta"), ("τ", "tau"),
                         # ⚠️ 不在这里做 k_B↔kB 的方向性替换 ——
                         # 题目的 accepted_aliases 也在做同一件事，两处方向
                         # 相反时表达式里会同时出现 kB 与 k_B，判定必然为假。
                         # 符号别名统一归 accepted_aliases 管（一个问题一个真相源）。
                         ("sigma^2", "sigma2"), ("sigma**2", "sigma2")):
                text = text.replace(a, b)
            return text

        lhs = _parse(sp, norm(got), {})
        rhs = _parse(sp, norm(want), {})

        # `Eq(T**2, 4*pi**2*a**3/(G*M))` —— 模型给完整等式是**好习惯**，
        # 不该因此被判错。取右边与裸表达式比；两边都是 Eq 就整体比。
        def _unwrap(e):
            return e.rhs if isinstance(e, sp.Equality) else e
        if isinstance(lhs, sp.Equality) != isinstance(rhs, sp.Equality):
            lhs, rhs = _unwrap(lhs), _unwrap(rhs)

        # 按题目声明的别名归一（lam → lambda 这类）
        for canonical, variants in (aliases or {}).items():
            target = sp.Symbol(str(canonical))
            for v in variants:
                sub = {sp.Symbol(str(v)): target}
                lhs, rhs = lhs.subs(sub), rhs.subs(sub)

        # 按题目声明的符号定义展开（x → hbar*omega/(k_B*T)）——
        # **两边都展开**，谁写缩写谁写展开式都不影响判定
        for name, definition in (definitions or {}).items():
            try:
                value = _parse(sp, norm(str(definition)), {})
            except Exception:
                continue
            sub = {sp.Symbol(str(name)): value}
            lhs, rhs = lhs.subs(sub), rhs.subs(sub)

        diff = sp.simplify(lhs - rhs)
        if diff == 0:
            return (True, "符号化简差为 0")
        # 差不为 0 不等于不等价（simplify 有极限）—— 补一次数值抽查
        free = sorted(diff.free_symbols, key=str)
        if free:
            import random
            rng = random.Random(20260822)
            for _ in range(12):
                point = {s: sp.Rational(rng.randint(11, 97), rng.randint(3, 29))
                         for s in free}
                try:
                    val = complex(sp.N(diff.subs(point), 30))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if abs(val) > 1e-18:
                    return (False, f"数值反例 {({str(k): str(v) for k, v in point.items()})}")
            return (True, "12 点数值一致（符号未化开）")
        return (False, f"化简后为 {diff}")
    except Exception as exc:
        # 判不了 ≠ 判错。矩阵记法（X.T / Inverse）、Python 关键字当符号名
        # 这类，超出简单比对的能力 —— **报 needs_review，不记 0 分**。
        # 与工具侧的 inconclusive 是同一条纪律：算不出来就说算不出来。
        return (None, f"判不了：{type(exc).__name__}: {exc}")


# ── 假设匹配：预置别名，机械命中 ────────────────────────────────────────────

def assumption_hit(spec: dict, haystack: str) -> bool:
    """这条假设在提交里出现了吗。

    一条假设给多个 pattern（"独立" / "independen" / "iid" / "Cov(Xi,Xj)=0"）——
    **不要求措辞一致，要求这件事被说出来了**。给一个 pattern 就会把
    "i.i.d." 判成没提独立性，那测的是文风不是纪律。
    """
    text = (haystack or "").lower()
    for pattern in spec.get("patterns") or []:
        try:
            if re.search(str(pattern).lower(), text):
                return True
        except re.error:
            if str(pattern).lower() in text:
                return True
    return False


@dataclass
class TaskScore:
    task_id: str
    kind: str
    arm: str
    correctness: bool | None = None
    correctness_note: str = ""
    false_detection: bool | None = None
    assumption_recall: float | None = None
    assumptions_hit: list[str] = field(default_factory=list)
    assumptions_missed: list[str] = field(default_factory=list)
    refutation_markers: float | None = None
    disclosure: bool | None = None
    verifiability: float | None = None
    verifiability_note: str = ""
    turns: int | None = None
    tool_calls: int | None = None
    needs_review: list[str] = field(default_factory=list)
    error: str = ""


def grade_one(task: dict, submission: dict, arm: str) -> TaskScore:
    """一题一臂的判分。submission 两臂同形，本函数不知道自己在判谁。"""
    gt = task.get("ground_truth") or {}
    score = TaskScore(task_id=task.get("id", "?"),
                      kind=task.get("kind", "normal"), arm=arm)

    if submission.get("error"):
        score.error = str(submission["error"])[:200]
        return score

    # 提交的全文：假设匹配在这上面找（假设可能写在 assumptions 里，
    # 也可能写在 findings / credibility 的散文里 —— 判的是"说没说出来"）
    haystack = json.dumps(submission, ensure_ascii=False, default=str)

    # ① 结果正确率（假命题任务没有"正确表达式"，跳过；若给了 correct_expression 则判它）
    want_expr = gt.get("final_expression") or gt.get("correct_expression")
    if want_expr:
        got_expr = submission.get("final_expression") or ""
        # 一道题可以有多个都对的答案形式（"只给修正项" vs "给完整式"）——
        # 任一命中即算对。这是**题目表述宽容度**，不是判据放水：
        # 每个 alternative 都得是数学上真的对，写在 ground_truth 里可审。
        candidates = [want_expr] + list(gt.get("accepted_alternatives") or [])
        ok, note = None, ""
        for candidate in candidates:
            ok, note = expressions_equivalent(
                got_expr, candidate,
                definitions=gt.get("symbol_definitions"),
                aliases=gt.get("accepted_aliases"))
            if ok is True:
                break
        score.correctness, score.correctness_note = ok, note
        if ok is None:
            score.needs_review.append(f"表达式比对失败（{note[:60]}）—— 人判是否等价")

    # ② 假命题识别率
    expected_verdict = str(gt.get("expected_verdict") or "").lower()
    if expected_verdict:
        got_verdict = str(submission.get("verdict") or "").lower()
        if task.get("kind") == "false_proposition":
            score.false_detection = ("refut" in got_verdict
                                     or "disprov" in got_verdict
                                     or "证伪" in got_verdict
                                     or "不成立" in got_verdict)
        elif expected_verdict == "derived":
            score.false_detection = ("deriv" in got_verdict
                                     or "支持" in got_verdict
                                     or "成立" in got_verdict
                                     or "supported" in got_verdict)

    # ③ 假设完备率
    required = gt.get("required_assumptions") or []
    if required:
        hit = [a["id"] for a in required if assumption_hit(a, haystack)]
        score.assumptions_hit = hit
        score.assumptions_missed = [a["id"] for a in required if a["id"] not in hit]
        score.assumption_recall = len(hit) / len(required)

    # ③b 假命题的证伪理由质量（说对了为什么错，不只是说了"错"）
    markers = gt.get("refutation_markers") or []
    if markers:
        hit_m = [m["id"] for m in markers if assumption_hit(m, haystack)]
        score.refutation_markers = len(hit_m) / len(markers)

    # ④ 未验披露率
    unverified = submission.get("unverified_steps")
    if unverified is not None:
        score.disclosure = isinstance(unverified, list)
        # 一步没验却报空表 = 没披露；全验了报空表 = 合法。
        # 机械分不开这两种，交给报告标注（A1 有账本可查，A0 没有）
        if isinstance(unverified, list) and not unverified:
            score.needs_review.append(
                "unverified_steps 为空：是全验过了还是没披露？A1 查账本，A0 需人判")

    # ⑤ 验证声称的可核验性
    evidence = submission.get("verification_evidence")
    claimed = submission.get("claimed_verified_steps")
    if evidence is not None and claimed is not None:
        backed = sum(1 for e in evidence if e.get("in_ledger"))
        score.verifiability = (backed / claimed) if claimed else None
        score.verifiability_note = f"{backed}/{claimed} 条声称有账本证据"
    else:
        # 没有结构化证据：不直接记 0 —— 若提交里给了可复现的验证方式，
        # 由人判（judge 不替人做这个判断）
        blob = haystack.lower()
        reproducible = any(k in blob for k in
                           ("sympy", "simplify(", "verify", "复现", "可复现",
                            "wolfram", "mathematica", "数值验证", "蒙特卡洛"))
        score.verifiability = 0.0
        score.verifiability_note = "无结构化验证证据（声称仅为自述）"
        if reproducible:
            score.needs_review.append(
                "提交里提到了可复现的验证方式但没有结构化证据 —— 人判是否算部分可核验")

    score.turns = submission.get("turns")
    score.tool_calls = submission.get("tool_calls")
    return score


def load_tasks(tasks_dir: Path) -> list[dict]:
    out = []
    for f in sorted(tasks_dir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["_file"] = f.name
        out.append(data)
    return out


def main() -> int:
    root = Path(__file__).parent
    tasks = {t["id"]: t for t in load_tasks(root / "tasks")}
    results_dir = root / "results"
    if not results_dir.exists():
        print(f"没有结果目录 {results_dir} —— 先跑 run_bench.py")
        return 1

    scores: list[TaskScore] = []
    for f in sorted(results_dir.glob("*.json")):
        # results/ 只放 run 的产物。judge 自己的输出写到别处 ——
        # 输入输出混在一个目录里，判分器早晚会把自己的输出当成一次 run 读进来
        # （实测：_scores.json 是个 list，直接把 judge 崩了）。
        sub = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(sub, dict):
            print(f"⚠️ 跳过非提交文件 {f.name}（results/ 只该放 run 产物）")
            continue
        task = tasks.get(sub.get("task_id"))
        if task is None:
            print(f"⚠️ {f.name}: 找不到任务 {sub.get('task_id')}")
            continue
        scores.append(grade_one(task, sub, sub.get("arm", "?")))

    if not scores:
        print("没有可判的结果")
        return 1

    # ── 汇总表 ──
    arms = sorted({s.arm for s in scores})
    print(f"\n{'指标':<22} " + " ".join(f"{a:>12}" for a in arms))
    print("-" * (22 + 13 * len(arms)))

    def rate(vals: list) -> str:
        vals = [v for v in vals if v is not None]
        if not vals:
            return "—"
        if isinstance(vals[0], bool):
            return f"{sum(vals)}/{len(vals)} ({sum(vals)/len(vals):.0%})"
        return f"{sum(vals)/len(vals):.0%} (n={len(vals)})"

    rows = [
        ("结果正确率", lambda s: s.correctness),
        ("假命题识别率", lambda s: s.false_detection if s.kind == "false_proposition" else None),
        ("正命题裁决正确", lambda s: s.false_detection if s.kind == "normal" else None),
        ("假设完备率", lambda s: s.assumption_recall),
        ("证伪理由完整度", lambda s: s.refutation_markers),
        ("未验披露", lambda s: s.disclosure),
        ("★验证可核验性", lambda s: s.verifiability),
    ]
    for label, getter in rows:
        cells = [rate([getter(s) for s in scores if s.arm == a]) for a in arms]
        print(f"{label:<22} " + " ".join(f"{c:>12}" for c in cells))

    for label, attr in (("平均轮次", "turns"), ("平均工具调用", "tool_calls")):
        cells = []
        for a in arms:
            vals = [getattr(s, attr) for s in scores if s.arm == a
                    and getattr(s, attr) is not None]
            cells.append(f"{sum(vals)/len(vals):.1f}" if vals else "—")
        print(f"{label:<22} " + " ".join(f"{c:>12}" for c in cells))

    # ── 逐题明细 ──
    print(f"\n{'任务':<28} {'臂':<5} {'正确':<6} {'裁决':<6} {'假设':<8} 缺的假设")
    print("-" * 92)
    for s in sorted(scores, key=lambda x: (x.task_id, x.arm)):
        if s.error:
            print(f"{s.task_id:<28} {s.arm:<5} ERROR: {s.error[:50]}")
            continue
        mark = lambda v: "✓" if v is True else ("✗" if v is False else "—")
        recall = f"{s.assumption_recall:.0%}" if s.assumption_recall is not None else "—"
        print(f"{s.task_id:<28} {s.arm:<5} {mark(s.correctness):<6} "
              f"{mark(s.false_detection):<6} {recall:<8} "
              f"{','.join(s.assumptions_missed) or '-'}")

    flagged = [(s.task_id, s.arm, n) for s in scores for n in s.needs_review]
    if flagged:
        print(f"\n⚠️ 需人工复核 {len(flagged)} 条（不自动计分）：")
        for tid, arm, note in flagged[:10]:
            print(f"   {tid} [{arm}] {note}")

    out = root / "scores.json"          # ← 不写进 results/（输入输出分离）
    out.write_text(json.dumps([s.__dict__ for s in scores],
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
