"""Per-node execution transcript — captures full agent activity in real-time.

Writes a JSONL file per node execution so that every LLM response, tool call,
and result is visible for debugging, iteration, and observability.

File location: logs/transcripts/{node_id}.jsonl
Each line is a JSON event with timestamp, type, and payload.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Transcript directory (relative to project root)
TRANSCRIPT_DIR = Path(__file__).parent.parent.parent / "logs" / "transcripts"

# Truncation limits for transcript entries
MAX_TEXT_CHARS = 10_000       # LLM text responses
MAX_TOOL_INPUT_CHARS = 5_000  # Tool call inputs (code can be long)
MAX_TOOL_OUTPUT_CHARS = 5_000 # Tool call results
MAX_CODE_CHARS = 20_000       # Python code in execute_python (keep more)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


def _safe_serialize(obj: Any, max_chars: int = 5000) -> Any:
    """Serialize an object for transcript, truncating large strings."""
    if isinstance(obj, str):
        return _truncate(obj, max_chars)
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "code":
                result[k] = _truncate(str(v), MAX_CODE_CHARS)
            elif k in ("content", "_content", "stdout", "stderr", "raw_response"):
                result[k] = _truncate(str(v), max_chars)
            elif isinstance(v, (dict, list)):
                result[k] = _safe_serialize(v, max_chars)
            elif isinstance(v, str) and len(v) > max_chars:
                result[k] = _truncate(v, max_chars)
            else:
                result[k] = v
        return result
    if isinstance(obj, list):
        return [_safe_serialize(item, max_chars) for item in obj[:50]]
    return obj


class NodeTranscript:
    """Captures the full execution transcript for a single node run.

    Usage:
        transcript = NodeTranscript(node_id, node_type)
        transcript.log_system("Starting execution")
        transcript.log_llm_response(iteration, text, tool_calls)
        transcript.log_tool_call(iteration, tool_name, tool_input, result, duration_ms)
        transcript.close()
    """

    def __init__(self, node_id: str, node_type: str, attempt: int = 0) -> None:
        self.node_id = node_id
        self.node_type = node_type
        self.attempt = attempt
        self._start_time = time.monotonic()

        # Ensure directory exists
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

        # File path: {node_id}.jsonl (append mode for retries)
        suffix = f"_retry{attempt}" if attempt > 0 else ""
        self._path = TRANSCRIPT_DIR / f"{node_id}{suffix}.jsonl"
        self._file = open(self._path, "a", encoding="utf-8")

        self._write_event("session_start", {
            "node_id": node_id,
            "node_type": node_type,
            "attempt": attempt,
        })
        logger.info("Transcript opened: %s", self._path)

    def _write_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Write a single event line to the transcript."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.monotonic() - self._start_time, 1),
            "type": event_type,
            **payload,
        }
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
            self._file.write(line + "\n")
            self._file.flush()  # flush immediately for real-time tailing
        except Exception as e:
            logger.warning("Transcript write failed: %s", e)

    def log_system(self, message: str, **extra: Any) -> None:
        """Log a system-level event (state transition, budget check, etc.)."""
        self._write_event("system", {"message": message, **extra})

    def log_llm_start(self, iteration: int) -> None:
        """Log that an LLM call is starting (before streaming begins)."""
        self._write_event("llm_start", {"iteration": iteration})

    def log_llm_token(self, iteration: int, text: str) -> None:
        """Log a batch of streaming tokens from the LLM."""
        self._write_event("llm_token", {"iteration": iteration, "text": text})

    def log_context(self, num_messages: int, total_tokens: int) -> None:
        """Log the assembled context summary."""
        self._write_event("context", {
            "num_messages": num_messages,
            "estimated_tokens": total_tokens,
        })

    def log_llm_response(
        self,
        iteration: int,
        text: str | None,
        tool_calls: list[dict[str, Any]] | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model: str,
    ) -> None:
        """Log a full LLM response — text and/or tool calls."""
        payload: dict[str, Any] = {
            "iteration": iteration,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost_usd, 6),
        }
        if text:
            payload["text"] = _truncate(text, MAX_TEXT_CHARS)
        if tool_calls:
            # Serialize tool calls with input truncation
            tc_summary = []
            for tc in tool_calls:
                tc_entry = {
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": _safe_serialize(tc.get("input", {}), MAX_TOOL_INPUT_CHARS),
                }
                tc_summary.append(tc_entry)
            payload["tool_calls"] = tc_summary

        self._write_event("llm_response", payload)

    def log_tool_call(
        self,
        iteration: int,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
        duration_ms: float,
        permitted: bool,
    ) -> None:
        """Log a tool execution with full input and output."""
        self._write_event("tool_exec", {
            "iteration": iteration,
            "tool": tool_name,
            "permitted": permitted,
            "duration_ms": round(duration_ms, 1),
            "input": _safe_serialize(tool_input, MAX_TOOL_INPUT_CHARS),
            "output": _safe_serialize(result, MAX_TOOL_OUTPUT_CHARS),
        })

    def log_completion(
        self,
        success: bool,
        stop_reason: str,
        iterations: int,
        total_cost: float,
        met: list[str] | None = None,
        unmet: list[str] | None = None,
    ) -> None:
        """Log the final completion evaluation."""
        self._write_event("completion", {
            "success": success,
            "stop_reason": stop_reason,
            "iterations": iterations,
            "total_cost_usd": round(total_cost, 6),
            "met_criteria": met or [],
            "unmet_criteria": unmet or [],
        })

    def log_reflection(
        self,
        score: float,
        severity: str,
        findings: int,
        action: str,
    ) -> None:
        """Log a reflection result."""
        self._write_event("reflection", {
            "quality_score": score,
            "severity": severity,
            "findings_count": findings,
            "action": action,
        })

    def close(self) -> None:
        """Close the transcript file."""
        self._write_event("session_end", {
            "total_elapsed_s": round(time.monotonic() - self._start_time, 1),
        })
        try:
            self._file.close()
        except Exception:
            pass
        logger.info("Transcript closed: %s", self._path)

    def __enter__(self) -> "NodeTranscript":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
