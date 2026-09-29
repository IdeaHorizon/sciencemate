"""任务结局 —— 和「收尾健康不健康」分开的第二条轴。

## 为什么需要它

`summary["status"]` 一条轴同时被问两个问题：**这个 run 收尾收干净了吗**，和
**它被交代的活干成了吗**。两者在很多真实局面里答案不同 —— experiment 的
operation run 可能收尾完整、审计通过，而任务本身是 failed / partial / blocked。

那样的 run 在这条字段出现之前：

  - 显示 `completed`，summary 除标识外和真 success **逐字段相同**（#1086 逐键比过）；
  - 于是登记 post-node flow、自动派 `_reviewer`、编排闭环算它已闭环、
    `run_history.is_completed` 为真。

节点唯一能让 status 不是 completed 的办法是往 `hook_state["blockers"]` 写一条 ——
一写就是 `blocked`，而 `blocked` 在 Core 里另有含义（中途停靠、没走到评估）。
于是「任务 failed」「任务 partial」「真 blocked」「收尾故障」四种情况共用一个词。

先例是 #735 的 `loop_terminal`（「loop 怎么结束的」单独成字段，注释写着
「status 是老词表……新消费者请读这两个字段」）。任务结局缺的就是这样一个字段。

## 判据

- **词表固定**，不接受自由文本：模型能写出什么词，下游就得认什么词 —— 那就不是
  一条能被机械消费的轴。
- **没报告就是 `None`，不推断成 success。** 「没人回答过这个问题」和「回答是成功」
  是两件事，压成同一个值，这条轴就白加了
  （[[feedback_absent_check_looks_like_passed_check]]）。
- **过渡期不变量**：在所有把 `status == "completed"` 当成「任务成功」的消费方改读
  这条轴之前，非 success 的任务**不能**看到 `completed`。否则节点侧删掉兼容
  blocker 之后，假成功会原样回来。这条由 `finalize_run` 机械执行，不靠各节点
  自己记得写 blocker。
"""
from __future__ import annotations

from typing import Any

#: 合法结局。`cancelled` 现在没有生产方，先占位 —— 词表要一次定齐，
#: 否则第一个需要它的人只能塞进 `failed` 里，而那正是本文件要拆开的那种压平。
OUTCOMES: tuple[str, ...] = ("success", "partial", "failed", "blocked", "cancelled")

#: 非 success 的结局在过渡期把 status 降到哪一档。
#:
#: `blocked` → `blocked`：Core 里这个词本来就是「停下来了，要有人来」。
#: 其余 → `incomplete`：它说的是「没交齐」，而且**不落局面快照**——
#: 有 blocker 才落（`core/executor.py`），而局面快照会让派发闸按
#: 「同样的输入必然得到同样的结论」拒绝重派。作业瞬时失败这类 failed
#: 不满足那个前提，重派一次完全可能成功，不该被那道闸挡住。
_STATUS_FLOOR = {"blocked": "blocked"}

_KEY = "node_task_outcome"


def record_task_outcome(
    state: Any,
    outcome: str,
    *,
    detail: str = "",
    reported_by: str | None = None,
) -> dict:
    """登记这一趟**任务**的结局。由节点在收尾时调用，`finalize_run` 原样写进 summary。

    重复调用以最后一次为准（收尾过程里结论可能被降格，例如声明 success 被机械
    降成 partial）；每次都落一条 transcript，改判过程本身是可审计的。

    `outcome` 不在词表里直接 `ValueError` —— 静默改写调用方的结论，是这条轴
    最不该有的行为。
    """
    normalized = str(outcome or "").strip().lower()
    if normalized not in OUTCOMES:
        raise ValueError(
            f"task outcome {outcome!r} 不是合法值。合法值：{', '.join(OUTCOMES)}")
    record = {
        "outcome": normalized,
        "detail": str(detail or "")[:500],
    }
    if reported_by:
        record["reported_by"] = str(reported_by)[:120]
    previous = state.hook_state.get(_KEY)
    state.hook_state[_KEY] = record
    try:
        state.append_transcript(
            "node_task_outcome_recorded",
            outcome=normalized,
            detail=record["detail"],
            reported_by=record.get("reported_by"),
            previous=(previous or {}).get("outcome"),
        )
    except Exception:       # 记账失败不能把结论本身弄丢
        pass
    return record


def task_outcome(state: Any) -> dict | None:
    """读回这一趟的任务结局；没人报告过就是 `None`（不是 success）。"""
    record = getattr(state, "hook_state", {}).get(_KEY)
    return dict(record) if isinstance(record, dict) else None


def status_floor(outcome: str | None, status: str) -> str:
    """过渡期不变量：任务没做成的 run 不许显示成 `completed`。

    只往下压，不往上抬 —— 收尾本身坏了（status 已经不是 completed）时，
    任务结局是 success 也不能把它改回 completed。两条轴各答各的。
    """
    if not outcome or outcome == "success":
        return status
    if status != "completed":
        return status
    return _STATUS_FLOOR.get(outcome, "incomplete")
