"""audit_computational_workflow — 计算流水线科学合理性预审。"""
from __future__ import annotations

from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .artifact_staging import commit_staged_artifact, stage_draft_content
from .workflow_science import format_science_report, validate_workflow_science


async def _audit_computational_workflow(
    state: State,
    content: str | None = None,
    plan_name: str = "Research_Plan",
    auto_save: bool = True,
    **_: Any,
) -> dict:
    body = (content or "").strip()
    if not body:
        rec = None
        for art in reversed(state.list_artifacts("research_plan")):
            rec = state.read_artifact(art["id"])
            if rec and (rec.get("content") or "").strip():
                break
        body = (rec or {}).get("content") or ""

    if not body.strip():
        return {
            "status": "error",
            "error": "缺少 content；请先写 research_plan 草稿或传入 markdown 正文。",
        }

    # #157：stats 让 agent/人工看得见"框架到底审了哪几张表"——否则错误数不降时
    # 只能猜是不是自己改的地方不对（实测 agent 反复重写合格的表，19→18→18 空转
    # 31 轮）。也便于在同一 error 集合连续出现时安全停手。
    stats: dict[str, Any] = {}
    report = validate_workflow_science(body, stats=stats)
    issues = [
        {
            "severity": i.severity,
            "step_id": i.step_id,
            "rule_id": i.rule_id,
            "message": i.message,
            "suggestion": i.suggestion,
        }
        for i in report.issues
    ]
    out: dict[str, Any] = {
        "status": "success",
        "passed": report.ok,
        "n_errors": len(report.errors),
        "n_warnings": len(report.warnings),
        "issues": issues,
        "summary": format_science_report(report),
        # 表级诊断：审了几张 workflow 表、忽略了几张非 workflow 表（资源/预算/
        # 进度表）、实际审了几行、有没有跨表冲突的 Step ID
        "workflow_tables_detected": stats.get("workflow_tables_detected", 0),
        "non_workflow_tables_ignored": stats.get("non_workflow_tables_ignored", 0),
        "task_rows_audited": stats.get("task_rows_audited", 0),
        "duplicate_step_ids": stats.get("duplicate_step_ids", []),
    }

    # #160：0-row 不得假通过并 auto_save（stats 已暴露；此处再挡一层）
    zero_rows = int(out["task_rows_audited"] or 0) == 0
    if zero_rows and report.ok:
        out["passed"] = False
        out["n_errors"] = max(int(out["n_errors"] or 0), 1)
        out["issues"] = list(issues) + [{
            "severity": "error",
            "step_id": "-",
            "rule_id": "no_workflow_task_rows",
            "message": "未解析到可审计的 workflow 任务行；拒绝假通过。",
            "suggestion": "补含可追溯列的 workflow 任务表后再 audit。",
        }]
        out["summary"] = (
            "未通过（no_workflow_task_rows）：0 行可审计任务，不能视为 audit 通过。"
        )

    if out["passed"]:
        staged = stage_draft_content(state, "research_plan", body)
        out["staged_file"] = staged
        state.hook_state["last_audited_research_plan"] = body
        state.hook_state["last_audited_plan_name"] = plan_name
        out["message"] = f"✅ 一致性检查通过。正文已写入 {staged}。"
        if auto_save:
            saved = commit_staged_artifact(
                state, "research_plan", plan_name, body,
                metadata={"saved_via": "audit_computational_workflow"},
            )
            out["artifact_id"] = saved.get("id")
            out["auto_saved"] = True
            out["message"] += (
                f" 已自动 save research_plan（id={saved.get('id')}）。"
                " **勿再用 content_b64 重发正文**；继续 save hypothesis_research_overview。"
            )
        else:
            out["auto_saved"] = False
            out["message"] += (
                f" 请 save_artifact(artifact_type='research_plan', name={plan_name!r}, "
                f"content_from_file={staged!r}) —— **禁止 inline 大正文**。"
            )
    else:
        out["auto_saved"] = False
        if zero_rows:
            out["message"] = (
                "⚠️ 未解析到可审计 workflow 任务行（task_rows_audited=0），"
                "不能假通过；请补任务表后再 audit/save。"
            )
        else:
            out["message"] = (
                f"⚠️ {len(report.errors)} 项 error 需补全依据/bridge 后再 audit/save。"
            )

    return out


register_tool(
    ToolDefinition(
        name="audit_computational_workflow",
        description=(
            "预审 research_plan 中 **computational_workflow** 的通用一致性（非学科硬编码）。\n\n"
            "**Use when**：写 task 表后、save_artifact(artifact_type='research_plan') 之前。\n\n"
            "**检查项**（规则化，跨学科）：\n"
            "  - 每步是否可追溯（关键参数/产出/falsifier 至少一项有实质内容）\n"
            "  - 非默认模型尺度是否写了选择依据\n"
            "  - 跨步模型尺度变更是否有 bridge 步骤或文字说明\n"
            "  - mermaid 标签与任务表模型表述是否一致\n\n"
            "**返回**：passed=false 时按 issues 补全依据或 bridge 步骤后再 audit。\n"
            "passed=true 且传入 content 时：**自动 stage + save research_plan**（避免 turn 截断）。\n"
            "**注意**：task_rows_audited=0（只有资源表/无 workflow 表）→ passed=false，不会假通过。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "research_plan markdown；留空则读当前 run 最新 research_plan",
                },
                "plan_name": {
                    "type": "string",
                    "description": "research_plan artifact 名（默认 Research_Plan）",
                },
                "auto_save": {
                    "type": "boolean",
                    "default": True,
                    "description": "audit 通过后是否自动 save（默认 true，避免大正文二次输出截断）",
                },
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _audit_computational_workflow,
)
