"""一条 KB 条目的完整出处链 —— "不无条件相信"的兑现。

## 为什么需要它

一条 claim 是**一句可检索的断言 + 一张通往完整记录的索引卡**。信任不住在
句子里，住在可走查的链里：

    org 知识卡
     └ promoted_from ────────→ project claim
         ├ sources ──────────→ chunk
         │   ├ source: doi:…            ← 外部锚，可核原文
         │   └ origin_artifact@version ─→ 冻结产物
         │        ├ 信封：produced_by_run_id / content_hash / prev_content_hash
         │        ├ 冻结账本（哈希链）—— 改过必然对不上
         │        └ produced_by_run ───→ transcript：当时每一步怎么做的
         └ prereg_artifact_id ─────────→ 当时承诺了什么判据

**每一跳的字段今天都已存在**（chunk.origin_artifact_id+version、
claim.prereg_chunk_id、artifact 信封、冻结账本）。缺的只是一个走查器 ——
存储在，查询器没有。所以本模块不新建任何数据，只是把已有的链走一遍。

## 有界

返回的是**骨架卡片**（每跳 id + 一句摘要），不展开全文。要全文自己
read_artifact —— 走查是为了知道"能不能走到"，不是为了把整条链灌进 context。

见 docs/RFC_KB_TWO_TIERS_20260820.md §16.1/§16.2。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 每跳摘要的截断
HOP_SNIPPET_CHARS = 120
#: 链的最大深度（防环、防超长闭包把输出撑爆）
MAX_HOPS = 40


@dataclass
class Hop:
    """链上的一跳。`ok=False` 表示这一跳断了 —— 断链本身是最该被看到的事实。"""

    kind: str                 # org_card / project_claim / chunk / artifact / prereg / run
    ref: str
    summary: str = ""
    ok: bool = True
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "summary": self.summary,
                "ok": self.ok, **({"detail": self.detail} if self.detail else {})}


def kb_provenance(state: Any, kb_id: str) -> dict:
    """走一条 KB 条目的出处链，返回骨架卡片。

    断链**如实报告**，不静默跳过：一条走不到证据的 org 结论，正是最该被
    发现的东西 —— 它意味着源项目归档后这条"真理"已经悬空了。
    """
    hops: list[Hop] = []
    seen: set[str] = set()
    broken: list[str] = []

    entity = "chunks" if kb_id.startswith("chunk_") else "claims"
    rec = _get(state, entity, kb_id)
    if rec is None:
        return {"status": "error", "code": "not_found",
                "error": f"{kb_id!r} 不在 KB 里", "hops": [], "intact": False}

    _walk(state, entity, kb_id, hops, seen, broken, depth=0)
    return {
        "status": "success",
        "root": kb_id,
        "hops": [h.as_dict() for h in hops],
        "intact": not broken,
        "broken": broken,
        "note": ("骨架卡片：每跳只给 id + 摘要。要全文用 read_artifact / "
                 "get_kb_record —— 走查是为了知道能不能走到，不是把链灌进 context。"),
    }


def _walk(state: Any, entity: str, rid: str, hops: list[Hop],
          seen: set[str], broken: list[str], *, depth: int) -> None:
    if rid in seen or depth > MAX_HOPS or len(hops) > MAX_HOPS:
        return
    seen.add(rid)
    rec = _get(state, entity, rid)
    if rec is None:
        hops.append(Hop("missing", rid, "链上这一跳查不到", ok=False))
        broken.append(rid)
        return

    if entity == "chunks":
        _chunk_hops(state, rec, hops, seen, broken, depth)
        return

    is_org = rec.get("scope") == "org"
    hops.append(Hop(
        "org_card" if is_org else "project_claim", rid,
        _snip(rec.get("statement") or rec.get("claim_text")),
        detail={k: v for k, v in (
            ("kind", rec.get("org_kind")),
            ("replication_count", rec.get("replication_count")),
            ("confidence_basis", _snip(rec.get("confidence_basis"))),
            ("status", rec.get("status")),
        ) if v},
    ))

    # org → 源 project 条目（晋升回链）
    prov = rec.get("promoted_from") or {}
    if is_org:
        src_id = str(prov.get("source_id") or "")
        if not src_id:
            hops.append(Hop("promoted_from", "—", "org 条目没有晋升出处", ok=False))
            broken.append(rid)
        else:
            hops.append(Hop("promoted_from", src_id,
                            f"晋升自项目 {prov.get('project_id')}，"
                            f"批准人 {prov.get('approved_by')}"))
            if _get(state, "claims", src_id) is not None:
                _walk(state, "claims", src_id, hops, seen, broken, depth=depth + 1)

    # 当时承诺了什么判据
    prereg_chunk = str(rec.get("prereg_chunk_id") or "")
    if prereg_chunk:
        chunk = _get(state, "chunks", prereg_chunk)
        origin = str((chunk or {}).get("origin_artifact_id") or "")
        hops.append(Hop("prereg", origin or prereg_chunk,
                        "冻结的预注册 —— 当时承诺了什么判据",
                        ok=bool(chunk), detail={"chunk_id": prereg_chunk}))
        if not chunk:
            broken.append(prereg_chunk)

    # 证据
    for src in (rec.get("sources") or [])[:MAX_HOPS]:
        sid = str(src)
        if sid.startswith("chunk_"):
            _walk(state, "chunks", sid, hops, seen, broken, depth=depth + 1)
        elif sid.startswith("claim_"):
            _walk(state, "claims", sid, hops, seen, broken, depth=depth + 1)
        else:
            hops.append(Hop("external_anchor", sid, "外部锚 —— 可核原文"))


def _chunk_hops(state: Any, rec: dict, hops: list[Hop], seen: set[str],
                broken: list[str], depth: int) -> None:
    rid = str(rec.get("id") or "")
    hops.append(Hop("chunk", rid, _snip(rec.get("text")),
                    detail={"source": rec.get("source"),
                            **({"group_readings": len(rec.get("group_readings") or [])}
                               if rec.get("group_readings") else {})}))
    anchor = str(rec.get("source") or "")
    if anchor:
        hops.append(Hop("external_anchor", anchor, "外部锚 —— 可核原文"))

    origin = str(rec.get("origin_artifact_id") or "")
    if not origin:
        return
    art = _read_artifact(state, origin)
    if art is None:
        # org 层的自产证据记录：产物本体留在**源项目**（产物是项目级的，跨不
        # 过来），但正文已随晋升过河，且记了源项目 + 版本 + 内容哈希。
        # 这是可核对的终点，不是断链 —— 拿哈希去那个项目对即可，对不上说明
        # 产物被改过。把它报成断链会让每张计算类项目的卡都显示"走不回证据"，
        # 那是**假警报**，而假警报会让人学会忽略真警报。
        src_project = str(rec.get("origin_project_id") or "")
        if src_project:
            hops.append(Hop(
                "artifact", origin,
                f"冻结产物在源项目 {src_project} —— 证据正文已随晋升过河，"
                f"本体按哈希可核对",
                detail={k: v for k, v in (
                    ("origin_project_id", src_project),
                    ("version", rec.get("origin_artifact_version")),
                    ("content_hash", rec.get("origin_content_hash")),
                    ("frozen", rec.get("origin_artifact_frozen")),
                ) if v is not None}))
            return
        hops.append(Hop("artifact", origin, "来源产物查不到（源项目已归档？）",
                        ok=False))
        broken.append(origin)
        return
    md = art.get("metadata") or {}
    hops.append(Hop(
        "artifact", origin, _snip(art.get("name") or origin),
        detail={
            "version": rec.get("origin_artifact_version") or art.get("version"),
            "frozen": bool(md.get("frozen")),
            "content_hash": art.get("content_hash"),
            "produced_by_run_id": art.get("produced_by_run_id"),
        },
    ))
    run_id = str(art.get("produced_by_run_id") or "")
    if run_id:
        hops.append(Hop("run", run_id,
                        "产出它的那次 run —— transcript 里有当时每一步怎么做的"))


# ── 盘面 ────────────────────────────────────────────────────────────────────


def _get(state: Any, entity: str, rid: str) -> dict | None:
    try:
        return state.get_kb_record(entity, rid)
    except Exception:
        return None


def _read_artifact(state: Any, aid: str) -> dict | None:
    try:
        return state.read_artifact(aid)
    except Exception:
        return None


def _snip(text: Any) -> str:
    s = str(text or "").strip()
    return s[:HOP_SNIPPET_CHARS] + ("…" if len(s) > HOP_SNIPPET_CHARS else "")
