"""Admission explanations must agree with selection, including exact-title exceptions."""
import pytest
from platform_runtime import _academic_exclusion_reason, _select_academic_search_results


@pytest.mark.parametrize('paper,kwargs,reason', [
    ({'title': 'Editorial Board', 'exact_title_match': True}, {}, 'nonresearch'),
    ({'title': 'Original study', 'exact_title_match': True}, {}, None),
    ({'title': 'Original study', 'score_breakdown': {'raw': {'relevance': 0}}}, {}, 'relevance'),
    ({'title': 'Original study', 'score_breakdown': {'raw': {'relevance': 1}}}, {'core_terms': ['sepsis']}, 'anchor'),
    ({'title': 'Sepsis prediction', 'score_breakdown': {'raw': {'relevance': 1}}}, {'mandatory_facets': [['sepsis'], ['children']]}, 'facets'),
    ({'title': 'Sepsis prediction', 'score_breakdown': {'raw': {'relevance': 1}}}, {'core_terms': ['sepsis']}, None),
])
def test_admission_reason_matches_selection(paper, kwargs, reason):
    assert _academic_exclusion_reason(paper, **kwargs) == reason
    selected, excluded = _select_academic_search_results([paper], limit=100, **kwargs)
    assert excluded == int(reason is not None)
    assert bool(selected) == (reason is None)


def test_result_limit_is_not_rejection():
    papers = [{'title': f'Original study {i}', 'exact_title_match': True} for i in range(3)]
    selected, excluded = _select_academic_search_results(papers, limit=1)
    assert len(selected) == 1
    assert excluded == 0


def test_property_forms_keep_relevant_evidence_without_dropping_topic_gate():
    from platform_runtime import _paper_matches_academic_facets
    facets = [['coral bleaching'], ['symbiodiniaceae composition change'], ['heat tolerance']]
    relevant = {
        'title': 'Symbiodiniaceae assemblage composition and coral bleaching response',
        'abstract': 'Corals retain thermotolerant symbionts during thermal stress.',
    }
    assert _paper_matches_academic_facets(relevant, facets)
    assert not _paper_matches_academic_facets({
        'title': 'Machine learning for coral bleaching assessment',
        'abstract': 'Statistical estimates of sea temperature and heat stress.',
    }, facets)
    assert _paper_matches_academic_facets({
        'title': 'Drug resistant bacteria',
    }, [['drug resistance'], ['bacteria']])


@pytest.mark.asyncio
async def test_source_overlap_respects_language_policy_and_preserves_diagnostics(monkeypatch):
    import asyncio
    import platform_runtime as runtime
    from nodes.literature.tools import search_engines, strategy_generator

    source_started, journal_started = asyncio.Event(), asyncio.Event()
    journal_queries = []

    class Results(list):
        def audit_dict(self):
            return {'attempted_sources': ['crossref'], 'warnings': []}

    class Manager:
        local_index = None

        def __init__(self, **kwargs):
            pass

        async def search_all(self, query, **kwargs):
            if query == 'coral bleaching':
                source_started.set()
                await asyncio.wait_for(journal_started.wait(), 1)
            else:
                assert kwargs['enabled_sources'] == {'cnki'}
            return Results()

        def _merge_results(self, left, right):
            return left + right

    class Crossref:
        def __init__(self, client):
            pass

        async def search_title(self, *args, **kwargs):
            return []

        async def search(self, query, **kwargs):
            journal_queries.append(query)
            journal_started.set()
            await asyncio.wait_for(source_started.wait(), 1)
            return []

    async def translate(*args):
        return {'cached': 0, 'translated': 0, 'status': 'ok'}

    monkeypatch.setattr(search_engines, 'SearchManager', Manager)
    monkeypatch.setattr(search_engines, 'CrossrefSearch', Crossref)
    monkeypatch.setattr(strategy_generator, 'generate_strategy_detailed', lambda q: (
        {'english_query': 'coral bleaching'}, {}))
    monkeypatch.setattr(runtime, '_academic_strategy_queries', lambda *a: ['coral bleaching', '珊瑚白化'])
    monkeypatch.setattr(runtime, '_add_chinese_titles', translate)
    monkeypatch.setattr(runtime, '_local_literature_assets', lambda papers: {})
    result = await runtime.run_literature_search({'query': 'coral bleaching', 'limit': 100})
    assert journal_queries == ['coral bleaching']
    assert result['strategy_diagnostics']['admission']['candidate_count'] == 0
    assert result['strategy_diagnostics']['admission']['excluded_by_reason'] == {}
    assert set(result['timings']) >= {'source_search', 'candidate_enrichment', 'title_translation', 'total'}


@pytest.mark.asyncio
async def test_title_batches_bound_parallelism_keep_success_and_reuse_cache(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from core import llm
    from platform_runtime import _add_chinese_titles
    active = peak = 0
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, messages, **kwargs):
            nonlocal active, peak
            rows = json.loads(messages[-1].content)
            calls.append(rows)
            assert len(rows) <= 20
            assert len(messages) == 2
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if rows[0]['id'] == 20:
                    raise TimeoutError()
                return SimpleNamespace(content=json.dumps([
                    {'id': r['id'], 'title_zh': '译文：' + r['title']} for r in rows]))
            finally:
                active -= 1

    class Index:
        cache = {}

        def get_title_translations(self, titles):
            return {t: self.cache[t] for t in titles if t in self.cache}

        def save_title_translations(self, values):
            self.cache.update(values)

    monkeypatch.setattr(llm, 'LLMClient', Client)
    papers = [{'title': f'Paper title {i}'} for i in range(65)]
    index = Index()
    result = await _add_chinese_titles(papers, index)
    assert result['status'] == 'partial'
    assert result['translated'] == 45
    assert result['failed_batches'] == 1
    assert 1 < peak <= 3
    assert len(index.cache) == 45
    calls.clear()
    result = await _add_chinese_titles(papers, index)
    assert result['cached'] == 45
    assert sum(len(rows) for rows in calls) == 20
    assert result['translated'] == 20
