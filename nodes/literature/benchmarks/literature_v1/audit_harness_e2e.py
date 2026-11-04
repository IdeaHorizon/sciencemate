#!/usr/bin/env python3
"""Audit a real literature-node run from its saved summary and transcript."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def artifact_types(summary: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    for item in summary.get("artifacts") or []:
        if isinstance(item, dict):
            value = item.get("artifact_type") or item.get("type")
            if value:
                found.add(str(value))
        elif isinstance(item, str):
            for kind in ("literature_index", "survey_report"):
                if kind in item:
                    found.add(kind)
    return sorted(found)


def audit(run_dir: Path) -> dict[str, Any]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    events = read_events(run_dir / "transcript.jsonl")
    calls = [event for event in events if event.get("event") == "tool_call"]
    names = Counter(str(event.get("name") or "") for event in calls)
    saved_report = any(
        event.get("name") == "save_artifact"
        and (event.get("args") or {}).get("artifact_type") == "survey_report"
        for event in calls
    )
    wiring = [event for event in events if event.get("event") == "chunk_author_wiring"]
    types = artifact_types(summary)
    status = summary.get("status")
    return {
        "run_id": summary.get("run_id") or run_dir.name,
        "scope": "literature_node_e2e",
        "status": status,
        "failure_category": summary.get("failure_category"),
        "failure_subcategory": summary.get("failure_subcategory"),
        "provider_error_detail": summary.get("provider_error_detail"),
        "tool_calls": dict(sorted(names.items())),
        "artifact_types": types,
        "author_wiring_chunks": max((int(event.get("n_with_author_wiring") or 0) for event in wiring), default=0),
        "checks": {
            "searched": names["search_papers"] > 0,
            "classified": names["classify_papers"] > 0,
            "archived": names["archive_papers"] > 0,
            "survey_report_saved": saved_report,
            "index_artifact_present": "literature_index" in types,
            "report_artifact_present": "survey_report" in types,
            "run_completed": status == "success",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    payload = [audit(run_dir) for run_dir in args.run_dirs]
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
