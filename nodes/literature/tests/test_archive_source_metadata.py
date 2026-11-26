"""Use real search-cache writers and index schema at the archive boundary."""
import json
import pickle
import time

import pytest

from core import paths
from nodes.literature.tools import archive_papers as archive, local_index
from nodes.literature.tools.search_engines import Paper, SearchManager


@pytest.fixture
def storage(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(paths, "literature_cache_dir", lambda: cache)
    database = tmp_path / "papers" / "literature_catalog.sqlite3"
    monkeypatch.setattr(local_index, "INDEX_DB", database)
    monkeypatch.setattr(paths, "literature_index_dir", lambda: tmp_path / "legacy")
    manager = SearchManager.__new__(SearchManager)
    manager._cache_dir = cache
    return manager, database


def source(**kwargs):
    return Paper(**{
        "title": "Original research", "doi": "10.1234/original",
        "abstract": "Complete original abstract from the source.",
        "source": "crossref", "metadata_provenance": {"abstract": "pubmed"},
        **kwargs,
    })


def test_archive_persists_real_index_abstract_even_when_fulltext_budget_is_exhausted(storage, tmp_path):
    _, database = storage
    index = local_index.LocalPaperIndex(str(database))
    paper = source()
    index.add_papers([paper])
    result = archive._archive_one(
        {"title": paper.title, "doi": "https://doi.org/10.1234/ORIGINAL",
         "abstract": "Model paraphrase.", "source": "crossref",
         "metadata_provenance": {"abstract": "model", "venue": "crossref"}},
        str(tmp_path / "archive"), deadline=time.monotonic() - 1,
    )
    saved = json.loads(open(result["index_path"]).read())
    assert result["abstract"] == saved["abstract"] == paper.abstract
    assert saved["metadata_provenance"] == {"abstract": "pubmed", "venue": "crossref"}
    assert saved["abstract_provenance"] == "source_cache"
    assert result["fulltext_status"] == "deferred"


def test_json_only_cache_restores_longest_matching_source_without_creating_database(storage):
    manager, database = storage
    manager._save_cached_results("short", [source(abstract="Short original.")])
    manager._save_cached_results("long", [source()])
    manager._save_cached_results("unrelated", [source(doi="10.1234/other", abstract="Other paper " * 100)])
    selected = {"title": "Original research", "doi": "DOI: 10.1234/ORIGINAL", "abstract": "Paraphrase"}
    assert archive._restore_original_metadata(selected)
    assert selected["abstract"] == source().abstract
    assert selected["source"] == "crossref"
    assert selected["metadata_provenance"]["abstract"] == "pubmed"
    assert not database.exists()


@pytest.mark.parametrize("indexed_abstract", [None, ""])
def test_corrupt_or_empty_index_does_not_block_cache_fallback(storage, indexed_abstract):
    manager, database = storage
    if indexed_abstract is None:
        database.parent.mkdir()
        database.write_text("broken SQLite")
    else:
        local_index.LocalPaperIndex(str(database)).add_papers([source(abstract=indexed_abstract)])
    manager._save_cached_results("valid", [source()])
    selected = {"doi": source().doi}
    assert archive._restore_original_metadata(selected)
    assert selected["abstract"] == source().abstract


def test_index_hit_does_not_scan_query_caches(storage, monkeypatch):
    _, database = storage
    local_index.LocalPaperIndex(str(database)).add_papers([source()])
    def unexpected():
        raise AssertionError("index hit should not scan caches")
    monkeypatch.setattr(paths, "literature_cache_dir", unexpected)
    assert archive._restore_original_metadata({"doi": source().doi})


def test_bad_json_and_old_pickle_are_ignored_without_losing_valid_cache(storage, tmp_path):
    manager, _ = storage
    marker = tmp_path / "pickle-executed"
    class Probe:
        def __reduce__(self):
            return open, (str(marker), "w")
    (manager._cache_dir / "old.pkl").write_bytes(pickle.dumps([Probe()]))
    for i, payload in enumerate(["{broken", "[]", '{"schema_version":99,"papers":[]}',
                                 '{"schema_version":1,"papers":[null]}',
                                 '{"schema_version":1,"papers":[{"unknown":1}]}']):
        (manager._cache_dir / f"bad{i}.json").write_text(payload)
    manager._save_cached_results("valid", [source()])
    assert archive._restore_original_metadata({"doi": source().doi})
    assert not marker.exists()


@pytest.mark.parametrize("doi", ["", "10.1234/not-present"])
def test_missing_source_cannot_turn_model_summary_into_original_abstract(storage, doi):
    manager, _ = storage
    manager._save_cached_results("unrelated", [source()])
    selected = {"doi": doi, "abstract": "Model paraphrase", "metadata_provenance": {"abstract": "model"}}
    assert not archive._restore_original_metadata(selected)
    assert selected["abstract"] == ""
    assert "abstract" not in selected["metadata_provenance"]


@pytest.mark.parametrize("bad_provenance", [None, "model", ["abstract"]])
def test_malformed_model_provenance_does_not_block_source_recovery(storage, bad_provenance):
    manager, _ = storage
    manager._save_cached_results("valid", [source()])
    selected = {"doi": source().doi, "metadata_provenance": bad_provenance}
    assert archive._restore_original_metadata(selected)
    assert selected["metadata_provenance"] == {"abstract": "pubmed"}


def test_legacy_index_does_not_invent_abstract_origin_from_overall_source(storage):
    _, database = storage
    legacy = source(source="crossref", metadata_provenance={})
    local_index.LocalPaperIndex(str(database)).add_papers([legacy])
    selected = {"doi": legacy.doi, "metadata_provenance": {"abstract": "model"}}
    assert archive._restore_original_metadata(selected)
    assert selected["abstract"] == legacy.abstract
    assert selected["source"] == "crossref"
    assert "abstract" not in selected["metadata_provenance"]
