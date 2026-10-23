"""DOI based abstract retry for subscribed papers with an auditable source trail."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx

from app.services.feed.profile import journal_key
from nodes.literature.tools.archive_papers import archive_indexes_only
from nodes.literature.tools.scihub_fetcher import paper_cache_dir
from nodes.literature.tools.search_engines import PubMedSearch, _publisher_landing_abstract

from nodes.literature.tools.publication_types import is_journal_notice_title


def _candidates(
    user_id: str, journal_names: list[str] | None, limit: int, *, project_only: bool = False, catalog_keys: list[str] | None = None
) -> list[dict]:
    if catalog_keys is not None and not catalog_keys:
        return []
    root = paper_cache_dir()
    wanted = {journal_key(name) for name in journal_names or [] if name}
    try:
        with sqlite3.connect(f"{root}/literature_catalog.sqlite3", timeout=5) as conn:
            key_clause = ""
            parameters = [str(user_id)]
            if catalog_keys is not None:
                key_clause = " AND doi IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(catalog_keys))
            rows = conn.execute(
                f"""SELECT metadata_json FROM papers
                   WHERE json_valid(metadata_json)
                     AND EXISTS (SELECT 1 FROM json_each(metadata_json, '$.profile_user_ids')
                                 WHERE value = ?)
                     AND COALESCE(json_extract(metadata_json, '$.abstract'), '') = ''
                     AND COALESCE(json_extract(metadata_json, '$.abstract_lookup_completed'), 0) = 0
                   {key_clause}
                   ORDER BY updated_at DESC""",
                parameters,
            ).fetchall()
    except sqlite3.OperationalError as exc:
        # A fresh instance can run the subscription worker before its first
        # literature index has been created. Other database errors must surface.
        if "no such table: papers" not in str(exc):
            raise
        return []
    if catalog_keys is not None:
        priority = {key: index for index, key in enumerate(catalog_keys)}
        rows = sorted((row for row in rows if str(json.loads(row[0]).get("doi") or "").lower() in priority),
                      key=lambda row: priority[str(json.loads(row[0]).get("doi") or "").lower()])
    now = datetime.now(UTC)
    result = []
    for (raw,) in rows:
        record = json.loads(raw)
        if wanted and journal_key(record.get("venue")) not in wanted:
            continue
        if project_only and "project_profile" not in (record.get("feed_acquisition_routes") or []):
            continue
        if not record.get("doi") or is_journal_notice_title(record.get("title") or ""):
            continue
        retry_at = record.get("abstract_lookup_retry_after")
        if retry_at:
            try:
                if datetime.fromisoformat(retry_at) > now:
                    continue
            except ValueError:
                pass
        result.append(record)
        if len(result) >= limit:
            break
    return result


def _source_order(record: dict, preferred: str | None = None) -> list[str]:
    # Learned journal successes take precedence over the initial field heuristic.
    medical = bool(re.search(r"medicine|medical|clinical|oncolog|cardiol|urolog|lancet|nejm|cancer", str(record.get("venue") or ""), re.I))
    order = ["pubmed", "openalex", "crossref", "publisher_landing"] if medical else ["crossref", "openalex", "pubmed", "publisher_landing"]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def _journal_preferences() -> dict[str, str]:
    counts = {}
    try:
        with sqlite3.connect(f"{paper_cache_dir()}/literature_catalog.sqlite3", timeout=5) as conn:
            rows = conn.execute("SELECT metadata_json FROM papers WHERE json_valid(metadata_json) ORDER BY updated_at DESC LIMIT 10000").fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            source = (record.get("metadata_provenance") or {}).get("abstract")
            if source in {"pubmed", "crossref", "openalex", "publisher_landing"} and record.get("abstract"):
                key = journal_key(record.get("venue"))
                if key:
                    counts.setdefault(key, {}).setdefault(source, 0)
                    counts[key][source] += 1
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
    return {key: max(values, key=values.get) for key, values in counts.items()}


async def _lookup(record: dict, client: httpx.AsyncClient, preferred: str | None = None) -> dict:
    doi = str(record["doi"]).strip()
    now = datetime.now(UTC)
    previous = record.get("abstract_lookup_source_state") or {}
    state = dict(previous) if isinstance(previous, dict) else {}
    attempts = []
    abstract, source = "", ""

    async def lookup(name):
        old = state.get(name) or {}
        try:
            until = datetime.fromisoformat(str(old.get("retry_after") or ""))
            if until > now:
                attempts.append({"source": name, "result": "cached_no_abstract", "previous_result": old.get("result", "")})
                return name, ""
        except (ValueError, TypeError):
            pass
        result, text = "no_abstract", ""
        try:
            if name == "pubmed":
                hits = await asyncio.wait_for(PubMedSearch(client).search(f"{doi}[doi]", limit=1), timeout=12)
                match = hits[0] if hits and str(hits[0].doi or "").strip().lower() == doi.lower() else None
                text = str(match.abstract or "").strip() if match else ""
            else:
                url = {"crossref": f"https://api.crossref.org/works/{doi}",
                       "openalex": f"https://api.openalex.org/works/https://doi.org/{doi}",
                       "publisher_landing": f"https://doi.org/{doi}"}[name]
                response = await asyncio.wait_for(client.get(url, follow_redirects=True), timeout=8)
                if response.status_code != 200:
                    result = f"http_{response.status_code}"
                elif name == "crossref":
                    text = str((response.json().get("message") or {}).get("abstract") or "").strip()
                elif name == "openalex":
                    inverted = response.json().get("abstract_inverted_index") or {}
                    text = " ".join(word for _, word in sorted((index, word) for word, positions in inverted.items() for index in positions))
                else:
                    text = _publisher_landing_abstract(response.text)
            if text:
                result = "found"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = type(exc).__name__
        transient = result in {"http_408", "http_429", "http_500", "http_502", "http_503", "http_504"} or result.endswith("Error")
        delay = timedelta(minutes=15 if result == "http_429" else 30) if transient else timedelta(days=7)
        state[name] = {"result": result, "attempted_at": now.isoformat(),
                       "retry_after": (now + delay).isoformat()}
        attempts.append({"source": name, "result": result})
        return name, text

    order = _source_order(record, preferred)
    # Two sources at a time, publishing the first successful response immediately.
    for start in range(0, len(order), 2):
        tasks = {asyncio.create_task(lookup(name)) for name in order[start:start + 2]}
        try:
            while tasks:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    found_source, text = task.result()
                    if text and not abstract:
                        abstract, source = text, found_source
                if abstract:
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if abstract:
            break

    transient = any(str(value.get("result") or "").endswith("Error") or value.get("result") in
                    {"http_408", "http_429", "http_500", "http_502", "http_503", "http_504"} for value in state.values())
    record["abstract_lookup_source_state"] = state
    record["abstract_lookup_attempts"] = attempts
    record["abstract_lookup_attempted_at"] = now.isoformat()
    record["abstract_lookup_completed"] = bool(abstract) or not transient
    record["abstract_enrichment_attempted"] = True
    if transient and not abstract:
        deadlines = [value["retry_after"] for value in state.values() if value.get("retry_after")]
        record["abstract_lookup_retry_after"] = min(deadlines) if deadlines else (now + timedelta(minutes=30)).isoformat()
    else:
        record.pop("abstract_lookup_retry_after", None)
    if abstract:
        record["abstract"] = abstract
        provenance = record.get("metadata_provenance")
        record["metadata_provenance"] = {**(provenance if isinstance(provenance, dict) else {}), "abstract": source}
    return record


async def backfill_subscription_abstracts(
    user_id: str, *, journal_names: list[str] | None = None, limit: int = 20,
    project_only: bool = False, catalog_keys: list[str] | None = None,
    updated_keys: list[str] | None = None,
) -> dict[str, int]:
    """Retry missing research abstracts and save exact per-source outcomes."""
    candidates = await asyncio.to_thread(
        _candidates, user_id, journal_names, limit, project_only=project_only, catalog_keys=catalog_keys
    )
    if not candidates:
        return {"candidates": 0, "found": 0}
    preferred = await asyncio.to_thread(_journal_preferences)
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), limits=httpx.Limits(max_connections=6, max_keepalive_connections=6), headers={"User-Agent": "harness-literature-abstract/1.0"}) as client:
        async def one(record: dict) -> dict:
            async with semaphore:
                return await _lookup(record, client, preferred.get(journal_key(record.get("venue"))))
        updated = await asyncio.gather(*(one(record) for record in candidates))
    if project_only:
        for record in updated:
            image = record.get("image") if isinstance(record.get("image"), dict) else {}
            if record.get("abstract_lookup_completed") and image.get("status") in {"available", "generated"}:
                record["literature_preparation"] = "ready"
    await asyncio.to_thread(archive_indexes_only, updated, paper_cache_dir())
    if updated_keys is not None:
        updated_keys.extend(str(record["doi"]).lower() for record in updated if record.get("doi"))
    return {"candidates": len(updated), "found": sum(bool(x.get("abstract")) for x in updated)}
