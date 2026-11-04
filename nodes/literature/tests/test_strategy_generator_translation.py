from __future__ import annotations

import asyncio
import json

import pytest

from nodes.literature.tools import strategy_generator
from platform_runtime import (
    RequestError,
    _academic_core_terms,
    _academic_english_query_ok,
    _academic_strategy_queries,
    _academic_term_matches,
    run_literature_search,
)


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


def test_planning_anchor_checklist_uses_the_same_scientific_scope_as_validation():
    source='研究PM2.5与PM10的差异，使用CAE和AI，300 K下的X射线与C语言处理。预期成果：SCI论文1篇。'
    anchors=strategy_generator._query_ascii_anchors(source)
    assert anchors==['PM2.5','PM10','CAE','AI','300','K','X','C']
    assert 'SCI' not in anchors and '1' not in anchors
    assert not strategy_generator._query_semantic_issues(
        source,'PM2.5 AND PM10 AND CAE AND AI AND 300 K AND X-ray AND C language')


def test_proposal_deliverables_are_not_retrieval_constraints_but_scientific_terms_are():
    source=('研究300K下金属疲劳和自愈建模，兼顾CAE集成。'
            '预期成果：本构模型1套、SCI论文1—2篇、验证报告1份。'
            '可行性：对应AICON前沿方向，使用CPU计算。')
    cleaned=strategy_generator._semantic_anchor_source(source)
    assert 'SCI' not in cleaned and 'AICON' not in cleaned
    assert all(term in cleaned for term in ['300K','CAE','CPU','疲劳','自愈'])
    candidate='metal fatigue AND self-healing AND modeling AND 300K AND CAE AND CPU'
    assert strategy_generator._query_semantic_issues(source,candidate)==[]
    assert 'missing_ascii_anchors:CAE' in strategy_generator._query_semantic_issues(source,candidate.replace('CAE',''))
    for explicit in ['检索SCI论文', '仅检索2026年以来的金属疲劳论文', '研究300K下金属疲劳', '分析AICON方法']:
        assert strategy_generator._semantic_anchor_source(explicit)==explicit


@pytest.mark.parametrize("source,english", [
    (
        "比较 MPI 与 UCX 的通信延迟，排除模拟研究",
        "MPI AND UCX communication latency comparison NOT simulation studies",
    ),
    (
        "比较宋代诗歌与元代诗歌，排除教材",
        "Song dynasty poetry AND Yuan dynasty poetry comparison NOT textbooks",
    ),
])
def test_constraints_work_across_research_fields(source: str, english: str) -> None:
    assert strategy_generator._query_semantic_issues(source, english) == []


@pytest.mark.parametrize("source,english", [
    ("布拉格", "Bragg"),
    ("布拉格反射", "Bragg reflection"),
    ("量子计算", "quantum computing"),
    ("什么是布拉格？", "Bragg"),
    ("什么是AI？", "AI"),
    ("什么是pH？", "pH"),
    ("mRNA疫苗", "mRNA vaccine"),
    ("单细胞RNA测序", "single-cell RNA sequencing"),
    ("AI在医学中的应用", "AI medical applications"),
    ("MPI通信", "MPI communication"),
    ("海洋微塑料", "marine microplastics"),
    ("宋代诗歌", "Song dynasty poetry"),
    ("低温超导", "low temperature superconductivity"),
])
def test_short_topic_accepts_complete_translation_without_extra_call(
    monkeypatch, source: str, english: str,
) -> None:
    calls = []

    def fake_call(*_args, diagnostics=None, **_kwargs):
        calls.append(1)
        if diagnostics is not None:
            diagnostics["status"] = "ok"
        return english

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")

    strategy, diagnostics = strategy_generator.generate_strategy_detailed(source)

    assert strategy["english_query"] == english
    assert diagnostics["translation_attempts"] == 1
    assert calls == [1]
    assert _academic_english_query_ok(english, source)


@pytest.mark.parametrize("source,first,corrected", [
    ("布拉格反射", "Bragg", "Bragg reflection"),
    ("光合作用", "photosynthesis", "photosynthesis"),
    ("癌症治疗方法", "cancer treatment", "cancer treatment methods"),
    ("mRNA疫苗", "mRNA", "mRNA vaccine"),
    ("X射线", "X-ray", "X-ray"),
    ("量子计算有哪些应用？", "quantum computing", "quantum computing applications"),
])
def test_suspiciously_short_translation_is_checked_once(
    monkeypatch, source: str, first: str, corrected: str,
) -> None:
    replies = iter([first, corrected])
    calls = []

    def fake_call(*_args, diagnostics=None, **_kwargs):
        calls.append(1)
        if diagnostics is not None:
            diagnostics["status"] = "ok"
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")

    strategy, diagnostics = strategy_generator.generate_strategy_detailed(source)

    assert strategy["english_query"] == corrected
    assert diagnostics["translation_attempts"] == 2
    assert diagnostics["short_query_confirmation"] == "confirmed"
    assert calls == [1, 1]


def test_valid_short_query_survives_unavailable_confirmation(monkeypatch) -> None:
    replies = iter(["photosynthesis", ""])

    def fake_call(*_args, diagnostics=None, **_kwargs):
        if diagnostics is not None:
            diagnostics["status"] = "ok"
        return next(replies)

    monkeypatch.setattr(strategy_generator, "_call_llm", fake_call)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")

    strategy, diagnostics = strategy_generator.generate_strategy_detailed("光合作用")

    assert strategy["english_query"] == "photosynthesis"
    assert diagnostics["short_query_confirmation"] == "first_valid_query_retained"
    assert diagnostics["translation_attempts"] == 2


def test_source_structure_drives_detail_and_one_word_admission() -> None:
    assert strategy_generator._query_semantic_issues("布拉格", "Bragg") == []
    assert strategy_generator._query_semantic_issues(
        "量子计算", "quantum computing"
    ) == []
    assert strategy_generator._query_semantic_issues(
        "“布拉格反射”？", "Bragg reflection"
    ) == []
    assert "not_usable_english" in strategy_generator._query_semantic_issues(
        "布拉格反射在什么条件下出现？", "Bragg"
    )
    assert not _academic_english_query_ok("AI", "AI在医学中的应用有哪些？")
    assert strategy_generator._query_semantic_issues(
        "如何治疗癌症？", "cancer treatment"
    ) == []
    assert strategy_generator._query_semantic_issues(
        "如何研究宋代诗歌？", "Song dynasty poetry research"
    ) == []
    assert "missing_ascii_anchors:X" in strategy_generator._query_semantic_issues(
        "X射线", "ray"
    )
    assert strategy_generator._query_semantic_issues("X射线", "X-ray") == []


def test_one_word_query_flows_into_search_and_two_letter_anchor_is_exact() -> None:
    strategy = {
        "english_query": "Bragg",
        "search_queries": [{"dimension": "core_problem", "query": "Bragg"}],
    }
    assert _academic_strategy_queries(strategy, "布拉格") == ["Bragg", "布拉格"]
    assert _academic_core_terms("pH") == ["ph"]
    assert _academic_english_query_ok("pH", "酸碱度")
    assert _academic_term_matches("ph", "soil ph measurements")
    assert not _academic_term_matches("ph", "physics measurements")


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
    assert timeouts == [35.0, 30.0, 35.0]


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
    monkeypatch, tmp_path,
) -> None:
    from nodes.literature.tools.search_engines import CrossrefSearch
    calls = []

    async def empty_title(*_args, **_kwargs):
        return []

    monkeypatch.setattr(CrossrefSearch, "search_title", empty_title)
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))

    def invalid_strategy(question: str, **_kwargs):
        calls.append(question)
        return {
            "english_query": "",
            "search_queries": [],
        }, {
            "status": "translation_failed",
            "translation_status": "invalid_output",
            "translation_request_status": "ok",
            "translation_attempts": 1,
            "translation_attempt_1_http_status": 200,
        }

    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", invalid_strategy)
    with pytest.raises(RequestError) as exc_info:
        asyncio.run(run_literature_search({"query": "古DNA如何解释人口迁移？"}))

    assert len(calls) == 1
    assert exc_info.value.code == "strategy_translation_failed"
    assert exc_info.value.details["translation_status"] == "invalid_output"
    assert "内容校验" in str(exc_info.value)
    assert "HTTP 200" not in str(exc_info.value)


@pytest.mark.parametrize("http_status", [400, 401, 403, 429])
def test_upstream_rejection_is_not_retried_with_different_prompts(monkeypatch, http_status):
    calls = []

    def rejected(*_args, diagnostics=None, **_kwargs):
        calls.append(1)
        diagnostics.update(status="http_error", http_status=http_status, elapsed=2.5)
        return None

    monkeypatch.setattr(strategy_generator, "_call_llm", rejected)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed(
        "浸入边界法格子玻尔兹曼在水利沉降领域颗粒沉降的研究"
    )

    assert strategy["english_query"] == ""
    assert len(calls) == 1
    assert diagnostics["translation_attempts"] == 1
    assert diagnostics["translation_attempt_1_http_status"] == http_status
    assert diagnostics["translation_attempt_1_elapsed"] == 2.5
    assert diagnostics["translation_attempt_1_semantic_issues"] == []
    assert diagnostics["retry_stopped_reason"] == "upstream_rejected"


def test_transport_failure_allows_only_one_recovery(monkeypatch):
    calls = []

    def timed_out(*_args, diagnostics=None, **_kwargs):
        calls.append(1)
        diagnostics.update(status="read_timeout", elapsed=25.0)
        return None

    monkeypatch.setattr(strategy_generator, "_call_llm", timed_out)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    _, diagnostics = strategy_generator.generate_strategy_detailed("颗粒沉降")

    assert len(calls) == 2
    assert diagnostics["translation_attempts"] == 2
    assert diagnostics["translation_attempt_2_semantic_issues"] == []
    assert diagnostics["retry_stopped_reason"] == "transport_recovery_exhausted"


def test_transient_timeout_can_recover_to_valid_query(monkeypatch):
    calls = []

    def recovered(*_args, diagnostics=None, **_kwargs):
        calls.append(1)
        diagnostics["status"] = "read_timeout" if len(calls) == 1 else "ok"
        return None if len(calls) == 1 else "particle sedimentation"

    monkeypatch.setattr(strategy_generator, "_call_llm", recovered)
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    strategy, diagnostics = strategy_generator.generate_strategy_detailed("颗粒沉降")

    assert strategy["english_query"] == "particle sedimentation"
    assert diagnostics["translation_attempts"] == 2
    assert diagnostics["translation_valid"]


def test_http_error_diagnostics_keep_status_without_credentials(monkeypatch):
    import httpx

    secret = "private-test-credential-without-standard-prefix"
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_key", secret)

    def handle(request):
        return httpx.Response(
            429, headers={"Retry-After": "60"},
            json={"error": {"message": f"rate limit for {secret}"}},
        )

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    diagnostics = {}
    assert strategy_generator._call_llm("translate", "颗粒沉降", diagnostics=diagnostics) is None
    assert diagnostics["http_status"] == 429
    assert diagnostics["retry_after_seconds"] == 60
    assert diagnostics["elapsed"] >= 0
    assert secret not in json.dumps(diagnostics)


@pytest.mark.parametrize("model,thinking", [("deepseek-v4-pro", {"type":"disabled"}), ("other-model", None)])
def test_keyword_planning_bounds_v4_reasoning_without_sending_vendor_options_to_other_models(monkeypatch, model, thinking):
    import httpx
    monkeypatch.setattr(strategy_generator, "LLM_CONFIG", {"api_base":"https://example.invalid", "api_key":"test", "model":model})
    def handle(request):
        body=json.loads(request.content)
        assert body.get("thinking") == thinking
        assert body.get("reasoning_effort") == ("none" if thinking else None)
        assert len(body["messages"]) == 2
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":"valid reply"}}]})
    original=httpx.Client
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert strategy_generator._call_llm("Plan keywords", "research query") == "valid reply"


def test_streaming_keyword_call_uses_consistent_nonreasoning_options_and_records_output(monkeypatch):
    import httpx
    monkeypatch.setattr(strategy_generator, "LLM_CONFIG", {
        "api_base": "https://example.invalid", "api_key": "test", "model": "deepseek-v4-pro"})
    def handle(request):
        body = json.loads(request.content)
        assert body["stream"] and len(body["messages"]) == 2
        assert body["thinking"] == {"type": "disabled"} and body["reasoning_effort"] == "none"
        chunks = [
            {"choices": [{"delta": {"content": "valid reply"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 0}}},
        ]
        data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    diagnostics = {}
    assert strategy_generator._call_llm("plan", "question", stream=True, diagnostics=diagnostics,
                                         read_timeout=45, total_timeout=90) == "valid reply"
    assert diagnostics["status"] == "ok" and diagnostics["stream_complete"]
    assert diagnostics["reasoning_tokens"] == 0 and diagnostics["content_chars"] == 11
    assert diagnostics["thinking_mode_requested"] == "disabled"
    assert diagnostics["reasoning_effort_requested"] == "none"


def test_http_success_with_reasoning_only_records_truncation_and_never_uses_reasoning_as_query(monkeypatch):
    import httpx
    monkeypatch.setattr(strategy_generator, "LLM_CONFIG", {"api_base":"https://example.invalid", "api_key":"test", "model":"reasoning-model"})
    def handle(request):
        return httpx.Response(200,json={"choices":[{"finish_reason":"length","message":{
            "content":None,"reasoning_content":"Internal draft is not a search query"}}],
            "usage":{"completion_tokens":4096,"completion_tokens_details":{"reasoning_tokens":4096}}})
    original=httpx.Client
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    diagnostics={}
    assert strategy_generator._call_llm('Plan keywords','研究问题',diagnostics=diagnostics,max_tokens=4096,read_timeout=35)==''
    assert diagnostics['output_truncated'] and diagnostics['content_chars']==0
    assert diagnostics['reasoning_tokens']==4096 and diagnostics['max_tokens_requested']==4096
    assert diagnostics['read_timeout_seconds']==35 and diagnostics['message_count']==2


@pytest.mark.parametrize("status,http_status,finish_reason,expected", [
    ("http_error", 429, None, "限流（HTTP 429）"),
    ("http_error", 401, None, "凭据或权限"),
    ("http_error", 503, None, "HTTP 503"),
    ("read_timeout", None, None, "响应超时"),
    ("ok", None, None, "未返回英文检索词正文"),
    ("ok", 200, None, "未返回英文检索词正文"),
    ("ok", 200, "length", "输出被截断"),
])
def test_runtime_reports_and_persists_actual_upstream_failure(
    monkeypatch, tmp_path, status, http_status, finish_reason, expected,
):
    from nodes.literature.tools.search_engines import CrossrefSearch

    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))

    async def empty_title(*_args, **_kwargs):
        return []

    def invalid_strategy(question, **_kwargs):
        return {"english_query": "", "search_queries": []}, {
            "status": "translation_failed", "translation_status": "request_failed",
            "translation_request_status": status, "translation_attempts": 1,
            "translation_attempt_1_request_status": status,
            "translation_attempt_1_http_status": http_status,
            "translation_attempt_1_elapsed": 25,
            "translation_attempt_1_output_chars": 0,
            "translation_attempt_1_finish_reason": finish_reason,
        }

    monkeypatch.setattr(CrossrefSearch, "search_title", empty_title)
    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", invalid_strategy)
    query = "浸入边界法格子玻尔兹曼在水利沉降领域颗粒沉降的研究"
    with pytest.raises(RequestError, match=expected):
        asyncio.run(run_literature_search({"query": query, "request_id": "failure-test"}))
    rows = [json.loads(p.read_text()) for p in (tmp_path / "search_cache/performance").glob("*.json")]
    assert len(rows) == 1
    assert rows[0]["query"] == query
    assert rows[0]["request_id"] == "failure-test"
    assert rows[0]["failed_stage"] == "strategy"
    assert rows[0]["strategy_diagnostics"]["translation_attempt_1_http_status"] == http_status
    assert rows[0]["timings"]["strategy_generation"] >= 0


@pytest.mark.parametrize("source,candidate,anchors", [
    ("查找 MOF 材料分离 CO₂ 和 N₂ 的综述论文", "MOF CO2 N2 separation review", ["MOF", "CO2", "N2"]),
    ("查找 MOF 材料分离 CO2 和 N2 的综述论文", "MOF CO₂ N₂ separation review", ["MOF", "CO2", "N2"]),
    ("研究H₂O₂的催化分解", "H2O2 catalytic decomposition", ["H2O2"]),
    ("分析ＣＯ₂与Ｎ₂分离方法", "CO2 and N2 separation methods", ["CO2", "N2"]),
])
def test_scientific_typography_preserves_whole_formula_anchors(source, candidate, anchors):
    assert strategy_generator._query_ascii_anchors(source) == anchors
    assert strategy_generator._query_semantic_issues(source, candidate) == []


@pytest.mark.parametrize("candidate", ["MOF CO N separation review", "MOF CO2 separation review"])
def test_scientific_typography_does_not_allow_losing_formula_digits(candidate):
    issues = strategy_generator._query_semantic_issues(
        "查找 MOF 材料分离 CO₂ 和 N₂ 的综述论文", candidate)
    assert any(issue.startswith("missing_ascii_anchors:") and "N2" in issue for issue in issues)


def test_diagnostics_do_not_replace_letters_from_an_invalid_short_key(monkeypatch):
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_key", "k")
    text = "kinase checkpoint signaling"
    assert strategy_generator._response_diagnostics(text)["output_preview"] == text
