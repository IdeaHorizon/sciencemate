"""Installed desktop subscriptions use their bundled mapping and report failures."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import database, launcher
from app.config import settings
from app.models.feed import FeedItem
from app.services.feed import domain_progress
from app.services.literature_harvester import (
    JournalMappingUnavailable,
    LiteratureIndexHarvester,
    _journal_targets,
)

MAPPING = (
    "journal_name\t二级学科代码\tissn\tcas_quartile\t分类依据\t分类状态\n"
    "Applied Linguistics\t050201\t0142-6001\t1\tmanual_source_verified\tmanually_verified\n"
)


@pytest.mark.parametrize("configured", [True, False])
def test_mapping_comes_from_the_installed_harness(tmp_path, monkeypatch, configured):
    bundled = tmp_path / "ScienceMate/Resources/python/Lib/site-packages/app/harness"
    mapping = bundled / "docs/xiaohongshu_journal_map.tsv"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(MAPPING, encoding="utf-8")
    monkeypatch.delenv("HARNESS_JOURNAL_DOMAIN_MAP", raising=False)
    monkeypatch.setattr(settings, "harness_root", str(bundled) if configured else "")
    monkeypatch.setattr(launcher, "find_the_harness", lambda: bundled)

    targets = _journal_targets({"050201"}, required=True)

    assert list(targets) == ["issn:0142-6001"]
    assert targets["issn:0142-6001"]["domains"] == ["050201"]


def test_an_explicit_missing_mapping_is_not_replaced_by_another_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(tmp_path / "missing.tsv"))
    with pytest.raises(JournalMappingUnavailable):
        _journal_targets({"050201"}, required=True)
    # Existing feed reads remain available even if the acquisition resource is broken.
    assert _journal_targets({"050201"}) == {}


@pytest.mark.parametrize("contents", [None, "not-a-journal-table\n"])
async def test_mapping_failure_is_visible_and_does_not_block_a_retry(tmp_path, monkeypatch, contents):
    mapping = tmp_path / "mapping.tsv"
    if contents is not None:
        mapping.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path / "literature"))
    monkeypatch.setattr(settings, "literature_harvester_enabled", True)
    harvester = LiteratureIndexHarvester()
    user_id = str(uuid4())

    for _ in range(2):
        assert harvester.request_online_refresh(user_id=user_id, domains=["050201"])
        task = harvester._online_tasks[str(user_id)]
        assert await task is False
        await asyncio.sleep(0)
        assert not harvester.is_online_refreshing(user_id)
        assert domain_progress.read(user_id)["050201"]["state"] == "failed"
        assert str(user_id) not in harvester._last_online_refresh


@pytest.mark.parametrize("failures", [0, 1])
async def test_only_successful_refreshes_get_the_success_cooldown(db_engine, monkeypatch, failures):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings, "literature_harvester_enabled", True)
    harvester = LiteratureIndexHarvester()
    monkeypatch.setattr(harvester, "run_once", AsyncMock(return_value={"failed": failures}))
    user_id = str(uuid4())

    assert harvester.request_online_refresh(user_id=user_id, domains=["050201"])
    assert await harvester._online_tasks[str(user_id)] is (failures == 0)
    await asyncio.sleep(0)

    assert (str(user_id) in harvester._last_online_refresh) is (failures == 0)
    assert domain_progress.read(user_id)["050201"]["state"] == (
        "completed" if failures == 0 else "failed"
    )


async def test_selected_domain_fetches_and_projects_a_paper_with_the_installed_mapping(
    tmp_path, monkeypatch, db_engine
):
    from nodes.literature.tools.journal_policy import reset_cache
    from nodes.literature.tools.search_engines import Paper, SearchManager, SearchResults

    mapping = tmp_path / "bundled-harness/docs/xiaohongshu_journal_map.tsv"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(MAPPING, encoding="utf-8")
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path / "literature"))
    reset_cache()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    paper = Paper(
        title="Learning English vocabulary through classroom interaction",
        doi="10.1234/desktop-mapping-test", source="crossref",
        venue="Applied Linguistics", issn="0142-6001",
        year=datetime.now(UTC).year, pub_date=datetime.now(UTC).date().isoformat(),
        abstract="A controlled study of vocabulary learning in English language classrooms.",
    )
    fetch = AsyncMock(return_value=SearchResults(
        [paper], source_diagnostics={"crossref": {"status": "ok", "raw_count": 1}}
    ))
    monkeypatch.setattr(SearchManager, "search_all", fetch)
    harvester = LiteratureIndexHarvester()
    monkeypatch.setattr(harvester, "_complete_assets", AsyncMock())
    try:
        report = await harvester.run_once(domain_codes={"050201"}, online=True)
        if harvester._asset_task:
            await harvester._asset_task
        assert fetch.await_args.args[0] == "issn:0142-6001"
        assert report["queries"] == report["accepted"] == report["archived"] == 1
        assert report["failed"] == 0
        assert report["projected"] == 1
        async with factory() as db:
            item = await db.scalar(select(FeedItem).where(
                FeedItem.canonical_key == "doi:10.1234/desktop-mapping-test"
            ))
            assert item is not None
            assert "050201" in item.domains
    finally:
        reset_cache()
