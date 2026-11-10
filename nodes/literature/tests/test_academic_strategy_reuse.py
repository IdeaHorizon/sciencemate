import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from nodes.literature.tools import academic_strategy, strategy_generator
from platform_runtime import _academic_boolean_facets


@pytest.fixture
def cached_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))
    monkeypatch.setattr(strategy_generator, "LLM_CONFIG", {
        "api_base": "https://example.invalid/v1", "api_key": "test-secret-credential",
        "model": "test-model",
    })
    return tmp_path


def test_repeated_concurrent_question_reuses_one_interpretation(monkeypatch, cached_environment):
    calls = []
    question = "浸入边界法格子玻尔兹曼在水利沉降领域颗粒沉降的研究"

    def generate(source, **kwargs):
        calls.append(source)
        assert kwargs["require_concepts"]
        query = "immersed boundary method AND lattice Boltzmann AND particle sedimentation"
        return {
            "english_query": query, "search_queries": [{"query": query}],
        }, {"translation_valid": True}

    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(academic_strategy.generate_academic_strategy, [question, question]))
    assert calls == [question]
    assert results[0][0] == results[1][0]
    assert sum(result[1]["strategy_cache_hit"] for result in results) == 1
    files = list(cached_environment.rglob("*.json"))
    assert len(files) == 1
    assert "test-secret-credential" not in files[0].read_text()
    assert "test-secret-credential" not in files[0].name


def test_model_or_account_change_does_not_reuse_another_strategy(monkeypatch, cached_environment):
    calls = []

    def generate(source, **kwargs):
        calls.append(source)
        return {"english_query": "quantum computing", "search_queries": [{"query": "quantum computing"}]}, {}

    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", generate)
    academic_strategy.generate_academic_strategy("量子计算")
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "model", "other-model")
    academic_strategy.generate_academic_strategy("量子计算")
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_key", "other-account")
    academic_strategy.generate_academic_strategy("量子计算")
    assert len(calls) == 3


def test_planning_instruction_change_invalidates_previous_interpretation(monkeypatch, cached_environment):
    from nodes.literature.tools import bilingual_strategy
    calls = []
    def generate(source, **kwargs):
        calls.append(source)
        return {'english_query':'quantum computing', 'search_queries':[{'query':'quantum computing'}]}, {}
    monkeypatch.setattr(strategy_generator, 'generate_strategy_detailed', generate)
    academic_strategy.generate_academic_strategy('量子计算')
    academic_strategy.generate_academic_strategy('量子计算')
    assert len(calls) == 1
    monkeypatch.setattr(bilingual_strategy, 'PROMPT', bilingual_strategy.PROMPT + '\nUpdated concept grouping instructions.')
    academic_strategy.generate_academic_strategy('量子计算')
    assert len(calls) == 2


@pytest.mark.parametrize('mode', ['focused', 'exploratory'])
def test_cached_interpretation_recompiles_chinese_without_another_model_call(monkeypatch,cached_environment,mode):
    from nodes.literature.tools import bilingual_strategy as bs
    data = {'retrieval_mode': mode, 'core_concepts': [
        {'zh': ['电子病历'], 'en': ['electronic health records', 'EHR']}],
        'exclude_concepts': [{'zh': ['病例报告'], 'en': ['case reports']}],
        'search_groups': [{'label': '核心', 'additional_concepts': []}]}
    if mode == 'exploratory':
        data['search_groups'].append({'label': '外部验证', 'additional_concepts': [
            {'zh': ['外部验证'], 'en': ['external validation']}]})
    question = 'Electronic health records research, excluding case reports'
    original = bs.compile_plan(json.dumps(data), question)
    # A valid cached plan from before acronym sharing still has its concepts.
    for group in original['bilingual_queries']:
        group['zh'] = group['zh'].replace('("电子病历" + "EHR")', '"电子病历"')
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        return json.loads(json.dumps(original)), {'translation_valid': True}
    monkeypatch.setattr(strategy_generator, 'generate_strategy_detailed', generate)
    academic_strategy.generate_academic_strategy(question)
    current, diagnostics = academic_strategy.generate_academic_strategy(question)
    assert calls == [1] and diagnostics['strategy_cache_hit']
    assert current['english_query'] == original['english_query']
    for before, after in zip(original['bilingual_queries'], current['bilingual_queries']):
        assert before['en'] == after['en']
        assert '("电子病历" + "EHR")' in after['zh']
        assert ' - ("病例报告")' in after['zh']
        assert ('"外部验证"' in after['zh']) == (after['label'] == '外部验证')


def test_cached_query_with_unknown_concepts_is_regenerated(monkeypatch,cached_environment):
    from nodes.literature.tools import bilingual_strategy as bs
    original = bs.compile_plan(json.dumps({'core_concepts': [{'zh':['主题'], 'en':['topic']}],
        'search_groups':[{'additional_concepts':[]}]}), 'topic research')
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        return json.loads(json.dumps(original)), {'translation_valid': True}
    monkeypatch.setattr(strategy_generator, 'generate_strategy_detailed', generate)
    academic_strategy.generate_academic_strategy('topic research')
    path = next(cached_environment.rglob('*.json'))
    stored = json.loads(path.read_text())
    stored['strategy']['bilingual_queries'][0]['en'] = '"different topic"'
    path.write_text(json.dumps(stored))
    current, diagnostics = academic_strategy.generate_academic_strategy('topic research')
    assert calls == [1, 1] and not diagnostics['strategy_cache_hit']
    assert current['bilingual_queries'] == original['bilingual_queries']


def test_failure_is_not_cached_and_expired_success_regenerates(monkeypatch, cached_environment):
    calls = []

    def generate(source, **kwargs):
        calls.append(source)
        if len(calls) == 1:
            return {"english_query": "", "search_queries": []}, {"translation_valid": False}
        return {"english_query": "quantum computing", "search_queries": [{"query": "quantum computing"}]}, {}

    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", generate)
    academic_strategy.generate_academic_strategy("量子计算")
    assert not list(cached_environment.rglob("*.json"))
    academic_strategy.generate_academic_strategy("量子计算")
    path = next(cached_environment.rglob("*.json"))
    row = json.loads(path.read_text())
    row["created_at"] = 1
    path.write_text(json.dumps(row))
    academic_strategy.generate_academic_strategy("量子计算")
    assert len(calls) == 3


def test_missing_and_cannot_silently_disable_concept_filters(monkeypatch):
    replies = iter([
        "immersed boundary method lattice Boltzmann particle sedimentation hydraulic sedimentation",
        json.dumps({"concept_groups": [
            ["immersed boundary method"], ["lattice Boltzmann"],
            ["particle sedimentation"], ["hydraulic sedimentation"],
        ]}),
    ])

    def call(*_args, diagnostics, **_kwargs):
        diagnostics["status"] = "ok"
        return next(replies)

    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    monkeypatch.setattr(strategy_generator, "_call_llm", call)
    strategy, diagnostics = strategy_generator.generate_strategy_detailed(
        "浸入边界法格子玻尔兹曼在水利沉降领域颗粒沉降的研究", require_concepts=True,
    )
    assert diagnostics["translation_attempts"] == 2
    assert "missing_concept_structure" in diagnostics["translation_attempt_1_semantic_issues"]
    assert _academic_boolean_facets(strategy["english_query"]) == [
        ["immersed boundary method"], ["lattice boltzmann"], ["particle sedimentation"],
    ]


def test_explicit_single_concept_is_valid_without_artificial_and(monkeypatch):
    def call(*_args, diagnostics, **_kwargs):
        diagnostics["status"] = "ok"
        return json.dumps({"concept_groups": [["reproducibility of computational scientific research"]]})

    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "https://example.invalid")
    monkeypatch.setattr(strategy_generator, "_call_llm", call)
    strategy, diagnostics = strategy_generator.generate_strategy_detailed(
        "计算科学研究的可重复性这个概念", require_concepts=True,
    )
    assert diagnostics["translation_valid"]
    assert " AND " not in strategy["english_query"]


def test_structured_concepts_preserve_alternatives_and_exclusions():
    query, groups = strategy_generator._structured_concept_query(json.dumps({
        "concept_groups": [["distributed computing", "distributed processing"], ["communication latency"]],
        "exclude": ["simulation-only studies"],
    }))
    assert groups[0] == ["distributed computing", "distributed processing"]
    assert '"distributed computing" OR "distributed processing"' in query
    assert 'NOT ("simulation-only studies")' in query
    assert strategy_generator._structured_concept_query('{"concept_groups":[["仍为中文"]]}') == ("", [])
