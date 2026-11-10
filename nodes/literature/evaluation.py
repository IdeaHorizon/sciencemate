"""Offline measurement and independent review records for literature search.

Run with: python -m nodes.literature.evaluation --input search.json --out report.json
The input may be a search API response or a saved search audit. This records
exploratory assessments; formal scoring follows the selected benchmark protocol.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlparse

from shared.contrib.literature_identity import PublicationIdentityIndex

DIMENSIONS = {
    "relevance_ranking": "前排相关与排序",
    "evidence_quality": "论文证据质量",
    "facet_coverage": "问题要点覆盖",
    "reference_validity": "引文真实性",
    "effective_research_count": "有效研究数量",
    "bibliographic_accuracy": "书目准确与完整",
    "temporal_fit": "时间适切性",
}


def evaluate_search(result: dict, reviews: dict | None = None) -> dict:
    papers = result.get("papers", [])
    if not isinstance(papers, list) or any(not isinstance(p, dict) for p in papers):
        raise ValueError("papers 必须为文献对象列表")
    identities = PublicationIdentityIndex()
    duplicates = 0
    for paper in papers:
        current = identities.find(paper)
        if current is None:
            identities.remember(dict(paper))
        else:
            duplicates += 1
            identities.remember(current, alias=paper)
    count = len(papers)
    supplied = (reviews or {}).get("dimensions", {})
    if set(supplied) - set(DIMENSIONS):
        raise ValueError("评分记录包含未知维度")
    dimensions = {}
    for key, label in DIMENSIONS.items():
        review = dict(supplied.get(key) or {})
        score = review.get("score")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError(f"{label}必须是 0–100 分，未判断时填写 null")
            if review.get("method") not in {"human", "ai_assisted", "automated_verification"}:
                raise ValueError(f"{label}缺少有效评价方式")
            if not str(review.get("reviewer") or "").strip() or not str(review.get("reason") or "").strip():
                raise ValueError(f"{label}须记录评审者及判断理由")
            if not isinstance(review.get("evidence"), list) or not review["evidence"] or any(not isinstance(e, str) or not e.strip() for e in review["evidence"]):
                raise ValueError(f"{label}须提供文献或核验记录等证据")
        dimensions[key] = {
            "label": label, "score": score, "scale": 100,
            "status": "assessed" if score is not None else "unassessed",
            "method": review.get("method"), "reviewer": review.get("reviewer"),
            "reason": review.get("reason") or "尚无充分评价依据",
            "evidence": review.get("evidence") or [],
        }
    fields = ("title", "authors", "year", "venue", "abstract")
    field_counts = {field: {"present": sum(bool(p.get(field)) for p in papers), "total": count}
                    for field in fields}
    link_count = sum(bool(p.get("doi")) or urlparse(str(p.get("url") or "")).scheme in {"http", "https"}
                     for p in papers)
    diagnostic = result.get("strategy_diagnostics") or {}
    return {
        "schema_version": 1, "created_at": datetime.now(UTC).isoformat(),
        "evaluation_level": "exploratory", "formal_scores_available": False,
        "query": result.get("query") or (result.get("request") or {}).get("query"),
        "audit_id": result.get("audit_id") or diagnostic.get("audit_id"),
        "search_code_sha256": result.get("search_code_sha256"),
        "input_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "conditions": {"request": result.get("request", {}), "options": result.get("options", {}),
                       "from_cache": result.get("from_cache"), "scope": result.get("scope")},
        "automated_metrics": {
            "status": result.get("status", "completed"), "returned_count": count,
            "unique_identity_count": count - duplicates, "duplicate_count": duplicates,
            "duplicate_rate_percent": round(duplicates / count * 100, 2) if count else None,
            "elapsed_seconds": result.get("elapsed_seconds", (result.get("timings") or {}).get("total")),
            "field_presence": field_counts, "identifier_or_link_present": {"present": link_count, "total": count},
            "source_counts": dict(Counter(str(p.get("source") or "unknown") for p in papers)),
        },
        "dimensions": dimensions, "weighted_total": None,
        "interpretation": [
            "字段存在不等于书目准确，链接存在不等于论文真实性已核验。",
            "缺失摘要不自动降低论文证据质量；未知维度保留未评分。",
            "本文件记录探索性评价；正式评分须遵循选定 benchmark 版本的标注与 gold/qrels 流程。",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reviews", type=Path, help="JSON containing independent dimensions and evidence")
    args = parser.parse_args()
    result = json.loads(args.input.read_text(encoding="utf-8"))
    reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else None
    report = evaluate_search(result, reviews)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
