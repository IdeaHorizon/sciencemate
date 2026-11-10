"""UI/E2E parity at the real retrieval pipeline, with deterministic providers."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import platform_runtime as runtime
from nodes.literature.tools import academic_search, academic_strategy, local_index, search_engines
from nodes.literature.tools import academic_tool, platform_sources
from nodes.literature.tools.search_papers import _dispatch_search_papers
from nodes.literature.tools.search_engines import Paper, SearchResults


@pytest.fixture
def providers(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_LITERATURE_ACCESS_FILE", raising=False)
    index = local_index.LocalPaperIndex(tmp_path / "index.sqlite3")
    monkeypatch.setattr(local_index, "get_index", lambda: index)
    queries, enrichment, crossref_calls = [], [], []
    rows = [Paper(title=f"Particle sedimentation in viscous fluids {i}",
                  doi=f"10.1/particle-{i}", source="arxiv", year=2025,
                  authors=["Test Author"], venue="Research", url=f"https://example.invalid/{i}",
                  abstract="Particle sedimentation in viscous fluids was studied experimentally.")
            for i in range(45)]
    rows += [deepcopy(rows[0]), Paper(title="Editorial Board", doi="10.1/editorial", source="arxiv"),
             Paper(title="Optical encryption in perovskites", doi="10.1/unrelated", source="arxiv")]

    class Manager:
        local_index = index
        def __init__(self, **kwargs): pass
        async def search_all(self, query, **kwargs):
            queries.append((query, kwargs))
            assert not kwargs["use_query_cache"] and not kwargs["include_local_catalog"]
            assert not kwargs["persist_results"] and not kwargs["enrich_metadata"]
            return SearchResults(deepcopy(rows), requested_sources=kwargs["enabled_sources"], attempted_sources=["arxiv"])
        def _merge_results(self, left, right):
            return list({p.doi: p for p in [*left, *right]}.values())
        async def _enrich_metadata_cross_source(self, papers, sources):
            enrichment.extend(p.doi for p in papers)

    class Crossref:
        def __init__(self, client): pass
        async def search_title(self, query, **kwargs):
            crossref_calls.append("title")
            return []
        async def search(self, query, **kwargs):
            crossref_calls.append("journal")
            return []

    plan = {"english_query": "particle sedimentation", "concept_groups": [["particle sedimentation"]],
            "bilingual_queries": [{"label": "主题", "en": "particle sedimentation", "zh": "颗粒沉降"}]}
    monkeypatch.setattr(academic_strategy, "generate_academic_strategy", lambda q: (deepcopy(plan), {}))
    monkeypatch.setattr(search_engines, "SearchManager", Manager)
    monkeypatch.setattr(search_engines, "CrossrefSearch", Crossref)
    return SimpleNamespace(index=index, queries=queries, enrichment=enrichment, crossref=crossref_calls, rows=rows)


@pytest.mark.asyncio
async def test_ui_and_e2e_share_admission_order_and_cache(providers):
    ui = await runtime.run_literature_search({"query": "颗粒沉降"})
    state = SimpleNamespace(hook_state={})
    e2e = await _dispatch_search_papers(state, "颗粒沉降", max_results=30)
    assert e2e["status"] == "success"
    assert len(ui["papers"]) == e2e["candidate_count"] == 45
    assert all(p["doi"] not in {"10.1/editorial", "10.1/unrelated"} for p in ui["papers"])
    assert [p["doi"] for p in e2e["papers"]] == [p["doi"] for p in ui["papers"][:30]]
    assert [p["quality_score"] for p in e2e["papers"]] == [p["score"] for p in ui["papers"][:30]]
    assert e2e["strategy_diagnostics"]["metadata_reuse"]["cached_count"] == 47
    calls = len(providers.queries)
    page = await academic_tool.read_candidates(state, e2e["search_id"], e2e["next_offset"], 30)
    assert len(providers.queries) == calls
    assert [p["doi"] for p in page["papers"]] == [p["doi"] for p in ui["papers"][30:]]
    assert page["next_offset"] is None
    assert any(w["code"] == "platform_sources_unavailable" for w in e2e["warnings"])


@pytest.mark.asyncio
async def test_query_round_keeps_every_original_keyword_group_without_seven_category_passes(providers,monkeypatch):
    from nodes.literature.tools import academic_paging
    original=['particle sedimentation','particle sedimentation AND (fluid OR liquid)']
    plan={"english_query":original[0],"concept_groups":[["particle sedimentation"]],
          "bilingual_queries":[{"label":str(i),"en":q,"zh":"颗粒沉降"+str(i)} for i,q in enumerate(original)]}
    monkeypatch.setattr(academic_strategy,'generate_academic_strategy',lambda q:(deepcopy(plan),{}))
    seen=[]
    async def collect(manager,query,sources,limits,position,**kwargs):
        seen.append((query,kwargs.get('category'),position))
        return SearchResults(deepcopy(providers.rows),attempted_sources=['arxiv']), {'arxiv':{'offset':100}}
    monkeypatch.setattr(academic_paging,'collect_page',collect)
    request={'query':'颗粒沉降分组检索','limit':None,'per_category_limit':200,'pagination_scope':'query'}
    first=await academic_search.run_literature_search(request)
    assert [item[0] for item in seen]==original[:1]
    assert len(first['continuation']['groups'])==2
    assert list(first['continuation']['groups'].values()).count(None)==1
    second=await academic_search.run_literature_search({**request,'continuation':first['continuation']})
    assert [item[0] for item in seen]==original and all(item[1] is None for item in seen)
    assert all(value is not None for value in second['continuation']['groups'].values())
    assert first['decomposed_queries']==second['decomposed_queries']==original
    assert len(second['continuation']['accepted']['预印本'])==45


@pytest.mark.asyncio
async def test_ui_and_e2e_preserve_cached_abstract_origin(providers):
    providers.rows[0].abstract = ""
    providers.rows[45].abstract = ""  # Duplicate candidate in another response.
    providers.index.add_papers([Paper(
        title="Particle sedimentation in viscous fluids 0", doi="10.1/particle-0",
        abstract="Particle sedimentation in viscous fluids was studied experimentally.",
        metadata_provenance={"abstract": "pubmed"},
    )])
    ui = await runtime.run_literature_search({"query": "颗粒沉降"})
    paper = next(p for p in ui["papers"] if p["doi"] == "10.1/particle-0")
    assert paper["metadata_provenance"]["abstract"] == "pubmed"
    state = SimpleNamespace(hook_state={})
    e2e = await _dispatch_search_papers(state, "颗粒沉降", max_results=30)
    pool = state.hook_state[academic_tool.POOL_KEY][e2e["search_id"]]["papers"]
    assert next(p for p in pool if p["doi"] == "10.1/particle-0")["metadata_provenance"]["abstract"] == "pubmed"


@pytest.mark.asyncio
async def test_disabled_crossref_never_runs_title_or_journal_recall(providers):
    await academic_search.run_literature_search({"query": "颗粒沉降"}, enabled_sources={"arxiv"})
    assert providers.crossref == []
    assert all(options["enabled_sources"] == {"arxiv"} for _, options in providers.queries)
    with pytest.raises(academic_search.AcademicSearchError, match="检索来源"):
        await academic_search.run_literature_search({"query": "test"}, enabled_sources=set())


@pytest.mark.asyncio
async def test_default_e2e_preserves_ui_exact_title_cache_policy(providers):
    title = "Sedimentation of a tracer"
    providers.index.add_papers([Paper(title=title, doi="10.1/cached-title", source="semantic_scholar")])
    ui = await runtime.run_literature_search({"query": title})
    state = SimpleNamespace(hook_state={})
    result = await academic_tool.search(state, title)
    pool = state.hook_state[academic_tool.POOL_KEY][result["search_id"]]["papers"]
    assert "10.1/cached-title" in {p["doi"] for p in pool}
    assert [p["doi"] for p in pool] == [p["doi"] for p in ui["papers"]]


@pytest.mark.asyncio
async def test_pipeline_error_contract_preserved(monkeypatch):
    async def fail(*args, **kwargs):
        raise academic_search.AcademicSearchError("strategy_translation_failed", "invalid plan", {"stage": "strategy"})
    monkeypatch.setattr(academic_search, "run_literature_search", fail)
    with pytest.raises(runtime.RequestError) as raised:
        await runtime.run_literature_search({"query": "x"})
    assert raised.value.code == "strategy_translation_failed"
    assert raised.value.details == {"stage": "strategy"}
    result = await _dispatch_search_papers(SimpleNamespace(hook_state={}), "x")
    assert result["status"] == "error" and result["code"] == "strategy_translation_failed"


@pytest.mark.asyncio
async def test_private_source_work_is_cancelled_with_search(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def collect(*args, **kwargs):
        started.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    async def core(request, progress, **kwargs):
        progress({"stage": "search_plan", "search_plan": [{"zh": "颗粒沉降"}]})
        await started.wait()
        raise asyncio.CancelledError()
    monkeypatch.setattr(platform_sources, "collect_sources", collect)
    monkeypatch.setattr(academic_search, "run_literature_search", core)
    with pytest.raises(asyncio.CancelledError):
        await academic_tool.search(SimpleNamespace(hook_state={}), "x")
    assert cancelled.is_set()


def test_model_budget_does_not_expand_when_reading_candidate_pages():
    state = SimpleNamespace(hook_state={})
    snapshot = {"query": "q", "search_id": "s", "diagnostics": {},
                "papers": [{"doi": f"10.1/{i}", "title": str(i)} for i in range(160)]}
    for offset in range(0, 120, 30):
        page = academic_tool._page(state, snapshot, offset=offset, limit=30)
        assert len(page["papers"]) == 30
    assert page["context_budget_remaining"] == 0 and page["next_offset"] is None
    assert academic_tool._page(state, snapshot, offset=120)["papers"] == []


@pytest.mark.asyncio
async def test_cnki_reuses_managed_jobs_and_keeps_school_records_separate(monkeypatch):
    monkeypatch.setenv("HARNESS_LITERATURE_ACCESS_FILE", "/test/capability.json")
    calls = []
    async def call(op, payload):
        calls.append((op, payload))
        if op == "school":
            return {"papers": [{"title": "Local paper", "doi": "10.1/private", "school_holdings": ["School"]}]}
        if op == "cnki_availability":
            return {"available": True}
        assert op == "cnki_submit"
        return {"id": "job", "state": "completed", "result": {"papers": [
            {"title": "知网题录", "doi": "10.1/cnki", "source": "cnki", "score": 0}]}}
    monkeypatch.setattr(platform_sources, "call", call)
    result = await platform_sources.collect_sources("问题", [{"zh": "颗粒沉降"}], cnki=True, school=True)
    assert [op for op, _ in calls].count("cnki_submit") == 1
    assert {p["doi"] for p in result["papers"]} == {"10.1/private", "10.1/cnki"}
    assert result["warnings"] == []


def test_federation_matches_ui_doi_or_normalized_title_year_rule():
    from shared.contrib.literature_federation import merge_channels, merge_school_papers
    public = [{"title": "Particle-Sedimentation", "doi": "10.1/x", "year": 2025, "score": .8},
              {"title": "Different", "doi": "10.1/y", "year": 2026, "score": .7}]
    cnki = [{"title": "Particle Sedimentation", "year": 2025, "abstract": "Abstract", "score": 0},
            {"title": "Alias", "doi": "https://doi.org/10.1/X", "year": 2025, "score": 0}]
    result = merge_channels(public, cnki)
    assert len(result) == 2 and result[0]["score"] == .8
    assert result[0]["abstract"] == "Abstract" and "abstract" not in public[0]
    assert [p["federated_rank"] for p in result] == [0, 1]
    school = [{"title": "Private", "doi": "10.1/s", "school_holdings": ["School"],
               "school_fulltext_url": None, "metadata_provenance": {"school_library": "School"}}]
    mixed = merge_school_papers({"papers": public*3}, school, limit=20)
    assert mixed["papers"][4]["doi"] == "10.1/s"


@pytest.mark.asyncio
async def test_pending_cnki_can_resume_without_replanning_or_public_retrieval(providers, monkeypatch):
    async def pending(*args, **kwargs):
        return {"papers": [], "warnings": [{"code": "cnki_pending"}],
                "sources": {"cnki": {"jobs": [{"id": "owned-job", "state": "working"}]}}}
    monkeypatch.setattr(platform_sources, "collect_sources", pending)
    state = SimpleNamespace(hook_state={})
    first = await academic_tool.search(state, "颗粒沉降")
    retrievals = len(providers.queries)
    async def finished(op, payload):
        assert op == "cnki_status" and payload == {"job_id": "owned-job"}
        return {"id": "owned-job", "state": "completed", "count": 1, "result": {"papers": [
            {"title": "颗粒沉降实验研究", "doi": "10.1/new-cnki", "source": "cnki", "year": 2025,
             "score": 0, "score_breakdown": {}}]}}
    monkeypatch.setattr(platform_sources, "call", finished)
    page = await academic_tool.read_candidates(state, first["search_id"], first["next_offset"], refresh_sources=True)
    assert len(providers.queries) == retrievals
    cnki = next(p for p in page["papers"] if p["doi"] == "10.1/new-cnki")
    assert cnki["quality_score"] is None and cnki["score_status"] == "not_scored"
    assert not any(w["code"] == "cnki_pending" for w in page["warnings"])
    assert page["candidate_count"] == 46


@pytest.mark.asyncio
async def test_unlimited_pipeline_rotates_groups_and_passes_their_own_cursors(providers, monkeypatch):
    from nodes.literature.tools import academic_paging
    plan = {"english_query": "particle sedimentation", "concept_groups": [["particle sedimentation"]],
            "bilingual_queries": [
                {"label": "主题", "en": "particle sedimentation", "zh": "颗粒沉降"},
                {"label": "实验", "en": "particle sedimentation AND experiment", "zh": "颗粒沉降 * 实验"}]}
    monkeypatch.setattr(academic_strategy, "generate_academic_strategy", lambda q: (deepcopy(plan), {}))
    calls = []
    async def page(manager, query, sources, limits, position):
        calls.append((query, position))
        batch = deepcopy(providers.rows[:1])
        batch[0].doi = f"10.1/page-{len(calls)}"
        remaining = {"arxiv": {"offset": 30}} if len(calls) == 1 else {}
        return SearchResults(batch, requested_sources=sources, attempted_sources=["arxiv"]), remaining
    monkeypatch.setattr(academic_paging, "collect_page", page)
    cursor = None
    ids = set()
    for _ in range(3):
        result = await academic_search.run_literature_search(
            {"query": "颗粒沉降", "limit": None, "continuation": cursor})
        ids.update(p["doi"] for p in result["papers"])
        cursor = result["continuation"]
    assert cursor is None and len(ids) == 3
    assert calls == [("particle sedimentation", None),
                     ("particle sedimentation AND experiment", None),
                     ("particle sedimentation", {"arxiv": {"offset": 30}})]
    assert providers.crossref == ["title"]


def test_school_merge_has_no_implicit_200_result_ceiling():
    from shared.contrib.literature_federation import merge_school_papers
    public = [{"title": f"Public {i}", "doi": f"10.1/p-{i}"} for i in range(250)]
    school = [{"title": f"School {i}", "doi": f"10.1/s-{i}"} for i in range(70)]
    result = merge_school_papers({"papers": public}, school, limit=None)
    assert len(result["papers"]) == 320


@pytest.mark.asyncio
async def test_category_paging_skips_a_full_journal_lane_but_continues_theses(providers,monkeypatch):
    from nodes.literature.tools import academic_paging
    from nodes.literature.tools.academic_paging import query_key
    from nodes.literature.tools.academic_categories import CATEGORIES
    calls=[]
    async def page(manager,query,sources,limits,position,*,category):
        calls.append(category)
        rows=deepcopy(providers.rows[:3])
        for p in rows:
            p.source='crossref'
            p.pub_type='journal-article' if category=='期刊文章' else 'dissertation'
        return SearchResults(rows,requested_sources=sources,attempted_sources=['crossref']),{'crossref':{'offset':3}}
    monkeypatch.setattr(academic_paging,'collect_page',page)
    request={'query':'颗粒沉降','limit':None,'per_category_limit':2}
    first=await academic_search.run_literature_search(request)
    assert first['total']==2
    cursor=first['continuation']
    assert len(cursor['groups'])==len(CATEGORIES)-1
    assert query_key('期刊文章\nparticle sedimentation') not in cursor['groups']
    # A journal cap must not consume either the Chinese journal or thesis slot.
    assert query_key('学位论文\nparticle sedimentation') in cursor['groups']
    cursor['groups']={query_key('学位论文\nparticle sedimentation'):None}
    providers.rows=[p for p in providers.rows if p.doi not in {row['doi'] for row in first['papers']}]
    second=await academic_search.run_literature_search({**request,'continuation':cursor})
    assert calls==['期刊文章','学位论文']
    assert second['continuation'] is None
