"""Structured logging framework for the research platform.

Provides contextual logging for:
- LLM calls (model, tokens, latency, cost)
- Tool calls (tool name, duration, success/failure)
- Node/entity state transitions
- API requests (method, path, status, latency)

Two usage patterns:
1. Pass a logger: log_llm_call(my_logger, model=..., ...)
2. Module-level: log_llm_call(model=..., ...)  — uses module logger
"""

import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

# Context variables for request-scoped correlation
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
project_id_var: ContextVar[str | None] = ContextVar("project_id", default=None)
node_id_var: ContextVar[str | None] = ContextVar("node_id", default=None)

_module_logger = logging.getLogger("app.core.logging")


class StructuredFormatter(logging.Formatter):
    """JSON-like structured log formatter with context injection."""

    def format(self, record: logging.LogRecord) -> str:
        # Build structured fields
        fields = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Inject context vars
        request_id = request_id_var.get()
        if request_id:
            fields["request_id"] = request_id
        project_id = project_id_var.get()
        if project_id:
            fields["project_id"] = project_id
        node_id = node_id_var.get()
        if node_id:
            fields["node_id"] = node_id

        # Include any extra fields
        if hasattr(record, "extra_fields"):
            fields.update(record.extra_fields)

        # Format as key=value pairs (structured but human-readable)
        parts = [f"{k}={v}" for k, v in fields.items()]
        line = " | ".join(parts)

        # logging.Formatter.format() 附加 exc_info/stack_info；这个 override 以前
        # 整个丢掉了 —— 于是全后端每一处 logger.exception() 都只留一行标题、
        # 没有任何 traceback（E2E v7 挂了三小时，日志只有 "Local execution
        # failed" 五个字）。异常通道不是可选装饰，格式化器无权吞掉它。
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line = f"{line}\n{record.exc_text}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


def setup_logging(debug: bool = False) -> None:
    """Configure application-wide logging."""
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

    # Quiet noisy libraries
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── Domain-specific log helpers ───────────────────────────────────────────


def _emit(lgr: logging.Logger, level: int, message: str, extra: dict[str, Any]) -> None:
    record = lgr.makeRecord(lgr.name, level, "", 0, message, (), None)
    record.extra_fields = extra  # type: ignore[attr-defined]
    lgr.handle(record)


def log_llm_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    purpose: str = "",
    *,
    latency_ms: float | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Log an LLM call with structured fields."""
    extra: dict[str, Any] = {
        "event": "llm_call",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost_usd, 6),
        "success": success,
    }
    if purpose:
        extra["purpose"] = purpose
    if latency_ms is not None:
        extra["latency_ms"] = round(latency_ms, 1)
    if error:
        extra["error"] = error

    level = logging.INFO if success else logging.ERROR
    _emit(_module_logger, level, f"LLM call: {model} ({purpose})", extra)


def log_tool_call(
    tool_name: str,
    tool_input: Any = None,
    result: Any = None,
    *,
    success: bool = True,
    duration_ms: float | None = None,
    node_type: str | None = None,
    error: str | None = None,
) -> None:
    """Log a tool invocation."""
    extra: dict[str, Any] = {
        "event": "tool_call",
        "tool": tool_name,
        "success": success,
    }
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 1)
    if node_type:
        extra["node_type"] = node_type
    if error:
        extra["error"] = error

    level = logging.INFO if success else logging.ERROR
    _emit(_module_logger, level, f"Tool call: {tool_name}", extra)


def log_state_transition(
    *,
    entity_type: str,
    entity_id: str,
    from_state: str,
    to_state: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a state machine transition."""
    fields: dict[str, Any] = {
        "event": "state_transition",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "from": from_state,
        "to": to_state,
    }
    if extra:
        fields.update(extra)
    _emit(
        _module_logger,
        logging.INFO,
        f"{entity_type} {entity_id}: {from_state} → {to_state}",
        fields,
    )


class Timer:
    """Context manager for timing operations."""

    def __init__(self) -> None:
        self.start: float = 0
        self.elapsed_ms: float = 0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000
