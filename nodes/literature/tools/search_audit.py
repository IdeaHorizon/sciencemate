"""Bounded local evidence for one public academic-search execution."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import uuid
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

PAPER_FIELDS = ("title", "abstract", "authors", "year", "venue", "doi", "url",
                "source", "pub_type", "publication_category", "metadata_provenance")


def search_code_fingerprint() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[3]
    for name in ("nodes/literature/tools/academic_search.py", "nodes/literature/tools/academic_admission.py",
                 "nodes/literature/tools/search_engines.py", "nodes/literature/tools/academic_strategy.py",
                 "nodes/literature/tools/filter.py", "shared/contrib/literature_identity.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def write_search_audit(record: dict) -> bool:
    temporary = None
    try:
        from core.paths import literature_cache_dir
        directory = literature_cache_dir() / "search_audits"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record["search_code_sha256"] = search_code_fingerprint()
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload + "\n")
        temporary.replace(directory / (record["audit_id"] + ".json"))
        for old in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-200]:
            old.unlink(missing_ok=True)
        return True
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return False


def audited_search(function):
    @wraps(function)
    async def run(request, progress=None, **options):
        audit_id = uuid.uuid4().hex
        started = time.monotonic()
        payload = dict(request) if isinstance(request, dict) else request
        if isinstance(payload, dict) and not payload.get("request_id"):
            payload["request_id"] = audit_id
        record = {
            "schema_version": 1, "audit_id": audit_id,
            "request_id": payload.get("request_id") if isinstance(payload, dict) else None,
            "started_at": datetime.now(UTC).isoformat(),
            "scope": "shared_public_academic_search",
            "request": {key: request[key] for key in ("query", "limit", "remote_refresh")
                        if isinstance(request, dict) and key in request},
            "options": {"year_from": options.get("year_from"),
                        "enabled_sources": sorted(options["enabled_sources"]) if options.get("enabled_sources") is not None else None},
            "events": [],
        }

        def on_progress(event):
            record["events"].append({key: event[key] for key in (
                "stage", "progress_percent", "query_index", "result_count", "raw_count",
            ) if key in event})
            record["events"] = record["events"][-200:]
            if progress is not None:
                progress(event)

        try:
            result = await function(payload, progress=on_progress, **options)
        except BaseException as exc:
            record.update(status="cancelled" if type(exc).__name__ == "CancelledError" else "failed",
                          error_type=type(exc).__name__, error_code=getattr(exc, "code", None),
                          elapsed_seconds=round(time.monotonic() - started, 4))
            write_search_audit(record)
            details = getattr(exc, "details", None)
            if isinstance(details, dict):
                details["audit_id"] = audit_id
            raise
        diagnostic = result.setdefault("strategy_diagnostics", {})
        diagnostic["audit_id"] = audit_id
        papers = [{key: paper[key] for key in PAPER_FIELDS if key in paper}
                  for paper in result.get("papers", [])]
        record.update(
            status="completed", elapsed_seconds=round(time.monotonic() - started, 4),
            query=result.get("query"), search_plan=result.get("search_plan", []),
            decomposed_queries=result.get("decomposed_queries", []),
            requested_sources=result.get("requested_sources", []),
            attempted_sources=result.get("attempted_sources", []),
            unavailable_sources=result.get("unavailable_sources", []),
            from_cache=result.get("from_cache", False), timings=result.get("timings", {}),
            selection_rules=diagnostic.get("selection_rules", {}),
            admission=diagnostic.get("admission", {}), metadata_reuse=diagnostic.get("metadata_reuse", {}),
            source_audits=diagnostic.get("source_audits", []),
            papers=papers,
            result_sha256=hashlib.sha256(json.dumps(papers, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        )
        diagnostic["audit_status"] = "saved" if write_search_audit(record) else "write_failed"
        return result
    return run
