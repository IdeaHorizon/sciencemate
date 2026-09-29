"""SQLite catalog for the shared literature asset store.

The catalog contains metadata and paths only. PDF/text/image bytes remain in the
per-paper directories so the files can be copied, inspected, or recovered
without depending on SQLite.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

CATALOG_FILENAME = "literature_catalog.sqlite3"


def catalog_path(papers_root: str | Path) -> Path:
    return Path(papers_root) / CATALOG_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS papers (
            doi TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            year INTEGER,
            article_dir TEXT NOT NULL,
            index_path TEXT NOT NULL,
            pdf_path TEXT NOT NULL DEFAULT '',
            text_path TEXT NOT NULL DEFAULT '',
            figure_path TEXT NOT NULL DEFAULT '',
            fulltext_status TEXT NOT NULL DEFAULT '',
            fulltext_source TEXT NOT NULL DEFAULT '',
            image_status TEXT NOT NULL DEFAULT '',
            image_source TEXT NOT NULL DEFAULT '',
            pdf_sha256 TEXT NOT NULL DEFAULT '',
            image_sha256 TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            xiaohongshu_eligible INTEGER NOT NULL DEFAULT 0,
            classification_mode TEXT NOT NULL DEFAULT '',
            second_level_domains TEXT NOT NULL DEFAULT '[]',
            harvest_batch TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            citations INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_fulltext ON papers(fulltext_status)")
    # Stable sort/filter keys for academic search. Relevance remains query-time.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_published_at ON papers(published_at)")
    for statement in (
        "ALTER TABLE papers ADD COLUMN xiaohongshu_eligible INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE papers ADD COLUMN classification_mode TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE papers ADD COLUMN second_level_domains TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE papers ADD COLUMN harvest_batch TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE papers ADD COLUMN published_at TEXT NOT NULL DEFAULT ''",
    ):
        try: conn.execute(statement)
        except sqlite3.OperationalError: pass
    try:
        conn.execute("ALTER TABLE papers ADD COLUMN citations INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Promote legacy citation counts only once; opening the catalog happens for
    # every harvest batch, so an unguarded full-table UPDATE would rescan millions
    # of rows on every page.
    if int(conn.execute("PRAGMA user_version").fetchone()[0] or 0) < 1:
        conn.execute("UPDATE papers SET citations=CAST(json_extract(metadata_json, '$.citations') AS INTEGER) WHERE citations=0 AND json_valid(metadata_json) AND json_type(metadata_json, '$.citations') IN ('integer','real','text')")
        conn.execute("PRAGMA user_version=1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_citations ON papers(citations DESC)")
    conn.execute("CREATE TABLE IF NOT EXISTS feed_recommendations (user_id TEXT NOT NULL, doi TEXT NOT NULL, batch_id TEXT NOT NULL, shown_at REAL NOT NULL, section TEXT NOT NULL DEFAULT '', score REAL, action TEXT NOT NULL DEFAULT 'shown', PRIMARY KEY(user_id, doi, batch_id))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_recommendations_user ON feed_recommendations(user_id, shown_at)")
    conn.commit()
    return conn



def upsert_papers(papers_root: str | Path, records: list[dict[str, Any]]) -> None:
    """Batch upsert paper indexes in one SQLite transaction.

    JSON files are written by the caller; this function only updates the
    catalog. Repeating a batch is idempotent and existing non-empty asset
    paths are preserved when incoming records omit them.
    """
    path = catalog_path(papers_root)
    prepared = []
    for record in records:
        doi = str(record.get("doi") or "").strip().lower()
        if not doi:
            url = str(record.get("url") or "").strip()
            if not url:
                continue
            doi = "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        image = record.get("image") if isinstance(record.get("image"), dict) else {}
        year = record.get("year")
        try: year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError): year = None
        prepared.append((doi, str(record.get("title") or ""), str(record.get("source") or ""), year,
            str(record.get("article_dir") or ""), str(record.get("index_path") or ""), str(record.get("pdf_path") or ""),
            str(record.get("text_path") or ""), str(image.get("path") or ""), str(record.get("fulltext_status") or ""),
            str(record.get("fulltext_source") or ""), str(image.get("status") or ""), str(image.get("source") or ""),
            str(record.get("pdf_sha256") or ""), str(image.get("sha256") or ""), json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            1 if record.get("xiaohongshu_eligible") else 0,
            str(record.get("classification_mode") or (record.get("journal_classification") or {}).get("reason") or ""),
            json.dumps(record.get("second_level_domains") or (record.get("journal_classification") or {}).get("level2_codes") or [], ensure_ascii=False),
            str(record.get("harvest_batch") or ""), str(record.get("published_at") or record.get("pub_date") or ""),
            int(record.get("citations") or 0), time.time()))
    if not prepared: return
    with _connect(path) as conn:
        conn.executemany(
            """INSERT INTO papers
            (doi,title,source,year,article_dir,index_path,pdf_path,text_path,figure_path,fulltext_status,fulltext_source,image_status,image_source,pdf_sha256,image_sha256,metadata_json,xiaohongshu_eligible,classification_mode,second_level_domains,harvest_batch,published_at,citations,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(doi) DO UPDATE SET
              title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE papers.title END,
              source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE papers.source END,
              year=COALESCE(excluded.year,papers.year), article_dir=CASE WHEN excluded.article_dir<>'' THEN excluded.article_dir ELSE papers.article_dir END,
              index_path=CASE WHEN excluded.index_path<>'' THEN excluded.index_path ELSE papers.index_path END,
              pdf_path=CASE WHEN excluded.pdf_path<>'' THEN excluded.pdf_path ELSE papers.pdf_path END, text_path=CASE WHEN excluded.text_path<>'' THEN excluded.text_path ELSE papers.text_path END,
              figure_path=CASE WHEN excluded.figure_path<>'' THEN excluded.figure_path ELSE papers.figure_path END,
              fulltext_status=CASE WHEN excluded.fulltext_status NOT IN ('','not_requested') THEN excluded.fulltext_status ELSE papers.fulltext_status END,
              fulltext_source=CASE WHEN excluded.fulltext_source<>'' THEN excluded.fulltext_source ELSE papers.fulltext_source END,
              image_status=CASE WHEN excluded.image_status<>'' THEN excluded.image_status ELSE papers.image_status END, image_source=CASE WHEN excluded.image_source<>'' THEN excluded.image_source ELSE papers.image_source END,
              pdf_sha256=CASE WHEN excluded.pdf_sha256<>'' THEN excluded.pdf_sha256 ELSE papers.pdf_sha256 END, image_sha256=CASE WHEN excluded.image_sha256<>'' THEN excluded.image_sha256 ELSE papers.image_sha256 END,
              metadata_json=excluded.metadata_json, xiaohongshu_eligible=MAX(papers.xiaohongshu_eligible,excluded.xiaohongshu_eligible), classification_mode=CASE WHEN excluded.classification_mode<>'' THEN excluded.classification_mode ELSE papers.classification_mode END,
              second_level_domains=CASE WHEN excluded.second_level_domains<>'[]' THEN excluded.second_level_domains ELSE papers.second_level_domains END, harvest_batch=CASE WHEN excluded.harvest_batch<>'' THEN excluded.harvest_batch ELSE papers.harvest_batch END, published_at=CASE WHEN excluded.published_at<>'' THEN excluded.published_at ELSE papers.published_at END, citations=MAX(papers.citations,excluded.citations), updated_at=excluded.updated_at""", prepared)
        conn.commit()

def upsert_paper(papers_root: str | Path, record: dict[str, Any]) -> None:
    """Register one per-paper index; safe to call repeatedly across runs."""
    doi = str(record.get("doi") or "").strip().lower()
    if not doi:
        # RSS and some repository feeds have no DOI.  Use a deterministic URL
        # key so they remain searchable without pretending a DOI exists.
        url = str(record.get("url") or "").strip()
        if not url:
            return
        doi = "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    image = record.get("image") if isinstance(record.get("image"), dict) else {}
    year = record.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    values = (
        doi,
        str(record.get("title") or ""),
        str(record.get("source") or ""),
        year,
        str(record.get("article_dir") or ""),
        str(record.get("index_path") or ""),
        str(record.get("pdf_path") or ""),
        str(record.get("text_path") or ""),
        str(image.get("path") or ""),
        str(record.get("fulltext_status") or ""),
        str(record.get("fulltext_source") or ""),
        str(image.get("status") or ""),
        str(image.get("source") or ""),
        str(record.get("pdf_sha256") or ""),
        str(image.get("sha256") or ""),
        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        1 if record.get("xiaohongshu_eligible") else 0,
        str(record.get("classification_mode") or (record.get("journal_classification") or {}).get("reason") or ""),
        json.dumps(record.get("second_level_domains") or (record.get("journal_classification") or {}).get("level2_codes") or [], ensure_ascii=False),
        str(record.get("harvest_batch") or ""),
        str(record.get("published_at") or record.get("pub_date") or ""),
        int(record.get("citations") or 0),
        time.time(),
    )
    path = catalog_path(papers_root)
    with _connect(path) as conn:
        conn.execute(
            """INSERT INTO papers
            (doi,title,source,year,article_dir,index_path,pdf_path,text_path,
             figure_path,fulltext_status,fulltext_source,image_status,image_source,
             pdf_sha256,image_sha256,metadata_json,xiaohongshu_eligible,classification_mode,
             second_level_domains,harvest_batch,published_at,citations,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(doi) DO UPDATE SET
              title=excluded.title, source=excluded.source, year=excluded.year,
              article_dir=excluded.article_dir, index_path=excluded.index_path,
              pdf_path=excluded.pdf_path, text_path=excluded.text_path,
              figure_path=excluded.figure_path, fulltext_status=excluded.fulltext_status,
              fulltext_source=excluded.fulltext_source, image_status=excluded.image_status,
              image_source=excluded.image_source, pdf_sha256=excluded.pdf_sha256,
              image_sha256=excluded.image_sha256, metadata_json=excluded.metadata_json,
              xiaohongshu_eligible=excluded.xiaohongshu_eligible,
              classification_mode=excluded.classification_mode,
              second_level_domains=excluded.second_level_domains,
              harvest_batch=excluded.harvest_batch,
              published_at=excluded.published_at,
              citations=MAX(papers.citations,excluded.citations), updated_at=excluded.updated_at""",
            values,
        )
        conn.commit()


def retry_asset_records(
    papers_root: str | Path, limit: int = 20
) -> list[dict[str, Any]]:
    """返回需要补齐全文或真实图片的论文记录。

    标题卡属于可见兜底而不是真实论文插图，因此仍是 figure 重试候选。
    SQLite 只负责筛选；完整元数据从同一行的 ``metadata_json`` 恢复。
    """
    path = catalog_path(papers_root)
    if not path.is_file():
        return []
    with _connect(path) as conn:
        rows = conn.execute(
            """SELECT metadata_json FROM papers
               WHERE fulltext_status IN ('', 'not_requested', 'fetch_failed',
                                         'deferred', 'oa_unknown', 'unavailable')
                  OR image_status IN ('', 'not_requested', 'failed', 'unavailable')
                  OR (json_valid(metadata_json)
                      AND json_extract(metadata_json, '$.image.fallback') = 'title_card')
               ORDER BY updated_at ASC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    records: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records
