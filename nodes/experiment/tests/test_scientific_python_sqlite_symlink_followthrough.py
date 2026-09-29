"""符号链接指向的库：伴随文件在真实路径旁，指引必须把模型带到那里。

SQLite 的 unix VFS 会解析符号链接，``-journal``/``-wal`` 建在目标文件旁。
run_root 里 ``res.db -> data/real.db`` 且事务中途被杀时，按现行文案
「检查数据库旁的伴随文件」在 ``res.db`` 旁找不到任何伴随文件，于是
immutable=1 或 ``cp res.db*`` 后打开副本，两条路都 success 并读出
未提交数据。节点已经把数据库路径解析成真实路径（归因靠它），指引应当
直接写出真实路径及其实际存在的伴随文件。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core import sandbox
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)
from nodes.experiment.tests.test_scientific_python_sqlite_recovery_followthrough import (
    _KILLED_MID_TRANSACTION,
    _QUERY,
)
from nodes.experiment.tools import safe_bash


@pytest.mark.parametrize(
    "idiomatic_suffix",
    ["", "c.row_factory = sqlite3.Row\n"],
    ids=["direct", "row_factory"],
)
def test_symlinked_hot_journal_db_guidance_names_real_companions(
    tmp_path,
    idiomatic_suffix,
) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path / "state")
    py = lambda code: asyncio.run(safe_bash._safe_execute_python(  # noqa: E731
        state, code, cwd=str(run_root), timeout=60))
    bash = lambda cmd: asyncio.run(safe_bash._safe_run_bash(  # noqa: E731
        state, cmd, cwd=str(run_root), timeout=60))
    try:
        assert py("print('warm')")["status"] == "success"
        (run_root / "data").mkdir()
        os.symlink("data/real.db", run_root / "res.db")
        subprocess.run(
            [sys.executable, "-c", _KILLED_MID_TRANSACTION,
             str(run_root / "res.db")],
            check=False,
        )
        real = run_root / "data" / "real.db"
        assert (run_root / "data" / "real.db-journal").is_file()
        assert not list(run_root.glob("res.db-*"))
        host = tmp_path / "truth"
        host.mkdir()
        for path in real.parent.glob("real.db*"):
            shutil.copyfile(path, host / path.name)
        conn = sqlite3.connect(host / "real.db")
        truth = conn.execute(
            "select count(*), sum(x < 0) from t").fetchone()
        conn.close()
        assert tuple(truth) == (3, 0)

        first = py(
            "import sqlite3\nc = sqlite3.connect('res.db')\n"
            + idiomatic_suffix
            + _QUERY
        )
        assert first["status"] == "error", first
        if not idiomatic_suffix:
            assert first.get("reason") == (
                "scientific_primary_run_root_readonly"
            )
        recovery = str(first.get("recovery") or "")
        # 伴随文件在真实路径旁：指引必须写出真实路径和那里的 -journal。
        assert str(real) in recovery, recovery
        assert "real.db-journal" in recovery, recovery
        if idiomatic_suffix:
            assert "候选数据库真实路径" in recovery, recovery

        match = re.search(r"cp 到 (\S+?)/（", recovery)
        assert match, recovery
        names = [
            name for name in ("real.db", "real.db-journal", "real.db-wal",
                              "real.db-shm")
            if (real.parent / name).exists()
        ]
        sources = " ".join(str(real.parent / name) for name in names)
        copied = bash(f"cp {sources} {match.group(1)}/")
        assert copied["status"] == "success", copied
        followed = py(
            "import os, sqlite3\n"
            "c = sqlite3.connect(os.path.join(os.environ['TMPDIR'], 'real.db'))\n"
            + _QUERY)
        assert followed["status"] == "success", followed
        assert "count,neg= 3 0" in str(followed.get("stdout_tail") or ""), followed
    finally:
        _evict_attempt(state)


def test_symlink_escape_is_not_probed_or_disclosed(
    tmp_path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    outside_db = outside / "secret.db"
    outside_db.write_bytes(b"not-needed-for-static-envelope")
    outside_journal = outside / "secret.db-journal"
    outside_journal.write_bytes(b"secret-sidecar")
    os.symlink(outside_db, run_root / "res.db")

    original_is_file = Path.is_file
    original_is_symlink = Path.is_symlink

    def guarded_is_file(path: Path) -> bool:
        if path == outside_db or path.is_relative_to(outside):
            raise AssertionError(f"probed outside run_root: {path}")
        return original_is_file(path)

    def guarded_is_symlink(path: Path) -> bool:
        if path == outside_db or path.is_relative_to(outside):
            raise AssertionError(f"probed outside run_root: {path}")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "is_symlink", guarded_is_symlink)
    result = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": (
                "sqlite3.OperationalError: attempt to write a readonly database"
            ),
        },
        scientific_primary=True,
        code="import sqlite3\nsqlite3.connect('res.db')\n",
        cwd=str(run_root),
        run_roots=[run_root],
        scratch_tmp=run_root / ".python-scratch" / "tmp",
    )

    assert result.get("reason") != "scientific_primary_run_root_readonly"
    recovery = str(result.get("recovery") or "")
    assert "禁止使用 immutable=1" in recovery
    assert str(outside) not in recovery
    assert outside_journal.name not in recovery
