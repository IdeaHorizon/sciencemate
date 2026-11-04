#!/usr/bin/env python3
"""Audit ranks 21-100 without claiming human relevance judgements."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path


NON_RESEARCH = re.compile(
    r"\b(editorial(?: board)?|corrigendum|erratum|correction|retraction(?: notice)?|"
    r"issue information|table of contents|associate registry|publisher'?s note|"
    r"editor'?s note|front matter|back matter|inside cover)\b", re.I
)
DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def identity(paper: dict) -> str:
    doi = str(paper.get("doi") or "").lower().strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi)
    if doi:
        return "doi:" + doi.rstrip("/")
    title = "".join(ch.casefold() for ch in str(paper.get("title") or "") if ch.isalnum())
    return "title:" + title


def audit_task(task: dict, today: date) -> dict:
    papers = sorted(task.get("papers") or [], key=lambda p: int(p.get("rank") or 10**9))
    deep = [p for p in papers if 21 <= int(p.get("rank") or 0) <= 100]
    all_seen: set[str] = set()
    duplicate_ranks = []
    for paper in papers:
        key = identity(paper)
        if key in all_seen:
            duplicate_ranks.append(paper.get("rank"))
        all_seen.add(key)
    issues = []
    fields = ("title", "authors", "year", "venue")
    for paper in deep:
        row_issues = []
        doi = str(paper.get("doi") or "").strip()
        url = str(paper.get("url") or "").strip()
        if doi and not DOI.match(re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)):
            row_issues.append("malformed_doi")
        if not doi and not url:
            row_issues.append("missing_doi_and_url")
        for field in fields:
            if not paper.get(field):
                row_issues.append("missing_" + field)
        if not paper.get("abstract"):
            row_issues.append("missing_abstract")
        if NON_RESEARCH.search(str(paper.get("title") or "")):
            row_issues.append("non_research_title_pattern")
        year = paper.get("year")
        if isinstance(year, int) and year > today.year + 1:
            row_issues.append("implausible_future_year")
        elif isinstance(year, int) and year > today.year:
            row_issues.append("future_dated_online_record")
        if row_issues:
            issues.append({"rank": paper.get("rank"), "title": paper.get("title"), "issues": row_issues})
    counts = Counter(issue for row in issues for issue in row["issues"])
    return {
        "task_id": task["task_id"], "deep_count": len(deep),
        "duplicate_ranks_all_100": duplicate_ranks,
        "issue_counts": dict(sorted(counts.items())), "flagged_items": issues,
    }


def render(rows: list[dict], source: Path) -> str:
    totals = Counter()
    for row in rows:
        totals.update(row["issue_counts"])
    lines = [
        "# LiteratureBench v0.3 第 21–100 位自动审计", "",
        f"结果文件：`{source}`。规则检查只发现待复核线索，不是相关性或学术价值判定。", "",
        f"深层结果共 {sum(r['deep_count'] for r in rows)} 条；跨页重复 "
        f"{sum(len(r['duplicate_ranks_all_100']) for r in rows)} 条。", "",
        "## 汇总", "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in sorted(totals.items()))
    lines += ["", "## 分题", "", "| 题号 | 第21–100位 | 重复 | 缺摘要 | 书目字段缺失 | 非研究题名线索 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        c = row["issue_counts"]
        bib = sum(c.get("missing_" + f, 0) for f in ("title", "authors", "year", "venue"))
        lines.append(f"| {row['task_id']} | {row['deep_count']} | {len(row['duplicate_ranks_all_100'])} | {c.get('missing_abstract', 0)} | {bib} | {c.get('non_research_title_pattern', 0)} |")
    lines += ["", "具体条目和触发规则见配套 JSON；未来一年预出版记录单独标记，不自动判错。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    rows = [audit_task(task, date.today()) for task in results]
    args.out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.out_md.write_text(render(rows, args.results), encoding="utf-8")


if __name__ == "__main__":
    main()
