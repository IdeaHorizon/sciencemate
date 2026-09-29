"""调度器的路由仪表盘 —— 机械算出"这个项目现在处在什么位置"。

## 缺的是什么

调度器每轮拿到的局面注入（`_situation` hook）只回答"**流程**走到哪了"：收尾
三步欠不欠账、有没有 research_state。它回答不了"**研究**走到哪了"：

  - 用户最初要的是什么（`research_intake.json` 在盘上，从没进过注入）
  - 承诺的闭合条件兑现了几条、还欠哪几类
  - 盘上到底有没有证据、有多少

2026-08-18 英国饮食那趟的现场：调度器把一份 S0-S17 全是文献裁决的
research_plan 派给了计算实验节点，而"literature/data 只有 README、零证据产物"
这个致命事实，是 experiment 进场之后自己翻盘发现的。**那本该是派发前就该知道
的事**：证据只有 7 件、一手史料 0 件，此时派裁决必然 inconclusive。

统筹者看不见局面，就只能按默认剧本办事 —— 这就是"官僚"的机械成因，不是模型
不聪明。

## 兑现进度不在这里算

「闭合条目兑现了几条」由 `core.prereg_commitments.closure_tally` 给 —— 那是账本
自己的实现。这里曾经复制过一份同样的循环，两份"怎么算兑现"迟早分叉，而分叉时
两边都不报错。

## 为什么单独一个模块

计算与渲染分离：本模块只**算事实**（可单测，不依赖 hook / prompt），
`loop_hooks_builtin` 只负责把它渲染成注入文本。往 hook 里堆领域逻辑会让"项目
处在什么位置"这个问题散进注入代码里 —— 而它显然是个领域问题。

## 一条硬约束：只出计数与单行摘要

调度器上下文是全系统最稀缺的资源（800KB 衰减是实测硬墙），而它每轮都吃这段
注入。所以这里**永远不返回产物正文**，只返回数字和一行摘要 —— 要细节调度器
自己去 read_file，坐标本模块会给。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.prereg_commitments import ClosureTally, closure_tally
from shared.lib.artifact_policy import (
    carries_discharge_ledger,
    evidence_record_types,
    is_evidence_record,
)


@dataclass(frozen=True)
class EvidenceInventory:
    """盘上有多少证据 —— 按类型分，权威记录与支撑材料分开。"""

    #: 权威研究执行记录（experiment_log 等）：类型 -> 份数
    records: dict[str, int]
    #: 能勾账但非权威记录的支撑材料（clean_results 等）份数
    supporting: int
    #: KB 里带外部锚点（DOI/arXiv/PMID 等）的 claim 数
    anchored_claims: int

    @property
    def record_total(self) -> int:
        return sum(self.records.values())

    @property
    def is_empty(self) -> bool:
        """一份权威记录都没有 —— 此时任何"裁决/写作"类派发都会撞空。"""
        return self.record_total == 0


@dataclass(frozen=True)
class ResearchSituation:
    """一次路由决策需要的全部机械事实。"""

    goal: str | None
    closure: ClosureTally | None
    evidence: EvidenceInventory
    research_state_path: str | None
    research_state_verdict: str | None
    #: 用户交来的文件有几份、放在哪个绝对目录里。
    #:
    #: 为什么这条也进"局面"：`user_files` hook 只在**新增时**注入，而局面段是
    #: 每条用户消息前覆盖式重算的 —— 压缩掉一次注入之后，只有这里还记得盘上
    #: 有用户给的文件。一个模型忘掉用户传过文件，跟没传过没有区别。
    user_file_count: int = 0
    user_files_dir: str | None = None


def _evidence_inventory(state: Any) -> EvidenceInventory:
    """盘点证据 —— 类型性质查注册表，不认节点名也不写死类型名单。"""
    records: dict[str, int] = {}
    supporting = 0
    try:
        for artifact in state.list_artifacts() or []:
            artifact_type = str(artifact.get("type") or "")
            if is_evidence_record(artifact_type):
                records[artifact_type] = records.get(artifact_type, 0) + 1
            elif carries_discharge_ledger(artifact_type):
                supporting += 1
    except Exception:
        # 盘点不出来不该让整轮注入失败：返回空盘点，调度器会看到"零证据"并
        # 据此谨慎行事 —— 与真的零证据同一个方向，不会误导成"证据充足"。
        pass

    return EvidenceInventory(
        records=records,
        supporting=supporting,
        anchored_claims=_anchored_claim_count(state),
    )


def _anchored_claim_count(state: Any) -> int:
    """KB 里带外部锚点的 claim 数 —— 取证类研究的"库存"就是它。

    只数带 sources 的：没有外部锚点的 claim 核验不了，计入库存会把"看着有货"
    和"真有货"混为一谈。
    """
    try:
        from core import api as harness_api

        project_id = getattr(state, "project_id", None)
        if not project_id:
            return 0
        claims = harness_api.kb_list("claims", project_id=project_id, limit=500)
    except Exception:
        return 0

    count = 0
    for claim in claims or []:
        if isinstance(claim, dict) and (claim.get("sources") or []):
            count += 1
    return count


def compute_situation(
    state: Any,
    *,
    research_state_path: str | None = None,
    research_state_verdict: str | None = None,
) -> ResearchSituation:
    """算出本轮路由需要的全部机械事实。

    `research_state_*` 由调用方传入（hook 那边已经解析过一次），避免同一份文件
    在一轮里被找两遍。
    """
    from core.research_intake import load_intake

    goal = None
    try:
        intake = load_intake(getattr(state, "project_root", None))
        if isinstance(intake, dict):
            # 字段名以 `core.research_intake` 写入的那份为准：`original_text`
            # 加一条 `amendments` 修订链。**取修订链末尾**：用户后来说的话才是
            # 当前目标，拿原始那句去路由等于对着过期的意图做决策。
            #
            # ⚠️ 这里最初按猜的字段名写成 `text`，于是目标行永远是空的、注入里
            # 根本不出现 —— 而单测里我自己造的假数据也叫 `text`，绿得毫无意义。
            # 2026-08-19 真跑一次才现形。
            amendments = intake.get("amendments") or []
            latest = ""
            if isinstance(amendments, list) and amendments:
                last = amendments[-1]
                if isinstance(last, dict):
                    latest = str(last.get("text") or "")
            raw = (latest or str(intake.get("original_text") or "")).strip()
            goal = " ".join(raw.split()) or None
    except Exception:
        goal = None

    worktree = getattr(state, "project_worktree", None)
    user_files: list = []
    if worktree is not None:
        try:
            from core import materials

            user_files = materials.inventory(worktree)
        except Exception:
            user_files = []

    return ResearchSituation(
        goal=goal,
        closure=closure_tally(state),
        evidence=_evidence_inventory(state),
        research_state_path=research_state_path,
        research_state_verdict=research_state_verdict,
        user_file_count=len(user_files),
        user_files_dir=(
            str(user_files[0].absolute_path.parent) if user_files else None
        ),
    )


def render_situation_facts(situation: ResearchSituation) -> list[str]:
    """渲染成注入行。只有计数和单行摘要 —— 见模块文档的上下文约束。"""
    lines: list[str] = []

    if situation.goal:
        goal = situation.goal
        if len(goal) > 120:
            goal = goal[:117].rstrip() + "…"
        lines.append(f"- 🎯 用户目标：{goal}")

    closure = situation.closure
    if closure is not None:
        detail = f"- 📊 承诺账：{closure.fulfilled}/{closure.total} 条已兑现"
        if closure.open_total:
            detail += (
                f"；还欠 {closure.open_total} 条"
                f"（数值条 {closure.open_numeric} / 陈述条 {closure.open_statement}）"
            )
        else:
            detail += "（全部兑现）"
        lines.append(detail)
        if closure.all_open_are_statements:
            lines.append(
                "  ⚠️ 还欠的账**全是陈述条** —— 这类条目靠系统性取证与论证兑现，"
                "不是靠新跑一次计算实验。派发前先确认这一趟真需要算东西。"
            )

    if situation.user_file_count and situation.user_files_dir:
        lines.append(
            f"- 📎 用户交来 {situation.user_file_count} 份文件，"
            f"在 `{situation.user_files_dir}`（绝对路径，read_file / run_bash 直接用）。"
            "别叫用户再把文件放到别处。"
        )

    evidence = situation.evidence
    if evidence.is_empty:
        lines.append(
            "- 📭 盘上**零份**研究执行记录"
            f"（支撑材料 {evidence.supporting} 份，带锚点 claim {evidence.anchored_claims} 条）。"
            "此时派裁决/写作类工作会撞空，先把证据备上。"
        )
    else:
        summary = "、".join(
            f"{name} × {count}" for name, count in sorted(evidence.records.items())
        )
        lines.append(
            f"- 📚 证据库存：{summary}"
            f"；支撑材料 {evidence.supporting} 份，带锚点 claim {evidence.anchored_claims} 条"
        )

    return lines


def known_evidence_record_types() -> tuple[str, ...]:
    """转发注册表 —— 让调度器侧的报错文案能列出合法类型，不必自己维护名单。"""
    return evidence_record_types()
