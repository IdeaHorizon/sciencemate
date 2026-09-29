"""派发口按 schema 核**取值**（enum / 区间 / 非空 / pattern / required）。

判决拆除第三波刀 1：契约只在 schema 声明一次、派发口核一次、报错列合法值；
工具体内不再手写 176 处同一件事。类型（type）刻意不在这里查——有工具故意宽松
（save_artifact.metadata 收 JSON 字符串），那条走「崩了之后回头问形状」。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core import tool_errors as _errs
from core.state import State
from core.tool_registry import ToolDefinition, execute, register_tool

_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "n": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": {"type": "string", "minLength": 1},
        "name": {"type": "string", "pattern": r"^[a-z_]+$"},
        "items": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
        "opts": {"type": "object", "properties": {"level": {"type": "string", "enum": ["lo", "hi"]}}},
        "loose": {"type": "object"},
    },
    "required": ["mode"],
}
_CALLS: list[dict] = []


async def _echo(*, state, **kw):
    _CALLS.append(kw)
    return {"status": "success", "got": kw}


register_tool(ToolDefinition(name="schema_values_fixture", description="t",
                             parameters_schema=_SCHEMA), _echo)


def _state() -> State:
    return State.new(node_type="test", base_dir=Path(tempfile.mkdtemp()))


def _run(**kw):
    return asyncio.run(execute("schema_values_fixture", _state(), **kw))


def test_enum_violation_lists_legal_values_and_never_reaches_tool():
    _CALLS.clear()
    r = _run(mode="medium")
    assert r["status"] == "error" and r["error_code"] == _errs.REJECTED
    assert "fast" in r["error"] and "slow" in r["error"]
    assert r["parameter_violations"] and _CALLS == []


def test_in_contract_call_passes_through_untouched():
    r = _run(mode="fast", n=3, reason="x", name="ok_name", items=["a"], opts={"level": "hi"})
    assert r["status"] == "success" and r["got"]["n"] == 3


def test_range_and_pattern_and_nonempty():
    assert "最小值" in _run(mode="fast", n=0)["error"]
    assert "最大值" in _run(mode="fast", n=11)["error"]
    assert "不能为空" in _run(mode="fast", reason="   ")["error"]
    assert "不匹配" in _run(mode="fast", name="Bad Name")["error"]


def test_nested_items_and_properties_are_checked():
    assert "items[1]" in _run(mode="fast", items=["a", "zzz"])["error"]
    assert "opts.level" in _run(mode="fast", opts={"level": "mid"})["error"]


def test_none_means_absent_and_optional_none_is_fine():
    assert _run(mode="fast", n=None, reason=None)["status"] == "success"


def test_schema_required_is_enforced_even_when_signature_has_defaults():
    r = _run(n=2)
    assert r["status"] == "error" and r["error_code"] == _errs.MISSING_PARAMETERS
    assert "mode" in r["missing_parameters"]


def test_type_is_deliberately_not_checked_before_the_call():
    # 刻意宽松的工具（声明 object、也收字符串）照常跑到工具体内。
    r = _run(mode="fast", loose='{"k": 1}')
    assert r["status"] == "success" and r["got"]["loose"] == '{"k": 1}'
