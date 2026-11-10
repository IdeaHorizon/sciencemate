"""E2E adapter: shared search, bounded model context, resumable candidate pages."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from . import academic_search

PER_CALL = 30
PER_RUN = 120
POOL_KEY = "_literature_academic_candidates"
SEEN_KEY = "_literature_model_paper_keys"


def identity(paper: dict) -> str:
    return str(paper.get("doi") or "").strip().lower() or "title:" + " ".join(
        str(paper.get("title") or "").casefold().split()
    )


def _page(state, snapshot: dict, *, offset: int = 0, limit: int = 20) -> dict:
    seen = set(state.hook_state.get(SEEN_KEY, [])) if state else set()
    budget = max(0, PER_RUN - len(seen))
    papers, cursor, overlap = [], offset, []
    candidates = snapshot["papers"]
    while cursor < len(candidates) and len(papers) < min(PER_CALL, limit) and budget:
        paper = candidates[cursor]
        cursor += 1
        key = identity(paper)
        if key in seen:
            if len(overlap) < 5:
                overlap.append(paper)
            continue
        papers.append(paper)
        seen.add(key)
        budget -= 1
    fallback = not papers and bool(overlap)
    if fallback:
        papers = overlap[:min(5, limit)]
    if state:
        state.hook_state[SEEN_KEY] = sorted(seen)
    projected = []
    for paper in papers:
        item = dict(paper)
        authors = item.get("authors") or []
        item["authors"] = ", ".join(authors[:5]) if isinstance(authors, list) else authors
        item["abstract"] = str(item.get("abstract") or "")[:500]
        item["quality_score"] = item.get("score") if item.get("score_breakdown") else None
        item["score_status"] = "scored" if item.get("score_breakdown") else "not_scored"
        projected.append(item)
    return {
        "status": "success", "query": snapshot["query"], "papers": projected,
        "total": len(projected), "candidate_count": len(candidates),
        "search_id": snapshot["search_id"],
        "next_offset": cursor if cursor < len(candidates) and budget else None,
        "context_budget_remaining": budget,
        "candidate_truncated_for_context": len(candidates) > len(projected),
        "model_visible_per_call_limit": PER_CALL, "model_visible_per_run_limit": PER_RUN,
        "dedup_fallback_used": fallback, "dedup_fallback_count": len(projected) if fallback else 0,
        "run_level_dedup_applied": state is not None,
        "returned_sources": sorted({str(p.get("source") or "").split("/", 1)[0] for p in projected}),
        **snapshot["diagnostics"],
    }


async def search(state, query: str, max_results: int = 20, year_from: int | None = None, **options: Any) -> dict:
    """Question-mode default for the registered search_papers tool."""
    from .platform_sources import collect_sources
    switches = {"arxiv": "include_arxiv", "biorxiv": "include_biorxiv", "medrxiv": "include_medrxiv",
                "semantic_scholar": "include_s2", "openalex": "include_openalex", "pubmed": "include_pubmed",
                "crossref": "include_crossref", "cnki": "include_cnki"}
    explicit = any(options.get(flag) is not None for flag in switches.values())
    legacy = {"arxiv", "semantic_scholar", "openalex", "crossref", "cnki"}
    sources = {name for name, flag in switches.items() if options.get(flag) is True or
               (options.get(flag) is None and (not explicit or name in legacy))}
    if not sources:
        return {"status": "error", "error": "至少要启用一个检索来源", "code": "invalid_sources"}
    # Same default public sources as the search page; S2 remains available when
    # explicitly selected, without silently changing the UI's source policy.
    if not explicit:
        sources.discard("semantic_scholar")
    include_school = options.get("include_school") if options.get("include_school") is not None else not explicit
    private_task = None

    def progress(event):
        nonlocal private_task
        if event.get("stage") == "search_plan" and private_task is None:
            private_task = asyncio.create_task(collect_sources(
                query, event.get("search_plan", []), cnki="cnki" in sources,
                school=include_school,
            ))

    try:
        result = await academic_search.run_literature_search(
            {"query": query, "limit": 200}, progress,
            enabled_sources=sources if explicit else None, year_from=year_from,
        )
        extra = await private_task if private_task else {"papers": [], "warnings": [], "sources": {}}
    except asyncio.CancelledError:
        if private_task:
            private_task.cancel()
            await asyncio.gather(private_task, return_exceptions=True)
        raise
    except Exception as exc:
        if private_task:
            private_task.cancel()
            await asyncio.gather(private_task, return_exceptions=True)
        return {"status": "error", "code": getattr(exc, "code", "search_failed"), "error": str(exc)[:500]}
    # Platform-owned records stay in their user/institution stores. Never write
    # private CNKI/school records to the shared public literature index.
    from shared.contrib.literature_federation import merge_channels, merge_school_papers
    public_and_school = merge_school_papers(result, extra.get("school_papers", []), limit=200)
    papers = merge_channels(public_and_school["papers"], extra.get("cnki_papers", []))
    if year_from is not None:
        def within_year(paper):
            try:
                return int(str(paper.get("year"))[:4]) >= year_from
            except (TypeError, ValueError):
                return True
        papers = [p for p in papers if within_year(p)]
    search_id = hashlib.sha256(json.dumps([query, sorted(sources), year_from, include_school], ensure_ascii=False).encode()).hexdigest()[:20]
    diagnostics = {key: result.get(key) for key in (
        "search_plan", "decomposed_queries", "requested_sources", "attempted_sources", "unavailable_sources",
        "source_counts", "warnings", "timings", "strategy_diagnostics", "excluded_low_relevance",
    )}
    diagnostics["warnings"] = [*(diagnostics.get("warnings") or []), *extra["warnings"]]
    diagnostics["platform_sources"] = extra["sources"]
    diagnostics["requested_sources"] = sorted(sources | ({"school_library"} if include_school else set()))
    attempted = set(diagnostics.get("attempted_sources") or [])
    if extra["sources"].get("cnki", {}).get("jobs"):
        attempted.add("cnki")
    if extra["sources"].get("school", {}).get("state") == "completed":
        attempted.add("school_library")
    diagnostics["attempted_sources"] = sorted(attempted)
    unavailable = set(diagnostics.get("unavailable_sources") or [])
    for warning in extra["warnings"]:
        if warning.get("code", "").endswith("unavailable"):
            unavailable.update(warning.get("sources", []))
    diagnostics["unavailable_sources"] = sorted(unavailable)
    diagnostics["source_counts"] = {}
    for paper in papers:
        source = str(paper.get("source") or "")
        diagnostics["source_counts"][source] = diagnostics["source_counts"].get(source, 0) + 1
    diagnostics["requested_query_mode"] = "question"
    diagnostics["query_result_cache_used"] = False
    snapshot = {"search_id": search_id, "query": query, "papers": papers, "diagnostics": diagnostics,
                "public_and_school": public_and_school["papers"], "year_from": year_from}
    if state:
        pools = state.hook_state.setdefault(POOL_KEY, {})
        pools[search_id] = snapshot
        while len(pools) > 8:
            del pools[next(iter(pools))]
    return _page(state, snapshot, limit=min(30, max(1, max_results)))


async def read_candidates(state, search_id: str, offset: int = 0, limit: int = 20,
                          refresh_sources: bool = False, **_) -> dict:
    snapshot = state.hook_state.get(POOL_KEY, {}).get(search_id) if state else None
    if snapshot is None:
        return {"status": "error", "error": "当前运行没有这次检索的候选，请先调用 search_papers。"}
    if refresh_sources:
        from .platform_sources import call
        from shared.contrib.literature_federation import merge_channels
        diagnostics = snapshot["diagnostics"]
        jobs = diagnostics.get("platform_sources", {}).get("cnki", {}).get("jobs", [])
        if jobs:
            results = await asyncio.gather(*(call("cnki_status", {"job_id": job["id"]})
                                             for job in jobs), return_exceptions=True)
            if all(isinstance(job, dict) for job in results):
                cnki = merge_channels(*(job.get("result", {}).get("papers", []) for job in results))
                papers = merge_channels(snapshot["public_and_school"], cnki)
                year_from = snapshot.get("year_from")
                if year_from is not None:
                    def in_range(paper):
                        try:
                            return int(str(paper.get("year"))[:4]) >= year_from
                        except (TypeError, ValueError):
                            return True
                    papers = [p for p in papers if in_range(p)]
                snapshot["papers"] = papers
                diagnostics["platform_sources"]["cnki"]["jobs"] = [
                    {k: job.get(k) for k in ("id", "query", "state", "count", "detail")} for job in results]
                if all(job.get("state") == "completed" for job in results):
                    diagnostics["warnings"] = [w for w in diagnostics.get("warnings", []) if w.get("code") != "cnki_pending"]
                diagnostics["source_counts"] = {}
                for paper in papers:
                    source = str(paper.get("source") or "")
                    diagnostics["source_counts"][source] = diagnostics["source_counts"].get(source, 0) + 1
                # New CNKI rows alter the federated order. Re-scan from the front;
                # run-level identity tracking skips already seen candidates.
                offset = 0
            else:
                diagnostics["warnings"] = [w for w in diagnostics.get("warnings", []) if w.get("code") != "cnki_refresh_failed"]
                diagnostics["warnings"].append({"code": "cnki_refresh_failed", "sources": ["cnki"],
                    "detail": "本次无法读取知网任务的新进度，已保留此前结果。"})
    return _page(state, snapshot, offset=max(0, offset), limit=min(30, max(1, limit)))
