"""cluster_hypothesis_candidates — 候选假设去重聚类（Co-Scientist Proximity agent 轻量版）。"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "than", "that", "this", "with",
    "的", "了", "在", "与", "和", "比", "更", "是", "有", "对", "为",
})


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _token_set(text: str) -> set[str]:
    return {
        t for t in _normalize(text).split()
        if len(t) > 2 and t not in _STOPWORDS
    }


def overlap_ratio(a: str, b: str) -> float:
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def cluster_by_overlap(
    items: list[dict[str, Any]],
    *,
    threshold: float = 0.55,
) -> dict[str, Any]:
    """Greedy clustering: first item in cluster is representative."""
    n = len(items)
    assigned = [-1] * n
    clusters: list[list[int]] = []

    for i in range(n):
        if assigned[i] >= 0:
            continue
        cluster_id = len(clusters)
        cluster = [i]
        assigned[i] = cluster_id
        text_i = items[i].get("claim_text") or items[i].get("hypothesis_text") or ""

        for j in range(i + 1, n):
            if assigned[j] >= 0:
                continue
            text_j = items[j].get("claim_text") or items[j].get("hypothesis_text") or ""
            if overlap_ratio(text_i, text_j) >= threshold:
                cluster.append(j)
                assigned[j] = cluster_id
        clusters.append(cluster)

    pairwise: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            ti = items[i].get("claim_text") or items[i].get("hypothesis_text") or ""
            tj = items[j].get("claim_text") or items[j].get("hypothesis_text") or ""
            ratio = overlap_ratio(ti, tj)
            if ratio >= 0.35:
                pairwise.append({
                    "a": items[i].get("label") or f"H{i + 1}",
                    "b": items[j].get("label") or f"H{j + 1}",
                    "overlap": round(ratio, 3),
                    "likely_duplicate": ratio >= threshold,
                })

    cluster_out: list[dict[str, Any]] = []
    representatives: list[str] = []
    duplicates_to_merge: list[dict[str, str]] = []

    for cid, members in enumerate(clusters):
        labels = [items[m].get("label") or f"H{m + 1}" for m in members]
        rep_idx = members[0]
        rep_label = labels[0]
        representatives.append(rep_label)
        cluster_out.append({
            "cluster_id": cid,
            "size": len(members),
            "labels": labels,
            "representative": rep_label,
            "claim_text": (
                items[rep_idx].get("claim_text")
                or items[rep_idx].get("hypothesis_text")
                or ""
            )[:300],
        })
        for m in members[1:]:
            duplicates_to_merge.append({
                "duplicate": items[m].get("label") or f"H{m + 1}",
                "merge_into": rep_label,
            })

    return {
        "n_candidates": n,
        "n_clusters": len(clusters),
        "overlap_threshold": threshold,
        "clusters": cluster_out,
        "representatives": representatives,
        "duplicates_to_merge": duplicates_to_merge,
        "pairwise_high_overlap": pairwise,
        "recommendation": (
            f"保留 {len(representatives)} 个代表候选"
            + (f"，合并/丢弃 {len(duplicates_to_merge)} 条重复" if duplicates_to_merge else "")
        ),
    }


def rank_candidates(
    items: list[dict[str, Any]],
    *,
    cluster_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Composite ranking: HIF primary, gap coverage secondary, overlap penalty."""
    rep_labels = set()
    if cluster_result:
        rep_labels = set(cluster_result.get("representatives") or [])

    ranked: list[dict[str, Any]] = []
    for item in items:
        label = item.get("label") or "?"
        hif = int(item.get("hif") or 0)
        g = int(item.get("G") or item.get("gap_score") or 0)
        q = item.get("Q")
        plausibility_reject = q is not None and int(q) <= 1
        is_rep = not rep_labels or label in rep_labels
        overlap_penalty = 0 if is_rep else 15
        composite = hif + g * 2 - overlap_penalty
        if plausibility_reject:
            composite = 0
        ranked.append({
            "label": label,
            "claim_text": (item.get("claim_text") or "")[:200],
            "hif": hif,
            "composite_score": composite,
            "is_cluster_representative": is_rep,
            "plausibility_reject": plausibility_reject,
        })

    ranked.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, entry in enumerate(ranked, 1):
        entry["rank"] = i
    return ranked


async def _cluster_hypothesis_candidates(
    state: State,
    hypotheses: list[dict[str, Any]],
    overlap_threshold: float = 0.55,
    rank: bool = False,
    save_report: bool = True,
    **_: Any,
) -> dict[str, Any]:
    # hypotheses 非空由 parameters_schema 的 minItems=1 声明，派发口核取值。
    if len(hypotheses) < 2:
        return {
            "status": "success",
            "message": "仅 1 条候选，跳过聚类",
            "n_clusters": 1,
            "representatives": [hypotheses[0].get("label") or "H1"],
        }

    result = cluster_by_overlap(hypotheses, threshold=overlap_threshold)
    state.hook_state["hypothesis_cluster_result"] = result

    ranking: list[dict[str, Any]] | None = None
    if rank:
        ranking = rank_candidates(hypotheses, cluster_result=result)
        result["ranking"] = ranking

    artifact_id: str | None = None
    if save_report:
        md_lines = [
            "# Hypothesis Candidate Clustering",
            "",
            f"- candidates: {result['n_candidates']}",
            f"- clusters: {result['n_clusters']}",
            f"- threshold: {overlap_threshold}",
            f"- **{result['recommendation']}**",
            "",
            "## Clusters",
        ]
        for c in result["clusters"]:
            md_lines.append(
                f"- cluster {c['cluster_id']}: {c['representative']} "
                f"(size={c['size']}, members={c['labels']})"
            )
        if result["duplicates_to_merge"]:
            md_lines.extend(["", "## Duplicates to merge/drop"])
            for d in result["duplicates_to_merge"]:
                md_lines.append(f"- {d['duplicate']} → merge into {d['merge_into']}")
        if ranking:
            md_lines.extend(["", "## Ranking"])
            for r in ranking:
                md_lines.append(
                    f"- #{r['rank']} {r['label']}: composite={r['composite_score']} "
                    f"(HIF={r['hif']})"
                )
        content = (
            "\n".join(md_lines)
            + "\n\n---\n\n```json\n"
            + json.dumps(result, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
        art = state.save_artifact(
            "hypothesis_cluster_report",
            "Candidate_Clustering",
            content,
            metadata={
                "n_candidates": result["n_candidates"],
                "n_clusters": result["n_clusters"],
                "n_duplicates": len(result["duplicates_to_merge"]),
            },
        )
        artifact_id = art["id"]

    dup_count = len(result["duplicates_to_merge"])
    msg = result["recommendation"]
    if dup_count:
        msg += f"。高重叠对: {len(result['pairwise_high_overlap'])}"

    return {
        "status": "success",
        "artifact_id": artifact_id,
        "message": msg,
        **result,
    }


register_tool(
    ToolDefinition(
        name="cluster_hypothesis_candidates",
        description=(
            "对 3–6 条候选 hypothesis 做词汇重叠聚类与去重建议（Co-Scientist Proximity 轻量版）。\n\n"
            "**Use when**：\n"
            "  - 发散阶段产出 ≥3 条候选，收敛到 1–3 条之前\n"
            "  - 怀疑多条假设实质是同一机制的不同表述\n\n"
            "**Do NOT use when**：\n"
            "  - 只有 1–2 条候选\n"
            "  - prereg 已 freeze\n\n"
            "**参数**：rank=true 时按 HIF+G 做 composite 排序（需在 hypotheses 里带 hif/G/Q）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "claim_text": {"type": "string"},
                            "hif": {"type": "integer"},
                            "G": {"type": "integer"},
                            "Q": {"type": "integer"},
                        },
                        "required": ["claim_text"],
                    },
                    "minItems": 1,
                },
                "overlap_threshold": {
                    "type": "number",
                    "default": 0.55,
                    "description": "判定重复的词汇重叠阈值",
                },
                "rank": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否输出 composite 排序",
                },
                "save_report": {"type": "boolean", "default": True},
            },
            "required": ["hypotheses"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _cluster_hypothesis_candidates,
)
