"""子 run 跑完了，到底**关掉了父任务的哪一部分**（#1097 第 4 / 5 条）。

## 为什么 `completed` 不够

Experiment 的一个 operation run 可以收尾完整、审计通过，而它做的只是"装环境、
编译、跑通"——`upstream_goal_effect=operational_subtask_only`。这件事今天已经写在
节点的日志、closure input、artifact metadata 和 clean payload 里，但**没有任何一条
到得了父 run**：ROC 返回没有、`run_node` 的结构化返回没有、decision package 只拿到
`final_text_preview[:800]` 的自由文本、结果压缩器的白名单会把新对象整个丢掉。

于是父侧看到 child `completed`，就把整个 scientific objective 当成完成了。

## 这里定义两样东西

1. **一个 canonical 对象**（`ChildObligationEffect`），沿这条链逐跳传递：

       frozen closure receipt → ROC 返回 / child durable event → child summary
       → run_node 返回 → 父 durable ledger → decision package / 父 obligation reducer

   任何一跳丢掉它，父侧就退回"只看 status"。所以每一跳都要有判据钉着（#1097 验收 7）。

2. **父侧 reducer**（`remaining_obligation`）：拿 `TaskContractRevision × effect`
   算"还欠什么"。child 的生命周期 `completed` 只能关掉它**自己声明关掉的那部分**；
   父任务若还有 scientific objective，保持 open。

## 一条纪律

自由文本 summary 只用来展示，**不作 reducer 输入**。读一段话判断"它是不是把科学
目标做完了"，就是把机械判据换成了模型的理解 —— 而那正是这条 issue 的病因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

#: 子 run 对父任务目标的效应。缺席（None）= 没报告，**不等于"整个做完了"**。
EFFECT_FULL = "scientific_objective"        # 这一趟推进的就是科学目标本身
EFFECT_OPERATIONAL = "operational_subtask_only"   # 只是运维/构建子任务
EFFECT_NONE = "no_effect"                   # 什么也没推进（诊断、探查）
EFFECTS = (EFFECT_FULL, EFFECT_OPERATIONAL, EFFECT_NONE)

#: 这一趟给科学结论贡献了什么。
CONTRIBUTION_NONE = "none"
CONTRIBUTION_EVIDENCE = "evidence"
CONTRIBUTIONS = (CONTRIBUTION_NONE, CONTRIBUTION_EVIDENCE)


@dataclass(frozen=True)
class ChildObligationEffect:
    """子 run 的收尾收据**派生**出来的那个对象。它不是模型写的一句话。"""

    upstream_goal_effect: str
    scientific_contribution: str = CONTRIBUTION_NONE
    task_contract_revision_digest: str = ""
    source_receipt: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_contract_revision_digest": self.task_contract_revision_digest,
            "upstream_goal_effect": self.upstream_goal_effect,
            "scientific_contribution": self.scientific_contribution,
            "source_receipt": dict(self.source_receipt or {}),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ChildObligationEffect | None:
        """读回一个 effect；**读不出来就是 None**，不造一个默认值。

        造默认值 = 替子 run 声明它做了什么，而这个对象存在的全部理由就是
        "别再从自由文本里猜这件事"。
        """
        if not isinstance(data, dict):
            return None
        effect = str(data.get("upstream_goal_effect") or "").strip()
        if effect not in EFFECTS:
            return None
        contribution = str(data.get("scientific_contribution") or "").strip()
        return cls(
            upstream_goal_effect=effect,
            scientific_contribution=(
                contribution if contribution in CONTRIBUTIONS else CONTRIBUTION_NONE),
            task_contract_revision_digest=str(
                data.get("task_contract_revision_digest") or ""),
            source_receipt=dict(data.get("source_receipt") or {}),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )

    @property
    def closes_scientific_objective(self) -> bool:
        return (self.upstream_goal_effect == EFFECT_FULL
                and self.scientific_contribution == CONTRIBUTION_EVIDENCE)


def remaining_obligation(revision: Any, effect: ChildObligationEffect | None,
                         *, child_status: str = "") -> dict[str, Any]:
    """父侧 reducer：这一趟之后，这个任务还欠什么。

    三条判据，机械：

    * child 没跑成（status 不是 completed）→ 什么都没关掉；
    * child 跑成了但**没报告** effect → 保守：不替它声明关掉了什么
      （「没报告」和「报告说全做完了」必须是两个答案）；
    * child 跑成了且报告了 → 只关掉它自己声明的那部分。

    `revision` 是 `TaskContractRevision`（或任何带 `assignment_kind` 的对象）。
    合同绑了确切 prereg（`exact_bound`）= 这个任务有科学目标；`explicit_none` =
    父明说这趟不绑，没有科学目标要关。
    """
    from core.task_contract import ASSIGNMENT_EXACT

    kind = getattr(revision, "assignment_kind", None) if revision is not None else None
    has_scientific_objective = kind == ASSIGNMENT_EXACT

    if child_status and child_status != "completed":
        return {
            "scientific_objective_open": has_scientific_objective,
            "operational_subtask_closed": False,
            "reason": f"子 run 终态是 {child_status}，没有关掉任何东西",
        }
    if effect is None:
        return {
            "scientific_objective_open": has_scientific_objective,
            "operational_subtask_closed": False,
            "reason": (
                "子 run 跑完了但没报告它关掉了什么 —— 不替它声明。"
                "「没报告」和「报告说全做完了」是两个答案。"),
        }
    closed_science = has_scientific_objective and effect.closes_scientific_objective
    return {
        "scientific_objective_open": has_scientific_objective and not closed_science,
        "operational_subtask_closed": True,
        "reason": (
            "科学目标已由这一趟的证据关闭" if closed_science else
            f"这一趟的效应是 {effect.upstream_goal_effect}"
            f"（科学贡献 {effect.scientific_contribution}）—— "
            + ("父任务的科学目标仍然开着" if has_scientific_objective
               else "这个任务本来就没有科学目标要关")),
        "effect": effect.as_dict(),
    }


def record_obligation_effect(
    state: Any,
    *,
    upstream_goal_effect: str,
    scientific_contribution: str = CONTRIBUTION_NONE,
    source_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """节点在收尾时登记这一趟的效应；`finalize_run` 原样写进 summary。

    与 `core/blockers.record_blocker`、`core/task_outcome.record_task_outcome`
    同形：消费方全在 core（reducer、decision package、压缩器白名单、父侧记账），
    生产方在节点。

    `task_contract_revision_digest` **由框架从 state 填**，不由调用方给 ——
    这一趟按哪份合同执行是派发时定死的事实，让被派的一方自己声明等于让它
    自己给自己发授权。

    词表外的取值直接 `ValueError`：静默改写调用方的结论，是这条轴最不该有的行为。
    """
    effect = str(upstream_goal_effect or "").strip()
    if effect not in EFFECTS:
        raise ValueError(
            f"upstream_goal_effect={upstream_goal_effect!r} 不是合法值。"
            f"合法值：{', '.join(EFFECTS)}")
    contribution = str(scientific_contribution or "").strip()
    if contribution not in CONTRIBUTIONS:
        raise ValueError(
            f"scientific_contribution={scientific_contribution!r} 不是合法值。"
            f"合法值：{', '.join(CONTRIBUTIONS)}")
    record = ChildObligationEffect(
        upstream_goal_effect=effect,
        scientific_contribution=contribution,
        task_contract_revision_digest=str(
            getattr(state, "task_contract_digest", "") or ""),
        source_receipt=dict(source_receipt or {}),
    ).as_dict()
    state.hook_state["child_obligation_effect"] = record
    try:
        state.append_transcript("child_obligation_effect_recorded", **record)
    except Exception:       # 记账失败不能把结论本身弄丢
        pass
    return record
