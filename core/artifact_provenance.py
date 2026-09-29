"""工件来源 —— 一份工件是**谁产的**、以及它是不是这个平台产的。

为什么要有这个模块（2026-08-04，e2e8 实测）：

用户给了一批做完的研究材料（预注册/实验日志/原始数据），只要求"写成论文"。
调度器一路走对了 —— 过 writing-gate、拿到 `ready_to_write`、派 writing。然后
撞墙：

    Writing 节点要求 experiment_log artifact 作为输入。材料在 workspace 里但
    不在当前 run 的 artifacts 中。……但 experiment_log 是 experiment 节点的专属
    产出——我不能新建。……我需要先起 experiment 节点让它产出 experiment_log。
    但这样会重复跑实验……让我看看能否用 "replay" 或 "register_only" 模式。

它**主动想避免重跑，找不到合法出口**。根因是 `required_input_artifact_types`
这道机械门把两件事混成了一件：

    "有没有实验记录"        ← 门真正该管的（防编数据，必须保留）
    "这个平台跑没跑过实验"  ← 门实际在管的（不该管 —— 拿别人的数据写论文是常态）

所以要有第三种来源：`imported`。但**放宽入口的同时必须锁死出口** —— 否则
"导入一份 experiment_log" 就成了绕过防编数据门禁的新后门。

## 不许被洗白

转发路径（executor 注入上游 artifact、run_node 回填子产物）都调
`state.save_artifact()`，而它此前无条件盖 `produced_by_node_type=self.node_type`
—— **接收方**。也就是说上游 experiment_log 转发进 writing 子 run 之后就变成
"writing 产的"了。真来源只在 `_import_required_outputs` 那条路上靠 metadata
键侥幸保住（decision_package 读的时候得先试 metadata 再退顶层，就是这个原因）。

如果 `imported` 走同样的路，它会在第一次转发时被洗成 `produced`。那我加的
不是入口，是 fail-open。

所以来源改成 record 顶层的一等公民 `provenance` 块，且：

  - 默认（不传）= 本 run 自己产的，跟以前一样
  - 转发/回填 = `forwarded`，**保留原产出方**，不冒名
  - 导入 = `imported`，带源路径 + sha256，`by_node_type` 恒为 `_import`
  - `is_imported()` 顺着 forwarded 链一路看到底 —— 转发多少次都还是导入的

顶层 `produced_by_node_type` / `produced_by_run_id` 保留（老消费方在读），但
改为**从 provenance 派生**，两者不可能对不上。

判据还是那条：证据可以持久化，判决不可以。"这份数据是外部的"是证据，必须
跟着工件走到底；"所以这篇论文可信"是判决，谁用谁现算。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

KIND_PRODUCED = "produced"
KIND_IMPORTED = "imported"
KIND_FORWARDED = "forwarded"

#: 导入的工件不冒充任何 producing 节点。用一个不存在的节点名占位，任何
#: "这个类型必须由 X 节点产出"的机械校验都会照常判它不是 X。
IMPORT_NODE_TYPE = "_import"

_ALL_KINDS = frozenset({KIND_PRODUCED, KIND_IMPORTED, KIND_FORWARDED})


def produced(node_type: str | None, run_id: str | None) -> dict:
    """本 run 自己产的 —— save_artifact 不传 provenance 时的默认。"""
    return {
        "kind": KIND_PRODUCED,
        "by_node_type": node_type,
        "by_run_id": run_id,
    }


def imported(*, source_path: str, sha256: str, size_bytes: int,
             by_run_id: str | None, note: str | None = None) -> dict:
    """外部带进来的材料 —— 平台没跑过。"""
    p = {
        "kind": KIND_IMPORTED,
        "by_node_type": IMPORT_NODE_TYPE,
        "by_run_id": by_run_id,
        "source_path": source_path,
        "source_sha256": sha256,
        "source_size_bytes": size_bytes,
    }
    if note:
        p["note"] = note
    return p


def forwarded(source: dict | None, *, via_node_type: str | None,
              via_run_id: str | None) -> dict:
    """转发/回填 —— 谁转的是新信息，**谁产的不变**。

    `source` 是被转发那份 record 的 provenance。拿不到（老工件没有这个块）
    就退回 produced(via_*)，跟改动前的行为一致，不凭空编来源。
    """
    if not isinstance(source, dict) or source.get("kind") not in _ALL_KINDS:
        return produced(via_node_type, via_run_id)
    out = dict(source)
    out["kind"] = KIND_FORWARDED
    out["via_node_type"] = via_node_type
    out["via_run_id"] = via_run_id
    # 原始 kind 只在第一次转发时记下；再转发不覆盖，否则链条中段丢信息
    out.setdefault("origin_kind", source.get("origin_kind") or source["kind"])
    return out


def of(record: dict | None) -> dict | None:
    """取一份 artifact record 的 provenance；老工件没有就返 None。"""
    if not isinstance(record, dict):
        return None
    p = record.get("provenance")
    return p if isinstance(p, dict) else None


def is_imported(record: dict | None) -> bool:
    """这份工件（顺着转发链看到底）是不是外部导入的。

    转发不改变这个答案 —— 这正是本模块存在的理由。
    """
    p = of(record)
    if not p:
        return False
    return KIND_IMPORTED in (p.get("kind"), p.get("origin_kind"))


def true_producer(record: dict | None) -> tuple[str | None, str | None]:
    """(node_type, run_id) —— 真正产出这份工件的人，不是转发它的人。"""
    p = of(record)
    if not p:
        # 老工件：退回顶层字段（改动前那套）
        rec = record or {}
        return rec.get("produced_by_node_type"), rec.get("produced_by_run_id")
    return p.get("by_node_type"), p.get("by_run_id")


def describe(record: dict | None) -> str:
    """给提示词/报告用的一句话来源说明。"""
    p = of(record)
    if not p:
        return "来源未记录（本框架 provenance 之前的旧工件）"
    if is_imported(record):
        src = p.get("source_path") or "?"
        sha = (p.get("source_sha256") or "")[:12]
        return f"外部导入（本平台未产出）：{src}  sha256:{sha}"
    node, run = true_producer(record)
    via = p.get("via_node_type")
    base = f"本平台产出：{node} @ {run}"
    return f"{base}（经 {via} 转发）" if via else base


def hash_file(path: Path) -> tuple[str, int]:
    """(sha256, size_bytes) —— 分块读，不把大文件整个吞进内存。"""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def imported_inputs(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从一批 record 里挑出导入的 —— 消费方据此决定要不要如实披露。"""
    return [a for a in artifacts if is_imported(a)]
