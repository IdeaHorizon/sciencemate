"""Curator audit 工具：统一 curator_audit(action='log|revert')。

实现层在 `core/curator_audit.py`；这里只做工具注册 + dispatch。
"""
from __future__ import annotations

from typing import Any

from core.curator_audit import (
    get_curator_run, list_curator_runs, revert_curator_run,
)
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


_AUDIT_ACTIONS = ("log", "revert")


async def _curator_audit(
    *,
    state: State,
    action: str,
    run_id: str | None = None,
    reasoning: str = "",
    limit: int = 20,
    **_: Any,
) -> dict:
    """curator 审计统一入口。`action` 决定走哪种操作。

    Args:
      action:
        - "log"     —— 查 curator 审计日志。
                       `run_id=None` 时返最近 `limit` 条 run；指定时返单 run 详情。
        - "revert"  —— 撤销一次 curator run 的 AUTO 写入。
                       必填 `run_id` + `reasoning`（非空，说清撤销原因）。

    action 的合法值与 reasoning 非空都由 parameters_schema 声明、派发口核一次
    （core.tool_registry._schema_value_violations），这里不再手写。
    """
    if action == "log":
        if run_id:
            rec = get_curator_run(state, run_id)
            if rec is None:
                return {"status": "error",
                        "error": f"curator_run {run_id!r} not found"}
            return {"status": "success", "run": rec}
        runs = list_curator_runs(state, limit=limit)
        return {"status": "success", "count": len(runs), "runs": runs}

    if action == "revert":
        if not run_id:
            return {"status": "error",
                    "error": "action='revert' 需要 run_id"}
        summary = revert_curator_run(state, run_id, reasoning=reasoning)
        return {"status": "success", "revert_summary": summary}


register_tool(
    ToolDefinition(
        name="curator_audit",
        description=(
            "**curator 审计统一入口**。`action` 决定操作：\n\n"
            "  - `log`：查审计日志。`run_id=None` 时列最近 N 次 run；指定时返单 run 详情。\n"
            "    用途：调试 curator 行为 / revert 前确认 / 评估 dreaming 健康度。\n"
            "  - `revert`：撤销一次 curator run 的 AUTO 写入（高风险）。\n"
            "    必填 `run_id` + `reasoning`（非空，说清撤销原因）。撤销范围：该 run 创建的 entity 标 reverted；\n"
            "    derived 字段清空；typed edges 删除；pending proposals 标 reverted。\n"
            "    **无法**撤销 lifecycle 变更（review_history append-only）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": list(_AUDIT_ACTIONS),
                    "description": "log / revert",
                },
                "run_id": {
                    "type": "string",
                    "description": "log 模式可选（指定看详情）；revert 模式必填",
                },
                "reasoning": {
                    "type": "string", "minLength": 1,
                    "description": "revert 模式必填：非空，说清撤销原因（进 audit trail）",
                },
                "limit": {
                    "type": "integer", "default": 20, "minimum": 1, "maximum": 200,
                    "description": "log 模式无 run_id 时取最近 N 条",
                },
            },
            "required": ["action"],
        },
        risk_level="medium",        # log low / revert high → 折中
    ),
    _curator_audit,
)
