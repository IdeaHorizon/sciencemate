"""A clean desktop install can find journals before it has any private metrics."""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1.feed import search_subscriptions
from app.services.feed import journal_search


@pytest.fixture(autouse=True)
def empty_catalog(tmp_path, monkeypatch):
    path = tmp_path / "ScienceMate data #1/literature/reference/journal_metrics.sqlite3"
    monkeypatch.setenv("HARNESS_JOURNAL_METRICS_DB", str(path))
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path / "literature"))
    journal_search._cache.clear()
    yield path
    journal_search._cache.clear()


def remote_row(title="Water Research", issn="0043-1354"):
    return {"title": title, "ISSN": [issn], "issn-type": [{"type": "print", "value": issn}]}


def response(message, status=200):
    return httpx.Response(status, json={"message": message}, request=httpx.Request("GET", "https://api.crossref.org/journals"))


async def test_clean_install_can_find_and_select_a_journal_without_creating_a_metrics_db(empty_catalog, monkeypatch):
    # Exact Water Research comes after a fuzzy match in the real public directory.
    fetch = AsyncMock(return_value=response({"items": [remote_row("Water Resources Research", "0043-1397"), remote_row()]}))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    result = await search_subscriptions(q="Water Research", kind="journal", limit=1, user=None, db=None, lang="zh")
    assert len(result.journals) == 1
    journal = result.journals[0]
    assert journal.name == "Water Research" and journal.key == journal.issn == "0043-1354"
    assert journal.impact_factor is None and journal.jcr_quartile is None
    assert not empty_catalog.exists()
    assert fetch.await_args.args[1] == "https://api.crossref.org/journals"
    assert fetch.await_args.kwargs["params"] == {"query": "Water Research", "rows": 20}


@pytest.mark.parametrize("query", ["00431354", "0043-1354"])
async def test_issn_lookup_uses_a_journal_record_not_an_article_search(query, monkeypatch):
    fetch = AsyncMock(return_value=response(remote_row()))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    rows = await journal_search.search(query)
    assert rows[0]["name"] == "Water Research"
    assert fetch.await_args.args[1] == "https://api.crossref.org/journals/0043-1354"
    assert fetch.await_args.kwargs["params"] == {}


async def test_existing_local_metadata_and_abbreviation_lookup_are_preserved(empty_catalog, monkeypatch):
    empty_catalog.parent.mkdir(parents=True)
    with sqlite3.connect(empty_catalog) as conn:
        conn.execute("CREATE TABLE journals(journal, journal_abbr, issn, eissn, impact_factor, jcr_quartile)")
        conn.execute("INSERT INTO journals VALUES(?,?,?,?,?,?)", ("Water Research", "Water Res", "0043-1354", "1879-2448", 12.0, "Q1"))
    fetch = AsyncMock(side_effect=AssertionError("a local hit must not need the network"))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    rows = await journal_search.search("Water Res")
    assert rows[0]["impact_factor"] == 12.0 and rows[0]["jcr_quartile"] == "Q1"
    assert rows[0]["eissn"] == "1879-2448"
    fetch.assert_not_awaited()


async def test_a_broken_private_metrics_database_does_not_hide_public_candidates(empty_catalog, monkeypatch):
    empty_catalog.parent.mkdir(parents=True)
    empty_catalog.write_bytes(b"not a database")
    fetch = AsyncMock(return_value=response({"items": [remote_row()]}))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    assert (await journal_search.search("Water Research"))[0]["issn"] == "0043-1354"
    assert empty_catalog.read_bytes() == b"not a database"


async def test_directory_results_are_cached_without_sharing_mutable_rows(monkeypatch):
    fetch = AsyncMock(return_value=response({"items": [remote_row("ACS ES&amp;T Water", "2690-0637")]}))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    rows = await journal_search.search("ACS ES&T Water")
    assert rows[0]["name"] == "ACS ES&T Water"
    rows[0]["name"] = "changed by caller"
    assert (await journal_search.search("ACS ES&T Water"))[0]["name"] == "ACS ES&T Water"
    assert fetch.await_count == 1


@pytest.mark.parametrize("status", [429, 500])
async def test_source_failures_are_visible_and_retryable_not_empty_success(status, monkeypatch):
    failure = response({}, status)
    fetch = AsyncMock(side_effect=httpx.HTTPStatusError("unavailable", request=failure.request, response=failure))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    with pytest.raises(HTTPException) as error:
        await search_subscriptions(q="Water Research", kind="journal", limit=20, user=None, db=None, lang="zh")
    assert error.value.status_code == 503 and "期刊目录" in error.value.detail
    fetch.side_effect = None
    fetch.return_value = response({"items": [remote_row()]})
    assert (await journal_search.search("Water Research"))[0]["issn"] == "0043-1354"
    assert fetch.await_count == 2


@pytest.mark.parametrize("message", [None, {}, {"items": "invalid"}])
async def test_malformed_success_response_is_not_reported_as_no_matches(message, monkeypatch):
    monkeypatch.setattr(journal_search, "crossref_list_get", AsyncMock(return_value=response(message)))
    with pytest.raises(journal_search.JournalSearchUnavailable):
        await journal_search.search("Water Research")


async def test_genuine_empty_result_and_unknown_issn_remain_empty(monkeypatch):
    fetch = AsyncMock(return_value=response({"items": []}))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    assert await journal_search.search("No such journal") == []
    missing = response({}, 404)
    fetch.side_effect = httpx.HTTPStatusError("not found", request=missing.request, response=missing)
    assert await journal_search.search("0000-0000") == []


async def test_empty_query_does_not_start_a_network_lookup(monkeypatch):
    fetch = AsyncMock(side_effect=AssertionError("empty input must not search"))
    monkeypatch.setattr(journal_search, "crossref_list_get", fetch)
    assert await journal_search.search("  ") == []
