"""这份产物能不能当**确证证据**用（#1097 第 5 条）。

## 现场

Experiment 的一个 operation run 可以收尾完整、审计通过，而它做的只是装环境、编译、
跑通。节点已经把这件事如实写进 artifact metadata：
`upstream_goal_effect=operational_subtask_only`。

但在 `5ae74712` 上 `git grep upstream_goal_effect` 显示：除了 Experiment 自己的生产
代码与自审之外，**下游一个消费方都没有**。也就是说，写作、审稿、综合这些会把实验
产物当证据的入口，读到它和读到一份真的确证结果**一模一样**。

## 这条防线管什么、不管什么

管的是**引用时被洗成确证**：一份自称"只是运维子任务"的产物，不得用来支持/反驳预注册
命题，也不得用来兑现 confirmatory closure。它仍然可以作为运维事实、方法/构建依据，
或明确标注为 post-hoc 的材料 —— 那些用途是正当的，一刀切会把它变成"什么都不能提"。

不管的是**开工前的用途**：一个 run 开工时声称做科学、跑完了改口说是运维，这条防线
看不出来。那是 admission-frozen use receipt 要答的问题，两者不互斥。

## 为什么放在 shared/lib

因为它必须**只有一份**。让每个下游节点各写一句 `metadata.get("upstream_goal_effect")
== "operational_subtask_only"`，就是同一个判据的 N 份抄件，而抄件会各自演化、分叉时
不报错（[[feedback_one_truth_source_per_question]]）。
"""
from __future__ import annotations

from typing import Any

#: 产物 metadata 上那个字段的名字。**这里是唯一出处**，下游别各自写字符串。
EFFECT_KEY = "upstream_goal_effect"

#: 不得作为确证证据的效应取值。
_NOT_CONFIRMATORY = frozenset({"operational_subtask_only", "no_effect"})

#: 允许的用途（给模型看的那句话里要逐条说清 —— 只说"不行"会让它无路可走）。
LEGITIMATE_USES = (
    "运维事实（装了什么、编译过没过、跑通没跑通）",
    "方法或构建依据（怎么搭起来的）",
    "**明确标注为 post-hoc** 的材料",
)


def declared_effect(record: Any) -> str:
    """读出这份产物自己声明的效应；没声明返回 ""（**不猜**）。"""
    if not isinstance(record, dict):
        return ""
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get(EFFECT_KEY) or "").strip()


def may_serve_as_confirmatory_evidence(record: Any) -> bool:
    """这份产物能不能当确证证据用。

    没声明效应 → **能**（绝大多数产物本来就没有这个字段，默认拦下会把整个仓库的
    证据链一刀切断）。声明了且在禁用集合里 → 不能。
    """
    return declared_effect(record) not in _NOT_CONFIRMATORY


def confirmatory_use_note(record: Any) -> str | None:
    """要跟着这份产物一起交到读它的人手里的那句话；不需要时返回 None。

    这句话不是"拒绝"，是**归属**：它说清这份产物能用来干什么、不能用来干什么。
    只说不行会逼着模型要么无视它，要么把整条线停掉。
    """
    effect = declared_effect(record)
    if not effect or effect not in _NOT_CONFIRMATORY:
        return None
    return (
        f"⚠️ 这份产物自己声明 `{EFFECT_KEY}={effect}` —— 它**不是**确证证据。\n"
        "  不得用它支持或反驳预注册命题，也不得用它兑现 confirmatory closure。\n"
        "  可以用作：" + "；".join(LEGITIMATE_USES) + "。\n"
        "  真要拿它下科学结论，先让产出它的节点做一次以科学为目的的执行。"
    )
