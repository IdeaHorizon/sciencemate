"""`supersede_closure_draft` 与旧名 `supersede_experiment_log` 的过渡期兼容（#923）。

这个工具否定的是**误建的 closure 草稿**，不是 experiment_log 本身；`fix/879-minimal`
因此改名为 `supersede_closure_draft`。但仓库根
`tests/test_instructed_tools_are_in_whitelist.py` 断言的是旧名，而该文件不在本节点
scope 内（#923）。两名并存让任一版本的断言都能通过。

本文件钉住三件事：两名指向同一实现、schema 完全一致、正名在描述里可辨认。
**删除条件**：#923 落地后删掉旧名与本文件里对应的断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import yaml  # noqa: E402

from core.loader import load_harness  # noqa: E402
from core.tool_registry import _REGISTRY  # noqa: E402
import nodes.experiment.tools.contract_audit  # noqa: E402,F401  触发注册

_CANON = "supersede_closure_draft"
_LEGACY = "supersede_experiment_log"


def _defs() -> dict:
    """已注册的 ToolDefinition，按名字索引。"""
    return _REGISTRY.tools


def _executors() -> dict:
    """名字 → 实现函数。"""
    return _REGISTRY.executors


def test_both_names_are_on_the_node_tool_surface():
    tools = load_harness("experiment").tools
    assert _CANON in tools, "正名必须在工具面上"
    assert _LEGACY in tools, (
        "旧名是过渡期兼容，#923 落地前不得移除 —— 根测试仍断言它")


def test_both_names_share_one_implementation():
    ex = _executors()
    assert ex[_CANON] is ex[_LEGACY], (
        "两名必须指向同一个实现 —— 别名不得是第二份逻辑")


def test_both_names_share_one_schema():
    d = _defs()
    assert d[_CANON].parameters_schema == d[_LEGACY].parameters_schema, (
        "参数 schema 必须一致，否则两名会有不同的调用契约")
    assert d[_CANON].risk_level == d[_LEGACY].risk_level
    assert d[_CANON].allowed_node_types == d[_LEGACY].allowed_node_types


def test_the_canonical_name_is_identifiable_from_the_description():
    """模型看到两个同义工具时，得能分辨哪个是正名。

    标注放在**旧名**上（而不是正名上）：正名的描述保持干净，旧名首句就说明
    自己是过渡期旧名并指向正名。这样 #923 落地删掉旧名之后，正名的描述不需要
    再改一次。
    """
    d = _defs()
    legacy_desc = d[_LEGACY].description
    assert "旧名" in legacy_desc and _CANON in legacy_desc, (
        "旧名的描述首句应说明它是过渡期旧名并指向正名")
    assert "旧名" not in d[_CANON].description, (
        "正名的描述不应被过渡期措辞污染 —— 删掉别名时它不必跟着改")


def test_harness_records_the_deletion_condition():
    """兼容层必须写明删除条件，否则会变成永久债。"""
    raw = (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    assert "#923" in raw and "过渡期旧名" in raw, (
        "harness 里应标注旧名是过渡期兼容及其删除条件")
    # 顺带确认 YAML 仍可解析、两名都在。
    tools = yaml.safe_load(raw)["tools"]
    assert {_CANON, _LEGACY} <= set(tools)
