#!/usr/bin/env python3
"""Audit the first N DOI/title pairs per task against Crossref metadata.

Crossref 404 or a low title similarity is a manual-review flag, not proof of
a fabricated citation. This diagnostic is separate from relevance scoring.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def normalize_title(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def normalize_doi(value: object) -> str:
    doi = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        doi = doi.removeprefix(prefix)
    return doi.rstrip("/")


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def crossref_years(item: dict[str, Any]) -> list[int]:
    years: set[int] = set()
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            years.add(parts[0][0])
    return sorted(years)


def crossref_authors(item: dict[str, Any]) -> list[str]:
    values = []
    for author in item.get("author") or []:
        name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
        if name:
            values.append(name)
    return values


def author_similarity(returned: object, registered: object) -> float | None:
    left = [normalize_name(v) for v in returned or [] if normalize_name(v)]
    right = [normalize_name(v) for v in registered or [] if normalize_name(v)]
    if not left or not right:
        return None

    def surname(name: str) -> str:
        return name.split()[-1] if name.split() else ""

    left_names, right_names = set(left), set(right)
    exact = len(left_names & right_names) / max(1, len(left_names))
    left_surnames = {surname(v) for v in left}
    right_surnames = {surname(v) for v in right}
    surname_overlap = len(left_surnames & right_surnames) / max(1, len(left_surnames))
    first_author = float(surname(left[0]) == surname(right[0]))
    return round(0.5 * exact + 0.3 * surname_overlap + 0.2 * first_author, 3)


def type_check(returned: object, registered: object) -> str:
    compatible = {
        "期刊文章": {"journal-article"},
        "会议文章": {"proceedings-article", "proceedings"},
        "书籍": {"book", "book-chapter", "edited-book", "reference-book"},
        "预印本": {"posted-content"},
    }
    left, right = str(returned or ""), str(registered or "")
    if not left or not right:
        return "unresolved"
    return "likely_match" if right in compatible.get(left, set()) else "manual_review"


def crossref_record(doi: str, timeout: float) -> dict[str, Any]:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": "LiteratureBench/0.3 DOI-audit"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                item = json.load(response)["message"]
            return {
                "status": "registered_crossref",
                "registered_title": (item.get("title") or [""])[0],
                "registered_doi": item.get("DOI") or "",
                "registered_type": item.get("type") or "",
                "registered_authors": crossref_authors(item),
                "registered_years": crossref_years(item),
                "registered_venue": (item.get("container-title") or [""])[0],
            }
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"status": "not_found_crossref" if exc.code == 404 else "http_error", "http_status": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            return {"status": "transport_error", "error": type(exc).__name__ + ": " + str(exc)[:160]}
    raise AssertionError("unreachable")


def audit(results: list[dict[str, Any]], *, start_rank: int, top: int, timeout: float, delay: float, workers: int, out: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}
    for task in results:
        papers = [
            paper for paper in sorted(task.get("papers") or [], key=lambda item: item.get("rank") or 10**9)
            if start_rank <= int(paper.get("rank") or 0) <= top
        ]
        pending = []
        for paper in papers:
            doi = normalize_doi(paper.get("doi"))
            if doi and doi not in cache:
                pending.append(doi)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = list(pool.map(lambda doi: (doi, crossref_record(doi, timeout)), dict.fromkeys(pending)))
        cache.update(fetched)
        if delay:
            time.sleep(delay)
        for paper in papers:
            doi = normalize_doi(paper.get("doi"))
            row: dict[str, Any] = {
                "task_id": task["task_id"], "rank": paper.get("rank"), "doi": doi,
                "returned_title": paper.get("title") or "",
                "returned_authors": paper.get("authors") or [],
                "returned_year": paper.get("year"),
                "returned_venue": paper.get("venue") or "",
                "returned_type": paper.get("publication_category") or "",
            }
            if not doi:
                row["status"] = "missing_doi"
            else:
                if doi not in cache:
                    cache[doi] = crossref_record(doi, timeout)
                metadata = cache[doi]
                row.update(metadata)
                if metadata["status"] == "registered_crossref":
                    title = str(metadata.get("registered_title") or "")
                    similarity = SequenceMatcher(None, normalize_title(row["returned_title"]), normalize_title(title)).ratio()
                    row["title_similarity"] = round(similarity, 3)
                    row["title_check"] = "likely_match" if similarity >= 0.90 else "manual_review"
                    row["author_similarity"] = author_similarity(
                        row["returned_authors"], metadata.get("registered_authors"))
                    row["authors_check"] = (
                        "unresolved" if row["author_similarity"] is None else
                        "likely_match" if row["author_similarity"] >= 0.75 else "manual_review"
                    )
                    years = metadata.get("registered_years") or []
                    row["year_check"] = (
                        "unresolved" if not row["returned_year"] or not years else
                        "likely_match" if int(row["returned_year"]) in years else "manual_review"
                    )
                    venue_similarity = SequenceMatcher(
                        None, normalize_title(row["returned_venue"]),
                        normalize_title(metadata.get("registered_venue")),
                    ).ratio()
                    row["venue_similarity"] = round(venue_similarity, 3)
                    row["venue_check"] = (
                        "unresolved" if not row["returned_venue"] or not metadata.get("registered_venue") else
                        "likely_match" if venue_similarity >= 0.80 else "manual_review"
                    )
                    row["type_check"] = type_check(row["returned_type"], metadata.get("registered_type"))
                    checks = [row[f] for f in ("title_check", "authors_check", "year_check", "venue_check", "type_check")]
                    row["status"] = "likely_metadata_match" if all(v == "likely_match" for v in checks) else "manual_metadata_review"
            records.append(row)
            out.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(task["task_id"], len(records), "records audited", flush=True)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--start-rank", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"output exists: {args.out}")
    if not 1 <= args.start_rank <= args.top <= 100 or args.timeout <= 0 or args.delay < 0 or not 1 <= args.workers <= 12:
        raise ValueError("invalid top, timeout, or delay")
    rows = json.loads(args.results.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    audit(rows, start_rank=args.start_rank, top=args.top, timeout=args.timeout, delay=args.delay, workers=args.workers, out=args.out)


if __name__ == "__main__":
    main()
