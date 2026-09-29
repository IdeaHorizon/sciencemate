"""v0.9：cited claim_id 真实性检查 —— 防 LLM hallucinate phantom citation。

v5 dogfood 实测：writing LLM 在 manuscript 引 `claim_9f8f2f80` 4+ 次，但 KB
里没这条 claim —— LLM 编了一个符合 `claim_<8hex>` 格式的 fake id。reviewer
看到但没 escalate 为 critical（因为非 hypothesis verdict 类错误），结果 KB
诚信被破坏。

机械层根因：writing 节点的 `manuscript_cites_kb_claims` qc 只数 claim_id 出
现次数，不验证每个 id 在 KB 真存在。

本 utility 提供**纯 Python 机械验证**（不靠 LLM）：
  - 正则扫 artifact 文本里所有 `claim_<8+ hex>`
  - 跟 state.list_kb("claims") 真存在的对比
  - 返 phantom list（cited 但 KB 不在 → LLM 编造）+ 真实 cited list

写 manuscript / analysis_report 的节点应该用本 utility 跑 qc，phantom
threshold=0（任何 1 个 phantom 都算严重诚信问题）。

API:
    find_phantom_citations(content: str, state) → list[str]   # phantom claim_ids
    validate_artifact_citations(artifact_id: str, state) → dict
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from shared.lib.artifact_text import artifact_text

# claim_id 格式：claim_<8+ hex chars>（KB v3 用 8 hex；扩到 8-32 cover 兼容）
# 容忍 LaTeX 转义的 `\_` —— writing 节点产 .tex 时 `_` 必须转义，但语义上仍是同一 claim_id
_CLAIM_ID_RE = re.compile(r"\bclaim\\?_([a-f0-9]{8,32})\b")
_CONCEPT_ID_RE = re.compile(r"\bconcept\\?_([a-f0-9]{8,32})\b")
_EPISTEMIC_VERSION_ID_RE = re.compile(r"\bepv\\?_([a-f0-9]{8,32})\b")


def _scientific_epistemic_versions(
    state: Any,
    version_ids: set[str],
) -> dict[str, dict[str, str]]:
    """Resolve Scientific Capital ``epv_*`` IDs without coupling legacy mode.

    Scientific Capital is integrated on a separate rollout branch, while this
    citation utility is also used by the legacy-only main branch.  Reading the
    append-only SQLite store directly keeps the compatibility boundary narrow:
    legacy projects have no database and simply return no matches; scientific
    projects validate the exact version IDs that retrieval gave the writer.
    The connection is read-only so citation validation can never mutate capital.
    """
    project_root = getattr(state, "project_root", None)
    if project_root is None or not version_ids:
        return {}
    db_path = Path(project_root) / "scientific_capital" / "scientific_capital.sqlite"
    if not db_path.is_file():
        return {}

    found: dict[str, dict[str, str]] = {}
    ordered = sorted(version_ids)
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for start in range(0, len(ordered), 500):
                chunk = ordered[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""SELECT ev.id, ev.support_status, eo.object_type
                        FROM epistemic_versions AS ev
                        JOIN epistemic_objects AS eo ON eo.id = ev.object_id
                        WHERE ev.id IN ({placeholders})""",
                    chunk,
                ).fetchall()
                for version_id, support_status, object_type in rows:
                    found[str(version_id)] = {
                        "support_status": str(support_status),
                        "object_type": str(object_type),
                    }
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        # Absence/corruption is fail-closed: unresolved epv IDs remain phantom.
        return {}
    return found


def find_phantom_citations(
    content: str,
    state: Any,
    *,
    entity: str = "claims",
    include_org: bool = True,
) -> dict:
    """扫文本找所有 `claim_<id>`，对比 KB 看哪些是 phantom（LLM 编的）。

    Args:
      content: 要扫的文本（manuscript / analysis_report 等 artifact content）
      state: harness State；用 state.list_kb / state.project_root
      entity: "claims" 或 "concepts"；默认 claims（最常 phantom）
      include_org: 也扫 org-scope KB（默认 True；跨项目 claim 也算真实）

    Returns:
      {
        "cited":   set of all claim_ids 在文本里找到的（已含 prefix）
        "real":    set of those that exist in KB
        "phantom": set of those that DON'T exist (LLM hallucinated)
        "cited_count_per_id": dict[claim_id, int]   引用频次
      }
    """
    pat = _CLAIM_ID_RE if entity == "claims" else _CONCEPT_ID_RE
    prefix = "claim_" if entity == "claims" else "concept_"

    # 扫文本
    cited = set()
    counts: dict[str, int] = {}
    for m in pat.finditer(content):
        full_id = prefix + m.group(1)
        cited.add(full_id)
        counts[full_id] = counts.get(full_id, 0) + 1

    # Scientific Capital uses immutable epistemic-version IDs as the citation
    # anchor.  They are valid knowledge citations alongside legacy claim IDs.
    if entity == "claims":
        for m in _EPISTEMIC_VERSION_ID_RE.finditer(content):
            full_id = "epv_" + m.group(1)
            cited.add(full_id)
            counts[full_id] = counts.get(full_id, 0) + 1

    if not cited:
        return {
            "cited": set(), "real": set(), "phantom": set(),
            "cited_count_per_id": {},
        }

    # KB 里真存在的 id（project + org）
    real_ids = {r["id"] for r in state.list_kb(entity)}
    scientific_records: dict[str, dict[str, str]] = {}
    if entity == "claims":
        scientific_records = _scientific_epistemic_versions(
            state, {citation for citation in cited if citation.startswith("epv_")},
        )
        real_ids.update(scientific_records)
    if include_org and state.project_root is not None:
        # state.list_kb 默认应该包含 org —— 但保险起见再扫一次 org file 直读
        # state.list_kb 行为见 core/state.py；它返合并后的 list（含 org）
        # 这里不重复加，list_kb 已包含
        pass

    phantom = cited - real_ids
    real = cited & real_ids

    # v3.1（审计）：引用"存在"不够 —— 引用已 refuted / superseded 的 claim
    # 而不注明，同样是诚信问题（把已被推翻的结论当有效证据用）。
    # 单独返 invalid_status 列表；调用方（qc / reviewer 规则）决定严重度。
    invalid_status: dict[str, str] = {}
    if entity == "claims" and real:
        by_id = {r["id"]: r for r in state.list_kb(entity)}
        for cid in real:
            st = (by_id.get(cid) or {}).get("status")
            if cid.startswith("epv_"):
                support = (scientific_records.get(cid) or {}).get("support_status")
                # Unsupported candidates remain citable for explicit discussion,
                # but cannot silently pass as supporting knowledge.
                if support == "unsupported_candidate":
                    st = support
            if st in (
                "refuted", "superseded", "needs_review", "unsupported_candidate",
            ):
                invalid_status[cid] = st

    return {
        "cited": cited, "real": real, "phantom": phantom,
        "invalid_status": invalid_status,
        "cited_count_per_id": counts,
    }


# ── v3.2 item#3：manuscript ↔ KB verdict 一致性（verdict 版 phantom-citation）──
#
# 实测（v9 dogfood）：论文正文写 "H3 partially supported"，KB 里 H3 claim status
# 却是 open（从没判定过）；orchestrator 汇报写 "H2 partially supported"，KB 里 H2
# 是 refuted。"每个结论可溯源"的第一卖点被自己的产物打破，却无任何机制拦。
#
# 这是 phantom-citation 的 **verdict 版**：上游只查"引用的 claim_id 存不存在"，
# 没查"论文对这条 hypothesis 下的结论跟 KB 里它的真实 status 对不对得上"。

# 论文正文里的 verdict 断言词 → 归一类别
_VERDICT_AFFIRM = re.compile(
    r"(supported|confirmed|validated|upheld|holds\b|成立|证实|支持)", re.IGNORECASE)
_VERDICT_REFUTE = re.compile(
    r"(refuted|rejected|falsified|disproven|not supported|证伪|拒绝|推翻|不成立)",
    re.IGNORECASE)
_VERDICT_PARTIAL = re.compile(
    r"(partial|mixed|inconclusive|部分支持|部分)", re.IGNORECASE)

# KB claim.status → 该 status 下"允许被正文断言"的 verdict 类别
# open / needs_review = 尚未定论 → 正文不该对它下任何确定 verdict
_STATUS_UNDECIDED = {"open", "needs_review"}


def check_verdict_consistency(content: str, state: Any) -> dict:
    """检查 manuscript/analysis 正文对 hypothesis 的 verdict 陈述与 KB status 一致。

    只对**正文里引用了 claim_id 的 hypothesis 类 claim** 做机械核对（有 claim_id
    锚点，零 NLP 歧义）。窗口 ±500 字符内找 verdict 断言词，与 KB status 比对。

    Returns:
      {
        "checked": [claim_id, ...]                # 正文引用到的 hypothesis claim
        "undecided_but_concluded": [{id,status,prose_verdict}]  # KB 未定论、正文却下了结论
        "direct_contradictions":  [{id,status,prose_verdict}]   # 正文与 KB 直接相反
        "passed": bool
      }
    """
    hyp_by_id = {
        r["id"]: r for r in state.list_kb("claims")
        if r.get("claim_type") == "hypothesis"
    }
    if not hyp_by_id or not content:
        return {"checked": [], "undecided_but_concluded": [],
                "direct_contradictions": [], "passed": True}

    undecided: list[dict] = []
    contradictions: list[dict] = []
    checked: list[str] = []

    for cid, rec in hyp_by_id.items():
        # 正文里找这个 claim_id 的所有出现位置（容忍 LaTeX \_ 转义）
        pat = re.compile(re.escape(cid).replace(r"\_", r"\\?_"))
        positions = [m.start() for m in pat.finditer(content)]
        if not positions:
            continue
        checked.append(cid)
        status = rec.get("status", "open")

        # 汇总窗口内出现的 verdict 断言
        prose = ""
        for p in positions:
            prose += " " + content[max(0, p - 500): p + 500]
        affirm = bool(_VERDICT_AFFIRM.search(prose))
        refute = bool(_VERDICT_REFUTE.search(prose))
        partial = bool(_VERDICT_PARTIAL.search(prose))
        # partial 优先（"not fully supported" 这类既命中 affirm 又该算 partial）
        prose_verdict = ("partial" if partial else
                         "refuted" if refute else
                         "affirmed" if affirm else None)
        if prose_verdict is None:
            continue    # 正文只是引用它，没下 verdict → 不管

        # (a) KB 未定论，正文却下了确定结论
        if status in _STATUS_UNDECIDED:
            undecided.append({"id": cid, "status": status,
                              "prose_verdict": prose_verdict})
            continue
        # (b) 直接矛盾：正文说成立 / KB 说 refuted（或反之）
        if ((prose_verdict == "affirmed" and status in ("refuted", "superseded"))
                or (prose_verdict == "refuted" and status == "validated")):
            contradictions.append({"id": cid, "status": status,
                                   "prose_verdict": prose_verdict})

    return {
        "checked": checked,
        "undecided_but_concluded": undecided,
        "direct_contradictions": contradictions,
        "passed": not undecided and not contradictions,
    }


def validate_artifact_citations(
    artifact_id: str, state: Any,
    *, entity: str = "claims",
) -> dict:
    """读 artifact + 跑 citation check + 返结构化结果（含 phantom 详情）。

    用于 hook on_turn_end 自动跑 + qc 读 transcript 时看的统一格式。

    Returns:
      {
        "artifact_id": str,
        "artifact_type": str,
        "passed": bool,    # phantom 为空就 pass
        "n_cited": int,
        "n_phantom": int,
        "phantom_ids": list[str],
        "phantom_with_counts": list[{"id": "claim_X", "occurrences": 4}],
        "real_ids_sample": list[str]    # 前 5 个真实引用，方便调试
      }
    """
    rec = state.read_artifact(artifact_id)
    if rec is None:
        return {
            "artifact_id": artifact_id, "passed": False,
            "error": f"artifact {artifact_id!r} 不存在",
        }
    content = artifact_text(rec)
    res = find_phantom_citations(content, state, entity=entity)
    phantom = sorted(res["phantom"])
    invalid = res.get("invalid_status") or {}
    return {
        "artifact_id": artifact_id,
        "artifact_type": rec.get("type"),
        "entity": entity,
        # v3.1：引用 refuted/superseded/needs_review 的 claim 同样 fail
        "passed": len(phantom) == 0 and len(invalid) == 0,
        "n_cited": len(res["cited"]),
        "n_phantom": len(phantom),
        "phantom_ids": phantom,
        "phantom_with_counts": [
            {"id": p, "occurrences": res["cited_count_per_id"].get(p, 0)}
            for p in phantom
        ],
        "invalid_status_citations": [
            {"id": cid, "status": st,
             "occurrences": res["cited_count_per_id"].get(cid, 0)}
            for cid, st in sorted(invalid.items())
        ],
        "real_ids_sample": sorted(res["real"])[:5],
    }
