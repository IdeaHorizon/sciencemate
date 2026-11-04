"""Publisher journal covers, cached per journal and distinct from paper figures."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import logging
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urljoin

import httpx
from PIL import Image

from app.config import data_root
from app.services.feed.literature_projection import literature_papers_root

POSITIVE_TTL = 30 * 86400
NEGATIVE_TTL = 86400
MAX_BYTES = 3 * 1024 * 1024
MAX_SOURCE_IMAGE_BYTES = 8 * 1024 * 1024
LOOKUP_VERSION = 2
OPTICA_CATALOG = "https://www.optica.org/Publications"
_SLOTS = asyncio.Semaphore(6)
_OPTICA_LOCK = asyncio.Lock()
_INFLIGHT: dict[str, asyncio.Task] = {}
logger = logging.getLogger(__name__)


def cache_dir() -> Path:
    root = data_root("state") / "feed-journal-covers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def journal_key(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", html.unescape(name).casefold())


def _issns(*values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        match.upper() for value in values
        for match in re.findall(r"\b\d{4}-\d{3}[\dXx]\b", str(value or ""))
    ))


@dataclass(frozen=True)
class Journal:
    name: str
    issns: tuple[str, ...]
    publisher: str
    homepage: str = ""

    @property
    def key(self) -> str:
        # Publisher metadata may add/reorder ISSNs later; retain the cache key.
        identity = journal_key(self.name)
        return hashlib.sha256(identity.encode()).hexdigest()[:24]


def identify(item) -> Journal | None:
    if getattr(item, "kind", "") != "paper" or not getattr(item, "venue", ""):
        return None
    extra = item.extra if isinstance(item.extra, dict) else {}
    name = html.unescape(item.venue).strip()
    doi = str(extra.get("doi") or "").lower()
    metadata = {}
    catalog = literature_papers_root() / "literature_catalog.sqlite3"
    key = str(extra.get("catalog_key") or doi)
    if catalog.is_file() and key:
        try:
            with sqlite3.connect(f"file:{catalog}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT metadata_json FROM papers WHERE doi=?", (key,)).fetchone()
            metadata = json.loads(row[0]) if row else {}
        except (OSError, sqlite3.Error, ValueError):
            metadata = {}
    doi = str(metadata.get("doi") or doi).lower()
    identifiers = _issns(metadata.get("issn"), metadata.get("eissn"), extra.get("issn"), extra.get("eissn"))
    # The local metrics table supplies canonical names/ISSNs, including aliases.
    try:
        from nodes.literature.tools.journal_metrics import lookup_journal_metrics
        metrics = lookup_journal_metrics(name, issn=identifiers[0] if identifiers else "")
    except (ImportError, OSError, sqlite3.Error):
        metrics = {}
    if metrics:
        name = str(metrics.get("journal") or name)
        identifiers = _issns(metrics.get("issn"), metrics.get("eissn"), *identifiers)
    publisher = (
        "optica" if doi.startswith("10.1364/") else
        "elsevier" if doi.startswith("10.1016/") else
        "wiley" if doi.startswith(("10.1002/", "10.1111/")) else ""
    )
    from app.services.feed.journal_cover_inventory import candidate_index, enriched
    known = candidate_index().get(journal_key(name)) or candidate_index().get(journal_key(item.venue))
    journal = replace(known, publisher=known.publisher or publisher) if known else Journal(name, identifiers, publisher)
    return enriched(journal)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    Path(handle.name).replace(path)


class _OpticaCovers(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.covers = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        alt = str(attrs.get("alt") or "").strip()
        if tag == "img" and alt.lower().endswith(" cover"):
            url = urljoin(OPTICA_CATALOG, attrs.get("src") or "")
            if url.startswith("https://opg.optica.org/images/"):
                self.covers[journal_key(alt[:-6])] = url


async def _candidates(journal: Journal, client: httpx.AsyncClient) -> list[tuple[str, str]]:
    publisher = journal.publisher.lower()
    if "elsevier" in publisher:
        return [
            (f"https://ars.els-cdn.com/content/image/X{issn.replace('-', '')}.jpg",
             "publisher_issn_cover") for issn in journal.issns[:2]
        ]
    if "wiley" in publisher or "blackwell" in publisher:
        return [
            (f"https://media.wiley.com/journal/{issn.replace('-', '')}-cover.jpg",
             "publisher_issn_cover") for issn in journal.issns[:2]
        ]
    if "springer" in publisher and (match := re.search(r"/journal/(\d+)", journal.homepage)):
        return [(f"https://covers.springernature.com/journals/jpg_width_153_pixels/{int(match[1]):05d}.jpg",
                 journal.homepage)]
    if not any(term in publisher for term in ("optica", "optical society")):
        return []
    async with _OPTICA_LOCK:
        path = cache_dir() / "optica-directory.json"
        directory = _read(path)
        if time.time() - float(directory.get("checked_at", 0)) > POSITIVE_TTL:
            response = await client.get(OPTICA_CATALOG)
            response.raise_for_status()
            from shared.contrib.paper_image_policy import challenge_page
            if challenge_page(response.text):
                return []
            parser = _OpticaCovers()
            parser.feed(response.text)
            directory = {"checked_at": time.time(), "covers": parser.covers}
            _write(path, directory)
    url = directory.get("covers", {}).get(journal_key(journal.name))
    return [(url, OPTICA_CATALOG)] if url else []


def _cover_url_allowed(url: str) -> bool:
    from shared.contrib.paper_image_policy import image_url_rejection_reason
    # "journal-cover" is disallowed as a paper figure but is the purpose here.
    return not image_url_rejection_reason(re.sub("journal[-_]cover", "cover", url, flags=re.I))


async def _get_public(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_BYTES) -> httpx.Response:
    """Bounded public-page fetch, including each redirect."""
    from app.services.feed.link_preview import _assert_reachable_and_public
    for _ in range(6):
        await _assert_reachable_and_public(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("page_too_large")
            # aiter_bytes already decoded compression; do not decode a second time.
            headers = {k: v for k, v in response.headers.items()
                       if k.lower() not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, content=bytes(body),
                                  headers=headers, request=response.request)
    raise ValueError("too_many_redirects")


class _HomepageCovers(HTMLParser):
    """Only explicitly labelled covers on the identified journal homepage."""
    def __init__(self, journal: Journal, url: str):
        super().__init__(convert_charrefs=True)
        self.journal, self.url = journal, url
        self.images: list[str] = []
        self._title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._title = True
        if tag != "img":
            return
        src = attrs.get("data-src") or attrs.get("src") or ""
        label = " ".join(str(attrs.get(k) or "") for k in ("alt", "class", "id"))
        if not re.search(r"cover|封面", label + " " + src, re.I):
            return
        alt = str(attrs.get("alt") or "").strip()
        # A related journal's named cover is not this journal's cover.
        named = re.sub(r"(?i)^cover(?: image)?(?: of)?(?: the)?(?: current issue)?(?: for| from| of)?\s*", "", alt)
        named = re.sub(r"(?i)\s+(?:journal )?cover(?: image)?.*$", "", named)
        named = re.sub(r"(?i)\s+(?:january|february|march|april|may|june|july|august|september|october|november|december|volume|vol\.)\b.*$", "", named).strip(" :-")
        generic = journal_key(named) in {"", "issue", "journal", "currentissue", "featuredcurrentissue"}
        page_name = self.title.split("|")[0].strip()
        if not generic and journal_key(named) not in journal_key(self.journal.name) and journal_key(named) != journal_key(page_name):
            return
        url = urljoin(self.url, src)
        if src and _cover_url_allowed(url) and url not in self.images:
            self.images.append(url)

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False

    def handle_data(self, data):
        if self._title:
            self.title += data


async def _homepage_candidates(journal: Journal, client: httpx.AsyncClient) -> list[tuple[str, str]]:
    if not journal.homepage:
        return []
    response = await _get_public(client, re.sub(r"^http://", "https://", journal.homepage))
    from shared.contrib.paper_image_policy import challenge_page
    if challenge_page(response.text):
        raise ValueError("challenge_page")
    parser = _HomepageCovers(journal, str(response.url))
    parser.feed(response.text)
    # Homepage redirects sometimes land on a generic publisher catalogue.
    identity = journal_key(journal.name) in journal_key(parser.title)
    identity = identity or bool(set(_issns(response.text)) & set(journal.issns))
    if not identity:
        raise ValueError("homepage_journal_identity_unconfirmed")
    return [(url, str(response.url)) for url in parser.images[:3]]


async def _download(client: httpx.AsyncClient, url: str) -> tuple[bytes, dict] | None:
    if not _cover_url_allowed(url):
        return None
    response = await _get_public(client, url, max_bytes=MAX_SOURCE_IMAGE_BYTES)
    if not _cover_url_allowed(str(response.url)):
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return None
    return validate_image(response.content, content_type, url)


def validate_image(body: bytes, content_type: str, url: str) -> tuple[bytes, dict] | None:
    from shared.contrib.paper_image_policy import image_bytes_rejection_reason
    if (content_type not in {"image/jpeg", "image/png", "image/webp"}
            or not _cover_url_allowed(url) or len(body) > MAX_SOURCE_IMAGE_BYTES):
        return None
    if image_bytes_rejection_reason(body):
        return None
    try:
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
            if width * height > 20_000_000:
                return None
            image.verify()
    except Exception:
        return None
    if width < 80 or height < 100 or not 0.35 <= width / height <= 1.2:
        return None
    original_sha = hashlib.sha256(body).hexdigest()
    if len(body) > MAX_BYTES or width > 768 or height > 1024:
        with Image.open(io.BytesIO(body)) as source:
            picture = source.convert("RGB")
            picture.thumbnail((768, 1024), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=88)
            body = output.getvalue()
            width, height = picture.size
            content_type = "image/jpeg"
    return body, {
        "content_type": content_type, "width": width, "height": height,
        "sha256": hashlib.sha256(body).hexdigest(), "image_url": url, "original_sha256": original_sha,
    }


def save_cover(journal: Journal, result: tuple[bytes, dict], source: str, *, method: str) -> None:
    root = cache_dir()
    body, details = result
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
        handle.write(body)
    Path(handle.name).replace(root / f"{journal.key}.image")
    _write(root / f"{journal.key}.json", {
        "kind": "journal_cover", "journal": journal.name, "issns": list(journal.issns),
        "publisher": journal.publisher, "homepage": journal.homepage,
        "checked_at": time.time(), "status": "available", "lookup_version": LOOKUP_VERSION,
        "source": source, "method": method, **details,
    })


async def refresh(journal: Journal) -> None:
    """One bounded background fetch; no publication or model request involved."""
    root = cache_dir()
    entry = {
        "kind": "journal_cover", "journal": journal.name, "issns": list(journal.issns),
        "publisher": journal.publisher, "homepage": journal.homepage,
        "checked_at": time.time(), "status": "unavailable", "attempts": [],
        "lookup_version": LOOKUP_VERSION,
    }
    try:
        async with _SLOTS:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                    timeout=6, follow_redirects=True,
                    headers={"User-Agent": "ScienceMate-journal-covers/1.0"},
                ) as client:
                    for lookup in (_candidates, _homepage_candidates):
                        for url, source in await lookup(journal, client):
                            attempt = {"url": url}
                            entry["attempts"].append(attempt)
                            try:
                                result = await _download(client, url)
                            except Exception as exc:
                                attempt["result"] = f"HTTP_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                                continue
                            if not result:
                                attempt["result"] = "not_valid_cover"
                                continue
                            body, details = result
                            target = root / f"{journal.key}.image"
                            with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
                                handle.write(body)
                            Path(handle.name).replace(target)
                            entry.update(details, status="available", source=source)
                            break
                        if entry["status"] == "available":
                            break
                    if entry["status"] != "available":
                        entry["failure_reason"] = "cover_not_found" if journal.homepage or journal.publisher else "journal_homepage_unresolved"
    except Exception as exc:
        entry["failure_reason"] = f"HTTP_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else (
            str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        )
    # A transient refresh failure must not destroy a previously valid cover.
    previous = _read(root / f"{journal.key}.json")
    if entry["status"] != "available" and previous.get("status") == "available":
        try:
            valid_previous = hashlib.sha256((root / f"{journal.key}.image").read_bytes()).hexdigest() == previous.get("sha256")
        except OSError:
            valid_previous = False
        if valid_previous:
            entry = {**previous, "checked_at": time.time(), "refresh_failed": True}
    _write(root / f"{journal.key}.json", entry)


def needs_refresh(journal: Journal) -> bool:
    entry = metadata(journal)
    ttl = POSITIVE_TTL if entry.get("status") == "available" and not entry.get("refresh_failed") else NEGATIVE_TTL
    return (entry.get("status") != "available" and entry.get("lookup_version") != LOOKUP_VERSION
            or time.time() - float(entry.get("checked_at", 0)) >= ttl)


def metadata(journal: Journal) -> dict:
    """A shipped cover is available even on a machine with an empty cache."""
    entry = _read(cache_dir() / f"{journal.key}.json")
    if entry.get("status") == "available":
        return entry
    from app.services.feed import journal_cover_bundle
    bundled = journal_cover_bundle.metadata(journal.key)
    if not bundled:
        return entry
    if entry.get("checked_at"):
        bundled.update(checked_at=entry["checked_at"], refresh_failed=True)
    return bundled


async def ensure(journal: Journal) -> None:
    await asyncio.shield(_schedule(journal))


def _schedule(journal: Journal) -> asyncio.Task:
    if journal.key not in _INFLIGHT:
        task = asyncio.create_task(refresh(journal))
        _INFLIGHT[journal.key] = task
        def done(result):
            _INFLIGHT.pop(journal.key, None)
            if not result.cancelled() and result.exception():
                logger.warning("Journal cover task failed for %s: %s", journal.name,
                               type(result.exception()).__name__)
        task.add_done_callback(done)
    return _INFLIGHT[journal.key]


def get_or_schedule(item) -> tuple[tuple[bytes, str] | None, bool]:
    """Return a cached cover immediately; queue any network work once per journal."""
    journal = identify(item)
    if journal is None:
        return None, False
    root = cache_dir()
    entry = _read(root / f"{journal.key}.json")
    due = needs_refresh(journal)
    asset = None
    if entry.get("status") == "available":
        try:
            body = (root / f"{journal.key}.image").read_bytes()
            if hashlib.sha256(body).hexdigest() == entry.get("sha256"):
                asset = body, entry["content_type"]
            else:
                due = True
        except (OSError, KeyError):
            due = True
    if asset is None:
        from app.services.feed import journal_cover_bundle
        asset = journal_cover_bundle.read(journal.key)
        if asset is None and metadata(journal).get("method") == "bundled":
            due = True
    if due and journal.key not in _INFLIGHT:
        _schedule(journal)
    return asset, asset is None and journal.key in _INFLIGHT
