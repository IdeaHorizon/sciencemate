from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from nodes.literature.benchmarks.literature_v1.merge_reviews_v3 import merge
from nodes.literature.benchmarks.literature_v1.prepare_annotation_v3 import add_candidate
from nodes.literature.benchmarks.literature_v1.score_v3 import dcg, ndcg, read_labels, score_task


def test_ndcg_rewards_correct_order():
    assert ndcg([7, 3, 1], [7, 3, 1]) == pytest.approx(100)
    assert ndcg([1, 3, 7], [7, 3, 1]) < 100
    assert dcg([0, 0]) == 0


def test_cross_platform_pool_deduplicates_and_keeps_sources_private():
    pooled, audit = {}, {}
    tasks = {"T": {"prompt_zh": "question"}}
    facets = {"T": {"facets": ["one"]}}
    first = {"rank": 1, "doi": "10.1/x", "title": "Paper", "authors": ["A"]}
    second = {**first, "rank": 4, "abstract": "A complete abstract", "venue": "Journal"}
    add_candidate(pooled, audit, paper=first, task_id="T", system="System A",
                  path=Path("a.json"), is_seed=False, tasks=tasks, facets=facets)
    add_candidate(pooled, audit, paper=second, task_id="T", system="System B",
                  path=Path("b.json"), is_seed=False, tasks=tasks, facets=facets)
    assert len(pooled) == 1
    row = next(iter(pooled.values()))
    assert row["abstract"] == "A complete abstract"
    assert "System A" not in json.dumps(row)
    sources = next(iter(audit.values()))["source_records"]
    assert [source["system"] for source in sources] == ["System A", "System B"]


def test_incomplete_reviews_are_rejected(tmp_path):
    fields = ["task_id", "candidate_key", "reviewer_id"]
    paths = []
    for name in ("a.csv", "b.csv"):
        path = tmp_path / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerow({"task_id": "T", "candidate_key": "doi:x", "reviewer_id": name})
        paths.append(path)
    _, errors = merge(*paths)
    assert errors
    assert any("invalid relevance" in error for error in errors)


def test_unresolved_adjudication_blocks_scoring(tmp_path):
    path = tmp_path / "adjudication.csv"
    fields = ["task_id", "candidate_key", "final_" + "relevance", "final_evidence_quality",
              "final_reference_status", "final_facet_ids", "final_year_fit", "final_item_type",
              "final_title_accuracy", "final_authors_accuracy", "final_year_accuracy", "final_venue_accuracy",
              "conflict_fields", "adjudicator_id", "adjudication_rationale"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        writer.writerow({
            "task_id": "T", "candidate_key": "doi:x", "final_relevance": "2",
            "final_evidence_quality": "unknown", "final_reference_status": "unresolved",
            "final_facet_ids": "1", "final_year_fit": "N/A", "final_item_type": "review",
            "final_title_accuracy": "accurate", "final_authors_accuracy": "accurate",
            "final_year_accuracy": "accurate", "final_venue_accuracy": "accurate",
        })
    with pytest.raises(ValueError, match="unresolved reference"):
        read_labels(path)


def test_invalid_final_label_blocks_scoring(tmp_path):
    path = tmp_path / "adjudication.csv"
    fields = ["task_id", "candidate_key", "facet_definitions", *["final_" + field for field in (
        "relevance", "evidence_quality", "reference_status", "facet_ids", "year_fit", "item_type",
        "title_accuracy", "authors_accuracy", "year_accuracy", "venue_accuracy",
    )], "conflict_fields", "adjudicator_id", "adjudication_rationale"]
    row = {field: "" for field in fields}
    row.update({
        "task_id": "T", "candidate_key": "doi:x", "facet_definitions": '["one"]',
        "final_relevance": "9", "final_evidence_quality": "2", "final_reference_status": "verified",
        "final_facet_ids": "2", "final_year_fit": "N/A", "final_item_type": "review",
        "final_title_accuracy": "accurate", "final_authors_accuracy": "accurate",
        "final_year_accuracy": "accurate", "final_venue_accuracy": "accurate",
    })
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerow(row)
    with pytest.raises(ValueError, match="invalid final_relevance"):
        read_labels(path)


def test_task_scores_are_independent():
    papers = [
        {"rank": 1, "doi": "10.1/a", "title": "A"},
        {"rank": 2, "doi": "10.1/b", "title": "B"},
    ]
    base = {
        "final_evidence_quality": "3", "final_reference_status": "verified",
        "final_year_fit": "fits", "final_item_type": "original_research",
        "final_title_accuracy": "accurate", "final_authors_accuracy": "accurate",
        "final_year_accuracy": "accurate", "final_venue_accuracy": "accurate",
        "facet_definitions": '["x", "y"]', "year_rule": "N/A",
    }
    labels = {
        ("T", "doi:10.1/a"): {**base, "final_relevance": "3", "final_facet_ids": "1"},
        ("T", "doi:10.1/b"): {**base, "final_relevance": "2", "final_facet_ids": "2"},
    }
    row = score_task({"task_id": "T", "papers": papers}, {"broad_field": "science"}, labels)
    assert row["relevance_ranking"] == 100
    assert row["facet_coverage"] == 100
    assert row["citation_authenticity"] == 100
    assert row["valid_research_count"] == 10  # two valid papers, fixed denominator 20
    assert row["bibliography_accuracy"] == 100
    assert row["time_appropriateness"] is None
