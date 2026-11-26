"""Presentation contracts for runtime facts, including historical event readers.

Dispatch status carries identities, never arbitrary task text. The old adapter
stored dispatch announcements as speech, so readers upgrade them by provenance
or their declared dispatch relationship, without inspecting the text itself.
The original transcript/database record remains available for diagnostics.
"""
from __future__ import annotations

from typing import Any


def node_dispatch_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "nodeType": str(raw.get("node_type") or raw.get("nodeType")
                        or raw.get("aboutNodeType") or ""),
        "background": bool(raw.get("background")),
    }
    dispatch_id = raw.get("dispatch_id") or raw.get("dispatchId")
    if dispatch_id:
        payload["dispatchId"] = str(dispatch_id)
    # Submission is attribution, not a message anchor or display text.
    if raw.get("submissionId"):
        payload["submissionId"] = str(raw["submissionId"])
    return payload


def presentation_event(
    kind: str, source: dict[str, Any], payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    legacy_dispatch = kind == "orchestrator.said" and (
        source.get("rawEvent") == "node_dispatch_announced"
        or (not source.get("rawEvent") and payload.get("aboutNodeType")
            and not payload.get("repliesToMessageId"))
    )
    if kind == "node.dispatched" or legacy_dispatch:
        return "node.dispatched", node_dispatch_payload(payload)
    return kind, payload
