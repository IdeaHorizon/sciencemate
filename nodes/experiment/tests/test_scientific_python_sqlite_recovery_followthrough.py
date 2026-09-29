"""照 scientific-primary 的 SQLite 恢复指引走一遍：不能静默读到未提交的数据。

run_root 只读之后，作业被杀时留下热回滚日志（``-journal``）的库，普通读与
``mode=ro`` 都会报 ``attempt to write a readonly database``，结果里给出 SQLite
恢复指引。指引给了两条路：「干净关闭的库用 immutable=1」和「把库及其伴随
文件复制到 $TMPDIR」。本用例按指引原文逐条照做；任一条路只要返回
success，读到的就必须是已提交的数据。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys

import pytest

from core import sandbox
from nodes.experiment.tools import safe_bash
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)

# 已提交 3 行；第二个事务插入 397 行并把前三行改成 -1，未提交就被 SIGKILL。
# cache_size 很小，脏页在提交前已写进主库文件，热日志里存着回滚所需的原页。
_KILLED_MID_TRANSACTION = r'''
import os, signal, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("pragma journal_mode=delete")
c.execute("pragma cache_size=5")
c.execute("create table t(x, pad)")
c.execute("begin")
c.executemany("insert into t values(?, zeroblob(2000))", [(i,) for i in range(3)])
c.execute("commit")
c.execute("begin")
c.executemany("insert into t values(?, zeroblob(2000))", [(i,) for i in range(3, 400)])
c.execute("update t set x = -1 where x < 3")
os.kill(os.getpid(), signal.SIGKILL)
'''

_QUERY = (
    "print('count,neg=', "
    "c.execute('select count(*) from t').fetchone()[0], "
    "c.execute('select count(*) from t where x < 0').fetchone()[0])\n"
)


def _py(state, code, run_root):
    return asyncio.run(safe_bash._safe_execute_python(
        state, code, cwd=str(run_root), timeout=60))


def _bash(state, cmd, run_root):
    return asyncio.run(safe_bash._safe_run_bash(
        state, cmd, cwd=str(run_root), timeout=60))


def _committed_truth(run_root, tmp_path) -> tuple[int, int]:
    """在宿主可写目录的副本上让 SQLite 自己回滚热日志，得到已提交状态。"""
    host = tmp_path / "truth"
    host.mkdir()
    for path in run_root.glob("res.db*"):
        shutil.copyfile(path, host / path.name)
    conn = sqlite3.connect(host / "res.db")
    try:
        return (
            conn.execute("select count(*) from t").fetchone()[0],
            conn.execute("select count(*) from t where x < 0").fetchone()[0],
        )
    finally:
        conn.close()


def test_sqlite_recovery_does_not_lead_to_silent_torn_read(tmp_path) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path / "state")
    try:
        warm = _py(state, "print('warm')", run_root)
        assert warm["status"] == "success", warm
        subprocess.run(
            [sys.executable, "-c", _KILLED_MID_TRANSACTION,
             str(run_root / "res.db")],
            check=False,
        )
        assert (run_root / "res.db-journal").is_file()
        truth = _committed_truth(run_root, tmp_path)
        assert truth == (3, 0)
        expected = f"count,neg= {truth[0]} {truth[1]}"

        first = _py(
            state, "import sqlite3\nc = sqlite3.connect('res.db')\n" + _QUERY,
            run_root)
        assert first["status"] == "error", first
        assert first.get("reason") == "scientific_primary_run_root_readonly", first
        recovery = str(first.get("recovery") or "")
        assert recovery, first

        followed = []
        # 路一：指引把 immutable=1 当读法，且没点名热日志（-journal）这个例外
        # → 读到这句话的模型会照做。
        if "immutable=1" in recovery and "-journal" not in recovery:
            followed.append(("immutable=1", _py(
                state,
                "import sqlite3\n"
                "c = sqlite3.connect('file:res.db?mode=ro&immutable=1', uri=True)\n"
                + _QUERY,
                run_root,
            )))
        # 路二：把库连同指引点名、且确实存在的伴随文件复制进 Python 的
        # $TMPDIR 再读。轻量 Python 里 shutil.copy 会被早期门拦下，bash 的
        # $TMPDIR 又是另一个目录，所以这里用 bash 复制到显式的 scratch 路径。
        companions = [
            f"res.db{suffix}" for suffix in ("-wal", "-shm", "-journal")
            if suffix in recovery and (run_root / f"res.db{suffix}").exists()
        ]
        names = " ".join(["res.db", *companions])
        copied = _bash(state, f"cp {names} .python-scratch/tmp/", run_root)
        assert copied["status"] == "success", copied
        followed.append((f"copy {names}", _py(
            state,
            "import os, sqlite3\n"
            "c = sqlite3.connect(os.path.join(os.environ['TMPDIR'], 'res.db'))\n"
            + _QUERY,
            run_root,
        )))

        for label, result in followed:
            if result.get("status") == "success":
                assert expected in str(result.get("stdout_tail") or ""), (
                    label, recovery, result)
    finally:
        _evict_attempt(state)
