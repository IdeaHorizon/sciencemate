"""让步（concession）—— agent 对一条 blocking 义务的公开申报式应答。

## 为什么有这个模块（判决拆除战役批 0，docs/verdict_demolition/）

义务系统给了框架"记账"的能力，但 blocking 义务在无人值守下没有任何合法出口：
chat 的终态闸把 complete 驳回成 continue，而清偿唯一路径常常不在 agent 力内
（上游节点起不来、平台能力缺席、外部证据不可得）。qinp 铸图包事故就是这个
形状——agent 试遍了合法路径，每条都被机械墙挡死，能动性只剩报 blocked。

科学上的正确形态（README S2/S3/S4）：检查照跑、三态如实可见，**继续与否的
判断权归 agent，终审权归 referee 与用户**。让步就是那个判断的记录载体：

    「我知道欠着 X；我因为 R 决定带着这笔账继续；我做了补偿 C。」

## 三条不变量

1. **让步是事实不是判决**（feedback_verdict_vs_evidence）：append-only 落盘，
   义务照旧每轮从 run 历史现推；"这条账还拦不拦"在读取时现算，不养状态机。
2. **让步改不了 status / 出处**：被让步的义务仍然渲染在账上（标 🤝）、仍进
   summary 的 open_items、仍交 referee 终审 —— 它只是不再焊死 complete。
3. **出口不得由关键词把守**（deriv:516 实测教训：英文写的诚实降级过不去）：
   匹配靠机械 key_hash，理由是自由文本、原样记录、从不解析。

## 存放

`<project worktree>/.research/concessions.jsonl`（append-only；没绑 worktree 的
run 落 state.root，只对本 run 生效——让步是项目级事实，worktree 是它的家）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def obligation_key_hash(kind: str, owed_by: str | None, what: str) -> str:
    """与 Obligation.key 同源的机械匹配码（8 hex）。"""
    blob = f"{kind}|{owed_by or ''}|{(what or '')[:120]}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def _store_path(state: Any) -> Path | None:
    base = getattr(state, "project_worktree", None) or getattr(state, "root", None)
    if base is None:
        return None
    return Path(base) / ".research" / "concessions.jsonl"


def load(state: Any) -> dict[str, dict]:
    """key_hash → 最新一条让步记录。读不到就是没有（不猜）。"""
    p = _store_path(state)
    if p is None or not p.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kh = rec.get("key_hash")
                if kh:
                    out[str(kh)] = rec
    except OSError:
        return {}
    return out


def record(state: Any, *, key_hash: str, kind: str, owed_by: str | None,
           what: str, reason: str, compensation: str = "") -> dict | None:
    """落一条让步。整个命题入账（feedback_the_record_must_carry_the_whole_proposition）：
    欠什么、谁欠、为什么让步、补偿了什么、谁在哪个 run 何时让的。
    返回写入的记录；落不了盘返回 None（不假装成功）。
    """
    p = _store_path(state)
    if p is None:
        return None
    rec = {
        "key_hash": key_hash,
        "kind": kind,
        "owed_by": owed_by,
        "what": (what or "")[:400],
        "reason": (reason or "").strip(),
        "compensation": (compensation or "").strip(),
        "by_node": getattr(state, "node_type", None),
        "run_id": getattr(state, "run_id", None),
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return rec
