from copy import deepcopy
import json
import sqlite3

import pytest

from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper, SearchManager
from nodes.literature.tools import search_audit
from nodes.literature.evaluation import evaluate_search
from shared.contrib.literature_federation import merge_channels
from shared.contrib.literature_status import abstract_processing_status


def test_full_identifiers_titles_and_scientific_symbols_stay_distinct():
    long = "A scientific study " * 6
    rows = [Paper(title=title, doi=doi) for title, doi in [
        ("中文颗粒沉降研究", ""), ("中文材料研究", ""),
        (long + "heating", ""), (long + "cooling", ""),
        ("C++ study", ""), ("C# study", ""),
        ("Same title", "10.1234/" + "a" * 60 + "1"),
        ("Same title", "10.1234/" + "a" * 60 + "2"),
        ("One", "10.1234/a-b"), ("Other", "10.1234/ab"),
    ]]
    assert len(SearchManager.__new__(SearchManager)._merge_results([], rows)) == 10
    assert len(merge_channels([p.to_dict() for p in rows])) == 10


def test_doi_variants_merge_but_conflicting_dois_and_ambiguous_title_do_not():
    a = {"title": "Same", "year": 2026, "doi": "10.1234/one"}
    equivalent = {**a, "doi": "https://doi.org/10.1234/ONE", "abstract": "Abstract"}
    conflict = {**a, "doi": "10.1234/two"}
    unknown = {**a, "doi": ""}
    merged = merge_channels([a, equivalent, conflict, unknown])
    assert len(merged) == 3
    assert merged[0]["abstract"] == "Abstract"


def test_unicode_index_and_legacy_collision_reuse_are_safe(tmp_path):
    index = LocalPaperIndex(str(tmp_path / "index.sqlite3"))
    first, second = Paper(title="颗粒沉降研究", abstract="First"), Paper(title="材料结构研究", abstract="Second")
    index.add_papers([first])
    with sqlite3.connect(index.db_path) as conn:
        conn.execute("UPDATE paper_index SET doi=?", ("hash:" + index._legacy_title_hash(first.title),))
    assert index.get_papers_by_identity([second]) == {}
    assert index.get_papers_by_identity([first])["title:" + first.title].abstract == "First"
    index.add_papers([first, second])
    assert len(index.get_papers_by_identity([first, second])) == 2
    with sqlite3.connect(index.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM paper_index").fetchone()[0] == 2


@pytest.mark.parametrize("record,expected", [
    ({}, "not_requested"), ({"abstract_enrichment_attempted": True}, "pending"),
    ({"abstract_lookup_completed": True}, "not_found"),
    ({"abstract": "Source abstract"}, "available"),
    ({"abstract_lookup_completed": True, "abstract_lookup_source_state": {"crossref": {"result": "http_403"}}}, "access_blocked"),
    ({"abstract_lookup_source_state": {"crossref": {"result": "http_429"}}}, "retry_pending"),
    ({"abstract_lookup_source_state": {"crossref": {"result": "ReadTimeout"}}}, "retry_pending"),
    ({"literature_preparation": "ready", "abstract_enrichment_attempted": True}, "legacy_completed"),
])
def test_abstract_states_are_not_conflated(record, expected):
    assert abstract_processing_status(record) == expected


@pytest.mark.asyncio
async def test_search_audit_preserves_order_params_and_failures(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "literature_cache_dir", lambda: tmp_path)
    received_requests = []
    @search_audit.audited_search
    async def search(request, progress=None, **options):
        received_requests.append(dict(request))
        progress({"stage": "search", "raw_count": 2})
        if request["query"] == "fail":
            raise ValueError("fixture failure")
        return {"query": request["query"], "papers": [{"title": "B"}, {"title": "A"}],
                "decomposed_queries": ["topic AND example"], "timings": {"total": 0.1}}
    request = {"query": "topic", "limit": 50, "api_key": "do-not-record", "request_id": "caller-request-1"}
    before = deepcopy(request)
    response = await search(request, enabled_sources={"crossref"}, year_from=2020)
    audit_id = response["strategy_diagnostics"]["audit_id"]
    saved = json.loads((tmp_path / "search_audits" / (audit_id + ".json")).read_text())
    assert saved["search_code_sha256"] and saved["result_sha256"]
    assert received_requests[0]["request_id"] == saved["request_id"] == "caller-request-1"
    assert saved["audit_id"] != saved["request_id"]
    assert saved["options"]["year_from"] == 2020
    assert [p["title"] for p in saved["papers"]] == ["B", "A"]
    assert "do-not-record" not in json.dumps(saved)
    assert request == before
    with pytest.raises(ValueError):
        await search({"query": "fail"})
    records = [json.loads(p.read_text()) for p in (tmp_path / "search_audits").glob("*.json")]
    assert sorted(r["status"] for r in records) == ["completed", "failed"]
    failed = next(r for r in records if r["status"] == "failed")
    assert received_requests[1]["request_id"] == failed["request_id"] == failed["audit_id"]
    monkeypatch.setattr(search_audit, "write_search_audit", lambda record: False)
    assert (await search(request))["strategy_diagnostics"]["audit_status"] == "write_failed"


def test_measurement_does_not_invent_quality_scores_or_link_verification():
    result = {"query": "topic", "papers": [{"title": "A", "doi": "10.1234/a"},
              {"title": "A", "doi": "https://doi.org/10.1234/A"}], "timings": {"total": 3}}
    report = evaluate_search(result)
    assert report["automated_metrics"]["duplicate_count"] == 1
    assert report["automated_metrics"]["elapsed_seconds"] == 3
    assert report["weighted_total"] is None
    assert all(row["score"] is None for row in report["dimensions"].values())
    with pytest.raises(ValueError):
        evaluate_search(result, {"dimensions": {"evidence_quality": {"score": 85}}})
    reviews = {"dimensions": {"evidence_quality": {"score": 85, "method": "ai_assisted",
               "reviewer": "test reviewer", "reason": "Fixture evidence", "evidence": ["doi:10.1234/a"]}}}
    report = evaluate_search(result, reviews)
    assert report["dimensions"]["evidence_quality"]["score"] == 85
    assert report["dimensions"]["reference_validity"]["score"] is None
    assert report["formal_scores_available"] is False
