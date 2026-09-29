"""判决拆除三波（hypothesis）：取值契约归 schema，派发口核一次。

从前这些工具各自手写「enum / 非空 / minItems」检查并返回 error；现在合法
取值只声明在 parameters_schema 里，`execute()` 按 schema 核取值并把合法值
列给模型。这里钉住的是「墙搬去了派发口」：直接调工具体不再拒绝，经
`execute()` 派发仍被机械拒绝且报错带 `parameter_violations`。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State.new(node_type="hypothesis", base_dir=tmp_path)


def _dispatch(state: State, tool: str, **kwargs):
    return asyncio.run(execute(tool, state, **kwargs))


@pytest.mark.parametrize(
    ("tool", "kwargs", "needle"),
    [
        ("stage_hypothesis_draft",
         {"artifact_type": "not_a_singleton", "content": "x"}, "artifact_type"),
        ("audit_hypothesis_vs_conclusions", {"hypotheses": []}, "hypotheses"),
        ("score_hypothesis_innovation", {"assessments": []}, "assessments"),
        ("cluster_hypothesis_candidates", {"hypotheses": []}, "hypotheses"),
        ("evolve_hypothesis",
         {"hypothesis": {"claim_text": "c"}, "mode": "bogus"}, "mode"),
        ("validate_hypothesis_outputs", {"checks": ["bogus_check"]}, "checks"),
        ("read_reference_paper", {"doi": "   "}, "doi"),
    ],
)
def test_dispatch_rejects_by_schema_and_lists_the_contract(state, tool, kwargs, needle):
    result = _dispatch(state, tool, **kwargs)
    assert result["status"] == "error", result
    assert result.get("parameter_violations"), result
    assert needle in result["error"], result["error"]
    assert result.get("parameters_schema"), "报错必须带 schema，合法值不靠猜"
