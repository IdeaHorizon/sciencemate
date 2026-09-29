"""文献本地资产 / 采集补齐全链的行为判据（文献节点内）。

这些用例原本追加在顶层 tests/test_literature_local_assets.py；顶层 tests/ 不在
文献 owner 的 scope 内（见 .scope_map.yaml），故按文献节点的提交边界搬到这里。
断言不变，仍覆盖：跨源摘要补齐、统一 catalog 与三要素目录、index-only 归档保留
更长摘要、周期补齐读共享 catalog、失败不推进成功水位、按订阅集合取到期查询、
资讯图片补齐不进全文链。
"""
from __future__ import annotations

import json
import sqlite3

import nodes.literature.tools.archive_papers as archive_module
from nodes.literature.tools.archive_papers import archive_indexes_only
from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper, SearchManager


def test_cross_source_duplicate_keeps_later_abstract() -> None:
    manager = SearchManager.__new__(SearchManager)
    first = Paper(title="Same paper", doi="10.1234/same", source="crossref")
    second = Paper(
        title="Same paper",
        doi="10.1234/same",
        source="pubmed",
        abstract="The complete source abstract.",
        authors=["A. Author"],
    )

    merged = manager._merge_results([], [first, second])

    assert len(merged) == 1
    assert merged[0].abstract == "The complete source abstract."
    assert merged[0].authors == ["A. Author"]


def test_academic_index_uses_shared_catalog_and_three_component_layout(tmp_path) -> None:
    papers_root = tmp_path / "papers"
    catalog = papers_root / "literature_catalog.sqlite3"
    local_index = LocalPaperIndex(db_path=str(catalog))
    source_paper = Paper(
        title="Shared catalog paper",
        doi="10.1234/shared",
        source="crossref",
        abstract="Original complete abstract returned by a source.",
    )
    local_index.add_papers([source_paper], "shared query")

    records = archive_indexes_only([source_paper.to_dict()], str(papers_root))
    article = papers_root / "10.1234__shared"

    assert (article / "index" / "index.json").is_file()
    assert (article / "paper").is_dir()
    assert (article / "figure").is_dir()
    manifest = json.loads((article / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["index"]["status"] == "acquired"
    assert manifest["components"]["paper"]["status"] == "not_requested"
    assert manifest["components"]["figure"]["status"] == "not_requested"
    assert records[0]["abstract"] == source_paper.abstract

    with sqlite3.connect(catalog) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"paper_index", "papers"} <= tables


def test_index_only_archive_does_not_overwrite_a_longer_abstract(tmp_path) -> None:
    papers_root = tmp_path / "papers"
    archive_indexes_only(
        [{
            "title": "Preserve abstract",
            "doi": "10.1234/preserve",
            "abstract": "A complete source abstract that must be retained.",
        }],
        str(papers_root),
    )
    archive_indexes_only(
        [{"title": "Preserve abstract", "doi": "10.1234/preserve", "abstract": ""}],
        str(papers_root),
    )

    saved = json.loads(
        (papers_root / "10.1234__preserve" / "index" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["abstract"] == "A complete source abstract that must be retained."


def test_periodic_completion_reads_not_requested_assets_from_shared_catalog(
    tmp_path, monkeypatch
) -> None:
    papers_root = tmp_path / "papers"
    archive_indexes_only(
        [{
            "title": "Needs assets",
            "doi": "10.1234/needs-assets",
            "abstract": "Source abstract must survive periodic completion.",
        }],
        str(papers_root),
    )
    seen: list[dict] = []

    def fake_archive(record, output_dir, deadline=None, **kwargs):
        seen.append(dict(record))
        assert kwargs["restore_original_metadata"] is False
        return record

    monkeypatch.setattr(archive_module, "_archive_one", fake_archive)
    totals = archive_module.complete_missing_assets(
        str(papers_root), limit=10, budget_seconds=30
    )

    assert totals == {
        "candidates": 1,
        "completed": 1,
        "failed": 0,
        "deferred": 0,
    }
    assert seen[0]["abstract"] == "Source abstract must survive periodic completion."


def test_failed_harvest_does_not_advance_success_watermark(tmp_path) -> None:
    local = LocalPaperIndex(db_path=str(tmp_path / "papers.db"))
    local.register_harvest_query("issn:1234-5678")
    local.mark_harvest_query(
        "issn:1234-5678", "error", error="HTTP 429"
    )

    assert local.due_harvest_queries(604800, limit=10) == ["issn:1234-5678"]


def test_due_harvest_for_ignores_stale_global_queue(tmp_path) -> None:
    local = LocalPaperIndex(db_path=str(tmp_path / "papers.db"))
    local.register_harvest_queries([
        "issn:old-global-1",
        "issn:old-global-2",
        "issn:subscribed",
    ])

    assert local.due_harvest_queries_for(
        ["issn:subscribed"], 604800, limit=1
    ) == ["issn:subscribed"]


def test_feed_figure_completion_never_enters_fulltext_archive(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        archive_module,
        "retry_asset_records",
        lambda *_args, **_kwargs: [{
            "title": "Figure only",
            "doi": "10.1234/figure-only",
            "fulltext_status": "not_requested",
            "figure_status": "not_requested",
        }],
    )
    seen: list[str] = []
    monkeypatch.setattr(
        archive_module,
        "archive_index_and_figure",
        lambda record, _output: seen.append(record["title"]) or record,
    )
    monkeypatch.setattr(
        archive_module,
        "_archive_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("feed figure completion must not fetch full text")
        ),
    )

    totals = archive_module.complete_missing_figures(str(tmp_path), limit=10)

    assert totals == {"candidates": 1, "completed": 1, "failed": 0, "deferred": 0}
    assert seen == ["Figure only"]
