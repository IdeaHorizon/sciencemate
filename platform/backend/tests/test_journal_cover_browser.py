import asyncio
from collections import defaultdict
import hashlib
import io
from types import SimpleNamespace

from PIL import Image
import pytest

from app.services.feed import journal_covers as covers
from app.services.feed import journal_cover_browser as browser


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "cache_dir", lambda: tmp_path)


def test_oup_main_cover_is_kept_and_related_journals_are_excluded():
    journal = covers.Journal("African Affairs", ("0001-9909",), "OUP")
    page = {"title": "African Affairs | Oxford Academic", "url": "https://academic.oup.com/afraf",
            "html": "ISSN 0001-9909", "images": [
                {"src": "https://cdn.test/afraf-cover.jpeg", "alt": "African Affairs Cover Image for Volume 125, Issue 499"},
                {"src": "https://academic.oup.com/wber/current/cover/small", "alt": "Cover image of current issue from The World Bank Economic Review"},
            ]}
    assert browser.candidates(journal, page) == ["https://cdn.test/afraf-cover.jpeg"]


def test_wrong_journal_page_cannot_supply_cover():
    with pytest.raises(ValueError, match="identity_unconfirmed"):
        browser.candidates(covers.Journal("Nature", ("0028-0836",), ""), {
            "title": "Science", "url": "https://publisher.test/science", "html": "ISSN 0036-8075", "images": [],
        })
    with pytest.raises(ValueError, match="identity_unconfirmed"):
        browser.candidates(covers.Journal("Nature", (), ""), {
            "title": "Nature Communications", "url": "https://publisher.test/wrong", "html": "", "images": [],
        })


def test_confirmed_source_name_handles_long_catalogue_title():
    journal = covers.Journal("Accountability in Research-Ethics Integrity and Policy", ("0898-9621",), "")
    covers._write(covers.cache_dir() / f"{journal.key}.source.json", {
        "status": "resolved", "source_name": "Accountability in Research", "issns": ["0898-9621"],
    })
    assert browser.candidates(journal, {
        "title": "Accountability in Research | Taylor & Francis Online", "html": "",
        "url": "https://publisher.test/journal", "images": [
            {"src": "https://publisher.test/cover.jpg", "alt": "Cover image for Accountability in Research"},
        ],
    }) == ["https://publisher.test/cover.jpg"]


def test_normal_publisher_page_with_cloudflare_script_is_not_a_challenge():
    assert not browser.challenged({
        "title": "Accounts of Chemical Research | ACS Publications",
        "html": '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>',
    })
    assert browser.challenged({"title": "Just a moment...", "html": ""})


@pytest.mark.asyncio
async def test_restarted_browser_releases_only_closed_verification_slots(monkeypatch):
    client = browser.Browser("http://127.0.0.1:29222")
    client.gates = {"old.test": {"target_id": "vanished"},
                    "live.test": {"target_id": "still-open"}}
    async def tabs():
        return [{"id": "still-open"}]
    monkeypatch.setattr(client, "tabs", tabs)
    try:
        await client.prune_closed_verifications()
        assert set(client.gates) == {"live.test"}
        assert set(covers._read(covers.cache_dir() / "browser-verification.json")) == {"live.test"}
    finally:
        await client.client.aclose()


def test_lazy_cover_does_not_use_placeholder_image():
    journal = covers.Journal("Active Learning in Higher Education", (), "Sage")
    page = {
        "title": "Active Learning in Higher Education: Sage Journals",
        "url": "https://journals.sagepub.com/home/alh", "html": "",
        "images": [{"src": "data:image/gif;base64,placeholder",
                    "dataSrc": "/cms/asset/alha_27_3.largecover.png", "alt": ""}],
    }
    expected = ["https://journals.sagepub.com/cms/asset/alha_27_3.largecover.png"]
    assert browser.candidates(journal, page) == expected
    page["images"] = []
    page["html"] = '<img src="data:image/gif;base64,placeholder" data-src="/cms/asset/alha_27_3.largecover.png">'
    assert browser.candidates(journal, page) == expected


def test_old_urls_use_current_verified_publisher_routes():
    assert browser.canonical_url("http://afraf.oxfordjournals.org/") == "https://academic.oup.com/afraf"
    assert browser.canonical_url("http://asq.sagepub.com/") == "https://journals.sagepub.com/home/asq"
    assert browser.canonical_url("https://www.tandfonline.com/toc/gacr20/current") == "https://www.tandfonline.com/journals/gacr20"


def test_large_cover_is_resized_and_still_cached_as_image():
    data = io.BytesIO()
    Image.new("RGB", (1132, 1600), "blue").save(data, format="PNG")
    result = covers.validate_image(data.getvalue(), "image/png", "https://publisher.test/largecover.png")
    assert result
    body, metadata = result
    assert metadata["width"] <= 768 and metadata["height"] <= 1024
    assert metadata["original_sha256"] == hashlib.sha256(data.getvalue()).hexdigest()
    assert metadata["content_type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8")


def test_browser_disabled_by_default_and_only_accepts_loopback():
    assert browser.config() == {}
    covers._write(covers.cache_dir() / "browser-config.json", {"enabled": True, "cdp_url": "http://remote.test:9222"})
    assert browser.config() == {}


@pytest.mark.asyncio
async def test_challenge_tab_is_preserved_without_blocking_another_publisher(monkeypatch):
    class Browser:
        locks = defaultdict(asyncio.Lock)
        gates = {}
        async def blocked(self, host):
            return host in self.gates
        async def page(self, tab, url):
            if "blocked.test" in url:
                return {"title": "Just a moment...", "html": "<title>Just a moment...</title>", "url": url}
            return {"title": "Accessible Journal", "html": "", "url": url, "images": []}
        def save_gates(self):
            pass
    instance = Browser()
    blocked = covers.Journal("Blocked Journal", (), "", "https://blocked.test/journal")
    okay = covers.Journal("Accessible Journal", (), "", "https://accessible.test/journal")
    assert await browser.process(instance, {"id": "human-tab"}, blocked) is True
    assert instance.gates["blocked.test"]["target_id"] == "human-tab"
    assert await browser.process(instance, {"id": "worker-tab"}, okay) is False
    assert covers._read(covers.cache_dir() / f"{okay.key}.browser.json")["status"] == "cover_not_found"


@pytest.mark.asyncio
async def test_successful_browser_cover_is_in_shared_cache_and_skipped_next_round(monkeypatch):
    journal = covers.Journal("Example Journal", (), "", "https://publisher.test/journal")
    data = io.BytesIO()
    Image.new("RGB", (150, 200), "blue").save(data, format="PNG")
    async def download(client, url):
        return covers.validate_image(data.getvalue(), "image/png", url)
    monkeypatch.setattr(covers, "_download", download)
    class Browser:
        locks = defaultdict(asyncio.Lock)
        gates = {}
        async def blocked(self, host): return False
        async def page(self, tab, url):
            return {"title": journal.name, "url": url, "html": "", "images": [
                {"src": "https://publisher.test/cover.png", "alt": "Journal cover"},
            ]}
    assert await browser.process(Browser(), {"id": "tab"}, journal) is False
    assert not browser.due(journal)
    entry = covers._read(covers.cache_dir() / f"{journal.key}.json")
    assert entry["method"] == "verified_browser"
    assert (covers.cache_dir() / f"{journal.key}.image").read_bytes() == data.getvalue()


@pytest.mark.asyncio
async def test_timed_out_worker_tab_is_replaced_before_next_journal(monkeypatch):
    journals = [covers.Journal(n, (), "", "https://publisher.test/" + n)
                for n in ("Broken", "HumanVerification", "Healthy")]
    created, closed, visited = [], [], []
    class Browser:
        def __init__(self, url):
            self.client = SimpleNamespace(aclose=self.aclose)
        async def aclose(self): pass
        async def tabs(self): return []
        async def prune_closed_verifications(self): pass
        async def new_tab(self):
            tab = {"id": str(len(created))}
            created.append(tab)
            return tab
        async def close_tab(self, tab): closed.append(tab["id"])
    async def process(instance, tab, journal):
        visited.append((journal.name, tab["id"]))
        if journal.name == "Broken": raise TimeoutError()
        return journal.name == "HumanVerification"
    monkeypatch.setattr(browser, "Browser", Browser)
    monkeypatch.setattr(browser, "config", lambda: {"cdp_url": "http://localhost:29222"})
    monkeypatch.setattr(browser, "candidate_index", lambda: {j.key: j for j in journals})
    monkeypatch.setattr(browser, "enriched", lambda j: j)
    monkeypatch.setattr(browser, "process", process)
    monkeypatch.setattr(browser, "report", lambda journals, **kw: kw)
    monkeypatch.setattr(browser, "WORKERS", 1)
    result = await browser.warm_missing()
    assert visited == [("Broken", "0"), ("HumanVerification", "1"), ("Healthy", "2")]
    assert closed == ["0", "2"]
    assert result["completed"] == 3
