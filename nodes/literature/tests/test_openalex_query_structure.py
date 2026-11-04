import httpx
import pytest

from nodes.literature.tools.search_engines import OpenAlexSearch


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected", [
    ("synthetic control method", '"synthetic control method"'),
    ('"immersed boundary" AND "lattice Boltzmann" AND sedimentation',
     '"immersed boundary" AND "lattice Boltzmann" AND sedimentation'),
    ("immersed boundary method lattice Boltzmann particle sedimentation",
     "immersed boundary method lattice Boltzmann particle sedimentation"),
    ('("machine learning" OR "statistical learning") NOT "deep learning"',
     '("machine learning" OR "statistical learning") NOT "deep learning"'),
])
async def test_search_preserves_concepts_instead_of_quoting_whole_query(query, expected):
    seen = []

    def handle(request):
        seen.append(request.url.params["filter"])
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = OpenAlexSearch(client)
        engine.cfg = {**engine.cfg, "enabled": True}
        await engine.search(query)
    assert seen == [f"title_and_abstract.search:{expected}"]


@pytest.mark.asyncio
async def test_issn_and_date_filters_remain_journal_scoped():
    seen = []

    def handle(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = OpenAlexSearch(client)
        engine.cfg = {**engine.cfg, "enabled": True}
        await engine.search("issn:1234-5678", date_from="2026-01-01")
    assert seen[0]["filter"].startswith(
        "primary_location.source.issn:1234-5678,from_publication_date:2026-01-01,"
    )
    assert seen[0]["sort"] == "publication_date:desc"
