from __future__ import annotations

from nodes.literature.tools.literature_catalog import upsert_paper
from platform_runtime import _local_literature_assets


def test_academic_search_returns_archived_figure_urls(tmp_path, monkeypatch) -> None:
    literature_home = tmp_path / "literature"
    papers_root = literature_home / "papers"
    article = papers_root / "10.1234__search-figure"
    figure = article / "figure" / "result.webp"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"webp")
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(literature_home))

    upsert_paper(
        papers_root,
        {
            "doi": "10.1234/search-figure",
            "title": "Search result with a local figure",
            "article_dir": str(article),
            "image": {"status": "available", "path": str(figure)},
        },
    )

    assets = _local_literature_assets([{"doi": "10.1234/search-figure"}])
    assert assets["10.1234/search-figure"]["local_figure_urls"] == [
        "/api/v1/literature/asset?doi=10.1234%2Fsearch-figure&kind=figure&name=result.webp"
    ]
