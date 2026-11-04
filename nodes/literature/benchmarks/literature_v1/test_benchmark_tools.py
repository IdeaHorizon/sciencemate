"""Deterministic regression tests for LiteratureBench bookkeeping."""
from __future__ import annotations

from nodes.literature.benchmarks.literature_v1.score import score
from nodes.literature.benchmarks.literature_v1.validate_results import validate
from nodes.literature.benchmarks.literature_v1.run_harness_search import normalize_papers
from nodes.literature.benchmarks.literature_v1.merge_result_candidates import merge
from nodes.literature.benchmarks.literature_v1.combine_retries import combine


def test_unique_title_can_match_missing_doi_but_wrong_doi_cannot():
    gold = {"papers": [
        {"doi": "10.1000/a", "title": "Unique title", "relevance": 3},
        {"doi": "10.1000/b", "title": "Ambiguous title", "relevance": 2},
        {"doi": "10.1000/c", "title": "Ambiguous title", "relevance": 0},
    ]}
    result = {"task_id": "T", "system": {"name": "demo"}, "status": "success", "papers": [
        {"rank": 1, "title": "Unique title", "doi": ""},
        {"rank": 2, "title": "Ambiguous title", "doi": ""},
        {"rank": 3, "title": "Unique title", "doi": "10.1000/wrong"},
    ]}
    scored = score(result, gold)
    assert scored["judged_at_10"] == 1
    assert scored["ndcg_at_10"] is None
    assert len(scored["unmatched"]) == 2

def test_doi_less_result_merges_with_unique_doi_candidate():
    pool = {"status": "candidate_pool_not_gold", "topics": [{
        "id": "T", "candidates": [{"doi": "10.1000/a", "title": "Exact paper", "candidate_sources": []}],
    }]}
    result = {"task_id": "T", "system": {"name": "demo"}, "papers": [
        {"rank": 1, "doi": "", "title": "Exact paper"},
    ]}
    merged = merge(pool, [result])
    assert len(merged["topics"][0]["candidates"]) == 1
    assert merged["topics"][0]["candidates"][0]["source_records"][0]["system"] == "demo"


def test_abstract_status_preserves_provenance():
    papers = normalize_papers({"papers": [
        {"title": "A", "source": "crossref", "abstract": "text", "metadata_provenance": {"abstract": "pubmed"}},
        {"title": "B", "source": "crossref", "abstract": "source text"},
        {"title": "C", "source": "crossref", "ai_summary": "generated"},
        {"title": "D", "source": "crossref"},
    ]})
    assert [paper["abstract_status"] for paper in papers] == [
        "enriched_source", "source", "ai_summary", "missing",
    ]
    assert papers[0]["abstract_provenance"] == "pubmed"
    assert papers[0]["abstract"] == "text"
    assert papers[2]["abstract"] == ""  # generated text must not be passed off as source abstract


def test_result_validator_rejects_empty_success_and_duplicate_task():
    row = {
        "benchmark_version": "0.3.0",
        "system": {"name": "demo", "mode": "academic_search"},
        "task_id": "T",
        "status": "success",
        "attempts": [{"elapsed_seconds": 1.0, "outcome": "success", "raw_evidence_path": "raw/T.json"}],
        "papers": [],
    }
    errors = validate([row, row], {"T"})
    assert any("empty results" in error for error in errors)
    assert any("duplicate system/mode/task" in error for error in errors)

def test_retry_keeps_first_failure_and_uses_first_success():
    first = {
        "task_id": "T", "system": {"name": "demo"}, "prompt": "q", "request_settings": {},
        "status": "partial", "papers": [], "attempts": [{"elapsed_seconds": 2}],
    }
    second = {
        **first, "status": "success", "papers": [{"rank": 1, "title": "A"}],
        "attempts": [{"elapsed_seconds": 3}],
    }
    merged = combine([first], [second])[0]
    assert merged["status"] == "success"
    assert merged["first_attempt_status"] == "partial"
    assert merged["selected_attempt"] == 2
    assert [attempt["elapsed_seconds"] for attempt in merged["attempts"]] == [2, 3]

def test_validator_allows_failed_first_attempt_without_raw_response():
    row = {
        "benchmark_version": "0.3.0", "system": {"name": "demo", "mode": "search"},
        "task_id": "T", "status": "success", "selected_attempt": 2,
        "attempts": [
            {"elapsed_seconds": 2, "outcome": "HTTP 503"},
            {"elapsed_seconds": 3, "outcome": "success", "raw_evidence_path": "raw/retry.json"},
        ],
        "papers": [{"rank": 1, "title": "A"}],
    }
    assert validate([row], {"T"}) == []
