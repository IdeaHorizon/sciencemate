"""Recover missing hypothesis artifacts when LLM output was truncated."""
from __future__ import annotations

import json
import logging
from typing import Any

from core.state import State

from .tools.artifact_staging import commit_staged_artifact, draft_path

log = logging.getLogger("hypothesis.artifact_recovery")

_DEFAULT_PLAN_NAME = "Research_Plan"


def _iter_transcript(state: State) -> list[dict[str, Any]]:
    if not state.transcript_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _research_plan_body_from_transcript(state: State) -> tuple[str, str] | None:
    """Return (content, plan_name) from latest passed audit tool_call args."""
    events = _iter_transcript(state)
    pending: dict[str, Any] | None = None
    for event in events:
        if event.get("event") == "tool_call" and event.get("name") == "audit_computational_workflow":
            pending = event
            continue
        if pending and event.get("event") == "tool_result":
            preview = event.get("result_preview")
            passed = False
            if isinstance(preview, dict):
                passed = preview.get("passed") is True
            elif isinstance(preview, str):
                passed = '"passed": true' in preview or "'passed': True" in preview
            if passed:
                args = pending.get("args") or {}
                content = (args.get("content") or "").strip()
                if content:
                    name = (args.get("plan_name") or _DEFAULT_PLAN_NAME).strip()
                    return content, name
            pending = None
    return None


def _research_plan_body(state: State) -> tuple[str, str] | None:
    cached = (state.hook_state.get("last_audited_research_plan") or "").strip()
    if cached:
        name = (state.hook_state.get("last_audited_plan_name") or _DEFAULT_PLAN_NAME).strip()
        return cached, name

    # 落点问 `draft_path`（写方也问它）。这里以前自己拼 `state.root / "outputs/…"`，
    # 而草稿写在 `<worktree>/hypothesis/drafts/` —— 恢复路径因此永远走不通。
    staged = draft_path(state, "research_plan")
    if staged.is_file():
        text = staged.read_text(encoding="utf-8").strip()
        if text:
            return text, _DEFAULT_PLAN_NAME

    return _research_plan_body_from_transcript(state)


def recover_missing_artifacts(state: State) -> list[dict[str, Any]]:
    """Best-effort recovery when loop ended without saving research_plan."""
    recovered: list[dict[str, Any]] = []
    if state.list_artifacts("research_plan"):
        return recovered

    body_info = _research_plan_body(state)
    if not body_info:
        return recovered

    content, plan_name = body_info
    try:
        result = commit_staged_artifact(
            state,
            "research_plan",
            plan_name,
            content,
            metadata={"recovered_from": "artifact_recovery_hook"},
        )
        recovered.append({"artifact_type": "research_plan", **result})
        log.info("recovered research_plan (run=%s, id=%s)", state.run_id, result.get("id"))
    except Exception as exc:
        log.warning("research_plan recovery failed: %s", exc)

    return recovered
