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
    _cached_abstract_matches_title,
    _index_paper,
    _parse_ai_summaries,
    _parse_title_translations,
    _academic_strategy_queries,
    _academic_retry_strategy_better,
    _academic_boolean_facets,
    _academic_comparison_facets,
    _paper_matches_academic_facets,
    _academic_period_focus,
    _academic_period_method_terms,
    _academic_nonresearch_record,
    _academic_primary_focus_penalty,
    _academic_named_concept_gate,
    _select_academic_search_results,
)


def test_academic_search_excludes_source_documents_but_keeps_scholarship() -> None:
    assert _academic_nonresearch_record({"title": "UNESCO: Ninth Session of the Intergovernmental Committee", "venue": "International Journal of Cultural Property"})
    assert _academic_nonresearch_record({"title": "The Resolution on the Return and Restitution of Cultural Property", "venue": "International Journal of Cultural Property"})
    assert _academic_nonresearch_record({"title": "Ancient DNA study", "venue": "Science News"})
    assert _academic_nonresearch_record({"title": "Issues in Repatriation The Return of Cultural Treasures. Jeanette Greenfield The Ethics of Collecting Cultural Property. Phyllis Mauch Messenger", "venue": "Current Anthropology"})
    assert _academic_nonresearch_record({"title": "Endangered Languages: Essays on Revitalization (review)",
                                         "abstract": "Reviewed by: A collected volume on language education."})
    assert not _academic_nonresearch_record({"title": "Repatriation without Ethics: Return of Colonial Collections", "publication_category": "书籍"})
    assert not _academic_nonresearch_record({"title": "The role of UN resolutions in repatriation law", "venue": "Museum Studies"})


def test_academic_search_primary_focus_prefers_target_and_evaluated_method() -> None:
    sepsis_facets = _academic_boolean_facets("machine learning AND early prediction AND sepsis")
    terms = ["machine", "learning", "early", "prediction", "sepsis"]
    assert _academic_primary_focus_penalty({"title": "Early prediction of sepsis-induced cardiomyopathy"}, terms, sepsis_facets) > 0
    assert _academic_primary_focus_penalty({"title": "Early prediction of sepsis in ICU patients"}, terms, sepsis_facets) == 0
    rag_terms = ["retrieval", "augmented", "generation", "reranking"]
    assert _academic_primary_focus_penalty({"title": "Stealthy membership inference for RAG"}, rag_terms, []) > 0
    assert _academic_primary_focus_penalty({"title": "Cross-encoder reranking for RAG"}, rag_terms, []) == 0


def test_cached_abstract_must_overlap_its_title() -> None:
    liver = "We study the molecular signals of hepatic regeneration after injury. " * 5
    museum = "We study repatriation of museum collections and colonial restitution. " * 5
    title = "Issues in the Repatriation of Museum Collections"
    assert not _cached_abstract_matches_title(title, liver)
    assert _cached_abstract_matches_title(title, museum)


def test_sinkhorn_query_does_not_admit_unrelated_entropy_work() -> None:
    terms = ["entropy", "regularization", "optimal", "transport", "sinkhorn"]
    assert not _academic_named_concept_gate({"title": "Entropy Regularization in General Graph Matching"}, terms)
    assert _academic_named_concept_gate({"title": "Sinkhorn Algorithm for Approximate Matching"}, terms)
    assert _academic_named_concept_gate({"title": "Entropy-Regularized Optimal Transport"}, terms)


def test_alternative_observation_methods_do_not_hide_urban_cooling_studies() -> None:
    facets = _academic_boolean_facets(
        "urban green space AND urban heat island mitigation AND "
        "(remote sensing OR ground observation)"
    )
    assert _paper_matches_academic_facets(
        {"title": "Cooling from urban green parks reduces heat islands", "abstract": ""},
        facets,
    )
    assert not _paper_matches_academic_facets(
        {"title": "Cooling from rural forests", "abstract": ""}, facets,
    )


def test_valid_english_retry_beats_more_chinese_fallback_queries() -> None:
    fallback = {"english_query": "《藻海无边》与《简·爱》", "search_queries": [
        {"query": "藻海无边"}, {"query": "简爱"}, {"query": "文学比较"},
    ]}
    translated = {"english_query": "Wide Sargasso Sea Jane Eyre colonial literature", "search_queries": [
        {"query": "Wide Sargasso Sea Jane Eyre colonial literature"},
    ]}
    assert _academic_retry_strategy_better(fallback, translated)
    assert not _academic_retry_strategy_better(translated, fallback)


def test_later_child_population_constraint_excludes_adult_asthma() -> None:
    facets = _academic_boolean_facets(
        "(PM2.5 OR fine particulate matter) AND long-term exposure AND "
        "(incident asthma OR new-onset asthma) AND (children OR child OR pediatric)"
    )
    assert ["children", "child", "pediatric"] in facets
    adult = {"title": "Long-term PM2.5 exposure and incident asthma among elderly adults", "abstract": "Children were discussed as background."}
    child = {"title": "Long-term PM2.5 exposure and incident childhood asthma", "abstract": "A pediatric cohort study."}
    assert not _paper_matches_academic_facets(adult, facets)
    assert _paper_matches_academic_facets(child, facets)
    unrelated = {"title": "Long-term PM2.5 exposure and childhood cancer", "abstract": "A pediatric cohort study of cancer incidence."}
    assert not _paper_matches_academic_facets(unrelated, facets)
    no_pm25 = {"title": "Long-term exposure to NO2 and incident asthma in children", "abstract": "A pediatric cohort study."}
    assert not _paper_matches_academic_facets(no_pm25, facets)
    coarse = {"title": "Long-term coarse particulate matter exposure and asthma in children",
              "abstract": "Earlier PM2.5 studies motivated this investigation of coarse PM."}
    ultrafine = {"title": "Exposure to ultrafine particles and incident asthma in children",
                 "abstract": "Fine particulate matter is discussed as prior work."}
    exacerbation = {"title": "Severe asthma exacerbations in asthmatic children",
                    "abstract": "Long-term PM2.5 exposure was measured in this cohort."}
    other_outcome = {"title": "Long-term PM2.5 exposure and risk of urticaria in children",
                     "abstract": "Asthma studies motivated this research."}
    absenteeism = {"title": "School absenteeism in students with asthma from elevated AQI risk areas",
                   "abstract": "PM2.5 exposure was measured over the school year."}
    assert not _paper_matches_academic_facets(coarse, facets)
    assert not _paper_matches_academic_facets(ultrafine, facets)
    assert not _paper_matches_academic_facets(exacerbation, facets)
    assert not _paper_matches_academic_facets(other_outcome, facets)
    assert not _paper_matches_academic_facets(absenteeism, facets)


def test_comparison_accepts_evidence_about_either_approach() -> None:
    facets = _academic_boolean_facets(
        "school education AND community daily use AND endangered language revitalization"
    )
    comparison = _academic_comparison_facets(
        facets, "学校教育和社区日常使用，哪种更有助于濒危语言复兴？"
    )
    assert _paper_matches_academic_facets(
        {"title": "School education in endangered language revitalization"}, comparison,
    )
    assert _paper_matches_academic_facets(
        {"title": "Community language revitalization through daily practice"}, comparison,
    )
    assert not _paper_matches_academic_facets(
        {"title": "School education and mathematics learning"}, comparison,
    )
    assert not _paper_matches_academic_facets(
        {"title": "Revitalization of endangered heritage buildings",
         "abstract": "Community groups seek the best use of historic sites."}, comparison,
    )


def test_comparison_searches_each_option_with_the_shared_goal() -> None:
    question = "学校教育和社区日常使用，哪种更有助于濒危语言复兴？"
    strategy = {"english_query": "school education AND community daily use AND endangered language revitalization",
                "search_queries": [{"query": "school community endangered language revitalization"}]}
    queries = _academic_strategy_queries(strategy, question)
    assert "school education endangered language revitalization" in queries
    assert "community daily use endangered language revitalization" in queries
    assert "community endangered language revitalization" in queries
    assert queries[-1] == question


def test_chinese_academic_query_uses_english_strategy_for_international_sources() -> None:
    original = "古 DNA 如何解释欧洲青铜时代的人口迁移？"
    strategy = {
        "english_query": "ancient DNA European Bronze Age population migration",
        "search_queries": [
            {"query": "DNA"},
            {"query": "人口迁移"},
        ],
    }
    queries = _academic_strategy_queries(strategy, original)
    assert queries[0] == strategy["english_query"]
    assert queries[-1] == original
    assert "人口迁移" not in queries[:-1]


def test_long_translated_query_searches_compact_concepts_first() -> None:
    english = (
        "retrieval-augmented generation reranking retrieved results reduce "
        "answer errors incorrect citations experimental studies reviews task types"
    )
    queries = _academic_strategy_queries(
        {"english_query": english, "search_queries": [{"query": english}]},
        "检索增强生成中的重排序能否减少错误引文？",
    )
    assert queries[0] == "retrieval-augmented generation reranking retrieved reduce answer errors citations"
    assert english in queries
    assert queries[-1].startswith("检索增强生成")


def test_academic_boolean_object_facets_reject_other_diseases_and_species() -> None:
    sepsis_facets = _academic_boolean_facets(
        "(machine learning OR artificial intelligence) AND "
        "(sepsis OR septic shock) AND early prediction"
    )
    assert sepsis_facets == [
        ["machine learning", "artificial intelligence"],
        ["sepsis", "septic shock"],
        ["early prediction"],
    ]

    def paper(title: str) -> dict:
        return {
            "title": title, "abstract": "", "score": 0.8,
            "score_breakdown": {"raw": {"relevance": 0.8}},
        }

    delirium = paper("Machine learning for early delirium prediction")
    sepsis = paper("Machine learning for early sepsis prediction")
    ranked, excluded = _select_academic_search_results(
        [delirium, sepsis], limit=10,
        core_terms=["machine", "learning", "sepsis"],
        mandatory_facets=sepsis_facets,
    )
    assert ranked == [sepsis]
    assert excluded == 1

    # The translated strategy can place the target disease after method and
    # outcome. The third AND group still must gate admission.
    third_group = _academic_boolean_facets(
        "(machine learning OR artificial intelligence) AND "
        "(early prediction OR early detection) AND sepsis AND "
        "(review OR external validation)"
    )
    adr = paper("Machine learning for early prediction of adverse drug reactions")
    ranked, excluded = _select_academic_search_results(
        [adr, sepsis], limit=10,
        core_terms=["machine", "learning", "early", "sepsis"],
        mandatory_facets=third_group,
    )
    assert ranked == [sepsis]
    assert excluded == 1

    drought_facets = _academic_boolean_facets("drought AND wheat AND root traits")
    maize = paper("Drought response of maize root traits")
    wheat = paper("Drought response of wheat root traits")
    ranked, excluded = _select_academic_search_results(
        [maize, wheat], limit=10,
        core_terms=["drought", "wheat", "root"],
        mandatory_facets=drought_facets,
    )
    assert ranked == [wheat]
    assert excluded == 1


def test_named_period_ranking_prefers_study_of_requested_population() -> None:
    focus = _academic_period_focus(
        "ancient DNA European Bronze Age population migration archaeology"
    )
    assert focus == ("european", "bronze age", ["population", "migration"])
    assert _academic_period_focus(
        "ancient DNA Bronze Age Europe population migration genetics"
    ) == ("europe", "bronze age", ["population", "migration"])
    assert _academic_period_focus("Average Age in clinical prediction") is None
    assert _academic_period_method_terms(
        "ancient DNA Bronze Age Europe population migration"
    ) == ["dna", "genom", "genetic"]

    def paper(title: str, abstract: str = "") -> dict:
        return {
            "title": title, "abstract": abstract, "score": 0.7,
            "score_breakdown": {"raw": {"relevance": 0.8}},
        }

    rabbit = paper("Ancient DNA from Bronze Age bones of European rabbit")
    pig = paper("Ancient DNA and population turnover in southern Levantine pigs--signature of the sea peoples migration?")
    human = paper(
        "Population genomics of Bronze Age Eurasia",
        "Human migration in Europe inferred from ancient genomes.",
    )
    ranked, excluded = _select_academic_search_results(
        [rabbit, pig, human], limit=10,
        core_terms=["ancient", "dna", "european", "bronze", "age", "population", "migration"],
        period_focus=focus,
        method_terms=["dna", "genom", "genetic"],
    )
    assert ranked[0] is human
    assert rabbit not in ranked
    assert pig not in ranked
    assert excluded == 2

    archaeology_only = paper("Bronze Age population migrations in Europe")
    ranked, excluded = _select_academic_search_results(
        [archaeology_only, human], limit=10,
        core_terms=["bronze", "age", "europe", "population", "migration"],
        period_focus=focus,
        method_terms=["dna", "genom", "genetic"],
    )
    assert ranked == [human]
    assert excluded == 1

    incidental_period = paper(
        "Genomic population history in modern Europe",
        "This study concerns modern populations. " + "Background. " * 70
        + "Bronze Age was also historically important.",
    )
    ranked, excluded = _select_academic_search_results(
        [incidental_period, human], limit=10,
        core_terms=["genomic", "europe", "bronze", "age", "population"],
        period_focus=focus,
        method_terms=["dna", "genom", "genetic"],
    )
    assert ranked == [human]
    assert excluded == 1


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


# ── Crossref 发表日期：issued → published → published-print → published-online，
# 永远不取 created（DOI 登记日）。#1185 第 1 项；日期表沿用 #981 回退前那张。──
@pytest.mark.parametrize("dates, expected", [
    # 1984 年的论文 2002 年补登 DOI：created 不是发表年。
    ({"issued": {"date-parts": [[1984, 10, 29]]},
      "published-online": {"date-parts": [[1984, 10, 29]]},
      "created": {"date-parts": [[2002, 7, 27]]}}, (1984, "1984-10-29")),
    # 只有 created：宁可没有年份，也不把登记日当发表日。
    ({"created": {"date-parts": [[2002, 7, 27]]}}, (None, "")),
    # 部分日期保持部分，不补成 1 月 1 日；按零填充（与 PubMed 一致，ISO 可读）。
    ({"issued": {"date-parts": [[2024]]}}, (2024, "2024")),
    ({"issued": {"date-parts": [[2024, 2]]}}, (2024, "2024-02")),
    # issued 是假日期（2 月 30 日）→ 换下一个字段，年与日仍出自同一个字段。
    ({"issued": {"date-parts": [[2024, 2, 30]]},
      "published-print": {"date-parts": [[2024, 3, 1]]}}, (2024, "2024-03-01")),
    # issued 早于纸本：取最早已知发表日，年与日不分叉。
    ({"issued": {"date-parts": [[2023, 12, 5]]},
      "published-print": {"date-parts": [[2024, 2, 1]]}}, (2023, "2023-12-05")),
    # Crossref 真会给 [[null]] / 空列表。
    ({"issued": {"date-parts": [[None]]},
      "published": {"date-parts": [[2021, 6]]}}, (2021, "2021-06")),
    ({"issued": {"date-parts": [[]]}, "published": None}, (None, "")),
])
def test_crossref_year_and_date_share_publication_evidence(dates, expected):
    paper = CrossrefSearch(None)._parse({"message": {"items": [{
        "title": ["Publication date regression"], **dates,
    }]}})[0]
    assert (paper.year, paper.pub_date) == expected


@pytest.mark.asyncio
async def test_crossref_doi_enrichment_fills_year_from_the_same_date_source(monkeypatch):
    """按 DOI 补齐年份走的是另一段代码：它必须和检索结果用同一个出处。"""
    import nodes.literature.tools.search_engines as se
    from nodes.literature.tools.search_engines import Paper, SearchManager

    class Response:
        status_code = 200

        def json(self):
            return {"message": {
                "issued": {"date-parts": [[1984, 10, 29]]},
                "published-print": {"date-parts": [[1985, 1]]},
                "created": {"date-parts": [[2002, 7, 27]]},
            }}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *args, **kwargs):
            assert url == "https://api.crossref.org/works/10.1000/classic"
            return Response()

    monkeypatch.setattr(se.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    manager = object.__new__(SearchManager)
    manager._progress_callback = None
    paper = Paper(
        title="Classic paper missing its year",
        doi="10.1000/classic",
        source="openalex",
        authors=["Existing Author"],
        venue="Existing Journal",
        abstract="Existing abstract",
        url="https://doi.org/10.1000/classic",
    )

    await manager._enrich_metadata_cross_source([paper], {"crossref"})

    assert paper.year == 1984
@pytest.mark.asyncio
async def test_crossref_issn_window_reads_all_pages() -> None:
    class Response:
        def __init__(self, message):
            self.message = message

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": self.message}

    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, url, **kwargs):
            params = dict(kwargs["params"])
            self.calls.append(params)
            doi = "10.1234/one" if params["cursor"] == "*" else "10.1234/two"
            item = {
                "DOI": doi,
                "title": [doi],
                "container-title": ["Marine Pollution Bulletin"],
                "published-online": {"date-parts": [[2026, 9, 1]]},
            }
            if len(self.calls) > 1:
                return Response({"items": [], "next-cursor": "next"})
            return Response({"items": [item], "next-cursor": "next"})

    client = Client()
    papers = await CrossrefSearch(client).search_issn_window(
        "0025-326X", date_from="2025-09-01", date_until="2026-09-24", rows=1
    )
    assert len(papers) == 1
    assert client.calls[0]["cursor"] == "*"
    assert client.calls[1]["cursor"] == "next"
    assert "issn:0025-326X" in client.calls[0]["filter"]
