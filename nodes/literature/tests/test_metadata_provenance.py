"""Value/source consistency across retrieval, durable caches and feed archives."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from nodes.literature.tools import archive_papers as archive
from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper, SearchManager
from nodes.literature.tools.search_papers import _reuse_local_indexes
from shared.contrib.literature_federation import merge_channels, merge_school_papers


def test_discarded_duplicate_cannot_change_abstract_origin():
    first = Paper(title="Research", doi="10.1234/provenance", source="crossref")
    donor = Paper(title=first.title, doi=first.doi, source="pubmed",
                  abstract="Official complete abstract.", metadata_provenance={"abstract": "pubmed"})
    discarded = Paper(title=first.title, doi=first.doi, source="openalex",
                      abstract="Different abstract.", venue="Journal",
                      metadata_provenance={"abstract": "openalex", "venue": "openalex",
                                           "crossref_query_mode": "title"})
    manager = SearchManager.__new__(SearchManager)
    result = manager._merge_results([first], [donor, discarded])
    assert len(result) == 1
    assert result[0].abstract == donor.abstract
    assert result[0].metadata_provenance == {
        "abstract": "pubmed", "venue": "openalex", "crossref_query_mode": "title",
    }


def test_index_reopen_and_update_preserve_origin_of_selected_values(tmp_path):
    path = str(tmp_path / "index.sqlite3")
    index = LocalPaperIndex(path)
    original = Paper(title="Research", doi="10.1234/provenance", source="crossref",
                     abstract="The complete official abstract obtained from PubMed.", venue="Old venue",
                     metadata_provenance={"abstract": "pubmed", "venue": "crossref"})
    assert index.add_papers([original]) == (1, 0)
    # A separate process must recover original origins, not only 'local_index'.
    index = LocalPaperIndex(path)
    loaded = index.get_papers_by_identity([original])[original.doi]
    assert loaded.metadata_provenance == original.metadata_provenance
    updated = Paper(title=original.title, doi=original.doi, abstract="Short abstract.", venue="New venue",
                    metadata_provenance={"abstract": "openalex", "venue": "openalex"})
    assert index.add_papers([updated]) == (1, 0)
    loaded = index.get_papers_by_identity([original])[original.doi]
    assert loaded.abstract == original.abstract
    assert loaded.venue == updated.venue
    assert loaded.metadata_provenance == {"abstract": "pubmed", "venue": "openalex"}
    # An unannotated replacement cannot retain the previous value's claimed origin.
    index.add_papers([Paper(title=original.title, doi=original.doi, venue="Untraced venue")])
    assert index.get_papers_by_identity([original])[original.doi].metadata_provenance == {"abstract": "pubmed"}


def test_old_index_migrates_without_inventing_historical_origins(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    index = LocalPaperIndex(path)
    paper = Paper(title="Legacy", doi="10.1234/legacy", source="crossref", abstract="Historical abstract")
    index.add_papers([paper])
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE paper_index DROP COLUMN metadata_provenance")
    reopened = LocalPaperIndex(path)
    loaded = reopened.get_papers_by_identity([paper])[paper.doi]
    assert loaded.abstract == paper.abstract
    assert loaded.metadata_provenance == {}
    remote = Paper(title=paper.title, doi=paper.doi)
    _reuse_local_indexes(SimpleNamespace(local_index=reopened), [remote])
    assert remote.metadata_provenance["abstract"] == "local_index"


def test_failed_provenance_write_cannot_leave_a_partial_value_update(tmp_path):
    index = LocalPaperIndex(str(tmp_path / "index.sqlite3"))
    original = Paper(title="Original", doi="10.1234/atomic", metadata_provenance={"title": "crossref"})
    index.add_papers([original])
    invalid = Paper(title="Changed", doi=original.doi, metadata_provenance={"title": object()})
    assert index.add_papers([invalid, Paper(title="Another paper", doi="10.1234/valid")]) == (1, 1)
    saved = index.get_papers_by_identity([original])[original.doi]
    assert saved.title == original.title
    assert saved.metadata_provenance == original.metadata_provenance


def test_keyword_search_reuses_actual_cached_abstract_origin(tmp_path):
    index = LocalPaperIndex(str(tmp_path / "index.sqlite3"))
    cached = Paper(title="Research", doi="10.1234/provenance", abstract="A cached abstract",
                   citations=23, metadata_provenance={"abstract": "pubmed", "citations": "openalex"})
    index.add_papers([cached])
    remote = Paper(title=cached.title, doi=cached.doi, metadata_provenance={"abstract": "stale"})
    _reuse_local_indexes(SimpleNamespace(local_index=index), [remote])
    assert remote.abstract == cached.abstract
    assert remote.metadata_provenance["abstract"] == "pubmed"
    assert remote.metadata_provenance["citations"] == "openalex"


@pytest.mark.parametrize("periodic", [False, True])
def test_archive_refresh_keeps_completed_abstract_and_source(tmp_path, monkeypatch, periodic):
    initial = {"title": "Research", "doi": "10.1234/provenance", "authors": ["Author"],
               "abstract": "The complete official abstract obtained from PubMed.",
               "metadata_provenance": {"abstract": "pubmed", "authors": "pubmed"}}
    archive.archive_indexes_only([initial], str(tmp_path))
    incoming = {"title": initial["title"], "doi": initial["doi"], "abstract": "Short.",
                "authors": [], "venue": "Journal", "index_failure_reason": "",
                "metadata_provenance": {"abstract": "openalex", "venue": "openalex"}}
    if periodic:
        monkeypatch.setattr(archive, "fetch_paper_image_url", lambda *a, **k: {"status": "unavailable"})
        monkeypatch.setattr(archive, "generate_title_card", lambda *a, **k: {"status": "unavailable"})
        saved = archive.archive_index_and_figure(incoming, str(tmp_path))
    else:
        saved = archive.archive_indexes_only([incoming], str(tmp_path))[0]
    assert saved["abstract"] == initial["abstract"]
    assert saved["authors"] == initial["authors"]
    expected = {"abstract": "pubmed", "authors": "pubmed", "venue": "openalex"}
    assert saved["metadata_provenance"] == expected
    with sqlite3.connect(tmp_path / "literature_catalog.sqlite3") as conn:
        record = json.loads(conn.execute("SELECT metadata_json FROM papers").fetchone()[0])
    assert record["metadata_provenance"] == expected


def test_federation_keeps_value_origins_and_does_not_mutate_inputs():
    public = {"title": "Research", "doi": "10.1234/provenance", "source": "crossref",
              "venue": "Public venue", "metadata_provenance": {"venue": "crossref"}}
    school = {"title": public["title"], "doi": public["doi"], "source": "School",
              "abstract": "School abstract", "venue": "Other venue", "school_holdings": ["School"],
              "school_fulltext_url": "/school/1", "metadata_provenance": {
                  "school_library": "School", "abstract": "School", "venue": "School"}}
    snapshot = deepcopy([public, school])
    expected = {"venue": "crossref", "abstract": "School", "school_library": "School"}
    assert merge_channels([public], [school])[0]["metadata_provenance"] == expected
    merged = merge_school_papers({"papers": [public]}, [school], limit=20)
    assert merged["papers"][0]["metadata_provenance"] == expected
    assert [public, school] == snapshot
