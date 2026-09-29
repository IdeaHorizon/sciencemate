"""效果传感器：org 卡被送出去之后，发生了什么。

## 为什么必须有

人批只是**入口**质量。一张卡的最终判据是「下游项目用了它，并且没被坑」——
而注入之后发生什么此前**零记录**：命中没命中、被引用还是被无视、有没有
误导过一个项目，全不知道。

后果具体而机械：`find_stale_suspects` 读 `last_cited_at`，而**全仓没有任何
地方写这个字段**。于是老化复查永远落回 `created_at`，按日历猜，
跟这张卡有没有人用完全无关。同理正典刷新只能按数量触发。

## 两种"没用上"是两回事

    送出去了，没人引用   → 它可能是噪音，或者写得没法用。**卡的问题**
    从来没被送出去       → 没有新项目命中它的域。**域的问题**（休眠，不是过时）

合并成一个"stale"会把这两件事的处置搞混：前者该重写或降级，后者该原样留着。

## 引用怎么算

**机械推导，不新开写路径**：一条 project claim 的 `sources` 里出现 org claim id
就是一次引用。不要求任何节点"记得上报"—— 只要还需要模型主动做的事，
它就有一半概率不做。

## 有界

账本是 append-only jsonl；读取一律先聚合成计数，输出量与账本长度无关。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: 注入账本文件名（org 层）
LEDGER_NAME = "usage_ledger.jsonl"

#: 一次注入最多记多少条 id —— 与注入预算同量级，防账本被单次写爆
MAX_IDS_PER_EVENT = 40

EVENT_INJECTED = "injected"


@dataclass
class Usage:
    """一条 org 卡的使用画像。"""

    org_id: str
    injected: int = 0
    cited: int = 0
    last_injected_at: str = ""
    last_cited_at: str = ""
    injected_into: tuple[str, ...] = ()
    cited_by_projects: tuple[str, ...] = ()

    @property
    def sent_but_ignored(self) -> bool:
        """送出去过、没人引用 —— **卡的问题**（噪音，或写得没法用）。"""
        return self.injected > 0 and self.cited == 0

    @property
    def never_sent(self) -> bool:
        """从没被送出去 —— **域的问题**（休眠），不是这张卡过时。"""
        return self.injected == 0

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id, "injected": self.injected, "cited": self.cited,
            "last_injected_at": self.last_injected_at,
            "last_cited_at": self.last_cited_at,
            "injected_into": list(self.injected_into),
            "cited_by_projects": list(self.cited_by_projects),
            "sent_but_ignored": self.sent_but_ignored,
            "never_sent": self.never_sent,
        }


def record_injection(state: Any, org_ids: list[str], *,
                     project_id: str, at: str) -> None:
    """记一次注入。**静默失败**：传感器坏了不该让开题跑不起来。

    注入是读路径上的动作，这里的写必须便宜且不可能抛 —— 一个观测设施
    把被观测的过程搞挂，是最坏的那种耦合。
    """
    ids = [str(i) for i in (org_ids or []) if i][:MAX_IDS_PER_EVENT]
    if not ids:
        return
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": EVENT_INJECTED, "org_ids": ids,
                                "project_id": project_id, "at": at},
                               ensure_ascii=False) + "\n")
    except Exception:
        return


def usage_report(state: Any) -> dict[str, Usage]:
    """全 org 层的使用画像。有界：先聚合成计数，不返回原始账本。"""
    out: dict[str, Usage] = {}
    inj_count: Counter = Counter()
    inj_last: dict[str, str] = {}
    inj_projects: dict[str, set] = {}

    for ev in _read_ledger():
        if ev.get("event") != EVENT_INJECTED:
            continue
        at = str(ev.get("at") or "")
        pid = str(ev.get("project_id") or "")
        for oid in ev.get("org_ids") or ():
            oid = str(oid)
            inj_count[oid] += 1
            if at > inj_last.get(oid, ""):
                inj_last[oid] = at
            inj_projects.setdefault(oid, set()).add(pid)

    cite_count, cite_last, cite_projects = _derive_citations(state)

    for oid in set(inj_count) | set(cite_count):
        out[oid] = Usage(
            org_id=oid,
            injected=inj_count.get(oid, 0),
            cited=cite_count.get(oid, 0),
            last_injected_at=inj_last.get(oid, ""),
            last_cited_at=cite_last.get(oid, ""),
            injected_into=tuple(sorted(inj_projects.get(oid, ()))),
            cited_by_projects=tuple(sorted(cite_projects.get(oid, ()))),
        )
    return out


def usage_of(state: Any, org_id: str) -> Usage:
    return usage_report(state).get(org_id) or Usage(org_id=org_id)


# ── 引用：机械推导，不新开写路径 ────────────────────────────────────────────


def _derive_citations(state: Any) -> tuple[Counter, dict, dict]:
    """一条 project claim 的 sources 里出现 org claim id = 一次引用。

    不要求节点"记得上报"：只要还需要模型主动做的事，它就有一半概率不做。
    """
    counts: Counter = Counter()
    last: dict[str, str] = {}
    projects: dict[str, set] = {}
    try:
        rows = state.list_kb("claims") or []
    except Exception:
        return counts, last, projects

    org_ids = {str(r.get("id")) for r in rows if r.get("scope") == "org"}
    if not org_ids:
        return counts, last, projects

    for rec in rows:
        if rec.get("scope") == "org":
            continue
        at = str(rec.get("created_at") or "")
        pid = str(rec.get("project_id") or "")
        for src in rec.get("sources") or ():
            sid = str(src)
            if sid not in org_ids:
                continue
            counts[sid] += 1
            if at > last.get(sid, ""):
                last[sid] = at
            projects.setdefault(sid, set()).add(pid)
    return counts, last, projects


# ── 盘面 ────────────────────────────────────────────────────────────────────


def _ledger_path():
    from core import paths

    return paths.org_root() / LEDGER_NAME


def _read_ledger() -> list[dict]:
    """读账本。**整段不可抛** —— 包括算路径那一步。

    第一版把 `_ledger_path()` 放在 try 外面，于是"传感器不该搞挂被观测的过程"
    这条只兑现了一半：写路径护住了，读路径没有。守卫的边界要覆盖全程，
    不是覆盖最显眼的那一段。
    """
    out: list[dict] = []
    try:
        path = _ledger_path()
        if not path.exists():
            return []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out
