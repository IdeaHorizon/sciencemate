"""验证 docs/tool-spec.md §6 dummy example 工具能跑通。

虽然真实文件叫 .example（不被自动 import），这里手动 import 进来验证规范跑通。
跑：  python -m pytest tests/test_extract_keywords_example.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute, get_tool


def _load_example_module():
    """动态 load extract_keywords.py.example（不靠 bootstrap）。

    `.py.example` 后缀 importlib 默认认不出，强制指定 SourceFileLoader。
    """
    from importlib.machinery import SourceFileLoader
    path = Path(__file__).parent.parent / "nodes" / "experiment" / "tools" / "extract_keywords.py.example"
    loader = SourceFileLoader("dummy_extract_keywords", str(path))
    spec = importlib.util.spec_from_loader("dummy_extract_keywords", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dummy_extract_keywords"] = mod
    loader.exec_module(mod)
    return mod


bootstrap()                     # 确保 framework 起来
_load_example_module()          # 注册 extract_keywords


def _make_state() -> State:
    return State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()))


def test_dummy_tool_registered():
    """Dummy 工具应该已经注册到 registry。"""
    t = get_tool("extract_keywords")
    assert t is not None
    assert t.name == "extract_keywords"
    assert "top_n" in t.parameters_schema["properties"]


def test_dummy_tool_happy_path():
    """正常输入返合规 dict。"""
    state = _make_state()
    out = asyncio.run(execute(
        "extract_keywords", state,
        text="the quick brown fox jumps over the lazy dog the cat the bird",
        top_n=3,
    ))
    assert out["status"] == "success"
    assert len(out["keywords"]) == 3
    assert out["keywords"][0]["word"] == "the"       # 出现最多
    assert out["keywords"][0]["count"] >= 4
    assert out["total_words"] > 0


def test_dummy_tool_empty_text():
    """空 text 返合规 error。"""
    out = asyncio.run(execute("extract_keywords", _make_state(), text=""))
    assert out["status"] == "error"
    assert "非空" in out["error"]


def test_dummy_tool_invalid_top_n():
    """top_n 超出范围返合规 error。"""
    out = asyncio.run(execute(
        "extract_keywords", _make_state(),
        text="hello world", top_n=999,
    ))
    assert out["status"] == "error"
    # 区间由 schema 声明、派发口核；报错列出上限，工具体内没有这道手写检查。
    assert "50" in out["error"] and out["parameter_violations"]


def test_dummy_tool_accepts_unknown_kwargs():
    """LLM 多传字段 → executor 用 **_ 兜底，不 TypeError。"""
    out = asyncio.run(execute(
        "extract_keywords", _make_state(),
        text="hello world",
        unknown_field="LLM 多传的",
        another_extra=42,
    ))
    # 不崩 → 通过
    assert out["status"] == "success"


def test_dummy_tool_no_matching_words():
    """text 没匹配单词 → 返 success 但 keywords 为空。"""
    out = asyncio.run(execute("extract_keywords", _make_state(),
                                text="123 !! ?? @@"))
    assert out["status"] == "success"
    assert out["keywords"] == []
    assert "没匹配到" in out["note"]
