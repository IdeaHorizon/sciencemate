"""derivation_log 的冻结门：演绎纪律的机械层。

## 这道门查什么、不查什么

查的全是**纪律的痕迹在不在**，不是推导做得好不好 —— 后者是语义判断，归
reviewer。这条边界不许被"多加几个闸更严格"的直觉侵蚀：闸一旦开始查内容质量，
就会重演"门要一个形式、模型就生产这个形式"。

## 演绎模态的原罪

experiment 防"伪造执行"、observation 防"摘樱桃"，两者的闸对推导一条也拦不住：
一条 50 步的链每步都真跑过、引的定理也都真实存在，照样能在第 23 步偷换一个
假设。推导的原罪是**无效步骤伪装成有效** —— 跳步、偷换假设、适用域静默扩大、
近似不带误差控制。所以这道门的六条判据全都围着它转：

1. **验证章只认工具落的**（`verification.tool` 必须是注册在案的验证工具）。
   模型自己写 `status: verified` 进不来 —— 报告不是事实。
2. **每一步要么有章，要么显式挂未验**。未验不是罪，装作验过才是。
3. **结论的适用域由框架现算** = 沿链累积的活假设并集。模型手写一份必然漂移，
   而"适用域静默扩大"正是最难人眼发现的一类错。
4. **数值支持关不掉演绎命题**：主结果只有 numerically_supported 时，
   credibility 必须如实标明。10^6 个点上全对不是证明 —— 历史上死过一打这样
   的"定理"。
5. **恒等式/不等式类结论要有反例搜索记录**：找不到反例是结论，没找过不是。
   两者在结果上都表现为"没有反例"，只有过程记录能区分。
6. **exploratory 不许勾账**：不能用生成猜想的那批数值实验去确证同一个猜想。
   与 observation 的同条判据同源 —— anti-HARKing 在演绎侧的形态。
"""
from __future__ import annotations

from typing import Any

# 空判定与路径取值只有一份实现 —— 见 shared/lib/metadata_contract 的模块说明：
# 同一个问题曾有三份实现、两个互相矛盾的答案（`0` 到底算不算填了）。
from shared.lib.metadata_contract import dig as _dig, is_filled as _is_filled

_LOG_TYPE = "derivation_log"

EXPLORATORY = "exploratory"
CONFIRMATORY = "confirmatory"
AUDIT = "audit"
KNOWN_MODES = (EXPLORATORY, CONFIRMATORY, AUDIT)

#: audit 模式对**这份被审的推导**的裁决。与 `verdicts`（对命题的裁决）正交 ——
#: 一份有漏洞的证明完全可能碰巧证了个真命题，这两件事必须分开记。
AUDIT_VERDICTS = ("sound", "flawed", "unverifiable")

#: 验证状态。四值语义见 shared/tools/library/derivation_check.py。
KNOWN_STATUSES = ("verified", "numerically_supported", "inconclusive",
                  "failed", "unverified")


# ── 合法验证来源：**现算，不是手写名单** ────────────────────────────────────
#
# 2026-08-23 之前这两项是硬编码元组，里面躺着三个空位子：`check_lean`
# （工具从未实现）、`interval` 与 `formal`（没有任何工具产出这两种 method）。
#
# 后果不是"少了个功能"，是**门上多了个后门**：`_audit_steps` 先看
# `verification.tool` 在不在白名单、再拿 probe 去账本反查。模型手写
# `{tool: "check_lean", status: "verified", probe: "随便"}`，第一段**会放行**；
# 唯一拦住它的是账本反查，而账本读不到时那段刻意跳过（取证失灵 ≠ 伪造）。
# 于是名单里一个永远不会写账本的名字，成了只在降级路径上生效的近路。
#
# 「护栏要扫盘不要写名单」的反向形态：手写名单的经典毛病是新东西默认漏过，
# 这里是**名单里的东西根本不存在**。病根同一个 —— 名单是人手维护的抄件，
# 而它描述的事实（哪些工具真的会验证并落账）在别处演化。
#
# 判据现在是：有资格署名 ⟺ 这个工具真的注册了自己会往账本写。
# 能力不在（没装 flint / 没有 Lean）→ 工具不注册 → 名字不在白名单 →
# 模型手写它当场被拒。见 shared/lib/verifier_registry。

def trusted_verifiers() -> tuple[str, ...]:
    from shared.lib.verifier_registry import trusted_verifiers as _live

    return _live()


def known_methods() -> tuple[str, ...]:
    from shared.lib.verifier_registry import known_methods as _live

    return _live()
#: 不需要机械验证章的步骤类型：引用外部定理、纯文字论证。它们各有别的纪律
#: （cited_theorem 要外部锚点），但不该被要求给出 CAS 等价证明。
JUSTIFICATIONS_WITHOUT_MECHANICAL_CHECK = ("cited_theorem", "prose", "definition")

#: 结构纪律：这份记录**长什么样** —— 写入门与冻结门都查（必须有内容）。
STRUCTURAL_REQUIRED_PATHS: tuple[str, ...] = (
    "mode",
    "steps",
)
#: 必须**在场**、但可以为空的字段。
#
# `assumptions: []` 是合法取值：一个纯代数恒等式的推导确实不需要任何额外假设。
# 把空列表当"没填"，就是逼着模型为了过门编一条假设出来 —— 闸反过来制造它要防
# 的行为（同 observation 的 `n_excluded: 0`：真的什么都没排除时那是合法的 0）。
#
# 但字段本身必须在场：key 都不写，说明这一趟压根没做假设账这件事。
PRESENT_BUT_MAY_BE_EMPTY: tuple[str, ...] = (
    "assumptions",
    "findings",      # 这一趟确实可以没有值得单独记的发现
)
#: 只有 confirmatory 欠的：对照冻结的命题 + 主结果的验证水平必须交代。
#
# `main_result` 是**这趟推导推出来的那个东西** —— 显式声明，不让下游去猜。
#
# 2026-08-22 benchmark 实测：判分器从"末步 claim 的等号右边"提取主结果，
# 拿到的是 `(x/2)²/sinh²(x/2)（附加，独立路线；非承重）` —— 模型在链尾加了
# 一条附加验证路线，**推导链的末步不一定是主结果**。于是 correctness 判成
# 0%，差点得出"harness 让模型算得更差"的假结论。
#
# 三个下游都要它，不是为 benchmark 打的补丁：
#   · Analysis 拿理论预测去对账实验测量（T 线的闭环靠它）
#   · writing 引用主结果进论文
#   · 判分 / 审计要比对最终表达式
# **别让下游猜，让上游声明。**
CONFIRMATORY_REQUIRED_PATHS: tuple[str, ...] = (
    "credibility",
    "verdicts",
    "counterexample_search",
    "main_result",
)
#: 只有 audit 欠的。
#
# `audit_target` 必须带内容指纹 —— 审计最基本的作弊方式是**审了一份、
# 报告贴的是另一份**（postprocess 的 figure 源绑定 hash 同款原罪）。
# 其余与 confirmatory 同：审计结论也是要被引用的证据，欠 credibility 与裁决。
AUDIT_REQUIRED_PATHS: tuple[str, ...] = (
    "audit_target",
    "audit_verdict",
    "credibility",
)
#: `main_result` 里**必须有内容**的键。
#
# 只要 statement：不是所有结论都是一个表达式 —— 存在性命题、终止性证明、
# "该命题不成立"都没有可写的 expression。要求它非空，就是逼着模型
# 为了过门编一个式子（门要一个形式，模型就生产这个形式）。
#
# `expression` 允许为空，但**非空时必须机器可解析** —— 那是下游拿去比对
# 与对账的东西，混进中文注释就等于没给。
MAIN_RESULT_REQUIRED_KEYS: tuple[str, ...] = ("statement",)


def _audit_steps(steps: Any, ledger: dict[str, dict] | None = None,
                 ledger_readable: bool = False,
                 ) -> tuple[list[str], list[str], list[str], list[str]]:
    """推导链的结构纪律。

    返回 (problems, advisories, unverified_step_ids, failed_step_ids)。

    `unverified` **不是错误** —— 它是这条链的诚实状态，现算出来给 reviewer 和
    credibility 用。problems 仍拦（类型契约与**伪造的验证章**）；
    advisories 不拦（判决拆除批 3w：203 空链 / 216 缺 claim / 220 缺
    justification / 248 无验证块——如实记录随产物走）。
    「justification 剥套话后必须剩实质」反显然闸已删（224 档一：对散文做
    关键词黑名单，写句假「by Lemma 3」即过——文案并入 save gate 提示语）。
    """
    problems: list[str] = []
    advisories: list[str] = []
    unverified: list[str] = []
    failed: list[str] = []

    if steps is None:
        # 缺失由 audit_record_shape 的 derivation_record advisory 承载，不重复。
        return ([], [], [], [])
    if not isinstance(steps, list):
        return (["steps 必须是列表（推导链按顺序排）"], [], [], [])
    if not steps:
        advisories.append("steps 为空 —— 这份推导记录没有可审的推导链")
        return (problems, advisories, unverified, failed)

    seen_ids: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"steps[{index}] 必须是对象")
            continue
        step_id = str(step.get("id") or "").strip() or f"steps[{index}]"
        if step_id in seen_ids:
            problems.append(f"{step_id} 的 id 重复 —— 前提引用会指向哪一个？")
        seen_ids.add(step_id)

        if not _is_filled(step.get("claim")):
            advisories.append(f"{step_id} 缺 claim（这一步到底断言了什么）")

        justification = str(step.get("justification") or "").strip().lower()
        if not justification:
            advisories.append(f"{step_id} 缺 justification（凭什么走这一步）")

        # 前提必须指向已经出现过的步骤或假设 —— 环形依赖与前向引用都是
        # "看起来有推导链、实际没有"的典型形态。
        premises = step.get("premises")
        if premises is not None and not isinstance(premises, list):
            problems.append(f"{step_id} 的 premises 必须是列表")
        elif isinstance(premises, list):
            for ref in premises:
                ref_text = str(ref).strip()
                if ref_text and ref_text not in seen_ids and not ref_text.startswith("A"):
                    problems.append(
                        f"{step_id} 引用了 {ref_text}，但它不在前面的步骤里 "
                        f"（假设请用 A 开头的 id，并写进 assumptions）")

        verification = step.get("verification")
        if not isinstance(verification, dict) or not verification:
            # 判决拆除 248 降格：没有验证块的步骤按未验如实入账（unverified
            # 现算随产物走），不再拒绝——未验不是罪，装作验过才是。
            unverified.append(step_id)
            if justification not in JUSTIFICATIONS_WITHOUT_MECHANICAL_CHECK:
                advisories.append(
                    f"{step_id} 没有 verification 块也未标免验类别——已按未验"
                    f"（unverified）入账；可调 check_step 验一遍贴回整块，或把 "
                    f"justification 标成 "
                    f"{'/'.join(JUSTIFICATIONS_WITHOUT_MECHANICAL_CHECK)} 之一")
            continue

        status = str(verification.get("status") or "").strip().lower()
        method = str(verification.get("method") or "").strip().lower()
        tool = str(verification.get("tool") or "").strip()

        methods = known_methods()
        verifiers = trusted_verifiers()
        if status not in KNOWN_STATUSES:
            problems.append(
                f"{step_id} 的 verification.status={status or '缺失'} 不合法。"
                f"合法值：{'/'.join(KNOWN_STATUSES)}")
        if method not in methods:
            problems.append(
                f"{step_id} 的 verification.method={method or '缺失'} 不合法。"
                f"本环境可用的：{'/'.join(methods)}"
                + ("（method 跟着工具走 —— 环境里没有的验证后端不会出现在这个"
                   "清单里，写了也不认）" if method else ""))

        # ── 本门的核心判据：章只认工具落的，且必须真调过 ──────────────────
        if status in ("verified", "numerically_supported"):
            if tool not in verifiers:
                problems.append(
                    f"{step_id} 声称 {status}，但 verification.tool={tool or '缺失'} "
                    f"不是本环境注册在案的验证工具。合法来源：{'、'.join(verifiers)}。"
                    f"**自己写的验证章不算数** —— 真去调一次工具，把它返回的 "
                    f"verification 块原样贴进来。")
            elif ledger_readable:
                # 只查工具名 = 只挡"随手编个名字"，挡不住"照着合法格式编一个章"。
                # 真判据是本 run 的账本里有没有这次调用（shared/lib/derivation_ledger）。
                # ⚠️ 账本读不到时不查 —— 那是取证手段失灵，不是伪造；
                # 拦了就是把一次读盘失败变成"这个节点交不了差"。
                fingerprint = str(verification.get("probe") or "").strip()
                if not fingerprint:
                    problems.append(
                        f"{step_id} 的验证章缺 probe（式子指纹）。工具返回的 "
                        f"verification 块里一定带它 —— **把整块原样贴进来**，"
                        f"不要只手抄 status 和 tool。")
                elif fingerprint not in (ledger or {}):
                    problems.append(
                        f"{step_id} 的 probe={fingerprint} 在本 run 的验证账本里"
                        f"查无此项 —— 这一步没有真的调过验证工具。"
                        f"**报告不是事实**：真调一次 {tool}，把它返回的 "
                        f"verification 原样贴进来。")
                else:
                    recorded = str((ledger or {})[fingerprint].get("status") or "")
                    if recorded != status:
                        problems.append(
                            f"{step_id} 自称 {status}，而账本里这个式子"
                            f"（probe={fingerprint}）最后一次的真实结论是 "
                            f"{recorded or '未知'}。以账本为准 —— "
                            f"要改结论就补上假设重验一次，别改章。")
        elif status in ("inconclusive", "unverified"):
            unverified.append(step_id)
        elif status == "failed":
            failed.append(step_id)

    return (problems, advisories, unverified, failed)


def _live_assumptions(assumptions: Any) -> tuple[list[str], list[str], list[str]]:
    """返回 (problems, advisories, 带进结论的假设 id)。

    活假设 = 引入了、且没有被显式解除的。结论的适用域是它们的并集 ——
    **由框架现算，不读模型手写的那份**：手写的必然随链演化而漂移，
    而"适用域静默扩大"正是最难人眼发现的一类错。
    330（缺 statement）/334（缺 discharged_by）已降格为 advisory
    （判决拆除批 3w）。
    """
    problems: list[str] = []
    advisories: list[str] = []
    live: list[str] = []
    if not isinstance(assumptions, list):
        return (["assumptions 必须是列表（没有假设就写空列表 []）"], [], [])
    for index, item in enumerate(assumptions):
        if not isinstance(item, dict):
            problems.append(f"assumptions[{index}] 必须是对象")
            continue
        label = str(item.get("id") or f"assumptions[{index}]")
        if not _is_filled(item.get("statement")):
            advisories.append(f"{label} 缺 statement（这条假设到底假设了什么）")
        status = str(item.get("status") or "").strip().lower()
        if status in ("discharged", "解除"):
            if not _is_filled(item.get("discharged_by")):
                advisories.append(
                    f"{label} 声明已解除，但没写 discharged_by（哪一步解除的）")
        else:
            live.append(label)
    return (problems, advisories, live)


def _frozen_closure_keys(state: Any) -> set[str]:
    """冻结预注册里所有闭合条目的合法键。拿不到就返回空集（不拦）。"""
    try:
        from core.prereg_commitments import frozen_questions

        return {
            item.key
            for question in frozen_questions(state).values()
            for item in question.closure
        }
    except Exception:
        return set()


def _closure_key_hint(state: Any) -> str:
    """把合法键**按种类**列出来 —— 光列一串键，调用方还是不知道规则。

    键规则本身是不对称的（`core/prereg_commitments.ClosureItem.key`）：
    数值条按 **metric 名**（与 experiment 的 measured_metrics 约定一致），
    陈述条按 **id**。这条规则以前只活在框架代码里，模型只能猜 ——
    2026-08-22 两趟 e2e 给了两个不同答案（第一趟按 metric 名写对，
    第二趟按 id 写错），错的那趟不报错、账本静默归零。
    """
    try:
        from core.prereg_commitments import frozen_questions, NUMERIC

        numeric, statement = [], []
        for question in frozen_questions(state).values():
            for item in question.closure:
                (numeric if item.kind == NUMERIC else statement).append(item.key)
        parts = []
        if numeric:
            parts.append("数值条（写进 measured_metrics，键是 **metric 名**）："
                         + "、".join(sorted(numeric)))
        if statement:
            parts.append("陈述条（写进 closure_discharges，键是 **id**）："
                         + "、".join(sorted(statement)))
        return "；".join(parts)
    except Exception:
        return ""


def _audit_discharge_keys(state: Any, metadata: dict[str, Any]) -> str | None:
    """勾账的键必须逐字对上冻结预注册 —— **两个入口都查**。

    第一版只查了 `closure_discharges`，漏了 `measured_metrics`。
    2026-08-22 e2e 第二趟当场撞上：模型把数值条兑现写进 measured_metrics，
    键用了 item id（`CLASSICAL_LIMIT`）而不是 metric 名（`lim_{x->0} C/k_B`）。
    冻结门放行、报告写着"已兑现"，而账本认为零兑现 —— 与 2026-08-19 那笔
    「12 条兑现一条都不算数」完全同形，只是换了个入口。

    **一道闸只覆盖一半的入口，等于没有覆盖。**
    """
    legal = _frozen_closure_keys(state)
    if not legal:
        return None          # 读不到冻结协议：不拦（非 Project 独立运行等）
    bad: list[str] = []
    for field in ("closure_discharges", "measured_metrics"):
        block = _dig(metadata, field)
        if not isinstance(block, dict):
            continue
        bad += [f"{field}.{k}" for k in block if k not in legal]
    if not bad:
        return None
    hint = _closure_key_hint(state)
    return (
        f"这些勾账键在冻结预注册里不存在：{'、'.join(sorted(bad))}。\n"
        f"合法键（逐字照抄）：{hint or '、'.join(sorted(legal))}。\n"
        "键对不上的兑现记录账本一条都不认，**而且不会报错** —— "
        "只会在下游对账时表现为「零兑现」。")


def _audit_main_result(main: Any) -> tuple[dict[str, str], dict[str, str]]:
    """主结果的形状纪律。返回 (problems, advisories)。

    `expression` 必须是**机器可解析的表达式**，不是带注释的散文 ——
    下游要拿它去 sympy 比对、去与实验值对账。写成
    `(x/2)²/sinh²(x/2)（附加路线；非承重）` 那样，下游只能猜。
    「必须有 statement」降格为 advisory（判决拆除批 3w，427）；形状（对象）
    与可解析性（434，sympy 解析契约，改判 C）保持拒绝。
    """
    if not isinstance(main, dict):
        return ({"main_result_shape": (
            "main_result 必须是对象，含 expression（机器可解析的表达式）"
            "与 statement（人读的一句话）。")}, {})
    problems: dict[str, str] = {}
    advisories: dict[str, str] = {}
    missing = [k for k in MAIN_RESULT_REQUIRED_KEYS if not _is_filled(main.get(k))]
    if missing:
        advisories["main_result_incomplete"] = (
            f"main_result 缺 {'、'.join(missing)}。"
            "expression 给机器（下游要解析它去比对/对账），statement 给人。")
    expr = str(main.get("expression") or "")
    if expr:
        # 中文/全角出现在 expression 里，基本可以断定混进了注释
        if any("\u4e00" <= ch <= "\u9fff" for ch in expr):
            problems["main_result_not_parseable"] = (
                f"main_result.expression 里有中文：{expr[:60]!r}。"
                "它要能被 sympy 直接解析 —— 说明与限定条件写进 statement 或 "
                "assumptions，别混进表达式。")
    return (problems, advisories)


def _structured_concessions(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """metadata.concessions：结构化让步声明 —— **义务出口不得由关键词把守**。

    形态（判决拆除批 3w「跨组统一修项」，deriv 516/638/762 同修）：

        concessions:
          - check: rigor_promise          # 让步针对哪条义务（机械匹配，非子串）
            reason: "区间因依赖收不窄，实际只到 L1"
            achieved: l1                  # 可选，rigor_promise 用

    只认带非空 reason 的条目；check 名逐字匹配。散文里写「已降级」不再是
    出口 —— 中文关键词匹配让英文诚实降级过不去，也会被一句空话骗过
    （同病：QC judge 只读可见句）。
    """
    raw = metadata.get("concessions")
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        check = str(item.get("check") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if check and reason:
            out[check] = item
    return out


#: 每个严格度档要求主结果验到什么水平：{档: 可接受的 method 集合}。
#:
#: 这是**对主结果的承诺**，不是对每一步的要求 —— 一条链里绝大多数步骤用
#: check_step 就够了，逐步都要形式化是把闸开成了刑具。
_RIGOR_LADDER: dict[str, tuple[str, ...]] = {
    "l0": ("numeric", "symbolic", "interval", "formal"),
    "l1": ("symbolic", "interval", "formal"),
    "l1.5": ("interval", "formal"),
    "l2": ("formal",),
}


def _audit_rigor_promise(state: Any, metadata: dict[str, Any],
                         steps: Any) -> dict[str, str]:
    """派你的人要了哪一档，你实际做到了哪一档 —— 对不上就得如实说。

    ## 为什么必须机械核对（2026-08-23 真跑抓到）

    `rigor_level` 原本是 `expected_inputs` 里一条纯声明：描述写得很好
    （L0 数值 / L1 CAS / L1.5 区间认证 / L2 形式化），调度器读了它、
    **认真地选了 L1.5**，参数也确实传进了节点 —— 然后 `grep -rn rigor_level`
    在整个代码库里**一个读它的地方都没有**。节点引导没提过它，代码没人消费它。
    那趟 run 的账本里 6 条 `numerically_supported` 原样通过。

    调度器以为自己要了区间认证，节点以为自己做完了，**两边都不报错**。
    这与 check_lean 曾经只是白名单里一个字符串是同一族病：声明存在，实现不在。

    ## 判据：兑现，或者如实降级

    拿不到高档是常有的事（结论不是数值表达式、裸 Lean 无 mathlib、区间因
    依赖问题收不窄）。所以这道闸**不要求兑现**，只要求**说出来** ——
    在 `credibility` 里写明实际到了哪一档。

    静默忽略不放行，如实降级放行。同「未验不是罪，装作验过才是」：
    一份说好 L1.5、实际只有数值抽查却不交代的记录，比老实标 L1 的危险得多,
    因为下游会按 L1.5 的可信度去引用它。
    """
    requested = str((_node_inputs(state) or {}).get("rigor_level") or "").strip().lower()
    if not requested or requested not in _RIGOR_LADDER:
        return {}                       # 没要求，或者档位不认识 —— 不管
    accepted = _RIGOR_LADDER[requested]

    main = _dig(metadata, "main_result")
    expression = str((main or {}).get("expression") or "").strip() if isinstance(main, dict) else ""

    # 主结果那条式子在链上验到了什么 method
    achieved: set[str] = set()
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            verification = step.get("verification")
            if not isinstance(verification, dict):
                continue
            if str(verification.get("status") or "") not in (
                    "verified", "numerically_supported"):
                continue
            method = str(verification.get("method") or "").strip().lower()
            if not method:
                continue
            # 与主结果相关的步骤：表达式出现在 claim 里，或者没声明表达式时
            # 退回"整条链验到的最高档"（结论不是式子的情形）。
            claim = str(step.get("claim") or "")
            if not expression or expression in claim:
                achieved.add(method)
    if achieved & set(accepted):
        return {}                       # 承诺兑现了

    # 没兑现 —— 那就必须**结构化地**交代。判决拆除 516「一处修」：出口原是
    # 中文关键词匹配 credibility 散文（"降级/未达/只到/达不到/实际"）——英文
    # 诚实降级过不去，一句空话反而过得去。义务出口不得由关键词把守：
    # 现在只认 metadata.concessions 里 check="rigor_promise" 的结构化让步
    # （带非空 reason；可选 achieved 写实际档位）。
    concession = _structured_concessions(metadata).get("rigor_promise")
    if concession is not None:
        return {}                       # 如实降级（结构化申报），放行

    return {"rigor_promise": (
        f"派发时要求 rigor_level={requested}（可接受的验证方式："
        f"{'/'.join(accepted)}），但主结果没有这一档的验证章"
        f"{'（链上只有 ' + '/'.join(sorted(achieved)) + '）' if achieved else ''}。\n"
        f"两条合法出路：\n"
        f"  1. 真去补上 —— L1.5 用 `interval_check`（主结果那条式子给个 domain），"
        f"L2 用 `check_lean`；\n"
        f"  2. **拿不到就如实降级** —— 在 metadata.concessions 里加一条 "
        f'{{"check": "rigor_promise", "achieved": "<实际档位>", '
        f'"reason": "<为什么拿不到 {requested}>"}}。\n'
        f"拿不到不是罪，不说才是：下游会按 {requested} 的可信度去引用你的结论。")}


def _node_inputs(state: Any) -> dict[str, Any]:
    """本 run 收到的 node_inputs（读不到就当空 —— 观测失灵不等于违规）。

    ⚠️ 真实来源是 `state.hook_state["node_inputs"]`（executor 在起 loop 前落进去
    的，见 core/executor.py「把 node_inputs 落 hook_state，让 hook 能拿到」）。
    **State 上没有 `node_inputs` 属性** —— 第一版我按直觉写了 getattr，
    那样这道闸会永远读到空、永远不触发，又是一个幽灵机制。
    今天已经在同一个坑里栽过一次（list_artifacts 的条目不带 metadata）。
    """
    hook_state = getattr(state, "hook_state", None)
    if isinstance(hook_state, dict):
        value = hook_state.get("node_inputs")
        if isinstance(value, dict):
            return value
    return {}


def _audit_the_audit(metadata: dict[str, Any], steps: Any) -> dict[str, str]:
    """audit 模式的独有纪律。

    ## 这一档防的是什么

    审计外来推导有两种独有的作弊/失误方式，前两档的闸都拦不住：

    1. **审了一份、报告另一份** —— 源不绑定。同 postprocess 的图片源绑定
       hash：不去推断谁改的，直接让对不上的进不来。
    2. **把「我验不了」讲成「它错了」** —— 这一档最危险的错误方向。
       审计的结论会被人拿去否定别人的工作，而 CAS 判不了的东西多得很
       （逻辑推理、元数学、超出 sympy 能力的积分）。所以
       **指控必须挂证据**：说某一步有缺陷，要么那一步的验证结论真是 failed，
       要么在 findings 里点名说清矛盾在哪。笼统一句"不够严谨"不算指控，
       算意见。

    ## 两个正交的裁决

    `audit_verdict` 判**这份推导**（sound / flawed / unverifiable），
    `verdicts` 判**命题本身**。一份有漏洞的证明可能碰巧证了个真命题 ——
    把这两件事塞进一个字段，就没法如实表达那种情况。

    ## 两档（判决拆除批 3w，呈裁⑤定案）

    返回 (problems, advisories)。audit_target 的**内容指纹**（source +
    content_hash + 形状）单独保留为 B —— 「审一份贴另一份」放行即账假；
    其余（599 缺 reasoning / 607 flawed 未点名 / 638 指控未挂证据）降格：
    如实记 advisory 随产物走，referee 终审。638 的证据出口同步改结构化
    （原来对 findings 序列化串做 `sid in blob` 子串匹配——出口不得由关键词
    把守）：flawed_steps 条目可以是 {"step": id, "evidence": ...} 对象，
    或结构化 finding 的 step/steps/id 字段逐字点名该步骤。
    """
    problems: dict[str, str] = {}
    advisories: dict[str, str] = {}

    target = _dig(metadata, "audit_target")
    if isinstance(target, dict):
        if not _is_filled(target.get("content_hash")):
            problems["audit_source_binding"] = (
                "audit_target 缺 content_hash —— 审计报告必须绑死它审的是**哪一份**。"
                "没有指纹，「审了一份、报告贴另一份」在记录上无法区分。")
        if not _is_filled(target.get("source")):
            problems["audit_source_missing"] = (
                "audit_target 缺 source（这份推导从哪来：文件路径 / URL / 谁提供的）。")
    elif target is not None:
        problems["audit_target_shape"] = "audit_target 必须是对象（含 source 与 content_hash）"

    verdict = _dig(metadata, "audit_verdict")
    if not isinstance(verdict, dict):
        if verdict is not None:
            problems["audit_verdict_shape"] = (
                f"audit_verdict 必须是对象，含 status（{'/'.join(AUDIT_VERDICTS)}）"
                "、reasoning，flawed 时还要 flawed_steps。")
        return (problems, advisories)

    status = str(verdict.get("status") or "").strip().lower()
    if status not in AUDIT_VERDICTS:
        problems["audit_verdict_status"] = (
            f"audit_verdict.status={status or '缺失'} 不合法。合法值："
            f"{'/'.join(AUDIT_VERDICTS)} —— "
            "sound=这份推导站得住；flawed=有缺陷（点名步骤并挂证据）；"
            "unverifiable=超出机械验证能力，需人审（**这不是失败，是诚实**）。")
        return (problems, advisories)

    if not _is_filled(verdict.get("reasoning")):
        # 判决拆除 599 降格
        advisories["audit_reasoning"] = "audit_verdict 缺 reasoning（凭什么下这个裁决）"

    if status != "flawed":
        return (problems, advisories)

    # ── flawed：指控应当挂证据（判决拆除 607/638 降格）──
    flawed = verdict.get("flawed_steps")
    if not isinstance(flawed, list) or not flawed:
        advisories["audit_flaw_unnamed"] = (
            "audit_verdict.status=flawed 却没有 flawed_steps。"
            "**指控要点名到步骤** —— 笼统说「不够严谨」是意见，不是审计结论。")
        return (problems, advisories)

    step_index = {}
    if isinstance(steps, list):
        for i, st in enumerate(steps):
            if isinstance(st, dict):
                step_index[str(st.get("id") or f"steps[{i}]")] = st

    def _flawed_step_id(entry: Any) -> str:
        # 结构化条目：{"step": id, "evidence": ...}；旧形态：裸 id 字符串。
        if isinstance(entry, dict):
            return str(entry.get("step") or entry.get("id") or "").strip()
        return str(entry).strip()

    unknown = [
        _flawed_step_id(x) for x in flawed
        if _flawed_step_id(x) not in step_index
    ]
    if unknown:
        problems["audit_flaw_unknown_step"] = (
            f"flawed_steps 指向了链上不存在的步骤：{'、'.join(unknown)}。"
            f"合法步骤 id：{'、'.join(sorted(step_index)) or '（链为空）'}")

    # 证据识别是**结构化**的（出口不得由关键词/子串把守）：
    #   ① 该步验证结论真是 failed（工具证否）；
    #   ② flawed_steps 条目本身是 {"step": id, "evidence": <非空>}；
    #   ③ findings 为结构化列表且某条的 step/steps/id 字段逐字点名该步骤。
    findings = _dig(metadata, "findings")
    findings_named: set[str] = set()
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            for key in ("step", "steps", "id"):
                value = finding.get(key)
                if isinstance(value, list):
                    findings_named.update(str(v).strip() for v in value)
                elif value is not None:
                    findings_named.add(str(value).strip())
    unsupported = []
    for entry in flawed:
        sid = _flawed_step_id(entry)
        step = step_index.get(sid)
        if step is None:
            continue
        ver_status = str((step.get("verification") or {}).get("status") or "").lower()
        if ver_status == "failed":
            continue                     # 工具真的判否了 —— 硬证据
        if isinstance(entry, dict) and _is_filled(entry.get("evidence")):
            continue                     # 条目自带论证证据
        if sid in findings_named:
            continue                     # 结构化 finding 点名了该步骤
        unsupported.append(sid)
    if unsupported:
        advisories["audit_flaw_unsupported"] = (
            f"这些步骤被判有缺陷却没挂证据：{'、'.join(unsupported)}。"
            "证据的合法形态：该步验证结论为 failed（工具证否）；或 flawed_steps "
            '条目写成 {"step": <id>, "evidence": "<矛盾在哪>"}；或结构化 finding '
            "的 step/steps/id 字段逐字点名该步骤。\n"
            "**把「我验不了」讲成「它错了」是这一档最危险的错误方向** —— "
            "验不了就用 unverifiable，那是诚实，不是失败。")
    return (problems, advisories)


def audit_record_shape(metadata: Any) -> dict[str, Any]:
    """只看这份 metadata 自己就能判的那部分 —— 写入门与冻结门共用。

    不读盘、不碰 state：所以它在产物还没落盘的那一刻就能跑。冻结门查的是
    它的**超集**（另加要对照项目事实才判得动的：勾账键对不对得上冻结 prereg、
    主结果的验证水平）。

    anti-HARKing 那条为什么在这里而不是只在冻结门：
    `core.prereg_commitments._scan_result_metadata` 收兑现记录时**不看产物冻没冻**,
    按类型扫全部产物的 metadata。一份 exploratory 记录只要写了
    `closure_discharges`、哪怕从不调 `freeze_artifact`，勾账照样进账本。
    闸放哪层按对手是谁推 —— 对手是"不冻结就交付"，闸就不能只长在冻结上。
    """
    if not isinstance(metadata, dict):
        metadata = {}
    mode = str(_dig(metadata, "mode") or "").strip().lower()
    reasons: dict[str, str] = {}
    advisories: dict[str, str] = {}

    if mode not in KNOWN_MODES:
        reasons["mode"] = (
            f"metadata.mode 必须是 {'/'.join(KNOWN_MODES)} 之一（当前：{mode or '缺失'}）。"
            "exploratory=找猜想找形式（数值实验、特例摸底），**不能勾账**；"
            "confirmatory=对照冻结的命题给出验证链并裁决。")

    missing = [p for p in STRUCTURAL_REQUIRED_PATHS
               if not _is_filled(_dig(metadata, p))]
    # 在场即可的字段：判 key 存不存在，不判空不空 —— `assumptions: []` 是
    # 合法取值（纯代数恒等式不需要额外假设）。把空当没填就是逼模型编一条。
    missing += [p for p in PRESENT_BUT_MAY_BE_EMPTY
                if _dig(metadata, p) is None]
    if missing:
        # 判决拆除 678 降格（D-OB1 记录结构：save gate 一律不再拒存盘）：
        # 记录不完整如实记 advisory，照存盘。
        advisories["derivation_record"] = "推导记录不完整：" + "、".join(missing)

    # ── anti-HARKing：探索不能确证自己 ────────────────────────────────────
    if mode == EXPLORATORY:
        discharge_block = _dig(metadata, "closure_discharges")
        if isinstance(discharge_block, dict) and discharge_block:
            reasons["exploratory_cannot_close"] = (
                f"exploratory run 写了 closure_discharges：{'、'.join(sorted(discharge_block))}。"
                "探索这一趟是在找猜想的形式，用它勾账等于用生成猜想的那批数值实验"
                "确证同一个猜想。把猜想交给 Analysis 立成研究问题、冻结闭合条件，"
                "再起一趟 confirmatory run 兑现。")
        measurements = _dig(metadata, "measured_metrics")
        if isinstance(measurements, dict) and measurements:
            reasons["exploratory_cannot_measure"] = (
                "exploratory run 写了 measured_metrics —— 同上：预注册承诺的量"
                "只能由 confirmatory run 兑现。")

    return {"mode": mode or None, "missing": missing,
            "reasons": reasons, "advisories": advisories}


def audit_derivation_log(state: Any, artifact_id: str) -> dict[str, Any]:
    """冻结前审计。返回 {passed, mode, missing, reasons, advisories, derived}。

    reasons 拦冻结（B/C：mode 契约、anti-HARKing、伪造验证章、勾账键、
    audit_target 指纹、rigor 承诺未申报）；advisories 不拦——照冻结，
    未过项随 transcript 与 derived 如实入账（判决拆除批 3w derivation 节）。
    """
    record = state.read_artifact(artifact_id)
    if not isinstance(record, dict) or record.get("type") != _LOG_TYPE:
        return {"passed": False, "mode": None, "missing": [], "derived": {},
                "advisories": {},
                "reasons": {"artifact": f"{artifact_id} 不是 {_LOG_TYPE}"}}

    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    shape = audit_record_shape(metadata)
    mode, reasons = shape["mode"], dict(shape["reasons"])
    advisories = dict(shape["advisories"])
    missing = list(shape["missing"])

    # 只有 confirmatory / audit 才欠的那几档 —— 判决拆除 722 拆条（呈裁⑤
    # 定案）：audit 模式的 **audit_target 缺席升 B 保留**（审一份贴另一份，
    # 指纹是唯一防线——内容指纹本身由 _audit_the_audit 把守）；其余
    # （credibility / verdicts / counterexample_search / main_result /
    # audit_verdict）降格为 advisory。
    mode_required: list[str] = []
    if mode == CONFIRMATORY:
        mode_required = list(CONFIRMATORY_REQUIRED_PATHS)
    elif mode == AUDIT:
        mode_required = list(AUDIT_REQUIRED_PATHS)
    mode_missing = [p for p in mode_required if not _is_filled(_dig(metadata, p))]
    if mode_missing:
        missing += mode_missing
        blocking_mode_missing = [p for p in mode_missing
                                 if mode == AUDIT and p == "audit_target"]
        advisory_mode_missing = [p for p in mode_missing
                                 if p not in blocking_mode_missing]
        if blocking_mode_missing:
            reasons["audit_target_missing"] = (
                "audit 模式必须声明 audit_target（source + content_hash）——"
                "审计报告不绑指纹，「审了一份、报告贴另一份」在记录上无法区分。")
        if advisory_mode_missing:
            advisories["derivation_record"] = (
                "推导记录不完整：" + "、".join(
                    [p for p in missing if p not in blocking_mode_missing]))

    # ── 推导链 ────────────────────────────────────────────────────────────
    from shared.lib import derivation_ledger as _ledger

    step_problems, step_advisories, unverified, failed = _audit_steps(
        _dig(metadata, "steps"),
        ledger=_ledger.by_probe(state),
        ledger_readable=_ledger.ledger_is_readable(state))
    if step_problems:
        reasons["steps_structure"] = "；".join(step_problems)
    if step_advisories:
        advisories["steps_structure"] = "；".join(step_advisories)

    # 判决拆除 737 降格（决定性理由：墙**奖励删除反例**——链上留 failed
    # 验证是最诚实的记录，拒绝冻结等于教 agent 删掉那次失败）。failed 步骤
    # 照留链上：derived["failed_steps"] 现算入账（见证现成），这里只出
    # advisory——结论不得建在已证否的一步上，由 reviewer/referee 终审。
    if failed and mode == CONFIRMATORY:
        advisories["failed_steps_in_chain"] = (
            f"这些步骤的验证结论是 failed（找到了反例）仍留在链上："
            f"{'、'.join(failed)}。留下失败记录是诚实的——但主结果不得依赖"
            "这些步骤；修那一步，或在 credibility/findings 里说明结论如何"
            "绕开它们。failed 清单已随 derived.failed_steps 机械入账。")

    # ── 假设账本 ──────────────────────────────────────────────────────────
    assumption_problems, assumption_advisories, live = _live_assumptions(
        _dig(metadata, "assumptions"))
    if assumption_problems:
        reasons["assumptions_structure"] = "；".join(assumption_problems)
    if assumption_advisories:
        advisories["assumptions_structure"] = "；".join(assumption_advisories)

    # ── 数值支持不能冒充演绎 ──────────────────────────────────────────────
    steps = _dig(metadata, "steps")
    numeric_only = []
    if isinstance(steps, list):
        numeric_only = [
            str(s.get("id") or f"#{i}")
            for i, s in enumerate(steps) if isinstance(s, dict)
            and str((s.get("verification") or {}).get("status") or "").lower()
            == "numerically_supported"
        ]
    if mode == CONFIRMATORY and numeric_only:
        # 判决拆除 762 降格 + 关键词出口改结构化（跨组统一修项）：原判据对
        # credibility 散文做子串匹配（"数值/numeric/…"）——出口不得由关键词
        # 把守。合法出口＝metadata.concessions 里 check="numeric_support" 的
        # 结构化让步；没申报则如实记 advisory，随产物交 referee。
        if "numeric_support" not in _structured_concessions(metadata):
            advisories["numeric_support_not_disclosed"] = (
                f"这些步骤只有数值支持：{'、'.join(numeric_only)}，而 metadata."
                'concessions 里没有 {"check": "numeric_support", "reason": ...} '
                "的结构化交代。**数值证据关不掉演绎命题** —— 10^6 个点上全对"
                "不是证明。numerically_supported 清单已随 "
                "derived.numerically_supported_steps 机械入账。")

    # ── 勾账的键必须逐字对上冻结预注册 ────────────────────────────────────
    #
    # 与 observation 同源（2026-08-19 那笔 12 条兑现归零的账）：键错了不报错，
    # 语法合法、冻结放行、节点自己报告"已兑现"，直到下游对账才发现是零。
    key_problem = _audit_discharge_keys(state, metadata)
    if key_problem:
        reasons["unknown_closure_keys"] = key_problem

    # ── 主结果的形状（confirmatory / audit 都要）──────────────────────────
    if mode in (CONFIRMATORY, AUDIT):
        main = _dig(metadata, "main_result")
        if main is not None:
            main_problems, main_advisories = _audit_main_result(main)
            for key, value in main_problems.items():
                reasons.setdefault(key, value)
            for key, value in main_advisories.items():
                advisories.setdefault(key, value)

        # 派发时要的严格度档，兑现了没有（兑现或结构化如实降级，二选一）——
        # 516 保留为范式样板：完整义务形态，仍拦。
        for key, value in _audit_rigor_promise(state, metadata,
                                               _dig(metadata, "steps")).items():
            reasons.setdefault(key, value)

    # ── audit 模式的独有纪律 ──────────────────────────────────────────────
    if mode == AUDIT:
        audit_problems, audit_advisories = _audit_the_audit(metadata, steps)
        for key, value in audit_problems.items():
            reasons.setdefault(key, value)
        for key, value in audit_advisories.items():
            advisories.setdefault(key, value)

    # ── findings（两种模式都要）────────────────────────────────────────────
    # 判决拆除 796 降格（D-OB3）：findings 空如实记 advisory，不拦冻结。
    findings = _dig(metadata, "findings")
    if not _is_filled(findings):
        advisories.setdefault("no_findings", (
            "findings 为空。哪怕这一趟只是把已知结果重推一遍，也要写下你**看到了"
            "什么** —— 哪一步最脆、哪个假设其实可以去掉、哪里跟文献的做法不同。"
            "一份只有推导链的记录，把这一趟真正的收获丢在了 prose 里。"))

    derived = {
        "unverified_steps": unverified,
        "failed_steps": failed,
        "numerically_supported_steps": numeric_only,
        # 结论的适用域：框架现算，不读模型手写的那份
        "validity_domain": live,
        # 降格项的见证（判决拆除批 3w）：advisories 随冻结入 transcript。
        "advisories": advisories,
    }
    return {"passed": not reasons, "mode": mode or None,
            "missing": missing, "reasons": reasons,
            "advisories": advisories, "derived": derived}


# ── 冻结门注册 ──────────────────────────────────────────────────────────────
from shared.tools.library.artifacts_extra import (
    register_freeze_gate as _register_freeze_gate,
    register_save_gate as _register_save_gate,
)


def _derivation_log_freeze_gate(state, artifact_id, record):
    audit = audit_derivation_log(state, artifact_id)
    state.append_transcript(
        "derivation_pre_freeze_gate",
        artifact_id=artifact_id, mode=audit["mode"], passed=audit["passed"],
        missing=audit["missing"], reasons=audit["reasons"],
        advisories=audit["advisories"],
        derived=audit["derived"],
    )
    if audit["passed"]:
        # 现算出来的几份账随冻结一起落盘 —— 它们是判决的输入，不是判决本身，
        # 每次冻结重算，不会随规则演化而作废。advisories 已并入 derived。
        return {"derived": audit["derived"]}
    mode = audit["mode"]
    expected = list(STRUCTURAL_REQUIRED_PATHS) + list(PRESENT_BUT_MAY_BE_EMPTY)
    if mode == CONFIRMATORY:
        expected += list(CONFIRMATORY_REQUIRED_PATHS)
    elif mode == AUDIT:
        expected += list(AUDIT_REQUIRED_PATHS)
    return {
        "failures": {"derivation_discipline": "; ".join(
            f"{k}: {v}" for k, v in audit["reasons"].items())},
        "missing": audit["missing"],
        "mode": mode,
        "reasons": audit["reasons"],
        "advisories": audit["advisories"],
        "hint": (
            f"本 run 模式={mode or '未声明'}。metadata 建议含：" + "、".join(expected)
            + "，外加 findings（缺项照冻结、如实记 advisory）。仍拦死的："
            "mode 合法、验证章只认工具落的、勾账键逐字对上冻结 prereg、"
            "audit 必须绑 audit_target 指纹、rigor 承诺兑现或在 metadata."
            "concessions 里结构化如实降级。exploratory 模式不许写 "
            "closure_discharges / measured_metrics。"
        ),
    }


_register_freeze_gate(_LOG_TYPE, _derivation_log_freeze_gate)


# ── 写入门：结构纪律站到必经之路上 ──────────────────────────────────────────
#
# 2026-08-23 实测事故（benchmark 真跑，damped_resonance_width）：模型**真的**
# 调了 9 次 check_step 验了每一步，却把 step.verification 写成字符串
# `"verified"`——把工具返回的验证章丢了，只手抄了一个结论词。然后
# save_artifact 两次、**从头到尾没调 freeze_artifact**，run 判 completed。
#
# 上面那道冻结门查的正是这件事（`_audit_steps` 的核心判据："自己写的验证章
# 不算数"），它写得没问题 —— 它只是**一次都没跑**。冻结是模型自愿调用的动作，
# 把类型的结构纪律挂在自愿动作上，等于给了一条"不冻结就什么都不查"的近路。
#
# 分工（判据一个字都不重写，两道门调同一批函数 ——「一个问题一个真相源」）：
#
# | | 写入门（这里） | 冻结门（上面） |
# |---|---|---|
# | 查什么 | 这条链的**结构**成不成立 | 这份**承诺**兑现了没有 |
# | 与模式 | 无关，每次写都查 | 相关（confirmatory 勾账 / audit 自审）|
# | 判据 | `_audit_steps` | 完整 `audit_derivation_log` |
#
# 写入门查的是**只看这份 draft 就能判**的那些（`audit_record_shape` + 链结构）；
# 闭合项对账、主结果验证水平要对照项目里别的事实，留给冻结门。
#
# 2026-08-23 修正：这里原本只查 steps，理由是"`required_metadata` 已经在写入面
# 拦掉缺字段的产物"。那层已经删了 —— 它是**模式盲**的类型级近似，把
# credibility / verdicts / counterexample_search 判成全模式必需，而本文件的冻结门
# 明写着它们只有 confirmatory 才欠。后果：一趟合法的 exploratory 推导
# （找猜想找形式、按设计不勾账）**连产物都存不下来**，模型只能编一份裁决出来。
# 契约的唯一声明权收回本模块，两道门共用同一批判据函数。
def _record_advisories_on_draft(draft: dict, advisories: dict[str, str]) -> None:
    """降格项的机械落点：未过项写进**要落盘的那份** metadata（照存盘，如实可见）。

    run_save_gate 传进来的 metadata 与 save_artifact 将写盘的是同一个 dict——
    改它即改账。metadata 不是 dict 时别的 blocking 判据必然在场，不强写。
    """
    metadata = (draft or {}).get("metadata")
    if advisories and isinstance(metadata, dict):
        existing = metadata.get("advisories")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(advisories)
        metadata["advisories"] = merged


def _derivation_log_save_gate(state, draft: dict) -> dict[str, Any]:
    from shared.lib import derivation_ledger as _ledger

    metadata = draft.get("metadata") or {}
    shape = audit_record_shape(metadata)
    failures = dict(shape["reasons"])
    advisories = dict(shape["advisories"])

    problems, step_advisories, _unverified, _failed = _audit_steps(
        _dig(metadata, "steps"),
        ledger=_ledger.by_probe(state),
        ledger_readable=_ledger.ledger_is_readable(state))
    if problems:
        failures["steps_structure"] = "；".join(problems)
    if step_advisories:
        advisories["steps_structure"] = "；".join(step_advisories)

    # 判决拆除批 3w（D-OB1：save gate 一律不再拒存盘——降格项照存盘，
    # 未过项写进产物 metadata.advisories）。仍拦死的只有 B/C：mode 契约、
    # anti-HARKing、伪造的验证章、类型错误。
    _record_advisories_on_draft(draft, advisories)

    if not failures:
        return {}
    return {
        "failures": failures,
        "missing": shape["missing"],
        "mode": shape["mode"],
        "advisories": advisories,
        "hint": (
            "**验证章要原样贴，不能手抄。** `check_step` 返回的 `verification` "
            "是一个对象（带 tool / status / method / probe），把**整块**放进 "
            "step.verification —— 只写一个 \"verified\" 字符串，等于把证据丢了"
            "只留下自述，而自述正是这道门要挡的东西。\n"
            "没验过的步骤是合法的：不带 verification 块会按 unverified 如实"
            "入账（也可把 justification 标成 "
            f"{'/'.join(JUSTIFICATIONS_WITHOUT_MECHANICAL_CHECK)} 之一）—— "
            "**未验不是罪，装作验过才是**。"
        ),
    }


_register_save_gate(
    _LOG_TYPE, _derivation_log_save_gate,
    content_contract={
        "mode": (
            f"{'/'.join(KNOWN_MODES)} 之一。exploratory=找猜想找形式，"
            "⛔**不许**写 closure_discharges / measured_metrics；"
            "confirmatory=对照冻结命题给验证链并裁决；audit=审别人的推导。"
        ),
        "assumptions / findings": (
            "**key 必须在，值可以为空**。`assumptions: []` 是合法取值 —— 纯代数"
            "恒等式确实不需要额外假设，为了过门编一条出来才是这道闸要防的。"
            "key 都不写则说明这一趟压根没做假设账这件事。"
        ),
        "steps[].verification": (
            "调 check_step / find_counterexample / dimensional_check / limit_check "
            "拿到的**整个 verification 对象**原样贴进来（含 probe 指纹）。"
            "自己手写 status:verified 写不进去。没机械验过的步骤把 justification "
            "标成 cited_theorem / prose / definition —— 未验不是罪，装作验过才是。"
        ),
        "steps[].justification": (
            "凭什么走这一步。「显然/易证/trivially/clearly」不是理由，是**跳过"
            "理由** —— 写出用到的恒等式/定理名，或标成免验类别并给出处。"
            "（原反显然闸已拆：这是给你的提醒，不是墙——审阅的人看得见空话。）"
        ),
        "concessions": (
            "结构化让步声明（可选）：[{check, reason, ...}]。兑现不了的义务"
            "（如 rigor_promise 要求的档位、numeric_support 的交代）在这里"
            "**结构化**申报——check 逐字点名义务名、reason 写为什么。散文里的"
            "「已降级」不再被识别为出口。"
        ),
    })
