"""Optional, read-only institutional literature connector.

The SQLite file is a stand-in for a university-owned catalogue.  It is never
written by ScienceMate; a real installation can replace this adapter with the
library's search API without moving the platform's own PostgreSQL database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


def _configuration() -> tuple[Path, str, str] | None:
    raw_path = os.getenv("SCHOOL_LIBRARY_DB_PATH", "").strip()
    institution = os.getenv("SCHOOL_LIBRARY_INSTITUTION_ID", "").strip()
    if not raw_path or not institution:
        return None
    return (
        Path(raw_path).expanduser().resolve(),
        institution,
        os.getenv("SCHOOL_LIBRARY_NAME", "模拟学校馆藏").strip() or "模拟学校馆藏",
    )


def _connect(path: Path) -> sqlite3.Connection:
    # mode=ro is a database-level guarantee, not merely a convention.
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _authorized(institution_id: str) -> tuple[Path, str] | None:
    config = _configuration()
    if config is None or institution_id != config[1]:
        return None
    return config[0], config[2]


def search(query: str, *, institution_id: str, limit: int = 200) -> list[dict]:
    """Return catalogue metadata only for a user in the configured school."""
    allowed = _authorized(institution_id)
    if allowed is None:
        return []
    path, label = allowed
    terms = re.findall(r"[\w\u4e00-\u9fff]+", query.casefold())[:12]
    if not terms:
        return []
    clauses = [
        "lower(title || ' ' || coalesce(abstract, '') || ' ' || coalesce(keywords, '')) LIKE ?"
        for _ in terms
    ]
    parameters = [f"%{term}%" for term in terms]
    sql = (
        "SELECT id, doi, title, authors_json, abstract, venue, year, published_at, "
        "url, CASE WHEN fulltext_text IS NOT NULL AND fulltext_text <> '' "
        "THEN 1 ELSE 0 END AS has_fulltext "
        "FROM school_papers WHERE "
        + " AND ".join(clauses)
        + " ORDER BY published_at DESC, id LIMIT ?"
    )
    with closing(_connect(path)) as conn:
        rows = conn.execute(sql, (*parameters, max(1, min(int(limit), 200)))).fetchall()
    return [
        {
            **dict(row),
            "authors": json.loads(row["authors_json"] or "[]"),
            "library_name": label,
            "has_fulltext": bool(row["has_fulltext"]),
        }
        for row in rows
    ]


def recent_records(*, institution_id: str, since: str) -> list[dict]:
    """Read only public metadata for a scheduled institution feed refresh."""
    allowed = _authorized(institution_id)
    if allowed is None:
        return []
    path, label = allowed
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            "SELECT id, doi, title, authors_json, abstract, venue, year, "
            "published_at, url, CASE WHEN fulltext_text IS NOT NULL "
            "AND fulltext_text <> '' THEN 1 ELSE 0 END AS has_fulltext "
            "FROM school_papers WHERE published_at >= ? "
            "ORDER BY published_at DESC, id",
            (since,),
        ).fetchall()
    return [
        {
            **dict(row),
            "authors": json.loads(row["authors_json"] or "[]"),
            "library_name": label,
            "has_fulltext": bool(row["has_fulltext"]),
        }
        for row in rows
    ]


def fulltext(record_id: str, *, institution_id: str) -> str | None:
    allowed = _authorized(institution_id)
    if allowed is None:
        return None
    path, _ = allowed
    with closing(_connect(path)) as conn:
        row = conn.execute(
            "SELECT fulltext_text FROM school_papers WHERE id=?", (record_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _doi(value: object) -> str:
    return re.sub(
        r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.I
    ).casefold()


def _identity(paper: dict) -> str:
    doi = _doi(paper.get("doi"))
    return (
        "doi:" + doi
        if doi
        else "title:" + " ".join(str(paper.get("title") or "").casefold().split())
    )


def _school_paper(row: dict) -> dict:
    doi = _doi(row.get("doi"))
    title = str(row["title"])
    year = row.get("year")
    url = str(row.get("url") or (f"https://doi.org/{doi}" if doi else "")) or None
    label = str(row["library_name"])
    return {
        "title": title,
        "title_zh": None,
        "abstract_zh": None,
        "authors": list(row["authors"]),
        "year": year,
        "pub_date": row.get("published_at"),
        "venue": row.get("venue"),
        "doi": doi or None,
        "url": url,
        "abstract": row.get("abstract") or None,
        "ai_summary": None,
        "source": label,
        "citations": 0,
        "score": 0.0,
        "score_breakdown": {},
        "intent_coverage": 0.0,
        "ranking_score": 0.0,
        "cas_quartile": None,
        "cas_top": False,
        "impact_factor": None,
        "impact_factor_year": None,
        "jcr_quartile": None,
        "cas_year": None,
        "journal_metrics_match": None,
        "index_completeness": {
            key: bool(value)
            for key, value in {
                "title": title,
                "authors": row["authors"],
                "year": year,
                "venue": row.get("venue"),
                "doi": doi,
                "abstract": row.get("abstract"),
                "url": url,
            }.items()
        },
        "metadata_provenance": {"school_library": label},
        "publication_category": "期刊文章",
        "exact_title_match": False,
        "local_pdf_url": None,
        "local_figure_urls": [],
        "school_holdings": [label],
        "school_fulltext_url": (
            f"/api/v1/literature/school/fulltext/{quote(str(row['id']), safe='')}"
            if row["has_fulltext"]
            else None
        ),
    }


def merge_search_results(result: dict, rows: list[dict], *, limit: int) -> dict:
    """Interleave school-only hits without inventing a comparable quality score."""
    merged = dict(result)
    public_papers = [dict(paper) for paper in result.get("papers") or []]
    by_key = {_identity(paper): paper for paper in public_papers}
    school_only = []
    added = 0
    for row in rows:
        incoming = _school_paper(row)
        key = _identity(incoming)
        current = by_key.get(key)
        if current is None:
            school_only.append(incoming)
            by_key[key] = incoming
            added += 1
            continue
        current["school_holdings"] = list(
            dict.fromkeys([*(current.get("school_holdings") or []), *incoming["school_holdings"]])
        )
        if incoming["school_fulltext_url"]:
            current["school_fulltext_url"] = incoming["school_fulltext_url"]
        provenance = dict(current.get("metadata_provenance") or {})
        provenance["school_library"] = row["library_name"]
        current["metadata_provenance"] = provenance
        for field in ("abstract", "authors", "venue", "year", "pub_date", "url"):
            if not current.get(field) and incoming.get(field):
                current[field] = incoming[field]
    # Each source keeps its own order. Give a school-only hit one slot after
    # every four public hits, so a full public result does not hide the school
    # catalogue. When either source runs out, fill the remaining slots from
    # the other. The federated position is presentation order, not a quality
    # score comparable across two different search systems.
    papers = []
    public_index = school_index = public_since_school = 0
    cap = max(1, min(int(limit), 200))
    while len(papers) < cap and (public_index < len(public_papers) or school_index < len(school_only)):
        if school_index < len(school_only) and (
            public_index >= len(public_papers) or public_since_school >= 4
        ):
            papers.append(school_only[school_index])
            school_index += 1
            public_since_school = 0
        else:
            papers.append(public_papers[public_index])
            public_index += 1
            public_since_school += 1
    if rows:
        for position, paper in enumerate(papers):
            paper["federated_rank"] = position
    merged["papers"] = papers
    merged["total"] = len(merged["papers"])
    merged["candidate_count"] = (
        int(result.get("candidate_count") or len(result.get("papers") or [])) + added
    )
    counts = dict(result.get("source_counts") or {})
    if rows:
        counts["school_library"] = len(rows)
    merged["source_counts"] = counts
    return merged
