#!/usr/bin/env python3
"""Pool multiple systems plus independent seeds into blinded v0.3 review sheets."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
from pathlib import Path

import yaml


FIELDS = [
    "task_id", "candidate_key", "prompt", "facet_definitions", "year_rule",
    "doi", "title", "abstract", "authors", "year", "venue", "lookup_url",
    "reviewer_id", "relevance", "evidence_quality", "reference_status",
    "facet_ids", "year_fit", "item_type", "title_accuracy", "authors_accuracy",
    "year_accuracy", "venue_accuracy", "evidence_url", "rationale",
]


def candidate_key(paper: dict) -> str:
    doi = str(paper.get("doi") or "").lower().strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi)
    if doi:
        return "doi:" + doi
    title = "".join(ch.lower() for ch in str(paper.get("title") or "") if ch.isalnum())
    return "title:" + hashlib.sha256(title.encode()).hexdigest()[:20]


def deep_sample(papers: list[dict], task_id: str, system: str, seed: int) -> list[dict]:
    selected = list(papers[:20])
    for page_start in (20, 40, 60, 80):
        page = papers[page_start:page_start + 20]
        if not page:
            continue
        rng = random.Random(f"{seed}:{system}:{task_id}:{page_start}")
        selected.extend(rng.sample(page, min(2, len(page))))
    return selected


def add_candidate(
    pooled: dict[tuple[str, str], dict],
    audit: dict[tuple[str, str], dict],
    *, paper: dict, task_id: str, system: str, path: Path, is_seed: bool,
    tasks: dict, facets: dict,
) -> None:
    doi = str(paper.get("doi") or "").strip()
    url = str(paper.get("url") or "").strip()
    key = (task_id, candidate_key(paper))
    row = pooled.get(key) or {
        "task_id": task_id,
        "candidate_key": key[1],
        "prompt": tasks[task_id]["prompt_zh"],
        "facet_definitions": json.dumps(facets[task_id]["facets"], ensure_ascii=False),
        "year_rule": facets[task_id].get("year_rule", "N/A"),
        **{field: "" for field in FIELDS[5:]},
    }
    incoming = {
        "doi": doi,
        "title": paper.get("title") or "",
        "abstract": paper.get("abstract") or "",
        "authors": json.dumps(paper.get("authors") or [], ensure_ascii=False),
        "year": paper.get("year") or "",
        "venue": paper.get("venue") or "",
        "lookup_url": url or ("https://doi.org/" + doi if doi else ""),
    }
    for field, value in incoming.items():
        if value and (
            not row.get(field)
            or (field == "abstract" and len(str(value)) > len(str(row.get(field) or "")))
        ):
            row[field] = value
    pooled[key] = row
    entry = audit.setdefault(key, {
        "task_id": task_id, "candidate_key": row["candidate_key"], "source_records": [],
    })
    rank = int(paper.get("rank") or 0)
    entry["source_records"].append({
        "system": system,
        "result_file": str(path),
        "original_rank": paper.get("rank"),
        "sample_scope": (
            "independent_seed" if is_seed else "top20" if rank <= 20 else "deep_page_sample"
        ),
    })


def load_results(path: Path, task_ids: set[str], *, require_complete: bool) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload if isinstance(payload, list) else [payload]
    ids = {str(row.get("task_id") or "") for row in values}
    if not ids <= task_ids or (require_complete and ids != task_ids):
        raise ValueError(f"{path}: task ids do not match the frozen taskset")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True,
                        help="one frozen result JSON per compared system")
    parser.add_argument("--seed-results", type=Path, nargs="*", default=[],
                        help="independently collected seed papers in result JSON shape")
    parser.add_argument("--taskset", type=Path, required=True)
    parser.add_argument("--facets", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261016)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"output exists: {args.out}")

    tasks = {item["id"]: item for item in yaml.safe_load(
        args.taskset.read_text(encoding="utf-8"))["tasks"]}
    facets = yaml.safe_load(args.facets.read_text(encoding="utf-8"))["tasks"]
    if set(tasks) != set(facets):
        raise ValueError("taskset and facets must contain the same task ids")

    task_ids = set(tasks)
    result_sets = [
        (path, load_results(path, task_ids, require_complete=True), False)
        for path in args.results
    ]
    result_sets += [
        (path, load_results(path, task_ids, require_complete=False), True)
        for path in args.seed_results
    ]
    pooled: dict[tuple[str, str], dict] = {}
    audit: dict[tuple[str, str], dict] = {}
    for path, results, is_seed in result_sets:
        for result in results:
            task_id = result["task_id"]
            papers = sorted(result.get("papers") or [], key=lambda p: int(p.get("rank") or 10**9))
            system = (
                "independent_seed:" + path.stem if is_seed else
                str((result.get("system") or {}).get("name") or path.stem)
            )
            selected = papers if is_seed else deep_sample(papers, task_id, system, args.seed)
            for paper in selected:
                add_candidate(
                    pooled, audit, paper=paper, task_id=task_id, system=system,
                    path=path, is_seed=is_seed, tasks=tasks, facets=facets,
                )

    rows = sorted(pooled.values(), key=lambda row: (row["task_id"], row["candidate_key"]))
    audit_rows = [audit[key] for key in sorted(audit)]
    args.out.mkdir(parents=True)
    for reviewer, offset in (("A", 1), ("B", 2)):
        ordered = list(rows)
        random.Random(args.seed + offset).shuffle(ordered)
        with (args.out / f"reviewer_{reviewer}.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(ordered)
    (args.out / "rank_audit.json").write_text(
        json.dumps(audit_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "README.md").write_text(
        "# LiteratureBench v0.3 跨平台双人盲评包\n\n"
        "reviewer_A.csv 与 reviewer_B.csv 包含同一批候选，但顺序不同；不含系统名称、来源和原始名次。\n"
        "候选池合并所有参评系统的前20条、各系统第2至第5页固定抽样，以及独立检索种子；按DOI优先去重。\n"
        "两位评审者须独立填写：relevance=0..3；evidence_quality=0..3或unknown；"
        "reference_status=verified/invalid/unresolved；facet_ids填写直接支持的要点序号；"
        "year_fit=fits/out_of_scope/unknown/N/A；四个accuracy字段填写accurate/missing/conflict；"
        "item_type填写original_research/review/conference_paper/book_chapter/book/preprint/"
        "other_research/non_research之一；并填写reviewer_id、evidence_url和rationale。完成前不得互看。\n"
        "rank_audit.json记录各平台原始名次，仅供仲裁和复算人员使用，不交给盲评者。\n",
        encoding="utf-8",
    )
    print(f"{len(rows)} pooled blinded records per reviewer: {args.out}")


if __name__ == "__main__":
    main()
