"""Shared publication windows for acquisition and recommendation."""

from __future__ import annotations

from datetime import datetime, timedelta
import re

ACADEMIC_WINDOW_DAYS = 31
WEB_WINDOW_MONTHS = 12


def publication_precision(raw: object) -> str:
    """Preserve YYYY-MM source dates before parsing supplies placeholder day one."""
    text = str(raw or "").strip().split("T", 1)[0].replace("/", "-")
    parts = [part for part in text.split("-") if part]
    if not parts or not all(re.fullmatch(r"\d+", part) for part in parts[:3]):
        return ""
    if len(parts) >= 3:
        return "day"
    return "month" if len(parts) == 2 else "year"


def academic_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=ACADEMIC_WINDOW_DAYS)


def web_cutoff(now: datetime) -> datetime:
    try:
        return now.replace(year=now.year - 1)
    except ValueError:  # February 29 in a leap year.
        return now.replace(year=now.year - 1, day=28)


def month_overlaps_window(published: datetime, cutoff: datetime, now: datetime) -> bool:
    """A YYYY-MM source date represents the whole month, not its first day."""
    next_month = (published.replace(day=28) + timedelta(days=4)).replace(day=1)
    return published <= now and next_month > cutoff


def is_web_discovery(extra: object) -> bool:
    if not isinstance(extra, dict):
        return False
    routes = extra.get("feed_acquisition_routes") or []
    mode = str(extra.get("classification_mode") or "")
    return (
        "web" in routes
        or bool(extra.get("web_search_backend"))
        or mode.endswith(("_web", "_web_search"))
    )
