"""Blocker 记录 —— 「这一趟没做成，原因是这个」的结构化落盘。

## 为什么在 core 而不是在工具里

`shared/tools/library/blockers.py` 的 `report_blocker` 是**给模型用的入口**，
但 blocker 本身不是工具的私有数据结构：`core/executor.py` 读
`hook_state["blockers"]` 决定 run 的终态（有 blocker → `status="blocked"`），
`core/dispatch_gate.py` 读它决定要不要再派一次，`core/run_history.py` 把它
读成历史。也就是说**消费方全在 core**，只有生产方在工具层。

于是框架自己也需要能记一条 blocker：

  - 病态重复熔断（`core/tool_call_cache`）停机时，得让 orchestrator 知道
    这一趟为什么没结果 —— 否则终态只是一句 `failed`，下游还得再猜一遍。
  - data 节点交了 blocked report 却没交 required delivery 时，同理。

这两处如果各自照着工具里那份 dict 手抄一遍字段，就是三份会各自演化的
"blocker 长什么样"。所以形状定义在这里，工具层只做**参数校验 + 面向模型的
文案**，两边共用同一个构造函数。
"""
from __future__ import annotations

from typing import Any

#: 合法 category。这份枚举同时是 `report_blocker` 工具 schema 里的 enum ——
#: 一处定义，工具那边引用，不另抄一份。
CATEGORIES = frozenset({
    "missing_input",
    "missing_capability",
    "environment",
    "permission",
    "upstream_quality",
    "external_job",
    "scientific_uncertainty",
    "other",
})


def record_blocker(
    state: Any,
    *,
    summary: str,
    category: str = "other",
    evidence_paths: list[str] | None = None,
    requested_action: str = "",
    suggested_owner: str = "",
    retryable_after_change: bool = True,
    reported_by: str | None = None,
) -> dict:
    """把一条 blocker 记进 `hook_state["blockers"]` + transcript，返回该条记录。

    `reported_by` 只在**框架自己**登记时传（值形如 `"framework:tool_call_cache"`）：
    模型主动申报的 blocker 与框架代记的 blocker 在归因上完全不同，读的人必须
    能分开。模型走 `report_blocker` 工具时不传，字段就不存在。
    """
    blockers = state.hook_state.setdefault("blockers", [])
    # run_id / node_type 只是**标签**：真 State 永远有，测试替身/最小 state 可能
    # 没有。缺标签时照常登记 —— blocker 落进 hook_state 才是这个函数的作用，
    # 为了两个名字把整条登记扔掉才是 fail-open。
    blocker = {
        "blocker_id": f"{getattr(state, 'run_id', '') or '?'}:{len(blockers) + 1}",
        "reporting_node": getattr(state, "node_type", "") or "",
        "category": category if category in CATEGORIES else "other",
        "summary": str(summary or "")[:4000],
        "evidence_paths": [str(p) for p in (evidence_paths or []) if str(p).strip()][:50],
        "requested_action": str(requested_action or "")[:4000],
        "suggested_owner": str(suggested_owner or "")[:200],
        "retryable_after_change": bool(retryable_after_change),
    }
    if reported_by:
        blocker["reported_by"] = str(reported_by)[:200]
    blockers.append(blocker)
    try:
        state.append_transcript("blocker_reported", **blocker)
    except Exception:
        pass   # 记账失败不该反过来毁掉调用方（登记本身已经在 hook_state 里了）
    return blocker
