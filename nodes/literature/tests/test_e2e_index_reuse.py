from __future__ import annotations

from types import SimpleNamespace

import pytest

from nodes.literature.tools.search_engines import Paper, SearchResults


def complete_paper(title: str, doi: str, *, source: str = "crossref") -> Paper:
    return Paper(
        title=title,
        doi=doi,
        source=source,
        authors=["Ada Example"],
        abstract=f"Official abstract for {title}.",
        year=2026,
        venue="Journal of Tests",
        url=f"https://doi.org/{doi}",
    )


class FakeLocalIndex:
    def __init__(self, cached: dict[str, Paper]):
        self.cached = cached
        self.persisted: list[Paper] = []

    def get_papers_by_identity(self, candidates: list[Paper]) -> dict[str, Paper]:
        keys = {str(p.doi or "").lower() for p in candidates}
        return {key: paper for key, paper in self.cached.items() if key in keys}

    def add_papers(self, papers: list[Paper], search_query: str = ""):
        self.persisted = list(papers)
        return len(papers), 0


class FakeManager:
    def __init__(self, remote: list[Paper], cached: dict[str, Paper]):
        self.remote = remote
        self.local_index = FakeLocalIndex(cached)
        self.search_kwargs = {}
        self.enriched: list[str] = []

    async def search_all(self, query: str, **kwargs):
        self.search_kwargs = kwargs
        return SearchResults(
            self.remote,
            requested_sources=kwargs["enabled_sources"],
            attempted_sources={"crossref"},
        )

    async def _enrich_metadata_cross_source(self, papers, enabled_sources):
        self.enriched = [p.doi for p in papers]
        for paper in papers:
            if not paper.abstract:
                paper.abstract = "Enriched official abstract."
                paper.metadata_provenance["abstract"] = "crossref"


@pytest.mark.asyncio
async def test_e2e_remote_candidates_reuse_index_without_local_recall(monkeypatch):
    import nodes.literature.tools.search_papers as sp

    remote_hit = Paper(title="Remote hit", doi="10.1/hit", source="crossref")
    remote_miss = Paper(
        title="Remote miss",
        doi="10.1/miss",
        source="crossref",
        authors=["Grace Example"],
        year=2026,
        venue="Journal of Tests",
        url="https://doi.org/10.1/miss",
    )
    unrelated_local = complete_paper("Unrelated local", "10.1/unrelated")
    manager = FakeManager(
        [remote_hit, remote_miss],
        {
            "10.1/hit": complete_paper("Remote hit", "10.1/hit"),
            "10.1/unrelated": unrelated_local,
        },
    )
    monkeypatch.setattr(sp, "SearchManager", lambda: manager)

    out = await sp._search_papers(
        SimpleNamespace(hook_state={}),
        query="remote topic",
        include_arxiv=False,
        include_s2=False,
        include_openalex=False,
        include_pubmed=False,
        include_crossref=True,
        include_cnki=False,
    )

    assert out["status"] == "success"
    assert {paper["doi"] for paper in out["papers"]} == {"10.1/hit", "10.1/miss"}
    assert "10.1/unrelated" not in {paper["doi"] for paper in out["papers"]}
    assert manager.search_kwargs["include_local_catalog"] is False
    assert manager.search_kwargs["use_query_cache"] is False
    assert manager.search_kwargs["enrich_metadata"] is False
    assert manager.search_kwargs["persist_results"] is False
    assert manager.enriched == ["10.1/miss"]
    assert out["index_reuse"]["hit_count"] == 1
    assert out["index_reuse"]["complete_hit_count"] == 1
    assert out["metadata_enrichment"]["target_count"] == 1
    assert {paper.doi for paper in manager.local_index.persisted} == {
        "10.1/hit",
        "10.1/miss",
    }


@pytest.mark.asyncio
async def test_e2e_incomplete_index_gets_one_bounded_retry_per_run(monkeypatch):
    import nodes.literature.tools.search_papers as sp

    cached = complete_paper("Incomplete cached", "10.1/incomplete")
    cached.abstract = ""
    manager = FakeManager(
        [Paper(title="Incomplete cached", doi="10.1/incomplete", source="crossref")],
        {"10.1/incomplete": cached},
    )
    monkeypatch.setattr(sp, "SearchManager", lambda: manager)
    state = SimpleNamespace(hook_state={})

    first = await sp._search_papers(state, query="incomplete cached")
    assert first["index_reuse"]["incomplete_hit_count"] == 1
    assert manager.enriched == ["10.1/incomplete"]

    manager.enriched = []
    await sp._search_papers(state, query="incomplete cached")
    assert manager.enriched == []

@pytest.mark.asyncio
async def test_e2e_index_persistence_failure_does_not_hide_search_results(monkeypatch):
    import nodes.literature.tools.search_papers as sp

    paper = complete_paper("Search still succeeds", "10.1/persist-failure")
    manager = FakeManager([paper], {})

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("database is locked")

    manager.local_index.add_papers = fail_persistence
    monkeypatch.setattr(sp, "SearchManager", lambda: manager)

    out = await sp._search_papers(
        SimpleNamespace(hook_state={}),
        query="search still succeeds",
        include_arxiv=False,
        include_s2=False,
        include_openalex=False,
        include_pubmed=False,
        include_crossref=True,
        include_cnki=False,
    )

    assert out["status"] == "success"
    assert out["papers"][0]["doi"] == "10.1/persist-failure"
    assert out["index_persistence"] == {
        "attempted": True,
        "status": "error",
        "added_or_updated": 0,
        "skipped": 0,
        "error_type": "RuntimeError",
    }
