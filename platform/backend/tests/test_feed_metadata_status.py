
import httpx
import pytest

from app.services.feed import abstract_backfill, literature_projection
from nodes.literature.tools.archive_papers import archive_indexes_only


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected,complete", [(200, "not_found", True), (429, "retry_pending", False), (403, "access_blocked", False)])
async def test_source_absence_failures_and_access_denials_have_distinct_states(monkeypatch, status, expected, complete):
    async def pubmed(*args, **kwargs):
        return []
    monkeypatch.setattr(abstract_backfill.PubMedSearch, "search", pubmed)
    def handler(request):
        return httpx.Response(status, json={"message": {}, "abstract_inverted_index": {}})
    record = {"doi": "10.1234/test", "literature_preparation": "ready"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        saved = await abstract_backfill._lookup(record, client)
    assert saved["abstract_status"] == expected
    assert saved["abstract_lookup_completed"] is complete
    if not complete:
        assert saved["abstract_lookup_retry_after"]
        assert literature_projection.preparation_status(saved) == "pending"


def test_legacy_failed_completion_is_eligible_for_retry(tmp_path, monkeypatch):
    record = {"title": "Research", "doi": "10.1234/test", "profile_user_ids": ["user"],
              "abstract_lookup_completed": True,
              "abstract_lookup_source_state": {"crossref": {"result": "http_403"}}}
    archive_indexes_only([record], str(tmp_path))
    monkeypatch.setattr(abstract_backfill, "paper_cache_dir", lambda: str(tmp_path))
    assert len(abstract_backfill._candidates("user", None, 20)) == 1


def test_legacy_ready_without_detailed_attempts_stays_visible():
    legacy = {"abstract_enrichment_attempted": True, "literature_preparation": "ready"}
    assert literature_projection.preparation_status(legacy) == "ready"
