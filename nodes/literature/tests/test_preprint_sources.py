from __future__ import annotations

import pytest

from nodes.literature.tools.search_engines import EuropePmcPreprintSearch
from platform_runtime import _publication_category


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get(self, url, *, params, timeout):
        self.calls.append((url, dict(params), timeout))
        return FakeResponse({"resultList": {"result": self.results}})


def record(publisher: str) -> dict:
    return {
        "id": "PPR1",
        "doi": "10.1101/2026.09.01.123456",
        "title": "Blood <i>cells</i> in the ageing brain",
        "authorList": {"author": [{"fullName": "Ada Example"}]},
        "firstPublicationDate": "2026-09-01",
        "abstractText": "<h4>Abstract</h4><p>Original abstract text.</p>",
        "bookOrReportDetails": {"publisher": publisher},
        "citedByCount": 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "publisher"),
    [("biorxiv", "bioRxiv"), ("medrxiv", "medRxiv")],
)
async def test_preprint_source_is_server_scoped_and_classified(server, publisher) -> None:
    client = FakeClient([record(publisher), record("Research Square")])

    papers = await EuropePmcPreprintSearch(client, server).search(
        "blood cells ageing brain",
        limit=20,
        date_from="2026-08-01",
    )

    assert len(client.calls) == 1
    params = client.calls[0][1]
    assert "SRC:PPR" in params["query"]
    assert f'PUBLISHER:"{publisher}"' in params["query"]
    assert "FIRST_PDATE:[2026-08-01 TO " in params["query"]
    assert len(papers) == 1
    paper = papers[0]
    assert paper.source == server
    assert paper.title == "Blood cells in the ageing brain"
    assert paper.abstract == "Abstract Original abstract text."
    assert paper.pub_type == "posted-content"
    assert _publication_category(paper) == "预印本"
