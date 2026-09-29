"""Dreaming 自动触发 —— 决定何时该跑 curator Mode 2。

策略（用户 confirmed）：
  - 每 N 条新 KB claim/concept 写入 → mark pending（N=30）
  - 新 dead_end claim 写入 → 立即 mark pending（cross-project 资产）
  - review_history flip ≥ 3 → mark pending
  - 距上次 dreaming > 14 天 → mark pending

存储：`<project_root>/dreaming_pending.json`
  {
    "pending": true,
    "reasons": ["+30 claim threshold", "new dead_end", "stale"],
    "since": ISO,
    "kb_writes_counter_at_mark": 145,
  }

chat.py 主循环检测到 pending → 自动后台跑 _curator(mode='dreaming')，显示"正在
整理记忆中..."。跑完清 pending。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.paths import project_dir as _paths_project_dir

log = logging.getLogger("dreaming_scheduler")


# 用户选定阈值（v0.3.2+）
NEW_CLAIM_THRESHOLD = int(os.getenv("HARNESS_DREAMING_NEW_CLAIM_THRESHOLD", "30"))
STALE_DAYS_THRESHOLD = int(os.getenv("HARNESS_DREAMING_STALE_DAYS", "14"))
# v2.0：memory candidate 队列阈值 —— 积压 ≥ 10 条 pending candidate 时 mark dreaming
CANDIDATE_PENDING_THRESHOLD = int(
    os.getenv("HARNESS_DREAMING_CANDIDATE_THRESHOLD", "10"),
)


#: 现算时最多回看多少个 run（有界：项目跑久了 runs 目录会很长）。
_LEDGER_SCAN_LIMIT = 400


def last_dreaming_at(project_id: str) -> str | None:
    """上次 curator dreaming 是什么时候 —— **从 run 账本现算**，不读盖章文件。

    为什么不盖章：上一代写 `last_dreaming.json`，而 `clear_pending()` 无条件
    调它，`/skip-dreaming` 和 dreaming 失败路径又都调 `clear_pending()` ——
    于是**"跳过"被实现成了"刚做过"**，14 天 stale 时钟被整个推后。

    账本里的事实伪造不了：一个 `status=completed` 的 `_curator` run 是不是
    真的发生过，盘上有 summary.json 为证。跳过不会产生 run，所以也就无章可盖。

    再往前一代更糟：判据读的是 `core.curator_audit` 写的审计文件，而
    `CuratorAudit(` 全仓零构造点 —— 那条链路从来没有写入方，于是永远判
    "从未跑过"、永远重新 mark、门禁死锁不自愈（v10c 实测 12 次 curator 循环、
    113 分钟）。教训是同一条：**不可撤销的判断，别建在"希望被写下来的记录"上。**
    """
    from core.paths import runs_parent

    try:
        root = runs_parent(project_id)
    except Exception:
        return None
    if root is None or not root.is_dir():
        return None
    try:
        run_dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                          key=lambda d: d.name, reverse=True)[:_LEDGER_SCAN_LIMIT]
    except OSError:
        return None
    for d in run_dirs:
        s = d / "summary.json"
        if not s.is_file():
            continue
        try:
            rec = json.loads(s.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("node_type") != "_curator":
            continue
        if str(rec.get("status")) != "completed":
            continue
        stamp = str(rec.get("ended_at") or rec.get("started_at") or "")
        if stamp:
            return stamp
        # run_id 前缀是 epoch 秒 —— 账本自带时间，不必额外记一份
        head = str(rec.get("run_id") or d.name).split("-")[0]
        if head.isdigit():
            return datetime.fromtimestamp(int(head), tz=timezone.utc).isoformat()
    return None


def _read_last_dreaming(project_id: str) -> str | None:
    return last_dreaming_at(project_id)

def _pending_path(project_id: str) -> Path | None:
    pd = _paths_project_dir(project_id)
    if pd is None:
        return None
    pd.mkdir(parents=True, exist_ok=True)
    return pd / "dreaming_pending.json"


def _counter_path(project_id: str) -> Path | None:
    pd = _paths_project_dir(project_id)
    if pd is None:
        return None
    pd.mkdir(parents=True, exist_ok=True)
    return pd / "kb_writes_since_dreaming.json"


def _read_counter(project_id: str) -> int:
    p = _counter_path(project_id)
    if p is None or not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("count", 0))
    except Exception:
        return 0


def _write_counter(project_id: str, count: int) -> None:
    p = _counter_path(project_id)
    if p is None:
        return
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
    tmp.replace(p)


def read_pending(project_id: str) -> dict | None:
    p = _pending_path(project_id)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


#: 由 run 账本**现算**的理由。它们描述局面，不描述发生过的事，所以既不该被
#: `mark_pending()` 写进盘，也不该从盘上读回来 —— 账本一变它们就作废。
#: 盘上仍可能存着历史遗留的这类理由（2026-08-21 之前的版本写的），
#: `_persisted_event_reasons()` 负责在读的时候把它们滤掉，不必单跑迁移脚本。
_DERIVED_REASON_PREFIXES = (
    "never run dreaming",
    "invalid last_dreaming_at",
    "stale (",
)


def _is_derived_reason(reason: str) -> bool:
    return str(reason).startswith(_DERIVED_REASON_PREFIXES)


def _persisted_event_reasons(project_id: str) -> list[str]:
    """盘上记着的**事件**理由，滤掉历史遗留的判决类理由。"""
    pending = read_pending(project_id)
    if not pending or not pending.get("pending"):
        return []
    return [
        str(r) for r in (pending.get("reasons") or [])
        if not _is_derived_reason(str(r))
    ]


def mark_pending(project_id: str, *, reason: str) -> None:
    """登记 dreaming 应该跑了。多次 mark 累积 reasons。

    只收**事件**理由（写了 dead_end / 争议 flip / KB 写入超阈值）。局面类理由
    （从没 dream 过 / stale）由 `check_stale()` 现算，落盘就会冻住 —— 那正是
    2026-08-21 死锁的第二层。
    """
    if _is_derived_reason(reason):
        log.debug("refusing to persist derived reason %r for %s", reason, project_id)
        return
    p = _pending_path(project_id)
    if p is None:
        return
    existing = read_pending(project_id) or {"pending": True, "reasons": []}
    existing.setdefault("reasons", [])
    if reason not in existing["reasons"]:
        existing["reasons"].append(reason)
    existing["pending"] = True
    existing.setdefault("since", datetime.now(timezone.utc).isoformat())
    existing["kb_writes_counter_at_mark"] = _read_counter(project_id)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)
    log.info("Marked dreaming pending for %s: %s", project_id, reason)


def clear_pending(project_id: str) -> None:
    """清 pending 标志 + 重置 KB 写入计数器。

    **不盖"刚做过"的章** —— 那正是"跳过被实现成完成"的来源：本函数的三个
    调用点里有两个跟"是否真做了事"无关（`/skip-dreaming`、dreaming 失败路径
    的 finally）。"上次什么时候 dream 的"改由 `last_dreaming_at()` 从 run
    账本现算：跳过不会产生 run，所以无章可盖。
    """
    p = _pending_path(project_id)
    if p and p.exists():
        p.unlink()
    _write_counter(project_id, 0)
    log.info("Cleared dreaming pending for %s", project_id)


def on_kb_write(project_id: str | None, *, entity: str, record: dict) -> None:
    """state.write_kb 写完调一次。检查各阈值，必要时 mark pending。

    - 任何 claim / concept 写入 → counter++
    - counter ≥ NEW_CLAIM_THRESHOLD → mark
    - claim_type='dead_end' → mark
    - review_history flip ≥ 3 → mark
    """
    if not project_id:
        return     # 无 project 不持久化 pending
    if entity not in ("claims", "concepts"):
        return

    new_count = _read_counter(project_id) + 1
    _write_counter(project_id, new_count)

    if new_count >= NEW_CLAIM_THRESHOLD:
        mark_pending(project_id,
                      reason=f"+{NEW_CLAIM_THRESHOLD} KB writes since last dreaming")

    if entity == "claims" and record.get("claim_type") == "dead_end":
        mark_pending(project_id, reason="new dead_end (cross-project lesson)")

    rh = record.get("review_history") or []
    flips = sum(1 for e in rh if e.get("from_status") != e.get("to_status"))
    if flips >= 3:
        mark_pending(project_id, reason=f"high dispute (flips={flips})")


def check_stale(project_id: str, last_dreaming_at_iso: str | None = None) -> str | None:
    """距上次 dreaming 是否已超阈值。返回**理由字符串**，不 stale 则返 None。

    **不落盘。** 这是一条判决，不是一件证据 —— 它完全由 run 账本现算，账本变了
    它就该跟着变。把它写进 `dreaming_pending.json` 会让判决冻在文件里：
    2026-08-21 实测，账本读不到 curator run（判据锚错了根，见
    `core.paths.runs_parent`）→ 每次派发都 mark 一次 "never run dreaming" →
    而 `should_run_dreaming()` 读到 pending 就早退、再也不核对账本。于是即便
    后来账本修好了、curator 真跑过 3 次，那条判决仍然从文件里复活。
    通则：证据可持久化，判决不可以。

    last_dreaming_at_iso: `last_dreaming_at()` 从 run 账本现算；None / 无效则
    视为从未跑过。
    """
    if last_dreaming_at_iso is None:
        return "never run dreaming"
    try:
        last_dt = datetime.fromisoformat(last_dreaming_at_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "invalid last_dreaming_at"
    age = datetime.now(timezone.utc) - last_dt
    if age.days >= STALE_DAYS_THRESHOLD:
        return f"stale ({age.days}d ≥ {STALE_DAYS_THRESHOLD}d)"
    return None


def maybe_check_stale_from_audit(project_id: str) -> bool:
    """距上次 dreaming 是否已 stale（现算，不落盘）。

    判据是 `last_dreaming_at()` —— **从 run 账本现算**。
    这里曾经有三层历史包袱，都已删：

      · 读 `core.curator_audit` 的审计文件：那条链路全仓零构造点，永远读到
        空、永远判"从未跑过"、门禁死锁不自愈（v10c 实测 113 分钟）。
      · 读 `last_dreaming.json` 盖章文件：`/skip-dreaming` 与失败路径都会
        经 `clear_pending()` 盖章，"跳过"于是等于"刚做过"。
      · 把 stale 判决 mark 进 pending 文件：判决一旦落盘就不再复算，账本修好
        了也醒不过来（2026-08-21，6.5 小时）。

    三次的教训是同一条：**这个问题只有一个真相源，就是 run 账本。**
    """
    return check_stale(project_id, last_dreaming_at(project_id)) is not None


def should_run_dreaming(project_id: str | None) -> tuple[bool, list[str]]:
    """该不该起一次 curator dreaming。返 (should_run, reasons)。

    两个来源，语义不同，所以来源也不同：
      · **事件**（写了 dead_end、争议 flip、KB 写入超阈值）—— 真的发生过，
        落盘在 `dreaming_pending.json`，由 `clear_pending()` 消费。
      · **局面**（从没 dream 过 / 距上次太久）—— 现算，不落盘。

    ⚠️ 这是**建议**，不是准入。它驱动 `dreaming_due_reminder` 提醒，不拦任何
    节点派发 —— 尤其不拦 writing。曾经有过那道门，把一个做完的研究钉死 6.5
    小时（见 `shared/tools/run_node.py` 里那段"别加回来"）。
    """
    if not project_id:
        return False, []
    reasons: list[str] = _persisted_event_reasons(project_id)
    stale_reason = check_stale(project_id, last_dreaming_at(project_id))
    if stale_reason is not None and stale_reason not in reasons:
        reasons.append(stale_reason)
    return bool(reasons), reasons
