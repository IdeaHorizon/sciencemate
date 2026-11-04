#!/usr/bin/env python3
"""Combine a first run with one protocol-allowed retry per failed task."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def combine(first: list[dict[str, Any]], retry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    original = {row["task_id"]: row for row in first}
    if len(original) != len(first):
        raise ValueError("first run contains duplicate task_id")
    seen: set[str] = set()
    for row in retry:
        task_id = row["task_id"]
        if task_id in seen or task_id not in original:
            raise ValueError(f"unknown or duplicate retry task: {task_id}")
        seen.add(task_id)
        base = original[task_id]
        for field in ("system", "prompt", "request_settings"):
            if base.get(field) != row.get(field):
                raise ValueError(f"{task_id}: changed {field} between attempts")
        if base.get("status") == "success":
            raise ValueError(f"{task_id}: successful task must not be retried")
        attempts = (base.get("attempts") or []) + (row.get("attempts") or [])
        if len(attempts) != 2:
            raise ValueError(f"{task_id}: expected exactly two attempts")
        keep_first = bool(base.get("papers")) and not row.get("papers") and row.get("status") != "success"
        selected = dict(base if keep_first else row)
        selected["attempts"] = attempts
        selected["first_attempt_status"] = base.get("status")
        selected["selected_attempt"] = 1 if keep_first else 2
        original[task_id] = selected
    return [original[row["task_id"]] for row in first]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--retry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"output exists: {args.out}")
    first = json.loads(args.first.read_text(encoding="utf-8"))
    retry = json.loads(args.retry.read_text(encoding="utf-8"))
    combined = combine(first, retry)
    args.out.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(combined)} tasks, {len(retry)} retries: {args.out}")


if __name__ == "__main__":
    main()
