import json
import pickle

from nodes.literature.tools import local_index
from nodes.literature.tools.search_engines import Paper, SearchManager


def manager(tmp_path):
    result = SearchManager.__new__(SearchManager)
    result._cache_dir = tmp_path
    return result


def test_search_cache_round_trips_versioned_json(tmp_path):
    cache = manager(tmp_path)
    paper = Paper(title='中文标题', doi='10.1/example', authors=['Author'], year=2024,
                  metadata_provenance={'year': {'source': 'crossref'}})
    cache._save_cached_results('query', [paper])
    path = cache._cache_path('query')
    assert path.suffix == '.json'
    assert json.loads(path.read_text())['schema_version'] == 1
    assert cache._load_cached_results('query') == [paper]
    path.write_text('{broken')
    assert cache._load_cached_results('query') is None
    path.write_text(json.dumps({'schema_version': 99, 'papers': []}))
    assert cache._load_cached_results('query') is None


def test_legacy_pickle_is_never_executed(tmp_path):
    marker = tmp_path / 'executed'
    class Probe:
        def __reduce__(self):
            return open, (str(marker), 'w')
    (tmp_path / 'old.pkl').write_bytes(pickle.dumps([Probe()]))
    cache = manager(tmp_path)
    assert cache._load_cached_results('old') is None
    assert not marker.exists()
    cache._save_cached_results('old', [Paper(title='fresh')])
    assert not (tmp_path / 'old.pkl').exists()
    assert not marker.exists()


def test_legacy_sqlite_catalog_is_copied_once_without_losing_original(tmp_path, monkeypatch):
    legacy = tmp_path / 'survey_index' / 'papers.db'
    old = local_index.LocalPaperIndex(str(legacy))
    old.add_papers([Paper(title='Legacy evidence', doi='10.1000/legacy', year=2020)])
    original = legacy.read_bytes()
    destination = tmp_path / 'papers' / 'literature_catalog.sqlite3'
    monkeypatch.setattr(local_index, 'INDEX_DB', destination)
    monkeypatch.setattr(local_index._paths, 'literature_index_dir', lambda: legacy.parent)
    current = local_index.LocalPaperIndex(str(destination))
    assert [p.doi for p in current.search_local('Legacy')] == ['10.1000/legacy']
    assert legacy.read_bytes() == original
    current.add_papers([Paper(title='Current evidence', doi='10.1000/current', year=2025)])
    reopened = local_index.LocalPaperIndex(str(destination))
    assert [p.doi for p in reopened.search_local('Current')] == ['10.1000/current']
    assert legacy.read_bytes() == original
