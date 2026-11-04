"""Optional local Chrome worker. Human verification stays in the visible browser."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict
import html
import io
import json
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image

from app.services.feed import journal_covers as covers
from app.services.feed.journal_cover_inventory import candidate_index, enriched
from app.services.feed.link_preview import _assert_reachable_and_public

logger = logging.getLogger(__name__)
VERSION = 5
WORKERS = 3
MAX_MANUAL_TABS = 16


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.endswith((".oxfordjournals.org", ".oupjournals.org")) and host.split(".")[0] != "www":
        return "https://academic.oup.com/" + host.split(".")[0]
    if host.endswith(".sagepub.com") and host.split(".")[0] not in {"www", "us", "uk", "journals", "in"}:
        return "https://journals.sagepub.com/home/" + host.split(".")[0]
    if host == "www.tandfonline.com" and (m := re.match(r"/(?:loi|toc)/([^/]+)", p.path)):
        return "https://www.tandfonline.com/journals/" + m[1]
    return urlunsplit(("https", p.netloc, p.path, p.query, "")) if p.scheme in {"http", "https"} else url


def scope(url: str) -> str:
    host = urlsplit(url).hostname or ""
    for domain in ("sagepub.com", "oup.com", "oxfordjournals.org", "tandfonline.com",
                   "cambridge.org", "acs.org", "nature.com", "rsc.org", "annualreviews.org"):
        if host == domain or host.endswith("." + domain):
            return domain
    return host


def config() -> dict:
    value = covers._read(covers.cache_dir() / "browser-config.json")
    p = urlsplit(str(value.get("cdp_url", "")))
    if not value.get("enabled") or p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return {}
    return value


def due(journal: covers.Journal) -> bool:
    if covers.metadata(journal).get("status") == "available":
        return False
    state = covers._read(covers.cache_dir() / f"{journal.key}.browser.json")
    return state.get("version") != VERSION or time.time() >= float(state.get("retry_at", 0))


async def command(ws_url: str, method: str, **params) -> dict:
    import websockets
    p = urlsplit(ws_url)
    if p.scheme != "ws" or p.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("browser_debug_endpoint_not_local")
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        async with asyncio.timeout(15):
            while True:
                data = json.loads(await ws.recv())
                if data.get("id") == 1:
                    if "error" in data:
                        raise RuntimeError("browser_command_failed:" + method)
                    return data.get("result", {})


SNAPSHOT = """JSON.stringify({title:document.title,url:location.href,
 ready:document.readyState,html:document.documentElement.outerHTML.slice(0,3000000),
 images:Array.from(document.images).map(i=>({src:i.currentSrc||i.src,dataSrc:i.getAttribute('data-src'),alt:i.alt,
 className:i.className,width:i.naturalWidth,height:i.naturalHeight})).slice(0,300)})"""


async def snapshot(tab: dict) -> dict:
    data = await command(tab["webSocketDebuggerUrl"], "Runtime.evaluate", expression=SNAPSHOT, returnByValue=True)
    return json.loads(data.get("result", {}).get("value", "{}"))


def challenged(page: dict) -> bool:
    title = str(page.get("title") or "")
    # Normal journal pages also contain Cloudflare's background JS detection.
    # Its script URL alone does not mean this page needs human verification.
    return bool(re.search(
        r"just a moment|attention required|access denied|security check|verify.*human|robot check|"
        r"human verification|checking your browser|request rejected|pardon our interruption", title, re.I))


def candidates(journal: covers.Journal, page: dict) -> list[str]:
    title = page.get("title", "")
    markup = page.get("html", "")
    if challenged(page):
        raise ValueError("waiting_for_verification")
    source = covers._read(covers.cache_dir() / f"{journal.key}.source.json")
    aliases = [journal.name]
    if source.get("status") == "resolved" and source.get("source_name"):
        aliases.append(source["source_name"])
    title_parts = re.split(r"[|:]|\s[-–—]\s", html.unescape(title))
    title_names = {covers.journal_key(re.sub(r"(?i)^learn about\s+|\s+home$", "", p.strip())) for p in title_parts}
    identity = bool({covers.journal_key(n) for n in aliases} & title_names)
    identity = identity or bool(set(covers._issns(markup)) & set(journal.issns))
    if not identity:
        raise ValueError("homepage_journal_identity_unconfirmed")
    # DOM currentSrc includes lazy-loaded/srcset assets that are absent from raw HTML.
    parser = covers._HomepageCovers(journal, page["url"])
    parser.title = title
    for image in page.get("images", []):
        parser.handle_starttag("img", [
            ("src", image.get("src", "")), ("alt", image.get("alt", "")),
            ("data-src", image.get("dataSrc", "")),
            ("class", str(image.get("className", ""))),
        ])
    # Background tabs may not trigger lazy loading. Use declared image URLs too.
    parser.feed(markup)
    return parser.images[:3]


class Browser:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=8, trust_env=False)
        self.gates = covers._read(covers.cache_dir() / "browser-verification.json")
        self.locks = defaultdict(asyncio.Lock)
        self.open_lock = asyncio.Lock()

    async def tabs(self) -> list[dict]:
        response = await self.client.get(self.url + "/json/list")
        response.raise_for_status()
        return [x for x in response.json() if x.get("type") == "page"]

    async def new_tab(self) -> dict:
        async with self.open_lock:
            response = await self.client.get(self.url + "/json/version")
            response.raise_for_status()
            result = await command(response.json()["webSocketDebuggerUrl"], "Target.createTarget",
                                   url="about:blank", background=True)
            for _ in range(10):
                for tab in await self.tabs():
                    if tab["id"] == result["targetId"]:
                        return tab
                await asyncio.sleep(.1)
            raise RuntimeError("browser_tab_not_created")

    async def close_tab(self, tab: dict):
        try:
            await self.client.get(self.url + "/json/close/" + tab["id"])
        except httpx.HTTPError:
            pass

    def save_gates(self):
        covers._write(covers.cache_dir() / "browser-verification.json", self.gates)

    async def blocked(self, host: str) -> bool:
        gate = self.gates.get(host)
        if not gate:
            return False
        if time.time() - gate.get("last_checked", 0) < 30:
            return True
        gate["last_checked"] = time.time()
        tab = next((t for t in await self.tabs() if t["id"] == gate.get("target_id")), None)
        if tab is not None:
            page = await snapshot(tab)
            if challenged(page):
                self.save_gates()
                return True
        self.gates.pop(host, None)
        self.save_gates()
        return False

    async def page(self, tab: dict, url: str) -> dict:
        await _assert_reachable_and_public(url)
        previous = (await snapshot(tab)).get("url")
        navigation = await command(tab["webSocketDebuggerUrl"], "Page.navigate", url=url)
        if navigation.get("errorText"):
            raise ValueError("browser_navigation_failed")
        started = time.monotonic()
        deadline = started + 18
        last = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(.6)
            last = await snapshot(tab)
            if last.get("url", "about:blank") == "about:blank":
                continue
            if url != previous and last.get("url") == previous:
                continue
            if challenged(last):
                # Render the challenge for the human; never operate its controls.
                if time.monotonic() > deadline - 12:
                    return last
                continue
            has_cover = any(re.search(r"cover", str(i), re.I) for i in last.get("images", []))
            if time.monotonic() - started >= 2.5 and (last.get("ready") == "complete" or has_cover):
                return last
        return last

    async def loaded_image(self, tab: dict, url: str) -> tuple[bytes, dict] | None:
        tree = await command(tab["webSocketDebuggerUrl"], "Page.getFrameTree")
        data = await command(tab["webSocketDebuggerUrl"], "Page.getResourceContent",
                             frameId=tree["frameTree"]["frame"]["id"], url=url)
        if not data.get("base64Encoded"):
            return None
        body = base64.b64decode(data["content"])
        with Image.open(io.BytesIO(body)) as image:
            content_type = Image.MIME.get(image.format, "")
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            return None
        return covers.validate_image(body, content_type, url)


def record(journal: covers.Journal, status: str, **details):
    delay = 60 if status in {"waiting_for_verification", "verification_tabs_full"} else 86400
    if status == "error":
        delay = 3600
    covers._write(covers.cache_dir() / f"{journal.key}.browser.json", {
        "journal": journal.name, "status": status, "checked_at": time.time(),
        "retry_at": time.time() + delay, "version": VERSION, **details,
    })


async def process(browser: Browser, tab: dict, journal: covers.Journal) -> bool:
    """Return True if this worker tab must stay open for human verification."""
    url = canonical_url(journal.homepage)
    if not url:
        record(journal, "no_homepage")
        return False
    host = scope(url)
    async with browser.locks[host]:
        if await browser.blocked(host):
            record(journal, "waiting_for_verification", host=host)
            return False
        page = await browser.page(tab, url)
        if challenged(page):
            if len(browser.gates) >= MAX_MANUAL_TABS:
                record(journal, "verification_tabs_full")
                return False
            browser.gates[host] = {"target_id": tab["id"], "journal": journal.name,
                                  "url": url, "last_checked": time.time()}
            browser.save_gates()
            record(journal, "waiting_for_verification", host=host)
            return True
        try:
            images = candidates(journal, page)
        except ValueError as exc:
            record(journal, str(exc))
            return False
        # Preserve a verified modern destination without overwriting metadata provenance.
        source_path = covers.cache_dir() / f"{journal.key}.source.json"
        source = covers._read(source_path)
        if page.get("url", "").startswith("https://"):
            source.update(browser_homepage=page["url"], browser_checked_at=time.time())
            covers._write(source_path, source)
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "ScienceMate-journal-covers/1.0"}) as client:
            for image_url in images:
                result = None
                try:
                    result = await covers._download(client, image_url)
                except Exception:
                    pass
                if result is None:
                    try:
                        result = await browser.loaded_image(tab, image_url)
                    except Exception:
                        pass
                if result:
                    covers.save_cover(journal, result, page["url"], method="verified_browser")
                    record(journal, "available", source=page["url"])
                    return False
        record(journal, "cover_not_found", candidates=len(images), page=page.get("url", ""))
        await asyncio.sleep(.5)
        return False


def report(journals: list[covers.Journal], *, running: bool, completed: int = 0) -> dict:
    assets = Counter()
    states = Counter()
    for journal in journals:
        assets[covers.metadata(journal).get("status", "pending")] += 1
        state = covers._read(covers.cache_dir() / f"{journal.key}.browser.json")
        if state:
            states[state.get("status", "unknown")] += 1
    value = {"total": len(journals), "assets": dict(assets), "browser": dict(states),
             "running": running, "completed_this_round": completed, "updated_at": time.time()}
    covers._write(covers.cache_dir() / "browser-progress.json", value)
    from app.services.feed.journal_cover_inventory import write_report
    write_report(journals, running=running)
    return value


async def warm_missing() -> dict:
    options = config()
    if not options:
        return {"enabled": False}
    journals = [enriched(j) for j in candidate_index().values()]
    browser = Browser(options["cdp_url"])
    try:
        await browser.tabs()
        grouped = defaultdict(list)
        for journal in journals:
            if due(journal):
                grouped[scope(canonical_url(journal.homepage))].append(journal)
        # Interleave sites; one publisher cannot occupy all workers waiting on its lock.
        queue = []
        while grouped:
            for host in list(grouped):
                queue.append(grouped[host].pop(0))
                if not grouped[host]:
                    del grouped[host]
        pending = iter(queue)
        completed = 0
        report_lock = asyncio.Lock()
        await asyncio.to_thread(report, journals, running=True)

        async def worker():
            nonlocal completed
            tab = None
            try:
                for journal in pending:
                    if not due(journal):
                        continue
                    try:
                        if tab is None:
                            tab = await browser.new_tab()
                        async with asyncio.timeout(60):
                            if await process(browser, tab, journal):
                                tab = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        record(journal, "error", reason=type(exc).__name__)
                        # A timed-out renderer can also hang every later navigation.
                        # Replace only this worker's tab; keep human verification tabs.
                        if tab:
                            await browser.close_tab(tab)
                            tab = None
                    completed += 1
                    if completed % 25 == 0:
                        async with report_lock:
                            value = await asyncio.to_thread(report, journals, running=True, completed=completed)
                        logger.info("Browser journal covers: %s", value)
            finally:
                if tab:
                    await browser.close_tab(tab)
        await asyncio.gather(*(worker() for _ in range(WORKERS)))
        return await asyncio.to_thread(report, journals, running=False, completed=completed)
    finally:
        await browser.client.aclose()


async def run_forever():
    while True:
        try:
            await warm_missing()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No cookie values, CDP IDs or signed URLs in application logs.
            logger.warning("Browser cover worker unavailable: %s", type(exc).__name__)
        await asyncio.sleep(300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(asyncio.run(warm_missing()), flush=True)
