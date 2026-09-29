"""Archive RSS records through the shared literature paper layout.

RSS is a metadata source, not a second paper store: every record uses the same
``literature/papers/<doi>/{index,paper,figure}`` layout and catalog as the
literature node's Crossref/OpenAlex results.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


from app.services.harness_imports import ensure_harness_importable


def _year(value: Any) -> int | None:
    text = str(value or "")
    try:
        return int(text[:4]) if text[:4].isdigit() else None
    except (TypeError, ValueError):
        return None


def _record(item: Any, source_name: str, domains: list[str] | None = None) -> dict[str, Any]:
    ensure_harness_importable()
    key = str(getattr(item, "canonical_key", "") or "")
    doi = key[4:] if key.startswith("doi:") else ""
    published = getattr(item, "published_at", None)
    return {
        "title": str(getattr(item, "title", "") or ""),
        "authors": list(getattr(item, "authors", []) or []),
        "year": _year(published),
        "venue": str(getattr(item, "venue", "") or source_name),
        "doi": doi,
        "url": str(getattr(item, "url", "") or ""),
        "abstract": str(getattr(item, "summary", "") or ""),
        "source": f"rss:{source_name}",
        "source_type": "rss",
        "source_name": source_name,
        "domains": list(domains or []),
        "rss_canonical_key": key,
        "published_at": published.isoformat() if isinstance(published, datetime) else str(published or ""),
        "image_url": str(getattr(item, "image_url", "") or ""),
        "is_oa": False,
        "oa_status": "unknown",
        "oa_url": "",
    }


async def archive_rss_item(
    item: Any, source_name: str, client: Any | None = None, *,
    allow_fallback: bool = True, domains: list[str] | None = None,
) -> dict[str, Any]:
    """Archive one RSS item using the common index/figure strategy.

    ``client`` is accepted for the ingest API but the shared image strategy owns
    its own bounded HTTP clients. ``allow_fallback`` is kept as an explicit hook
    for future scheduling policy; RSS currently always attempts the same strategy.
    """
    ensure_harness_importable()
    from core.paths import literature_papers_dir
    from nodes.literature.tools.archive_papers import archive_index_and_figure

    record = _record(item, source_name, domains)
    if not allow_fallback:
        record["image_attempt_policy"] = "disabled"
    return await asyncio.to_thread(
        archive_index_and_figure, record, str(literature_papers_dir())
    )
