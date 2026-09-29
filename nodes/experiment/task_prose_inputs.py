"""Node-owned declaration of which Experiment inputs contain task prose.

``expected_inputs`` has only human-facing descriptions, so it cannot safely
distinguish task body from artifact identities, paths, or parameters.  The two
explicit lists in this node harness are the sole machine-readable source for
that distinction.  Core deliberately ignores these node-private keys.
"""
from __future__ import annotations

import hashlib
import json

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_HARNESS_PATH = Path(__file__).with_name("harness.yaml")


class TaskProseInputConfigurationError(RuntimeError):
    """The node task-prose declaration cannot safely be consumed."""


@dataclass(frozen=True)
class TaskProseInputContract:
    """Canonical task-body inputs plus legacy read-only aliases."""

    canonical_keys: tuple[str, ...]
    compatibility_keys: tuple[str, ...]

    @property
    def ordered_keys(self) -> tuple[str, ...]:
        return self.canonical_keys + self.compatibility_keys


@dataclass(frozen=True)
class TaskProseSource:
    """The one declared task-body field selected for a run."""

    key: str
    text: str


def _string_key_list(
    raw: Any,
    *,
    field: str,
    path: Path,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(raw, list) or (not allow_empty and not raw):
        expectation = "a list of non-empty strings"
        if not allow_empty:
            expectation = "a non-empty list of non-empty strings"
        raise TaskProseInputConfigurationError(
            f"{path}: {field} must be {expectation}"
        )
    if any(not isinstance(item, str) or not item.strip() for item in raw):
        raise TaskProseInputConfigurationError(
            f"{path}: {field} must contain only non-empty strings"
        )
    keys = tuple(item.strip() for item in raw)
    if len(set(keys)) != len(keys):
        raise TaskProseInputConfigurationError(
            f"{path}: {field} must not repeat a key"
        )
    return keys


def load_task_prose_input_contract(
    harness_path: Path | None = None,
) -> TaskProseInputContract:
    """Load and validate the node-owned task-body declaration.

    Canonical keys must be actual ``expected_inputs`` keys.  Compatibility keys
    are deliberately not formal inputs: if a caller becomes formal, move it to
    the canonical list instead of silently assigning it two identities.
    """
    path = harness_path or _HARNESS_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TaskProseInputConfigurationError(
            f"cannot load task-prose declaration from {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TaskProseInputConfigurationError(
            f"{path}: harness root must be a mapping"
        )
    expected_inputs = raw.get("expected_inputs")
    if not isinstance(expected_inputs, dict):
        raise TaskProseInputConfigurationError(
            f"{path}: expected_inputs must be a mapping"
        )
    canonical = _string_key_list(
        raw.get("task_prose_input_keys"),
        field="task_prose_input_keys",
        path=path,
    )
    compatibility = _string_key_list(
        raw.get("task_prose_compatibility_input_keys", []),
        field="task_prose_compatibility_input_keys",
        path=path,
        allow_empty=True,
    )
    undeclared = [key for key in canonical if key not in expected_inputs]
    if undeclared:
        raise TaskProseInputConfigurationError(
            f"{path}: canonical task-prose keys are not expected_inputs: "
            + ", ".join(undeclared)
        )
    overlap = set(canonical) & set(compatibility)
    if overlap:
        raise TaskProseInputConfigurationError(
            f"{path}: task-prose canonical and compatibility keys overlap: "
            + ", ".join(sorted(overlap))
        )
    formal_compatibility = [
        key for key in compatibility if key in expected_inputs
    ]
    if formal_compatibility:
        raise TaskProseInputConfigurationError(
            f"{path}: compatibility task-prose keys must be undeclared aliases: "
            + ", ".join(formal_compatibility)
        )
    return TaskProseInputContract(canonical, compatibility)


# Importing either consumer validates the declaration during node bootstrap.
TASK_PROSE_INPUT_CONTRACT = load_task_prose_input_contract()
TASK_PROSE_INPUT_KEYS = TASK_PROSE_INPUT_CONTRACT.ordered_keys


def first_task_prose_source(
    values: Mapping[str, Any] | None,
) -> TaskProseSource | None:
    """Select one non-empty source in canonical-then-compatibility order."""
    if values is None:
        return None
    for key in TASK_PROSE_INPUT_KEYS:
        value = values.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text:
            return TaskProseSource(key=key, text=text)
    return None


TASK_PROSE_INPUT_RECEIPT_EVENT = "experiment_task_prose_input_receipt"
TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION = 1
_TASK_PROSE_INPUT_RECEIPT_CACHE_KEY = "_experiment_task_prose_input_receipt_v1"


def _has_only_the_initial_loop_seed(state: Any) -> bool:
    """True only while the current transcript has its first loop seed."""
    transcript_path = getattr(state, "transcript_path", None)
    if not isinstance(transcript_path, Path):
        return False
    count = 0
    try:
        with transcript_path.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("event") == "loop_seed":
                    count += 1
                    if count > 1:
                        return False
    except (OSError, ValueError, TypeError):
        return False
    return count == 1


def freeze_initial_task_prose_source(state: Any) -> None:
    """Freeze the structured turn-one task-prose selection into transcript.

    ``node_inputs`` is structured before Core renders it as Markdown. This
    node-owned compatibility receipt is written after the initial loop seed and
    before the model can call a tool, so later prompt text or resume inputs
    cannot manufacture a task-body source. Resource tools consume the
    transcript receipt rather than this mutable hook-state cache.
    """
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return
    if _TASK_PROSE_INPUT_RECEIPT_CACHE_KEY in hook_state:
        return
    if not _has_only_the_initial_loop_seed(state):
        return
    raw_inputs = hook_state.get("node_inputs")
    inputs = raw_inputs if isinstance(raw_inputs, Mapping) else {}
    if any(not isinstance(key, str) or not key for key in inputs):
        return
    source = first_task_prose_source(inputs)
    source_text = source.text if source is not None else None
    receipt = {
        "schema_version": TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION,
        "origin": "turn_one_node_inputs",
        "node_input_keys": sorted(inputs),
        "source_key": source.key if source is not None else None,
        "source_text": source_text,
        "source_sha256": (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_text is not None else None
        ),
    }
    try:
        state.append_transcript(TASK_PROSE_INPUT_RECEIPT_EVENT, **receipt)
    except Exception:
        return
    hook_state[_TASK_PROSE_INPUT_RECEIPT_CACHE_KEY] = receipt
