"""守住 v6 连接能力审计里「交付说声称、但现有用例一条都没守」的部分。

每条都是：库里有一个（不执行的）run_root 内字面连接，真正打开库的是审计
认不出的调用；审计声称这些写法会让分析 incomplete、不给 run_root 归因。
安全 recovery 与该归因无关，任何 SQLite 只读类错误都应保留指引。
下列用例专门保护 reason 标签不被错误归因；旧有 45 条用例在这些放宽
变异下全绿：模块引用逸出审计整段关掉、shadow/
rebind 整段关掉（或只关 arg / def/class / 赋值值校验）、星号导入放行、
别名不动点只跑一轮、非字面 getattr 当作已消费。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nodes.experiment.tools import safe_bash

_SHIM = (
    "class _Shim:\n"
    "    def connect(self, *_):\n"
    "        return __import__('sqlite3').connect(OUTSIDE)\n"
)

_CASES = {
    # 模块引用逸出（变异：module_reference_is_consumed 恒 True）
    "module_in_list": "mods = [sqlite3]\nmods[0].connect(OUTSIDE)\n",
    "module_as_argument": (
        "def open_with(mod):\n"
        "    return mod.connect(OUTSIDE)\n"
        "open_with(sqlite3)\n"
    ),
    "module_vars": "vars(sqlite3)['connect'](OUTSIDE)\n",
    "module_dunder_dict": "sqlite3.__dict__['connect'](OUTSIDE)\n",
    # 非字面 getattr（变异：getattr 第二参不校验字面）
    "dynamic_getattr": "name = 'connect'\ngetattr(sqlite3, name)(OUTSIDE)\n",
    # 星号导入（变异：from sqlite3 import * 不置 incomplete）
    "star_import": "from sqlite3 import *\nconnect(OUTSIDE)\n",
    # 别名不动点（变异：只跑一轮）
    "alias_needs_second_pass": (
        "if True:\n"
        "    first = sqlite3.connect\n"
        "second = first\n"
        "second(OUTSIDE)\n"
    ),
    # shadow/rebind（变异：整段关掉，或分别关掉下面三种）
    "rebind_module_name": _SHIM + "sqlite3 = _Shim()\nsqlite3.connect('inside.db')\n",
    "rebind_alias_value": (
        _SHIM
        + "open_db = sqlite3.connect\n"
        + "open_db = _Shim().connect\n"
        + "open_db('inside.db')\n"
    ),
    "shadow_by_parameter": (
        _SHIM
        + "def load(sqlite3):\n"
        + "    return sqlite3.connect('inside.db')\n"
        + "load(_Shim())\n"
    ),
    "shadow_by_class": (
        "class sqlite3:\n"
        "    @staticmethod\n"
        "    def connect(*_):\n"
        "        return __import__('sqlite3').connect(OUTSIDE)\n"
        "sqlite3.connect('inside.db')\n"
    ),
}


@pytest.mark.parametrize("body", list(_CASES.values()), ids=list(_CASES))
def test_unaudited_sqlite_capability_is_not_attributed_to_run_root(
    tmp_path: Path,
    body: str,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside.db"
    code = (
        "import sqlite3\n"
        "if False:\n"
        "    sqlite3.connect('dormant-inside.db')\n"
        + body.replace("OUTSIDE", repr(str(outside)))
    )
    compile(code, "<case>", "exec")
    result = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": "sqlite3.OperationalError: unable to open database file",
        },
        scientific_primary=True,
        code=code,
        cwd=str(run_root),
        run_roots=[run_root],
        scratch_tmp=run_root / ".python-scratch" / "tmp",
    )
    assert result.get("reason") != "scientific_primary_run_root_readonly", code
    recovery = str(result.get("recovery") or "")
    assert "禁止使用 immutable=1" in recovery, code
