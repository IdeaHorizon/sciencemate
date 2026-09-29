"""上游返工路由（v3.6）—— 让"我被上游卡住了"成为可执行的一等动作。

## 为什么

三轮 E2E 的同一个根因反复发作：**节点只能对自己的产物负责，卡住时只会重跑自己**。
实测数据（三轮 reviewer 裁决 / decision 执行）：

    E2E#1   redirect 建议 5 次 → 实际执行 0 次；revise 执行 74 次
    E2E#2   redirect 建议 0 次 → proceed 说了 110 次（286 run 的乒乓期间）
    E2E#3   redirect 建议 2 次 → 执行 3 次（链路本身是通的）

发作现场：
  - writing 反复重写，其实是 literature 没把被引论文入库；
  - curator 反复查 KB，其实是 literature 的 7/16 chunk 没接 author；
  - reviewer 说"根因在上游"但漏填 target_node → 框架**静默改判 revise**，
    诊断结论被 schema 疏漏抹掉（这就是 E2E#1 那 5 次的去向）。

结论：路是建好的（redirect_upstream 裁决 + decision REDIRECT 选项），缺三样：
  ③ 建议不许被静默降级；① 重复失败要能机械转成 redirect；② 当事节点要有申诉权。
本模块提供三者共用的**依赖图推断**与**候选目标**能力。

## 依赖图从哪来

各节点 harness.yaml 已声明 `required_input_artifact_types` /
`required_output_artifact_types`，据此可机械推出 "谁产出我需要的东西"。
注意这只是**近似**：writing 依赖 experiment_log（→experiment），但"引用是编的"
真正该退回 literature。所以：
  - 节点主动申诉（②）时**以申诉者指名的目标为准**（它最清楚缺什么）；
  - 机械触发（①）时框架**不猜**，只给候选集并强制上层选择 + 说明理由。
"""
from __future__ import annotations

import logging

log = logging.getLogger("upstream_routing")

# 研究流水线的常规顺序（依赖图推不出时的兜底候选，按"越靠上游越靠前"排）
PIPELINE_ORDER: tuple[str, ...] = (
    "literature", "hypothesis", "data", "experiment", "observation", "derivation",
    "postprocess", "writing",
)


def _load(node_type: str):
    from core.loader import load_harness
    try:
        return load_harness(node_type)
    except Exception as e:      # 节点缺失/yaml 坏了不该炸掉调度
        log.debug("load_harness(%s) failed: %s", node_type, e)
        return None


def producer_map() -> dict[str, str]:
    """artifact_type → 产出它的 node_type（同类型多产者时取流水线更上游的）。"""
    out: dict[str, str] = {}
    for nt in PIPELINE_ORDER:
        h = _load(nt)
        if h is None:
            continue
        for t in (h.required_output_artifact_types or []):
            out.setdefault(t, nt)      # PIPELINE_ORDER 保证先到者更上游
    return out


def upstream_candidates(node_type: str) -> list[str]:
    """给定节点的上游候选（依赖图优先，其次流水线顺序），已去重去自身。

    依赖图：本节点 required_input_artifact_types 的产出者。
    兜底：流水线里排在它前面的所有 producing 节点 —— 覆盖"真正症结不在直接
    上游"的情况（如 writing 的引用问题其实要回 literature）。
    """
    from core.loader import node_owes_post_node_flow

    h = _load(node_type)
    prod = producer_map()
    ordered: list[str] = []

    def _add(n: str) -> None:
        # 候选必须是**跑完能把 flow 关掉**的节点。REDIRECT 授权出去之后，
        # run_node 里的绑定/空转计数/闭合三件事都挂在 node_owes_post_node_flow
        # 上；服务节点（post_run_flow: none）满足不了它，被选中就成了一条永远
        # 关不掉、也不被熔断看见的义务。
        #
        # 2026-09-17 yuankk 那条会话就是这么来的：PIPELINE_ORDER 这张手写名单里
        # 还留着 postprocess，而 postprocess 早已改造成 figures 服务 —— 框架自己
        # 把一个不可执行的目标摆进了候选集，reviewer 照单选了，然后空转 40 轮。
        # 删掉名单里那一项是打补丁：下一个改成服务的节点照样漏。判据要机械。
        if n and n != node_type and n not in ordered and node_owes_post_node_flow(n):
            ordered.append(n)

    if h is not None:
        for t in (h.required_input_artifact_types or []):
            _add(prod.get(t) or "")
    if node_type in PIPELINE_ORDER:
        idx = PIPELINE_ORDER.index(node_type)
        for n in PIPELINE_ORDER[:idx]:
            _add(n)
    return ordered


def infer_redirect_target(node_type: str) -> str | None:
    """依赖图能唯一确定的直接上游（用于 reviewer 漏填 target 时的补全）。

    只在**唯一**候选时返回；有歧义就返回 None，交由上层显式选择 ——
    宁可要一次澄清，也不要猜错方向后又白跑一轮。
    """
    cands = upstream_candidates(node_type)
    return cands[0] if len(cands) == 1 else None
