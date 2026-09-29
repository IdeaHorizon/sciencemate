from __future__ import annotations

import pytest

from nodes.literature.tools.journal_policy import decide, reset_cache
from nodes.literature.tools.search_engines import (
    ArxivSearch, CrossrefSearch, SemanticScholarSearch, _openaire_abstract,
    _direct_pdf_candidates, _pdf_original_abstract, _public_pdf_candidates,
    _publisher_landing_abstract,
)
from platform_runtime import (
    _clean_literature_text,
    _index_paper,
    _parse_ai_summaries,
    _parse_title_translations,
    _select_academic_search_results,
)


def test_offline_title_guess_is_not_treated_as_verified(tmp_path, monkeypatch) -> None:
    mapping = tmp_path / "map.tsv"
    mapping.write_text(
        "journal_name\t二级学科代码\t分类依据\t分类状态\n"
        "ACTA NEUROPATHOLOGICA\t010108\toffline_strict_title_anchor\tverified\n"
        "A Reviewed Journal\t100204\tmanual_source_verified\tmanually_verified\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))
    reset_cache()
    try:
        guessed = decide(
            venue="ACTA NEUROPATHOLOGICA",
            source="crossref",
            pub_type="journal-article",
            title="Neuropathology study",
        )
        assert guessed.accepted is False
        assert guessed.reason == "offline_mapping_not_manually_verified"

        reviewed = decide(
            venue="A Reviewed Journal",
            source="crossref",
            pub_type="journal-article",
            title="Clinical neurology study",
        )
        assert reviewed.accepted is True
        assert reviewed.domains == ("100204",)
    finally:
        reset_cache()


def test_arxiv_and_semantic_scholar_preserve_exact_publication_dates() -> None:
    arxiv = ArxivSearch(client=None)._parse(
        """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Fresh preprint</title><summary>Abstract</summary>
          <published>2026-08-26T12:34:56Z</published>
          <id>https://arxiv.org/abs/2608.12345</id>
          <author><name>Ada Example</name></author></entry>
        </feed>"""
    )[0]
    assert arxiv.pub_date == "2026-08-26T12:34:56Z"

    semantic = SemanticScholarSearch(client=None)._parse(
        {
            "data": [
                {
                    "title": "Fresh indexed paper",
                    "authors": [],
                    "year": 2026,
                    "publicationDate": "2026-08-25",
                    "externalIds": {},
                }
            ]
        }
    )[0]
    assert semantic.pub_date == "2026-08-25"


@pytest.mark.asyncio
async def test_arxiv_uses_each_generated_query_in_one_request() -> None:
    class Response:
        text = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" />'

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.calls = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    client = Client()
    results = await ArxivSearch(client).search(
        "blood-derived cells brain immune cells ageing",
        max_results=20,
    )

    assert results == []
    assert len(client.calls) == 1
    _, kwargs = client.calls[0]
    assert kwargs["params"]["search_query"] == (
        "blood-derived cells brain immune cells ageing"
    )
    assert kwargs["params"]["max_results"] == 20


def test_academic_ranking_prefers_full_original_intent_without_theme_dedup() -> None:
    def paper(title: str, abstract: str, score: float) -> dict:
        return {
            "title": title,
            "abstract": abstract,
            "score": score,
            "exact_title_match": False,
            "score_breakdown": {"raw": {"relevance": 0.8}},
        }

    broad_a = paper(
        "Resident microglia rather than peripheral macrophages in brain tumors",
        "Resident immune cells promote tumor vascularization.",
        0.92,
    )
    direct = paper(
        "Aging of the blood-brain barrier and peripheral immune cells",
        "In the ageing human brain, blood-derived immune cells may enter and "
        "replace resident microglia.",
        0.76,
    )
    broad_b = paper(
        "Resident microglia and peripheral macrophages in brain tumors",
        "A second paper on resident immune cells in tumors.",
        0.90,
    )

    ranked, excluded = _select_academic_search_results(
        [broad_a, direct, broad_b],
        limit=10,
        core_terms=[
            "blood", "cells", "replace", "immune", "ageing", "human", "brain"
        ],
    )

    assert excluded == 0
    assert ranked[0] is direct
    # 不做重复主题压制：两篇相近的脑肿瘤论文都仍在结果中。
    assert broad_a in ranked
    assert broad_b in ranked


def test_academic_index_removes_jats_without_rewriting_text() -> None:
    raw = "<jats:title>Abstract</jats:title><jats:p>Stable &amp; verified.</jats:p>"
    assert _clean_literature_text(raw) == "Abstract Stable & verified."


def test_title_translation_parser_accepts_only_bounded_chinese_json() -> None:
    raw = '说明：```json\n[{"id":0,"title_zh":"衰老人脑的免疫细胞替代"},{"id":7,"title_zh":"越界"}]\n```'
    assert _parse_title_translations(raw, {0, 1}) == {0: "衰老人脑的免疫细胞替代"}
    assert _parse_title_translations("这是一段解释，不是 JSON", {0}) == {}


def test_ai_summary_is_separate_from_source_abstract() -> None:
    raw = '[{"id":0,"ai_summary":"该论文围绕多相流数值模拟这一研究主题展开。"}]'
    assert _parse_ai_summaries(raw, {0}) == {
        0: "该论文围绕多相流数值模拟这一研究主题展开。"
    }
    assert _parse_ai_summaries("解释文字，不是 JSON", {0}) == {}

    from nodes.literature.tools.search_engines import Paper
    indexed = _index_paper(Paper(title="A paper without an abstract"), "paper")
    assert indexed["abstract"] is None
    assert indexed["ai_summary"] is None

def test_cas_snapshot_handles_runtime_venue_variants() -> None:
    from nodes.literature.tools.cas_ranking import diagnostics, lookup_cas

    state = diagnostics()
    assert state["error"] == ""
    assert str(state["path"]).endswith("cas_journal_ranking_2025.json")
    assert int(state["loaded_keys"]) > 20_000
    assert lookup_cas("Physics of Fluids") == (2, False)
    assert lookup_cas("Computers &amp; Mathematics with Applications") == (2, False)
    assert lookup_cas("Communications in Computational Physics") == (3, False)
    assert lookup_cas("Physical review. E") == (3, False)
    assert lookup_cas("Capillarity") == (None, False)


def test_journal_metrics_import_and_identifier_lookup(tmp_path, monkeypatch) -> None:
    import sqlite3
    from nodes.literature.tools import journal_metrics

    source = tmp_path / "factor.sqlite3"
    conn = sqlite3.connect(source)
    conn.execute(
        """CREATE TABLE factor (
            journal TEXT, journal_abbr TEXT, issn TEXT, eissn TEXT,
            nlm_id TEXT, factor REAL, jcr TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO factor VALUES(?,?,?,?,?,?,?)",
        ("Example Journal", "Ex J", "1234-5678", "8765-4321", "1234567", 4.2, "Q1"),
    )
    conn.commit()
    conn.close()

    target = tmp_path / "journal_metrics.sqlite3"
    assert journal_metrics.import_factor_database(source, target, factor_year=2024) == 1
    monkeypatch.setenv("HARNESS_JOURNAL_METRICS_DB", str(target))
    journal_metrics._lookup_cached.cache_clear()
    by_issn = journal_metrics.lookup_journal_metrics("Wrong title", issn="12345678")
    by_alias = journal_metrics.lookup_journal_metrics("", journal_abbr="Ex. J.")
    assert by_issn["impact_factor"] == 4.2
    assert by_issn["impact_factor_year"] == 2024
    assert by_issn["jcr_quartile"] == "Q1"
    assert by_issn["match_method"] == "issn"
    assert by_alias["journal"] == "Example Journal"

def test_publisher_landing_abstract_accepts_only_explicit_abstract_metadata() -> None:
    page = """
      <meta name="description" content="This generic publisher page description is deliberately long enough but is not an abstract and must never be used as one.">
      <meta name="citation_abstract" content="This is the official article abstract exposed by the publisher landing page, with enough text to pass the minimum validation boundary safely.">
    """
    assert _publisher_landing_abstract(page).startswith("This is the official article abstract")
    assert _publisher_landing_abstract(
        '<meta name="description" content="' + ("generic " * 30) + '">'
    ) == ""

def test_openaire_abstract_requires_an_exact_doi_match() -> None:
    payload = {
        "response": {"results": {"result": [
            {"metadata": {"oaf:entity": {"oaf:result": {
                "pid": {"@classid": "doi", "$": "10.1000/right"},
                "description": {"$": "Abstract: " + "official source text " * 10},
            }}}}
        ]}}
    }
    abstract = _openaire_abstract(payload, "10.1000/right")
    assert abstract.startswith("official source text")
    assert _openaire_abstract(payload, "10.1000/wrong") == ""


def test_public_pdf_discovery_and_original_abstract_validation() -> None:
    page_html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjournal.example%2Fpaper.pdf">PDF</a>'
        '<a class="result__a" href="https://researchgate.net/request.pdf">request</a>'
    )
    assert _public_pdf_candidates(page_html) == ["https://journal.example/paper.pdf"]
    doi_landing = (
        '<a href="javascript:open_pdf()">'
        'https://publisher.example/issues/verified-paper.pdf</a>'
    )
    assert _direct_pdf_candidates(doi_landing) == [
        "https://publisher.example/issues/verified-paper.pdf"
    ]

    import pymupdf
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "A Verified Example Paper\nDOI: 10.1000/pdf-test\n\n"
        "Abstract. This is the author supplied original abstract from the paper, "
        "and it contains enough validated text to be accepted by the extractor.\n\n"
        "Keywords: validation; metadata",
    )
    raw = document.tobytes()
    document.close()
    abstract = _pdf_original_abstract(
        raw, doi="10.1000/pdf-test", title="A Verified Example Paper"
    )
    assert abstract.startswith("This is the author supplied original abstract")
    assert _pdf_original_abstract(
        raw, doi="10.1000/different", title="Unrelated Other Work"
    ) == ""

@pytest.mark.asyncio
async def test_crossref_can_reserve_a_journal_only_recall_page() -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"items": []}}

    class Client:
        def __init__(self) -> None:
            self.params = None

        async def get(self, url, **kwargs):
            self.params = kwargs["params"]
            return Response()

    client = Client()
    assert await CrossrefSearch(client).search(
        "Bragg reflection",
        rows=30,
        publication_types=("journal-article",),
    ) == []
    assert client.params["rows"] == 30
    assert client.params["filter"] == "type:journal-article"


@pytest.mark.asyncio
async def test_crossref_title_mode_uses_query_title() -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"items": []}}

    class Client:
        def __init__(self) -> None:
            self.params = None

        async def get(self, url, **kwargs):
            self.params = kwargs["params"]
            return Response()

    client = Client()
    assert await CrossrefSearch(client).search(
        "Constraints on the Cosmic Expansion History from GWTC-3",
        rows=10,
        title_only=True,
    ) == []
    assert client.params["query.title"].startswith("Constraints on the Cosmic Expansion")
    assert "query" not in client.params
