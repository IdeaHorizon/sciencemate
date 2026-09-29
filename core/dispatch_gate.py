"""派发闸：节点如实报告了"我卡住了"，局面没变就不该再派它一次。

## 缺的是什么（issue #524 / #395-9）

`report_blocker` 早就把**该发生什么才值得再跑**结构化落了盘：`category`、
`requested_action`、`suggested_owner`、`retryable_after_change`。它进 transcript、
进 `summary.json` 的 `blockers`、也被交回调度器。

派发层从来没读过它。

于是 continuous 模式里出现了这个形态（2026-08-19 实测，三个 writing 子 run）：
writing 判定上游材料不足 → 如实报 blocker、写一份材料不足报告 → **必需产出齐、
QC 全过 → run 是 `completed`** → 调度器再派一次 → 同样的输入、同样的结论。
第三次才碰巧走通。

关键在最后那一步：既有的重复失败熔断（`run_node._repeated_failure_for`）数的是
**失败**，而 `consecutive_failures` 见到 `is_completed` 第一眼就断链。**"我完整地
报告了我被卡住"在账本上是一次成功**，熔断器对它完全不在场。这不是熔断器写错了，
是这条路径从来没有被任何闸覆盖过 —— 机制存在，只是没接到路径上。

## 判据：局面变没变，不是重试了几次

计数器在这里是错的工具。"再跑一次值不值"取决于**上游材料变没变**，而这是机械
事实：

    upstream_fingerprint = sha256( 项目里**不属于该节点**的全部产物的
                                   (id, version, content_hash) 排序后拼接 )

排除该节点自己的产出是必须的：writing 那份"材料不足报告"本身就是一个新产物，
把它算进去，指纹每跑一次都变，闸立刻自废。

再加上 `node_inputs` 的规范化哈希 —— 调用方换了要求（指名版本、显式要求降级
产物、换一个研究问题）就是一次真实的扰动，理应放行。

两个都没变 → 拒绝派发，并把上一次报的 blocker 原文摆出来。

## 解除路径（必须在报错里写全，否则等于逼调用方绕行）

  (a) 让上游真的变：派上游节点补齐（报错列出候选）
  (b) 改 `node_inputs`：指名版本、显式要求降级产物、换任务
  (c) human-only：`request_human_input` / `CONTINUOUS_STATUS: blocked`
  逃生阀：`HARNESS_BLOCKER_DISPATCH_GATE=off`

## 不做的事

- **不做永久锁**。局面一变就自动解除，不需要谁来"清标记" —— 需要有人记得清的
  标记迟早会被忘记（`busy` 布尔那次的教训）。`retryable_after_change=False` 也
  只改文案、不加锁：把它做成永久拒绝，就是 #426 里"环境修好也无法解锁"的复刻。
- **不猜 blocker 解决没解决**。盘上没有"blocker 已解决"这个事实，就不发明它。
  能机械回答的只有"局面变没变"，闸只问这一句。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: summary.json 里这次 run 被卡住时的局面快照。只在真报了 blocker 时才写 ——
#: "扫过了没有"和"根本没扫"的区别由 `blockers` 自己表达，不必再存一个空壳。
BLOCKED_SITUATION_KEY = "blocked_situation"

_ENV_SWITCH = "HARNESS_BLOCKER_DISPATCH_GATE"


def gate_enabled() -> bool:
    return (os.getenv(_ENV_SWITCH) or "").strip().lower() not in {"off", "0", "false"}


def upstream_fingerprint(worktree: Path | str | None, node_type: str) -> str | None:
    """项目里**不属于 node_type** 的全部产物的内容指纹。

    返回 None = 算不出来（没绑 worktree 的 CLI 单跑 / 目录读不到）。调用方
    据此**放行**：证据不在场时不猜，也不假装拦住了。
    """
    if not worktree:
        return None
    root = Path(worktree)
    if not root.is_dir():
        return None
    from core.ledger import workspace_store

    parts: list[str] = []
    for head in workspace_store(root).heads().values():
        owner = head.produced_by_node_type
        if owner == node_type or owner.lstrip("_") == node_type.lstrip("_"):
            continue          # 自己的产出不算局面变化
        parts.append(f"{head.artifact_id}@v{head.version}:{head.sha256}")
    if not parts:
        # 一份上游产物都没有也是一种确定的局面（"什么都还没有"），
        # 它同样应该被指纹表达 —— 否则空项目里的闸恒不生效。
        return hashlib.sha256(b"").hexdigest()
    blob = "\n".join(sorted(parts))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def inputs_fingerprint(node_inputs: Any) -> str:
    """`node_inputs` 的规范化哈希。键序无关，值按 JSON 规范化。"""
    try:
        blob = json.dumps(node_inputs or {}, sort_keys=True, ensure_ascii=False,
                          default=str)
    except (TypeError, ValueError):
        blob = str(node_inputs)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def capture(state: Any, node_type: str, node_inputs: Any) -> dict | None:
    """run 结束时把"我卡在什么局面上"落盘。只在报了 blocker 时调用。"""
    fingerprint = upstream_fingerprint(
        getattr(state, "project_worktree", None), node_type)
    if fingerprint is None:
        return None
    return {
        "upstream_fingerprint": fingerprint,
        "inputs_fingerprint": inputs_fingerprint(node_inputs),
    }


def evaluate(
    *,
    blockers: list[dict],
    prior_situation: dict | None,
    worktree: Path | str | None,
    node_type: str,
    node_inputs: Any,
) -> dict | None:
    """该不该拒绝这次派发。返回 None = 放行；否则给出拒绝的机械依据。"""
    if not gate_enabled() or not blockers or not isinstance(prior_situation, dict):
        return None
    prior_upstream = str(prior_situation.get("upstream_fingerprint") or "")
    prior_inputs = str(prior_situation.get("inputs_fingerprint") or "")
    if not prior_upstream:
        return None                      # 老记录没有局面快照 —— 没有依据就不拦
    current_upstream = upstream_fingerprint(worktree, node_type)
    if current_upstream is None or current_upstream != prior_upstream:
        return None                      # 上游变了（或算不出来）→ 放行
    current_inputs = inputs_fingerprint(node_inputs)
    if prior_inputs and current_inputs != prior_inputs:
        return None                      # 调用方换了要求 → 放行
    return {
        "kind": "blocked_situation_unchanged",
        "node_type": node_type,
        "upstream_fingerprint": current_upstream,
        "blockers": blockers[:5],
    }


def render_refusal(decision: dict, upstream_candidates: list[str]) -> str:
    """拒绝文案。三条解除路径必须逐条写出来 —— 只说不许而不说怎么解，
    调用方唯一能做的就是换个姿势再撞一次。"""
    node_type = decision.get("node_type") or "?"
    lines = [
        f"⛔ 拒绝再次启动 {node_type}：它上一次已经如实报告了阻塞，"
        f"而**上游产物与 node_inputs 与那一次逐字节相同**。"
        f"同样的输入必然得到同样的结论（实测：同一个材料不足报告被重跑三次）。",
        "上一次报的是：",
    ]
    for blocker in decision.get("blockers") or []:
        category = blocker.get("category") or "other"
        summary = str(blocker.get("summary") or "").strip()[:400]
        lines.append(f"  · [{category}] {summary}")
        action = str(blocker.get("requested_action") or "").strip()[:400]
        if action:
            lines.append(f"    需要发生的事：{action}")
        owner = str(blocker.get("suggested_owner") or "").strip()
        if owner:
            lines.append(f"    建议由谁做：{owner}")
        if blocker.get("retryable_after_change") is False:
            lines.append(
                "    报告方明确说了：**光是重跑没有用**，必须有人改变外部条件。")
    lines += [
        "解除本闸只需要让局面真的变一次，三条路：",
        f"  (a) 派上游节点把缺的东西补上 —— 候选：{upstream_candidates or '（无上游）'}；"
        f"把『缺什么 / 达到什么标准算补齐』写进它的 node_inputs；",
        f"  (b) 改 {node_type} 的 node_inputs —— 指名要用哪一版上游产物、"
        f"或显式要求它产降级产物（gap report / pilot）并如实标注缺口；",
        "  (c) 确属需要人或外部环境改变的卡点 → 用 request_human_input 呈上去，"
        "或输出 CONTINUOUS_STATUS: blocked，别在这里空转。",
    ]
    return "\n".join(lines)
