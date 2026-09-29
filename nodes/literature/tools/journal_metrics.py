"""统一的期刊级指标查询与离线导入。

数据文件默认位于 ``$HARNESS_FRAMEWORK_HOME/literature/reference/``，不随代码仓
分发。运行时只读 SQLite；构建/更新数据库是显式离线操作。
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from core import paths as _paths

_SCHEMA_VERSION = "1"
_DEFAULT_DB_NAME = "journal_metrics.sqlite3"


def database_path() -> Path:
    configured = os.environ.get("HARNESS_JOURNAL_METRICS_DB", "").strip()
    return Path(configured).expanduser() if configured else _paths.literature_reference_dir() / _DEFAULT_DB_NAME


def normalize_issn(value: str | None) -> str:
    compact = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    return f"{compact[:4]}-{compact[4:]}" if len(compact) == 8 else ""


def normalize_journal(value: str | None) -> str:
    text = html.unescape(str(value or "")).lower().strip()
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def _journal_candidates(value: str | None) -> list[str]:
    decoded = html.unescape(str(value or "")).strip()
    variants = [decoded]
    without_parentheses = re.sub(r"\s*\([^)]*\)\s*", " ", decoded).strip()
    if without_parentheses and without_parentheses != decoded:
        variants.append(without_parentheses)
    result: list[str] = []
    for variant in variants:
        normalized = normalize_journal(variant)
        for key in (normalized, normalized[:-1] if normalized.endswith("s") and not normalized.endswith("ss") else normalized):
            if key and key not in result:
                result.append(key)
    return result


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS journals (
            id INTEGER PRIMARY KEY,
            journal TEXT NOT NULL,
            journal_normalized TEXT NOT NULL UNIQUE,
            journal_abbr TEXT,
            journal_abbr_normalized TEXT,
            issn TEXT,
            eissn TEXT,
            nlm_id TEXT,
            impact_factor REAL,
            impact_factor_year INTEGER,
            jcr_quartile TEXT,
            cas_quartile INTEGER,
            cas_top INTEGER NOT NULL DEFAULT 0,
            cas_year INTEGER,
            factor_source TEXT,
            cas_source TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS journal_aliases (
            alias_normalized TEXT NOT NULL,
            journal_id INTEGER NOT NULL REFERENCES journals(id) ON DELETE CASCADE,
            alias_type TEXT NOT NULL,
            PRIMARY KEY(alias_normalized, journal_id)
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_journals_issn ON journals(issn);
        CREATE INDEX IF NOT EXISTS idx_journals_eissn ON journals(eissn);
        CREATE INDEX IF NOT EXISTS idx_journals_nlm_id ON journals(nlm_id);
        CREATE INDEX IF NOT EXISTS idx_journals_name ON journals(journal_normalized);
        CREATE INDEX IF NOT EXISTS idx_journals_abbr ON journals(journal_abbr_normalized);
        CREATE INDEX IF NOT EXISTS idx_aliases_name ON journal_aliases(alias_normalized);
    """)
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (_SCHEMA_VERSION,))


def _upsert_aliases(conn: sqlite3.Connection, journal_id: int, values: list[tuple[str, str]]) -> None:
    for value, alias_type in values:
        for alias in _journal_candidates(value):
            conn.execute(
                "INSERT OR IGNORE INTO journal_aliases(alias_normalized,journal_id,alias_type) VALUES(?,?,?)",
                (alias, journal_id, alias_type),
            )


def import_factor_database(source_db: Path, target_db: Path, *, factor_year: int) -> int:
    """从 impact_factor 兼容 SQLite 导入 JIF/JCR/标识符。"""
    source_db = Path(source_db)
    target_db = Path(target_db)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target = sqlite3.connect(target_db)
    try:
        _create_schema(target)
        count = 0
        for row in source.execute(
            "SELECT journal,journal_abbr,issn,eissn,nlm_id,factor,jcr FROM factor"
        ):
            journal, abbr, issn, eissn, nlm_id, factor, jcr = row
            normalized = normalize_journal(journal)
            if not normalized:
                continue
            target.execute(
                """INSERT INTO journals(
                       journal,journal_normalized,journal_abbr,journal_abbr_normalized,
                       issn,eissn,nlm_id,impact_factor,impact_factor_year,jcr_quartile,factor_source
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(journal_normalized) DO UPDATE SET
                       journal_abbr=excluded.journal_abbr,
                       journal_abbr_normalized=excluded.journal_abbr_normalized,
                       issn=excluded.issn,eissn=excluded.eissn,nlm_id=excluded.nlm_id,
                       impact_factor=excluded.impact_factor,
                       impact_factor_year=excluded.impact_factor_year,
                       jcr_quartile=excluded.jcr_quartile,
                       factor_source=excluded.factor_source,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    str(journal or ""), normalized, str(abbr or ""), normalize_journal(abbr),
                    normalize_issn(issn), normalize_issn(eissn), str(nlm_id or "").strip(" ."),
                    float(factor) if factor is not None else None, int(factor_year), str(jcr or "").strip(" .") or None,
                    "suqingdong/impact_factor local import",
                ),
            )
            journal_id = target.execute("SELECT id FROM journals WHERE journal_normalized=?", (normalized,)).fetchone()[0]
            _upsert_aliases(target, journal_id, [(str(journal or ""), "journal"), (str(abbr or ""), "abbreviation")])
            count += 1
        target.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('factor_year',?)", (str(factor_year),))
        target.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('factor_source_path',?)", (str(source_db),))
        target.commit()
        return count
    finally:
        source.close()
        target.close()


def import_cas_xlsx(source_xlsx: Path, target_db: Path, *, cas_year: int) -> int:
    """把现有中科院分区表并入同一查询库；不依赖 pandas/openpyxl。"""
    from .cas_ranking import _xlsx_rows

    target_db = Path(target_db)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(target_db)
    try:
        _create_schema(target)
        rows = iter(_xlsx_rows(Path(source_xlsx)))
        headers = [str(value).strip() for value in next(rows)]
        journal_index = next((i for i, name in enumerate(headers) if "期刊" in name or "journal" in name.lower()), 0)
        quartile_index = next((i for i, name in enumerate(headers) if "分区" in name or "quartile" in name.lower()), 1)
        top_index = next((i for i, name in enumerate(headers) if name.lower() == "top"), None)
        count = 0
        for row in rows:
            journal = row[journal_index].strip() if journal_index < len(row) else ""
            raw_quartile = row[quartile_index].strip() if quartile_index < len(row) else ""
            try:
                quartile = int(float(raw_quartile))
            except (TypeError, ValueError):
                continue
            normalized = normalize_journal(journal)
            if not normalized or quartile not in {1, 2, 3, 4}:
                continue
            raw_top = row[top_index].strip().lower() if top_index is not None and top_index < len(row) else ""
            is_top = int(raw_top in {"是", "yes", "true", "1"})
            target.execute(
                """INSERT INTO journals(journal,journal_normalized,cas_quartile,cas_top,cas_year,cas_source)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(journal_normalized) DO UPDATE SET
                       cas_quartile=excluded.cas_quartile,cas_top=excluded.cas_top,
                       cas_year=excluded.cas_year,cas_source=excluded.cas_source,
                       updated_at=CURRENT_TIMESTAMP""",
                (journal, normalized, quartile, is_top, int(cas_year), "local CAS ranking xlsx"),
            )
            journal_id = target.execute("SELECT id FROM journals WHERE journal_normalized=?", (normalized,)).fetchone()[0]
            _upsert_aliases(target, journal_id, [(journal, "journal")])
            count += 1
        target.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('cas_year',?)", (str(cas_year),))
        target.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('cas_source_path',?)", (str(source_xlsx),))
        target.commit()
        return count
    finally:
        target.close()


def _row_dict(row: sqlite3.Row | None, match_method: str = "") -> dict[str, Any]:
    if row is None:
        return {}
    result = dict(row)
    result["cas_top"] = bool(result.get("cas_top"))
    result["match_method"] = match_method
    return result


@lru_cache(maxsize=8192)
def _lookup_cached(
    db_name: str, journal: str, journal_abbr: str, issn: str, eissn: str, nlm_id: str
) -> dict[str, Any]:
    path = Path(db_name)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = "j.*"
    try:
        for field, value in (("issn", normalize_issn(issn)), ("eissn", normalize_issn(eissn)), ("nlm_id", nlm_id.strip())):
            if value:
                row = conn.execute(f"SELECT {columns} FROM journals j WHERE j.{field}=? LIMIT 1", (value,)).fetchone()
                if row:
                    return _row_dict(row, field)
        candidates: list[tuple[str, str]] = []
        candidates.extend((key, "journal") for key in _journal_candidates(journal))
        candidates.extend((key, "journal_abbr") for key in _journal_candidates(journal_abbr))
        seen: set[str] = set()
        for key, method in candidates:
            if key in seen:
                continue
            seen.add(key)
            row = conn.execute(
                f"SELECT {columns} FROM journals j WHERE j.journal_normalized=? OR j.journal_abbr_normalized=? LIMIT 1",
                (key, key),
            ).fetchone()
            if row:
                return _row_dict(row, method)
            row = conn.execute(
                f"SELECT {columns} FROM journal_aliases a JOIN journals j ON j.id=a.journal_id WHERE a.alias_normalized=? LIMIT 1",
                (key,),
            ).fetchone()
            if row:
                return _row_dict(row, "alias")
        return {}
    finally:
        conn.close()


def lookup_journal_metrics(
    journal: str = "", *, journal_abbr: str = "", issn: str = "", eissn: str = "", nlm_id: str = ""
) -> dict[str, Any]:
    return _lookup_cached(
        str(database_path()), str(journal or ""), str(journal_abbr or ""),
        str(issn or ""), str(eissn or ""), str(nlm_id or ""),
    )


def diagnostics() -> dict[str, Any]:
    path = database_path()
    if not path.is_file():
        return {"path": str(path), "exists": False, "journals": 0, "with_factor": 0}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        total, with_factor, with_cas = conn.execute(
            "SELECT count(*),sum(impact_factor IS NOT NULL),sum(cas_quartile IS NOT NULL) FROM journals"
        ).fetchone()
        metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        return {
            "path": str(path), "exists": True, "journals": total,
            "with_factor": with_factor or 0, "with_cas": with_cas or 0,
            "metadata": metadata,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="构建本地期刊指标数据库")
    parser.add_argument("--factor-db", type=Path)
    parser.add_argument("--factor-year", type=int, default=2024)
    parser.add_argument("--cas-xlsx", type=Path)
    parser.add_argument("--cas-year", type=int, default=2025)
    parser.add_argument("--output", type=Path, default=database_path())
    args = parser.parse_args()
    if not args.factor_db and not args.cas_xlsx:
        parser.error("至少提供 --factor-db 或 --cas-xlsx")
    if args.factor_db:
        print(f"factor imported: {import_factor_database(args.factor_db, args.output, factor_year=args.factor_year)}")
    if args.cas_xlsx:
        print(f"CAS imported: {import_cas_xlsx(args.cas_xlsx, args.output, cas_year=args.cas_year)}")
    print(diagnostics())


if __name__ == "__main__":
    main()
