"""Describe abstract availability without treating provider failures as absence."""

TERMINAL_EMPTY_RESULTS = frozenset({"no_abstract", "not_found", "http_404"})
ACCESS_BLOCKED_RESULTS = frozenset({"http_401", "http_403", "captcha", "verification_required"})


def lookup_failed(result: str) -> bool:
    return result not in TERMINAL_EMPTY_RESULTS | {"found"}


def abstract_processing_status(record: dict) -> str:
    if str(record.get("abstract") or "").strip():
        return "available"
    states = record.get("abstract_lookup_source_state") or {}
    results = [str(row.get("result") or "") for row in states.values() if isinstance(row, dict)]
    if any(result in ACCESS_BLOCKED_RESULTS for result in results):
        return "access_blocked"
    if any(lookup_failed(result) for result in results):
        return "retry_pending"
    if record.get("abstract_lookup_completed") is True:
        return "not_found"
    if not results and record.get("literature_preparation") == "ready" and record.get("abstract_enrichment_attempted"):
        # Keep usable legacy records visible, without claiming every source was checked.
        return "legacy_completed"
    if results or record.get("abstract_enrichment_attempted"):
        return "pending"
    return "not_requested"
