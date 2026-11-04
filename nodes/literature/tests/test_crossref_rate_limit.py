import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, UTC
from email.utils import format_datetime

import httpx
import pytest

from nodes.literature.tools.crossref_http import crossref_list_get, retry_seconds
from nodes.literature.tools.search_engines import CrossrefSearch


@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(tmp_path))


@pytest.mark.asyncio
async def test_primary_title_and_journal_calls_share_spacing_and_concurrency():
    starts, inflight, maximum = [], 0, 0

    async def serve(request):
        nonlocal inflight, maximum
        starts.append(time.monotonic())
        inflight += 1
        maximum = max(maximum, inflight)
        # A slow response must retain the concurrency slot, even after 1s.
        await asyncio.sleep(1.15 if len(starts) == 1 else .02)
        inflight -= 1
        return httpx.Response(200, json={"message": {"items": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as a, \
            httpx.AsyncClient(transport=httpx.MockTransport(serve)) as b:
        await asyncio.gather(
            CrossrefSearch(a).search("digital literacy"),
            CrossrefSearch(b).search("digital literacy", publication_types=("journal-article",)),
            CrossrefSearch(a).search_title("digital literacy"),
        )
    assert maximum == 1
    assert len(starts) == 3
    assert all(right-left >= 1.04 for left, right in zip(starts, starts[1:]))


@pytest.mark.asyncio
async def test_429_retries_once_after_wait_and_preserves_query():
    starts, queries = [], []

    async def serve(request):
        starts.append(time.monotonic())
        queries.append(request.url.params["query"])
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(starts)==1 else \
            httpx.Response(200, json={"message": {"items": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await CrossrefSearch(client).search("civil servants") == []
    assert queries == ["civil servants", "civil servants"]
    assert starts[1]-starts[0] >= 1.04


@pytest.mark.asyncio
async def test_long_cooldown_is_shared_without_requesting_again():
    calls = 0
    async def serve(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        for query in ("first", "second"):
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await CrossrefSearch(client).search(query)
            assert exc.value.response.status_code == 429
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_request_releases_shared_lock():
    entered = asyncio.Event()
    async def stalled(request):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(stalled)) as client:
        task = asyncio.create_task(crossref_list_get(client, "https://api.crossref.org/works"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        assert (await asyncio.wait_for(crossref_list_get(client, "https://api.crossref.org/works"), 2)).status_code == 200


def test_retry_after_http_date_and_invalid_values():
    future = format_datetime(datetime.now(UTC)+timedelta(seconds=60), usegmt=True)
    assert 58 <= retry_seconds(future, 2) <= 60
    assert retry_seconds("broken", 2) == 2
    assert retry_seconds("nan", 2) == 2
    assert retry_seconds(None, 2) == 2


@pytest.mark.asyncio
async def test_independent_processes_share_the_same_request_slot():
    script = '''
import asyncio, time, json
import httpx
from nodes.literature.tools.crossref_http import crossref_list_get
class Client:
    async def get(self, url, **kwargs):
        start = time.time()
        await asyncio.sleep(.1)
        print(json.dumps({'start': start, 'end': time.time()}), flush=True)
        return httpx.Response(200, request=httpx.Request('GET', url))
asyncio.run(crossref_list_get(Client(), 'https://api.crossref.org/works'))
'''
    async def child():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
            env=os.environ.copy(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)
    a, b = sorted(await asyncio.gather(child(), child()), key=lambda row: row['start'])
    assert b['start'] - a['start'] >= 1.04
    assert b['start'] >= a['end']


def test_warning_summary_deduplicates_groups_and_keeps_sources_separate():
    from platform_runtime import _academic_search_warnings
    primary = {"code":"source_request_failed", "sources":["crossref"], "http_status":429, "detail":"HTTP 429:"}
    extra = {"code":"journal_recall_unavailable", "sources":["crossref"], "http_status":429}
    other = {"code":"source_request_failed", "sources":["arxiv"], "http_status":429}
    audits = [{"warnings":[primary, extra, other]}, {"warnings":[primary, extra]}]
    warnings = _academic_search_warnings(audits)
    assert len(warnings) == 2
    assert "crossref" in warnings[0]["detail"] and "429" in warnings[0]["detail"]
    assert warnings[1]["sources"] == ["arxiv"]
    assert audits[0]["warnings"][0]["detail"] == "HTTP 429:"
