#!/usr/bin/env python3
"""Add every system-returned paper to a candidate pool before blind review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def doi_key(value: object) -> str:
    doi = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        doi = doi.removeprefix(prefix)
    return doi.rstrip("/")


def title_key(value: object) -> str:
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def paper_key(paper: dict[str, Any]) -> str:
    doi = doi_key(paper.get("doi"))
    if doi:
        return "doi:" + doi
    title = title_key(paper.get("title"))
    return "title:" + title if title else ""


def load_results(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else [payload]
    if not all(isinstance(row, dict) and row.get("task_id") for row in rows):
        raise ValueError(f"{path}: 结果缺少 task_id")
    return rows


def merge(pool: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    if pool.get("status") != "candidate_pool_not_gold":
        raise ValueError("输入不是待标注候选池")
    by_task = {topic["id"]: topic for topic in pool["topics"]}
    if len(by_task) != len(pool["topics"]):
        raise ValueError("候选池存在重复 task id")
    keys = {task_id: {paper_key(item): item for item in topic["candidates"] if paper_key(item)}
            for task_id, topic in by_task.items()}
    titles: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for task_id, topic in by_task.items():
        titles[task_id] = {}
        for item in topic["candidates"]:
            title = title_key(item.get("title"))
            if title:
                titles[task_id].setdefault(title, []).append(item)

    for result in results:
        task_id = str(result["task_id"])
        if task_id not in by_task:
            raise ValueError(f"结果包含未知任务 {task_id}")
        system = str((result.get("system") or {}).get("name") or "unknown")
        for paper in (result.get("papers") or [])[:50]:
            key = paper_key(paper)
            if not key:
                continue
            candidate = keys[task_id].get(key)
            if candidate is None and not doi_key(paper.get("doi")):
                # DOI-less result may match one known titled record, but an
                # ambiguous title must stay separate for human adjudication.
                owners = titles[task_id].get(title_key(paper.get("title")), [])
                if len(owners) == 1:
                    candidate = owners[0]
            if candidate is None:
                candidate = {
                    "doi": doi_key(paper.get("doi")),
                    "title": paper.get("title") or "",
                    "year": paper.get("year"),
                    "venue": paper.get("venue") or "",
                    "candidate_sources": [],
                    "source_records": [],
                    "relevance": None,
                    "adjudication_note": "",
                }
                by_task[task_id]["candidates"].append(candidate)
                keys[task_id][key] = candidate
                title = title_key(candidate.get("title"))
                if title:
                    titles[task_id].setdefault(title, []).append(candidate)
            source = "system_result:" + system
            candidate["candidate_sources"] = sorted(set(candidate.get("candidate_sources") or []) | {source})
            candidate.setdefault("source_records", []).append({
                "system": system, "task_id": task_id, "rank": paper.get("rank"),
                "title": paper.get("title"), "doi": paper.get("doi"), "url": paper.get("url"),
            })
    for topic in pool["topics"]:
        topic["candidates"].sort(key=lambda item: paper_key(item))
    return pool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"输出已存在: {args.out}")
    pool = json.loads(args.candidates.read_text(encoding="utf-8"))
    rows = [row for path in args.results for row in load_results(path)]
    merged = merge(pool, rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(merged['topics'])} tasks, {sum(len(t['candidates']) for t in merged['topics'])} candidates")


if __name__ == "__main__":
    main()
