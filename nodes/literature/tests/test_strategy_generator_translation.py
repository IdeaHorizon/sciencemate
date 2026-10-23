from __future__ import annotations

import asyncio
import json

import pytest

from nodes.literature.tools import strategy_generator
from platform_runtime import RequestError, run_literature_search


def _structured(english_query: str) -> str:
    return json.dumps({
        "research_question": "测试问题",
        "english_query": english_query,
        "search_queries": [{
            "dimension": "core_problem",
            "query": english_query,
            "sources": ["crossref"],
            "weight": 1.0,
        }],
        "filters": {"min_citations": 0, "sort_by": "relevance"},
    })


def test_invalid_structured_translation_uses_later_valid_line(monkeypatch) -> None:
    replies = iter([
        "翻译如下：\nEnglish query: lattice Boltzmann irregular particle sedimentation",
    ])

    def fake_call(*_args, diagnostics=None, **_kwargs):
        if diagnostics is not None:
            diagnostics["status"] = "ok"
            diagnostics["http_status"] = 200
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed("三维不规则颗粒沉降模拟")

    assert strategy["english_query"] == "lattice Boltzmann irregular particle sedimentation"
    assert diagnostics["status"] == "fallback_translated"
    assert diagnostics["translation_request_status"] == "ok"
    assert diagnostics["translation_status"] == "valid"
    assert diagnostics["translation_valid"] is True


def test_semantic_validation_requires_ascii_anchors_exclusions_and_detail() -> None:
    source = "比较 PM2.5 与 PM10 对儿童哮喘的影响，排除成人研究。"

    assert strategy_generator._query_semantic_issues(
        source,
        "PM2.5 AND PM10 AND childhood asthma impact NOT adult studies",
    ) == []
    issues = strategy_generator._query_semantic_issues(source, "particulate matter asthma")
    assert any(issue.startswith("missing_ascii_anchors:") for issue in issues)
    assert "missing_exclusion" in issues
    assert "missing_relation" in issues


def test_http_success_with_invalid_translation_is_not_semantic_success(monkeypatch) -> None:
    replies = iter([
        "我无法翻译这个问题。",
        "仍然无法翻译。",
        "还是无法翻译。",
    ])

    def fake_call(*_args, diagnostics=None, **_kwargs):
        if diagnostics is not None:
            diagnostics["status"] = "ok"
            diagnostics["http_status"] = 200
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed("中文原题")

    assert strategy["english_query"] == ""
    assert strategy["search_queries"] == []
    assert diagnostics["status"] == "translation_failed"
    assert diagnostics["translation_request_status"] == "ok"
    assert diagnostics["translation_status"] == "invalid_output"
    assert diagnostics["translation_valid"] is False
    assert diagnostics["translation_output_has_cjk"] is True
    assert len(diagnostics["translation_output_sha256"]) == 16


def test_second_translation_prompt_recovers_from_invalid_first_output(monkeypatch) -> None:
    replies = iter([
        "殖民时期馆藏返还",
        "colonial museum collection repatriation ethics law case studies",
    ])
    calls = []

    def fake_call(*_args, diagnostics=None, **kwargs):
        calls.append(kwargs.get("max_tokens", 1024))
        if diagnostics is not None:
            diagnostics["status"] = "ok"
            diagnostics["http_status"] = 200
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed("殖民时期馆藏返还的伦理问题")

    assert strategy["english_query"].startswith("colonial museum collection")
    assert diagnostics["status"] == "fallback_translated_recovery"
    assert diagnostics["translation_status"] == "valid"
    assert diagnostics["translation_attempts"] == 2
    assert calls[-1] == 4096


def test_third_translation_prompt_recovers_after_two_invalid_outputs(monkeypatch) -> None:
    replies = iter([
        "",
        "仍然只返回中文",
        "wheat drought root traits AND yield water use efficiency NOT triticale-only studies",
    ])
    timeouts = []

    def fake_call(*_args, diagnostics=None, **kwargs):
        timeouts.append(kwargs.get("read_timeout"))
        if diagnostics is not None:
            diagnostics["status"] = "ok"
            diagnostics["http_status"] = 200
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed(
        "干旱条件下小麦根系性状与产量的关系，排除只研究小黑麦的论文"
    )

    assert strategy["english_query"].startswith("wheat drought root traits")
    assert diagnostics["status"] == "fallback_translated_recovery"
    assert diagnostics["translation_attempts"] == 3
    assert timeouts == [25.0, 30.0, 35.0]


def test_long_boolean_translation_is_not_rejected_as_explanatory_prose() -> None:
    query = (
        "(wheat OR Triticum aestivum) AND drought AND "
        "(root depth OR root biomass OR root mass OR root traits) AND "
        "(yield OR grain yield) AND (water use efficiency OR WUE) AND "
        "(field experiment OR field study OR review) AND "
        "(wheat triticale comparison OR comparative study) NOT triticale-only"
    )

    assert len(query) > 250
    assert strategy_generator._extract_english_query(query) == query


def test_rule_fallback_never_labels_chinese_as_english_query(monkeypatch) -> None:
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed("古DNA与人口迁移")

    assert strategy["english_query"] == ""
    assert strategy["search_queries"] == []
    assert diagnostics["status"] == "translation_failed"


def test_search_reports_translation_failure_instead_of_searching_chinese_internationally(
    monkeypatch,
) -> None:
    calls = []

    def invalid_strategy(question: str):
        calls.append(question)
        return {
            "english_query": "",
            "search_queries": [],
        }, {
            "status": "translation_failed",
            "translation_status": "invalid_output",
            "translation_request_status": "ok",
        }

    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", invalid_strategy)
    with pytest.raises(RequestError) as exc_info:
        asyncio.run(run_literature_search({"query": "古DNA如何解释人口迁移？"}))

    assert len(calls) == 1
    assert exc_info.value.code == "strategy_translation_failed"
    assert exc_info.value.details["translation_status"] == "invalid_output"
