from __future__ import annotations

import pytest

from nodes.literature.tools.search_engines import Paper, PubMedSearch, SearchManager


class FakeResponse:
    def __init__(self, *, data=None, text=""):
        self._data = data
        self.text = text
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, *, params, timeout):
        self.calls.append((url, dict(params), timeout))
        if url.endswith("esearch.fcgi"):
            return FakeResponse(data={"esearchresult": {"idlist": ["12345"]}})
        return FakeResponse(
            text="""<?xml version="1.0"?>
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>12345</PMID>
              <Article>
                <Journal>
                  <JournalIssue><PubDate><Year>2026</Year><Month>Sep</Month><Day>2</Day></PubDate></JournalIssue>
                  <Title>Journal of Useful Medicine</Title>
                </Journal>
                <ArticleTitle>Blood <i>cells</i> in the ageing brain</ArticleTitle>
                <Abstract>
                  <AbstractText Label="BACKGROUND">Original background.</AbstractText>
                  <AbstractText Label="RESULTS">Original results.</AbstractText>
                </Abstract>
                <AuthorList>
                  <Author><ForeName>Ada</ForeName><LastName>Example</LastName></Author>
                </AuthorList>
                <ELocationID EIdType="doi">10.1000/example</ELocationID>
              </Article>
              <MeshHeadingList>
                <MeshHeading><DescriptorName>Brain</DescriptorName></MeshHeading>
              </MeshHeadingList>
            </MedlineCitation>
            <PubmedData>
              <ArticleIdList>
                <ArticleId IdType="pubmed">12345</ArticleId>
                <ArticleId IdType="doi">10.1000/example</ArticleId>
              </ArticleIdList>
            </PubmedData>
          </PubmedArticle>
        </PubmedArticleSet>"""
        )


@pytest.mark.asyncio
async def test_pubmed_search_uses_esearch_then_batch_efetch_without_key(monkeypatch):
    client = FakeClient()
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setattr(
        PubMedSearch, "_wait_for_request_slot", classmethod(lambda cls, min_interval: _noop())
    )

    papers = await PubMedSearch(client).search("ageing brain", limit=20)

    assert len(client.calls) == 2
    assert client.calls[0][0].endswith("esearch.fcgi")
    assert client.calls[1][0].endswith("efetch.fcgi")
    assert all("api_key" not in params for _, params, _ in client.calls)
    assert client.calls[1][1]["id"] == "12345"
    assert len(papers) == 1
    paper = papers[0]
    assert paper.source == "pubmed"
    assert paper.pub_type == "journal-article"
    assert paper.title == "Blood cells in the ageing brain"
    assert paper.abstract == "BACKGROUND: Original background.\nRESULTS: Original results."
    assert paper.authors == ["Ada Example"]
    assert paper.doi == "10.1000/example"
    assert paper.pub_date == "2026-09-02"
    assert paper.fields_of_study == ["Brain"]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_pubmed_date_filter_is_sent_to_esearch(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        PubMedSearch, "_wait_for_request_slot", classmethod(lambda cls, min_interval: _noop())
    )

    await PubMedSearch(client).search(
        "microplastic health effects",
        limit=5,
        date_from="2026-08-20",
    )

    term = client.calls[0][1]["term"]
    assert "2026/08/20[Date - Publication]" in term
    assert "[Date - Publication]" in term


@pytest.mark.asyncio
async def test_crossref_result_uses_pubmed_to_fill_official_abstract(monkeypatch):
    async def fake_pubmed_search(self, query, limit=20, date_from=None):
        assert query == "10.1000/est-paper[doi]"
        assert limit == 1
        return [
            Paper(
                title="Same EST paper",
                abstract="The official PubMed abstract.",
                authors=["Ada Example"],
                venue="Environmental Science & Technology",
                year=2026,
                url="https://pubmed.ncbi.nlm.nih.gov/12345/",
                doi="10.1000/est-paper",
                source="pubmed",
            )
        ]

    monkeypatch.setattr(PubMedSearch, "search", fake_pubmed_search)
    manager = object.__new__(SearchManager)
    manager._progress_callback = None
    paper = Paper(
        title="Same EST paper",
        abstract="",
        authors=["Existing Author"],
        venue="Environmental Science & Technology",
        year=2026,
        url="https://doi.org/10.1000/est-paper",
        doi="10.1000/est-paper",
        source="crossref",
    )

    await manager._enrich_metadata_cross_source([paper], {"pubmed"})

    assert paper.abstract == "The official PubMed abstract."
    assert paper.metadata_provenance["abstract"] == "pubmed"
    # 已有字段不应被补齐来源覆盖。
    assert paper.authors == ["Existing Author"]
    assert paper.url == "https://doi.org/10.1000/est-paper"


@pytest.mark.asyncio
async def test_openalex_doi_enrichment_is_separate_from_keyword_search(monkeypatch):
    import nodes.literature.tools.search_engines as se

    class Response:
        status_code = 200

        def json(self):
            return {
                "abstract_inverted_index": {"Official": [0], "OpenAlex": [1], "abstract": [2]},
                "authorships": [],
                "publication_year": 2026,
                "doi": "https://doi.org/10.1000/openalex-fill",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *args, **kwargs):
            assert url == ("https://api.openalex.org/works/https://doi.org/10.1000/openalex-fill")
            return Response()

    monkeypatch.setattr(se.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    manager = object.__new__(SearchManager)
    manager._progress_callback = None
    paper = Paper(
        title="Paper missing a Crossref abstract",
        doi="10.1000/openalex-fill",
        source="crossref",
        authors=["Existing Author"],
        venue="Existing Journal",
        year=2026,
        url="https://doi.org/10.1000/openalex-fill",
    )

    await manager._enrich_metadata_cross_source([paper], {"openalex"})

    assert paper.abstract == "Official OpenAlex abstract"
    assert paper.metadata_provenance["abstract"] == "openalex"
