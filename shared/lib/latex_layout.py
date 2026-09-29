"""Deterministic LaTeX layout diagnostics.

TeX exits successfully for several conditions that make the produced PDF
undeliverable.  This module turns the unambiguous subset of those diagnostics
into a machine-readable gate.  It intentionally does not try to infer visual
quality from arbitrary warning prose; every blocking category below maps to a
specific TeX/package diagnostic with a measurable overflow.
"""

from __future__ import annotations

import re
from typing import Any

_BOX_WARNING_RE = re.compile(
    r"Overfull \\(?P<axis>[hv])box \((?P<points>\d+(?:\.\d+)?)pt too (?:wide|high)\)"
    r"(?P<context>[^\n]*)",
    re.IGNORECASE,
)
_FLOAT_TOO_LARGE_RE = re.compile(
    r"LaTeX Warning: Float too large for page by (?P<points>\d+(?:\.\d+)?)pt"
    r"(?P<context>[^\n]*)",
    re.IGNORECASE,
)
_TABULARX_TOO_WIDE_RE = re.compile(
    r"Package tabularx Warning: X Columns too narrow \(table too wide\)",
    re.IGNORECASE,
)
_UNDEFINED_RE = re.compile(
    r"LaTeX Warning: There were undefined (?P<kind>references|citations)",
    re.IGNORECASE,
)

# Ordinary prose can create tiny overfull hboxes because of an indivisible URL
# or glyph.  A 12 pt spill is already roughly one body-text character and is a
# deterministic delivery defect.  Alignment overflows and vertical overflows
# are never tolerated: those are structured content leaving its allocated box.
MAX_PROSE_HBOX_OVERFLOW_PT = 12.0


def _finding(
    category: str,
    message: str,
    *,
    points: float | None = None,
    context: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {"category": category, "message": message}
    if points is not None:
        result["overflow_pt"] = points
    if context.strip():
        result["context"] = context.strip()
    return result


def audit_latex_log(log_text: str) -> dict[str, Any]:
    """Classify final-pass LaTeX diagnostics into failures and warnings."""
    hard_failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for _ in _TABULARX_TOO_WIDE_RE.finditer(log_text):
        hard_failures.append(
            _finding(
                "tabularx_table_too_wide",
                "tabularx could not allocate usable X columns; the table is wider than its box",
            )
        )

    for match in _FLOAT_TOO_LARGE_RE.finditer(log_text):
        points = float(match.group("points"))
        hard_failures.append(
            _finding(
                "float_too_large_for_page",
                "a float is taller than the available page area",
                points=points,
                context=match.group("context"),
            )
        )

    for match in _BOX_WARNING_RE.finditer(log_text):
        points = float(match.group("points"))
        axis = match.group("axis").lower()
        context = match.group("context")
        in_alignment = "alignment" in context.lower()
        if axis == "v":
            category = "overfull_vbox"
            hard = True
        elif in_alignment:
            category = "overfull_alignment"
            hard = True
        else:
            category = "overfull_hbox"
            hard = points > MAX_PROSE_HBOX_OVERFLOW_PT
        finding = _finding(
            category,
            "content extends beyond its allocated TeX box",
            points=points,
            context=context,
        )
        (hard_failures if hard else warnings).append(finding)

    for match in _UNDEFINED_RE.finditer(log_text):
        kind = match.group("kind").lower()
        hard_failures.append(
            _finding(
                f"undefined_{kind}",
                f"the final LaTeX pass still contains undefined {kind}",
            )
        )

    all_overflows = [
        float(item["overflow_pt"])
        for item in (*hard_failures, *warnings)
        if "overflow_pt" in item
    ]
    return {
        "schema_version": 1,
        "passed": not hard_failures,
        "hard_failure_count": len(hard_failures),
        "warning_count": len(warnings),
        "max_overflow_pt": max(all_overflows, default=0.0),
        "hard_failures": hard_failures,
        "warnings": warnings,
    }
