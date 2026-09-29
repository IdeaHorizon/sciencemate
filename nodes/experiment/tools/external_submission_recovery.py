"""外部提交身份恢复：用提交前 intent 对账，不接受调用方提供 job_id。

提交动作的不可逆边界在 ``external_submission_intent`` 落盘之后、
``job_submission`` 收据落盘之前。进程在这个窗口崩溃时，唯一安全的恢复方式是
用 intent 中由框架生成的 nonce 查询调度器；把“再提交一次”当恢复会制造重复
作业。本模块只投影已有事实，不拥有第二套作业生命周期。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

try:
    from .pbs_scheduler import (
        PBS_PRO,
        TORQUE,
        pbs_flavor,
        pbs_history_not_configured,
        pbs_qstat_argv,
    )
except ImportError:
    from tools.pbs_scheduler import (
        PBS_PRO,
        TORQUE,
        pbs_flavor,
        pbs_history_not_configured,
        pbs_qstat_argv,
    )

_INTENT_TYPE = "external_submission_intent"
_RECOVERY_TYPE = "external_job_submission_recovery"
_INTENT_ADOPTION_TYPE = "external_submission_intent_adoption"
_KNOWN_SCHEDULERS = frozenset({"local", "slurm", "pbs", "kubernetes"})
_NONCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_SLURM_ID_RE = re.compile(r"[0-9]+(?:_[0-9]+)?")
_CONTAINER_RUNTIME_ID_RE = re.compile(r"[0-9a-f]{64}")
_TERMINAL_INTENT_REASONS = {
    "aborted_before_submit": "submission_aborted_before_submit",
    "rejected_by_scheduler": "submission_rejected_by_scheduler",
}


class SubmissionRecoveryError(RuntimeError):
    """恢复账本不完整或自相矛盾；此时必须 fail closed。"""


QueryRunner = Callable[..., dict[str, Any]]


def _read_payload(state: Any, artifact: dict[str, Any], expected_type: str,
                  *, require_same_run: bool = True) -> dict[str, Any]:
    artifact_id = str(artifact.get("id") or "")
    if not artifact_id:
        raise SubmissionRecoveryError(f"{expected_type} artifact ID 缺失")
    outer = state.read_artifact(artifact_id)
    if not isinstance(outer, dict) or outer.get("type") != expected_type:
        raise SubmissionRecoveryError(f"{expected_type} {artifact_id} 不可读或类型错误")
    if (outer.get("produced_by_node_type") != "experiment"
            or (require_same_run
                and outer.get("produced_by_run_id") != getattr(state, "run_id", None))):
        raise SubmissionRecoveryError(
            f"{expected_type} {artifact_id} 不是当前 Experiment run 的产物")
    content = outer.get("content")
    if not isinstance(content, str):
        raise SubmissionRecoveryError(f"{expected_type} {artifact_id} content 不是字符串")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SubmissionRecoveryError(
            f"{expected_type} {artifact_id} content JSON 损坏") from exc
    if not isinstance(payload, dict):
        raise SubmissionRecoveryError(f"{expected_type} {artifact_id} content 必须是对象")
    return {**payload, "_artifact_id": artifact_id,
            "_artifact_version": outer.get("version"),
            "_produced_by_run_id": outer.get("produced_by_run_id")}


def _all_payloads(state: Any, artifact_type: str,
                  *, require_same_run: bool = True) -> list[dict[str, Any]]:
    try:
        artifacts = state.list_artifacts(artifact_type, own_only=True)
    except TypeError:  # 最小测试替身的兼容面；真实 State 始终支持 own_only。
        artifacts = state.list_artifacts(artifact_type)
    return [_read_payload(state, artifact, artifact_type,
                          require_same_run=require_same_run)
            for artifact in artifacts or []]


def _terminal_intent_validation_error(intent: dict[str, Any]) -> str | None:
    status = intent.get("intent_status")
    if status == "prepared":
        return None
    if status == "aborted_before_submit":
        if (
            intent.get("submission_boundary_crossed") is False
            and isinstance(intent.get("aborted_at"), str)
            and isinstance(intent.get("abort_reason"), str)
        ):
            return None
        return "pre-submit abort intent 缺少可信终态字段"
    if status == "rejected_by_scheduler":
        if (
            intent.get("submission_boundary_crossed") is True
            and intent.get("scheduler_acceptance") is False
            and isinstance(intent.get("terminal_at"), str)
            and isinstance(intent.get("terminal_reason"), str)
        ):
            return None
        return "scheduler rejection intent 缺少可信终态字段"
    return "submission intent 状态不受支持"


def _own_attempt_payloads(
    state: Any, artifact_type: str, route_attempt_id: str,
) -> list[dict[str, Any]]:
    """按 route attempt 取本 run 的账本行。

    intent/receipt/recovery 都落在节点目录（跨 run 共享）：别的 run 的行是
    账本事实而非读取错误。与本次 attempt 无关的跨 run 残留由
    ``dangling_external_submission_intents`` 分类（已收养的视为终态
    unknown_dead）、由输出冲突守卫拦截，不在这里炸掉本 run 自己的恢复出口；
    但凡匹配到本次 attempt 却归属别的 run，仍按非属主拒绝（fail-closed，
    reconcile 只恢复本 run 的提交）。
    """
    run_id = str(getattr(state, "run_id", None) or "")
    matches: list[dict[str, Any]] = []
    for item in _all_payloads(state, artifact_type, require_same_run=False):
        if str(item.get("route_attempt_id") or "") != route_attempt_id:
            continue
        if str(item.get("_produced_by_run_id") or "") != run_id:
            raise SubmissionRecoveryError(
                f"{artifact_type} {item.get('_artifact_id')} "
                "不是当前 Experiment run 的产物")
        matches.append(item)
    return matches


def _intent_for_attempt(state: Any, route_attempt_id: str) -> dict[str, Any]:
    matches = _own_attempt_payloads(state, _INTENT_TYPE, route_attempt_id)
    if not matches:
        raise SubmissionRecoveryError("route attempt 没有持久化 submission intent")
    if len(matches) != 1:
        raise SubmissionRecoveryError("同一 route attempt 对应多份 submission intent")
    intent = matches[0]
    nonce = str(intent.get("submission_nonce") or "")
    scheduler = str(intent.get("scheduler") or "").lower()
    terminal_error = _terminal_intent_validation_error(intent)
    if terminal_error is not None:
        raise SubmissionRecoveryError(terminal_error)
    if intent.get("dry_run") is not False:
        raise SubmissionRecoveryError("submission intent 不是 real submission")
    if nonce != route_attempt_id or not _NONCE_RE.fullmatch(nonce):
        raise SubmissionRecoveryError("submission nonce 未与 route attempt 精确绑定")
    if scheduler not in _KNOWN_SCHEDULERS:
        raise SubmissionRecoveryError("submission intent scheduler 不受支持")
    if not isinstance(intent.get("script_sha256"), str):
        raise SubmissionRecoveryError("submission intent 缺少 script_sha256")
    return intent


def _recovery_identity_key(item: dict[str, Any]) -> tuple[str, ...]:
    scheduler = str(item.get("scheduler") or "").strip().lower()
    local = scheduler == "local"
    return (
        scheduler,
        str(item.get("namespace") or ""),
        str(item.get("launch_host") or "").casefold(),
        str(item.get("scheduler_cluster") or "").casefold(),
        str(item.get("resource_uid") or ""),
        str(item.get("job_id") or ""),
        str(item.get("submission_nonce") or ""),
        "" if local else str(item.get("process_group_id") or ""),
        "" if local else str(item.get("process_start_ticks") or ""),
        str(item.get("container_runtime_id") or "") if local else "",
    )


def _normalized_known_receipt(
    item: dict[str, Any], route_attempt_id: str, intent: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(item)
    if str(intent.get("scheduler") or "").strip().lower() != "local":
        # Remote receipts retain their existing legacy compatibility: optional
        # scope fields may be absent and are reconciled by the remote scheduler
        # query/route contracts rather than by Docker identity rules.
        return normalized
    scheduler = str(item.get("scheduler") or "").strip().lower()
    expected_job_id = str(intent.get("job_id") or "").strip()
    job_id = str(item.get("job_id") or "").strip()
    nonce = str(item.get("submission_nonce") or "").strip()
    runtime_id = str(item.get("container_runtime_id") or "").strip()
    if (
        scheduler != "local"
        or not expected_job_id
        or job_id != expected_job_id
        or nonce != route_attempt_id
        or not _CONTAINER_RUNTIME_ID_RE.fullmatch(runtime_id)
    ):
        raise SubmissionRecoveryError(
            "local success receipt 与 submission intent 的 immutable identity 不一致"
        )
    normalized["scheduler"] = scheduler
    normalized["job_id"] = job_id
    normalized["submission_nonce"] = nonce
    normalized["container_runtime_id"] = runtime_id
    normalized["process_group_id"] = None
    normalized["process_start_ticks"] = None
    return normalized


def _known_receipt_for_attempt(
    state: Any, route_attempt_id: str, intent: dict[str, Any],
) -> dict[str, Any] | None:
    receipts: list[dict[str, Any]] = []
    for artifact_type in ("job_submission", _RECOVERY_TYPE):
        for item in _own_attempt_payloads(state, artifact_type, route_attempt_id):
            if (item.get("status") == "success"
                    and str(item.get("job_id") or "")):
                receipts.append(_normalized_known_receipt(
                    item, route_attempt_id, intent))
    identities = {_recovery_identity_key(item) for item in receipts}
    if len(identities) > 1:
        raise SubmissionRecoveryError("同一 route attempt 已出现冲突的成功作业身份")
    return receipts[-1] if receipts else None


def _latest_recovery_for_attempt(
    state: Any, route_attempt_id: str,
) -> dict[str, Any] | None:
    matches = _own_attempt_payloads(state, _RECOVERY_TYPE, route_attempt_id)
    if len(matches) > 1:
        raise SubmissionRecoveryError(
            "同一 route attempt 对应多个 recovery artifact identity")
    return matches[-1] if matches else None


def _run_query(argv: list[str], *, timeout: int = 20) -> dict[str, Any]:
    # 复用 resource_manager 的受限 argv 查询实现；不引入第二个 subprocess 通道。
    try:
        from .resource_manager import _run
    except ImportError:
        from tools.resource_manager import _run
    return _run(argv, timeout=timeout)


def _invoke(runner: QueryRunner, argv: list[str]) -> dict[str, Any]:
    try:
        result = runner(argv, timeout=20)
    except Exception as exc:
        return {
            "ok": False, "returncode": None, "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}", "_argv": list(argv),
        }
    if not isinstance(result, dict):
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": "query runner returned non-object", "_argv": list(argv)}
    return {**result, "_argv": list(argv)}


def _query_digest(results: list[dict[str, Any]]) -> str:
    bounded = [{
        "argv": list(item.get("_argv") or []),
        "ok": bool(item.get("ok")),
        "returncode": item.get("returncode"),
        "stdout": str(item.get("stdout") or "")[:100_000],
        "stderr": str(item.get("stderr") or "")[:20_000],
    } for item in results]
    return hashlib.sha256(json.dumps(
        bounded, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _result(status: str, *, identities: list[dict[str, Any]] | None = None,
            results: list[dict[str, Any]] | None = None,
            reason: str | None = None) -> dict[str, Any]:
    rows = identities or []
    query_results = results or []
    raw_digest = _query_digest(query_results)
    evidence_digest = hashlib.sha256(json.dumps({
        "query_status": status,
        "reason": reason,
        "identities": rows,
        "raw_query_digest": raw_digest,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
       default=str).encode("utf-8")).hexdigest()
    return {
        "query_status": status,
        "candidate_count": len(rows),
        "identities": rows,
        # 不持久化可能很大的 scheduler stdout/stderr；保存命令、匹配行和完整
        # 查询结果 digest，既能复核“查了什么”，又不会把日志/环境内容抄进账本。
        "query_commands": [list(item.get("_argv") or []) for item in query_results],
        "query_evidence_sha256": evidence_digest,
        **({"reason": reason} if reason else {}),
    }


def _query_local(intent: dict[str, Any], _runner: QueryRunner) -> dict[str, Any]:
    del _runner
    nonce = str(intent["submission_nonce"])
    job_id = str(intent.get("job_id") or "").strip()
    expected_image_id = str(intent.get("sandbox_image_id") or "").strip()
    if not job_id or not expected_image_id:
        return _result("query_error", reason="local_container_contract_missing")
    try:
        from core.sandbox import inspect_container
        inspected = inspect_container(job_id)
    except Exception as exc:
        return _result(
            "query_error",
            reason=f"local_container_inspect_failed:{type(exc).__name__}",
        )
    if not isinstance(inspected, dict):
        return _result("query_error", reason="local_container_inspect_invalid")
    if inspected.get("error"):
        return _result(
            "query_error",
            reason=("local_container_inspect_failed:"
                    + str(inspected.get("error"))),
        )
    if inspected.get("exists") is not True:
        return _result("zero", reason="local_container_not_found")
    runtime_id = str(inspected.get("id") or "").strip()
    if not _CONTAINER_RUNTIME_ID_RE.fullmatch(runtime_id):
        return _result(
            "query_error", reason="local_container_runtime_identity_invalid")
    if not (
        inspected.get("managed") is True
        and str(inspected.get("kind") or "") == "job"
        and str(inspected.get("name") or "") == job_id
        and str(inspected.get("image_id") or "") == expected_image_id
    ):
        return _result("query_error", reason="local_container_identity_mismatch")
    identity = {
        "scheduler": "local",
        "job_id": job_id,
        "namespace": None,
        "launch_host": intent.get("launch_host"),
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": nonce,
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": runtime_id,
    }
    # Docker name is mutable and prepare_launch currently has no framework-owned
    # submission-nonce/route-attempt label. Inspection by the planned name can
    # therefore discover a candidate, but even an otherwise matching managed Job
    # cannot prove that it is the container created for this intent. Keep the
    # candidate as reconciliation evidence and fail closed until Core exposes an
    # intent-bound immutable label through inspect_container.
    return _result(
        "query_error",
        identities=[identity],
        reason="local_immutable_identity_unprovable",
    )


def _slurm_since(submitted_at: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        parsed = datetime.now(UTC)
    return (parsed.astimezone(UTC) - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _parse_slurm_rows(text: str, nonce: str, *, cluster: str) -> list[dict[str, Any]]:
    expected = f"ai4s:{nonce}"
    rows: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 2 or parts[1] != expected or not _SLURM_ID_RE.fullmatch(parts[0]):
            continue
        row_cluster = parts[2] if len(parts) >= 3 and parts[2] else cluster
        rows.append({
            "scheduler": "slurm", "job_id": parts[0], "namespace": None,
            "launch_host": None, "scheduler_cluster": row_cluster,
            "resource_uid": None,
        })
    return rows


def _query_slurm(intent: dict[str, Any], runner: QueryRunner) -> dict[str, Any]:
    config = _invoke(runner, ["scontrol", "show", "config"])
    queue = _invoke(runner, ["squeue", "-h", "-o", "%i|%k"])
    accounting = _invoke(runner, [
        "sacct", "-X", "-n", "-P", "-S", _slurm_since(intent.get("submitted_at")),
        "-o", "JobIDRaw,Comment%256,Cluster",
    ])
    results = [config, queue, accounting]
    if not all(item.get("ok") for item in results):
        return _result("query_error", results=results,
                       reason="slurm_authoritative_query_failed")
    match = re.search(r"(?m)^\s*ClusterName\s*=\s*([^\s#]+)",
                      str(config.get("stdout") or ""))
    if not match:
        return _result("query_error", results=results,
                       reason="slurm_cluster_identity_unavailable")
    cluster = match.group(1)
    nonce = str(intent["submission_nonce"])
    identities = (
        _parse_slurm_rows(str(queue.get("stdout") or ""), nonce, cluster=cluster)
        + _parse_slurm_rows(str(accounting.get("stdout") or ""), nonce, cluster=cluster)
    )
    unique = {(
        item["scheduler_cluster"], item["job_id"]): item for item in identities
    }
    rows = list(unique.values())
    if not rows:
        return _result("zero", results=results, reason="slurm_nonce_not_found")
    if len(rows) > 1:
        return _result("multiple", identities=rows, results=results,
                       reason="slurm_nonce_resolved_to_multiple_jobs")
    return _result("unique", identities=rows, results=results)


def _parse_pbs_jobs(text: str, nonce: str) -> list[dict[str, Any]]:
    jobs: list[tuple[str, dict[str, str]]] = []
    current_id = ""
    fields: dict[str, str] = {}
    last_key = ""
    for line in str(text or "").splitlines():
        match = re.match(r"^Job Id:\s*(\S+)\s*$", line)
        if match:
            if current_id:
                jobs.append((current_id, fields))
            current_id, fields, last_key = match.group(1), {}, ""
            continue
        attr = re.match(r"^\s+([A-Za-z0-9_.-]+)\s*=\s*(.*)$", line)
        if attr and current_id:
            last_key = attr.group(1)
            fields[last_key] = attr.group(2).strip()
        elif current_id and last_key and line[:1].isspace():
            fields[last_key] += line.strip()
    if current_id:
        jobs.append((current_id, fields))
    token = f"AI4S_SUBMISSION_NONCE=ai4s:{nonce}"
    rows: list[dict[str, Any]] = []
    for job_id, attrs in jobs:
        variables = str(attrs.get("Variable_List") or "")
        if token not in {item.strip() for item in variables.split(",")}:
            continue
        cluster = job_id.split(".", 1)[1] if "." in job_id else None
        rows.append({
            "scheduler": "pbs", "job_id": job_id, "namespace": None,
            "launch_host": None, "scheduler_cluster": cluster,
            "resource_uid": None,
        })
    return rows


def _query_pbs(intent: dict[str, Any], runner: QueryRunner) -> dict[str, Any]:
    detected = pbs_flavor(runner)
    flavor = str(detected["flavor"])
    probe = dict(detected["probe"])
    nonce = str(intent["submission_nonce"])

    if flavor == PBS_PRO:
        history = _invoke(runner, pbs_qstat_argv(history=True))
        results = [probe, history]
        if history.get("ok"):
            rows = _parse_pbs_jobs(str(history.get("stdout") or ""), nonce)
            if not rows:
                return _result(
                    "zero", results=results, reason="pbs_nonce_not_found",
                )
            if len(rows) > 1:
                return _result(
                    "multiple", identities=rows, results=results,
                    reason="pbs_nonce_resolved_to_multiple_jobs",
                )
            return _result("unique", identities=rows, results=results)

        # PBS Pro/OpenPBS sites may disable history.  Active-only output cannot
        # prove uniqueness across completed jobs, so retain candidates but keep
        # reconciliation pending with the exact capability reason.
        active = _invoke(runner, pbs_qstat_argv())
        results.append(active)
        active_rows = (
            _parse_pbs_jobs(str(active.get("stdout") or ""), nonce)
            if active.get("ok") else []
        )
        if pbs_history_not_configured(history):
            return _result(
                "query_error",
                identities=active_rows,
                results=results,
                reason=(
                    "pbs_history_not_configured_after_active_zero"
                    if not active_rows
                    else "pbs_history_not_configured"
                ),
            )
        return _result(
            "query_error",
            identities=active_rows,
            results=results,
            reason=(
                "pbs_history_query_unavailable_after_active_zero"
                if not active_rows
                else "pbs_history_query_unavailable"
            ),
        )

    if flavor == TORQUE:
        # Torque alone gives -x XML semantics, so recovery must stay on the
        # active text query and never feed XML to the text parser.
        active = _invoke(runner, pbs_qstat_argv())
        results = [probe, active]
        if not active.get("ok"):
            return _result(
                "query_error", results=results,
                reason="pbs_authoritative_query_failed",
            )
        rows = _parse_pbs_jobs(str(active.get("stdout") or ""), nonce)
        if not rows:
            return _result("zero", results=results, reason="pbs_nonce_not_found")
        if len(rows) > 1:
            return _result(
                "multiple", identities=rows, results=results,
                reason="pbs_nonce_resolved_to_multiple_jobs",
            )
        return _result("unique", identities=rows, results=results)

    # An unknown flavor retains the baseline recovery surface: active text plus
    # the legacy -x history query.  Unlike status polling, recovery must still
    # find a completed nonce job when the version string cannot be classified.
    # Merge and deduplicate both views exactly as before.
    active = _invoke(runner, pbs_qstat_argv())
    history = _invoke(runner, pbs_qstat_argv(history=True))
    results = [probe, active, history]
    if not active.get("ok"):
        return _result(
            "query_error", results=results,
            reason="pbs_authoritative_query_failed",
        )
    active_rows = _parse_pbs_jobs(str(active.get("stdout") or ""), nonce)
    if not history.get("ok"):
        return _result(
            "query_error", identities=active_rows, results=results,
            reason=(
                "pbs_history_query_unavailable_after_active_zero"
                if not active_rows else "pbs_history_query_unavailable"
            ),
        )
    history_rows = _parse_pbs_jobs(str(history.get("stdout") or ""), nonce)
    rows = list({
        (row.get("scheduler_cluster"), row["job_id"]): row
        for row in active_rows + history_rows
    }.values())
    if not rows:
        return _result("zero", results=results, reason="pbs_nonce_not_found")
    if len(rows) > 1:
        return _result(
            "multiple", identities=rows, results=results,
            reason="pbs_nonce_resolved_to_multiple_jobs",
        )
    return _result("unique", identities=rows, results=results)


def _query_kubernetes(intent: dict[str, Any], runner: QueryRunner) -> dict[str, Any]:
    context = _invoke(runner, ["kubectl", "config", "current-context"])
    command = ["kubectl"]
    namespace = str(intent.get("namespace") or "")
    if namespace:
        command += ["-n", namespace]
    command += [
        "get", "jobs", "-l",
        f"ai4s-harness/submission-nonce={intent['submission_nonce']}",
        "-o", "json",
    ]
    query = _invoke(runner, command)
    results = [context, query]
    if not context.get("ok") or not query.get("ok"):
        return _result("query_error", results=results,
                       reason="kubernetes_authoritative_query_failed")
    cluster = str(context.get("stdout") or "").strip()
    if not cluster:
        return _result("query_error", results=results,
                       reason="kubernetes_context_identity_unavailable")
    try:
        document = json.loads(str(query.get("stdout") or ""))
    except json.JSONDecodeError:
        return _result("query_error", results=results,
                       reason="kubernetes_query_json_invalid")
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return _result("query_error", results=results,
                       reason="kubernetes_query_shape_invalid")
    rows: list[dict[str, Any]] = []
    expected = str(intent["submission_nonce"])
    for item in items:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        if (not isinstance(labels, dict)
                or labels.get("ai4s-harness/submission-nonce") != expected):
            continue
        name = str(metadata.get("name") or "")
        uid = str(metadata.get("uid") or "")
        item_namespace = str(metadata.get("namespace") or namespace)
        if not name or not uid or not item_namespace:
            return _result("query_error", results=results,
                           reason="kubernetes_identity_incomplete")
        rows.append({
            "scheduler": "kubernetes", "job_id": name,
            "namespace": item_namespace, "launch_host": None,
            "scheduler_cluster": cluster, "resource_uid": uid,
        })
    if not rows:
        return _result("zero", results=results, reason="kubernetes_nonce_not_found")
    if len(rows) > 1:
        return _result("multiple", identities=rows, results=results,
                       reason="kubernetes_nonce_resolved_to_multiple_jobs")
    return _result("unique", identities=rows, results=results)


_QUERIES = {
    "local": _query_local,
    "slurm": _query_slurm,
    "pbs": _query_pbs,
    "kubernetes": _query_kubernetes,
}


def _recovery_name(state: Any, nonce: str) -> str:
    return f"external_job_submission_recovery_{state.run_id}_{nonce}"


def _save_reconciliation(
    state: Any,
    intent: dict[str, Any],
    query: dict[str, Any],
) -> dict[str, Any]:
    previous = _latest_recovery_for_attempt(
        state, str(intent["route_attempt_id"]))
    previous_query = (
        previous.get("reconciliation")
        if isinstance(previous, dict) else None
    )
    if (isinstance(previous_query, dict)
            and previous_query.get("query_status") == query.get("query_status")
            and previous_query.get("query_evidence_sha256")
            == query.get("query_evidence_sha256")):
        return {**previous, "_reused_reconciliation": True}
    unique = query.get("query_status") == "unique"
    identity = dict((query.get("identities") or [{}])[0]) if unique else {}
    payload = {
        key: value for key, value in intent.items() if not key.startswith("_")
    }
    payload.update(identity)
    payload.update({
        "status": "success" if unique else "submission_outcome_unknown",
        "dry_run": False,
        "do_not_resubmit": not unique,
        "job_id": identity.get("job_id") if unique else None,
        "submission_intent_artifact_id": intent["_artifact_id"],
        "submission_persistence": {
            "status": "identity_reconciled" if unique else "reconciliation_pending",
            "intent_artifact_id": intent["_artifact_id"],
        },
        "reconciliation": {
            **query,
            "checked_at": datetime.now(UTC).isoformat(),
            "route_attempt_id": intent["route_attempt_id"],
            "submission_nonce": intent["submission_nonce"],
        },
    })
    artifact = state.save_artifact(
        _RECOVERY_TYPE,
        _recovery_name(state, str(intent["submission_nonce"])),
        json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={
            "scheduler": intent["scheduler"],
            "job_id": str(identity.get("job_id") or ""),
            "namespace": identity.get("namespace", intent.get("namespace")),
            "launch_host": identity.get("launch_host", intent.get("launch_host")),
            "route_attempt_id": intent["route_attempt_id"],
            "submission_nonce": intent["submission_nonce"],
            "query_status": query.get("query_status"),
        },
    )
    state.append_transcript(
        "external_submission_identity_reconciliation_checked",
        route_attempt_id=intent["route_attempt_id"],
        submission_nonce=intent["submission_nonce"],
        scheduler=intent["scheduler"],
        query_status=query.get("query_status"),
        candidate_count=query.get("candidate_count"),
        query_evidence_sha256=query.get("query_evidence_sha256"),
        recovery_artifact_id=artifact.get("id"),
        do_not_resubmit=not unique,
    )
    return {**payload, "_artifact_id": artifact.get("id")}


def _matching_normal_workflow(state: Any, receipt: dict[str, Any]) -> dict[str, Any] | None:
    """找本 run 与 receipt 身份一致的 normal workflow 行。

    workflow 行落在节点目录（跨 run 共享）：上一 run 提交成功后被杀会留下
    合法的跨 run 行，它是账本事实而非读取错误。这里按归属过滤——非本 run
    的行跳过不匹配（与 ``_own_attempt_payloads`` 同族），对本 run 行的身份
    判定与多匹配 fail-closed 一律不变；不做任何收养/释放。
    """
    run_id = str(getattr(state, "run_id", None) or "")
    matches = [
        item for item in _all_payloads(state, "external_job_workflow",
                                       require_same_run=False)
        if (str(item.get("_produced_by_run_id") or "") == run_id
            and str(item.get("submission_nonce") or "")
            == str(receipt.get("submission_nonce") or "")
            and str(item.get("scheduler") or "").lower()
            == str(receipt.get("scheduler") or "").lower()
            and str(item.get("job_id") or "")
            == str(receipt.get("job_id") or "")
            and (
                str(receipt.get("scheduler") or "").lower() != "local"
                or (
                    str(receipt.get("container_runtime_id") or "")
                    and str(item.get("container_runtime_id") or "")
                    == str(receipt.get("container_runtime_id") or "")
                )
            ))
    ]
    if len(matches) > 1:
        raise SubmissionRecoveryError("恢复身份对应多个 normal external workflow")
    return matches[-1] if matches else None


def _normal_workflow_task_id(state: Any, receipt: dict[str, Any]) -> str | None:
    if not getattr(state, "project_root", None):
        return None
    try:
        from core.tasks import TaskList
        tasks = TaskList(Path(state.project_root) / "tasks").list_all()
    except Exception:
        return None
    nonce = str(receipt.get("submission_nonce") or "")
    matches = [
        task for task in tasks
        if (task.status != "completed"
            and "external_job_key=" in task.description
            and f"submission_nonce={nonce}" in task.description)
    ]
    return str(matches[0].id) if len(matches) == 1 else None


def _ensure_normal_external_workflow(
    state: Any, receipt: dict[str, Any],
) -> dict[str, Any]:
    try:
        existing = _matching_normal_workflow(state, receipt)
    except SubmissionRecoveryError as exc:
        return {"status": "error", "reason": "external_workflow_ledger_invalid",
                "error": str(exc)}
    existing_task_id = _normal_workflow_task_id(state, receipt)
    if (existing is not None
            and (not getattr(state, "project_root", None) or existing_task_id)):
        return {"status": "success", "already_persisted": True,
                "workflow_artifact_id": existing["_artifact_id"],
                "task_id": existing_task_id}
    try:
        try:
            from .resource_manager import _persist_external_job_workflow
        except ImportError:
            from tools.resource_manager import _persist_external_job_workflow
        task_id = _persist_external_job_workflow(state, receipt)
        persisted = _matching_normal_workflow(state, receipt)
    except Exception as exc:
        return {"status": "error", "reason": "external_workflow_persistence_failed",
                "error_type": type(exc).__name__}
    if persisted is None:
        return {"status": "error", "reason": "external_workflow_persistence_failed"}
    verified_task_id = _normal_workflow_task_id(state, receipt)
    if getattr(state, "project_root", None) and not verified_task_id:
        return {"status": "error", "reason": "external_workflow_task_persistence_failed",
                "workflow_artifact_id": persisted["_artifact_id"]}
    return {"status": "success", "task_id": verified_task_id or task_id,
            "workflow_artifact_id": persisted["_artifact_id"]}


def _project_route_identity(
    state: Any,
    *,
    intent: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    try:
        try:
            from .execution_route import record_external_route_identity_resolution
        except ImportError:
            from tools.execution_route import record_external_route_identity_resolution
    except (ImportError, AttributeError):
        return {
            "status": "integration_pending",
            "reason": "route_identity_resolution_api_unavailable",
        }
    identity = {
        key: receipt.get(key) for key in (
            "scheduler", "job_id", "namespace", "launch_host",
            "scheduler_cluster", "resource_uid", "process_group_id",
            "process_start_ticks",
            "container_runtime_id",
        )
    }
    identity.update({
        "submission_nonce": intent["submission_nonce"],
        "submission_artifact_id": receipt["_artifact_id"],
    })
    try:
        return record_external_route_identity_resolution(
            state,
            route_attempt_id=str(intent["route_attempt_id"]),
            domain_receipt=identity,
            reconciliation_artifact_id=str(receipt["_artifact_id"]),
        )
    except TypeError as exc:
        return {
            "status": "integration_pending",
            "reason": "route_identity_resolution_api_signature_mismatch",
            "error_type": type(exc).__name__,
        }


def _finish_recovery_workflow(
    state: Any, intent: dict[str, Any], receipt: dict[str, Any],
) -> dict[str, Any]:
    nonce = str(intent["submission_nonce"])
    completed_recovery_task_ids: set[str] = set()
    if getattr(state, "project_root", None):
        try:
            from core.tasks import TaskList
            tasks = TaskList(Path(state.project_root) / "tasks")
            for task in tasks.list_all():
                if (task.status != "completed"
                        and "external_job_identity_recovery_key=" in task.description
                        and f"submission_nonce={nonce}" in task.description):
                    tasks.complete(
                        task.id,
                        notes=(
                            "identity reconciled as "
                            f"{receipt.get('scheduler')}:{receipt.get('job_id')}"
                        ),
                    )
                    completed_recovery_task_ids.add(str(task.id))
        except Exception as exc:
            # task 仍是 active 就不能清 blocker；下一次幂等调用会从 route replay
            # 继续收尾，而不会重新查询或重新提交。
            return {"status": "error", "reason": "recovery_task_completion_failed",
                    "error_type": type(exc).__name__}
    evidence_ids = {str(intent["_artifact_id"]), str(receipt["_artifact_id"])}
    blockers = list(getattr(state, "hook_state", {}).get("blockers") or [])
    resolved = [
        item for item in blockers
        if (
            isinstance(item, dict)
            and item.get("reported_by") == "framework:external_job_identity_unresolved"
            and evidence_ids.intersection(
                str(value) for value in (item.get("evidence_paths") or []))
        )
    ]
    try:
        for blocker in resolved:
            state.append_transcript(
                "blocker_resolved",
                blocker_id=blocker.get("blocker_id"),
                reported_by=blocker.get("reported_by"),
                resolution="external_submission_identity_reconciled",
                route_attempt_id=intent["route_attempt_id"],
                recovery_artifact_id=receipt["_artifact_id"],
            )
    except Exception as exc:
        return {"status": "error", "reason": "blocker_resolution_persistence_failed",
                "error_type": type(exc).__name__}
    resolved_ids = {id(item) for item in resolved}
    try:
        state.append_transcript(
            "external_submission_identity_recovery_resolved",
            route_attempt_id=intent["route_attempt_id"],
            submission_nonce=nonce,
            scheduler=receipt.get("scheduler"),
            job_id=receipt.get("job_id"),
            recovery_artifact_id=receipt["_artifact_id"],
        )
    except Exception as exc:
        # blocker_resolved 已落盘；hook_state cache 仍保留，下一轮可见且可重放。
        return {"status": "error", "reason": "recovery_resolution_persistence_failed",
                "error_type": type(exc).__name__}
    state.hook_state["blockers"] = [
        item for item in blockers if id(item) not in resolved_ids
    ]
    recovery_cache = state.hook_state.get("external_job_identity_recovery")
    if isinstance(recovery_cache, dict):
        cases = [
            case for case in (recovery_cache.get("cases") or [])
            if str((case or {}).get("submission_nonce") or "") != nonce
        ]
        if cases:
            recovery_cache["cases"] = cases
            recovery_cache["follow_up_tasks"] = [
                item for item in (recovery_cache.get("follow_up_tasks") or [])
                if str(item) not in completed_recovery_task_ids
            ]
        else:
            state.hook_state.pop("external_job_identity_recovery", None)
    return {"status": "success", "resolved_blocker_count": len(resolved)}


def reconcile_external_submission(
    state: Any,
    route_attempt_id: str,
    *,
    query_runner: QueryRunner | None = None,
) -> dict[str, Any]:
    """按 route attempt 恢复唯一外部身份；0/多/查询错误都保持禁重提。"""
    attempt = str(route_attempt_id or "").strip()
    if not _NONCE_RE.fullmatch(attempt):
        return {"status": "error", "reason": "route_attempt_id_invalid"}
    try:
        intent = _intent_for_attempt(state, attempt)
        known = _known_receipt_for_attempt(state, attempt, intent)
    except SubmissionRecoveryError as exc:
        return {
            "status": "error", "reason": "submission_recovery_ledger_invalid",
            "error": str(exc), "do_not_resubmit": True,
        }

    terminal_status = intent.get("intent_status")
    if terminal_status in _TERMINAL_INTENT_REASONS:
        return {
            "status": "already_terminal",
            "reason": _TERMINAL_INTENT_REASONS[str(terminal_status)],
            "route_attempt_id": attempt,
            "intent_artifact_id": intent.get("_artifact_id"),
            "submission_boundary_crossed": intent.get(
                "submission_boundary_crossed"),
            "output_roots_released": True,
            # 同一个 route attempt 已封口，不能拿原身份重提；新计划可创建新 attempt。
            "do_not_resubmit": True,
        }

    if known is None:
        query = _QUERIES[str(intent["scheduler"])](
            intent, query_runner or _run_query)
        try:
            receipt = _save_reconciliation(state, intent, query)
        except SubmissionRecoveryError as exc:
            return {
                "status": "error", "reason": "submission_recovery_ledger_invalid",
                "error": str(exc), "do_not_resubmit": True,
            }
        if query.get("query_status") != "unique":
            return {
                "status": "reconciliation_pending",
                "reason": query.get("reason") or query.get("query_status"),
                "query_status": query.get("query_status"),
                "candidate_count": query.get("candidate_count"),
                "do_not_resubmit": True,
                "route_attempt_id": attempt,
                "recovery_artifact_id": receipt.get("_artifact_id"),
            }
    else:
        receipt = known

    handoff = _ensure_normal_external_workflow(state, receipt)
    if handoff.get("status") != "success":
        return {
            "status": "integration_pending",
            "reason": handoff.get("reason") or "external_workflow_persistence_failed",
            "do_not_resubmit": True,
            "route_attempt_id": attempt,
            "recovery_artifact_id": receipt.get("_artifact_id"),
            "normal_workflow": handoff,
        }
    projection = _project_route_identity(state, intent=intent, receipt=receipt)
    if projection.get("status") != "success":
        return {
            "status": "integration_pending",
            "reason": projection.get("reason") or "route_identity_projection_failed",
            "do_not_resubmit": True,
            "route_attempt_id": attempt,
            "recovery_artifact_id": receipt.get("_artifact_id"),
            "resolved_identity": {
                key: receipt.get(key) for key in (
                    "scheduler", "job_id", "namespace", "launch_host",
                    "scheduler_cluster", "resource_uid", "process_group_id",
                    "process_start_ticks",
                    "container_runtime_id",
                )
            },
            "route_projection": projection,
            "normal_workflow": handoff,
        }
    closure = _finish_recovery_workflow(state, intent, receipt)
    if closure.get("status") != "success":
        return {
            "status": "integration_pending",
            "reason": closure.get("reason") or "recovery_workflow_closure_failed",
            "do_not_resubmit": True,
            "route_attempt_id": attempt,
            "recovery_artifact_id": receipt.get("_artifact_id"),
            "route_projection": projection,
            "normal_workflow": handoff,
            "recovery_closure": closure,
        }
    return {
        "status": "success",
        "already_reconciled": known is not None,
        "route_attempt_id": attempt,
        "recovery_artifact_id": receipt.get("_artifact_id"),
        "resolved_identity": {
            key: receipt.get(key) for key in (
                "scheduler", "job_id", "namespace", "launch_host",
                "scheduler_cluster", "resource_uid", "process_group_id",
                "process_start_ticks",
                "container_runtime_id",
            )
        },
        "route_projection": projection,
        "normal_workflow": handoff,
        "recovery_closure": closure,
    }


def adopted_intent_artifact_ids(state: Any) -> set[str]:
    """已凭死亡证明被收养（终态 unknown_dead）的 intent artifact id 集合。

    收养记录跨 run 生效（写它的 run 结束后仍需成立），所以按节点目录读且不
    校验 produced_by_run_id；但字段不完整或缺少正面探活证据的记录不算数
    （fail-closed，缺证据时 intent 继续保留其输出根）。
    """
    try:
        artifacts = state.list_artifacts(_INTENT_ADOPTION_TYPE, own_only=True)
    except TypeError:  # 最小测试替身的兼容面；真实 State 始终支持 own_only。
        artifacts = state.list_artifacts(_INTENT_ADOPTION_TYPE)
    adopted: set[str] = set()
    for artifact in artifacts or []:
        outer = state.read_artifact(str(artifact.get("id") or ""))
        if not isinstance(outer, dict) or outer.get("type") != _INTENT_ADOPTION_TYPE:
            continue
        if outer.get("produced_by_node_type") != "experiment":
            continue
        try:
            payload = json.loads(str(outer.get("content") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("classification") != "unknown_dead":
            continue
        probe = payload.get("liveness_probe")
        intent_artifact_id = str(payload.get("adopted_intent_artifact_id") or "")
        inspected = probe.get("inspect") if isinstance(probe, dict) else None
        # fail-closed：除本地作业隔离层可用外，还必须留有正面的容器死亡证据——
        # inspect 确证容器不存在，或存在但已退出/死亡；证据缺失的记录不算数。
        container_dead = isinstance(inspected, dict) and (
            inspected.get("exists") is False
            or (inspected.get("exists") is True
                and str(inspected.get("status") or "") in {"exited", "dead"}))
        if (intent_artifact_id
                and isinstance(probe, dict)
                and probe.get("liveness_probe_available") is True
                and container_dead
                and isinstance(payload.get("checked_at"), str)):
            adopted.add(intent_artifact_id)
    return adopted


def record_intent_adoption(
    state: Any, reservation: dict[str, Any], liveness_probe: dict[str, Any],
) -> dict[str, Any]:
    """把探明确死的跨 run intent 收养为终态 unknown_dead。

    只追加一条不可变收养记录（artifact + transcript 事件），不改写已冻结的
    intent 本体；此后 ``dangling_external_submission_intents`` 与输出冲突守卫
    把该 intent 视为终态，释放其输出根保留。
    """
    checked_at = datetime.now(UTC).isoformat()
    intent_artifact_id = str(reservation.get("intent_artifact_id") or "")
    nonce = str(reservation.get("submission_nonce") or intent_artifact_id)
    payload = {
        "classification": "unknown_dead",
        "adopted_intent_artifact_id": intent_artifact_id,
        "route_attempt_id": reservation.get("route_attempt_id"),
        "submission_nonce": reservation.get("submission_nonce"),
        "scheduler": reservation.get("scheduler"),
        "intent_produced_by_run_id": reservation.get("produced_by_run_id"),
        "adopted_by_run_id": getattr(state, "run_id", None),
        "checked_at": checked_at,
        "liveness_probe": liveness_probe,
        "output_roots_released": list(reservation.get("output_roots") or []),
    }
    artifact = state.save_artifact(
        _INTENT_ADOPTION_TYPE,
        f"{_INTENT_ADOPTION_TYPE}_{state.run_id}_{nonce}",
        json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={
            "adopted_intent_artifact_id": intent_artifact_id,
            "submission_nonce": reservation.get("submission_nonce"),
            "classification": "unknown_dead",
        },
    )
    state.append_transcript(
        "external_submission_intent_adopted",
        adoption_artifact_id=artifact.get("id"),
        intent_artifact_id=intent_artifact_id,
        submission_nonce=reservation.get("submission_nonce"),
        container_ref=liveness_probe.get("container_ref"),
        checked_at=checked_at,
    )
    return {"artifact_id": artifact.get("id"), **payload}


def dangling_external_submission_intents(state: Any) -> list[dict[str, Any]]:
    """返回尚无成功身份的 intent；供 turn-start 与输出冲突守卫共用。"""
    try:
        # intent/receipt 落在节点目录（跨 run 共享）。上一 run 在 submit 在飞
        # 窗口被杀时，其 prepared intent 对后继 run 可见；把“归属别的 run”当
        # 读取错误会让整个账本 ledger_invalid，submit/reconcile/finalize 三个
        # 出口同时锁死。这里如实读出并把归属交给下方逐条分类。
        intents = _all_payloads(state, _INTENT_TYPE, require_same_run=False)
        known_attempts = {
            str(item.get("route_attempt_id") or "")
            for artifact_type in ("job_submission", _RECOVERY_TYPE)
            for item in _all_payloads(state, artifact_type, require_same_run=False)
            if item.get("status") == "success" and str(item.get("job_id") or "")
        }
        adopted = adopted_intent_artifact_ids(state)
    except SubmissionRecoveryError as exc:
        return [{
            "status": "ledger_invalid", "reason": str(exc),
            "do_not_resubmit": True, "output_roots": [],
        }]
    rows = []
    run_id = str(getattr(state, "run_id", None) or "")
    for intent in intents:
        attempt = str(intent.get("route_attempt_id") or "")
        if not attempt or attempt in known_attempts or intent.get("dry_run") is not False:
            continue
        intent_status = intent.get("intent_status")
        terminal_error = _terminal_intent_validation_error(intent)
        if intent_status in _TERMINAL_INTENT_REASONS and terminal_error is None:
            continue
        if (terminal_error is None
                and str(intent.get("_produced_by_run_id") or "") != run_id):
            # 跨 run 残留的非终态 intent。已凭死亡证明收养的视为终态
            # unknown_dead，不再保留输出根；未收养的保持 ledger_invalid 的
            # 全量拦截语义（fail-closed），并带上容器身份供冲突守卫探活。
            if str(intent.get("_artifact_id") or "") in adopted:
                continue
            rows.append({
                "status": "ledger_invalid",
                "reason": (
                    f"external_submission_intent {intent.get('_artifact_id')} "
                    f"属于其他 run（{intent.get('_produced_by_run_id')}）且未达终态"
                ),
                "do_not_resubmit": True,
                "foreign_run": True,
                "produced_by_run_id": intent.get("_produced_by_run_id"),
                "route_attempt_id": attempt,
                "submission_nonce": intent.get("submission_nonce"),
                "scheduler": intent.get("scheduler"),
                "job_id": intent.get("job_id"),
                "container_runtime_id": intent.get("container_runtime_id"),
                "workdir": intent.get("workdir"),
                "output_roots": list(intent.get("output_roots") or []),
                "intent_artifact_id": intent.get("_artifact_id"),
            })
            continue
        if terminal_error is not None:
            rows.append({
                "status": "ledger_invalid",
                "reason": terminal_error,
                "do_not_resubmit": True,
                "output_roots": list(intent.get("output_roots") or []),
                "intent_artifact_id": intent.get("_artifact_id"),
            })
            continue
        rows.append({
            "status": "submission_outcome_unknown",
            "route_attempt_id": attempt,
            "submission_nonce": intent.get("submission_nonce"),
            "scheduler": intent.get("scheduler"),
            "namespace": intent.get("namespace"),
            "workdir": intent.get("workdir"),
            "output_roots": list(intent.get("output_roots") or []),
            "intent_artifact_id": intent.get("_artifact_id"),
            "script_sha256": intent.get("script_sha256"),
            "submitted_at": intent.get("submitted_at"),
            "job_name": intent.get("job_name"),
            "script_path": intent.get("script_path"),
            "do_not_resubmit": True,
        })
    return rows


def unresolved_submission_output_reservations(state: Any) -> list[dict[str, Any]]:
    """未知提交仍保留 output/workdir；调用方不得把“无 job_id”当成无冲突。"""
    reservations = []
    for row in dangling_external_submission_intents(state):
        roots = [str(item) for item in row.get("output_roots") or [] if str(item)]
        if not roots and row.get("workdir"):
            roots = [str(row["workdir"])]
        reservations.append({**row, "output_roots": roots})
    return reservations


async def _reconcile_external_submission(
    state: Any, route_attempt_id: str, **_: Any,
) -> dict[str, Any]:
    return reconcile_external_submission(state, route_attempt_id)


register_tool(
    ToolDefinition(
        name="reconcile_external_submission",
        description=(
            "恢复 submit_job 在 intent 落盘后、job identity 收据落盘前中断的提交。"
            "只接受框架 route_attempt_id，并按 submission nonce 机械查询 local/SLURM/"
            "PBS/Kubernetes；不接受调用方 job_id。查无、多个或查询失败都会保持禁重提。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "route_attempt_id": {
                    "type": "string",
                    "description": "submit_job 返回或路线事件中的 route attempt ID。",
                },
            },
            "required": ["route_attempt_id"],
            "additionalProperties": False,
        },
        allowed_node_types=["experiment"],
        risk_level="low",
    ),
    _reconcile_external_submission,
)
