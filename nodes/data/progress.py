"""Small progress reporter for data-node single-run debugging."""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any


_FALSE_VALUES = {"0", "false", "off", "no", "quiet"}


@contextmanager
def model_call_timing():
    """Measure a model call without periodic waiting messages.

    On macOS the monotonic clock excludes sleep. Report wall time separately
    so a suspended host cannot look like an unbounded model request.
    """
    started, wall_started = time.monotonic(), time.time()
    timing: dict[str, float] = {}
    try:
        yield timing
    finally:
        elapsed = time.monotonic() - started
        timing["wall_elapsed_s"] = round(time.time() - wall_started, 1)
        gap = timing["wall_elapsed_s"] - elapsed
        if gap > 5:
            timing["suspend_or_clock_gap_s"] = round(gap, 1)


def _enabled() -> bool:
    value = os.getenv("DATA_NODE_PROGRESS", "1").strip().lower()
    return value not in _FALSE_VALUES


def emit_progress(state: Any, stage: str, detail: str = "", *, terminal: bool = True, **fields: Any) -> None:
    """Write a concise progress marker to transcript and stderr.

    This is intentionally data-node local. It gives `run_node.py --harness data`
    stage-level progress without changing the framework core. Set
    DATA_NODE_PROGRESS=0 to silence terminal output; transcript events remain.
    Internal call diagnostics use terminal=False; display stage transitions only.
    """
    payload = {
        "stage": stage,
        "detail": detail,
        **{key: value for key, value in fields.items() if value is not None},
    }
    try:
        state.append_transcript("data_progress", **payload)
    except Exception:
        pass
    if not terminal or not _enabled():
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    extras = " ".join(
        f"{key}={value}" for key, value in payload.items()
        if key not in {"stage", "detail"} and value not in ("", None, [], {})
    )
    message = f"[data {timestamp}] {stage}"
    if detail:
        message += f": {detail}"
    if extras:
        message += f" ({extras})"
    print(message, file=sys.stderr, flush=True)
