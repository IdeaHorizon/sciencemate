import pytest

import platform_runtime as runtime
from nodes.literature.tools import search_engines, strategy_generator
from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper


@pytest.mark.asyncio
async def test_existing_missing_metadata_is_reused_across_searches(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))
    monkeypatch.setitem(strategy_generator.LLM_CONFIG, "api_base", "")
    index = LocalPaperIndex(str(tmp_path / "index.sqlite3"))
    index.add_papers([Paper(
        title="Particle sedimentation in a flowing fluid", doi="10.1/existing",
        url="https://example.invalid/existing", year=2020, venue="Research Journal",
    )])
    calls = []

    class Results(list):
        def audit_dict(self):
            return {"attempted_sources": ["crossref"], "warnings": []}

    class Manager:
        local_index = index

        def __init__(self, **kwargs):
            pass

        async def search_all(self, query, **kwargs):
            # New object instances emulate independent bridge processes.
            return Results([
                Paper(title="Particle sedimentation in a flowing fluid", doi="10.1/existing",
                      url="https://example.invalid/existing", year=2020, venue="Research Journal"),
                Paper(title="Particle sedimentation simulation", doi="10.1/new",
                      url="https://example.invalid/new", year=2021, venue="Research Journal"),
            ])

        def _merge_results(self, left, right):
            return list({paper.doi: paper for paper in left + right}.values())

        async def _enrich_metadata_cross_source(self, papers, sources):
            calls.extend(p.doi for p in papers)
            # Searches were attempted but no abstract/author metadata exists.

    class Crossref:
        def __init__(self, client):
            pass

        async def search_title(self, *args, **kwargs):
            return []

        async def search(self, *args, **kwargs):
            return []

    def strategy(question, **kwargs):
        return {"english_query": "particle sedimentation",
                "search_queries": [{"query": "particle sedimentation"}]}, {}

    monkeypatch.setattr(search_engines, "SearchManager", Manager)
    monkeypatch.setattr(search_engines, "CrossrefSearch", Crossref)
    monkeypatch.setattr(strategy_generator, "generate_strategy_detailed", strategy)
    monkeypatch.setattr(runtime, "_local_literature_assets", lambda papers: {})
    first = await runtime.run_literature_search({"query": "particle sedimentation"})
    assert calls and set(calls) == {"10.1/new"}
    assert len(first["papers"]) == 2
    calls.clear()
    second = await runtime.run_literature_search({"query": "particle sedimentation studies"})
    assert calls == []
    assert len(second["papers"]) == 2
    assert second["strategy_diagnostics"]["metadata_reuse"] == {
        "cached_count": 2, "cached_with_missing_fields": 2,
        "enrichment_target_count": 0, "final_enrichment_target_count": 0,
    }
