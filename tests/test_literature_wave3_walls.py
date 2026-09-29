"""判决拆除三波（literature）：删的数量下限/非空闸 + 降格的如实记账。

每条测试对应清单里一处改动，把墙加回去必转红：
- archive_papers:382  零篇论文照归档，paper_count=0 如实进 metadata（D 删）
- classify_papers:87  1 篇也能分类（D 删）；:166 漏派论文回退+记 unassigned（E 降格）
- finalize_evidence_package:38  evidence_summary 空 → advisory 随包走（D 降格）
- search_papers:45  query 空串由 schema minLength 在派发口拒（C schema）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import nodes.literature.tools  # noqa: F401  注册工具
from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State.new(node_type="literature", base_dir=tmp_path)


def _run(state: State, tool: str, **kw):
    return asyncio.run(execute(tool, state, **kw))


def test_archive_papers_records_an_empty_index_honestly(state):
    result = _run(state, "archive_papers", papers_json="[]", name="empty_index")
    assert result["status"] == "success", result
    assert result["paper_count"] == 0
    record = state.read_artifact(result["artifact_id"])
    assert record["metadata"]["paper_count"] == 0
    assert json.loads(record["content"])["paper_count"] == 0


def test_classify_papers_accepts_a_single_paper_and_defaults_unassigned(monkeypatch, state):
    """1 篇不再被「至少 2 篇」拦；模型漏派的 paper_id 回退到首主题并如实记账。"""
    from nodes.literature.tools import classify_papers as cp

    calls: list[str] = []

    async def fake_llm(prompt: str, max_tokens: int = 2000) -> dict:
        calls.append(prompt[:40])
        if "Read the complete abstracts" in prompt:
            # 两篇论文，模型只派了 paper 0；paper 1 漏派
            return {"themes": [{"id": 1, "name": "Thermostats"}, {"id": 2, "name": "Other"}],
                    "assignments": [{"paper_id": 0, "theme_ids": [2]}]}
        if "Unify independently derived" in prompt:
            return {"theme_labels": {"1": "Thermostats", "2": "Other"},
                    "mapping": {"0:1": 1, "0:2": 2}}
        return {"key_findings": ["kf"], "open_questions": ["oq"]}

    monkeypatch.setattr(cp, "_call_llm_json", fake_llm)

    single = _run(state, "classify_papers",
                  papers_json=json.dumps([{"title": "only one", "abstract": "a"}]))
    assert single["status"] == "success", single
    assert single["total_papers"] == 1
    assert single["paper_theme_assignments"] == [{"paper_index": 0, "theme_ids": [2]}]

    two = _run(state, "classify_papers",
               papers_json=json.dumps([{"title": "p0", "abstract": "a"},
                                       {"title": "p1", "abstract": "b"}]))
    assert two["status"] == "success", two
    assert two["unassigned_paper_ids_defaulted"] == [1]
    assert two["paper_theme_assignments"][1] == {"paper_index": 1, "theme_ids": [1]}
    assert two["total_papers"] == 2


def test_classify_papers_zero_papers_is_an_empty_result_without_llm_calls(monkeypatch, state):
    from nodes.literature.tools import classify_papers as cp

    async def boom(prompt: str, max_tokens: int = 2000) -> dict:
        raise AssertionError("零篇论文不该打模型")

    monkeypatch.setattr(cp, "_call_llm_json", boom)
    result = _run(state, "classify_papers", papers_json="[]")
    assert result["status"] == "success", result
    assert result["total_papers"] == 0
    assert result["clusters"] == []


def test_finalize_evidence_package_records_missing_summary_as_advisory(state):
    result = _run(
        state, "finalize_evidence_package",
        name="pkg", queries_json='["q"]', included_papers_json="[]",
        evidence_summary="   ", unknowns_json='["nothing found"]',
    )
    assert result["status"] == "success", result
    record = state.read_artifact(result["artifact_id"])
    content = json.loads(record["content"])
    assert content["evidence_summary"] == ""
    assert any("evidence_summary 为空" in a for a in content["advisories"]), content


def test_search_papers_empty_query_is_rejected_by_schema_at_dispatch(state):
    result = _run(state, "search_papers", query="   ")
    assert result["status"] == "error"
    assert result.get("parameter_violations"), result
    assert "query" in result["error"]
