"""Inventory and resumable cover lookup for every configured candidate journal."""
from __future__ import annotations

import asyncio
from collections import Counter
import csv
from dataclasses import replace
from functools import lru_cache
import io
import logging
import os
from pathlib import Path
import time
import tempfile

import httpx

from app.services.feed import journal_covers as covers

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[5]
SOURCE_TTL = 90 * 86400


def _rows(filename: str, env: str = "") -> list[dict]:
    path = Path(os.environ.get(env) or ROOT / "docs" / filename)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream, delimiter="\t"))
    except OSError:
        return []


@lru_cache(maxsize=1)
def candidate_index() -> dict[str, covers.Journal]:
    """Union of CAS Q1/Q2, mapped and generic pools; covers do not change admission."""
    result: dict[str, covers.Journal] = {}
    publishers = {covers.journal_key(r["journal_name"]): r for r in _rows("xiaohongshu_journal_issn.tsv")}
    from nodes.literature.tools.journal_metrics import lookup_journal_metrics
    for row in (
        _rows("cas_q1_q2_journals.tsv", "HARNESS_CAS_Q1_Q2_JOURNALS")
        + _rows("xiaohongshu_journal_map.tsv", "HARNESS_JOURNAL_DOMAIN_MAP")
        + _rows("xiaohongshu_generic_journal_pool.tsv")
    ):
        name = row.get("journal_name", "").strip()
        if not name:
            continue
        key = covers.journal_key(name)
        prior = result.get(key)
        extra = publishers.get(key, {})
        metrics = lookup_journal_metrics(name)
        issns = covers._issns(
            *(prior.issns if prior else ()), row.get("issn"),
            extra.get("issn"), metrics.get("issn"), metrics.get("eissn"),
        )
        journal = covers.Journal(
            prior.name if prior else name, issns,
            (prior.publisher if prior else "") or row.get("publisher") or extra.get("publisher") or "",
        )
        result[key] = journal
    return result


def enriched(journal: covers.Journal) -> covers.Journal:
    source = covers._read(covers.cache_dir() / f"{journal.key}.source.json")
    return replace(
        journal,
        issns=covers._issns(*journal.issns, source.get("issns")),
        publisher=source.get("publisher") or journal.publisher,
        homepage=source.get("browser_homepage") or source.get("homepage") or journal.homepage,
    )


def _source_entry(journal: covers.Journal, results: list[dict], *, by_name: bool = False) -> dict:
    matches = [
        r for r in results
        if (covers.journal_key(r.get("display_name", "")) == covers.journal_key(journal.name)
            if by_name else bool(set(r.get("issn") or []) & set(journal.issns)))
    ]
    matches.sort(key=lambda r: covers.journal_key(r.get("display_name", "")) != covers.journal_key(journal.name))
    entry = {"checked_at": time.time(), "status": "unresolved"}
    if matches:
        match = matches[0]
        entry.update(
            status="resolved", source_id=match.get("id"),
            source_name=match.get("display_name"), issns=match.get("issn") or [],
            publisher=match.get("host_organization_name") or "",
            homepage=match.get("homepage_url") or "",
            match_method="exact_name" if by_name else "issn",
        )
    return entry


async def resolve_sources(journals: list[covers.Journal]) -> None:
    """ISSN batches avoid thousands of individual metadata searches."""
    root = covers.cache_dir()
    due = []
    for journal in journals:
        entry = covers._read(root / f"{journal.key}.source.json")
        ttl = SOURCE_TTL if entry.get("status") == "resolved" else covers.NEGATIVE_TTL
        if time.time() - float(entry.get("checked_at", 0)) >= ttl:
            due.append(journal)
    groups = []
    with_issn = [j for j in due if j.issns]
    for start in range(0, len(with_issn), 20):
        groups.append((with_issn[start:start + 20], False))
    groups.extend(([j], True) for j in due if not j.issns)
    queue = iter(groups)
    paused = asyncio.Event()
    async with httpx.AsyncClient(timeout=15) as client:
        async def worker():
            for batch, by_name in queue:
                if paused.is_set():
                    return
                params = {
                    "per-page": "50",
                    "select": "id,display_name,issn,homepage_url,host_organization_name",
                }
                if by_name:
                    params["search"] = batch[0].name
                else:
                    identifiers = list(dict.fromkeys(i for j in batch for i in j.issns[:2]))
                    params["filter"] = "issn:" + "|".join(identifiers)
                try:
                    response = await client.get("https://api.openalex.org/sources", params=params)
                    if response.status_code in (401, 403, 429):
                        paused.set()
                        logger.info("Journal homepage discovery paused: HTTP %s", response.status_code)
                        return
                    response.raise_for_status()
                    results = response.json().get("results", [])
                    for journal in batch:
                        covers._write(root / f"{journal.key}.source.json",
                                      _source_entry(journal, results, by_name=by_name))
                except (httpx.HTTPError, ValueError):
                    # Leave unresolved records due; a transient error is not a negative result.
                    pass
                await asyncio.sleep(0.25)
        await asyncio.gather(*(worker() for _ in range(3)))


def write_report(journals: list[covers.Journal], *, running: bool) -> dict:
    root = covers.cache_dir()
    counts: Counter = Counter()
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t")
    writer.writerow(["journal", "issns", "publisher", "homepage", "status", "reason", "cover_url", "checked_at"])
    for original in journals:
        journal = enriched(original)
        entry = covers.metadata(journal)
        status = entry.get("status", "pending")
        counts[status] += 1
        writer.writerow([
            journal.name, ";".join(journal.issns), journal.publisher, journal.homepage,
            status, entry.get("failure_reason", ""), entry.get("image_url", ""),
            entry.get("checked_at", ""),
        ])
    report = {"total": len(journals), **counts, "running": running, "updated_at": time.time()}
    covers._write(root / "coverage.json", report)
    target = root / "coverage.tsv"
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False, encoding="utf-8") as handle:
        handle.write(output.getvalue())
    Path(handle.name).replace(target)
    return report


async def warm_all() -> dict:
    journals = list(candidate_index().values())
    await asyncio.to_thread(write_report, journals, running=True)
    await resolve_sources(journals)
    pending = [enriched(j) for j in journals]
    queue = iter(j for j in pending if covers.needs_refresh(j))
    completed = 0
    report_lock = asyncio.Lock()

    async def worker():
        nonlocal completed
        for original in queue:
            journal = enriched(original)
            if covers.needs_refresh(journal):
                try:
                    await covers.ensure(journal)
                except Exception:
                    logger.exception("Journal cover lookup failed for %s", journal.name)
            completed += 1
            if completed % 100 == 0:
                # Disk scans must not block API requests on the event loop.
                async with report_lock:
                    report = await asyncio.to_thread(write_report, journals, running=True)
                logger.info("Journal covers: %s", report)
    await asyncio.gather(*(worker() for _ in range(6)))
    return await asyncio.to_thread(write_report, journals, running=False)


async def run_forever() -> None:
    while True:
        try:
            await warm_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Journal cover inventory round failed")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # HTTP URLs can contain provider parameters; only our aggregate log is needed.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(asyncio.run(warm_all()), flush=True)
