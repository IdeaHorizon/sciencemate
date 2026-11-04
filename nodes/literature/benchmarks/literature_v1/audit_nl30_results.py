#!/usr/bin/env python3
"""Summarize observable NL30 module results; do not substitute for blind ratings."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median


def identity(paper: dict) -> str:
    doi = str(paper.get("doi") or "").lower().strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        doi = doi.removeprefix(prefix)
    if doi:
        return f"doi:{doi}"
    title = "".join(ch.lower() for ch in str(paper.get("title") or "") if ch.isalnum())
    return f"title:{title}" if title else ""


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(pct * len(ordered)) - 1]


def summarize(result: dict) -> dict:
    papers = sorted(result.get("papers") or [], key=lambda p: int(p.get("rank") or 10**9))[:100]
    seen: set[str] = set()
    duplicates = 0
    for paper in papers:
        key = identity(paper)
        if key and key in seen:
            duplicates += 1
        seen.add(key)
    diagnostics = result.get("retrieval_diagnostics") or {}
    attempts = result.get("attempts") or []
    elapsed = sum(float(a["elapsed_seconds"]) for a in attempts if isinstance(a.get("elapsed_seconds"), (int, float)))
    return {
        "task_id": str(result.get("task_id") or ""),
        "status": str(result.get("status") or "unknown"),
        "returned": len(papers),
        "first_page": min(20, len(papers)),
        "deeper_pages": max(0, len(papers) - 20),
        "duplicates": duplicates,
        "doi_present": sum(bool(p.get("doi")) for p in papers),
        "link_present": sum(bool(p.get("doi") or p.get("url")) for p in papers),
        "abstract_present": sum(bool(p.get("abstract")) for p in papers),
        "metadata_complete": sum(all(bool(p.get(f)) for f in ("title", "authors", "year", "venue")) for p in papers),
        "elapsed_seconds": round(elapsed, 2),
        "attempts": len(attempts),
        "search_mode": diagnostics.get("search_mode"),
        "from_cache": diagnostics.get("from_cache"),
        "source_counts": dict(Counter(str(p.get("source") or "unknown") for p in papers)),
        "warnings": len(diagnostics.get("warnings") or []),
    }


def render(rows: list[dict], source: Path) -> str:
    statuses = Counter(row["status"] for row in rows)
    times = [row["elapsed_seconds"] for row in rows if row["attempts"]]
    total = sum(row["returned"] for row in rows)
    modes = Counter(str(row["search_mode"] or "unknown") for row in rows)
    lines = [
        "# ScienceMate 学术检索 NL30 自动审计",
        "",
        f"结果文件：`{source}`。这是接口返回的可观测指标，**不是相关性、证据质量或引文真实性评分**。",
        "",
        f"运行题数：{len(rows)}；状态：{dict(statuses)}；返回位置：{total}（上限每题 100）。",
        f"请求耗时中位数：{median(times):.1f} 秒；P90：{percentile(times, .9):.1f} 秒。" if times else "无有效耗时记录。",
        f"检索模式：{dict(modes)}。",
        "",
        "| 题号 | 状态 | 返回 | 第 1 页 | 后续页 | 重复 | DOI | 链接字段 | 摘要 | 书目完整 | 耗时(s) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row[k]) for k in (
            "task_id", "status", "returned", "first_page", "deeper_pages", "duplicates",
            "doi_present", "link_present", "abstract_present", "metadata_complete", "elapsed_seconds",
        )) + " |")
    lines += [
        "",
        "说明：DOI/链接字段存在不代表链接可达或引用正确；摘要非空不代表摘要质量。",
        "人工相关性、证据质量和问题要点覆盖须按评分标准双人盲评后另报。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    results = payload if isinstance(payload, list) else [payload]
    rows = [summarize(result) for result in results]
    report = render(rows, args.results)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()
