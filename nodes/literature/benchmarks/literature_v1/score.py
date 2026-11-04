#!/usr/bin/env python3
"""机械计算共同检索指标；相关性只来自隐藏 gold，不调用模型或网络。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


def _title(value: object) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def key(paper: dict[str, Any]) -> str:
    doi = str(paper.get("doi") or "").strip().lower()
    if doi:
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
            doi = doi.removeprefix(prefix)
        return f"doi:{doi}"
    title = _title(paper.get("title"))
    return f"title:{title}" if title else ""


def dcg(relevance: list[int]) -> float:
    return sum((2**value - 1) / math.log2(index + 2) for index, value in enumerate(relevance))


def average(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def score(result: dict[str, Any], gold_task: dict[str, Any]) -> dict[str, Any]:
    gold = {key(p): int(p["relevance"]) for p in gold_task["papers"] if key(p)}
    papers = sorted(result.get("papers") or [], key=lambda p: int(p.get("rank") or 10**9))
    # A system may omit DOI yet return the same exact title. Match that title only
    # when it identifies one gold record; never use a title to excuse a wrong DOI.
    title_owners: dict[str, set[str]] = {}
    for reference in gold_task["papers"]:
        title = _title(reference.get("title"))
        if title:
            title_owners.setdefault(title, set()).add(key(reference))
    unique_titles = {title: next(iter(owners)) for title, owners in title_owners.items() if len(owners) == 1}
    raw_keys = [key(paper) for paper in papers]
    keys = [unique_titles.get(item.removeprefix("title:"), item) if item.startswith("title:") else item
            for item in raw_keys]
    # 同一 DOI/题名多次出现不应凭重复占据多个排序位置；只让首次出现获得相关性。
    seen: set[str] = set()
    relevance = []
    for item in keys:
        relevance.append(gold.get(item, 0) if item and item not in seen else 0)
        if item:
            seen.add(item)
    ideal = sorted(gold.values(), reverse=True)
    top10 = relevance[:10]
    ideal10 = ideal[:10]
    core = {item for item, value in gold.items() if value == 3}
    nonempty = [item for item in keys if item]
    unjudged10 = any(item not in gold for item in keys[:10])
    unjudged50 = any(item not in gold for item in keys[:50])
    quality_scored = result.get("status") == "success"
    return {
        "task_id": result.get("task_id"),
        "system": (result.get("system") or {}).get("name", "unknown"),
        "status": result.get("status"),
        "ndcg_at_10": dcg(top10) / dcg(ideal10) if quality_scored and not unjudged10 and dcg(ideal10) else None,
        "precision_at_10": sum(value >= 2 for value in top10) / 10 if quality_scored and not unjudged10 else None,
        "recall_at_50": len(set(keys[:50]) & core) / len(core) if quality_scored and core and not unjudged50 else None,
        "identifier_presence": sum(bool(p.get("doi") or p.get("url")) for p in papers) / len(papers) if papers else None,
        "judged_at_10": sum(item in gold for item in keys[:10]),
        "judged_at_50": sum(item in gold for item in keys[:50]),
        "metadata_completeness": sum(all(bool(p.get(field)) for field in ("title", "authors", "year", "venue")) for p in papers) / len(papers) if papers else None,
        "duplicate_rate": (len(nonempty) - len(set(nonempty))) / len(nonempty) if nonempty else None,
        "unmatched": [item for item in keys if item and item not in gold],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    gold = json.loads(args.gold.read_text(encoding="utf-8"))["tasks"]
    rows: list[dict[str, Any]] = []
    for path in args.results:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for result in payload if isinstance(payload, list) else [payload]:
            task_id = str(result.get("task_id") or "")
            if task_id not in gold:
                raise ValueError(f"{path}: task {task_id!r} 不在 gold 中，不能静默跳过")
            row = score(result, gold[task_id])
            elapsed = [attempt.get("elapsed_seconds") for attempt in result.get("attempts", []) if isinstance(attempt.get("elapsed_seconds"), (float, int))]
            row["latency_seconds"] = sum(elapsed) if elapsed else None
            rows.append(row)
    metrics = ["ndcg_at_10", "precision_at_10", "recall_at_50", "identifier_presence", "metadata_completeness", "duplicate_rate"]
    systems = {}
    for name in sorted({str(row["system"]) for row in rows}):
        subset = [row for row in rows if row["system"] == name]
        systems[name] = {metric: average([row[metric] for row in subset]) for metric in metrics}
        latency = [row["latency_seconds"] for row in subset if row["latency_seconds"] is not None]
        systems[name]["latency_p50_seconds"] = median(latency) if latency else None
        systems[name]["failure_count"] = sum(row["status"] not in {'success', 'not_offered'} for row in subset)
        systems[name]["scored_counts"] = {metric: sum(row[metric] is not None for row in subset) for metric in metrics}
    output = {"summary": {"tasks_scored": len(rows), "by_system": systems}, "rows": rows}
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
