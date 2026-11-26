"""Export a release catalogue containing only its public schema and metrics."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from nodes.literature.tools.journal_metrics import _create_schema

DESTINATION = Path(__file__).resolve().parents[2] / "nodes/literature/data/reference/journal_metrics.sqlite3"


def export_catalog(source: Path, target: Path) -> int:
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("Export to a separate file; the user's database must stay unchanged")
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(f".{uuid4().hex}.building")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(scratch)) as dst:
                _create_schema(dst)
                # Fresh pages avoid copying private metadata or deleted rows.
                for table in ("journals", "journal_aliases"):
                    columns = [row[1] for row in dst.execute(f"PRAGMA table_info({table})")]
                    names = ",".join(f'"{name}"' for name in columns)
                    rows = src.execute(f"SELECT {names} FROM {table} ORDER BY {names}")
                    dst.executemany(
                        f"INSERT INTO {table}({names}) VALUES({','.join('?' for _ in columns)})", rows,
                    )
                for key, value in src.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('factor_year','cas_year')"
                ):
                    dst.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (key, str(int(value))))
                count = dst.execute("SELECT count(*) FROM journals").fetchone()[0]
                if not count:
                    raise ValueError("Refusing to ship an empty journal catalogue")
                if dst.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Journal aliases reference missing records")
                dst.commit()
                dst.execute("VACUUM")
        scratch.replace(target)
        return count
    finally:
        scratch.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=DESTINATION)
    args = parser.parse_args()
    count = export_catalog(args.source, args.output)
    print(f"Exported {count} journals to {args.output}")


if __name__ == "__main__":
    main()
