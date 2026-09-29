"""derivation 节点的 loop hook：推导进度板 + 收尾闸。

## 和 scratchpad 白板的分工

两块板子，**判据完全不同**，谁也替不了谁：

| | scratchpad | 这一块 |
|---|---|---|
| 谁写 | 模型自己 | 没人写 —— 框架从验证账本现算 |
| 内容 | 我在哪、下一步、哪些路堵死了 | 验过哪些式子、哪些还红着 |
| 性质 | **语义**（判断、意图、暗线） | **机械**（这件事发生过没有） |

这是「机械可判归框架，语义判断归模型」的标准形态。让模型自己记"我验过
哪些步"，那份记录必然随推导演化而漂移 —— **从不更新的字段不是事实**，
而这里连字段都不该有：账本已经是事实，现算就好。

## 为什么长推导需要它

复利崩塌的另一半在 context 侧：单步正确率随 context 增长而衰减。100 步推导
的第 80 步，模型看到的应该是干净的链状态，不是 79 轮对话的残骸。

体积严格有界（同"局面只出计数与单行摘要"那条硬约束）：只报计数 + 还红着的
那几条。**verified 的不列** —— 它们是历史，占板面不产生决策。
"""
from __future__ import annotations

from core.loop_hooks import HookContext, LoopHook, register_loop_hook
from core.llm import LLMMessage

#: 板上最多列几条待处理的式子。多于此只报计数 —— 一屏列不完的清单
#: 不会被读，只会挤掉真正的工作内容。
_MAX_LISTED = 6
_BOARD_STATE_KEY = "_derivation_board_last_signature"


def _open_failures(state) -> list[dict]:
    """账本里**最后一次结论仍是 failed** 的式子。

    按 probe 取最后一次是关键：`sqrt(x**2)=x` 先 failed、补上 x>0 后 verified，
    那它已经不是问题了。取"曾经 failed 过"会让板子永远挂着已经修好的东西，
    而一块永远在喊狼来了的板子，两轮之后就没人看了。
    """
    from shared.lib import derivation_ledger as ledger

    return [rec for rec in ledger.by_probe(state).values()
            if str(rec.get("status") or "") == "failed"]


def _render(state) -> str | None:
    from shared.lib import derivation_ledger as ledger

    table = ledger.by_probe(state)
    if not table:
        return None

    counts: dict[str, int] = {}
    for rec in table.values():
        key = str(rec.get("status") or "?")
        counts[key] = counts.get(key, 0) + 1

    lines = ["🔢 推导验证账本（框架现算，不是你写的）", ""]
    order = ("verified", "numerically_supported", "inconclusive", "failed")
    summary = "　".join(
        f"{k}={counts[k]}" for k in order if counts.get(k))
    extra = "　".join(f"{k}={v}" for k, v in counts.items() if k not in order)
    lines.append(f"  已验式子 {len(table)} 条：{summary}{('　' + extra) if extra else ''}")

    failures = _open_failures(state)
    if failures:
        lines.append("")
        lines.append(f"  ⛔ 这些式子验下来**不成立**（共 {len(failures)} 条）：")
        for rec in failures[:_MAX_LISTED]:
            relation = {"eq": "=", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}.get(
                str(rec.get("relation") or "eq"), "=")
            lines.append(
                f"    · {str(rec.get('lhs') or '')[:60]} {relation} "
                f"{str(rec.get('rhs') or '')[:60]}")
        if len(failures) > _MAX_LISTED:
            lines.append(f"    …… 另有 {len(failures) - _MAX_LISTED} 条")
        lines.append(
            "    反例是强结论：回去找错，或补上适用域重验一次"
            "（补假设重验后账本以最后一次为准）。**不要换个写法再试一次直到闸放行。**")

    numeric_only = counts.get("numerically_supported", 0)
    if numeric_only:
        lines.append("")
        lines.append(
            f"  ⚠️ {numeric_only} 条只有数值支持（不是证明）。承重的那些要补演绎论证，"
            "或在 credibility 里如实交代主结果验到了哪个水平。")
    return "\n".join(lines)


def _board_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """账本有变化时注入。

    只在**签名变化**时注入 —— 每轮重复推同一块板子，是在给 context 交租金
    却不产生新信息。签名取"条数 + 各状态计数"，够灵敏（新验一条就变），
    也够便宜（不哈希正文）。
    """
    rendered = _render(ctx.state)
    if not rendered:
        return None
    signature = rendered[:200] + str(len(rendered))
    if ctx.state.hook_state.get(_BOARD_STATE_KEY) == signature:
        return None
    ctx.state.hook_state[_BOARD_STATE_KEY] = signature
    return [LLMMessage(role="system", content=rendered)]


def _asked_vs_answered(state) -> str | None:
    """把「题目问什么」与「你答了什么」并排摆出来。

    ## 为什么这是框架的活

    2026-08-23 三臂对照抓到的真实案例：一道题问**振幅**共振曲线的半高全宽，
    模型算出了**功率**的半高宽（γ 而非 √3γ）。它的账本
    **10 次验证全部 verified、0 次 failed** —— 每一步代数变换都对，
    算出的量本身也没错，错在**它回答的不是题目问的那个问题**。

    `check_step` 保证"这一步的变换成立"，**不保证"这条链在回答提问"**。
    后者是语义判断，机械层做不到，也不该假装做得到。

    但框架能做一件确实机械的事：**把两句话并排摆出来**。
    判断切不切题归模型（和 reviewer），摆出来归框架 ——
    这与 derivation_board 现算账本、把判断留给模型是同一个分工。

    体积严格有界：只出两句话，不复述推导。
    """
    try:
        from core.prereg_commitments import frozen_questions
    except Exception:
        return None
    try:
        questions = frozen_questions(state) or {}
    except Exception:
        return None
    asked = []
    for qid, q in questions.items():
        text = (getattr(q, "proposition", "") or getattr(q, "text", "") or "").strip()
        if text:
            asked.append(f"    [{qid}] {text[:200]}")
    if not asked:
        return None

    answered = ""
    logs_seen = 0
    unfrozen: list[str] = []
    try:
        for art in (state.list_artifacts() or []):
            if art.get("type") != "derivation_log":
                continue
            logs_seen += 1
            # ⚠️ `list_artifacts()` 的条目**只有 {id, type, name}**，永远不带
            # metadata（见 core/state.py 里 entry 的构造）。要读内容必须
            # `read_artifact(id)` —— 冻结门一直是这么做的，这里 2026-08-23
            # 第一版没跟上：于是无论产物写没写 main_result，这段都读到空，
            # 每次都走"没有 main_result"那条分支。
            #
            # 单元测试没抓到，因为测试替身自己实现了一个**带 metadata 的**
            # list_artifacts —— 替身遮住了被测的真实契约。现在这几条测试
            # 绑真 State 跑（见 test_derivation_board.py 末尾）。
            record = state.read_artifact(str(art.get("id") or "")) or {}
            metadata = record.get("metadata") or {}
            if not metadata.get("frozen"):
                unfrozen.append(str(art.get("id") or art.get("name") or "?"))
            main = metadata.get("main_result") or {}
            statement = str(main.get("statement") or "").strip()
            expression = str(main.get("expression") or "").strip()
            if statement or expression:
                answered = f"    {statement[:200]}"
                if expression:
                    answered += f"\n    expression: {expression[:120]}"
    except Exception:
        pass

    # 一份 derivation_log 都没有 → 静默。还没产出的时候问"你答的是不是那道题"
    # 是噪音（同 fixture 回放 / 独立运行的场景）。
    if not logs_seen:
        return None

    # ⚠️ 有日志、却读不到 main_result 时**不许静默** ——
    # 2026-08-23 实测：这道切题自查上线后第一次真跑就完全没出现，因为那趟的
    # 产物根本没写 main_result，而这里当时直接 `return None` 走人。
    # 「静默跳过」让一道防线的缺席看起来和"检查通过了"一模一样：
    # 日志里没有它，报告里也没有它，我差点据此得出"这道提示对模型无效"的结论。
    # 读不到判据来源，要说的是**判据来源不见了**，不是什么都不说。
    if not answered:
        return (
            "⚠️ 收尾前：你的 derivation_log 里**没有 main_result** —— "
            "这趟推导到底推出了什么，下游读不出来。\n"
            "  题目问的：\n" + "\n".join(asked) + "\n\n"
            "  `main_result` 至少要有 `statement`（这一趟得到的结论，一句话），"
            "`expression` 有的话给机器可解析的式子。\n"
            "  三个下游都要它：analysis 拿理论预测去对实验测量、writing 引用主结果、"
            "审计要比对最终表达式。**别让下游从末步 claim 里猜** —— "
            "链的末步经常是附加验证路线，不是主结果。"
            + (f"\n  另外这些日志还没冻结：{'、'.join(unfrozen[:3])} —— "
               "补完 main_result 后 `freeze_artifact` 一下，"
               "未冻结的推导不能作为下游的理论依据（T 线的理论先于实验，"
               "靠的就是冻结时间戳）。" if unfrozen else "")
        )

    frozen_note = (
        f"\n  ⚠️ 另外：{'、'.join(unfrozen[:3])} **还没冻结**。"
        "对完切题就 `freeze_artifact` —— 冻结门查的是承诺兑现"
        "（闭合项对账、主结果验证水平），不冻结等于这些检查一次都没跑过。"
        if unfrozen else "")

    return (
        "🔍 收尾前最后一件事：**你答的是题目问的那个问题吗？**\n\n"
        "  题目问的：\n" + "\n".join(asked) + "\n\n"
        "  你的主结果：\n" + answered + "\n\n"
        "  逐字对一遍两边的**限定词** —— 是振幅还是功率、是首阶修正项还是完整表达式、"
        "是某个极限下的近似还是精确闭式、单位/约定是否一致。\n"
        "  ⚠️ 这一条工具帮不了你：`check_step` 只验「这一步的变换成立」，"
        "**验不出「你在回答另一个问题」** —— 一条 10 步全 verified 的链，"
        "照样可能答的是隔壁那道题。真实案例见 review_spec 第 1 维。\n"
        "  对不上就改 main_result；对得上就继续收尾。"
        + frozen_note
    )


def _record_finish_checks(state, fired: list[str]) -> None:
    """把收尾闸**这一次实际触发了哪几条检查**落进 transcript。

    为什么需要它（2026-08-23）：框架的 `finish_gate_blocked` 只记
    `n_messages`，而本闸把多条检查合并成一条消息 —— 于是 n_messages 恒为 1，
    从 transcript 里**根本看不出切题自查有没有跑过**。我因此花了一轮去翻
    messages_checkpoint 才确认它执行了。

    「一道检查缺席」和「它跑了且通过」不能长得一样 —— 这条在提示文案上已经
    修过（读不到判据来源要说出来），观测面同样欠一份：**执行过要留下痕迹**，
    否则下一次诊断又得靠猜。落盘失败不影响主路径（观测不是机制）。
    """
    try:
        state.append_transcript("derivation_finish_checks",
                                fired=fired, n_checks=len(fired))
    except Exception:
        pass


def _board_on_before_finish(ctx: HookContext) -> list[LLMMessage] | None:
    """收尾前：账本里还有验不过的式子，而你要收工了。

    ⚠️ 这道闸**只放一次**（框架侧 `_finish_gate_used` 置位），所以它必须
    说得准、说得可操作 —— 拿它去跟模型拉锯，第二次就没有机会了。

    它不是硬拦：`failed` 的式子留在账本里完全可能是正常的（探索过程中的
    试错、被撤掉的路线）。真正不许放行的是"failed 的步骤还挂在链上当结论"，
    那条在冻结门（写入面）查，不在这里。这里只负责**让模型在收工前看见它**
    —— 忘了自己有一步没修，和明知故犯，是两件事。
    """
    messages: list[str] = []
    fired: list[str] = []

    # ① 切题自查 —— 每次收尾都问，不只在有 failed 时问。
    #    那道真实错题的账本是 0 次 failed，只查 failed 的话它根本不会被问到。
    aligned = _asked_vs_answered(ctx.state)
    if aligned:
        messages.append(aligned)
        fired.append("alignment")

    failures = _open_failures(ctx.state)
    if not failures:
        _record_finish_checks(ctx.state, fired)
        return ([LLMMessage(role="system", content="\n\n".join(messages))]
                if messages else None)
    listed = "\n".join(
        f"  · {str(r.get('lhs') or '')[:70]} vs {str(r.get('rhs') or '')[:70]}"
        for r in failures[:_MAX_LISTED])
    messages.append((
        f"⛔ 收尾前对一下账：本 run 有 {len(failures)} 条式子验下来**不成立**，"
        f"而它们至今没有被重验推翻：\n{listed}\n\n"
        "在收工之前，对每一条给个交代（三选一，都是合法出口）：\n"
        "  1. 它错在哪、改对了 —— 补上适用域或修正式子，**重验一次**\n"
        "  2. 这条路线已经撤掉 —— 确认它没有出现在 derivation_log 的 steps 里\n"
        "  3. 它本身就是一个发现（原命题不成立）—— 写进 findings，"
        "并让 verdict 如实反映\n\n"
        "**证否是结果，不是失败。** 但把它留在账本里不说话，"
        "下游没法知道你是修了、撤了、还是没看见。"
    ))
    fired.append("open_failures")
    _record_finish_checks(ctx.state, fired)
    return [LLMMessage(role="system", content="\n\n".join(messages))]


register_loop_hook(LoopHook(
    name="derivation_board",
    description=(
        "从验证账本现算推导进度：验了多少条、哪些还红着、几条只有数值支持。"
        "**框架现算，不是模型自报** —— 与 scratchpad 白板分工："
        "那块记语义（我在哪、下一步），这块记机械事实（这件事验过没有）。"
        "只在账本变化时注入；体积有界（只列还红着的，verified 的不占板面）。"
        "另挂收尾闸：还有验不过的式子就在收工前要一次交代（只放一次，不硬拦）。"
    ),
    on_turn_start=_board_on_turn_start,
    on_before_finish=_board_on_before_finish,
))
