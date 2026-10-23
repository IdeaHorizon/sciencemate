"""Create a separate, fictional university literature database for integration demos."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path


def create(path: Path) -> None:
    # 两条「近期」记录的日期按建库当天算，不写死。学术馈送只收 31 天内的记录
    # （app/services/feed/windows.ACADEMIC_WINDOW_DAYS），写死 2026-09-20 的那条
    # 在 2026-10-22 起就掉出窗口，「两条都入馈送」的判据于是跟日历走：10-20 CI 绿、
    # 10-22 本机红，同一 commit。LIB-003 保持陈旧，窗口判据仍有一条反例。
    today = date.today()
    recent = today - timedelta(days=25)
    newer = today - timedelta(days=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS school_papers (
                id TEXT PRIMARY KEY,
                doi TEXT UNIQUE,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                abstract TEXT,
                keywords TEXT,
                venue TEXT,
                year INTEGER,
                published_at TEXT,
                url TEXT,
                fulltext_text TEXT
            )
        """)
        rows = [
            (
                "LIB-001",
                "10.5555/sciencemate.demo.001",
                "Lattice Boltzmann simulation of irregular particle sedimentation",
                ["Demo Researcher", "Example Author"],
                (
                    "A demonstration study of lattice Boltzmann and immersed boundary "
                    "methods for irregular particle sedimentation."
                ),
                "lattice boltzmann immersed boundary particle sedimentation",
                "Demo Journal of Fluid Mechanics",
                recent.year,
                recent.isoformat(),
                "https://doi.org/10.5555/sciencemate.demo.001",
                (
                    "FICTIONAL SCHOOL LIBRARY FULL TEXT — LIB-001\n"
                    "This is test data, not a published paper."
                ),
            ),
            (
                "LIB-002",
                "10.5555/sciencemate.demo.002",
                (
                    "Lattice Boltzmann and immersed boundary methods "
                    "for three-dimensional particle settling"
                ),
                ["Sample Scholar"],
                (
                    "A fictional institutional-only record about lattice Boltzmann "
                    "simulations of three-dimensional particle settling."
                ),
                "lattice boltzmann immersed boundary three-dimensional settling",
                "Demo University Repository",
                newer.year,
                newer.isoformat(),
                "https://doi.org/10.5555/sciencemate.demo.002",
                (
                    "FICTIONAL SCHOOL LIBRARY FULL TEXT — LIB-002\n"
                    "This is test data, not a published paper."
                ),
            ),
            (
                "LIB-003",
                "10.5555/sciencemate.demo.003",
                "A fictional campus climate survey",
                ["Sample Scholar"],
                "An unrelated test record for query isolation.",
                "campus climate survey",
                "Demo University Repository",
                2025,
                "2025-11-12",
                None,
                None,
            ),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO school_papers
              (id, doi, title, authors_json, abstract, keywords, venue, year,
               published_at, url, fulltext_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            [
                (
                    id_,
                    doi,
                    title,
                    json.dumps(authors),
                    abstract,
                    keywords,
                    venue,
                    year,
                    published_at,
                    url,
                    fulltext,
                )
                for (
                    id_,
                    doi,
                    title,
                    authors,
                    abstract,
                    keywords,
                    venue,
                    year,
                    published_at,
                    url,
                    fulltext,
                ) in rows
            ],
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="Output SQLite path, separate from the platform database"
    )
    args = parser.parse_args()
    create(args.path)
    print(args.path.resolve())
