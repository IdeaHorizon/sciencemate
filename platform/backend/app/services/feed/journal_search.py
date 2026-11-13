"""Subscription candidates work without a developer's private metrics database."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections import OrderedDict

import httpx

from nodes.literature.tools.crossref_http import crossref_list_get
from nodes.literature.tools.journal_metrics import database_path, normalize_issn
from app.services.feed.profile import journal_display_name, journal_key

logger = logging.getLogger(__name__)
_cache: OrderedDict[tuple[str, int], tuple[float, list[dict]]] = OrderedDict()


class JournalSearchUnavailable(RuntimeError):
    """The directory could not be searched; this is not a zero-match result."""


def _local(query: str, limit: int) -> list[dict]:
    path = database_path()
    if not path.is_file():
        return []
    needle = query.casefold()
    compact = needle.replace("-", "")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT journal,issn,eissn,impact_factor,jcr_quartile FROM journals
                   WHERE lower(journal) LIKE ? OR lower(coalesce(journal_abbr, '')) LIKE ?
                      OR replace(lower(coalesce(issn, '')), '-', '') LIKE ?
                      OR replace(lower(coalesce(eissn, '')), '-', '') LIKE ?
                   ORDER BY impact_factor DESC NULLS LAST, journal LIMIT ?""",
                (f"%{needle}%", f"%{needle}%", f"%{compact}%", f"%{compact}%", limit),
            ).fetchall()
    except sqlite3.Error:
        logger.warning("Local journal directory is unavailable; trying the public directory", exc_info=True)
        return []
    return [{"key": str(row["issn"] or row["eissn"] or row["journal"]).casefold(),
             "name": journal_display_name(row["journal"]), "issn": str(row["issn"] or ""),
             "eissn": str(row["eissn"] or ""), "impact_factor": row["impact_factor"],
             "jcr_quartile": row["jcr_quartile"]} for row in rows]


def _candidate(row: dict) -> dict | None:
    name = journal_display_name(row.get("title"))
    identifiers = sorted({value for raw in row.get("ISSN") or [] if (value := normalize_issn(raw))})
    if not name or not identifiers:
        return None
    typed = {item.get("type"): normalize_issn(item.get("value"))
             for item in row.get("issn-type") or [] if isinstance(item, dict)}
    electronic = typed.get("electronic", "")
    primary = typed.get("print") or identifiers[0]
    return {"key": primary.casefold(), "name": name, "issn": primary,
            "eissn": electronic if electronic != primary else "",
            # Crossref registration does not establish an impact factor or JCR quartile.
            "impact_factor": None, "jcr_quartile": None}


async def _public(query: str, limit: int) -> list[dict]:
    by_issn = bool(re.fullmatch(r"\d{4}-?\d{3}[\dXx]", query))
    url = "https://api.crossref.org/journals"
    params = {"query": query, "rows": max(20, limit)}
    if by_issn:
        url += "/" + normalize_issn(query)
        params = {}
    try:
        async with asyncio.timeout(12), httpx.AsyncClient(timeout=8) as client:
            response = await crossref_list_get(client, url, params=params,
                headers={"User-Agent": "ScienceMate/1.0 (journal subscription search)"})
    except httpx.HTTPStatusError as exc:
        if by_issn and exc.response.status_code == 404:
            return []
        raise
    payload = response.json()
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise ValueError("invalid_journal_directory_response")
    rows = [message] if by_issn else message.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_journal_directory_items")
    entries = {}
    for row in rows:
        if candidate := _candidate(row):
            entries.setdefault(candidate["key"], candidate)
    # Keep the upstream relevance order while putting a full name/ISSN match first.
    wanted = journal_key(query)
    return sorted(entries.values(), key=lambda row: not (
        journal_key(row["name"]) == wanted or normalize_issn(query) in
        {row["issn"], row["eissn"]} - {""}))[:limit]


async def search(query: str, *, limit: int = 20) -> list[dict]:
    query = journal_display_name(query)
    if not query:
        return []
    limit = max(1, min(limit, 50))
    if local := await asyncio.to_thread(_local, query, limit):
        return local
    key = (query.casefold(), limit)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        _cache.move_to_end(key)
        return [dict(row) for row in cached[1]]
    try:
        rows = await _public(query, limit)
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as exc:
        logger.warning("Public journal directory search failed: %s", type(exc).__name__)
        raise JournalSearchUnavailable("journal_directory_unavailable") from exc
    _cache[key] = (time.monotonic() + 300, rows)
    _cache.move_to_end(key)
    while len(_cache) > 128:
        _cache.popitem(last=False)
    return [dict(row) for row in rows]
