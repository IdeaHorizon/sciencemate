"""PROFILE.md + PROJECT.md 操作工具（curated 稳定指令层）。

设计原则：
  - **user 显式确认才能写**。agent 不能直接 update_profile / update_project；
    必须走 propose_profile_update → user 经 resolve_proposal accept → 落地
  - 工具暴露：read（任何 agent）+ propose（任何 agent）+ resolve（orchestrator 经
    人审）
  - 直接 update 工具留给框架内部 / curator accept_proposal 时用

工具清单：
  read_profile               读 PROFILE.md（user 全局）+ PROJECT.md（项目级）
  propose_profile_update     提议改 PROFILE.md / PROJECT.md，进 inbox 等 user 确认
  _update_profile_md         （内部）真正写文件 —— 由 resolve_proposal accept 路径触发
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.directives_loader import (
    read_profile_md, read_project_md,
    write_profile_md, write_project_md,
    append_to_section,
)
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# read_profile —— 给 agent / user 读
# ─────────────────────────────────────────────────────────────────────────────

async def _read_profile(state: State,
                         scope: str = "both",
                         **_: Any) -> dict:
    """读 PROFILE.md (user 全局) + PROJECT.md (项目级)。

    scope: 'user' / 'project' / 'both'
    """
    out: dict[str, Any] = {"status": "success"}
    if scope in ("user", "both"):
        out["profile_md"] = read_profile_md()
    if scope in ("project", "both"):
        proot = state.project_root
        out["project_md"] = read_project_md(proot) if proot else None
        out["project_root"] = str(proot) if proot else None
    return out


register_tool(
    ToolDefinition(
        name="read_profile",
        # 纯读：结果可用同样参数重调取回。重复调用紧凑化与压缩器都扫这个
        # 声明（core/tool_call_cache.cacheable_tools），不再各写一份名单。
        replayable_read=True,
        description=(
            "读用户档案（PROFILE.md，跨项目）+ 项目档案（PROJECT.md）。"
            "scope: 'user' / 'project' / 'both'（默认 both）。"
            "这两份是 curated 稳定指令层：每次 LLM call 都注入 system_prompt。"
            "比 memory directive 重，比 KB 轻 —— user 显式确认过的稳定偏好/约定。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["user", "project", "both"],
                            "default": "both"},
            },
        },
        risk_level="low",
    ),
    _read_profile,
)


# ─────────────────────────────────────────────────────────────────────────────
# propose_profile_update —— agent 提议改，等人审
# ─────────────────────────────────────────────────────────────────────────────

async def _propose_profile_update(
    state: State,
    scope: str,
    section: str,
    new_content: str,
    reasoning: str,
    operation: str = "append",
    target_node: str | None = None,
    **_: Any,
) -> dict:
    """提议改 PROFILE.md / PROJECT.md 的某段。

    scope: 'user' (PROFILE.md) / 'project' (PROJECT.md)
    section: 例 "## 交互偏好" / "## 项目约束" / "## 节点级指令"
    new_content: 要 append 的一行 / 整段 content
    operation:
      'append' - append 到 section（默认）
      'replace_section' - 替换整个 section（小心用）
      'replace_all' - 替换整个文件（极少用，需 user 极慎重）
    target_node: 仅 PROJECT.md "## 节点级指令" 段下添加 `### <node>` 子段时用
    """
    # scope / operation 枚举、new_content / reasoning 非空由 parameters_schema
    # 声明，派发口核一次，这里不再手写。
    if scope == "project" and not state.project_root:
        return {"status": "error",
                "error": "scope=project 需要 state.project_root（这次 run 没传 project_id）"}

    # 写到 proposals.jsonl（统一 inbox）。复用 proposals.py 的 path 逻辑。
    from shared.tools.library.proposals import _append_jsonl, _kb_proposals_path

    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    record = {
        "id": proposal_id,
        "at": _now(),
        "proposed_by_run_id": state.run_id,
        "proposed_by_node_type": state.node_type,
        "proposal_type": (
            "profile_update" if scope == "user" else "project_update"
        ),
        "target_entity": "PROFILE.md" if scope == "user" else "PROJECT.md",
        "target_id": section,
        "proposed_action": f"{operation} → {section}",
        "reasoning": reasoning,
        "extra": {
            "scope": scope,
            "section": section,
            "new_content": new_content,
            "operation": operation,
            "target_node": target_node,
        },
        "status": "pending",
    }
    # PROFILE 走 user 层 proposal 池 —— 但我们当前 proposals.py 只有 KB(项目) +
    # Skill(org)。为简化：profile/project 提议都写到项目层 kb_proposals.jsonl
    # （以"项目内的待审议建议"看待；profile_update 后 accept 时落 PROFILE.md）
    _append_jsonl(_kb_proposals_path(state), record)
    return {"status": "success", "proposal_id": proposal_id, "scope": scope}


register_tool(
    ToolDefinition(
        name="propose_profile_update",
        description=(
            "提议改 PROFILE.md（user 全局）或 PROJECT.md（项目级）。"
            "**不直接落** —— 写到 inbox 等 user 经 resolve_proposal accept。"
            "用法：agent 在 chat 中检测到 user 说出**持久性约束**（'以后'/'始终'/"
            "'每次'/'我偏好'）→ 调这个工具记录提议。"
            "scope='user' → PROFILE.md；scope='project' → PROJECT.md。"
            "section 例：'## 交互偏好' / '## 项目约束' / '## 节点级指令'。"
            "operation 默认 'append'（append 一行到 section）。"
            "reasoning 非空 —— 说清为什么，让 user 知道。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["user", "project"]},
                "section": {"type": "string",
                              "description": "目标 markdown section header，例 '## 交互偏好'"},
                "new_content": {"type": "string", "minLength": 1},
                "reasoning": {"type": "string", "minLength": 1,
                              "description": "非空，说清为什么要改（让 user 知道）"},
                "operation": {"type": "string",
                                "enum": ["append", "replace_section", "replace_all"],
                                "default": "append"},
                "target_node": {"type": "string",
                                  "description": "可选；PROJECT.md '## 节点级指令' 段下加 '### <node>' 子段时用"},
            },
            "required": ["scope", "section", "new_content", "reasoning"],
        },
        risk_level="low",
    ),
    _propose_profile_update,
)


# ─────────────────────────────────────────────────────────────────────────────
# 内部 helper：resolve_proposal accept 时调，真正写文件
# ─────────────────────────────────────────────────────────────────────────────

def apply_profile_update(state: State, proposal: dict) -> dict:
    """proposals.py resolve_proposal 路由到这里时调。

    返回 {wrote_path: str}。失败抛异常。
    """
    extra = proposal.get("extra") or {}
    scope = extra.get("scope")
    section = extra.get("section", "")
    new_content = extra.get("new_content", "")
    operation = extra.get("operation", "append")

    if scope == "user":
        current = read_profile_md() or ""
        write_fn = lambda content: write_profile_md(content)
    elif scope == "project":
        if not state.project_root:
            raise ValueError("project scope 需要 project_root")
        current = read_project_md(state.project_root) or ""
        write_fn = lambda content: write_project_md(state.project_root, content)
    else:
        raise ValueError(f"unknown scope: {scope!r}")

    # 操作
    if operation == "append":
        new_text = append_to_section(current, section, new_content)
    elif operation == "replace_section":
        # 简化实现：找到 section header → 替换至下个 ## 之前
        import re
        pattern = re.compile(rf"({re.escape(section)})\s*\n(.*?)(?=^##\s+|\Z)",
                              re.DOTALL | re.MULTILINE)
        if pattern.search(current):
            new_text = pattern.sub(f"{section}\n\n{new_content}\n\n", current)
        else:
            new_text = current.rstrip() + f"\n\n{section}\n\n{new_content}\n"
    elif operation == "replace_all":
        new_text = new_content
    else:
        raise ValueError(f"unknown operation: {operation!r}")

    written = write_fn(new_text)
    return {"wrote_path": str(written)}
