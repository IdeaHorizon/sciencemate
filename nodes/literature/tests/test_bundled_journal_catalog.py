"""Journal data works in a clean, read-only installation and remains user-overridable."""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nodes.literature.tools import journal_metrics as metrics


@pytest.fixture(autouse=True)
def clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path / "literature"))
    monkeypatch.delenv("HARNESS_JOURNAL_METRICS_DB", raising=False)
    metrics._lookup_cached.cache_clear()
    yield
    metrics._lookup_cached.cache_clear()


def test_shipped_data_is_complete_and_lookup_needs_no_user_database():
    bundled = metrics.read_database_path()
    before = hashlib.sha256(bundled.read_bytes()).digest()
    facts = metrics.diagnostics()
    assert facts["journals"] == 22396
    assert facts["with_factor"] == 19726
    assert facts["with_cas"] == 21771
    assert facts["metadata"] == {"schema_version": "1", "factor_year": "2024", "cas_year": "2025"}
    record = metrics.lookup_journal_metrics(issn="00489697")
    assert record["journal"] == "Science of The Total Environment"
    assert record["impact_factor_year"] == 2024
    assert metrics.lookup_journal_metrics(journal_abbr="Sci Total Environ")["id"] == record["id"]
    assert not metrics.database_path().exists()
    assert hashlib.sha256(bundled.read_bytes()).digest() == before


def test_user_imports_and_explicit_override_remain_authoritative(tmp_path, monkeypatch):
    user_db = metrics.database_path()
    user_db.parent.mkdir(parents=True)
    user_db.touch()
    assert metrics.read_database_path() == user_db
    explicit = tmp_path / "学校数据 #1" / "journal_metrics.sqlite3"
    monkeypatch.setenv("HARNESS_JOURNAL_METRICS_DB", str(explicit))
    assert metrics.read_database_path() == explicit
    assert metrics.database_path() == explicit
    assert metrics.lookup_journal_metrics(issn="00489697") == {}
    assert not explicit.exists()


def test_packaged_catalogue_can_be_read_from_a_read_only_path_with_spaces(tmp_path, monkeypatch):
    installed = tmp_path / "Program Files #1" / "nodes/literature"
    catalog = installed / "data/reference/journal_metrics.sqlite3"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(metrics.read_database_path().read_bytes())
    catalog.chmod(0o444)
    monkeypatch.setattr(metrics, "__file__", str(installed / "tools/journal_metrics.py"))
    assert metrics.lookup_journal_metrics(issn="00489697")["journal"] == "Science of The Total Environment"
    assert metrics.diagnostics()["journals"] == 22396
    assert sorted(path.name for path in catalog.parent.iterdir()) == ["journal_metrics.sqlite3"]


def test_release_export_drops_private_metadata_and_preserves_the_source(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts/package/build_journal_catalog.py"
    spec = importlib.util.spec_from_file_location("journal_catalog_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    source, target = tmp_path / "source.sqlite3", tmp_path / "release.sqlite3"
    marker = "PRIVATE_IMPORT_PATH_DO_NOT_DISTRIBUTE"
    with closing(sqlite3.connect(source)) as conn:
        metrics._create_schema(conn)
        conn.execute("INSERT INTO journals(id,journal,journal_normalized) VALUES(1,'Example Journal','examplejournal')")
        conn.execute("INSERT INTO journal_aliases VALUES('example',1,'abbreviation')")
        conn.execute("INSERT INTO metadata VALUES('factor_source_path',?)", (marker,))
        conn.execute("INSERT INTO metadata VALUES('factor_year','2024')")
        conn.execute("CREATE TABLE private_notes(note)")
        conn.execute("INSERT INTO private_notes VALUES(?)", (marker,))
        conn.commit()
    before = source.read_bytes()
    assert exporter.export_catalog(source, target) == 1
    assert source.read_bytes() == before
    assert marker.encode() not in target.read_bytes()
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT alias_normalized FROM journal_aliases").fetchone() == ("example",)
        assert dict(conn.execute("SELECT * FROM metadata")) == {"schema_version": "1", "factor_year": "2024"}
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='private_notes'").fetchone()
    with pytest.raises(ValueError, match="separate file"):
        exporter.export_catalog(source, source)
