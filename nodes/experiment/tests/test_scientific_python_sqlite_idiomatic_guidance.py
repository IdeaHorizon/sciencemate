"""惯用写法的 SQLite 代码也必须拿到热日志安全指引。

v6 的连接能力逸出审计把 ``sqlite3.Row``、``except sqlite3.OperationalError``、
``sqlite3.PARSE_DECLTYPES``、``sqlite3.sqlite_version`` 等与建连接无关的
模块属性读取当成「能力逸出」，于是撤掉整段 recovery。结果：run_root 里
留下热回滚日志（``-journal``）的库，模型只看到一句
``attempt to write a readonly database``，没有伴随文件警告、没有
``.python-scratch/tmp`` 的字面路径；它最自然的下一步 ``immutable=1``
返回 success 并读出未提交数据。v4 对这些写法都给指引。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys

import pytest

from core import sandbox
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)
from nodes.experiment.tests.test_scientific_python_sqlite_recovery_followthrough import (
    _KILLED_MID_TRANSACTION,
)
from nodes.experiment.tools import safe_bash

_IDIOMATIC_READS = {
    "row_factory": (
        "import sqlite3\n"
        "c = sqlite3.connect('res.db')\n"
        "c.row_factory = sqlite3.Row\n"
    ),
    "except_operational_error": (
        "import sqlite3\n"
        "try:\n"
        "    c = sqlite3.connect('res.db')\n"
        "    c.execute('select 1').fetchall()\n"
        "except sqlite3.OperationalError:\n"
        "    raise\n"
    ),
    "except_error_base": (
        "import sqlite3\n"
        "try:\n"
        "    sqlite3.connect('res.db').execute('select 1')\n"
        "except sqlite3.Error as exc:\n"
        "    print(exc)\n"
        "    raise\n"
    ),
    "detect_types": (
        "import sqlite3\n"
        "c = sqlite3.connect('res.db', detect_types=sqlite3.PARSE_DECLTYPES)\n"
    ),
    "print_version": (
        "import sqlite3\n"
        "print(sqlite3.sqlite_version)\n"
        "c = sqlite3.connect('res.db')\n"
    ),
    "register_adapter": (
        "import sqlite3\n"
        "sqlite3.register_adapter(bool, int)\n"
        "c = sqlite3.connect('res.db')\n"
    ),
}


@pytest.mark.parametrize("code", list(_IDIOMATIC_READS.values()),
                         ids=list(_IDIOMATIC_READS))
def test_idiomatic_sqlite_read_keeps_hot_journal_guidance(tmp_path, code) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    scratch_tmp = run_root / ".python-scratch" / "tmp"
    result = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": (
                "sqlite3.OperationalError: attempt to write a readonly database"
            ),
        },
        scientific_primary=True,
        code=code,
        cwd=str(run_root),
        run_roots=[run_root],
        scratch_tmp=scratch_tmp,
    )
    recovery = str(result.get("recovery") or "")
    assert "禁止使用 immutable=1" in recovery, result
    assert str(scratch_tmp) in recovery, result


def test_row_factory_read_of_hot_journal_db_gets_followable_guidance(
    tmp_path,
) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path / "state")
    py = lambda code: asyncio.run(safe_bash._safe_execute_python(  # noqa: E731
        state, code, cwd=str(run_root), timeout=60))
    bash = lambda cmd: asyncio.run(safe_bash._safe_run_bash(  # noqa: E731
        state, cmd, cwd=str(run_root), timeout=60))
    query = (
        "print('count,neg=', *tuple(c.execute("
        "'select count(*), sum(x < 0) from t').fetchone()))\n"
    )
    try:
        assert py("print('warm')")["status"] == "success"
        subprocess.run(
            [sys.executable, "-c", _KILLED_MID_TRANSACTION,
             str(run_root / "res.db")],
            check=False,
        )
        assert (run_root / "res.db-journal").is_file()
        host = tmp_path / "truth"
        host.mkdir()
        for path in run_root.glob("res.db*"):
            shutil.copyfile(path, host / path.name)
        conn = sqlite3.connect(host / "res.db")
        truth = conn.execute(
            "select count(*), sum(x < 0) from t").fetchone()
        conn.close()
        assert tuple(truth) == (3, 0)

        first = py(
            "import sqlite3\n"
            "c = sqlite3.connect('res.db')\n"
            "c.row_factory = sqlite3.Row\n" + query)
        assert first["status"] == "error", first
        recovery = str(first.get("recovery") or "")
        # 没有这段文案，模型拿不到 .python-scratch/tmp 的路径，也看不到
        # 「有 -journal 时禁止 immutable=1」——而 immutable=1 会读出 4 1。
        assert "禁止使用 immutable=1" in recovery, first
        scratch_tmp = run_root / ".python-scratch" / "tmp"
        assert str(scratch_tmp) in recovery, first

        copied = bash(f"cp res.db res.db-journal {scratch_tmp}/")
        assert copied["status"] == "success", copied
        followed = py(
            "import os, sqlite3\n"
            "c = sqlite3.connect(os.path.join(os.environ['TMPDIR'], 'res.db'))\n"
            "c.row_factory = sqlite3.Row\n" + query)
        assert followed["status"] == "success", followed
        assert "count,neg= 3 0" in str(followed.get("stdout_tail") or ""), followed
    finally:
        _evict_attempt(state)
