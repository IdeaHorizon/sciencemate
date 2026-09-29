"""Standardized three-artifact completion for non-scientific Experiment operations."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

log = logging.getLogger(__name__)

try:
    from shared.tools.library.artifacts_extra import (
        _freeze_artifact,
        register_save_gate,
    )
except ImportError:
    from tools.artifacts_extra import _freeze_artifact, register_save_gate

_TASK_KINDS = frozenset({"python_install", "build", "file_delivery", "external_job", "generic"})
_TASK_KIND_ALIASES = {"toolchain_build": "build"}
_TASK_KIND_INPUTS = _TASK_KINDS | frozenset(_TASK_KIND_ALIASES)
_REAL_EXECUTION_TASK_KINDS = frozenset({"build"})
_REAL_EXECUTION_OPERATION_KINDS = frozenset({"toolchain_build"})
_OPERATION_TASK_KIND_COMPATIBILITY = {
    "toolchain_build": frozenset({"build", "external_job"}),
}
_OUTCOMES = frozenset({"success", "failed", "blocked"})
_CLOSURE_OWNER = "record_operation_completion"
_CLOSURE_SCHEMA_VERSION = 1
_CHILD_OBLIGATION_SCHEMA_VERSION = 1
_EXTERNAL_JOB_SCOPE_FIELDS = (
    "scheduler",
    "job_id",
    "namespace",
    "launch_host",
    "scheduler_cluster",
    "resource_uid",
    "submission_nonce",
    "container_runtime_id",
    "process_group_id",
    "process_start_ticks",
    "route_attempt_id",
)
_ROUTE_COMPLETION_CHECK = "execution_route_complete"
_OPERATION_CLOSURE_ARTIFACT_TYPES = frozenset(
    {
        "raw_results",
        "clean_results",
        "experiment_log",
    }
)


def _current_execution_mode(state: Any) -> str:
    """Read mode through the run-contract owner; failures stay unclassified."""
    try:
        try:
            from .run_contract import load_execution_mode_view
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.run_contract import load_execution_mode_view
        return str(load_execution_mode_view(state).get("mode") or "")
    except Exception:
        return ""


def _declared_paths_without_producer_receipt(
    state: Any, declared_paths: list[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Declared products that no satisfying attempt's frozen output receipt vouches for.

    P0a v4 的共同不变量（Codex 复审 23 号）：**声明的 build 产物必须映射到满足义务
    的那次 route attempt 在收尾时冻结的产物身份收据**（`output_observations`，
    由 execution_route 在 route_step_outcome / route_step_external_execution_verified
    里写下：lexical 路径、kind、dev/ino/size、正文 sha256）。这里不再 stat mtime
    猜作者——mtime 可保留（cp -p / tar / rsync -t）、可回拨（touch -d），realpath
    会抹掉符号链接的 lexical 身份，容差会吞掉严格顺序。判定：
    · 声明路径此刻的身份（同一助手 output_identity 取）必须与某份收据里的同路径
      条目逐字段相同（kind、dev/ino/size、sha256 或大文件的 ctime、链接目标）；
    · 收据里没有这条 lexical 路径 → 未归属（窗后 ln -s / ln / mv 顶替、路线前后
      由 shell / Python / safe_write_file 写出的文件都落在这里）；
    · 收据被截断（一步匹配文件超过上限）→ 未归属，理由单列，出口是收窄声明。
    返回 (unattributed, receipts)。缺失的文件交给既有的产物存在性检查判。
    """
    try:
        from .execution_action_census import satisfying_attempt_output_receipts
        from .execution_route import output_identity, output_identity_matches
    except ImportError:  # pragma: no cover - standalone node bootstrap.
        from tools.execution_action_census import satisfying_attempt_output_receipts
        from tools.execution_route import output_identity, output_identity_matches
    receipts = satisfying_attempt_output_receipts(state)
    by_path: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for receipt in receipts:
        for row in receipt.get("output_observations") or []:
            if isinstance(row, dict) and str(row.get("path") or ""):
                by_path.setdefault(str(row["path"]), []).append((receipt, row))
    truncated = [r["attempt_id"] for r in receipts if r.get("truncated")]
    unattributed: list[dict[str, Any]] = []
    for raw in declared_paths or []:
        if not str(raw).strip():
            continue
        current = output_identity(str(raw))
        if current.get("kind") == "missing":
            # Missing products are judged by the existing evidence-path checks.
            continue
        candidates = by_path.get(current["path"]) or []
        if not candidates:
            unattributed.append({
                "path": current["path"],
                "reason": (
                    "producer_receipt_truncated" if truncated
                    else "no_producer_receipt"
                ),
                "current_identity": current,
                "truncated_attempt_ids": truncated,
            })
            continue
        reasons: list[dict[str, Any]] = []
        matched = False
        for receipt, row in candidates:
            ok, reason = output_identity_matches(row, current)
            if ok:
                matched = True
                break
            reasons.append({"attempt_id": receipt.get("attempt_id"), "reason": reason})
        if not matched:
            unattributed.append({
                "path": current["path"],
                "reason": reasons[-1]["reason"] if reasons else "identity_mismatch",
                "current_identity": current,
                "receipts": reasons,
            })
    summary = [
        {
            "attempt_id": r.get("attempt_id"),
            "route_step_id": r.get("route_step_id"),
            "source_event": r.get("source_event"),
            "observed_paths": len(r.get("output_observations") or []),
            "truncated": bool(r.get("truncated")),
        }
        for r in receipts
    ]
    return unattributed, summary


def _real_execution_obligation(
    state: Any,
    *,
    task_kind: str,
    outcome: str,
    declared_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Derive physical work from immutable scope; caller kind may only add it.

    Assignment and physical execution are orthogonal.  A typed-none run and
    a temporary pending-assignment run must meet the same physical standard
    before either can claim that a build succeeded.  The receipt-backed
    operation category is authoritative; ``task_kind`` is only a presentation
    cross-check and may strengthen, never remove, that obligation.  The
    action-census owner supplies the physical fact; caller checks and an
    existing file are not execution receipts.
    """
    if outcome != "success":
        return {"passed": True, "applicable": False}
    try:
        try:
            from .execution_action_census import operation_execution_obligation
            from .run_contract import load_run_contract
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.execution_action_census import operation_execution_obligation
            from tools.run_contract import load_run_contract
        contract = load_run_contract(state)
        execution_mode = str(contract.get("execution_mode") or "")
        operation_kind = str(contract.get("operation_kind") or "")
    except Exception as exc:
        return {
            "passed": False,
            "applicable": True,
            "status": "execution_obligation_audit_error",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    if execution_mode != "operational" or not operation_kind:
        return {
            "passed": False,
            "applicable": True,
            "status": "execution_obligation_scope_unavailable",
            "reason": "receipt-backed operational scope is unavailable",
            "execution_mode": execution_mode or None,
            "operation_kind": operation_kind or None,
        }

    compatible_task_kinds = _OPERATION_TASK_KIND_COMPATIBILITY.get(operation_kind)
    if compatible_task_kinds is not None and task_kind not in compatible_task_kinds:
        return {
            "passed": False,
            "applicable": True,
            "status": "operation_task_kind_mismatch",
            "task_kind_mismatch": {
                "immutable_operation_kind": operation_kind,
                "requested_task_kind": task_kind,
                "compatible_task_kinds": sorted(compatible_task_kinds),
            },
        }

    real_execution_required = (
        operation_kind in _REAL_EXECUTION_OPERATION_KINDS
        or task_kind in _REAL_EXECUTION_TASK_KINDS
    )
    if not real_execution_required:
        return {
            "passed": True,
            "applicable": False,
            "operation_kind": operation_kind,
        }

    try:
        obligation = operation_execution_obligation(state)
    except Exception as exc:
        return {
            "passed": False,
            "applicable": True,
            "status": "execution_obligation_audit_error",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    passed = obligation.get("real_execution_obligation_satisfied") is True
    if passed and declared_paths:
        # P0a v4：义务已满足 ≠ 声明的产物出自那次执行。产物归属按满足义务的 attempt
        # 收尾时冻结的身份收据判，与写它的工具无关、也不看 mtime。
        unattributed, receipts = _declared_paths_without_producer_receipt(
            state, declared_paths)
        if unattributed:
            return {
                "passed": False,
                "applicable": True,
                "status": "build_artifact_not_produced_by_satisfying_attempt",
                "unattributed_paths": unattributed,
                "producer_receipts": receipts,
                "operation_execution_obligation": obligation,
                "operation_kind": operation_kind,
                "task_kind": task_kind,
            }
    return {
        "passed": passed,
        "applicable": True,
        "status": (
            "real_execution_obligation_satisfied"
            if passed
            else "real_execution_obligation_missing"
        ),
        "operation_execution_obligation": obligation,
        "operation_kind": operation_kind,
        "task_kind": task_kind,
    }


def _route_correction_disclosure(state: Any) -> dict[str, Any]:
    """Read the active limitation projection from the route receipt owner."""
    try:
        try:
            from .execution_route import route_correction_witness_disclosure
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.execution_route import route_correction_witness_disclosure
        disclosure = route_correction_witness_disclosure(state)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"ok": True, **disclosure}


def _correction_disclosure_matches(
    metadata: dict[str, Any],
    *,
    limitations: list[dict[str, Any]],
    check: dict[str, Any] | None,
    check_name: str,
) -> bool:
    closure_input = metadata.get("operation_closure_input")
    if not isinstance(closure_input, dict):
        return False
    frozen_checks = closure_input.get("checks")
    if not isinstance(frozen_checks, list):
        return False
    observed_checks = [
        item for item in frozen_checks
        if isinstance(item, dict) and item.get("name") == check_name
    ]
    return bool(
        metadata.get("route_correction_witness_limitations", [])
        == limitations
        and observed_checks == ([check] if check is not None else [])
    )


def _operation_closure_artifact_save_gate(
    state: Any,
    draft: dict[str, Any],
) -> dict[str, Any]:
    """operation 三件套只能由 record_operation_completion 原子生成。"""
    if str(getattr(state, "node_type", "") or "") != "experiment":
        return {}
    artifact_type = str((draft or {}).get("type") or "")
    if artifact_type not in _OPERATION_CLOSURE_ARTIFACT_TYPES:
        return {}

    mode = _current_execution_mode(state)
    if mode == "scientific":
        return {}
    if mode == "operational":
        reason = (
            "operation 的 raw_results、clean_results、experiment_log 是同一个"
            "原子闭环，只能由 record_operation_completion 生成；"
            "通用 save_artifact 会制造 foreign closure 并永久阻断 exactly-one 审计"
        )
        hint = "不要换名或手工补三件套；收集真实输出与 checks 后调用 record_operation_completion。"
    else:
        reason = "Experiment scope 尚未分类，不能判断闭环产物归科学流程还是 operation 原子闭环"
        hint = (
            "先调用 classify_experiment_scope；scientific 按科学结果流程写入，"
            "operation 使用 record_operation_completion。"
        )
    return {
        "failures": {"closure_owner": reason},
        "hint": hint,
    }


def _external_job_reference(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in _EXTERNAL_JOB_SCOPE_FIELDS}


def _local_exit_receipt(
    record: dict[str, Any],
    health: dict[str, Any],
) -> dict[str, Any]:
    """Interpret the immutable-ID-bound native managed-job terminal receipt.

    probe_external_job_health obtains this snapshot through
    _local_container_status(job_id, container_runtime_id). That shared
    authority has already rejected a reused name, an unmanaged/non-job
    container, and a foreign sandbox namespace. Completion still rechecks
    the immutable ID and terminal fields before treating the native job
    record's exit_code as evidence; output text and the retired wrapper status
    file are never used.
    """
    job_id = str(record.get("job_id") or "").strip()
    expected_id = str(record.get("container_runtime_id") or "").strip()
    if not job_id or re.fullmatch(r"[0-9a-f]{64}", expected_id) is None:
        return {
            "verified": False,
            "reason": "local_container_identity_missing",
        }

    scheduler_result = health.get("scheduler_result")
    scheduler_result = scheduler_result if isinstance(scheduler_result, dict) else {}
    raw = scheduler_result.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    sandbox_state = raw.get("sandbox_state")
    sandbox_state = sandbox_state if isinstance(sandbox_state, dict) else {}
    if (
        scheduler_result.get("status") != "success"
        or raw.get("ok") is not True
        or not sandbox_state
    ):
        return {
            "verified": False,
            "reason": "local_container_status_unavailable",
            "error": raw.get("stderr") or scheduler_result.get("error"),
        }
    if sandbox_state.get("exists") is not True:
        return {
            "verified": False,
            "reason": "local_container_terminal_receipt_missing",
            "container_runtime_id": expected_id,
        }
    if (
        str(sandbox_state.get("id") or "") != expected_id
        or str(sandbox_state.get("name") or "") != job_id
        or sandbox_state.get("managed") is not True
        or str(sandbox_state.get("kind") or "") != "job"
    ):
        return {
            "verified": False,
            "reason": "local_container_identity_mismatch",
            "container_runtime_id": expected_id,
            "observed_container_runtime_id": sandbox_state.get("id"),
        }
    if sandbox_state.get("running") is True:
        return {
            "verified": False,
            "reason": "local_container_still_running",
            "container_runtime_id": expected_id,
        }

    container_status = str(sandbox_state.get("status") or "").casefold()
    returncode = sandbox_state.get("exit_code")
    if (
        container_status not in {"exited", "dead"}
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
    ):
        return {
            "verified": False,
            "reason": "local_container_terminal_state_unverified",
            "container_status": container_status or None,
            "returncode": returncode,
            "container_runtime_id": expected_id,
        }
    # Core native job records do not yet consistently publish cgroup OOM facts.
    # Preserve a future/available authoritative boolean, but do not turn an
    # absent or malformed field into a negative observation.
    raw_oom_killed = sandbox_state.get("oom_killed")
    oom_observable = isinstance(raw_oom_killed, bool)
    oom_killed = raw_oom_killed if oom_observable else None
    return {
        "verified": True,
        "succeeded": (container_status == "exited" and returncode == 0 and not oom_killed),
        "source": "native_job_record",
        "returncode": returncode,
        "oom_killed": oom_killed,
        "oom_observable": oom_observable,
        "container_status": container_status,
        "container_runtime_id": expected_id,
        "finished_at": sandbox_state.get("finished_at"),
    }


def _termination_declared(record: dict[str, Any]) -> bool:
    declared = record.get("expected_termination") if isinstance(record, dict) else None
    return isinstance(declared, dict) and bool(declared.get("exit_codes"))


def _with_termination_verdict(
    record: dict[str, Any], health: dict[str, Any], evidence: dict[str, Any],
) -> dict[str, Any]:
    """在调度器给出的终态证据上附上预期终止判定（第 5 步 5b）；没有声明时原样返回。

    succeeded 仍是物理事实（退出码 0）；termination_matched 说的是是否符合提交时锚定任务
    原文冻结的预期。OOM 被杀不算符合。
    """
    if not isinstance(evidence, dict) or evidence.get("verified") is not True:
        return evidence
    if not _termination_declared(record):
        return evidence
    try:
        try:
            from .resource_manager import _termination_verdict
        except ImportError:
            from tools.resource_manager import _termination_verdict
        code = evidence.get("returncode")
        verdict = _termination_verdict(
            record, health,
            exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None)
    except Exception:
        return evidence
    # expected_termination 判的是"程序按提交时冻结的预期退出码结束"。被调度器
    # 结束的作业没有属于自己的退出码，sacct 的 ExitCode 低位恒为 0，会与
    # exit_codes=[0] 撞上 —— 那不是符合预期，是没有机会给出结果。
    matched = (
        verdict.get("termination_matched") is True
        and not evidence.get("oom_killed")
        and evidence.get("scheduler_terminated") is not True
    )
    return {**evidence, "termination_matched": matched, "termination": verdict}


def _declared_completion_paths_fresh(record: dict[str, Any], health: dict[str, Any]) -> bool:
    """与 finalize 同一个新鲜度判据：completion_paths 的 mtime 必须晚于 submitted_at。

    原先这里只看 exists，提交前就躺在那儿的旧文件也能算完成证据。
    """
    try:
        try:
            from .resource_manager import _operation_completion_paths_fresh
        except ImportError:
            from tools.resource_manager import _operation_completion_paths_fresh
        fresh, _reason = _operation_completion_paths_fresh(record, health)
    except Exception:
        return False
    return bool(fresh)


def _external_job_success_evidence(
    record: dict[str, Any],
    health: dict[str, Any],
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    """区分 scheduler 终态与有客观证据的成功终态。"""
    lifecycle_status = str(lifecycle.get("status") or "")
    if lifecycle.get("resolution") == "legacy_lifecycle_scope_ambiguous":
        return {"verified": False, "reason": "lifecycle_scope_ambiguous"}
    if lifecycle_status in {"cancelled", "superseded"}:
        return {
            "verified": False,
            "reason": f"lifecycle_{lifecycle_status}",
            "lifecycle_artifact_id": lifecycle.get("lifecycle_artifact_id"),
        }
    if health.get("health_state") == "failure_signal" or health.get("error_evidence"):
        return {
            "verified": False,
            "reason": "health_failure_signal",
            "error_evidence": health.get("error_evidence") or [],
        }

    scheduler = str(record.get("scheduler") or "").casefold()
    if scheduler == "local":
        return _with_termination_verdict(record, health, _local_exit_receipt(record, health))

    scheduler_result = health.get("scheduler_result") or {}
    raw = scheduler_result.get("raw") if isinstance(scheduler_result, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    stdout = str(raw.get("stdout") or "")
    if scheduler == "kubernetes":
        try:
            status = json.loads(stdout).get("status") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            status = {}
        if status.get("succeeded") and not status.get("failed"):
            return {
                "verified": True,
                "succeeded": True,
                "source": "kubernetes_job_status",
                "succeeded_pods": status.get("succeeded"),
            }
        if status.get("failed"):
            return {
                "verified": True,
                "succeeded": False,
                "source": "kubernetes_job_status",
                "failed_pods": status.get("failed"),
            }
    elif scheduler == "pbs":
        exit_match = re.search(r"(?im)^\s*exit_status\s*=\s*(-?\d+)\s*$", stdout)
        if exit_match:
            return _with_termination_verdict(record, health, {
                "verified": True,
                "succeeded": int(exit_match.group(1)) == 0,
                "source": "pbs_exit_status",
                "returncode": int(exit_match.group(1)),
            })
    elif scheduler == "slurm":
        terminal = health.get("terminal_evidence")
        if (
            isinstance(terminal, dict)
            and terminal.get("source") == "slurm_accounting"
            and terminal.get("terminal") is True
            and isinstance(terminal.get("returncode"), int)
            and not isinstance(terminal.get("returncode"), bool)
        ):
            return _with_termination_verdict(record, health, {
                "verified": True,
                # Allocation exit status remains the physical success fact even
                # when a step OOM is diagnostic evidence.
                "succeeded": terminal.get("succeeded") is True,
                # 调度器把它结束的（超时/取消/抢占/节点故障/allocation 级 OOM）。
                # 预期终止判定据此一律不成立。
                "scheduler_terminated": terminal.get("scheduler_terminated") is True,
                "source": "slurm_accounting",
                "returncode": terminal.get("returncode"),
                "allocation_state": terminal.get("allocation_state"),
                "allocation_states": list(terminal.get("allocation_states") or []),
                "allocation_rows": list(terminal.get("allocation_rows") or []),
                "oom_killed": terminal.get("oom_killed") is True,
                "oom_steps": list(terminal.get("oom_steps") or []),
                "max_rss": terminal.get("max_rss"),
                "max_rss_bytes": terminal.get("max_rss_bytes"),
                "max_rss_source": terminal.get("max_rss_source"),
            })

    # 调度器已经说了"这个作业是我结束的"：不能再落到产物兜底。被墙钟杀掉的作业
    # 照样可能留下部分产物，那条兜底会把它读成成功。上面的 slurm 分支要求
    # returncode 可解析，ExitCode 为空/畸形时会跳过它 —— 这里补住那道缝。
    terminal_evidence = health.get("terminal_evidence")
    if (isinstance(terminal_evidence, dict)
            and terminal_evidence.get("scheduler_terminated") is True):
        return {
            "verified": True,
            "succeeded": False,
            "scheduler_terminated": True,
            "source": str(terminal_evidence.get("source") or "scheduler_accounting"),
            "returncode": terminal_evidence.get("returncode"),
            "allocation_states": list(terminal_evidence.get("allocation_states") or []),
            "reason": "scheduler_terminated",
        }
    completion_paths = health.get("completion_paths") or []
    if completion_paths and all(
        isinstance(item, dict) and item.get("exists") is True and not item.get("blocked")
        for item in completion_paths
    ) and _declared_completion_paths_fresh(record, health) and not _termination_declared(record):
        return {
            "verified": True,
            "succeeded": True,
            "source": "declared_completion_paths",
            "completion_paths": completion_paths,
        }
    return {"verified": False, "reason": "scheduler_success_unverified"}


def _own_run_managed_submissions(
    state: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """本 run 自己产生的受管提交（closure refs 的权威来源）。

    返回 (records, excluded, errors)：
    - records：信封 ``produced_by_run_id == state.run_id``（v2 节点目录跨 run
      持久，payload 层不带 run 身份 —— 先例：finalize 侧取消账本同字段校验）
      且 lifecycle 仍活跃的提交；
    - excluded：**只**排 cancelled/superseded 的本 run 提交，带 lifecycle_status
      供披露 —— 避免 cancel→重提→success 被 terminal/success 门误降级；
      已 finalize 的提交**保留进 refs**：它是本 run 真正做过并已收尾的作业，
      收据就是它的终态事实。原来把 finalized 一并排除，于是「先收尾再闭环」
      拿到空 refs，成功闭环被机械降级成 partial —— 作业层解耦后这会成为默认
      顺序，因此这一项与解耦必须同 PR；
    - errors：读取/解析异常。**不吞**：枚举继续，异常记 witness 由调用方落账
      （账本损坏是记录完整性事实，不是拒绝冻结的理由 —— BF-12 死路墙判据）。
      为此不走 rm._submission_payloads（它对损坏 payload 静默丢弃，artifact_id
      到不了这里），按规格 §1a 直接逐信封枚举；payload 过滤与 rm:602-603 一致。
      信封文件本身不可解析的（list_artifacts 层跳过）无 artifact_id 可记，
      规格接受该层不可归因。
    """
    try:
        from .resource_manager import (
            _CANCELLED_JOB_STATES,
            _SUBMISSION_RECORD_TYPES,
            lifecycle_for_submission,
            planned_stop_cancellation,
        )
    except ImportError:
        from tools.resource_manager import (
            _CANCELLED_JOB_STATES,
            _SUBMISSION_RECORD_TYPES,
            lifecycle_for_submission,
            planned_stop_cancellation,
        )
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    reopened_attempts = _reopened_route_attempt_ids(state)
    for artifact_type in _SUBMISSION_RECORD_TYPES:
        try:
            rows = state.list_artifacts(artifact_type, own_only=True) or []
        except Exception as exc:
            errors.append({"artifact_type": artifact_type,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        for row in rows:
            # list_artifacts 只给瘦行（id/type/name）；信封按 id 读全件（§1a）。
            artifact_id = str(row.get("id") or "")
            try:
                envelope = state.read_artifact(artifact_id)
            except Exception as exc:
                errors.append({"artifact_id": artifact_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                continue
            if envelope is None:
                # read_artifact 对不可读/不可解析信封吞异常返 None——列举刚见过
                # 这个 id，读不回来本身就是账本完整性事实，可归因地记下。
                errors.append({"artifact_id": artifact_id,
                               "error": "ArtifactUnreadable: read_artifact 返回 "
                                        "None（文件缺失或信封不可解析）"})
                continue
            if str(envelope.get("produced_by_run_id") or "") != str(state.run_id):
                continue
            try:
                payload = json.loads(str(envelope.get("content") or "{}"))
            except (TypeError, ValueError) as exc:
                errors.append({"artifact_id": artifact_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                continue
            if not isinstance(payload, dict):
                errors.append({"artifact_id": artifact_id,
                               "error": "PayloadShapeError: content 不是 JSON object"})
                continue
            if not (payload.get("status") == "success" and not payload.get("dry_run")
                    and payload.get("scheduler") and payload.get("job_id")):
                continue
            payload["artifact_id"] = artifact_id
            payload["submission_record_type"] = artifact_type
            try:
                lifecycle = lifecycle_for_submission(state, payload)
            except Exception as exc:
                errors.append({"artifact_id": artifact_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                continue
            status = lifecycle.get("status") if isinstance(lifecycle, dict) else None
            # 按计划停止的作业（D07）lifecycle 也是 cancelled，但它是任务要求的结局：留在
            # refs 里走作业核验，收尾才会强制附 external_job_stopped_as_planned。排掉的话
            # 不传 refs 的收尾会绕过这条披露（第三会话复审 D07 P2）。
            if status == "cancelled" and planned_stop_cancellation(state, payload) is not None:
                records.append(payload)
                continue
            # 只排作废的（cancelled/superseded）。已 finalize 的提交是本 run 做过
            # 且已收尾的作业，必须留在 refs 里 —— 用补集写法会把它一并排掉。
            if status is not None and status in _CANCELLED_JOB_STATES:
                excluded.append({
                    "reference": _external_job_reference(payload),
                    "lifecycle_status": status,
                    "resolution": (lifecycle or {}).get("resolution"),
                })
                continue
            # 051：已 finalize 的失败提交，若它的路线步骤之后被带 recovery_basis 的
            # 修订重开，那次尝试已被重开取代——留在 success 的判据里会让"重试做成了"
            # 只能记 partial（09-22 活体探针 p5：ROC 自动 refs 含第一次失败作业，
            # 调用方不能缩小，cancel_job 又改不了已 finalize 的作业）。排除但披露，
            # 与 cancelled/superseded 同一张表；route-complete 门仍要求重开的步骤
            # 最终 verified，所以这不是放过失败，只是不让被取代的失败顶掉成功。
            attempt_id = str(payload.get("route_attempt_id") or "").strip()
            if status == "finalized" and attempt_id and attempt_id in reopened_attempts:
                excluded.append({
                    "reference": _external_job_reference(payload),
                    "lifecycle_status": "superseded_by_route_reopen",
                    "resolution": (
                        f"route step reopened by amendment after attempt {attempt_id} "
                        "was finalized; a later attempt owns the step's outcome"
                    ),
                    "route_attempt_id": attempt_id,
                })
                continue
            records.append(payload)
    return records, excluded, errors


def _reopened_route_attempt_ids(state: Any) -> set[str]:
    """带 recovery_basis 的路线修订重开过的 attempt（declared_route_recovery_basis 事件）。"""
    try:
        try:
            from .execution_route import _read_transcript_events
        except ImportError:
            from tools.execution_route import _read_transcript_events
        events, _warnings = _read_transcript_events(state)
    except Exception:
        return set()
    return {
        str(event.get("attempt_id") or "").strip()
        for event in events
        if isinstance(event, dict)
        and str(event.get("event") or "") == "declared_route_recovery_basis"
        and str(event.get("attempt_id") or "").strip()
    }


#: 收养行与冻结 closure 里的作业引用逐字段比对的身份字段（unresolved_external_workflows
#: 的行里恰好带着这些）。
_ADOPTION_IDENTITY_FIELDS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "container_runtime_id",
)


def _sealed_state_unknown_disclosure(
    state: Any, row: dict[str, Any],
) -> dict[str, Any] | None:
    """之前某个 run 已带着这个作业以 blocked 封口，且封口时它的状态就读不出（任一类）。

    封口时只是暂时读不出、之后才变成永久读不出（slurm 过了 MinJobAge 被清除）也算：调用方
    另核「此刻永久读不出」（条件①），此时收养它对谁都没有进展（第三会话复审
    review_commits_0914b_third.md cb69292b P3-1）。
    依据是那份冻结 closure 自己记下的 external_jobs_terminal 检查（每行
    state_observation），不是模型写的 blocker 文本。读不出的记录跳过——找不到依据就
    照常收养：宁可多挡一次，不可漏收一个仍可能被收尾的作业。
    """
    try:
        logs = state.list_artifacts("experiment_log")
    except Exception:
        return None
    for item in reversed(logs or []):
        try:
            record = state.read_artifact(str(item.get("id") or ""))
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("produced_by_run_id") == state.run_id:
            continue
        metadata = record.get("metadata")
        if (not isinstance(metadata, dict) or metadata.get("record_kind") != "operation"
                or metadata.get("frozen") is not True):
            continue
        closure_input = metadata.get("operation_closure_input")
        if not isinstance(closure_input, dict) or closure_input.get("outcome") != "blocked":
            continue
        for check in closure_input.get("checks") or []:
            if not isinstance(check, dict) or check.get("name") != "external_jobs_terminal":
                continue
            evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
            for health_row in evidence.get("health") or []:
                if not isinstance(health_row, dict):
                    continue
                if not health_row.get("state_observation"):
                    continue
                reference = health_row.get("reference")
                reference = reference if isinstance(reference, dict) else {}
                if all(str(reference.get(field) or "") == str(row.get(field) or "")
                       for field in _ADOPTION_IDENTITY_FIELDS):
                    return {
                        "sealed_by_run_id": record.get("produced_by_run_id"),
                        "closure_id": metadata.get("operation_closure_id"),
                        "experiment_log_artifact_id": item.get("id"),
                        "sealed_state_observation": health_row.get("state_observation"),
                    }
    return None


def _adopt_unresolved_workflow_refs(state: Any) -> dict[str, Any]:
    """continuation run 收养：零本 run 提交时按未决 workflow 派生 refs。

    恰一行 → 由该行构造 scope 引用（行内非空字段参与匹配，缺 process_group_id
    等字段无妨 —— 选择器只比对非空字段）；零行 → 不收养（维持 oc:136 判决的
    记账降级现状）；多行 → 一次性拒绝并把每个候选的完整引用给出，重调时任选
    其一整项复制为 external_job_refs 元素即可。

    不收养、只披露的一类（2026-09-14 第三会话复审 §四 问题 2）：此刻仍永久读不出
    （本地账本读成功却没有记录 / 调度器报告查不到），且之前某个 run 已带着它以
    blocked 如实封口、封口时就读不出（任一类）。它的终态谁也读不出，收养它等于让一个无关 run 永远不能 success。
    暂时读不出、或还没有人如实封口过的，照常收养，续跑理应再试。
    """
    try:
        from .resource_manager import (
            _permanently_unreadable_observation,
            unresolved_external_workflows,
        )
    except ImportError:
        from tools.resource_manager import (
            _permanently_unreadable_observation,
            unresolved_external_workflows,
        )
    rows: list[dict[str, Any]] = []
    disclosed: list[dict[str, Any]] = []
    for row in unresolved_external_workflows(state):
        scheduler = str(row.get("scheduler") or "")
        observation = _permanently_unreadable_observation(scheduler, row.get("health") or {})
        sealed = (_sealed_state_unknown_disclosure(state, row)
                  if observation is not None else None)
        if sealed is None:
            rows.append(row)
            continue
        disclosed.append({
            "reference": _external_job_reference(row),
            "state_observation": observation.get("kind"),
            "observed": {key: observation.get(key)
                         for key in ("scheduler_phase", "stderr", "returncode")},
            "suggested_owner": "core" if scheduler.lower() == "local" else "run_owner",
            **sealed,
        })
    disclosure = {"disclosed_unreadable_jobs": disclosed} if disclosed else {}
    if not rows:
        return {"ok": True, "refs": [], **disclosure}
    if len(rows) > 1:
        return {
            "ok": False,
            "error_code": "external_job_adoption_ambiguous",
            "error": (
                "本 run 无受管提交，但存在多个未决 external job workflow，"
                "无法唯一收养。重调 record_operation_completion 时把下列候选"
                "之一整项复制进 external_job_refs 即可。"
            ),
            "candidates": [_external_job_reference(row) for row in rows],
            **disclosure,
        }
    row = rows[0]
    ref = {
        field: row.get(field)
        for field in _EXTERNAL_JOB_SCOPE_FIELDS
        if row.get(field) not in (None, "")
    }
    return {"ok": True, "refs": [ref], "adopted": True,
            "workflow_status": row.get("workflow_status"), **disclosure}


def _closure_receipt_for(state: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    """本 run 为该作业铸下的有效终态收据；没有或立不住则 None。

    判据完全复用 rm 的收据审计（所有权、schema、身份、终态相位、机械证据自洽），
    这里不重建。outcome=None 表示只问「这份收据立不立得住、它记的是什么」。
    """
    try:
        try:
            from .resource_manager import _operation_closure_receipt
        except ImportError:
            from tools.resource_manager import _operation_closure_receipt
        found = _operation_closure_receipt(state, record, None)
    except Exception:
        return None
    return found if found.get("status") == "success" else None


def _managed_external_job_verification(
    state: Any,
    job_ids: list[str] | None,
    external_job_refs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Resolve managed submissions and verify scheduler terminal state.

    A lifecycle artifact is a closure receipt, not proof that a submitted job
    is currently alive. Resolve the authoritative submission receipt and ask
    the scheduler/immutable-container health path used by the rest of the
    external-job lifecycle. The aggregate postcondition view itself is
    read-only; after it is built, one shared consumer persists only a declared
    expected-output failure needed for exact-output correction. It never
    projects aggregate success. This breaks the former cycle where operation
    completion waited for finalize while finalize waited for the operation log.
    """
    try:
        from .resource_manager import (
            _operation_completion_postconditions,
            _job_key_for_record,
            _state_observation_kind,
            _submission_payloads,
            _task_external_jobs,
            freeze_first_terminal_output_observation,
            lifecycle_for_submission,
            planned_stop_cancellation,
            probe_external_job_health,
        )
    except ImportError:
        from tools.resource_manager import (
            _operation_completion_postconditions,
            _job_key_for_record,
            _state_observation_kind,
            _submission_payloads,
            _task_external_jobs,
            freeze_first_terminal_output_observation,
            lifecycle_for_submission,
            planned_stop_cancellation,
            probe_external_job_health,
        )

    # 候选域与 finalize 侧同源：submission 收据 + 跨 session handoff 任务行；
    # 同一 job key 以 submission 收据优先（它携带 route_attempt_id）。
    merged_candidates: dict[str, dict[str, Any]] = {}
    for row in _task_external_jobs(state):
        merged_candidates.setdefault(_job_key_for_record(row), row)
    for record in _submission_payloads(state):
        merged_candidates[_job_key_for_record(record)] = record
    submissions = list(merged_candidates.values())
    requested_refs = [dict(item) for item in (external_job_refs or []) if isinstance(item, dict)]
    requested_ids = {str(job_id) for job_id in (job_ids or []) if str(job_id).strip()}
    if not requested_refs and not requested_ids:
        return {
            "ok": False,
            "error_code": "external_job_identity_missing",
            "error": (
                "external_job 必须提供已受管的 job_ids，或 scope-exact "
                "external_job_refs 作为真实执行证据。refs/job_ids 也可整体"
                "省略 —— 工具会从本 run 受管提交账本自动派生完整身份。"
            ),
        }

    selected: list[dict[str, Any]] = []
    # 并集语义（F9 §2b）：refs 与 job_ids 都走同一选择器解析。job_ids 里未被
    # 任何 ref 覆盖的 id 合成 {job_id} 请求参与解析（增选/跨 run 收养）——
    # 不能因 refs 非空（F9 后凡有本 run 提交则恒含自动派生项）就把 job_ids
    # 退化成只许子集的交叉校验。
    covered_ids = {str(ref.get("job_id") or "") for ref in requested_refs}
    requests: list[dict[str, Any]] = requested_refs + [
        {"job_id": job_id} for job_id in sorted(requested_ids - covered_ids)
    ]
    for requested in requests:
        requested_job_id = str(requested.get("job_id") or "")
        requested_scheduler = str(requested.get("scheduler") or "").casefold()
        if not requested_job_id:
            return {
                "ok": False,
                "error_code": "external_job_identity_invalid",
                "error": "external_job_refs 的每一项都必须包含 job_id",
            }
        candidates = []
        for record in submissions:
            if str(record.get("job_id") or "") != requested_job_id:
                continue
            if (
                requested_scheduler
                and str(record.get("scheduler") or "").casefold() != requested_scheduler
            ):
                continue
            if any(
                requested.get(field) not in (None, "")
                and (
                    str(record.get(field) or "").casefold()
                    != str(requested.get(field) or "").casefold()
                    if field == "launch_host"
                    else str(record.get(field) or "") != str(requested.get(field) or "")
                )
                for field in _EXTERNAL_JOB_SCOPE_FIELDS[2:]
            ):
                continue
            candidates.append(record)
        if len(candidates) != 1:
            # 报错必须可执行（BF-12）：给出同 job_id 的账本近似候选与差异字段，
            # 并声明 refs 整体可省略 —— 工具会从本 run 受管提交账本自动派生。
            near = [
                _external_job_reference(record)
                for record in submissions
                if str(record.get("job_id") or "") == requested_job_id
            ][:3]
            mismatched_fields = sorted({
                field
                for record in submissions
                if str(record.get("job_id") or "") == requested_job_id
                for field in _EXTERNAL_JOB_SCOPE_FIELDS[2:]
                if requested.get(field) not in (None, "")
                # 与上方匹配谓词同一把尺（§5：casefold 归一后再算 mismatched，
                # 镜像 rm._external_job_evidence_identity_value）——否则仅大小写
                # 漂移的 launch_host 会被误报进教育错误。
                and (
                    str(record.get(field) or "").casefold()
                    != str(requested.get(field) or "").casefold()
                    if field == "launch_host"
                    else str(record.get(field) or "") != str(requested.get(field) or "")
                )
            })
            return {
                "ok": False,
                "error_code": (
                    "external_job_identity_missing"
                    if not candidates
                    else "external_job_identity_ambiguous"
                ),
                "error": (
                    "未找到唯一的受管 external job submission；同 job_id 跨 scope "
                    "时必须提供 scheduler/namespace/launch_host 等精确引用。"
                    "external_job_refs 也可整体省略 —— 工具会从本 run 受管提交"
                    "账本自动派生完整身份。"
                ),
                "requested_ref": requested,
                "candidate_count": len(candidates),
                "near_candidates": near,
                "mismatched_fields": mismatched_fields,
            }
        selected.append(candidates[0])

    unique = {_job_key_for_record(record): record for record in selected}
    selected = [unique[key] for key in sorted(unique)]
    # 防御性校验：每个 requested_id 都已作为独立请求走过选择器（上方并集），
    # 走到这里而有 id 未落进 selected 是选择器不变量被破坏。报错仍按 BF-12
    # 给出口：refs 可能含框架自动派生项，调用方未必传过。
    if requested_ids and not requested_ids <= {
        str(row.get("job_id") or "") for row in selected
    }:
        return {
            "ok": False,
            "error_code": "external_job_identity_mismatch",
            "error": (
                "job_ids 有未解析到受管作业的项（external_job_refs 可能含框架"
                "自动派生项，并非全部来自调用方声明）。把未命中的作业以完整 "
                "external_job_refs 元素传入；refs/job_ids 也可整体省略 —— "
                "工具会从本 run 受管提交账本自动派生。"
            ),
            "unmatched_job_ids": sorted(
                requested_ids - {str(row.get("job_id") or "") for row in selected}
            ),
        }

    health_rows: list[dict[str, Any]] = []
    all_terminal = True
    all_successful = True
    for record in selected:
        # 收据优先。作业收尾时已按机械证据铸下不可变终态收据；此处再探针一次是
        # 重算，而且探的对象常常已被 cleanup 删掉，探回来是「terminal 但退出码为
        # 空」的假象（2026-09-08 与 09-09 活体各一次）。收据在就直接读它，
        # 判据留在铸造它的那一侧，不在这里重建第二套。
        receipt = _closure_receipt_for(state, record)
        if receipt is not None:
            payload = receipt.get("payload") or {}
            recorded = str(payload.get("outcome") or "")
            terminal = True
            # 提交时锚定任务原文声明了预期终止、收尾时机械判定符合的 operation_failed，
            # 达成了这一步的契约：结果词仍是物理事实，契约是否达成看 termination_matched。
            termination_matched = (
                recorded == "operation_failed"
                and (payload.get("health") or {}).get("termination_matched") is True)
            successful = recorded == "operation_completed" or termination_matched
            all_terminal = all_terminal and terminal
            all_successful = all_successful and successful
            health_rows.append({
                "reference": _external_job_reference(record),
                "terminal": True,
                "success_verified": successful,
                "success_evidence": {
                    "verified": True,
                    "succeeded": recorded == "operation_completed",
                    **({"termination_matched": True} if termination_matched else {}),
                    "source": "external_job_operation_closure",
                    "recorded_outcome": recorded,
                },
                "lifecycle": lifecycle_for_submission(state, record),
                "scheduler_phase": "terminal",
                "receipt_artifact_id": receipt.get("artifact_id"),
            })
            continue
        # 计划内停止（D07）：事前声明、取消已确认、不是替换。取消不铸收尾收据，作业也已被
        # 清理，探针读不出有用的东西；终态证据就是这笔确认过的取消。
        planned = planned_stop_cancellation(state, record)
        if planned is not None:
            all_terminal = all_terminal and True
            health_rows.append({
                "reference": _external_job_reference(record),
                "terminal": True,
                "success_verified": True,
                "success_evidence": {
                    "verified": True,
                    "succeeded": False,
                    "termination_matched": True,
                    "source": "planned_stop_cancellation",
                    **planned,
                },
                "lifecycle": lifecycle_for_submission(state, record),
                "scheduler_phase": "terminal",
            })
            continue
        health = freeze_first_terminal_output_observation(state, record, probe_external_job_health(
            state,
            str(record.get("scheduler") or ""),
            str(record.get("job_id") or ""),
            record.get("namespace"),
        ))
        terminal = health.get("status") == "success" and health.get("scheduler_phase") == "terminal"
        lifecycle = lifecycle_for_submission(state, record)
        success_evidence = _external_job_success_evidence(record, health, lifecycle)
        completion_postconditions = _operation_completion_postconditions(
            state,
            record,
            health,
            str(record.get("execution_class") or ""),
        )
        execution_success_verified = bool(
            terminal
            and success_evidence.get("verified")
            and (
                success_evidence.get("succeeded")
                or success_evidence.get("termination_matched") is True
            )
        )
        successful = bool(
            execution_success_verified
            and completion_postconditions.get("passed") is True
        )
        all_terminal = all_terminal and terminal
        all_successful = all_successful and successful
        health_rows.append(
            {
                "reference": _external_job_reference(record),
                "terminal": terminal,
                "execution_success_verified": execution_success_verified,
                "success_verified": successful,
                "success_evidence": success_evidence,
                "completion_postconditions": completion_postconditions,
                "lifecycle": lifecycle,
                "scheduler_phase": health.get("scheduler_phase"),
                "health_state": health.get("health_state"),
                "health_status": health.get("status"),
                "error": health.get("error"),
                # 读不出时的观测类别，随 closure 冻结；后续 run 据此判断「前一个 run 已带着
                # 永久读不出的它如实封口」，不看模型写的 blocker 文本。
                "state_observation": _state_observation_kind(
                    str(record.get("scheduler") or ""), health),
            }
        )
    verification = {
        "ok": True,
        "terminal": all_terminal,
        "successful": all_successful,
        "external_job_refs": [_external_job_reference(record) for record in selected],
        "health": health_rows,
    }
    return _persist_failed_route_postconditions(state, verification)


def _persist_failed_route_postconditions(
    state: Any,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Persist exact-output failures observed by the shared job verifier.

    The aggregate completion view stays read-only.  This consumer records only
    a route declaration that was mechanically observed missing after the
    physical execution itself succeeded.  It never projects route success, so
    a failing health source cannot accidentally unlock a dependent step.
    """
    if not verification.get("ok"):
        return verification
    try:
        try:
            from .execution_route import (
                record_external_route_execution_verification,
            )
        except ImportError:
            from tools.execution_route import (
                record_external_route_execution_verification,
            )
    except Exception as exc:
        return {
            **verification,
            "ok": False,
            "error_code": "external_route_failure_projection_unavailable",
            "error": (
                "route expected-output failure 无法加载持久化入口；"
                "不能声称 exact-output correction 已可用"
            ),
            "error_type": type(exc).__name__,
        }

    projections: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for row in verification.get("health") or []:
        if not isinstance(row, dict):
            continue
        completion_view = row.get("completion_postconditions") or {}
        route = (completion_view.get("sources") or {}).get("route") or {}
        if (
            row.get("execution_success_verified") is not True
            or route.get("status") != "ready"
            or route.get("declared") is not True
            or route.get("passed") is True
            or not list(route.get("missing_expected_outputs") or [])
        ):
            continue
        projection = record_external_route_execution_verification(
            state,
            external_job_ref=dict(row.get("reference") or {}),
            terminal=row.get("terminal") is True,
            success_verified=True,
            success_evidence=dict(row.get("success_evidence") or {}),
            expected_output_observation=dict(route),
        )
        projections.append(projection)
        failures.append({
            "reference": row.get("reference"),
            "completion_postconditions": completion_view,
            "route_projection": projection,
        })
        persisted_missing = bool(
            projection.get("status") == "error"
            and projection.get("reason") == "route_expected_outputs_missing"
        )
        if not persisted_missing:
            return {
                **verification,
                "ok": False,
                "error_code": "external_route_failure_projection_failed",
                "error": (
                    "route expected_outputs 已机械判为缺失，但失败事实未能"
                    "安全持久化；未建立 correction basis，禁止按纠正文案继续"
                ),
                "route_failure_projections": projections,
                "completion_postcondition_failures": failures,
            }
    if failures:
        return {
            **verification,
            "route_failure_projections": projections,
            "completion_postcondition_failures": failures,
        }
    return verification


def _completion_postcondition_failure_guidance(
    verification: dict[str, Any],
    *,
    blocked_action: str,
) -> dict[str, Any] | None:
    """Format one actionable model-facing error from the shared views."""
    failures: list[dict[str, Any]] = []
    route_missing = False
    for row in verification.get("health") or []:
        if not isinstance(row, dict):
            continue
        view = row.get("completion_postconditions")
        if not (
            row.get("execution_success_verified") is True
            and isinstance(view, dict)
            and view.get("passed") is not True
        ):
            continue
        sources = view.get("sources") or {}
        route = sources.get("route") or {}
        route_missing = route_missing or bool(
            route.get("declared") is True
            and route.get("missing_expected_outputs")
        )
        failures.append({
            "reference": row.get("reference"),
            "completion_postconditions": view,
        })
    if not failures:
        return None
    correction = (
        "若 route expected_outputs 声明错误，missing 事实已经持久化："
        "用 declare_execution_route 携带 amendment_reason 与 recovery_basis "
        "纠正同一 attempt 后重试。"
        if route_missing else ""
    )
    return {
        "error": (
            "external job 的物理执行已有成功证据，但声明的完成后置条件"
            f"未全部通过；不能推进 success。{correction}"
            "若产物确实缺失、health 失败或无法纠正，先 report_blocker，"
            f"再{blocked_action}。"
        ),
        "completion_postcondition_failures": failures,
        "recovery": {
            "exact_output_correction_available": route_missing,
            "blocked_action": blocked_action,
        },
    }


def _project_verified_external_jobs(
    state: Any,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """将每个已机械核验成功的 job 投影到其精确 route attempt。"""
    try:
        try:
            from .execution_route import (
                record_external_route_execution_verification,
            )
        except ImportError:
            from tools.execution_route import (
                record_external_route_execution_verification,
            )
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "external_route_projection_unavailable",
            "error": (f"external execution verification 无法加载：{type(exc).__name__}: {exc}"),
        }

    try:
        try:
            from .execution_route import external_finalization_already_projected
        except ImportError:
            from tools.execution_route import external_finalization_already_projected
    except Exception:
        external_finalization_already_projected = None  # type: ignore[assignment]

    projections: list[dict[str, Any]] = []
    for row in verification.get("health") or []:
        if not isinstance(row, dict):
            return {
                "ok": False,
                "error_code": "external_route_projection_invalid_receipt",
                "error": "external job verifier 返回了不可解析的 health receipt",
            }
        # 作业收尾时已把路线终态事实写下了，闭环不再投第二次：重复投影要么无意义，
        # 要么在两次之间发生过修订时直接撞 route_external_execution_verification_conflict。
        if external_finalization_already_projected is not None and (
                external_finalization_already_projected(
                    state, row.get("reference") or {})):
            projections.append({
                "reference": row.get("reference"),
                "skipped": "already_projected_by_finalize",
            })
            continue
        completion_view = row.get("completion_postconditions") or {}
        route_observation = (completion_view.get("sources") or {}).get("route")
        projection = record_external_route_execution_verification(
            state,
            external_job_ref=dict(row.get("reference") or {}),
            terminal=row.get("terminal") is True,
            success_verified=row.get("success_verified") is True,
            success_evidence=dict(row.get("success_evidence") or {}),
            expected_output_observation=(
                dict(route_observation)
                if isinstance(route_observation, dict)
                and route_observation.get("status") == "ready"
                else None
            ),
        )
        projections.append(projection)
        if projection.get("status") not in {"success", "not_applicable"}:
            return {
                "ok": False,
                "error_code": "external_route_projection_failed",
                "error": (
                    "external job 已客观成功，但 route execution receipt "
                    "未能安全持久化；禁止先写 operation success"
                ),
                "projection": projection,
                "projections": projections,
            }
    return {"ok": True, "projections": projections}


async def _verify_external_job_execution(
    state: Any,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Persist the route output observation for one terminal successful job.

    This is deliberately narrower than operation completion and external-job
    finalization: it neither freezes artifacts nor closes a handoff task. A
    passed aggregate gate writes route success. Missing expected outputs write
    the existing failed observation so exact-output correction has a basis,
    then return an actionable error. A failing health source never preprojects
    success. A whole operation still owns its evidence triplet and finalization.
    """
    scheduler_text = str(scheduler or "").strip().lower()
    job_id_text = str(job_id or "").strip()
    if not scheduler_text or not job_id_text:
        return {
            "status": "error",
            "error_code": "external_job_identity_missing",
            "error": "scheduler 和 job_id 都是必填的",
        }
    try:
        try:
            from .resource_manager import _external_job_record
        except ImportError:
            from tools.resource_manager import _external_job_record
        record = _external_job_record(state, scheduler_text, job_id_text, namespace)
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "external_job_identity_lookup_failed",
            "error": (f"无法读取唯一受管 external job 身份：{type(exc).__name__}: {exc}"),
        }
    if record is None:
        return {
            "status": "error",
            "error_code": "external_job_identity_missing",
            "error": (
                "未找到唯一受管 external job 记录；同 scheduler/job_id "
                "跨 scope 时必须提供 namespace"
            ),
        }

    reference = _external_job_reference(record)
    verification = _managed_external_job_verification(state, None, [reference])
    if not verification.get("ok"):
        return {
            "status": "error",
            "error_code": verification.get("error_code"),
            "error": verification.get("error"),
            "details": verification,
        }
    if not verification.get("terminal"):
        return {
            "status": "error",
            "error_code": "external_jobs_not_terminal",
            "error": ("受管 external job 仍在运行或状态未知；不能推进后续 route step"),
            "external_job_refs": verification.get("external_job_refs") or [],
            "health": verification.get("health") or [],
        }
    if not verification.get("successful"):
        guidance = _completion_postcondition_failure_guidance(
            verification,
            blocked_action=(
                '调用 finalize_external_job(outcome="operation_blocked")，'
                '或 record_operation_completion(outcome="blocked")'
            ),
        )
        return {
            "status": "error",
            "error_code": "external_job_success_unverified",
            **(
                guidance
                or {
                    "error": (
                        "external job 已终止，但没有可验证的成功证据；"
                        "不能推进后续 route step"
                    )
                }
            ),
            "external_job_refs": verification.get("external_job_refs") or [],
            "health": verification.get("health") or [],
        }

    projection = _project_verified_external_jobs(state, verification)
    if not projection.get("ok"):
        return {
            "status": "error",
            "error_code": projection.get("error_code"),
            "error": projection.get("error"),
            "external_job_refs": verification.get("external_job_refs") or [],
            "health": verification.get("health") or [],
            "route_projection": projection,
        }
    projections = projection.get("projections")
    # 作业收尾（或按计划停止的取消，D07）已经写下路线终态事实时，投影被跳过：路线已解锁，
    # 按成功返回，不报「缺收据」误导模型（第三会话复审 D07 P3）。走到这里作业核验已判成功。
    already_projected = bool(
        isinstance(projections, list) and len(projections) == 1
        and isinstance(projections[0], dict)
        and projections[0].get("skipped") == "already_projected_by_finalize")
    if already_projected:
        # 「已写下」不等于成功：finalize 缺预期产物时同样写收尾事件（failure_class=
        # expected_outputs_missing）再返回 error，这里不能据此报 success（第三会话复审 307e4079 P2）。
        # 只有生效收尾事件为成功、或这一步已 verified（例如纠正后），才解锁后续步骤。
        try:
            try:
                from .execution_route import (
                    build_route_snapshot, external_finalization_projection,
                )
            except ImportError:
                from tools.execution_route import (
                    build_route_snapshot, external_finalization_projection,
                )
            finalized = external_finalization_projection(state, reference) or {}
            attempt_id = str(finalized.get("attempt_id") or "")
            step_verified = bool(attempt_id) and any(
                isinstance(info, dict)
                and str(info.get("attempt_id") or "") == attempt_id
                and info.get("state") == "verified"
                for info in (build_route_snapshot(state).get("steps") or {}).values()
            )
        except Exception as exc:
            # 读不出已写下的收尾事实：按「不是成功」处理，与下面同一个出口。
            finalized, step_verified = {"audit_error": f"{type(exc).__name__}: {exc}"}, False
        if finalized.get("route_outcome") != "success" and not step_verified:
            failure_class = str(finalized.get("failure_class") or "")
            missing = list(finalized.get("missing_expected_outputs") or [])
            outputs_missing = failure_class == "expected_outputs_missing"
            return {
                "status": "error",
                "error_code": (
                    "route_expected_outputs_missing" if outputs_missing
                    else "route_external_finalization_not_successful"),
                "error": (
                    f"作业收尾已写下路线结果，但这一步声明的预期产物缺失：{missing}；"
                    "步骤是 failed，后续 route step 不能解锁"
                    if outputs_missing else
                    f"无法读取该作业已写下的路线收尾事实（{finalized['audit_error']}）；"
                    "不能确认后续 route step 已解锁"
                    if finalized.get("audit_error") else
                    f"作业收尾已写下路线结果 route_outcome={finalized.get('route_outcome')!r}，"
                    "不是成功；后续 route step 不能解锁"),
                "route_outcome": finalized.get("route_outcome"),
                "failure_class": failure_class or None,
                "missing_expected_outputs": missing,
                "external_job_refs": verification.get("external_job_refs") or [],
                "next_actions": (
                    [
                        "作业确实把产物写在别处：带 recovery_basis（failure_class=\"expected_output\"）"
                        "修订路线，把 expected_outputs 改指向作业真实写下的文件",
                        "作业确实没写出这些产物：按失败如实收尾，不要重提同一作业",
                    ] if outputs_missing else [
                        "按收尾结果处理：失败先诊断，带 recovery_basis 修订路线后再重开这一步",
                    ]),
            }
    if not already_projected and (
        not isinstance(projections, list)
        or len(projections) != 1
        or not isinstance(projections[0], dict)
        or projections[0].get("status") != "success"
    ):
        return {
            "status": "error",
            "error_code": "external_route_projection_receipt_missing",
            "error": (
                "external job 虽已验证成功，但未写入唯一的 route execution receipt；"
                "不能把后续 route step 视为已解锁"
            ),
            "external_job_refs": verification.get("external_job_refs") or [],
            "health": verification.get("health") or [],
            "route_projection": projection,
        }
    return {
        "status": "success",
        "scheduler": scheduler_text,
        "job_id": job_id_text,
        "already_projected": already_projected,
        "external_job_refs": verification.get("external_job_refs") or [],
        "health": verification.get("health") or [],
        "route_projection": projection,
    }


def _path_check(
    path_text: str, *, executable: bool = False, json_file: bool = False,
    declared_kind: str = "file",
) -> dict[str, Any]:
    """ROC 证据路径检查——035a 起是共享 evaluator 的薄包装（存在、类型、非空；目录声明
    以尾 / 表示，须至少含一个非空普通文件）。"""
    try:
        from .output_postconditions import evaluate_declared_path
    except ImportError:  # pragma: no cover - standalone node bootstrap.
        from tools.output_postconditions import evaluate_declared_path
    path = Path(path_text).expanduser()
    row = evaluate_declared_path(str(path), declared_kind=declared_kind)
    passed = bool(row.get("passed"))
    evidence: dict[str, Any] = {
        "path": str(path), "exists": row.get("kind") != "missing",
        "kind": row.get("kind"), "declared_kind": declared_kind,
    }
    if not passed:
        evidence["reason"] = row.get("reason")
    if path.exists() and path.is_file():
        evidence["bytes"] = path.stat().st_size
    if executable:
        executable_ok = bool(path.exists() and path.is_file() and path.stat().st_mode & 0o111)
        passed = passed and executable_ok
        evidence["executable"] = executable_ok
    if json_file and passed:
        try:
            json.loads(path.read_text(encoding="utf-8"))
            evidence["json_parseable"] = True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            passed = False
            evidence["json_parseable"] = False
    return {"name": f"path:{path.name}", "passed": bool(passed), "evidence": evidence}


def _checks(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item["name"]).strip(),
            "passed": item["passed"],
            "evidence": item.get("evidence", ""),
        }
        for item in raw or []
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        and isinstance(item.get("passed"), bool)
    ]


def _invalid_check_indices(raw: list[dict[str, Any]] | None) -> list[int]:
    """Return malformed caller supplied checks instead of silently discarding them.

    The operation receipt is written only by this tool.  A caller that spells
    a failed check as status=failed used to have it disappear here, leaving a
    later and misleading closure error.
    """
    invalid: list[int] = []
    for index, item in enumerate(raw or []):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or not isinstance(item.get("passed"), bool)
        ):
            invalid.append(index)
    return invalid


def _has_evidence(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return value is not None


def _current_run_artifacts(state: Any, artifact_type: str) -> list[dict[str, Any]]:
    run_id = str(getattr(state, "run_id", "") or "")
    records: list[dict[str, Any]] = []
    for summary in state.list_artifacts(artifact_type) or []:
        artifact_id = str(summary.get("id") or "")
        record = state.read_artifact(artifact_id) if artifact_id else None
        if str((record or {}).get("produced_by_run_id") or "") == run_id:
            records.append({**summary, "record": record or {}})
    return records


def _active_closure_artifacts(
    state: Any, artifact_type: str,
) -> list[dict[str, Any]]:
    """Return closure inventory using the canonical active view for all three
    closure artifact types.

    #879：此前只有 experiment_log 走有效视图，raw_results / clean_results 原样
    返回，于是「先误判 scientific 存草稿、再改判 operation」的死锁只要换一个产物
    类型就原样复现。三类共用同一个视图，冲突判定才不会因类型不同而分叉。
    """
    records = _current_run_artifacts(state, artifact_type)
    try:
        try:
            from .contract_audit import active_closure_artifacts
        except ImportError:
            from tools.contract_audit import active_closure_artifacts
        active, _ = active_closure_artifacts(state, artifact_type)
    except Exception:
        # Inventory uncertainty must stay fail-closed: an unreadable active view
        # cannot authorize dropping an existing log from operation closure.
        return records
    active_ids = {
        str(item.get("id") or "")
        for item in active
        if str(item.get("id") or "")
    }
    return [item for item in records if str(item.get("id") or "") in active_ids]


def _closure_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = (item.get("record") or {}).get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _closure_state(state: Any, closure_id: str) -> dict[str, Any]:
    """Inspect current-run closure artifacts before this tool writes anything."""
    inventory = {
        artifact_type: _active_closure_artifacts(state, artifact_type)
        for artifact_type in ("raw_results", "clean_results", "experiment_log")
    }
    all_records = [item for records in inventory.values() for item in records]
    if not all_records:
        return {"kind": "empty", "inventory": inventory}
    foreign = [
        item
        for item in all_records
        if not (
            _closure_metadata(item).get("operation_closure_owner") == _CLOSURE_OWNER
            and _closure_metadata(item).get("operation_closure_id") == closure_id
            and _closure_metadata(item).get("operation_closure_version") == _CLOSURE_SCHEMA_VERSION
        )
    ]
    duplicates = [item for records in inventory.values() if len(records) > 1 for item in records]
    if foreign or duplicates:
        conflicts = foreign or duplicates
        return {
            "kind": "conflict",
            "inventory": inventory,
            "conflicts": [
                {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "name": item.get("name"),
                    "frozen": bool(_closure_metadata(item).get("frozen")),
                }
                for item in conflicts
            ],
        }
    has_triplet = all(
        len(inventory[artifact_type]) == 1
        for artifact_type in ("raw_results", "clean_results", "experiment_log")
    )
    if has_triplet and all(
        _closure_metadata(item).get("frozen") is True
        for records in inventory.values()
        for item in records
    ):
        return {"kind": "complete", "inventory": inventory}
    return {"kind": "partial", "inventory": inventory}


def operation_child_obligation_projection(state: Any) -> dict[str, Any]:
    """Project the child delivery effect from one complete frozen closure.

    The projection never decides whether a parent scientific objective is
    complete.  It only says what this Experiment child actually delivered,
    and derives that statement from the already-frozen operation receipt
    rather than caller text or a mutable hook-state cache.
    """
    closure_id = f"{state.run_id}:operation"
    closure = _closure_state(state, closure_id)
    if closure.get("kind") != "complete":
        return {
            "status": "unavailable",
            "reason": "operation_closure_not_complete",
            "closure_id": closure_id,
        }
    inventory = closure["inventory"]
    records = {
        artifact_type: inventory[artifact_type][0]
        for artifact_type in ("raw_results", "clean_results", "experiment_log")
    }
    metadata = {
        artifact_type: _closure_metadata(record)
        for artifact_type, record in records.items()
    }
    closure_inputs = [item.get("operation_closure_input") for item in metadata.values()]
    if not all(isinstance(item, dict) for item in closure_inputs):
        return {
            "status": "unavailable",
            "reason": "operation_closure_receipt_missing",
            "closure_id": closure_id,
        }
    authoritative = dict(closure_inputs[0])
    if any(item != authoritative for item in closure_inputs[1:]):
        return {
            "status": "unavailable",
            "reason": "operation_closure_receipt_conflict",
            "closure_id": closure_id,
        }
    effect = str(authoritative.get("upstream_goal_effect") or "")
    if effect != "operational_subtask_only" or any(
        item.get("upstream_goal_effect") != effect for item in metadata.values()
    ):
        return {
            "status": "unavailable",
            "reason": "operation_child_effect_unverifiable",
            "closure_id": closure_id,
        }
    scientific_contribution = str(
        authoritative.get("scientific_contribution") or ""
    )
    if scientific_contribution != "none" or any(
        item.get("scientific_contribution") != scientific_contribution
        for item in metadata.values()
    ):
        return {
            "status": "unavailable",
            "reason": "operation_scientific_contribution_unverifiable",
            "closure_id": closure_id,
        }
    outcome = str(authoritative.get("outcome") or "")
    delivery_status = "completed" if outcome == "success" else outcome
    if not outcome or not delivery_status:
        return {
            "status": "unavailable",
            "reason": "operation_child_outcome_missing",
            "closure_id": closure_id,
        }
    return {
        "status": "ready",
        "child_obligation": {
            "schema_version": _CHILD_OBLIGATION_SCHEMA_VERSION,
            "kind": "operation_child_delivery",
            "run_id": str(state.run_id),
            "closure_id": closure_id,
            "source": "frozen_operation_closure",
            "receipt_artifact_id": str(records["raw_results"]["id"]),
            "artifact_ids": {
                artifact_type: str(record["id"])
                for artifact_type, record in records.items()
            },
            "delivery_status": delivery_status,
            "outcome": outcome,
            "upstream_goal_effect": effect,
            "scientific_contribution": scientific_contribution,
            "parent_goal_completion_claimed": False,
            # P0a v3 M4（issue §4 与任务书都列了）：对象要带它派生自的冻结收据身份，
            # 让父侧 reducer 能逐字节核回来，而不是只认一个 artifact id。
            "source_receipt": {
                "artifact_id": str(records["raw_results"]["id"]),
                "closure_id": closure_id,
                # v4：哈希在账本记录层（read_artifact），不在 list_artifacts 的摘要层
                # ——v3 读错了层，恒为空（Codex 复审 23 号）。
                "content_hash": (
                    str((records["raw_results"].get("record") or {}).get("content_hash") or "")
                    or None
                ),
            },
        },
    }


def operation_closure_status(state: Any) -> dict[str, Any]:
    """返回当前 run 的 operation closure 封口状态，不吞并科学结果三件套。"""
    closure_id = f"{state.run_id}:operation"
    inventory = {
        artifact_type: _active_closure_artifacts(state, artifact_type)
        for artifact_type in ("raw_results", "clean_results", "experiment_log")
    }
    records = [item for values in inventory.values() for item in values]
    owned = [
        item
        for item in records
        if (
            _closure_metadata(item).get("operation_closure_owner") == _CLOSURE_OWNER
            and _closure_metadata(item).get("operation_closure_id") == closure_id
        )
    ]
    mode = _current_execution_mode(state)
    operational = str(
        getattr(state, "hook_state", {}).get("_request_mode") or ""
    ) == "operation" or mode == "operational"
    if not owned and not operational:
        return {
            "closure_id": closure_id,
            "kind": "empty",
            "sealed": False,
            "owned_artifact_count": 0,
        }
    closure = _closure_state(state, closure_id)
    kind = str(closure.get("kind") or "conflict")
    # 库存一并带出：封口的消费方（路线层）要靠它区分「这一格 run 内还有救」和
    # 「这一格是诚实终态」。已冻结的产物一律不可否定，所以只有未冻结的草稿才是
    # 可 supersede 的出口；分不出这两者就只能一律说「开新 run」，那对 partial /
    # conflict 是假终态。
    frozen_ids: list[str] = []
    supersedable_ids: list[str] = []
    for item in records:
        meta = _closure_metadata(item)
        artifact_id = str(item.get("id") or item.get("artifact_id") or "")
        if not artifact_id:
            continue
        (frozen_ids if meta.get("frozen") else supersedable_ids).append(artifact_id)
    return {
        "closure_id": closure_id,
        "kind": kind,
        "sealed": kind != "empty",
        "owned_artifact_count": len(owned),
        "frozen_artifact_ids": frozen_ids,
        "supersedable_artifact_ids": supersedable_ids,
    }


def _unbindable_pending_steps(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """还没执行、且按 program 判定永远绑定不上执行动作的步骤（只读命令 / 工具名）。"""
    try:
        from .execution_route import unbindable_route_step_reason
    except ImportError:
        from tools.execution_route import unbindable_route_step_reason
    derived = snapshot.get("steps") or {}
    found: list[dict[str, Any]] = []
    for step in (snapshot.get("route") or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        if (derived.get(step.get("id")) or {}).get("state") != "pending":
            continue
        action = step.get("action") or {}
        reason = unbindable_route_step_reason(action)
        if reason is not None:
            found.append({
                "step_id": step.get("id"),
                "tool": action.get("tool"),
                "program": action.get("program"),
                "kind": "unbindable_read_only_step",
                "reason": reason,
            })
    return found


def _route_completion_verification(state: Any) -> dict[str, Any]:
    """从 canonical route 派生 success 门；不保存第二份路线进度。"""
    try:
        try:
            from .execution_route import build_route_snapshot
        except ImportError:
            from tools.execution_route import build_route_snapshot
        snapshot = build_route_snapshot(state)
    except Exception as exc:
        return {
            "ok": False,
            "applicable": True,
            "error_code": "execution_route_audit_failed",
            "error": f"canonical execution route 无法审计：{type(exc).__name__}: {exc}",
        }
    if snapshot.get("status") == "unavailable" and snapshot.get("reason") == "route_not_declared":
        return {
            "ok": True,
            "applicable": False,
            "route_state": "not_declared",
            "check": None,
        }
    route_state = str(snapshot.get("route_state") or "unavailable")
    if snapshot.get("status") != "ready" or route_state != "complete":
        incomplete = {
            "ok": False,
            "applicable": True,
            "error_code": "execution_route_incomplete",
            "error": (
                "canonical execution route 尚未 complete；operation 不能记录 outcome=success"
            ),
            "route_state": route_state,
            "route_status": snapshot.get("status"),
            "route_reason": snapshot.get("reason"),
            "ready_step_ids": snapshot.get("ready_step_ids") or [],
        }
        # 收敛任务书 K10：升级前冻结的路线里，只读命令或工具名步骤永远不会有 attempt，
        # 路线就永远到不了 complete。读取不回头判它，但要把出口说出来。
        unbindable = _unbindable_pending_steps(snapshot)
        if unbindable:
            step_ids = [item["step_id"] for item in unbindable]
            incomplete["unbindable_steps"] = unbindable
            incomplete["error"] += (
                f"；其中步骤 {step_ids} 永远不会有 attempt：只读命令直接执行、不带 route_step_id，"
                "工具名不是可执行入口"
            )
            incomplete["next_actions"] = [
                f"declare_execution_route 带 amendment_reason 修订路线：删掉步骤 {step_ids}，"
                "并同时把它们从其他步骤的 after 里删掉",
                'record_operation_completion(outcome="success") 重新收尾',
            ]
        cancelled_steps = sorted(
            str(step_id) for step_id, info in (snapshot.get("steps") or {}).items()
            if isinstance(info, dict) and info.get("state") == "cancelled")
        if cancelled_steps:
            # 没声明计划内停止的取消不追认（D07）；把如实出口说出来，别让模型去重跑或绕开 cancel_job。
            incomplete["cancelled_steps"] = cancelled_steps
            incomplete["error"] += (
                f"；步骤 {cancelled_steps} 的作业被取消，提交时没有声明计划内停止，不算完成")
            incomplete["next_actions"] = [
                *incomplete.get("next_actions", []),
                'record_operation_completion(outcome="blocked") 如实收尾，写明作业被取消',
                "任务本身要求中途停下（常驻服务、按判据停、重启测试）时，提交时声明 "
                "expected_termination.planned_stop；不要为了记 success 重跑，也不要绕开 cancel_job",
            ]
        return incomplete
    route_ref = snapshot.get("route_ref") or {}
    check = {
        "name": _ROUTE_COMPLETION_CHECK,
        "passed": True,
        "evidence": {
            "route_artifact_id": route_ref.get("artifact_id"),
            "route_version": route_ref.get("version"),
            "route_content_hash": route_ref.get("content_hash"),
            "route_state": route_state,
        },
    }
    return {
        "ok": True,
        "applicable": True,
        "route_state": route_state,
        "route_ref": route_ref,
        "check": check,
    }


def audit_operation_route_alignment(state: Any) -> dict[str, Any]:
    """验证冻结 success 收据与当前 frozen canonical route 仍一致。"""
    # 与 closure inventory 同源：被合法否定的草稿不得让这条一致性审计空转。
    clean = _active_closure_artifacts(state, "clean_results")
    if len(clean) != 1:
        return {
            "passed": True,
            "applicable": False,
            "reason": "operation clean_results 尚未形成；由基础 closure audit 处理",
        }
    content = str((clean[0].get("record") or {}).get("content") or "")
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {
            "passed": True,
            "applicable": False,
            "reason": "operation clean_results 无法解析；由基础 closure audit 处理",
        }
    if not isinstance(payload, dict) or payload.get("outcome") != "success":
        return {
            "passed": True,
            "applicable": False,
            "reason": "failed/blocked operation 不以 route complete 作为终态条件",
        }
    checks = (payload.get("verification") or {}).get("checks") or []
    route_checks = [
        item
        for item in checks
        if isinstance(item, dict) and item.get("name") == _ROUTE_COMPLETION_CHECK
    ]
    current = _route_completion_verification(state)
    if not current.get("ok"):
        return {
            "passed": False,
            "applicable": True,
            "route_state": current.get("route_state"),
            "reason": str(current.get("error") or "execution route 未完成"),
            "details": current,
        }
    expected = current.get("check")
    if not current.get("applicable"):
        passed = not route_checks
        return {
            "passed": passed,
            "applicable": bool(route_checks),
            "route_state": current.get("route_state"),
            "reason": (
                "success closure 与未声明路线状态一致"
                if passed
                else "success closure 引用了当前已不存在的 canonical route"
            ),
        }
    passed = route_checks == [expected]
    return {
        "passed": passed,
        "applicable": True,
        "route_state": current.get("route_state"),
        "route_ref": current.get("route_ref"),
        "frozen_route_check": route_checks[0] if len(route_checks) == 1 else None,
        "reason": (
            "冻结 success 收据绑定当前 complete canonical route"
            if passed
            else "冻结 success 收据缺少当前 complete canonical route 的精确版本绑定"
        ),
    }


def _registered_blocker(state: Any, blocker_id: str) -> dict[str, Any] | None:
    wanted = str(blocker_id or "").strip()
    if not wanted or wanted == "experiment_closure_incomplete":
        return None
    for blocker in state.hook_state.get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        if str(blocker.get("blocker_id") or blocker.get("id") or "") != wanted:
            continue
        if str(blocker.get("reporting_node") or "experiment") == "experiment":
            return dict(blocker)
    return None


def _reported_blocker_evidence(blocker: dict[str, Any]) -> dict[str, Any]:
    """Project a blocker without changing the legacy ownerless shape."""
    evidence = {
        "blocker_id": blocker.get("blocker_id") or blocker.get("id"),
        "category": blocker.get("category"),
        "summary": blocker.get("summary"),
        "evidence_paths": blocker.get("evidence_paths") or [],
        "requested_action": blocker.get("requested_action"),
    }
    suggested_owner = blocker.get("suggested_owner")
    if isinstance(suggested_owner, str) and suggested_owner.strip():
        evidence["suggested_owner"] = suggested_owner.strip()
    return evidence


def _file_entry(path: Path, role: str, retention: str = "protected") -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path.resolve()),
        "sha256": digest,
        "role": role,
        "retention": retention,
        "bytes": path.stat().st_size,
    }


_EVIDENCE_ROLE_PRIORITY = {
    "operation_output": 0,
    "operation_json_output": 1,
    "operation_executable": 2,
}


def _normalize_evidence_files(
    artifact_paths: list[str] | None,
    executable_paths: list[str] | None,
    json_paths: list[str] | None,
) -> list[dict[str, Any]]:
    """按解析后的文件身份合并证据角色，并保留最强机械校验。"""
    by_path: dict[str, dict[str, Any]] = {}
    groups = (
        (artifact_paths or [], "operation_output", False, False),
        (json_paths or [], "operation_json_output", False, True),
        (executable_paths or [], "operation_executable", True, False),
    )
    for paths, role, executable, json_file in groups:
        for path_text in paths:
            # 035a：kind 在 resolve 之前读——尾 / 是目录声明，resolve 会把它抹掉。
            declared_kind = "directory" if str(path_text).rstrip().endswith("/") else "file"
            path = Path(str(path_text)).expanduser()
            try:
                canonical = str(path.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                canonical = str(path.absolute())
            current = by_path.get(canonical)
            if current is None:
                by_path[canonical] = {
                    "path": canonical,
                    "role": role,
                    "executable": executable,
                    "json_file": json_file,
                    "declared_kind": declared_kind,
                }
                continue
            current["executable"] = bool(current["executable"] or executable)
            current["json_file"] = bool(current["json_file"] or json_file)
            if _EVIDENCE_ROLE_PRIORITY[role] > _EVIDENCE_ROLE_PRIORITY[str(current["role"])]:
                current["role"] = role
    return list(by_path.values())


def _evidence_files_from_owned_raw_draft(
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """从旧版工具 draft 恢复原证据；不接受重试调用换一组路径。"""
    record = item.get("record") or {}
    try:
        payload = json.loads(str(record.get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return None, "旧 raw draft 不是可解析的 JSON manifest"
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return None, "旧 raw draft 缺少 files 列表"
    by_path: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, tuple[Any, Any, Any]] = {}
    role_flags = {
        "operation_output": (False, False),
        "operation_json_output": (False, True),
        "operation_executable": (True, False),
    }
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            return None, f"旧 raw draft files[{index}] 不是 object"
        role = str(entry.get("role") or "")
        if role == "operation_verification_receipt":
            continue
        if role not in role_flags:
            return None, f"旧 raw draft files[{index}] 含未知 operation role={role!r}"
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            return None, f"旧 raw draft files[{index}] 缺少 path"
        try:
            canonical = str(Path(raw_path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None, f"旧 raw draft files[{index}] path 无法规范化"
        fingerprint = (
            entry.get("sha256"),
            entry.get("bytes"),
            entry.get("retention"),
        )
        previous_fingerprint = fingerprints.get(canonical)
        if previous_fingerprint is not None and previous_fingerprint != fingerprint:
            return None, (
                "旧 raw draft 同一路径的重复条目内容身份不一致，必须人工对账：" + canonical
            )
        fingerprints[canonical] = fingerprint
        executable, json_file = role_flags[role]
        current = by_path.get(canonical)
        if current is None:
            by_path[canonical] = {
                "path": canonical,
                "role": role,
                "executable": executable,
                "json_file": json_file,
            }
            continue
        current["executable"] = bool(current["executable"] or executable)
        current["json_file"] = bool(current["json_file"] or json_file)
        if _EVIDENCE_ROLE_PRIORITY[role] > _EVIDENCE_ROLE_PRIORITY[str(current["role"])]:
            current["role"] = role
    return list(by_path.values()), None


def _coalesced_owned_raw_content(
    item: dict[str, Any],
) -> tuple[str | None, str | None]:
    """只修复身份一致的重复 manifest 项；其他旧 draft 保持 fail-closed。"""
    record = item.get("record") or {}
    try:
        payload = json.loads(str(record.get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return None, "旧 raw draft 不是可解析的 JSON manifest"
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return None, "旧 raw draft 缺少 files 列表"
    priorities = {
        **_EVIDENCE_ROLE_PRIORITY,
        "operation_verification_receipt": 3,
    }
    positions: dict[str, int] = {}
    fingerprints: dict[str, tuple[Any, Any, Any]] = {}
    merged: list[dict[str, Any]] = []
    changed = False
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            return None, f"旧 raw draft files[{index}] 不是 object"
        role = str(entry.get("role") or "")
        if role not in priorities:
            return None, f"旧 raw draft files[{index}] 含未知 operation role={role!r}"
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            return None, f"旧 raw draft files[{index}] 缺少 path"
        try:
            canonical = str(Path(raw_path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None, f"旧 raw draft files[{index}] path 无法规范化"
        fingerprint = (
            entry.get("sha256"),
            entry.get("bytes"),
            entry.get("retention"),
        )
        if canonical not in positions:
            positions[canonical] = len(merged)
            fingerprints[canonical] = fingerprint
            merged.append(dict(entry))
            continue
        if fingerprints[canonical] != fingerprint:
            return None, (
                "旧 raw draft 同一路径的重复条目内容身份不一致，必须人工对账：" + canonical
            )
        changed = True
        previous = merged[positions[canonical]]
        if priorities[role] > priorities[str(previous.get("role") or "")]:
            previous["role"] = role
    if not changed:
        return None, None
    payload["files"] = merged
    return json.dumps(payload, ensure_ascii=False, indent=2), None


def _freeze_failure(
    result: dict[str, Any],
    *,
    closure_id: str,
    artifact_type: str,
    artifact_ids: dict[str, str],
) -> dict[str, Any]:
    """保留 freeze gate 的具体证据，并指向唯一安全恢复入口。"""
    payload: dict[str, Any] = {
        "status": "error",
        "error_code": "operation_artifact_freeze_failed",
        "error": result.get("error") or f"{artifact_type} freeze failed",
        "closure_id": closure_id,
        "artifact_type": artifact_type,
        **artifact_ids,
        "partial_closure": {
            "owner": _CLOSURE_OWNER,
            "state": "partial",
            "retry_guidance": (
                "这是 record_operation_completion 拥有的可恢复 partial closure。"
                "不要手工 save_artifact、freeze_artifact、删除或改写三件套；"
                "修复 failed_checks/reasons 指向的外部证据后，以相同 task_kind、"
                "objective、outcome 重试本工具。若自动生成内容本身不合法，"
                "请报告 framework blocker。"
            ),
        },
    }
    for key in ("failed_checks", "reasons", "hint", "errors"):
        if key in result:
            payload[key] = result[key]
    if result.get("error_code"):
        payload["freeze_error_code"] = result["error_code"]
    return payload


def _operation_log(
    *,
    objective: str,
    task_kind: str,
    status: str,
    raw_id: str,
    clean_id: str,
    checks: list[dict[str, Any]],
    next_step: str,
    summary: str,
) -> str:
    verification_lines = []
    for item in checks:
        check_status = "passed" if item.get("passed") else "failed"
        evidence = json.dumps(item.get("evidence"), ensure_ascii=False, default=str)
        verification_lines.append(
            "- {}: {}; evidence={}".format(item.get("name"), check_status, evidence)
        )
    verification = "\n".join(verification_lines)
    return (
        "# Experiment Log — Operation\n\n"
        "## Objective\n"
        f"{objective}\n\n"
        "## Execution Evidence\n"
        f"- task_kind: {task_kind}\n"
        f"- raw_results_artifact_id: {raw_id}\n"
        f"- clean_results_artifact_id: {clean_id}\n\n"
        "## Verification\n"
        f"{verification}\n\n"
        "## Result\n"
        f"status: {status}\n"
        "record_kind: operation\n"
        "upstream_goal_effect: operational_subtask_only\n"
        "scientific_contribution: none\n"
        "note: this receipt closes only the operational subtask, never the upstream scientific objective.\n"
        f"summary: {summary or 'operation evidence recorded'}\n"
        + (f"next_step: {next_step}\n" if next_step else "")
    )


async def _record_operation_completion(
    state: Any,
    task_kind: str,
    objective: str,
    outcome: str = "success",
    package_name: str | None = None,
    import_name: str | None = None,
    expected_version: str | None = None,
    artifact_paths: list[str] | None = None,
    executable_paths: list[str] | None = None,
    json_paths: list[str] | None = None,
    job_ids: list[str] | None = None,
    external_job_refs: list[dict[str, Any]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    blocker_id: str = "",
    next_step: str = "",
    summary: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Create and freeze raw evidence, clean verification, and the canonical operation log."""
    # 判决拆除·第三波（oc:132，2026-09-02）：作用域归注册表/分诊层——本工具只在
    # operation 模式派发。走到这里而 mode 不对是**框架**路由不变量被破坏，模型对
    # 此无能为力，所以不再当 error 返给模型：记 log.error，收据照记（如实带 mode）。
    request_mode = str(state.hook_state.get("_request_mode") or "")
    if request_mode != "operation":
        log.error("framework routing invariant violated: record_operation_completion "
                  "dispatched with _request_mode=%r (expected 'operation')", request_mode)
    # oc:139 → schema：task_kind/outcome 枚举与 objective 非空由 schema 在派发口核。
    kind, outcome = str(task_kind or "").lower().strip(), str(outcome or "").lower().strip()
    objective = str(objective or "").strip()
    requested_kind = kind
    kind = _TASK_KIND_ALIASES.get(requested_kind, requested_kind)
    next_step = str(next_step or "").strip()

    invalid_checks = _invalid_check_indices(checks)
    status_style = [
        index
        for index, item in enumerate(checks or [])
        if isinstance(item, dict) and "status" in item
    ]
    if invalid_checks or status_style:
        positions = ", ".join(str(index) for index in sorted(set(invalid_checks + status_style)))
        return {
            "status": "error",
            "error": (
                "checks 参数第 " + positions + " 项必须是 {name, passed, evidence}；"
                "失败检查请写 {name: ..., passed: false, evidence: ...}，不接受 status=failed。"
            ),
            "error_code": "invalid_checks",
        }
    correction_disclosure = _route_correction_disclosure(state)
    if not correction_disclosure.get("ok"):
        return {
            "status": "error",
            "error_code": "route_correction_disclosure_audit_failed",
            "error": (
                "无法从 authoritative validated recovery receipt 派生本地纠正"
                "限制；拒绝冻结 operation closure"
            ),
            "details": correction_disclosure.get("error"),
        }
    limitation_check_name = str(
        correction_disclosure.get("check_name") or "")
    if not limitation_check_name:
        return {
            "status": "error",
            "error_code": "route_correction_disclosure_audit_failed",
            "error": "route correction disclosure 缺少框架拥有的具名 check 名称",
        }
    if any(
        isinstance(item, dict)
        and item.get("name") == limitation_check_name
        for item in (checks or [])
    ):
        return {
            "status": "error",
            "error_code": "reserved_verification_check",
            "error": (
                f"{limitation_check_name} 由框架从 validated "
                "recovery receipt 自动生成，调用方不能自行声明"
            ),
        }

    try:
        try:
            from .run_contract import audit_execution_intent_binding
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.run_contract import audit_execution_intent_binding
        intent_binding = audit_execution_intent_binding(state, require=False)
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "execution_intent_binding_required",
            "error": "operation closure cannot audit immutable upstream intent",
            "execution_intent_binding": {
                "passed": False,
                "status": "binding_audit_error",
                "reason": f"{type(exc).__name__}: {exc}",
            },
        }
    if not (
        intent_binding.get("passed", False)
        and intent_binding.get("status") == "bound"
        and intent_binding.get("scope_mode") == "operational"
    ):
        # 门本身是 23a0fe78 的刻意加固（配套回归测试
        # test_legacy_operation_scope_cannot_create_a_new_closure 钉着），不放宽。
        #
        # 但拒绝必须**可执行**。`intent_unavailable_at_classification` 的成因是调用方
        # 派发时没给 node_inputs —— run_node 的 schema 里它不是必填（required 只有
        # node_type 和 user_note），所以这是框架允许的派发方式，而 experiment 改不了
        # 别人给自己的输入。原文案「需要一个带 node_inputs 的新 run」正是
        # shared/tools/run_node.py:733 记下的那个坑：现场照做不了，只会原样重派、
        # 一模一样地再失败。这里按成因分流，把「该谁解、怎么解」说清楚。
        _binding_status = str(intent_binding.get("status") or "")
        _scope_mode = str(intent_binding.get("scope_mode") or "")
        if _scope_mode and _scope_mode != "operational":
            _binding_status = (
                f"{_binding_status}/scope_mode={_scope_mode}"
            )
        if _binding_status == "intent_unavailable_at_classification":
            return {
                "status": "error",
                "error_code": "execution_intent_binding_required",
                "error": (
                    "本 run 在分类时就没有可核验的上游意图：调用方派发时没有传 node_inputs。"
                    "这不是本 run 能自行补救的 —— 你改不了别人给你的输入，"
                    "重派同一个调用只会同样失败。"
                ),
                "next_step": (
                    "调用 report_blocker(suggested_owner='<派发本 run 的调用方>', ...)，"
                    "写明本 run 需要以非空 node_inputs 重新派发才能收尾，"
                    "并附上已完成的工作与证据路径；不要重跑，也不要改写自己的 scope。"
                ),
                "execution_intent_binding": intent_binding,
            }
        return {
            "status": "error",
            "error_code": "execution_intent_binding_required",
            "error": (
                "operation closure requires a current v1 upstream-input binding; "
                f"本 run 的绑定状态是 {_binding_status!r}，legacy receipts are "
                "audit-only and cannot authorize new writes"
            ),
            "execution_intent_binding": intent_binding,
        }

    closure_id = f"{state.run_id}:operation"
    closure = _closure_state(state, closure_id)
    if closure["kind"] == "conflict":
        return {
            "status": "error",
            "error_code": "operation_closure_conflict",
            "error": (
                "当前 run 已存在不属于 operation completion 的结果 artifact；"
                "不会再写第二套三件套。若这些是误建的未冻结草稿（例如先按 "
                "scientific 存下、随后改判 operation），逐个调用 "
                "supersede_closure_draft(artifact_id=\"<conflicts 里的 id>\", "
                "reason=\"<为何是误建>\") 把它们从有效视图中排除后重试；"
                "raw_results 与 clean_results 给任意一个 id 即可成对否定。"
                "已冻结的产物是已验证证据，不可否定。"),
            "conflicts": closure["conflicts"],
            "recovery_tool": "supersede_closure_draft",
        }

    # One active-view preflight owns both complete idempotence and partial
    # recovery.  Existing drafts are included: a save/freeze interruption must
    # not let a stale raw record slip into a newly completed triplet.
    expected_limitations = correction_disclosure.get("limitations") or []
    expected_limitation_check = correction_disclosure.get("check")
    for artifact_type, records in closure["inventory"].items():
        for record in records:
            if not _correction_disclosure_matches(
                _closure_metadata(record),
                limitations=expected_limitations,
                check=expected_limitation_check,
                check_name=limitation_check_name,
            ):
                return {
                    "status": "error",
                    "error_code": "route_correction_disclosure_mismatch",
                    "error": (
                        f"现有 {artifact_type} 的本地纠正限制被省略或改写；"
                        "拒绝幂等返回或混接后续三件套"
                    ),
                }

    if closure["kind"] == "complete":
        inventory = closure["inventory"]
        frozen_input = _closure_metadata(inventory["raw_results"][0]).get("operation_closure_input")
        requested_identity = (kind, objective, outcome)
        frozen_identity = (
            (
                frozen_input.get("task_kind"),
                frozen_input.get("objective"),
                # 机械降级（success→partial）不改变身份：比对调用方**请求**的值，
                # 否则同一组入参的幂等重试会与冻结身份不符而被本门拒掉。
                frozen_input.get("requested_outcome", frozen_input.get("outcome")),
            )
            if isinstance(frozen_input, dict)
            else (None, None, None)
        )
        if requested_identity != frozen_identity:
            return {
                "status": "error",
                "error_code": "operation_closure_conflict",
                "error": "请求与已冻结 operation closure 身份不一致，不能当作幂等成功。",
                "closure_id": closure_id,
                "expected": {
                    "task_kind": frozen_identity[0],
                    "objective": frozen_identity[1],
                    "outcome": frozen_identity[2],
                },
            }
        route_audit = audit_operation_route_alignment(state)
        if outcome == "success" and not route_audit.get("passed"):
            return {
                "status": "error",
                "error_code": "operation_route_audit_failed",
                "error": route_audit.get("reason"),
                "execution_route": route_audit,
            }
        response = {
            "status": "success",
            "idempotent": True,
            "closure_id": closure_id,
            "artifact_id": inventory["experiment_log"][0]["id"],
            "experiment_log_artifact_id": inventory["experiment_log"][0]["id"],
            "raw_results_artifact_id": inventory["raw_results"][0]["id"],
            "clean_results_artifact_id": inventory["clean_results"][0]["id"],
        }
        child_projection = operation_child_obligation_projection(state)
        child_obligation = child_projection.get("child_obligation")
        if isinstance(child_obligation, dict):
            response.update({
                "upstream_goal_effect": child_obligation["upstream_goal_effect"],
                "scientific_contribution": child_obligation[
                    "scientific_contribution"
                ],
                "child_obligation": child_obligation,
            })
        else:
            response["child_obligation_projection"] = child_projection
        # F9 存量死锁出路：修复前冻结的 closure 可能带空 refs——它不能作为
        # 未决 workflow 的 finalize 证据。幂等返回如实披露并指明唯一出口，
        # 不静默假装可用（探针只在冻结 refs 为空时才发生）。
        frozen_log_refs = list(
            _closure_metadata(inventory["experiment_log"][0]).get("external_job_refs") or []
        )
        if not frozen_log_refs:
            try:
                from .resource_manager import unresolved_external_workflows
            except ImportError:
                from tools.resource_manager import unresolved_external_workflows
            unresolved = unresolved_external_workflows(state)
            if unresolved:
                response["external_job_refs_frozen_empty"] = True
                response["unresolved_external_workflows"] = [
                    {"scheduler": row.get("scheduler"), "job_id": row.get("job_id"),
                     "workflow_status": row.get("workflow_status")}
                    for row in unresolved
                ]
                response["recovery"] = (
                    "该 closure 铸于自动派生 refs 之前，不能作为这些作业的 "
                    "finalize 证据；用 resume_run 开 continuation run —— 新收尾"
                    "会自动收养未决 workflow 并铸出可 finalize 的新 log。"
                )
        return response

    resumed_input: dict[str, Any] | None = None
    if closure["kind"] == "partial":
        for records in closure["inventory"].values():
            if records:
                candidate = _closure_metadata(records[0]).get("operation_closure_input")
                if isinstance(candidate, dict):
                    resumed_input = candidate
                    break
        if not resumed_input:
            return {
                "status": "error",
                "error_code": "operation_closure_conflict",
                "error": "部分 operation closure 缺少工具持久化的输入，不能安全续写。",
            }
        # 与 complete 幂等分支同一把尺：机械降级（success→partial）不改变身份，
        # 比对调用方**请求**的 outcome（requested_outcome 回退 outcome），否则
        # 降级过的 closure 在冻结中途崩溃后，同一组入参的重试会被本门拒掉。
        expected = (
            resumed_input.get("task_kind"),
            resumed_input.get("objective"),
            resumed_input.get("requested_outcome", resumed_input.get("outcome")),
        )
        requested = (kind, objective, outcome)
        if requested != expected:
            return {
                "status": "error",
                "error_code": "operation_closure_conflict",
                "error": "重试参数与已冻结 closure 不一致；不会把新的 clean/log 绑定到旧 raw。",
                "closure_id": closure_id,
                "expected": {
                    "task_kind": expected[0],
                    "objective": expected[1],
                    "outcome": expected[2],
                },
            }
        # 解包赋值仍取**生效**值：降级过的 closure 续写时 status 必须与冻结
        # receipt 一致（partial），不能被请求值抬回 success。
        kind, objective = expected[0], expected[1]
        outcome = resumed_input.get("outcome")
        next_step = str(resumed_input.get("next_step") or "")
        summary = str(resumed_input.get("summary") or "")

    verified = list(resumed_input.get("checks") or []) if resumed_input else _checks(checks)
    existing_route_checks = [
        item
        for item in verified
        if isinstance(item, dict) and item.get("name") == _ROUTE_COMPLETION_CHECK
    ]
    if not resumed_input and existing_route_checks:
        return {
            "status": "error",
            "error_code": "reserved_verification_check",
            "error": (
                f"{_ROUTE_COMPLETION_CHECK} 由框架根据 frozen route 自动生成，调用方不能自行声明"
            ),
        }
    if not resumed_input and expected_limitation_check is not None:
        verified.append(expected_limitation_check)
    resolved_external_job_refs = (
        list(resumed_input.get("external_job_refs") or []) if resumed_input else []
    )
    excluded_jobs: list[dict[str, Any]] = []
    ledger_errors: list[dict[str, Any]] = []
    adoption: dict[str, Any] | None = None
    if resumed_input and not resolved_external_job_refs:
        # 存量回填（F9）：修复前冻结的 partial closure 带着空 refs——续写完成前
        # 按同一两级枚举补全，使本次落盘的 clean/log 可作 finalize 证据。
        # 只回填本次落盘件与 checks；已冻结的 raw 与 closure_input 原样不动。
        own_jobs, excluded_jobs, ledger_errors = _own_run_managed_submissions(state)
        backfill_refs = [_external_job_reference(record) for record in own_jobs]
        if not backfill_refs:
            adoption = _adopt_unresolved_workflow_refs(state)
            if not adoption.get("ok"):
                return {
                    "status": "error",
                    "error_code": adoption.get("error_code"),
                    "error": adoption.get("error"),
                    "candidates": adoption.get("candidates"),
                }
            backfill_refs = list(adoption.get("refs") or [])
        if backfill_refs:
            job_verification = _managed_external_job_verification(state, None, backfill_refs)
            if not job_verification.get("ok"):
                return {
                    "status": "error",
                    "error_code": job_verification.get("error_code"),
                    "error": job_verification.get("error"),
                    "details": job_verification,
                }
            resolved_external_job_refs = list(job_verification.get("external_job_refs") or [])
            backfill_terminal = bool(job_verification.get("terminal"))
            backfill_successful = bool(job_verification.get("successful"))
            verified.append({
                "name": "external_jobs_terminal", "passed": backfill_terminal,
                "evidence": {"external_job_refs": resolved_external_job_refs,
                             "health": job_verification.get("health") or []},
            })
            verified.append({
                "name": "external_jobs_successful", "passed": backfill_successful,
                "evidence": {"external_job_refs": resolved_external_job_refs},
            })
            verified.append({
                "name": "resumed_refs_backfilled", "passed": True,
                "evidence": {"external_job_refs": resolved_external_job_refs,
                             "source": ("adopted_unresolved_workflow"
                                        if adoption and adoption.get("adopted")
                                        else "own_run_submissions")},
            })
            if outcome == "success" and not (backfill_terminal and backfill_successful):
                guidance = _completion_postcondition_failure_guidance(
                    job_verification,
                    blocked_action=(
                        '在 continuation run 中调用 '
                        'record_operation_completion(outcome="blocked")'
                    ),
                )
                # 续传不启用降级（closure_input 复制 resumed_input，改判会造成
                # outcome/status 分叉）；存量 partial-success 无成功证据时拒绝，
                # 出口：走 continuation run 重新收尾。
                return {
                    "status": "error",
                    "error_code": ("external_jobs_not_terminal"
                                   if not backfill_terminal
                                   else "external_job_success_unverified"),
                    **(
                        guidance
                        or {
                            "error": (
                                "存量 partial closure 以 success 续写，但受管作业"
                                "缺少可验证的终态成功证据；不能续写 success。出口："
                                "用 resume_run 开 continuation run 重新诚实收尾，"
                                "或先完成对账。"
                            )
                        }
                    ),
                    "details": job_verification,
                }
    blocker: dict[str, Any] | None = None
    if outcome == "blocked" and not resumed_input:
        blocker = _registered_blocker(state, blocker_id)
        if blocker is None:
            # 判决拆除（oc:178 删，2026-08-31）：收据 outcome=blocked 本身就是
            # blocker 声明，要求另有一条已登记 blocker 是重复抄件。缺登记如实
            # 记成一条 passed=False 的 check，不再拒绝收尾。
            verified.append({
                "name": "registered_blocker_present", "passed": False,
                "evidence": {"reason": "no matching report_blocker entry in this run",
                             "blocker_id": blocker_id},
            })
        if blocker is not None:
            verified.append(
            {
                "name": "reported_blocker",
                "passed": False,
                "evidence": _reported_blocker_evidence(blocker),
            }
        )
    if outcome == "blocked" and resumed_input:
        persisted_blocker = resumed_input.get("blocker")
        blocker = (
            dict(persisted_blocker)
            if isinstance(persisted_blocker, dict)
            else {"blocker_id": resumed_input.get("blocker_id")}
        )
    distribution = str(package_name or "").strip()
    module = str(import_name or package_name or "").strip()
    if kind == "python_install" and not resumed_input:
        if not distribution or not module:
            # 判决拆除·第三波（oc:156 降格，2026-09-02）：与同函数 O6 先例一致——
            # 缺 package_name/import_name 不再拒记收据，就是一条 passed=False 的
            # check；success 由下游机械降级并披露。
            verified.append(
                {
                    "name": "package_identity_declared",
                    "passed": False,
                    "evidence": {
                        "reason": "python_install receipt declared no package_name/import_name",
                        "package_name": distribution or None,
                        "import_name": module or None,
                    },
                }
            )
    if kind == "python_install" and not resumed_input and distribution and module:
        try:
            version = importlib.metadata.version(distribution)
            verified.append(
                {
                    "name": "package_metadata",
                    "passed": expected_version in (None, "", version),
                    "evidence": {
                        "package": distribution,
                        "version": version,
                        "expected_version": expected_version or None,
                    },
                }
            )
        except importlib.metadata.PackageNotFoundError:
            verified.append(
                {
                    "name": "package_metadata",
                    "passed": False,
                    "evidence": {"package": distribution, "error": "not installed"},
                }
            )
        try:
            __import__(module)
            verified.append(
                {
                    "name": "python_import",
                    "passed": True,
                    "evidence": {"module": module, "python": sys.executable},
                }
            )
        except Exception as exc:
            verified.append(
                {
                    "name": "python_import",
                    "passed": False,
                    "evidence": {"module": module, "error": f"{type(exc).__name__}: {exc}"},
                }
            )
    persisted_evidence = resumed_input.get("evidence_files") if resumed_input else None
    if isinstance(persisted_evidence, list) and all(
        isinstance(item, dict) and str(item.get("path") or "").strip()
        for item in persisted_evidence
    ):
        evidence_files = [dict(item) for item in persisted_evidence]
    elif resumed_input and closure["inventory"]["raw_results"]:
        evidence_files, migration_error = _evidence_files_from_owned_raw_draft(
            closure["inventory"]["raw_results"][0]
        )
        if migration_error:
            return {
                "status": "error",
                "error_code": "operation_closure_reconciliation_required",
                "error": migration_error,
                "closure_id": closure_id,
                "blocker": {
                    "kind": "operation_closure_reconciliation_required",
                    "suggested_owner": "framework",
                    "node_action": "reconcile_owned_raw_draft",
                },
            }
        evidence_files = evidence_files or []
    else:
        evidence_files = _normalize_evidence_files(artifact_paths, executable_paths, json_paths)
    evidence_paths = [str(item["path"]) for item in evidence_files]
    # 判决拆除 O6（oc:136/138 降格，2026-08-31，随 origin/main 合入）：缺证据不再
    # 拒绝记收据 —— 同函数已有 verified{passed,evidence} 机制，缺证据就是一条
    # passed=False 的 check，如实进收据。
    # managed_job_ids_declared（oc:136 降格产物）已下移：F9 修复后 refs 由账本
    # 自动派生，「未声明」不再等于「无身份」；只有派生与收养后仍无身份时才记账
    # （见下方 has_managed_job_evidence 为假的分支）。
    if kind in {"generic", "build", "file_delivery"} and not evidence_paths:
        verified.append({"name": "evidence_paths_present", "passed": False,
                         "evidence": {"reason": "no stdout/stderr, build log, or deliverable artifact_paths declared"}})
    fresh_path_checks = [
        _path_check(
            str(item["path"]),
            executable=item.get("executable") is True,
            json_file=item.get("json_file") is True,
            declared_kind=str(item.get("declared_kind") or "file"),
        )
        for item in evidence_files
    ]
    if not resumed_input:
        verified.extend(fresh_path_checks)
        # ``toolchain_build`` normalizes to ``build`` so a local, direct
        # executable can close without a scheduler. Once the caller supplies
        # managed job identity, however, that identity is not advisory: use
        # the same authoritative terminal verification and route projection
        # as an explicit ``external_job`` closure. Otherwise a successful
        # build closure would silently discard its exact refs, while
        # finalize_external_job correctly refuses the frozen log later.
        # F9 修复：closure refs 的权威来源是本 run 的受管提交账本，不是调用方
        # 手抄参数。两级枚举 —— 本 run 提交（信封 run_id + lifecycle 过滤）为主；
        # 零本 run 提交且调用方未声明时，单例收养未决 workflow（continuation run
        # 主场景）。调用方声明降级为增选/交叉确认，不能缩小自动派生集合。
        own_jobs, excluded_jobs, ledger_errors = _own_run_managed_submissions(state)
        own_refs = [_external_job_reference(record) for record in own_jobs]
        effective_refs = [
            dict(item) for item in (external_job_refs or []) if isinstance(item, dict)
        ]
        if own_refs:
            effective_refs = effective_refs + own_refs
        elif not effective_refs and not list(job_ids or []):
            adoption = _adopt_unresolved_workflow_refs(state)
            if not adoption.get("ok"):
                return {
                    "status": "error",
                    "error_code": adoption.get("error_code"),
                    "error": adoption.get("error"),
                    "candidates": adoption.get("candidates"),
                }
            effective_refs = list(adoption.get("refs") or [])
        has_managed_job_evidence = bool(job_ids or effective_refs)
        # 判决拆除 O6（oc:136 降格，2026-09-03 落地）：账本与收养都为空、调用方也
        # 未声明身份时不进硬校验 —— 上方 managed_job_ids_declared 已记 passed=False，
        # success 会被机械降 partial 并披露。有身份之后的 identity/success 真 B
        # 校验一条不动；terminal 墙按 F9 评审收窄为 success-only（blocked/failed
        # 记 passed=False check 如实进收据，运行中作业的诚实收据合法保留）。
        if has_managed_job_evidence:
            job_verification = _managed_external_job_verification(state, job_ids, effective_refs)
            if not job_verification.get("ok"):
                return {
                    "status": "error",
                    "error_code": job_verification.get("error_code"),
                    "error": job_verification.get("error"),
                    "details": job_verification,
                }
            resolved_external_job_refs = list(job_verification.get("external_job_refs") or [])
            terminal_check = {
                "name": "external_jobs_terminal",
                "passed": bool(job_verification.get("terminal")),
                "evidence": {
                    "external_job_refs": resolved_external_job_refs,
                    "health": job_verification.get("health") or [],
                },
            }
            verified.append(terminal_check)
            if not terminal_check["passed"] and outcome == "success":
                health_rows = job_verification.get("health") or []
                probe_failed = any(
                    row.get("health_status") != "success" for row in health_rows
                )
                return {
                    "status": "error",
                    "error_code": "external_jobs_not_terminal",
                    "error": (
                        (
                            "受管 external job 的调度器探针失败（不可达或身份不可证），"
                            "终态未知；不能记录 success。出口：改记 outcome=blocked"
                            "（refs 照样冻结进 closure，供恢复后 finalize）、修复探针后"
                            "重试，或走 reconcile_external_submission 对账。"
                        )
                        if probe_failed
                        else (
                            "受管 external job 的终态读不出（调度器没有给出运行或结束的"
                            "结论）；不能记录 success——这既不说明它还在跑，也不说明它已结束。出口：稍后重读后"
                            "重试，或改记 outcome=blocked（refs 照样冻结进 closure）。"
                        )
                        if any(row.get("scheduler_phase") == "unknown" for row in health_rows)
                        else (
                            "受管 external job 仍在运行；不能以 success 掩盖开放作业。"
                            "出口：等待终态后重试，或改记 outcome=blocked（refs 照样"
                            "冻结进 closure），交由跨 session 恢复收尾。"
                        )
                    ),
                    "checks": [terminal_check],
                }
            success_check = {
                "name": "external_jobs_successful",
                "passed": bool(job_verification.get("successful")),
                "evidence": {
                    "external_job_refs": resolved_external_job_refs,
                    "health": job_verification.get("health") or [],
                },
            }
            verified.append(success_check)
            planned_rows = [
                row for row in (job_verification.get("health") or [])
                if isinstance(row, dict)
                and (row.get("success_evidence") or {}).get("source") == "planned_stop_cancellation"
            ]
            if planned_rows:
                # 强制如实记账：这些作业是按任务要求中途停下的，不是自己跑完的（D07）。
                verified.append({
                    "name": "external_job_stopped_as_planned",
                    "passed": True,
                    "evidence": {"jobs": [
                        {"reference": row.get("reference"),
                         **{key: (row.get("success_evidence") or {}).get(key) for key in (
                             "task_quote", "cancellation_intent_artifact_id",
                             "cancellation_outcome_artifact_id", "lifecycle_artifact_id")}}
                        for row in planned_rows
                    ]},
                })
            if outcome == "success" and not success_check["passed"]:
                unreadable_kinds = sorted({
                    str(row.get("state_observation"))
                    for row in (job_verification.get("health") or [])
                    if isinstance(row, dict) and row.get("state_observation")
                })
                guidance = _completion_postcondition_failure_guidance(
                    job_verification,
                    blocked_action=(
                        '调用 record_operation_completion(outcome="blocked")'
                    ),
                )
                return {
                    "status": "error",
                    "error_code": "external_job_success_unverified",
                    **(
                        guidance
                        or {
                            "error": (
                                "受管 external job 的状态读不出"
                                f"（{'、'.join(unreadable_kinds)}）：既不能说它"
                                "已结束，也没有成功证据；不能记录 success"
                                if unreadable_kinds
                                else
                                "external job 虽已终止，但没有可验证的成功证据，"
                                "或退出码/生命周期表明失败或取消；不能记录 success"
                            )
                        }
                    ),
                    "checks": [terminal_check, success_check],
                }
            if outcome == "success":
                route_projection = _project_verified_external_jobs(state, job_verification)
                if not route_projection.get("ok"):
                    return {
                        "status": "error",
                        "error_code": route_projection.get("error_code"),
                        "error": route_projection.get("error"),
                        "external_route_projection": route_projection,
                    }
            if adoption and adoption.get("adopted"):
                verified.append({
                    "name": "external_workflow_adopted", "passed": True,
                    "evidence": {"external_job_refs": resolved_external_job_refs,
                                 "workflow_status": adoption.get("workflow_status")},
                })
        elif kind == "external_job":
            # oc:136 判决保持：账本、收养、调用方三路都无身份时如实记账，
            # success 由机械降 partial 接管，不立墙。
            verified.append({
                "name": "managed_job_ids_declared", "passed": False,
                "evidence": {"reason": (
                    "external_job receipt declared no managed job_ids and none "
                    "were derivable from this run's submission ledger or "
                    "unresolved workflows"
                )},
            })
        if ledger_errors:
            # 账本降级不是拒绝理由（BF-12：agent 修不了账本）；如实记 witness，
            # success 由既有机械降 partial 接管。
            verified.append({
                "name": "external_job_ledger_degraded", "passed": False,
                "evidence": {"entries": ledger_errors},
            })
        if excluded_jobs:
            state.append_transcript(
                "operation_external_jobs_excluded",
                count=len(excluded_jobs),
                entries=excluded_jobs,
            )

    # external success 必须先由上面的机械 verifier 写入 execution receipt，
    # 再像所有其他 operation 一样接受同一个 route-complete 硬门。这里没有
    # in_progress 特例，也不依赖尚未产生的 experiment_log/finalize 归档。
    route_verification = _route_completion_verification(state)
    route_incomplete_at_closure: dict[str, Any] | None = None
    if outcome == "success":
        if not route_verification.get("ok"):
            return {
                "status": "error",
                "error_code": route_verification.get("error_code"),
                "error": route_verification.get("error"),
                "execution_route": route_verification,
            }
        expected_route_check = route_verification.get("check")
    elif route_verification.get("ok"):
        expected_route_check = route_verification.get("check")
    else:
        # F11（2026-09-06 平台 E2E 活体撞出）：「failed/blocked 不要求路线 complete」
        # 是既有判决（test_failed_and_blocked_completion_do_not_require_route_complete），
        # 保留不动 —— 但从前连一条 check 都不记，于是「一个步骤失败就收尾」铸出的
        # 冻结件看起来像一次走完了的失败，而 closure 一封口同一 run 内再不可改判
        # （closure_id 恒为 <run_id>:operation，身份三元组变了即 conflict，无重开通道）。
        # 收尾时路线还剩哪些步骤没跑是**事实**：如实记账，出口写进返回值（BF-12），
        # 不新立墙。
        route_incomplete_at_closure = {
            "route_state": route_verification.get("route_state"),
            "ready_step_ids": route_verification.get("ready_step_ids") or [],
            "route_reason": route_verification.get("route_reason"),
            "error_code": route_verification.get("error_code"),
        }
        expected_route_check = {
            "name": _ROUTE_COMPLETION_CHECK,
            "passed": False,
            "evidence": {
                "reason": ("operation closed while the canonical route still had "
                           "unexecuted steps"),
                **route_incomplete_at_closure,
            },
        }
    if resumed_input:
        expected_route_checks = [expected_route_check] if expected_route_check else []
        if existing_route_checks != expected_route_checks:
            return {
                "status": "error",
                "error_code": "operation_closure_conflict",
                "error": (
                    "部分 closure 的冻结 route 绑定与当前 canonical route 不一致，不能续写。"
                ),
                "execution_route": route_verification,
            }
    elif expected_route_check is not None:
        verified.append(expected_route_check)

    # Existing complete/partial closures are historical immutable facts and
    # remain readable/resumable after the census contract is introduced.
    # For a new closure, evaluate physical execution only after managed-job
    # verification has projected the exact attempt into the canonical route;
    # evaluating earlier would deadlock a genuine submitted build in its
    # pre-verification ``in_progress`` projection.
    if closure["kind"] == "empty":
        real_execution = _real_execution_obligation(
            state,
            task_kind=kind,
            outcome=outcome,
            declared_paths=[
                *[str(item) for item in (artifact_paths or [])],
                *[str(item) for item in (executable_paths or [])],
                # 对抗审查（09-21）：json_paths 原来只冻结成证据、从不归属——同一个假
                # 文件走 artifact_paths 被拒、走 json_paths 就过。
                *[str(item) for item in (json_paths or [])],
            ],
        )
        if not real_execution.get("passed"):
            if real_execution.get("status") == "build_artifact_not_produced_by_satisfying_attempt":
                # P0a v4：声明的 build 产物不在任何一次满足义务的 attempt 收尾时冻结的
                # 产物身份收据里，或此刻的身份已与收据不同——要么是路线之外（之前或
                # 之后）由 shell / Python / safe_write_file 写出、ln / ln -s / mv 顶替
                # 的，要么产出它的步骤没把它写进 expected_outputs。不看工具名，只看归属。
                return {
                    "status": "error",
                    "error_code": "operation_build_artifact_not_produced_by_satisfying_attempt",
                    "error": (
                        "声明的 build 产物没有满足真实执行义务的 attempt 的产物身份收据"
                        "（收据在该 attempt 收尾时冻结：lexical 路径、kind、inode、sha256），"
                        "或此刻的身份与收据不同；路线之外写出、事后顶替的文件不能充当构建结果"
                    ),
                    "unattributed_paths": real_execution.get("unattributed_paths"),
                    "producer_receipts": real_execution.get("producer_receipts"),
                    "real_execution_obligation": real_execution,
                    "retryable_in_current_run": True,
                    "payload_must_not_rerun": False,
                    "next_action": (
                        "若产物应由构建产生：把它写进产出它的 route 步骤的 expected_outputs"
                        "（相对该步骤 workdir_role 的路径），真实执行该步骤（同一 attempt 内"
                        "覆盖旧文件也算），再用相同 completion 输入重试；unattributed_paths "
                        "里的 reason 说明是没有收据、身份被换还是收据被截断（后者请收窄声明）。"
                        + (
                            "若本 run 确实只是写文件，请以 task_kind=file_delivery 或 generic "
                            "收尾，不要声明为 build。"
                            # 对抗审查（09-21）：toolchain_build run 只兼容 build/external_job，
                            # 这条出口在那里会撞 operation_task_kind_mismatch——不说走不通的路。
                            if _OPERATION_TASK_KIND_COMPATIBILITY.get(
                                str(real_execution.get("operation_kind") or "")) is None
                            else "本 run 的 operation category 只兼容 "
                            + "/".join(sorted(_OPERATION_TASK_KIND_COMPATIBILITY[
                                str(real_execution.get("operation_kind") or "")]))
                            + "，不能改用其他 task_kind 收尾；产物不是构建产生的就以 "
                              "outcome='blocked' 或 'failed' 如实收尾。"
                        )
                    ),
                }
            if real_execution.get("status") == "operation_task_kind_mismatch":
                mismatch = dict(real_execution["task_kind_mismatch"])
                return {
                    "status": "error",
                    "error_code": "operation_task_kind_mismatch",
                    "error": (
                        "task_kind 与本 run 不可变的 operation category 不兼容；"
                        "调用方字段不能降低真实执行义务"
                    ),
                    "task_kind_mismatch": mismatch,
                    "real_execution_obligation": real_execution,
                    "retryable_in_current_run": True,
                    "payload_must_not_rerun": True,
                    "do_not_retry_payload": True,
                    "next_action": {
                        "owner": "experiment",
                        "action": "retry_operation_completion",
                        "model_callable": True,
                        "compatible_task_kinds": mismatch[
                            "compatible_task_kinds"
                        ],
                    },
                }
            return {
                "status": "error",
                "error_code": "operation_real_execution_required",
                "error": (
                    "不能用只读诊断或 dry-run 物化结果证明真实 build 已完成；"
                    "当前 action census 没有权威的真实执行收据"
                ),
                "real_execution_obligation": real_execution,
                "next_action": (
                    "若任务仍需成功：声明并实际执行 mechanical build route，待其终态"
                    "与输出完成核验后，用相同 completion 输入重试；若无法执行，"
                    "请如实记录 blocker，并以 outcome='blocked' 收尾。"
                ),
            }

    # 判决拆除 O6（oc:138 降格，2026-08-31）：缺证据路径不再拒绝记收据 —— 上方
    # evidence_paths_present 已记成一条 passed=False 的 check，而「success 但验证
    # 未通过」由下方机械降 partial 接管。这里不再另立一堵墙。
    if not verified:
        # 判决拆除 O6（oc:170 降格）：零可验证项本身作为一条 passed=False 的
        # check 如实进收据，不再拒绝登记。
        verified.append({"name": "verification_evidence_present", "passed": False,
                         "evidence": {"reason": "no verifiable checks were supplied or derivable"}})
    failed_checks = [item for item in verified if not item["passed"]]
    requested_outcome = outcome
    outcome_demoted_from = None
    if outcome == "success" and failed_checks:
        # 判决拆除跨批依赖 §3（2026-08-31）：「outcome=success 但验证未通过」是
        # 真 B（伪造成功），但不在这里新造拒绝墙 —— 机械降 outcome=partial 并
        # 披露，账本永远如实写明哪些检查没过。
        outcome_demoted_from = "success"
        outcome = "partial"
    if outcome not in {"success", "partial"}:
        # 判决拆除（oc:176 next_step 字数子句删；oc:178 删 —— 收据 outcome=blocked
        # 本身就是 blocker 声明，重复抄件）。非成功收据的证据完备性不再是拒绝
        # 理由，未过项如实进 checks，referee 终审。
        if not failed_checks:
            verified.append({"name": "failure_evidence_present", "passed": False,
                             "evidence": {"reason": "non-success outcome carried no failed check"}})
        elif any(not _has_evidence(item.get("evidence")) for item in failed_checks):
            verified.append({"name": "failure_evidence_substantiated", "passed": False,
                             "evidence": {"reason": "at least one failed check carried no evidence"}})
        if len(next_step) < 8:
            verified.append({"name": "next_step_actionable", "passed": False,
                             "evidence": {"reason": "next_step was empty or too short to act on"}})
        failed_checks = [item for item in verified if not item["passed"]]

    status = "completed" if outcome == "success" else outcome
    closure_input = (
        dict(resumed_input)
        if resumed_input
        else {
            "task_kind": kind,
            "objective": objective,
            # outcome 存**生效值**：收据、clean_results 与 status 必须一致，
            # 否则下游 operation_log_contract 会判「outcome 与收据不符」。
            "outcome": outcome,
            # 请求值另存，供幂等重试的身份比对使用（见上方 frozen_identity）。
            "requested_outcome": requested_outcome,
            **({"outcome_demoted_from": outcome_demoted_from,
                "demoted_by_failed_checks": [item["name"] for item in failed_checks]}
               if outcome_demoted_from else {}),
            "checks": verified,
            "next_step": next_step or None,
            "summary": str(summary or "").strip() or None,
            "blocker_id": ((blocker or {}).get("blocker_id") or (blocker or {}).get("id")),
            "blocker": blocker,
            "external_job_refs": resolved_external_job_refs,
            **({"external_jobs_excluded": excluded_jobs} if excluded_jobs else {}),
            # 未收养、只披露的遗留作业：不作 check——它不是本 run 的失败，作 check 会把
            # success 机械降成 partial。
            **({"disclosed_unreadable_jobs": list(adoption["disclosed_unreadable_jobs"])}
               if adoption and adoption.get("disclosed_unreadable_jobs") else {}),
            "evidence_files": evidence_files,
            "upstream_goal_effect": "operational_subtask_only",
            "scientific_contribution": "none",
            "execution_intent_binding": intent_binding,
        }
    )
    metadata = {
        "record_kind": "operation",
        "status": status,
        "operation_closure_owner": _CLOSURE_OWNER,
        "operation_closure_id": closure_id,
        "operation_closure_version": _CLOSURE_SCHEMA_VERSION,
        "operation_closure_input": closure_input,
        "upstream_goal_effect": "operational_subtask_only",
        "scientific_contribution": "none",
        "operation_child_obligation_version": _CHILD_OBLIGATION_SCHEMA_VERSION,
        "execution_intent_binding": intent_binding,
        "route_correction_witness_limitations": (
            correction_disclosure.get("limitations") or []
        ),
    }
    if resolved_external_job_refs:
        metadata["external_job_refs"] = resolved_external_job_refs
    inventory = closure["inventory"]
    raw_id = str(inventory["raw_results"][0]["id"]) if inventory["raw_results"] else ""
    raw_frozen_already = bool(
        inventory["raw_results"]
        and _closure_metadata(inventory["raw_results"][0]).get("frozen") is True
    )
    if not raw_id:
        raw_dir = Path(state.root) / "operation_evidence"
        raw_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = raw_dir / f"operation_receipt_{state.run_id}.json"
        receipt = {
            "schema_version": 2,
            "closure_id": closure_id,
            "record_kind": "operation",
            **closure_input,
            "blocker": blocker,
        }
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        raw_files = [_file_entry(receipt_path, "operation_verification_receipt")]
        for item in evidence_files:
            path = Path(str(item["path"])).expanduser()
            if path.is_file() and str(path.resolve()) != str(receipt_path.resolve()):
                raw_files.append(_file_entry(path, str(item["role"])))
        raw_name = (
            str(inventory["raw_results"][0].get("name") or "") if inventory["raw_results"] else ""
        ) or f"operation_raw_{state.run_id}"
        raw_saved = state.save_artifact(
            "raw_results",
            raw_name,
            json.dumps(
                {"record_kind": "operation", "closure_id": closure_id, "files": raw_files},
                ensure_ascii=False,
                indent=2,
            ),
            metadata=metadata,
        )
        raw_id = str(raw_saved["id"])
    elif not raw_frozen_already:
        repaired_content, repair_error = _coalesced_owned_raw_content(inventory["raw_results"][0])
        if repair_error:
            return {
                "status": "error",
                "error_code": "operation_closure_reconciliation_required",
                "error": repair_error,
                "closure_id": closure_id,
            }
        if repaired_content is not None:
            raw_name = str(
                inventory["raw_results"][0].get("name") or f"operation_raw_{state.run_id}"
            )
            raw_saved = state.save_artifact(
                "raw_results", raw_name, repaired_content, metadata=metadata
            )
            raw_id = str(raw_saved["id"])
    raw_frozen = (
        {"status": "success", "already_frozen": True}
        if raw_frozen_already
        else await _freeze_artifact(state, raw_id, "freeze immutable operation source evidence")
    )
    if raw_frozen.get("status") != "success":
        return _freeze_failure(
            raw_frozen,
            closure_id=closure_id,
            artifact_type="raw_results",
            artifact_ids={"raw_results_artifact_id": raw_id},
        )

    clean_id = str(inventory["clean_results"][0]["id"]) if inventory["clean_results"] else ""
    clean_frozen_already = bool(
        inventory["clean_results"]
        and _closure_metadata(inventory["clean_results"][0]).get("frozen") is True
    )
    if not clean_id:
        clean_payload = {
            "schema_version": 2,
            "closure_id": closure_id,
            "record_kind": "operation",
            "status": status,
            "upstream_goal_effect": "operational_subtask_only",
            "scientific_contribution": "none",
            "execution_intent_binding": intent_binding,
            "reason": f"operation/{kind}: this run produced operational evidence, not a hypothesis-level scientific conclusion.",
            "raw_results_artifact_id": raw_id,
            "verification": {"passed": outcome == "success", "checks": verified},
            "task_kind": kind,
            "objective": objective,
            "outcome": outcome,
            "next_step": next_step or None,
            "blocker_id": closure_input["blocker_id"],
        }
        clean_name = (
            str(inventory["clean_results"][0].get("name") or "")
            if inventory["clean_results"]
            else ""
        ) or f"operation_clean_{state.run_id}"
        clean_saved = state.save_artifact(
            "clean_results",
            clean_name,
            json.dumps(clean_payload, ensure_ascii=False, indent=2, default=str),
            metadata=metadata,
        )
        clean_id = str(clean_saved["id"])
    clean_frozen = (
        {"status": "success", "already_frozen": True}
        if clean_frozen_already
        else await _freeze_artifact(state, clean_id, "freeze normalized operation verification")
    )
    if clean_frozen.get("status") != "success":
        return _freeze_failure(
            clean_frozen,
            closure_id=closure_id,
            artifact_type="clean_results",
            artifact_ids={
                "raw_results_artifact_id": raw_id,
                "clean_results_artifact_id": clean_id,
            },
        )

    log_id = str(inventory["experiment_log"][0]["id"]) if inventory["experiment_log"] else ""
    log_frozen_already = bool(
        inventory["experiment_log"]
        and _closure_metadata(inventory["experiment_log"][0]).get("frozen") is True
    )
    if not log_id:
        log_name = (
            str(inventory["experiment_log"][0].get("name") or "")
            if inventory["experiment_log"]
            else ""
        ) or f"operation_log_{state.run_id}"
        log_saved = state.save_artifact(
            "experiment_log",
            log_name,
            _operation_log(
                objective=objective,
                task_kind=kind,
                status=status,
                raw_id=raw_id,
                clean_id=clean_id,
                checks=verified,
                next_step=next_step,
                summary=str(summary or "").strip(),
            ),
            metadata=metadata,
        )
        log_id = str(log_saved["id"])
    log_frozen = (
        {"status": "success", "already_frozen": True}
        if log_frozen_already
        else await _freeze_artifact(state, log_id, "freeze canonical operation log")
    )
    if log_frozen.get("status") != "success":
        return _freeze_failure(
            log_frozen,
            closure_id=closure_id,
            artifact_type="experiment_log",
            artifact_ids={
                "raw_results_artifact_id": raw_id,
                "clean_results_artifact_id": clean_id,
                "experiment_log_artifact_id": log_id,
            },
        )
    child_projection = operation_child_obligation_projection(state)
    child_obligation = child_projection.get("child_obligation")
    child_delivery = (
        {
            "upstream_goal_effect": child_obligation["upstream_goal_effect"],
            "scientific_contribution": child_obligation["scientific_contribution"],
            "child_obligation": child_obligation,
        }
        if isinstance(child_obligation, dict)
        else {"child_obligation_projection": child_projection}
    )
    # outcome_demoted_from 必须一路披露到 transcript 与返回值 —— 机械降级不是
    # 静默修正，调用方要能看见「你报的 success 被降成了 partial，因为这些 check」。
    _demoted = ({"outcome_demoted_from": outcome_demoted_from}
                if outcome_demoted_from else {})
    # F11：路线没走完就收尾 —— 已经如实记进 check，但返回值还必须把**代价与出口**
    # 说清楚：本 run 的 closure 就此封口且不可改判，想重试剩余步骤只能开 continuation
    # run。不说的话，调用方（活体实测）会以为还能在同一 run 里重跑，然后撞死在
    # operation_closure_conflict 上，把一次步骤失败变成整个 run 的死锁。
    _route_gap = {}
    if route_incomplete_at_closure:
        _route_gap = {
            "route_incomplete_at_closure": route_incomplete_at_closure,
            # 出口必须是**调用方真做得到**的事（BF-12）：开 continuation run 不是
            # 节点能力 —— resume_run 只有人手 CLI 入口（无 register_tool），平台侧
            # 的 continuation session 由人在 UI 发起。所以这里如实说明「本 run 到此
            # 为止、剩下的要人来开新 run」，而不是指一条节点走不通的路。
            "recovery": (
                "本次收尾时 canonical route 还有未执行步骤"
                f"（ready_step_ids={route_incomplete_at_closure.get('ready_step_ids')}）。"
                f"closure {closure_id} 已封口，本 run 到此为止：同一 run 内不能改判"
                " outcome、不能开第二个 closure（closure_id 恒为 <run_id>:operation）、"
                "也不能再修订路线重试（封口后路线内容变更被 "
                "execution_route_sealed_by_operation_closure 拒绝）。你（节点）没有开新"
                " run 的工具，能做且该做的是：把「哪些步骤没跑完、为什么、下一步需要什么」"
                "如实报告给发起方，由人开 continuation run 继续。不要手工改写或补冻结三件套。"
            ),
            "suggested_owner": "run_owner",
            "node_action": "report_unfinished_route_and_stop",
            "retryable_in_this_run": False,
        }
        state.append_transcript(
            "operation_closed_with_incomplete_route",
            closure_id=closure_id,
            outcome=outcome,
            **route_incomplete_at_closure,
        )
    state.append_transcript(
        "operation_completion_recorded",
        raw_results_artifact_id=raw_id,
        clean_results_artifact_id=clean_id,
        experiment_log_artifact_id=log_id,
        closure_id=closure_id,
        task_kind=kind,
        outcome=outcome,
        failed_checks=[item["name"] for item in failed_checks],
        **child_delivery,
        **_demoted,
    )
    return {
        "status": "success",
        "artifact_id": log_id,
        "experiment_log_artifact_id": log_id,
        "raw_results_artifact_id": raw_id,
        "clean_results_artifact_id": clean_id,
        "closure_id": closure_id,
        "outcome": outcome,
        "checks": verified,
        "failed_checks": [item["name"] for item in failed_checks],
        **child_delivery,
        **_demoted,
        **_route_gap,
        **({"disclosed_unreadable_jobs": list(closure_input["disclosed_unreadable_jobs"])}
           if closure_input.get("disclosed_unreadable_jobs") else {}),
    }


def audit_operation_completion(state: Any) -> dict[str, Any]:
    """Compatibility entry: operation completion is now the frozen evidence triplet."""
    try:
        from .contract_audit import audit_operation_log_contract
    except ImportError:
        from tools.contract_audit import audit_operation_log_contract
    return audit_operation_log_contract(state)


register_tool(
    ToolDefinition(
        name="verify_external_job_execution",
        description=(
            "After job_status or wait_for_external_job reports a terminal managed job, "
            "verify its exact scheduler/container evidence and every declared health/route "
            "completion postcondition. When all pass, persist the route-success receipt. "
            "When route expected_outputs are missing, persist that failed observation and "
            "return the exact correction or report_blocker→blocked exit; a health failure "
            "never unlocks a dependent step. Use this before a dependent declared submit_job step. "
            "It does not freeze operation artifacts, close the external workflow, or replace "
            "record_operation_completion/finalize_external_job."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": ["local", "slurm", "pbs", "kubernetes"]},
                "job_id": {"type": "string"},
                "namespace": {"type": "string"},
            },
            "required": ["scheduler", "job_id"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _verify_external_job_execution,
)


register_tool(
    ToolDefinition(
        name="record_operation_completion",
        description=(
            "Create and freeze the complete non-scientific Experiment evidence triplet: "
            "raw_results：immutable execution evidence；clean_results：normalized verification； "
            "and the canonical experiment_log. Use after package installation, build/min-run, "
            "file delivery, external-job completion, or another direct operation. External jobs "
            "are re-checked against managed submission/scheduler state and copied into the log as "
            "scope-exact external_job_refs before finalize_external_job. "
            "ONE-WAY DOOR: this seals the single closure of this run (closure_id is "
            "<run_id>:operation). Afterwards the same run cannot change outcome, cannot open a "
            "second closure, and has no re-open path — a later call with a different "
            "task_kind/objective/outcome is rejected as operation_closure_conflict. So do NOT "
            "call it merely because one route step failed: if the canonical route still has "
            "ready steps you intend to retry, fix and re-run those steps FIRST — sealing also "
            "blocks route amendment, which is the only way to re-arm a failed step. Closing over "
            "an unfinished route is allowed (recorded honestly, not refused), but it ends the "
            "run for good: you have no tool to start another one, so the rest would need a "
            "human to open a continuation run."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "task_kind": {"type": "string", "enum": sorted(_TASK_KIND_INPUTS)},
                "objective": {"type": "string", "minLength": 1},
                "outcome": {"type": "string", "enum": sorted(_OUTCOMES), "default": "success"},
                "package_name": {"type": "string"},
                "import_name": {"type": "string"},
                "expected_version": {"type": "string"},
                "artifact_paths": {"type": "array", "items": {"type": "string"}},
                "executable_paths": {"type": "array", "items": {"type": "string"}},
                "json_paths": {"type": "array", "items": {"type": "string"}},
                "job_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "external_job_refs": {
                    "type": "array",
                    "description": (
                        "Optional scope-exact managed job references. Required when the same job_id "
                        "exists in more than one scheduler namespace/host/cluster."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "scheduler": {"type": "string", "minLength": 1},
                            "job_id": {"type": "string", "minLength": 1},
                            "namespace": {"type": "string"},
                            "launch_host": {"type": "string"},
                            "scheduler_cluster": {"type": "string"},
                            "resource_uid": {"type": "string"},
                            "submission_nonce": {"type": "string"},
                            "container_runtime_id": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                                "description": "Required immutable Docker ID for local managed jobs.",
                            },
                            "route_attempt_id": {"type": "string"},
                        },
                        "required": ["scheduler", "job_id"],
                    },
                },
                "blocker_id": {
                    "type": "string",
                    "description": "For outcome=blocked: report_blocker result.blocker.blocker_id from this run.",
                },
                "checks": {
                    "type": "array",
                    "description": (
                        "Each check must be {name: non-empty string, passed: boolean, evidence: non-empty value}. "
                        "For outcome=blocked, pass blocker_id from report_blocker; the tool records it as the failed check. "
                        "For outcome=failed, include passed=false with evidence; do not use status=failed."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "passed": {"type": "boolean"},
                            "evidence": {},
                        },
                        "required": ["name", "passed"],
                    },
                },
                "next_step": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["task_kind", "objective"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _record_operation_completion,
)

for _artifact_type in sorted(_OPERATION_CLOSURE_ARTIFACT_TYPES):
    register_save_gate(
        _artifact_type,
        _operation_closure_artifact_save_gate,
    )
