"""infeasible 任务的一等公民出口 —— 声明协议 + 检测（2026-07-09，架构修复 D）。

## 背景（实测根因）

experiment 节点收到一个无法真正执行的任务（prereg 要真 benchmark trace + 人工
标注，环境里都没有）后，**静默替换设计**：造了一批"按假设设计好 ground truth"
的 synthetic 数据跑完，还基于它翻了 hypothesis claim 的 status —— 循环验证。
harness rules 里明明写着"不得自行降级需求…必须 request_human_input"，但那是
prompt 文字，零机械后果。

正确行为链（用户定义）：probe 环境（硬件/软件真的不够吗）→ 尝试获取（装软件/
生成或找上游要数据）→ 拿不准就 request_human_input → **穷尽后才判 infeasible，
且必须显式声明、把球踢回上游降级设计** —— 而不是换个能跑的实验糊弄。

## 声明协议（node → 框架）

节点确认任务不可执行时，在产出 artifact（如 experiment_log）上声明：

  metadata:
    infeasible: true                  # 必须
    infeasible_reason: "<缺什么、试过什么获取手段、为什么不行>"
    redirect_target: "hypothesis"     # 可选；建议回哪个上游节点降级设计（默认 hypothesis）

  或在 content 里写显式段落（LLM 更容易做到）：
    ## Feasibility（或 ## 可行性）
    verdict: infeasible
    ...

## 框架机械后果（本模块被两处消费）

  1. decision_package：产出声明 infeasible → 推荐动作**机械强制 REDIRECT**
     （不看 reviewer 平均分、不许 PROCEED）—— "不能执行"从此有合法出口，
     且必然流回上游，不会静默消失。
  2. update_claim_status：本 run 的 experiment_log 声明 infeasible → 禁止
     validated/refuted 翻转（防"换成 synthetic 数据然后循环证伪"）。

纯函数、无 I/O 依赖（读 artifact 走传入的 state）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from shared.lib.artifact_text import artifact_text

log = logging.getLogger("feasibility")

# content 声明：Feasibility/可行性 段落标题 + infeasible 判定词
_FEASIBILITY_HEADER_RE = re.compile(
    r"(?im)^\s*#{1,4}\s*(?:\d+[\.、]?\s*)?(Feasibility|可行性)\b")
_INFEASIBLE_VERDICT_RE = re.compile(
    r"(?i)\binfeasible\b|不可执行|无法执行|不可行")


def artifact_declares_infeasible(rec: dict | None) -> bool:
    """单个 artifact record 是否声明了 infeasible（metadata 标志 或 content 段落）。"""
    if not rec:
        return False
    md = rec.get("metadata") or {}
    if md.get("infeasible") is True:
        return True
    content = artifact_text(rec)
    m = _FEASIBILITY_HEADER_RE.search(content)
    if not m:
        return False
    # 只在 Feasibility 段落附近找判定词（段头后 600 字符内），避免全篇误扫
    window = content[m.end():m.end() + 600]
    return bool(_INFEASIBLE_VERDICT_RE.search(window))


#: 声明里没给目标时的落点。它必须自己先满足"能接 REDIRECT"这个条件。
_DEFAULT_REDIRECT_TARGET = "hypothesis"


def _target_that_can_take_the_handoff(declared: Any) -> str:
    """把声明里那个目标收敛成一个**真能接住 REDIRECT** 的节点。

    `redirect_target` 是**模型写在 artifact metadata 里**的值，所以它可以是任何
    字符串 —— 包括一个服务节点（`post_run_flow: none`）。而 REDIRECT 给服务节点
    的义务在账本上永远关不掉、也不被空转熔断看见（2026-09-17 实测空转 40 轮，
    见 core/loader.node_owes_post_node_flow）。

    这里不降级成 REVISE —— 那会把"任务不可执行、去上游改设计"变成"原样重跑一遍"，
    正是 infeasible 这条路当初要避免的。回落到既有默认值，语义原样保留、且执行得了。
    """
    from core.loader import node_owes_post_node_flow

    name = str(declared or "").strip()
    if name and node_owes_post_node_flow(name):
        return name
    if name:
        log.warning(
            "infeasible 声明的 redirect_target=%r 接不住 REDIRECT（服务/系统节点），"
            "回落到 %r", name, _DEFAULT_REDIRECT_TARGET)
    return _DEFAULT_REDIRECT_TARGET


def find_infeasibility_declaration(
    state: Any, artifact_ids: list[str] | None = None,
) -> dict | None:
    """在（指定的或全部）本 run artifact 里找 infeasible 声明。

    返回 {"artifact_id", "reason", "redirect_target"} 或 None。
    artifact_ids 为 None 时扫 state 里全部 artifact（update_claim_status 守卫用）。
    """
    if artifact_ids is None:
        artifact_ids = [a["id"] for a in state.list_artifacts()]
    for aid in artifact_ids:
        rec = state.read_artifact(aid)
        if rec is None or not artifact_declares_infeasible(rec):
            continue
        md = rec.get("metadata") or {}
        return {
            "artifact_id": aid,
            "reason": str(md.get("infeasible_reason") or "")[:300],
            "redirect_target": _target_that_can_take_the_handoff(
                md.get("redirect_target")),
        }
    return None
