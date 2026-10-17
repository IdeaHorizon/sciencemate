"""Action-local selected-input checks; no route, stage or command inference.

The existing delivery ledger owns input verification.  This module only asks it
about the packages an action explicitly consumes.  An absent ledger entry is
not a formal verification receipt: ordinary parent packages receive a shared
structural inspection with explicit limits, without creating a Data request.
"""

from __future__ import annotations

import copy
import time
from typing import Any

try:
    from .action_boundary import v3_ready
    from .contract_audit import audit_input_delivery_for_execution
except ImportError:  # pragma: no cover - standalone node loading
    from tools.action_boundary import v3_ready
    from tools.contract_audit import audit_input_delivery_for_execution


def check_consumed_input(
    state: Any,
    *,
    runner: str,
    input_package_artifact_id: str | None = None,
    input_package_bindings: dict[str, str] | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a zero-execution error only for this action's selected inputs.

    No selector means no formal-input consumption declared by this call; it is
    not permission inferred from scope, stage, command text or unrelated specs.
    Empty but explicitly supplied selectors are left to the shared validator.
    """
    if observation is not None:
        observation.update(status="not_selected", selection={}, source="tool_entry_preflight")
    if not v3_ready(state):
        return None
    if input_package_artifact_id is None and input_package_bindings is None:
        return None
    if observation is not None:
        observation["selection"] = copy.deepcopy(
            {
                "input_package_artifact_id": input_package_artifact_id,
                "input_package_bindings": input_package_bindings,
            }
        )
    try:
        audit = audit_input_delivery_for_execution(
            state,
            input_package_artifact_id,
            input_package_bindings,
            consumed_only=True,
        )
        verification_status = (
            "structure_checked"
            if audit.get("passed") and audit.get("inspection_kind") == "ordinary_dataset_structure"
            else "verified"
            if audit.get("passed") and audit.get("applicable")
            else "not_performed"
            if not audit.get("applicable")
            else "not_verified"
        )
        state.append_transcript(
            "input_delivery_preflight",
            runner=runner,
            verification_status=verification_status,
            consumed_only=True,
            **audit,
        )
    except Exception as exc:
        # No executor has been called.  An unreadable verification source is
        # not permission to execute; retain the same corrective/terminal exit.
        audit = {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["input_delivery_audit_unavailable"],
            "exception_type": type(exc).__name__,
        }
        verification_status = "not_verified"
    if observation is not None:
        observation.update(
            status=verification_status, audit=copy.deepcopy(audit), observed_at=time.time()
        )
    if audit.get("passed"):
        return None
    return {
        "status": "error",
        "error_code": "formal_input_not_ready",
        "error": (
            "本动作声明消费的输入未通过核验，命令未启动、作业未提交。"
            "请按审计明细修复本次输入绑定或完成该输入的交付核验后重试；"
            "普通已有数据包只需修复实际包结构，不要求另建 Data 请求。"
            "不要靠移除实际使用的输入声明绕过核验。"
            "不消费这些输入的准备、诊断、取消和诚实 finish 仍可继续。"
        ),
        "runner": runner,
        "payload_spawned": False,
        "job_submitted": False,
        "audit": audit,
        "retryable_after_change": True,
        "next_action": {
            "owner": "experiment",
            "action": "repair_selected_input_or_report_blocker",
            "model_callable": True,
        },
    }
