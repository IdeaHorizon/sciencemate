"""Resource discovery and scheduler submission helpers for experiment node.

This module intentionally stays generic: local, SLURM, PBS, and Kubernetes
are detected through their public CLIs. Cloud-provider pricing is not guessed;
callers may pass an explicit hourly rate when they have a real allocation.
"""
from __future__ import annotations

import hashlib

import asyncio
from collections import Counter
from copy import deepcopy
import json
import logging
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

from core.cancellation import RunCancelled
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

try:
    from ..task_prose_inputs import (
        TASK_PROSE_INPUT_KEYS,
        TASK_PROSE_INPUT_RECEIPT_EVENT,
        TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION,
        TaskProseSource,
        first_task_prose_source,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from task_prose_inputs import (
        TASK_PROSE_INPUT_KEYS,
        TASK_PROSE_INPUT_RECEIPT_EVENT,
        TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION,
        TaskProseSource,
        first_task_prose_source,
    )

try:
    from shared.tools.library.artifacts_extra import register_save_gate
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from tools.artifacts_extra import register_save_gate
try:
    from .build_resource_guard import (
        BuildLimits, active_build_resource_plan, derive_build_limits,
    )
    from .run_contract import (
        load_run_contract,
        record_actual_run_params,
        requires_experiment_fallback_input_gate,
        run_role_non_applicability_reason,
    )
    from .path_roles import (
        collect_path_roles, default_stage_workdir, experiment_output_dir,
        matching_path_roles,
        validate_path_roles,
    )
    from .mpi_runtime import mpi_runtime_remediation
    from .pbs_scheduler import (
        pbs_flavor,
        query_pbs_job,
        reset_pbs_flavor_cache_for_tests as _reset_pbs_flavor_cache_for_tests,
    )
    from .preflight import EXECUTION_STAGES
except ImportError:
    from tools.build_resource_guard import (
        BuildLimits, active_build_resource_plan, derive_build_limits,
    )
    from tools.run_contract import (
        load_run_contract,
        record_actual_run_params,
        requires_experiment_fallback_input_gate,
        run_role_non_applicability_reason,
    )
    from tools.path_roles import (
        collect_path_roles, default_stage_workdir, experiment_output_dir,
        matching_path_roles,
        validate_path_roles,
    )
    from tools.mpi_runtime import mpi_runtime_remediation
    from tools.pbs_scheduler import (
        pbs_flavor,
        query_pbs_job,
        reset_pbs_flavor_cache_for_tests as _reset_pbs_flavor_cache_for_tests,
    )
    from tools.preflight import EXECUTION_STAGES


# 调度器词表的唯一真相源（判决拆除·第三波「一题一答」，2026-09-02）：以前
# `_job_status_sync` / `_submit_job` / 五个工具 schema 各抄一份 {local,slurm,pbs,
# kubernetes}，现在 schema enum 与代码都从这里取；派发口按 schema 核值。
SCHEDULERS: tuple[str, ...] = ("local", "slurm", "pbs", "kubernetes")
SUBMIT_SCHEDULERS: tuple[str, ...] = ("auto", *SCHEDULERS)

_EXTERNAL_JOB_LIFECYCLE_TYPE = "external_job_lifecycle"
_EXTERNAL_JOB_OPERATION_CLOSURE_TYPE = "external_job_operation_closure"
_EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION = 2
_EXTERNAL_JOB_OPERATION_CLOSURE_KIND = "operation_finalization"
_LEGACY_OPERATION_CLOSURE_METADATA_REQUIRED = frozenset({
    "scheduler", "job_id", "outcome", "execution_class",
})
_LEGACY_OPERATION_CLOSURE_METADATA_ALLOWED = (
    _LEGACY_OPERATION_CLOSURE_METADATA_REQUIRED
    | {"class_unverified", "class_disputed"}
)
_OPERATION_BLOCKER_WITNESS_KIND = "independent_blockers_v1"
_EXTERNAL_JOB_SUBMISSION_RECOVERY_TYPE = "external_job_submission_recovery"
_EXTERNAL_JOB_CANCELLATION_INTENT_TYPE = "external_job_cancellation_intent"
_EXTERNAL_JOB_CANCELLATION_OUTCOME_TYPE = "external_job_cancellation_outcome"
_EXTERNAL_ROUTE_PROJECTION_RECORD_TYPE = "external_job_route_projection_record"
_UNKNOWN_ORPHAN_TYPE = "unknown_orphan"
_UNKNOWN_ORPHAN_RESOLUTION_TYPE = "unknown_orphan_resolution"
_SUBMISSION_RECORD_TYPES = ("job_submission", _EXTERNAL_JOB_SUBMISSION_RECOVERY_TYPE)
SUBMISSION_LEDGER_FAILED_CHECK = "job_submission_records_readable"
SUBMISSION_LEDGER_BLOCK_REASON_PREFIX = (
    "authoritative job_submission records unavailable: "
)


class SubmissionLedgerError(RuntimeError):
    """A current-run submission receipt cannot be trusted or reconciled."""


_MANAGED_EXTERNAL_ARTIFACT_TYPES = frozenset({
    "external_submission_intent",
    # 收养记录（unknown_dead 死亡证明）会释放活跨 run intent 的输出根保留，
    # 只能由 Python 层探活通道 record_intent_adoption 依据正面死亡证据写入
    # （state.save_artifact 不经过 save gate，不受影响）；模型面通用
    # save_artifact 铸造它等于零探活自发通行证，必须被门拦下。
    "external_submission_intent_adoption",
    "job_submission",
    "external_job_submission_recovery",
    "external_job_workflow",
    "external_job_identity_recovery_workflow",
    "external_job_lifecycle",
    "external_job_cancellation_intent",
    "external_job_cancellation_outcome",
    "execution_environment_evidence",
    _UNKNOWN_ORPHAN_TYPE,
    # 隔离解除记录：它一出现就让冲突扫描放行 orphan 锁死的输出根，等价于
    # 一张“输出根解锁通行证”。只能由 resolve_unknown_orphan 依据机械证据
    # （自述进程逐个确证已死 + 输出根自 recorded_at 起静默）铸造；模型面
    # 通用 save_artifact 铸造它等于零证据自我解锁，必须被门拦下。
    _UNKNOWN_ORPHAN_RESOLUTION_TYPE,
    # operation 型 external job 的终态收据：payload 是 finalize 门依据机械
    # 证据（exit code / completion path 快照 / error_evidence）铸造的事实，
    # 模型面通用 save_artifact 铸造它等于无证据自发放行输出根，必须被门拦下。
    _EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
    # "本次收尾没有投影任何路线终态"的事后追加记录：它是收尾真的走完的唯一
    # 强留痕，模型面伪造它等于凭空给自己开一张"投影已处理"的证明。
    _EXTERNAL_ROUTE_PROJECTION_RECORD_TYPE,
})


def _managed_external_artifact_save_gate(
    state: Any,
    draft: dict[str, Any],
) -> dict[str, Any]:
    """受管外部身份与生命周期 artifact 不能由通用 writer 铸造。"""
    del state, draft
    return {
        "failures": {
            "managed_external_artifact_owner": (
                "外部提交 intent、调度器身份、恢复/取消收据与环境证据只能由"
                "对应的受管工具根据机械执行结果创建"
            ),
        },
        "hint": (
            "使用 submit_job、reconcile_external_submission、job_status/"
            "finalize_external_job 或 cancel_job；不要用 save_artifact 伪造"
            "外部作业事实。悬空作业按出路选择：能证明归属就走 "
            "reconcile_external_submission/cancel_job/finalize_external_job；"
            "证不明归属只能 record_unknown_orphan —— 它是隔离并锁死其输出根，"
            "不是释放；解锁另走 resolve_unknown_orphan 的机械证据通道。"
        ),
    }
_ACTIVE_JOB_STATES = frozenset({"submitted", "running", "unknown"})
_ANALYZED_FINALIZE_OUTCOMES = frozenset({
    "analyzed_success", "analyzed_failure", "analyzed_inconclusive",
})
_OPERATION_FINALIZE_OUTCOMES = frozenset({
    "operation_completed", "operation_failed", "operation_blocked",
})
_OPERATION_EXECUTION_CLASSES = frozenset({"diagnostic", "toolchain_build"})
_CANCELLED_JOB_STATES = frozenset({"cancelled", "superseded"})
_CANCELLATION_IDENTITY_FIELDS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id", "process_start_ticks",
    "container_runtime_id",
)
# Immutable wire identity schema shared by operation closure v0/v1/v2.
# Never extend this tuple
# when the broader cancellation identity evolves; introduce a new receipt
# schema version and migration instead.
try:
    from . import output_postconditions as _output_postconditions
except ImportError:  # pragma: no cover - standalone node bootstrap.
    import tools.output_postconditions as _output_postconditions  # type: ignore

_OPERATION_CLOSURE_IDENTITY_FIELDS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id", "process_start_ticks",
    "container_runtime_id",
)
_HEALTH_CONTRACT_VERSION = 1
_DEFAULT_HEALTH_ERROR_PATTERNS = (
    "fatal error", "segmentation fault", "out of memory", "killed by",
    "cmake error", "traceback (most recent call last)",
    "HARNESS_IDENTITY_PREFLIGHT status=failed",
    "HARNESS_SANDBOX_LIMIT",
)
#: 平台自己写出的失败标记（沙箱限制、身份预检失败）。模型声明的 error_patterns 只替换启发式
#: 关键词，替换不掉这些——否则自带一份 error_patterns，平台杀掉作业的证据就从 error_evidence
#: 里消失了。
_PLATFORM_HEALTH_ERROR_MARKERS = (
    "HARNESS_IDENTITY_PREFLIGHT status=failed",
    "HARNESS_SANDBOX_LIMIT",
)


def _health_error_markers(contract: dict[str, Any] | None) -> list[str]:
    declared = contract.get("error_patterns") if isinstance(contract, dict) else None
    markers = [str(item) for item in (declared or _DEFAULT_HEALTH_ERROR_PATTERNS)]
    present = {item.casefold() for item in markers}
    markers.extend(item for item in _PLATFORM_HEALTH_ERROR_MARKERS
                   if item.casefold() not in present)
    return markers
# 本地沙箱的 walltime 兜底：这不是调度器契约（SLURM/PBS 省略 walltime 时必须
# 不落指令、记 site_default_unknown），只是无人给出期限时本地沙箱的平台安全
# 上限，来源在 sandbox_contract.walltime_source 里如实标为 platform_safety_default。
_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES = 60
_LOCAL_PIDS_EVENTS_PATH = "/sys/fs/cgroup/pids.events"

def _walltime_for_a_human(scheduler: str, walltime_minutes: int | None) -> str:
    """批准卡上那一行「最多跑多久」。

    三种局面要分清，因为对人的含义完全不同：
      · 显式给了 → 就是这个数；
      · 本地没给 → 平台按安全上限杀，把那个数**和它的来历**一起说出来；
      · 调度器没给 → 站点默认，我们这边确实不知道，就说不知道（别编一个）。
    """
    if walltime_minutes is not None:
        return f"{walltime_minutes} 分钟（显式指定）"
    if scheduler == "local":
        return (f"{_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES} 分钟"
                "（平台安全上限，到点杀进程；要更久请显式指定）")
    return "站点默认（未指定，本平台不知道该站点的上限）"


def the_local_walltime_seconds(
    walltime_minutes: int | None, hard_deadline_s: int | None
) -> int:
    """本地作业到点被杀的那个秒数 —— 这个问题只在这里回答一次。

    调用方给了就按给的算；没给才落到平台安全上限。这条规则此前在两处各写了
    一遍（sandbox_contract 里一次、SandboxLimits 里一次），两份必须永远相等，
    否则记录上写的期限和真正生效的期限会分叉。
    """
    if hard_deadline_s:
        return int(hard_deadline_s)
    minutes = (
        walltime_minutes
        if walltime_minutes is not None
        else _LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES
    )
    return int(minutes) * 60


_LOCAL_PIDS_LIMIT_EXIT_CODE = 138


def _job_key(
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    launch_host: str | None = None,
    scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None,
    process_group_id: str | None = None,
    process_start_ticks: int | str | None = None,
    container_runtime_id: str | None = None,
) -> str:
    """Canonical scheduler-scoped identity for every lifecycle consumer."""
    return json.dumps({
        "scheduler": str(scheduler or "").casefold(),
        "namespace": str(namespace or ""),
        "launch_host": str(launch_host or "").casefold(),
        "scheduler_cluster": str(scheduler_cluster or "").casefold(),
        "resource_uid": str(resource_uid or ""),
        "submission_nonce": str(submission_nonce or ""),
        "process_group_id": str(process_group_id or ""),
        "process_start_ticks": str(process_start_ticks or ""),
        "container_runtime_id": str(container_runtime_id or ""),
        "job_id": str(job_id or ""),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _job_key_for_record(record: dict[str, Any]) -> str:
    return _job_key(
        str(record.get("scheduler") or ""),
        str(record.get("job_id") or ""),
        record.get("namespace"),
        record.get("launch_host"),
        record.get("scheduler_cluster"),
        record.get("resource_uid"),
        record.get("submission_nonce"),
        record.get("process_group_id"),
        record.get("process_start_ticks"),
        record.get("container_runtime_id"),
    )


_EXTERNAL_JOB_HANDOFF_READY_KEY = "_external_job_handoff_ready"


def _mark_external_job_handoff_ready(
    state: Any,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    launch_host: str | None = None, scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None,
    container_runtime_id: str | None = None,
) -> bool:
    """Record that one bounded managed wait completed while the job stayed active."""
    scheduler_text = str(scheduler).lower()
    nonce = str(submission_nonce or "").strip()
    runtime_id = str(container_runtime_id or "")
    if scheduler_text == "local" and (
        not nonce or not re.fullmatch(r"[0-9a-f]{64}", runtime_id)
    ):
        # A local job name is reusable.  A legacy wait must not authorize a
        # different container that later acquires the same managed name.
        try:
            state.hook_state.pop(_EXTERNAL_JOB_HANDOFF_READY_KEY, None)
            state.append_transcript(
                "external_job_handoff_ineligible",
                scheduler=scheduler_text,
                job_id=str(job_id),
                reason="missing_submission_nonce_or_immutable_container_runtime_id",
            )
        except Exception:
            pass
        return False
    payload = {
        "scheduler": scheduler_text,
        "job_id": str(job_id),
        "namespace": namespace, "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster, "resource_uid": resource_uid,
        "submission_nonce": submission_nonce,
        "container_runtime_id": container_runtime_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        state.hook_state[_EXTERNAL_JOB_HANDOFF_READY_KEY] = payload
    except Exception:
        return False
    # The hook_state assignment is the durable permit.  Transcript telemetry is
    # best-effort and must not turn an already-recorded permit into a false
    # negative that invites another wait/identity transition.
    try:
        state.append_transcript("external_job_handoff_eligible", **payload)
    except Exception:
        pass
    return True


def external_job_handoff_ready(state: Any) -> dict[str, Any] | None:
    """Return the node-local permit created by an active bounded wait."""
    try:
        value = state.hook_state.get(_EXTERNAL_JOB_HANDOFF_READY_KEY)
    except AttributeError:
        return None
    return dict(value) if isinstance(value, dict) else None


def _external_wait_cancellation_signal(state: Any) -> dict[str, Any] | None:
    """Read the same sticky cancellation state used by the core tool choke point."""
    try:
        from core.cancellation import signal_for
        signal_value = signal_for(state)
        if signal_value:
            return signal_value
    except Exception:
        pass
    event = getattr(state, "kill_event", None)
    if event is not None:
        try:
            if event.is_set():
                return {"reason": "external job wait cancelled"}
        except Exception:
            pass
    return None


def _raise_if_external_wait_cancelled(state: Any) -> None:
    signal_value = _external_wait_cancellation_signal(state)
    if signal_value:
        from core.cancellation import RunCancelled
        raise RunCancelled("wait_for_external_job", signal_value)


async def _sleep_with_external_wait_cancellation(state: Any, delay_s: float) -> None:
    """Sleep in short bounded intervals so cancellation never waits for a poll."""
    deadline = time.monotonic() + max(0.0, delay_s)
    while True:
        _raise_if_external_wait_cancelled(state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


async def _probe_external_job_health_cancellable(
    state: Any,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
) -> dict:
    """Query scheduler health without making cancellation wait for its CLI timeout."""
    _raise_if_external_wait_cancelled(state)
    probe_task = asyncio.create_task(asyncio.to_thread(
        probe_external_job_health, state, scheduler, job_id, namespace,
    ))
    try:
        while True:
            done, _ = await asyncio.wait({probe_task}, timeout=1.0)
            if done:
                return probe_task.result()
            _raise_if_external_wait_cancelled(state)
    except BaseException:
        # ``to_thread`` cannot terminate a running scheduler CLI safely. Let the
        # read-only query finish in the background and consume a late exception,
        # while cancellation immediately returns control to the run lifecycle.
        if not probe_task.done():
            def _consume_late_probe(task: asyncio.Task) -> None:
                try:
                    task.result()
                except BaseException:
                    pass
            probe_task.add_done_callback(_consume_late_probe)
        raise


def _read_json_artifact(state: Any, artifact: dict[str, Any]) -> dict[str, Any] | None:
    try:
        record = state.read_artifact(artifact["id"]) or {}
        payload = json.loads(str(record.get("content") or "{}"))
        return payload if isinstance(payload, dict) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

def _job_lifecycle_states(state: Any) -> dict[str, dict[str, Any]]:
    """Latest append-only lifecycle record for each scheduler job."""
    latest: dict[str, dict[str, Any]] = {}
    try:
        artifacts = state.list_artifacts(_EXTERNAL_JOB_LIFECYCLE_TYPE) or []
    except Exception:
        return latest
    for artifact in artifacts:
        payload = _read_json_artifact(state, artifact)
        if payload and payload.get("scheduler") and payload.get("job_id"):
            payload = {**payload, "lifecycle_artifact_id": artifact.get("id")}
            latest[_job_key(
                str(payload["scheduler"]), str(payload["job_id"]),
                payload.get("namespace"), payload.get("launch_host"),
                payload.get("scheduler_cluster"), payload.get("resource_uid"),
                payload.get("submission_nonce"), payload.get("process_group_id"),
                payload.get("process_start_ticks"), payload.get("container_runtime_id"),
            )] = payload
    return latest


def _validated_submission_receipt(
    artifact_id: str,
    artifact_type: str,
    outer: dict[str, Any],
) -> dict[str, Any]:
    """Validate one current-run receipt without interpreting its lifecycle."""
    if outer.get("type") != artifact_type:
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: outer artifact type is invalid")
    content = outer.get("content")
    if not isinstance(content, str):
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: content JSON is invalid")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: content JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: content must be a JSON object")
    status = payload.get("status")
    if status not in {
        "success", "accepted_identity_unresolved", "submission_outcome_unknown",
    }:
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: receipt status is invalid")
    if not isinstance(payload.get("dry_run"), bool):
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: dry_run is invalid")
    if not isinstance(payload.get("scheduler"), str) or not payload["scheduler"].strip():
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: scheduler is invalid")
    job_id = payload.get("job_id")

    # A dry-run is a durable planning receipt, not a scheduler submission.
    if (
        status == "success"
        and not payload["dry_run"]
        and (not isinstance(job_id, str) or not job_id.strip())
    ):
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: successful receipt has no job_id")
    if status in {"accepted_identity_unresolved", "submission_outcome_unknown"} and (
            job_id not in {None, ""} or not payload.get("do_not_resubmit")):
        raise SubmissionLedgerError(
            f"{artifact_type} {artifact_id}: unresolved identity receipt is invalid")
    return {
        **payload,
        "artifact_id": artifact_id,
        "submission_record_type": artifact_type,
    }


def _read_submission_receipts_strict(
    state: Any,
    *,
    artifact_types: tuple[str, ...],
    current_run_only: bool,
) -> list[dict[str, Any]]:
    """Strictly read selected receipt types from their artifact authority.

    Project worktree ledgers contain receipts from multiple runs. In
    current_run_only mode, a head consistently owned by another run is a
    foreign candidate and is filtered before body access. All I/O and payload
    validation failures remain explicit ledger errors.
    """
    current_run_id = str(getattr(state, "run_id", "") or "")
    records: list[dict[str, Any]] = []
    for artifact_type in artifact_types:
        try:
            artifacts = list(state.list_artifacts(
                artifact_type, own_only=True))
        except TypeError as exc:
            # Compatibility for old minimal adapters only. Real State supports
            # own_only and must not admit another node's lookalike receipt.
            if hasattr(state, "find_artifact_path"):
                raise SubmissionLedgerError(
                    f"{artifact_type}: artifact listing failed: "
                    f"{type(exc).__name__}: {exc}") from exc
            try:
                artifacts = list(state.list_artifacts(artifact_type))
            except Exception as fallback_exc:
                raise SubmissionLedgerError(
                    f"{artifact_type}: artifact listing failed: "
                    f"{type(fallback_exc).__name__}: {fallback_exc}"
                ) from fallback_exc
        except Exception as exc:
            raise SubmissionLedgerError(
                f"{artifact_type}: artifact listing failed: "
                f"{type(exc).__name__}: {exc}") from exc
        for artifact in artifacts:
            artifact_id = str(artifact.get("id") or "")
            if not artifact_id:
                raise SubmissionLedgerError(f"{artifact_type}: artifact ID is invalid")

            # On real State, the ledger head owns producer identity and is
            # readable without touching the artifact body. Filter a consistent
            # foreign run here so a missing foreign body cannot poison this
            # run. Missing producer identity remains fail-closed.
            artifact_head = getattr(state, "artifact_head", None)
            if callable(artifact_head):
                try:
                    head = artifact_head(artifact_id)
                except Exception as exc:
                    raise SubmissionLedgerError(
                        f"{artifact_type} {artifact_id}: artifact ledger head "
                        f"read failed: {type(exc).__name__}: {exc}"
                    ) from exc
                if head is None:
                    raise SubmissionLedgerError(
                        f"{artifact_type} {artifact_id}: artifact ledger head is missing")
                producer_run_id = str(
                    getattr(head, "produced_by_run_id", "") or "")
                if current_run_id and not producer_run_id:
                    raise SubmissionLedgerError(
                        f"{artifact_type} {artifact_id}: producer run is invalid")
                if (
                    current_run_only
                    and current_run_id
                    and producer_run_id
                    and producer_run_id != current_run_id
                ):
                    continue

            try:
                outer = state.read_artifact(artifact_id)
            except Exception as exc:
                raise SubmissionLedgerError(
                    f"{artifact_type} {artifact_id}: artifact body read "
                    f"failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(outer, dict):
                raise SubmissionLedgerError(
                    f"{artifact_type} {artifact_id}: artifact file is missing")
            if not callable(artifact_head):
                producer_run_id = str(outer.get("produced_by_run_id") or "")
                if (
                    current_run_only
                    and current_run_id
                    and producer_run_id
                    and producer_run_id != current_run_id
                ):
                    continue
            # Minimal legacy test adapters exposed only content, while real
            # State records always carry type and are strictly validated.
            if "type" not in outer and not hasattr(state, "find_artifact_path"):
                outer = {**outer, "type": artifact_type}
            validated = _validated_submission_receipt(
                artifact_id, artifact_type, outer)
            validated["_receipt_produced_by_run_id"] = producer_run_id
            records.append(validated)
    return records


def read_owed_submission_receipts(state: Any) -> list[dict[str, Any]]:
    """Strictly read only receipts owned by the current run."""
    return _read_submission_receipts_strict(
        state,
        artifact_types=_SUBMISSION_RECORD_TYPES,
        current_run_only=True,
    )


def _identity_recovery_receipt(
    state: Any, fields: dict[str, str],
) -> dict[str, Any] | None:
    """Return the unique durable reconciliation receipt; task status is not fact."""
    nonce = str(fields.get("submission_nonce") or "")
    if not nonce:
        return None
    receipts = _read_submission_receipts_strict(
        state,
        artifact_types=(_EXTERNAL_JOB_SUBMISSION_RECOVERY_TYPE,),
        current_run_only=False,
    )
    matches: list[dict[str, Any]] = []
    for payload in receipts:
        reconciliation = payload.get("reconciliation")
        persistence = payload.get("submission_persistence")
        if (
            payload.get("status") == "success"
            and payload.get("job_id")
            and str(payload.get("submission_nonce") or "") == nonce
            and isinstance(reconciliation, dict)
            and reconciliation.get("query_status") == "unique"
            and isinstance(persistence, dict)
            and persistence.get("status") == "identity_reconciled"
        ):
            matches.append(payload)
    identities = {_job_key_for_record(item) for item in matches}
    if len(identities) > 1:
        raise SubmissionLedgerError(
            "identity recovery has conflicting reconciled job identities: "
            + str(fields.get("external_job_identity_recovery_key") or ""))
    return matches[-1] if matches else None


def lifecycle_for_submission(
    state: Any, submission: dict[str, Any],
    *, lifecycle_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve lifecycle without assigning an old unscoped record by guesswork."""
    scheduler = str(submission.get("scheduler") or "")
    job_id = str(submission.get("job_id") or "")
    identity = _job_key_for_record(submission)
    states = lifecycle_states if lifecycle_states is not None else _job_lifecycle_states(state)
    exact = states.get(identity)
    if exact:
        return {
            "status": exact.get("lifecycle_status"),
            "resolution": "exact",
            "lifecycle_artifact_id": exact.get("lifecycle_artifact_id"),
        }
    namespace, launch_host = submission.get("namespace"), submission.get("launch_host")
    scheduler_cluster, resource_uid = (submission.get("scheduler_cluster"),
                                       submission.get("resource_uid"))
    if not scheduler or not job_id or not (namespace or launch_host
                                          or scheduler_cluster or resource_uid
                                          or submission.get("submission_nonce")
                                          or submission.get("process_group_id")
                                          or submission.get("process_start_ticks")
                                          or submission.get("container_runtime_id")):
        return {"status": None, "resolution": "absent", "lifecycle_artifact_id": None}
    legacy_scoped_key = _job_key(
        scheduler, job_id, namespace, launch_host, scheduler_cluster, resource_uid)
    legacy = states.get(legacy_scoped_key)
    if legacy and identity != legacy_scoped_key:
        compatible_identities = {
            _job_key_for_record(row)
            for row in _submission_payloads(state)
            if _job_key(
                str(row.get("scheduler") or ""), str(row.get("job_id") or ""),
                row.get("namespace"), row.get("launch_host"),
                row.get("scheduler_cluster"), row.get("resource_uid"),
            ) == legacy_scoped_key
        }
        if compatible_identities == {identity}:
            return {
                "status": legacy.get("lifecycle_status"),
                "resolution": "unique_legacy",
                "lifecycle_artifact_id": legacy.get("lifecycle_artifact_id"),
            }
        return {
            "status": None,
            "resolution": "legacy_lifecycle_scope_ambiguous",
            "lifecycle_artifact_id": legacy.get("lifecycle_artifact_id"),
        }
    legacy = states.get(_job_key(scheduler, job_id))
    if not legacy:
        return {"status": None, "resolution": "absent", "lifecycle_artifact_id": None}
    scoped_identities = {
        _job_key_for_record(row)
        for row in _submission_payloads(state)
        if str(row.get("scheduler") or "").casefold() == scheduler.casefold()
        and str(row.get("job_id") or "") == job_id
        and (row.get("namespace") or row.get("launch_host")
             or row.get("scheduler_cluster") or row.get("resource_uid")
             or row.get("container_runtime_id"))
    }
    if scoped_identities == {identity}:
        return {
            "status": legacy.get("lifecycle_status"),
            "resolution": "unique_legacy",
            "lifecycle_artifact_id": legacy.get("lifecycle_artifact_id"),
        }
    return {
        "status": None,
        "resolution": "legacy_lifecycle_scope_ambiguous",
        "lifecycle_artifact_id": legacy.get("lifecycle_artifact_id"),
    }


def owed_external_job_closure_records(state: Any) -> list[dict[str, Any]]:
    """Single read-only judgement of current-run jobs that still owe closure."""
    receipts = read_owed_submission_receipts(state)
    unresolved = [item for item in receipts if item.get("status") in {
        "accepted_identity_unresolved", "submission_outcome_unknown",
    }]
    if unresolved:
        ids = ", ".join(str(item["artifact_id"]) for item in unresolved)
        raise SubmissionLedgerError(
            "scheduler submission outcome/identity remains unresolved: " + ids)

    lifecycle_states = _job_lifecycle_states(state)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in receipts + _task_external_jobs(state, strict=True):
        is_task_obligation = bool(payload.get("task_id"))
        if (
            not is_task_obligation
            and (
                payload.get("status") != "success"
                or payload.get("dry_run")
                or not payload.get("scheduler")
                or not payload.get("job_id")
            )
        ):
            continue
        lifecycle = lifecycle_for_submission(
            state, payload, lifecycle_states=lifecycle_states)
        if lifecycle.get("status") in {
            "cancelled", "superseded", "finished", "failed", "finalized",
        }:
            continue
        identity = _job_key_for_record(payload)
        if identity in seen:
            continue
        seen.add(identity)
        records.append({
            **payload,
            "lifecycle_status": lifecycle.get("status"),
            "lifecycle_resolution": lifecycle.get("resolution"),
        })
    return records


def submission_ledger_failure_reason(error: BaseException) -> str:
    """Stable preview/on-end wording for the shared strict judgement."""
    return SUBMISSION_LEDGER_BLOCK_REASON_PREFIX + str(error)[:1000]


def job_lifecycle_state(
    state: Any, scheduler: str, job_id: str,
    namespace: str | None = None, launch_host: str | None = None,
    scheduler_cluster: str | None = None, resource_uid: str | None = None,
    submission_nonce: str | None = None, process_group_id: str | None = None,
    process_start_ticks: int | str | None = None,
    container_runtime_id: str | None = None,
) -> str | None:
    return lifecycle_for_submission(state, {
        "scheduler": scheduler, "job_id": job_id,
        "namespace": namespace, "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster, "resource_uid": resource_uid,
        "submission_nonce": submission_nonce, "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
    }).get("status")

def _record_job_lifecycle(
    state: Any, *, scheduler: str, job_id: str,
    lifecycle_status: str, reason: str = "",
    superseded_by: str | None = None, namespace: str | None = None,
    launch_host: str | None = None, scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None, process_group_id: str | None = None,
    process_start_ticks: int | str | None = None,
    container_runtime_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "scheduler": str(scheduler).lower(), "job_id": str(job_id),
        "namespace": namespace, "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster, "resource_uid": resource_uid,
        "submission_nonce": submission_nonce, "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
        "lifecycle_status": lifecycle_status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason, "superseded_by": superseded_by,
    }
    state.save_artifact(
        _EXTERNAL_JOB_LIFECYCLE_TYPE,
        f"job_lifecycle_{scheduler}_{job_id}_{time.time_ns()}",
        json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={"scheduler": payload["scheduler"], "job_id": payload["job_id"],
                  "namespace": namespace, "launch_host": launch_host,
                  "scheduler_cluster": scheduler_cluster, "resource_uid": resource_uid,
                  "submission_nonce": submission_nonce,
                  "process_group_id": process_group_id,
                  "process_start_ticks": process_start_ticks,
                  "container_runtime_id": container_runtime_id,
                  "lifecycle_status": lifecycle_status},
    )
    try:
        state.append_transcript("external_job_lifecycle", **payload)
    except Exception:
        pass
    return payload

def _normalize_output_roots(output_paths: list[str] | None, workdir: str | None,
                            fallback: str | Path) -> list[str]:
    """Reserve explicit output roots; workdir is the conservative fallback."""
    values = list(output_paths or []) or [workdir or str(fallback)]
    base = os.path.expanduser(workdir or str(fallback))
    roots: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        path = os.path.expandvars(os.path.expanduser(value.strip()))
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        roots.append(os.path.realpath(path))
    return sorted(set(roots))


def _path_is_within(path: str, roots: list[str]) -> bool:
    """True only when ``path`` is contained in one declared managed root."""
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _health_contract(
    health_check: dict[str, Any] | None,
    *, workdir: str | None, output_roots: list[str], scheduler_output_dir: Path,
    expected_duration_s: int | None,
) -> dict[str, Any]:
    """Validate a declarative, read-only health probe contract.

    It deliberately accepts paths and literal error markers only. Persisting an
    arbitrary shell progress command here would turn a durable submission record
    into a command-execution capability for a later chat session.
    """
    if health_check is None:
        health_check = {}
    if not isinstance(health_check, dict):
        raise ValueError("health_check 必须是 object")
    # 判决拆除·第三波（rm:1528 → schema，2026-09-02）：字段集、整数区间、数组上限
    # 由 submit_job schema（additionalProperties:false / minimum / maximum / maxItems /
    # minLength）在派发口核；这里只读已知字段，未知名字不拒绝——如实记进契约
    # （从不被当作命令执行，所以忽略是安全的）。跨字段下限（stall ≥ 2×poll）
    # schema 表达不了，留手写。
    known = {"progress_paths", "completion_paths", "error_patterns",
             "poll_interval_s", "stall_after_s"}
    ignored_fields = sorted(str(name) for name in set(health_check) - known)

    poll_interval_s = int(health_check.get("poll_interval_s") or 180)
    default_stall = max(900, min(3600, int(expected_duration_s or 3600) // 4))
    stall_after_s = int(health_check.get("stall_after_s") or default_stall)
    if stall_after_s < poll_interval_s * 2:
        raise ValueError(
            f"health_check.stall_after_s 必须 ≥ 2×poll_interval_s（≥ {poll_interval_s * 2}）")
    allowed_roots = list(output_roots) + [os.path.realpath(str(scheduler_output_dir))]
    base = os.path.expanduser(workdir or str(scheduler_output_dir))

    def paths(name: str) -> list[str]:
        normalized: list[str] = []
        for value in health_check.get(name) or []:
            text = str(value or "").strip()
            if not text:
                continue
            raw = os.path.expandvars(os.path.expanduser(text))
            candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
            if not _path_is_within(candidate, allowed_roots):
                raise ValueError(f"health_check.{name} 路径必须位于声明的 output_paths/output_dir 内: {value}")
            normalized.append(candidate)
        return sorted(set(normalized))

    patterns = health_check.get("error_patterns")
    if patterns is None:
        patterns = list(_DEFAULT_HEALTH_ERROR_PATTERNS)
    literals = sorted({str(p).strip().casefold() for p in patterns if str(p or "").strip()})
    # 035a：尾 / 是目录声明的唯一语法，必须在 realpath 之前判；规范化后的路径丢了它，
    # 所以按规范化路径另记 kind（没有尾 / 的一律 file，既有声明不会被静默改判）。
    completion_kinds: dict[str, str] = {}
    for value in health_check.get("completion_paths") or []:
        text = str(value or "").strip()
        if not text:
            continue
        raw = os.path.expandvars(os.path.expanduser(text))
        candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
        completion_kinds[candidate] = _output_postconditions.declared_output_kind(text)
    contract = {
        "version": _HEALTH_CONTRACT_VERSION,
        "poll_interval_s": poll_interval_s,
        "stall_after_s": stall_after_s,
        "progress_paths": paths("progress_paths"),
        "completion_paths": paths("completion_paths"),
        "completion_kinds": completion_kinds,
        "error_patterns": literals,
    }
    if ignored_fields:
        contract["ignored_fields"] = ignored_fields
    return contract

def _paths_overlap(left: str, right: str) -> bool:
    try:
        return os.path.commonpath([left, right]) in {left, right}
    except ValueError:
        return False

def _submission_payloads(state: Any) -> list[dict[str, Any]]:
    """Load primary submission receipts and their durable recovery fallback."""
    records: list[dict[str, Any]] = []
    for artifact_type in _SUBMISSION_RECORD_TYPES:
        try:
            artifacts = state.list_artifacts(artifact_type) or []
        except Exception:
            continue
        for artifact in artifacts:
            payload = _read_json_artifact(state, artifact)
            if (payload and payload.get("status") == "success" and not payload.get("dry_run")
                    and payload.get("scheduler") and payload.get("job_id")):
                payload["artifact_id"] = artifact.get("id")
                payload["submission_record_type"] = artifact_type
                records.append(payload)
    return records

def _task_external_jobs(
    state: Any, *, strict: bool = False,
) -> list[dict[str, Any]]:
    project_root = getattr(state, "project_root", None)
    if not project_root:
        return []
    tasks_dir = Path(project_root) / "tasks"
    try:
        if not tasks_dir.exists():
            return []
        if not tasks_dir.is_dir():
            raise SubmissionLedgerError(
                "external job task ledger path is not a directory")
        if strict:
            ledger_path = tasks_dir / "tasks.jsonl"
            if ledger_path.exists():
                for line_number, line in enumerate(
                    ledger_path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if not line.strip():
                        continue
                    try:
                        decoded = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SubmissionLedgerError(
                            "external job task ledger contains invalid JSON at "
                            f"line {line_number}"
                        ) from exc
                    if not isinstance(decoded, dict):
                        raise SubmissionLedgerError(
                            "external job task ledger contains a non-object at "
                            f"line {line_number}")
        from core.tasks import TaskList
        tasks = TaskList(tasks_dir).list_all()
    except SubmissionLedgerError:
        if strict:
            raise
        return []
    except Exception as exc:
        if strict:
            raise SubmissionLedgerError(
                "external job task ledger is unreadable: "
                f"{type(exc).__name__}: {exc}") from exc
        return []
    records: list[dict[str, Any]] = []
    for task in tasks:
        # Task status is model-writable bookkeeping, never physical closure.
        # Empty owner is the explicit legacy compatibility case; tasks owned by
        # another node are outside Experiment's closure authority.
        if task.owner_node not in {"", "experiment"}:
            continue
        if task.status == "completed" and not strict:
            continue
        fields = {}
        for line in task.description.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
        recovery_marker_present = "external_job_identity_recovery_key" in fields
        if recovery_marker_present:
            recovery_fields = (
                "external_job_identity_recovery_key",
                "submission_nonce",
                "scheduler",
            )
            missing_recovery_fields = [
                name for name in recovery_fields if not fields.get(name)
            ]
            if missing_recovery_fields:
                if strict:
                    raise SubmissionLedgerError(
                        f"external job identity recovery task {task.id} has "
                        "invalid identity fields: "
                        + ", ".join(missing_recovery_fields))
                continue
            if strict:
                reconciled = _identity_recovery_receipt(state, fields)
                if reconciled is None:
                    raise SubmissionLedgerError(
                        "scheduler submission outcome/identity remains unresolved: "
                        f"recovery task {task.id} "
                        f"({fields['external_job_identity_recovery_key']})")
                records.append({
                    **reconciled,
                    "task_id": None,
                    "identity_recovery_task_id": task.id,
                    "submitted_by_run_id": (
                        reconciled.get("_receipt_produced_by_run_id")
                        or fields.get("submitted_by_run_id")
                    ),
                })
            continue
        normal_marker_present = "external_job_key" in fields
        if not normal_marker_present:
            continue
        missing_identity_fields = [
            name for name in ("external_job_key", "scheduler", "job_id")
            if not fields.get(name)
        ]
        if missing_identity_fields:
            if strict:
                raise SubmissionLedgerError(
                    f"external job task {task.id} has invalid identity fields: "
                    + ", ".join(missing_identity_fields))
            continue
        try:
            roots = json.loads(fields.get("output_roots", "[]"))
        except json.JSONDecodeError:
            roots = []
        try:
            health_contract = json.loads(fields.get("health_contract", "{}"))
        except json.JSONDecodeError:
            health_contract = {}
        records.append({"scheduler": fields["scheduler"], "job_id": fields["job_id"],
                        "workdir": fields.get("workdir"), "output_roots": roots,
                        "task_id": task.id,
                        "external_job_key": fields.get("external_job_key"),
                        "namespace": fields.get("namespace") or None,
                        "scheduler_cluster": fields.get("scheduler_cluster") or None,
                        "resource_uid": fields.get("resource_uid") or None,
                        "submission_nonce": fields.get("submission_nonce") or None,
                        "process_group_id": fields.get("process_group_id") or None,
                        "process_start_ticks": fields.get("process_start_ticks") or None,
                        "container_runtime_id": fields.get("container_runtime_id") or None,
                        "sandbox_control_dir": fields.get("sandbox_control_dir") or None,
                        "resource_guard_status_path": fields.get("resource_guard_status_path") or None,
                        "launch_host": fields.get("launch_host") or None,
                        "remote_marker_path": fields.get("remote_marker_path") or None,
                        "stdout_path": fields.get("stdout_path") or None,
                        "stderr_path": fields.get("stderr_path") or None,
                        "scheduler_output_dir": fields.get("output_dir") or None,
                        "expected_duration_s": fields.get("expected_duration_s") or None,
                        # 仅回读 task 行的自述；finalize 侧还要与受防伪写入门
                        # 保护的 job_submission payload 交叉核验后才可信。
                        "execution_class": fields.get("execution_class") or None,
                        "submitted_at": fields.get("submitted_at") or None,
                        # 这条待办来自哪个 run。任务清单是**项目级**的，框架把它
                        # 按 owner 注入同项目的任何后续 experiment run；于是一个
                        # 新 run 会看到别的 run 留下的未收尾作业并去关掉它。
                        # 关掉本身是对的（开放作业不该永远悬空），但账本上必须
                        # 读得出「这条终态不是本 run 自己的执行」。
                        "handoff_origin_run_id": (
                            fields.get("external_job_key", "").split(":", 1)[0]
                            or None),
                        "health_contract": health_contract if isinstance(health_contract, dict) else {}})
    return records

def _resolved_unknown_orphan_ids(state: Any) -> dict[str, str]:
    """orphan_artifact_id -> 解除记录 id；解除只追加，不可再被否定。"""
    resolved: dict[str, str] = {}
    try:
        artifacts = state.list_artifacts(_UNKNOWN_ORPHAN_RESOLUTION_TYPE) or []
    except Exception:
        return resolved
    for artifact in artifacts:
        payload = _read_json_artifact(state, artifact)
        orphan_id = str((payload or {}).get("orphan_artifact_id") or "")
        if orphan_id:
            resolved.setdefault(orphan_id, str(artifact.get("id") or ""))
    return resolved


def _unknown_orphan_records(state: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        artifacts = state.list_artifacts(_UNKNOWN_ORPHAN_TYPE) or []
    except Exception:
        return records
    # 已有解除记录的 orphan 不再产生冲突：隔离本体一个字都不改，放行只由
    # 追加的 unknown_orphan_resolution 决定。
    resolved = _resolved_unknown_orphan_ids(state)
    for artifact in artifacts:
        if str(artifact.get("id") or "") in resolved:
            continue
        payload = _read_json_artifact(state, artifact)
        if payload and payload.get("status") == "active":
            payload["artifact_id"] = artifact.get("id")
            records.append(payload)
    return records

async def _record_unknown_orphan(state: State, output_paths: list[str], observed_processes: list[str] | None = None, log_paths: list[str] | None = None, note: str = "", **_: Any) -> dict[str, Any]:
    """Record an unowned legacy process without claiming a run_id; its outputs stay reserved."""
    # 判决拆除·第三波（rm:273 → schema，2026-09-02）：至少一条由 schema minItems 核。
    roots = _normalize_output_roots(output_paths, None, experiment_output_dir(state, "runtime", create=True))
    observed = [str(item) for item in observed_processes or []]
    # 自述的形式在登记这一刻就定了生死：记录不可改写，形式不可机械复核的自述
    # 解除时一律探不活（unavailable），那条隔离就只剩换目录一条出路。登记时
    # 当场报出来，别等到 resolve 才发现。recorded_on_host 只作溯源留痕，不参与
    # pid 归属推断 —— 登记主机不等于进程主机，拿它解释裸 pid 就是替远端进程
    # 出具假死亡证明。
    unprobeable = [{"reference": parsed["reference"], "problem": parsed["issue"]}
                   for parsed in (_parse_observed_process(item) for item in observed)
                   if parsed["issue"]]
    payload = {"status": "active", "classification": "unknown_orphan", "recorded_by_run_id": state.run_id, "recorded_on_host": socket.gethostname(), "recorded_at": datetime.now(timezone.utc).isoformat(), "output_roots": roots, "observed_processes": observed, "log_paths": list(log_paths or []), "note": note}
    artifact = state.save_artifact(_UNKNOWN_ORPHAN_TYPE, f"unknown_orphan_{state.run_id}_{time.time_ns()}", json.dumps(payload, ensure_ascii=False, indent=2), metadata={"output_roots": roots, "classification": "unknown_orphan"})
    state.append_transcript("unknown_orphan_recorded", artifact_id=artifact.get("id"), output_roots=roots)
    message = (
        "这是隔离并阻断，不是释放：未知历史进程不归属任何 run，上列输出根"
        "从现在起锁死，重叠的新提交一律被 active_external_job_output_conflict "
        "拦下。继续推进请换一组不重叠的 output_paths；确需重新使用这些根，"
        f'走 resolve_unknown_orphan(artifact_id="{artifact.get("id")}", '
        'reason="...")：它要求自述进程逐个机械确证已死（pid 只认 '
        "pid=<数字>@<主机名>，且只能在那台主机上用 /proc 判定），"
        "且这些根自本次登记起没有任何新写入。"
    )
    if unprobeable:
        message += (
            "\n注意：本次登记的 observed_processes 有 "
            f"{len(unprobeable)} 条形式不可机械复核（见 unprobeable_observed_processes）。"
            "记录已冻结、不可改写，这些自述在解除时会一律判为“无法探活”而拒绝解除 —— "
            "这条隔离今后只能靠换一组不重叠的 output_paths 绕开。下次登记请把每条写成 "
            "pid=<数字>@<主机名> 或 container=<id>，说明文字放进 note。")
    return {
        "status": "success", "artifact_id": artifact.get("id"),
        "classification": "unknown_orphan", "output_roots": roots,
        "unprobeable_observed_processes": unprobeable,
        "message": message,
    }


def _normalize_host(value: str) -> str:
    """主机名归一：只吃掉首尾空白/尾点与大小写差异，域名一律保留。"""
    return str(value or "").strip().strip(".").casefold()


def _hosts_match(claimed: str, here: str) -> bool:
    """两个主机名是否确证是同一台机器（拿不准一律为假）。

    不能截断到第一个点再比：``host.dc1.internal`` 与 ``host.dc2.internal`` 是两台
    机器，截断后却相等 —— 于是远端自述被当成本机，本机 ``/proc`` 查不到那个 pid
    就会替**另一台机器上的进程**开死亡证明，正是本通道要堵的那类误放行。
    规则：两边都带域时必须整串相同；只要有一边没写域，就只能按短名比 —— 自述
    常写短名而 ``gethostname()`` 常给 FQDN（反过来也有），这一步宽松是必要的，
    但它只发生在没有域可比的时候，绝不用于两个明确不同的域之间。
    """
    claimed, here = _normalize_host(claimed), _normalize_host(here)
    if not claimed or not here:
        return False
    if claimed == here:
        return True
    claimed_short, _, claimed_domain = claimed.partition(".")
    here_short, _, here_domain = here.partition(".")
    if claimed_short != here_short:
        return False
    return not (claimed_domain and here_domain)


_PID_REFERENCE_KEYS = {"pid", "process"}
_CONTAINER_REFERENCE_KEYS = {"container", "docker", "container_runtime_id"}
_PID_REFERENCE_FORM = "pid=<数字>@<主机名>"
_CONTAINER_REFERENCE_FORM = "container=<id>"


def _parse_observed_process(reference: str) -> dict[str, str]:
    """把一条 observed_processes 自述解析成机械探活输入；``issue`` 非空即形式不合法。

    只认两种可机械复核的形式，且两者都必须是整条自述的全部内容：
    ``pid=<数字>@<主机名>``（在该主机上查 ``/proc/<pid>``）与 ``container=<id>``。

    pid 的主机是**强制**的，不接受裸 pid，也不回退到 orphan 的 ``recorded_on_host``：
    ``/proc`` 只覆盖本机，而本项目的作业跑在 spr-cu04/05、输出写共享 beegfs、
    解除在登录节点发起。登记主机不等于进程主机 —— 拿登记主机去解释裸 pid，等于
    替一个远端进程用本机 ``/proc`` 出具死亡证明，"查不到"会被当成"已经死了"。
    主机写不出来的后果因此是**无法解除**（fail-closed），不是误放行。

    尾随自由文本一律拒绝而不是截断：``pid=12345 (python train.py) on spr-cu04``
    截到第一个空格就把自述里明明写着的主机静默丢弃，再判成本机 —— 那正是上面
    这条误放行。pid 还要先规范化成整数：``/proc/00001`` 不存在，但 pid 1 可能
    正活着；用 ``isdecimal`` 而不是 ``isdigit`` 判定，因为 ``"²".isdigit()`` 为真
    但 ``int("²")`` 抛 ValueError（全角 ``１２３４５`` 两者都为真，照常归一）。
    """
    token = str(reference or "").strip()
    prefix, separator, value = token.partition("=")
    key = prefix.strip().lower() if separator else ""
    ref = (value if separator else token).strip()
    pid_text, at_sign, host_claim = ref.partition("@")
    parsed = {"reference": token, "probe": "none", "ref": ref, "pid": "", "host": "", "issue": ""}
    if key in _CONTAINER_REFERENCE_KEYS:
        parsed["probe"] = "container"
        if not ref:
            parsed["issue"] = f"container 自述没有写出容器 id；正确写法是 {_CONTAINER_REFERENCE_FORM}"
        elif any(char.isspace() for char in ref):
            parsed["issue"] = (
                f"container 自述 {ref!r} 里还有别的文字；正确写法只有 {_CONTAINER_REFERENCE_FORM}，"
                "说明请写进 note，不要拼在自述里（多余文字不会被截掉，只会让这条无法探活）")
        return parsed
    if key in _PID_REFERENCE_KEYS or (not key and pid_text.isdecimal()):
        parsed["probe"] = "pid"
        if any(char.isspace() for char in ref):
            parsed["issue"] = (
                f"pid 自述 {ref!r} 里还有别的文字；正确写法只有 {_PID_REFERENCE_FORM}，"
                "说明请写进 note —— 尾随文字里的主机名不会被采信，这里也不会截断猜测")
            return parsed
        if not pid_text.isdecimal():
            parsed["issue"] = (
                f"pid 自述 {ref!r} 的 pid 不是十进制数字；正确写法是 {_PID_REFERENCE_FORM}")
            return parsed
        try:
            parsed["pid"] = str(int(pid_text))
        except ValueError:  # pragma: no cover - isdecimal 之外的极端形态兜底
            parsed["issue"] = (
                f"pid 自述 {ref!r} 的 pid 无法解析为整数；正确写法是 {_PID_REFERENCE_FORM}")
            return parsed
        parsed["host"] = _normalize_host(host_claim)
        if not at_sign or not parsed["host"]:
            parsed["issue"] = (
                f"pid 自述 {ref!r} 没有写出主机；正确写法是 {_PID_REFERENCE_FORM}。"
                "/proc 只覆盖本机，远端作业（SLURM 计算节点等）的 pid 在本机查不到"
                "并不等于它已经死了，所以缺主机的 pid 一律不出具死亡证明")
        return parsed
    parsed["issue"] = (
        f"自述里没有可机械探活的 {_PID_REFERENCE_FORM} 或 {_CONTAINER_REFERENCE_FORM}")
    return parsed


def _probe_observed_process_liveness(reference: str) -> dict[str, Any]:
    """机械探活一条 observed_processes 自述；无法机械判定一律记为 unavailable。

    自述是 LLM 写的自由文本，不能当证据用：形式解析见 ``_parse_observed_process``
    （pid 必须自带 ``@<主机名>``），``container=<id>`` 复用 record_intent_adoption
    同一条本地作业隔离层探活路径。pid 只在其声明主机等于本机时才查 ``/proc`` ——
    声明的是别的主机就 unavailable，与 ``_adopt_dead_foreign_intent`` 拒绝为非
    local 调度器出具死亡证明同理。
    """
    parsed = _parse_observed_process(reference)
    token, probe = parsed["reference"], parsed["probe"]
    if parsed["issue"]:
        refused: dict[str, Any] = {"reference": token, "probe": probe,
                                   "state": "unavailable", "detail": parsed["issue"]}
        if parsed["pid"]:
            refused["pid"] = parsed["pid"]
        return refused
    if probe == "pid":
        pid, claimed = parsed["pid"], parsed["host"]
        here = _normalize_host(socket.gethostname())
        if not _hosts_match(claimed, here):
            return {"reference": token, "probe": "pid", "pid": pid, "state": "unavailable",
                    "host": claimed,
                    "detail": (f"pid 声明在主机 {claimed}，本机是 {here}；"
                               "远端进程无法用本机 /proc 探活（两边都写了域名且域不同时"
                               "一律按不同主机处理，短名相同也不例外）")}
        if not os.path.isdir("/proc"):
            return {"reference": token, "probe": "pid", "pid": pid, "state": "unavailable",
                    "detail": "本机没有 /proc，无法机械探活 pid"}
        alive = os.path.exists(f"/proc/{pid}")
        return {"reference": token, "probe": "pid", "pid": pid, "host": claimed,
                "state": "alive" if alive else "dead",
                "detail": f"{here} 上 /proc/{pid} {'存在' if alive else '不存在'}"}
    if probe == "container":
        from core import sandbox
        available, availability_detail = sandbox.availability(refresh=True)
        if not available:
            return {"reference": token, "probe": "container", "state": "unavailable",
                    "detail": ("本地作业隔离层不可用"
                               f"（{availability_detail}），无法出具死亡证明")}
        inspected = sandbox.inspect_container(parsed["ref"])
        if inspected.get("error"):
            return {"reference": token, "probe": "container", "state": "unavailable",
                    "detail": f"inspect 失败（{inspected['error']}），无法出具死亡证明"}
        status = str(inspected.get("status") or "")
        if inspected.get("exists") and status not in {"exited", "dead"}:
            return {"reference": token, "probe": "container", "state": "alive",
                    "detail": f"容器仍在 {status or 'running'}"}
        return {"reference": token, "probe": "container", "state": "dead",
                "detail": f"容器 exists={bool(inspected.get('exists'))} status={status or 'absent'}"}
    return {"reference": token, "probe": "none", "state": "unavailable",  # pragma: no cover
            "detail": parsed["issue"] or "自述无法机械探活"}


# mtime 基线的安全余量（秒）：recorded_at 取自记录主机的 datetime.now() 墙钟，
# 而 mtime 由文件系统盖章，两者不同源。实测同机 200/200 次"严格在 recorded_at
# 之后"的写入里，st_mtime 反而早最多 6.2ms（时钟源粒度 + 页缓存回写时机）。
# 比较时把基线往前推这个余量：可疑窗口只会变宽，方向永远是更容易判 written、
# 更难解除，绝不会因为时钟毛刺把"隔离期内被写过"读成静默。
# 取 2s：比实测同机偏差大三个数量级，也覆盖 NTP 同步主机间常见的几十毫秒量级
# 偏差；而隔离到解除的实际间隔是分钟到天，宽 2s 不会把真静默的根挡在门外。
_SILENCE_MTIME_MARGIN_S = 2.0
# 跨出登记根之后的条目上限。**判定方式必须是"realpath 之后还在不在这个根里"**，
# 不能是"是不是符号链接"、更不能是"st_dev 变没变"：
#   - ``latest -> results`` 这类根内良性链接，目标就在根里，属于这批输出本身。按
#     "是链接就吃预算"算，一棵 26k 条目的树只要挂一条这样的链接就可能超限；而遍历
#     是 LIFO 栈 + (dev,ino) 去重，链接先出栈就把整棵真实子树记成"跨链接到达"、
#     真实目录先出栈则链接命中去重一条都不吃 —— 结论取决于 ``os.listdir`` 的返回
#     顺序，同一棵树换个链接名就从 silent 翻成 unavailable。
#   - ``st_dev`` 变化在 HPC 上是**嵌套挂载**（节点本地 scratch bind mount、autofs／
#     NFS submount、btrfs subvolume、overlayfs），挂载点仍在登记根内，那底下也是
#     这批输出，一条链接都没有也会被判"跨出"而吃满预算。
# 两种误判的后果都是永久锁死：预算是编译期常量、mtime 单调，超限后再没有任何动作
# 序列能解除（照拒绝文案去 unlink 那条链接，只会把 root mtime 抬高、把 unavailable
# 变成更死的 written）。realpath 仍落在根内就不计预算：根本身登记时被 realpath 过，
# 那棵真实树按构造必然有限，加上 (dev,ino) 去重必然终止（50 万条目实测 3.76s）。
# 真正需要兜底的只有 realpath 跑到根外：``latest -> /`` 这种链接能把核验拖成近乎
# 无限的遍历。超限即 unavailable（fail-closed，不是"扫到这儿算静默"），且拒绝必须
# 指名是哪条链接撑爆了预算，好让调用方针对那一条处理。
_SILENCE_MAX_CROSSED_ENTRIES = 20000


def _probe_output_roots_silent(
    roots: list[str], recorded_at: str, *, listing_limit: int = 20,
) -> dict[str, Any]:
    """核验隔离期内输出根静默：任一条目 mtime 晚于 recorded_at 即仍可能有写者。

    “什么都没看到”不是静默证据：根不存在（被删除/改名）、一个条目都 stat 不到，
    全部回 unavailable，而不是让零证据冒充正面证据。目录 mtime 与文件一起纳入
    基线，隔离期内的新建/删除/改名才不会在核验里不留痕。

    树内部的符号链接**跟进到目标**，与常规条目同一把尺子：真正被写的是链接指向
    的别处，只 lstat 链接自身会把静默伪造出来；而把链接一律判 unavailable 又会
    锁死 ``latest -> results`` 这类根内良性链接，且"移除链接再重核"会抬高父目录
    mtime、把解除通道永久关死。跟进用 (st_dev, st_ino) 去重防环，链接自身的 lstat
    mtime 也一并纳入（重建链接同样是隔离期内的写入）。只有目标真的够不着 ——
    断链、权限不足、目录读不了 —— 才算无法证明静默。

    遍历量的上限只加在**真正跨出登记根**的下降上，判据是 ``os.path.realpath`` 之后
    还在不在这个根里 —— 不是"是不是符号链接"（``latest -> results`` 的目标就在根内，
    是这批输出本身），也不是"``st_dev`` 变没变"（嵌套挂载点仍在根内）。根内遍历不设
    上限：根是 realpath 过的真实目录树，配合 (dev,ino) 去重按构造必然终止，而给它
    设上限就等于按输出树大小永久锁死 orphan。超限时拒绝里带 ``overflow_source``，
    指名是哪条链接把预算撑爆的；此后的其余根只做存在性登记，记在 ``deferred_roots``。

    唯一的例外是根本身：``output_roots`` 在登记时被 realpath 过，那时不可能是
    链接；解除时若发现它是链接，说明整条路径被换掉了，跟进只会去量另一批文件。
    这种情况仍记为无法证明静默，出路是换一组不重叠的 output_paths。

    残余风险：``recorded_at`` 是记录主机墙钟，mtime 由文件系统盖章，beegfs 跨主机
    时钟偏差超过 ``_SILENCE_MTIME_MARGIN_S`` 时，隔离期最初那一瞬的写入仍可能被读
    成"早于基线"。余量只压住毫秒量级毛刺，压不住未同步时钟的秒级漂移。
    """
    try:
        since = datetime.fromisoformat(
            str(recorded_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return {"state": "unavailable", "suspicious_files": [], "unreadable": [],
                "missing_roots": [], "entries_checked": 0,
                "detail": f"orphan recorded_at 不可解析（{recorded_at!r}），无法定基线"}
    since -= _SILENCE_MTIME_MARGIN_S
    unreadable: list[str] = []
    suspicious: list[str] = []
    missing: list[str] = []
    deferred: list[str] = []
    visited: set[tuple[int, int]] = set()
    checked = 0
    crossed = 0
    overflow_source = ""
    # 跨出判定要对**整组**登记根判，不能只跟当前这一个根比：一条 orphan 的多个
    # 输出根之间互相引用是常态（MOM6 的 OUTPUT/RESTART -> ../RESTART），只跟单根
    # 比会把兄弟根整棵树当成"根外"吃预算，超限即永久锁死。
    roots_real = [os.path.realpath(r) for r in roots]

    for root in roots:
        if not os.path.lexists(root):
            missing.append(root)
            continue
        if os.path.islink(root):
            # 根在登记时被 realpath 过，那时它一定不是链接：现在是，说明整条路径
            # 被换掉了 —— 跟进只会去量另一批文件，证不了"登记的那批输出"静默。
            unreadable.append(f"{root} -> {os.path.realpath(root)}: "
                              "输出根本身现在是符号链接（登记时是真实目录），路径已被替换")
            continue
        if overflow_source:
            # 预算已被前一个根撑爆。仍然对剩下的根做一次登记级核验（存在性上面已
            # 查过，这里补一次 stat）：否则调用方处理完被指名的那条链接、下一轮才
            # 发现第二个根早就没了或读不了，白跑一轮。
            try:
                os.stat(root)
            except OSError as exc:
                unreadable.append(f"{root}: {exc.strerror}")
            else:
                deferred.append(root)
            continue
        # 栈元素是 (路径, 把它带出登记根的那条链接的标签)；标签为空即根内条目。
        pending = [(root, "")]
        while pending:
            path, crossing = pending.pop()
            link_target = os.path.realpath(path) if os.path.islink(path) else ""
            label = f"{path} -> {link_target}" if link_target else path
            try:
                # follow_symlinks=True：量的是真正被写的那个 inode。
                info = os.stat(path)
            except OSError as exc:
                unreadable.append(
                    f"{label}: {exc.strerror}"
                    + ("（符号链接目标够不着）" if link_target else ""))
                continue
            key = (info.st_dev, info.st_ino)
            if key in visited:  # 环，或多条路径指向同一 inode：量一次就够。
                continue
            visited.add(key)
            if not crossing and link_target and not _path_is_within(
                    link_target, roots_real):
                # realpath 真跑到了**全部**登记根之外，从这一条起才开始吃预算。
                # 目标仍在根内（``latest -> results``）不算跨出；指向同一条 orphan
                # 的另一个登记根（MOM6 的 ``OUTPUT/RESTART -> ../RESTART``）也不算
                # ——那本来就是这批输出自己，只跟单个根比会把它误判成跨出并锁死；
                # 跨到别的 st_dev（嵌套挂载）只要路径还在根里同样不算。
                crossing = label
            if crossing:
                if crossed >= _SILENCE_MAX_CROSSED_ENTRIES:
                    overflow_source = crossing
                    break
                crossed += 1
            checked += 1
            mtime = info.st_mtime
            if link_target:
                try:  # 链接自身被重建也是隔离期内的写入。
                    mtime = max(mtime, os.lstat(path).st_mtime)
                except OSError:
                    pass
            if mtime > since:
                suspicious.append(label)
            if os.path.isdir(path):
                try:
                    pending.extend((os.path.join(path, name), crossing)
                                   for name in os.listdir(path))
                except OSError as exc:
                    unreadable.append(f"{label}: {exc.strerror}")
    if missing:
        return {"state": "unavailable", "suspicious_files": sorted(suspicious)[:listing_limit],
                "unreadable": sorted(unreadable)[:listing_limit],
                "missing_roots": sorted(missing)[:listing_limit], "entries_checked": checked,
                "detail": ("输出根不存在（可能被删除或改名），无从证明静默："
                           + "、".join(sorted(missing)[:listing_limit]))}
    if overflow_source:
        return {"state": "unavailable", "suspicious_files": sorted(suspicious)[:listing_limit],
                "unreadable": sorted(unreadable)[:listing_limit], "missing_roots": [],
                "entries_checked": checked, "crossed_entries_checked": crossed,
                "overflow_source": overflow_source,
                "deferred_roots": sorted(deferred)[:listing_limit],
                "detail": (f"{overflow_source} 的 realpath 落在登记的输出根之外，其下"
                           f"待核验条目超过 {_SILENCE_MAX_CROSSED_ENTRIES} 上限，无法在"
                           "有限步内证明静默（登记根内的遍历不设上限，这个上限只对"
                           "realpath 真正跑到根外的下降生效：根内的符号链接、嵌套挂载"
                           "都照量不误）"
                           + (f"；其余输出根本次只做了存在性登记：{'、'.join(sorted(deferred)[:listing_limit])}"
                              if deferred else ""))}
    if unreadable:
        return {"state": "unavailable", "suspicious_files": sorted(suspicious)[:listing_limit],
                "unreadable": sorted(unreadable)[:listing_limit], "missing_roots": [],
                "entries_checked": checked,
                "detail": ("输出根有够不着的条目（断链或权限不足），无法证明静默："
                           + "、".join(sorted(unreadable)[:listing_limit]))}
    if suspicious:
        return {"state": "written", "suspicious_files": sorted(suspicious)[:listing_limit],
                "unreadable": [], "missing_roots": [], "entries_checked": checked,
                "detail": (f"{len(suspicious)} 个条目的 mtime 晚于 {recorded_at}"
                           f"（含 {_SILENCE_MTIME_MARGIN_S:g}s 时钟安全余量）")}
    if not checked:
        return {"state": "unavailable", "suspicious_files": [], "unreadable": [],
                "missing_roots": [], "entries_checked": 0,
                "detail": "一个条目都没核验到，静默无证据可依"}
    return {"state": "silent", "suspicious_files": [], "unreadable": [], "missing_roots": [],
            "entries_checked": checked, "since": recorded_at,
            "detail": f"{checked} 个条目全部早于 {recorded_at}"}


def _unknown_orphan_alternative_route(roots: list[str]) -> str:
    return ("或者不解除：改用一组与 " + ("、".join(roots[:3]) or "该 orphan 输出根")
            + " 不重叠的 output_paths 重新提交，隔离锁不影响其它目录。")


_SILENCE_DO_NOT_MUTATE = (
    "先别动这些根里的任何条目：删除、移动、改名、替换链接都会抬高父目录 mtime，"
    "只会让静默更难证明，绝不会让核验通过。")


def _unknown_orphan_silence_next_action(
    orphan_id: str, roots: list[str], silence: dict[str, Any],
) -> str:
    """静默核验没过时的下一步；每一条都必须是真能走通的动作。

    分支必须诚实：mtime 是单调证据，隔离期内一旦有写入，这些根**永远**不可能再
    核验成静默 —— 这时唯一可执行的出路是换目录，不能让调用方去等一个不会到来的
    状态，更不能暗示"清理掉可疑文件再试"（那既是自毁动作，也仍然不会通过）。
    """
    if silence["state"] == "written":
        return (
            _SILENCE_DO_NOT_MUTATE
            + "这些根在隔离期内确实被写过（见 suspicious_files；根内符号链接已跟进到真实"
            "目标，条目写成“链接 -> 目标”），而 mtime 只增不减：即使现在让写入方停下来，"
            "这条隔离也不可能再靠静默证据解除。唯一可执行的下一步是换目录。"
            "另请顺手查明写者身份 —— 若它其实是本节点提交的受管作业，用 job_status/"
            "cancel_job 处理那个作业本身（对这条隔离记录无效）。"
            + _unknown_orphan_alternative_route(roots))
    if silence.get("missing_roots"):
        return (
            "输出根已经不存在（被删除或改名），静默核验永远拿不到证据，重试没有意义。"
            "唯一可执行的下一步是换目录。" + _unknown_orphan_alternative_route(roots))
    if silence.get("overflow_source"):
        # overflow 必须排在 unreadable 之前：预算耗尽前也会攒下 EACCES／断链条目，
        # 落到下面那条分支就会给出"chmod 后重试"——那是一条永远走不通的建议，
        # 无论权限怎么修，下一次核验照样在同一条链接上撑爆预算。
        return (
            _SILENCE_DO_NOT_MUTATE
            + f"核验预算是被 {silence['overflow_source']} 这一条撑爆的：它的 realpath 落在"
            "登记的输出根之外，根外那棵树的条目数超过上限。撑爆预算的是**根外**那棵树的"
            "规模，跟这些根里输出本身有多大无关（登记根内的遍历不设上限，再大的 rank 目录树、"
            "根内的 latest -> results、嵌套挂载都照量），也跟权限无关。原地重试不会改变结论。"
            "可执行的下一步有两条：人工核查这条链接指向哪里、那个目标是否还在被写，"
            "据此判断这批输出能不能放心复用；或者直接换目录。"
            + (f"另注意其余输出根本次只做了存在性登记，未深入核验："
               f"{'、'.join(silence['deferred_roots'])}。"
               if silence.get("deferred_roots") else "")
            + _unknown_orphan_alternative_route(roots))
    if silence.get("unreadable"):
        return (
            _SILENCE_DO_NOT_MUTATE
            + "silence_probe.unreadable 里是够不着的条目：只有权限一类可以安全补救 —— "
            "对这些路径 chmod u+rX（目录光有 +r 还不够，os.stat 子项要 +x；chmod 不改 mtime），"
            "恢复可读后直接重新调用 "
            f'resolve_unknown_orphan(artifact_id="{orphan_id}", reason="...")。'
            "断链、或输出根本身被换成了符号链接，则无法在不改动 mtime 的前提下补救："
            "请人工核查它指向哪里、那个目标是否还在被写，确认后改走换目录一路。"
            + _unknown_orphan_alternative_route(roots))
    return (
        _SILENCE_DO_NOT_MUTATE
        + "本次核验拿不到可依据的证据（detail 写明了原因），重试不会改变结论。"
        "唯一可执行的下一步是换目录。" + _unknown_orphan_alternative_route(roots))


async def _resolve_unknown_orphan(
    state: State, artifact_id: str = "", reason: str = "", **_: Any,
) -> dict[str, Any]:
    """unknown_orphan 隔离记录的机械证据解除通道（fail-closed，只追加）。

    record_unknown_orphan 只有入口没有出口：它把输出根锁死，冲突扫描无条件
    计数，后继 run 的 resume/fresh/cancel/reconcile/finalize 全部撞
    ``active_external_job_output_conflict``，隔离设计说的“人工核查后”在工具面
    没有对应动作。解除必须靠正面证据 —— 自述进程逐个机械确证已死、输出根自
    ``recorded_at`` 起没有新写入 —— 再铸一条不可变 unknown_orphan_resolution。
    进程存活、探活不可用、根上有新写入，一律保持隔离：证据不可得就锁着。
    """
    orphan_id = str(artifact_id or "").strip()
    reason_text = str(reason or "").strip()
    known = {str(item.get("id") or ""): item
             for item in state.list_artifacts(_UNKNOWN_ORPHAN_TYPE) or []}
    if not orphan_id or orphan_id not in known:
        return {
            "status": "error",
            "error": (
                f"artifact_id={orphan_id!r} 不是本 project 的 unknown_orphan 记录；"
                "解除只对本 project 的隔离记录生效。"),
            "known_unknown_orphan_ids": sorted(known),
            "next_action": (
                "从 known_unknown_orphan_ids 里取阻断本次提交的那一条"
                "（submit_job 的 blocker.conflicts[].artifact_id 也给出同一个 id），"
                '再调用 resolve_unknown_orphan(artifact_id="<该 id>", reason="<解除依据>")。'),
        }
    payload = _read_json_artifact(state, known[orphan_id])
    if not payload or payload.get("classification") != "unknown_orphan":
        return {"status": "error",
                "error": f"artifact {orphan_id} 的 payload 不是 unknown_orphan 隔离记录；隔离保持。",
                "next_action": "确认 artifact_id 取自 record_unknown_orphan 的返回或冲突 blocker 后重试。"}
    roots = [str(item) for item in payload.get("output_roots") or []]
    # reason 校验在幂等短路之前：对外契约不能因调用顺序而消失。
    if not reason_text:
        return {
            "status": "error",
            "error": "reason 必填且非空：解除记录是不可变证据，必须写清依据。",
            "next_action": (
                f'重新调用 resolve_unknown_orphan(artifact_id="{orphan_id}", '
                'reason="<谁按什么证据核查了这批输出、为什么确认已无未知写者>")。'),
        }
    already = _resolved_unknown_orphan_ids(state).get(orphan_id)
    if already:
        return {"status": "success", "artifact_id": already, "orphan_artifact_id": orphan_id,
                "output_roots": roots, "reused": True,
                "message": f"该 orphan 已有解除记录 {already}；解除只铸一次，冲突扫描已放行这些输出根。"}
    observed = [str(item) for item in payload.get("observed_processes") or []]
    process_probes = [_probe_observed_process_liveness(item) for item in observed]
    alive = [probe for probe in process_probes if probe["state"] == "alive"]
    unavailable = [probe for probe in process_probes if probe["state"] == "unavailable"]
    if alive:
        return {
            "status": "error",
            "error": ("orphan 自述进程仍有存活："
                      + "；".join(f"{p['reference']}（{p['detail']}）" for p in alive)
                      + "。隔离保持。"),
            "process_probes": process_probes,
            "next_action": (
                "先让这些进程终止（属于本节点提交的作业用 cancel_job 受管取消；"
                "外部进程等其自然退出或由其属主停掉），确认探活转为已死后再重新调用 "
                f'resolve_unknown_orphan(artifact_id="{orphan_id}", reason="...")。'
                + _unknown_orphan_alternative_route(roots)),
        }
    if unavailable:
        return {
            "status": "error",
            "error": ("orphan 自述进程无法机械探活："
                      + "；".join(f"{p['reference']}（{p['detail']}）" for p in unavailable)
                      + "。探活不可用不等于已死，隔离保持。"),
            "process_probes": process_probes,
            "next_action": (
                "隔离记录不可改写，这条 orphan 的自述已经冻结：只有当它本来就写成可机械"
                "复核、且能在本机复核的身份时才可能解除 —— pid 必须写成 "
                "pid=<数字>@<主机名>（整条自述只有这个，不带说明文字），由该主机的 /proc "
                "判定；漏写主机的裸 pid 无法解除，因为本机 /proc 查不到一个远端 pid 并"
                "不等于它已经死了，这里宁可锁着也不出具假死亡证明。若自述确实指向本机以外"
                "的主机，到那台主机上发起解除；container=<id> 由本地作业隔离层判定"
                "（隔离层不可用时等待本地作业隔离层恢复后再试）。自述形式本身不可复核时"
                "无法补救，只能换目录。" + _unknown_orphan_alternative_route(roots)),
        }
    silence = _probe_output_roots_silent(roots, str(payload.get("recorded_at") or ""))
    if silence["state"] != "silent":
        return {
            "status": "error",
            "error": (f"输出根静默核验未通过（{silence['detail']}）；"
                      "可能仍有未知写者，隔离保持。"),
            "silence_probe": silence,
            "suspicious_files": silence["suspicious_files"],
            "next_action": _unknown_orphan_silence_next_action(orphan_id, roots, silence),
        }
    resolution = {
        "resolution_type": "unknown_orphan_isolation_release",
        "orphan_artifact_id": orphan_id,
        "output_roots": roots,
        "orphan_recorded_at": payload.get("recorded_at"),
        "orphan_recorded_by_run_id": payload.get("recorded_by_run_id"),
        "orphan_recorded_on_host": payload.get("recorded_on_host"),
        "resolved_on_host": socket.gethostname(),
        "process_probes": process_probes,
        "silence_probe": silence,
        "reason": reason_text,
        "run_id": state.run_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    artifact = state.save_artifact(
        _UNKNOWN_ORPHAN_RESOLUTION_TYPE,
        f"unknown_orphan_resolution_{state.run_id}_{time.time_ns()}",
        json.dumps(resolution, ensure_ascii=False, indent=2),
        metadata={"orphan_artifact_id": orphan_id, "output_roots": roots,
                  "classification": "unknown_orphan_resolution"},
    )
    state.append_transcript(
        "unknown_orphan_resolved", artifact_id=artifact.get("id"),
        orphan_artifact_id=orphan_id, output_roots=roots,
        processes_probed=len(process_probes), entries_checked=silence.get("entries_checked"))
    return {
        "status": "success", "artifact_id": artifact.get("id"),
        "orphan_artifact_id": orphan_id, "output_roots": roots,
        "process_probes": process_probes, "silence_probe": silence, "reused": False,
        "message": ("已铸造不可变 unknown_orphan_resolution：自述进程全部确证已死、"
                    "输出根自登记起静默。冲突扫描不再计这条隔离，重叠提交可继续；"
                    "orphan 记录本体保持原样，解除不可再否定。"),
    }

def _adopt_dead_foreign_intent(state: Any, reservation: dict[str, Any]) -> dict[str, Any]:
    """跨 run 残留 intent 的“死亡证明收养”通道（fail-closed）。

    上一 run 在 submit 在飞窗口被杀时，其 prepared intent 停在非终态并保留
    输出根，会把同 project 的后继 run 全部拦死。只有拿到正面死亡证据才收养：
    必须先证明本地作业隔离层可用 —— ``inspect_container`` 返回 ``exists=False``
    本身分不清“容器真没了”和“隔离层不可用”—— 再确证 intent 的容器不存在或已
    退出。活容器、隔离层不可用、探测失败、非 local 调度器一律保持拦截。
    """
    if str(reservation.get("scheduler") or "").lower() != "local":
        return {"adopted": False,
                "reason": "non_local_scheduler_has_no_container_probe"}
    container_ref = str(
        reservation.get("container_runtime_id")
        or reservation.get("job_id") or "").strip()
    if not container_ref:
        return {"adopted": False, "reason": "missing_container_identity"}
    from core import sandbox
    # 死亡证明必须用探活时刻的证据：进程级缓存里的“隔离层可用”可能早于
    # 后端失联，而 inspect 对后端不可达与容器不存在同样返回
    # exists=False，会把探活失败误当正面死亡证据。
    available, availability_detail = sandbox.availability(refresh=True)
    if not available:
        return {"adopted": False,
                "reason": ("liveness_probe_unavailable: 本地作业隔离层不可用"
                           f"（{availability_detail}），无法出具死亡证明")}
    inspected = sandbox.inspect_container(container_ref)
    if inspected.get("error"):
        return {"adopted": False,
                "reason": ("liveness_probe_unavailable: inspect 失败"
                           f"（{inspected['error']}），无法出具死亡证明")}
    if inspected.get("exists") and str(
            inspected.get("status") or "") not in {"exited", "dead"}:
        return {"adopted": False,
                "reason": f"container_alive: {inspected.get('status') or 'running'}"}
    probe = {
        "liveness_probe_available": True,
        "availability_detail": availability_detail,
        "container_ref": container_ref,
        "inspect": {key: inspected.get(key) for key in
                    ("exists", "status", "exit_code", "finished_at", "id")},
    }
    try:
        from .external_submission_recovery import record_intent_adoption
    except ImportError:
        from tools.external_submission_recovery import record_intent_adoption
    adoption = record_intent_adoption(state, reservation, probe)
    return {"adopted": True, "adoption_artifact_id": adoption.get("artifact_id")}


def _active_output_conflicts(state: Any, output_roots: list[str]) -> list[dict[str, Any]]:
    lifecycle = _job_lifecycle_states(state)
    candidates = _submission_payloads(state) + _task_external_jobs(state)
    conflicts: list[dict[str, Any]] = []
    try:
        try:
            from .external_submission_recovery import (
                unresolved_submission_output_reservations,
            )
        except ImportError:
            from tools.external_submission_recovery import (
                unresolved_submission_output_reservations,
            )
        unresolved = unresolved_submission_output_reservations(state)
    except Exception as exc:
        # 真实提交前无法读取 unknown-intent 账本时，不能假定没有遗留作业。
        unresolved = [{
            "status": "ledger_invalid",
            "reason": f"{type(exc).__name__}: {exc}",
            "output_roots": [],
        }]
    for reservation in unresolved:
        if reservation.get("foreign_run"):
            adoption_probe = _adopt_dead_foreign_intent(state, reservation)
            if adoption_probe.get("adopted"):
                continue
            reservation = {
                **reservation,
                "reason": (f"{reservation.get('reason')}；"
                           f"{adoption_probe.get('reason')}"),
            }
        old_roots = [
            os.path.realpath(os.path.expanduser(str(item)))
            for item in reservation.get("output_roots") or []
            if str(item).strip()
        ]
        if reservation.get("status") == "ledger_invalid":
            overlaps = [(root, root) for root in output_roots]
        else:
            overlaps = [
                (new, old)
                for new in output_roots
                for old in old_roots
                if _paths_overlap(new, old)
            ]
        if overlaps:
            conflicts.append({
                "kind": "external_submission_identity_unknown",
                "route_attempt_id": reservation.get("route_attempt_id"),
                "intent_artifact_id": reservation.get("intent_artifact_id"),
                "query_status": reservation.get("status"),
                "reason": reservation.get("reason"),
                "overlaps": overlaps,
                "do_not_resubmit": True,
            })
    for orphan in _unknown_orphan_records(state):
        overlaps = [(new, old) for new in output_roots for old in orphan.get("output_roots", []) if _paths_overlap(new, old)]
        if overlaps:
            conflicts.append({
                "kind": "unknown_orphan", "artifact_id": orphan.get("artifact_id"),
                "overlaps": overlaps, "note": orphan.get("note", ""),
                "recorded_at": orphan.get("recorded_at"),
                # 这条不是活作业，job_status/cancel_job/finalize 对它全都不适用；
                # 出路只有换目录或出具解除证据，必须写在冲突里。
                "resolution": (
                    "这是 record_unknown_orphan 铸下的隔离锁，不是在跑的作业："
                    "job_status/cancel_job/reconcile/finalize 都对它无效。"
                    "换一组不重叠的 output_paths 即可继续；确需复用这些根，调用 "
                    f'resolve_unknown_orphan(artifact_id="{orphan.get("artifact_id")}", '
                    'reason="<解除依据>")，它要求自述进程逐个机械确证已死'
                    "（pid=<数字>@<主机名> 只能在该主机上判定，container=<id> 由本地作业"
                    f'隔离层探活），且这些根自 {orphan.get("recorded_at")} 起无任何新写入。'),
            })
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        scheduler, job_id = str(candidate["scheduler"]), str(candidate["job_id"])
        identity = _job_key_for_record(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        lifecycle_resolution = lifecycle_for_submission(
            state, candidate, lifecycle_states=lifecycle)
        status = lifecycle_resolution.get("status")
        if lifecycle_resolution.get("resolution") == "legacy_lifecycle_scope_ambiguous":
            old_roots = _normalize_output_roots(candidate.get("output_roots"),
                                                candidate.get("workdir"),
                                                candidate.get("workdir") or ".")
            overlaps = [(new, old) for new in output_roots for old in old_roots
                        if _paths_overlap(new, old)]
            if overlaps:
                conflicts.append({
                    "kind": "legacy_lifecycle_scope_ambiguous",
                    "scheduler": scheduler, "job_id": job_id,
                    "namespace": candidate.get("namespace"),
                    "launch_host": candidate.get("launch_host"),
                    "lifecycle_artifact_id": lifecycle_resolution.get("lifecycle_artifact_id"),
                    "overlaps": overlaps,
                })
            continue
        if status is not None and status not in _ACTIVE_JOB_STATES:
            continue
        # A submission artifact alone is not proof that a job is still live.
        # Re-check it here so a naturally finished job does not permanently
        # reserve its workdir merely because no continuation has recorded a
        # terminal lifecycle transition yet. Failed status lookup remains a
        # conservative conflict: reusing a possibly-live output tree can
        # corrupt scientific files.
        live = _external_job_is_active(
            scheduler, job_id, candidate.get("namespace"),
            candidate.get("launch_host"), candidate.get("container_runtime_id"),
        )
        if not live:
            continue
        old_roots = _normalize_output_roots(candidate.get("output_roots"),
                                            candidate.get("workdir"),
                                            candidate.get("workdir") or ".")
        overlaps = [(new, old) for new in output_roots for old in old_roots
                    if _paths_overlap(new, old)]
        if overlaps:
            conflicts.append({"scheduler": scheduler, "job_id": job_id,
                              "task_id": candidate.get("task_id"), "overlaps": overlaps})
    return conflicts

def _external_job_is_active(scheduler: str, job_id: str, namespace: str | None,
                            remote_host: str | None = None,
                            container_runtime_id: str | None = None) -> bool:
    """Whether a prior job must still reserve outputs; unknown is unsafe."""
    result = _job_status_sync(
        scheduler, job_id, namespace, remote_host=remote_host,
        container_runtime_id=container_runtime_id,
    )
    raw = result.get("raw") or {}
    stdout = str(raw.get("stdout") or "")
    kind = str(scheduler).lower()
    if kind == "local":
        return stdout.strip() == "RUNNING" or not raw.get("ok")
    if kind == "slurm":
        # squeue prints no row after a completed/cancelled job.
        return not raw.get("ok") or bool(stdout.strip())
    if kind == "kubernetes" and raw.get("ok"):
        try:
            status = json.loads(stdout).get("status") or {}
            return not (status.get("completionTime") or status.get("failed"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
    # qstat success means the PBS job is still known; unsupported/failed
    # queries must reserve the path until a user reconciles it.
    return True


def pending_external_jobs(state: Any) -> list[dict[str, Any]]:
    """One-shot status of this run's active submitted jobs for preview/handoff."""
    lifecycle = _job_lifecycle_states(state)
    pending: list[dict[str, Any]] = []
    for submission in _submission_payloads(state):
        scheduler, job_id = str(submission["scheduler"]), str(submission["job_id"])
        lifecycle_resolution = lifecycle_for_submission(
            state, submission, lifecycle_states=lifecycle)
        lifecycle_status = lifecycle_resolution.get("status")
        if lifecycle_status is not None and lifecycle_status not in _ACTIVE_JOB_STATES:
            continue
        if lifecycle_resolution.get("resolution") == "legacy_lifecycle_scope_ambiguous":
            pending.append({"scheduler": scheduler, "job_id": job_id,
                            "job_name": submission.get("job_name"), "status": "unknown",
                            "lifecycle_resolution": lifecycle_resolution.get("resolution"),
                            "workdir": submission.get("workdir"),
                            "output_roots": submission.get("output_roots") or []})
            continue
        result = _job_status_sync(scheduler, job_id, submission.get("namespace"),
                                  remote_host=submission.get("launch_host"),
                                  container_runtime_id=submission.get("container_runtime_id"))
        raw = result.get("raw") or {}
        stdout = str(raw.get("stdout") or "")
        if scheduler.lower() == "local":
            status = "running" if stdout.strip() == "RUNNING" else ("finished_or_unavailable" if stdout.strip() == "NOT_RUNNING" else "unknown")
        elif scheduler.lower() == "slurm" and raw.get("ok"):
            status = "running" if stdout.strip() else "finished_or_unavailable"
        else:
            status = "unknown" if not raw.get("ok") else "running"
        if status in {"running", "unknown"}:
            pending.append({"scheduler": scheduler, "job_id": job_id,
                            "job_name": submission.get("job_name"), "status": status,
                            "workdir": submission.get("workdir"),
                            "output_roots": submission.get("output_roots") or []})
    return pending

def _run(args: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def _first_line(text: str, max_len: int = 500) -> str:
    return (text.splitlines()[0] if text else "")[:max_len]


def _local_resources() -> dict[str, Any]:
    # 主机 CPU / 内存怎么读，一处回答（`shared.lib.hostinfo`：psutil，跨平台）。旧写法
    # 直接读 /proc/meminfo，非 Linux 静默成 None。
    from shared.lib import hostinfo

    mem = hostinfo.memory()
    mem_total_mb = mem.total_bytes // (1024 * 1024) if mem.total_bytes else None
    mem_available_mb = (
        mem.available_bytes // (1024 * 1024) if mem.available_bytes is not None else None
    )

    gpus = []
    if shutil.which("nvidia-smi"):
        q = _run([
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ], timeout=8)
        if q["ok"]:
            for line in q["stdout"].splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    gpus.append({
                        "index": parts[0],
                        "name": parts[1],
                        "memory_total_mb": _to_int(parts[2]),
                        "memory_free_mb": _to_int(parts[3]),
                        "utilization_pct": _to_int(parts[4]),
                    })
    # 本机能不能受管地跑作业，看的是隔离后端守不守得住写边界，不是"本机存在"——
    # 此前这里固定写 True（#893）。
    from core import sandbox

    try:
        boundary_available, boundary_detail = sandbox.availability()
    except Exception as exc:  # 探不出来就如实说探不出来，不当成可用
        boundary_available, boundary_detail = False, f"{type(exc).__name__}: {exc}"
    return {
        "scheduler": "local",
        "available": bool(boundary_available),
        "availability_detail": boundary_detail,
        "cpu_count": hostinfo.logical_cpus(),
        "memory_total_mb": mem_total_mb,
        "memory_available_mb": mem_available_mb,
        "gpus": gpus,
        # nvidia-smi 看得见的卡 ≠ 本地作业用得上：原生后端不提供本地 GPU 执行
        # （core/sandbox.prepare_launch 对 gpus>0 直接拒）。
        "gpu_execution_supported": False,
    }


def _to_int(v: str) -> int | None:
    try:
        return int(v)
    except Exception:
        return None


def _detect_slurm() -> dict[str, Any]:
    available = bool(shutil.which("sinfo") and shutil.which("sbatch"))
    out = {
        "scheduler": "slurm",
        "available": available,
        "commands": {k: bool(shutil.which(k)) for k in ("sinfo", "squeue", "sbatch", "scancel")},
    }
    if not available:
        return out
    sinfo = _run(["sinfo", "-h", "-o", "%P|%a|%D|%t|%c|%m|%G"], timeout=12)
    partitions = []
    if sinfo["ok"]:
        for line in sinfo["stdout"].splitlines()[:80]:
            p = line.split("|")
            if len(p) >= 7:
                partitions.append({
                    "partition": p[0].rstrip("*"),
                    "default": p[0].endswith("*"),
                    "availability": p[1],
                    "nodes": _to_int(p[2]),
                    "state": p[3],
                    "cpus_per_node": _to_int(p[4]),
                    "memory_mb": _to_int(p[5]),
                    "gres": p[6],
                })
    out["partitions"] = partitions
    out["sinfo_error"] = None if sinfo["ok"] else _first_line(sinfo["stderr"])
    return out


def _detect_pbs() -> dict[str, Any]:
    available = bool(shutil.which("qsub") and shutil.which("qstat"))
    out = {
        "scheduler": "pbs",
        "available": available,
        "commands": {k: bool(shutil.which(k)) for k in ("qsub", "qstat", "pbsnodes", "qdel")},
    }
    if not available:
        return out
    qstat = _run(["qstat", "-Q"], timeout=12)
    queues = []
    if qstat["ok"]:
        for line in qstat["stdout"].splitlines()[2:80]:
            cols = line.split()
            if cols:
                queues.append({"queue": cols[0], "raw": line})
    out["queues"] = queues
    out["qstat_error"] = None if qstat["ok"] else _first_line(qstat["stderr"])
    return out


def _detect_k8s(namespace: str | None = None) -> dict[str, Any]:
    available = bool(shutil.which("kubectl"))
    out = {
        "scheduler": "kubernetes",
        "available": available,
        "commands": {"kubectl": available},
        # kubectl/集群可探测不等于 submit_job 已有 PVC/volume 映射契约。
        # 保留资源发现事实，但不能把该 transport 推荐为自动提交目标。
        "submission_contract_available": False,
        "submission_blocker": "kubernetes_volume_contract_required",
    }
    if not available:
        return out
    ns_args = ["-n", namespace] if namespace else []
    nodes = _run(["kubectl", "get", "nodes", "-o", "json", *ns_args], timeout=15)
    parsed_nodes = []
    if nodes["ok"]:
        try:
            doc = json.loads(nodes["stdout"])
            for item in doc.get("items", [])[:80]:
                status = item.get("status", {})
                alloc = status.get("allocatable", {})
                parsed_nodes.append({
                    "name": item.get("metadata", {}).get("name"),
                    "cpu": alloc.get("cpu"),
                    "memory": alloc.get("memory"),
                    "gpu_nvidia": alloc.get("nvidia.com/gpu"),
                })
        except Exception:
            pass
    out["nodes"] = parsed_nodes
    out["kubectl_error"] = None if nodes["ok"] else _first_line(nodes["stderr"])
    return out


def _redacted_capability_grant(grant: Any) -> dict[str, Any]:
    if not isinstance(grant, dict):
        return {}
    return {str(key): value for key, value in grant.items()
            if not re.search(r"(?:secret|token|password|credential|key)", str(key), re.IGNORECASE)}


def _core_platform_capabilities() -> dict[str, Any]:
    """Expose Core-owned declarations alongside live probes without duplicating authority."""
    software: list[dict[str, Any]] = []
    compute: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        from core.host_capabilities import load as load_host_software
        software = [
            {"name": item.name, "invoke": item.invoke, "version": item.version,
             "notes": item.notes, "resolvable": item.resolvable}
            for item in load_host_software()
        ]
    except Exception as exc:
        errors.append(f"host_software_unavailable:{type(exc).__name__}")
    try:
        from core.capabilities import snapshot as compute_snapshot
        capabilities, capability_error = compute_snapshot()
        compute = [
            {"kind": item.kind, "status": item.status, "detail": item.detail,
             "grant": _redacted_capability_grant(item.grant)}
            for item in capabilities
        ]
        if capability_error:
            errors.append(str(capability_error))
    except Exception as exc:
        errors.append(f"compute_grants_unavailable:{type(exc).__name__}")
    return {
        "authority": "Core deployment declarations are authoritative for registered software and authorized compute access; live probes only report current availability.",
        "host_software": software,
        "compute_grants": compute,
        "errors": errors,
    }


def _discover(namespace: str | None = None) -> dict[str, Any]:
    schedulers = [_local_resources(), _detect_slurm(), _detect_pbs(), _detect_k8s(namespace)]
    available = [s["scheduler"] for s in schedulers if s.get("available")]
    return {
        "status": "success",
        "core_platform_capabilities": _core_platform_capabilities(),
        "available_schedulers": available,
        "recommended_default": (
            "slurm" if "slurm" in available
            else "pbs" if "pbs" in available
            else "local"
        ),
        "resources": schedulers,
    }


async def _discover_resources(
    state: State,
    namespace: str | None = None,
    save_artifact: bool = True,
    **_: Any,
) -> dict:
    """Discover live resources while carrying Core-owned host declarations."""
    try:
        result = _discover(namespace)
        if save_artifact:
            name = f"resource_profile_{state.run_id}"
            state.save_artifact(
                "resource_profile", name,
                json.dumps(result, ensure_ascii=False, indent=2),
                metadata={"namespace": namespace, "schema_version": "1.0"},
            )
            result["artifact_name"] = name
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _default_memory_gb(total_cpus: int) -> float:
    """返回无领域资源计划时的既有保守内存建议。"""
    return max(2.0, max(1, int(total_cpus)) * 2.0)


def _recommend(
    task_type: str,
    mpi_ranks: int | None,
    cpus_per_rank: int | None,
    gpus: int | None,
    memory_gb: float | None,
    walltime_minutes: int | None,
    hourly_rate_usd: float | None,
    scheduler_preference: str | None,
    namespace: str | None,
) -> dict[str, Any]:
    profile = _discover(namespace)
    available = set(profile["available_schedulers"])
    sched = scheduler_preference if scheduler_preference in available else profile["recommended_default"]
    if gpus and gpus > 0 and "slurm" in available:
        sched = "slurm"
    elif sched == "local" and ((mpi_ranks or 1) > 1 or (gpus or 0) > 0):
        # Local can still run MPI, but make the limitation explicit.  GPU is not a
        # limitation but an absence: the native backends do not provide it (#893).
        pass

    ranks = mpi_ranks or 1
    cpr = cpus_per_rank or 1
    cpus = max(1, ranks * cpr)
    mem = memory_gb if memory_gb is not None else _default_memory_gb(cpus)
    wall = walltime_minutes or 60
    cost = None
    if hourly_rate_usd is not None:
        cost = hourly_rate_usd * (wall / 60.0)

    warnings = []
    local = next((r for r in profile["resources"] if r["scheduler"] == "local"), {})
    if sched == "local":
        if local.get("cpu_count") and cpus > local["cpu_count"]:
            warnings.append(f"requested {cpus} CPUs exceeds local cpu_count={local['cpu_count']}")
        if local.get("memory_available_mb") and mem * 1024 > local["memory_available_mb"]:
            warnings.append(f"requested {mem:.1f} GB exceeds local available memory")
        if gpus and gpus > 0:
            warnings.append(
                "local native backends do not provide GPU execution (visible GPUs are not "
                "usable by local jobs); submit_job(scheduler=local, gpus>0) is refused before "
                "approval — use a GPU scheduler such as slurm, or gpus=0 if no GPU is needed")

    return {
        "status": "success",
        "recommendation": {
            "scheduler": sched,
            "task_type": task_type,
            "mpi_ranks": ranks,
            "cpus_per_rank": cpr,
            "total_cpus": cpus,
            "gpus": gpus or 0,
            "memory_gb": mem,
            "walltime_minutes": wall,
            "estimated_resource_cost_usd": cost,
            "cost_basis": "caller_provided_hourly_rate" if hourly_rate_usd is not None else "not_estimated_no_rate",
            "warnings": warnings,
        },
        "resource_profile": profile,
    }


def _submission_memory_contract(
    state: State,
    *,
    memory_gb: float | None,
    total_cpus: int,
    use_build_resource_plan: bool = False,
) -> dict[str, Any]:
    """解析提交内存值、来源和准入策略，不创建第二份资源状态。"""
    if memory_gb is not None:
        return {
            "memory_gb": float(memory_gb),
            "source": "explicit_tool_argument",
            "mode": "fixed",
            "resource_plan_artifact_id": None,
        }

    plan = active_build_resource_plan(state) if use_build_resource_plan else {}
    requested = plan.get("requested_resources") or {}
    planned_memory = requested.get("memory_gb")
    if str(plan.get("status") or "") == "success":
        try:
            planned_value = float(planned_memory)
        except (TypeError, ValueError):
            planned_value = 0.0
        if math.isfinite(planned_value) and planned_value > 0:
            policy = str(plan.get("runtime_resource_policy") or "fixed")
            return {
                "memory_gb": planned_value,
                "source": "build_resource_plan",
                "mode": "flexible" if policy == "flexible" else "fixed",
                "resource_plan_artifact_id": plan.get("artifact_id"),
            }

    # 复用 recommend_resources 的既有公式。它只是带来源的保守回退，
    # 不是软件领域知识；高后果构建仍由 route/build_resource_plan 门要求计划。
    automatic = _default_memory_gb(total_cpus)
    return {
        "memory_gb": automatic,
        "source": "automatic_recommendation",
        "mode": "flexible",
        "resource_plan_artifact_id": None,
    }


async def _recommend_resources(
    state: State,
    task_type: str = "generic_hpc",
    mpi_ranks: int | None = None,
    cpus_per_rank: int | None = None,
    gpus: int | None = 0,
    memory_gb: float | None = None,
    walltime_minutes: int | None = None,
    hourly_rate_usd: float | None = None,
    scheduler_preference: str | None = None,
    namespace: str | None = None,
    **_: Any,
) -> dict:
    """Recommend resources for the current run without creating a research artifact."""
    try:
        result = _recommend(
            task_type, mpi_ranks, cpus_per_rank, gpus, memory_gb,
            walltime_minutes, hourly_rate_usd, scheduler_preference, namespace,
        )
        # A resource recommendation is transient execution planning, not a
        # scientific input or output.  Keep it available to the current run
        # for display/diagnostics, while the actual submitted allocation is
        # recorded by the job_submission artifact.
        state.hook_state["last_resource_recommendation"] = result
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _core_software_matches(profile: dict[str, Any], requirement: str) -> list[dict[str, Any]]:
    wanted = str(requirement or "").strip().casefold()
    if not wanted:
        return []
    declared = ((profile.get("core_platform_capabilities") or {}).get("host_software") or [])
    return [item for item in declared if isinstance(item, dict) and wanted in (
        str(item.get("name") or "").casefold() + " " + str(item.get("invoke") or "").casefold())]


async def _preflight_build_resources(
    state: State,
    compile_mode: str,
    mpi_ranks: int,
    cpus_per_rank: int,
    gpus: int,
    memory_gb: float,
    walltime_minutes: int,
    runtime_resource_policy: str,
    scheduler_preference: str | None = None,
    required_software: list[str] | None = None,
    namespace: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Advisory read of the chosen build mode against host declarations and a live resource snapshot.

    判决拆除·第三波（rm:701-787 缩成纯咨询，2026-09-02）：这曾是宪法档一点名的
    「ABI 预检/预测失败」型工具——返回 error/pause 并要求「不得配置/编译」。
    现在恒 status=success：capability_issues / capacity_warnings / recommendation
    如实给出，记进 build_resource_plan 产物与 transcript，编不编由调用方定；
    编译失败由现实（configure/链接器）自己拒绝。compile_mode /
    runtime_resource_policy / 资源整数下限由 schema 在派发口核（rm:718/720/723）。
    """
    mode = str(compile_mode or "").strip().lower()
    policy = str(runtime_resource_policy or "").strip().lower()
    # 上游把本工具缩成纯咨询（恒 success，不拦编译）。"不拦" 不等于 "照说可行"：
    # 非有限/非正的内存请求算不出容量结论，必须如实进 capability_issues，否则
    # decision=build_mode_feasible 就是框架自己编的一句假话，下游 build_resource_plan
    # 还会拿它当已获准的计划（NaN 比较恒假，连 <=0 都挡不住）。
    unusable_memory = (
        not isinstance(memory_gb, (int, float))
        or isinstance(memory_gb, bool)
        or not math.isfinite(float(memory_gb))
        or float(memory_gb) <= 0
    )
    recommendation = _recommend(
        "build_mode_preflight", mpi_ranks, cpus_per_rank, gpus, memory_gb,
        walltime_minutes, None, scheduler_preference, namespace,
    )
    profile = recommendation["resource_profile"]
    warnings = list(recommendation["recommendation"].get("warnings") or [])
    issues: list[str] = []
    software_evidence: list[dict[str, Any]] = []
    for item in required_software or []:
        if not isinstance(item, str) or not item.strip():
            issues.append("required_software entries must be non-empty strings")
            continue
        matches = _core_software_matches(profile, item)
        software_evidence.append({"requirement": item, "registered_matches": matches})
        if any(match.get("resolvable") is False for match in matches):
            issues.append(f"Core-registered software is currently unresolved: {item}")
    if mode in {"mpi", "hybrid"}:
        mpi_registered = any(_core_software_matches(profile, token)
                             for token in ("mpi", "mpicc", "mpifort", "mpirun"))
        mpi_local = any(shutil.which(binary) for binary in ("mpicc", "mpifort", "mpirun", "mpiexec"))
        if not mpi_registered and not mpi_local:
            issues.append("MPI build mode requested but neither Core registration nor a local MPI launcher/compiler is available")
    if unusable_memory:
        issues.append(
            "memory_gb must be a positive finite number; this plan cannot be used "
            f"as an approved build resource budget (declared: {memory_gb!r})")
    if mode in {"gpu", "hybrid"} and gpus < 1:
        issues.append("GPU/hybrid compile_mode requires gpus >= 1 in the pre-build resource plan")
    if issues:
        decision = "capability_concerns"
        advice = (
            "Advisory only: the declared build mode may not be runnable on this host/registration "
            "(see capability_issues). Resolve them, choose another approved resource target, or "
            "proceed and record the deviation in experiment_log; nothing here blocks configure/compile.")
    elif warnings and policy == "requires_user_approval":
        decision = "capacity_concerns_user_consent_declared"
        advice = (
            "Capacity warnings exist and the declared policy requires user consent for runtime "
            "changes: ask the user (request_human_input) before compiling, or proceed and record "
            "the choice; nothing here blocks configure/compile.")
    elif warnings:
        decision = "capacity_concerns"
        advice = (
            "Capacity warnings exist (see capacity_warnings). Compile only in the declared mode and "
            "record the warnings in experiment_log; runtime changes after compilation are limited "
            "to the declared policy.")
    else:
        decision = "build_mode_feasible"
        advice = (
            "Record this plan; compile only in the declared mode. Runtime changes after "
            "compilation are limited to the declared policy.")
    payload = {
        "status": "success", "decision": decision, "compile_mode": mode,
        "runtime_resource_policy": policy,
        "requested_resources": recommendation["recommendation"],
        "resource_profile": profile,
        "required_software": software_evidence,
        "capability_issues": issues, "capacity_warnings": warnings,
        "recommendation": advice,
    }
    state.hook_state["build_resource_preflight"] = payload
    try:
        artifact = state.save_artifact(
            "build_resource_plan", f"build_resource_plan_{state.run_id}",
            json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={"compile_mode": mode, "decision": decision, "schema_version": "1.0"},
        )
        payload["artifact_id"] = artifact.get("id")
    except Exception:
        pass
    try:
        state.append_transcript("build_resource_preflight", **payload)
    except Exception:
        pass
    return payload


def _hhmm(minutes: int) -> str:
    h, m = divmod(max(1, int(minutes)), 60)
    return f"{h:02d}:{m:02d}:00"


def _safe_job_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name.strip())
    return (cleaned or "experiment_job")[:64]


# 判决拆除·第三波（rm:1446 删 + `_reject_high_risk` 随删，2026-09-02）：
# `_submit_sync` 的高危分支条件永假（唯一调用方传 highrisk_authorized=not dry_run）；
# 高危 job 命令的真实门是 `_submit_job` 里那一次结构化 HITL 确认（含分类名）。


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _scheduler_path_block(
        *, label: str, value: str, allowed_roles: set[str],
        state: State) -> dict[str, Any]:
    """Return a caller-actionable failure for an unusable scheduler path.

    Scheduler work and scheduler-visible output have a narrower authority than
    a normal shell write. In particular, a one-off ``approved_write_root``
    must not turn into a durable job-output location: a submitted process can
    outlive the approval turn and write after the node has handed off.
    """
    matches = matching_path_roles(value, state)
    allowed_paths = sorted({
        role.path for role in collect_path_roles(state)
        if role.role in allowed_roles and role.writable
        and not role.container_only
    })
    has_approved_write_root = any(
        role.role == "approved_write_root" and role.writable
        and not role.container_only
        for role in matches
    )
    kind = (
        "approved_write_root_not_scheduler_usable"
        if has_approved_write_root else "scheduler_path_not_declared"
    )
    caller_action = (
        "Omit this scheduler path to use the framework-assigned run_root, or "
        "re-dispatch Experiment with node_inputs.path_roles.run_root or "
        "node_inputs.path_roles.build_root covering the required absolute path."
    )
    approval_note = (
        " approved_write_root is not a scheduler workdir or output role."
        if has_approved_write_root else ""
    )
    return {
        "status": "error",
        "error": (
            f"{label} must be inside a declared writable role from "
            f"{sorted(allowed_roles)}: {value}. "
            f"Declared usable roots: {allowed_paths}. {caller_action}"
            f"{approval_note}"
        ),
        "blocker": {
            "kind": kind,
            "field": label,
            "requested_path": value,
            "allowed_roles": sorted(allowed_roles),
            "declared_usable_roots": allowed_paths,
            "caller_action": caller_action,
            "human_action": "not_applicable",
        },
    }


def _unresolved_scheduler_path_block(
        *, label: str, value: str) -> dict[str, Any]:
    """Reject placeholders rather than guessing a cluster-visible location."""
    caller_action = (
        "Omit this scheduler path to use the framework-assigned run_root, or "
        "re-dispatch Experiment with an absolute node_inputs.path_roles.run_root "
        "or node_inputs.path_roles.build_root."
    )
    return {
        "status": "error",
        "error": (
            f"{label} contains an unresolved path placeholder: {value!r}. "
            "Experiment will not guess or ask a person to authorize a scheduler "
            f"path. {caller_action}"
        ),
        "blocker": {
            "kind": "unresolved_scheduler_path_placeholder",
            "field": label,
            "requested_path": value,
            "caller_action": caller_action,
            "human_action": "not_applicable",
        },
    }


def _has_unresolved_path_placeholder(value: str) -> bool:
    return bool(re.search(r"<[^<>]+>", value))


def _local_job_boundary_block(
        state: State, scheduler: str, workdir: str | None,
        output_dir: str | None, stage: str = "diagnostic") -> dict[str, Any] | None:
    """`scheduler="local"` 的作业在**本机**、以本进程子进程的身份跑。

    所以它受的是本机写边界，不是远端执行根那一套：`slurm`/`pbs` 能用共享盘上
    已声明的 run_root，是因为写发生在计算节点；local 没有这个差别。

    Core 的 write boundary 是默认真相源；若它不覆盖目标，只接受本进程已经消费
    人工确认并登记的 run-local capability。路径角色本身从不授予这项能力。拒绝
    发生在提交前，而不是让容器先启动再报只读文件系统。
    """
    if scheduler != "local":
        return None
    # stage 仅为旧内部调用签名的兼容参数，不授予额外路径权限。
    del stage
    from core.project_workspace import resolve_tool_path
    try:
        from .subprocess_policy import path_has_local_write_capability
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.subprocess_policy import path_has_local_write_capability

    for label, value in (("workdir", workdir), ("output_dir", output_dir)):
        if not value:
            continue
        try:
            resolve_tool_path(state, str(value), write=True)
        except Exception as exc:
            if path_has_local_write_capability(state, str(value)):
                continue
            try:
                state.append_transcript(
                    "local_job_outside_write_boundary",
                    field=label, path=str(value), reason=str(exc))
            except Exception:
                pass
            return {
                "status": "error",
                "error": (
                    f"scheduler='local' 的 {label} 必须落在本机写边界内"
                    f"（本节点工作区或本 run 目录）：{value}\n"
                    f"原因：{exc}\n"
                    "local 作业跑在提交机上，不享有 slurm/pbs 那条"
                    "「写发生在计算节点」的豁免。共享盘上的运行目录请用 "
                    "scheduler='slurm'/'pbs' 提交；确需在本机跑，就把 workdir/"
                    "output_dir 放进 run_root/build_root 的本地分配。"),
                "blocker": {
                    "kind": "local_job_outside_write_boundary",
                    "field": label,
                    "requested_path": str(value),
                    "human_action": "not_applicable",
                },
            }
    return None


def _scheduler_role_guard(
        state: State, scheduler: str, command: str,
        workdir: str | None, output_dir: str | None,
        output_roots: list[str], stage: str = "diagnostic") -> dict | None:
    # stage 不再是路径能力；所有路径都必须由 path_roles 本身授权。
    del stage
    contract = validate_path_roles(state)
    if not contract["valid"]:
        return {
            "status": "error",
            "error": "invalid path_roles: " + "; ".join(contract["errors"]),
        }
    for label, values, allowed_roles in (
            ("workdir", [workdir] if workdir else [],
             {"build_root", "run_root", "source_worktree_root"}),
            ("output_dir", [output_dir] if output_dir else [],
             {"build_root", "run_root"}),
            ("output_paths", output_roots, {"build_root", "run_root"}),
    ):
        for value in values:
            matches = matching_path_roles(value, state)
            if not any(
                    role.role in allowed_roles
                    and role.writable and not role.container_only
                    for role in matches):
                return _scheduler_path_block(
                    label=label, value=value, allowed_roles=allowed_roles,
                    state=state)
    # A scheduler script executes the complete command body. Checking only its
    # separate workdir field let `workdir=None; command="cd /outside && make"`
    # evade path roles. Reuse safe_bash's parser before any real submission.
    try:
        try:
            from .safe_bash import _scope_guard_bash
        except ImportError:
            from tools.safe_bash import _scope_guard_bash
        # Slurm/PBS payloads execute on a scheduler host. They still need
        # declared execution roots, but are not required to be inside this
        # submit host's Git worktree. Kubernetes needs an explicit volume
        # contract and therefore retains the local containment rule.
        command_block = _scope_guard_bash(
            state, command, cwd=workdir, remote=scheduler in {"slurm", "pbs"})
    except Exception as exc:
        return {"status": "error", "error": (
            "cannot validate job command path roles; refusing submission: "
            f"{type(exc).__name__}: {exc}")}
    if command_block is not None:
        return command_block
    # 判决拆除 O5（rm:1005 降格，2026-08-31）：workdir 落在 source_worktree_root
    # 而未声明 output_dir 时不再拒绝 —— 调用方（_submit_job）已把缺省 output_dir
    # 机械默认到 run 本地 runtime/logs 下并披露（scheduler_output_dir_defaulted）。
    return None



def _script_preview(script: str, command: str | None = None, limit: int = 2000) -> str:
    """Preview that can never hide the payload behind fixed boilerplate.

    The command being submitted is the LAST thing in a generated script, and
    everything above it is generated preamble that carries no per-run
    information.  A plain ``script[:limit]`` therefore spends the whole budget
    on boilerplate and drops the one line the caller is doing ``dry_run`` to
    check — silently, so the preview reads as a complete script.  Growing the
    preamble by a few hundred characters is enough to do it, which is exactly
    what happened when the identity preflight was added.

    Truncating the MIDDLE keeps that impossible: the payload lives in the tail
    budget, so no future preamble growth can push it out of view.  The elision
    marker states how much was dropped, so a truncated preview never reads as
    a whole one.
    """
    if len(script) <= limit:
        return script
    payload = command.strip() if command else ""
    if payload:
        marker = "\n# ... [generated preamble elided; complete payload follows] ...\n"
        header_budget = max(0, limit - len(marker) - len(payload) - 1)
        return f"{script[:header_budget]}{marker}{payload}\n"
    marker_template = "\n# ... [{n} characters elided from the generated preamble] ...\n"
    # Reserve the marker, then favour the tail: the payload is what dry_run is for.
    budget = max(0, limit - len(marker_template.format(n=len(script))))
    tail_len = (budget * 3) // 5
    head_len = budget - tail_len
    elided = len(script) - head_len - tail_len
    return (script[:head_len]
            + marker_template.format(n=elided)
            + script[len(script) - tail_len:])


def _identity_preflight_lines(workdir: str | None) -> list[str]:
    """Preamble run on the scheduler-assigned host before the payload.

    The failure this gate exists for is the classic compute-node one: the job
    lands on a host where the submitting user is not resolvable through the
    directory service, so writes fail in confusing ways much later.  That
    failure is only *possible* on a host that HAS a directory-service query
    mechanism.  "This platform ships no such tool" is a different fact from
    "the tool says I do not exist", and only the second one is an identity
    failure — conflating them made every job on a host without ``getent``
    (macOS, minimal containers) exit before its payload ran.

    So the source is probed first, and the strict cross-check runs only where
    a source exists.  Where none does, identity is reported as unverified
    (``source=none``) rather than failed, matching how an unreadable HOME is
    already reported as ``home_status=unavailable``.  The workdir check is
    platform-independent and stays fail-closed for every source.
    """
    workdir_text = os.path.expanduser(workdir) if workdir else ""
    lines = """# HARNESS scheduler identity preflight
HARNESS_IDENTITY_UID="$(id -u)"
HARNESS_IDENTITY_NAME="$(id -un 2>/dev/null || true)"
HARNESS_IDENTITY_HOME="${HOME:-}"
if command -v getent >/dev/null 2>&1; then
  HARNESS_IDENTITY_SOURCE=getent
  if ! HARNESS_IDENTITY_NSS="$(getent passwd "$HARNESS_IDENTITY_UID")"; then
    printf "HARNESS_IDENTITY_PREFLIGHT status=failed reason=nss_lookup uid=%s source=getent\n" "$HARNESS_IDENTITY_UID" >&2
    exit 86
  fi
  HARNESS_IDENTITY_NSS_UID="$(printf "%s\n" "$HARNESS_IDENTITY_NSS" | cut -d: -f3)"
  HARNESS_IDENTITY_NAME="$(printf "%s\n" "$HARNESS_IDENTITY_NSS" | cut -d: -f1)"
  HARNESS_IDENTITY_HOME="$(printf "%s\n" "$HARNESS_IDENTITY_NSS" | cut -d: -f6)"
elif command -v dscl >/dev/null 2>&1; then
  HARNESS_IDENTITY_SOURCE=dscl
  if [ -z "$HARNESS_IDENTITY_NAME" ] || ! HARNESS_IDENTITY_NSS_UID="$(dscl . -read "/Users/$HARNESS_IDENTITY_NAME" UniqueID 2>/dev/null | awk '{print $2}')" || [ -z "$HARNESS_IDENTITY_NSS_UID" ]; then
    printf "HARNESS_IDENTITY_PREFLIGHT status=failed reason=nss_lookup uid=%s source=dscl\n" "$HARNESS_IDENTITY_UID" >&2
    exit 86
  fi
else
  HARNESS_IDENTITY_SOURCE=none
  HARNESS_IDENTITY_NSS_UID="$HARNESS_IDENTITY_UID"
fi
if [ "$HARNESS_IDENTITY_NSS_UID" != "$HARNESS_IDENTITY_UID" ]; then
  printf "HARNESS_IDENTITY_PREFLIGHT status=failed reason=uid_mismatch uid=%s nss_uid=%s source=%s\n" "$HARNESS_IDENTITY_UID" "$HARNESS_IDENTITY_NSS_UID" "$HARNESS_IDENTITY_SOURCE" >&2
  exit 87
fi
if [ -n "$HARNESS_IDENTITY_HOME" ] && [ -d "$HARNESS_IDENTITY_HOME" ] && [ -x "$HARNESS_IDENTITY_HOME" ]; then
  HARNESS_IDENTITY_HOME_STATUS=ok
else
  HARNESS_IDENTITY_HOME_STATUS=unavailable
fi
""".splitlines()
    if workdir_text:
        lines += f"""HARNESS_IDENTITY_WORKDIR={shlex.quote(workdir_text)}
if [ ! -d "$HARNESS_IDENTITY_WORKDIR" ] || [ ! -r "$HARNESS_IDENTITY_WORKDIR" ] || [ ! -w "$HARNESS_IDENTITY_WORKDIR" ] || [ ! -x "$HARNESS_IDENTITY_WORKDIR" ]; then
  printf "HARNESS_IDENTITY_PREFLIGHT status=failed reason=workdir_access uid=%s workdir=%s\n" "$HARNESS_IDENTITY_UID" "$HARNESS_IDENTITY_WORKDIR" >&2
  exit 88
fi
""".splitlines()
    lines.append(
        "printf \"HARNESS_IDENTITY_PREFLIGHT status=pass uid=%s user=%s source=%s home_status=%s workdir=%s\\n\" \"$HARNESS_IDENTITY_UID\" \"$HARNESS_IDENTITY_NAME\" \"$HARNESS_IDENTITY_SOURCE\" \"$HARNESS_IDENTITY_HOME_STATUS\" \"${HARNESS_IDENTITY_WORKDIR:-}\"")
    return lines



def _stage_in_readable(state: State, source: Path) -> bool:
    """Is this file inside something this run may legitimately read from?

    读的范围本来就比写宽，所以这里不复用写角色表的语义，只用它当"本 run 认识
    这棵树"的证据，并补上 run 目录本身 —— 未绑 Project 的 run（CLI / SLURM
    直投）里，模型自己产出的输入文件就落在 `state.root` 下，而它不命中任何
    角色。少了这一条，unbound run 想暂存自己刚写好的输入文件会被判成
    "outside this run's readable roots"（2026-08-18 实测）。
    """
    for anchor in (getattr(state, "project_worktree", None),
                   getattr(state, "root", None)):
        if not anchor:
            continue
        try:
            if source.is_relative_to(Path(str(anchor)).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return bool(matching_path_roles(source, state))


def _validate_stage_in(state: State, stage_in: Any) -> list[dict[str, str]]:
    """Validate file-only staging without granting a new remote write root.

    每一项都带上 `sha256` 与 `mode`：两者都进提交确认的 payload，所以**批准之后
    再换掉源文件内容，批准就自动失效**（提交前会重算一次并比对）。只登记
    src/dst 字符串是不够的 —— 路径没变而内容变了，人批准的东西和实际暂存的
    东西就不是一回事。
    """
    if stage_in is None:
        return []
    if not isinstance(stage_in, list):
        raise ValueError("stage_in must be a list of {src, dst} objects")
    validated: list[dict[str, str]] = []
    for index, item in enumerate(stage_in):
        if not isinstance(item, dict):
            raise ValueError(f"stage_in[{index}] must be an object")
        src, dst = item.get("src"), item.get("dst")
        if not isinstance(src, str) or not isinstance(dst, str):
            raise ValueError(f"stage_in[{index}] requires string src and dst")
        source = Path(os.path.expanduser(src))
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            raise ValueError(f"stage_in[{index}].src must be an absolute regular file")
        source = source.resolve(strict=True)
        if not _stage_in_readable(state, source):
            raise ValueError(f"stage_in[{index}].src is outside this run's readable roots")
        target = Path(dst)
        if (not dst.strip() or target.is_absolute() or ".." in target.parts
                or str(target) in {".", ""}):
            raise ValueError(f"stage_in[{index}].dst must be a non-empty relative path without ..")
        # 可执行位保留，其余位不继承。固定 0644 会让暂存的求解器/脚本丢掉 +x，
        # 作业以 Permission denied 挂掉 —— 而那条错误正好落在重定向之前的
        # bootstrap 日志里，最难查的位置。
        mode = "0755" if os.stat(source).st_mode & 0o111 else "0644"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        validated.append({"src": str(source), "dst": target.as_posix(),
                          "mode": mode, "sha256": digest})
    return validated


def _stage_in_lines(workdir: str | None, stage_in: list[dict[str, str]]) -> list[str]:
    if not workdir or not stage_in:
        return []
    lines: list[str] = []
    for item in stage_in:
        src = shlex.quote(item["src"])
        dst = shlex.quote(os.path.join(os.path.expanduser(workdir), item["dst"]))
        lines += [
            f'test ! -L {dst} || exit 89',
            f'install -D -m {item.get("mode", "0644")} {src} {dst}',
        ]
    return lines


def _bootstrap_lines(workdir: str | None, output_dir: str | None, scheduler: str,
                     job_name: str) -> list[str]:
    """作业脚本开头的目录 bootstrap + 日志重定向。

    Kubernetes 不走这里：Pod 里的绝对路径和宿主机同名路径没有任何关系，
    `mkdir -p /beegfs/...` 会在容器内建出一个空目录，identity preflight 于是
    通过、作业却读不到任何输入 —— 比直接失败难查得多。K8s 要挂载就得显式声明
    volume/PVC，那是另一套契约。
    """
    if scheduler == "kubernetes":
        return []
    lines: list[str] = []
    if workdir:
        lines.append(f'mkdir -p {shlex.quote(os.path.expanduser(workdir))}')
    if output_dir:
        lines.append(f'mkdir -p {shlex.quote(os.path.expanduser(output_dir))}')
    names = _script_log_basenames(scheduler, job_name) if output_dir else None
    if names:
        out_name, err_name = names
        lines += [
            f'HARNESS_SCHEDULER_LOG_DIR={shlex.quote(os.path.expanduser(output_dir))}',
            _SCHEDULER_LOG_ID_LINE,
            f'exec >"$HARNESS_SCHEDULER_LOG_DIR/{out_name}" '
            f'2>"$HARNESS_SCHEDULER_LOG_DIR/{err_name}"',
        ]
    return lines


def _scheduler_memory_mebibytes(memory_gb: float) -> int:
    """把 API 的 GiB 预算向上取整成调度器可表达的 MiB，绝不截成 0。"""
    value = float(memory_gb)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("memory_gb must be a positive finite number")
    return max(1, math.ceil(value * 1024))


def _mount_contains(root: Path, target: Path) -> bool:
    return target == root or target.is_relative_to(root)


def _materialize_framework_local_sandbox_roots(
    state: State,
    targets: list[Path],
) -> None:
    """Materialize only canonical lazy roots needed by this local projection.

    A path-role declaration is never permission to create a caller-owned
    directory. The only roots this may create are the run-local allocations
    whose provenance was produced by the framework itself.
    """
    canonical_kinds = {
        "framework:experiment_build": "build",
        "framework:experiment_runtime": "runtime",
    }
    for role in collect_path_roles(state):
        kind = canonical_kinds.get(str(role.source))
        if (kind is None or not role.writable or role.container_only):
            continue
        root = Path(role.path).expanduser().resolve(strict=False)
        if not any(_mount_contains(root, target) for target in targets):
            continue
        canonical_root = experiment_output_dir(
            state, kind, create=False,
        ).expanduser().resolve(strict=False)
        if root != canonical_root:
            continue
        experiment_output_dir(state, kind, create=True)


def _local_job_sandbox_roots(
    state: State,
    command: str,
    workdir: Path,
    *,
    output_roots: list[str],
    scheduler_output_dir: Path,
    bootstrap_log_dir: Path,
    stage_in: list[dict[str, str]] | None = None,
    materialize_framework_roots: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Project one local detached job onto the smallest proven RW mounts.

    Path roles describe meaning, not host authority.  Reuse the Bash sandbox
    policy so every selected role is also backed by a Core or consumed-human
    local-write capability, then retain only the most-specific roots needed by
    this job's cwd and statically proven write targets.  Framework state stays
    under a parent read-only mount; only application output children may be
    rebound writable.
    """
    try:
        from .safe_bash import (
            REMOTE_SCRATCH, UNRESOLVED, _analyze_shell_path_effects,
        )
        from .subprocess_policy import (
            BashSandboxContractError, bash_sandbox_roots,
        )
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.safe_bash import (
            REMOTE_SCRATCH, UNRESOLVED, _analyze_shell_path_effects,
        )
        from tools.subprocess_policy import (
            BashSandboxContractError, bash_sandbox_roots,
        )

    cwd = workdir.expanduser().resolve(strict=False)
    effects = _analyze_shell_path_effects(command, str(cwd), remote=False)
    unresolved = sorted({
        target for _operation, target in effects
        if target in {UNRESOLVED, REMOTE_SCRATCH}
    })
    if unresolved:
        raise BashSandboxContractError(
            "local_job_path_analysis_unresolved: " + ", ".join(unresolved)
        )

    raw_targets = [
        *(target for _operation, target in effects),
        *output_roots,
        str(scheduler_output_dir),
        str(bootstrap_log_dir),
    ]
    if stage_in:
        raw_targets.extend(str(cwd / item["dst"]) for item in stage_in)

    targets: list[Path] = []
    for value in raw_targets:
        try:
            target = Path(str(value)).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BashSandboxContractError(
                f"local_job_invalid_write_target: {value!r}"
            ) from exc
        if not target.is_absolute():
            raise BashSandboxContractError(
                f"local_job_write_target_not_absolute: {value!r}"
            )
        if target not in targets:
            targets.append(target)

    if materialize_framework_roots:
        _materialize_framework_local_sandbox_roots(state, [cwd, *targets])

    writable_candidates, readonly = bash_sandbox_roots(
        state,
        str(cwd),
        authorized_targets=[str(target) for target in targets],
    )
    writable_candidates = [
        Path(root).expanduser().resolve(strict=False)
        for root in writable_candidates
    ]
    readonly = [
        Path(root).expanduser().resolve(strict=False) for root in readonly
    ]

    # A broad Core root and a specific path-role root may both cover a target.
    # Selecting the deepest candidate prevents the broader root from winning
    # Core's later mount minimisation.
    writable: list[Path] = []
    for target in targets:
        covering = [
            root for root in writable_candidates
            if _mount_contains(root, target)
        ]
        if not covering:
            raise BashSandboxContractError(
                f"local_job_write_target_not_capable: {target}"
            )
        selected = max(covering, key=lambda root: (len(root.parts), str(root)))
        if selected not in writable:
            writable.append(selected)

    # cwd need only be mounted, not necessarily writable (for example a
    # source_worktree with outputs directed to run_root).  Prefer an existing
    # read-only coverage; otherwise select its most-specific capable RW root.
    if not any(_mount_contains(root, cwd) for root in [*readonly, *writable]):
        covering = [
            root for root in writable_candidates
            if _mount_contains(root, cwd)
        ]
        if not covering:
            raise BashSandboxContractError(
                f"local_job_workdir_not_mounted: {cwd}"
            )
        selected = max(covering, key=lambda root: (len(root.parts), str(root)))
        if selected not in writable:
            writable.append(selected)

    state_root = Path(state.root).expanduser().resolve(strict=False)
    if any(_mount_contains(root, state_root) for root in writable):
        raise BashSandboxContractError(
            f"framework_state_root_write_forbidden: {state_root}"
        )
    if not any(_mount_contains(root, state_root) for root in readonly):
        raise BashSandboxContractError(
            f"framework_state_root_readonly_mount_missing: {state_root}"
        )

    # Stage sources are inputs.  Usually state/project/role overlays already
    # cover them; add an exact RO file mount only when no existing mount does.
    for item in stage_in or []:
        source = Path(item["src"]).expanduser().resolve(strict=True)
        if not any(
            _mount_contains(root, source) for root in [*readonly, *writable]
        ):
            readonly.append(source)

    # Docker accepts a RO parent followed by a RW child, but Core deliberately
    # rejects an exact path present in both sets.
    readonly = [root for root in readonly if root not in writable]
    return writable, readonly



def _local_enforcement_account() -> dict[str, Any]:
    """本地受管作业的能力记账 —— 照抄隔离层的回答，不自己编。

    这里以前写死 ``"enforcement": "docker_cgroup_and_pid1_supervisor"``。两个问题：

    1. **它说了假话**。Docker 已在 PR C 删干净，`core/isolation` 下只剩原生后端
       （linux = bwrap + Landlock + systemd cgroup，darwin = seatbelt）；收据里却
       告诉读者这个作业跑在 Docker 容器的 cgroup 与 PID1 监护下。
    2. **它是常量，永远不变**。同一个字符串在守得住和守不住的宿主上一模一样 ——
       2026-09-07 实测：UI 侧 worker 够不到 systemd 用户会话，`mem_cap`/`pids_cap`
       双双落不下去（issue #849），而收据照旧宣称有 cgroup 监护。

    真相源只有一个，而且它早就答着了：`enforcement_snapshot()` 的 ``enforced``
    与 ``missing_for_unattended``（同文件 `the_backend_accounts_pids_in_a_cgroup`
    已是这个用法）。答不上来时如实记 ``unknown`` 并说明原因，**不猜、不沉默** ——
    宁可让读者看到"这次不知道"，也不能让他把不存在的边界当成已兑现。
    """
    try:
        from core import isolation

        snap = isolation.enforcement_snapshot()
        enforced = sorted(str(x) for x in (snap.get("enforced") or []))
        missing = sorted(str(x) for x in (snap.get("missing_for_unattended") or []))
        return {
            "enforcement": "native_managed_job",
            "enforcement_backend": str(snap.get("backend") or "unknown"),
            "enforced_invariants": enforced,
            "missing_for_unattended": missing,
        }
    except Exception as exc:
        return {
            "enforcement": "unknown",
            "enforcement_backend": "unknown",
            "enforcement_unknown_reason": f"{type(exc).__name__}: {exc}",
        }


def the_backend_accounts_pids_in_a_cgroup() -> bool:
    """当前执行后端是不是**用 cgroup 记 pids** —— 也就是 payload 采样得到
    ``/sys/fs/cgroup/pids.events``。

    这个问题只有隔离层能答，而它早就答着了：`enforcement_snapshot()` 的
    ``enforced`` 里有没有 ``pids_cap``。这里不自己判平台、不自己找路径 ——
    "哪些不变量守得住"是隔离层的职责，节点再答一遍就是第二个真相源。

    ## 为什么需要这个判断（2026-09-06 真机实测）

    下面那段 pids prelude 的 docstring 写着它是给「detached **Docker** cgroup」
    用的，而 Docker 已在 PR C 删干净。它却仍然无条件注入每一个
    ``scheduler=local`` 的作业：原生后端（macOS seatbelt、Linux Landlock+bwrap）
    下根本没有那个容器 cgroup 可采样，于是**每一个本地作业在跑 payload 之前
    就 exit 127**（`HARNESS_SANDBOX_ERROR pids_events_unavailable phase=baseline`）。

    实测后果：Mac 安装包上一个真课题走到 experiment 节点，三条执行通道全废，
    9 条闭合条件 0 条兑现，一行科学计算都没跑成。

    ## 判不出来时按"没有"算

    这道 guard 的作用是把**被 cgroup 拒掉的 fork** 变成一个权威的非零终态；
    不注入它并不会放宽任何上限 —— 上限要么由 cgroup 真的守着，要么这个后端
    本来就不守（那时 `missing_for_unattended` 里会有 `pids_cap`，自主档的准入
    按 profile 决定，见 `services/unattended.py`）。所以"不确定"时不注入，
    损失的是证据不是防线；反过来注入则是让整台机器一个作业都跑不了。
    """
    try:
        from core import isolation

        enforced = isolation.enforcement_snapshot().get("enforced") or []
    except Exception:
        return False
    return "pids_cap" in {str(item) for item in enforced}


def _local_pids_event_prelude() -> list[str]:
    """Bash-builtins-only baseline probe for the job's own cgroup.

    路径两级解析：Docker cgroup namespace 时代 `/sys/fs/cgroup/pids.events`
    固定可读（容器视角即自己的 cgroup）；原生后端（systemd-run --scope）作业的
    cgroup 在层级深处，按 `/proc/self/cgroup` 现场解析（v2 单行 `0::<path>`；
    v1 取 pids 控制器行）。两级都读不到 → **如实警告并跳过采样，不再 fatal**：
    采样是证据不是防线（TasksMax/pids.max 才是墙，见
    `the_backend_accounts_pids_in_a_cgroup` 的判据）；Docker 年代的 exit 127
    在路径不固定的原生宿主上会让每个本地作业在 payload 之前出生即死
    （2026-09-06 本机实测：sleep 5 全部 127）。
    """
    return [
        f"HARNESS_PIDS_EVENTS_PATH={shlex.quote(_LOCAL_PIDS_EVENTS_PATH)}",
        "harness_resolve_pids_events() {",
        '  [ -r "$HARNESS_PIDS_EVENTS_PATH" ] && return 0',
        "  HARNESS_CG_REL=",
        "  while IFS=: read -r HARNESS_CG_HIER HARNESS_CG_CTL HARNESS_CG_PATH; do",
        '    case "$HARNESS_CG_CTL" in',
        '      ""|*pids*) HARNESS_CG_REL="$HARNESS_CG_PATH" ;;',
        "    esac",
        "  done < /proc/self/cgroup",
        '  [ -n "$HARNESS_CG_REL" ] || return 1',
        '  HARNESS_PIDS_EVENTS_PATH="/sys/fs/cgroup${HARNESS_CG_REL}/pids.events"',
        '  [ -r "$HARNESS_PIDS_EVENTS_PATH" ]',
        "}",
        "harness_read_pids_max() {",
        "  HARNESS_PIDS_MAX_VALUE=",
        '  [ -r "$HARNESS_PIDS_EVENTS_PATH" ] || return 1',
        '  while read -r HARNESS_PIDS_KEY HARNESS_PIDS_VALUE; do',
        '    [ "$HARNESS_PIDS_KEY" = max ] || continue',
        '    case "$HARNESS_PIDS_VALUE" in',
        "      ''|*[!0-9]*) return 1 ;;",
        "    esac",
        '    HARNESS_PIDS_MAX_VALUE="$HARNESS_PIDS_VALUE"',
        "    break",
        '  done < "$HARNESS_PIDS_EVENTS_PATH"',
        '  [ -n "$HARNESS_PIDS_MAX_VALUE" ]',
        "}",
        "if harness_resolve_pids_events && harness_read_pids_max; then",
        '  HARNESS_PIDS_MAX_BEFORE="$HARNESS_PIDS_MAX_VALUE"',
        "else",
        "  printf '%s\\n' 'HARNESS_SANDBOX_ERROR pids_events_unavailable phase=baseline non_fatal=1 (cgroup 证据采样跳过；pids 上限仍由后端 TasksMax 强制)' >&2",
        "  HARNESS_PIDS_MAX_BEFORE=",
        "fi",
    ]


def _local_pids_event_terminal_lines() -> list[str]:
    """Turn a new pids.max denial into an authoritative non-zero terminal.

    baseline 没采到（HARNESS_PIDS_MAX_BEFORE 为空）就没有可比的增量 ——
    如实透传 payload 退出码，不把证据缺失变成作业失败（与 baseline 段同一判决）。
    """
    return [
        'HARNESS_PAYLOAD_RC="$?"',
        "set -e",
        'if [ -n "$HARNESS_PIDS_MAX_BEFORE" ] && harness_read_pids_max; then',
        '  HARNESS_PIDS_MAX_AFTER="$HARNESS_PIDS_MAX_VALUE"',
        '  if [ "$HARNESS_PIDS_MAX_AFTER" -gt "$HARNESS_PIDS_MAX_BEFORE" ]; then',
        "    printf 'HARNESS_SANDBOX_LIMIT pids baseline=%s final=%s\\n' \\",
        '      "$HARNESS_PIDS_MAX_BEFORE" "$HARNESS_PIDS_MAX_AFTER" >&2',
        f"    exit {_LOCAL_PIDS_LIMIT_EXIT_CODE}",
        "  fi",
        'elif [ -n "$HARNESS_PIDS_MAX_BEFORE" ]; then',
        "  printf '%s\\n' 'HARNESS_SANDBOX_ERROR pids_events_unavailable phase=terminal non_fatal=1' >&2",
        "fi",
        'exit "$HARNESS_PAYLOAD_RC"',
        "",
    ]


def _script_for(
    scheduler: str,
    command: str,
    job_name: str,
    mpi_ranks: int,
    cpus_per_rank: int,
    gpus: int,
    memory_gb: float,
    walltime_minutes: int | None,
    queue: str | None,
    nodelist: str | None,
    image: str | None,
    workdir: str | None,
    output_dir: str | None = None,
    stage_in: list[dict[str, str]] | None = None,
    bootstrap_dir: str | None = None,
    submission_nonce: str | None = None,
    hard_deadline_s: int | None = None,
) -> str:
    quoted = command.strip()
    stage_lines = _stage_in_lines(workdir, stage_in or [])
    bootstrap = _bootstrap_lines(workdir, output_dir, scheduler, job_name)
    cd_line = f"cd {shlex.quote(os.path.expanduser(workdir))}" if workdir else None
    boot_dir = shlex.quote(os.path.expanduser(bootstrap_dir)) if bootstrap_dir else None
    if scheduler == "slurm":
        lines = [
            "#!/usr/bin/env bash",
            f"#SBATCH --job-name={job_name}",
            f"#SBATCH --ntasks={mpi_ranks}",
            f"#SBATCH --cpus-per-task={cpus_per_rank}",
            f"#SBATCH --mem={_scheduler_memory_mebibytes(memory_gb)}M",
        ]
        if walltime_minutes is not None:
            lines.append(f"#SBATCH --time={_hhmm(walltime_minutes)}")
        if submission_nonce:
            lines.append(f"#SBATCH --comment=ai4s:{submission_nonce}")
        if boot_dir:
            # 调度器自己的 stdout/stderr 只覆盖脚本内 `exec` 重定向**之前**那段
            # （mkdir / stage-in / identity preflight）。它必须指向一个提交时就
            # 存在的目录 —— 脚本自己就写在 job_dir 里，所以那里一定存在。
            # 不给这两条指令的代价是：bootstrap 阶段的失败进 SLURM 默认的
            # `slurm-<id>.out`，落在 sbatch 继承的 cwd（平台进程工作目录）里，
            # 既没人去读也会堆积垃圾文件。
            lines.append(f"#SBATCH --output={boot_dir}/bootstrap.out")
            lines.append(f"#SBATCH --error={boot_dir}/bootstrap.err")
        if queue:
            lines.append(f"#SBATCH --partition={queue}")
        if nodelist:
            lines.append(f"#SBATCH --nodelist={nodelist}")
        if gpus:
            lines.append(f"#SBATCH --gres=gpu:{gpus}")
        lines += ["set -eo pipefail", *bootstrap]
        lines += _identity_preflight_lines(workdir)
        lines += stage_lines
        if cd_line:
            lines.append(cd_line)
        lines += [quoted, ""]
        return "\n".join(lines)
    if scheduler == "pbs":
        lines = [
            "#!/usr/bin/env bash",
            f"#PBS -N {job_name}",
            f"#PBS -l select=1:ncpus={mpi_ranks * cpus_per_rank}:mem={_scheduler_memory_mebibytes(memory_gb)}mb",
        ]
        if walltime_minutes is not None:
            lines.append(f"#PBS -l walltime={_hhmm(walltime_minutes)}")
        if boot_dir:
            # 同 SLURM：只承接 exec 重定向之前那段，落在提交时必然存在的目录。
            # PBS 的指令行不做变量展开，所以用固定名 —— job_dir 本身已按
            # 时间戳+作业名唯一，不会互相覆盖。
            lines.append(f"#PBS -o {boot_dir}/bootstrap.out")
            lines.append(f"#PBS -e {boot_dir}/bootstrap.err")
        else:
            lines.append("#PBS -j oe")
        if queue:
            lines.append(f"#PBS -q {queue}")
        if gpus:
            lines.append(f"#PBS -l ngpus={gpus}")
        if submission_nonce:
            lines.append(
                f"#PBS -v AI4S_SUBMISSION_NONCE=ai4s:{submission_nonce}")
        lines += ["set -eo pipefail", *bootstrap]
        lines += _identity_preflight_lines(workdir)
        lines += stage_lines
        lines.append(cd_line or "cd \"$PBS_O_WORKDIR\"")
        lines += [quoted, ""]
        return "\n".join(lines)
    if scheduler == "kubernetes":
        img = image or "ubuntu:24.04"
        cpu = str(max(1, mpi_ranks * cpus_per_rank))
        mem = f"{_scheduler_memory_mebibytes(memory_gb)}Mi"
        gpu_line = f'\n              nvidia.com/gpu: "{gpus}"' if gpus else ""
        kubernetes_payload = json.dumps("\n".join(
            ["set -eo pipefail", *bootstrap, *_identity_preflight_lines(workdir),
             *stage_lines, cd_line or ":", quoted]))
        deadline = (
            f"  activeDeadlineSeconds: {int(hard_deadline_s)}\n"
            if hard_deadline_s is not None else ""
        )
        return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name.lower()}
  labels:
    ai4s-harness/submission-nonce: "{submission_nonce or "unknown"}"
spec:
{deadline}  template:
    spec:
      restartPolicy: Never
      containers:
        - name: {job_name.lower()}
          image: {img}
          command: ["/bin/bash", "-lc"]
          args:
            - {kubernetes_payload}
          resources:
            requests:
              cpu: "{cpu}"
              memory: "{mem}"{gpu_line}
            limits:
              cpu: "{cpu}"
              memory: "{mem}"{gpu_line}
"""
    if scheduler == "local" and the_backend_accounts_pids_in_a_cgroup():
        # cgroup 后端：把 pids 证据留在 cgroup 里，两次读都用 Bash 内建，
        # 这样一个被拒的 fork 也阻止不了终态修正本身。
        #
        # **只在这个后端下注入**：这段 prelude 采样的是 cgroup 的
        # `pids.events`，那是 cgroup 提供的东西。原生后端（seatbelt /
        # Landlock+bwrap）没有它，无条件注入会让每个本地作业在跑 payload
        # 之前 exit 127 —— 见 `the_backend_accounts_pids_in_a_cgroup` 的注释。
        lines = [
            "#!/usr/bin/env bash",
            "set -eo pipefail",
            *_local_pids_event_prelude(),
            "set +e",
            "(",
            "set -eo pipefail",
            *bootstrap,
            *_identity_preflight_lines(workdir),
            *stage_lines,
        ]
        if cd_line:
            lines.append(cd_line)
        lines += [quoted, ")", *_local_pids_event_terminal_lines()]
        return "\n".join(lines)

    lines = ["#!/usr/bin/env bash", "set -eo pipefail", *bootstrap]
    lines += _identity_preflight_lines(workdir)
    lines += stage_lines
    if cd_line:
        lines.append(cd_line)
    lines += [quoted, ""]
    return "\n".join(lines)


def _scheduler_output_dir(
    scheduler: str,
    workdir: str | None,
    job_dir: Path,
    output_dir: str | None = None,
) -> Path:
    """Choose a scheduler-visible stdout/stderr directory.

    SLURM/PBS jobs often run on compute nodes where the submit host's sandbox
    `/tmp` is not mounted.  If a shared workdir is provided, scheduler output
    must go under that shared filesystem; otherwise the job can fail before the
    script body starts because the stdout/stderr path cannot be opened.
    """
    if output_dir:
        return Path(os.path.expanduser(output_dir))
    if scheduler in {"slurm", "pbs"} and workdir:
        return Path(os.path.expanduser(workdir)) / "logs"
    return job_dir


def _output_path_patterns(scheduler: str, output_dir: Path, job_name: str) -> dict[str, str]:
    base = str(output_dir)
    if scheduler == "slurm":
        return {
            "stdout_path_pattern": f"{base}/%x-%j.out",
            "stderr_path_pattern": f"{base}/%x-%j.err",
        }
    if scheduler == "pbs":
        return {
            "stdout_path_pattern": f"{base}/{job_name}.out",
            "stderr_path_pattern": f"{base}/{job_name}.err",
        }
    return {}


#: 脚本内把调度器作业号取进这个变量，重定向文件名再用它拼。
_SCHEDULER_LOG_ID_LINE = (
    'HARNESS_SCHEDULER_LOG_ID="${SLURM_JOB_ID:-${PBS_JOBID:-unknown}}"')


def _script_log_basenames(scheduler: str, job_name: str) -> tuple[str, str] | None:
    """脚本内重定向要写的文件名 —— **由 `_output_path_patterns` 推导**。

    这两处必须同源。分开写会分叉，而且分叉是静默的：2026-08-18 实测，pattern
    对外宣告 `<job_name>-<job_id>.out`，脚本里 `exec >` 写的却是 `<job_id>.out`，
    于是 `job_health` 拿 `stdout_path` 做进度/停滞判定时永远看到 `exists:False`，
    一个正常跑着的作业被判成"没有任何输出"；交接简报里给模型的也是那条不存在
    的路径。改这里之前先想清楚 pattern 那边跟不跟着变。
    """
    patterns = _output_path_patterns(scheduler, Path("."), job_name)
    if not patterns:
        return None

    def _render(pattern: str) -> str:
        # %x 在渲染期已知（job_name 已过 _safe_job_name，shell 安全）；
        # %j 只有运行期才知道，换成脚本里那个变量。
        return (os.path.basename(pattern)
                .replace("%x", job_name)
                .replace("%j", "$HARNESS_SCHEDULER_LOG_ID"))

    return _render(patterns["stdout_path_pattern"]), _render(patterns["stderr_path_pattern"])


def _bootstrap_log_patterns(scheduler: str, job_dir: Path) -> dict[str, str]:
    """`exec` 重定向**之前**那段（mkdir / stage-in / identity preflight）的落点。

    重定向必须在 mkdir 之后（外部日志目录可能还不存在），所以 mkdir 自己的失败
    没法写进外部日志 —— 而那恰好是 bootstrap 新引入的失败模式。给调度器一个
    **提交时就一定存在**的本地目录（job_dir，脚本自己就写在那儿），既不丢诊断，
    也不会把 `slurm-<id>.out` 撒在 sbatch 的 cwd（平台进程的工作目录）里。
    """
    if scheduler not in {"slurm", "pbs"}:
        return {}
    base = str(job_dir)
    # 固定文件名（不带 %j）：job_dir 已按时间戳+作业名唯一，无覆盖风险；而 PBS
    # 的指令行不展开变量，两边用同一个名字才不会再分出第二套命名。
    return {
        "bootstrap_stdout_path_pattern": f"{base}/bootstrap.out",
        "bootstrap_stderr_path_pattern": f"{base}/bootstrap.err",
    }


def _resolve_output_paths(patterns: dict[str, str], job_name: str, job_id: str | None) -> dict[str, str]:
    if not job_id:
        return {}
    out: dict[str, str] = {}
    for key, value in patterns.items():
        resolved = value.replace("%x", job_name).replace("%j", str(job_id))
        out[key.replace("_pattern", "")] = resolved
    return out


def _persist_submission_intent(
    state: State | None,
    base: dict[str, Any],
    *,
    route_attempt_id: str | None,
) -> dict[str, Any]:
    """在 scheduler/Docker launch 不可逆动作前强制落盘唯一 intent。"""
    if state is None:
        return {
            "status": "error",
            "reason": "submission_intent_state_missing",
        }
    nonce = str(base.get("submission_nonce") or "").strip()
    payload = {
        **base,
        "intent_status": "prepared",
        "route_attempt_id": route_attempt_id,
        "command_sha256": hashlib.sha256(
            str(base.get("command") or "").encode("utf-8")
        ).hexdigest(),
    }
    artifact_name = f"external_submission_intent_{state.run_id}_{nonce}"
    metadata = {
        "submission_nonce": nonce,
        "scheduler": base.get("scheduler"),
        "route_attempt_id": route_attempt_id,
    }
    try:
        artifact = state.save_artifact(
            "external_submission_intent",
            artifact_name,
            json.dumps(payload, ensure_ascii=False, indent=2),
            metadata=metadata,
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": "submission_intent_persistence_failed",
            "error_type": type(exc).__name__,
        }
    try:
        state.append_transcript(
            "external_submission_intent_persisted",
            artifact_id=artifact.get("id"),
            submission_nonce=nonce,
            scheduler=base.get("scheduler"),
            route_attempt_id=route_attempt_id,
            script_sha256=base.get("script_sha256"),
        )
    except Exception as exc:
        # artifact 已经落盘而不可逆提交尚未发生。若把 prepared 留在账本里，
        # 恢复器只能保守地把它当成“可能已提交”，永久占住输出路径。以同一
        # artifact 身份写 v2 明确封成 pre-submit abort；补偿写失败时才保留
        # prepared/unknown，绝不删除意图或猜测调度器状态。
        compensated = _terminalize_submission_intent(
            state,
            base,
            route_attempt_id=route_attempt_id,
            intent_status="aborted_before_submit",
            submission_boundary_crossed=False,
            terminal_reason="intent_transcript_persistence_failed",
        )
        if compensated.get("status") != "success":
            return {
                "status": "error",
                "reason": "submission_intent_persistence_failed",
                "error_type": type(exc).__name__,
                "intent_status": "prepared_outcome_unknown",
                "compensation_error_type": compensated.get("error_type"),
                "artifact_id": artifact.get("id"),
                "do_not_resubmit": True,
            }
        return {
            "status": "error",
            "reason": "submission_intent_persistence_failed",
            "error_type": type(exc).__name__,
            "intent_status": "aborted_before_submit",
            "artifact_id": compensated.get("artifact_id"),
            "submission_boundary_crossed": False,
        }
    return {"status": "success", "artifact_id": artifact.get("id")}


def _terminalize_submission_intent(
    state: State | None,
    base: dict[str, Any],
    *,
    route_attempt_id: str | None,
    intent_status: str,
    submission_boundary_crossed: bool,
    terminal_reason: str,
    terminal_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用同一 artifact 身份封口一次已持久化的提交意图。"""
    if state is None:
        return {"status": "error", "reason": "submission_intent_state_missing"}
    nonce = str(base.get("submission_nonce") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        **base,
        "intent_status": intent_status,
        "route_attempt_id": route_attempt_id,
        "command_sha256": hashlib.sha256(
            str(base.get("command") or "").encode("utf-8")
        ).hexdigest(),
        "submission_boundary_crossed": submission_boundary_crossed,
        "terminal_at": now,
        "terminal_reason": terminal_reason,
    }
    if intent_status == "aborted_before_submit":
        payload.update({"aborted_at": now, "abort_reason": terminal_reason})
    elif intent_status == "rejected_by_scheduler":
        payload["scheduler_acceptance"] = False
    if terminal_evidence:
        payload["terminal_evidence"] = terminal_evidence
    try:
        artifact = state.save_artifact(
            "external_submission_intent",
            f"external_submission_intent_{state.run_id}_{nonce}",
            json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={
                "submission_nonce": nonce,
                "scheduler": base.get("scheduler"),
                "route_attempt_id": route_attempt_id,
            },
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": "submission_intent_terminal_persistence_failed",
            "error_type": type(exc).__name__,
            "intent_status": "prepared_outcome_unknown",
            "do_not_resubmit": True,
        }
    return {
        "status": "success",
        "artifact_id": artifact.get("id"),
        "intent_status": intent_status,
        "submission_boundary_crossed": submission_boundary_crossed,
    }

def _local_sandbox_path_contract_error(
    exc: BaseException,
    *,
    scheduler: str,
    dry_run: bool,
    job_name: str,
) -> dict[str, Any]:
    detail = str(exc)
    reason = (
        "path_capability_required"
        if "path_capability_required" in detail
        else "local_sandbox_path_contract_rejected"
    )
    return {
        "status": "error",
        "error": f"local sandbox path contract rejected: {detail}",
        "reason": reason,
        "scheduler": scheduler,
        "dry_run": dry_run,
        "job_name": job_name,
        "blocker": {"kind": reason},
    }


@dataclass(frozen=True)
class _LocalSandboxProjection:
    """One local job's proven filesystem view, computed once per submission.

    ``mount_roots`` is the resolved bind-mount set (writable + read-only) that
    `_local_job_sandbox_roots` already had to compute for the path contract.
    Inside those trees host and container agree on path and content, so the
    host filesystem may adjudicate a payload target.  Everything else the job
    sees comes from the image, where the host has no say at all.
    """

    workdir: Path
    mount_roots: list[str]
    writable_roots: list[str] = field(default_factory=list)
    """可写子集（payload 自建产物的判定域：缺席目标落在这里 = 本作业将构建它）。"""


def _preflight_local_submission_sandbox(
    state: State,
    command: str,
    *,
    workdir: str | None,
    output_roots: list[str],
    runtime_root: Path,
    output_dir: str | None,
    job_name: str,
    stage_in: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any] | None, _LocalSandboxProjection | None]:
    """Validate local Docker mounts before a route attempt is persisted.

    This projection may materialize only framework-owned canonical build/runtime
    allocations that cover its bootstrap, log, workdir, or output targets; it
    never materializes caller-owned roots. The provisional job directory is
    only a projected path. `_submit_sync` repeats the check at the actual job
    directory as a TOCTOU defense, but deterministic path-contract failures
    must return before `route_step_bound` can exist.

    Returns ``(block, projection)``. The projection carries the job's effective
    cwd and the resolved mount set; the payload executable preflight needs both
    so that "does this file exist" is asked of the same filesystem view the job
    will actually get.
    """
    safe_name = _safe_job_name(job_name)
    provisional_job_dir = (
        runtime_root / "jobs" / f".preflight-{uuid.uuid4().hex}_{safe_name}"
    )
    scheduler_output_dir = _scheduler_output_dir(
        "local", workdir, provisional_job_dir, output_dir,
    )
    try:
        if workdir:
            local_workdir = Path(workdir).expanduser().resolve(strict=True)
            if not local_workdir.is_dir():
                raise NotADirectoryError(str(local_workdir))
        else:
            local_workdir = provisional_job_dir.expanduser().resolve(strict=False)
        writable, readonly = _local_job_sandbox_roots(
            state,
            command,
            local_workdir,
            output_roots=output_roots,
            scheduler_output_dir=scheduler_output_dir,
            bootstrap_log_dir=provisional_job_dir,
            stage_in=stage_in,
            materialize_framework_roots=True,
        )
    except Exception as exc:
        return _local_sandbox_path_contract_error(
            exc,
            scheduler="local",
            dry_run=False,
            job_name=safe_name,
        ), None
    return None, _LocalSandboxProjection(
        workdir=local_workdir,
        mount_roots=[str(root) for root in (*writable, *readonly)],
        writable_roots=[str(root) for root in writable],
    )


_PAYLOAD_PREFLIGHT_NEXT_STEPS = (
    "\n\n下一步二选一（本次拒绝零执行、零 route attempt 消耗，step 仍是 "
    "ready，修正后直接重新调用 submit_job）：\n"
    "① 拆成两个 route 步骤：先提交只做构建的 build 步骤"
    "（workdir_role=build_root）产出二进制并核实产物存在，再单独提交只执行"
    "既有二进制的 run 步骤；\n"
    "② 用 stage_in=[{src, dst}] 交付已构建好的可执行文件（src 带可执行位时"
    "收据 mode=0755，预检按收据直接放行），命令引用 workdir 下的 dst 路径。"
)


async def _preflight_local_payload_executables(
    state: State,
    command: str,
    *,
    sandbox: _LocalSandboxProjection,
    stage_in: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """本地真实提交前，对 payload 中路径形态的可执行目标（含 mpirun 目标）
    执行与本地 safe_run_bash 同口径的存在性/执行位/ABI 预检。

    - 相对目标一律按作业真正的 cwd（`sandbox.workdir`；caller 没给 workdir
      时就是本次作业目录）解析，不落到 harness 进程 cwd 上。
    - stage_in 将物化的目标不看当前磁盘，按收据判定（mode=0755 放行）；其
      ABI 探测在 src 上进行，因为 dst 在提交前尚不存在。
    - 判定范围就是 `_local_job_sandbox_roots` 刚刚为这次作业解析出来的 bind
      mount 集合（`sandbox.mount_roots`）——不是"声明过的路径角色"这种近似。
      挂载点内宿主与容器同路径同内容，宿主机说了算；其余（镜像自带的
      /usr、/opt/<toolchain> 等绝对系统路径）宿主机没有判定权，一律放行。
    - 失败返回结构化 block：零执行、零 intent、零 route attempt 消耗。
    - 通过时返回按 sha256 钉住的既存目标清单；它进入提交确认 payload，并由
      `_submit_sync` 在 adapter.prepare 之前复检（TOCTOU 二道防线，与
      stage_in 的内容钉住同构）。
    """
    try:
        from .safe_bash import (
            _exec_preflight_bash, _host_verifiable_exec_target,
            _is_non_bypassable_framework_block, _runtime_abi_preflight_bash,
            collect_exec_path_targets)
    except ImportError:
        from tools.safe_bash import (
            _exec_preflight_bash, _host_verifiable_exec_target,
            _is_non_bypassable_framework_block, _runtime_abi_preflight_bash,
            collect_exec_path_targets)

    workdir = str(sandbox.workdir)
    host_verifiable_roots = list(sandbox.mount_roots)
    staged_targets: dict[str, dict[str, str]] = {}
    substitutions: dict[str, str] = {}
    for item in stage_in or []:
        dst_abs = os.path.abspath(os.path.join(
            workdir, str(item.get("dst") or "")))
        receipt = {"mode": str(item.get("mode") or ""),
                   "sha256": str(item.get("sha256") or ""),
                   "src": str(item.get("src") or "")}
        staged_targets[dst_abs] = receipt
        if receipt["src"]:
            substitutions[dst_abs] = receipt["src"]

    # 判决拆除（2026-09-06 E2E 活体撞出）：payload **自建产物**不做存在性预检。
    # `configure && build && ./产物` 是一条批处理作业的正常形态——最后入口由同一
    # payload 构建出来，提交时不存在不是失败预测的依据（同款墙 wave3 已在
    # safe_bash 拆除：照跑，真实失败由 rc 127/126 + exec_diagnosis 兜住）。
    # 判据是事实：目标缺席 且 落在本作业已证明的可写根内 → 合成 0755 收据放行，
    # 并 transcript 如实披露（不参与内容钉住——还没有内容可钉）。既存目标与
    # 可写根外的缺席目标（真·打错路径）仍走原预检教育。
    internal_writable = [
        Path(root).expanduser().resolve(strict=False)
        for root in getattr(sandbox, "writable_roots", []) or []
    ]
    payload_internal_targets: list[str] = []
    if internal_writable:
        try:
            from .safe_bash import _expand_cmd_vars, _split_shell_segments
        except ImportError:
            from tools.safe_bash import _expand_cmd_vars, _split_shell_segments
        segments = list(_split_shell_segments(_expand_cmd_vars(command)))

        def _has_producer_before(segment_text: str) -> bool:
            # 该段之前至少有一个非 cd 的实段（有机会构建目标）；首个实段的
            # 缺席目标仍是打错路径，老墙照拦。
            for seg in segments:
                if seg == segment_text:
                    return False
                if seg.strip() and not re.match(r"^cd\s", seg.strip()):
                    return True
            return False

        for entry in collect_exec_path_targets(command, cwd=workdir):
            target = entry["path"]
            key = os.path.abspath(os.path.join(workdir, target))
            if key in staged_targets or os.path.exists(key):
                continue
            if not _has_producer_before(str(entry.get("segment") or "")):
                continue
            resolved = Path(key)
            if any(resolved == root or resolved.is_relative_to(root)
                   for root in internal_writable):
                staged_targets[key] = {"mode": "0755", "sha256": "", "src": "",
                                       "payload_internal": "true"}
                payload_internal_targets.append(key)
    if payload_internal_targets:
        try:
            state.append_transcript(
                "payload_internal_exec_targets",
                targets=payload_internal_targets,
                reason=("declared exec targets are products of this very payload; "
                        "existence preflight waived, reality adjudicates at run time"),
            )
        except Exception:
            pass
    exec_block = _exec_preflight_bash(
        command,
        cwd=workdir,
        staged_targets=staged_targets,
        # payload 在受管容器/作业环境内运行，宿主机 shebang 解释器解析对它
        # 不成立，交由既有 bootstrap/运行时报错语义处理。
        check_script_interpreter=False,
        # 同理，存在性/执行位也只在受管挂载点内成立。
        host_verifiable_roots=host_verifiable_roots,
    )
    if exec_block is not None:
        return {
            "status": "error",
            "reason": "payload_exec_preflight_rejected",
            "error": (str(exec_block.get("error") or "")
                      + _PAYLOAD_PREFLIGHT_NEXT_STEPS),
            "blocker": {"kind": "payload_exec_preflight_rejected"},
        }, []
    abi_block = await _runtime_abi_preflight_bash(
        state, command, cwd=workdir, path_substitutions=substitutions,
        host_verifiable_roots=host_verifiable_roots)
    if abi_block is not None:
        if _is_non_bypassable_framework_block(abi_block):
            # 框架探测自身不可用：保留其原始 reason/blocker 语义（同样是
            # 提交前零消耗拒绝），不伪装成 payload ABI 事实。
            return abi_block, []
        return {
            "status": "error",
            "reason": "payload_abi_preflight_rejected",
            "error": (str(abi_block.get("error") or "")
                      + _PAYLOAD_PREFLIGHT_NEXT_STEPS),
            "blocker": {"kind": "payload_abi_preflight_rejected"},
        }, []
    pinned: list[dict[str, str]] = []
    for entry in collect_exec_path_targets(command, cwd=workdir):
        path = entry["path"]
        if path in staged_targets:
            continue   # 内容已由 stage_in 收据钉住，并在提交前既有复检覆盖
        if any(item["path"] == path for item in pinned):
            continue
        if not _host_verifiable_exec_target(path, host_verifiable_roots):
            continue   # 镜像内目标：宿主机同名文件不是被批准执行的那一个
        if not os.path.isfile(path):
            continue   # 目录/设备等非常规文件不参与内容钉住
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
        pinned.append({"path": path, "sha256": digest})
    return None, pinned


@dataclass
class _PreparedSubmissionLaunch:
    """Memory-only handoff between prepare, durable intent, and launch.

    This is deliberately not a job record or a new lifecycle. The existing
    submission intent and job_submission artifacts remain the only durable
    truth. A prepared local Docker launch has already reserved its sandbox
    control directory, so it must be abandoned if intent persistence fails
    before the irreversible runtime invocation.
    """

    intent_fields: dict[str, Any]
    opaque: Any


@runtime_checkable
class _SubmissionLaunchAdapter(Protocol):
    """Narrow launch seam; status/cancel/finalize retain existing ownership."""

    adapter_id: str

    def prepare(self, spec: dict[str, Any]) -> _PreparedSubmissionLaunch:
        """Allocate a launch candidate without starting its payload."""

    def launch(self, prepared: _PreparedSubmissionLaunch) -> dict[str, Any]:
        """Cross the irreversible runtime submission boundary exactly once."""

    def abandon(self, prepared: _PreparedSubmissionLaunch) -> None:
        """Release a prepared but definitely unsubmitted candidate."""

    def accepted_receipt(
        self,
        prepared: _PreparedSubmissionLaunch,
        launch_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Project immutable identity returned after a successful launch."""


class _NativeLocalJobLaunchAdapter:
    """Adapter over Core 的本地 detached 作业启动原语（`sandbox.prepare_launch`）。

    只持有一次性的 launch 状态；持久 intent、身份恢复、状态查询、取消与
    finalize 仍走既有 Experiment 路径。

    2026-09-09 改名：原名 ``_LocalDockerLaunchAdapter`` / ``local-docker``。
    Docker 已在 PR C 删干净，`core/isolation` 下只剩原生后端，旧名让模型、
    用户和审计把不存在的容器边界当成真实的。

    **保留的 wire alias 及其删除条件**：Core 交回的 launch 对象字段仍叫
    ``container_name`` / ``image_id``，收据里的 ``container_runtime_id`` 与
    ``sandbox_contract.adapter="docker"`` 也照旧写出 —— 历史收据要能继续查询、
    取消、恢复和 finalize，身份不能被重解释。这些 alias 的 owner 是 Core
    （`core/sandbox.py` 的 `NativeJobLaunch`、`stop_container`、
    `inspect_container`）；**Core 那边改名之后**，节点这边同批删除。
    """

    adapter_id = "native-local-job"

    def prepare(self, spec: dict[str, Any]) -> _PreparedSubmissionLaunch:
        from core import sandbox

        launch = sandbox.prepare_launch(
            spec["argv"],
            cwd=spec["cwd"],
            writable_roots=spec["writable_roots"],
            readonly_roots=spec["readonly_roots"],
            limits=spec["limits"],
            detached=True,
            stdout_path=spec["stdout_path"],
            stderr_path=spec["stderr_path"],
            gpus=spec["gpus"],
        )
        return _PreparedSubmissionLaunch(
            intent_fields={
                "job_id": launch.container_name,
                "sandbox_control_dir": str(launch.control_dir),
                "sandbox_image": sandbox.image_name(),
                "sandbox_image_id": launch.image_id,
            },
            opaque=launch,
        )

    def launch(self, prepared: _PreparedSubmissionLaunch) -> dict[str, Any]:
        return _run(prepared.opaque.argv, timeout=30)

    def abandon(self, prepared: _PreparedSubmissionLaunch) -> None:
        prepared.opaque.cleanup()

    def accepted_receipt(
        self,
        prepared: _PreparedSubmissionLaunch,
        launch_result: dict[str, Any],
    ) -> dict[str, Any]:
        del prepared
        return {
            "container_runtime_id": str(launch_result.get("stdout") or "").strip(),
        }


def _submission_launch_adapter_for(
    scheduler: str,
) -> _SubmissionLaunchAdapter | None:
    """Return only deployment-owned launch adapters.

    This private factory is intentionally not a registry or tool parameter:
    callers cannot use it to select a future remote backend before Core and
    the platform provide its trusted execution contract.
    """

    if str(scheduler).lower() == "local":
        return _NativeLocalJobLaunchAdapter()
    return None


def _submit_sync(
    runtime_root: Path,
    scheduler: str,
    command: str,
    job_name: str,
    mpi_ranks: int,
    cpus_per_rank: int,
    gpus: int,
    memory_gb: float,
    storage_gb: float,
    walltime_minutes: int | None,
    queue: str | None,
    nodelist: str | None,
    image: str | None,
    workdir: str | None,
    dry_run: bool,
    namespace: str | None,
    output_dir: str | None = None,
    output_paths: list[str] | None = None,
    execution_class: str | None = None,
    expected_duration_s: int | None = None,
    execution_params: dict[str, Any] | None = None,
    health_check: dict[str, Any] | None = None,
    *,
    # 这两个从这里起是 keyword-only 且**必填**（issue #791）：它们决定沙箱挂载集合、
    # stage-in 拷贝步骤与本地提交的状态闸，漏传的代价是"预检按它们放行、提交却
    # 不带它们"。2026-09-04 现网：唯一生产调用点 22 个位置实参 + 7 个关键字，两个
    # 都没传，`state` 恒 None → 本地真实提交全拒（部署门 6 红）；`stage_in` 的缺失
    # 被前一个缺陷挡着没显形。默认值就是这种漏传能沉默的原因 —— 删掉默认值，
    # 漏传在调用那一刻就是 TypeError，不是运行期一个理由指错的 error dict。
    stage_in: list[dict[str, str]] | None,
    state: State | None,
    guard_process_tree: bool = False,
    submission_nonce: str | None = None,
    route_attempt_id: str | None = None,
    memory_contract: dict[str, Any] | None = None,
    precomputed_build_limits: BuildLimits | None = None,
    hard_deadline_s: int | None = None,
    payload_executables: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    job_name = _safe_job_name(job_name)
    scheduler = scheduler.lower()
    if scheduler == "local":
        workdir = os.path.realpath(os.path.expanduser(workdir)) if workdir else None
        output_dir = os.path.realpath(os.path.expanduser(output_dir)) if output_dir else None
    queue = _safe_scheduler_directive(queue, "queue", scheduler)
    nodelist = _safe_scheduler_directive(nodelist, "nodelist", scheduler)
    submission_nonce = str(submission_nonce or uuid.uuid4().hex)
    job_dir = runtime_root / "jobs" / f"{submission_nonce}_{job_name}"
    sched_output_dir = _scheduler_output_dir(scheduler, workdir, job_dir, output_dir)
    output_patterns = _output_path_patterns(scheduler, sched_output_dir, job_name)
    output_patterns.update(_bootstrap_log_patterns(scheduler, job_dir))
    output_roots = _normalize_output_roots(output_paths, workdir, runtime_root)
    local_workdir: Path | None = None
    local_mount_roots: tuple[list[Path], list[Path]] | None = None
    if scheduler == "local" and not dry_run:
        if state is None:
            return {
                "status": "error",
                "error": "local submission requires run state",
                "reason": "local_submission_state_missing",
                "scheduler": scheduler,
                "dry_run": dry_run,
                "job_name": job_name,
            }
        # payload 可执行目标复检（TOCTOU 二道防线，与 stage_in 内容钉住
        # 同构）：人批准的是预检时那些内容的 sha256；批准之后目标内容变了，
        # 批准就对它不成立。必须早于脚本、intent 与 adapter.prepare。
        for pinned in payload_executables or []:
            pinned_path = str(pinned.get("path") or "")
            try:
                current = hashlib.sha256(
                    Path(pinned_path).read_bytes()).hexdigest()
            except OSError:
                current = None
            if current == str(pinned.get("sha256") or ""):
                continue
            state.append_transcript(
                "job_submission_payload_executable_changed",
                scheduler=scheduler, job_name=job_name, path=pinned_path)
            return {
                "status": "error",
                "reason": "payload_executable_changed_after_approval",
                "error": (
                    "可执行目标的内容在批准之后发生了变化，本次批准已失效"
                    f"（payload_executables 复检，未提交）：{pinned_path}\n"
                    "下一步：重新调用 submit_job 发起提交并重新确认——新内容"
                    "的 sha256 会重新进入确认 payload；若该变化并非本意，先"
                    "恢复文件内容再重新提交。"),
                "scheduler": scheduler,
                "dry_run": dry_run,
                "job_name": job_name,
                "blocker": {
                    "kind": "payload_executable_changed_after_approval",
                    "path": pinned_path,
                    "human_action": "resubmit_and_reconfirm",
                },
            }
        try:
            if workdir:
                local_workdir = Path(workdir).expanduser().resolve(strict=True)
                if not local_workdir.is_dir():
                    raise NotADirectoryError(str(local_workdir))
            else:
                # The framework-owned job directory is created only after the
                # path/capability contract succeeds.
                local_workdir = job_dir.expanduser().resolve(strict=False)
            local_mount_roots = _local_job_sandbox_roots(
                state,
                command,
                local_workdir,
                output_roots=output_roots,
                scheduler_output_dir=sched_output_dir,
                bootstrap_log_dir=job_dir,
                stage_in=stage_in,
            )
        except Exception as exc:
            return _local_sandbox_path_contract_error(
                exc,
                scheduler=scheduler,
                dry_run=dry_run,
                job_name=job_name,
            )
    try:
        # Remote scheduler output is bootstrapped inside the job script.  A
        # submit-host mkdir here would recreate the unsafe local /beegfs write.
        if scheduler not in {"slurm", "pbs"}:
            sched_output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {
            "status": "error",
            "error": f"cannot create scheduler output_dir {sched_output_dir}: {type(e).__name__}: {e}",
            "scheduler": scheduler,
            "dry_run": dry_run,
            "job_name": job_name,
        }
    ext = "yaml" if scheduler == "kubernetes" else "sh"
    script_path = job_dir / f"{job_name}.{ext}"
    script = _script_for(
        scheduler, command, job_name, mpi_ranks, cpus_per_rank, gpus,
        memory_gb, walltime_minutes, queue, nodelist, image, workdir, str(sched_output_dir),
        stage_in, str(job_dir), submission_nonce,
        hard_deadline_s=hard_deadline_s,
    )
    _write(script_path, script)
    if scheduler != "kubernetes":
        script_path.chmod(0o600 if dry_run else 0o700)

    base = {
        "status": "success",
        "scheduler": scheduler,
        "dry_run": dry_run,
        "job_name": job_name,
        # Persist a complete handoff identity.  A later run must be able to
        # inspect the submission without depending on the original shell.
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "submission_nonce": submission_nonce,
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "command": command,
        "workdir": os.path.expanduser(workdir) if workdir else None,
        "namespace": namespace,
        "route_attempt_id": route_attempt_id,
        "script_path": str(script_path),
        "script_preview": _script_preview(script, command),
        "scheduler_output_dir": str(sched_output_dir),
        # 框架生成的目录（脚本自己就写在这儿），从不来自模型 —— job_health 因此
        # 可以直接把它当作可读根，不必要求它落在 output_roots 里。
        "bootstrap_log_dir": str(job_dir),
        "output_roots": output_roots,
        "stage_in": stage_in or [],
        "memory_contract": dict(memory_contract or {}),
        "expected_duration_s": expected_duration_s,
        "time_contract": {
            "expected_duration_s": expected_duration_s,
            "hard_deadline_s": hard_deadline_s if scheduler == "local" else None,
            "scheduler_walltime_minutes": (
                walltime_minutes if scheduler in {"slurm", "pbs"} else None
            ),
            "scheduler_walltime_source": (
                ("explicit" if walltime_minutes is not None
                 else "site_default_unknown")
                if scheduler in {"slurm", "pbs"}
                else "not_applicable"
            ),
        },
        **output_patterns,
    }
    if scheduler == "local":
        from core.sandbox import image_name

        sandbox_walltime_s = the_local_walltime_seconds(walltime_minutes, hard_deadline_s)
        base["sandbox_contract"] = {
            # 这条记的是「谁把作业放到 OS 上」。Docker 已在 PR C 删干净，
            # `core/isolation` 下只剩原生后端；写 "docker" 会让读者以为作业跑在
            # 容器里。`adapter_kind` 是新字段，`adapter` 保留为一次性 wire alias
            # 供旧收据读取方过渡（删除条件见 _NativeLocalJobLaunchAdapter 的
            # docstring）。
            "adapter": "docker",
            "adapter_kind": "native_local_job",
            "image": image_name(),
            "network": "none",
            "memory_gb": memory_gb,
            "storage_gb": storage_gb,
            # cpus 是**请求值**（mpi_ranks × cpus_per_rank），不是已生效的限额：Core 的
            # 隔离义务里没有 CPU_CAP（core/isolation 的 Invariant），只有 linux 后端在
            # systemd --user scope 可用时才把它翻成 CPUQuota，节点看不到这一步是否发生
            # （#841）。字段名留给旧读者，来源与生效性另行标明。
            "cpus": mpi_ranks * cpus_per_rank,
            "cpus_source": "requested",
            "cpu_limit_enforcement": "backend_dependent_unverified",
            "walltime_seconds": sandbox_walltime_s,
            "walltime_source": (
                "explicit_hard_deadline"
                if hard_deadline_s is not None
                else "platform_safety_default"
            ),
            "pids_event_guard": {
                "source": (f"{_LOCAL_PIDS_EVENTS_PATH}:max_delta "
                           "(self-cgroup fallback via /proc/self/cgroup)"),
                "limit_exit_code": _LOCAL_PIDS_LIMIT_EXIT_CODE,
                "unavailable_behavior": "non_fatal_warning",
            },
        }
    try:
        base["health_contract"] = _health_contract(
            health_check, workdir=workdir, output_roots=output_roots,
            scheduler_output_dir=sched_output_dir, expected_duration_s=expected_duration_s,
        )
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "scheduler": scheduler,
                "dry_run": dry_run, "job_name": job_name}
    build_limits = precomputed_build_limits
    if execution_class == "toolchain_build" or guard_process_tree:
        if build_limits is None:
            contract_mode = str((memory_contract or {}).get("mode") or "")
            enforced_deadline_s = (
                hard_deadline_s if scheduler == "local"
                else walltime_minutes * 60 if walltime_minutes is not None
                else None
            )
            build_limits = derive_build_limits(
                state, command, enforced_deadline_s,
                requested_memory_gb=memory_gb,
                requested_total_cpus=(mpi_ranks * cpus_per_rank),
                exact_resource_contract=(
                    contract_mode == "fixed"
                    if memory_contract else scheduler == "local"
                ),
                memory_request_source=(memory_contract or {}).get("source"),
            )
        if scheduler == "local":
            base["resource_guard"] = {
                **build_limits.public(),
                **_local_enforcement_account(),
                "hard_deadline_enforced": hard_deadline_s is not None,
                "live_resource_health": "compatibility_only",
            }
        else:
            base["resource_guard"] = {
                "enforcement": (
                    "scheduler_memory_time_contract"
                    if walltime_minutes is not None
                    else "scheduler_memory_contract_site_walltime_unknown"
                ),
                "memory_gb": memory_gb,
                "walltime_minutes": walltime_minutes,
                "node_derived_advisory": build_limits.public(),
                "platform_verification_required": [
                    "pids", "disk_pressure_monitoring", "filesystem_quota"],
            }
    if dry_run:
        base["submit_command"] = _submit_command(scheduler, script_path, namespace)
        if scheduler == "local":
            # 信号反转防线：dry_run 只渲染脚本，不跑 payload 可执行目标预检；
            # 说清楚这一点，agent 才不会把"dry_run 成功"记成"预检通过"，
            # 然后在真实提交处被拒。
            base["payload_exec_preflight"] = {
                "evaluated": False,
                "reason": "dry_run",
                "note": (
                    "dry_run 只渲染脚本：payload 可执行目标的存在性/执行位/ABI "
                    "预检不在这里跑，dry_run 成功不代表预检会通过。该预检只在 "
                    "dry_run=false 的真实提交前跑，判定范围是那次提交解析出的"
                    "沙箱挂载集合（作业 cwd 及其可写/只读挂载根）；stage_in 的 "
                    "dst 按收据 mode 判定；落在挂载集合之外的目标"
                    "（/usr/local/bin/python3.12 这类由镜像提供的绝对系统路径）"
                    "宿主机没有判定权，一律放行、不做存在性判定。"),
            }
        return base

    # 判决拆除·第三波（rm:1536 删，2026-09-02）：scheduler 由 schema enum 核，
    # 同条件二次拒绝删；真到不了这里的 None 由现实（subprocess TypeError）兜住。
    cmd = _submit_command(scheduler, script_path, namespace)
    if scheduler == "local":
        out = job_dir / f"{job_name}.out"
        err = job_dir / f"{job_name}.err"
        out.touch(exist_ok=False)
        err.touch(exist_ok=False)
        from core import sandbox as _sandbox

        assert state is not None
        assert local_workdir is not None
        assert local_mount_roots is not None
        writable, readonly = local_mount_roots
        total_cpus = mpi_ranks * cpus_per_rank
        limits = _sandbox.SandboxLimits(
            memory_bytes=max(64 * 1024**2, int(memory_gb * 1024**3)),
            cpus=float(total_cpus),
            pids=min(4096, max(128, total_cpus * 32)),
            walltime_seconds=the_local_walltime_seconds(walltime_minutes, hard_deadline_s),
            storage_bytes=max(64 * 1024**2, int(storage_gb * 1024**3)),
            output_bytes=min(64 * 1024**2, max(1, int(storage_gb * 1024**3))),
            tmpfs_bytes=max(64 * 1024**2, min(512 * 1024**2, int(memory_gb * 1024**3 / 4))),
        )
        adapter = _submission_launch_adapter_for(scheduler)
        if adapter is None:
            return {
                **base,
                "status": "error",
                "reason": "local_launch_adapter_unavailable",
                "error": "deployment-owned local Docker launch adapter is unavailable",
            }
        try:
            from shared.lib.shell import bash_shell

            prepared = adapter.prepare({
                "argv": [bash_shell(), str(script_path)],
                "cwd": local_workdir,
                "writable_roots": writable,
                "readonly_roots": readonly,
                "limits": limits,
                "stdout_path": out,
                "stderr_path": err,
                "gpus": gpus,
            })
        except Exception as exc:
            # prepare 失败发生在 intent 与 launch 之前：零执行的准入拒绝，
            # 用可识别 reason 让路线层写可恢复 outcome 而非永久 failed。
            return {
                **base,
                "status": "error",
                "reason": "sandbox_admission_rejected",
                "error": f"sandbox rejected local job: {exc}",
            }
        base.update(prepared.intent_fields)
        base["launch_adapter"] = adapter.adapter_id
        intent = _persist_submission_intent(
            state, base, route_attempt_id=route_attempt_id)
        if intent.get("status") != "success":
            adapter.abandon(prepared)
            return {**base, **intent}
        base["submission_intent_artifact_id"] = intent["artifact_id"]
        submitted = adapter.launch(prepared)
        if not submitted.get("ok"):
            returncode = submitted.get("returncode")
            if returncode is None and str(submitted.get("stderr") or "") != "not found":
                return {
                    **base,
                    "status": "submission_outcome_unknown",
                    "submit_result": submitted,
                    "do_not_resubmit": True,
                    "error": (
                        "Docker launch 未返回可判定结果；容器可能已启动，必须按"
                        " planned job_id 对账，禁止直接重提"
                    ),
                }
            terminal = _terminalize_submission_intent(
                state, base, route_attempt_id=route_attempt_id,
                intent_status=("aborted_before_submit"
                               if str(submitted.get("stderr") or "") == "not found"
                               else "rejected_by_scheduler"),
                submission_boundary_crossed=(
                    str(submitted.get("stderr") or "") != "not found"),
                terminal_reason="docker_runtime_rejected_launch",
                terminal_evidence={"returncode": returncode},
            )
            adapter.abandon(prepared)
            return {
                **base,
                "status": "error" if terminal.get("status") == "success"
                else "submission_outcome_unknown",
                "submit_result": submitted,
                "intent_status": terminal.get("intent_status"),
                "do_not_resubmit": True,
                "error": "sandbox container failed to start",
            }
        accepted_receipt = adapter.accepted_receipt(prepared, submitted)
        runtime_id = str(accepted_receipt.get("container_runtime_id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", runtime_id):
            return {
                **base,
                "status": "submission_outcome_unknown",
                "submit_result": submitted,
                "do_not_resubmit": True,
                "error": "sandbox runtime did not return an immutable container ID",
            }
        return {
            **base,
            "stdout_path": str(out),
            "stderr_path": str(err),
            **accepted_receipt,
            "sandbox_limits": {
                "memory_bytes": limits.memory_bytes,
                "cpus": limits.cpus,
                "pids": limits.pids,
                "walltime_seconds": limits.walltime_seconds,
                "storage_bytes": limits.storage_bytes,
                "storage_entries": limits.storage_entries,
                "output_bytes": limits.output_bytes,
                "network": "none",
            },
            "submit_result": submitted,
        }
    intent = _persist_submission_intent(
        state, base, route_attempt_id=route_attempt_id)
    if intent.get("status") != "success":
        return {**base, **intent}
    base["submission_intent_artifact_id"] = intent["artifact_id"]
    submitted = _run(cmd, timeout=20)
    if not submitted["ok"]:
        returncode = submitted.get("returncode")
        command_not_found = (
            returncode is None
            and str(submitted.get("stderr") or "") == "not found"
        )
        outcome_unknown = (
            not command_not_found
            and (returncode is None or int(returncode) < 0)
        )
        if outcome_unknown:
            return {
                **base,
                "status": "submission_outcome_unknown",
                "submit_command": cmd,
                "submit_result": submitted,
                "do_not_resubmit": True,
                "error": (
                    "scheduler CLI 未返回可判定结果；提交可能已被接受，必须按 nonce "
                    "对账，禁止直接重提"
                ),
            }
        terminal_status = (
            "aborted_before_submit" if command_not_found
            else "rejected_by_scheduler"
        )
        terminal = _terminalize_submission_intent(
            state, base, route_attempt_id=route_attempt_id,
            intent_status=terminal_status,
            submission_boundary_crossed=not command_not_found,
            terminal_reason=(
                "scheduler_cli_not_found" if command_not_found
                else "scheduler_rejected_submission"
            ),
            terminal_evidence={
                "returncode": returncode,
                "stderr_sha256": hashlib.sha256(
                    str(submitted.get("stderr") or "").encode("utf-8")
                ).hexdigest(),
            },
        )
        if terminal.get("status") != "success":
            return {
                **base,
                "status": "submission_outcome_unknown",
                "submit_command": cmd,
                "submit_result": submitted,
                "intent_status": "prepared_outcome_unknown",
                "do_not_resubmit": True,
                "error": "调度器已明确拒绝，但提交意图终态持久化失败；禁止直接重提",
            }
        return {
            **base,
            "status": "error",
            "submit_command": cmd,
            "submit_result": submitted,
            "intent_status": terminal_status,
            "submission_boundary_crossed": not command_not_found,
            "output_roots_released": True,
            "do_not_resubmit": True,
            "retry_requires_new_attempt": True,
        }
    identity = _parse_submission_identity(scheduler, str(submitted.get("stdout") or ""))
    if identity is None:
        return {
            **base,
            "status": "accepted_identity_unresolved",
            "submit_command": cmd,
            "submit_result": submitted,
            "job_id": None,
            "error": "scheduler accepted the submission but no machine-readable job identity was parsed",
            "do_not_resubmit": True,
        }
    job_id = str(identity["job_id"])
    resolved_namespace = base.get("namespace") or identity.get("namespace")
    return {
        **base,
        **identity,
        "namespace": resolved_namespace,
        "submit_command": cmd,
        "submit_result": submitted,
        "job_id": job_id,
        **_resolve_output_paths(output_patterns, job_name, job_id),
    }


def _submit_command(scheduler: str, script_path: Path, namespace: str | None) -> list[str] | None:
    if scheduler == "slurm":
        return ["sbatch", "--parsable", str(script_path)]
    if scheduler == "pbs":
        return ["qsub", str(script_path)]
    if scheduler == "kubernetes":
        cmd = ["kubectl"]
        if namespace:
            cmd += ["-n", namespace]
        return cmd + ["create", "-f", str(script_path), "-o", "json"]
    if scheduler == "local":
        return ["bash", str(script_path)]
    return None


def _safe_scheduler_directive(
    value: str | None,
    field: str,
    scheduler: str | None = None,
) -> str | None:
    """只接受调度器字段的窄语法值，禁止同一 directive 行追加 option。"""
    if value is None:
        return None
    raw = str(value)
    if not raw:
        return None
    if raw != raw.strip() or any(char.isspace() for char in raw):
        raise ValueError(f"{field} contains forbidden whitespace")
    if len(raw) > 200 or raw.startswith("-") or any(
        char in raw for char in ("#", "=", chr(92))
    ):
        raise ValueError(f"{field} is not a valid scheduler directive value")

    scheduler_name = str(scheduler or "").lower()
    if field == "queue" and scheduler_name == "slurm":
        pattern = r"[A-Za-z0-9][A-Za-z0-9._-]*(?:,[A-Za-z0-9][A-Za-z0-9._-]*)*"
    elif field == "queue" and scheduler_name == "pbs":
        pattern = (
            r"[A-Za-z0-9][A-Za-z0-9._-]*"
            r"(?:@[A-Za-z0-9][A-Za-z0-9._:-]*)?"
        )
    elif field == "nodelist" and scheduler_name == "slurm":
        pattern = r"[A-Za-z0-9][A-Za-z0-9._,\[\]-]*"
    else:
        pattern = r"[A-Za-z0-9][A-Za-z0-9._,:@\[\]-]*"
    if re.fullmatch(pattern, raw) is None:
        raise ValueError("{} is not a valid {} value".format(
            field, scheduler_name or "scheduler"))
    return raw


def _parse_submission_identity(scheduler: str, stdout: str) -> dict[str, str] | None:
    """Parse only scheduler machine formats; human prose is never an identity."""
    scheduler = str(scheduler or "").lower()
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    line = lines[0]
    if scheduler == "slurm":
        match = re.fullmatch(r"(?P<job_id>\d+)(?:;(?P<scheduler_cluster>[A-Za-z0-9_.-]+))?", line)
        if not match:
            return None
        return {key: value for key, value in match.groupdict().items() if value}
    if scheduler == "pbs":
        if not re.fullmatch(r"\d+(?:\[[^\]\s]+\])?(?:\.[A-Za-z0-9_.:-]+)?", line):
            return None
        return {"job_id": line}
    if scheduler == "kubernetes":
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            return None
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if not isinstance(metadata, dict) or str(document.get("kind") or "") != "Job":
            return None
        name, uid = metadata.get("name"), metadata.get("uid")
        if not isinstance(name, str) or not name or not isinstance(uid, str) or not uid:
            return None
        identity = {"job_id": name, "resource_uid": uid}
        if isinstance(metadata.get("namespace"), str) and metadata["namespace"]:
            identity["namespace"] = metadata["namespace"]
        return identity
    return None


def _parse_job_id(scheduler: str, stdout: str) -> str | None:
    """Compatibility helper for callers that need only the parsed scheduler ID."""
    identity = _parse_submission_identity(scheduler, stdout)
    return str(identity["job_id"]) if identity else None



def _job_submission_confirmation_text(submission: dict[str, Any]) -> str:
    """Canonical identity of every execution-affecting field in one approval."""
    return json.dumps(submission, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _managed_job_confirmation_category(
    *, action: str, scheduler: str, highrisk_category: str | None = None,
) -> str | None:
    """Return the existing HITL category, or ``None`` for safe local submit."""
    normalized_scheduler = str(scheduler or "").strip().lower()
    if action == "submit":
        if normalized_scheduler == "local":
            if highrisk_category is None:
                return None
            return f"本地受管作业提交（含高危命令：{highrisk_category}）"
        if highrisk_category is None:
            return "真实外部作业提交"
        return f"真实外部作业提交（含高危命令：{highrisk_category}）"
    if action == "cancel":
        if normalized_scheduler == "local":
            return "取消本地受管作业"
        return "取消真实外部作业"
    raise ValueError(f"unsupported managed job confirmation action: {action!r}")


def _container_launch_identity(command: str) -> dict[str, str] | None:
    """Extract a container runtime/image without executing model-supplied shell."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    runtimes = {"docker", "podman", "apptainer", "singularity"}
    for index, token in enumerate(tokens):
        if Path(token).name not in runtimes or index + 1 >= len(tokens):
            continue
        runtime = Path(token).name
        action = tokens[index + 1]
        if runtime in {"docker", "podman"} and action != "run":
            continue
        if runtime in {"apptainer", "singularity"} and action not in {"exec", "run"}:
            continue
        pos = index + 2
        flags_with_value = {"-v", "--volume", "-e", "--env", "--env-file", "-w", "--workdir", "--name", "--user", "--network", "--gpus", "--cpus", "--memory", "--bind", "-B", "--pwd"}
        while pos < len(tokens):
            value = tokens[pos]
            if value in flags_with_value:
                pos += 2
                continue
            if value.startswith("-"):
                pos += 1
                continue
            return {"runtime": runtime, "action": action, "image_reference": value}
    return None

def _container_execution_evidence(command: str) -> dict[str, Any] | None:
    identity = _container_launch_identity(command)
    if identity is None:
        return None
    runtime, image = identity["runtime"], identity["image_reference"]
    version = _run([runtime, "--version"], timeout=10)
    image_digest = None
    if runtime in {"docker", "podman"}:
        inspect = _run([runtime, "image", "inspect", image], timeout=15)
        digest_probe = _run([runtime, "image", "inspect", "--format", "{{index .RepoDigests 0}}", image], timeout=15)
        candidate = str(digest_probe.get("stdout") or "").strip()
        if "@sha256:" in candidate:
            image_digest = candidate
    else:
        inspect = _run([runtime, "inspect", "--json", image], timeout=15)
    return {**identity, "image_digest": image_digest, "image_digest_resolved": bool(image_digest), "runtime_version": version, "image_inspect": inspect,
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "captured_at": datetime.now(timezone.utc).isoformat()}

def _execution_probe(args: list[str], timeout: int = 12) -> dict[str, Any]:
    return _run(args, timeout=timeout)

_SLURM_ACCOUNTING_FIELDS = (
    "JobIDRaw", "State", "ExitCode", "Partition", "NodeList", "AllocCPUS",
    "ReqMem", "ElapsedRaw", "MaxRSS",
)
_SLURM_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED",
    "SPECIAL_EXIT",
})
#: 作业**自己**走到头的两种终态：程序跑完并给出自己的退出码。其余终态都是调度器
#: 把它结束的 —— 此时 sacct 的 ExitCode 低位恒为 0（程序没机会给退出码），把它当
#: "exit 0" 会把超时/取消/抢占/节点故障读成成功。
#:
#: 取补集而不是另列一张"被杀名单"：以后 _SLURM_TERMINAL_STATES 里再加状态时，
#: 默认归入"被调度器结束"（加严），而不是默认归入"自己跑完"（放松）。
_SLURM_SELF_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED"})
_SLURM_SCHEDULER_KILL_STATES = _SLURM_TERMINAL_STATES - _SLURM_SELF_TERMINAL_STATES


def _slurm_accounting_argv(job_id: str) -> list[str]:
    return [
        "sacct", "-n", "-P", "-j", str(job_id),
        "-o", ",".join(_SLURM_ACCOUNTING_FIELDS),
    ]


def _slurm_state_name(value: Any) -> str:
    text = str(value or "").strip().upper()
    return (text.split()[0] if text else "").rstrip("+")


def _slurm_exit_status(value: Any) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\s*(-?\d+)(?::(-?\d+))?\s*", str(value or ""))
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2) or 0)


def _slurm_memory_bytes(value: Any) -> int | None:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:I?B)?\s*",
        str(value or "").upper(),
    )
    if match is None:
        return None
    power = "KMGTPE".find(match.group(2)) + 1 if match.group(2) else 0
    return int(float(match.group(1)) * (1024 ** power))


def _slurm_accounting_facts(
    job_id: str, result: dict[str, Any],
) -> dict[str, Any] | None:
    """Reduce every returned allocation and step into one accounting view."""
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    rows: list[dict[str, str]] = []
    for raw_line in str(result.get("stdout") or "").splitlines():
        if not raw_line.strip():
            continue
        values = [item.strip() for item in raw_line.split("|")]
        values.extend([""] * (len(_SLURM_ACCOUNTING_FIELDS) - len(values)))
        rows.append(dict(zip(_SLURM_ACCOUNTING_FIELDS, values, strict=False)))
    allocation_source_rows = [
        row for row in rows
        if row["JobIDRaw"] and "." not in row["JobIDRaw"]
    ]
    if not allocation_source_rows:
        return None
    step_rows = [
        row for row in rows
        if row["JobIDRaw"] and "." in row["JobIDRaw"]
    ]
    allocation_rows = []
    for row in allocation_source_rows:
        returncode, signal = _slurm_exit_status(row["ExitCode"])
        allocation_rows.append({
            "job_id": row["JobIDRaw"],
            "state": _slurm_state_name(row["State"]),
            "returncode": returncode,
            "exit_code": returncode,
            "exit_signal": signal,
        })
    allocation_states = [row["state"] for row in allocation_rows]
    allocation_codes = [row["returncode"] for row in allocation_rows]
    first_nonzero = next(
        (code for code in allocation_codes if isinstance(code, int) and code != 0),
        None,
    )
    all_exit_zero = all(code == 0 for code in allocation_codes)
    aggregate_returncode = first_nonzero if first_nonzero is not None else (
        0 if all_exit_zero else None
    )
    terminal = all(
        state in _SLURM_TERMINAL_STATES for state in allocation_states
    )
    # terminal 只说"调度器不再持有它"。成功还要求每个 allocation 自己走到
    # COMPLETED —— 退出码低位为 0 并不代表程序跑完了。
    completed_cleanly = bool(allocation_states) and all(
        state == "COMPLETED" for state in allocation_states
    )
    # 只看 allocation 行：step 行（123.batch）的 OUT_OF_MEMORY 是诊断证据，
    # 按既有决定不否定 allocation 的成功（见 operation_completion 同处注释）。
    scheduler_terminated = any(
        state in _SLURM_SCHEDULER_KILL_STATES for state in allocation_states
    )
    oom_steps = [
        row["JobIDRaw"] for row in rows
        if _slurm_state_name(row["State"]) == "OUT_OF_MEMORY"
    ]
    memory_rows = [
        (_slurm_memory_bytes(row["MaxRSS"]), row)
        for row in step_rows
        if _slurm_memory_bytes(row["MaxRSS"]) is not None
    ]
    max_memory = max(
        memory_rows, key=lambda item: item[0] or 0,
    ) if memory_rows else None
    max_rss_source = None
    if max_memory is not None:
        max_rss_source = {
            "source": "slurm_sacct_step",
            "job_id": max_memory[1]["JobIDRaw"],
        }
    return {
        "allocation_state": (
            allocation_states[0] if len(allocation_states) == 1 else None
        ),
        "allocation_states": allocation_states,
        "allocation_rows": allocation_rows,
        "returncode": aggregate_returncode,
        "exit_code": aggregate_returncode,
        "exit_signal": (
            allocation_rows[0]["exit_signal"]
            if len(allocation_rows) == 1 else None
        ),
        "terminal": terminal,
        "succeeded": terminal and all_exit_zero and completed_cleanly,
        # 作业是被调度器结束的，不是自己跑完的。收货端据此否定"符合预期终止"：
        # 预期退出码只能由程序自己达成，不能由 NODE_FAIL 的 0:0 撞上 exit_codes=[0]。
        "scheduler_terminated": scheduler_terminated,
        "oom_killed": bool(oom_steps),
        "oom_steps": oom_steps,
        "max_rss": max_memory[1]["MaxRSS"] if max_memory is not None else None,
        "max_rss_bytes": max_memory[0] if max_memory is not None else None,
        "max_rss_source": max_rss_source,
        "states": sorted({_slurm_state_name(row["State"]) for row in rows}),
    }


def _scheduler_environment_snapshot(scheduler: str, job_id: str | None, namespace: str | None) -> dict[str, Any]:
    scheduler = str(scheduler or "").lower()
    if scheduler == "slurm":
        accounting = (
            _execution_probe(_slurm_accounting_argv(str(job_id)))
            if job_id else None
        )
        accounting_facts = (
            _slurm_accounting_facts(str(job_id), accounting)
            if job_id and accounting is not None else None
        ) or {}
        return {
            "runtime_version": _execution_probe(["scontrol", "--version"]),
            "job_snapshot": (
                _execution_probe(["scontrol", "show", "job", "-o", str(job_id)])
                if job_id else None
            ),
            "accounting_snapshot": accounting,
            "max_rss": accounting_facts.get("max_rss"),
            "max_rss_bytes": accounting_facts.get("max_rss_bytes"),
            "max_rss_source": accounting_facts.get("max_rss_source"),
        }
    if scheduler == "pbs":
        if job_id:
            snapshot = query_pbs_job(_execution_probe, str(job_id))
            runtime_version = dict(snapshot.get("pbs_flavor_probe") or {})
        else:
            detected = pbs_flavor(_execution_probe)
            snapshot = None
            runtime_version = dict(detected.get("probe") or {})
        return {"runtime_version": runtime_version, "job_snapshot": snapshot}
    if scheduler == "kubernetes":
        prefix = ["kubectl"] + (["-n", namespace] if namespace else [])
        return {"runtime_version": _execution_probe(["kubectl", "version", "--client", "--output=json"]), "job_snapshot": _execution_probe(prefix + ["get", "job", str(job_id), "-o", "json"]) if job_id else None}
    if scheduler == "local":
        from core import isolation
        from core.sandbox import inspect_container

        native = isolation.enforcement_snapshot()
        return {
            "runtime_version": {
                "status": "not_applicable",
                "reason": "native_job_backend_has_no_runtime_version_command",
            },
            "backend_identity": {
                "kind": "native_managed_job",
                "backend": native.get("backend"),
                "source": "core.isolation.enforcement_snapshot",
            },
            "job_snapshot": inspect_container(str(job_id)) if job_id else None,
        }
    return {"runtime_version": {"status": "not_applicable", "reason": "unknown scheduler"}}

def _persist_execution_environment_evidence(state: State, result: dict[str, Any], phase: str) -> None:
    command = str(result.get("command") or "")
    script_path = str(result.get("script_path") or "")
    script_hash = None
    try:
        script_hash = hashlib.sha256(Path(script_path).read_bytes()).hexdigest() if script_path else None
    except OSError:
        pass
    container = _container_execution_evidence(command)
    evidence = {"schema_version": 1, "phase": phase, "captured_at": datetime.now(timezone.utc).isoformat(),
                "run_id": state.run_id, "job_id": result.get("job_id"), "scheduler": result.get("scheduler"),
                "job_name": result.get("job_name"), "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "script_path": script_path or None, "script_sha256": script_hash,
                "host": {"hostname": os.uname().nodename, "kernel": os.uname().release,
                         "gpu": _execution_probe(["nvidia-smi", "--query-gpu=driver_version,name,uuid", "--format=csv,noheader"])},
                "scheduler_snapshot": _scheduler_environment_snapshot(str(result.get("scheduler") or ""), result.get("job_id"), result.get("namespace")),
                "container": container or {"status": "not_applicable", "reason": "payload is not a recognized container run/exec"}}
    state.save_artifact("execution_environment_evidence", f"execution_environment_{state.run_id}_{result.get('job_id') or time.time_ns()}_{phase}", json.dumps(evidence, ensure_ascii=False, indent=2), metadata={"phase": phase, "scheduler": str(result.get("scheduler") or ""), "job_id": str(result.get("job_id") or ""), "image_digest": (container or {}).get("image_digest")})
    state.append_transcript("execution_environment_evidence", phase=phase, job_id=result.get("job_id"), scheduler=result.get("scheduler"))


def _persist_external_job_workflow(state: State, result: dict[str, Any]) -> str | None:
    """Persist an immediate handoff without allowing its failure to hide a job id."""
    try:
        try:
            from nodes.experiment.hooks import persist_external_job_workflow
        except ImportError:
            from hooks import persist_external_job_workflow
        return persist_external_job_workflow(state, result)
    except Exception:
        log.warning("unable to persist immediate external job workflow", exc_info=True)
        return None


def _persist_external_job_identity_recovery_workflow(
    state: State, result: dict[str, Any],
) -> str | None:
    """Create a recovery-only task; it is never a normal finalizable workflow."""
    try:
        try:
            from nodes.experiment.hooks import persist_external_job_identity_recovery
        except ImportError:
            from hooks import persist_external_job_identity_recovery
        return persist_external_job_identity_recovery(state, result)
    except Exception:
        log.warning("unable to persist immediate external job identity recovery", exc_info=True)
        return None


def _persist_submission_recovery(
    state: State, result: dict[str, Any], error: Exception,
) -> dict[str, Any]:
    """Record an accepted job when its primary receipt cannot be written.

    The scheduler has already accepted the job.  Returning a generic error here
    would invite a duplicate submission and lose the only known scheduler id.
    A recovery receipt uses the same payload shape consumed by lifecycle readers.
    """
    error_text = f"{type(error).__name__}: {error}"
    recovery = dict(result)
    recovery["submission_persistence"] = {
        "status": "recovery_receipt", "primary_error": error_text,
    }
    artifact_id = None
    try:
        artifact = state.save_artifact(
            _EXTERNAL_JOB_SUBMISSION_RECOVERY_TYPE,
            f"external_job_submission_recovery_{state.run_id}_"
            f"{result.get('submission_nonce') or time.time_ns()}",
            json.dumps(recovery, ensure_ascii=False, indent=2),
            metadata={"scheduler": result.get("scheduler"),
                      "job_id": str(result.get("job_id") or ""),
                      "namespace": result.get("namespace"),
                      "launch_host": result.get("launch_host"),
                      "primary_error": error_text[:500]},
        )
        artifact_id = artifact.get("id")
    except Exception:
        log.warning("unable to persist external job recovery receipt", exc_info=True)
    try:
        state.hook_state["external_job_submission_recovery"] = recovery
        state.append_transcript(
            "external_job_submission_persistence_failed",
            scheduler=result.get("scheduler"), job_id=result.get("job_id"),
            namespace=result.get("namespace"), launch_host=result.get("launch_host"),
            recovery_artifact_id=artifact_id, error=error_text[:500],
        )
    except Exception:
        log.warning("unable to record external job submission recovery state", exc_info=True)
    return {"status": "recovered" if artifact_id else "unrecoverable",
            "artifact_id": artifact_id, "primary_error": error_text}


def _record_submission_persistence_blocker(
    state: State, result: dict[str, Any], recovery: dict[str, Any],
) -> None:
    try:
        from core.blockers import record_blocker
        record_blocker(
            state, category="external_job",
            summary=("external job was accepted but its primary submission receipt "
                     "could not be persisted"),
            requested_action=("do not resubmit; reconcile the returned scheduler/job identity "
                              "and repair submission persistence before finalizing"),
            suggested_owner="experiment", retryable_after_change=True,
            reported_by="framework:external_job_submission_persistence",
            evidence_paths=[str(recovery.get("artifact_id") or "")],
        )
    except Exception:
        log.warning("unable to record submission persistence blocker", exc_info=True)


def _record_submission_identity_blocker(
    state: State, result: dict[str, Any], recovery: dict[str, Any],
) -> None:
    """Make an unidentified or outcome-unknown submission non-terminal."""
    outcome_unknown = result.get("status") == "submission_outcome_unknown"
    try:
        from core.blockers import record_blocker
        record_blocker(
            state, category="external_job",
            summary=(
                "the submission boundary returned without a verifiable outcome; "
                "an external job may exist"
                if outcome_unknown else
                "scheduler accepted an external job but returned no verifiable job identity"
            ),
            requested_action=(
                "do not resubmit; use the persisted submission nonce and recovery "
                "task to reconcile whether a scheduler job exists before finalization"
                if outcome_unknown else
                "do not resubmit; use the persisted scheduler response, submission "
                "nonce, script hash, and recovery task to reconcile the scheduler "
                "identity before any finalization"
            ),
            suggested_owner="experiment", retryable_after_change=True,
            reported_by="framework:external_job_identity_unresolved",
            evidence_paths=[str(recovery.get("artifact_id") or "")],
        )
    except Exception:
        log.warning("unable to record submission identity blocker", exc_info=True)


def _submission_route_action(
    command: str,
    *,
    stage: str,
    dry_run: bool,
    route_step_id: str | None = None,
) -> dict[str, Any]:
    try:
        try:
            from .safe_bash import (
                _is_major_build, _is_major_run, _route_observed_program,
                _static_submit_program_sequence,
            )
        except ImportError:
            from tools.safe_bash import (
                _is_major_build, _is_major_run, _route_observed_program,
                _static_submit_program_sequence,
            )
        major_build = _is_major_build(command)
        major_run = _is_major_run(command)
        observed_program = _route_observed_program(command)
        program_sequence, sequence_reason = _static_submit_program_sequence(
            command)
        program = program_sequence[0] if program_sequence else observed_program
    except Exception:
        major_build = False
        major_run = False
        program = ""
        program_sequence = None
        sequence_reason = "projection_error"
    effects = {"workspace_write"}
    if not dry_run and (major_build or major_run):
        effects.add("process_tree")
    if not dry_run:
        # 真实提交本身就是受管生命周期：external_job 已经满足所有权契约，
        # managed_lifecycle 一起带上，保证"提交走 submit_job"这条在两侧同形。
        effects.update({"external_job", "process_tree", "managed_lifecycle"})
    action = {
        "tool": "submit_job",
        "program": program,
        "route_step_id": str(route_step_id or "").strip(),
        "read_only": False,
        "observed_effects": sorted(effects),
        "workdir_roles": [],
        "dry_run": bool(dry_run),
        "legacy_policy": {
            "stage": stage,
            "guarded_build": stage == "toolchain_build",
            "formal_simulation": stage == "simulation" and not dry_run,
        },
        "mechanical_major_build": major_build,
        "mechanical_major_run": major_run,
    }
    if program_sequence is not None:
        action["program_sequence"] = program_sequence
    elif str(program).startswith("compound:"):
        # A joined diagnostic token is lossy (paths can contain "+") and must
        # never fall back to a first-entry route match.
        action["program_sequence_unavailable_reason"] = sequence_reason
    return action


def _resolve_submission_route(
    state: State,
    action: dict[str, Any],
) -> dict[str, Any]:
    try:
        try:
            from .execution_route import resolve_execution_context
        except ImportError:
            from tools.execution_route import resolve_execution_context
        return resolve_execution_context(state, action)
    except Exception as exc:
        return {
            "decision": "resolver_error",
            "reason": type(exc).__name__,
            "tool": str(action.get("tool") or ""),
        }


def _submission_route_workdir(
    state: State,
    decision: dict[str, Any],
    *,
    create: bool = True,
) -> dict[str, Any]:
    try:
        try:
            from .execution_route import route_default_workdir
        except ImportError:
            from tools.execution_route import route_default_workdir
        return route_default_workdir(state, decision, create=create)
    except Exception as exc:
        return {"status": "resolver_error", "reason": type(exc).__name__}


def _record_submission_route(
    state: State,
    action: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    try:
        try:
            from .execution_route import record_execution_route_shadow
        except ImportError:
            from tools.execution_route import record_execution_route_shadow
        record_execution_route_shadow(state, action, decision)
    except Exception:
        pass


def _enforce_submission_route(
    state: State,
    action: dict[str, Any],
    decision: dict[str, Any],
    *,
    phase: str = "pre_spawn",
) -> dict[str, Any] | None:
    try:
        try:
            from .execution_route import enforce_execution_route
        except ImportError:
            from tools.execution_route import enforce_execution_route
        return enforce_execution_route(
            state, action, decision, phase=phase)
    except Exception as exc:
        effects = {
            str(item) for item in (
                decision.get("effective_effects")
                or action.get("observed_effects")
                or []
            )
        }
        exact_read_only = (
            action.get("read_only") is True and not effects
        )
        if action.get("dry_run") is True or exact_read_only:
            return None
        return {
            "status": "error",
            "reason": "execution_route_resolver_failed",
            "error": (
                "执行上下文门异常，真实作业已在目录物化和提交前拒绝；"
                "只读和 dry-run 诊断仍可继续。"
            ),
            "route_blocked": True,
            "blocker": {
                "kind": "execution_route_resolver_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "report_framework_blocker_keep_read_only_diagnostics",
            },
        }


def _begin_submission_route_attempt(
    state: State,
    decision: dict[str, Any],
    action: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        try:
            from .execution_route import begin_route_step_attempt, route_binding_block
        except ImportError:
            from tools.execution_route import begin_route_step_attempt, route_binding_block
        binding = begin_route_step_attempt(
            state, decision, tool="submit_job", action=action)
        return binding, route_binding_block(binding)
    except Exception as exc:
        if decision.get("decision") != "matched_ready_step":
            return None, None
        return None, {
            "status": "error",
            "reason": "route_binding_persistence_failed",
            "error": "路线步骤开始收据无法持久化，作业未提交。",
            "route_blocked": True,
            "blocker": {
                "kind": "route_binding_persistence_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "repair_transcript_persistence_before_retry",
            },
        }


def _finish_submission_route_attempt(
    state: State,
    binding: dict[str, Any] | None,
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    try:
        try:
            from .execution_route import (
                finish_route_step_attempt, managed_execution_result_receipt,
                route_attempt_receipt, route_outcome_block,
            )
        except ImportError:
            from tools.execution_route import (
                finish_route_step_attempt, managed_execution_result_receipt,
                route_attempt_receipt, route_outcome_block,
            )
        event = finish_route_step_attempt(
            state,
            binding,
            result=result,
            error=error,
            external_submission=True,
        )
        attempt_receipt = route_attempt_receipt(event)
        if isinstance(result, dict) and attempt_receipt is not None:
            result["route_attempt"] = attempt_receipt
        block = route_outcome_block(event)
        if isinstance(block, dict):
            if attempt_receipt is not None:
                block["route_attempt"] = attempt_receipt
            execution_receipt = managed_execution_result_receipt(result)
            if execution_receipt is not None:
                block["execution_receipt"] = execution_receipt
        return block
    except Exception as exc:
        if not binding:
            return None
        return {
            "status": "error",
            "reason": "route_outcome_persistence_failed",
            "error": "调度动作已返回，但路线结果收据处理失败；禁止重提。",
            "blocker": {
                "kind": "route_outcome_persistence_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "repair_and_reconcile_attempt_before_retry",
            },
        }


def _begin_submission_action_census(
    state: State,
    action: dict[str, Any],
    decision: dict[str, Any],
    route_binding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist the action admission immediately before `_submit_sync`."""
    try:
        from .execution_action_census import begin_execution_action
    except ImportError:
        from tools.execution_action_census import begin_execution_action
    return begin_execution_action(
        state, action, decision, route_binding=route_binding,
    )


def _submission_action_spawn_facts(
    result: dict[str, Any],
    *,
    dry_run: bool,
    scheduler: str,
) -> tuple[bool | None, bool | None, str]:
    """Project only `_submit_sync`'s explicit submission outcome semantics."""
    if dry_run:
        return False, False, "submit_sync.dry_run"
    status = str(result.get("status") or "")
    runtime_id = str(result.get("container_runtime_id") or "").strip()
    if scheduler == "local" and re.fullmatch(r"[0-9a-f]{64}", runtime_id):
        return True, True, "submit_sync.local_accepted_receipt"
    if (
        scheduler != "local"
        and status == "success"
        and str(result.get("job_id") or "").strip()
    ):
        return None, True, "submit_sync.scheduler_accepted_identity"
    if status == "accepted_identity_unresolved":
        return None, True, "submit_sync.accepted_identity_unresolved"
    if status == "submission_outcome_unknown":
        return None, None, "submit_sync.outcome_unknown"
    if status == "error":
        return False, False, "submit_sync.known_rejection"
    return None, None, "submit_sync.unclassified_outcome"


def _finish_submission_action_census(
    state: State,
    token: dict[str, Any],
    *,
    dry_run: bool,
    scheduler: str,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Record spawn and terminal phases without hiding physical outcomes."""
    try:
        from .execution_action_census import settle_execution_action
    except ImportError:
        from tools.execution_action_census import settle_execution_action
    if result is None:
        payload_spawned, job_submitted, proof_source = (
            (False, False, "submit_sync.dry_run_exception")
            if dry_run else (None, None, "submit_sync.exception_unknown")
        )
    else:
        payload_spawned, job_submitted, proof_source = (
            _submission_action_spawn_facts(
                result, dry_run=dry_run, scheduler=scheduler,
            )
        )
    return settle_execution_action(
        state,
        token,
        payload_spawned=payload_spawned,
        job_submitted=job_submitted,
        proof_source=proof_source,
        result=result,
        error=error,
    )


def _submission_route_receipt_failure(
    result: dict[str, Any],
    route_block: dict[str, Any],
) -> dict[str, Any]:
    """调度器已接受时绝不把 route 收据故障包装成可安全重试的普通 error。"""
    return {
        **result,
        "status": "submitted_needs_recovery",
        "error": "作业已提交，但路线收据未可靠持久化；保留 job_id 并先对账，禁止重提。",
        "route_receipt": route_block,
        "blocker": {
            "kind": "external_job_route_receipt_failed",
            "scheduler": result.get("scheduler"),
            "job_id": result.get("job_id"),
            "namespace": result.get("namespace"),
            "suggested_owner": "framework",
            "node_action": "repair_and_reconcile_attempt_before_retry",
        },
    }


def _submission_action_census_failure(
    result: dict[str, Any],
    action_census: dict[str, Any],
    action_token: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Expose an incomplete audit without erasing the physical outcome.

    The scheduler result and route receipt are already durable when this is
    called.  A failed census follow-up therefore needs reconciliation, not a
    second submission.  Keep the physical outcome as evidence while making
    the tool-level status unambiguously non-successful.
    """
    problem = next(
        (
            phase
            for phase in (
                action_census.get("spawn"),
                action_census.get("terminal"),
            )
            if isinstance(phase, dict) and phase.get("status") != "success"
        ),
        action_census,
    )
    token = problem.get("action_token") if isinstance(problem, dict) else None
    if not isinstance(token, dict):
        token = action_token
    missing_phase = (
        problem.get("missing_phase") if isinstance(problem, dict) else None
    )
    next_action = problem.get("next_action") if isinstance(problem, dict) else None
    if not isinstance(next_action, dict):
        # 对抗审查（09-21）：原来的兜底写了一个不存在的模型工具（owner=experiment）。
        # 与 census 自己的持久化失败同一形状：runtime-owned，不是模型工具。
        next_action = {
            "owner": "experiment_runtime",
            "action": "retry_missing_census_phase_only",
            "phase": missing_phase or "terminal",
            "model_callable": False,
        }
    model_next_action = (
        problem.get("model_next_action") if isinstance(problem, dict) else None
    )
    if not isinstance(model_next_action, dict):
        model_next_action = {
            "action": "report_blocker_and_end_current_run",
            "reason": "runtime reconciliation is not a model tool",
        }
    deferred_terminal = action_census.get("terminal")
    if not (
        isinstance(deferred_terminal, dict)
        and deferred_terminal.get("status") == "deferred"
    ):
        deferred_terminal = None
    accepted = bool(
        not dry_run
        and (
            result.get("status") in {
                "success",
                "accepted_identity_unresolved",
                "submission_outcome_unknown",
            }
            or result.get("job_id")
            or result.get("container_runtime_id")
        )
    )
    physical_outcome = dict(result)
    physical_outcome.pop("execution_action_census", None)
    response = {
        **result,
        "status": "submitted_needs_recovery" if accepted else "error",
        "error": (
            "执行动作已经返回，但 action census 收据不完整；"
            "保留物理结果并只补缺失账本阶段，禁止重跑动作。"
        ),
        "execution_outcome": physical_outcome,
        "execution_action_census": action_census,
        "action_token": token,
        "missing_phase": missing_phase,
        "payload_must_not_rerun": True,
        "do_not_retry_payload": True,
        "safe_to_retry": False,
        **({"deferred_terminal": deferred_terminal} if deferred_terminal else {}),
        **({"model_next_action": model_next_action}
           if isinstance(model_next_action, dict) else {}),
        "blocker": {
            "kind": "execution_action_census_persistence_failed",
            "action_token": token,
            "missing_phase": missing_phase,
            "next_action": next_action,
            **({"deferred_terminal": deferred_terminal} if deferred_terminal else {}),
            "scheduler": result.get("scheduler"),
            "job_id": result.get("job_id"),
        },
    }
    if accepted:
        response["do_not_resubmit"] = True
    return response


def _submission_static_validity_block(
    state: State, command: str,
) -> dict[str, Any] | None:
    """在 scope、目录物化和脚本生成前验证 scheduler payload 语义。"""
    from shared.lib import dangerous_commands as danger

    boundary = danger.match_boundary_violation(command, mode="shell")
    if boundary:
        try:
            state.append_transcript(
                "boundary_write_blocked", tool="submit_job",
                cmd_preview=command[:200], category=boundary)
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "framework_boundary_violation",
            "error": danger.BOUNDARY_DENY_MESSAGE.format(category=boundary),
            "blocker": {
                "kind": "framework_boundary_violation",
                "category": boundary,
            },
        }
    try:
        try:
            from . import timeout_escalation as _te
        except ImportError:
            from tools import timeout_escalation as _te
        bash_decision = _te.classify_bash_execution(command)
        analyzer_reason = (
            _te.bash_analyzer_unavailable_reason()
            or bash_decision.analysis.analyzer_unavailable
        )
        if analyzer_reason is not None:
            try:
                state.append_transcript(
                    "bash_semantic_analyzer_unavailable", tool="submit_job",
                    reason=analyzer_reason, cmd_preview=command[:200])
            except Exception:
                pass
            return {
                "status": "error",
                "error": (
                    "Experiment Bash 语义分析器不可用，作业未提交："
                    f"{analyzer_reason}。这是框架运行时依赖/部署配置缺失；"
                    "experiment 不得安装或修改全局 Python 环境。请如实 report_blocker，"
                    "由 framework owner 在部署依赖中提供 tree-sitter 与 "
                    "tree-sitter-bash 后重试。"
                ),
                "blocker": {
                    "kind": "bash_semantic_analyzer_unavailable",
                    "reason": analyzer_reason,
                    "suggested_owner": "framework",
                    "node_action": "report_blocker_do_not_modify_global_environment",
                },
            }
    except Exception as exc:
        return {
            "status": "error",
            "reason": "bash_semantic_analysis_failed",
            "error": "Bash 语义分析器异常，作业未提交。",
            "blocker": {
                "kind": "bash_semantic_analysis_failed",
                "reason": type(exc).__name__,
            },
        }
    # 判决拆除·第三波（rm:1908 降格，2026-09-02）：nohup/&/disown/裸 sbatch 只是
    # 预测「会孤儿化」——作业脚本返回时容器/cgroup 会收掉子进程，不可逆损害不成立。
    # 照提交，submission 记录挂 unmanaged_background_launch 见证。
    # 但外部控制面（ssh/pdsh/kubectl exec/docker/systemd-run/…）不在此列：
    # 它把 payload 搬到受管边界之外，容器与账本都管不到，属确定的边界逃逸。
    from .bash_semantics import EXTERNAL_CONTROL_EFFECT as _EXT_EFFECT
    if bash_decision.analysis.external_control == _EXT_EFFECT:
        return {
            "status": "error",
            "error": (
                "job command 不得通过 ssh、pdsh、kubectl exec、docker、systemd-run "
                "等外部控制面把执行搬到受管边界之外；submit_job 本身负责受管提交与 "
                "external-job handoff，远端资源请用对应 scheduler 提交。"
            ),
            "blocker": {"kind": "unmanaged_background_launch"},
        }
    if bash_decision.unverifiable_execution:
        uncertainty = bash_decision.uncertainty_kind or "unknown"
        blocker_kind = (
            "bash_parse_error" if uncertainty == "parse_error"
            else "unverifiable_job_payload"
        )
        try:
            state.append_transcript(
                "bash_execution_unverifiable",
                tool="submit_job",
                uncertainty_kind=uncertainty,
                cmd_preview=command[:200])
        except Exception:
            pass
        return {
            "status": "error",
            "reason": blocker_kind,
            "error": (
                "job command 无法被静态验证，作业未提交。"
                "这不表示检测到了后台任务；请使用静态命令头和可审查的内联 payload。"
            ),
            "blocker": {
                "kind": blocker_kind,
                "uncertainty_kind": uncertainty,
            },
        }
    return None


#: core/context_engine._build_user_prompt 把节点输入渲染成「## 节点输入」下的
#: 「- **<key>**：<value>」。value 可跨多行，C3a 只把下一个输入键当作
#: 字段边界；任务正文中的 Markdown 标题仍属于 value。此格式由 Core owner
#: 维护，C3a 在跨-owner 登记中明确记录此临时依赖。
_RENDERED_NODE_INPUT = re.compile(
    r"^- \*\*(?P<key>[^*\n]+)\*\*：",
    re.MULTILINE,
)
_NODE_INPUT_SECTION = re.compile(r"^## 节点输入\s*$", re.MULTILINE)
#: 退出码语境：声明的码必须紧跟在这些字样之后（中间只允许空白与「：:=为是」）。
_EXIT_CODE_CONTEXT = re.compile(
    r"(?P<keyword>退出码|返回码|退出状态|状态码|exit(?:ed)?\s+with\s+(?:code|status)|"
    r"exit[\s_-]*code|exit[\s_-]*status|exitcode|return[\s_-]*code|returncode|"
    r"sys\.exit\(|os\._exit\(|exit\(|exit|return)"
    r"\s*[:：=为是]?\s*$",
    re.IGNORECASE,
)

#: These are Core-owned sections appended after the rendered node-input block.
#: A task body may legitimately contain its own ``##`` headings, so a generic
#: Markdown heading is *not* a field boundary. This temporary adapter is
#: intentionally strict and is replaced when Core supplies a typed first-turn
#: input receipt (see the cross-owner ledger).
_CORE_NODE_INPUT_TAIL_HEADERS = {
    "upstream_artifacts": "## 可用的上游 artifact（用 `read_artifact` 查看）",
    "kb_context": "## 项目 KB 状态（💡 历史记录参考，**不是 ground truth**）",
    "required_outputs_reminder": "## 提醒：必须产出的 artifact 类型 =",
}
_SUPPORTED_STARTUP_USER_CHANNELS = frozenset({
    "owner_review_spec",
    "node_inputs",
    *_CORE_NODE_INPUT_TAIL_HEADERS,
})
#: 裸 exit / return 后的数字，只有紧跟这些字符（或行尾）才算退出码：「exit 3'」「return 2;」算，
#: 「return 2 files」「exit 1 of 3 doors」这类英文句子不算（第三会话复审 0914c P3）。
_BARE_EXIT_CODE_FOLLOWERS = frozenset(";)'\"`，。；）、」\n")


def _code_in_exit_context(quote: str, code: int) -> bool:
    """引文里至少有一处这个码紧跟在退出码字样之后。编号「2.」、资源量「1 GiB」不算。"""
    for match in re.finditer(rf"(?<!\d){code}(?!\d)", quote):
        context = _EXIT_CODE_CONTEXT.search(quote[max(0, match.start() - 32):match.start()])
        if context is None:
            continue
        if context.group("keyword").lower() in {"exit", "return"}:
            following = quote[match.end():match.end() + 1]
            if following and following not in _BARE_EXIT_CODE_FOLLOWERS:
                continue
        return True
    return False


def _startup_node_input_manifest(
    events: list[dict[str, Any]], loop_seed_index: int,
) -> tuple[tuple[str, ...], frozenset[str]] | None:
    """Return the first-turn key schema and Core-declared user channels."""
    for event in reversed(events[:loop_seed_index]):
        if not isinstance(event, dict) or event.get("event") != (
            "startup_injection_manifest"
        ):
            continue
        raw_keys = event.get("node_input_keys")
        raw_channels = event.get("user")
        if (
            not isinstance(raw_keys, list)
            or any(not isinstance(key, str) or not key for key in raw_keys)
            or len(set(raw_keys)) != len(raw_keys)
            or not isinstance(raw_channels, list)
            or any(
                not isinstance(channel, str) or not channel
                for channel in raw_channels
            )
            or len(set(raw_channels)) != len(raw_channels)
            or any(
                channel not in _SUPPORTED_STARTUP_USER_CHANNELS
                for channel in raw_channels
            )
            or (bool(raw_keys) != ("node_inputs" in raw_channels))
        ):
            return None
        return tuple(raw_keys), frozenset(raw_channels)
    return None


def _rendered_node_input_block(
    content: str, user_channels: frozenset[str],
) -> str | None:
    """Return exactly one authenticated Core-rendered node-input block.

    A generic Markdown heading is not a field boundary: task prose may contain
    one. Instead, the first Core tail header declared by the startup manifest
    is the only boundary. After that boundary, artifact and KB content are
    data and are never reparsed as headers. A missing declared boundary fails
    closed.
    """
    sections = list(_NODE_INPUT_SECTION.finditer(content))
    if len(sections) != 1:
        return None
    block = content[sections[0].end():]
    first_tail_channel = next(
        (
            channel for channel in _CORE_NODE_INPUT_TAIL_HEADERS
            if channel in user_channels
        ),
        None,
    )
    if first_tail_channel is None:
        return block
    first_tail_header = _CORE_NODE_INPUT_TAIL_HEADERS[first_tail_channel]
    offset = 0
    for line in block.splitlines(keepends=True):
        if line.startswith(first_tail_header):
            return block[:offset]
        offset += len(line)
    return None


def _rendered_task_prose_source_from_seed(
    loop_seed: dict[str, Any],
    expected_input_keys: tuple[str, ...],
    user_channels: frozenset[str],
) -> tuple[TaskProseSource | None, list[str], str]:
    """Authenticate task prose against complete first rendered fields.

    This is a fail-closed compatibility parser for the Core renderer. It
    accepts one user message and one ``## 节点输入`` section whose complete
    field-key multiset exactly matches the startup manifest. Values end only
    at the next rendered input field, so a normal task heading remains data.
    """
    user_contents: list[str] = []
    for message in loop_seed.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        user_contents.append(
            content if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        )
    if len(user_contents) != 1:
        return None, [], "ambiguous_rendered_inputs"
    input_block = _rendered_node_input_block(user_contents[0], user_channels)
    if input_block is None:
        return None, [], "ambiguous_rendered_inputs"
    headers = list(_RENDERED_NODE_INPUT.finditer(input_block))
    actual_input_keys = [
        str(match.group("key") or "").strip() for match in headers
    ]
    if (
        any(not key for key in actual_input_keys)
        or Counter(actual_input_keys) != Counter(expected_input_keys)
    ):
        return None, actual_input_keys, "ambiguous_rendered_inputs"
    rendered_values: dict[str, str] = {}
    for index, match in enumerate(headers):
        key = actual_input_keys[index]
        value_end = (
            headers[index + 1].start()
            if index + 1 < len(headers) else len(input_block)
        )
        rendered_values[key] = input_block[match.end():value_end]
    return (
        first_task_prose_source(rendered_values),
        actual_input_keys,
        "authenticated_rendered_inputs",
    )


def _task_prose_source_from_receipt(
    events: list[dict[str, Any]], loop_seed_index: int,
) -> tuple[TaskProseSource | None, list[str], str]:
    """Read a structured node-owned receipt, or report why it is unusable."""
    manifest = _startup_node_input_manifest(events, loop_seed_index)
    if manifest is None:
        return None, [], "missing_or_invalid_startup_manifest"
    expected_input_keys, user_channels = manifest
    receipts = [
        event for event in events[loop_seed_index + 1:]
        if isinstance(event, dict)
        and event.get("event") == TASK_PROSE_INPUT_RECEIPT_EVENT
    ]
    if not receipts:
        return None, list(expected_input_keys), "receipt_absent"
    if len(receipts) != 1:
        return None, list(expected_input_keys), "conflicting_task_prose_receipts"
    receipt = receipts[0]
    raw_keys = receipt.get("node_input_keys")
    if (
        receipt.get("schema_version") != TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION
        or receipt.get("origin") != "turn_one_node_inputs"
        or not isinstance(raw_keys, list)
        or any(not isinstance(key, str) or not key for key in raw_keys)
        or len(set(raw_keys)) != len(raw_keys)
        or Counter(raw_keys) != Counter(expected_input_keys)
    ):
        return None, list(expected_input_keys), "invalid_task_prose_receipt"
    rendered_source, _rendered_keys, rendered_status = (
        _rendered_task_prose_source_from_seed(
            events[loop_seed_index], expected_input_keys, user_channels,
        )
    )
    if rendered_status != "authenticated_rendered_inputs":
        return None, list(raw_keys), "invalid_task_prose_receipt"
    source_key = receipt.get("source_key")
    source_text = receipt.get("source_text")
    source_sha256 = receipt.get("source_sha256")
    if source_key is None:
        if (
            source_text is not None
            or source_sha256 is not None
            or rendered_source is not None
        ):
            return None, list(raw_keys), "invalid_task_prose_receipt"
        return None, list(raw_keys), "authenticated_receipt"
    if (
        not isinstance(source_key, str)
        or source_key not in TASK_PROSE_INPUT_KEYS
        or source_key not in raw_keys
        or not isinstance(source_text, str)
        or not source_text.strip()
        or not isinstance(source_sha256, str)
        or hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        != source_sha256
    ):
        return None, list(raw_keys), "invalid_task_prose_receipt"
    receipt_source = TaskProseSource(key=source_key, text=source_text)
    if receipt_source != rendered_source:
        return None, list(raw_keys), "invalid_task_prose_receipt"
    return receipt_source, list(raw_keys), "authenticated_receipt"


def _legacy_task_prose_source_from_rendering(
    event: dict[str, Any],
    expected_input_keys: tuple[str, ...],
    user_channels: frozenset[str],
) -> tuple[TaskProseSource | None, list[str], str]:
    """Fail-closed parser only for runs created before the receipt existed."""
    source, keys, status = _rendered_task_prose_source_from_seed(
        event, expected_input_keys, user_channels,
    )
    if status != "authenticated_rendered_inputs":
        return source, keys, status
    return source, keys, "authenticated_legacy_rendering"


def _task_prose_source_for_anchor(
    state: Any,
) -> tuple[TaskProseSource | None, list[str], str]:
    """Find one immutable declared task-body source for a new declaration.

    New runs use a structured receipt frozen by the existing turn-one hook.
    Legacy runs without that receipt may use only a startup-manifest guarded
    Markdown parser. No branch guesses a source from arbitrary rendered text.
    """
    try:
        try:
            from .execution_route import _read_transcript_events
        except ImportError:
            from tools.execution_route import _read_transcript_events
        events, warnings = _read_transcript_events(state)
    except Exception:
        return None, [], "transcript_unavailable"
    if warnings:
        return None, [], "transcript_integrity_unavailable"
    for event_index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("event") != "loop_seed":
            continue
        source, keys, status = _task_prose_source_from_receipt(
            events, event_index,
        )
        if status != "receipt_absent":
            return source, keys, status
        manifest = _startup_node_input_manifest(events, event_index)
        if manifest is None:
            return None, [], "missing_or_invalid_startup_manifest"
        expected_input_keys, user_channels = manifest
        return _legacy_task_prose_source_from_rendering(
            event, expected_input_keys, user_channels,
        )
    return None, [], "missing_loop_seed"


def _task_text_for_anchor(state: Any) -> str:
    """Compatibility reader for the one declared task-body source."""
    source, _actual_input_keys, _receipt_status = _task_prose_source_for_anchor(state)
    return source.text if source is not None else ""


def _anchored_expected_termination(
    state: Any, expected_termination: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """expected_termination 的锚点核对（第 5 步 5b）：返回（冻结用的规范化声明, 拒绝）。

    预期必须在结果出现之前冻结，而且依据来自任务、不来自模型自己的观测（AGENTS L54）：
    确认卡在 benchmark 里自动批准、预授权时没人看，「不许追认」也挡不住先跑一次看到
    exit 3 再声明。所以 task_quote 必须逐字出自本 run 的任务原文，且每个码都在引文里以
    独立数字出现；码只允许 1..123（124–127、≥128、负数是超时、不可执行、信号终止等由平台
    或信号造成的终止）。

    planned_stop（计划内停止，D07）：任务要求这个作业中途用 cancel_job 停下（常驻服务用完
    关掉、按判据停、检查点重启测试）。同样提交时就冻结、引文逐字出自任务原文；取消确认后
    路线步骤按计划停止记账（见 _close_confirmed_cancellation），不影响退出码判定。
    """
    if expected_termination is None:
        return None, None
    declared = expected_termination if isinstance(expected_termination, dict) else {}
    planned_stop = declared.get("planned_stop") is True
    raw_codes = declared.get("exit_codes")
    raw_codes = raw_codes if isinstance(raw_codes, list) else []
    codes = sorted({code for code in raw_codes
                    if isinstance(code, int) and not isinstance(code, bool)})
    quote = str(declared.get("task_quote") or "").strip()
    problems: list[str] = []
    if (raw_codes or not planned_stop) and (
            not codes or len(codes) != len(raw_codes)
            or any(not 1 <= code <= 123 for code in codes)):
        problems.append(
            "exit_codes 必须是 1..123 的不重复整数（0 是默认的成功；124–127、≥128、负数是"
            "超时、不可执行、信号终止等由平台或信号造成的终止，不能声明为预期）；只声明计划内停止时"
            "省略 exit_codes、写 planned_stop: true")
    source, actual_input_keys, input_receipt_status = _task_prose_source_for_anchor(state)
    anchor_input_key = (
        source.key if source is not None and quote and quote in source.text else None
    )
    selected_task_prose_input_key = source.key if source is not None else None
    accepted_task_prose_input_keys = list(TASK_PROSE_INPUT_KEYS)
    actual_node_input_keys = list(dict.fromkeys(actual_input_keys))
    missing_task_prose_input_keys = [
        key for key in accepted_task_prose_input_keys
        if key not in actual_node_input_keys
    ]
    if not input_receipt_status.startswith("authenticated"):
        problems.append(
            "首轮节点输入收据无法认证，不能从 Markdown 推断任务正文"
        )
    if not quote or anchor_input_key is None:
        problems.append(
            "task_quote 必须逐字出自首条 loop_seed 中一个声明的任务正文输入段，"
            "不能取自框架生成的 KB、artifact 或提醒段落"
        )
    missing = [code for code in codes if not _code_in_exit_context(quote, code)]
    if missing:
        problems.append(
            f"task_quote 里的退出码 {missing} 没有紧跟在「退出码/返回码/exit/sys.exit(/return」"
            "一类字样之后（编号、资源量等数字不算退出码）")
    if problems:
        accepted_label = ", ".join(accepted_task_prose_input_keys) or "（无）"
        actual_label = ", ".join(actual_node_input_keys) or "（未找到可解析输入）"
        missing_label = ", ".join(missing_task_prose_input_keys) or "（无）"
        next_actions = [
            "先修正调用输入：任务正文只能放在声明的任务正文键中；"
            f"可接受键=[{accepted_label}]，本次可见键=[{actual_label}]，"
            f"缺失键=[{missing_label}]。修正上游映射后可安全重试同一提交。",
            "只有任务本身没有声明预期退出或计划停止时，才去掉 expected_termination；"
            "否则保留声明并让 task_quote 逐字引用对应任务正文段。",
        ]
        if not input_receipt_status.startswith("authenticated"):
            next_actions = [
                "本 run 的启动节点输入收据不可认证；重新派发一个新 Experiment run 后，"
                "在声明的任务正文键中写明预期终止，再提交 expected_termination。",
            ]
        return None, {
            "status": "error",
            "error_code": "expected_termination_not_anchored",
            "error": ("expected_termination 未通过锚点核对：" + "；".join(problems)
                      + "。本次未生成脚本、未弹确认卡、未提交。"),
            "problems": problems,
            "accepted_task_prose_input_keys": accepted_task_prose_input_keys,
            "selected_task_prose_input_key": selected_task_prose_input_key,
            "actual_node_input_keys": actual_node_input_keys,
            "missing_task_prose_input_keys": missing_task_prose_input_keys,
            "input_receipt_status": input_receipt_status,
            "side_effects": "none",
            "retryable": True,
            "next_actions": next_actions,
        }
    normalized: dict[str, Any] = {"task_quote": quote, "anchor": anchor_input_key}
    if codes:
        normalized["exit_codes"] = codes
    if planned_stop:
        normalized["planned_stop"] = True
    return normalized, None


def _planned_stop_declared(record: Any) -> bool:
    declared = record.get("expected_termination") if isinstance(record, dict) else None
    return isinstance(declared, dict) and declared.get("planned_stop") is True


def planned_stop_cancellation(
    state: Any, record: dict[str, Any], *, target_lifecycle: str | None = None,
) -> dict[str, Any] | None:
    """按计划停止的证据（D07）：提交时声明 planned_stop 的本地作业，lifecycle 已是 cancelled
    （不是 superseded）且取消结果 confirmed。任一条不满足返回 None——没声明的取消不追认。

    路线投影与 ROC 共用这一个判据（第三会话复审 D07 P3）。取消确认时 lifecycle 还没写，
    调用方传 target_lifecycle 代替读 lifecycle；取消结果 confirmed 仍从账本实读。
    """
    if not _planned_stop_declared(record) or str(record.get("scheduler") or "").lower() != "local":
        return None
    lifecycle = lifecycle_for_submission(state, record) if target_lifecycle is None else {}
    status = lifecycle.get("status") if target_lifecycle is None else target_lifecycle
    if status != "cancelled":
        return None
    try:
        transaction = _latest_cancellation_transaction(state, record) or {}
    except _CancellationLedgerError:
        return None
    outcome = transaction.get("outcome") or {}
    if outcome.get("outcome") != "confirmed":
        return None
    return {
        "planned_stop": True,
        "task_quote": (record.get("expected_termination") or {}).get("task_quote"),
        "cancellation_intent_artifact_id": (transaction.get("intent") or {}).get(
            "intent_artifact_id"),
        "cancellation_outcome_artifact_id": outcome.get("outcome_artifact_id"),
        "lifecycle_artifact_id": lifecycle.get("lifecycle_artifact_id"),
    }


def _referenced_files_payload(command: str, workdir: Any, state: Any) -> dict[str, Any]:
    """submit_job 载荷摘要里命令引用的文件指纹；读不出时退回旧摘要（守卫行为同升级前）。"""
    try:
        try:
            from .safe_bash import _referenced_files_payload as referenced
        except ImportError:
            from tools.safe_bash import _referenced_files_payload as referenced
        return referenced(command, workdir, state)
    except Exception:
        return {}


async def _submit_job(
    state: State,
    command: str,
    scheduler: str = "auto",
    job_name: str = "experiment_job",
    mpi_ranks: int = 1,
    cpus_per_rank: int = 1,
    gpus: int = 0,
    memory_gb: float = 4.0,
    storage_gb: float = 8.0,
    walltime_minutes: int | None = None,
    queue: str | None = None,
    nodelist: str | None = None,
    image: str | None = None,
    workdir: str | None = None,
    dry_run: bool = True,
    namespace: str | None = None,
    output_dir: str | None = None,
    output_paths: list[str] | None = None,
    stage: str | None = None,
    expected_duration_s: int | None = None,
    foreground_wait_s: int | None = None,
    hard_deadline_s: int | None = None,
    execution_params: dict[str, Any] | None = None,
    input_package_artifact_id: str | None = None,
    input_package_bindings: dict[str, str] | None = None,
    health_check: dict[str, Any] | None = None,
    stage_in: list[dict[str, str]] | None = None,
    route_step_id: str | None = None,
    expected_termination: dict[str, Any] | None = None,
    **_: Any,
) -> dict:
    """Render or submit a job through local/SLURM/PBS/Kubernetes."""
    # 判决拆除·第三波（rm:1799/1803/1806/1820/1822/1825 → schema，2026-09-02）：
    # command 非空、整数/正数下限、stage 与 scheduler 枚举、expected_duration_s
    # 下限全部由 submit_job schema 在派发口核；工具体内不再手写。
    # 例外：有限性。schema 的 `minimum` 挡得住 0/负数，挡不住 inf（NaN 靠比较
    # 恒假顺带被挡，inf 不是）。"内存是有限实数" 是 C 类物理事实，schema 表达不了，
    # 所以这一条留在工具体内 —— 不是把删掉的墙偷偷加回来，是它本来就不在 schema
    # 能覆盖的范围内。
    if memory_gb is not None and (
        not isinstance(memory_gb, (int, float))
        or isinstance(memory_gb, bool)
        or not math.isfinite(float(memory_gb))
        or float(memory_gb) <= 0
    ):
        return {
            "status": "error",
            "reason": "invalid_requested_resources",
            "error": "memory_gb must be a positive finite number",
        }
    # ``scope`` answers whether this run may produce scientific evidence;
    # ``stage`` is the existing framework execution-stage vocabulary.  They
    # must not be conflated as ``stage=operation``.  When an operation caller
    # omits stage, choose the compatible non-results stage.  An explicit value
    # remains authoritative and is validated below, so invalid caller input is
    # never silently rewritten.
    static_block = _submission_static_validity_block(state, command)
    if static_block is not None:
        return static_block
    if isinstance(workdir, str) and not workdir.strip():
        workdir = None
    if isinstance(output_dir, str) and not output_dir.strip():
        output_dir = None
    # stage 只接受旧 caller 的输入以便迁移审计，不参与目录、科学身份、资源强度
    # 或授权。真正的 execution_class 在路线和机械动作解析后由工具内部派生。
    legacy_stage_hint = (
        str(stage).strip().lower()
        if stage is not None and str(stage).strip()
        else None
    )
    if legacy_stage_hint is not None and legacy_stage_hint not in set(EXECUTION_STAGES):
        # stage 不对 LLM 暴露（它是旧 caller 的兼容输入），所以 schema 核不到它；
        # 这是调用方契约违规（C 类：值不在词表里），不是充分性判决。词表唯一
        # 真相源是 preflight.EXECUTION_STAGES，合法取值逐字列出送到调用方
        # （BF-12：封闭词表的合法取值必须随拒绝一起给出）。
        return {
            "status": "error",
            "error": (
                "stage 必须是 " + "、".join(EXECUTION_STAGES)
                + " 之一；scope（operation / scientific）是另一维，不能写进 stage。"
            ),
            "allowed_stages": list(EXECUTION_STAGES),
        }
    compatibility_stage = legacy_stage_hint or "diagnostic"
    if hard_deadline_s is not None and (
        not isinstance(hard_deadline_s, int)
        or isinstance(hard_deadline_s, bool)
        or hard_deadline_s <= 0
    ):
        return {"status": "error", "error": "hard_deadline_s 必须是正整数"}
    if foreground_wait_s is not None and (
        not isinstance(foreground_wait_s, int)
        or isinstance(foreground_wait_s, bool)
        or not 1 <= foreground_wait_s <= 900
    ):
        return {"status": "error", "error": "foreground_wait_s 必须是 1..900 的整数"}
    if dry_run and foreground_wait_s is not None:
        return {
            "status": "error",
            "reason": "foreground_wait_requires_real_submission",
            "error": "foreground_wait_s 只适用于 dry_run=false 的真实提交。",
        }
    scheduler = str(scheduler or "").strip().lower()
    if scheduler == "auto":
        try:
            scheduler = str(_discover(namespace)["recommended_default"]).lower()
        except Exception as exc:
            return {"status": "error", "error": (
                "无法解析 scheduler=auto；拒绝真实提交： "
                f"{type(exc).__name__}: {exc}")}
    if hard_deadline_s is not None and scheduler not in {"local", "kubernetes"}:
        return {
            "status": "error",
            "reason": "backend_deadline_contract_conflict",
            "error": (
                "hard_deadline_s 仅用于本地受管作业或具有完整 volume 合同的 "
                "Kubernetes 作业；SLURM/PBS 请显式使用 walltime_minutes。"
            ),
        }
    if scheduler == "local" and image:
        return {
            "status": "error",
            "error": (
                "local submission does not accept a caller-selected image; "
                "the deployment-pinned sandbox image is mandatory"
            ),
        }
    if scheduler == "local" and int(gpus or 0) > 0:
        # 本机原生隔离后端不提供 GPU 执行：core/sandbox.prepare_launch 对 gpus>0 直接抛
        # SandboxContractError。那道拒绝发生在人批准之后（#893）——确认卡白弹，批准的
        # 是一个注定起不来的作业。同一个物理事实挪到这里、确认卡之前说清楚，零副作用。
        # dry_run 同样拒：渲染一份本地永远起不来的脚本只会让"dry_run 成功"误导人。
        return {
            "status": "error",
            "error_code": "local_gpu_execution_unsupported",
            "error": (
                "scheduler=local 不能申请 GPU：本机原生隔离后端不提供 GPU 执行，这样的作业"
                "会在批准之后被拒。本次未生成脚本、未弹确认卡、未提交。"
            ),
            "requested_gpus": int(gpus),
            "side_effects": "none",
            "retryable": True,
            "next_actions": [
                "作业确实不需要 GPU：把 gpus 改为 0 后重新调用",
                "需要 GPU：改用有 GPU 的调度器（如 scheduler=\"slurm\"，先 dry_run 渲染）",
            ],
        }
    normalized_expected_termination, termination_refusal = _anchored_expected_termination(
        state, expected_termination)
    if termination_refusal is not None:
        return termination_refusal
    try:
        queue = _safe_scheduler_directive(queue, "queue", scheduler)
        nodelist = _safe_scheduler_directive(
            nodelist, "nodelist", scheduler)
    except ValueError as exc:
        return {
            "status": "error",
            "reason": "invalid_scheduler_directive",
            "error": str(exc),
            "blocker": {"kind": "invalid_scheduler_directive"},
        }
    if scheduler == "kubernetes":
        # 当前 API 只有提交机路径，没有 PVC/volume 名称、容器挂载点及
        # 输入/输出映射的权威契约。生成带宿主机 cd 的 YAML 会把“已渲染”
        # 误报成“可运行”；在契约存在前，dry-run 也只返回结构化 blocker，
        # 且不创建 runtime、脚本或 submission intent。
        try:
            state.append_transcript(
                "kubernetes_volume_contract_required",
                tool="submit_job",
                dry_run=bool(dry_run),
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "kubernetes_volume_contract_required",
            "error": (
                "Kubernetes 提交需要显式 PVC/volume、容器挂载点与输入输出"
                "映射契约；当前 submit_job 只有宿主机路径，已在生成 YAML "
                "和 submission intent 前拒绝。"
            ),
            "blocker": {
                "kind": "kubernetes_volume_contract_required",
                "suggested_owner": "framework_or_platform",
            },
        }
    if not dry_run and scheduler != "local":
        return {
            "status": "error",
            "error": (
                f"scheduler={scheduler} 尚未提供与本地同等级的受信沙盒执行器，"
                "因此只允许 dry_run 渲染，不允许真实提交。必须先建设调度器侧的"
                "只读根文件系统、显式卷、断网、cgroup/ephemeral-storage 和 PID 1 监督契约。"
            ),
            "blocker": {"kind": "remote_sandbox_contract_unavailable", "scheduler": scheduler},
        }
    # An omitted path receives the framework-owned, run-local location. An
    # explicitly supplied invalid path is never rewritten and remains rejected.
    for label, values in (
            ("workdir", [workdir] if workdir is not None else []),
            ("output_dir", [output_dir] if output_dir is not None else []),
            ("output_paths", list(output_paths or [])),
    ):
        for value in values:
            if isinstance(value, str) and _has_unresolved_path_placeholder(value):
                return _unresolved_scheduler_path_block(label=label, value=value)
    explicit_output_dir = bool(output_dir)
    explicit_workdir = workdir is not None
    route_action = _submission_route_action(
        command,
        stage=compatibility_stage,
        dry_run=bool(dry_run),
        route_step_id=route_step_id,
    )
    route_decision = _resolve_submission_route(state, route_action)
    use_build_resource_plan = (
        bool(route_action.get("mechanical_major_build"))
        or (
            route_decision.get("decision") == "matched_ready_step"
            and route_decision.get("declared_workdir_role") == "build_root"
        )
    )
    memory_contract = _submission_memory_contract(
        state,
        memory_gb=memory_gb,
        total_cpus=max(1, int(mpi_ranks) * int(cpus_per_rank)),
        use_build_resource_plan=use_build_resource_plan,
    )
    memory_gb = float(memory_contract["memory_gb"])
    pre_materialization_block = _enforce_submission_route(
        state, route_action, route_decision, phase="pre_materialization")
    if pre_materialization_block is not None:
        return pre_materialization_block
    if scheduler == "local" and explicit_workdir:
        # Authorization is more fundamental than existence: reject an
        # undeclared path without probing or materializing it.  This also
        # preserves the caller-actionable scheduler contract diagnostic for a
        # missing path outside every writable role.
        workdir_roles = matching_path_roles(str(workdir), state)
        allowed_workdir_roles = {
            "build_root", "run_root", "source_worktree_root",
        }
        if not any(
                role.role in allowed_workdir_roles
                and role.writable and not role.container_only
                for role in workdir_roles):
            return _scheduler_path_block(
                label="workdir",
                value=str(workdir),
                allowed_roles=allowed_workdir_roles,
                state=state,
            )
        if not os.path.isdir(str(workdir)):
            route_choice = _submission_route_workdir(
                state, route_decision, create=False,
            )
            try:
                exact_framework_root = (
                    route_choice.get("status") == "resolved"
                    and Path(str(workdir)).expanduser().resolve(strict=False)
                    == Path(str(route_choice.get("path"))).expanduser().resolve(
                        strict=False
                    )
                )
            except (OSError, RuntimeError, ValueError):
                exact_framework_root = False
            if exact_framework_root:
                _submission_route_workdir(
                    state, route_decision, create=True,
                )
        try:
            try:
                from .safe_bash import resolve_required_workdir
            except ImportError:
                from tools.safe_bash import resolve_required_workdir
            workdir_error = resolve_required_workdir(state, workdir)[1]
        except Exception as exc:
            return {
                "status": "error",
                "reason": "required_workdir_resolution_failed",
                "error": "本地作业工作目录校验异常，未创建 runtime 或脚本。",
                "blocker": {
                    "kind": "required_workdir_resolution_failed",
                    "reason": type(exc).__name__,
                },
            }
        if workdir_error is not None:
            return workdir_error
    runtime_root = experiment_output_dir(state, "runtime", create=True)
    workdir_resolution = "explicit" if explicit_workdir else "action_default"
    if workdir is None:
        route_choice = _submission_route_workdir(state, route_decision)
        if route_choice.get("status") == "resolved":
            workdir = str(route_choice["path"])
            workdir_resolution = "resolved"
        elif route_action.get("mechanical_major_build"):
            workdir = str(default_stage_workdir(
                state, "toolchain_build", create=True))
        else:
            workdir = str(experiment_output_dir(state, "runtime", create=True))
    if not output_dir:
        # 判决拆除 O5（rm:1005 降格）：缺省 output_dir 机械默认到 run 本地
        # runtime/logs；workdir 是 source_worktree_root 时把这次默认披露出来，
        # 让「日志会不会污染源码树」有账可查而不是靠拒绝挡住。
        output_dir = str(runtime_root / "logs")
        if workdir and scheduler in {"slurm", "pbs"}:
            try:
                if any(role.role == "source_worktree_root"
                       for role in matching_path_roles(workdir, state)):
                    state.append_transcript(
                        "scheduler_output_dir_defaulted",
                        workdir=workdir, output_dir=output_dir,
                        reason="workdir_is_source_worktree_root_and_no_output_dir_declared")
            except Exception:
                pass
    if scheduler == "local":
        workdir = os.path.realpath(os.path.expanduser(workdir))
        output_dir = os.path.realpath(os.path.expanduser(output_dir))
    output_roots = _normalize_output_roots(output_paths, workdir, runtime_root)
    try:
        validated_stage_in = _validate_stage_in(state, stage_in)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    try:
        try:
            from .safe_bash import _observed_workdir_roles
        except ImportError:
            from tools.safe_bash import _observed_workdir_roles
        route_action["workdir_roles"] = _observed_workdir_roles(state, workdir)
    except Exception:
        route_action["workdir_roles"] = []
    route_decision = dict(route_decision)
    route_decision["workdir_role_observed"] = (
        route_decision.get("declared_workdir_role")
        in route_action["workdir_roles"]
    )
    route_decision["workdir_resolution_status"] = workdir_resolution
    route_decision["resolved_workdir"] = str(workdir)
    route_action["payload_digest"] = hashlib.sha256(json.dumps(
        {
            "command": command,
            "scheduler": scheduler,
            "cwd": str(Path(str(workdir)).resolve(strict=False)),
            "mpi_ranks": int(mpi_ranks),
            "cpus_per_rank": int(cpus_per_rank),
            "gpus": int(gpus),
            "memory_gb": float(memory_gb),
            "memory_contract": memory_contract,
            "walltime_minutes": walltime_minutes,
            "hard_deadline_s": hard_deadline_s,
            "queue": queue,
            "nodelist": nodelist,
            "namespace": namespace,
            "execution_params": execution_params,
            "stage_in": validated_stage_in,
            # K9 改法 1：命令引用的脚本改了才算载荷变了（见 safe_bash._referenced_file_fingerprints）。
            **_referenced_files_payload(command, workdir, state),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    try:
        try:
            from .safe_bash import (
                _activity_path_role_guard, _bash_path_effects_guard)
        except ImportError:
            from tools.safe_bash import (
                _activity_path_role_guard, _bash_path_effects_guard)
        if scheduler != "kubernetes":
            path_effect_block = _bash_path_effects_guard(
                state, command, cwd=workdir,
                remote=scheduler in {"slurm", "pbs"},
                allow_authorization=False,
            )
            if path_effect_block is not None:
                return path_effect_block
        activity_block = _activity_path_role_guard(
            state, command, cwd=workdir, route_decision=route_decision)
        if activity_block is not None:
            return activity_block
    except Exception as exc:
        return {
            "status": "error",
            "reason": "submission_path_analysis_failed",
            "error": "作业路径分析异常，未生成脚本或提交作业。",
            "blocker": {
                "kind": "submission_path_analysis_failed",
                "reason": type(exc).__name__,
            },
        }
    _record_submission_route(state, route_action, route_decision)
    if validated_stage_in and scheduler == "kubernetes":
        return {"status": "error", "error": (
            "stage_in for Kubernetes requires an explicit volume/PVC contract; "
            "host paths are not accepted")}
    if scheduler == "kubernetes" and explicit_output_dir:
        # Pod 里的绝对路径与宿主机同名路径无关，所以宿主机上的 output_dir 对这个
        # 作业毫无意义：接受它只会让提交端建出一个没人写的空目录，然后作业把日志
        # 写进容器里同名的另一个地方。要落盘就得显式挂 volume/PVC。
        return {"status": "error", "error": (
            "output_dir for Kubernetes requires an explicit volume/PVC contract; "
            "a submit-host path is not visible inside the Pod")}
    # 判决拆除·第三波（rm:1908 降格，2026-09-02）：job 命令含 nohup/setsid/行尾 &/
    # disown/裸 sbatch 只是预测「会孤儿化」——作业脚本返回时容器/cgroup 会收掉子
    # 进程，不可逆损害不成立。照提交，submission 记录挂 unmanaged_background_launch
    # 见证（下面 submission_witness 定义后写入）。
    try:
        try:
            from . import timeout_escalation as _te
        except ImportError:
            from tools import timeout_escalation as _te
        unmanaged_background = _te.looks_backgrounded(command)
    except Exception:
        unmanaged_background = False
    # Validate the actual shell body before any confirmation is consumed.
    # ``workdir`` is only scheduler metadata: a command can still use `cd` or
    # redirection to write elsewhere, so it must pass the same node-local
    # path-role guard as safe_run_bash. This is a validity gate, never a bypass
    # permission.
    role_block = _scheduler_role_guard(
        state, scheduler, command, workdir, output_dir, output_roots)
    if role_block is not None:
        return role_block
    # 角色契约之后再问本机写边界：角色层的诊断更具体（哪个字段、该声明什么），
    # 先让它说话；这里只负责 local 特有的那条「写发生在提交机上」的限制。
    local_block = _local_job_boundary_block(state, scheduler, workdir, output_dir)
    if local_block is not None:
        return local_block
    contract = load_run_contract(state)
    scientific_primary = (
        str(contract.get("execution_mode") or "scientific") == "scientific"
        and str(contract.get("run_role") or "") == "primary"
    )
    formal_input_gate_required = (
        scientific_primary or requires_experiment_fallback_input_gate(contract)
    )
    formal_input_gate = {
        "evaluated": True,
        "required": bool(formal_input_gate_required),
        "run_role": contract.get("run_role"),
        "run_role_source": contract.get("run_role_source"),
    }
    if not formal_input_gate_required:
        role_reason = run_role_non_applicability_reason(contract)
        if role_reason:
            formal_input_gate["not_applicable_reason"] = role_reason
    # This event is the authoritative observation once execution reaches the
    # existing formal-input predicate. Earlier validation returns intentionally
    # have no observation: the gate was not evaluated, rather than evaluated
    # false. Scheduler results below project this exact object.
    state.append_transcript(
        "submit_job_formal_input_gate_evaluated", **formal_input_gate,
    )
    formal_scientific_action = (
        not dry_run
        and (
            route_decision.get("policy") == "formal_scientific_execution"
            or (formal_input_gate_required and route_action.get("mechanical_major_run"))
            or (
                formal_input_gate_required
                and route_decision.get("decision") == "matched_ready_step"
                and route_decision.get("authoritative") is True
                and route_decision.get("declared_workdir_role") == "run_root"
                and bool(set(route_decision.get("effective_effects") or []).intersection({
                    "process_tree", "external_job", "scientific_execution",
                }))
            )
        )
    )
    route_build_action = (
        route_decision.get("decision") == "matched_ready_step"
        and route_decision.get("authoritative") is True
        and route_decision.get("declared_workdir_role") == "build_root"
    )
    execution_class = (
        "simulation" if formal_scientific_action
        else "toolchain_build" if (
            route_action.get("mechanical_major_build") or route_build_action
        )
        else "diagnostic"
    )
    guard_process_tree = (
        bool(route_action.get("mechanical_major_build"))
        or bool(route_action.get("mechanical_major_run"))
        or "process_tree" in set(route_decision.get("effective_effects") or [])
    )
    precomputed_build_limits = None
    if execution_class == "toolchain_build" or guard_process_tree:
        enforced_deadline_s = (
            hard_deadline_s if scheduler == "local"
            else walltime_minutes * 60 if walltime_minutes is not None
            else None
        )
        precomputed_build_limits = derive_build_limits(
            state, command, enforced_deadline_s,
            requested_memory_gb=float(memory_gb),
            requested_total_cpus=(int(mpi_ranks) * int(cpus_per_rank)),
            exact_resource_contract=(memory_contract["mode"] == "fixed"),
            memory_request_source=str(memory_contract["source"]),
        )
    # 判决拆除 O4（rm:1931 降格）随 origin/main 合入；守卫谓词取 main 的
    # `not dry_run` 而非 HEAD 的 formal_scientific_action —— 后者会让降格记账
    # 只对正式科学提交触发，等于把已授权的拆除静默缩小到一个子集。
    submission_witness: dict[str, Any] = {}
    if unmanaged_background:
        submission_witness["unmanaged_background_launch"] = {
            "hint": (
                "job command 含 nohup/setsid/行尾 &/disown 或裸 sbatch/qsub：作业脚本"
                "返回时其子进程随作业一起被收回，脱离的部分不会被 job_status / handoff "
                "观察到；submit_job 本身已是受管提交，payload 里不需要再后台化。"),
        }
        try:
            state.append_transcript(
                "unmanaged_background_launch_witnessed", tool="submit_job",
                scheduler=scheduler, command_preview=command[:200])
        except Exception:
            pass
    if not dry_run:
        contract = load_run_contract(state)
        # #726 第一刀同批（顺带落地判决核验的 rm:1931 deviated）：formal_simulation
        # 不再看 contract.stage —— 它缺省即 "simulation"（run_contract:331）,会让
        # 每个 primary run 恒判 formal_simulation=True,再撞下面 stage!=simulation
        # 就往 O4 账本灌一条恒真的"声明冲突已裁决"假事件。改用路线派生的
        # formal_scientific_action（这次提交是否真是正式科学执行）,并只在 caller
        # **明确声明了**一个非 simulation 的 legacy stage 时才披露覆写；stage=None
        # （没声明）不是冲突,不触发。
        formal_simulation = bool(formal_scientific_action)
        if formal_simulation and legacy_stage_hint not in (None, "simulation"):
            # 判决拆除 O4（rm:1931 降格，2026-08-31）：框架按权威源
            # （contract.run_role/stage）取值，声明不一致披露出来而不是拒绝。
            try:
                state.append_transcript(
                    "stage_declaration_overridden_by_contract",
                    declared_stage=legacy_stage_hint, authoritative_stage="simulation",
                    run_role=contract.get("run_role"))
            except Exception:
                pass
            submission_witness["stage_declaration_overridden"] = {
                "declared_stage": legacy_stage_hint, "authoritative_stage": "simulation"}
            stage = "simulation"
    if not dry_run:
        try:
            from .preflight import audit_execution_contract
            from .contract_audit import audit_input_delivery_for_execution
            from .run_contract import record_execution_precondition_witness, record_prereg_deviation
        except ImportError:
            from tools.preflight import audit_execution_contract
            from tools.contract_audit import audit_input_delivery_for_execution
            from tools.run_contract import record_execution_precondition_witness, record_prereg_deviation
        input_delivery = audit_input_delivery_for_execution(
            state,
            input_package_artifact_id,
            input_package_bindings,
        )
        state.append_transcript("input_delivery_preflight", **input_delivery)
        if not input_delivery.get("passed"):
            # 判决拆除 O2（rm:1946 降格，2026-08-31）：正式输入包未验收照跑；
            # input_delivery:unverified 如实进提交记录，且机械降
            # 执行前提见证（效应而非许可，run 内不可撤销）。
            record_execution_precondition_witness(
                state, "submit_job",
                "formal input package unverified at real submission")
            submission_witness["input_delivery_unverified"] = input_delivery
        execution_contract = audit_execution_contract(
            state, execution_params, stage=stage, runner="submit_job")
        state.append_transcript("execution_contract_preflight", **execution_contract)
        if not execution_contract.get("passed"):
            # 判决拆除 O1（rm:1951 降格，2026-08-31，专审一）：与冻结 prereg 不
            # 一致不再禁止提交 —— amendment 出口在本节点走不通（死路墙实证）。
            # 偏离必须申报：mismatched/missing/unexpected 三张表进 transcript、
            # 提交记录与 run manifest。（判决拆除·第三波 rm:1975 删：stage 已由
            # schema enum 限定在 EXECUTION_STAGES，stage_invalid 在此不可达。）
            record_prereg_deviation(state, "submit_job", {
                "kind": "execution_params_deviate_from_frozen_prereg",
                "blocking_reasons": execution_contract.get("blocking_reasons"),
                "mismatched_parameters": execution_contract.get("mismatched_parameters"),
                "missing_expected_parameters": execution_contract.get("missing_expected_parameters"),
                "unexpected_execution_parameters": execution_contract.get("unexpected_execution_parameters"),
            })
            submission_witness["prereg_deviation"] = {
                "blocking_reasons": execution_contract.get("blocking_reasons"),
                "mismatched_parameters": execution_contract.get("mismatched_parameters"),
                "missing_expected_parameters": execution_contract.get("missing_expected_parameters"),
                "unexpected_execution_parameters": execution_contract.get("unexpected_execution_parameters"),
            }
    if not dry_run:
        conflicts = _active_output_conflicts(state, output_roots)
        if conflicts:
            return {
                "status": "error",
                "error": ("存在仍在运行或未对账的 external job，其输出路径与本次提交重叠；"
                          "先用 job_status/continue 对账，或以 cancel_job 受管取消后再提交。"
                          "其中 kind=unknown_orphan 的是隔离锁而非活作业，对账/取消对它无效："
                          "换一组不重叠的 output_paths，或按该条 conflicts[].resolution 用 "
                          "resolve_unknown_orphan 出具机械解除证据。"),
                "blocker": {"kind": "active_external_job_output_conflict",
                            "output_roots": output_roots, "conflicts": conflicts},
            }
    if formal_scientific_action:
        try:
            from .preflight import audit_experiment_preflight
            preflight = audit_experiment_preflight(state, phase="hpc_submit")
            state.append_transcript("experiment_preflight", **preflight)
        except Exception as exc:
            preflight = {
                "passed": False,
                "reason": f"preflight error: {type(exc).__name__}: {exc}",
                "blocking_reasons": ["audit_error"],
            }
        if not preflight["passed"]:
            # 判决拆除 O3（rm:1977 降格，2026-08-31）：聚合闸拆开全是记录完备性
            # —— 照跑并把未过项如实进提交记录与 run manifest；stage 不合法留 C。
            if "stage" in (preflight.get("blocking_reasons") or []):
                return {
                    "status": "error",
                    "error": "stage must be diagnostic, build, or simulation",
                    "preflight": preflight,
                }
            try:
                incomplete = state.hook_state.setdefault("experiment_preflight_incomplete", [])
                if isinstance(incomplete, list):
                    incomplete.append({
                        "phase": "hpc_submit",
                        "blocking_reasons": preflight.get("blocking_reasons"),
                        "reason": preflight.get("reason"),
                    })
            except Exception:
                pass
            submission_witness["experiment_preflight_incomplete"] = {
                "blocking_reasons": preflight.get("blocking_reasons"),
                "reason": preflight.get("reason"),
            }
    # 路线授权放在全部确定性语义/路径/科学检查之后、HITL 与 submit 之前。
    # 这样错误原因保持具体（例如 prereg 参数不匹配），同时仍保证零提交。
    route_block = _enforce_submission_route(
        state, route_action, route_decision, phase="pre_spawn")
    if route_block is not None:
        return route_block
    payload_executables: list[dict[str, str]] = []
    if scheduler == "local" and not dry_run:
        local_sandbox_block, local_sandbox = _preflight_local_submission_sandbox(
            state,
            command,
            workdir=workdir,
            output_roots=output_roots,
            runtime_root=runtime_root,
            output_dir=output_dir,
            job_name=job_name,
            stage_in=validated_stage_in,
        )
        if local_sandbox_block is not None:
            return local_sandbox_block
        if local_sandbox is None:
            # 投影与 block 互斥，这条不该发生；真发生了也必须是零消耗拒绝，
            # 而不是让 payload 预检退化成无范围可用。
            return _local_sandbox_path_contract_error(
                RuntimeError("local_sandbox_projection_missing"),
                scheduler="local", dry_run=False, job_name=job_name)
        # 可执行目标预检（与本地 safe_run_bash 同口径）：必须发生在任何
        # 持久化/HITL 之前——失败零执行、零 intent、零 route attempt 消耗，
        # step 留在 ready。判定用上面刚解析出的挂载集合与作业真实 cwd，
        # 而不是宿主机全盘视角（E-14）。
        payload_preflight = _preflight_local_payload_executables(
            state,
            command,
            sandbox=local_sandbox,
            stage_in=validated_stage_in,
        )
        if asyncio.iscoroutine(payload_preflight):
            # 真实实现是协程（ABI 探针经咽喉 spawn_and_wait）；测试桩可为同步。
            payload_preflight = await payload_preflight
        payload_exec_block, payload_executables = payload_preflight
        if payload_exec_block is not None:
            return payload_exec_block
    if not dry_run:
        # Managed lifecycle ownership and human confirmation are independent.
        # Reuse the single high-risk classifier; only remote submission or a
        # matched high-risk local payload enters the one-shot HITL contract.
        from shared.lib import dangerous_commands as _danger
        from .execution_guard import classify_high_risk
        highrisk_category = classify_high_risk(command, mode="shell")
        from core.sandbox import image_name as sandbox_image_name, trusted_image_id

        try:
            sandbox_identity = {
                "image": sandbox_image_name(),
                "image_id": trusted_image_id(),
                "network": "none",
            }
        except Exception as exc:
            return {"status": "error", "error": f"mandatory sandbox unavailable: {exc}"}
        category = _managed_job_confirmation_category(
            action="submit", scheduler=scheduler,
            highrisk_category=highrisk_category,
        )
        if category is None:
            state.append_transcript(
                "job_submission_confirmation_not_required", scheduler=scheduler,
                job_name=job_name, command_preview=command[:200],
                reason="local_managed_no_highrisk",
            )
        else:
            approval_text = _job_submission_confirmation_text({
                "operation": "submit_external_job",
                "command": command,
                "scheduler": scheduler,
                "job_name": job_name,
                "mpi_ranks": mpi_ranks,
                "cpus_per_rank": cpus_per_rank,
                "gpus": gpus,
                "memory_gb": memory_gb,
                "storage_gb": storage_gb,
                "walltime_minutes": walltime_minutes,
                "queue": queue,
                "nodelist": nodelist,
                "image": image,
                "workdir": workdir,
                "output_dir": output_dir,
                "output_roots": output_roots,
                "namespace": namespace,
                "execution_class": execution_class,
                "legacy_stage_hint": legacy_stage_hint,
                "expected_duration_s": expected_duration_s,
                "foreground_wait_s": foreground_wait_s,
                "hard_deadline_s": hard_deadline_s,
                "execution_params": execution_params,
                "input_package_artifact_id": input_package_artifact_id,
                "input_package_bindings": input_package_bindings,
                "health_check": health_check,
                # 只在声明时进确认文本：不声明的提交确认文本不变，升级前已批准的卡照样认。
                **({"expected_termination": normalized_expected_termination}
                   if normalized_expected_termination else {}),
                "stage_in": validated_stage_in,
                # 与 stage_in 同理：批准针对的是这些目标当时的内容（sha256）。
                "payload_executables": payload_executables,
                "memory_contract": memory_contract,
                "route_step_id": route_step_id,
                "sandbox": sandbox_identity,
            })
            if _danger.bypass_enabled():
                state.append_transcript(
                    "job_submission_bypassed", scheduler=scheduler,
                    job_name=job_name, command_preview=command[:200],
                )
            elif _danger.is_confirmed(state, approval_text):
                # 批准可能是上一轮给的。人看到并批准的是**当时那些文件的内容**
                # （payload 里带 sha256），所以提交前重新算一次；对不上说明源文件
                # 在批准之后被改过，这次批准对它不成立。路径没变而内容变了，是这条
                # 机制唯一能被悄悄绕过的形态。
                try:
                    recheck = _validate_stage_in(state, stage_in)
                except ValueError as exc:
                    return {"status": "error", "error": str(exc)}
                if recheck != validated_stage_in:
                    state.append_transcript(
                        "job_submission_stage_in_changed", scheduler=scheduler,
                        job_name=job_name)
                    return {
                        "status": "error",
                        "error": ("stage_in 的源文件在批准之后发生了变化，"
                                  "本次批准已失效；请重新发起提交并重新确认。"),
                        "blocker": {"kind": "stage_in_changed_after_approval",
                                    "human_action": "resubmit_and_reconfirm"},
                    }
                _danger.consume_confirmation(state, approval_text)
                state.append_transcript(
                    "job_submission_confirmed", scheduler=scheduler,
                    job_name=job_name, command_preview=command[:200],
                )
            else:
                state.append_transcript(
                    "job_submission_blocked_pending_confirm", scheduler=scheduler,
                    job_name=job_name, command_preview=command[:200],
                )
                # 声明会改变「这次非零退出算不算达成任务」，人批准时必须在卡片上看得到。
                termination_preview = (
                    "expected_termination="
                    + "、".join(
                        ([f"退出码 {normalized_expected_termination['exit_codes']}"]
                         if normalized_expected_termination.get("exit_codes") else [])
                        + (["计划内停止（按任务要求中途用 cancel_job 停下）"]
                           if normalized_expected_termination.get("planned_stop") else []))
                    + f"（任务原文：{normalized_expected_termination['task_quote']}）\n"
                    if normalized_expected_termination else "")
                return _danger.build_pause_payload(
                    state, tool="submit_job", text=approval_text,
                    category=category,
                    preview=(
                        f"scheduler={scheduler}\njob_name={job_name}\n"
                        f"workdir={workdir or ''}\noutput_dir={output_dir or ''}\n"
                        f"stage_in={json.dumps(validated_stage_in, ensure_ascii=False)}\n"
                        f"memory_contract={json.dumps(memory_contract, ensure_ascii=False)}\n"
                        # 「它最多能跑多久」是批准这件事时第二重要的数（第一是命令
                        # 本身）：它决定这台机器要被占多久，也决定作业会不会在跑到
                        # 一半时被杀。2026-09-07 实测 —— 卡片上没有它，人只能在事后
                        # 从提交记录里翻出 `walltime_seconds`。内存契约都摆出来了，
                        # 时间没道理不摆。
                        f"walltime={_walltime_for_a_human(scheduler, walltime_minutes)}\n"
                        f"{termination_preview}"
                        f"command={command}"
                    ),
                )
    route_binding = None
    route_attempt_finished = False
    if not dry_run:
        route_binding, route_binding_error = _begin_submission_route_attempt(
            state, route_decision, route_action)
        if route_binding_error is not None:
            return route_binding_error
    try:
        action_census_token = _begin_submission_action_census(
            state, route_action, route_decision, route_binding,
        )
    except Exception as exc:
        action_census_token = {
            "status": "error",
            "error_code": "execution_action_census_admission_failed",
            "reason": type(exc).__name__,
            "error": "执行动作准入账本无法持久化，作业未物化、未提交。",
        }
    if action_census_token.get("status") != "success":
        if route_binding is not None:
            _finish_submission_route_attempt(
                state, route_binding, result=action_census_token,
            )
        return action_census_token
    action_census_finished = False
    submission_nonce = str(
        (route_binding or {}).get("attempt_id") or uuid.uuid4().hex
    )
    try:
        result = _submit_sync(
            experiment_output_dir(state, "runtime", create=True),
            scheduler, command, job_name,
            int(mpi_ranks), int(cpus_per_rank), int(gpus), float(memory_gb),
            float(storage_gb),
            int(walltime_minutes) if walltime_minutes is not None else None,
            queue, nodelist, image, workdir, bool(dry_run), namespace,
            output_dir, output_paths, execution_class, expected_duration_s,
            execution_params, health_check,
            # 预检（沙箱挂载集合 / payload 可执行目标）按 validated_stage_in 放行，
            # 提交必须带同一份；state 是本地提交的状态闸读的那份。见 #791。
            stage_in=validated_stage_in,
            state=state,
            guard_process_tree=guard_process_tree,
            submission_nonce=submission_nonce,
            route_attempt_id=(route_binding or {}).get("attempt_id"),
            memory_contract=memory_contract,
            precomputed_build_limits=precomputed_build_limits,
            hard_deadline_s=hard_deadline_s,
            payload_executables=payload_executables,
        )
        result["formal_input_gate"] = formal_input_gate
        try:
            action_census = _finish_submission_action_census(
                state,
                action_census_token,
                dry_run=bool(dry_run),
                scheduler=scheduler,
                result=result,
            )
        except Exception as exc:
            action_census = {
                "passed": False,
                "status": "audit_error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        action_census_finished = True
        if not action_census.get("passed"):
            result["execution_action_census"] = action_census
            if not dry_run and (
                result.get("status") in {
                    "success", "accepted_identity_unresolved",
                    "submission_outcome_unknown",
                }
                or result.get("container_runtime_id")
            ):
                result["do_not_resubmit"] = True
        if result.get("status") in {
            "accepted_identity_unresolved", "submission_outcome_unknown",
        }:
            recovery = _persist_submission_recovery(
                state, result, RuntimeError(str(result.get("error") or "missing job identity")))
            task_id = _persist_external_job_identity_recovery_workflow(state, result)
            _record_submission_identity_blocker(state, result, recovery)
            response = {
                **result,
                "submission_persistence": recovery,
                "identity_recovery": {
                    "workflow_status": "accepted_identity_unresolved",
                    "task_id": task_id,
                    "recovery_artifact_id": recovery.get("artifact_id"),
                },
                "blocker": {
                    "kind": "external_job_identity_unresolved",
                    "recovery_artifact_id": recovery.get("artifact_id"),
                    "scheduler": result.get("scheduler"),
                    "namespace": result.get("namespace"),
                },
            }
            route_receipt_block = _finish_submission_route_attempt(
                state, route_binding, result=response)
            route_attempt_finished = True
            if route_receipt_block is not None:
                response["route_receipt"] = route_receipt_block
            return response
        if result.get("status") == "success":
            result["execution_class"] = execution_class
            # 兼容旧 consumer；值由工具派生，绝不回写 caller 的 stage 提示。
            result["stage"] = execution_class
            if legacy_stage_hint is not None:
                result["legacy_stage_hint"] = legacy_stage_hint
            result["expected_duration_s"] = expected_duration_s
            result["execution_params"] = execution_params
            result["input_package_artifact_id"] = input_package_artifact_id
            result["input_package_bindings"] = input_package_bindings
            result["health_contract"] = result.get("health_contract") or {}
            if normalized_expected_termination:
                result["expected_termination"] = normalized_expected_termination
            # 判决拆除 O1/O2/O3/O4：申报的偏离与未验收见证进提交记录本体 ——
            # 记录必须把整个命题带上，含失败的那半。
            if submission_witness:
                result["submission_witness"] = submission_witness
            try:
                artifact = state.save_artifact(
                    "job_submission",
                    f"job_submission_{state.run_id}_{result['submission_nonce']}",
                    json.dumps(result, ensure_ascii=False, indent=2),
                    metadata={"scheduler": result.get("scheduler"), "dry_run": dry_run,
                              "namespace": result.get("namespace"),
                              "launch_host": result.get("launch_host"),
                              "execution_class": execution_class,
                              "output_roots": result.get("output_roots", [])},
                )
                result["submission_artifact_id"] = artifact.get("id")
            except Exception as exc:
                if dry_run:
                    raise
                recovery = _persist_submission_recovery(state, result, exc)
                result["external_workflow_task_id"] = _persist_external_job_workflow(state, result)
                _record_submission_persistence_blocker(state, result, recovery)
                response = {
                    **result,
                    "status": "submitted_needs_recovery",
                    "error": ("scheduler accepted the job but the primary submission receipt "
                              "was not persisted; do not resubmit"),
                    "submission_persistence": recovery,
                    "blocker": {
                        "kind": "external_job_submission_persistence_failed",
                        "recovery_artifact_id": recovery.get("artifact_id"),
                        "scheduler": result.get("scheduler"),
                        "job_id": result.get("job_id"),
                        "namespace": result.get("namespace"),
                        "launch_host": result.get("launch_host"),
                    },
                }
                route_receipt_block = _finish_submission_route_attempt(
                    state, route_binding, result=response)
                route_attempt_finished = True
                if route_receipt_block is not None:
                    response["route_receipt"] = route_receipt_block
                return response

            if not dry_run:
                try:
                    _persist_execution_environment_evidence(state, result, "submitted")
                except Exception:
                    log.warning("unable to persist container execution evidence", exc_info=True)
                result["external_workflow_task_id"] = _persist_external_job_workflow(state, result)
            # 只有真实提交才算"实际运行参数"；dry_run 没跑过任何东西。
            # 这些值是工具入参，不是 LLM 自述，可以直接作为审计证据。
            if not dry_run:
                record_actual_run_params(state, "submit_job", {
                    "scheduler": result.get("scheduler") or scheduler,
                    "mpi_ranks": int(mpi_ranks),
                    "cpus_per_rank": int(cpus_per_rank),
                    "gpus": int(gpus),
                    "memory_gb": float(memory_gb),
                    "storage_gb": float(storage_gb),
                    "walltime_minutes": (
                        int(walltime_minutes)
                        if walltime_minutes is not None else None),
                    "hard_deadline_s": hard_deadline_s,
                    "foreground_wait_s": foreground_wait_s,
                    "queue": queue,
                    "nodelist": nodelist,
                    "stage": execution_class,
                    "execution_class": execution_class,
                    "expected_duration_s": expected_duration_s,
                    **(execution_params or {}),
                })
        if not dry_run and not route_attempt_finished:
            route_receipt_block = _finish_submission_route_attempt(
                state, route_binding, result=result)
            route_attempt_finished = True
            if route_receipt_block is not None:
                if result.get("status") == "success" or result.get("job_id"):
                    return _submission_route_receipt_failure(
                        result, route_receipt_block)
                result["route_receipt"] = route_receipt_block
        if not action_census.get("passed"):
            return _submission_action_census_failure(
                result,
                action_census,
                action_census_token,
                dry_run=bool(dry_run),
            )
        # The durable submission record and the route `submitted` outcome must
        # exist before any synchronous UX wait.  Core may hand control back
        # while this await continues; recovery and duplicate-submit prevention
        # therefore never depend on the in-memory wait task.
        if (
            not dry_run
            and foreground_wait_s is not None
            and result.get("status") == "success"
            and result.get("job_id")
        ):
            result["submission_status"] = "submitted"
            waited = await _wait_for_external_job(
                state,
                str(result.get("scheduler") or scheduler),
                str(result["job_id"]),
                namespace=result.get("namespace"),
                max_wait_s=foreground_wait_s,
                _allow_short_wait=True,
            )
            result["foreground_wait"] = waited
            wait_outcome = str(waited.get("wait_outcome") or "")
            if wait_outcome == "scheduler_terminal":
                result["workflow_status"] = "awaiting_analysis"
            elif waited.get("status") == "success":
                result["workflow_status"] = "awaiting_external_job"
            else:
                result["workflow_status"] = "submitted_health_unknown"
            try:
                state.append_transcript(
                    "job_foreground_wait_finished",
                    scheduler=result.get("scheduler"),
                    job_id=result.get("job_id"),
                    foreground_wait_s=foreground_wait_s,
                    wait_outcome=wait_outcome or None,
                    wait_status=waited.get("status"),
                )
            except Exception:
                pass
        return result
    except RunCancelled:
        # The job already has a durable identity and submitted route outcome.
        # Cancelling this local await must not rewrite that fact as route
        # failure; explicit job cancellation remains cancel_job's job.
        raise
    except Exception as e:
        action_census: dict[str, Any] | None = None
        if not action_census_finished:
            try:
                action_census = _finish_submission_action_census(
                    state,
                    action_census_token,
                    dry_run=bool(dry_run),
                    scheduler=scheduler,
                    error=e,
                )
            except Exception as census_exc:
                action_census = {
                    "passed": False,
                    "status": "audit_error",
                    "reason": f"{type(census_exc).__name__}: {census_exc}",
                }
            action_census_finished = True
        if not dry_run:
            result = {
                "status": "submission_outcome_unknown",
                "scheduler": scheduler,
                "dry_run": False,
                "job_name": job_name,
                "job_id": None,
                "namespace": namespace,
                "workdir": workdir,
                "scheduler_output_dir": output_dir,
                "output_roots": output_roots,
                "submission_nonce": submission_nonce,
                "route_attempt_id": (route_binding or {}).get("attempt_id"),
                "do_not_resubmit": True,
                "safe_to_retry": False,
                "error": f"{type(e).__name__}: {e}",
                "execution_action_census": action_census,
            }
            recovery = _persist_submission_recovery(state, result, e)
            task_id = _persist_external_job_identity_recovery_workflow(
                state, result,
            )
            _record_submission_identity_blocker(state, result, recovery)
            response = {
                **result,
                "submission_persistence": recovery,
                "identity_recovery": {
                    "workflow_status": "submission_outcome_unknown",
                    "task_id": task_id,
                    "recovery_artifact_id": recovery.get("artifact_id"),
                },
                "blocker": {
                    "kind": "external_job_submission_outcome_unknown",
                    "recovery_artifact_id": recovery.get("artifact_id"),
                    "scheduler": scheduler,
                    "namespace": namespace,
                },
            }
            if not route_attempt_finished:
                route_receipt_block = _finish_submission_route_attempt(
                    state, route_binding, result=response,
                )
                route_attempt_finished = True
                if route_receipt_block is not None:
                    response["route_receipt"] = route_receipt_block
            return response
        if not route_attempt_finished:
            _finish_submission_route_attempt(state, route_binding, error=e)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


_LOCAL_CONTAINER_ID = re.compile(r"^hf-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,124}$")
_DOCKER_RUNTIME_ID = re.compile(r"^[0-9a-f]{64}$")


def _local_job_ended_on_its_own(sandbox_state: dict[str, Any]) -> bool:
    """The native supervisor wrote this job's own exit code (core/isolation/
    _native_job.py: ``exited`` + code, or ``dead`` + 127 on launch failure).
    Absent, ``created``, running, and ``dead`` without a code are not proof."""
    exit_code = sandbox_state.get("exit_code")
    return bool(sandbox_state.get("exists") and not sandbox_state.get("running")
                and isinstance(exit_code, int) and not isinstance(exit_code, bool))


def _remote_job_ended_on_its_own(
    scheduler: str, raw: dict[str, Any], job_id: str,
) -> dict[str, Any] | None:
    """远端调度器的正向终止证据；拿不到就返回 None（当作读不出，取消照旧可行）。

    ``_scheduler_phase`` 的 terminal 只表示"调度器不再持有它"，不是"它自己结束了"。
    2026-09-12 审查实测：k8s 作业在重试退避窗口里 status 是 {failed: 1} 且 active 缺席，
    phase 就报 terminal —— 一个还在跑、马上要起下一个 pod 的作业会被判成已结束，于是
    取消不掉（违反不变量 8）。本地那条分支要求监督进程亲手写下的退出码，远端也该同样
    要求作业自己的终止信号。
    """
    kind = str(scheduler or "").lower()
    stdout = str(raw.get("stdout") or "")
    if kind == "kubernetes":
        try:
            status = json.loads(stdout).get("status") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        for condition in status.get("conditions") or []:
            if (isinstance(condition, dict)
                    and str(condition.get("type")) in {"Complete", "Failed"}
                    and str(condition.get("status")) == "True"):
                return {"source": "kubernetes_condition",
                        "condition": condition.get("type"),
                        "completion_time": status.get("completionTime")}
        if status.get("completionTime"):
            return {"source": "kubernetes_completion_time",
                    "completion_time": status.get("completionTime")}
        return None
    if kind == "pbs":
        # PBS F/C/E is positive terminal state; carry the scheduler exit code when present.
        if not re.search(r"job_state\s*=\s*[CEF]\b", stdout):
            return None
        evidence: dict[str, Any] = {"source": "pbs_job_state"}
        exit_match = re.search(
            r"(?im)^\s*exit_status\s*=\s*(-?\d+)\s*$", stdout,
        )
        if exit_match is not None:
            exit_code = int(exit_match.group(1))
            evidence.update({
                "exit_code": exit_code,
                "returncode": exit_code,
                "succeeded": exit_code == 0,
            })
        return evidence
    if kind == "slurm":
        # Queue absence is not terminal proof. Accounting allocation state owns
        # terminality; step rows contribute only OOM and resource facts.
        accounting = _run(_slurm_accounting_argv(str(job_id)), timeout=10)
        facts = _slurm_accounting_facts(str(job_id), accounting)
        if facts is None or facts.get("terminal") is not True:
            return None
        return {"source": "slurm_accounting", **facts}
    return None


async def _observed_job_end(
    state: Any, record: dict[str, Any],
) -> dict[str, Any] | None:
    """Positive evidence that this job already ended on its own, else None.

    Read-only.  None means running *or unknown*: cancellation stays possible
    (invariant 8).  A dict means cancelling would record ``cancelled`` for a job
    whose own terminal state is readable (invariant 6) — close it with
    finalize_external_job instead.
    """
    receipt = _operation_closure_receipt(state, record, None)
    if receipt.get("status") == "success":
        payload = receipt.get("payload") or {}
        return {
            "source": "operation_closure_receipt",
            "receipt_artifact_id": receipt.get("artifact_id"),
            "recorded_outcome": payload.get("outcome"),
            "exit_code": (payload.get("health") or {}).get("exit_code"),
        }
    scheduler = str(record.get("scheduler") or "").lower()
    try:
        result = await asyncio.to_thread(
            _job_status_sync, scheduler, str(record.get("job_id") or ""),
            record.get("namespace"), remote_host=record.get("launch_host"),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception:
        return None
    if _scheduler_phase(scheduler, result) != "terminal":
        return None
    if scheduler != "local":
        # sacct 带 10 秒超时，同步调用会卡住整个事件循环（2026-09-13 审查实测 10.06 秒）。
        ended = await asyncio.to_thread(
            _remote_job_ended_on_its_own,
            scheduler, (result.get("raw") or {}), str(record.get("job_id") or ""))
        if ended is None:
            return None        # 调度器不再持有它 ≠ 它自己结束了：按读不出处理
        evidence = {
            **ended,
            "scheduler_phase": "terminal",
            "scheduler_result": result.get("raw"),
        }
        declared = record.get("expected_termination")
        if isinstance(declared, dict) and declared.get("exit_codes"):
            exit_code = evidence.get("returncode")
            verdict = _termination_verdict(
                record,
                {},
                exit_code=(
                    exit_code
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else None
                ),
            )
            matched = verdict.get("termination_matched")
            if (evidence.get("oom_killed") is True
                    or evidence.get("scheduler_terminated") is True):
                # 与 finalize 侧同一判据：被调度器结束 ⇒ 预期终止不成立。
                matched = False
            evidence["termination_matched"] = matched
            evidence["termination"] = verdict
        return evidence
    sandbox_state = (result.get("raw") or {}).get("sandbox_state") or {}
    if not _local_job_ended_on_its_own(sandbox_state):
        return None
    return {"source": "native_job_record", "scheduler_phase": "terminal",
            "sandbox_status": sandbox_state.get("status"),
            "exit_code": sandbox_state.get("exit_code")}


def _local_container_status(
    job_id: str,
    expected_container_id: str | None = None,
) -> dict[str, Any]:
    """Read the authoritative Docker state for one local sandbox job."""
    if not _LOCAL_CONTAINER_ID.fullmatch(str(job_id)):
        return {"ok": False, "returncode": None, "stdout": "", "stderr": (
            "scheduler=local 的 job_id 必须是 submit_job 返回的 hf-... 受管容器标识，"
            f"不能使用 PID 或 job_name；收到 {str(job_id)[:80]!r}")}
    from core import sandbox

    container = sandbox.inspect_container(str(job_id))
    if container.get("error"):
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": str(container["error"]), "sandbox_state": container}
    if container.get("exists"):
        if not container.get("managed"):
            return {"ok": False, "returncode": None, "stdout": "",
                    "stderr": "同名容器不带 harness 受管标签；拒绝读取或操作",
                    "sandbox_state": container}
        if not _DOCKER_RUNTIME_ID.fullmatch(str(expected_container_id or "")):
            return {"ok": False, "returncode": None, "stdout": "",
                    "stderr": "受管作业记录缺少不可变 Docker container ID；拒绝跟随可复用容器名",
                    "sandbox_state": container}
        if container.get("id") != expected_container_id:
            return {"ok": False, "returncode": None, "stdout": "",
                    "stderr": "容器名已被其他实例复用；不可变 container ID 不匹配",
                    "sandbox_state": container}
        if container.get("kind") != "job":
            return {"ok": False, "returncode": None, "stdout": "",
                    "stderr": "受管容器不是 local job；拒绝读取或操作",
                    "sandbox_state": container}
        if container.get("namespace") != sandbox.sandbox_namespace():
            return {"ok": False, "returncode": None, "stdout": "",
                    "stderr": "受管作业不属于当前 sandbox namespace；拒绝读取或操作",
                    "sandbox_state": container}
    return {
        "ok": True,
        "returncode": 0,
        "stdout": "RUNNING" if container.get("running") else "NOT_RUNNING",
        "stderr": "",
        "sandbox_state": container,
    }


def _cancellation_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Freeze every field needed to disambiguate one cancellation target."""
    return {field: record.get(field) for field in _CANCELLATION_IDENTITY_FIELDS}


def _operation_closure_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Project onto the frozen v0/v1/v2 receipt identity wire schema."""
    return {
        field: record.get(field)
        for field in _OPERATION_CLOSURE_IDENTITY_FIELDS
    }


def _operation_closure_identity_key(identity: dict[str, Any]) -> str:
    """Normalize only fields frozen into the v0/v1/v2 receipt schema."""
    normalized = {}
    for field in _OPERATION_CLOSURE_IDENTITY_FIELDS:
        value = str(identity.get(field) or "")
        if field in {"scheduler", "launch_host", "scheduler_cluster"}:
            value = value.casefold()
        normalized[field] = value
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _cancellation_identity_key(identity: dict[str, Any]) -> str:
    normalized = {
        "scheduler": str(identity.get("scheduler") or "").casefold(),
        "job_id": str(identity.get("job_id") or ""),
        "namespace": str(identity.get("namespace") or ""),
        "launch_host": str(identity.get("launch_host") or "").casefold(),
        "scheduler_cluster": str(identity.get("scheduler_cluster") or "").casefold(),
        "resource_uid": str(identity.get("resource_uid") or ""),
        "submission_nonce": str(identity.get("submission_nonce") or ""),
        "process_group_id": str(identity.get("process_group_id") or ""),
        "process_start_ticks": str(identity.get("process_start_ticks") or ""),
        "container_runtime_id": str(identity.get("container_runtime_id") or ""),
    }
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


class _CancellationLedgerError(RuntimeError):
    pass


def _read_cancellation_artifact_strict(
    state: Any, artifact: dict[str, Any], artifact_type: str,
) -> dict[str, Any]:
    artifact_id = str(artifact.get("id") or "")
    try:
        record = state.read_artifact(artifact_id) or {}
        if (record.get("type") != artifact_type
                or record.get("produced_by_node_type") != "experiment"
                or record.get("produced_by_run_id") != state.run_id):
            raise _CancellationLedgerError(
                f"{artifact_type} {artifact_id!r} has invalid owner or type")
        payload = json.loads(str(record.get("content") or ""))
    except Exception as exc:
        raise _CancellationLedgerError(
            f"{artifact_type} {artifact_id!r} is unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise _CancellationLedgerError(
            f"{artifact_type} {artifact_id!r} is not a JSON object")
    return payload


def _latest_cancellation_transaction(
    state: Any, record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the latest intent and its outcome for this exact target identity."""
    expected = _cancellation_identity_key(_cancellation_identity(record))
    intents: list[dict[str, Any]] = []
    try:
        artifacts = state.list_artifacts(
            _EXTERNAL_JOB_CANCELLATION_INTENT_TYPE, own_only=True) or []
    except Exception as exc:
        raise _CancellationLedgerError(
            "cannot list external-job cancellation intents") from exc
    for artifact in artifacts:
        payload = _read_cancellation_artifact_strict(
            state, artifact, _EXTERNAL_JOB_CANCELLATION_INTENT_TYPE)
        if (not isinstance(payload.get("identity"), dict)
                or not payload.get("cancellation_id")):
            raise _CancellationLedgerError(
                "cancellation intent is missing identity or cancellation_id")
        if _cancellation_identity_key(payload["identity"]) == expected:
            intents.append({**payload, "intent_artifact_id": artifact.get("id")})
    if not intents:
        return None
    intent = max(intents, key=lambda item: str(item.get("recorded_at") or ""))
    outcomes: list[dict[str, Any]] = []
    try:
        artifacts = state.list_artifacts(
            _EXTERNAL_JOB_CANCELLATION_OUTCOME_TYPE, own_only=True) or []
    except Exception as exc:
        raise _CancellationLedgerError(
            "cannot list external-job cancellation outcomes") from exc
    for artifact in artifacts:
        payload = _read_cancellation_artifact_strict(
            state, artifact, _EXTERNAL_JOB_CANCELLATION_OUTCOME_TYPE)
        if (not payload.get("cancellation_id")
                or payload.get("outcome") not in {"confirmed", "unknown", "rejected", "not_sent"}):
            raise _CancellationLedgerError(
                "cancellation outcome is missing a valid transaction state")
        if payload.get("cancellation_id") == intent["cancellation_id"]:
            outcomes.append({**payload, "outcome_artifact_id": artifact.get("id")})
    outcome = (max(outcomes, key=lambda item: str(item.get("recorded_at") or ""))
               if outcomes else None)
    return {"intent": intent, "outcome": outcome}


def _persist_cancellation_intent(
    state: State, record: dict[str, Any], *, reason: str,
    superseded_by: str | None,
) -> dict[str, Any]:
    """Durably bind cancellation intent before invoking a scheduler or Docker."""
    cancellation_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "status": "intent_persisted",
        "cancellation_id": cancellation_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "identity": _cancellation_identity(record),
        "reason": str(reason or ""),
        "superseded_by": superseded_by,
        "target_lifecycle": "superseded" if superseded_by else "cancelled",
    }
    try:
        artifact = state.save_artifact(
            _EXTERNAL_JOB_CANCELLATION_INTENT_TYPE,
            f"external_job_cancellation_intent_{state.run_id}_{cancellation_id}",
            json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={
                "cancellation_id": cancellation_id,
                **{field: payload["identity"].get(field)
                   for field in _CANCELLATION_IDENTITY_FIELDS},
            },
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": "cancellation_intent_persistence_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    payload["intent_artifact_id"] = artifact.get("id")
    try:
        state.append_transcript(
            "external_job_cancellation_intent", **payload,
        )
    except Exception:
        pass
    return payload


def _persist_cancellation_outcome(
    state: State, intent: dict[str, Any], *, outcome: str,
    cancel_result: dict[str, Any],
) -> dict[str, Any] | None:
    payload = {
        "schema_version": 1,
        "cancellation_id": intent["cancellation_id"],
        "intent_artifact_id": intent.get("intent_artifact_id"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "identity": intent["identity"],
        "cancel_result": cancel_result,
    }
    try:
        artifact = state.save_artifact(
            _EXTERNAL_JOB_CANCELLATION_OUTCOME_TYPE,
            f"external_job_cancellation_outcome_{state.run_id}_"
            f"{intent['cancellation_id']}",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            metadata={
                "cancellation_id": intent["cancellation_id"],
                "outcome": outcome,
            },
        )
    except Exception:
        log.warning("unable to persist external job cancellation outcome", exc_info=True)
        return None
    payload["outcome_artifact_id"] = artifact.get("id")
    try:
        state.append_transcript("external_job_cancellation_outcome", **payload)
    except Exception:
        pass
    return payload


_CANCELLATION_RECONCILIATION_BLOCKER_PREFIX = (
    "framework:external_job_cancellation_unknown:"
)


def _cancellation_reconciliation_reported_by(intent: dict[str, Any]) -> str:
    identity = intent.get("identity")
    identity_key = _cancellation_identity_key(
        identity if isinstance(identity, dict) else {},
    )
    transaction_key = json.dumps(
        {
            "identity": identity_key,
            "cancellation_id": str(intent.get("cancellation_id") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(transaction_key.encode("utf-8")).hexdigest()
    return f"{_CANCELLATION_RECONCILIATION_BLOCKER_PREFIX}{digest[:24]}"


def _record_cancellation_reconciliation_blocker(
    state: State, intent: dict[str, Any], outcome: dict[str, Any] | None,
) -> list[str]:
    """Block repeats using the two durable cancellation transaction facts.

    The intent proves that the irreversible call may already have happened.  A
    persisted outcome adds what was observed after that call.  No third recovery
    ledger is needed: if outcome persistence itself failed, the intent alone is
    deliberately sufficient to fail closed.
    """
    evidence = [str(value) for value in (
        (outcome or {}).get("outcome_artifact_id"),
        intent.get("intent_artifact_id"),
    ) if value]
    reported_by = _cancellation_reconciliation_reported_by(intent)
    blockers = _ensure_blocker_list_for_recording(state)
    existing = [
        item for item in blockers
        if isinstance(item, dict)
        and item.get("reported_by") == reported_by
    ]
    if not existing:
        try:
            from core.blockers import record_blocker
            record_blocker(
                state,
                category="external_job",
                summary=("external job cancellation may have been accepted but its "
                         "outcome is not durable or verifiable"),
                requested_action=("do not repeat cancellation or resubmit; reconcile the "
                                  "exact scoped scheduler identity, then repair lifecycle"),
                suggested_owner="experiment",
                retryable_after_change=True,
                reported_by=reported_by,
                evidence_paths=evidence,
            )
        except Exception:
            log.warning("unable to record cancellation reconciliation blocker", exc_info=True)
    return evidence


def _resolve_cancellation_reconciliation_blocker(
    state: State, transaction: dict[str, Any] | None,
) -> None:
    """Resolve only one exact, subsequently confirmed cancellation transaction."""
    if not isinstance(transaction, dict):
        return
    intent = transaction.get("intent")
    outcome = transaction.get("outcome")
    if (not isinstance(intent, dict) or not isinstance(outcome, dict)
            or outcome.get("outcome") != "confirmed"):
        return
    reported_by = _cancellation_reconciliation_reported_by(intent)
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list):
        return
    remaining = [
        item for item in blockers
        if not (
            isinstance(item, dict) and item.get("reported_by") == reported_by
        )
    ]
    if len(remaining) == len(blockers):
        return
    state.hook_state["blockers"] = remaining
    try:
        state.append_transcript(
            "blocker_resolved", reported_by=reported_by,
            reason="exact_cancellation_transaction_closed",
            cancellation_id=intent.get("cancellation_id"),
        )
    except Exception:
        pass


def _resolve_cancellation_blockers_after_finalize(state: State, record: dict[str, Any]) -> None:
    """作业已按实际终态收尾，「取消结果未知」这件事随之了结（verify 清单 #13）。

    取消结果未知时登记的 blocker 原先只在取消被确认时清。作业自行结束、finalize 写入 finalized
    之后，cancel_job 因 lifecycle 已是终态被拒，走不到清理那一步；core 只要 blocker 非空就把
    run 记 blocked——没有出口（L111）。这里按同一作业身份的每一次取消 intent 清掉对应 blocker。
    取消账本读不出时不清：宁可留着 blocker，也不凭猜测放掉。
    """
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        return
    expected = _cancellation_identity_key(_cancellation_identity(record))
    try:
        artifacts = state.list_artifacts(
            _EXTERNAL_JOB_CANCELLATION_INTENT_TYPE, own_only=True) or []
        intents = [
            payload for payload in (
                _read_cancellation_artifact_strict(
                    state, artifact, _EXTERNAL_JOB_CANCELLATION_INTENT_TYPE)
                for artifact in artifacts)
            if isinstance(payload.get("identity"), dict)
            and _cancellation_identity_key(payload["identity"]) == expected
        ]
    except Exception:
        return
    reported = {_cancellation_reconciliation_reported_by(intent) for intent in intents}
    resolved = sorted({
        str(item.get("reported_by")) for item in blockers
        if isinstance(item, dict) and item.get("reported_by") in reported})
    if not resolved:
        return
    state.hook_state["blockers"] = [
        item for item in blockers
        if not (isinstance(item, dict) and item.get("reported_by") in reported)]
    try:
        state.append_transcript(
            "blocker_resolved", reported_by=resolved,
            reason="external_job_finalized_after_unknown_cancellation",
            scheduler=record.get("scheduler"), job_id=record.get("job_id"),
        )
    except Exception:
        pass


def _cancel_sync(
    scheduler: str,
    job_id: str,
    namespace: str | None,
    *,
    container_runtime_id: str | None = None,
    refuse_if_ended: bool = False,
    absent_is_unknown: bool = False,
) -> dict[str, Any]:
    scheduler = scheduler.lower()
    if scheduler == "local":
        status = _local_container_status(job_id, container_runtime_id)
        if not status.get("ok"):
            return {"ok": False, "error": status["stderr"]}
        sandbox_state = status.get("sandbox_state") or {}
        if refuse_if_ended and _local_job_ended_on_its_own(sandbox_state):
            # Last read before the signal.  The job ended on its own after the
            # pre-confirmation probe: send nothing and keep record.json — it is
            # the only copy of the exit code that finalize needs.
            return {
                "ok": False, "action": "sandbox_stop_skipped",
                "reason": "external_job_already_ended", "already_ended": True,
                "sandbox_state": sandbox_state,
                "error": ("作业在取消确认之后、发信号之前已自行结束"
                          f"（status={sandbox_state.get('status')}，"
                          f"exit_code={sandbox_state.get('exit_code')}）；"
                          "未发信号，未删除作业账本。用 finalize_external_job 收尾。"),
            }
        if absent_is_unknown and not sandbox_state.get("exists"):
            # 取消事务里：作业账本里没有这条记录 ≠ 取消成功。原先返回 already_absent + ok，
            # 被记成 confirmed、lifecycle 写 cancelled，而一个信号都没发（2026-09-13 审查）。
            # 本地记录丢失（例如 jobs 根在 /tmp 被清）时进程可能仍在运行：结果未知，走取消
            # 事务的对账出口，不写 cancelled。收尾清理不传这个开关——那时作业已有终态证据，
            # 记录不在就是已经清掉了。
            return {
                "ok": False, "outcome_unknown": True,
                "action": "sandbox_stop_skipped", "reason": "local_job_record_missing",
                "sandbox_state": sandbox_state,
                "error": ("本地作业账本里找不到这个作业的记录，进程是否仍在运行无从确认；"
                          "未发信号，不记为已取消。"),
            }
        try:
            from core.sandbox import stop_container

            existed = bool(sandbox_state.get("exists"))
            if existed:
                stopped = stop_container(
                    str(job_id), remove=True,
                    expected_container_id=container_runtime_id,
                )
                if stopped is False:
                    # stop_container 在进程组没有按期退出（或记录在两次读之间没了）时返回 False。
                    # 原先不看返回值、一律 ok=True，取消事务据此写 cancelled——进程其实还在跑
                    # （合入 origin/main d852a6f5 后第三会话指出）。停没停下无从确认：结果未知，
                    # 走取消事务的对账出口，不写 cancelled。
                    return {
                        "ok": False, "outcome_unknown": True,
                        "action": "sandbox_stop", "reason": "local_job_stop_unconfirmed",
                        "container_id": str(job_id), "sandbox_state": sandbox_state,
                        "error": ("已发停止信号，但进程组没有按期退出，是否已停下无从确认；"
                                  "不记为已取消。"),
                    }
            return {"ok": True, "action": "sandbox_stop", "container_id": str(job_id),
                    "already_absent": not existed}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if scheduler == "slurm":
        result = _run(["scancel", str(job_id)], timeout=15)
    elif scheduler == "pbs":
        result = _run(["qdel", str(job_id)], timeout=15)
    elif scheduler == "kubernetes":
        cmd = ["kubectl"] + (["-n", namespace] if namespace else [])
        result = _run(cmd + ["delete", "job", str(job_id)], timeout=20)
    else:
        return {"ok": False, "error": f"unsupported scheduler: {scheduler}"}
    outcome_unknown = (
        not result.get("ok")
        and result.get("returncode") is None
        and str(result.get("stderr") or "") != "not found"
    )
    return {
        "ok": bool(result.get("ok")),
        "action": "scheduler_cancel",
        "result": result,
        "outcome_unknown": outcome_unknown,
        **({"error": "scheduler cancellation outcome is unknown"}
           if outcome_unknown else {}),
    }

def _job_status_sync(scheduler: str, job_id: str, namespace: str | None,
                     remote_host: str | None = None,
                     container_runtime_id: str | None = None) -> dict[str, Any]:
    scheduler = scheduler.lower()
    if scheduler == "slurm":
        r = _run(["squeue", "-j", job_id, "-h", "-o", "%i|%T|%M|%R"], timeout=10)
    elif scheduler == "pbs":
        r = query_pbs_job(_run, job_id)
    elif scheduler == "kubernetes":
        cmd = ["kubectl"]
        if namespace:
            cmd += ["-n", namespace]
        r = _run(cmd + ["get", "job", job_id, "-o", "json"], timeout=12)
    elif scheduler == "local":
        r = _local_container_status(job_id, container_runtime_id)
    else:
        return {"status": "error", "error": f"unsupported scheduler: {scheduler}"}
    return {
        "status": "success" if r["ok"] else "error",
        "scheduler": scheduler,
        "job_id": job_id,
        "raw": r,
    }


async def _job_status(
    state: State,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    **_: Any,
) -> dict:
    """Query a submitted job."""
    # 判决拆除·第三波（rm:2305 → schema，2026-09-02）：scheduler enum / job_id 非空由 schema 核。
    try:
        record = _external_job_record(state, scheduler, job_id, namespace)
        if record is None:
            return {"status": "error",
                    "error": ("未找到唯一受管 external job 记录；同 scheduler/job_id "
                              "跨 scope 时必须提供 namespace")}
        namespace = namespace or record.get("namespace")
        return _job_status_sync(
            scheduler, job_id, namespace, remote_host=record.get("launch_host"),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _scheduler_phase(scheduler: str, result: dict[str, Any]) -> str:
    """Map scheduler query output to running, terminal, or unknown.

    Terminal means only that the scheduler no longer owns this job. It never
    proves scientific success; the experiment must inspect outputs afterwards.
    """
    raw = result.get("raw") or {}
    if not isinstance(raw, dict):
        return "unknown"
    scheduler = str(scheduler or "").lower()
    stdout = str(raw.get("stdout") or "")
    if scheduler == "local":
        return "running" if stdout.strip() == "RUNNING" else ("terminal" if stdout.strip() == "NOT_RUNNING" else "unknown")
    if scheduler == "slurm":
        return "running" if raw.get("ok") and stdout.strip() else ("terminal" if raw.get("ok") else "unknown")
    if scheduler == "pbs":
        if not raw.get("ok"):
            return "unknown"
        return "terminal" if re.search(r"job_state\s*=\s*[CEF]\b", stdout) else "running"
    if scheduler == "kubernetes":
        if not raw.get("ok"):
            return "unknown"
        try:
            status = json.loads(stdout).get("status") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return "unknown"
        return "running" if status.get("active") else ("terminal" if status.get("succeeded") or status.get("failed") else "unknown")
    return "unknown"


def _external_job_record(
    state: Any, scheduler: str, job_id: str,
    namespace: str | None = None, launch_host: str | None = None,
    submission_nonce: str | None = None,
    process_group_id: str | None = None,
    process_start_ticks: int | str | None = None,
    container_runtime_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one managed job without guessing across scheduler scopes."""
    scheduler_text, job_id_text = str(scheduler or ""), str(job_id or "")
    candidates = [
        row for row in _submission_payloads(state) + _task_external_jobs(state)
        if str(row.get("scheduler") or "").casefold() == scheduler_text.casefold()
        and str(row.get("job_id") or "") == job_id_text
    ]
    if namespace is not None:
        candidates = [row for row in candidates
                      if str(row.get("namespace") or "") == str(namespace)]
    if launch_host is not None:
        candidates = [row for row in candidates
                      if str(row.get("launch_host") or "").casefold()
                      == str(launch_host).casefold()]
    if submission_nonce is not None:
        candidates = [row for row in candidates
                      if str(row.get("submission_nonce") or "")
                      == str(submission_nonce)]
    if process_group_id is not None:
        candidates = [row for row in candidates
                      if str(row.get("process_group_id") or "")
                      == str(process_group_id)]
    if process_start_ticks is not None:
        candidates = [row for row in candidates
                      if str(row.get("process_start_ticks") or "")
                      == str(process_start_ticks)]
    if container_runtime_id is not None:
        candidates = [row for row in candidates
                      if str(row.get("container_runtime_id") or "")
                      == str(container_runtime_id)]
    identities = {_job_key_for_record(row) for row in candidates}
    return candidates[0] if len(identities) == 1 else None


def _authoritative_record_for_read(
    state: Any, scheduler: str, job_id: str, namespace: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve one scope-exact durable receipt for a read-only query."""
    record = _external_job_record(state, scheduler, job_id, namespace)
    return record, None


def _health_file_snapshot(path: str) -> dict[str, Any]:
    """一条 completion path 的快照（035a：类型、inode、ctime、目录条目——共享 evaluator 的输入）。"""
    return _output_postconditions.snapshot_row(path)


_TERMINAL_OUTPUT_OBSERVATION_EVENT = "external_job_terminal_output_observation"


def freeze_first_terminal_output_observation(
    state: Any, record: dict[str, Any], health: dict[str, Any],
) -> dict[str, Any]:
    """health 通道的首次终态观测冻结（035a，SCOPE G3）。

    finalize 因「completion path 不存在」被拒后，只要事后补一个文件再 finalize 就能过：
    health 没有终态上界，每次都重新读盘。这里在**第一次**看到 terminal 时把 completion
    path 快照（补上正文摘要）按完整 job identity 写成 append-only 事件；之后对同一作业
    的每次终态观测都改读这份冻结快照，不再读盘。持久化失败不冻结也不放行：标 unfrozen，
    消费方按不可判定处理。
    """
    if health.get("status") != "success" or health.get("scheduler_phase") != "terminal":
        return health
    job_key = _job_key_for_record(record)
    try:
        try:
            from .execution_route import _read_transcript_events
        except ImportError:
            from tools.execution_route import _read_transcript_events
        events, _warnings = _read_transcript_events(state)
    except Exception:
        events = []
    frozen = [
        event for event in events
        if event.get("event") == _TERMINAL_OUTPUT_OBSERVATION_EVENT
        and str(event.get("job_key") or "") == job_key
    ]
    if frozen:
        first = frozen[0]
        return {
            **health,
            "completion_paths": [dict(row) for row in (first.get("completion_paths") or [])
                                 if isinstance(row, dict)],
            "terminal_output_observation": {
                "source": "frozen_first_terminal",
                "recorded_at": first.get("at"),
                "not_after_ns": first.get("not_after_ns"),
                "not_after_source": first.get("not_after_source"),
            },
        }
    rows: list[dict[str, Any]] = []
    for snapshot in health.get("completion_paths") or []:
        if not isinstance(snapshot, dict):
            continue
        row = dict(snapshot)
        if row.get("exists") and row.get("kind") == "file":
            identity = _output_postconditions.output_identity(str(row.get("path")))
            row["sha256"] = identity.get("sha256")
            row["sha256_skipped"] = identity.get("sha256_skipped")
        rows.append(row)
    raw = ((health.get("scheduler_result") or {}).get("raw") or {}) if isinstance(
        health.get("scheduler_result"), dict) else {}
    sandbox = raw.get("sandbox_state") if isinstance(raw, dict) else None
    not_after_ns: int | None = None
    not_after_source = "first_terminal_observation"
    for key in ("finished_at", "ended_at"):
        value = (sandbox or {}).get(key) if isinstance(sandbox, dict) else None
        if value:
            try:
                not_after_ns = int(datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")).timestamp() * 1_000_000_000)
                not_after_source = f"sandbox_state.{key}"
                break
            except ValueError:
                continue
    if not_after_ns is None:
        not_after_ns = time.time_ns()
    payload = {
        "job_key": job_key,
        "scheduler": record.get("scheduler"),
        "job_id": record.get("job_id"),
        "submission_nonce": record.get("submission_nonce"),
        "completion_paths": rows,
        "not_after_ns": not_after_ns,
        "not_after_source": not_after_source,
    }
    try:
        state.append_transcript(_TERMINAL_OUTPUT_OBSERVATION_EVENT, **payload)
    except Exception as exc:
        return {
            **health, "completion_paths": rows,
            "terminal_output_observation": {"source": "unfrozen", "error_type": type(exc).__name__},
        }
    return {
        **health, "completion_paths": rows,
        "terminal_output_observation": {
            "source": not_after_source, "not_after_ns": not_after_ns,
            "not_after_source": not_after_source,
        },
    }


#: health 里最多带几条配置修法（每个日志文件按严重度排，一条规则一次）。
_HEALTH_DIAGNOSIS_CAP = 6


def _configured_guidance(text: str, *, source: str) -> list[dict[str, Any]]:
    try:
        from .diagnose import configured_guidance
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.diagnose import configured_guidance
    return configured_guidance(text, source=source)


def _tail_for_health(path: str, limit: int = 8192) -> str:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit), os.SEEK_SET)
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


_MAX_RESOURCE_STATUS_BYTES = 1024 * 1024


def _resource_guard_health_snapshot(
    record: dict[str, Any],
    allowed_roots: list[str],
    scheduler_phase: str,
) -> dict[str, Any]:
    """只读受管本地监督器状态；缺失或损坏不能伪装成 healthy。"""
    raw_path = record.get("resource_guard_status_path")
    if not isinstance(raw_path, str) or not raw_path:
        return {
            "resource_health": "not_applicable",
            "decision": "continue",
            "decision_reasons": ["resource_guard_status_not_declared"],
            "active_warnings": [],
            "status_snapshot": None,
        }
    real = os.path.realpath(raw_path)
    if not _path_is_within(real, allowed_roots):
        return {
            "resource_health": "unknown",
            "decision": "continue_with_fast_sampling",
            "decision_reasons": ["resource_guard_status_outside_declared_outputs"],
            "active_warnings": ["resource_guard_status_unavailable"],
            "status_snapshot": {
                "path": real,
                "exists": False,
                "blocked": "outside_declared_outputs",
            },
        }
    snapshot = _health_file_snapshot(real)
    if not snapshot.get("exists"):
        return {
            "resource_health": (
                "unknown" if scheduler_phase in {"running", "unknown"}
                else "not_applicable"
            ),
            "decision": (
                "continue_with_fast_sampling"
                if scheduler_phase in {"running", "unknown"}
                else "continue"
            ),
            "decision_reasons": ["resource_guard_status_not_yet_available"],
            "active_warnings": (
                ["resource_guard_status_unavailable"]
                if scheduler_phase in {"running", "unknown"}
                else []
            ),
            "status_snapshot": snapshot,
        }
    size = snapshot.get("size_bytes")
    if not isinstance(size, int) or size < 0 or size > _MAX_RESOURCE_STATUS_BYTES:
        return {
            "resource_health": "unknown",
            "decision": "continue_with_fast_sampling",
            "decision_reasons": ["resource_guard_status_size_invalid"],
            "active_warnings": ["resource_guard_status_unavailable"],
            "status_snapshot": snapshot,
        }
    try:
        payload = json.loads(Path(real).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "resource_health": "unknown",
            "decision": "continue_with_fast_sampling",
            "decision_reasons": ["resource_guard_status_invalid"],
            "active_warnings": ["resource_guard_status_unavailable"],
            "status_snapshot": snapshot,
        }
    if not isinstance(payload, dict):
        return {
            "resource_health": "unknown",
            "decision": "continue_with_fast_sampling",
            "decision_reasons": ["resource_guard_status_invalid"],
            "active_warnings": ["resource_guard_status_unavailable"],
            "status_snapshot": snapshot,
        }
    health = str(payload.get("resource_health") or "unknown")
    if health not in {"healthy", "pressure", "critical", "exhausted", "unknown"}:
        health = "unknown"
    decision = str(payload.get("decision") or "")
    if decision not in {
        "continue", "continue_with_fast_sampling", "emergency_stop",
    }:
        decision = (
            "continue"
            if health == "healthy"
            else "emergency_stop"
            if health == "exhausted"
            else "continue_with_fast_sampling"
        )
    reasons = payload.get("decision_reasons")
    if not isinstance(reasons, list):
        reasons = []
    warnings = payload.get("active_warnings")
    if not isinstance(warnings, list):
        warnings = []
    return {
        "resource_health": health,
        "decision": decision,
        "decision_reasons": [str(item) for item in reasons],
        "active_warnings": [str(item) for item in warnings],
        "status_snapshot": snapshot,
        "supervisor_status": payload.get("status"),
        "supervisor_reason": payload.get("reason"),
        "failure_class": payload.get("failure_class"),
        "behavior_evidence": (
            payload.get("behavior_evidence")
            if isinstance(payload.get("behavior_evidence"), dict)
            else {}
        ),
    }


def _joint_job_resource_decision(
    health_state: str,
    resource: dict[str, Any],
) -> tuple[str, list[str]]:
    """联合解释作业进度与资源事实；这里只给建议，绝不执行取消。"""
    resource_health = str(resource.get("resource_health") or "unknown")
    reasons = [f"job:{health_state}", f"resource:{resource_health}"]
    if resource_health == "exhausted":
        return "diagnose_resource_exhaustion", reasons
    if health_state in {"failure_signal", "terminal_needs_analysis", "unknown"}:
        return "diagnose", reasons
    if health_state == "stalled":
        if resource_health in {"pressure", "critical"}:
            return "managed_cancel_recommended", reasons
        return "diagnose", reasons
    if health_state == "running_without_progress_evidence":
        if resource_health in {"pressure", "critical", "unknown"}:
            return "diagnose", reasons
        return "continue", reasons
    if resource_health in {"pressure", "critical", "unknown"}:
        return "continue_with_fast_sampling", reasons
    return "continue", reasons


def probe_external_job_health(state: Any, scheduler: str, job_id: str,
                              namespace: str | None = None) -> dict[str, Any]:
    """Read one managed job health snapshot without executing model-supplied shell."""
    record, identity_error = _authoritative_record_for_read(
        state, scheduler, job_id, namespace)
    if identity_error is not None:
        return identity_error
    if record is None:
        return {"status": "error",
                "error": ("未找到受管 job_submission/handoff 记录，或同 scheduler/job_id "
                          "存在多个 scope；请提供 namespace 后重试"),
                "scheduler": scheduler, "job_id": job_id, "namespace": namespace}
    namespace = namespace or record.get("namespace")
    try:
        scheduler_result = _job_status_sync(
            scheduler, job_id, namespace, remote_host=record.get("launch_host"),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception as exc:
        scheduler_result = {"status": "error", "raw": {"ok": False, "stderr": f"{type(exc).__name__}: {exc}"}}
    phase = _scheduler_phase(scheduler, scheduler_result)
    terminal_evidence = None
    if phase == "terminal" and str(scheduler or "").lower() == "slurm":
        terminal_evidence = _remote_job_ended_on_its_own(
            "slurm", scheduler_result.get("raw") or {}, str(job_id),
        )
    contract = record.get("health_contract") if isinstance(record.get("health_contract"), dict) else {}
    paths = list(contract.get("progress_paths") or [])
    log_paths = [record.get("stdout_path"), record.get("stderr_path")]
    # 脚本内 `exec` 重定向**之前**那段（mkdir / stage-in / identity preflight）
    # 的落点。作业死在 bootstrap 时 payload 日志根本不会出现，错误只在这里。
    bootstrap_paths = [p for p in (record.get("bootstrap_stdout_path"),
                                   record.get("bootstrap_stderr_path"))
                       if isinstance(p, str) and p]
    allowed = list(record.get("output_roots") or [])
    for key in ("scheduler_output_dir", "bootstrap_log_dir"):
        if record.get(key):
            allowed.append(os.path.realpath(str(record[key])))
    observed: list[dict[str, Any]] = []
    now = time.time()
    progress_newest = None
    activity_newest = None
    declared_progress_paths = {
        os.path.realpath(path)
        for path in paths
        if isinstance(path, str) and path
    }
    candidate_paths = list(dict.fromkeys(
        [p for p in paths + log_paths if isinstance(p, str) and p]
    ))
    for path in candidate_paths:
        real = os.path.realpath(path)
        evidence_kind = (
            "declared_progress"
            if real in declared_progress_paths
            else "output_activity"
        )
        if not _path_is_within(real, allowed):
            observed.append({
                "path": real,
                "exists": False,
                "blocked": "outside_declared_outputs",
                "evidence_kind": evidence_kind,
            })
            continue
        item = {**_health_file_snapshot(real), "evidence_kind": evidence_kind}
        observed.append(item)
        if item.get("exists") and isinstance(item.get("mtime_epoch_s"), (int, float)):
            mtime = float(item["mtime_epoch_s"])
            activity_newest = max(mtime, activity_newest or 0.0)
            if evidence_kind == "declared_progress":
                progress_newest = max(mtime, progress_newest or 0.0)
    # bootstrap 日志只进 observed（诊断可见），**不进 activity_newest**：它在作业启动时
    # 写一次就不再更新。喂进进度判定的后果正好相反 —— 一个死在 bootstrap 的作业
    # 会因此显示成"有过进度"，把 running_without_progress_evidence 翻成 healthy，
    # 于是真正的失败要等满 stall_after_s（默认 900 秒）才暴露。
    for path in bootstrap_paths:
        real = os.path.realpath(path)
        if not _path_is_within(real, allowed):
            continue
        observed.append({**_health_file_snapshot(real), "role": "bootstrap"})
    errors: list[dict[str, str]] = []
    remediation: dict[str, Any] | None = None
    # 049-3：配置规则（diagnose_patterns.yaml）的修法投影，纯信息——
    # error_evidence / decision / 任何门都不读它；它只让模型在同一份 health 里
    # 看到"这行日志对应哪条修法"。
    diagnosis: list[dict[str, Any]] = []
    for path in log_paths + bootstrap_paths:
        if not isinstance(path, str) or not path:
            continue
        real = os.path.realpath(path)
        if not _path_is_within(real, allowed):
            continue
        text = _tail_for_health(real)
        for marker in _health_error_markers(contract):
            if marker and str(marker).casefold() in text.casefold():
                errors.append({"path": real, "marker": str(marker)})
        if len(diagnosis) < _HEALTH_DIAGNOSIS_CAP:
            try:
                for item in _configured_guidance(text, source=real):
                    diagnosis.append({"path": real, **item})
            except Exception as exc:  # 诊断永远不能让 health 失败
                log.warning("health diagnosis 跳过 %s: %s", real, exc)
        if remediation is None:
            candidate = mpi_runtime_remediation(str(record.get("command") or ""), text)
            if candidate is not None:
                remediation = candidate
                errors.append({"path": real, "marker": candidate["kind"]})
    stall_after_s = int(contract.get("stall_after_s") or 900)
    progress_age_s = (
        None if progress_newest is None else max(0.0, now - progress_newest)
    )
    activity_age_s = (
        None if activity_newest is None else max(0.0, now - activity_newest)
    )
    expected = record.get("expected_duration_s")
    try:
        expected_s = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected_s = None
    elapsed_s = None
    try:
        submitted = datetime.fromisoformat(str(record.get("submitted_at") or "").replace("Z", "+00:00"))
        elapsed_s = max(0.0, (datetime.now(timezone.utc) - submitted).total_seconds())
    except ValueError:
        pass
    if errors:
        health_state = "failure_signal"
    elif phase == "terminal":
        health_state = "terminal_needs_analysis"
    elif phase == "unknown":
        health_state = "unknown"
    elif progress_newest is not None:
        if progress_age_s is not None and progress_age_s > stall_after_s:
            health_state = "stalled"
        elif expected_s and elapsed_s is not None and elapsed_s > expected_s:
            health_state = "overdue_but_progressing"
        else:
            health_state = "healthy"
    elif activity_newest is not None:
        if expected_s and elapsed_s is not None and elapsed_s > expected_s:
            health_state = "overdue_with_output_activity"
        else:
            health_state = "running_with_output_activity"
    else:
        health_state = "running_without_progress_evidence"
    resource_health = _resource_guard_health_snapshot(
        record, allowed, phase,
    )
    decision, decision_reasons = _joint_job_resource_decision(
        health_state, resource_health,
    )
    decision_reasons.extend(
        str(item)
        for item in resource_health.get("decision_reasons") or []
        if str(item) not in decision_reasons
    )
    workflow_status = (
        "awaiting_analysis"
        if phase not in {"running", "unknown"}
        or resource_health.get("resource_health") == "exhausted"
        else "awaiting_external_job"
    )
    return {
        "status": "success", "scheduler": str(scheduler).lower(), "job_id": str(job_id),
        "scheduler_phase": phase,
        "workflow_status": workflow_status,
        "health_state": health_state,
        "resource_health": resource_health.get("resource_health"),
        "decision": decision,
        "decision_reasons": decision_reasons,
        "resource_health_evidence": resource_health,
        "progress_paths": observed,
        "completion_paths": [
            _health_file_snapshot(os.path.realpath(path))
            if _path_is_within(os.path.realpath(path), allowed)
            else {"path": os.path.realpath(path), "exists": False, "blocked": "outside_declared_outputs"}
            for path in contract.get("completion_paths") or []
        ],
        "error_evidence": errors, "mpi_runtime_remediation": remediation,
        "diagnosis": diagnosis[:_HEALTH_DIAGNOSIS_CAP],
        "progress_age_s": progress_age_s,
        "activity_age_s": activity_age_s,
        "health_evidence_level": (
            "declared_progress" if progress_newest is not None
            else "output_activity" if activity_newest is not None
            else "none"
        ),
        "stall_after_s": stall_after_s, "elapsed_s": elapsed_s,
        "expected_duration_s": expected_s, "scheduler_result": scheduler_result,
        "terminal_evidence": terminal_evidence,
    }


async def _check_external_job_health(state: State, scheduler: str, job_id: str,
                                     namespace: str | None = None, **_: Any) -> dict:
    return probe_external_job_health(state, scheduler, job_id, namespace)


# 收敛任务书 K13（缺陷 #13）：短作业常在几秒内结束，第二次探测却要等 min(interval, max_wait_s)
# ——默认 180 秒。调用方没指定 poll_interval_s 时（submit_job 前台等待就是这样），前几次按短退避
# 探测，每次不超过声明的间隔，用完之后回到声明的间隔。
_EXTERNAL_WAIT_BACKOFF_S = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)


async def _wait_for_external_job(
    state: State, scheduler: str, job_id: str, namespace: str | None = None,
    max_wait_s: int = 300,
    poll_interval_s: int | None = None, **_: Any,
) -> dict:
    """Wait inside a live experiment session without spending LLM turns per poll.

    The wait is bounded and cancellation-aware. An active elapsed wait creates
    a node-local handoff permit, so the next model stop can persist the durable
    external-job workflow rather than being rewritten into another identical
    wait forever.
    """
    _raise_if_external_wait_cancelled(state)
    initial = await _probe_external_job_health_cancellable(
        state, scheduler, job_id, namespace)
    if initial.get("status") != "success":
        return initial
    if initial.get("workflow_status") != "awaiting_external_job":
        return {
            "status": "success",
            "wait_outcome": (
                "scheduler_terminal"
                if initial.get("scheduler_phase") == "terminal"
                else "needs_diagnosis"
            ),
            "health": initial,
        }
    if (
        initial.get("health_state") in {"stalled", "failure_signal", "unknown"}
        or initial.get("decision") in {
            "diagnose", "diagnose_resource_exhaustion",
            "managed_cancel_recommended",
        }
    ):
        return {"status": "success", "wait_outcome": "needs_diagnosis", "health": initial}
    record = _external_job_record(state, scheduler, job_id, namespace) or {}
    configured = (record.get("health_contract") or {}).get("poll_interval_s", 180)
    interval = int(poll_interval_s if poll_interval_s is not None else configured)
    backoff = list(_EXTERNAL_WAIT_BACKOFF_S) if poll_interval_s is None else []

    def next_gap() -> float:
        return min(float(interval), backoff.pop(0)) if backoff else float(interval)

    deadline = time.monotonic() + max_wait_s
    next_probe = time.monotonic() + min(next_gap(), max_wait_s)
    health = initial
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        until_probe = max(0.0, next_probe - time.monotonic())
        await _sleep_with_external_wait_cancellation(
            state,
            min(60.0, remaining, until_probe),
        )
        _raise_if_external_wait_cancelled(state)
        if time.monotonic() < next_probe and time.monotonic() < deadline:
            continue
        health = await _probe_external_job_health_cancellable(
            state, scheduler, job_id, namespace)
        try:
            state.append_transcript("external_job_wait_tick", scheduler=scheduler, job_id=job_id,
                                    health_state=health.get("health_state"),
                                    resource_health=health.get("resource_health"),
                                    decision=health.get("decision"),
                                    workflow_status=health.get("workflow_status"))
        except Exception:
            pass
        if health.get("status") != "success":
            return health
        if health.get("workflow_status") != "awaiting_external_job":
            return {
                "status": "success",
                "wait_outcome": (
                    "scheduler_terminal"
                    if health.get("scheduler_phase") == "terminal"
                    else "needs_diagnosis"
                ),
                "health": health,
            }
        if (
            health.get("health_state") in {"stalled", "failure_signal", "unknown"}
            or health.get("decision") in {
                "diagnose", "diagnose_resource_exhaustion",
                "managed_cancel_recommended",
            }
        ):
            return {"status": "success", "wait_outcome": "needs_diagnosis", "health": health}
        next_probe = time.monotonic() + next_gap()
    handoff_eligible = _mark_external_job_handoff_ready(
        state, scheduler, job_id, namespace, record.get("launch_host"),
        record.get("scheduler_cluster"), record.get("resource_uid"),
        record.get("submission_nonce"), record.get("container_runtime_id"))
    return {
        "status": "success",
        "wait_outcome": "wait_elapsed_running",
        "health": health,
        "handoff_eligible": handoff_eligible,
        "note": (
            (
                "仍在运行；健康证据等级见 health_state/health_evidence_level。"
                "已完成一次受管等待。若本次会话无需继续监控，"
                "现在可以安全结束，由 external-job handoff 持久化后续任务。"
            )
            if handoff_eligible
            else (
                "作业仍在运行，但提交 nonce/不可变容器身份不完整；"
                "本次等待未授予持久 handoff，必须继续受管恢复。"
            )
        ),
    }


def _external_workflow_rows(
    state: Any,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project scheduler state for an explicitly selected obligation set."""
    lifecycle = _job_lifecycle_states(state)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        scheduler, job_id = str(row.get("scheduler") or ""), str(row.get("job_id") or "")
        key = _job_key_for_record(row)
        if not scheduler or not job_id or key in seen:
            continue
        seen.add(key)
        if "lifecycle_status" in row:
            # The current-run shared judgement already resolved this receipt.
            lifecycle_resolution = {
                "status": row.get("lifecycle_status"),
                "resolution": row.get("lifecycle_resolution"),
            }
        else:
            lifecycle_resolution = lifecycle_for_submission(
                state, row, lifecycle_states=lifecycle)
            status = lifecycle_resolution.get("status")
            if status is not None and status not in _ACTIVE_JOB_STATES:
                continue
        health = probe_external_job_health(state, scheduler, job_id, row.get("namespace"))
        rows.append({"scheduler": scheduler, "job_id": job_id,
                     "namespace": row.get("namespace"),
                     "launch_host": row.get("launch_host"),
                     "scheduler_cluster": row.get("scheduler_cluster"),
                     "resource_uid": row.get("resource_uid"),
                     "submission_nonce": row.get("submission_nonce"),
                     "container_runtime_id": row.get("container_runtime_id"),
                     "task_id": row.get("task_id"), "workdir": row.get("workdir"),
                     "output_roots": row.get("output_roots") or [],
                     "health": health,
                     "lifecycle_resolution": lifecycle_resolution.get("resolution"),
                     "workflow_status": ("legacy_lifecycle_scope_ambiguous"
                                         if lifecycle_resolution.get("resolution")
                                         == "legacy_lifecycle_scope_ambiguous"
                                         else health.get("workflow_status", "awaiting_external_job"))})
    return rows


def current_run_owed_external_workflows(state: Any) -> list[dict[str, Any]]:
    """Preview the strict current-run closure judgement plus durable task debts."""
    candidates = owed_external_job_closure_records(state)
    return _external_workflow_rows(state, candidates)


def unresolved_external_workflows(state: Any) -> list[dict[str, Any]]:
    """Compatibility view used to discover/adopt cross-run continuation debts."""
    candidates = _submission_payloads(state) + _task_external_jobs(state)
    return _external_workflow_rows(state, candidates)

def _complete_handoff_tasks(
    state: State, scheduler: str, job_id: str, note: str,
    *, namespace: str | None = None, launch_host: str | None = None,
    scheduler_cluster: str | None = None, resource_uid: str | None = None,
    submission_nonce: str | None = None, process_group_id: str | None = None,
    process_start_ticks: int | str | None = None,
    container_runtime_id: str | None = None,
) -> dict[str, Any]:
    project_root = getattr(state, "project_root", None)
    if not project_root:
        return {"status": "success", "completed_task_ids": []}
    target = _job_key(scheduler, job_id, namespace, launch_host,
                      scheduler_cluster, resource_uid, submission_nonce,
                      process_group_id, process_start_ticks,
                      container_runtime_id)
    completed: list[str] = []
    try:
        from core.tasks import TaskList
        tasks = TaskList(Path(project_root) / "tasks")
        for task in tasks.list_all():
            if task.status == "completed":
                continue
            fields = {}
            for line in task.description.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    fields[key.strip()] = value.strip()
            if _job_key(
                fields.get("scheduler", ""), fields.get("job_id", ""),
                fields.get("namespace") or None, fields.get("launch_host") or None,
                fields.get("scheduler_cluster") or None, fields.get("resource_uid") or None,
                fields.get("submission_nonce") or None,
                fields.get("process_group_id") or None,
                fields.get("process_start_ticks") or None,
                fields.get("container_runtime_id") or None,
            ) == target:
                tasks.complete(task.id, notes=note)
                completed.append(str(task.id))
    except Exception as exc:
        return {
            "status": "error",
            "reason": "handoff_task_completion_failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
            "completed_task_ids": completed,
        }
    return {"status": "success", "completed_task_ids": completed}


def _frozen_experiment_log(state: Any, artifact_id: str) -> dict[str, Any] | None:
    try:
        record = state.read_artifact(artifact_id)
    except Exception:
        return None
    if not isinstance(record, dict) or record.get("type") != "experiment_log":
        return None
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata.get("frozen") or metadata.get("auto_generated"):
        return None
    return record


def _external_job_evidence_identity_value(field: str, value: Any) -> str:
    """Normalize one evidence-identity field exactly as ``_job_key`` does."""
    rendered = str(value or "")
    if field in {"scheduler", "launch_host"}:
        return rendered.casefold()
    return rendered


def _external_job_evidence_identity_diagnostic(
    evidence: dict[str, Any], record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return recovery data when frozen evidence lacks the exact job identity.

    Exact matching intentionally includes nonce/runtime fields: a local Docker
    container name can be reused, so scheduler/job_id alone is not proof that
    the analyzed output belongs to this managed submission. The diagnostic
    exposes the already-authoritative record instead of making the agent guess
    which identity field was omitted.
    """
    metadata = evidence.get("metadata") or {}
    refs = metadata.get("external_job_refs") if isinstance(metadata, dict) else None
    expected_ref = {
        field: record.get(field)
        for field in _CANCELLATION_IDENTITY_FIELDS
    }
    expected = _job_key_for_record(record)
    candidates = (
        [ref for ref in refs if isinstance(ref, dict)]
        if isinstance(refs, list)
        else []
    )
    for ref in candidates:
        if _job_key_for_record(ref) == expected:
            return None

    def _match_score(ref: dict[str, Any]) -> int:
        return sum(
            _external_job_evidence_identity_value(field, ref.get(field))
            == _external_job_evidence_identity_value(field, expected_ref.get(field))
            for field in _CANCELLATION_IDENTITY_FIELDS
        )

    closest = max(candidates, key=_match_score, default={})
    missing_fields: list[str] = []
    mismatched_fields: list[str] = []
    for field in _CANCELLATION_IDENTITY_FIELDS:
        expected_value = _external_job_evidence_identity_value(
            field, expected_ref.get(field))
        observed_value = _external_job_evidence_identity_value(
            field, closest.get(field))
        if observed_value == expected_value:
            continue
        if expected_value and not observed_value:
            missing_fields.append(field)
        else:
            mismatched_fields.append(field)
    return {
        "required_identity_fields": list(_CANCELLATION_IDENTITY_FIELDS),
        "required_external_job_ref": expected_ref,
        "missing_identity_fields": missing_fields,
        "mismatched_identity_fields": mismatched_fields,
        "recovery": (
            "Read the matching job_submission or external_job_workflow, copy "
            "required_external_job_ref into a new or amended experiment_log "
            "metadata.external_job_refs, freeze that evidence version, then retry "
            "finalize_external_job. Do not guess identity values from sandbox state "
            "or a container hostname."
        ),
    }


_EXTERNAL_JOB_HANDOFF_BLOCKER_PREFIX = (
    "framework:experiment_external_job_handoff:"
)
_LEGACY_EXTERNAL_JOB_HANDOFF_BLOCKER = "framework:experiment_external_job_handoff"
_FINALIZED_NEEDS_ROUTE_PREFIX = "framework:finalized_needs_route_reconciliation:"
_BLOCKER_LEDGER_RECOVERY_MARKER = "framework:experiment_blocker_ledger_recovered"
_FINALIZED_NEEDS_CLEANUP_PREFIX = "framework:finalized_needs_cleanup:"
_EXTERNAL_JOB_NEEDS_TASK_PREFIX = (
    "framework:external_job_needs_task_reconciliation:"
)
_EXTERNAL_JOB_CLASS_DISPUTED_PREFIX = (
    "framework:external_job_class_disputed:"
)
#: 收据冻结的类别与本次重算不一致时挂的分歧 blocker。与上面那条同族：
#: 没有任何 resolve/reconcile 路径会删它，必须由人或上游节点显式处置。
_EXTERNAL_JOB_CLASS_DIVERGENCE_PREFIX = (
    "framework:experiment:external_job_class_divergence:"
)
_CLOSABLE_ROUTE_PROJECTION_STATUSES = frozenset({"success", "not_applicable"})


def _external_job_handoff_reported_by(record: dict[str, Any]) -> str:
    digest = hashlib.sha256(_job_key_for_record(record).encode("utf-8")).hexdigest()
    return f"{_EXTERNAL_JOB_HANDOFF_BLOCKER_PREFIX}{digest[:24]}"


def _finalized_needs_route_reported_by(record: dict[str, Any]) -> str:
    digest = hashlib.sha256(_job_key_for_record(record).encode("utf-8")).hexdigest()
    return f"{_FINALIZED_NEEDS_ROUTE_PREFIX}{digest[:24]}"


def _external_job_class_disputed_reported_by(record: dict[str, Any]) -> str:
    digest = hashlib.sha256(_job_key_for_record(record).encode("utf-8")).hexdigest()
    return f"{_EXTERNAL_JOB_CLASS_DISPUTED_PREFIX}{digest[:24]}"


def _independent_blocker_recorded(blockers: list[Any]) -> bool:
    """是否存在一条**不由框架代记**的 blocker。

    框架为 external job 对账代记的 blocker（handoff / needs_route / needs_task /
    needs_cleanup / cancellation 各前缀）都会在同一次收尾末尾被判为 stale 清掉，
    拿它们当"已登记 blocker"是自消耗的补偿控制：解锁完就消失，run 最终不带任何
    blocker。凡是要用"有人已经把阻塞事实记进账本"来解锁让步路的地方，都必须看
    非框架来源的那一条（模型经 report_blocker 登记时不写 reported_by）。
    """
    return any(
        isinstance(item, dict)
        and not str(item.get("reported_by") or "").startswith("framework:")
        for item in blockers
    )


def _canonical_operation_blocker_entries(
    blockers: list[Any],
) -> list[dict[str, Any]]:
    """Freeze the non-framework blocker dicts accepted by the existing gate."""
    entries: list[dict[str, Any]] = []
    for item in blockers:
        if not isinstance(item, dict):
            continue
        if str(item.get("reported_by") or "").startswith("framework:"):
            continue
        # Keep the old gate predicate exactly: every non-framework dict counts.
        # Production report_blocker entries are structured, but tightening that
        # predicate here would create a second, hidden rejection after the gate.
        canonical_entry = json.loads(json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ))
        entries.append(canonical_entry)
    entries.sort(key=lambda item: json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ))
    return entries


def _operation_blocker_witness(blockers: list[Any]) -> dict[str, Any] | None:
    entries = _canonical_operation_blocker_entries(blockers)
    if not entries:
        return None
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "witness_type": _OPERATION_BLOCKER_WITNESS_KIND,
        "count": len(entries),
        "entries": entries,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _operation_blocker_witness_valid(witness: Any) -> bool:
    """Validate a v1 blocked receipt without consulting mutable live blockers."""
    if not isinstance(witness, dict):
        return False
    count = witness.get("count")
    entries = witness.get("entries")
    digest = witness.get("sha256")
    if (
        witness.get("witness_type") != _OPERATION_BLOCKER_WITNESS_KIND
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        or not isinstance(entries, list)
        or len(entries) != count
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return False
    canonical_entries = _canonical_operation_blocker_entries(entries)
    if canonical_entries != entries:
        return False
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest == expected


def _ensure_blocker_list_for_recording(state: Any) -> list[dict[str, Any]]:
    """Recover malformed blocker containers without allowing false completion."""
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        raise RuntimeError("experiment hook_state is unavailable for durable blockers")
    current = hook_state.get("blockers")
    if isinstance(current, list):
        return current
    recovered = []
    if isinstance(current, dict):
        recovered.append(current)
    elif isinstance(current, tuple):
        recovered.extend(item for item in current if isinstance(item, dict))
    hook_state["blockers"] = recovered
    if current is not None and not any(
        isinstance(item, dict)
        and item.get("reported_by") == _BLOCKER_LEDGER_RECOVERY_MARKER
        for item in recovered
    ):
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="other",
            summary="experiment blocker ledger had a non-list container",
            requested_action=(
                "inspect the recovered blocker entries before clearing this blocker"
            ),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=_BLOCKER_LEDGER_RECOVERY_MARKER,
        )
        blocker.update({
            "reason": "malformed_blocker_ledger_recovered",
            "original_type": type(current).__name__,
        })
    return hook_state["blockers"]


def _record_finalized_needs_route_blocker(
    state: State,
    record: dict[str, Any],
    route_projection: dict[str, Any],
    *,
    outcome: str,
    evidence_artifact_id: str,
) -> dict[str, Any]:
    """Persist one retryable route-projection blocker for an exact job identity."""
    reported_by = _finalized_needs_route_reported_by(record)
    blockers = _ensure_blocker_list_for_recording(state)
    blocker = next(
        (
            item for item in blockers
            if isinstance(item, dict) and item.get("reported_by") == reported_by
        ),
        None,
    )
    if blocker is None:
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="external_job",
            summary=(
                "external job is terminal but its execution route projection failed"
            ),
            requested_action=(
                "repair route projection and retry finalize_external_job without "
                "resubmitting the external job"
            ),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=reported_by,
        )
    blocker.update({
        "reason": "finalized_needs_route_reconciliation",
        "scheduler": str(record.get("scheduler") or "").lower(),
        "job_id": str(record.get("job_id") or ""),
        "namespace": record.get("namespace"),
        "submission_nonce": record.get("submission_nonce"),
        "container_runtime_id": record.get("container_runtime_id"),
        "outcome": outcome,
        "evidence_artifact_id": evidence_artifact_id,
        "route_projection": dict(route_projection),
    })
    try:
        state.append_transcript(
            "external_job_finalization_route_reconciliation_required",
            reported_by=reported_by,
            scheduler=blocker["scheduler"],
            job_id=blocker["job_id"],
            submission_nonce=blocker.get("submission_nonce"),
            container_runtime_id=blocker.get("container_runtime_id"),
            route_reason=route_projection.get("reason"),
        )
    except Exception:
        pass
    return dict(blocker)


def _resolve_finalized_needs_route_blocker(
    state: State, record: dict[str, Any],
) -> None:
    reported_by = _finalized_needs_route_reported_by(record)
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list):
        return
    remaining = [
        item for item in blockers
        if not (
            isinstance(item, dict) and item.get("reported_by") == reported_by
        )
    ]
    if len(remaining) == len(blockers):
        return
    state.hook_state["blockers"] = remaining
    try:
        state.append_transcript(
            "blocker_resolved", reported_by=reported_by,
            reason="external_route_projection_completed",
            scheduler=str(record.get("scheduler") or "").lower(),
            job_id=str(record.get("job_id") or ""),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception:
        pass


def _external_job_needs_task_reported_by(record: dict[str, Any]) -> str:
    digest = hashlib.sha256(_job_key_for_record(record).encode("utf-8")).hexdigest()
    return f"{_EXTERNAL_JOB_NEEDS_TASK_PREFIX}{digest[:24]}"


def _record_external_job_needs_task_blocker(
    state: State,
    record: dict[str, Any],
    task_completion: dict[str, Any],
    *,
    closure_kind: str,
    outcome: str,
    evidence_artifact_id: str,
) -> dict[str, Any]:
    """Persist one retryable task-closure blocker for an exact job identity."""
    reported_by = _external_job_needs_task_reported_by(record)
    blockers = _ensure_blocker_list_for_recording(state)
    blocker = next(
        (
            item for item in blockers
            if isinstance(item, dict) and item.get("reported_by") == reported_by
        ),
        None,
    )
    retry_tool = (
        "finalize_external_job" if closure_kind == "finalize" else "cancel_job"
    )
    if blocker is None:
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="external_job",
            summary=(
                "external job terminal facts are durable but its matching "
                "handoff task could not be completed"
            ),
            requested_action=(
                f"repair the task ledger and retry {retry_tool} without "
                "resubmitting or repeating scheduler cancellation"
            ),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=reported_by,
            evidence_paths=(
                [evidence_artifact_id] if evidence_artifact_id else []
            ),
        )
    blocker.update({
        "reason": "external_job_needs_task_reconciliation",
        "closure_kind": closure_kind,
        "scheduler": str(record.get("scheduler") or "").lower(),
        "job_id": str(record.get("job_id") or ""),
        "namespace": record.get("namespace"),
        "submission_nonce": record.get("submission_nonce"),
        "container_runtime_id": record.get("container_runtime_id"),
        "outcome": outcome,
        "evidence_artifact_id": evidence_artifact_id,
        "task_completion": dict(task_completion),
    })
    try:
        state.append_transcript(
            "external_job_task_reconciliation_required",
            reported_by=reported_by,
            closure_kind=closure_kind,
            scheduler=blocker["scheduler"],
            job_id=blocker["job_id"],
            submission_nonce=blocker.get("submission_nonce"),
            container_runtime_id=blocker.get("container_runtime_id"),
            task_reason=task_completion.get("reason"),
        )
    except Exception:
        pass
    return dict(blocker)


def _resolve_external_job_needs_task_blocker(
    state: State, record: dict[str, Any],
) -> None:
    """Resolve only this exact job's task blocker after lifecycle persistence."""
    reported_by = _external_job_needs_task_reported_by(record)
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list):
        return
    remaining = [
        item for item in blockers
        if not (
            isinstance(item, dict) and item.get("reported_by") == reported_by
        )
    ]
    if len(remaining) == len(blockers):
        return
    state.hook_state["blockers"] = remaining
    try:
        state.append_transcript(
            "blocker_resolved", reported_by=reported_by,
            reason="matching_external_job_task_completed",
            scheduler=str(record.get("scheduler") or "").lower(),
            job_id=str(record.get("job_id") or ""),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception:
        pass


def _external_handoff_markers_after_finalization(
    state: State, finalized_record: dict[str, Any],
) -> set[str]:
    """Compute exact handoff identities that remain open after this closure."""
    lifecycle = _job_lifecycle_states(state)
    finalized_key = _job_key_for_record(finalized_record)
    active: set[str] = set()
    seen: set[str] = set()
    for row in _submission_payloads(state) + _task_external_jobs(state):
        scheduler = str(row.get("scheduler") or "")
        job_id = str(row.get("job_id") or "")
        key = _job_key_for_record(row)
        if not scheduler or not job_id or key in seen:
            continue
        seen.add(key)
        if key == finalized_key:
            continue
        resolution = lifecycle_for_submission(
            state, row, lifecycle_states=lifecycle,
        )
        status = resolution.get("status")
        if status is not None and status not in _ACTIVE_JOB_STATES:
            continue
        active.add(_external_job_handoff_reported_by(row))
    return active


def _reconcile_external_job_handoff_blockers(
    state: State, active_markers: set[str],
) -> None:
    """Remove only stale exact handoff blockers; preserve every open identity."""
    blockers = _ensure_blocker_list_for_recording(state)
    remaining = []
    removed = []
    for item in blockers:
        reported_by = str(item.get("reported_by") or "") if isinstance(item, dict) else ""
        stale_scoped = (
            reported_by.startswith(_EXTERNAL_JOB_HANDOFF_BLOCKER_PREFIX)
            and reported_by not in active_markers
        )
        stale_legacy = (
            reported_by == _LEGACY_EXTERNAL_JOB_HANDOFF_BLOCKER
            and not active_markers
        )
        if stale_scoped or stale_legacy:
            removed.append(reported_by)
        else:
            remaining.append(item)
    if not removed:
        return
    state.hook_state["blockers"] = remaining
    for reported_by in removed:
        try:
            state.append_transcript(
                "blocker_resolved", reported_by=reported_by,
                reason="external_job_identity_no_longer_unresolved",
            )
        except Exception:
            pass


def _finalized_needs_cleanup_reported_by(record: dict[str, Any]) -> str:
    digest = hashlib.sha256(_job_key_for_record(record).encode("utf-8")).hexdigest()
    return f"{_FINALIZED_NEEDS_CLEANUP_PREFIX}{digest[:24]}"


def _record_finalized_needs_cleanup_blocker(
    state: State,
    record: dict[str, Any],
    cleanup: dict[str, Any],
    *,
    closure_kind: str = "finalize",
) -> dict[str, Any]:
    """Persist one retryable, identity-bound cleanup blocker without duplicates."""
    retry_tool = "finalize_external_job" if closure_kind == "finalize" else "cancel_job"
    reported_by = _finalized_needs_cleanup_reported_by(record)
    blockers = _ensure_blocker_list_for_recording(state)
    blocker = next(
        (
            item for item in blockers
            if isinstance(item, dict) and item.get("reported_by") == reported_by
        ),
        None,
    )
    if blocker is None:
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="external_job",
            summary=("local external job reached terminal state but its immutable-ID "
                     "sandbox cleanup did not complete"),
            requested_action=("do not resubmit or mark the workflow complete; retry "
                              f"{retry_tool} after restoring native local-job/control-dir cleanup "
                              "capability"),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=reported_by,
            evidence_paths=[
                str(record.get("sandbox_control_dir") or ""),
            ],
        )
    blocker.update({
        "reason": (
            "finalized_needs_cleanup" if closure_kind == "finalize"
            else "cancelled_needs_cleanup"
        ),
        "closure_kind": closure_kind,
        "scheduler": str(record.get("scheduler") or "").lower(),
        "job_id": str(record.get("job_id") or ""),
        "namespace": record.get("namespace"),
        "submission_nonce": record.get("submission_nonce"),
        "container_runtime_id": record.get("container_runtime_id"),
        "sandbox_control_dir": record.get("sandbox_control_dir"),
        "cleanup_error": str(cleanup.get("error") or "")[:2000],
        "cleanup_error_type": cleanup.get("error_type"),
    })
    try:
        state.append_transcript(
            "external_job_terminal_cleanup_required",
            reported_by=reported_by,
            closure_kind=closure_kind,
            scheduler=blocker["scheduler"],
            job_id=blocker["job_id"],
            submission_nonce=blocker.get("submission_nonce"),
            container_runtime_id=blocker.get("container_runtime_id"),
            cleanup_error=blocker["cleanup_error"],
        )
    except Exception:
        pass
    return dict(blocker)


def _resolve_finalized_needs_cleanup_blocker(
    state: State,
    record: dict[str, Any],
) -> None:
    """Resolve only this exact job cleanup blocker after a verified retry."""
    reported_by = _finalized_needs_cleanup_reported_by(record)
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list):
        return
    remaining = [
        item for item in blockers
        if not (
            isinstance(item, dict)
            and item.get("reported_by") == reported_by
        )
    ]
    if len(remaining) == len(blockers):
        return
    state.hook_state["blockers"] = remaining
    try:
        state.append_transcript(
            "blocker_resolved",
            reported_by=reported_by,
            reason="immutable_local_sandbox_cleanup_completed",
            scheduler=str(record.get("scheduler") or "").lower(),
            job_id=str(record.get("job_id") or ""),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception:
        pass


def _cleanup_local_job_for_finalization(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Remove one local sandbox by immutable ID before irreversible closure."""
    job_id = str(record.get("job_id") or "")
    runtime_id = str(record.get("container_runtime_id") or "")
    control_dir = str(record.get("sandbox_control_dir") or "")
    if not _LOCAL_CONTAINER_ID.fullmatch(job_id):
        return {
            "status": "error",
            "error_type": "invalid_local_job_id",
            "error": "local finalization requires the managed hf-... container name",
            "job_id": job_id,
            "container_runtime_id": runtime_id or None,
            "sandbox_control_dir": control_dir or None,
        }
    if not _DOCKER_RUNTIME_ID.fullmatch(runtime_id):
        return {
            "status": "error",
            "error_type": "missing_immutable_container_runtime_id",
            "error": ("local finalization requires the 64-hex immutable Docker "
                      "container_runtime_id; no mutable-name cleanup was attempted"),
            "job_id": job_id,
            "container_runtime_id": runtime_id or None,
            "sandbox_control_dir": control_dir or None,
        }
    from core import sandbox

    try:
        named = sandbox.inspect_container(job_id)
        exact = sandbox.inspect_container(runtime_id)
        if named.get("error") or exact.get("error"):
            raise RuntimeError(
                "local sandbox identity is not inspectable before cleanup: "
                f"name={named.get('error')!r}, id={exact.get('error')!r}"
            )
        if bool(named.get("exists")) != bool(exact.get("exists")):
            raise RuntimeError(
                "local sandbox identity visibility is inconsistent before cleanup"
            )
        if exact.get("exists"):
            if (
                exact.get("id") != runtime_id
                or exact.get("name") != job_id
                or not exact.get("managed")
                or exact.get("kind") != "job"
                or exact.get("namespace") != sandbox.sandbox_namespace()
                or named.get("id") != runtime_id
            ):
                raise RuntimeError(
                    "local sandbox immutable identity or ownership changed before cleanup"
                )
            sandbox.stop_container(
                job_id,
                remove=True,
                expected_container_id=runtime_id,
            )
        else:
            # A confirmed cancellation normally removed the container already.
            # Release only the exact reservation; never issue a second stop.
            sandbox.release_reservation(job_id)

        post = sandbox.inspect_container(runtime_id)
        if post.get("error"):
            raise RuntimeError(
                "local sandbox absence is not verifiable after cleanup: "
                f"{post.get('error')}"
            )
        if post.get("exists"):
            raise RuntimeError(
                "local sandbox immutable container still exists after cleanup"
            )
        if control_dir:
            sandbox.cleanup_control_dir(control_dir)
            control_path = Path(control_dir).expanduser().resolve(strict=False)
            try:
                control_path.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(
                    "local sandbox control-dir absence is not verifiable"
                ) from exc
            else:
                raise RuntimeError(
                    "local sandbox control directory still exists after cleanup"
                )
    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "job_id": job_id,
            "container_runtime_id": runtime_id,
            "sandbox_control_dir": control_dir or None,
        }
    return {
        "status": "success",
        "action": "sandbox_stop_remove_and_control_cleanup",
        "job_id": job_id,
        "container_runtime_id": runtime_id,
        "sandbox_control_dir": control_dir or None,
    }


def _finalize_job_class(state: Any, record: dict[str, Any]) -> dict[str, Any]:
    """finalize 侧的 execution_class 只认 submit 时持久化收据的交叉核验。

    handoff task 行与 finalize 调用自述都可能被后续会话改写；唯一权威来源
    是受防伪写入门保护的 job_submission/recovery payload 里持久化的
    execution_class。payload 缺失、值不可识别或与 record 自述不一致时一律
    按 "simulation" 处理（fail-closed），绝不复用 hooks 的 caller-stage
    回退（那是洗白路径）。

    返回值同时带上核验的两端 —— record 自述的 claimed 与受管 payload 里的
    persisted —— 好让 fail-closed 的收尾把"是哪两个值对不上"刻进收据。
    """
    claimed = str(record.get("execution_class") or "").strip().lower()
    expected_key = _job_key_for_record(record)
    persisted = {
        str(row.get("execution_class") or "").strip().lower()
        for row in _submission_payloads(state)
        if _job_key_for_record(row) == expected_key
    }
    verdict = {"claimed_execution_class": claimed,
               "persisted_execution_class": ",".join(sorted(persisted))}
    if len(persisted) != 1:
        return {**verdict, "execution_class": "simulation", "verified": False,
                "reason": ("submission_payload_execution_class_missing"
                           if not persisted
                           else "submission_payload_execution_class_ambiguous")}
    payload_class = next(iter(persisted))
    if payload_class not in _OPERATION_EXECUTION_CLASSES | {"simulation"}:
        return {**verdict, "execution_class": "simulation", "verified": False,
                "reason": "submission_payload_execution_class_unrecognized"}
    if claimed != payload_class:
        return {**verdict, "execution_class": "simulation", "verified": False,
                "reason": "execution_class_cross_check_mismatch"}
    return {**verdict, "execution_class": payload_class, "verified": True,
            "reason": "cross_checked_with_submission_payload"}


def _operation_job_exit_code(health: dict[str, Any]) -> int | None:
    """机械读出的作业退出码；读不到就是 None，绝不采信模型自述。"""
    raw = (health.get("scheduler_result") or {}).get("raw")
    if not isinstance(raw, dict):
        return None
    sandbox_state = raw.get("sandbox_state")
    if not isinstance(sandbox_state, dict):
        return None
    code = sandbox_state.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _termination_verdict(
    record: dict[str, Any], health: dict[str, Any], *, exit_code: int | None = None,
) -> dict[str, Any]:
    """作业终止是否符合提交时冻结的 expected_termination（第 5 步 5b）。

    没有声明 → termination_matched=None（不适用，行为同现状）。收据里已冻结判定时直接读它
    （收据复用路径的 health 不带 scheduler_result）。否则读得到退出码才判：在声明的集合里、
    且没有 error_evidence（平台标记始终匹配，见 _health_error_markers）；读不到退出码为
    None——非零预期只能靠退出码核对，completion_paths 证明不了「以 3 退出」。
    物理退出码与收尾结果词都不因此改写：exit≠0 仍记 operation_failed。
    """
    declared = record.get("expected_termination") if isinstance(record, dict) else None
    codes = declared.get("exit_codes") if isinstance(declared, dict) else None
    if not isinstance(codes, list) or not codes:
        return {"declared": False, "termination_matched": None}
    expected = sorted({code for code in codes
                       if isinstance(code, int) and not isinstance(code, bool)})
    health = health if isinstance(health, dict) else {}
    if isinstance(health.get("termination_matched"), bool):
        return {"declared": True, "expected_exit_codes": expected,
                "exit_code": health.get("exit_code"),
                "termination_matched": health["termination_matched"],
                "source": "operation_closure_receipt"}
    # 被调度器结束的作业没有属于自己的退出码（sacct 的 ExitCode 低位恒为 0），
    # 拿它去对 expected_termination 会让 NODE_FAIL 的 0:0 撞上 exit_codes=[0]。
    # 判在这里而不是在各个调用点：termination_matched 有三个产生端
    # （finalize、cancel 守卫、record_external_route_finalization），收口一处。
    # 放在收据短路之后：已冻结的判定照旧原样重放，本包不改重放语义。
    terminal_evidence = health.get("terminal_evidence")
    if (isinstance(terminal_evidence, dict)
            and terminal_evidence.get("scheduler_terminated") is True):
        return {"declared": True, "expected_exit_codes": expected,
                "exit_code": terminal_evidence.get("returncode"),
                "termination_matched": False,
                "source": "scheduler_terminated"}
    code = exit_code if exit_code is not None else _operation_job_exit_code(health)
    if code is None:
        return {"declared": True, "expected_exit_codes": expected, "exit_code": None,
                "termination_matched": None, "source": "exit_code_unreadable"}
    errors = list(health.get("error_evidence") or [])
    return {"declared": True, "expected_exit_codes": expected, "exit_code": code,
            "termination_matched": code in expected and not errors,
            "source": "exit_code",
            **({"error_evidence_present": True} if errors else {})}


def _termination_receipt_fields(record: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """声明了预期终止时，收据冻结声明本身与判定；没声明时收据形状不变。"""
    verdict = _termination_verdict(record, health)
    if not verdict.get("declared"):
        return {}
    return {"expected_termination": dict(record.get("expected_termination") or {}),
            "termination_matched": verdict.get("termination_matched")}


def _operation_completion_paths_fresh(
    record: dict[str, Any], health: dict[str, Any],
) -> tuple[bool, str]:
    """completion_paths 全部存在且 mtime ≥ submitted_at 才算正面完成证据。

    只看 exists 会放行提交前就存在的旧产物（模型自证放行）；快照 mtime
    必须晚于该 job 的 submitted_at 才能证明产物来自这次执行。
    """
    snapshots = health.get("completion_paths")
    if not isinstance(snapshots, list) or not snapshots:
        return False, "health_contract 未声明 completion_paths（无完成产物可核验）"
    try:
        submitted = datetime.fromisoformat(
            str(record.get("submitted_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False, "受管记录缺少可解析的 submitted_at，无法证明产物晚于提交"
    observation = health.get("terminal_output_observation")
    if isinstance(observation, dict) and observation.get("source") == "unfrozen":
        return False, "首次终态观测未能冻结（持久化失败），不能据此判定完成产物"
    # 035a：与 route / ROC 同一个 evaluator。类型按提交时登记的 kind（尾 /），非空，
    # 内容不早于提交；只有当前作业精确登记的 stdout/stderr 可以为空。
    contract = record.get("health_contract") if isinstance(record.get("health_contract"), dict) else {}
    kinds = contract.get("completion_kinds") if isinstance(contract.get("completion_kinds"), dict) else {}
    passed, reason, _rows = _output_postconditions.evaluate_health_snapshots(
        snapshots,
        kinds={str(k): str(v) for k, v in kinds.items()},
        not_before_ns=int(submitted.timestamp() * 1_000_000_000),
        allow_empty_realpaths=[
            str(item) for item in (record.get("stdout_path"), record.get("stderr_path")) if item
        ],
    )
    return (True, "completion_paths_fresh") if passed else (False, reason)



def _external_route_completion_postcondition(
    state: Any, record: dict[str, Any], health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the route-owned completion declaration without side effects."""
    observation = health.get("terminal_output_observation") if isinstance(health, dict) else None
    not_after = observation.get("not_after_ns") if isinstance(observation, dict) else None
    try:
        try:
            from .execution_route import resolve_external_route_expected_outputs
        except ImportError:
            from tools.execution_route import resolve_external_route_expected_outputs
        result = resolve_external_route_expected_outputs(
            state,
            not_after_ns=int(not_after) if isinstance(not_after, int) and not_after > 0 else None,
            scheduler=str(record.get("scheduler") or ""),
            job_id=str(record.get("job_id") or ""),
            namespace=record.get("namespace"),
            launch_host=record.get("launch_host"),
            scheduler_cluster=record.get("scheduler_cluster"),
            resource_uid=record.get("resource_uid"),
            submission_nonce=record.get("submission_nonce"),
            process_group_id=record.get("process_group_id"),
            process_start_ticks=record.get("process_start_ticks"),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception as exc:
        return {
            "status": "indeterminate",
            "reason": "route_completion_postcondition_probe_failed",
            "error_type": type(exc).__name__,
            "declared": False,
            "passed": False,
        }
    if not isinstance(result, dict):
        return {
            "status": "indeterminate",
            "reason": "route_completion_postcondition_probe_invalid_response",
            "response_type": type(result).__name__,
            "declared": False,
            "passed": False,
        }
    return result


def _operation_completion_postconditions(
    state: Any,
    record: dict[str, Any],
    health: dict[str, Any],
    execution_class: str,
) -> dict[str, Any]:
    """Build the single read-only completion-postcondition view.

    Each declaration source keeps its existing mechanical verifier: health
    paths use submission time freshness, while route outputs use the route
    binding baseline.  This function only combines those facts; it never
    writes an event, artifact, or projection.
    """
    contract = (
        record.get("health_contract")
        if isinstance(record.get("health_contract"), dict)
        else {}
    )
    declared_health_paths = sorted({
        os.path.realpath(str(path))
        for path in (contract.get("completion_paths") or [])
        if str(path or "").strip()
    })
    snapshots = [
        dict(item) for item in (health.get("completion_paths") or [])
        if isinstance(item, dict)
    ]
    observed_health_paths = sorted({
        os.path.realpath(str(item.get("path")))
        for item in snapshots
        if str(item.get("path") or "").strip()
    })
    health_declared = bool(declared_health_paths)
    health_paths_match = bool(
        not health_declared
        or observed_health_paths == declared_health_paths
    )
    if health_declared:
        health_fresh, health_reason = _operation_completion_paths_fresh(
            record, health)
        health_passed = bool(health_paths_match and health_fresh)
        if not health_paths_match:
            health_reason = "completion_paths 声明与实际快照的路径集合不一致"
    else:
        health_passed = True
        health_reason = "health_completion_paths_not_declared"
    health_source = {
        "status": "ready",
        "declared": health_declared,
        "passed": health_passed,
        "declared_paths": declared_health_paths,
        "observed_paths": observed_health_paths,
        "path_identity_matched": health_paths_match,
        "snapshots": snapshots,
        "reason": health_reason,
    }

    route_source = _external_route_completion_postcondition(state, record, health)
    route_status = str(route_source.get('status') or "indeterminate")
    route_known = route_status in {"ready", "absent", "not_applicable"}
    route_declared = bool(route_source.get("declared"))
    route_passed = bool(
        route_known
        and (not route_declared or route_source.get("passed") is True)
    )
    route_source = {
        **route_source,
        "declared": route_declared,
        "passed": route_passed,
    }

    normalized_class = str(execution_class or "").strip().lower()
    zero_declaration_toolchain = bool(
        normalized_class == "toolchain_build"
        and route_known
        and not health_declared
        and not route_declared
    )
    status = "ready" if route_known else "indeterminate"
    passed = bool(
        status == "ready"
        and health_passed
        and route_passed
        and not zero_declaration_toolchain
    )
    declared_sources = [
        name for name, source in (
            ("health", health_source),
            ("route", route_source),
        )
        if source.get("declared") is True
    ]
    return {
        "schema_version": 1,
        "status": status,
        "passed": passed,
        "execution_class": normalized_class,
        "declared_sources": declared_sources,
        "zero_declaration_toolchain": zero_declaration_toolchain,
        "external_identity": _operation_closure_identity(record),
        "sources": {
            "health": health_source,
            "route": route_source,
        },
    }



def _valid_frozen_completion_postconditions(
    value: Any,
    record: dict[str, Any],
    execution_class: str,
    *,
    receipt_health: dict[str, Any] | None,
) -> bool:
    """Validate every frozen invariant without consulting cleaned-up files."""
    if not isinstance(value, dict) or not isinstance(receipt_health, dict):
        return False
    normalized_class = str(execution_class or "").strip().lower()
    if (
        value.get("schema_version") != 1
        or value.get("status") != "ready"
        or value.get("passed") is not True
        or str(value.get("execution_class") or "") != normalized_class
    ):
        return False
    identity = value.get("external_identity")
    if not (
        isinstance(identity, dict)
        and set(identity) == set(_OPERATION_CLOSURE_IDENTITY_FIELDS)
        and _operation_closure_identity_key(identity)
        == _operation_closure_identity_key(_operation_closure_identity(record))
    ):
        return False
    sources = value.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"health", "route"}:
        return False
    health = sources.get("health")
    route = sources.get("route")
    if not isinstance(health, dict) or not isinstance(route, dict):
        return False

    contract = (
        record.get("health_contract")
        if isinstance(record.get("health_contract"), dict)
        else {}
    )
    expected_health_paths = sorted({
        os.path.realpath(str(path))
        for path in (contract.get("completion_paths") or [])
        if str(path or "").strip()
    })
    declared_paths = health.get("declared_paths")
    observed_paths = health.get("observed_paths")
    snapshots = health.get("snapshots")
    if not (
        health.get("status") == "ready"
        and isinstance(health.get("declared"), bool)
        and isinstance(health.get("passed"), bool)
        and isinstance(declared_paths, list)
        and isinstance(observed_paths, list)
        and isinstance(snapshots, list)
        and all(isinstance(item, dict) for item in snapshots)
    ):
        return False
    normalized_declared = sorted({
        os.path.realpath(str(path))
        for path in declared_paths
        if str(path or "").strip()
    })
    normalized_observed = sorted({
        os.path.realpath(str(path))
        for path in observed_paths
        if str(path or "").strip()
    })
    snapshot_paths = sorted({
        os.path.realpath(str(item.get("path")))
        for item in snapshots
        if str(item.get("path") or "").strip()
    })
    health_declared = bool(expected_health_paths)
    if (
        health.get("declared") is not health_declared
        or normalized_declared != expected_health_paths
        or normalized_observed != expected_health_paths
        or snapshot_paths != expected_health_paths
        or health.get("path_identity_matched") is not True
        or list(receipt_health.get("completion_paths") or []) != snapshots
    ):
        return False
    if health_declared:
        health_fresh, _reason = _operation_completion_paths_fresh(
            record, {"completion_paths": snapshots}
        )
        if health.get("passed") is not health_fresh:
            return False
    elif health.get("passed") is not True or snapshots:
        return False

    list_fields = (
        "expected_outputs",
        "verified_output_specs",
        "verified_outputs",
        "missing_expected_outputs",
    )
    if not all(isinstance(route.get(field), list) for field in list_fields):
        return False
    expected_outputs = [
        str(item) for item in route.get("expected_outputs") or []
        if str(item).strip()
    ]
    verified_specs = [
        str(item) for item in route.get("verified_output_specs") or []
        if str(item).strip()
    ]
    verified_outputs = [
        str(item) for item in route.get("verified_outputs") or []
        if str(item).strip()
    ]
    missing_outputs = [
        str(item) for item in route.get("missing_expected_outputs") or []
        if str(item).strip()
    ]
    route_declared = bool(expected_outputs)
    route_status = str(route.get("status") or "")
    if (
        not isinstance(route.get("declared"), bool)
        or not isinstance(route.get("passed"), bool)
        or route.get("declared") is not route_declared
        or route_status not in {"ready", "absent", "not_applicable"}
        or route.get("passed") is not True
    ):
        return False
    if route_declared:
        route_presence = route.get("route_presence")
        route_identity = (
            route_presence.get("external_identity")
            if isinstance(route_presence, dict)
            else None
        )
        attempt_id = str(route.get("attempt_id") or "")
        record_attempt_id = str(record.get("route_attempt_id") or "")
        current_hash = str(route.get("step_execution_contract_hash") or "")
        bound_hash = str(route.get("bound_step_execution_contract_hash") or "")
        correction_applied = route.get("correction_applied")
        if not (
            route_status == "ready"
            and not missing_outputs
            and sorted(verified_specs) == sorted(expected_outputs)
            and bool(verified_outputs)
            and attempt_id
            and (not record_attempt_id or record_attempt_id == attempt_id)
            and str(route.get("route_step_id") or "")
            and current_hash
            and bound_hash
            and isinstance(correction_applied, bool)
            and (correction_applied or bound_hash == current_hash)
            and route.get("observation_source") in {
                "binding_verifier",
                "validated_correction_reverification",
                "route_step_external_execution_verified",
            }
            and isinstance(route_presence, dict)
            and route_presence.get("status") == "present"
            and str(route_presence.get("attempt_id") or "") == attempt_id
            and isinstance(route_identity, dict)
            and _operation_closure_identity_key(route_identity)
            == _operation_closure_identity_key(identity)
        ):
            return False
    elif any((expected_outputs, verified_specs, verified_outputs, missing_outputs)):
        return False

    zero_declaration_toolchain = bool(
        normalized_class == "toolchain_build"
        and not health_declared
        and not route_declared
    )
    if (
        value.get("zero_declaration_toolchain") is not zero_declaration_toolchain
        or zero_declaration_toolchain
    ):
        return False
    expected_sources = [
        name
        for name, declared in (
            ("health", health_declared),
            ("route", route_declared),
        )
        if declared
    ]
    return value.get("declared_sources") == expected_sources


def _persist_route_completion_postcondition(
    state: Any,
    record: dict[str, Any],
    health: dict[str, Any],
    completion_postconditions: dict[str, Any],
) -> dict[str, Any] | None:
    """Persist a declared route observation through the existing writer."""
    route = (
        (completion_postconditions.get("sources") or {}).get("route") or {}
    )
    if not (
        route.get("status") == "ready"
        and route.get("declared") is True
        and route.get("observation_source")
        != "route_step_external_execution_verified"
    ):
        return None
    execution_evidence = _operation_completion_execution_success_evidence(
        record, health
    )
    if not (
        execution_evidence.get("verified") is True
        and execution_evidence.get("succeeded") is True
    ):
        return {
            "status": "error",
            "reason": "operation_execution_success_unverified",
            "execution_success_evidence": execution_evidence,
        }
    try:
        try:
            from .execution_route import record_external_route_execution_verification
        except ImportError:
            from tools.execution_route import record_external_route_execution_verification
        return record_external_route_execution_verification(
            state,
            external_job_ref={
                **{
                    field: record.get(field)
                    for field in _OPERATION_CLOSURE_IDENTITY_FIELDS
                },
                "route_attempt_id": route.get("attempt_id"),
            },
            terminal=True,
            success_verified=True,
            success_evidence=execution_evidence,
            expected_output_observation=route,
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": "route_completion_postcondition_persistence_failed",
            "error_type": type(exc).__name__,
        }


def _operation_completion_execution_success_evidence(
    record: dict[str, Any], health: dict[str, Any],
) -> dict[str, Any]:
    """Return independent physical-success evidence used by completion writers."""
    errors = list(health.get("error_evidence") or [])
    exit_code = _operation_job_exit_code(health)
    termination = _termination_verdict(record, health)
    if errors:
        return {
            "verified": False,
            "succeeded": False,
            "reason": "health_error_evidence_present",
            "error_evidence": errors,
            "exit_code": exit_code,
        }
    if termination.get("declared"):
        return {
            "verified": False,
            "succeeded": False,
            "reason": "expected_termination_declared",
            "exit_code": exit_code,
        }
    if exit_code == 0:
        return {
            "verified": True,
            "succeeded": True,
            "source": "operation_exit_code",
            "returncode": 0,
            "domain_outcome": "operation_completed",
        }
    if exit_code is None:
        fresh, reason = _operation_completion_paths_fresh(record, health)
        if fresh:
            return {
                "verified": True,
                "succeeded": True,
                "source": "health_completion_paths_fresh",
                "completion_paths": list(health.get("completion_paths") or []),
                "domain_outcome": "operation_completed",
            }
        return {
            "verified": False,
            "succeeded": False,
            "reason": reason,
            "exit_code": None,
        }
    return {
        "verified": True,
        "succeeded": False,
        "source": "operation_exit_code",
        "returncode": exit_code,
        "domain_outcome": "operation_failed",
    }


def _operation_completion_postcondition_error(
    completion_postconditions: dict[str, Any] | None,
    *,
    blocked_sequence: str,
    errors: list[Any],
    exit_code: int | None,
    route_correction_available: bool,
    execution_success_evidence: dict[str, Any],
) -> dict[str, Any] | None:
    """Explain a failed declared postcondition without suggesting a dead end."""
    if (
        completion_postconditions is None
        or completion_postconditions.get("passed") is True
    ):
        return None
    sources = completion_postconditions.get("sources") or {}
    health_source = sources.get("health") or {}
    route_source = sources.get("route") or {}
    if completion_postconditions.get("zero_declaration_toolchain"):
        detail = (
            "execution_class=toolchain_build，但 health completion_paths "
            "与 route expected_outputs 都没有声明；构建类作业必须在提交"
            "或路线绑定时声明可机械核验的产物"
        )
    elif completion_postconditions.get("status") != "ready":
        detail = (
            "完成后置条件来源无法唯一解析："
            f"route={route_source.get('reason') or route_source.get('status')}"
        )
    elif health_source.get("declared") and not health_source.get("passed"):
        detail = (
            "health completion_paths 未通过："
            f"{health_source.get('reason')}"
        )
    else:
        detail = (
            "route expected_outputs 未通过："
            f"missing={route_source.get('missing_expected_outputs') or []}"
        )
    route_recovery = (
        "若 route expected_outputs 声明错误，请用 declare_execution_route "
        "携带 amendment_reason 与 recovery_basis 纠正同一 attempt 后重试；"
        if (
            route_correction_available
            and route_source.get('missing_expected_outputs')
        )
        else ""
    )
    return {
        "status": "error",
        "error_code": "operation_completed_positive_evidence_missing",
        "error": (
            "operation_completed 除退出码/错误证据外，还要求所有已声明的"
            f"完成后置条件同时通过；当前 {detail}。{route_recovery}"
            "若真实产物未生成、无法纠正或环境阻塞，请按序调用："
            f"{blocked_sequence}。"
        ),
        "error_evidence": errors,
        "exit_code": exit_code,
        "completion_postconditions": completion_postconditions,
        "route_correction_available": bool(
            route_correction_available
            and route_source.get('missing_expected_outputs')
        ),
        "execution_success_evidence": execution_success_evidence,
    }


def _operation_outcome_evidence_error(
    state: Any, record: dict[str, Any], health: dict[str, Any],
    outcome: str, note: str, *, class_unverified: bool,
    class_disputed: bool = False, caller_evidence_matched: bool = False,
    authoritative_execution_class: str = "",
    completion_postconditions: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """operation 终态的正面证据矩阵；不满足时给出逐字可执行的纠偏调用。"""
    scheduler = str(record.get("scheduler") or "")
    job_id = str(record.get("job_id") or "")
    call_prefix = (
        f'finalize_external_job(scheduler="{scheduler}", job_id="{job_id}"'
    )
    blocked_sequence = (
        "1) report_blocker(summary=\"<阻塞事实>\", requested_action=\"<需要谁做什么>\")；"
        f'2) {call_prefix}, outcome="operation_blocked", note="<阻塞原因>")'
    )
    # 收窄：fail-closed 的理由是「收尾成功即销毁沙箱，降格等于允许先毁证后补析」，
    # 而这个风险只在**正面宣称**上成立——一个 exit≠0 的作业没有可供补析的结果，
    # 失败与受阻是记录不是拒绝（F11 判决）。调用方已附上身份精确匹配的 frozen 非
    # 自动 experiment_log 时，"补析"这件事已经发生过，同样不需要拦。
    # 类别未核验里有一格必须一律严格：交叉核验不一致、而受管台账里的**权威值就是
    # simulation**。那不是"类别不明"，是"调用方主张与权威值相反"。此时用运维词表
    # 关掉它就是把科学作业洗白，调用方附的见证也解不开——见证证明的是它读过输出，
    # 不能证明这个作业不是科学模拟。
    laundering_risk = (
        str(authoritative_execution_class or "").strip().lower() == "simulation")
    if class_unverified and (
            (outcome == "operation_completed"
             and (laundering_risk or not caller_evidence_matched))
            or (outcome == "operation_failed" and laundering_risk)):
        return {
            "status": "error",
            "error_code": "operation_execution_class_unverified",
            "error": (
                "该 job 的 execution_class 无法与 submit 时持久化的 job_submission "
                "payload 交叉核验（缺失或不一致），不允许宣称成功或失败。"
                "只有两条收尾路：A) 附完整证据走科学通道 —— 铸造 frozen 非自动 "
                "experiment_log（metadata.external_job_refs 复制受管记录的完整 "
                f"identity）后调用 {call_prefix}, "
                'evidence_artifact_id="<log id>", outcome="analyzed_success|'
                'analyzed_failure|analyzed_inconclusive")；B) 只记录"作业已死但'
                f"身份未证\"的事实：{blocked_sequence}。"
            ),
        }
    errors = list(health.get("error_evidence") or [])
    exit_code = _operation_job_exit_code(health)
    termination = _termination_verdict(record, health)
    if outcome == "operation_completed":
        execution_success_evidence = (
            _operation_completion_execution_success_evidence(record, health)
        )
        # Preserve the established remote fallback: an unreadable exit code
        # still requires fresh health completion_paths.  The aggregate view
        # may add necessary declared conditions, but it must not broaden that
        # fallback to route-only evidence in this change.
        completion_fallback, fallback_reason = (
            _operation_completion_paths_fresh(record, health)
        )
        postcondition_error = _operation_completion_postcondition_error(
            completion_postconditions,
            blocked_sequence=blocked_sequence,
            errors=errors,
            exit_code=exit_code,
            route_correction_available=(
                execution_success_evidence.get("verified") is True
                and execution_success_evidence.get("succeeded") is True
            ),
            execution_success_evidence=execution_success_evidence,
        )
        if (
            postcondition_error is not None
            and not errors
            and not termination["declared"]
            and exit_code in {None, 0}
        ):
            return postcondition_error
        # 读得到的非零退出码不能被任何产物覆盖；只有退出码不可读时，
        # 才允许沿用既有的 health completion_paths 远端回退。
        # 提交时声明了预期终止的作业仍按物理事实记结果。
        if (errors or termination["declared"]
                or not (
                    exit_code == 0
                    or (exit_code is None and completion_fallback)
                )):
            detail = (
                f"error_evidence 非空（{len(errors)} 条）" if errors
                else (f"提交时声明了预期终止（退出码 {termination.get('expected_exit_codes')}），"
                      "收尾结果按物理事实记，是否符合预期由 termination_matched 机械判定"
                      if termination["declared"]
                      else f"exit_code={exit_code!r}，读得到的非零退出码不能由 completion_paths 代证"
                      if exit_code is not None
                      else f"exit_code=None 且 {fallback_reason}")
            )
            return {
                "status": "error",
                "error_code": "operation_completed_positive_evidence_missing",
                "error": (
                    "operation_completed 需要机械正面证据：error_evidence 为空，"
                    "且 exit_code==0，或 exit_code 不可读时 health_contract "
                    "声明的 completion_paths 全部存在且 mtime ≥ submitted_at；当前 "
                    f"{detail}。"
                    f"作业实际失败请改调 {call_prefix}, "
                    'outcome="operation_failed")；无法判定或被环境阻塞请按序调用：'
                    f"{blocked_sequence}。"
                ),
                "error_evidence": errors,
                "exit_code": exit_code,
            }
        if postcondition_error is not None:
            return postcondition_error
    elif outcome == "operation_failed":
        # 声明了预期非零退出、实际却以 0 退出：不符合预期，这本身就是失败证据。
        if (not errors and not (isinstance(exit_code, int) and exit_code != 0)
                and termination.get("termination_matched") is not False):
            return {
                "status": "error",
                "error_code": "operation_failed_negative_evidence_missing",
                "error": (
                    "operation_failed 需要 error_evidence 非空或非零 exit_code；"
                    f"当前 error_evidence 为空且 exit_code={exit_code!r}。作业成功"
                    f"请改调 {call_prefix}, outcome=\"operation_completed\")；"
                    f"无法判定请按序调用：{blocked_sequence}。"
                ),
                "exit_code": exit_code,
            }
    elif outcome == "operation_blocked":
        if not str(note or "").strip():
            return {
                "status": "error",
                "error_code": "operation_blocked_note_required",
                "error": (
                    "operation_blocked 需要非空 note 说明阻塞事实：重新调用 "
                    f'{call_prefix}, outcome="operation_blocked", '
                    'note="<阻塞原因>")。'
                ),
            }
        blockers = _ensure_blocker_list_for_recording(state)
        if not any(isinstance(item, dict) for item in blockers):
            return {
                "status": "error",
                "error_code": "operation_blocked_requires_recorded_blocker",
                "error": (
                    "operation_blocked 要求 state 已登记 blocker：先调用 "
                    "report_blocker(summary=\"<阻塞事实>\", "
                    "requested_action=\"<需要谁做什么>\")，再重新调用 "
                    f'{call_prefix}, outcome="operation_blocked", '
                    f'note="{str(note or "")[:200]}")。'
                ),
            }
        if not _independent_blocker_recorded(blockers):
            # operation_blocked 的唯一补偿控制不能由框架自己代记的 blocker 满足：
            # 那些 blocker（handoff/needs_route/needs_task/needs_cleanup）恰好会在
            # 本次收尾末尾被判 stale 清掉，用它们解锁等于零成本关掉一个作业，run
            # 最终仍不带任何 blocker。这条要求对**所有**让步路一视同仁：交叉核验
            # 失败（fail-closed 成 simulation）的搁浅路，与核验一致的争议路，付出
            # 的代价必须相同 —— 否则"核验失败"反而比"核验通过"更容易关掉作业。
            return {
                "status": "error",
                "error_code": (
                    "class_disputed_requires_independent_blocker" if class_disputed
                    else "operation_blocked_requires_independent_blocker"),
                "error": (
                    ("以 operation_blocked 关掉一个持久化为 simulation 的作业，"
                     if class_disputed else
                     "以 operation_blocked 关掉一个受管 external job，")
                    + "要求账本里有一条**非框架代记**的 blocker：当前只有框架为"
                    "external job 对账自动登记的 blocker（它们在本次收尾末尾就会"
                    "被判 stale 清掉，不构成任何留痕）。先调用 report_blocker("
                    'summary="<该作业为何关不掉、卡在哪>", '
                    'requested_action="<需要谁修什么或补做什么>")，'
                    f'再重新调用 {call_prefix}, outcome="operation_blocked", '
                    + ('disputed_execution_class="<diagnostic|toolchain_build>", '
                       if class_disputed else "")
                    + f'note="{str(note or "")[:200]}")。'
                ),
            }
    return None


#: 收据解析流水线各段的说明文案。合并前这些是五条独立拒绝返回里的 error 正文，
#: 内容一字未改，只是从五处返回收成一张表 + 一处返回。
_OPERATION_RECEIPT_UNREADABLE_MESSAGES = {
    "record_read": "current-run operation 收据不可读，无法证明无冲突。",
    "record_shape": "current-run operation 收据记录不可读，无法证明无冲突。",
    "content_missing": "当前 external job 的 operation 收据记录不完整。",
    "payload_json": "当前 external job 的 operation 收据 payload 不是可读 JSON。",
    "payload_identity": "当前 external job 的 operation 收据缺少完整 payload identity。",
}


def _operation_closure_receipt(
    state: Any, record: dict[str, Any], outcome: str | None,
) -> dict[str, Any]:
    """Resolve one current-run, node-owned operation-class job receipt.

    The local sandbox may already have been removed by a prior finalization
    attempt. Reuse is allowed only when the artifact proves the exact run,
    producer, supported schema, identity, terminal phase, requested outcome and
    mechanical evidence for that outcome. Duplicate or disagreeing receipts are
    ledger conflicts, never list-order choices.

    ``legacy_v0`` is the one compatibility concession: receipts minted before
    schema versioning omitted ``schema_version`` (and did not copy the complete
    identity into metadata). Their payload and artifact ownership must still
    satisfy every current authority/evidence check. An explicitly supplied
    unknown version is never treated as legacy.
    """
    expected_identity = _operation_closure_identity(record)
    expected_key = _operation_closure_identity_key(expected_identity)
    # 枚举本身失败 ≠ 没有收据。C1 记录层（2026-09-12）的 list_artifacts 只折叠账本行、
    # 坏行跳过，"一个无关的坏 json 打断整份枚举"已不存在；这里还能抛出来的是账本本身
    # 读不了。原先这里回退去扫 state.artifacts_dir——C1 之后没有这个属性，回退恒为空，
    # 读不出就被当成"本作业还没有终态收据"，于是重铸第二份同键收据、此后永久 conflict
    # （2026-09-13 审查）。失败关闭，不重铸。
    try:
        artifacts = state.list_artifacts(
            _EXTERNAL_JOB_OPERATION_CLOSURE_TYPE, own_only=True) or []
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "operation_closure_receipt_audit_failed",
            "error": ("枚举本作业的终态收据失败（记录账本读不出），无法判定是否已有收据；"
                      "不重铸收据，以免同一作业出现两份终态。修复记录账本可读性后重试。"),
            "unreadable_stage": "enumeration",
            "error_type": type(exc).__name__,
            "ownership_undeterminable": True,
        }

    candidates: list[dict[str, Any]] = []
    foreign_receipt_artifact_ids: list[str] = []
    for artifact in artifacts:
        # 空 artifact_id 在真实 State 下构造不出来：core/state.py 的
        # list_artifacts 每条 entry 写死 id=p.stem，p 来自 glob("*.json")，
        # 最极端的文件名（".json" / "..json"）stem 也非空。原先这里为它立了
        # 一堵墙，代价是把一个不可能的状态做成一条模型面拒绝。
        artifact_id = str(artifact.get("id") or "")
        # 解析流水线：记录 → content → payload JSON → payload identity。
        # 任一段读不动都是**同一堵墙**（"本 run 自己的收据不可读"），原先被切成
        # 五处返回、共用同一个 error_code、连 ArtifactRecordUnreadable 这个
        # error_type 字面量都重复写了两次。这里收成一处，把读不动的位置放进
        # unreadable_stage 字段 —— 调用方拿到的信息只多不少，而且一次看全。
        unreadable_stage = ""
        unreadable_error_type = ""
        stored: Any = None
        try:
            stored = state.read_artifact(artifact_id)
        except Exception as exc:
            unreadable_stage = "record_read"
            unreadable_error_type = type(exc).__name__
        if not unreadable_stage and not isinstance(stored, dict):
            unreadable_stage = "record_shape"
            unreadable_error_type = "ArtifactRecordUnreadable"
        # 记录本身读不出来时归属信号一个都拿不到。它已经被枚举成本类型的收据，
        # 因此**是**一条收据声明，只是这次读不出 —— 跳过它就可能重铸出第二份
        # 同 identity 终态收据。判不出归属时按"可能是我的"处理（fail closed）。
        # 与之相对，连产物记录都解析不出来的文件根本没进 artifacts（见上面的
        # 降级枚举），那种不构成声明，跳过。
        ownership_undeterminable = bool(unreadable_stage)
        stored_record = stored if isinstance(stored, dict) else {}
        metadata = stored_record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        metadata_identity = {
            field: metadata.get(field) for field in _OPERATION_CLOSURE_IDENTITY_FIELDS
        }
        metadata_identity_readable = all(
            field in metadata for field in _OPERATION_CLOSURE_IDENTITY_FIELDS
        )
        metadata_matches = bool(
            metadata_identity_readable
            and _operation_closure_identity_key(metadata_identity) == expected_key
        )
        legacy_metadata_shape = bool(
            "schema_version" not in metadata
            and _LEGACY_OPERATION_CLOSURE_METADATA_REQUIRED.issubset(metadata)
            and set(metadata).issubset(
                _LEGACY_OPERATION_CLOSURE_METADATA_ALLOWED)
        )
        legacy_scope_matches = bool(
            legacy_metadata_shape
            and str(metadata.get("scheduler") or "").casefold() == str(
                expected_identity.get("scheduler") or ""
            ).casefold()
            and str(metadata.get("job_id") or "") == str(
                expected_identity.get("job_id") or ""
            )
        )
        attributable_from_metadata = metadata_matches or legacy_scope_matches
        # 归属先于内容裁决（续，#879）：下面三处可读性硬拒原本只看 metadata 是否
        # 指名本 job identity，而 identity 十字段**不含 run id** —— 于是任何一个
        # 历史 run 留下的损坏收据，都会把此后每一个 run 的 finalize 永久钉死。
        # 这里先用框架落盘时写的 produced_by_run_id（模型写不到）判归属：一致地
        # 指向另一个 run 的，它正文坏不坏都不是本 run 的账本事故。
        # payload 此刻还读不出来，所以只用这两个可得信号，且同样要求彼此一致；
        # payload 可读时仍走下面完整的三信号判定，矛盾照样按篡改硬拒。
        stored_run_id = str(stored_record.get("produced_by_run_id") or "")
        metadata_run_id = str(metadata.get("run_id") or "")
        foreign_by_ownership = bool(
            stored_run_id
            and stored_run_id != str(state.run_id)
            and (not metadata_run_id or metadata_run_id == stored_run_id)
        )
        unreadable_is_ours = attributable_from_metadata and not foreign_by_ownership
        note_foreign_unreadable = attributable_from_metadata and foreign_by_ownership
        payload: Any = None
        if not unreadable_stage and "content" not in stored_record:
            unreadable_stage = "content_missing"
            unreadable_error_type = "ArtifactRecordUnreadable"
        if not unreadable_stage:
            try:
                payload = json.loads(str(stored_record.get("content") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                unreadable_stage = "payload_json"
                unreadable_error_type = type(exc).__name__

        payload_identity = (
            payload.get("identity") if isinstance(payload, dict) else None
        )
        payload_identity_readable = bool(
            isinstance(payload_identity, dict)
            and set(payload_identity) == set(_OPERATION_CLOSURE_IDENTITY_FIELDS)
        )
        payload_matches = bool(
            payload_identity_readable
            and _operation_closure_identity_key(payload_identity) == expected_key
        )
        if not unreadable_stage and not payload_identity_readable:
            unreadable_stage = "payload_identity"
            unreadable_error_type = "ReceiptIdentityUnreadable"
        if unreadable_stage:
            if unreadable_is_ours or ownership_undeterminable:
                return {
                    "status": "error",
                    "error_code": "operation_closure_receipt_audit_failed",
                    "error": _OPERATION_RECEIPT_UNREADABLE_MESSAGES[
                        unreadable_stage],
                    "artifact_id": artifact_id,
                    "unreadable_stage": unreadable_stage,
                    "error_type": unreadable_error_type,
                    "ownership_undeterminable": ownership_undeterminable,
                }
            if note_foreign_unreadable:
                foreign_receipt_artifact_ids.append(artifact_id)
            continue
        if (
            legacy_scope_matches
            and not metadata_matches
            and not payload_matches
        ):
            # v0 reused scheduler/job_id across attempts; its complete payload
            # identity positively attributes it to another attempt.
            continue
        if not (payload_matches or metadata_matches):
            continue
        # 归属先于内容裁决（#879 复审第一条）：artifact 目录是**节点**作用域、
        # 跨 run 累积的 —— own_only=True 只说"本节点产出"，不说"本 run 产出"
        # （core/state.py:list_artifacts 的 docstring 原文）。一份三处 run 标识
        # 彼此一致、共同指向**另一个** run 的收据，是那个 run 合法留下的历史
        # 事实，不是本 run 的账本冲突：跳过它，让本 run 按自己的机械证据重新
        # 铸一份。只有三者互相矛盾才是篡改或损坏，留给下面的 violations 硬拒。
        payload_run_id = str(payload.get("run_id") or "")
        coherent_foreign_run = bool(
            stored_run_id
            and payload_run_id
            and stored_run_id == payload_run_id
            and (not metadata_run_id or metadata_run_id == stored_run_id)
            and stored_run_id != str(state.run_id)
        )
        if coherent_foreign_run:
            foreign_receipt_artifact_ids.append(artifact_id)
            continue
        candidates.append({
            "artifact_id": artifact_id,
            "record": stored,
            "payload": payload,
            "metadata": metadata,
        })

    if not candidates:
        absent: dict[str, Any] = {"status": "absent"}
        if foreign_receipt_artifact_ids:
            # 留痕：本 run 没有可复用的收据，但同 identity 下确实存在其他 run
            # 的历史收据 —— 让"为什么重新铸造"可审计，而不是静默重铸。
            absent["foreign_run_receipt_artifact_ids"] = list(
                foreign_receipt_artifact_ids)
        return absent
    if len(candidates) != 1:
        return {
            "status": "error",
            "error_code": "operation_closure_receipt_conflict",
            "error": "同一 external job identity 存在多份 operation 终态收据，拒绝猜测权威版本。",
            "artifact_ids": [item["artifact_id"] for item in candidates],
        }

    candidate = candidates[0]
    stored = candidate["record"]
    payload = candidate["payload"]
    metadata = candidate["metadata"]
    violations: list[str] = []
    if str(stored.get("type") or "") != _EXTERNAL_JOB_OPERATION_CLOSURE_TYPE:
        violations.append("artifact_type")
    if str(stored.get("produced_by_node_type") or "") != "experiment":
        violations.append("produced_by_node_type")
    if str(stored.get("produced_by_run_id") or "") != str(state.run_id):
        violations.append("produced_by_run_id")
    if not isinstance(payload, dict):
        violations.append("content_json")
        payload = {}
    if payload.get("closure_type") != _EXTERNAL_JOB_OPERATION_CLOSURE_KIND:
        violations.append("closure_type")
    payload_has_schema = "schema_version" in payload
    metadata_has_schema = "schema_version" in metadata
    if not payload_has_schema and not metadata_has_schema:
        compatibility_mode = "legacy_v0"
        # Published v0 metadata contained only four authority fields plus
        # two optional class flags. It never contained v1 authority fields.
        if (
            not _LEGACY_OPERATION_CLOSURE_METADATA_REQUIRED.issubset(metadata)
            or not set(metadata).issubset(
                _LEGACY_OPERATION_CLOSURE_METADATA_ALLOWED)
        ):
            violations.append("legacy_v0_metadata_shape")
        if str(metadata.get("scheduler") or "").casefold() != str(
            expected_identity.get("scheduler") or ""
        ).casefold():
            violations.append("metadata_scheduler")
        if str(metadata.get("job_id") or "") != str(
            expected_identity.get("job_id") or ""
        ):
            violations.append("metadata_job_id")
        if metadata.get("outcome") != payload.get("outcome"):
            violations.append("metadata_outcome")
        if metadata.get("execution_class") != payload.get("execution_class"):
            violations.append("metadata_execution_class")
        for flag in ("class_unverified", "class_disputed"):
            if flag in metadata and bool(metadata.get(flag)) != bool(
                payload.get(flag)
            ):
                violations.append(f"metadata_{flag}")
    else:
        schema_version = payload.get("schema_version")
        compatibility_mode = (
            f"schema_v{schema_version}"
            if isinstance(schema_version, int) and not isinstance(schema_version, bool)
            else "schema_unknown"
        )
        metadata_schema_version = metadata.get("schema_version")
        if (
            not payload_has_schema
            or not metadata_has_schema
            or isinstance(schema_version, bool)
            or isinstance(metadata_schema_version, bool)
            or metadata_schema_version != schema_version
            or schema_version not in {
                1, _EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION
            }
        ):
            violations.append("schema_version")
        if metadata.get("closure_type") != payload.get("closure_type"):
            violations.append("metadata_closure_type")
        if str(metadata.get("run_id") or "") != str(payload.get("run_id") or ""):
            violations.append("metadata_run_id")
        if metadata.get("outcome") != payload.get("outcome"):
            violations.append("metadata_outcome")
        if metadata.get("execution_class") != payload.get("execution_class"):
            violations.append("metadata_execution_class")
        for flag in ("class_unverified", "class_disputed"):
            if flag not in metadata or bool(metadata.get(flag)) != bool(
                payload.get(flag)
            ):
                violations.append(f"metadata_{flag}")
        metadata_identity = {
            field: metadata.get(field) for field in _OPERATION_CLOSURE_IDENTITY_FIELDS
        }
        if not (
            all(field in metadata for field in _OPERATION_CLOSURE_IDENTITY_FIELDS)
            and _operation_closure_identity_key(metadata_identity) == expected_key
        ):
            violations.append("metadata_identity")
    if str(payload.get("run_id") or "") != str(state.run_id):
        violations.append("run_id")
    identity = payload.get("identity")
    if not (
        isinstance(identity, dict)
        and set(identity) == set(_OPERATION_CLOSURE_IDENTITY_FIELDS)
        and _operation_closure_identity_key(identity) == expected_key
    ):
        violations.append("identity")
    elif str(identity.get("scheduler") or "").casefold() == "local" and not (
        _LOCAL_CONTAINER_ID.fullmatch(str(identity.get("job_id") or ""))
        and str(identity.get("submission_nonce") or "").strip()
        and _DOCKER_RUNTIME_ID.fullmatch(
            str(identity.get("container_runtime_id") or "")
        )
    ):
        violations.append("local_immutable_identity")
    health = payload.get("health")
    if not (isinstance(health, dict) and
            health.get("scheduler_phase") == "terminal"):
        violations.append("terminal_phase")
    recorded_outcome = payload.get("outcome")
    if recorded_outcome not in _OPERATION_FINALIZE_OUTCOMES:
        violations.append("outcome")
    # outcome 改判先判：它是这一段里唯一原生满足 BF-12 的信封（回吐
    # recorded_outcome / requested_outcome，调用方照它重发即可），不该被后面
    # 那些"这份收据本身不合格"的判据挡在后面。
    # outcome=None：只问「这份收据本身立不立得住、它记的是什么」，不做改判比对。
    # run 级闭环用这个模式消费收据，避免在那边重建一套 outcome 判断。
    if outcome is not None and recorded_outcome != outcome:
        return {
            "status": "error",
            "error_code": "operation_closure_outcome_conflict",
            "error": (
                "同一 external job 已按另一 operation outcome 铸造终态收据，"
                "拒绝改判。改用 recorded_outcome 重新调用本次收尾即可继续。"
            ),
            "artifact_id": candidate["artifact_id"],
            "recorded_outcome": recorded_outcome,
            "requested_outcome": outcome,
        }
    # "这份收据不能作为恢复依据"的三类判据汇总到同一处返回。原先是三处独立
    # 返回、error_code 逐字相同（operation_closure_receipt_invalid）、机制也
    # 相同（violations 列表），调用方只能一条一条打地鼠：修好 identity 再撞
    # witness，修好 witness 再撞 evidence。一次给全。
    evidence_error_code = ""
    completion_postconditions = None
    if (
        recorded_outcome == "operation_completed"
        and compatibility_mode == "schema_v2"
    ):
        completion_postconditions = payload.get("completion_postconditions")
        if not _valid_frozen_completion_postconditions(
            completion_postconditions,
            record,
            str(payload.get("execution_class") or ""),
            receipt_health=payload.get("health"),
        ):
            violations.append("completion_postconditions")
    # Compatibility is intentionally one-way: v1 completed receipts keep
    # their legacy read/replay semantics and are not retroactively hardened
    # with a completion-postcondition view they never froze. New receipts are
    # v2. Delete this read-only branch only after the old-run support window.
    if (
        recorded_outcome == "operation_blocked"
        and compatibility_mode in {"schema_v1", "schema_v2"}
    ):
        # v1 的 blocked 收据用冻结在 payload 里的 witness 自证，不回头查活体
        # blocker 台账（那份是可变的，"收据没变、世界变了"不是收据的问题）。
        if (
            not str(payload.get("note") or "").strip()
            or not _operation_blocker_witness_valid(
                payload.get("independent_blocker_witness"))
        ):
            violations.append("independent_blocker_witness")
    else:
        # A terminal-shaped receipt is not sufficient authority: its stored exit
        # code/error evidence must still support the immutable outcome. Rebuild
        # the small scheduler-result view consumed by the gate used at first mint.
        receipt_health = {
            "status": "success",
            **health,
            "scheduler_result": {
                "raw": {"sandbox_state": {"exit_code": health.get("exit_code")}},
            },
        }
        evidence_error = _operation_outcome_evidence_error(
            state, record, receipt_health, str(recorded_outcome),
            str(payload.get("note") or ""),
            class_unverified=bool(payload.get("class_unverified")),
            class_disputed=bool(payload.get("class_disputed")),
            caller_evidence_matched=(
                (payload.get("caller_evidence") or {}).get("binding")
                == "matched"),
            authoritative_execution_class=str(
                payload.get("persisted_execution_class") or ""),
            completion_postconditions=completion_postconditions,
        )
        if evidence_error is not None:
            violations.append("outcome_evidence")
            evidence_error_code = str(evidence_error.get("error_code") or "")
    if violations:
        invalid: dict[str, Any] = {
            "status": "error",
            "error_code": "operation_closure_receipt_invalid",
            "error": "operation 终态收据不能作为 cleanup 后恢复依据。",
            "artifact_id": candidate["artifact_id"],
            "violations": violations,
        }
        if evidence_error_code:
            invalid["evidence_error_code"] = evidence_error_code
        return invalid
    return {
        "status": "success",
        "artifact_id": candidate["artifact_id"],
        "payload": payload,
        "compatibility_mode": compatibility_mode,
    }


def _existing_operation_closure(
    state: Any, record: dict[str, Any], outcome: str,
) -> str | None:
    """同一 identity+outcome 的有效 closure 只铸一次。"""
    receipt = _operation_closure_receipt(state, record, outcome)
    if receipt.get("status") == "success":
        return str(receipt.get("artifact_id") or "") or None
    return None


def _record_external_job_class_disputed_blocker(
    state: Any, record: dict[str, Any], *,
    disputed_execution_class: str, persisted_execution_class: str, note: str,
) -> dict[str, Any]:
    """为"争议收尾"登记一条框架自有、**不自动消解**的 blocker。

    以 operation_blocked 关掉一个交叉核验一致的 simulation 作业，是拿调用方的
    自述换来的让步：该作业既没有冻结科学证据，也没有 operation 型的机械结论。
    收据是静态的，光有收据的 run 可以在"零 blocker"状态下收工，让步就此消失在
    账本深处。这条 blocker 与 external-job 对账用的那几条不同 —— 没有任何
    resolve/reconcile 路径会删它，它必须由人或上游节点显式处置。
    """
    reported_by = _external_job_class_disputed_reported_by(record)
    blockers = _ensure_blocker_list_for_recording(state)
    blocker = next(
        (
            item for item in blockers
            if isinstance(item, dict) and item.get("reported_by") == reported_by
        ),
        None,
    )
    if blocker is None:
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="external_job",
            summary=(
                "an external job whose submit-time execution_class cross-checks as "
                "simulation was closed with operation_blocked on the caller's claim "
                "that it was not a scientific simulation"
            ),
            requested_action=(
                "audit this closure: either repair the execution_class derivation so "
                "such jobs are no longer derived as simulation, or produce the "
                "scientific analysis this job owed"
            ),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=reported_by,
        )
    blocker.update({
        "reason": "external_job_execution_class_disputed",
        "scheduler": str(record.get("scheduler") or "").lower(),
        "job_id": str(record.get("job_id") or ""),
        "namespace": record.get("namespace"),
        "submission_nonce": record.get("submission_nonce"),
        "container_runtime_id": record.get("container_runtime_id"),
        "persisted_execution_class": str(persisted_execution_class or ""),
        "disputed_execution_class": str(disputed_execution_class or ""),
        "note": str(note or "")[:4000],
    })
    try:
        state.append_transcript(
            "external_job_execution_class_disputed",
            reported_by=reported_by,
            scheduler=blocker["scheduler"],
            job_id=blocker["job_id"],
            submission_nonce=blocker.get("submission_nonce"),
            container_runtime_id=blocker.get("container_runtime_id"),
            persisted_execution_class=blocker["persisted_execution_class"],
            disputed_execution_class=blocker["disputed_execution_class"],
        )
    except Exception:
        pass
    return dict(blocker)


_CROSS_RUN_LEFTOVER_PROJECTION_REASON = (
    "cross_run_leftover_no_submission_in_current_route"
)


def _external_route_submission_presence(
    state: Any, record: dict[str, Any],
) -> dict[str, Any]:
    """本 run 的路线是否拥有该 job 的提交记录；任何读不动都算"确认不了"。"""
    try:
        try:
            from .execution_route import (
                describe_external_route_submission_presence,
            )
        except ImportError:
            from tools.execution_route import (
                describe_external_route_submission_presence,
            )
        presence = describe_external_route_submission_presence(
            state,
            scheduler=str(record.get("scheduler") or ""),
            job_id=str(record.get("job_id") or ""),
            namespace=record.get("namespace"),
            launch_host=record.get("launch_host"),
            scheduler_cluster=record.get("scheduler_cluster"),
            resource_uid=record.get("resource_uid"),
            submission_nonce=record.get("submission_nonce"),
            process_group_id=record.get("process_group_id"),
            process_start_ticks=record.get("process_start_ticks"),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception as exc:
        return {
            "status": "indeterminate",
            "reason": "route_presence_probe_failed",
            "error_type": type(exc).__name__,
        }
    if not isinstance(presence, dict):
        return {
            "status": "indeterminate",
            "reason": "route_presence_probe_invalid_response",
            "response_type": type(presence).__name__,
        }
    return presence


_ROUTE_PRESENCE_EVIDENCE_FIELDS = (
    "route_declared", "attempt_id", "traces",
    "scanned_route_events", "scanned_route_submissions",
    "error_type", "response_type",
)


def _route_ownership_fact(presence: dict[str, Any]) -> dict[str, Any]:
    """收据铸造那一刻**确已发生**的唯一路线事实：归属判定本身。

    它不说投影发生过什么 —— 铸收据时投影还没跑，收据又是 mint-once 的，任何
    对投影结果的预先声明都会被复用路径永久固化成谎。投影结果只由事后追加的
    external_job_route_projection_record 记录。
    """
    return {
        "observed_at": "operation_closure_minted",
        "status": str(presence.get("status") or ""),
        "reason": str(presence.get("reason") or ""),
        "evidence": {
            key: presence.get(key)
            for key in _ROUTE_PRESENCE_EVIDENCE_FIELDS
            if presence.get(key) is not None
        },
        "note": (
            "本字段只记录铸造收据时的归属判定；本次收尾究竟投影了什么，见引用"
            "该收据的 external_job_route_projection_record 与 transcript 事件。"
        ),
    }


def _record_route_projection_skip(
    state: Any, record: dict[str, Any], projection: dict[str, Any], *,
    closure_kind: str, outcome: str, closure_artifact_id: str,
    evidence_artifact_id: str,
) -> dict[str, Any] | None:
    """把"这次收尾没有投影任何路线终态"作为**事后**事实追加落账。

    追加式：不改写任何已冻结的收据，只新铸一条引用它的不可变记录 —— 收据被
    复用时痕迹照样落下，不会因为"同 identity+outcome 只铸一次"而丢失。

    这条留痕是**强保证**：写不下去就返回 None，调用方必须按投影失败处理，
    lifecycle/task 保持开放。尽力而为的 try/except: pass 会让"收尾走完了却查不到
    投影去向"成为可能，那正是这条记录要堵的洞。
    """
    identity = projection.get("external_identity") or _cancellation_identity(record)
    payload = {
        "record_type": "external_route_projection_skipped",
        "closure_kind": str(closure_kind or ""),
        "outcome": str(outcome or ""),
        "identity": identity,
        "projection": {
            key: projection.get(key)
            for key in ("status", "reason", "confirmation",
                        "upstream_status", "upstream_reason")
            if projection.get(key) is not None
        },
        "operation_closure_artifact_id": str(closure_artifact_id or ""),
        "evidence_artifact_id": str(evidence_artifact_id or ""),
        "run_id": state.run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        artifact = state.save_artifact(
            _EXTERNAL_ROUTE_PROJECTION_RECORD_TYPE,
            f"external_job_route_projection_record_{state.run_id}_{time.time_ns()}",
            json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={"scheduler": str(record.get("scheduler") or "").lower(),
                      "job_id": str(record.get("job_id") or ""),
                      "closure_kind": str(closure_kind or ""),
                      "outcome": str(outcome or ""),
                      "operation_closure_artifact_id": str(
                          closure_artifact_id or "")},
        )
        artifact_id = str((artifact or {}).get("id") or "")
        if not artifact_id:
            return None
        state.append_transcript(
            "external_job_route_projection_skipped",
            scheduler=str(record.get("scheduler") or ""),
            job_id=str(record.get("job_id") or ""),
            closure_kind=str(closure_kind or ""),
            outcome=str(outcome or ""),
            reason=projection.get("reason"),
            external_identity=identity,
            confirmation=projection.get("confirmation"),
            upstream_reason=projection.get("upstream_reason"),
            operation_closure_artifact_id=str(closure_artifact_id or ""),
            evidence_artifact_id=str(evidence_artifact_id or ""),
            projection_record_artifact_id=artifact_id,
        )
    except Exception:
        log.warning(
            "unable to append route projection skip record", exc_info=True)
        return None
    return {"projection_record_artifact_id": artifact_id}


def _route_projection_failure_guidance(
    presence: dict[str, Any], route_projection: dict[str, Any], *,
    retry_tool: str,
) -> str:
    """拒绝路径的下一步按**归属结论**分流，不按上游的 reason 字符串分流。

    归属被正面确认为 absent 时，本 run 的路线里根本没有这一步可修 —— identity
    又来自上一个 run 写的 handoff，agent 在本 run 里补不出来。许诺"修复后重试"
    对它是一条走不通的路，必须给跨 run 遗留真正走得通的出路。
    """
    status = str(presence.get("status") or "")
    upstream = str(
        route_projection.get("upstream_reason")
        or route_projection.get("reason") or "") or "unknown"
    if status == "present":
        return (
            "本 run 拥有该提交，但路线终态投影失败；lifecycle/task 保持开放，"
            f"禁止重复提交或重复取消，修复后重试 {retry_tool}。"
        )
    if status == "absent":
        return (
            "该 job 不属于本 run 的路线（归属判定 absent：跨 run 遗留），本 run 的"
            f"路线里没有这一步可以修复重投；投影仍以 {upstream} 失败，"
            "lifecycle/task 保持开放，禁止重复提交或重复取消。出路是补齐该遗留"
            "作业在 handoff task / job_submission 记录里的完整不可变 identity"
            f"（上一个 run 写得不全时无法机械对齐），再重试 {retry_tool}；"
            "identity 补不齐就按完整作业身份人工对账后处置，不要指望在本 run 的"
            "路线里修出这一步。"
        )
    return (
        f"无法正面确认该 job 是否属于本 run 的路线（归属判定 {status or 'unknown'}: "
        f"{presence.get('reason')}），路线终态投影失败（{upstream}）；"
        "lifecycle/task 保持开放，禁止重复提交或重复取消。先让归属可判定（修复"
        "本 run 的路线事件历史，或补齐该 job 的完整 identity），再重试 "
        f"{retry_tool}。"
    )


def _cross_run_leftover_projection(
    record: dict[str, Any], presence: dict[str, Any],
) -> dict[str, Any] | None:
    """跨 run 遗留作业的"无可投影"凭据；确认不了一律 None（fail-closed）。

    本 run 声明过带 external 效应的路线，却机械确认路线里没有该 job 的任何
    痕迹 —— 这一步在本 run 的路线上根本不存在，没有任何终态需要投影。凭据本身
    带上完整 identity 与判定依据，让"这次收尾没有投影任何路线终态"可审计。
    """
    if presence.get("status") != "absent":
        return None
    return {
        "status": "skipped",
        "reason": _CROSS_RUN_LEFTOVER_PROJECTION_REASON,
        "external_identity": (
            presence.get("external_identity")
            or _cancellation_identity(record)
        ),
        "confirmation": {
            key: presence.get(key)
            for key in (
                "status", "reason", "route_declared",
                "scanned_route_events", "scanned_route_submissions",
            )
            if presence.get(key) is not None
        },
    }


def _record_operation_job_closure(
    state: Any, record: dict[str, Any], *, execution_class: str,
    outcome: str, note: str, health: dict[str, Any], class_unverified: bool,
    class_disputed: bool = False, disputed_execution_class: str = "",
    persisted_execution_class: str = "", claimed_execution_class: str = "",
    class_verification_reason: str = "",
    route_ownership: dict[str, Any] | None = None,
    caller_evidence: dict[str, Any] | None = None,
    adopted_from_run_id: str = "",
    completion_postconditions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """铸造不可变的 operation 终态收据（只走 Python 层，不经通用 writer）。

    class_disputed 记的是"以 operation_blocked 关掉一个被持久化为 simulation 的
    作业"这件事：派生规则可能把环境探测一致地误判成 simulation，那种格子里既没有
    科学产物可冻结、也不该为它伪造科学 log。收据必须同时刻下**受管权威值**
    （persisted，交叉核验过）与**调用方本次的主张**（disputed，未经核验），这两个
    值必然不同，审计员读到的才是一份争议记录而非同义反复；同时登记一条不自动
    消解的 blocker，让这次让步在 run 结束前始终挂在账本上。

    class_unverified 记的是另一条 fail-closed 让步路（payload 缺失/歧义/不可识别
    /与 record 自述不一致，一律按 simulation 处理）。它同样是"没有科学证据也没有
    机械结论"的收尾，收据里必须留下与争议路对称的可审计痕迹：class_verification
    刻下核验的两端与失败原因，而不是只留一个布尔。
    """
    if class_disputed:
        _record_external_job_class_disputed_blocker(
            state, record,
            disputed_execution_class=disputed_execution_class,
            persisted_execution_class=persisted_execution_class,
            note=note,
        )
    existing = _operation_closure_receipt(state, record, outcome)
    if existing.get("status") == "error":
        return existing
    if existing.get("status") == "success":
        return {"artifact_id": existing["artifact_id"], "reused": True}
    if (
        outcome == "operation_completed"
        and not _valid_frozen_completion_postconditions(
            completion_postconditions, record, execution_class, receipt_health=health)
    ):
        return {
            "status": "error",
            "error_code": "operation_completion_postconditions_invalid",
            "error": (
                "operation_completed closure 必须冻结通过校验的统一完成后置条件视图；"
                "未铸造收据，也未执行 cleanup。"
            ),
        }
    identity = _operation_closure_identity(record)
    payload = {
        "schema_version": _EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION,
        "closure_type": _EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
        "identity": identity,
        "execution_class": execution_class,
        "class_unverified": bool(class_unverified),
        "class_disputed": bool(class_disputed),
        # 调用方本次附带的科学产物见证。不参与判定，但必须随收据一起冻结：收据
        # 重读时要靠它复原「当初为什么允许铸造」，否则同一份收据会被自己判为无效
        # （投影失败→改路线→重试收尾 这条主路径上当场撞死）。
        **({"caller_evidence": dict(caller_evidence)} if caller_evidence else {}),
        # 收养见证：这份终态是本 run 替另一个 run 的作业写下的。不参与判定，
        # 只让审计一眼读出「执行发生在别处」。
        **({"adopted_from_run_id": adopted_from_run_id}
           if adopted_from_run_id else {}),
        "outcome": outcome,
        "note": str(note or ""),
        "run_id": state.run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "health": {
            "scheduler_phase": health.get("scheduler_phase"),
            "health_state": health.get("health_state"),
            "error_evidence": list(health.get("error_evidence") or []),
            "completion_paths": list(health.get("completion_paths") or []),
            "exit_code": _operation_job_exit_code(health),
            **_termination_receipt_fields(record, health),
        },
        **({
            "completion_postconditions": deepcopy(completion_postconditions)
        } if outcome == "operation_completed" else {}),
    }
    if outcome == "operation_blocked":
        witness = _operation_blocker_witness(
            _ensure_blocker_list_for_recording(state))
        if witness is None:
            # 到不了这里：本函数唯一的调用者在调用前已经跑过
            # _operation_outcome_evidence_error，那道门用
            # _independent_blocker_recorded 判的是**逐字相同**的条件
            # （isinstance(dict) ∧ not reported_by.startswith("framework:")），
            # 而 witness is None ⟺ _canonical_operation_blocker_entries 为空
            # ⟺ 该谓词为 False。两者之间只有 blocker 的新增，没有删除。
            #
            # 原先这里返回一条模型面拒绝信封，比上游那条差 —— 上游会逐字给出
            # 该调用哪个工具、怎么重发，这里只有一句描述。真要守住"不带 witness
            # 的 blocked 收据不可能被写出"这个铸造边界不变量，正确工具是代码
            # 契约（调用方是代码不是模型），不是一个点不出动作的拒绝。
            raise RuntimeError(
                "operation_blocked 收据铸造点缺少 non-framework blocker witness；"
                "上游 _operation_outcome_evidence_error 应已拦下，此处不可达。"
            )
        payload["independent_blocker_witness"] = witness
    if class_disputed:
        payload["persisted_execution_class"] = str(persisted_execution_class or "")
        payload["disputed_execution_class"] = str(disputed_execution_class or "")
        payload["class_dispute_blocker_reported_by"] = (
            _external_job_class_disputed_reported_by(record))
    if class_unverified:
        payload["class_verification"] = {
            "verified": False,
            "claimed_execution_class": str(claimed_execution_class or ""),
            "persisted_execution_class": str(persisted_execution_class or ""),
            "reason": str(class_verification_reason or ""),
        }
    if route_ownership:
        # 只刻"铸造这一刻的归属判定"这件确已发生的事；投影结果由事后追加的
        # external_job_route_projection_record 负责，收据里不预先声明它。
        payload["route_ownership"] = dict(route_ownership)
    artifact = state.save_artifact(
        _EXTERNAL_JOB_OPERATION_CLOSURE_TYPE,
        f"external_job_operation_closure_{state.run_id}_{time.time_ns()}",
        json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={
            **identity,
            "schema_version": _EXTERNAL_JOB_OPERATION_CLOSURE_SCHEMA_VERSION,
            "closure_type": _EXTERNAL_JOB_OPERATION_CLOSURE_KIND,
            "run_id": state.run_id,
            "scheduler": str(record.get("scheduler") or "").lower(),
            "job_id": str(record.get("job_id") or ""),
            "outcome": outcome,
            "execution_class": execution_class,
            "class_unverified": bool(class_unverified),
            "class_disputed": bool(class_disputed),
        },
    )
    return {"artifact_id": artifact.get("id"), "reused": False}


def _finalize_after_cancellation_response(
    state: State, record: dict[str, Any], lifecycle: dict[str, Any], outcome: str,
) -> dict[str, Any]:
    """lifecycle 已是 cancelled/superseded：finalize 不改写（verify 清单 #14）。

    取消确认时已按取消收尾（路线投影、task、cleanup 都在 _close_confirmed_cancellation 里做过）。
    原先这里照常收尾，把 cancelled 改写成 finalized、另铸一份 operation 收据，与 cancel 一侧
    「lifecycle 单调」相悖。返回已有的取消记录，不铸收据、不投影、不清理——不是拒绝，是幂等终态。
    """
    try:
        transaction = _latest_cancellation_transaction(state, record) or {}
    except _CancellationLedgerError:
        transaction = {}
    status = lifecycle.get("status")
    # status 与 cancel 一侧的幂等重放一致（success + idempotent）：hooks 的失败判据只认成功
    # 集合，已取消的作业再收尾不是失败（第三会话复审 0914c P3）。
    return {
        "status": "success",
        "already_cancelled": True,
        "idempotent": True,
        "reason": "external_job_lifecycle_already_cancelled",
        "message": (
            f"该作业的 lifecycle 已是 {status}：取消已确认并按取消收尾，lifecycle 单调、不改写成 "
            f"finalized。本次 finalize（outcome={outcome}）未铸收尾收据、未投影路线、未清理；"
            "不需要再收尾，也不要重提作业。"),
        "scheduler": record.get("scheduler"),
        "job_id": record.get("job_id"),
        "requested_outcome": outcome,
        "lifecycle": lifecycle,
        "cancellation_intent_artifact_id": (
            (transaction.get("intent") or {}).get("intent_artifact_id")),
        "cancellation_outcome_artifact_id": (
            (transaction.get("outcome") or {}).get("outcome_artifact_id")),
        "do_not_resubmit": True,
    }


async def _finalize_external_job(
    state: State, scheduler: str, job_id: str, evidence_artifact_id: str = "",
    outcome: str = "", note: str = "", namespace: str | None = None,
    disputed_execution_class: str = "", **_: Any,
) -> dict:
    """Close a handoff by what this job did, not by what kind of run it sits in.

    作业按 submit 时持久化并交叉核验的 execution_class 分流：simulation 只收
    analyzed_*（完整冻结科学证据链一分不松）；diagnostic/toolchain_build 只收
    operation_* 并由本工具依机械证据铸造 external_job_operation_closure。

    **本函数不读 run 的 execution_mode。** 过去它读：运维模式下唯一被允许的证据只能
    由 record_operation_completion 铸造，而那是 run 级单向门（一调用即封口，封口后
    路线不可修订）。于是收尾要先封口、封口后修不了路线、路线修不了又过不了收尾——
    四条各自正确的规则合成没有出口。2026-09-08 与 09-09 两次活体各复现一次；09-08
    另一次能跑通，靠的是把运维任务误分类成科学任务从而绕开这堵墙，也就是分类正确
    反而没有出路。一个作业怎样结束，取决于它自己发生了什么，不取决于它所在的 run
    是什么身份；run 的身份只决定 run 级闭环消费这些收据的方式。
    """
    evidence_artifact_id = str(evidence_artifact_id or "")
    outcome = str(outcome or "")
    if outcome not in _ANALYZED_FINALIZE_OUTCOMES | _OPERATION_FINALIZE_OUTCOMES:
        return {"status": "error", "error": (
            "outcome 必须是 analyzed_success、analyzed_failure、analyzed_inconclusive"
            "（simulation 类 job），或 operation_completed、operation_failed、"
            "operation_blocked（submit 时派生为 diagnostic/toolchain_build 的 "
            "operation 类 job）")}
    if outcome in _ANALYZED_FINALIZE_OUTCOMES and not evidence_artifact_id:
        return {"status": "error", "error": (
            "analyzed_* 结局必须提供 evidence_artifact_id：先铸造 frozen 非自动 "
            "experiment_log（metadata.external_job_refs 复制完整 job identity），"
            "再重新调用 finalize_external_job 并带上该 artifact id")}
    record = _external_job_record(state, scheduler, job_id, namespace)
    if record is None:
        return {"status": "error",
                "error": ("未找到唯一受管 external job 记录；同 scheduler/job_id "
                          "跨 scope 时必须提供 namespace")}
    lifecycle_resolution = lifecycle_for_submission(state, record)
    if lifecycle_resolution.get("resolution") == "legacy_lifecycle_scope_ambiguous":
        return {"status": "error",
                "error": "legacy_lifecycle_scope_ambiguous: manual scope confirmation is required before finalization",
                "lifecycle": lifecycle_resolution}
    if lifecycle_resolution.get("status") in _CANCELLED_JOB_STATES:
        return _finalize_after_cancellation_response(
            state, record, lifecycle_resolution, outcome)
    # 收养判定：这条待办来自别的 run。任务清单是项目级的，框架按 owner 注入同项目
    # 的任何后续 experiment run，于是新 run 会看到并关掉历史遗留作业。关掉是对的
    # （开放作业不该永远悬空），但账本上必须读得出「执行发生在别处」。
    adopted_origin = str(record.get("handoff_origin_run_id") or "")
    if adopted_origin == str(state.run_id):
        adopted_origin = ""
    operation_branch = False
    class_unverified = False
    class_fields: dict[str, Any] = {}
    # 无条件按作业自己的 execution_class 分流。此处原本裹着 `if not operational:`，
    # 把机械收据这条路整条关给运维模式的 run —— 那正是死路的开关。
    job_class = _finalize_job_class(state, record)
    class_unverified = not job_class["verified"]
    class_fields = {
        "job_execution_class": job_class["execution_class"],
        "class_unverified": class_unverified,
    }
    if adopted_origin:
        # 放在类别派生之后：上面那句是整体赋值，写在它之前会被冲掉。
        class_fields["adopted_from_run_id"] = adopted_origin
    if class_unverified:
        # 搁浅路与争议路一样是让步，收据里必须能读出核验的两端与失败原因。
        class_fields["claimed_execution_class"] = (
            job_class["claimed_execution_class"])
        class_fields["persisted_execution_class"] = (
            job_class["persisted_execution_class"])
        class_fields["class_verification_reason"] = job_class["reason"]
    if outcome in _OPERATION_FINALIZE_OUTCOMES:
        if job_class["verified"] and job_class["execution_class"] == "simulation":
            # 硬拦截只覆盖 operation 型的成功/失败宣称：那才是"用运维口径
            # 替科学作业下结论"。operation_blocked 不宣称任何作业结果，只登记
            # "被挡住了"这一事实，必须留给它出路 —— 否则被派生规则一致地误判
            # 成 simulation 的环境探测（class_unverified=false，交叉核验一致）
            # 会零出路，只剩"给探测铸一份冻结科学 log"这条更坏的路。
            if outcome != "operation_blocked":
                return {
                    "status": "error",
                    "error_code": "simulation_job_requires_analyzed_outcome",
                    "error": (
                        "该 job 在 submit 时持久化的 execution_class=simulation，"
                        "科学收尾不可用 operation_completed/operation_failed 绕过："
                        "先铸造 frozen 非自动 experiment_log（"
                        "metadata.external_job_refs 复制受管记录的完整 identity），"
                        "再调用 finalize_external_job("
                        f'scheduler="{scheduler}", job_id="{job_id}", '
                        'evidence_artifact_id="<log id>", outcome="analyzed_success|'
                        'analyzed_failure|analyzed_inconclusive")。若该 job 事实上'
                        "不是科学模拟（例如环境探测/依赖安装被派生规则误判），"
                        "不得为它伪造科学 experiment_log：按序调用 "
                        'report_blocker(summary="<阻塞事实>", '
                        'requested_action="<需要谁做什么>") 后 '
                        f'finalize_external_job(scheduler="{scheduler}", '
                        f'job_id="{job_id}", outcome="operation_blocked", '
                        'disputed_execution_class="<diagnostic|toolchain_build>", '
                        'note="<阻塞原因>")，closure 会打 class_disputed 标记、'
                        "并登记一条不自动消解的 blocker 留待审计。"),
                    **class_fields,
                }
            # 争议主张必须是调用方本次显式给出的**另一个**类别：拿受管记录
            # 里那个已被交叉核验为 simulation 的值回填只会写出
            # claimed==persisted 的同义反复，审计员从中读不出任何争议内容。
            disputed_class = str(
                disputed_execution_class or "").strip().lower()
            if disputed_class not in _OPERATION_EXECUTION_CLASSES:
                return {
                    "status": "error",
                    "error_code": "class_dispute_requires_explicit_claim",
                    "error": (
                        "以 operation_blocked 关掉一个持久化并交叉核验为 "
                        "simulation 的作业，必须显式声明你主张的类别："
                        f'重新调用 finalize_external_job(scheduler="{scheduler}", '
                        f'job_id="{job_id}", outcome="operation_blocked", '
                        'disputed_execution_class="diagnostic" 或 '
                        '"toolchain_build", note="<该作业为何不是科学模拟>")。'
                        "该主张只作为争议记录写进 closure，不会改变受管记录里"
                        "持久化的 execution_class。"),
                    **class_fields,
                }
            class_fields["class_disputed"] = True
            class_fields["disputed_execution_class"] = disputed_class
            class_fields["persisted_execution_class"] = job_class["execution_class"]
        operation_branch = True
        if evidence_artifact_id:
            # 曾经这里直拒。拒绝本身没错——operation 类作业的终态确实由本工具依机械
            # 证据铸造，调用方给的科学产物不参与判定——但它与「运维 run 必须提供
            # evidence_artifact_id」那道墙互为死结：一边要求给，另一边收到就拒。
            # 2026-09-08 活体里模型正是在这两句之间来回撞。
            #
            # 现在改为见证：产物不参与任何判定，只把「调用方给了什么、它与本作业
            # 身份对不对得上」如实刻进收据。判定权仍在机械证据矩阵，出处校验也没有
            # 丢——身份诊断照跑，结果原样入账。
            caller_evidence: dict[str, Any] = {"artifact_id": evidence_artifact_id}
            log_record = _frozen_experiment_log(state, evidence_artifact_id)
            if log_record is None:
                caller_evidence["binding"] = "not_a_frozen_experiment_log"
            else:
                diagnostic = _external_job_evidence_identity_diagnostic(
                    log_record, record)
                caller_evidence["binding"] = (
                    "matched" if diagnostic is None else "mismatch")
                if diagnostic is not None:
                    caller_evidence["diagnostic"] = diagnostic
            class_fields["caller_evidence"] = caller_evidence
    elif job_class["verified"] and (
            job_class["execution_class"] in _OPERATION_EXECUTION_CLASSES):
        return {
            "status": "error",
            "error_code": "operation_job_rejects_analyzed_outcome",
            "error": (
                f"该 job 在 submit 时持久化的 execution_class="
                f"{job_class['execution_class']}，是 operation 型 job，"
                "不得借科学 experiment_log 走 analyzed_* 收尾："
                "省略 evidence_artifact_id 重新调用 finalize_external_job("
                f'scheduler="{scheduler}", job_id="{job_id}", '
                'outcome="operation_completed|operation_failed|operation_blocked")'),
            **class_fields,
        }
    namespace = namespace or record.get("namespace")
    operation_receipt = {"status": "absent"}
    # 收据复用对全部调度器生效：本地沙箱会被上一次 finalize 删掉，远端作业同样会被调度器
    # 回收或清除（k8s TTL、PBS 历史、slurm purge）。原先只限 local，远端作业一被遗忘，
    # 取消守卫读到收据说"去收尾"，收尾却不认收据说"状态未知"，两边互相指（2026-09-13 审查）。
    if operation_branch:
        operation_receipt = _operation_closure_receipt(
            state, record, outcome)
        if operation_receipt.get("status") == "error":
            return operation_receipt
        if operation_receipt.get("status") == "success":
            receipt_payload = operation_receipt["payload"]
            receipt_class_violations = []
            if str(receipt_payload.get("execution_class") or "") != str(
                class_fields.get("job_execution_class") or ""
            ):
                receipt_class_violations.append("execution_class")
            if bool(receipt_payload.get("class_unverified")) != class_unverified:
                receipt_class_violations.append("class_unverified")
            if bool(receipt_payload.get("class_disputed")) != bool(
                class_fields.get("class_disputed")
            ):
                receipt_class_violations.append("class_disputed")
            if receipt_class_violations:
                # 这里比的**不是**一条记录内部的两处字段，而是两个时刻的两次派生：
                # 收据刻的是铸造那一刻的类别事实（不可变），class_fields 是本次
                # 调用现算的 —— _finalize_job_class 拿 record.execution_class 做
                # claimed，而 record 的候选集包含模型可写的 task 行（测试助手
                # _mutate_record_execution_class 的 docstring 自陈"行自述被改写
                # 是真实可达的状态"）。两者不同不代表账本自相矛盾。
                #
                # 而拒绝在这里是**无出口死路**：收据不可变、改不了；唯一的 run 内
                # 出口是把错误的那份类别事实重新写回受管记录 —— 一堵只能靠恢复
                # 谎言才能穿过的墙，不可能是记录完整性防线。节点规则要求每个可达
                # 非终态至少有一个合法出口。
                #
                # 改为：两次派生的值都如实刻进 finalize 记录，挂一条**不自动
                # 消解**的 class 分歧 blocker（与 _record_external_job_class_disputed_blocker
                # 同族：没有任何 resolve/reconcile 路径会删它，必须由人或上游
                # 节点显式处置），然后按**收据里那份冻结的类别**继续收尾。
                class_fields["operation_receipt_class_divergence"] = list(
                    receipt_class_violations)
                class_fields["receipt_frozen_execution_class"] = str(
                    receipt_payload.get("execution_class") or "")
                class_fields["recomputed_execution_class"] = str(
                    class_fields.get("job_execution_class") or "")
                from core.blockers import record_blocker

                divergence_reported_by = (
                    f"{_EXTERNAL_JOB_CLASS_DIVERGENCE_PREFIX}"
                    f"{hashlib.sha256(_job_key_for_record(record).encode('utf-8')).hexdigest()[:24]}"
                )
                existing_blockers = _ensure_blocker_list_for_recording(state)
                if not any(
                    isinstance(item, dict)
                    and item.get("reported_by") == divergence_reported_by
                    for item in existing_blockers
                ):
                    record_blocker(
                        state,
                        category="external_job",
                        summary=(
                            "an immutable operation closure receipt disagrees with the "
                            "freshly recomputed job class; the receipt is authoritative "
                            "and the divergence needs explicit disposition"
                        ),
                        detail=json.dumps({
                            "artifact_id": operation_receipt["artifact_id"],
                            "diverging_fields": receipt_class_violations,
                            "receipt_frozen_execution_class":
                                class_fields["receipt_frozen_execution_class"],
                            "recomputed_execution_class":
                                class_fields["recomputed_execution_class"],
                        }, ensure_ascii=False, sort_keys=True),
                        reported_by=divergence_reported_by,
                    )
                # 收据是权威：它铸造时经过完整证据校验且不可变。
                class_fields["job_execution_class"] = class_fields[
                    "receipt_frozen_execution_class"]
            class_fields["operation_closure_compatibility_mode"] = str(
                operation_receipt.get("compatibility_mode") or "")
            # 收据铸造后，重试不能再改写已记录的终态说明。
            note = str(receipt_payload.get("note") or "")
            health = {
                "status": "success",
                **dict(receipt_payload["health"]),
            }
        else:
            health = freeze_first_terminal_output_observation(
                state, record, probe_external_job_health(state, scheduler, job_id, namespace))
    else:
        # scientific 收尾不能拿 operation 收据代替自身的实时证据核验。
        health = freeze_first_terminal_output_observation(
            state, record, probe_external_job_health(state, scheduler, job_id, namespace))
    if health.get("status") != "success":
        return health
    observed_sandbox = (
        ((health.get("scheduler_result") or {}).get("raw") or {}).get("sandbox_state")
        or {})
    local_record_missing = bool(
        str(scheduler).lower() == "local" and observed_sandbox.get("exists") is False)
    if local_record_missing or health.get("scheduler_phase") == "unknown":
        # 本地记录缺失时 core 报 NOT_RUNNING，看起来像已结束——但记录没了不等于进程停了。
        return _external_job_state_unknown_exit(
            record, source="finalize_external_job",
            observation=_job_state_unknown_observation(scheduler, health))
    if health.get("scheduler_phase") != "terminal":
        return {"status": "error",
                "error": ("job 仍在运行；不能关闭 workflow。等它结束后再收尾，"
                          "或用 cancel_job 取消。"),
                "health": health}
    # 判决拆除二审（rm:2560 保留·升 B，2026-08-31，呈裁①定案）：finalize 成功即
    # 销毁本地容器与 control dir（见下方 stop_container/cleanup_control_dir）——
    # 降格等于允许先毁证后补析；auto log 撑 analyzed_success 是出处伪造。
    # 账本真实性墙（B 类），保留。
    #
    # 合并 2026-09-03：本分支把「正文含 job_id 子串」换成了结构化身份比对
    # （external_job_refs 精确匹配完整不可变 identity）。两者谓词不同、不是二选一：
    # 结构化身份门保留（真 B：证明这份 log 指的就是这个作业），而 rm:2563 降格的
    # 对价 —— 正文未提 job_id 时的弱证据记账 —— 一并移植，不能因为立了更硬的墙
    # 就把上游降格换来的记账义务丢掉。
    evidence_ref = evidence_artifact_id
    closure_witness: dict[str, Any] = {}
    completion_postconditions: dict[str, Any] | None = None
    if operation_branch:
        if operation_receipt.get("status") == "success":
            completion_postconditions = receipt_payload.get(
                "completion_postconditions"
            )
        else:
            if outcome == "operation_completed":
                completion_postconditions = _operation_completion_postconditions(
                    state,
                    record,
                    health,
                    str(class_fields.get("job_execution_class") or ""),
                )
            gate_error = _operation_outcome_evidence_error(
                state,
                record,
                health,
                outcome,
                note,
                class_unverified=class_unverified,
                class_disputed=bool(class_fields.get("class_disputed")),
                caller_evidence_matched=(
                    (class_fields.get("caller_evidence") or {}).get("binding")
                    == "matched"
                ),
                authoritative_execution_class=str(
                    class_fields.get("persisted_execution_class") or ""
                ),
                completion_postconditions=completion_postconditions,
            )
            if gate_error is not None:
                if (
                    outcome == "operation_completed"
                    and gate_error.get("completion_postconditions") is not None
                ):
                    route_source = (
                        (completion_postconditions.get("sources") or {}).get("route")
                        or {}
                    )
                    if (
                        gate_error.get("route_correction_available") is True
                        and route_source.get('status') == "ready"
                        and route_source.get("declared") is True
                        and route_source.get("passed") is not True
                        and route_source.get('missing_expected_outputs')
                    ):
                        persisted_route = _persist_route_completion_postcondition(
                            state, record, health, completion_postconditions
                        )
                        if (
                            isinstance(persisted_route, dict)
                            and persisted_route.get("status") != "success"
                            and persisted_route.get("reason")
                            != "route_expected_outputs_missing"
                        ):
                            return {
                                "status": "error",
                                "error_code": (
                                    "operation_completion_postcondition_"
                                    "persistence_failed"
                                ),
                                "error": (
                                    "route expected_outputs 已机械判为缺失，但"
                                    " expected_outputs_missing 事实未能持久化；"
                                    "因此 correction basis 尚不存在。不要调用"
                                    " declare_execution_route 纠正；先修复持久化/"
                                    "身份问题后重试，或 report_blocker 后以"
                                    " operation_blocked 诚实收尾。"
                                ),
                                "route_persistence_error": persisted_route,
                                "completion_postconditions": (
                                    completion_postconditions
                                ),
                                "health": health,
                                **class_fields,
                            }
                return {**gate_error, "health": health, **class_fields}
            if outcome == "operation_completed":
                route_source = (
                    (completion_postconditions.get("sources") or {}).get("route")
                    or {}
                )
                if (
                    route_source.get('status') == "ready"
                    and route_source.get("declared") is True
                    and route_source.get("passed") is True
                ):
                    persisted_route = _persist_route_completion_postcondition(
                        state, record, health, completion_postconditions
                    )
                    if (
                        isinstance(persisted_route, dict)
                        and persisted_route.get("status") != "success"
                    ):
                        return {
                            "status": "error",
                            "error_code": (
                                "operation_completion_postcondition_persistence_failed"
                            ),
                            "error": (
                                "完成后置条件已经通过，但同一份 route observation "
                                "无法持久化；未铸造 closure，也未执行 cleanup。"
                            ),
                            "route_persistence_error": persisted_route,
                            "completion_postconditions": completion_postconditions,
                            "health": health,
                            **class_fields,
                        }
    else:
        evidence = _frozen_experiment_log(state, evidence_artifact_id)
        if evidence is None:
            return {"status": "error", "error": "evidence_artifact_id 必须是本次分析产生的 frozen、非自动 experiment_log"}
        identity_diagnostic = _external_job_evidence_identity_diagnostic(evidence, record)
        if identity_diagnostic is not None:
            # 此处原本对 operational run 覆写 recovery 文案，把模型指向
            # record_operation_completion —— 而那是 run 级单向门，正是死路的入口。
            # 现在收尾不再依赖 run 级产物，科学侧的 another/amended log 指引对所有
            # run 都成立，不再分叉。
            return {
                "status": "error",
                "error_code": "external_job_evidence_identity_mismatch",
                "error": (
                    "frozen experiment_log 的 metadata.external_job_refs 未精确匹配 "
                    "受管 external job 的完整不可变 identity"
                ),
                "health": health,
                **identity_diagnostic,
            }
        # 判决拆除 O6（rm:2563 降格，2026-08-31）：「正文含 job_id 子串」是字符串
        # 代理，不再当状态转移许可；照关，弱证据如实进 finalize 记录。
        if str(job_id) not in str(evidence.get("content") or ""):
            closure_witness["job_id_not_referenced_in_log"] = {
                "job_id": str(job_id), "evidence_artifact_id": evidence_artifact_id}
            try:
                state.append_transcript(
                    "external_job_closure_evidence_weak",
                    job_id=str(job_id), evidence_artifact_id=evidence_artifact_id,
                    reason="frozen experiment_log does not mention the job_id")
            except Exception:
                pass
    if operation_receipt.get("status") != "success":
        try:
            _persist_execution_environment_evidence(state, record, "terminal")
        except Exception:
            log.warning("unable to persist terminal execution environment evidence", exc_info=True)
    _ensure_blocker_list_for_recording(state)
    # 归属判定先于收据：它是铸收据那一刻确已发生的事实，可以刻进收据；
    # 投影结果那时还没发生，绝不能预先声明（收据是 mint-once 的）。
    route_presence = _external_route_submission_presence(state, record)
    operation_closure: dict[str, Any] | None = None
    if operation_branch:
        if operation_receipt.get("status") == "success":
            operation_closure = {
                "artifact_id": operation_receipt["artifact_id"],
                "reused": True,
            }
        else:
            operation_closure = _record_operation_job_closure(
                state, record,
                route_ownership=_route_ownership_fact(route_presence),
                execution_class=str(class_fields.get("job_execution_class") or ""),
                outcome=outcome, note=note, health=health,
                class_unverified=class_unverified,
                class_disputed=bool(class_fields.get("class_disputed")),
                disputed_execution_class=str(
                    class_fields.get("disputed_execution_class") or ""),
                persisted_execution_class=str(
                    class_fields.get("persisted_execution_class") or ""),
                claimed_execution_class=str(
                    class_fields.get("claimed_execution_class") or ""),
                class_verification_reason=str(
                    class_fields.get("class_verification_reason") or ""),
                caller_evidence=class_fields.get("caller_evidence"),
                adopted_from_run_id=adopted_origin,
                completion_postconditions=completion_postconditions,
            )
        if operation_closure.get("status") == "error":
            return operation_closure
        evidence_ref = str(operation_closure.get("artifact_id") or "")
        class_fields["operation_closure_artifact_id"] = evidence_ref
    cleanup = {"status": "not_required", "scheduler": str(scheduler).lower()}
    if str(scheduler).lower() == "local":
        cleanup = _cleanup_local_job_for_finalization(record)
        if cleanup.get("status") != "success":
            blocker = _record_finalized_needs_cleanup_blocker(
                state, record, cleanup,
            )
            return {
                "status": "finalized_needs_cleanup",
                "error": ("local sandbox cleanup failed before workflow closure; "
                          "the lifecycle and task ledgers remain open"),
                "scheduler": scheduler,
                "job_id": job_id,
                "workflow_status": "awaiting_cleanup",
                "outcome": outcome,
                "evidence_artifact_id": evidence_ref,
                **class_fields,
                "cleanup": cleanup,
                "blocker": blocker,
                "do_not_resubmit": True,
            }

    # Route projection is an authoritative terminal fact.  It must be durable
    # before either the lifecycle ledger or the follow-up task becomes closed.
    try:
        try:
            from .execution_route import record_external_route_finalization
        except ImportError:
            from tools.execution_route import record_external_route_finalization
        route_projection = record_external_route_finalization(
            state,
            scheduler=scheduler,
            job_id=job_id,
            namespace=record.get("namespace"),
            launch_host=record.get("launch_host"),
            scheduler_cluster=record.get("scheduler_cluster"),
            resource_uid=record.get("resource_uid"),
            submission_nonce=record.get("submission_nonce"),
            process_group_id=record.get("process_group_id"),
            process_start_ticks=record.get("process_start_ticks"),
            container_runtime_id=record.get("container_runtime_id"),
            domain_outcome=outcome,
            evidence_artifact_id=evidence_ref,
            termination_matched=(
                _termination_verdict(record, health).get("termination_matched") is True),
        )
        if not isinstance(route_projection, dict):
            route_projection = {
                "status": "error",
                "reason": "route_external_projection_invalid_response",
                "response_type": type(route_projection).__name__,
            }
        elif route_projection.get("status") not in _CLOSABLE_ROUTE_PROJECTION_STATUSES:
            route_projection = {
                **route_projection,
                "status": "error",
                "reason": "route_external_projection_not_durable",
                "upstream_status": route_projection.get("status"),
                "upstream_reason": route_projection.get("reason"),
            }
    except Exception as exc:
        route_projection = {
            "status": "error",
            "reason": "route_external_projection_failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
        }
    leftover_projection = _cross_run_leftover_projection(record, route_presence)
    if (
        route_projection.get("status") == "error"
        and leftover_projection is not None
        and str(route_projection.get("upstream_reason") or "")
        == "route_submission_not_found"
    ):
        # 跨 run 遗留作业：本 run 的路线里根本没有这一步，投影是 no-op 成功。
        # 只有"投影失败的原因正是找不到提交"且归属被正面确认过，才走这条路。
        skipped = {
            **leftover_projection,
            "upstream_status": route_projection.get("upstream_status"),
            "upstream_reason": route_projection.get("upstream_reason"),
        }
        appended = _record_route_projection_skip(
            state, record, skipped, closure_kind="finalize", outcome=outcome,
            closure_artifact_id=str(
                (operation_closure or {}).get("artifact_id") or ""),
            evidence_artifact_id=evidence_ref,
        )
        if appended is None:
            # 留痕落不下去，就不能凭这条 no-op 关掉作业：否则收尾走完了，
            # "这次没有投影任何路线终态"却查无实据。
            route_projection = {
                **route_projection,
                "reason": "route_projection_skip_record_not_durable",
                "skipped_projection": skipped,
            }
        else:
            route_projection = {**skipped, **appended}
    if route_projection.get("status") == "error":
        route_projection = {**route_projection, "ownership": route_presence}
        blocker = _record_finalized_needs_route_blocker(
            state, record, route_projection,
            outcome=outcome, evidence_artifact_id=evidence_ref,
        )
        return {
            "status": "finalized_needs_route_reconciliation",
            "error": _route_projection_failure_guidance(
                route_presence, route_projection,
                retry_tool="finalize_external_job"),
            "scheduler": scheduler,
            "job_id": job_id,
            "workflow_status": "awaiting_route_projection",
            "outcome": outcome,
            "evidence_artifact_id": evidence_ref,
            **class_fields,
            "route_projection": route_projection,
            "cleanup": cleanup,
            "blocker": blocker,
            "do_not_resubmit": True,
        }

    task_completion = _complete_handoff_tasks(
        state, scheduler, job_id,
        f"{outcome}; evidence={evidence_ref}; {note}".strip(),
        namespace=record.get("namespace"), launch_host=record.get("launch_host"),
        scheduler_cluster=record.get("scheduler_cluster"),
        resource_uid=record.get("resource_uid"),
        submission_nonce=record.get("submission_nonce"),
        process_group_id=record.get("process_group_id"),
        process_start_ticks=record.get("process_start_ticks"),
        container_runtime_id=record.get("container_runtime_id"),
    )
    if not isinstance(task_completion, dict):
        task_completion = {
            "status": "error",
            "reason": "handoff_task_completion_invalid_response",
            "response_type": type(task_completion).__name__,
        }
    elif task_completion.get("status") != "success":
        task_completion = {
            **task_completion,
            "status": "error",
            "reason": (
                task_completion.get("reason")
                or "handoff_task_completion_not_durable"
            ),
            "upstream_status": task_completion.get("status"),
        }
    if task_completion.get("status") == "error":
        blocker = _record_external_job_needs_task_blocker(
            state, record, task_completion, closure_kind="finalize",
            outcome=outcome, evidence_artifact_id=evidence_ref,
        )
        return {
            "status": "finalized_needs_task_reconciliation",
            "error": (
                "匹配的 handoff task 未能可靠完成；lifecycle 保持开放，"
                "禁止重复提交，修复 task ledger 后重试 finalize_external_job。"
            ),
            "scheduler": scheduler,
            "job_id": job_id,
            "workflow_status": "awaiting_task_completion",
            "outcome": outcome,
            "evidence_artifact_id": evidence_ref,
            **class_fields,
            "route_projection": route_projection,
            "task_completion": task_completion,
            "cleanup": cleanup,
            "blocker": blocker,
            "do_not_resubmit": True,
        }

    lifecycle = _record_job_lifecycle(
        state, scheduler=scheduler, job_id=job_id, lifecycle_status="finalized",
        reason=f"{outcome}: {note}".strip(), namespace=record.get("namespace"),
        launch_host=record.get("launch_host"),
        scheduler_cluster=record.get("scheduler_cluster"),
        resource_uid=record.get("resource_uid"),
        submission_nonce=record.get("submission_nonce"),
        process_group_id=record.get("process_group_id"),
        process_start_ticks=record.get("process_start_ticks"),
        container_runtime_id=record.get("container_runtime_id"),
    )
    active_handoff_markers = _external_handoff_markers_after_finalization(
        state, record,
    )
    _resolve_finalized_needs_cleanup_blocker(state, record)
    _resolve_finalized_needs_route_blocker(state, record)
    _resolve_external_job_needs_task_blocker(state, record)
    _resolve_cancellation_blockers_after_finalize(state, record)
    _reconcile_external_job_handoff_blockers(state, active_handoff_markers)
    try:
        state.append_transcript(
            "external_job_workflow_finalized", scheduler=scheduler,
            job_id=job_id, outcome=outcome,
            evidence_artifact_id=evidence_ref,
            **class_fields,
            **({"closure_witness": closure_witness} if closure_witness else {}),
        )
    except Exception:
        pass
    return {
        "status": "success", "scheduler": scheduler, "job_id": job_id,
        "workflow_status": "finalized", "outcome": outcome,
        "evidence_artifact_id": evidence_ref, **class_fields,
        "lifecycle": lifecycle,
        "route_projection": route_projection, "cleanup": cleanup,
        "task_completion": task_completion,
        **({"closure_witness": closure_witness} if closure_witness else {}),
    }


def _project_external_cancellation(
    state: State, record: dict[str, Any], *, evidence_artifact_id: str,
    planned_stop: bool = False,
) -> dict[str, Any]:
    """Project cancellation with the same complete scheduler scope as submission.

    与 finalize 同族：跨 run 遗留作业在本 run 的路线里没有提交，取消也没有任何
    路线终态可投影 —— 归属被正面确认为 absent 时按 no-op 处理并事后追加留痕。
    本 run 拥有该提交的取消投影要求一分不放宽。
    """
    presence = _external_route_submission_presence(state, record)
    try:
        try:
            from .execution_route import record_external_route_finalization
        except ImportError:
            from tools.execution_route import record_external_route_finalization
        projection = record_external_route_finalization(
            state,
            scheduler=str(record.get("scheduler") or ""),
            job_id=str(record.get("job_id") or ""),
            namespace=record.get("namespace"),
            launch_host=record.get("launch_host"),
            scheduler_cluster=record.get("scheduler_cluster"),
            resource_uid=record.get("resource_uid"),
            submission_nonce=record.get("submission_nonce"),
            process_group_id=record.get("process_group_id"),
            process_start_ticks=record.get("process_start_ticks"),
            container_runtime_id=record.get("container_runtime_id"),
            domain_outcome="stopped_as_planned" if planned_stop else "cancelled",
            evidence_artifact_id=evidence_artifact_id,
        )
        if not isinstance(projection, dict):
            projection = {
                "status": "error",
                "reason": "route_external_projection_invalid_response",
                "response_type": type(projection).__name__,
            }
        elif projection.get("status") not in _CLOSABLE_ROUTE_PROJECTION_STATUSES:
            projection = {
                **projection,
                "status": "error",
                "reason": "route_external_projection_not_durable",
                "upstream_status": projection.get("status"),
                "upstream_reason": projection.get("reason"),
            }
    except Exception as exc:
        projection = {
            "status": "error",
            "reason": "route_external_projection_failed",
            "error_type": type(exc).__name__,
        }
    if projection.get("status") != "error":
        return projection
    leftover_projection = _cross_run_leftover_projection(record, presence)
    if (
        leftover_projection is not None
        and str(projection.get("upstream_reason") or "")
        == "route_submission_not_found"
    ):
        skipped = {
            **leftover_projection,
            "upstream_status": projection.get("upstream_status"),
            "upstream_reason": projection.get("upstream_reason"),
        }
        appended = _record_route_projection_skip(
            state, record, skipped, closure_kind="cancel", outcome="cancelled",
            closure_artifact_id="", evidence_artifact_id=evidence_artifact_id,
        )
        if appended is not None:
            return {**skipped, **appended}
        projection = {
            **projection,
            "reason": "route_projection_skip_record_not_durable",
            "skipped_projection": skipped,
        }
    return {**projection, "ownership": presence}


_CANCELLATION_ROUTE_BLOCKER_PREFIX = (
    "framework:cancelled_needs_route_reconciliation:"
)


def _cancellation_route_reported_by(record: dict[str, Any]) -> str:
    identity = _cancellation_identity_key(_cancellation_identity(record))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{_CANCELLATION_ROUTE_BLOCKER_PREFIX}{digest[:24]}"


def _record_cancellation_route_blocker(
    state: State, record: dict[str, Any], route_projection: dict[str, Any],
    *, evidence_artifact_id: str,
) -> dict[str, Any]:
    reported_by = _cancellation_route_reported_by(record)
    blockers = _ensure_blocker_list_for_recording(state)
    blocker = next(
        (
            item for item in blockers
            if isinstance(item, dict) and item.get("reported_by") == reported_by
        ),
        None,
    )
    if blocker is None:
        from core.blockers import record_blocker

        blocker = record_blocker(
            state,
            category="external_job",
            summary=(
                "external job cancellation is confirmed but route projection failed"
            ),
            requested_action=(
                "retry cancel_job to replay route projection only; do not cancel or "
                "submit the external job again"
            ),
            suggested_owner="framework",
            retryable_after_change=True,
            reported_by=reported_by,
            evidence_paths=[evidence_artifact_id] if evidence_artifact_id else [],
        )
    blocker.update({
        "reason": "cancelled_needs_route_reconciliation",
        "scheduler": str(record.get("scheduler") or "").lower(),
        "job_id": str(record.get("job_id") or ""),
        "namespace": record.get("namespace"),
        "submission_nonce": record.get("submission_nonce"),
        "container_runtime_id": record.get("container_runtime_id"),
        "route_projection": dict(route_projection),
    })
    try:
        state.append_transcript(
            "external_job_cancellation_route_reconciliation_required",
            reported_by=reported_by,
            scheduler=blocker["scheduler"],
            job_id=blocker["job_id"],
            submission_nonce=blocker.get("submission_nonce"),
            container_runtime_id=blocker.get("container_runtime_id"),
            route_reason=route_projection.get("reason"),
        )
    except Exception:
        pass
    return dict(blocker)


def _resolve_cancellation_route_blocker(
    state: State, record: dict[str, Any],
) -> None:
    reported_by = _cancellation_route_reported_by(record)
    blockers = state.hook_state.get("blockers")
    if not isinstance(blockers, list):
        return
    remaining = [
        item for item in blockers
        if not (
            isinstance(item, dict) and item.get("reported_by") == reported_by
        )
    ]
    if len(remaining) == len(blockers):
        return
    state.hook_state["blockers"] = remaining
    try:
        state.append_transcript(
            "blocker_resolved", reported_by=reported_by,
            reason="external_cancellation_route_projection_completed",
            scheduler=str(record.get("scheduler") or "").lower(),
            job_id=str(record.get("job_id") or ""),
            container_runtime_id=record.get("container_runtime_id"),
        )
    except Exception:
        pass


def _cancellation_route_failure_response(
    state: State, record: dict[str, Any], *,
    cancel_result: dict[str, Any], evidence_artifact_id: str,
    route_projection: dict[str, Any], idempotent: bool,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocker = _record_cancellation_route_blocker(
        state, record, route_projection,
        evidence_artifact_id=evidence_artifact_id,
    )
    response = {
        "status": "cancelled_needs_route_reconciliation",
        "error": "取消结果已确认。" + _route_projection_failure_guidance(
            route_projection.get("ownership")
            if isinstance(route_projection.get("ownership"), dict) else {},
            route_projection, retry_tool="cancel_job"),
        "scheduler": record.get("scheduler"),
        "job_id": record.get("job_id"),
        "namespace": record.get("namespace"),
        "cancel": cancel_result,
        "idempotent": idempotent,
        "cancellation_intent_artifact_id": evidence_artifact_id,
        "route_projection": route_projection,
        "blocker": blocker,
        "do_not_repeat_cancel": True,
        "do_not_resubmit": True,
    }
    if lifecycle is not None:
        response["lifecycle"] = lifecycle
    return response


def _cancel_success_response(
    state: State, record: dict[str, Any], *, lifecycle: dict[str, Any],
    cancel_result: dict[str, Any], evidence_artifact_id: str,
    route_projection: dict[str, Any], task_completion: dict[str, Any],
    cleanup: dict[str, Any], idempotent: bool, planned_stop: bool = False,
) -> dict[str, Any]:
    """Build a success receipt after every closure ledger is durable."""
    return {
        "status": "success",
        "scheduler": record.get("scheduler"),
        "job_id": record.get("job_id"),
        "namespace": record.get("namespace"),
        "cancel": cancel_result,
        "lifecycle": lifecycle,
        "idempotent": idempotent,
        "cancellation_intent_artifact_id": evidence_artifact_id,
        "route_projection": route_projection,
        "task_completion": task_completion,
        "cleanup": cleanup,
        "stopped_as_planned": planned_stop,
        "message": (
            "取消已确认，按提交时声明的计划内停止记账：路线这一步按 expected_outputs 核对产物"
            "（缺产物记 expected_outputs_missing）；不需要再 finalize。收尾用 "
            'record_operation_completion(outcome="success")，会附 external_job_stopped_as_planned 检查。'
            if planned_stop else
            "取消已确认，作业记为 cancelled；提交时没有声明计划内停止，路线这一步不算完成（不追认）。"
            '如实收尾：record_operation_completion(outcome="blocked")，写明作业被取消。任务本身要求'
            "中途停下（常驻服务、按判据停、重启测试）时，提交时声明 expected_termination.planned_stop。"
        ),
    }


def _cancellation_reconciliation_response(
    state: State, transaction: dict[str, Any], *,
    cancel_result: dict[str, Any] | None, reason: str,
    status: str = "cancellation_reconciliation_required",
) -> dict[str, Any]:
    intent = transaction["intent"]
    outcome = transaction.get("outcome")
    evidence_artifact_ids = _record_cancellation_reconciliation_blocker(
        state, intent, outcome,
    )
    response = {
        "status": status,
        "error": ("取消事务不能安全推进 lifecycle；禁止重复取消或重提，"
                  "必须先按完整作业身份对账。"),
        "reconciliation_reason": reason,
        "scheduler": intent["identity"].get("scheduler"),
        "job_id": intent["identity"].get("job_id"),
        "cancellation_id": intent.get("cancellation_id"),
        "cancellation_intent_artifact_id": intent.get("intent_artifact_id"),
        "cancellation_evidence_artifact_ids": evidence_artifact_ids,
        "do_not_repeat_cancel": True,
        "do_not_resubmit": True,
        "blocker": {
            "kind": "external_job_cancellation_outcome_unknown",
            # 本地作业账本缺记录时，物理进程事实归 Core，与收尾出口的 owner 一致。
            "suggested_owner": (
                "core" if (cancel_result or {}).get("reason") == "local_job_record_missing"
                else "experiment"),
            "node_action": "reconcile_exact_job_identity_before_retry",
        },
        "next_actions": list(_JOB_STATE_UNKNOWN_NEXT_ACTIONS),
    }
    if outcome and outcome.get("outcome_artifact_id"):
        response["cancellation_outcome_artifact_id"] = outcome[
            "outcome_artifact_id"]
    if cancel_result is not None:
        response["cancel"] = cancel_result
    return response


#: 作业终态读不出时的诚实出口（取消对账、收尾读不出共用）：不宣称结束，也不宣称取消。
_JOB_STATE_UNKNOWN_NEXT_ACTIONS = (
    "先用 check_external_job_health / job_status 读一次实况：读得出终态，就用 "
    "finalize_external_job 按实际终态收尾",
    'report_blocker(summary="<作业精确身份，以及为什么读不出它的状态>", '
    'requested_action="<需要谁核实什么>")，然后 '
    'record_operation_completion(outcome="blocked")——诚实收尾，会永久封口本 run；'
    "不要重复取消，也不要重提作业",
)
#: 只是这次没读出来（身份不符、后端出错、调度器连不上）：先重读，收尾排最后。
_JOB_STATE_UNREADABLE_NEXT_ACTIONS = (
    "稍后用 check_external_job_health / job_status 重读；读得出终态就用 "
    "finalize_external_job 按实际终态收尾，仍在运行就等它结束或 cancel_job",
    "报身份不符（容器名被复用、命名空间不符）时，先按 scheduler/job_id/namespace 核对作业身份",
    '反复读不出、确实要结束本 run 时，才 report_blocker(summary="<作业精确身份与读不出的原话>", '
    'requested_action="<需要谁核实什么>")，然后 record_operation_completion(outcome="blocked")'
    "——会永久封口本 run；不要重复取消，也不要重提作业",
)


def _cancel_already_ended_response(
    record: dict[str, Any], *, lifecycle_status: Any, observed: dict[str, Any],
    message: str | None = None, idempotent: bool = False,
) -> dict[str, Any]:
    """作业已自行结束、没有可取消的对象：不记 cancelled，指回按实际终态收尾（不变量 6）。"""
    finalize_args = {"scheduler": record.get("scheduler"), "job_id": record.get("job_id")}
    if record.get("namespace"):
        finalize_args["namespace"] = record.get("namespace")
    response = {
        "status": "already_ended",
        "reason": "external_job_already_ended",
        "message": message or (
            "作业已自行结束，没有可取消的对象：未调用调度器，未写取消账本，"
            "lifecycle 仍开放。用 finalize_external_job 按实际终态收尾"
            "（outcome 按该作业的 execution_class 选）；"
            "不要把读得出终态的作业记成 cancelled。"),
        "scheduler": record.get("scheduler"),
        "job_id": record.get("job_id"),
        "namespace": record.get("namespace"),
        "cancelled": False,
        "lifecycle_status": lifecycle_status,
        "observed_end": observed,
        "next_tool": {"name": "finalize_external_job", "arguments": finalize_args},
        "do_not_repeat_cancel": True,
    }
    if idempotent:
        response["idempotent"] = True
    return response


def _last_read_observed_end(cancel_result: dict[str, Any] | None) -> dict[str, Any]:
    sandbox_state = (cancel_result or {}).get("sandbox_state") or {}
    return {"source": "native_job_record_last_read",
            "sandbox_status": sandbox_state.get("status"),
            "exit_code": sandbox_state.get("exit_code")}


#: 调度器自己明确回答「没有这个作业」时的原话。只认这些：调度器命令没装时 _run 的
#: stderr 也是 "not found"，泛匹配会把「读不出」说成「调度器查不到」。
_SCHEDULER_JOB_UNKNOWN_MARKERS: dict[str, tuple[str, ...]] = {
    "slurm": ("invalid job id specified",),
    "pbs": ("unknown job id",),
    "kubernetes": ("error from server (notfound)",),
}


def _job_state_unknown_observation(scheduler: str, health: dict[str, Any]) -> dict[str, Any]:
    """读不出作业状态时，按实际观测说是哪一种（2026-09-14 第三会话复审 P2）。

    - local_record_missing：本地作业账本读成功，明确没有这条记录；
    - scheduler_reports_job_unknown：调度器命令真跑了，原话报告查不到这个作业（可能已清除，
      也可能对本用户或本集群不可见）；
    - state_unreadable：其余一切——身份不符、命名空间不符、后端出错、调度器连不上、
      命令不存在或超时。多半稍后重读或核对身份就能恢复，不能说成「记录没了」。
    """
    result = health.get("scheduler_result") if isinstance(health, dict) else None
    raw = (result or {}).get("raw") if isinstance(result, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    sandbox_state = raw.get("sandbox_state")
    sandbox_state = sandbox_state if isinstance(sandbox_state, dict) else {}
    stderr = str(raw.get("stderr") or "").strip()[:500]
    scheduler = str(scheduler or "").lower()
    if scheduler == "local":
        kind = ("local_record_missing"
                if raw.get("ok") is True and sandbox_state.get("exists") is False
                else "state_unreadable")
    elif raw.get("returncode") is not None and any(
            marker in stderr.casefold()
            for marker in _SCHEDULER_JOB_UNKNOWN_MARKERS.get(scheduler, ())):
        kind = "scheduler_reports_job_unknown"
    else:
        kind = "state_unreadable"
    return {
        "kind": kind,
        "scheduler_phase": health.get("scheduler_phase") if isinstance(health, dict) else None,
        "sandbox_state": sandbox_state or None,
        "stderr": stderr or None,
        "returncode": raw.get("returncode"),
    }


#: 永久读不出：本地账本读成功却没有这条记录，或调度器原话报告查不到这个作业。其余读不出
#: （身份不符、后端出错、调度器连不上）可能是暂时的，续跑理应再试。
_PERMANENT_STATE_UNKNOWN_KINDS = frozenset({
    "local_record_missing", "scheduler_reports_job_unknown"})


def _state_observation_kind(scheduler: str, health: dict[str, Any]) -> str | None:
    """作业状态确实读不出时给出观测类别；读得出（运行中或调度器给了终态）时为 None。

    _job_state_unknown_observation 只该在读不出时调用——一个正常运行的本地作业记录
    存在，也会被它归成 state_unreadable——所以先卡住「读不出」这个前提。
    """
    if not isinstance(health, dict):
        return None
    raw = ((health.get("scheduler_result") or {}).get("raw") or {})
    sandbox_state = raw.get("sandbox_state") if isinstance(raw, dict) else None
    local_record_missing = bool(
        str(scheduler or "").lower() == "local"
        and isinstance(sandbox_state, dict) and sandbox_state.get("exists") is False)
    if not local_record_missing and health.get("scheduler_phase") != "unknown":
        return None
    return _job_state_unknown_observation(scheduler, health)["kind"]


def _permanently_unreadable_observation(
    scheduler: str, health: dict[str, Any],
) -> dict[str, Any] | None:
    kind = _state_observation_kind(scheduler, health)
    if kind not in _PERMANENT_STATE_UNKNOWN_KINDS:
        return None
    return _job_state_unknown_observation(scheduler, health)


def _external_job_state_unknown_exit(
    record: dict[str, Any], *, source: str, observation: dict[str, Any],
) -> dict[str, Any]:
    """作业状态读不出时的诚实出口（第 3 步，用户 2026-09-13 定）。

    不铸收据，不写 cancelled/finalized，lifecycle 保持原样；给出精确身份、owner。
    事实按观测写（_job_state_unknown_observation），不按本地/远端写死：
    - 本地账本确实没有记录：进程可能仍在运行，物理进程事实归 Core；
    - 调度器报告查不到：自身终态不可知（可能已清除，也可能不可见）；
    - 读不出（身份不符、后端出错、调度器连不上）：先稍后重读、核对身份，不引向收尾。
    report_blocker 后 record_operation_completion(outcome="blocked") 是最后一步，而且会
    永久封口本 run，所以文案里写明，读不出时排在重读之后。
    """
    scheduler = str(record.get("scheduler") or "")
    job_id = str(record.get("job_id") or "")
    local = scheduler.lower() == "local"
    kind = str(observation.get("kind") or "state_unreadable")
    stderr = observation.get("stderr")
    said = f"（原话：{stderr}）" if stderr else ""
    retry_first = kind == "state_unreadable"
    if kind == "local_record_missing":
        fact = "本地作业账本读取成功，但里面没有这个作业的记录；进程是否仍在运行无从确认"
        requested = (
            "请 Core/运维按进程组 process_group_id="
            f"{record.get('process_group_id')}、process_start_ticks="
            f"{record.get('process_start_ticks')} 核实该进程是否仍在运行并处置")
        orphan_risk: bool | None = True
    elif kind == "scheduler_reports_job_unknown":
        # squeue 的 Invalid job id 在作业被 PrivateData 隐藏、或查错集群（不带 -M）时也会出现，
        # 不能说成「调度器明确不认得」（第三会话复审 review_commits_0914b_third.md bfc1ffa7 P3）。
        fact = (f"调度器报告查不到这个作业{said}（可能已被清除，也可能对本用户或本集群不可见），"
                "它自身的终态不可知")
        requested = ("请 run owner 在调度器记账（如 sacct、tracejob）里核实该作业的实际终态，"
                     "并确认查询的集群与账户可见性（如 squeue -M、PrivateData 设置）")
        orphan_risk = False
    else:
        fact = f"这次读不出作业状态{said}；这既不说明作业已结束，也不说明记录已丢失"
        requested = (
            "先核对作业身份（scheduler/job_id/namespace）并稍后重读；反复读不出时，"
            + ("请 Core/运维核实本地作业后端与这条作业记录" if local
               else "请 run owner 核实调度器是否可达、该作业的实际终态"))
        orphan_risk = None
    if retry_first:
        guidance = ("先稍后用 check_external_job_health 重读；报身份不符就先核对作业身份。"
                    "反复读不出、确实要结束本 run 时，才 report_blocker 后 "
                    "record_operation_completion(outcome=\"blocked\")——它会永久封口本 run。")
    else:
        guidance = ("诚实收尾：先 report_blocker 记下下面的作业身份与需要谁核实什么，再 "
                    "record_operation_completion(outcome=\"blocked\")——它会永久封口本 run。")
    return {
        "status": "error",
        "error_code": "external_job_state_unknown",
        "error": (f"{scheduler}/{job_id}：{fact}。不能记成已结束，也不能记成已取消；"
                  f"不要重复取消或重提作业。{guidance}"),
        "source": source,
        "scheduler": scheduler,
        "job_id": job_id,
        "identity": _operation_closure_identity(record),
        "observation": kind,
        "observed": observation,
        "retry_first": retry_first,
        "lifecycle_written": False,
        "do_not_repeat_cancel": True,
        "do_not_resubmit": True,
        "blocker": {
            "kind": "external_job_state_unknown",
            "suggested_owner": "core" if local else "run_owner",
            "orphan_risk": orphan_risk,
            "requested_action": requested,
            "node_action": "report_blocker_then_record_operation_completion_blocked",
        },
        "next_actions": list(
            _JOB_STATE_UNREADABLE_NEXT_ACTIONS if retry_first
            else _JOB_STATE_UNKNOWN_NEXT_ACTIONS),
    }


def _close_confirmed_cancellation(
    state: State, record: dict[str, Any], *,
    transaction: dict[str, Any] | None, cancel_result: dict[str, Any],
    evidence_artifact_id: str, target_lifecycle: str, reason: str,
    superseded_by: str | None, idempotent: bool,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay only closure facts after scheduler cancellation is confirmed."""
    _ensure_blocker_list_for_recording(state)
    cleanup = {
        "status": "not_required",
        "scheduler": str(record.get("scheduler") or "").lower(),
    }
    if str(record.get("scheduler") or "").lower() == "local":
        cleanup = _cleanup_local_job_for_finalization(record)
        if cleanup.get("status") != "success":
            blocker = _record_finalized_needs_cleanup_blocker(
                state, record, cleanup, closure_kind="cancel",
            )
            response = {
                "status": "cancelled_needs_cleanup",
                "error": (
                    "取消结果已确认，但 local sandbox cleanup 失败；"
                    "未推进 route/task/lifecycle，禁止重复取消或提交。"
                ),
                "scheduler": record.get("scheduler"),
                "job_id": record.get("job_id"),
                "namespace": record.get("namespace"),
                "cancel": cancel_result,
                "idempotent": idempotent,
                "cancellation_intent_artifact_id": evidence_artifact_id,
                "cleanup": cleanup,
                "blocker": blocker,
                "do_not_repeat_cancel": True,
                "do_not_resubmit": True,
            }
            if lifecycle is not None:
                response["lifecycle"] = lifecycle
            return response

    # 计划内停止（D07）：提交时锚定任务原文声明了 planned_stop 的本地作业，取消已确认、
    # 不是替换。远端 scancel 返回 0 证明不了进程已停，暂不认。
    planned_stop = planned_stop_cancellation(
        state, record, target_lifecycle=target_lifecycle) is not None
    route_projection = _project_external_cancellation(
        state, record, evidence_artifact_id=evidence_artifact_id,
        planned_stop=planned_stop,
    )
    if route_projection.get("status") == "error":
        return _cancellation_route_failure_response(
            state, record, cancel_result=cancel_result,
            evidence_artifact_id=evidence_artifact_id,
            route_projection=route_projection, idempotent=idempotent,
            lifecycle=lifecycle,
        )

    task_completion = _complete_handoff_tasks(
        state,
        str(record.get("scheduler") or ""),
        str(record.get("job_id") or ""),
        f"{target_lifecycle} by experiment; cancellation={evidence_artifact_id}",
        namespace=record.get("namespace"),
        launch_host=record.get("launch_host"),
        scheduler_cluster=record.get("scheduler_cluster"),
        resource_uid=record.get("resource_uid"),
        submission_nonce=record.get("submission_nonce"),
        process_group_id=record.get("process_group_id"),
        process_start_ticks=record.get("process_start_ticks"),
        container_runtime_id=record.get("container_runtime_id"),
    )
    if not isinstance(task_completion, dict):
        task_completion = {
            "status": "error",
            "reason": "handoff_task_completion_invalid_response",
            "response_type": type(task_completion).__name__,
        }
    elif task_completion.get("status") != "success":
        task_completion = {
            **task_completion,
            "status": "error",
            "reason": (
                task_completion.get("reason")
                or "handoff_task_completion_not_durable"
            ),
            "upstream_status": task_completion.get("status"),
        }
    if task_completion.get("status") == "error":
        blocker = _record_external_job_needs_task_blocker(
            state, record, task_completion, closure_kind="cancel",
            outcome=target_lifecycle,
            evidence_artifact_id=evidence_artifact_id,
        )
        response = {
            "status": "cancelled_needs_task_reconciliation",
            "error": (
                "取消结果与路线投影已确认，但 matching handoff task 未完成；"
                "lifecycle 保持开放，禁止重复取消或提交。"
            ),
            "scheduler": record.get("scheduler"),
            "job_id": record.get("job_id"),
            "namespace": record.get("namespace"),
            "cancel": cancel_result,
            "idempotent": idempotent,
            "cancellation_intent_artifact_id": evidence_artifact_id,
            "route_projection": route_projection,
            "task_completion": task_completion,
            "cleanup": cleanup,
            "blocker": blocker,
            "do_not_repeat_cancel": True,
            "do_not_resubmit": True,
        }
        if lifecycle is not None:
            response["lifecycle"] = lifecycle
        return response

    if lifecycle is None:
        try:
            lifecycle = _record_job_lifecycle(
                state,
                scheduler=str(record.get("scheduler") or ""),
                job_id=str(record.get("job_id") or ""),
                lifecycle_status=target_lifecycle,
                reason=reason,
                superseded_by=superseded_by,
                namespace=record.get("namespace"),
                launch_host=record.get("launch_host"),
                scheduler_cluster=record.get("scheduler_cluster"),
                resource_uid=record.get("resource_uid"),
                submission_nonce=record.get("submission_nonce"),
                process_group_id=record.get("process_group_id"),
                process_start_ticks=record.get("process_start_ticks"),
                container_runtime_id=record.get("container_runtime_id"),
            )
        except Exception as exc:
            if transaction is not None:
                return _cancellation_reconciliation_response(
                    state, transaction, cancel_result=cancel_result,
                    reason=(
                        "lifecycle persistence failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            return {
                "status": "cancellation_reconciliation_required",
                "reason": "lifecycle persistence failed",
                "error_type": type(exc).__name__,
                "do_not_repeat_cancel": True,
                "do_not_resubmit": True,
            }
    active_handoff_markers = _external_handoff_markers_after_finalization(
        state, record,
    )
    _resolve_finalized_needs_cleanup_blocker(state, record)
    _resolve_cancellation_route_blocker(state, record)
    _resolve_external_job_needs_task_blocker(state, record)
    _resolve_cancellation_reconciliation_blocker(state, transaction)
    _reconcile_external_job_handoff_blockers(state, active_handoff_markers)
    return _cancel_success_response(
        state, record, lifecycle=lifecycle, cancel_result=cancel_result,
        evidence_artifact_id=evidence_artifact_id,
        route_projection=route_projection, task_completion=task_completion,
        cleanup=cleanup, idempotent=idempotent, planned_stop=planned_stop,
    )


_CANCEL_COMMAND_PREVIEW_CHARS = 200
_CANCEL_PREVIEW_UNKNOWN = "未知"


def _cancel_elapsed_runtime_preview(
    submitted_at: Any, *, now: datetime | None = None,
) -> str:
    """Format trusted submission time without inventing a missing timezone."""
    if not isinstance(submitted_at, str) or not submitted_at.strip():
        return _CANCEL_PREVIEW_UNKNOWN
    try:
        submitted = datetime.fromisoformat(
            submitted_at.strip().replace("Z", "+00:00"),
        )
    except ValueError:
        return _CANCEL_PREVIEW_UNKNOWN
    if submitted.tzinfo is None or submitted.utcoffset() is None:
        return _CANCEL_PREVIEW_UNKNOWN
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        return _CANCEL_PREVIEW_UNKNOWN
    elapsed = (
        current.astimezone(UTC)
        - submitted.astimezone(UTC)
    )
    if elapsed.total_seconds() < 0:
        return _CANCEL_PREVIEW_UNKNOWN
    elapsed_seconds = int(elapsed.total_seconds())
    days, remainder = divmod(elapsed_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d{hours}h{minutes}m"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _cancel_command_preview(command: Any) -> str:
    """Bound a human-only preview while stating exactly how much was omitted."""
    if not isinstance(command, str) or not command.strip():
        return _CANCEL_PREVIEW_UNKNOWN
    if len(command) <= _CANCEL_COMMAND_PREVIEW_CHARS:
        return command
    omitted = len(command) - _CANCEL_COMMAND_PREVIEW_CHARS
    return (
        command[:_CANCEL_COMMAND_PREVIEW_CHARS]
        + f"… [已截断 {omitted} 字符]"
    )


async def _cancel_job(
    state: State, scheduler: str, job_id: str, namespace: str | None = None,
    reason: str = "", superseded_by: str | None = None, **_: Any,
) -> dict:
    """Cancel one managed job as an append-only, recoverable transaction."""
    record = _external_job_record(state, scheduler, job_id, namespace)
    if record is None:
        return {"status": "error",
                "error": ("未找到唯一受管 external job 记录；同 scheduler/job_id "
                          "跨 scope 时必须提供 namespace")}
    lifecycle_resolution = lifecycle_for_submission(state, record)
    if lifecycle_resolution.get("resolution") == "legacy_lifecycle_scope_ambiguous":
        return {"status": "error",
                "error": "legacy_lifecycle_scope_ambiguous: manual scope confirmation is required before cancellation",
                "lifecycle": lifecycle_resolution}
    lifecycle_status = lifecycle_resolution.get("status")
    if (lifecycle_status is not None
            and lifecycle_status not in _ACTIVE_JOB_STATES
            and lifecycle_status not in _CANCELLED_JOB_STATES):
        return {
            "status": "error",
            "reason": "external_job_already_terminal",
            "error": (f"external job 已处于不可逆终态 {lifecycle_status!r}；"
                      "不能退回 cancelled/superseded。"),
            "scheduler": record.get("scheduler"),
            "job_id": record.get("job_id"),
            "lifecycle_status": lifecycle_status,
            "lifecycle": lifecycle_resolution,
        }
    try:
        transaction = _latest_cancellation_transaction(state, record)
    except _CancellationLedgerError as exc:
        return {
            "status": "error",
            "reason": "cancellation_ledger_unreadable",
            "error": ("取消事务账本不可验证；为避免重复取消，未调用调度器："
                      f"{exc}"),
            "scheduler": record.get("scheduler"),
            "job_id": record.get("job_id"),
            "blocker": {
                "kind": "cancellation_ledger_unreadable",
                "suggested_owner": "framework",
                "node_action": "repair_cancellation_ledger_before_retry",
            },
        }

    # Lifecycle is monotonic.  Retries of our own terminal states are receipts,
    # while every other terminal state is immutable and cannot become cancelled.
    if lifecycle_status in _CANCELLED_JOB_STATES:
        intent = transaction["intent"] if transaction else None
        evidence_id = str((intent or {}).get("intent_artifact_id") or
                          lifecycle_resolution.get("lifecycle_artifact_id") or "")
        if not evidence_id:
            return {
                "status": "error",
                "reason": "cancelled_lifecycle_evidence_missing",
                "error": "已取消 lifecycle 缺少可验证 artifact，不能伪造路线证据。",
            }
        lifecycle = {
            "lifecycle_status": lifecycle_status,
            "lifecycle_artifact_id": lifecycle_resolution.get(
                "lifecycle_artifact_id"),
        }
        return _close_confirmed_cancellation(
            state, record, transaction=transaction,
            cancel_result={"ok": True, "action": "already_cancelled"},
            evidence_artifact_id=evidence_id,
            target_lifecycle=str(lifecycle_status), reason=reason,
            superseded_by=superseded_by, idempotent=True,
            lifecycle=lifecycle,
        )
    # A previous durable intent means a crash may have happened immediately
    # before or after scheduler cancellation.  Never issue the irreversible call
    # again unless the prior outcome is durably known to be rejected.
    if transaction:
        intent, prior_outcome = transaction["intent"], transaction.get("outcome")
        if prior_outcome and prior_outcome.get("outcome") == "confirmed":
            return _close_confirmed_cancellation(
                state, record, transaction=transaction,
                cancel_result=prior_outcome.get("cancel_result") or {
                    "ok": True, "action": "confirmed_outcome_recovered",
                },
                evidence_artifact_id=str(intent["intent_artifact_id"]),
                target_lifecycle=str(
                    intent.get("target_lifecycle") or "cancelled"
                ),
                reason=str(intent.get("reason") or ""),
                superseded_by=intent.get("superseded_by"),
                idempotent=True,
            )
        if not prior_outcome or prior_outcome.get("outcome") == "unknown":
            return _cancellation_reconciliation_response(
                state, transaction,
                cancel_result=(prior_outcome or {}).get("cancel_result"),
                reason=("persisted cancellation intent has no terminal outcome"
                        if not prior_outcome else "persisted cancellation outcome is unknown"),
            )
        if prior_outcome.get("outcome") == "not_sent":
            # 上一次在发信号前的最后一读就看到作业已自行结束，没发任何信号：指回收尾。
            return _cancel_already_ended_response(
                record, lifecycle_status=lifecycle_status,
                observed=_last_read_observed_end(prior_outcome.get("cancel_result")),
                message=("上一次取消在发信号前的最后一读就看到作业已自行结束，没有发出信号；"
                         "没有可取消的对象。用 finalize_external_job 按实际终态收尾。"),
                idempotent=True)
        if prior_outcome.get("outcome") == "rejected":
            # 被拒之后作业可能已自行结束（例如 PBS 对 E 状态的作业拒绝 qdel）：只读地看一次
            # 实况，读得出终态就指回收尾，而不是停在"没有新证据不得重试"。被拒这个事实不改。
            ended_after_rejection = await _observed_job_end(state, record)
            if ended_after_rejection is not None:
                return {
                    **_cancel_already_ended_response(
                        record, lifecycle_status=lifecycle_status,
                        observed=ended_after_rejection, idempotent=True),
                    "previous_cancel_result": prior_outcome.get("cancel_result"),
                }
            return {
                "status": "error",
                "reason": "previous_cancellation_rejected",
                "error": ("上一次取消已被调度器明确拒绝；没有新的修复证据时，"
                          "不得重复调用同一取消命令。"),
                "scheduler": record.get("scheduler"),
                "job_id": record.get("job_id"),
                "idempotent": True,
                "remediation_required": True,
                "previous_cancel_result": prior_outcome.get("cancel_result"),
                "cancellation_intent_artifact_id": intent.get(
                    "intent_artifact_id"),
                "cancellation_outcome_artifact_id": prior_outcome.get(
                    "outcome_artifact_id"),
                "next_actions": [
                    "按 previous_cancel_result 修掉调度器拒绝的原因后，带新的修复证据重试",
                    *_JOB_STATE_UNKNOWN_NEXT_ACTIONS,
                ],
            }

    # Invariant 6: a job whose own terminal state is readable is closed by
    # finalize_external_job, never recorded as cancelled.  Only fresh attempts
    # reach here (every replay above returned), so jobs we killed ourselves are
    # never probed.  Runs before the confirmation card and again on the approved
    # re-call; writes nothing.
    ended = await _observed_job_end(state, record)
    if ended is not None:
        state.append_transcript("job_cancel_skipped_already_ended",
                                scheduler=scheduler, job_id=job_id, observed=ended)
        return _cancel_already_ended_response(
            record, lifecycle_status=lifecycle_status, observed=ended)

    from shared.lib import dangerous_commands as _danger
    identity = _cancellation_identity(record)
    # This exact text is the one-shot approval key. Runtime and command belong
    # only in the human preview below; runtime changes before an approved retry.
    confirmation = json.dumps({
        "operation": "cancel_external_job",
        "identity": identity,
        "reason": reason,
        "superseded_by": superseded_by,
    }, ensure_ascii=False, sort_keys=True)
    # Submit and cancel are intentionally asymmetric: cancelling even a local
    # managed job can irreversibly discard progress and leave partial outputs.
    category = _managed_job_confirmation_category(
        action="cancel", scheduler=scheduler,
    )
    if _danger.bypass_enabled():
        state.append_transcript("job_cancel_bypassed", scheduler=scheduler, job_id=job_id)
    elif _danger.is_confirmed(state, confirmation):
        _danger.consume_confirmation(state, confirmation)
        state.append_transcript("job_cancel_confirmed", scheduler=scheduler, job_id=job_id)
    else:
        state.append_transcript("job_cancel_blocked_pending_confirm", scheduler=scheduler, job_id=job_id)
        return _danger.build_pause_payload(
            state, tool="cancel_job", text=confirmation, category=category,
            preview=(f"identity={json.dumps(identity, ensure_ascii=False, sort_keys=True)}\n"
                     f"reason={reason}\nsuperseded_by={superseded_by or ''}\n"
                     f"已运行时长={_cancel_elapsed_runtime_preview(record.get('submitted_at'))}\n"
                     f"命令预览={_cancel_command_preview(record.get('command'))}"),
        )

    intent = _persist_cancellation_intent(
        state, record, reason=reason, superseded_by=superseded_by,
    )
    if intent.get("status") == "error":
        return {
            **intent,
            "scheduler": record.get("scheduler"),
            "job_id": record.get("job_id"),
            "blocker": {
                "kind": "cancellation_intent_persistence_failed",
                "node_action": "repair_artifact_persistence_before_cancellation",
            },
        }

    # 调度器调用放进线程：scancel/qdel/kubectl 带十几秒超时，不能卡住事件循环。
    result = await asyncio.to_thread(
        _cancel_sync,
        str(record.get("scheduler") or scheduler),
        str(record.get("job_id") or job_id),
        record.get("namespace"),
        container_runtime_id=record.get("container_runtime_id"),
        refuse_if_ended=True,
        absent_is_unknown=True,
    )
    if result.get("already_ended"):
        # 发信号前的最后一读看到作业已自行结束：没有发出任何信号。如实记 not_sent——原先
        # 记成 rejected（"调度器明确拒绝"），之后再调取消就停在一句不实的话上，永远到不了
        # 收尾指引（2026-09-13 审查）。
        persisted_outcome = _persist_cancellation_outcome(
            state, intent, outcome="not_sent", cancel_result=result,
        )
        response = _cancel_already_ended_response(
            record, lifecycle_status=lifecycle_status,
            observed=_last_read_observed_end(result),
            message=("作业在取消确认之后、发信号之前已自行结束：没有发出信号，取消账本记为 "
                     "not_sent，作业账本原样保留。用 finalize_external_job 按实际终态收尾。"))
        response["cancellation_intent_artifact_id"] = intent.get("intent_artifact_id")
        if persisted_outcome:
            response["cancellation_outcome_artifact_id"] = persisted_outcome.get(
                "outcome_artifact_id")
        return response
    if not result.get("ok"):
        outcome_kind = "unknown" if result.get("outcome_unknown") else "rejected"
        persisted_outcome = _persist_cancellation_outcome(
            state, intent, outcome=outcome_kind, cancel_result=result,
        )
        transaction = {"intent": intent, "outcome": persisted_outcome}
        if outcome_kind == "unknown" or persisted_outcome is None:
            return _cancellation_reconciliation_response(
                state, transaction, cancel_result=result,
                reason=("scheduler cancellation outcome is unknown"
                        if outcome_kind == "unknown"
                        else "cancellation outcome receipt could not be persisted"),
                status="cancellation_outcome_unknown",
            )
        return {
            "status": "error",
            "reason": result.get("reason") or "external_job_cancellation_rejected",
            "error": str(result.get("error") or result),
            "scheduler": record.get("scheduler"),
            "job_id": record.get("job_id"),
            "cancellation_intent_artifact_id": intent.get("intent_artifact_id"),
            "cancellation_outcome_artifact_id": persisted_outcome.get(
                "outcome_artifact_id"),
        }

    persisted_outcome = _persist_cancellation_outcome(
        state, intent, outcome="confirmed", cancel_result=result,
    )
    transaction = {"intent": intent, "outcome": persisted_outcome}
    if persisted_outcome is None:
        return _cancellation_reconciliation_response(
            state, transaction, cancel_result=result,
            reason="confirmed cancellation outcome receipt could not be persisted",
            status="cancellation_outcome_unknown",
        )

    # Re-read lifecycle after the external call to prevent a concurrent terminal
    # writer from being overwritten by cancelled.  Closure itself is replayed
    # through the same route-first helper used after a crash.
    latest_lifecycle = lifecycle_for_submission(state, record)
    latest_status = latest_lifecycle.get("status")
    lifecycle = None
    if latest_status in _CANCELLED_JOB_STATES:
        lifecycle = {
            "lifecycle_status": latest_status,
            "lifecycle_artifact_id": latest_lifecycle.get(
                "lifecycle_artifact_id"),
        }
    elif latest_status is not None and latest_status not in _ACTIVE_JOB_STATES:
        return _cancellation_reconciliation_response(
            state, transaction, cancel_result=result,
            reason=f"concurrent terminal lifecycle {latest_status!r} won the transition",
        )
    return _close_confirmed_cancellation(
        state, record, transaction=transaction, cancel_result=result,
        evidence_artifact_id=str(intent["intent_artifact_id"]),
        target_lifecycle="superseded" if superseded_by else "cancelled",
        reason=reason, superseded_by=superseded_by, idempotent=False,
        lifecycle=lifecycle,
    )

register_tool(
    ToolDefinition(
        name="discover_resources",
        description=(
            "Discover local CPU/memory/GPU resources and available schedulers (SLURM, PBS, Kubernetes).\n\n"
            "**Use when**:\n"
            "  - Before choosing local vs scheduler execution for an HPC experiment.\n"
            "  - When experiment_log needs objective resource availability evidence.\n\n"
            "**Do NOT use when**:\n"
            "  - You only need to run one immediate shell command -> use safe_run_bash.\n"
            "  - You need cloud list prices -> pass explicit rates to recommend_resources; this tool does not guess pricing.\n\n"
            "**关键参数**:\n"
            "  - namespace: optional Kubernetes namespace for kubectl queries.\n"
            "  - save_artifact: default true; stores a resource_profile artifact.\n\n"
            "**返回**: available_schedulers, recommended_default, and per-scheduler details."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Optional Kubernetes namespace."},
            },
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _discover_resources,
)

register_tool(
    ToolDefinition(
        name="recommend_resources",
        description=(
            "Recommend scheduler/resources for a task and estimate resource cost when an explicit hourly rate is provided.\n\n"
            "**Use when**:\n"
            "  - Prereg gives MPI ranks, GPUs, memory, or walltime and you need a resource plan.\n"
            "  - Before submit_job, to choose and review a resource target.\n\n"
            "**Do NOT use when**:\n"
            "  - You need actual queue submission -> use submit_job after reviewing the recommendation.\n"
            "  - You do not know cloud/allocation prices -> omit hourly_rate_usd; the tool will not fabricate cost.\n\n"
            "**关键参数**:\n"
            "  - mpi_ranks/cpus_per_rank/gpus/memory_gb/walltime_minutes: requested resources.\n"
            "  - hourly_rate_usd: optional real rate for cost estimation; no default guessing.\n"
            "  - scheduler_preference: local/slurm/pbs/kubernetes when prereg requires one.\n\n"
            "**返回**: resource recommendation, warnings, optional estimated_resource_cost_usd, and resource profile. The recommendation is transient run state; a successful submit_job records the actual allocation in job_submission."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "default": "generic_hpc"},
                "mpi_ranks": {"type": "integer", "minimum": 1},
                "cpus_per_rank": {"type": "integer", "minimum": 1},
                "gpus": {"type": "integer", "minimum": 0, "default": 0},
                "memory_gb": {"type": "number", "minimum": 0.1},
                "walltime_minutes": {"type": "integer", "minimum": 1},
                "hourly_rate_usd": {"type": "number", "minimum": 0},
                "scheduler_preference": {
                    "type": "string",
                    "enum": list(SCHEDULERS),
                },
                "namespace": {"type": "string"},
            },
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _recommend_resources,
)

register_tool(
    ToolDefinition(
        name="preflight_build_resources",
        description=(
            "Advisory read before CMake/configure/compile: compares the chosen serial/MPI/GPU/hybrid build mode with Core host declarations and a fresh live resource snapshot, "
            "records a build_resource_plan, and returns capability_issues / capacity_warnings / recommendation. It never blocks or pauses: whether to compile stays with you; "
            "record its concerns and your decision in experiment_log."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "compile_mode": {"type": "string", "enum": ["serial", "mpi", "gpu", "hybrid"]},
                "mpi_ranks": {"type": "integer", "minimum": 1},
                "cpus_per_rank": {"type": "integer", "minimum": 1},
                "gpus": {"type": "integer", "minimum": 0},
                "memory_gb": {"type": "number", "minimum": 0.1},
                "walltime_minutes": {"type": "integer", "minimum": 1},
                "scheduler_preference": {"type": "string", "enum": list(SCHEDULERS)},
                "required_software": {"type": "array", "items": {"type": "string"}},
                "runtime_resource_policy": {"type": "string", "enum": ["fixed", "requires_user_approval", "flexible"]},
                "namespace": {"type": "string"},
            },
            "required": [
                "compile_mode", "mpi_ranks", "cpus_per_rank", "gpus",
                "memory_gb", "walltime_minutes", "runtime_resource_policy",
            ],
        },
        allowed_node_types=["experiment"], risk_level="low",
    ),
    _preflight_build_resources,
)

register_tool(
    ToolDefinition(
        name="submit_job",
        description=(
            "通过统一入口渲染或提交 local/SLURM/PBS 作业；Kubernetes 目前仅可发现，缺少显式 PVC/volume 合同时连 dry-run 也会拒绝。默认 dry_run=true。\n\n"
            "**先决条件（dry_run=false 时）**: 冻结的 v2 路线里必须已有一个与本次提交对应的可执行步骤 —— "
            "先调 declare_execution_route 声明它，再把该 step 的 id 传进 route_step_id。没有它，真实提交会被拒。\n\n"
            "**Use when**:\n"
            "  - Canonical build, simulation, or another process-tree action needs a durable local/scheduler lifecycle.\n"
            "  - You need a reproducible submission script artifact for experiment_log.\n\n"
            "**Do NOT use when**:\n"
            "  - The command is a short interactive check -> use safe_run_bash.\n"
            "  - You want to bypass review of a destructive/high-risk payload: high-risk local submissions and every remote submission retain the structured one-shot confirmation path.\n\n"
            "**确认策略**: 普通本地受管提交在通过既有路线、路径、能力、沙箱与资源门禁且未命中高危命令时直接继续；不会伪造 bypass 或消费批准。\n\n"
            "**关键参数**:\n"
            "  - scheduler: auto/local/slurm/pbs/kubernetes。auto 只会依次选择 SLURM、PBS、local，绝不会自动选择 Kubernetes。\n"
            "  - dry_run: default true; writes the script and submit command but does not submit.\n"
            "  - command: shell payload to run inside the job script.\n"
            "  - workdir: optional directory where the payload should run; when omitted it defaults to this run's declared run_root.\n"
            "  - foreground_wait_s: optional synchronous wait only; expiry returns the still-running submitted job and never kills it.\n"
            "  - expected_duration_s: advisory duration for overdue reporting only; it never kills.\n"
            "  - hard_deadline_s: explicit local/Kubernetes hard deadline; only this installs a wall-clock kill.\n"
            "  - walltime_minutes: 硬性墙钟上限，**本地作业也算**。省略时本地按 "
            f"{_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES} 分钟的平台安全上限执行、到点杀进程；"
            "长作业请显式给足（省略在 SLURM/PBS 上则不落指令、站点默认记 unknown）。\n"
            "  - every scheduler script first verifies its executing UID resolves through NSS and can access the declared workdir; a failed check exits before the payload starts.\n"
            "  - image: 仅是 Kubernetes 镜像字段；当前仍必须先补齐平台 PVC/volume 挂载合同，不能据此绕过拒绝。\n\n"
            "  - nodelist: optional SLURM node list, e.g. spr-cu04, when a run must target a specific node.\n\n"
            "**返回**: script_path and script_preview. If dry_run=false, also returns job_id or submit error."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "Command payload to run inside the job."},
                "scheduler": {
                    "type": "string",
                    "enum": list(SUBMIT_SCHEDULERS),
                    "default": "auto",
                },
                "job_name": {"type": "string", "default": "experiment_job"},
                "mpi_ranks": {"type": "integer", "minimum": 1, "default": 1},
                "cpus_per_rank": {"type": "integer", "minimum": 1, "default": 1},
                "gpus": {"type": "integer", "minimum": 0, "default": 0},
                "memory_gb": {
                    "type": "number",
                    "minimum": 0.1,
                    "default": 4.0,
                    "description": (
                        "可选的明确内存契约（GiB）；构建路线省略时优先消费"
                        "获准的构建资源计划，其他情况使用带来源标记的节点自动建议。"
                    ),
                },
                "walltime_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Hard walltime. **本地作业同样适用**：省略时平台按 "
                        f"{_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES} 分钟的安全上限执行，"
                        "到点**直接杀掉**进程（不是警告、不是延长）。要跑得更久就把这个值"
                        "写出来；它会进入持久提交记录，并在需要确认时显示在卡片上。\n"
                        "SLURM/PBS：省略不落任何指令，站点默认记为 unknown。"
                    ),
                },
                "storage_gb": {"type": "number", "minimum": 0.1, "default": 8.0,
                               "description": "Maximum writable growth for a local sandbox job; Kubernetes ephemeral-storage request/limit."},
                "queue": {"type": "string", "description": "SLURM partition or PBS queue."},
                "nodelist": {"type": "string", "description": "Optional SLURM --nodelist value, e.g. spr-cu04."},
                "image": {"type": "string", "description": "Kubernetes container image."},
                "workdir": {"type": "string", "description": "Directory where the job payload should run. Defaults to this run's declared run_root when omitted."},
                "output_dir": {
                    "type": "string",
                    "description": "Optional scheduler-visible stdout/stderr directory. Defaults to this run's declared run_root/logs when omitted.",
                },
                "output_paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Application output roots reserved by this job. Every root must be under declared build_root or run_root; if omitted, workdir is conservatively reserved.",
                },
                "route_step_id": {
                    "type": "string",
                    "description": "可选：显式绑定当前冻结 declared_route v2 中的步骤 id；用于消歧和审计。",
                },
                "expected_duration_s": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "Advisory expected duration used for overdue health reporting. "
                        "Expiry never terminates the job."
                    ),
                },
                "foreground_wait_s": {
                    "type": "integer", "minimum": 1, "maximum": 900,
                    "description": (
                        "How long this tool call waits after the durable submission and "
                        "route receipt exist. Expiry returns submitted/running and never kills."
                    ),
                },
                "hard_deadline_s": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "Explicit destructive wall-clock deadline for scheduler=local or "
                        "Kubernetes (after its volume contract exists). Omit to keep local "
                        "Docker cgroup/PID 1 supervision with the platform safety ceiling."
                    ),
                },
                "execution_params": {
                    "type": "object",
                    "description": "Actual scientific simulation parameters; must match frozen prereg when a formal input package is consumed.",
                },
                "input_package_artifact_id": {
                    "type": "string",
                    "description": "Verified dataset or experiment_fallback_inputs artifact consumed by this real simulation.",
                },
                "input_package_bindings": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Complete spec_id -> verified dataset/experiment_fallback_inputs artifact id "
                        "mapping when this simulation consumes multiple formal input packages."
                    ),
                },
                "health_check": {
                    "type": "object",
                    "description": "Declarative polling contract: progress_paths/completion_paths must stay within output_paths or output_dir; optional literal error_patterns, poll_interval_s, stall_after_s. Never accepts shell commands.",
                    "additionalProperties": False,
                    "properties": {
                        "progress_paths": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1}},
                        "completion_paths": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1}},
                        "error_patterns": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 160}},
                        "poll_interval_s": {"type": "integer", "minimum": 30, "maximum": 3600},
                        "stall_after_s": {"type": "integer", "minimum": 60, "maximum": 604800},
                    },
                },
                "expected_termination": {
                    "type": "object",
                    "description": "Only when the task itself says how this job is expected to end: a non-zero exit (exit_codes, e.g. a job meant to fail) or a planned stop (planned_stop: true, e.g. a service shut down after use, a run stopped once a criterion is met, a checkpoint/restart test killed mid-run; stop it with cancel_job). exit_codes are 1..123; task_quote must be copied verbatim from the task text and contain each code as a standalone number, otherwise the submission is refused before the confirmation card. The job's outcome is still recorded as it physically happened (non-zero exit -> operation_failed); termination_matched records whether it met this declaration, and route steps / operation success read that.",
                    "additionalProperties": False,
                    "properties": {
                        "exit_codes": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": {"type": "integer", "minimum": 1, "maximum": 123}},
                        "task_quote": {"type": "string", "minLength": 1, "maxLength": 400},
                        "planned_stop": {"type": "boolean", "description": "true when the task asks for this job to be stopped mid-run with cancel_job. A confirmed cancel of a local job then counts as a planned stop: the route step is checked against expected_outputs and operation success records external_job_stopped_as_planned. Omit exit_codes if only this applies."},
                    },
                    "required": ["task_quote"],
                },
                "stage_in": {
                    "type": "array",
                    "description": "Optional file-only staging into workdir before payload; each item is {src: absolute readable file, dst: relative path}. Kubernetes requires a separate volume/PVC contract and rejects this parameter.",
                    "items": {
                        "type": "object",
                        "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                        "required": ["src", "dst"],
                    },
                },
                "dry_run": {"type": "boolean", "default": True},
                "namespace": {"type": "string", "description": "Kubernetes namespace."},
            },
            "required": ["command"],
        },
        allowed_node_types=["experiment"],
        risk_level="high",
    ),
    _submit_job,
)

register_tool(
    ToolDefinition(
        name="record_unknown_orphan",
        description=(
            "ISOLATE AND BLOCK a legacy or externally started process whose run ownership cannot be proven. "
            "This is NOT a release valve for a dangling job: it never guesses a run_id, and the declared "
            "output paths stay reserved so every overlapping submission keeps failing with "
            "active_external_job_output_conflict.\n\n"
            "**Use when**: a process/output exists that you cannot attribute to any run, and you are willing "
            "to stop using those output paths.\n"
            "**Do NOT use when**: you want to unblock a conflicting path -> pick non-overlapping output_paths, "
            "or, for an already-recorded orphan, use resolve_unknown_orphan with mechanical evidence.\n"
            "**关键参数**: observed_processes 的每一条都要整条写成 pid=<数字>@<主机名> 或 container=<id>，"
            "不带任何说明文字（说明放 note），否则该记录以后无法通过 resolve_unknown_orphan 解除。"
            "pid 只能由它所在主机的 /proc 判定，所以主机名是必填的：漏写主机的裸 pid=<数字> 一律判为"
            "“无法探活”而拒绝解除（fail-closed），因为本机 /proc 查不到一个远端 pid 并不等于它已经死了；"
            "带尾随文字的自述同样被拒，不会截断猜测。写不出可复核身份时就留空 observed_processes，"
            "改把线索写进 note，解除时走输出根静默一路。"
        ),
        parameters_schema={"type": "object", "properties": {"output_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}, "observed_processes": {"type": "array", "items": {"type": "string"}, "description": "Mechanically re-checkable process identities, each the entire string: pid=<digits>@<host> (the host is REQUIRED - a bare pid or a pid with trailing prose is refused at release time, because /proc only covers the local host and 'not found here' never proves a remote pid dead) or container=<id>. Free-form text can never be probed dead and permanently blocks release; leave the list empty and put the prose in note instead."}, "log_paths": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}}, "required": ["output_paths"]},
        allowed_node_types=["experiment"], risk_level="low",
    ),
    _record_unknown_orphan,
)

register_tool(
    ToolDefinition(
        name="resolve_unknown_orphan",
        description=(
            "Release one unknown_orphan isolation lock of THIS project against positive mechanical evidence. "
            "Mints an immutable unknown_orphan_resolution so the output-root conflict scan stops counting that "
            "orphan; the orphan record itself is never edited or deleted.\n\n"
            "**Use when**: submit_job is blocked by a conflicts[] entry with kind=unknown_orphan and you need "
            "those exact output roots back.\n"
            "**Do NOT use when**: any declared process may still be alive, or the roots may still be written -> "
            "the call is refused; choose non-overlapping output_paths instead.\n"
            "**证据要求（全部满足才放行）**: 每条 observed_processes 都被机械探活为已死"
            "（pid=<数字>@<主机名> 在该主机上查 /proc —— 主机必须显式写出且等于本机，裸 pid、"
            "空主机、带尾随文字的自述一律拒绝，本机 /proc 证不了远端进程；container=<id> 由"
            "本地作业隔离层探活；不可解析或探测不可用同样拒绝）；所有 output_roots 自 orphan "
            "recorded_at 起没有任何条目 mtime 更新（根内符号链接会被跟进到真实目标一并核验；"
            "根不存在、根本身变成符号链接、目标够不着、一个条目都核验不到，都算无证据而非静默）；"
            "reason 非空。\n"
            "**返回**: status=success 带 resolution artifact_id 与证据快照；status=error 时给出被拒证据和下一步动作。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "The unknown_orphan artifact id to release (from record_unknown_orphan or blocker.conflicts[].artifact_id).",
                },
                "reason": {
                    "type": "string",
                    "description": "Non-empty release rationale frozen into the immutable resolution record.",
                },
            },
            "required": ["artifact_id", "reason"],
        },
        allowed_node_types=["experiment"], risk_level="medium",
    ),
    _resolve_unknown_orphan,
)

register_tool(
    ToolDefinition(
        name="job_status",
        description=(
            "Query status for a job submitted through local, SLURM, PBS, or Kubernetes.\n\n"
            "**Use when**:\n"
            "  - After submit_job(dry_run=false), to monitor completion or queue state.\n\n"
            "**Do NOT use when**:\n"
            "  - No job has been submitted yet -> use submit_job first.\n\n"
            "**关键参数**: scheduler must match the submit scheduler; job_id is the returned scheduler id.\n"
            "**返回**: raw scheduler output with status=success/error."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "job_id": {"type": "string", "minLength": 1},
                "namespace": {"type": "string"},
            },
            "required": ["scheduler", "job_id"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _job_status,
)

register_tool(
    ToolDefinition(
        name="check_external_job_health",
        description=(
            "Read one declared external-job health snapshot: scheduler phase, declared progress, "
            "separately graded output/log activity, literal fatal markers, soft ETA, and stall state. "
            "Output activity alone is not reported as credible progress. It never runs a stored shell command, "
            "never cancels/retries the job, and terminal status still requires experiment analysis."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "job_id": {"type": "string", "minLength": 1},
                "namespace": {"type": "string"},
            },
            "required": ["scheduler", "job_id"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _check_external_job_health,
)

register_tool(
    ToolDefinition(
        name="wait_for_external_job",
        description=(
            "In a live chat session, wait for one managed external job without LLM sleep polling. "
            "It checks declared health at an interval for at most 15 minutes, then returns only on "
            "a terminal/diagnostic state or a still-running checkpoint with an explicit evidence level. "
            "It never cancels or retries."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "job_id": {"type": "string", "minLength": 1},
                "namespace": {"type": "string"},
                "max_wait_s": {"type": "integer", "minimum": 30, "maximum": 900, "default": 300},
                "poll_interval_s": {"type": "integer", "minimum": 30, "maximum": 3600},
            },
            "required": ["scheduler", "job_id"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _wait_for_external_job,
)

register_tool(
    ToolDefinition(
        name="finalize_external_job",
        description=(
            "Close a terminal external-job workflow. Outcome family follows the execution_class persisted at submit time (cross-checked, never the caller's claim): "
            "simulation-class jobs in scientific runs use analyzed_* and require evidence_artifact_id = a frozen non-automatic experiment_log whose metadata.external_job_refs contains this exact scoped job identity "
            "(never operation_completed/operation_failed; if such a job was in fact a probe or install that the derivation misclassified, do not forge a scientific log — report_blocker with the dispute, then close it with operation_blocked plus disputed_execution_class, which mints a class_disputed closure and leaves a standing audit blocker); "
            "operation-class jobs (execution_class=diagnostic 或 toolchain_build) use operation_completed/operation_failed/operation_blocked, must omit evidence_artifact_id, and the tool mints an immutable external_job_operation_closure from mechanical evidence "
            "(operation_completed needs empty error_evidence plus a successful terminal outcome, "
            "and every declared health completion_path and route expected_output must also pass "
            "its authoritative verifier; toolchain_build with neither declaration is rejected. "
            "A route declaration error can use exact-output correction after the missing observation "
            "is persisted; a real missing product exits via report_blocker then operation_blocked. "
            "operation_failed needs error evidence or a nonzero exit; operation_blocked needs a note "
            "plus a blocker you recorded yourself via report_blocker — the framework's own "
            "external-job reconciliation blockers never unlock it). "
            "Operational runs keep operation_* with a frozen experiment_log. "
            "This records an auditable lifecycle transition and does not change scientific verdict rules."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "job_id": {"type": "string", "minLength": 1},
                "evidence_artifact_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["analyzed_success", "analyzed_failure", "analyzed_inconclusive", "operation_completed", "operation_failed", "operation_blocked"]},
                "note": {"type": "string"},
                "namespace": {"type": "string"},
                "disputed_execution_class": {
                    "type": "string",
                    "enum": ["diagnostic", "toolchain_build"],
                    "description": (
                        "Only for operation_blocked on a job whose persisted execution_class "
                        "cross-checks as simulation: the class you claim it actually is. "
                        "Recorded as a dispute alongside the persisted class; it never "
                        "overrides the persisted class and never unlocks operation_completed/"
                        "operation_failed."
                    ),
                },
            },
            "required": ["scheduler", "job_id", "outcome"],
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _finalize_external_job,
)

register_tool(
    ToolDefinition(
        name="cancel_job",
        description=(
            "Cancel a still-running job previously submitted by experiment through the scheduler-aware managed path. "
            "For local jobs it stops the exact immutable-ID-matched native job, records lifecycle state, and closes matching handoff tasks. "
            "A job that already ended on its own is not cancelled: the call returns status=already_ended with the observed end state, "
            "records nothing, and names finalize_external_job as the way to close it. "
            "Real cancellation asks for confirmation unless bypass is enabled."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "job_id": {"type": "string", "minLength": 1},
                "namespace": {"type": "string"},
                "reason": {"type": "string"},
                "superseded_by": {"type": "string"},
            },
            "required": ["scheduler", "job_id"],
        },
        allowed_node_types=["experiment"],
        risk_level="high",
    ),
    _cancel_job,
)

for _managed_artifact_type in sorted(_MANAGED_EXTERNAL_ARTIFACT_TYPES):
    register_save_gate(
        _managed_artifact_type,
        _managed_external_artifact_save_gate,
    )
