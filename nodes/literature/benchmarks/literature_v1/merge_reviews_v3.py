#!/usr/bin/env python3
"""Validate two blind review sheets and create an adjudication sheet."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


LABELS = [
    "relevance", "evidence_quality", "reference_status", "facet_ids", "year_fit",
    "item_type", "title_accuracy", "authors_accuracy", "year_accuracy", "venue_accuracy",
]
ALLOWED = {
    "relevance": {"0", "1", "2", "3"},
    "evidence_quality": {"0", "1", "2", "3", "unknown"},
    "reference_status": {"verified", "invalid", "unresolved"},
    "year_fit": {"fits", "out_of_scope", "unknown", "N/A"},
    "item_type": {"original_research", "review", "conference_paper", "book_chapter", "book", "preprint", "other_research", "non_research"},
    "title_accuracy": {"accurate", "missing", "conflict"},
    "authors_accuracy": {"accurate", "missing", "conflict"},
    "year_accuracy": {"accurate", "missing", "conflict"},
    "venue_accuracy": {"accurate", "missing", "conflict"},
}


def read_sheet(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    keyed = {(r["task_id"], r["candidate_key"]): r for r in rows}
    if len(keyed) != len(rows):
        raise ValueError(f"duplicate task/candidate in {path}")
    return keyed


def parse_facets(value: str) -> set[str]:
    return {v.strip() for v in value.replace("，", ",").split(",") if v.strip()}


def validate_row(row: dict[str, str], path: Path) -> list[str]:
    errors = []
    key = f"{row.get('task_id')}/{row.get('candidate_key')}"
    if not row.get("reviewer_id", "").strip():
        errors.append(f"{path}: {key}: missing reviewer_id")
    for field, allowed in ALLOWED.items():
        value = row.get(field, "").strip()
        if value not in allowed:
            errors.append(f"{path}: {key}: invalid {field}={value!r}")
    try:
        definitions = json.loads(row.get("facet_definitions") or "[]")
        allowed_facets = {str(i) for i in range(1, len(definitions) + 1)}
        if not parse_facets(row.get("facet_ids", "")) <= allowed_facets:
            errors.append(f"{path}: {key}: facet_ids outside 1..{len(definitions)}")
    except json.JSONDecodeError:
        errors.append(f"{path}: {key}: malformed facet_definitions")
    if not row.get("evidence_url", "").strip() or not row.get("rationale", "").strip():
        errors.append(f"{path}: {key}: evidence_url and rationale are required")
    return errors


def merge(a_path: Path, b_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    a, b = read_sheet(a_path), read_sheet(b_path)
    if set(a) != set(b):
        raise ValueError("review sheets do not contain identical candidate sets")
    errors = [e for path, sheet in ((a_path, a), (b_path, b)) for row in sheet.values() for e in validate_row(row, path)]
    ids_a = {r["reviewer_id"].strip() for r in a.values() if r.get("reviewer_id", "").strip()}
    ids_b = {r["reviewer_id"].strip() for r in b.values() if r.get("reviewer_id", "").strip()}
    if ids_a & ids_b:
        errors.append("reviewer A and B identities overlap")
    rows = []
    for key in sorted(a):
        left, right = a[key], b[key]
        base = {field: left.get(field, "") for field in (
            "task_id", "candidate_key", "title", "doi", "prompt", "facet_definitions", "year_rule"
        )}
        row = {**base, "reviewer_a_id": left.get("reviewer_id", ""), "reviewer_b_id": right.get("reviewer_id", "")}
        conflicts = []
        for field in LABELS:
            av, bv = left.get(field, "").strip(), right.get(field, "").strip()
            if field == "facet_ids":
                av, bv = ",".join(sorted(parse_facets(av))), ",".join(sorted(parse_facets(bv)))
            row[f"a_{field}"] = av
            row[f"b_{field}"] = bv
            row[f"final_{field}"] = av if av == bv else ""
            if av != bv:
                conflicts.append(field)
        row.update({
            "a_evidence_url": left.get("evidence_url", ""), "b_evidence_url": right.get("evidence_url", ""),
            "a_rationale": left.get("rationale", ""), "b_rationale": right.get("rationale", ""),
            "conflict_fields": ",".join(conflicts), "adjudicator_id": "", "adjudication_rationale": "",
        })
        rows.append(row)
    return rows, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer-a", type=Path, required=True)
    parser.add_argument("--reviewer-b", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"output exists: {args.out}")
    rows, errors = merge(args.reviewer_a, args.reviewer_b)
    if errors:
        raise ValueError("review validation failed:\n" + "\n".join(errors[:50]) + (f"\n... {len(errors)-50} more" if len(errors) > 50 else ""))
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    conflicts = sum(bool(r["conflict_fields"]) for r in rows)
    print(f"{len(rows)} rows; {conflicts} require adjudication: {args.out}")


if __name__ == "__main__":
    main()
