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


def _publication_category(row: dict) -> str:
    from app.services.harness_imports import harness_module
    return harness_module("shared.contrib.publication_categories").publication_category(
        {"pub_type":"journal-article", **row})


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


def search(query: str, *, institution_id: str, limit: int | None = 200) -> list[dict]:
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
        + " ORDER BY published_at DESC, id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(max(1, int(limit)))
    with closing(_connect(path)) as conn:
        rows = conn.execute(sql, parameters).fetchall()
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
        "metadata_provenance": {
            "school_library": label,
            **{key: label for key, value in {
                "title": title, "authors": row["authors"], "year": year,
                "venue": row.get("venue"), "doi": doi,
                "abstract": row.get("abstract"), "url": url,
                "pub_date": row.get("published_at"),
            }.items() if value},
        },
        "publication_category": _publication_category(row),
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


def merge_search_results(result: dict, rows: list[dict], *, limit: int | None) -> dict:
    from app.services.harness_imports import harness_module
    return harness_module("shared.contrib.literature_federation").merge_school_papers(
        result, [_school_paper(row) for row in rows], limit=limit,
    )
