"""Repair malformed JSON in LLM tool-call arguments (e.g. LaTeX \\| in research_plan).

Also normalizes common tool-call singleton wrappers for prereg metadata:
  {"item": ["claim_…"]} / {"items": …} / {"value": …}  →  inner value
which otherwise fails freeze_and_register capital_basis checks.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("hypothesis.json_tool_args")

_VALID_JSON_ESCAPES = frozenset('"\\bfnrt/')


def repair_json_string_escapes(raw: str) -> str:
    """Double lone backslashes inside JSON string literals."""
    if not raw:
        return raw or "{}"

    out: list[str] = []
    i = 0
    in_string = False
    while i < len(raw):
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if ch == "\\":
            if i + 1 >= len(raw):
                out.append(ch)
                i += 1
                continue
            nxt = raw[i + 1]
            if nxt in _VALID_JSON_ESCAPES:
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            if nxt == "u" and i + 5 < len(raw):
                out.append(raw[i : i + 6])
                i += 6
                continue
            out.append("\\")
            out.append("\\")
            out.append(nxt)
            i += 2
            continue

        out.append(ch)
        if ch == '"':
            in_string = False
        i += 1

    return "".join(out)


def parse_tool_arguments(raw: str | dict | None) -> tuple[dict, str | None]:
    """Parse tool arguments; repair invalid escapes when possible."""
    if isinstance(raw, dict):
        return raw, None
    text = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as first_err:
        repaired = repair_json_string_escapes(text)
        if repaired == text:
            return {}, str(first_err)
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError as second_err:
            return {}, str(second_err)
        if not isinstance(parsed, dict):
            return {}, "tool arguments must be a JSON object"
        return parsed, repaired
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


# Common singleton wrappers LLMs / schema adapters put around lists & scalars.
# Only peel when the dict has exactly one key and that key is in this set —
# so real objects like {"substitutes": {}, "deferred": []} stay intact.
_SINGLETON_ENVELOPE_KEYS = frozenset({
    "item",
    "items",
    "value",
    "values",
    "data",
    "element",
    "elements",
    "entry",
    "entries",
    "result",
    "results",
    "list",
    "array",
})


def unwrap_singleton_envelope(value: Any) -> Any:
    """Peel common LLM tool-call singleton wrappers (possibly nested).

    Examples that become the inner value:
      {"item": ["a", "b"]}           → ["a", "b"]
      {"items": {"value": ["a"]}}    → ["a"]
      {"data": "none_found"}         → "none_found"

    Multi-key objects are not peeled at the top level (only their values are
    walked), so ``{"substitutes": {}, "deferred": []}`` is preserved.
    """
    while (
        isinstance(value, dict)
        and len(value) == 1
        and next(iter(value)) in _SINGLETON_ENVELOPE_KEYS
    ):
        value = next(iter(value.values()))
    if isinstance(value, dict):
        return {k: unwrap_singleton_envelope(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_singleton_envelope(v) for v in value]
    return value


# Backward-compatible alias used by earlier call sites / docs.
unwrap_item_envelope = unwrap_singleton_envelope


def normalize_prereg_metadata(metadata: dict | None) -> tuple[dict, bool]:
    """Normalize prereg metadata shapes that block freeze_and_register.

    Returns ``(normalized_metadata, changed)``.
    """
    if not isinstance(metadata, dict):
        return {}, False
    meta = dict(metadata)
    changed = False

    if "capital_basis" in meta:
        raw = meta["capital_basis"]
        unwrapped = unwrap_singleton_envelope(raw)
        if unwrapped != raw:
            meta["capital_basis"] = unwrapped
            changed = True
            log.info(
                "normalized capital_basis %s → %s",
                type(raw).__name__,
                type(unwrapped).__name__,
            )

    if "execution_commitment" in meta:
        raw_ec = meta["execution_commitment"]
        unwrapped_ec = unwrap_singleton_envelope(raw_ec)
        if unwrapped_ec != raw_ec:
            meta["execution_commitment"] = unwrapped_ec
            changed = True
        ec = meta.get("execution_commitment")
        if isinstance(ec, dict):
            cleaned = dict(ec)
            # Empty strings are not valid substitutes/deferred declarations.
            for key in ("substitutes", "deferred"):
                if cleaned.get(key) == "":
                    if key == "substitutes":
                        cleaned[key] = {}
                    else:
                        cleaned[key] = []
                    changed = True
            if cleaned != ec:
                meta["execution_commitment"] = cleaned

    return meta, changed


def sanitize_message_tool_calls(messages: list) -> int:
    """Repair tool-call argument strings so the next LLM request is valid JSON.

    Also unwraps ``{"item": …}`` envelopes inside ``save_artifact`` metadata when
    present in assistant history (helps the next LLM turn see the corrected shape).
    """
    fixed = 0
    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue
        for tc in tool_calls:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if not isinstance(raw_args, str) or not raw_args.strip():
                continue
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                repaired = repair_json_string_escapes(raw_args)
                if repaired != raw_args:
                    fn["arguments"] = repaired
                    fixed += 1
                    log.info("repaired tool-call arguments (%d chars)", len(repaired))
                try:
                    parsed = json.loads(fn.get("arguments") or raw_args)
                except json.JSONDecodeError:
                    continue
            if not isinstance(parsed, dict):
                continue
            name = fn.get("name") or ""
            if name == "save_artifact" and isinstance(parsed.get("metadata"), dict):
                meta, changed = normalize_prereg_metadata(parsed["metadata"])
                if changed:
                    parsed = dict(parsed)
                    parsed["metadata"] = meta
                    fn["arguments"] = json.dumps(parsed, ensure_ascii=False)
                    fixed += 1
                    log.info("normalized save_artifact metadata in history tool-call")
    return fixed
