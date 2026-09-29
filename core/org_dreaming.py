"""org 层的融合作业 —— 科学正典维护，不是记忆压缩。

## 与 OpenClaw dreaming 的本质区别

他家做的是助手记忆的**压缩**：把旧的揉成摘要，省 context。
我们做的是科学正典**维护**，三条纪律与压缩正相反：

  合并必须保证据闭包   证据取并集，不因为"合并了"而丢掉任何一条出处
  矛盾呈现而不抹平     两条 org 结论冲突 → 标记 + 浮出为开放问题，不自动裁决
                       组内矛盾是科研机会，不是数据缺陷
  融合本身可审计       取代用 superseded 四字段（作废不删除），账本永远走得回去

## 七项作业

  1 同锚合并    机械 —— 结构自带（org 书目按锚寻址，晋升时就合了）
  2 近重复归并  提议 —— 同一断言 → 合并 + 证据并集 + replication_count++
  3 矛盾呈现    机械 —— contradicts 边 + open_question，**不裁决**
  4 取代        提议 —— 新 validated 覆盖旧条，superseded 留痕
  5 老化质询    机械 —— 长期无引用标 stale-suspect；**dead_end 永不老化**
  6 综述刷新    提议 —— 某域新增晋升满阈值 → 重写活综述
  7 血统监控    机械 —— 某域产物全引同一份范例且审查分停滞 → 浮出"板结"

预算全部有界（每项作业的读取量与 org 总量无关）。
判断类走提议→人批，机械类直落 —— 与晋升同一套治理。

见 docs/RFC_KB_TWO_TIERS_20260820.md §9/§14.2。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 每次 dreaming 的作业上限。有界不是可选项：一个 dreaming run 发过
#: 51,937 次 search_kb（只有 78 个不同查询）—— 开放式"治理 KB"必然失控。
MAX_PAIRS_PER_JOB = 200
MAX_PROPOSALS_PER_RUN = 20

#: 近重复的判定阈值。刻意保守：**宁可漏合并，不可错合并** ——
#: 错合并会把两条不同的结论揉成一条，而证据链已经并起来了，事后拆不回去。
NEAR_DUPLICATE_MIN_OVERLAP = 0.75

#: 老化质询的静默期（天）。dead_end 不在此列 —— 它的价值恰在稀有时刻，
#: 十年没人撞不代表它过时，只代表这十年没人踩坑。
STALE_SUSPECT_DAYS = 730


@dataclass
class Job:
    """一项作业的产出。`proposals` 要人批，`applied` 已直落。"""

    name: str
    applied: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"job": self.name, "applied": self.applied,
                "proposals": self.proposals, "notes": self.notes}


# ── 2. 近重复归并 ───────────────────────────────────────────────────────────


def find_near_duplicates(state: Any) -> Job:
    """同一断言的两次独立发现 → 提议合并 + 复现记数 ++。

    **这是复利最具象的形态**：第二个项目没有"重新发现"，它加固了一条已有
    结论，且适用范围自动变宽（systems_tested 取并集）。

    只提议不直落：判"是不是同一条断言"需要语义判断，机械层给不出。
    """
    job = Job("near_duplicate_merge")
    findings = _org(state, kind="verified_finding")
    for i, a in enumerate(findings):
        for b in findings[i + 1:][:MAX_PAIRS_PER_JOB]:
            overlap = _token_overlap(_statement(a), _statement(b))
            if overlap < NEAR_DUPLICATE_MIN_OVERLAP:
                continue
            if _same_project(a, b):
                continue          # 同项目内的重复是晋升时的问题，不是复利
            job.proposals.append({
                "kind": "merge_near_duplicate",
                "ids": [a.get("id"), b.get("id")],
                "overlap": round(overlap, 3),
                "merged_replication_count": _rep(a) + _rep(b),
                "merged_applicability": _merge_applicability(a, b),
                "evidence_union": sorted(set(a.get("sources") or [])
                                         | set(b.get("sources") or [])),
                "why": ("两个项目独立得出相近结论 —— 合并后复现记数累加、"
                        "适用范围取并集。这是跨项目复现的系统性记账，"
                        "真实实验室几乎做不到。"),
            })
            if len(job.proposals) >= MAX_PROPOSALS_PER_RUN:
                job.notes.append(f"提议已达上限 {MAX_PROPOSALS_PER_RUN}，本轮截断")
                return job
    return job


# ── 3. 矛盾呈现 ─────────────────────────────────────────────────────────────


def find_contradictions(state: Any) -> Job:
    """两条 org 结论冲突 → 标记 + 浮出为开放问题。**不自动裁决。**

    自动裁决是这里最容易犯、也最贵的错：组内矛盾是**科研机会**，
    不是数据缺陷。KB 自己产生课题，正是从这里长出来的。

    判据：适用条件重叠（谈的是同一件事）但结论方向相反。
    """
    job = Job("contradiction_surfacing")
    findings = _org(state, kind="verified_finding")
    for i, a in enumerate(findings):
        for b in findings[i + 1:][:MAX_PAIRS_PER_JOB]:
            if not at_odds(a, b):
                continue
            job.applied.append({
                "kind": "contradicts_edge",
                "from": a.get("id"), "to": b.get("id"),
            })
            job.applied.append({
                "kind": "open_question",
                "question": ("两条已晋升结论在重叠适用条件下方向相反："
                             f"「{_statement(a)[:80]}」 vs 「{_statement(b)[:80]}」。"
                             "是条件划分不够细，还是其中一条的适用范围被高估？"),
                "contradicts": [a.get("id"), b.get("id")],
                "status": "open",
            })
    if job.applied:
        job.notes.append("矛盾**不自动裁决** —— 它是研究议程，交给下一个项目认领")
    return job


# ── 5. 老化质询 ─────────────────────────────────────────────────────────────


def find_stale_suspects(state: Any, *, now_iso: str) -> Job:
    """长期无人引用的 org 条目 → 标 stale-suspect 待质询（不删除）。

    **dead_end 永不老化**：它的价值恰在稀有时刻 —— 十年没人撞不代表它过时，
    只代表这十年没人踩坑。把它老化掉，下一个踩坑的人就没人提醒了。
    """
    job = Job("stale_suspect")

    # 判据从**真实使用**来，不从日历来。
    # 原来读 `last_cited_at` —— 全仓没有任何地方写这个字段，于是永远落回
    # `created_at`：一张天天被引用的卡和一张没人看的卡，老化得一样快。
    from core.org_usage import usage_report

    usage = usage_report(state)

    for rec in _org(state):
        if _kind(rec) == "dead_end":
            continue
        rid = str(rec.get("id") or "")
        u = usage.get(rid)
        last = str((u.last_cited_at if u else "")
                   or (u.last_injected_at if u else "")
                   or rec.get("created_at") or "")
        if not last or _days_between(last, now_iso) < STALE_SUSPECT_DAYS:
            continue

        # 两种"没用上"是两回事，处置也不同：
        #   送出去了没人引用 → 卡的问题（噪音 / 写得没法用）→ 重写或降级
        #   从来没被送出去   → 域的问题（休眠）→ 原样留着，别动它
        if u is None or u.never_sent:
            job.applied.append({
                "kind": "dormant_domain", "id": rid, "last_activity": last,
                "injected": 0,
                "note": ("从没被送到任何新项目面前 —— 是它的域休眠了，不是这张卡"
                         "过时了。**原样留着**，别按老化处置。"),
            })
            continue
        job.applied.append({
            "kind": "stale_suspect", "id": rid, "last_activity": last,
            "injected": u.injected, "cited": u.cited,
            "note": (f"被送到过 {u.injected} 个开题面前、{u.cited} 次被引用 —— "
                     f"送得出去却没人用，待质询：是噪音，还是写得没法用？"
                     f"（不删除）"),
        })
    return job


# ── 7. 血统监控 ─────────────────────────────────────────────────────────────


def find_ossified_exemplars(state: Any, *, usage: dict[str, list[float]]) -> Job:
    """某域近期产物全引同一份范例且审查分停滞 → 浮出"范例库板结"。

    `usage`：{exemplar_id: [按时间排序的模仿者审查分]}。分数由平台记，
    不由模型自评 —— 使用验证是范例选拔的**真正裁判**（RFC §19.2 第三关）。

    这是反僵化的最后一道闸：范例传手艺，但一份范例被所有人照抄且成绩不再
    提升，就说明它从"加速器"变成了"模具"。
    """
    job = Job("exemplar_lineage")
    for exemplar_id, scores in usage.items():
        if len(scores) < 5:
            continue          # 样本太少，谈不上板结
        recent, earlier = scores[-3:], scores[:-3]
        if not earlier:
            continue
        if sum(recent) / len(recent) > sum(earlier) / len(earlier):
            continue          # 还在变好
        job.applied.append({
            "kind": "open_question",
            "question": (f"范例 {exemplar_id} 已被 {len(scores)} 个产物模仿，"
                         "但模仿者的审查分不再提升 —— 该域范例库可能板结。"
                         "需要一份成功方式不同的样板换血。"),
            "exemplar_id": exemplar_id,
            "status": "open",
        })
    return job


# ── 6. 综述刷新 ─────────────────────────────────────────────────────────────


def find_canon_refreshes(state: Any) -> Job:
    """某域新增晋升满阈值 → 提议重写活综述。

    只提议：综述是**叙述**（讲脉络、讲争论、讲共识到哪一步），机械层给不出。
    这是"卡片 → 被正典吸收 → 新项目读正典"这条沉淀链的驱动器 ——
    没有它，几年后就是一堆谁也不敢信的半相关卡片。
    """
    job = Job("canon_refresh")
    try:
        from core.org_canon import refresh_jobs
    except Exception:
        return job
    job.proposals.extend(refresh_jobs(state)[:MAX_PROPOSALS_PER_RUN])
    if job.proposals:
        job.notes.append("综述吸收卡片后，卡片**降权不删除** —— 账本永远走得回去")
    return job


# ── 编排 ────────────────────────────────────────────────────────────────────


def run_dreaming(state: Any, *, now_iso: str,
                 exemplar_usage: dict | None = None) -> dict:
    """跑一轮 dreaming。**有界的 checklist，不是开放式治理**。

    逐项过完即收工 —— 这条纪律是拿 51,937 次 search_kb 换来的。
    """
    jobs = [
        find_near_duplicates(state),
        find_contradictions(state),
        find_stale_suspects(state, now_iso=now_iso),
        find_ossified_exemplars(state, usage=exemplar_usage or {}),
        find_canon_refreshes(state),
    ]
    return {
        "status": "success",
        "jobs": [j.as_dict() for j in jobs],
        "applied_count": sum(len(j.applied) for j in jobs),
        "proposal_count": sum(len(j.proposals) for j in jobs),
        "note": ("同锚合并（作业 1）是结构自带的：org 书目按锚寻址，"
                 "晋升那一刻就合了，不需要单独一趟。"),
    }


# ── 判据 ────────────────────────────────────────────────────────────────────

#: 极性词对 —— 判"结论方向相反"。刻意小而明确：宁可漏报矛盾，
#: 也不要把"措辞不同"当成"结论冲突"，那会把开放问题账淹掉。
_POLARITY = (
    ("显著", "不显著"), ("偏大", "偏小"), ("超出", "未超出"),
    ("成立", "不成立"), ("增大", "减小"), ("有效", "无效"),
    ("increase", "decrease"), ("exceeds", "within"),
)


def at_odds(a: dict, b: dict) -> bool:
    """两条结论是不是在说同一件事、方向却相反 —— **一处回答**。

    dreaming 的矛盾呈现、项目做完时比对组织已知、采纳时比对组织已有，问的都是这一件事
    （`core/org_corrections`）。判据：适用条件重叠（谈的是同一件事）且结论方向相反。
    刻意保守：宁可漏报，也不要把「措辞不同」当成「结论冲突」。
    """
    return _applicability_overlaps(a, b) and _polarity_conflict(_statement(a), _statement(b))


def _polarity_conflict(a: str, b: str) -> bool:
    for pos, neg in _POLARITY:
        if (pos in a and neg in b) or (neg in a and pos in b):
            return True
    return False


def _applicability_overlaps(a: dict, b: dict) -> bool:
    aa = a.get("applicability") or {}
    bb = b.get("applicability") or {}
    if not isinstance(aa, dict) or not isinstance(bb, dict):
        return False
    shared = set(aa) & set(bb)
    return bool(shared) and any(_loose_eq(aa[k], bb[k]) for k in shared)


def _loose_eq(x: Any, y: Any) -> bool:
    if isinstance(x, list) or isinstance(y, list):
        return bool(set(map(str, x if isinstance(x, list) else [x]))
                    & set(map(str, y if isinstance(y, list) else [y])))
    return str(x).strip().lower() == str(y).strip().lower()


def _merge_applicability(a: dict, b: dict) -> dict:
    out = dict(a.get("applicability") or {})
    for k, v in (b.get("applicability") or {}).items():
        if k not in out:
            out[k] = v
        elif isinstance(out[k], list) or isinstance(v, list):
            merged = list(out[k]) if isinstance(out[k], list) else [out[k]]
            merged += list(v) if isinstance(v, list) else [v]
            out[k] = sorted(dict.fromkeys(map(str, merged)))
    return out


def _token_overlap(a: str, b: str) -> float:
    ta, tb = set(_tok(a)), set(_tok(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _tok(text: str) -> list[str]:
    """分词：西文按词、CJK 按**双字滑窗**。

    第一版用 `re.split` 按非词字符切 —— 对中文完全失效：中文不写空格，
    整句被当成**一个 token**，任何两句的重叠恒为 0，近重复归并对中文
    永远不触发（而本平台的研究记录大量是中文）。这是"按空格分词"碰上
    CJK 的经典失效，且它不报错，只是永远不干活。

    双字滑窗（bigram）是这里的正确粒度：不需要词典、对措辞微调稳健
    （"能量误差" vs "的能量误差"共享大部分 bigram），也不会像单字那样
    把无关句子判成相似。
    """
    import re

    text = (text or "").lower()
    latin = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    grams: list[str] = []
    for run in cjk_runs:
        if len(run) == 1:
            grams.append(run)
        else:
            grams += [run[i:i + 2] for i in range(len(run) - 1)]
    return latin + grams


def _org(state: Any, *, kind: str | None = None) -> list[dict]:
    try:
        rows = state.list_kb("claims") or []
    except Exception:
        return []
    from core.org_corrections import in_force

    # 组织已经推翻 / 取代的条目不再参与维护：不和谁合并、不算矛盾、不老化质询 ——
    # 它们留在账本里，只是不再作数。
    out = [r for r in rows if isinstance(r, dict) and r.get("scope") == "org" and in_force(r)]
    if kind:
        out = [r for r in out if _kind(r) == kind]
    return out


def _kind(rec: dict) -> str:
    return str(rec.get("org_kind") or rec.get("claim_type") or "")


def _statement(rec: dict) -> str:
    return str(rec.get("statement") or rec.get("claim_text") or "")


def _rep(rec: dict) -> int:
    return int(rec.get("replication_count") or 1)


def _same_project(a: dict, b: dict) -> bool:
    return ((a.get("promoted_from") or {}).get("project_id")
            == (b.get("promoted_from") or {}).get("project_id"))


def _days_between(a_iso: str, b_iso: str) -> float:
    from datetime import datetime

    try:
        fmt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        return abs((fmt(b_iso) - fmt(a_iso)).days)
    except Exception:
        return 0.0
