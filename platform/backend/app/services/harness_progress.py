"""Map Harness transport records to the small user-facing progress contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_VISIBLE_PROTOCOL_TYPES = frozenset({"progress", "background_wait", "pause_required"})
_INTERNAL_TOOL_NAMES = frozenset({"write_scratchpad"})
_INTERNAL_TOOL_PREFIXES = ("_", "hook_", "internal_", "platform_")
_IDENTITY_FIELDS = (
    "request_id",
    "run_id",
    "child_run_id",
    "tool_call_id",
    "pending_tool_call_id",
    "pause_id",
    "job_id",
    "task_id",
    "tool_name",
    "stage",
    "phase",
)
_MAX_DETAIL_LENGTH = 280


def _text(event: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if len(text) > _MAX_DETAIL_LENGTH:
                return f"{text[: _MAX_DETAIL_LENGTH - 1].rstrip()}\u2026"
            return text
    return None


def _humanize(value: str) -> str:
    words = " ".join(value.replace("-", " ").replace("_", " ").split())
    return words[:1].upper() + words[1:] if words else ""


def _is_internal_progress(event: dict[str, Any]) -> bool:
    if (
        event.get("internal") is True
        or event.get("internal_only") is True
        or event.get("user_visible") is False
    ):
        return True
    namespace = event.get("source") or event.get("namespace") or event.get("channel")
    if isinstance(namespace, str) and namespace.strip().lower() in {
        "hook",
        "internal",
        "platform",
    }:
        return True
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str):
        return False
    normalized = tool_name.strip().lower().replace("-", "_").replace(".", "_")
    return normalized in _INTERNAL_TOOL_NAMES or normalized.startswith(_INTERNAL_TOOL_PREFIXES)


def _progress_id(event: dict[str, Any], kind: str) -> str:
    identity = {"type": kind}
    for field in _IDENTITY_FIELDS:
        value = event.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            identity[field] = value
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"harness-progress-{digest[:24]}"


def protocol_user_progress(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return sanitized UI progress for allowlisted Harness records only.

    Transport lifecycle records such as ``started``, ``transcript`` and
    ``child_event`` deliberately return ``None``. They still travel through the
    separate protocol callback used by durable transcript ingest.
    """

    kind = event.get("type")
    if kind not in _VISIBLE_PROTOCOL_TYPES:
        return None
    if kind == "progress" and _is_internal_progress(event):
        return None

    iteration = event.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        iteration = 1

    if kind == "progress":
        tool_name = _text(event, "tool_name")
        readable_tool = _humanize(tool_name) if tool_name else None
        detail = _text(event, "message", "detail")
        if detail is None:
            detail = f"Using {readable_tool}." if readable_tool else "The agent is working."
        progress = {
            "id": _progress_id(event, kind),
            "event": "tool.progress",
            "label": f"Using {readable_tool}" if readable_tool else "Agent is working",
            "detail": detail,
            "iteration": iteration,
        }
        if tool_name:
            progress["tool_name"] = tool_name
        return progress

    if kind == "background_wait":
        detail = _text(event, "message", "detail") or "A background task is still running."
        return {
            "id": _progress_id(event, kind),
            "event": "run.waiting_compute",
            "label": "Waiting for background work",
            "detail": detail,
            "iteration": iteration,
        }

    detail = (
        _text(event, "question", "message", "detail")
        or "The run is waiting for your input."
    )
    return {
        "id": _progress_id(event, kind),
        "event": "run.paused",
        "label": "Needs input",
        "detail": detail,
        "status": "waiting_human",
        "iteration": iteration,
    }
