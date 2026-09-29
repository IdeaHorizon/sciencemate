"""指路文案要在真实后端的真实 stderr 上出现，并且照做能走通。"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from core import sandbox
from nodes.experiment.tools import safe_bash
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)


def _require_native_backend() -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")


def test_clean_wal_database_readonly_failure_points_to_immutable(tmp_path) -> None:
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    db = run_root / "results.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("create table t(x)")
    conn.execute("insert into t values (42)")
    conn.commit()
    conn.close()
    try:
        failed = asyncio.run(safe_bash._safe_execute_python(
            state,
            "import sqlite3\n"
            f"print(sqlite3.connect('file:{db}?mode=ro', uri=True)"
            ".execute('select x from t').fetchall())\n",
            cwd=str(run_root), timeout=30))
        assert failed["status"] == "error", failed
        # bwrap 上真实 stderr 是 "unable to open database file"，Landlock 上是
        # "attempt to write a readonly database"；两者都必须指路。
        assert failed.get("reason") == "scientific_primary_run_root_readonly", failed
        assert "immutable=1" in str(failed.get("recovery") or ""), failed
        followed = asyncio.run(safe_bash._safe_execute_python(
            state,
            "import sqlite3\n"
            f"print(sqlite3.connect('file:{db}?mode=ro&immutable=1', uri=True)"
            ".execute('select x from t').fetchall())\n",
            cwd=str(run_root), timeout=30))
        assert followed["status"] == "success", followed
        assert "[(42,)]" in str(followed.get("stdout_tail") or "")
    finally:
        _evict_attempt(state)


def test_getattr_sqlite_outside_failure_is_not_misattributed_to_run_root(
    tmp_path,
) -> None:
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path / "state")
    outside = tmp_path / "outside" / "results.db"
    code = (
        "import sqlite3\n"
        "if False:\n"
        "    sqlite3.connect('dormant-inside.db')\n"
        f"getattr(sqlite3, 'connect')({str(outside)!r})\n"
    )
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, code, cwd=str(run_root), timeout=30))
        assert result["status"] == "error", result
        assert int(result.get("returncode") or 0) != 0, result
        assert result.get("reason") != (
            "scientific_primary_run_root_readonly"
        ), result
        assert "禁止使用 immutable=1" in str(result.get("recovery") or ""), result
        assert not outside.exists()
    finally:
        _evict_attempt(state)
