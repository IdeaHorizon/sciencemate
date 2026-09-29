"""`Run.status` 的**唯一**写入口（RFC 异步运行时 D11）。

## 为什么要有一个漏斗

一条 run 的状态此前有 5 个赋值点，散在 2 个模块里。它们各自都有道理，问题
在于**没有任何一处记得自己是第几个写的**：8-21 那条 run 收到两次终态，中间
还在推进研究并交付了完整简报，而库里只剩最后一次的值 —— 前一次盖错了这件事
连痕迹都没有。

漏斗解决的不是"谁有资格写"（几个写点都有正当理由），而是：

  1. 写点**唯一**，所以"还有谁在写"这个问题有确定答案（扫盘闸守着）；
  2. 每一次写都留下**出处**（谁写的、依据是什么），所以盖错了看得见；
  3. 终态被后续活动推翻时，这件事被**记下来**而不是被覆盖掉。

## 判决可以改，但改动要留痕

「判决不可持久化」说的是不要把**推导得出的结论**当事实存起来。run.status 是
投影，不是推导 —— 它必须落盘（UI、恢复、对账都读它）。所以这里的规矩不是
"不许写"，是"写了要说得出是谁写的、依据什么"。

## 这里**不做**准入判断

漏斗不否决任何一次写。谁该写、什么时候写是调用方的判断；漏斗只负责让这件事
留下痕迹。把政策塞进漏斗会让它变成第六个真相源。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models.execution import (
    TERMINAL_RUN_STATUSES,
    Run,
    RunStatus,
    RunStatusWriteError,
    allow_run_status_write,
)

logger = logging.getLogger(__name__)

#: summary 里存出处的键。
STATUS_LOG_KEY = "statusLog"
#: 终态被后续活动推翻的记录 —— 这是**缺陷的见证**，不是正常流程的一部分。
CONTRADICTED_KEY = "terminalContradicted"

#: 出处日志保留最近多少条。它是取证线索不是账本，无界增长会把 summary 撑爆。
#: 截断这件事本身也要说出来（见 `_append_log`）。
MAX_STATUS_LOG = 24

#: 谁在写。取值是**闭集**：新增写点必须在这里登记，于是"还有谁在写"永远
#: 是一个看得见的清单，而不是要靠 grep 全仓才能回答的问题。
SOURCES = (
    #: 事件投影器 —— 绝大多数状态变化的正主。
    "projector",
    #: 从**活体 pause 现场**重建（账面说死了，而进程正停在 pause 上等人）。
    "live_pause_reconciliation",
    #: 传输层自己失败了 —— worker 报不出来的失败只能由平台记。
    "transport_failure",
    #: 这一轮被下一轮顶掉了。一个会话同一时刻只有一条"当前一轮"，新的顶层 run
    #: 一开跑，之前那条就**结束了** —— 不给它一个终态，它会一直挂在
    #: `waiting_human` 上，把一个已经答过的提问反复递到人面前。
    "superseded_by_next_turn",
)


def _append_log(run: Run, entry: dict[str, Any]) -> dict[str, Any]:
    summary = dict(run.summary) if isinstance(run.summary, dict) else {}
    log = list(summary.get(STATUS_LOG_KEY) or [])
    log.append(entry)
    if len(log) > MAX_STATUS_LOG:
        dropped = len(log) - MAX_STATUS_LOG
        log = log[-MAX_STATUS_LOG:]
        # 截了多少条要说出来 —— 无声截断读起来就是"一共只发生过这些"。
        summary["statusLogDropped"] = int(summary.get("statusLogDropped") or 0) + dropped
    summary[STATUS_LOG_KEY] = log
    return summary


def project_run_status(
    run: Run,
    status: RunStatus | str,
    *,
    source: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """把 `run` 的状态改成 `status`，并记下是谁依据什么改的。

    `evidence` 要能让人**回到现场**：投影器给事件 id 与 kind，现场重建给观测
    时刻，传输失败给异常类别。一句"因为要改"不算证据。
    """
    if source not in SOURCES:
        raise RunStatusWriteError(
            f"未登记的写入方 {source!r}；合法取值：{SOURCES}。"
            "新增写点要在 run_status.SOURCES 里登记 —— 那张清单就是"
            "「还有谁在写」这个问题的答案。"
        )
    previous = run.status
    new = RunStatus(status) if not isinstance(status, RunStatus) else status
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "from": str(previous or ""),
        "to": str(new.value),
        "source": source,
        "evidence": dict(evidence or {}),
    }
    summary = _append_log(run, entry)

    # ── 终态被推翻：记下来，别覆盖掉 ────────────────────────────────────────
    #
    # 8-21 那条 run 的形状：17:47 盖终态 → 18:23 继续 pause/resume 干活 →
    # 20:30 又盖一次。前一次盖错了，而库里只剩最后一个值，痕迹为零。
    #
    # 判据完全机械：**已经是终态了，却又来了一个非终态** —— 那就是"它没结束"
    # 的直接证据，因为没有任何合法路径能让一条真结束的 run 回到运行中。
    was_terminal = previous in {s.value for s in TERMINAL_RUN_STATUSES}
    if was_terminal and new not in TERMINAL_RUN_STATUSES:
        contradictions = list(summary.get(CONTRADICTED_KEY) or [])
        contradictions.append(
            {
                "terminal": str(previous),
                "contradictedBy": str(new.value),
                "at": entry["at"],
                "evidence": entry["evidence"],
            }
        )
        summary[CONTRADICTED_KEY] = contradictions[-MAX_STATUS_LOG:]
        logger.warning(
            "Run %s was already %s and then went %s — a terminal was stamped too early",
            run.id, previous, new.value,
        )

    run.summary = summary
    with allow_run_status_write(run):
        run.status = new.value          # ← 全仓**唯一**一处写 Run.status
