import asyncio
import hashlib
import io
import time
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from app.services.feed import journal_covers as covers
from app.services.feed import journal_cover_inventory as inventory
from app.services.feed import link_preview


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "cache_dir", lambda: tmp_path)
    inventory.candidate_index.cache_clear()
    yield
    inventory.candidate_index.cache_clear()


def bitmap(size=(150, 200)):
    output = io.BytesIO()
    Image.new("RGB", size, "navy").save(output, format="PNG")
    return output.getvalue()


def test_inventory_covers_all_pools_and_merges_repeat_mapping(monkeypatch):
    rows = {
        "cas_q1_q2_journals.tsv": [{"journal_name": "Journal One", "issn": "1234-5678"}],
        "xiaohongshu_journal_map.tsv": [
            {"journal_name": "Journal Two", "issn": "2345-6789"},
            {"journal_name": "Journal Two", "issn": "3456-7890"},
        ],
        "xiaohongshu_generic_journal_pool.tsv": [{"journal_name": "Journal Three"}],
    }
    monkeypatch.setattr(inventory, "_rows", lambda fn, env="": rows.get(fn, []))
    journals = list(inventory.candidate_index().values())
    assert {j.name for j in journals} == {"Journal One", "Journal Two", "Journal Three"}
    assert next(j for j in journals if j.name == "Journal Two").issns == ("2345-6789", "3456-7890")


def test_source_match_requires_identifier_or_exact_name():
    journal = covers.Journal("Optics Express", ("1094-4087",), "")
    wrong = {"display_name": "Biomedical Optics Express", "issn": ["2156-7085"]}
    assert inventory._source_entry(journal, [wrong])["status"] == "unresolved"
    assert inventory._source_entry(journal, [wrong], by_name=True)["status"] == "unresolved"
    right = {"id": "source", "display_name": journal.name, "issn": list(journal.issns),
             "homepage_url": "https://publisher.test/oe", "host_organization_name": "Optica"}
    assert inventory._source_entry(journal, [wrong, right])["source_id"] == "source"


def test_cover_parser_rejects_related_journal_logo_and_challenge_images():
    parser = covers._HomepageCovers(covers.Journal("Example Journal", (), ""), "https://publisher.test/journal")
    parser.feed("""
      <title>Example Journal</title>
      <img alt="Journal cover" src="/journal-cover/example.jpg">
      <img alt="Other unrelated journal cover" src="/cover/other.jpg">
      <img alt="Cover" src="/logo.png">
      <img alt="Cover" src="https://captcha.perfdrive.com/captcha-public/images/ss_captcha.png">
      <img alt="Research figure" src="/figure.jpg">
    """)
    assert parser.images == ["https://publisher.test/journal-cover/example.jpg"]


def test_optica_parser_matches_exact_journal():
    parser = covers._OpticaCovers()
    parser.feed('<img alt="Biomedical Optics Express cover" src="https://opg.optica.org/images/boe.jpg">')
    assert covers.journal_key("Optics Express") not in parser.covers


@pytest.mark.asyncio
async def test_cache_reused_across_articles_and_requests_deduplicated(monkeypatch):
    journal = covers.Journal("Journal One", (), "")
    monkeypatch.setattr(covers, "identify", lambda item: journal)
    calls = []
    gate = asyncio.Event()

    async def refresh(value):
        calls.append(value)
        await gate.wait()
        body = bitmap()
        (covers.cache_dir() / f"{value.key}.image").write_bytes(body)
        covers._write(covers.cache_dir() / f"{value.key}.json", {
            "status": "available", "checked_at": time.time(), "content_type": "image/png",
            "sha256": hashlib.sha256(body).hexdigest(),
        })

    monkeypatch.setattr(covers, "refresh", refresh)
    assert covers.get_or_schedule(object()) == (None, True)
    assert covers.get_or_schedule(object()) == (None, True)
    await asyncio.sleep(0)
    assert len(calls) == 1
    gate.set()
    await covers.ensure(journal)
    asset, pending = covers.get_or_schedule(object())
    assert asset == (bitmap(), "image/png")
    assert not pending
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_lookup_has_negative_cache(monkeypatch):
    journal = covers.Journal("Journal One", (), "")
    monkeypatch.setattr(covers, "identify", lambda item: journal)
    covers._write(covers.cache_dir() / f"{journal.key}.json", {
        "status": "unavailable", "checked_at": time.time(), "lookup_version": covers.LOOKUP_VERSION,
    })
    assert covers.get_or_schedule(object()) == (None, False)
    assert not covers.needs_refresh(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["html", "landscape", "captcha", "valid"])
async def test_download_validates_actual_image_and_ui_hash(monkeypatch, kind):
    from shared.contrib import paper_image_policy as policy
    body = b"<html>captcha</html>" if kind == "html" else bitmap((500, 100) if kind == "landscape" else (150, 200))
    if kind == "captcha":
        monkeypatch.setitem(policy.KNOWN_UI_IMAGE_HASHES, hashlib.sha256(body).hexdigest(), "captcha")

    async def public(url):
        pass
    monkeypatch.setattr(link_preview, "_assert_reachable_and_public", public)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, content=body, headers={"content-type": "image/png"},
    ))) as client:
        result = await covers._download(client, "https://publisher.test/journal-cover.png")
    assert bool(result) == (kind == "valid")


@pytest.mark.asyncio
@pytest.mark.parametrize("has_figure", [True, False])
async def test_api_prioritizes_paper_figure_and_labels_journal_cover(tmp_path, monkeypatch, has_figure):
    from app.api.v1 import feed
    paper = tmp_path / "figure.png"
    paper.write_bytes(bitmap())
    item = SimpleNamespace(id="item", kind="paper", title="Paper", extra={}, domains=[],
                           url="https://doi.org/10.1234/test", image_url="", venue="Journal", source_id=None)
    class DB:
        async def scalar(self, statement):
            return item
    monkeypatch.setattr(feed, "_visible_clause", lambda user: True)
    monkeypatch.setattr(feed, "_local_literature_figure", lambda item: paper if has_figure else None)
    monkeypatch.setattr(feed.thumbnail, "cache_path", lambda item: tmp_path / "absent")
    monkeypatch.setattr(covers, "get_or_schedule", lambda item: ((bitmap(), "image/png"), False))
    response = await feed.get_item_image("item", user=SimpleNamespace(), db=DB())
    assert response.headers["X-Feed-Image-Kind"] == ("paper_figure" if has_figure else "journal_cover")


def test_news_does_not_use_journal_cover():
    assert covers.identify(SimpleNamespace(kind="news", venue="Nature")) is None


@pytest.mark.asyncio
async def test_compressed_homepage_decoded_once(monkeypatch):
    import gzip
    async def public(url):
        pass
    monkeypatch.setattr(link_preview, "_assert_reachable_and_public", public)
    content = b"<title>Example Journal</title><img alt='Journal cover' src='/cover.jpg'>"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, content=gzip.compress(content), headers={"content-encoding": "gzip"},
    ))) as client:
        result = await covers._homepage_candidates(covers.Journal(
            "Example Journal", (), "", "https://publisher.test/journal",
        ), client)
    assert result == [("https://publisher.test/cover.jpg", "https://publisher.test/journal")]


@pytest.mark.asyncio
async def test_cached_inventory_skips_fetches_and_reports_off_event_loop(monkeypatch):
    import threading
    from functools import lru_cache
    journals = [covers.Journal(f"Cached Journal {i}", (), "") for i in range(210)]
    monkeypatch.setattr(inventory, "candidate_index", lru_cache()(lambda: {j.key: j for j in journals}))
    monkeypatch.setattr(inventory, "enriched", lambda j: j)
    monkeypatch.setattr(covers, "needs_refresh", lambda j: False)
    async def resolve(items):
        pass
    async def unexpected_fetch(journal):
        pytest.fail("cached cover was fetched again")
    monkeypatch.setattr(inventory, "resolve_sources", resolve)
    monkeypatch.setattr(covers, "ensure", unexpected_fetch)
    main_thread = threading.get_ident()
    reports = []
    def report(items, *, running):
        assert threading.get_ident() != main_thread
        reports.append(running)
        return {"running": running}
    monkeypatch.setattr(inventory, "write_report", report)
    assert await inventory.warm_all() == {"running": False}
    assert reports == [True, False]
