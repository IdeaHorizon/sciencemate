"""Generic node → orchestrator blocker reporting.

The framework does not guess which machine, package, dataset, or upstream node
will solve a problem.  The agent that observed the failure reports evidence and
the coordinator decides the next action with normal ReAct reasoning.
"""

from __future__ import annotations

from typing import Any

from core.blockers import CATEGORIES as _CATEGORIES
from core.blockers import record_blocker
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


async def _report_blocker(
    state: State,
    summary: str,
    category: str = "other",
    evidence_paths: list[str] | None = None,
    requested_action: str = "",
    suggested_owner: str = "",
    retryable_after_change: bool = True,
    **_: Any,
) -> dict:
    summary = str(summary or "").strip()
    category = str(category or "other").strip()
    # 形状与落盘归 core/blockers —— 消费方（executor 的终态、dispatch_gate、
    # run_history）都在 core，框架自己也要能记一条（病态重复熔断 / data 空手）。
    # summary 非空与 category 枚举由 parameters_schema 声明、派发口核一次。
    blocker = record_blocker(
        state,
        summary=summary,
        category=category,
        evidence_paths=evidence_paths,
        requested_action=requested_action,
        suggested_owner=suggested_owner,
        retryable_after_change=retryable_after_change,
    )
    return {
        "status": "success",
        "blocker": blocker,
        "next_step": (
            "Preserve useful partial work in your owned Project directory and end this run. "
            "The orchestrator will receive this structured blocker and decide how to resolve it."
        ),
    }


register_tool(
    ToolDefinition(
        name="report_blocker",
        description=(
            "Report a problem you cannot solve in this node's current authority or environment. "
            "Use concrete error evidence; do not guess a fixed remedy. The report is returned to "
            "the orchestrator, which may dispatch another node, change resources, wait for an "
            "external job, or ask a human. Save useful partial work first, then end the run."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "category": {"type": "string", "enum": sorted(_CATEGORIES)},
                "evidence_paths": {"type": "array", "items": {"type": "string"}},
                "requested_action": {"type": "string"},
                "suggested_owner": {"type": "string"},
                "retryable_after_change": {"type": "boolean", "default": True},
            },
            "required": ["summary"],
        },
    ),
    _report_blocker,
)
