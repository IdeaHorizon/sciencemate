"""科学裁决权：假说被支持 / 证伪，由 Analysis 说了算。

v2.1 之前，experiment 既执行实验、又直接把 KB 里的 hypothesis claim 翻成
validated / refuted。围绕"它会不会翻错"长出了一层又一层补丁：声明
infeasible 就不许翻（防循环证伪）、prereg 合取项没测全不许翻、翻转必须挂
证据、同 session 翻转次数上限……每一条都对，但它们都在补同一个洞：

**做实验的人不该同时是裁决自己实验的人。**

v2.1 把裁决权交给 Analysis，落点是 research_state ——
一份版本化、假说状态与证据成链、有自己完整性门禁的产物。experiment 仍然
测量、仍然做执行层自查（数据可信度、统计规范、执行诚实），但"这条假说成
不成立"的判断要经过 Analysis。

这里就是那道权威边界：producing 节点想把 hypothesis claim 翻成
validated/refuted，当前 research_state 必须已经记下同向裁决。

**为什么"没有 research_state 就放行"不是后门**：scientific verdict 路径
本身要求 prereg 已冻结（resolve_prereg_hypotheses 走冻结 provenance），而
hypothesis 的收尾闸在冻结后强制产出 research_state。所以任何科学 run 里
research_state 必然存在 —— 放行分支只覆盖没有 Analysis 参与的历史/工程
路径，不是可绕的缝。
"""

from __future__ import annotations

import json
from typing import Any

#: KB claim 状态 → research_state 里的假说状态。
#: inconclusive / provisional 不在此列 —— 它们本来就不是"裁决成立"，
#: 不需要 Analysis 背书。
_REQUIRED_ANALYSIS_STATUS = {
    "validated": "supported",
    "refuted": "refuted",
}

#: Analysis 的产物落在项目工作区里这个节点目录下。
_ANALYSIS_WORKSPACES = ("hypothesis", "analysis")


def _load_latest_research_state(state: Any) -> dict[str, Any] | None:
    """读项目工作区里最新一版 research_state（唯一实现见 research_state_reader）。

    跨节点读取 —— 调用方通常是 experiment，它自己的节点目录是
    `experiments/`。Analysis 的目录才是权威所在。
    """
    from core import research_state_reader as _rs

    worktree = getattr(state, "project_worktree", None)
    found = _rs.latest(worktree)
    return found[1] if found else None


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    meta = record.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return {}
    return meta if isinstance(meta, dict) else {}


def analysis_verdict_for(state: Any, hypothesis_id: str) -> dict[str, Any] | None:
    """返回 research_state 里这条假说的记录（没有则 None）。"""
    record = _load_latest_research_state(state)
    if record is None:
        return None
    rows = _metadata(record).get("hypotheses")
    if not isinstance(rows, list):
        return None
    wanted = str(hypothesis_id or "").strip()
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == wanted:
            return row
    return None


def _evidence_record_type_of(node_type: str | None) -> str:
    """这个节点的证据记录叫什么 —— **现算，不写死**。

    三处报错原来硬编码 `experiment_log`。对 observation / derivation 来说
    那是**它们产不出来的类型**：报错把调用方指向一个它够不着的东西，
    等于告诉它"去做一件做不到的事"（契约必须送到调用方，而且得是对的那份）。

    判据从两处现算的交集来：节点 harness 声明的 required_output_artifact_types
    ∩ 注册表里的 evidence_record 类型。新模态一声明就自动正确 ——
    护栏要扫盘，不要写名单。

    取不到就退回泛称。**报错降级成不那么精确是可以接受的，
    指向错误的类型不行。**
    """
    fallback = "你的证据记录"
    if not node_type:
        return fallback
    try:
        from core.loader import load_harness
        from shared.lib.artifact_policy import evidence_record_types

        declared = load_harness(str(node_type)).required_output_artifact_types or []
        evidence = set(evidence_record_types())
        hit = [t for t in declared if t in evidence]
        return hit[0] if hit else fallback
    except Exception:
        return fallback


def scientific_verdict_block(
    state: Any,
    *,
    hypothesis_id: str | None,
    new_status: str | None,
) -> str | None:
    """要拦就返回给模型看的理由；放行返回 None。

    只管 producing 节点。`_curator` dreaming 等治理路径与架构节点不受约束。
    """
    required = _REQUIRED_ANALYSIS_STATUS.get(str(new_status or ""))
    if required is None:
        return None
    node_type = str(getattr(state, "node_type", "") or "")
    if node_type.startswith("_") or node_type in _ANALYSIS_WORKSPACES:
        return None

    # "查不了" 不等于 "没什么可查"。没绑 project worktree 就够不着 Analysis 的
    # 目录，此时放行等于：**这条路径下整道裁决权限门根本不存在**，做实验的人
    # 可以随手给自己预注册的假说下结论。v2.1 下每个 producing 节点的 run 都绑
    # worktree，绑不上说明配置坏了 —— 那正是最该拦的时候。
    #
    # 只拦点名了 hypothesis_id 的裁决：research_state 这个权威源只对预注册假说
    # 说话。没有工作区的 run 去翻 methodological / observational claim 不在它
    # 的管辖范围内，照旧放行，别把无关路径一起拦了。
    evidence_type = _evidence_record_type_of(node_type)

    if hypothesis_id and not getattr(state, "project_worktree", None):
        return (
            f"⛔ 无法核对科学裁决权：本次 run（{node_type or '未知节点'}）没有绑定 "
            f"project worktree，读不到 Analysis 的 research_state，因此无法确认 "
            f"{hypothesis_id} 现在是什么状态。\n"
            f"在核不了的情况下不能翻成 {new_status!r} —— 把结果与理由写进 "
            f"{evidence_type} 交回去，由 Analysis 更新 research_state。"
        )

    record = _load_latest_research_state(state)
    if record is None:
        # 这里是**查得了、确实没有**：worktree 在，Analysis 目录里没有任何
        # research_state → 这个项目从没经过 Analysis（历史/工程路径），保持旧行为。
        return None

    meta = _metadata(record)
    version = meta.get("version")
    if not str(hypothesis_id or "").strip():
        return (
            f"⛔ 把 claim 翻成 {new_status!r} 必须指名 hypothesis_id —— "
            f"本项目已有 research_state v{version}，裁决要与它对得上。"
        )

    row = analysis_verdict_for(state, str(hypothesis_id))
    if row is None:
        return (
            f"⛔ research_state v{version} 里没有 {hypothesis_id!r} 这条假说，"
            f"不能把它翻成 {new_status!r}。\n"
            f"科学裁决权在 Analysis：先把结果写进 {evidence_type} 交回去，"
            f"由 Analysis 更新 research_state（那里会机械校验证据），再回来翻 claim。"
        )

    actual = str(row.get("status") or "")
    if actual != required:
        return (
            f"⛔ research_state v{version} 记的是 {hypothesis_id}={actual!r}，"
            f"你要翻成 {new_status!r}（需要 {required!r}）。\n"
            f"取证的人不是裁决自己取证结果的人 —— 若你认为证据支持改判，"
            f"把结果与理由写进 {evidence_type}，由 Analysis 出新一版 research_state。"
        )
    return None
