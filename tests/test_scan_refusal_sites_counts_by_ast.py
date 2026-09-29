"""扫描器按 AST 计数，不按正则（判决拆除战役·刀4）。

正则版的四个盲区，每个一条用例：
  docstring 里写的「raise SchemaValidationError」不是拒绝点        → 0
  `return "", [], {"status": "error", …}` tuple 形态是拒绝点        → 1
  `return _error(...)` 助手形态是拒绝点                              → 1
  `raise ValueError(...)` 是内部契约，不是到达模型的拒绝形态         → 0
"""
from __future__ import annotations

import textwrap

import pytest

from scripts.scan_refusal_sites import count_refusal_sites, scan_file

DOCSTRING_ONLY = '''
def validate(record):
    """校验失败 raise SchemaValidationError；成功 return {"status": "error"} 绝不会发生。"""
    # raise StateContractError("也不算")
    return record
'''

TUPLE_FORM = '''
def read(path):
    if not path:
        return "", [], {"status": "error", "error": "path required"}
    return "x", [path], {"status": "success"}
'''

HELPER_FORM = '''
def _error(msg, **extra):
    return {"status": "error", "error": msg, **extra}

def act(x):
    if x is None:
        return _error("x required")
    return {"status": "success"}
'''

BUILTIN_EXCEPTION = '''
def parse(raw):
    if not isinstance(raw, str):
        raise ValueError("raw must be str")
    if raw == "":
        raise RuntimeError("empty")
    return raw
'''


@pytest.mark.parametrize("source, expected", [
    (DOCSTRING_ONLY, 0),
    (TUPLE_FORM, 1),
    (HELPER_FORM, 1 + 1),      # 助手自己的 return dict + 调用它的 return
    (BUILTIN_EXCEPTION, 0),
])
def test_each_blind_spot_of_the_regex(source, expected):
    assert count_refusal_sites(textwrap.dedent(source)) == expected


def test_contract_exceptions_count_whether_called_or_named():
    src = '''
from core.state import StateContractError
def a():
    raise StateContractError("x")
def b(exc):
    raise exc
def c():
    raise StateContractError
def d():
    raise _errs.ToolRejection("y")
'''
    assert count_refusal_sites(src) == 3      # a, c, d；b 抛的是变量，不计


def test_status_values_that_count():
    src = '''
def a(): return {"status": "blocked"}
def b(): return {"status": "needs_download_selection"}
def c(): return {"status": "recoverable_blocked"}
def d(): return {"status": "needs_x" if flag else "error"}
def e(): return {"status": "success"}
def f(): return {"status": "pause"}
def g(): return [{"status": "error"}]
'''
    assert count_refusal_sites(src) == 5


def test_unparseable_file_counts_zero_and_says_so(tmp_path, capsys):
    """语法错不静默：计 0（基线会因此缩水、棘轮转红）并在 stderr 报一行。"""
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8")
    assert scan_file(bad) == 0
    assert "cannot parse" in capsys.readouterr().err
