#!/usr/bin/env python3
"""Compute seven independent v0.3 scores from completed adjudicated labels."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import yaml

try:
    from nodes.literature.benchmarks.literature_v1.merge_reviews_v3 import ALLOWED, parse_facets
except ModuleNotFoundError:  # direct `python benchmarks/.../score_v3.py`
    from merge_reviews_v3 import ALLOWED, parse_facets


FINAL_FIELDS = [
    "relevance", "evidence_quality", "reference_status", "facet_ids", "year_fit", "item_type",
    "title_accuracy", "authors_accuracy", "year_accuracy", "venue_accuracy",
]
COMPLIANT_TYPES = {"original_research", "review", "conference_paper", "book_chapter", "book", "preprint", "other_research"}


def candidate_key(paper: dict) -> str:
    doi = str(paper.get("doi") or "").lower().strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi)
    if doi:
        return "doi:" + doi
    title = "".join(ch.lower() for ch in str(paper.get("title") or "") if ch.isalnum())
    return "title:" + hashlib.sha256(title.encode()).hexdigest()[:20]


def dcg(gains: list[float], k: int = 10) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg(actual: list[float], ideal: list[float]) -> float:
    denominator = dcg(sorted(ideal, reverse=True))
    return 0.0 if denominator == 0 else 100 * dcg(actual) / denominator


def read_labels(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    errors = []
    labels = {}
    for row in rows:
        key = (row["task_id"], row["candidate_key"])
        if key in labels:
            errors.append(f"duplicate {key}")
        for field in FINAL_FIELDS:
            value = row.get("final_" + field, "").strip()
            if not value and field != "facet_ids":
                errors.append(f"{key}: missing final_{field}")
            if field in ALLOWED and value not in ALLOWED[field]:
                errors.append(f"{key}: invalid final_{field}={value!r}")
        try:
            definitions = json.loads(row.get("facet_definitions") or "[]")
            permitted = {str(i) for i in range(1, len(definitions) + 1)}
            if not parse_facets(row.get("final_facet_ids", "")) <= permitted:
                errors.append(f"{key}: final_facet_ids outside 1..{len(definitions)}")
        except json.JSONDecodeError:
            errors.append(f"{key}: malformed facet_definitions")
        if row.get("conflict_fields") and (not row.get("adjudicator_id") or not row.get("adjudication_rationale")):
            errors.append(f"{key}: conflict lacks adjudicator/rationale")
        if row.get("final_reference_status") == "unresolved":
            errors.append(f"{key}: unresolved reference blocks formal scoring")
        if row.get("final_evidence_quality") == "unknown":
            errors.append(f"{key}: unknown evidence quality blocks formal scoring")
        if row.get("final_year_fit") == "unknown":
            errors.append(f"{key}: unknown year fit blocks formal scoring")
        labels[key] = row
    if errors:
        raise ValueError("adjudication incomplete:\n" + "\n".join(errors[:50]) + (f"\n... {len(errors)-50} more" if len(errors) > 50 else ""))
    return labels


def score_task(task: dict, task_info: dict, labels: dict[tuple[str, str], dict[str, str]]) -> dict:
    task_id = task["task_id"]
    papers = sorted(task.get("papers") or [], key=lambda p: int(p.get("rank") or 10**9))
    top20, top10 = papers[:20], papers[:10]
    task_labels = [v for (tid, _), v in labels.items() if tid == task_id]
    missing = [candidate_key(p) for p in top20 if (task_id, candidate_key(p)) not in labels]
    if missing:
        raise ValueError(f"{task_id}: top20 candidates missing labels: {missing[:3]}")

    def label(paper: dict) -> dict[str, str]:
        return labels[(task_id, candidate_key(paper))]

    def verified(row: dict[str, str]) -> bool:
        return row["final_reference_status"] == "verified"

    actual_rel, actual_eq = [], []
    for paper in top10:
        row = label(paper); rel = int(row["final_relevance"])
        actual_rel.append(float((2 ** rel - 1) if verified(row) else 0))
        actual_eq.append(float(int(row["final_evidence_quality"]) / 3 if verified(row) and rel >= 2 and row["final_item_type"] in COMPLIANT_TYPES else 0))
    ideal_rel, ideal_eq = [], []
    for row in task_labels:
        rel = int(row["final_relevance"])
        ideal_rel.append(float((2 ** rel - 1) if verified(row) else 0))
        ideal_eq.append(float(int(row["final_evidence_quality"]) / 3 if verified(row) and rel >= 2 and row["final_item_type"] in COMPLIANT_TYPES else 0))

    eligible = [label(p) for p in top20 if verified(label(p)) and int(label(p)["final_relevance"]) >= 2]
    facets = set()
    for row in eligible:
        facets.update(v.strip() for v in row["final_facet_ids"].split(",") if v.strip())
    facet_total = len(json.loads(task_labels[0]["facet_definitions"]))
    authentic = sum(verified(label(p)) for p in top20)
    effective = sum(verified(label(p)) and int(label(p)["final_relevance"]) >= 2 and label(p)["final_item_type"] in COMPLIANT_TYPES for p in top20)
    accurate_fields = sum(label(p)["final_" + f + "_accuracy"] == "accurate" for p in top20 for f in ("title", "authors", "year", "venue"))
    rel_hits = sum(verified(label(p)) and int(label(p)["final_relevance"]) >= 2 for p in top10)
    year_rule = str(task_labels[0].get("year_rule") or "N/A")
    if year_rule == "N/A":
        time_score = None
    else:
        time_score = 0.0 if not eligible else 100 * sum(r["final_year_fit"] == "fits" for r in eligible) / len(eligible)
    return {
        "task_id": task_id, "broad_field": task_info["broad_field"], "returned": len(papers),
        "relevance_ranking": round(ndcg(actual_rel, ideal_rel), 2),
        "p_at_10": round(100 * rel_hits / 10, 2),
        "evidence_quality": round(ndcg(actual_eq, ideal_eq), 2),
        "facet_coverage": round(100 * len(facets) / facet_total, 2),
        "citation_authenticity": round(100 * authentic / min(20, len(papers)), 2) if papers else 0.0,
        "citation_verified": authentic, "citation_denominator": min(20, len(papers)),
        "valid_research_count": round(100 * effective / 20, 2), "valid_research_items": effective,
        "bibliography_accuracy": round(100 * accurate_fields / (4 * len(top20)), 2) if top20 else 0.0,
        "bibliography_accurate_fields": accurate_fields, "bibliography_fields": 4 * len(top20),
        "time_appropriateness": None if time_score is None else round(time_score, 2),
    }


def averages(rows: list[dict], dimension: str) -> dict:
    by_field = defaultdict(list)
    for row in rows:
        if row[dimension] is not None:
            by_field[row["broad_field"]].append(row[dimension])
    field_means = {field: round(sum(v) / len(v), 2) for field, v in by_field.items()}
    direct = [row[dimension] for row in rows if row[dimension] is not None]
    return {
        "field_means": field_means,
        "equal_field_mean": round(sum(field_means.values()) / len(field_means), 2) if field_means else None,
        "direct_task_mean": round(sum(direct) / len(direct), 2) if direct else None,
        "scored_tasks": len(direct), "not_applicable_tasks": len(rows) - len(direct),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--taskset", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    taskset = yaml.safe_load(args.taskset.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in taskset["tasks"]}
    labels = read_labels(args.adjudication)
    rows = [score_task(task, tasks[task["task_id"]], labels) for task in results]
    dimensions = ["relevance_ranking", "evidence_quality", "facet_coverage", "citation_authenticity", "valid_research_count", "bibliography_accuracy", "time_appropriateness"]
    payload = {"benchmark_version": taskset["version"], "weighted_total": None, "tasks": rows, "summaries": {d: averages(rows, d) for d in dimensions}}
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"scored {len(rows)} tasks across seven independent dimensions: {args.out}")


if __name__ == "__main__":
    main()
