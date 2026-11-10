"""Exact title hits supplement topic retrieval; they never replace it."""
import sqlite3

import pytest

import platform_runtime as runtime
from nodes.literature.tools import academic_strategy, local_index, search_engines
from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper


TITLE = (
    "Wavelength‐Dependent Chiroptical Reversal in Resonant Metasurface "
    "Photothermoelectric Photodetector for Dual‐Mode Encrypted Communication"
)
TRANSLATED = "用于双模式加密通信的共振超表面光热电光电探测器中的波长依赖手性光学反转"


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))
    result = LocalPaperIndex(str(tmp_path / "index.sqlite3"))
    result.add_papers([
        Paper(title=TITLE, doi="10.1/wanted", year=2026, source="crossref"),
        Paper(title=TITLE + ": a different study", doi="10.1/different"),
        Paper(title="Editorial Board", doi="10.1/editorial"),
    ])
    # Exercise historical translation-only entries, without title_zh columns.
    result.save_title_translations({TITLE: TRANSLATED, "Editorial Board": "编委会"})
    monkeypatch.setattr(local_index, "get_index", lambda: result)
    return result


@pytest.mark.parametrize("query", [TRANSLATED, f"《{TRANSLATED}》", TITLE,
                                  TITLE.replace("‐", "-").lower()])
def test_original_and_translated_full_title_find_same_identity(index, query):
    assert [p.doi for p in index.find_exact_title(query)] == ["10.1/wanted"]


@pytest.mark.parametrize("query", ["共振超表面", "Wavelength", TRANSLATED + " 的综述", ""])
def test_topic_or_partial_title_does_not_bypass_remote_search(index, query):
    assert index.find_exact_title(query) == []


def test_translation_column_requires_current_source_hash(index):
    with sqlite3.connect(index.db_path) as conn:
        conn.execute("DELETE FROM title_translations")
        conn.execute(
            "UPDATE paper_index SET title_zh=?, title_zh_source_key=? WHERE doi=?",
            (TRANSLATED, index._abstract_hash(TITLE), "10.1/wanted"),
        )
    assert [p.doi for p in index.find_exact_title(TRANSLATED)] == ["10.1/wanted"]
    with sqlite3.connect(index.db_path) as conn:
        conn.execute("UPDATE paper_index SET title='Corrected original title' WHERE doi='10.1/wanted'")
    assert index.find_exact_title(TRANSLATED) == []


def test_reverse_translation_matches_whole_original_not_legacy_hash(index):
    prefix = "Scientific characterization of multilayer metasurfaces and optical "
    left, right = prefix + "heating", prefix + "cooling"
    assert index._legacy_title_hash(left) == index._legacy_title_hash(right)
    assert index._title_hash(left) != index._title_hash(right)
    index.add_papers([Paper(title=left, doi="10.1/heating"),
                      Paper(title=right, doi="10.1/cooling")])
    index.save_title_translations({left: "光学加热的完整研究"})
    assert [p.doi for p in index.find_exact_title("光学加热的完整研究")] == ["10.1/heating"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [TRANSLATED, TITLE])
@pytest.mark.parametrize("bilingual", [False, True])
async def test_known_title_keeps_strategy_and_related_remote_results(
    index, monkeypatch, query, bilingual,
):
    strategy_calls, remote_calls = [], []
    topic = (
        "wavelength-dependent chiroptical reversal AND resonant metasurface "
        "AND photothermoelectric photodetector"
    )

    def strategy(question):
        strategy_calls.append(question)
        plan = {"english_query": topic, "search_queries": [{"query": topic}]}
        if bilingual:
            plan.update(concept_groups=[[c.strip()] for c in topic.split(' AND ')],
                        bilingual_queries=[{"label":"主题", "en":topic, "zh":"波长依赖手性反转 * 共振超表面 * 光热电探测器"}])
        return plan, {}

    def complete_paper(title, doi):
        return Paper(
            title=title, doi=doi, abstract=title + " was experimentally investigated.",
            authors=["Researcher"], year=2026, venue="Research Journal",
            url="https://example.invalid/" + doi, source="crossref",
        )

    class Results(list):
        def audit_dict(self):
            return {"attempted_sources": ["crossref"], "warnings": [], "from_cache": False}

    class Manager:
        local_index = index

        def __init__(self, **kwargs):
            pass

        async def search_all(self, query, **kwargs):
            remote_calls.append((query, kwargs["enabled_sources"]))
            return Results([
                complete_paper(TITLE, "10.1/wanted"),
                complete_paper(
                    "Resonant metasurface photothermoelectric photodetectors "
                    "exhibiting wavelength-dependent chiroptical reversal",
                    "10.1/related",
                ),
                complete_paper("Plant genetics and crop breeding", "10.1/unrelated"),
                complete_paper("Editorial Board", "10.1/editorial"),
            ])

        async def _enrich_metadata_cross_source(self, *args):
            pytest.fail("Reuse indexed metadata; remote fixtures are already complete")

        def _merge_results(self, left, right):
            return left + right

    async def journal_search(*args, **kwargs):
        return []

    monkeypatch.setattr(search_engines, "SearchManager", Manager)
    monkeypatch.setattr(search_engines.CrossrefSearch, "search", journal_search)
    monkeypatch.setattr(academic_strategy, "generate_academic_strategy", strategy)
    monkeypatch.setattr(runtime, "_local_literature_assets", lambda papers: {})
    events=[]
    result = await runtime.run_literature_search({"query": query}, progress=events.append)
    if bilingual:
        assert result['search_plan']
        assert next(e for e in events if e['stage']=='search_plan')['search_plan']==result['search_plan']
        assert all('cnki' not in sources for _, sources in remote_calls)
        assert len(remote_calls)==1
    assert strategy_calls == [query]
    assert [q for q, _ in remote_calls] == result["decomposed_queries"]
    assert any({"crossref", "openalex"} <= sources for _, sources in remote_calls)
    assert [p["doi"] for p in result["papers"]] == ["10.1/wanted", "10.1/related"]
    assert result["search_mode"] == "remote"
    assert result["from_cache"] is False
    assert result["requested_sources"] and result["attempted_sources"] == ["crossref"]
    assert result["strategy_diagnostics"]["indexed_exact_title_matches"] == 1
    assert result["strategy_diagnostics"]["metadata_reuse"]["enrichment_target_count"] == 0
    assert result["papers"][0]["title_zh"] == TRANSLATED
    assert result["papers"][0]["exact_title_match"] is True
    assert result["papers"][1]["exact_title_match"] is False
    assert result["strategy_diagnostics"]["admission"]["excluded_by_reason"] == {
        "nonresearch": 1, "relevance": 1,
    }


@pytest.mark.asyncio
async def test_title_match_does_not_hide_failed_question_translation(index, monkeypatch):
    monkeypatch.setattr(
        academic_strategy, "generate_academic_strategy",
        lambda question: ({"english_query": ""}, {"translation_status": "failed"}),
    )
    with pytest.raises(runtime.RequestError) as error:
        await runtime.run_literature_search({"query": TRANSLATED})
    assert error.value.code == "strategy_translation_failed"


@pytest.mark.parametrize("hyphen", ["-", "‐", "‑", "﹣", "－"])
def test_publisher_hyphens_do_not_reject_correct_concept(hyphen):
    paper = {
        "title": TITLE.replace("‐", hyphen), "abstract": "",
        "score_breakdown": {"raw": {"relevance": 0.5625}},
    }
    facets = [
        ["wavelength-dependent chiral optical inversion", "wavelength-dependent chirality reversal"],
        ["resonant metasurface", "resonant metasurfaces"],
        ["photothermoelectric photodetector", "photothermoelectric photodetectors"],
    ]
    assert runtime._academic_exclusion_reason(paper, mandatory_facets=facets) is None
    # Other optical topics still fail the missing detector/facet requirements.
    assert runtime._academic_exclusion_reason(
        {**paper, "title": "Wavelength-dependent chirality reversal in molecules"},
        mandatory_facets=facets,
    ) == "facets"
