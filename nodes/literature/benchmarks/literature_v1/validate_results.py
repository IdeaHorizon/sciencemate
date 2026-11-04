#!/usr/bin/env python3
"""Validate normalized benchmark submissions; do not judge paper relevance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate(payload: object, task_ids: set[str], limit: int = 50, version: str = "0.3.0") -> list[str]:
    rows = payload if isinstance(payload, list) else [payload]
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    if not rows:
        return ["empty result file"]
    for number, row in enumerate(rows, 1):
        where = f"row {number}"
        if not isinstance(row, dict):
            errors.append(f"{where}: expected object")
            continue
        system = row.get("system") or {}
        system = system if isinstance(system, dict) else {}
        name, mode = str(system.get("name") or ""), str(system.get("mode") or "")
        task = str(row.get("task_id") or "")
        if row.get("benchmark_version") != version:
            errors.append(f"{where}: wrong benchmark_version")
        if task not in task_ids:
            errors.append(f"{where}: unknown task_id {task!r}")
        if not name or not mode:
            errors.append(f"{where}: system.name and system.mode are required")
        identity = (name, mode, task)
        if identity in seen:
            errors.append(f"{where}: duplicate system/mode/task; put retries in attempts")
        seen.add(identity)
        status = row.get("status")
        if status not in {"success", "partial", "timeout", "rate_limited", "error", "not_offered"}:
            errors.append(f"{where}: invalid status {status!r}")
        attempts = row.get("attempts")
        if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
            errors.append(f"{where}: expected 1 or 2 attempts")
        else:
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    errors.append(f"{where}: malformed attempt")
                    continue
                elapsed = attempt.get("elapsed_seconds")
                if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
                    errors.append(f"{where}: missing nonnegative elapsed_seconds")
                if not attempt.get("outcome"):
                    errors.append(f"{where}: missing attempt outcome")
            if status in {"success", "partial"}:
                selected_index = row.get("selected_attempt", len(attempts))
                if not isinstance(selected_index, int) or not 1 <= selected_index <= len(attempts):
                    errors.append(f"{where}: invalid selected_attempt")
                elif not isinstance(attempts[selected_index - 1], dict) or not attempts[selected_index - 1].get("raw_evidence_path"):
                    errors.append(f"{where}: selected successful/partial attempt needs raw_evidence_path")

        papers = row.get("papers")
        if not isinstance(papers, list):
            errors.append(f"{where}: papers must be an array, even on failure")
            continue
        if len(papers) > limit:
            errors.append(f"{where}: exceeds paper limit {limit}")
        if status == "success" and not papers:
            errors.append(f"{where}: empty results cannot be success")
        if status in {"timeout", "rate_limited", "error", "not_offered"} and papers:
            errors.append(f"{where}: papers with failure status; use partial")
        for rank, paper in enumerate(papers, 1):
            if not isinstance(paper, dict):
                errors.append(f"{where}: malformed paper at rank {rank}")
            elif paper.get("rank") != rank or not str(paper.get("title") or "").strip():
                errors.append(f"{where}: paper {rank} needs sequential rank and title")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--taskset", type=Path, default=Path(__file__).parent / "tasks/esi22_topics_v1.yaml")
    args = parser.parse_args()
    import yaml
    taskset = yaml.safe_load(args.taskset.read_text(encoding="utf-8"))
    tasks = {task["id"] for task in taskset["tasks"]}
    version = str(taskset["version"])
    limit = int(taskset.get("result_limit", 50))
    errors = []
    for path in args.results:
        payload = json.loads(path.read_text(encoding="utf-8"))
        errors.extend(f"{path}: {error}" for error in validate(payload, tasks, limit=limit, version=version))
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"validated {len(args.results)} result file(s)")


if __name__ == "__main__":
    main()
