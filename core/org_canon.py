"""正典层：领域活综述 —— 卡片是中间形态，正典是沉淀形态。

## 为什么必须有这一层

账本层为**审计**而生：原子、可溯源、不可合并（合并掉就丢审计链）。
但"值得花 token 读"的判据落在**消费侧** —— 新项目开题不该读 400 条原子
claim，该读 2 页领域综述、再按需下钻到被引条目。

这不是新发明，是科学自己组织知识的方式：论文 → 综述 → 教科书。
三层都是知识的载体，浓度递减、可信度背书递增。

## 防卡片坟场

几年后失败画像：检索命中 30 张半相关卡 = 又一种噪音。防线是把**沉淀方向**
写进机制：

    卡片 → 被正典综述吸收（叙述中引用）→ 新项目读正典、按需下钻卡片

被吸收的卡片降低检索权重（**不删除** —— 账本永远走得回去）。
成熟 org KB 主要通过正典被阅读，卡片是账本与正典之间的中间层。

## 唯一写者

正典只由 curator 写（dreaming 的综述刷新作业）。多写者的正典不是正典，
是又一个会分叉的抄件 —— 这与 MEMORY.md 单写者是同一条判断。

见 docs/RFC_KB_TWO_TIERS_20260820.md §8/§9/§18.6。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 综述刷新的触发阈值：某域自上次刷新以来新增晋升条目数。
#: 定期刷新是浪费（没新东西时重写一遍综述纯烧钱），按**增量**触发才对。
CANON_REFRESH_THRESHOLD = 8

#: 一篇活综述的目标长度（字符）。1–3 页 —— 超出说明该拆子域了，
#: 而不是把综述写成第二个账本。
CANON_TARGET_CHARS = 6000

#: artifact 类型：正典综述用文档承载（不是 KB 记录）。
#: 理由：它是**叙述**，有版本、有作者、要被人读和改 —— 那是 artifact 的形状。
CANON_ARTIFACT_TYPE = "org_canon"


@dataclass
class DomainState:
    """一个域的正典现状。"""

    domain: str
    canon_id: str | None = None
    canon_version: int = 0
    absorbed_ids: tuple[str, ...] = ()
    pending_ids: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    @property
    def needs_refresh(self) -> bool:
        return len(self.pending_ids) >= CANON_REFRESH_THRESHOLD

    def as_dict(self) -> dict:
        return {
            "domain": self.domain, "canon_id": self.canon_id,
            "canon_version": self.canon_version,
            "absorbed": len(self.absorbed_ids),
            "pending": list(self.pending_ids),
            "needs_refresh": self.needs_refresh,
            "open_questions": list(self.open_questions),
        }


#: 没有域的条目落进这个桶。它**不是一个域** —— 不写综述，只报数。
UNSORTED = "unsorted"


def domain_of(record: dict) -> str:
    """一条 org 记录属于哪个域。

    显式 `domain` 优先；没有就退回"未分域" —— **不猜**。
    猜错的分域比没有分域更糟：它会让开题注入送错知识，而错误的"本组已知"
    比没有已知更有害（人会照着它走）。
    """
    d = str(record.get("domain") or "").strip()
    return d or UNSORTED


def survey_domains(state: Any) -> list[DomainState]:
    """扫出每个域的正典现状与待吸收卡片。有界：只读 org 层记录的元信息。"""
    canons = _canon_index(state)
    buckets: dict[str, list[dict]] = {}
    for rec in _org_records(state):
        buckets.setdefault(domain_of(rec), []).append(rec)

    out: list[DomainState] = []
    for domain, records in sorted(buckets.items()):
        canon = canons.get(domain)
        absorbed = set((canon or {}).get("absorbed_ids") or ())
        pending = tuple(str(r.get("id")) for r in records
                        if str(r.get("id")) not in absorbed)
        out.append(DomainState(
            domain=domain,
            canon_id=(canon or {}).get("artifact_id"),
            canon_version=int((canon or {}).get("version") or 0),
            absorbed_ids=tuple(sorted(absorbed)),
            pending_ids=pending,
            open_questions=tuple(
                str(r.get("id")) for r in records
                if str(r.get("org_kind") or "") == "open_question"),
        ))
    return out


def refresh_jobs(state: Any) -> list[dict]:
    """哪些域该刷新综述了 —— 提议，不直落。

    综述是**叙述**：它要讲清这个域的脉络、争论在哪、共识到哪一步。
    机械层给不出叙述，只能指出"该刷了"和"待吸收哪些"。
    """
    jobs = []
    for ds in survey_domains(state):
        if not ds.needs_refresh:
            continue
        if ds.domain == UNSORTED:
            # `unsorted` 是"没填域"的桶，不是一个领域。给它派综述作业等于让
            # curator 把一堆互不相干的结论写进同一篇叙述 —— 真实数据上这里
            # 攒了 224 条（LAMMPS + 元胞自动机 + MLIP 混在一起）。
            # 报成待归档，不报成待综述：前者人能处理，后者只会烧掉一趟 run。
            jobs.append({
                "kind": "unfiled_backlog",
                "domain": UNSORTED,
                "count": len(ds.pending_ids),
                "sample": list(ds.pending_ids[:10]),
                "instruction": (
                    f"有 {len(ds.pending_ids)} 条 org 条目没有 domain，无法归入任何"
                    "活综述 —— 它们对开题注入是不可见的。请为它们补域"
                    "（suggest_from_evidence 可从证据链的 arXiv 分类机械建议，"
                    "查得到就给、查不到不猜），或判定为历史债不再维护。"
                    "**不要**把它们写进同一篇综述。"
                ),
            })
            continue
        jobs.append({
            "kind": "canon_refresh",
            "domain": ds.domain,
            "canon_id": ds.canon_id,
            "current_version": ds.canon_version,
            "absorb": list(ds.pending_ids),
            "open_questions": list(ds.open_questions),
            "instruction": (
                f"重写「{ds.domain}」的活综述：把这 {len(ds.pending_ids)} 条新晋升"
                "条目吸收进叙述（引用其 id），保留仍成立的旧结论，"
                "把开放问题单列一节。目标 1–3 页 —— 写不下说明该拆子域，"
                "不是把综述写成第二个账本。"
            ),
        })
    return jobs


def write_canon(state: Any, *, domain: str, body: str,
                absorbed_ids: list[str], at: str) -> dict:
    """落一版活综述。**唯一写者是 curator。**

    吸收关系记在综述的 metadata 里，供检索降权使用 —— 被吸收的卡片
    不删除，只是不再优先出现在检索面上（账本永远走得回去）。
    """

    if not body.strip():
        # 复审改判 C（操作无对象）：空正文落成当前版会静默清空检索面上的
        # 上一版叙述——这不是充分性判决，是「没有东西可写」的事实报错。
        return {"status": "error", "code": "empty_canon",
                "error": "综述正文为空 —— 没有内容可落版"}

    existing = _canon_index(state).get(domain)
    version = int((existing or {}).get("version") or 0) + 1
    merged = sorted(set((existing or {}).get("absorbed_ids") or ())
                    | set(absorbed_ids or ()))
    saved = state.save_artifact(
        CANON_ARTIFACT_TYPE,
        _canon_name(domain),
        body,
        metadata={
            "domain": domain,
            "version": version,
            "absorbed_ids": merged,
            "refreshed_at": at,
            "oversize": len(body) > CANON_TARGET_CHARS,
        },
    )
    return {"status": "success", "artifact_id": saved["id"],
            "domain": domain, "version": version,
            "absorbed_count": len(merged),
            "oversize": len(body) > CANON_TARGET_CHARS}


def canon_for_domain(state: Any, domain: str) -> dict | None:
    """开题注入读的就是这个 —— 正典优先，卡片按需下钻。

    按注册表上行链命中**最细的有正典的节点**：项目声明
    physics.comp-ph/mlip-robustness，该叶没综述就读 physics.comp-ph 的 ——
    粗粒度转述是上层综述的活，卡片不多说法（KB_SYSTEM_STATE §六.2）。
    """
    from core.domain_registry import ancestors

    index = _canon_index(state)
    entry = None
    for node in ancestors(domain) or (domain,):
        entry = index.get(node)
        if entry:
            domain = node
            break
    if not entry:
        return None
    rec = _read(state, entry["artifact_id"])
    if not rec:
        return None
    return {
        "artifact_id": entry["artifact_id"],
        "domain": domain,
        "version": entry["version"],
        "body": str(rec.get("content") or ""),
        "absorbed_ids": list(entry.get("absorbed_ids") or ()),
    }


def is_absorbed(state: Any, org_id: str) -> bool:
    """这条卡片已被正典吸收了吗 —— 检索降权用（不是删除）。"""
    for entry in _canon_index(state).values():
        if org_id in set(entry.get("absorbed_ids") or ()):
            return True
    return False


# ── 盘面 ────────────────────────────────────────────────────────────────────


def _canon_name(domain: str) -> str:
    return domain.replace("/", "_").replace(" ", "_") or "unsorted"


def _canon_index(state: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        entries = state.list_artifacts(CANON_ARTIFACT_TYPE) or []
    except Exception:
        return out
    for entry in entries:
        rec = _read(state, str(entry.get("id") or ""))
        md = (rec or {}).get("metadata") or {}
        domain = str(md.get("domain") or "")
        if not domain:
            continue
        prev = out.get(domain)
        version = int(md.get("version") or 0)
        if prev is None or version >= int(prev.get("version") or 0):
            out[domain] = {
                "artifact_id": str(entry.get("id")),
                "version": version,
                "absorbed_ids": list(md.get("absorbed_ids") or ()),
            }
    return out


def _read(state: Any, artifact_id: str) -> dict | None:
    try:
        return state.read_artifact(artifact_id)
    except Exception:
        return None


def _org_records(state: Any) -> list[dict]:
    try:
        rows = state.list_kb("claims") or []
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("scope") == "org"]
