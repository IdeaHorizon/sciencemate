#!/usr/bin/env python3
"""Assemble the 2026-10-15 exploratory NL30 run without losing failed attempts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from combine_retries import combine
from validate_results import validate


def read_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a result array")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--retry-one", type=Path, required=True)
    parser.add_argument("--tail", type=Path, required=True)
    parser.add_argument("--taskset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    pilot = read_rows(args.pilot)
    first = read_rows(args.first)
    retry_one = read_rows(args.retry_one)
    tail = read_rows(args.tail)
    retry_ids = {"NL11", "NL12"}
    retry = [*retry_one, *(row for row in tail if row["task_id"] in retry_ids)]
    original = [*pilot, *first, *(row for row in tail if row["task_id"] not in retry_ids)]
    rows = sorted(combine(original, retry), key=lambda row: row["task_id"])

    taskset = yaml.safe_load(args.taskset.read_text(encoding="utf-8"))
    ids = {task["id"] for task in taskset["tasks"]}
    if len(rows) != len(ids) or {row["task_id"] for row in rows} != ids:
        raise ValueError("results do not cover the complete NL30 taskset exactly once")
    errors = validate(rows, ids, limit=int(taskset["result_limit"]), version=str(taskset["version"]))
    if errors:
        raise ValueError("invalid assembled results:\n" + "\n".join(errors))
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"assembled {len(rows)} tasks, {len(retry)} retries, "
          f"{sum(len(row['papers']) for row in rows)} paper positions: {args.out}")


if __name__ == "__main__":
    main()
