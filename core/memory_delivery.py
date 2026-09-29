"""记忆的送达 —— 沉淀只是回路的一半。

## 为什么四条通道都必须机械触发

「只要还需要模型主动去查，它就有一半概率不查」是本仓实测出来的。上一代把
33.5KB 高质量教训放在盘上，主动读工具却被撤下、被动注入又被 `kb_query`
门控挡死 —— 实测 9 个节点里 8 个裸调度下**一个字节都收不到**。

所以这里没有 query 门控、没有 opt-in、没有"相关性判断"：

    宪法      每轮，全节点        用户立的规矩，逐字
    局面      每 run 开工         盘面现算 + 调度器叙事
    开工切片   turn 1，全节点      手册中适用于本节点的条目
    首用附单   首次调用工具 X 时    手册中挂在 X 上的条目

前两条无条件；后两条按 `applies_to` **机械匹配**，不按语义相关性 ——
相关性判断需要 embedding、会漏、且解释不了为什么没送到。

## 预算

全部是**常数**，不随手册规模变（KB「有界读取」原则的记忆版）：手册长到
一千条，注入量还是这些。手册可以长 —— 它是参考区，靠切片送达。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from core import memory as M

log = logging.getLogger(__name__)

#: 局面段预算（调度器上下文是全系统最稀缺的资源）。
SITUATION_BYTE_CAP = 3_072
#: 开工切片预算。
ONBOARDING_BYTE_CAP = 4_096
#: 首用附单：最多几条、多长。挂在一个工具上的教训超过 2 条，
#: 说明那个工具本身该修了，不是靠念更多条给模型听。
TOOL_BRIEF_MAX_ENTRIES = 2
TOOL_BRIEF_BYTE_CAP = 512

CONSTITUTION_HEADING = "## 📜 本项目的目标与铁律（用户所立）"
SITUATION_HEADING = "## 🧭 研究局面（框架现算）"
ONBOARDING_HEADING = "## 📕 与你这次工作有关的既往教训"
TOOL_BRIEF_HEADING = "📕 这个工具上次咬过人"


def _truncate(text: str, cap: int) -> str:
    """按**字节**截断并留痕。静默截断等于骗读者。"""
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return raw[:cap].decode("utf-8", errors="ignore") + f"\n…（已截断到 {cap} 字节）"


# ── 通道 1：宪法 ────────────────────────────────────────────────────────────


def constitution_block(state: Any) -> str | None:
    """研究目标 + 科研铁律。**无条件注入，位于一切可变状态之前。**

    它的权威来自说话的人，不来自加工 —— 所以排在被模型改写过的任何东西前面。
    """
    goal = M.read_section(state, M.SECTION_GOAL).strip()
    law_entries = M.laws(state)
    if not goal and not law_entries:
        return None
    out = [CONSTITUTION_HEADING, ""]
    if goal:
        out += [f"**研究目标**\n\n{goal}", ""]
    if law_entries:
        out.append("**科研铁律**（违反会在 review 亮红旗）")
        out.append("")
        for e in law_entries:
            out.append(f"- {e.text}")
        out.append("")
        # 出处不进注入面：它是给**人**看的审计线索，进 prompt 只会挤占预算。
        # 要看依据就读 MEMORY.md —— 那才是它该被读到的地方。
    return "\n".join(out).strip()


def law_checklist(state: Any) -> list[str]:
    """铁律逐条 —— 供 reviewer 机械展开成必答检查项。

    prompt 里写的"必须"不是机制（v21 收尾闸的教训）。铁律要有牙齿，
    只能靠它在审查时变成**必须逐条回答的问题**，而不是背景散文。
    """
    return [e.text for e in M.laws(state) if e.text.strip()]


# ── 通道 2：局面 ────────────────────────────────────────────────────────────


def situation_block(state: Any, node_type: str) -> str | None:
    """盘面现算的研究局面 + 调度器写的叙事。

    **九成不落盘**：什么冻了、兑现几条、有多少证据 —— 全在盘面上，手工维护的
    局面板必然漂移。只有"为什么走这条路"是盘面推不出来的，那一段才落盘。
    """
    # 没绑项目的匿名 run 没有"研究局面" —— 对它说"你有零份证据"是噪音，
    # 不是信息。局面是**项目**的属性。
    if getattr(state, "project_worktree", None) is None \
            and getattr(state, "project_root", None) is None:
        return None

    lines: list[str] = []
    try:
        from core.research_situation import (
            compute_situation, render_situation_facts,
        )

        lines = render_situation_facts(compute_situation(state))
    except Exception as e:                       # 现算失败不该拖垮整轮
        log.debug("situation compute failed: %s", e)
        lines = []

    narrative = M.read_section(state, M.SECTION_NARRATIVE).strip()
    if not lines and not narrative:
        return None

    out = [SITUATION_HEADING, ""]
    if lines:
        out += lines + [""]
    if narrative:
        out += ["**为什么走到这一步**（调度器所记）", "", narrative, ""]
    out.append("以上是**本轮现算**的事实，不是谁记下来的状态 —— "
               "与你看到的盘面冲突时以盘面为准，并把冲突说出来。")
    return _truncate("\n".join(out).strip(), SITUATION_BYTE_CAP)


# ── 通道 3：开工切片 ────────────────────────────────────────────────────────


def onboarding_slice(state: Any, node_type: str,
                     tool_names: Iterable[str] = ()) -> str | None:
    """本节点开工时该知道的既往教训。

    匹配判据是机械的：`applies_to.nodes` 命中本节点，或 `applies_to.tools`
    与**本节点工具面**相交（讲的是你根本调不到的工具的教训，对你没用）。
    """
    surface = {t for t in tool_names if t}
    hits = [e for e in M.manual_entries(state)
            if node_type in e.nodes or (surface & set(e.tools))]
    if not hits:
        return None
    # defect 优先（那是系统性缺陷，最该被看见），其次复发多的
    hits.sort(key=lambda e: (not e.defect, -e.seen))

    out = [ONBOARDING_HEADING, ""]
    for e in hits:
        mark = "⚠️ " if e.defect else ""
        tail = f"（已复发 {e.seen} 次）" if e.seen > 1 else ""
        out.append(f"- {mark}{e.text}{tail}")
    out += ["", "这些是本项目**付过学费**买来的。与你这次的观察冲突时，"
                "冲突本身值得记下来 —— 用 memory_note。"]
    return _truncate("\n".join(out), ONBOARDING_BYTE_CAP)


# ── 通道 4：首用附单 ────────────────────────────────────────────────────────


def tool_briefing(state: Any, tool_name: str) -> str | None:
    """首次调用某工具后，把挂在它上面的教训附在结果信封里。

    为什么挂在**首次调用之后**而不是之前：拦在之前需要在派发口做决策
    （拦不拦、拦了怎么放行），那是一道会误伤的闸；附在结果上零风险，
    而真正要防的是**第二次**用错 —— 首错由开工切片预防，迭代错由这里拦住。
    """
    hits = [e for e in M.manual_entries(state) if tool_name in e.tools]
    if not hits:
        return None
    hits.sort(key=lambda e: (not e.defect, -e.seen))
    out = [TOOL_BRIEF_HEADING]
    for e in hits[:TOOL_BRIEF_MAX_ENTRIES]:
        out.append(f"· {'⚠️ ' if e.defect else ''}{e.text}")
    return _truncate("\n".join(out), TOOL_BRIEF_BYTE_CAP)
