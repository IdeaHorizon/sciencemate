"""present_decision_package —— post-producing decision point。

每个 producing 节点完后，orchestrator 走顺序：
  1. run_node(_reviewer, ...)
  2. run_node(_curator, ...)
  3. present_decision_package(...)  ← 本工具
      → unwind 给 chat.py 渲染 ASCII 报告 + 4 个选项给 user
      → user 输入 1-4 或 auto-approve 倒计时自动选 recommended

工具本身**不阻塞 stdin**：生成结构化 pause_event，metadata 含
`type='decision_package'` + 推荐选项 index，chat.py 检测后特化处理。
"""
from __future__ import annotations

import json
import json as _json
import os
from pathlib import Path
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.decision_offer import Offer, PAUSE_OFFER_KEY, Rejection, resolve_answer
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── review 机械门（2026-07-09，架构级根因修复）─────────────────────────────────
#
# 修两处 fail-open / 无机械约束：
#   B. 红线一票否决：reviewer 的 verdict 本是 8 维平均整数取整，"1分=critical 红线"
#      只是 review_spec 里的**文字**、没有机制。于是 execution_completeness=1（连实验
#      都没做）被 honesty=5/deviation=5 平均稀释成 overall 3 → approve_with_revisions
#      → 推荐 PROCEED。现在：只要 critique 报了 critical concern 或任一维度分触底
#      （≤1），机械否决 PROCEED，不看平均分。
#   C. fail-closed：reviewer 没产出可解析 critique 时，旧逻辑默认推荐 PROCEED
#      （fail-open，等于无质量信号直接放行）。改为默认 REVISE。
#
# 注意区分"review 失败"（fail-closed → REVISE）与"owner opt-out 跳过 review"
# （合法，PROCEED 不变）。

_CRITICAL_SCORE_FLOOR = 1     # per_dimension_score ≤ 此值视为红线（spec 里 1 就是失败档）
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _critique_from_metadata(rec: dict | None) -> dict | None:
    """content 不可解析时，从 artifact metadata 重建最小可用 critique（#202 D）。

    `nodes/_reviewer/harness.yaml` 明确要求 reviewer 在 metadata 写
    verdict / recommended_action / n_concerns，理由原文就是"让 orchestrator
    decision package 能直接读"。但消费方一直只认 content —— 生产方被强制写的
    字段，消费方从不使用。实测：reviewer 把 critique 写成 Markdown（metadata
    完全合规）→ 整份 review 被判不可用。

    只在**两个关键字段至少有一个**时返回；否则 None（继续 fail-closed）。
    """
    if not rec:
        return None
    md = rec.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(md, dict):
        return None

    verdict = md.get("verdict")
    action = md.get("recommended_action")
    if isinstance(action, dict):          # 已是结构化形态
        action_str = action.get("action")
        target = action.get("target_node")
    else:
        action_str = action
        target = md.get("recommended_target_node") or md.get("target_node")
    if not verdict and not action_str:
        return None

    out: dict[str, Any] = {
        "verdict": verdict,
        "_source": "metadata_fallback",   # 审计：这份不是从 content 解析来的
    }
    if action_str:
        out["recommended_action"] = {"action": str(action_str),
                                      "target_node": target}
    n_concerns = md.get("n_concerns")
    if isinstance(n_concerns, int) and n_concerns > 0:
        # 只有计数、没有明细 —— 如实表达为占位，别伪造 concern 内容。
        out["concerns"] = [{
            "severity": "unknown",
            "summary": (f"critique content 不可解析；metadata 记录 {n_concerns} "
                        f"条 concern，明细请直接看 artifact 原文。"),
        }]
    return out


def _parse_critique_json(content: str) -> dict | None:
    """鲁棒解析 review_critique JSON：直接 loads → 去 ```json 围栏 → 首{到末}。

    reviewer（弱端点）偶尔把 critique 包在代码块里或带前后缀噪音；直接 json.loads
    失败就整份 review 丢掉太脆。多试几种提取，全失败才返 None。"""
    if not content or not content.strip():
        return None
    candidates = [content]
    m = _JSON_FENCE_RE.search(content)
    if m:
        candidates.append(m.group(1))
    i, j = content.find("{"), content.rfind("}")
    if i != -1 and j > i:
        candidates.append(content[i:j + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _red_line_reason(critique: dict) -> str | None:
    """返回红线原因字符串（None=无红线）。机械依据，不看 overall 平均分：
      ① 任一 severity=critical 的 concern，或 n_critical_concerns≥1；
      ② 任一 per_dimension_score ≤ _CRITICAL_SCORE_FLOOR。"""
    for c in (critique.get("concerns") or []):
        if isinstance(c, dict) and str(c.get("severity", "")).lower() == "critical":
            return f"critical concern：{str(c.get('description', ''))[:120]}"
    try:
        if int(critique.get("n_critical_concerns") or 0) >= 1:
            return f"n_critical_concerns={critique.get('n_critical_concerns')}"
    except (TypeError, ValueError):
        pass
    scores = critique.get("per_dimension_scores") or {}
    if isinstance(scores, dict):
        floored = sorted(k for k, v in scores.items()
                         if isinstance(v, int | float) and not isinstance(v, bool)
                         and v <= _CRITICAL_SCORE_FLOOR)
        if floored:
            return f"维度触底(≤{_CRITICAL_SCORE_FLOOR})：{', '.join(floored)}"
    return None


def _apply_critical_veto(
    critique: dict, action: str, target_node: str | None,
) -> tuple[str, str | None]:
    """B：红线机械否决。有红线且当前推荐是 proceed → 强制降级（有 redirect target
    则 redirect_upstream，否则 revise）；已经是 revise/redirect/abort/escalate 的
    不改（都不会静默放行）。返回 (action, override_note)。"""
    red = _red_line_reason(critique)
    if not red:
        return action, None
    if action == "proceed":
        new_action = "redirect_upstream" if target_node else "revise"
        return new_action, (
            f"⚠️ 框架否决：存在红线（{red}）——不受 overall 平均分影响，"
            f"不能直接 PROCEED，已降为 {new_action.upper()}。"
        )
    return action, f"⚠️ 红线（{red}）——推荐已是 {action.upper()}，不放行。"


# ── ASCII 渲染 ──────────────────────────────────────────────────────────────
#
# v0.5 起 5 个 option（v0.4 是 4 个）。新增 [3] REDIRECT 给"诊断到根因在上游"
# 的场景：reviewer 判断 source_node 输出不达标的根因不在 source 自己（信息已
# 用尽），而在更上游 X（需要 X 补一段 focused 工作）。orchestrator 收到 REDIRECT
# → 调 X with focused query → X 走完整 post-producing flow → 再回头重跑 source。
#
# 设计原则：producing 节点保持纯 worker（不思考调度），reviewer 出诊断，
# orchestrator 路由。

_OPTION_LABELS_BASE = [
    "PROCEED to next stage",
    "REVISE (re-run source_node with reviewer feedback)",
    "REDIRECT to upstream (re-run an earlier node with focused query, then re-run source_node)",
    "ABORT pipeline",
    "EDIT manually then proceed (pauses for you to edit artifact / KB)",
]


# ── #155：review 失败时的专用选项集 ────────────────────────────────────────
# 根因（qinp #155 现象 1）：review 挂了（截断/超时/5xx/无 critique）时，旧代码仍
# 显示固定 5 选项、推荐 REVISE，只把"优先 reviewer-only retry"藏在 feedback 文本
# 里 —— 用户只能靠 free text 表达"只重跑 reviewer"，再指望 orchestrator 理解。
# RETRY REVIEWER（复用已合格的 producer，只重跑 _reviewer）和 REVISE SOURCE
# （重跑 producing 节点）是两个完全不同的动作，必须各自有结构化选项。
# 且此时**不提供 PROCEED**：没有有效 critique 就放行 = 静默跳过独立审查。
_OPTION_LABELS_REVIEW_FAILED = [
    "RETRY REVIEWER (re-run _reviewer only; reuse the same completed producer)",
    "REVISE (re-run source_node with feedback)",
    "REDIRECT to upstream (re-run an earlier node, then re-run source_node)",
    "ABORT pipeline",
    "EDIT / MANUAL REVIEW (pauses for you to inspect artifact yourself)",
]
# 人工选择 → 规范动作名（pause_driver 机械记账用）
_REVIEW_FAILED_ACTIONS = ["retry_reviewer", "revise", "redirect_upstream", "abort", "edit"]
_NORMAL_ACTIONS = ["proceed", "revise", "redirect_upstream", "abort", "edit"]

#: Analysis 节点的物理 node_type（Phase 5 改名前仍叫 hypothesis）。
_ANALYSIS_NODE_TYPE = "hypothesis"



def _producer_was_truncated(state: Any, producing_run_id: str) -> dict | None:
    """producer 这一轮是**被轮次上限切断**的吗？是就返回它的 summary。

    读 `summary.json` 里框架自己写的 `stop_reason` / `max_turns` —— 不是模型
    自述。模型说"我做完了"和它其实是被切断，在 transcript 里长得一模一样。

    读不到就返回 None（当作没被截断）：把正常完成误判成残缺会让流程无限
    revise，比漏判贵。
    """
    if not producing_run_id:
        return None
    try:
        from core.paths import runs_parent

        base = runs_parent(getattr(state, "project_id", None))
        path = Path(base) / str(producing_run_id) / "summary.json"
        if not path.exists():
            return None
        summary = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    from core.executor import truncated_by_turn_cap

    return summary if truncated_by_turn_cap(summary) else None


def recommended_action_for_truncated(truncated: bool, current: str) -> str:
    """producer 是被轮次上限切断的 → 推荐 `revise`（带反馈重跑它）。

    菜单里三套动作都没有"接着把这个节点跑完"，而 `revise` 就是那条路 ——
    只是决策层此前不知道被截断，从来没推荐过它，于是在 retry_reviewer 和
    retry_curator 之间来回（E2E v23 实测空转三轮，每轮约一小时）。

    没被截断时**一个字不动**别人的判断：这里补的是一个缺失的事实，
    不是又一套推荐逻辑。
    """
    if not truncated:
        return current
    return "revise"

def _defers_to_analysis(source_node_type: str) -> bool:
    """source 节点的 harness 是否声明 post_run_flow: review_curate。

    权威是 harness 声明本身 —— 不是这里再列一张节点名单。名单式判断对下一个
    想用这档节奏的节点默认漏过。
    """
    from core.loader import load_harness

    try:
        return bool(load_harness(str(source_node_type)).defers_decision_to_analysis)
    except Exception:
        return False


# ── 授权的动作必须是**可执行**的（2026-09-17 yuankk 会话死循环）────────────
#
# 一条 REVISE/REDIRECT 授权的全部意义是"去起 target，起完这条 flow 就关了"。
# 而"起完能关"在 run_node 里不是自动成立的：绑定（entry → action_in_progress）、
# 空转计数（action_attempt_count）、闭合（_find_in_progress_entry）三件事全部挂在
# 同一个前提 `node_owes_post_node_flow(target)` 上。目标若不满足它 ——
#
#   绑定跳过 → 永不 action_in_progress → 既关不掉，也**不被空转熔断看见**
#
# 熔断器恰好在最需要它的那一档不在场：hook 每轮继续喊"去起 target"，模型每轮照做，
# 账本一动不动。yuankk 那条会话这样转了 40 轮，最后模型申报 blocked 又被
# "pending_post_node_flow 非空"驳回 —— 它被卡住的那件事，正是禁止它说自己被卡住的
# 那件事。
#
# 这里原先只挡住了"target 为空"一种不可执行，注释写的却是
# "Never create an unexecutable action_authorized state"。那句话对"target 是个不
# 进 flow 账本的节点"一字不差地成立 —— 护栏写成了名单，漏掉了同一类的另一半。
# 现在把判据摆正：**能不能关掉这条 flow** 才是唯一的问题，两种不可执行同一处答。


def _why_this_target_cannot_close_the_flow(target: object) -> str | None:
    """返回拒绝理由；`None` = 这个目标起完真能把 flow 关掉。"""
    from core.loader import list_harnesses, node_owes_post_node_flow

    name = str(target or "").strip()
    if not name:
        return (
            "⛔ 拒绝授权：REDIRECT 必须指名 recommended_target_node。"
            "没有目标的 action_authorized 是不可执行状态 —— 它会把 producer 门"
            "永久卡在 target=None 上。请补上目标节点，或改选 REVISE / ABORT。"
        )
    if not node_owes_post_node_flow(name):
        try:
            legal = sorted(n for n in list_harnesses() if node_owes_post_node_flow(n))
        except Exception:
            legal = []
        return (
            f"⛔ 拒绝授权：{name!r} 是**服务节点**（post_run_flow: none）或系统节点，"
            f"它跑完不进 post-node flow 账本 —— 起它一万次，这条 flow 也关不掉，"
            f"而且空转熔断看不见这种空转。\n\n"
            f"能关掉这条 flow 的目标只有：{legal or '（读不到节点清单）'}。\n"
            f"如果根因确实在 {name!r}，那不是一次 REDIRECT —— 由**产出节点自己**去"
            f"调用这个服务并重新交付（选 REVISE），或 ABORT 后重新编排。"
        )
    return None


# ── 选项集的唯一来源（2026-08-19 事故）────────────────────────────────────
#
# 此前一次呈递把选项集构造了**三遍**：决策包正文一遍（`_render_decision_package`
# 里调 `_build_option_labels`）、pause_event 一遍（本文件 return 处再调一遍）、
# 账本一遍（`presented_actions`）。而唯一的调用点**两处都没传 `curator_pending`**
# —— 两个人看的面都显示了框架已经撤下的 `[1] PROCEED to next stage ← recommended`，
# 账本的合法集却是 `[retry_curator, …]`。人点 PROCEED → 文案回传 → 拿去匹配另一套
# 动作名 → 认不出 → 静默 fail-closed → 同一张卡原地重现。实测循环三次。
#
# 三份抄件对齐第四次不解决问题。动作的**身份**从此只有一处声明：id → 文案。
# 选项集 = 一串 id；正文、pause、账本都是 `Offer` 的投影（见 core.decision_offer）。
_ACTION_CATALOG: dict[str, tuple[str, str]] = {
    "proceed": (
        "PROCEED to next stage",
        "接受本轮产物，进入下一个节点。",
    ),
    "retry_reviewer": (
        "RETRY REVIEWER (re-run _reviewer only; reuse the same completed producer)",
        "只重跑审查，复用已合格的 producer 产物。",
    ),
    "revise": (
        "REVISE (re-run source_node with reviewer feedback)",
        "带审查反馈重跑产出节点。",
    ),
    "redirect_upstream": (
        "REDIRECT to upstream (re-run an earlier node with focused query, then re-run source_node)",
        "根因在上游 —— 先让上游补一段工作，再回头重跑本节点。",
    ),
    "abort": (
        "ABORT pipeline",
        "终止本条研究流水线。",
    ),
    "edit": (
        "EDIT manually then proceed (pauses for you to edit artifact / KB)",
        "暂停，等你手工修改产物或 KB，之后再作决策。",
    ),
}


def build_decision_offer(
    *,
    decision_id: str,
    source_node_type: str,
    action_ids: list[str],
    recommended_action: str | None,
    redirect_target_node: str | None = None,
    facts: dict | None = None,
) -> "Offer":
    """一次呈递构造**一个** Offer —— 正文/pause/账本此后都只是它的投影。

    `recommended_action` 按 **id** 落位，不再按序号。序号当身份正是事故的另一半：
    curator-pending 集里 index 0 是 `retry_curator`，而
    `_recommended_index_from_action("proceed")` 也返回 0 —— 于是屏幕上那个
    "← recommended" 指向了 PROCEED，框架推荐的却是重跑 curator，两边都不报错。
    """
    from core.decision_offer import Choice, Offer

    choices = []
    for action_id in action_ids:
        label, description = _ACTION_CATALOG[action_id]
        if action_id == "redirect_upstream" and redirect_target_node:
            label = f"REDIRECT to upstream \u2192 '{redirect_target_node}'"
        choices.append(Choice(id=action_id, label=label, description=description))

    # 推荐项必须在集合里。不在（例如 review 挂了却推荐 proceed）→ 不编造，
    # 退回"没有推荐"，让人自己选；无人值守那条路另有 fail-closed 处理。
    rec = recommended_action if recommended_action in set(action_ids) else None
    return Offer(
        decision_id=decision_id,
        kind="decision_package",
        question=f"Post-node decision for {source_node_type}",
        choices=tuple(choices),
        recommended_id=rec,
        facts=dict(facts or {}),
    )


def _build_option_labels(redirect_target_node: str | None = None,
                          review_failed: bool = False,
) -> list[str]:
    """5 个 option；[3] REDIRECT 在 reviewer 推荐时填具体目标节点名。

    review_failed=True → RETRY-REVIEWER 打头、无 PROCEED 的专用集（#155）。
    review 优先：连有效审查都没有时，谈整合没有意义。
    """
    if review_failed:
        labels = list(_OPTION_LABELS_REVIEW_FAILED)
        if redirect_target_node:
            labels[2] = (f"REDIRECT to upstream → '{redirect_target_node}' "
                         "(then re-run source_node)")
        return labels
    labels = list(_OPTION_LABELS_BASE)
    if redirect_target_node:
        labels[2] = (
            f"REDIRECT to upstream → '{redirect_target_node}' "
            f"(re-run it with focused query, then re-run source_node)"
        )
    return labels


# ── 决策包的截断纪律 ────────────────────────────────────────────────
# 决策包是人做决定时唯一能看到的东西。任何在这里被静默丢掉的内容，都等于
# 「让人对他没看见的东西做决定」。2026-09-01 实测：产出节点写了四条假说的
# 终态，`splitlines()[:8]` 恰好切在「四条假说的终态：」这个标题之后，四条
# 裁决一条都没送到人眼前，而人看到的文本没有任何迹象表明它被切过。
#
# 所以这里的上限只允许**自报的**上限：可以截断（决策包必须有界），但截断
# 必须说出自己丢了多少，人才知道要不要去翻原始 artifact。
_TRUNC_LINE_CHARS = 500


def _capped_text(text: str, limit: int) -> str:
    """按字符截断并自报丢弃量。"""
    s = str(text)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…（此处截断，全文 {len(s)} 字）"


def _capped_lines(text: str, *, limit: int, indent: str) -> list[str]:
    """按行截断并自报丢弃量；过长的单行也自报。"""
    src = str(text).strip().splitlines()
    out = [f"{indent}{_capped_text(ln, _TRUNC_LINE_CHARS)}" for ln in src[:limit]]
    dropped = len(src) - limit
    if dropped > 0:
        out.append(f"{indent}… （另有 {dropped} 行未显示，完整内容见上面列出的 artifact）")
    return out


def _closure_ledger_lines(state: Any) -> list[str]:
    """预注册闭合账，渲成给**人**看的两三行。没有冻结预注册就返回空。

    只报四个数（总数 / 已兑现 / 其中定性降级 / 未兑现条目），外加欠账时的一句
    后果提示。逐条清单已经在 `render_commitment_brief` 里给模型了 —— 这里要的
    不是第二份清单，是让按下 PROCEED 的那个人在按之前看见那个分数。

    全程吞异常：决策包是人看审查结果的唯一入口，绝不能因为一个诊断算不出来
    而整张卡渲不出来。
    """
    try:
        from core.prereg_commitments import closure_tally

        tally = closure_tally(state)
    except Exception:
        return []
    if tally is None or not tally.total:
        return []

    lines = ["📋 预注册闭合账（冻结后不可改）"]
    head = f"  {tally.fulfilled}/{tally.total} 条已兑现"
    if tally.degraded:
        head += f"（其中 {tally.degraded} 条是**定性降级**，不是测得）"
    lines.append(head)
    for item in tally.degraded_items[:5]:
        lines.append(f"    🔻 {_capped_text(item, 200)}")
    if tally.open_total:
        for item in tally.open_items[:8]:
            lines.append(f"    ⬜ {_capped_text(item, 200)}")
        _more = tally.open_total - min(len(tally.open_items), 8)
        if _more > 0:
            lines.append(f"    … 另有 {_more} 条未兑现")
        lines.append(
            "  ⚠️ 还有未兑现的闭合条件。下游 writing 的输入门读的就是这本账 ——"
            "**现在 PROCEED，论文那一步会被拦回来**。兑现记录写在 experiment_log 的"
            " metadata：数值条 `measured_metrics`、陈述条 `closure_discharges`"
            "（discharged 必须挂 evidence）。"
        )
    return lines


def _render_decision_package(
    *,
    source_node_type: str,
    producing_run_id: str,
    producing_summary: str,
    artifact_ids_produced: list[str],
    curator_summary: str,
    review_critique_json: dict | None,
    review_failed_reason: str | None,
    offer: "Offer",
    recommended_feedback: str,
    recommended_target_node: str | None = None,
    framework_override_note: str | None = None,
    review_unusable: bool = False,
    closure_lines: list[str] | None = None,
    child_obligation_effect: dict | None = None,
) -> str:
    """构建 ASCII decision package 文本。

    review_unusable=True（#155）：**根本没有可用 critique** → 换 RETRY-REVIEWER
    打头、无 PROCEED 的选项集。仅 schema 瑕疵（critique 有）不算 unusable。
    """
    lines: list[str] = []
    sep = "═" * 66
    sub = "─" * 66

    lines.append(sep)
    lines.append(f"NODE COMPLETED: {source_node_type}  (run_id: {producing_run_id})")
    lines.append(sep)

    # 📦 produced
    lines.append("")
    lines.append("📦 Produced")
    if artifact_ids_produced:
        for aid in artifact_ids_produced[:30]:
            lines.append(f"  • {aid}")
        _more = len(artifact_ids_produced) - 30
        if _more > 0:
            lines.append(f"  … （另有 {_more} 个产物未列出）")
    else:
        lines.append("  (no artifacts)")
    if producing_summary:
        lines.extend(_capped_lines(producing_summary, limit=60, indent="    "))

    # 🎯 这一趟关掉了什么（#1097 第 4 条）
    #
    # 上面那段 `producing_summary` 是**自由文本**：它只该被看，不该被当判据。
    # 一个只装了环境、编译通过的 operation run，摘要读起来完全可以像"做完了"，
    # 而它的收尾收据里明写着 `upstream_goal_effect=operational_subtask_only`。
    # 人要在这张卡上做决定，那个机械事实就得摆在他眼前。
    if isinstance(child_obligation_effect, dict) and child_obligation_effect:
        from core.obligation_effect import (
            CONTRIBUTION_EVIDENCE, EFFECT_FULL,
        )

        _eff = str(child_obligation_effect.get("upstream_goal_effect") or "")
        _contrib = str(child_obligation_effect.get("scientific_contribution") or "")
        lines.append("")
        lines.append("🎯 这一趟对科学目标的效应（子节点收尾收据派生，非自由文本）")
        lines.append(f"  • upstream_goal_effect: {_eff or '（未报告）'}")
        lines.append(f"  • scientific_contribution: {_contrib or '（未报告）'}")
        if _eff and not (_eff == EFFECT_FULL and _contrib == CONTRIBUTION_EVIDENCE):
            lines.append(
                "  ⚠️ 它**没有**推进科学目标本身 —— 批准它不等于这个研究问题有了答案。")

    # 📋 预注册闭合账
    #
    # 与上面那条 `owner_spec_loaded=false` 同一个理由：**自报的事实必须有消费者**。
    # `ClosureTally` 的 docstring 写着它是"这项研究做到哪了的唯一机械答案"，
    # `render_commitment_brief` 每一轮都把逐条 ⬜/✅ 摆给**模型**看 —— 唯独拿决策
    # 的**人**看不见它。
    #
    # 2026-09-07 真机第五轮：experiment 冻结 experiment_log 时闭合账 0/8，节点照常
    # 收工、reviewer 给了 approve、我在这张卡上按了 PROCEED —— 屏幕上没有任何地方
    # 写着那个 0。三次派工之后 writing 才在入口门上把它拦下来，然后 REDIRECT 回
    # experiment 补账、再重跑 writing。**绕路的原因不是没人知道，是拿决策的人看不见。**
    if closure_lines:
        lines.append("")
        lines.extend(closure_lines)

    # 📊 curator
    lines.append("")
    lines.append("📊 KB Integration (from _curator)")
    if curator_summary:
        lines.extend(_capped_lines(curator_summary, limit=30, indent="  "))
    else:
        lines.append("  (curator skipped or no KB changes)")

    # 🔍 reviewer
    lines.append("")
    if review_failed_reason:
        lines.append("🔍 Review (from _reviewer)")
        lines.append(f"  ⚠️ review 失败/不可用: {_capped_text(review_failed_reason, 400)}")
        lines.append("  → fail-closed：默认推荐 REVISE（不在无质量信号时直接放行）")
    elif review_critique_json is None:
        lines.append("🔍 Review")
        lines.append("  (skipped — owner opted out via skip_post_node_review)")
    else:
        verdict = review_critique_json.get("verdict", "?")
        confidence = review_critique_json.get("confidence", "?")
        lines.append("🔍 Review (from _reviewer)")
        lines.append(f"  Verdict: {verdict}  (confidence: {confidence})")
        # 自报失效必须有消费者：critique 逐条如实记着 owner_spec_loaded，却
        # 从来没人读 —— 于是"owner 判据从未生效"跨越全部历史 E2E 无人发现。
        # 决策包是人看审查结果的唯一入口，就在这里亮出来。
        _rubric = review_critique_json.get("rubric_source") or {}
        if isinstance(_rubric, dict) and _rubric.get("owner_spec_loaded") is False:
            lines.append(
                "  ⚠️ 本次审查**未按 owner 的 review_spec**（rubric_source."
                "owner_spec_loaded=false）—— 用的是通用 rubric，节点特有判据"
                "（阈值依据/任务退化/定义锁定等）可能没有被检查。")
        strengths = review_critique_json.get("strengths") or []
        if strengths:
            lines.append("")
            lines.append("  Strengths")
            for s in strengths[:5]:
                lines.append(f"    ✓ {_capped_text(s, 400)}")
            if len(strengths) > 5:
                lines.append(f"    … （另有 {len(strengths) - 5} 条未列出）")
        concerns = review_critique_json.get("concerns") or []
        if concerns:
            lines.append("")
            lines.append(f"  Concerns ({len(concerns)} total)")
            # critical 优先
            sev_order = {"critical": 0, "major": 1, "minor": 2}
            _ordered = sorted(concerns, key=lambda x: sev_order.get(x.get("severity"), 3))
            for c in _ordered[:12]:
                sev = c.get("severity", "?")
                desc = _capped_text(c.get("description", ""), 600)
                lines.append(f"    [{sev}] {desc}")
            if len(_ordered) > 12:
                lines.append(f"    … （另有 {len(_ordered) - 12} 条未列出）")
        # reviewer 自报的 action 仅供参考；实际推荐以框架否决后的
        # recommended_action_index 为准（B：红线不受平均分稀释）。
        eff_action = (offer.recommended_id or "proceed").split("_")[0]
        lines.append("")
        if eff_action == "redirect" and recommended_target_node:
            lines.append(f"  Recommended: REDIRECT to upstream → '{recommended_target_node}'")
            lines.append(f"    （诊断：source_node 已尽力，根因在上游 {recommended_target_node}）")
        else:
            lines.append(f"  Recommended: {eff_action.upper()}")
        if recommended_feedback:
            label = ("Focused query for upstream" if eff_action == "redirect"
                    else "Feedback for re-run")
            lines.append(f"    {label}:")
            lines.extend(_capped_lines(recommended_feedback, limit=30, indent="      "))

    # 框架 override（红线否决 / infeasible 强制 REDIRECT）——不依赖 review 分支，
    # review 失败/跳过时（如 infeasible + review 崩）也必须展示。
    if framework_override_note:
        lines.append("")
        lines.append(f"  {framework_override_note}")

    # ── 选项区 ──
    # 这里**不再构造选项集**：正文、pause、账本同读一个 offer。此前这行自己调
    # `_build_option_labels`，而调用点从没传 `curator_pending` —— 屏幕上于是
    # 出现了框架已经撤下的 PROCEED，还带着 "← recommended"。
    lines.append("")
    lines.append(sub)
    lines.append("What next?")
    if "proceed" not in offer.choice_ids():
        # 为什么没有 PROCEED，要说在它该出现的位置上 —— 人才不会去别处找它。
        if review_unusable:
            lines.append("  （review 未产出有效 critique → 本轮**不提供 PROCEED**：")
            lines.append("    没有独立审查信号就放行 = 静默跳过质量门。）")
        else:
            lines.append("  （curator 尚未把这些 artifact 整合进 KB → 本轮**不提供 "
                         "PROCEED**：")
            lines.append("    PROCEED 会关闭本轮 flow，连带解除"
                         "\"整合未完成\"这道下游门禁。）")
    lines.append(offer.to_ascii_options())
    lines.append("")
    lines.append(f"Enter 1-{len(offer.choices)} to choose (or free text for context to add).")
    lines.append("If auto-approve is ON, recommended option will execute after 5s countdown.")
    lines.append(sep)

    return "\n".join(lines)


def _recommended_index_from_action(action: str) -> int:
    """把 review.recommended_action 转成 0-indexed option (v0.5 起 5 个)."""
    return {
        "proceed":             0,   # → [1] PROCEED
        "revise":              1,   # → [2] REVISE source_node
        "redirect_upstream":   2,   # → [3] REDIRECT to upstream (v0.5 新)
        "abort":               3,   # → [4] ABORT
        "escalate_to_human":   4,   # → [5] EDIT manually
    }.get((action or "proceed").lower(), 0)


# ── 工具实现 ────────────────────────────────────────────────────────────────

# 同一个 producer run 允许的 reviewer 重试次数上限。见 record_decision_answer
# 里的 E2E-4 活锁说明。
_RETRY_REVIEWER_MAX = int(os.getenv("HARNESS_RETRY_REVIEWER_MAX", "2") or 2)


def _durable_retry_attempts(state: Any, producing_run_id: str) -> int:
    """从 transcript 读"这个 producer run 已经重审到第几次" —— 磁盘上的事实。

    **读数字，不数条数**：授权/封顶事件自带权威计数

        reviewer_retry_authorized_by_human  attempt_number
        reviewer_retry_capped               attempts

    数条数会漏（事件写在哪一轮、有没有被压缩掉都会影响条数），读数字不会：
    哪怕只剩最后一条，它带的也是当时的累计值。实测这份 transcript 里两条事件
    都写着 2，而重启后内存是 0。

    orchestrator 的 transcript **按会话存、跨 run 追加**（同一份文件里同时有
    重启前后的事件），所以它答得了"总共几次"，而 `hook_state` 答不了。

    只解析含标记串的行 —— 决策时才调，长会话也扛得住。
    """
    if not producing_run_id:
        return 0
    path = getattr(state, "transcript_path", None)
    if not path:
        return 0
    best = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "reviewer_retry_" not in line or str(producing_run_id) not in line:
                    continue
                try:
                    record = _json.loads(line)
                except ValueError:
                    continue
                if str(record.get("producing_run_id") or "") != str(producing_run_id):
                    continue
                event = record.get("event")
                if event == "reviewer_retry_authorized_by_human":
                    best = max(best, int(record.get("attempt_number") or 0))
                elif event == "reviewer_retry_capped":
                    best = max(best, int(record.get("attempts") or 0))
                elif event == "reviewer_retry_started":
                    # 老事件不带数字 —— 退回数条数，聊胜于无
                    best = max(best, 1)
    except (OSError, ValueError, TypeError):
        return 0
    return best


def _effective_retry_attempts(state: Any, entry: dict) -> int:
    """本 producer run 已授权的 reviewer 重试次数 —— 内存与磁盘取较大值。

    ## 为什么不能只信内存（E2E v23，2026-08-11 实测）

        04:21:55  review_retry_capped: **true**（选项集因此含 PROCEED）
        04:47     重启后端部署修复
        05:29:23  review_retry_capped: **false**，选项退回无 PROCEED
                  → 自动批准选 1 = 再审一遍同一份产物

    计数存在 `hook_state["pending_post_node_flow"]` 里，那是进程内存。而进程
    被换掉的场合恰恰是崩溃 / 挂起 / 部署修复 —— 正是最需要熔断器记事的时候。

    这段代码的注释记着上一次同款教训（"轮间熔断够不着主路径，所以上限必须钉
    在授权这一刻"）。**时机改对了，耐久性没改。**

    ## 为什么取较大值而不是直接用磁盘

    磁盘读不动 / 事件没写全时，直接用磁盘会把上限**调低**（0 次），等于熔断器
    静默失效。取 max：磁盘只会把次数补回来，不会把它抹掉。
    """
    memory = int(entry.get("review_attempt_count") or 0)
    durable = _durable_retry_attempts(state, str(entry.get("producing_run_id") or ""))
    return max(memory, durable)



_ABSENT_SENTINELS = frozenset({
    "", "null", "none", "nil", "nan", "n/a", "na", "-", "undefined", "false",
})


def _normalize_absent(value: Any) -> str | None:
    """把"我没有值"的各种写法统一成 None。

    LLM 通过 JSON schema 传参时，"没有"经常变成字面量 null / 字符串 "none" /
    "N/A"。前者到 Python 是 None（对），后者是**非空字符串**（truthy），于是
    `if not value` 这类判断全部失效。这个坑一旦出现在门禁的输入上，就是把管线
    钉死（见调用处的 E2E-4 现场说明）。
    """
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in _ABSENT_SENTINELS else s


# ── review 门的独立性保证（issue #202）──────────────────────────────────────
#
# 这道门存在的**唯一理由**是"产出经过独立审查"。qinp 2026-07-28 实测：5 次审稿
# 全部失败（blank-out / args 截断 ×3 / Markdown 非 JSON）后，orchestrator 自己
# 调 save_artifact 写了一份 review_critique，门随即打开、菜单从 review-failed
# 专用集（无 PROCEED）切成普通集并推荐 [1] PROCEED。
#
# 框架此前有三层都没拦住：
#   1. save_artifact 的 producing-deliverable guard 只覆盖 producing 节点的产出
#      （_producing_output_owners 显式跳过 `_` 开头的架构节点），review_critique
#      是 _reviewer 的产出 → 不在表内 → orchestrator 自产畅通无阻。
#      （对照：它自己写 manuscript 会被当场拒绝 —— 漏的恰恰是最该防的那个。）
#   2. 渲染选项集时读的是**调用方传参** review_critique_artifact_id，而不是账本
#      的 review_state —— 账本明写 failed_awaiting_human / critique_id=null，
#      菜单却给了 PROCEED。
#   3. record_decision_answer 的 proceed 分支不校验 review_state 就把 flow 整条
#      出列 → 下游 producing-node gate 再无条目可拦 → 全链放行。
# 本轮纯靠操作者没按 PROCEED 才没实际突破。那是运气，不是机制。
_REVIEWER_NODE_TYPE = "_reviewer"


def _flow_entry_for(state: State, producing_run_id: str) -> dict | None:
    for e in (state.hook_state.get("pending_post_node_flow") or []):
        if e.get("producing_run_id") == producing_run_id:
            return e
    return None


def _run_started_at(run_id: str) -> "datetime | None":
    """run_id 的前缀就是开跑纪元秒（core/state.py 的唯一生成处）。

    解析不出来就返回 None —— 判据宁可不生效，也不能因为 id 换了形状就误判。
    """
    head = str(run_id or "").split("-", 1)[0]
    if not head.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(head), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _artifact_created_at(state: State, artifact_id: str) -> "datetime | None":
    rec = state.read_artifact(artifact_id)
    if not isinstance(rec, dict):
        return None
    raw = str(rec.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        made = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return made if made.tzinfo else made.replace(tzinfo=UTC)


def critique_provenance_problem(rec: dict | None) -> str | None:
    """这份 review_critique 是不是合格 reviewer 产出的？不合格返回原因。

    判据是 artifact metadata 里的 `produced_by_node_type`（core.state.save_artifact
    在唯一写入收口处盖章，绕不过）。**缺章的旧 artifact 一律放行** —— 本改动之前
    存的没有这个字段，硬拦会把历史 run 全判死；而所有新写入都必带章，真实洞
    （被审查方自产）照样堵死。
    """
    if rec is None:
        return None
    md = rec.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (json.JSONDecodeError, TypeError):
            md = {}
    md = md if isinstance(md, dict) else {}

    # 解析优先级（**绝不能读 metadata.source_node_type**，见下）：
    #   1. metadata.produced_by_node_type —— `_import_required_outputs` 强制
    #      写入的**真实产出方**（子 run 的 node_type）。回填走 parent 的
    #      save_artifact，顶层框架章会是 parent（_orchestrator），所以导入件
    #      必须靠这个键才判得对。
    #   2. 顶层 produced_by_node_type —— core.state.save_artifact 在写入收口
    #      盖的章。直写件用它（正是"orchestrator 自己写一份"那条路径）。
    #
    # ⚠️ 历史坑（PR#209 引入、2026-07-30 实测暴露）：这里原本第一优先读
    # `metadata.source_node_type`。但那个键在两处含义**相反** —— reviewer 的
    # harness 契约要求 critique 的 metadata 里写"**被审查对象**的来源节点"
    # （审 literature 的产物就写 literature）。于是 5 次成功审稿全被判成
    # "由 literature 产出、不是独立 reviewer"，强制 retry_reviewer 白烧 token。
    # 框架自有的 provenance 必须用不与节点契约撞车的键名。
    producer = (md.get("produced_by_node_type")
                or rec.get("produced_by_node_type"))
    if not producer:
        return None                      # 旧 artifact 无出处信息 → 不拦（迁移安全）
    if producer != _REVIEWER_NODE_TYPE:
        return (
            f"review_critique 由 {producer!r} 产出，不是独立 reviewer "
            f"（{_REVIEWER_NODE_TYPE!r}）—— 被审查方或编排方自己写的审查报告"
            f"不能解除 review 门。请 run_node(node_type='_reviewer') 真正跑一次审查。"
        )
    return None


async def _present_decision_package(
    state: State,
    source_node_type: str,
    producing_run_id: str,
    producing_summary: str = "",
    artifact_ids_produced: list[str] | None = None,
    curator_summary: str = "",
    review_critique_artifact_id: str | None = None,
    review_failed_reason: str | None = None,
    child_obligation_effect: dict | None = None,
    **_: Any,
) -> dict:
    """生成 decision package pause 事件。

    优先读 review_critique artifact 拿结构化字段；失败则用 review_failed_reason
    走 fallback 渲染。
    """
    # v3.8（E2E-4 实测死循环）：LLM 常把"没有失败原因"写成 JSON 字面量 null /
    # 字符串 "none" / "N/A"，到这里就是**非空字符串**，`not review_failed_reason`
    # 为假 → critique artifact 根本不被读 → review_unusable=True → 推荐
    # RETRY REVIEWER（该选项集不含 PROCEED）→ auto-approve 照做 → 再 review 同一
    # 个产物。现场：literature 的 survey_report 被连审 4 轮，reviewer 每轮都
    # APPROVE(4/5)，orchestrator 自己都写了"四轮一致给出 proceed 判断"，流水线
    # 就是出不去。一个 truthy 的 "null" 把整条管线钉死在第一个节点上。
    review_failed_reason = _normalize_absent(review_failed_reason)
    # issue #268（lujy 实测）：**critique id 也要同样归一化** —— 上面只修了
    # review_failed_reason 那一半，字符串 "null" 从另一个参数照样钻进来：
    # `review_critique_artifact_id="null"` 是 truthy → 下面拿它去 read_artifact
    # → 读不到 → 判 "artifact 'null' not found" → review_unusable → 菜单不含
    # PROCEED。现场：literature 明明产出了 survey_report、curator 整合完、
    # reviewer 也给了 critique，账本 review_state=done，用户却只能反复 RETRY
    # REVIEWER，每个子节点跑完都卡在同一处。
    review_critique_artifact_id = _normalize_absent(review_critique_artifact_id)

    # issue #268：账本已有合格 critique 时，用账本的补上传参的缺失。
    # 上面 #202 只做了"账本说没过 → 推翻传参"这一个方向；反方向（账本说过了、
    # 传参却没给/给了个 "null"）没人管，于是合格的 review 被当成不可用。
    # 账本是权威 —— 两个方向都该以它为准。
    _entry0 = _flow_entry_for(state, producing_run_id)
    if (review_critique_artifact_id is None and _entry0 is not None
            and _entry0.get("review_state") in ("done", "skipped")):
        _cid = _normalize_absent(_entry0.get("review_critique_artifact_id"))
        if _cid:
            review_critique_artifact_id = _cid
            review_failed_reason = None
            state.append_transcript(
                "review_critique_recovered_from_ledger",
                producing_run_id=producing_run_id, artifact_id=_cid,
                note="调用方未传/传了空值形态的 critique id；账本 review_state="
                     f"{_entry0.get('review_state')!r} 且记有 critique —— 按账本恢复"
                     "（issue #268）",
            )

    # #202 A2：**账本优先于传参**。账本说这轮 review 没过（review_state != done），
    # 就一律走 review-failed 选项集，不管调用方在参数里塞了哪个 critique id。
    # 实测事故里账本明写 review_state=failed_awaiting_human、critique_id=null，
    # 渲染却只看传参，于是给出了 [1] PROCEED ← recommended。
    _entry = _flow_entry_for(state, producing_run_id)
    if _entry is not None:
        _ledger_state = _entry.get("review_state")
        # skipped = owner 显式 opt-out review（合法）；done = 正常通过。
        if _ledger_state not in ("done", "skipped"):
            _ledger_cid = _entry.get("review_critique_artifact_id")
            if _normalize_absent(_ledger_cid) is None:
                if review_critique_artifact_id:
                    state.append_transcript(
                        "review_critique_ledger_override",
                        producing_run_id=producing_run_id,
                        passed_artifact_id=review_critique_artifact_id,
                        ledger_review_state=_ledger_state,
                    )
                review_critique_artifact_id = None
                review_failed_reason = review_failed_reason or (
                    f"账本记录本轮 review 未通过（review_state={_ledger_state!r}、"
                    f"critique 为空）。传入的 critique 不作数 —— 选项集以账本为准。"
                )

    # #202 的**第三种形态**（2026-09-01 本机 E2E 实拍）：账本 review_state=done
    # **且记着 critique id**，而传参给了**另一个** id。上面两条只覆盖了「账本说
    # 没过 → 推翻传参」和「传参没给 → 用账本补」，这一条没人管，于是传参赢。
    #
    # 实测代价：reviewer 本轮真产出的 critique 是 verdict=major_concerns
    # （1 条 critical、recommended_action=revise），调度器传的却是 8 月 30 日
    # 那份**同名无版本后缀**的基线 critique（verdict=approve、confidence 0.88）。
    # 人看到的是 "Verdict: approve" 和 `[1] PROCEED ← recommended`，据此批了 ——
    # 质量闸响了，人没看见，而且**失败方向朝开**。它连 provenance 闸都过得去：
    # 那份旧的也确实是 _reviewer 写的，缺的从来不是「谁写的」而是「是不是这一次」。
    #
    # 账本里那个 id 是框架在 reviewer 子 run 结束时，从它**真实产出的 artifact**
    # 上抄下来的（shared/tools/run_node.py 的 review_critique_id），不经模型转述。
    # 框架自己答得出的事不该问模型 —— 账本是权威，这一条只是把已经写在上面的
    # 那句话补完。
    if _entry is not None and _entry.get("review_state") == "done":
        _ledger_cid = _normalize_absent(_entry.get("review_critique_artifact_id"))
        if _ledger_cid and _ledger_cid != review_critique_artifact_id:
            state.append_transcript(
                "review_critique_id_diverged_from_ledger",
                producing_run_id=producing_run_id,
                passed_artifact_id=review_critique_artifact_id,
                ledger_artifact_id=_ledger_cid,
                note="账本记的是 reviewer 本轮真实产出的 critique；传参不作数。",
            )
            review_critique_artifact_id = _ledger_cid
            review_failed_reason = None

    # 兜底（账本里没有这条 producing run 时仍要挡住陈年 critique）：一份**早于
    # 本 producing run 开跑**就存在的 critique，不可能是对它的审查。
    # id 复用/同名覆盖下，「这个 artifact 在不在」永远答不出「它审的是不是这次」，
    # 判据必须带起点。run_id 的前缀就是开跑纪元秒（core/state.py: f"{int(time.time())}-…"）。
    if review_critique_artifact_id and not review_failed_reason:
        _started = _run_started_at(producing_run_id)
        _made = _artifact_created_at(state, review_critique_artifact_id)
        if _started is not None and _made is not None and _made < _started:
            state.append_transcript(
                "review_critique_predates_the_run_it_claims_to_review",
                producing_run_id=producing_run_id,
                artifact_id=review_critique_artifact_id,
                critique_created_at=_made.isoformat(),
                run_started_at=_started.isoformat(),
            )
            review_failed_reason = (
                f"critique {review_critique_artifact_id!r} 写于 "
                f"{_made.isoformat()}，早于本 run（{producing_run_id}）"
                f"开跑的 {_started.isoformat()} —— 它审的不是这一次。"
                "请 run_node(node_type='_reviewer') 对本轮产物真正跑一次审查。"
            )
            review_critique_artifact_id = None

    # #202 A：provenance 校验 —— 只有独立 _reviewer run 产出的 critique 才算数。
    if review_critique_artifact_id and not review_failed_reason:
        _rec_pv = state.read_artifact(review_critique_artifact_id)
        _pv_problem = critique_provenance_problem(_rec_pv)
        if _pv_problem:
            state.append_transcript(
                "review_critique_provenance_rejected",
                producing_run_id=producing_run_id,
                artifact_id=review_critique_artifact_id,
                reason=_pv_problem,
            )
            review_critique_artifact_id = None
            review_failed_reason = _pv_problem

    # 残件 critique：reviewer 轮次耗尽 / 提前收手，由 _reviewer 的
    # `reviewer_critique_lands` hook 兜底落盘（#194/#301/#395-6）。它**有内容
    # 可读，但没有独立审查结论** —— 必须走 #155 的 (a) 分支（review 不可用 →
    # retry_reviewer 打头、动作集不含 PROCEED），而不是 (b)「schema 有瑕疵」那条
    # 保留正常 5 项选项集的路。让一份被截断的审查放行下游，比 reviewer 白跑
    # 一轮贵得多。
    #
    # reviewer 自己 set_verdict 过的残件**不在此列**：那是它真下过的判断，只是
    # concerns 可能没收全，照常消费。判据只有一条：残件标记 + verdict 缺失。
    if review_critique_artifact_id and not review_failed_reason:
        _rec_inc = state.read_artifact(review_critique_artifact_id)
        if _rec_inc:
            _md = _rec_inc.get("metadata")
            if isinstance(_md, str):
                try:
                    _md = json.loads(_md)
                except (json.JSONDecodeError, TypeError):
                    _md = {}
            _md = _md if isinstance(_md, dict) else {}
            _body = _parse_critique_json(_rec_inc.get("content") or "") or {}
            _inc = _body.get("review_incomplete") or _md.get("review_incomplete")
            if _inc and not (_body.get("verdict") or _md.get("verdict")):
                state.append_transcript(
                    "review_critique_incomplete_rejected",
                    producing_run_id=producing_run_id,
                    artifact_id=review_critique_artifact_id,
                    turns_used=_inc.get("turns_used") if isinstance(_inc, dict) else None,
                )
                review_failed_reason = (
                    "reviewer 未给出独立审查结论：这份 critique 是框架兜底落盘的残件"
                    "（reviewer run 轮次耗尽或提前收手，verdict 缺失）。已收集到的 "
                    "concerns 保留在 artifact 里，可作为重跑 reviewer 的起点。"
                )
                # 置空才能让下面的 review_unusable 判为 True —— 有 id 没 json
                # 正是 #155 (a) 「根本没有可用 critique」的形态。
                review_critique_artifact_id = None

    review_critique_json: dict | None = None
    recommended_feedback = ""
    recommended_index = 0
    recommended_action_str = "proceed"
    recommended_target_node: str | None = None
    framework_override_note: str | None = None

    if review_critique_artifact_id and not review_failed_reason:
        rec = state.read_artifact(review_critique_artifact_id)
        if rec is None:
            review_failed_reason = (
                f"review_critique artifact {review_critique_artifact_id!r} not found"
            )
        else:
            review_critique_json = _parse_critique_json(rec.get("content") or "")
            if review_critique_json is None:
                # #202 D：content 解析不了 → 回退读 metadata。reviewer 契约
                # （nodes/_reviewer/harness.yaml）**强制要求** metadata 写
                # verdict / recommended_action / n_concerns，"让 orchestrator
                # decision package 能直接读" —— 但消费方从来只认 content，白白
                # 丢掉一条完全合规的降级路径。实测：reviewer#4 把 critique 写成
                # Markdown（metadata 完全合规），却被判 review 不可用。
                review_critique_json = _critique_from_metadata(rec)
                if review_critique_json is None:
                    # C：content 与 metadata 都不可用 → fail-closed（默认 REVISE）
                    review_failed_reason = (
                        "review_critique content 非法 JSON（已尝试去代码围栏 / 首尾"
                        "括号提取仍失败），且 metadata 也没有可用的 verdict / "
                        "recommended_action 字段"
                    )
                else:
                    state.append_transcript(
                        "review_critique_metadata_fallback",
                        producing_run_id=producing_run_id,
                        artifact_id=review_critique_artifact_id,
                    )
            else:
                ra = (review_critique_json.get("recommended_action") or {})
                recommended_action_str = ra.get("action", "proceed")
                recommended_feedback = ra.get("feedback_to_next_run", "") or ""
                # v0.5: redirect_upstream 必须含 target_node（上游节点 type）。
                # v3.6 修复（E2E#1 实测：5 次 redirect 建议 → 0 次执行，74 次执行
                # 全是 revise）：缺 target 时旧代码**静默改判 revise**，等于把
                # "根因在上游"这个诊断结论因为一个 schema 疏漏抹掉，然后继续重跑
                # 那个本来就修不好的节点。绝不能这么降级。
                #   - 依赖图能唯一确定上游 → 自动补全，保留 redirect；
                #   - 有歧义 → 保留"review 不可用"状态（动作集变成无 PROCEED、
                #     retry_reviewer 打头），让 reviewer 重出一份带 target 的结论。
                #     仍然自主，且绝不退化成"重跑当前节点"。
                if recommended_action_str == "redirect_upstream":
                    recommended_target_node = ra.get("target_node")
                    if not recommended_target_node:
                        from core.upstream_routing import (
                            infer_redirect_target,
                            upstream_candidates,
                        )
                        inferred = infer_redirect_target(source_node_type or "")
                        if inferred:
                            recommended_target_node = inferred
                            framework_override_note = (
                                f"ℹ️ reviewer 未填 target_node；依赖图唯一确定上游为 "
                                f"'{inferred}'，已自动补全并保留 REDIRECT。"
                            )
                        else:
                            cands = upstream_candidates(source_node_type or "")
                            review_failed_reason = (
                                "reviewer 判定根因在上游（redirect_upstream）但未指名 "
                                "target_node，依赖图也无法唯一确定。**不得降级为 revise**"
                                "（那等于继续重跑一个修不好的节点）。请指名一个上游后"
                                f"再执行；候选：{cands or '（无上游，考虑 abort/escalate）'}"
                            )
                            # 保留 REDIRECT 意图；target 留空 → 下游校验会要求补齐
                # B：红线机械否决（不受 overall 平均分影响）
                recommended_action_str, framework_override_note = _apply_critical_veto(
                    review_critique_json, recommended_action_str, recommended_target_node,
                )
                recommended_index = _recommended_index_from_action(recommended_action_str)

    # C：fail-closed —— review **失败/不可用**（区别于 owner opt-out 跳过）时，
    # 默认推荐 REVISE 而非 PROCEED。旧行为默认 PROCEED = 无质量信号静默放行。
    # #155：区分两种 "review_failed_reason"：
    #   a) **根本没有可用 critique**（artifact 找不到 / 内容非法 JSON / reviewer
    #      截断崩了）→ RETRY REVIEWER 才对症（producer 本身合格，是审的人挂了）。
    #   b) critique **有**，只是 schema 有瑕疵（如 redirect_upstream 缺
    #      target_node）→ 重跑 reviewer 多半复现同一个 schema 错，沿用旧行为
    #      降级 REVISE，选项集也保持正常 5 项。
    review_unusable = bool(review_failed_reason) and review_critique_json is None

    if review_unusable:
        # 推荐 **RETRY REVIEWER**（专用选项集第 [1] 项）；该选项集不含 PROCEED
        # —— 没有任何独立审查信号时不许放行。
        recommended_action_str = "retry_reviewer"
        recommended_index = 0
        if not recommended_feedback:
            # #151：reviewer 挂了（截断/超时/5xx/无 critique）≠ producer 有问题。
            # 先 **reviewer-only retry**，别上来就重跑一个本来就通过的 producer
            # ——那既烧钱又可能再撞同一个上限。框架已放行这条结构化重试路径
            # （failed 的 flow entry 保留为可重试凭据，见 run_node 的 guard）。
            recommended_feedback = (
                "reviewer 未产出可解析的 critique（review 失败，**不是 producer 的问题**）。\n"
                "fail-closed：没有独立审查信号不放行，本轮也不提供 PROCEED。\n"
                "选 [1] RETRY REVIEWER → 复用这个已合格的 producer，只重跑 _reviewer：\n"
                "  run_node(node_type='_reviewer', node_inputs={\n"
                f"      'source_node_type': '{source_node_type}',\n"
                f"      'producer_run_id': '{producing_run_id}',\n"
                "      'artifact_id': '<同一个已 import 的 artifact id>'})\n"
                "（让 reviewer 先精简产出 review_critique 再展开，避免再撞输出上限。）\n"
                "只有 retry 仍拿不到 critique 时，才考虑 [2] REVISE 重跑 source_node。"
            )

    # C.5：producer 是被**轮次上限**切断的 —— 那它不是"本来就通过"（2026-08-11）
    #
    # 上面那段 #151 的理由（"reviewer 挂了 ≠ producer 有问题，别重跑一个本来就
    # 通过的 producer"）成立，但它有个隐含前提：producer 真的做完了。而框架把
    # "做完了"和"撞上 max_turns 被切断"记成同一个 `completed`，于是这个前提
    # 悄悄不成立时没人知道。
    #
    # 实测代价（E2E v23）：experiment 在第 40 轮被切断，14 个模拟全跑完但没做
    # MSD 分析；决策层看不出交付物残缺，在 retry_reviewer 和 retry_curator 之间
    # 来回，一轮约一小时，空转三轮。
    #
    # 现在 summary 里有 `stop_reason` / `max_turns` 了，这里把它读出来：
    # 被截断 → 推荐 REVISE（带反馈重跑 producer，让它接着做完），并在反馈里
    # 写清"哪些已经做了、还差什么"，别让它从头再来一遍。
    _truncated = _producer_was_truncated(state, producing_run_id)
    if _truncated is not None:
        recommended_action_str = recommended_action_for_truncated(
            True, recommended_action_str)
        recommended_index = _NORMAL_ACTIONS.index("revise") \
            if "revise" in _NORMAL_ACTIONS else recommended_index
        recommended_feedback = (
            f"⚠️ **{source_node_type} 这一轮不是做完了停的，是撞上 max_turns="
            f"{_truncated.get('max_turns')} 被切断的**"
            f"（跑了 {_truncated.get('turns')} 轮）。\n"
            "所以这份交付物是**残缺的**，不是「审查环节出了问题」—— 重跑 reviewer "
            "或 curator 不会让它变完整。\n"
            "选 REVISE 带反馈重跑它，反馈里写清**已经做完的部分不要重做**"
            "（产物和中间数据都还在它自己的目录里），只补没做完的那一段。\n"
            "如果它一轮做不完：把任务拆小，或在 node_inputs 里说明本轮只做哪一段。"
        )

    # D：infeasible 一等公民出口 —— producing 节点在产出上声明"任务不可执行"
    # （metadata.infeasible=true 或 ## Feasibility 段落判 infeasible）→ 推荐动作
    # **机械强制 REDIRECT 回上游降级设计**，优先级最高（盖过 reviewer 推荐 /
    # 红线否决 / fail-closed）。语义："不能执行"是合法终态，但必然流回上游，
    # 不许被 PROCEED 淹掉、也不该被降成"revise 再糊弄一版"。
    from shared.lib.feasibility import find_infeasibility_declaration
    infeasible = find_infeasibility_declaration(
        state, list(artifact_ids_produced or []) or None)
    if infeasible:
        recommended_action_str = "redirect_upstream"
        recommended_target_node = infeasible["redirect_target"]
        recommended_index = _recommended_index_from_action("redirect_upstream")
        framework_override_note = (
            f"⛔ 框架强制：{infeasible['artifact_id']} 声明任务不可执行"
            + (f"（{infeasible['reason']}）" if infeasible["reason"] else "")
            + f" —— 必须 REDIRECT 回 '{recommended_target_node}' 降级/修改设计，"
              "不得 PROCEED，也不要原样 REVISE 重跑。"
        )
        if not recommended_feedback:
            _reason = infeasible["reason"] or "见 artifact 的 Feasibility 段"
            recommended_feedback = (
                f"下游节点判定任务不可执行：{_reason}。"
                f"请修改实验设计使其在现有资源下可执行，或显式降低 scope。"
            )

    # curator 已退出 post-producing flow（wangd 2026-08-19）——它是按需调取的
    # 后台节点，不再有 "curator 没整合完" 这个流程状态，因此也不再有它专属的
    # 选项集、推荐动作和 fail-closed 分支。
    #
    # #272 当初要解决的是"curator 空跑，PROCEED 却照样在菜单第一项"。那道门的
    # 前提是"每个 producing 节点跑完都必须整合"，而实测否掉了这个前提：同一个
    # session 里 curator 跑 5 次、KB 写入 0 次、三次明确判零候选。前提没了，
    # 门就没有防守对象。
    _flow_now = state.hook_state.get("pending_post_node_flow") or []
    _entry_now = next((e for e in _flow_now
                       if e.get("producing_run_id") == producing_run_id), None)

    if review_failed_reason and not review_unusable:
        # critique 有、只是 schema 瑕疵。
        # v3.6：**redirect_upstream 缺 target 不在此列** —— 那是"根因在上游"的
        # 有效诊断，降级 REVISE 等于让它继续重跑一个修不好的节点（E2E#1：5 次
        # redirect 建议 → 0 次执行）。此时保留 REDIRECT 推荐、target 留空，由
        # 下游校验要求补一个明确上游（decision_state=awaiting_human），
        # 宁可要一次澄清，也不要错误地重跑。
        if recommended_action_str != "redirect_upstream":
            recommended_action_str = "revise"
            recommended_index = 1

    # ── 选项集在**渲染之前**定死，此后只有投影 ────────────────────────────
    #
    # 这段原来在渲染之后（`presented_actions` 在下面几十行才算），于是渲染器
    # 只能自己再算一次标签 —— 而它的调用点从没传过 `curator_pending`，两个人
    # 看的面因此都显示了框架已经撤下的 PROCEED。顺序反了是那次分叉的结构成因，
    # 所以这里连顺序一起改：先定选项集，再渲染。
    # ── 最后一道：**推荐出去的动作必须是执行得了的**（2026-09-17）────────────
    #
    # 三个地方都能把 `recommended_action=redirect_upstream` 连同一个目标定下来：
    # reviewer 的 critique、依赖图自动补全、infeasibility 声明里的 redirect_target。
    # 三处各自校验就是三份会分叉的抄件，所以判据放在**settled 之后、呈递之前**
    # 这一处，问一次。
    #
    # 不这么做的代价不是理论上的：授权侧（record_decision_answer）已经会拒绝一个
    # 关不掉 flow 的目标，但**推荐照旧**——自动裁决每轮挑同一个被拒的推荐，拒了
    # 再呈递、呈递再拒，换成一个更短的死循环而已。要让它第一轮就终止，推荐本身
    # 就不能是那个东西。
    #
    # 绝不静默改判：REDIRECT 留在菜单里（人可以自己指一个合法上游），只是不再
    # **推荐**它；reviewer 原本的诊断连同它点名的目标一起写进 note 和反馈，
    # 一个字不丢 —— 抹掉"根因在上游"这个结论比推荐错目标更糟。
    # 只管**指名了但执行不了**的目标。"根本没指名"是另一件事，上面已经按既有设计
    # 处理过了（保留 REDIRECT 意图 + 空 target + review_failed_reason 要求补齐），
    # 那条路刻意**不**降级 —— 降了就抹掉"根因在上游"这个诊断。第一版把两者混成
    # 一条判据，当场让两个既有测试转红，正是它们在守这条线。
    _named_target = str(recommended_target_node or "").strip()
    _unexecutable_reco = (
        _why_this_target_cannot_close_the_flow(_named_target)
        if (recommended_action_str == "redirect_upstream" and _named_target) else None
    )
    if _unexecutable_reco:
        _named = _named_target      # 进得来就一定非空（上面的条件保证）
        framework_override_note = (
            (framework_override_note + "\n  " if framework_override_note else "")
            + f"⚠️ 框架撤下 REDIRECT 推荐：审查结论把根因指向 '{_named}'，"
            f"而它跑完不进 post-node flow 账本 —— 退回它，这条审查义务永远关不掉"
            f"（实测空转 40 轮且熔断看不见）。诊断本身保留在下面的反馈里：若症结在"
            f"某个**服务**（检索/前处理/出图），选 REVISE 让产出节点自己重新调用它；"
            f"若确在某个上游产出节点，选 REDIRECT 并指名它。"
        )
        recommended_feedback = (
            (recommended_feedback + "\n\n" if recommended_feedback else "")
            + f"（审查结论原本点名的上游是 '{_named}'。）"
        )
        recommended_action_str = "revise"
        recommended_target_node = None
        recommended_index = _recommended_index_from_action("revise")
        state.append_transcript(
            "decision_recommendation_was_unexecutable",
            producing_run_id=producing_run_id,
            named_target_node=_named,
            downgraded_to="revise",
        )

    flow = state.hook_state.get("pending_post_node_flow") or []
    _capped_now = bool((_entry_now or {}).get("review_retry_capped"))
    presented_actions = (
        _REVIEW_FAILED_ACTIONS if (review_unusable and not _capped_now)
        else _NORMAL_ACTIONS)
    presented_capped = _capped_now

    # 决定的身份 = 这一次呈递，不是 producing run。同一个 producing run 的
    # decision package 完全可以被呈递多次（curator 重跑后重呈递、进程重启后
    # 重呈递），每次的条款都可能不同 —— 对平台的不可变快照守卫来说，它们是
    # **不同的决定**。（2026-08-17 事故：同轮两次呈递派生出同一个 Decision.id，
    # 第二次条款已变 → 平台判"篡改" → 整轮被判死。病在 id 粒度，守卫是对的。）
    decision_id = f"{producing_run_id}:p{uuid4().hex[:8]}"
    offer = build_decision_offer(
        decision_id=decision_id,
        source_node_type=source_node_type,
        action_ids=list(presented_actions),
        recommended_action=recommended_action_str,
        redirect_target_node=recommended_target_node,
        # 「为什么问你」的判断依据随呈递走。
        #
        # 这些事实此前只进 pause 的 `metadata`，而 metadata 是 pause 自己的东西，
        # 平台各层按需挑着抄 —— 结果是面板上只剩五个动作名，人看不到
        # review 到底失败没有、重试还有没有额度、要几个人批。做决定需要的正是
        # 这些，它们是**呈递的**事实，家在这里。
        facts={
            "producingRunId": producing_run_id,
            "sourceNodeType": source_node_type,
            "reviewFailed": bool(review_failed_reason),
            "reviewFailedReason": str(review_failed_reason or ""),
            "reviewRetryCapped": bool(presented_capped),
            "reviewCritiqueArtifactId": review_critique_artifact_id or "",
            "artifactIdsProduced": list(artifact_ids_produced or []),
        },
    )

    package_text = _render_decision_package(
        closure_lines=_closure_ledger_lines(state),
        source_node_type=source_node_type,
        producing_run_id=producing_run_id,
        producing_summary=producing_summary,
        artifact_ids_produced=list(artifact_ids_produced or []),
        curator_summary=curator_summary,
        review_critique_json=review_critique_json,
        review_failed_reason=review_failed_reason,
        review_unusable=review_unusable,
        offer=offer,
        recommended_feedback=recommended_feedback,
        recommended_target_node=recommended_target_node,
        framework_override_note=framework_override_note,
        child_obligation_effect=child_obligation_effect,
    )

    # ── flow entry 记账（#155 状态机）────────────────────────────────────────
    # 关键修正（qinp #155 现象 3）：decision_state 以前在**呈递时**就写 done ——
    # 那时用户根本还没回答。加上 #151 保留 failed entry，就出现
    #   review 挂了 → 呈递 → decision_state=done → 下游 producing gate 不再拦
    # 的静默路径（"没有有效审查也能进下一阶段"）。我在 #151 甚至把"不卡新
    # producing"当安全特性写进注释和测试 —— 那是错的，这里一并纠正。
    # 现在：呈递只把 decision_state 置 "awaiting_human"，真正的 done 由
    # pause_driver 在**拿到人工答复后**机械写入（record_decision_answer）。
    # ── 重呈递不得踩在途状态（2026-08-17 空转 11 圈的引擎）──────────────────
    # 下面这个循环无条件把 entry 写成 awaiting_human。若该 entry 的授权动作
    # **已经起了**（action_in_progress），这一笔就把"已经起了"抹成"还没起"：
    # 调度器于是可以合法地再起一次，再抹一次……单个 flow 起了 11 次 hypothesis、
    # 空转 5.5 小时。在途那一轮该由它自己闭合，或在失败/重启时被机械退回，
    # 都轮不到一次重新呈递来决定。
    _in_flight = next(
        (entry for entry in flow
         if entry.get("producing_run_id") == producing_run_id
         and entry.get("decision_state") == "action_in_progress"),
        None,
    )
    if _in_flight is not None:
        _target = (_in_flight.get("action_target_node")
                   or _in_flight.get("authorized_target_node")
                   or _in_flight.get("deferred_to_node"))
        state.append_transcript(
            "decision_represent_ignored_in_flight",
            source_node_type=source_node_type,
            producing_run_id=producing_run_id,
            action_target_node=_target,
            action_attempt_count=_in_flight.get("action_attempt_count"),
        )
        return {
            "status": "error",
            "error": (
                f"这一轮的裁决**已经作出并且已经起了** {_target!r}"
                f"（第 {_in_flight.get('action_attempt_count') or 1} 次，"
                f"started_at={_in_flight.get('action_started_at')}）—— "
                "重新呈递不会改变这一点，只会让它被重复启动。\n"
                "等那个 run 结束即可，它完成时框架自动关闭本 flow。若它已经不在了"
                "（平台重启/被停止），下一次会话恢复会把本 entry 机械退回可重启状态。"
            ),
            "decision_state": "action_in_progress",
            "action_target_node": _target,
        }
    for entry in flow:
        if entry.get("producing_run_id") != producing_run_id:
            continue
        entry["decision_state"] = "awaiting_human"
        # #202 × #208 交汇：reviewer 重试已封顶时，review-failed 选项集（无
        # PROCEED）+ 重试上限 = 人工无路可走。#208 的封顶文案本身就明写"改选
        # PROCEED"，所以封顶后必须把 PROCEED 放回菜单 —— 此时它是**人工显式
        # override**（record_decision_answer 会如实记成 override，绝不记成
        # "review 通过"）。不加这条豁免就是我在 #151 造过的那种无出口 dead-end。
        # 选项集已在渲染前定死（见上面 `offer`）。这里只落账，不再第二次构造 ——
        # 第二次构造正是让账本与屏幕分叉的那条路。
        entry["decision_options"] = list(offer.choice_ids())
        entry["decision_offer_id"] = offer.offer_id
        entry["decision_recommended_action"] = recommended_action_str
        if review_unusable:
            # review 没成功 → 等人工在 RETRY/REVISE/REDIRECT/ABORT 里选。
            # entry 留着（它是"producer 当初合格"的凭据，#151），且此刻
            # **必须**继续拦下游（新 gate 认这个状态）。
            entry["review_state"] = "failed_awaiting_human"
            entry["review_retryable"] = True
    # 注意：这里不再 drop 任何 entry —— 走完与否由人工答复决定（见 pause_driver）。
    state.hook_state["pending_post_node_flow"] = flow

    # log to transcript（reviewer 跑挂时也能追责）
    # ── flow cadence: review_curate 顺延（v2.1 P3d）──────────────────────────
    # 声明了 review_curate 的 producing 节点（experiment），review 成功时不停下来
    # 问人 —— 把裁决顺延给下一个 Analysis run。Analysis 本来就是"这条假说成不
    # 成立、下一步做什么"的裁决者，让它连着判，比先问人再问它少一次决策疲劳。
    #
    # 红线（#151/#202/#208 语义原样保留）：**只在 review 成功时顺延**。
    # review 挂了 / curator 还没整合 → 走下面的正常呈递，继续拦下游。
    # 不顺延的三种情况（都是"这不是一轮正常出结果的实验"）：
    #   - review 不可用 → 没有可信审查，就没有可顺延的东西
    #   - curator 还没整合 → 账本没闭合
    #   - 框架推翻了 reviewer 推荐（infeasible 强制 REDIRECT、critical veto）
    #     → 这是**计划层失败**，且 redirect 目标未必是 Analysis（可能是 data）。
    #       顺延会把目标信息弄丢，也该让人知道计划出问题了。
    _framework_overrode = bool(framework_override_note)
    if (not review_unusable and not _framework_overrode
            and _defers_to_analysis(source_node_type)):
        deferred_entry = None
        for entry in flow:
            if entry.get("producing_run_id") == producing_run_id:
                entry["decision_state"] = "deferred_to_analysis"
                entry["deferred_to_node"] = _ANALYSIS_NODE_TYPE
                entry["decision_options"] = list(presented_actions)
                entry["decision_recommended_action"] = recommended_action_str
                entry["recommended_feedback"] = recommended_feedback
                deferred_entry = entry
        state.hook_state["pending_post_node_flow"] = flow
        state.append_transcript(
            "decision_deferred_to_analysis",
            source_node_type=source_node_type,
            producing_run_id=producing_run_id,
            recommended_action=recommended_action_str,
            deferred_to_node=_ANALYSIS_NODE_TYPE,
        )
        return {
            "status": "success",
            "deferred_to": _ANALYSIS_NODE_TYPE,
            "decision_state": "deferred_to_analysis",
            "message": (
                f"{source_node_type} 的 review + curator 都已走完且 review 有效 —— "
                f"本轮不占用人工决策，裁决顺延给下一个 {_ANALYSIS_NODE_TYPE} run。\n"
                f"下一步：run_node(node_type='{_ANALYSIS_NODE_TYPE}')，它会读 "
                f"experiments/ 结果、更新假说状态、出新一版 research_state。\n"
                f"在那之前不能起别的 producing 节点（框架机械拦截）。"
            ),
            "package": package_text,
            "recommended_action": recommended_action_str,
            "recommended_feedback": recommended_feedback,
            "flow_entry": deferred_entry,
        }

    # 决定的身份 = 这一次呈递，不是 producing run。同一个 producing run 的
    # decision package 完全可以被呈递多次（curator 重跑后重呈递、进程重启后
    # 重呈递），每次的条款都可能不同 —— 对平台的不可变快照守卫来说，它们是
    # **不同的决定**。身份在出生地生成，随 pause metadata 走完全程（答复事件
    # 带同一个 id），下游才不用拿 producing_run_id 去猜"这是哪一次"。
    # （2026-08-17 事故：同轮两次呈递派生出同一个 Decision.id，第二次条款
    # 已变 → 平台判"篡改" → 整轮被判死。病在 id 粒度，守卫本身是对的。）
    for _e in flow:
        if _e.get("producing_run_id") == producing_run_id:
            _e["decision_id"] = decision_id
            _e["decision_offer_id"] = offer.offer_id
    state.hook_state["pending_post_node_flow"] = flow

    state.append_transcript(
        "decision_package_presented",
        source_node_type=source_node_type,
        producing_run_id=producing_run_id,
        decision_id=decision_id,
        offer_id=offer.offer_id,
        recommended_action=recommended_action_str,
        review_failed=bool(review_failed_reason),
        # ── 审查意见必须**机械送达**决策方（2026-08-23）────────────────────
        #
        # `package_text` 是这里刚渲染好的决策包全文：verdict、按 severity 排
        # 序的 concerns（critical 优先）、owner_spec 失效告警、推荐动作。
        # 在此之前它**只**进了下面的 `pause_event`，没进这个 transcript 事件
        # —— 而平台侧建 Decision 行时读的正是这个事件的 `prompt` 字段
        # （execution_ingest：`raw.get("prompt") or f"Decision required for …"`）。
        # 取不到就落兜底文案，于是库里的 Decision 只有一句
        # 「Decision required for writing」。
        #
        # 实测代价（英国饮食 2026-08-23 02:11）：reviewer 判 major_concerns，
        # 唯一的 critical 是一处引文方向反转（论文里说斯摩莱特批评法国饮食，
        # 而 KB 锚点与本文局限性一节都指向英国本土 —— 该锚点支撑 Q2 的核心
        # 论证）。调度器手里只有那一句兜底文案，只能自己去 read_file 猜，
        # 于是派下去的修订指令里只有一条 minor 措辞，critical 整条丢失。
        #
        # 这里**不加任何门禁** —— 决定权仍然全在决策方（proceed 照旧可选）。
        # 变的只是：它看得见的东西不再取决于模型有没有想起来去读产物。
        # 机械可判的（"这份意见有没有送到"）归框架，语义判断（"要不要因此
        # 返工"）归模型。
        prompt=package_text,
        review_verdict=(review_critique_json or {}).get("verdict"),
        review_concerns=[
            {"severity": c.get("severity"), "description": str(c.get("description") or "")[:400]}
            for c in ((review_critique_json or {}).get("concerns") or [])
        ],
        # 跨桥契约：平台侧按这份**如实的**选项集建模 Decision，缺失时才允许
        # 按 review_failed 走静态 fallback。不写就是逼下游猜 —— 猜错=判死 run。
        decision_options=list(offer.choice_ids()),
        review_retry_capped=presented_capped,
    )

    _pause_payload = offer.to_pause_payload()
    return {
        "status": "pause",
        "pause_event": {
            # 这一次呈递**整份摊开**，不在这里挑字段。
            #
            # 上一版是把 `to_pause_payload()` 的键逐个抄进来的：抄了 6 个、漏了
            # `kind`。而 `kind` 参与 offer_id 的派生 —— 收到这份 payload 的一方
            # 因此永远重算不出同一个 offer_id，`from_pause_payload` 的自校验一路
            # 在失败、一路在走降级路径，两边都不报错。
            #
            # 漏字段这件事，只要"按名字列举"这个动作还在就会再发生。所以这里不
            # 再列举。新增字段默认活着，想丢它得特意去丢。
            **_pause_payload,
            # 同一份呈递再挂一个**可整体搬运**的副本。平台各层（ingest / API /
            # 前端）只搬这一个值，不认识它的内部字段 —— 摊开的那些键是给既有
            # 消费方（PauseEvent.option_details() / builtin / 纯文本客户端）读的
            # 便捷视图，两者派生自同一个 offer，不可能分叉。
            PAUSE_OFFER_KEY: _pause_payload,
            # 下面是 pause 事件**自己**的字段，覆盖上面的同名项：
            # 正文用决策包全文（比呈递里的 context 长）。
            "context": package_text,
            "asking_node_type": state.node_type,
            "asking_run_id": state.run_id,
            "metadata": {
                "type": "decision_package",
                "source_node_type": source_node_type,
                "producing_run_id": producing_run_id,
                "decision_id": decision_id,
                # recommended_* 不在这里再抄一份 —— 它们是**呈递的**事实，
                # 已经在 pause_event 顶层（摊开的那份）。metadata 只放 pause
                # 自己的事实。消费方改从顶层读（见 pause_driver）。
                "recommended_action": recommended_action_str,
                "recommended_feedback": recommended_feedback,
                "recommended_target_node": recommended_target_node,  # v0.5 redirect_upstream 用
                "review_failed": bool(review_failed_reason),
                "artifact_ids_produced": list(artifact_ids_produced or []),
                "review_critique_artifact_id": review_critique_artifact_id,
            },
        },
    }


def _answer_preview(answer: Any) -> str:
    """人的答复现在可能是结构化的（{offer_id, choice_id}），不再一定是字符串。"""
    if isinstance(answer, dict):
        return _json.dumps(answer, ensure_ascii=False)[:200]
    return str(answer or "")[:200]


def _offer_for_answer(pause_payload: dict, entry: dict) -> "Offer":
    """拿到"这个答复正在回答的那一次呈递"。

    首选：从 pause payload 还原（带顺序、label、recommended，且 offer_id 自校验）。
    降级：老 checkpoint 恢复出来的 pause 没有 `option_details`，只能按 entry 记的
    id 集重建 —— 那时 label 就等于 id，裸序号仍与账本同源，只是没法再受理文案。
    降级路径**不猜**：认不出照样 Rejection，不会像旧的子串匹配那样蒙对一个动作。
    """
    from core.decision_offer import Choice, Offer, OfferContractError

    try:
        return Offer.from_pause_payload(pause_payload)
    except OfferContractError:
        pass
    ids = [str(i) for i in (entry.get("decision_options") or _NORMAL_ACTIONS)]
    return Offer(
        decision_id=str(entry.get("decision_id") or entry.get("producing_run_id") or "unknown"),
        kind="post_node",
        question="Post-node decision",
        choices=tuple(Choice(id=i, label=i) for i in ids),
        recommended_id=(str(entry["decision_recommended_action"])
                        if entry.get("decision_recommended_action") in ids else None),
    )


def record_decision_answer(state: State, pause_payload: dict, answer: Any) -> dict | None:
    """把**人工对 decision package 的真实选择**机械记进 flow entry（#155）。

    `pause_payload` 是呈递时那份 pause_event **全份**（含 `option_details` /
    `offer_id` / `metadata`）。收全份是因为判答复需要的是**那一次呈递本身** ——
    顺序、label、合法集，缺一样就得靠猜。只收 `metadata` 的旧签名仍受理
    （老 checkpoint 恢复出来的 pause 没有结构化选项），走降级路径。

    由 core.pause_driver 在拿到答复后立刻调 —— 这是框架唯一能确知"用户选了啥"
    的地方：present_decision_package 只负责 unwind 出 pause，答复是回给
    orchestrator LLM 的文本，框架本来看不到。没有这一步，
    `reviewer_retry_authorized` 之类的事件只代表"框架认为有资格重试"，
    不代表"用户批准了本次重试"（qinp #155 现象 2）。

    entry 命运由选择决定：
      - retry_reviewer → review_state=retry_authorized（保留，reviewer guard 认它放行一次）
      - revise / redirect → action_authorized（保留，直到指定 producing 节点实际跑完）
      - edit → awaiting_manual_edit（保留，人工修改后再明确决策）
      - abort → 关闭本 flow（出 pending list），历史进 transcript
      - proceed（仅 review 成功时可选）→ 走完，出 pending list
    答复认不出 → 不动 entry（继续 fail-closed 拦下游），并把 `decision_rejection`
    写进 entry：**认不出必须吵**。此前这里只记一行 transcript 就返回，于是人的
    授权凭空消失、界面毫无变化、只能再点一次再消失一次（2026-08-19 实测三次）。

    返回被更新的 entry（或 None）。调用方读 `entry["decision_rejection"]` 判断
    这次答复有没有被受理。
    """
    pause_payload = dict(pause_payload or {})
    pause_metadata = pause_payload.get("metadata")
    if not isinstance(pause_metadata, dict):
        # 老签名：调用方直接传了 metadata。那份里没有 option_details，
        # 下面会走"按 entry 记的 id 集降级重建"。
        pause_metadata = pause_payload
    producing_run_id = (pause_metadata or {}).get("producing_run_id")
    if not producing_run_id:
        return None
    flow = state.hook_state.get("pending_post_node_flow") or []
    entry = next((e for e in flow
                  if e.get("producing_run_id") == producing_run_id), None)
    if entry is None:
        return None

    # 答复必须指回**它回答的那一次呈递**。pause metadata 是第一真相源（这个
    # 答复就是对这个 pause 的）；entry 里存的是最近一次呈递的 id，只作 metadata
    # 缺失时（老 checkpoint 恢复的 pause）的回退。两处都没有 → 不编造，平台侧
    # 按 producing_run_id 走老的派生路径。
    decision_id = (pause_metadata or {}).get("decision_id") or entry.get("decision_id")

    # 答复对着**这一次呈递**解析 —— 不再拿正则去猜一串动作名。
    #
    # 老路径是 `_parse_choice(answer, entry["decision_options"])`：只有 id 列表，
    # 没有顺序来源、没有 label，于是做子串匹配。那正是 2026-08-19 死循环的引擎 ——
    # 屏幕上印着 `PROCEED to next stage`（另一套选项集的文案），拿去撞
    # `[retry_curator, …]`，撞不上，静默丢弃。
    offer = _offer_for_answer(pause_payload, entry)
    outcome = resolve_answer(offer, answer)

    history = entry.setdefault("decision_history", [])
    if isinstance(outcome, Rejection):
        history.append({
            "answer_raw": _answer_preview(answer),
            "chosen_action": None,
            "rejection": outcome.code,
            "offer_id": offer.offer_id,
            "review_state_before": entry.get("review_state"),
            "review_attempt_count": entry.get("review_attempt_count", 0),
        })
        entry["accepted_action"] = None
        # 认不出 = 契约被违反，**必须留下可送达的东西**（带合法出口），
        # 不是只在 transcript 里记一笔然后装作无事发生。
        entry["decision_rejection"] = outcome.as_dict()
        state.append_transcript(
            "decision_answer_rejected", producing_run_id=producing_run_id,
            code=outcome.code, offer_id=offer.offer_id,
            legal_choice_ids=list(outcome.legal_choice_ids),
            answer_preview=_answer_preview(answer))
        return entry

    entry.pop("decision_rejection", None)
    chosen = outcome.choice_id
    history.append({
        "answer_raw": _answer_preview(answer),
        "chosen_action": chosen,
        "offer_id": outcome.offer_id,
        "note": outcome.note,
        "review_state_before": entry.get("review_state"),
        "review_attempt_count": entry.get("review_attempt_count", 0),
    })
    entry["accepted_action"] = chosen
    # 人在选项之外写的那段话（Offer 契约里的 `note`）。此前它只进
    # `decision_history[-1]["note"]` —— 那是**账本**，没有任何执行路径读它。
    # 于是 REVISE 重跑时只带 reviewer 的 `recommended_feedback` 过去，
    # 课题负责人亲口说的纠正意见结构上无处可去（2026-08-30 实测：同一条意见
    # 说两遍，research_plan 里 `S1 | 1 维 normal form` 一字未动，而调度器
    # 把这一轮总结成"reviewer 指出维度混淆，需同维对照"）。
    # 挂到 entry 上，`_execute_authorized_action` 才够得着。
    entry["human_note"] = str(getattr(outcome, "note", "") or "")
    # #761：决策附言是**用户的话**，和开题原文同级。此前它只走
    # `human_directive` 送给被打回的那一个节点；reviewer 做 provenance 对账时的
    # 「用户权威原文」只有开题 brief，于是附言里给的数字（阈值 0.02、种子数）
    # 一律被判 phantom —— 节点引得没错，核验面看不见。记进 research_intake，
    # 每个节点（含 reviewer）的权威原文段就都有它了；来源单独标注，不冒充开题原文。
    if entry["human_note"].strip() and getattr(state, "project_root", None) is not None:
        try:
            from core.research_intake import record_intake

            record_intake(state.project_root, entry["human_note"], source="decision_note",
                          session_id=str(getattr(state, "session_id", "") or ""))
        except Exception:                 # 锚点记录失败绝不阻断决策落账
            state.append_transcript("research_intake_record_failed", source="decision_note")

    if chosen == "retry_reviewer":
        # v3.8（E2E-4 实测活锁）：同一个 producer run 的 reviewer 重试必须有上限。
        # 现场：decision → retry_reviewer → reviewer APPROVE → 又 decision →
        # 又 retry_reviewer …… 连续 5 轮，literature 的 survey_report 被反复重审，
        # 每轮 reviewer 都给 APPROVE(4/5)，流水线一步都没往前走。
        #
        # 为什么现有熔断全都够不着：整个循环发生在**一个 orchestrator run 内部的
        # pause/resume**里（run_end=0、continuous_followup_scheduled=0），而乒乓 /
        # 停滞 / 重复失败熔断都在 _continuous_followup —— 那是**轮之间**执行的。
        # 这是 PR#193 那条教训的原样复现："轮间熔断够不着主路径"。而且
        # review_retry_succeeded_chain_reset 每轮重置链计数，让循环对计数器隐形。
        # 所以上限必须钉在**授权这一刻**。
        _attempts = _effective_retry_attempts(state, entry)
        if _RETRY_REVIEWER_MAX > 0 and _attempts >= _RETRY_REVIEWER_MAX:
            entry["decision_state"] = "awaiting_human"
            entry["accepted_action"] = None
            entry["decision_validation_error"] = (
                f"⛔ 同一个 producer run（{producing_run_id}）的 reviewer 重试已达上限"
                f"（{_attempts} 次）。再审一遍同一份产物不会产生新信息 —— 若前几轮"
                f"reviewer 都给了结论，那问题不在 review，在于**没有据此往下走**。\n"
                f"改选：PROCEED（接受并进入下一节点）/ REVISE（带反馈重跑 producer）/ "
                f"REDIRECT（退回上游）/ ABORT。"
            )
            # #202：封顶后下一次呈递必须把 PROCEED 放回菜单（见
            # _present_decision_package 的 _capped 分支），否则"重试封顶 +
            # review-failed 集无 PROCEED"= 人工无路可走。
            entry["review_retry_capped"] = True
            state.append_transcript(
                "reviewer_retry_capped", producing_run_id=producing_run_id,
                attempts=_attempts, cap=_RETRY_REVIEWER_MAX)
            return entry
        entry["decision_state"] = "done"
        entry["review_state"] = "retry_authorized"
        entry["retry_authorized_at"] = _now_iso()
        entry["retry_authorized_by"] = "human_decision_package"
        state.append_transcript(
            "reviewer_retry_authorized_by_human",
            producing_run_id=producing_run_id,
            attempt_number=entry.get("review_attempt_count", 0) + 1,
            previous_failure=entry.get("review_failed_reason"))
        return entry

    if chosen == "proceed":
        # #202 A2：PROCEED 意味着"带着已通过审查的产物进入下一阶段"，所以关闭
        # flow 前必须核对账本 —— 而不是听调用方说 review 过了。
        #
        # 两种情况严格区分：
        #   (a) 菜单**本不该**给 PROCEED（review 未过且重试没封顶）却收到了
        #       proceed —— 这正是自产 critique 撬开门的形态：拒绝，fail-closed，
        #       flow 不出列，下游继续被拦。
        #   (b) 重试已封顶（#208）→ 菜单合法地给了 PROCEED，人工知情下选择放行
        #       —— 允许，但如实记成**人工 override**，绝不记成"review 通过"。
        #       这是人的权力；框架的职责是别让它被伪装成"审查已通过"。
        _review_state = entry.get("review_state")
        if _review_state not in ("done", "skipped"):
            if not entry.get("review_retry_capped"):
                entry["decision_state"] = "awaiting_human"
                entry["accepted_action"] = None
                entry["decision_validation_error"] = (
                    f"⛔ 拒绝 PROCEED：账本记录本轮 review 未通过"
                    f"（review_state={_review_state!r}）。PROCEED 只能在审查真正"
                    f"通过后使用 —— 由被审查方/编排方自己写一份 review_critique "
                    f"并不能解除这道门。请先 run_node(node_type='_reviewer') 跑出"
                    f"有效 critique，或改选 REVISE / REDIRECT / ABORT。"
                )
                state.hook_state["pending_post_node_flow"] = flow
                state.append_transcript(
                    "decision_action_rejected",
                    producing_run_id=producing_run_id,
                    chosen_action=chosen,
                    reason="review_state_not_done",
                    review_state=_review_state,
                    flow_closed=False,
                )
                return entry
            # (b) 知情 override：留痕，且把状态记成 override 而非 done。
            entry["review_state"] = "human_override_without_review"
            entry["review_override_at"] = _now_iso()
            state.append_transcript(
                "review_gate_human_override",
                producing_run_id=producing_run_id,
                previous_review_state=_review_state,
                review_attempt_count=entry.get("review_attempt_count", 0),
                note=("人工在 reviewer 重试封顶后显式选择 PROCEED；本产物"
                      "**未经有效独立审查**，下游/论文不得声称已通过评审。"),
            )


    if chosen in {"revise", "redirect_upstream"}:
        target = entry.get("producing_node")
        if chosen == "redirect_upstream":
            # 自由填写/人工选择都可能选中 REDIRECT 而 reviewer 没给目标；reviewer
            # 也可能把根因指向一个不进 flow 账本的服务节点。两者都是**不可执行的
            # action_authorized**，判据同一个，答一次就够（见上面的谓词）。
            target = (
                (pause_metadata or {}).get("recommended_target_node")
                or entry.get("recommended_target_node")
            )
        _unexecutable = _why_this_target_cannot_close_the_flow(target)
        if _unexecutable is not None:
            entry["decision_state"] = "awaiting_human"
            entry["accepted_action"] = None
            entry["decision_validation_error"] = _unexecutable
            state.hook_state["pending_post_node_flow"] = flow
            state.append_transcript(
                "decision_action_rejected",
                producing_run_id=producing_run_id,
                chosen_action=chosen,
                requested_target_node=str(target or ""),
                reason=_unexecutable,
                flow_closed=False,
            )
            return entry
        if chosen == "redirect_upstream":
            # issue #166：记 redirect 边，供 run_node 起节点前检测踢皮球往返。
            _record_redirect_edge(state, entry.get("producing_node"), target)
        entry["decision_state"] = "action_authorized"
        entry["authorized_target_node"] = target
        entry["authorized_action"] = chosen
        entry["action_attempt_count"] = int(entry.get("action_attempt_count") or 0)
        entry["recommended_feedback"] = (
            (pause_metadata or {}).get("recommended_feedback")
            or entry.get("recommended_feedback")
        )
        state.hook_state["pending_post_node_flow"] = flow
        state.append_transcript(
            "decision_action_authorized",
            producing_run_id=producing_run_id,
            decision_id=decision_id,
            chosen_action=chosen,
            authorized_target_node=target,
            flow_closed=False,
        )
        return entry

    if chosen == "edit":
        entry["decision_state"] = "awaiting_manual_edit"
        state.hook_state["pending_post_node_flow"] = flow
        state.append_transcript(
            "decision_manual_edit_pending",
            producing_run_id=producing_run_id,
            decision_id=decision_id,
            flow_closed=False,
        )
        return entry

    if chosen == "abort":
        entry["decision_state"] = "done"
        entry["review_state"] = "aborted"
        state.append_transcript("decision_abort_recorded",
                                 producing_run_id=producing_run_id)
    else:
        # proceed is the only other terminal action in the normal action set.
        entry["decision_state"] = "done"

    # abort / proceed close this producer round.  Revision and redirect remain
    # active above until the authorized child actually produces a replacement.
    state.hook_state["pending_post_node_flow"] = [
        e for e in flow if e.get("producing_run_id") != producing_run_id]
    state.append_transcript(
        "decision_answer_recorded", producing_run_id=producing_run_id,
        decision_id=decision_id, chosen_action=chosen, flow_closed=True)
    return entry


# ── redirect 账本 + 踢皮球检测（issue #166 第 1 条）─────────────────────────
#
# 背景：experiment 侧有硬门禁「前处理产物缺失必须 redirect_upstream: data」，
# 而 data 侧原本零 QC、可以自由地以「这是计算任务」退回 → 两个节点无限对踢，
# 任务卡死（e2e 实测 POSCAR 生成任务）。框架此前**没有任何 redirect 账本**：
# redirect 全靠 orchestrator LLM 读 prompt 记着执行，所以「我们刚才已经踢过
# 一轮了」这个事实在框架层根本不可见，自然也无从仲裁。
#
# 判据：正常流程是 A --redirect--> B，B 产出，然后 orchestrator **重跑** A
# （重跑不是 redirect）。所以「B 又 redirect 回 A」本身就是异常信号 —— 说明
# 两个节点对同一份工作的归属没有共识。检测到反向边即判定踢皮球，交人工仲裁
# 归属，而不是让框架继续弹。
_REDIRECT_LEDGER_KEY = "_redirect_ledger"
_REDIRECT_LEDGER_MAX = 20


def _record_redirect_edge(state: State, source: str | None, target: str | None) -> None:
    """把一次 redirect 记进 orchestrator 级账本（hook_state 跨子 run 持久）。"""
    if not source or not target or source == target:
        return
    ledger = state.hook_state.setdefault(_REDIRECT_LEDGER_KEY, [])
    ledger.append({"from": source, "to": target, "at": _now_iso()})
    del ledger[:-_REDIRECT_LEDGER_MAX]
    state.append_transcript("redirect_edge_recorded", source=source, target=target,
                            ledger_size=len(ledger))


def detect_redirect_pingpong(state: State, node_type: str | None = None) -> dict | None:
    """账本里是否存在互为反向的 redirect 对（A→B 且 B→A）。

    node_type 给定时只报与该节点相关的那一对（run_node 起某节点前调用）。
    返回 {"pair": [A, B], "edges": [...]}；无往返返回 None。
    """
    ledger = state.hook_state.get(_REDIRECT_LEDGER_KEY) or []
    edges = {(e.get("from"), e.get("to")) for e in ledger
             if e.get("from") and e.get("to")}
    for src, dst in sorted(edges):
        if (dst, src) not in edges:
            continue
        if node_type is not None and node_type not in (src, dst):
            continue
        return {
            "pair": sorted([src, dst]),
            "edges": [e for e in ledger
                      if {e.get("from"), e.get("to")} == {src, dst}],
        }
    return None


def clear_redirect_pingpong(state: State, pair: list[str]) -> None:
    """人工仲裁归属后清掉这一对的账本，让流程能继续（否则永久卡死）。"""
    pair_set = set(pair or [])
    ledger = state.hook_state.get(_REDIRECT_LEDGER_KEY) or []
    state.hook_state[_REDIRECT_LEDGER_KEY] = [
        e for e in ledger if {e.get("from"), e.get("to")} != pair_set]
    state.append_transcript("redirect_pingpong_cleared", pair=sorted(pair_set))


# ── 权威决定驱动执行（issue #183）───────────────────────────────────────────
#
# #155 让框架**确知**人工选了什么（account 侧），但 resume 仍只把人工原始文本
# （常常就是一个裸数字 "1"）回填给 orchestrator LLM，动作由 LLM 自行重新解读。
# qinp 2026-07-26 PODsys canonical E2E 实测事故：review-failed 专用选项集里
# [1] = RETRY REVIEWER（该选项集**根本没有 PROCEED**），账本已正确记
# accepted_action=retry_reviewer，但 glm 按最常见的 "[1] 继续" 套用，复述成
# "用户选择 PROCEED"，把绑定 task 标 complete、对人谎报 review 已放行，且
# 从未真的起 _reviewer。这是一次 fail-open：review 门被静默突破。
#
# 修法（本模块提供，接线在 pause_driver / agent_loop）：
#   `describe_recorded_decision()` —— resume 回填时附上框架已解析的权威动作，
#   让 LLM 不可能把裸数字看歪。
# 执行侧的闸在 run_node 派发前查 `pending_post_node_flow`。原来这里还有一道
# `blocking_decision_for_task()` 拦 task(complete) —— task 清单是节点私账，标
# complete 不改变任何执行事实，那道闸只制造第二份判决（判决拆除·第三波删）。


def describe_recorded_decision(entry: dict | None) -> str | None:
    """把已机械记账的人工选择渲染成**无歧义**的一句话，供 resume 回填给 LLM。

    返回 None 表示没有可用的结构化决定（照旧只回原文，不改行为）。
    """
    if not entry:
        return None
    action = entry.get("accepted_action")
    if not action:
        return None
    actions = entry.get("decision_options") or _NORMAL_ACTIONS
    labels = (_OPTION_LABELS_REVIEW_FAILED
              if actions == _REVIEW_FAILED_ACTIONS
              else _OPTION_LABELS_BASE)
    try:
        idx = actions.index(action)
        label = labels[idx] if idx < len(labels) else action
        return f"[{idx + 1}] {label}（规范动作：{action}）"
    except (ValueError, IndexError):
        return f"（规范动作：{action}）"


register_tool(
    ToolDefinition(
        name="present_decision_package",
        description=(
            "Post-producing decision point —— **只在 _orchestrator 用**。\n\n"
            "每个 producing 节点完后流程：\n"
            "  1. run_node('_reviewer', ...)       → review_critique artifact\n"
            "  2. run_node('_curator', ...)         → KB 整合\n"
            "  3. **present_decision_package(...)** ← 本工具\n"
            "         → unwind 给 user 看 ASCII 报告 + 5 选项\n"
            "         → user 输入 1-5，或 auto-approve 倒计时 5 秒后自动选 recommended\n\n"
            "**5 个固定选项**（v0.5 起，新增 [3] REDIRECT）：\n"
            "  [1] PROCEED to next stage\n"
            "  [2] REVISE (re-run source_node with reviewer feedback)\n"
            "  [3] REDIRECT to upstream（re-run upstream node X，then re-run source_node）\n"
            "  [4] ABORT pipeline\n"
            "  [5] EDIT manually then proceed\n\n"
            "**Recommended option index** 自动从 review_critique 的 recommended_action 推：\n"
            "  proceed → 0；revise → 1；redirect_upstream → 2；abort → 3；escalate_to_human → 4\n\n"
            "**REDIRECT 场景**：reviewer 判定 source_node 已尽力，根因在更上游 "
            "（典型例：experiment 输出弱，根因在 hypothesis prereg 不严谨；"
            "hypothesis 设计 plan 时 baseline 不清，根因在 literature 覆盖不全）。"
            "此时 reviewer 写 `recommended_action.action='redirect_upstream'` + "
            "`target_node='<upstream>'` + `feedback_to_next_run='<focused query>'`。"
            "orchestrator 收到 [3] 选择后调上游节点带这个 focused query，跑完整 "
            "post-producing flow，然后回头重跑 source_node。\n\n"
            "**Reviewer 失败的 fallback**：传 review_failed_reason，工具仍渲染包，"
            "标 `⚠️ review failed, proceeding without`。\n\n"
            "**Owner opt-out**：source_node 设了 skip_post_node_review 时，"
            "传 review_critique_artifact_id=null + review_failed_reason=null，"
            "工具渲染 'Review skipped (owner opted out)'。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "source_node_type": {
                    "type": "string",
                    "description": "刚完的 producing node type",
                },
                "producing_run_id": {
                    "type": "string",
                    "description": "producing 子 run 的 id",
                },
                "producing_summary": {
                    "type": "string",
                    "description": "producing 节点 final text 摘要（可选）",
                },
                "artifact_ids_produced": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "producing 节点产的 artifact id 列表",
                },
                "curator_summary": {
                    "type": "string",
                    "description": "curator 整合摘要文本（可选）",
                },
                "review_critique_artifact_id": {
                    "type": "string",
                    "description": ("_reviewer 产的 review_critique artifact id"
                                     "（reviewer 跑挂则 null）"),
                },
                "review_failed_reason": {
                    "type": "string",
                    "description": "reviewer 失败原因（成功则 null）",
                },
            },
            "required": ["source_node_type", "producing_run_id"],
        },
        risk_level="low",
    ),
    _present_decision_package,
)
