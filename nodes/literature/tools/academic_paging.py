"""One bounded processing batch, with source-owned cursors and no total quota.

The existing engines still own queries, authentication, parsing and rate limits.
This transport only adds pagination to their ordinary requests. Nothing here
retries access denials or treats a failed page as an exhausted result set.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import xml.etree.ElementTree as ET

import httpx


class PagingError(RuntimeError):
    pass


def query_key(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def validate_continuation(value: dict | None, query: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("query") != query_key(query):
        raise ValueError("Invalid search continuation")
    groups = value.get("groups")
    if not isinstance(groups, dict) or len(groups) > 84:
        raise ValueError("Invalid search continuation groups")
    allowed = {"arxiv", "pubmed", "crossref", "crossref_journals", "openalex",
               "google_scholar", "semantic_scholar", "biorxiv", "medrxiv"}
    for key, positions in groups.items():
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid search group")
        if positions is None:
            continue  # A planned query group whose first page has not started.
        if not isinstance(positions, dict) or set(positions) - allowed:
            raise ValueError("Invalid continued source selection")
        for position in positions.values():
            if position is not None and not isinstance(position, dict):
                raise ValueError("Invalid source position")
            PageClient(None, "", position)
    return groups


class PageClient:
    """Delegate requests while preserving each engine's native query semantics."""
    def __init__(self, client, source: str, state: dict | None, category=None):
        self.client, self.source, self.state = client, source, state or {}
        self.category = category
        self.next_state = None
        self.response = None
        self.error = None
        self.offset = self.state.get("offset", 0)
        token = self.state.get("token")
        if (type(self.offset) is not int or self.offset < 0 or
                (token is not None and (not isinstance(token, str) or len(token) > 20000)) or
                (self.state.get("signature") is not None and
                 (not isinstance(self.state["signature"], str) or len(self.state["signature"]) != 64))):
            raise ValueError("Invalid source pagination position")

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def get(self, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        source = self.source
        # PubMed's second request retrieves the IDs of this page, not a new page.
        if source == "pubmed" and "esearch.fcgi" not in str(url):
            return await self.client.get(url, **kwargs)
        token = self.state.get("token")
        if source in {"crossref", "crossref_journals"}:
            params["cursor"] = token or "*"
            size = int(params["rows"])
        elif source == "openalex":
            size = min(int(params["per-page"]), 100)
            params["per-page"] = size
            if os.environ.get("OPENALEX_API_KEY"):
                params["cursor"] = token or "*"
            else:
                if self.offset >= 10000:
                    raise PagingError("OpenAlex requires an API key for cursor paging beyond its public paging window")
                params["page"] = self.offset // size + 1
        elif source in {"biorxiv", "medrxiv"}:
            params["cursorMark"] = token or "*"
            size = int(params["pageSize"])
        elif source == "pubmed":
            if self.offset >= 10000:
                raise PagingError("PubMed ESearch exposes only the first 10000 matches; remaining matches require EDirect or narrower source queries")
            params["retstart"] = self.offset
            size = int(params["retmax"])
        elif source == "semantic_scholar":
            if self.offset >= 1000:
                raise PagingError("Semantic Scholar relevance search exposes only the first 1000 matches")
            params["offset"] = self.offset
            size = int(params["limit"])
        elif source == "arxiv":
            params["start"] = self.offset
            size = int(params["max_results"])
        elif source == "google_scholar":
            params.update(start=self.offset, num=10)
            size = 10
        else:
            raise PagingError("This source does not support continued retrieval")
        if self.category:
            from .academic_categories import CROSSREF_TYPES, OPENALEX_TYPES
            if source in {"crossref", "crossref_journals"}:
                filters = [part for part in params.get("filter", "").split(",") if part and not part.startswith("type:")]
                params["filter"] = ",".join(filters + ["type:"+kind for kind in CROSSREF_TYPES[self.category]])
            elif source == "openalex":
                params["filter"] += ",type:"+OPENALEX_TYPES[self.category]
                if self.category in {"期刊文章", "中文期刊"}:
                    params["filter"] += ",language:" + ("zh" if self.category=="中文期刊" else "!zh")
            elif source == "pubmed":
                params["term"] = "("+params["term"]+") " + ("AND" if self.category=="中文期刊" else "NOT") + " chinese[Language]"
        kwargs["params"] = params
        response = await self.client.get(url, **kwargs)
        self.response = response
        if response.status_code >= 400:
            return response  # Existing engines retain their retry/backoff policy.
        try:
            self.record(response, size)
        except Exception as exc:
            self.error = exc
            raise
        return response

    def record(self, response, size):
        source, token, total = self.source, None, None
        if source in {"crossref", "crossref_journals"}:
            data = response.json()["message"]
            rows = data.get("items", [])
            token, total = data.get("next-cursor"), data.get("total-results")
            keys = [r.get("DOI") or r.get("title") for r in rows]
            more = len(rows) >= size
            if more and not token:
                raise PagingError("Crossref returned a full page without its next cursor")
        elif source == "openalex":
            data = response.json()
            rows = data.get("results", [])
            token, total = data.get("meta", {}).get("next_cursor"), data.get("meta", {}).get("count")
            keys = [r.get("id") for r in rows]
            more = bool(token) if os.environ.get("OPENALEX_API_KEY") else len(rows) >= size
        elif source in {"biorxiv", "medrxiv"}:
            data = response.json()
            rows = data.get("resultList", {}).get("result", [])
            token, total = data.get("nextCursorMark"), data.get("hitCount")
            keys = [r.get("id") or r.get("doi") for r in rows]
            more = bool(token) and bool(rows)
        elif source == "pubmed":
            data = response.json()["esearchresult"]
            rows = keys = data.get("idlist", [])
            total = data.get("count")
            more = len(rows) >= size
        elif source == "semantic_scholar":
            data = response.json()
            rows = data.get("data", [])
            total = data.get("total")
            keys = [r.get("paperId") or r.get("title") for r in rows]
            more = data.get("next") is not None
        elif source == "arxiv":
            root = ET.fromstring(response.text)
            rows = root.findall("{http://www.w3.org/2005/Atom}entry")
            total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
            keys = [r.findtext("{http://www.w3.org/2005/Atom}id") for r in rows]
            more = len(rows) >= size
        else:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")
            if soup.select_one("form[action*='sorry'],#gs_captcha_f,input[name='captcha']"):
                raise PagingError("Google Scholar requires human verification")
            rows = soup.select(".gs_ri")
            keys = [r.get_text(" ", strip=True) for r in rows]
            more = any("start=" in a.get("href", "") and
                       str(self.offset + len(rows)) in a.get("href", "") for a in soup.select("#gs_n a"))
        if total is not None:
            total = int(total)
            more = more and self.offset + len(rows) < total
        if not rows and total is not None and self.offset < total:
            raise PagingError("The source returned an empty page before its reported result count")
        signature = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
        if rows and signature == self.state.get("signature"):
            raise PagingError("The source repeated the preceding page; progress has been retained")
        self.next_state = {"offset": self.offset + len(rows), "token": token, "signature": signature,
                           "total": total} if more else None


class PageEngine:
    def __init__(self, engine, client, source):
        self.engine, self.client, self.source = engine, client, source

    async def search(self, query, limit, date_from=None):
        if self.source == "google_scholar":
            papers = await self.engine.search(query, limit)
        elif self.source == "crossref_journals":
            papers = await self.engine.search(query, limit, date_from=date_from, publication_types=("journal-article",))
        else:
            papers = await self.engine.search(query, limit, date_from=date_from)
        if self.client.error:
            raise self.client.error
        if self.client.response is None:
            raise PagingError("Source did not execute a result-page request")
        self.client.response.raise_for_status()
        return papers


async def collect_page(manager, query: str, sources: set[str], limits: dict[str, int],
                       state: dict | None, *, date_from=None, category=None):
    from .config import SEARCH_ENGINES
    from .search_engines import (ArxivSearch, EuropePmcPreprintSearch, PubMedSearch,
        SemanticScholarSearch, CrossrefSearch, OpenAlexSearch, GoogleScholarSearch)
    engines = {"arxiv":ArxivSearch, "pubmed":PubMedSearch, "crossref":CrossrefSearch,
               "crossref_journals":CrossrefSearch, "openalex":OpenAlexSearch,
               "google_scholar":GoogleScholarSearch}
    from .academic_categories import sources_for_category
    requested = sources_for_category(set(sources) - {"cnki"}, category)
    if "crossref" in requested and category is None:
        requested.add("crossref_journals")
    positions = {source: None for source in requested} if state is None else state
    if not isinstance(positions, dict) or set(positions) - requested:
        raise ValueError("Invalid continued source selection")
    manager._last_search_diagnostics = {}
    next_states, warnings, attempted, unavailable = {}, [], set(), set()
    clients, tasks = {}, []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for source, position in positions.items():
            canonical = "crossref" if source == "crossref_journals" else source
            if not SEARCH_ENGINES.get(canonical, {}).get("enabled", False):
                unavailable.add(canonical)
                warnings.append({"code":"source_unavailable","sources":[canonical],"reason":"disabled_in_config"})
                continue
            proxy = clients[source] = PageClient(client, source, position, category)
            if source in {"biorxiv", "medrxiv"}:
                engine = EuropePmcPreprintSearch(proxy, source)
            elif source == "semantic_scholar":
                engine = SemanticScholarSearch(proxy, manager._s2_key)
            else:
                engine = engines[source](proxy)
            attempted.add(canonical)
            tasks.append((source, manager._safe_search(PageEngine(engine, proxy, source), query,
                limits.get(canonical, 30), source, timeout_sec=30, date_from=date_from)))
        batches = await asyncio.gather(*(task for _,task in tasks))
    papers = []
    for (source,_), batch in zip(tasks,batches):
        canonical = "crossref" if source == "crossref_journals" else source
        diag = manager._last_search_diagnostics.get(source,{})
        if diag.get("status") != "ok":
            unavailable.add(canonical)
            warnings.append({"code":"source_request_failed","sources":[canonical],"reason":diag.get("status"),
                             "http_status":diag.get("http_status"),
                             "detail":f"{canonical} 后续分页暂未完成：{diag.get('error','请求失败')}"})
        elif clients[source].next_state is not None:
            next_states[source] = clients[source].next_state
        papers.extend(batch)
    return manager._finalize_results(manager._merge_results([],papers), sources,
        requested_sources=sorted(set(sources)-{"cnki"}), attempted_sources=attempted,
        unavailable_sources=unavailable,warnings=warnings,
        source_diagnostics=dict(manager._last_search_diagnostics)), next_states
