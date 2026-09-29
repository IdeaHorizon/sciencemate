"""Experiment 产物契约的确定性审计。

这里不重新解释科学结果，只审计框架可观察的事实：

* hypothesis verdict 是否真的调用过 KB status 更新并成功返回；
* 是否产生 methodological/dead_end sediment，或在日志中明确说明没有；
* experiment_log 是否存在可识别的 verdict/credibility 段。

审计结果由 Experiment hook 写入 transcript，供 harness.yaml 的
mechanical.transcript_event quality check 使用，避免把“是否做过”交给
LLM judge 猜测。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from core.tool_registry import _REGISTRY, ToolDefinition, contract_requirement, register_tool

try:
    from .input_delivery import (
        InputDeliveryLedgerError,
        active_input_delivery_entries,
        load_input_delivery_ledger,
        save_input_delivery_ledger,
    )
    from .run_contract import (
        audit_execution_intent_binding,
        audit_prereg_assignment,
        execution_intent_snapshot,
        load_bound_frozen_prereg,
        load_run_contract,
        observe_current_prereg_binding_witness,
        run_role_non_applicability_reason,
        requires_experiment_fallback_input_gate,
        resolve_run_acceptance,
    )
except ImportError:  # loaded as top-level ``tools.contract_audit`` by node runtime
    from tools.input_delivery import (
        InputDeliveryLedgerError,
        active_input_delivery_entries,
        load_input_delivery_ledger,
        save_input_delivery_ledger,
    )
    from tools.run_contract import (
        audit_execution_intent_binding,
        audit_prereg_assignment,
        execution_intent_snapshot,
        load_bound_frozen_prereg,
        load_run_contract,
        observe_current_prereg_binding_witness,
        run_role_non_applicability_reason,
        requires_experiment_fallback_input_gate,
        resolve_run_acceptance,
    )


# ── 内容契约：这两个 freeze 工具会按字段名拒绝，声明放这里 ──────────────────
#
# 一份声明，两个消费者：`register_tool` 渲染进模型看到的 description，校验器用
# `contract_requirement()` 取拒绝措辞。想让两边分叉，得先把这个 dict 拆成两个。
RAW_RESULTS_CONTRACT = {
    "files": "a non-empty list; each item needs path (absolute), sha256 (64-hex), "
             "role, retention (protected|disposable), bytes (int). Freeze re-hashes "
             "every file, so save the manifest only after the files are final",
}
CLEAN_RESULTS_CONTRACT = {
    "status": "a non-empty string",
    # 2026-09-11：原先这里是 analysis_eligible（资格位）。它已从目标契约删除，
    # 而"完成的科学结果必须可重放"这条要求本身是对的，只是真有无法重放的结果时
    # 需要一个出口——否则就是不可满足的要求。出口改成**这件事自己的申报**：
    # 说清为什么重放不了，而不是顺手把自己的结论资格调低。
    "not_replayable": "optional boolean; set true only when this completed measurement "
                      "genuinely cannot be replayed, and say why in reason",
    "reason": "required when not_replayable is true",
    "replay_manifest": "required when status='completed' and not_replayable is not true, "
                       "an object holding raw_results_artifact_id and source_hashes",
    "replay_manifest.raw_results_artifact_id": "a non-empty string naming the frozen raw_results "
                                               "artifact this clean result replays from",
    # 形状写死在这里，不只写"needs source_hashes"：一次真跑里模型反复被拒，
    # 因为说明没讲它是对象还是数组，而拒绝语对已经传了该字段的模型只会重复
    # "requires source_hashes"。key 只是标签（覆盖检查只看 values），但同名
    # 文件会让 basename 撞车，所以顺带把消歧规则也写进契约。
    "replay_manifest.source_hashes": "a non-empty object mapping each raw file name to its 64-hex "
                                     "SHA-256 (never a list), e.g. {\"verlet_result.json\": \"<64-hex>\", "
                                     "\"stdout.log\": \"<64-hex>\"}, whose values must cover every "
                                     "raw_results.files[].sha256 -- keys are labels only, use the full "
                                     "absolute path when two raw files share a basename",
}

_VERDICT_TOOLS = {"update_claim_status", "update_hypothesis_verdict"}
_SEDIMENT_TYPES = {"methodological", "dead_end"}
_VERDICT_STATUSES = {"validated", "refuted", "provisional"}
_EXECUTION_RECORD_TOOL = "create_experiment"


# One authority for the scientific terminal-closure surface. Consumers project
# this mapping instead of maintaining private subsets: otherwise adding a gate
# can protect one completion path while preview, turn guidance, or the
# audit-error fallback silently omit it.
TERMINAL_CLOSURE_REGISTRY: dict[str, str] = {
    "verdict": "experiment_verdict_audit",
    "sediment": "experiment_sediment_audit",
    "experiment_log_integrity": "experiment_log_integrity_audit",
    "result_evidence": "experiment_result_evidence_audit",
    "citation_binding": "experiment_citation_binding_audit",
    "execution_intent_binding": "experiment_execution_intent_audit",
    "scientific_question_closure": (
        "experiment_scientific_question_closure_audit"
    ),
    "prereg_assignment": "experiment_prereg_assignment_audit",
    "data_provenance": "experiment_data_provenance_audit",
    "job_submission_records_readable": (
        "experiment_job_submission_records_readable_audit"
    ),
}


def terminal_closure_projection(
    audit: dict[str, dict[str, Any]] | None,
    *,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Project one complete, fail-closed terminal audit view.

    Audit payloads retain their full fields. This reducer only supplies a
    deterministic missing/error record and the two key spaces needed by
    model-facing previews and durable transcript events.
    """
    source = audit if isinstance(audit, dict) else {}
    audit_checks: dict[str, dict[str, Any]] = {}
    event_checks: dict[str, dict[str, Any]] = {}
    for audit_key, event_key in TERMINAL_CLOSURE_REGISTRY.items():
        supplied = source.get(audit_key)
        if isinstance(supplied, dict):
            check = dict(supplied)
        else:
            reason = failure_reason or (
                f"registered terminal closure audit {audit_key!r} was not supplied"
            )
            check = {
                "passed": False,
                "applicable": True,
                "status": "audit_error" if failure_reason else "audit_missing",
                "reason": reason,
            }
        audit_checks[audit_key] = check
        event_checks[event_key] = check
    return {
        "audit_checks": audit_checks,
        "event_checks": event_checks,
        "failed_audit_keys": [
            key for key, check in audit_checks.items()
            if not check.get("passed", False)
        ],
        "failed_event_keys": [
            key for key, check in event_checks.items()
            if not check.get("passed", False)
        ],
    }


def audit_data_provenance(state: Any) -> dict[str, Any]:
    """Audit stale-input declarations with one fail-closed gate policy.

    ``warn`` only downgrades positively observed stale inputs.  An unavailable
    provenance authority is never equivalent to observing an empty set, even
    in warning mode.
    """
    mode = (os.getenv("HARNESS_PROVENANCE_GATE") or "enforce").strip().lower()
    try:
        from core import data_provenance as _provenance
        stale_inputs = _provenance.undeclared_stale_inputs(state)
    except Exception as exc:
        return {
            "passed": False,
            "applicable": True,
            "status": "audit_error",
            "gate_mode": mode,
            "stale_input_count": None,
            "unverified": [],
            "reason": (
                "data provenance audit unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
        }

    stale = list(stale_inputs or [])
    unverified = [
        {"path": row.get("path") if isinstance(row, dict) else None}
        for row in stale
    ]
    if not stale:
        return {
            "passed": True,
            "applicable": True,
            "status": "passed",
            "gate_mode": mode,
            "stale_input_count": 0,
            "unverified": [],
            "reason": "未发现未声明的开始前外部输入",
        }

    reason = (
        f"发现 {len(stale)} 个未声明的开始前外部输入；冻结前在产物 metadata "
        "声明 reused_inputs，或在项目 workspace 重产。"
    )
    warned = mode == "warn"
    return {
        "passed": warned,
        "applicable": True,
        "status": "warning" if warned else "failed",
        "gate_mode": mode,
        "stale_input_count": len(stale),
        "unverified": unverified,
        "reason": (
            reason + " HARNESS_PROVENANCE_GATE=warn：仅记录警告，不阻断终态。"
            if warned else reason
        ),
    }


def job_submission_records_readability_failure(
    error: BaseException,
) -> dict[str, Any]:
    """Normalize one authoritative submission-ledger read failure."""
    try:
        try:
            from .resource_manager import submission_ledger_failure_reason
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.resource_manager import submission_ledger_failure_reason
        reason = submission_ledger_failure_reason(error)
    except Exception:
        reason = (
            "authoritative job_submission records unavailable: "
            f"{type(error).__name__}: {error}"
        )
    return {
        "passed": False,
        "reason": reason,
    }


def audit_job_submission_records_readable(state: Any) -> dict[str, Any]:
    """Verify that the authoritative submission ledger can be read strictly.

    Whether readable jobs remain open is a separate lifecycle projection and
    deliberately is not a terminal-registry key.
    """
    try:
        try:
            from .resource_manager import owed_external_job_closure_records
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.resource_manager import owed_external_job_closure_records
        owed_external_job_closure_records(state)
    except Exception as exc:
        return job_submission_records_readability_failure(exc)
    return {
        "passed": True,
        "reason": "authoritative job_submission records are readable",
    }


def _events(state: Any) -> list[dict[str, Any]]:
    path = getattr(state, "transcript_path", None)
    if not path or not Path(path).exists():
        return []
    result: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            result.append(event)
    return result


def _tool_result_object(value: Any) -> dict[str, Any] | None:
    """Decode complete structured results from both in-process and runner transcripts."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


# 声明类工具自己写下的 receipt（见 tools/sediment.py）。它们经
# ``state.append_transcript`` 原样落盘 —— 与 ``tool_result`` 的 result_preview
# 不同，后者由 core 的 _brief 截到 500 字符，超长就从 dict 变成解析不了的字符串。
# 读 receipt 而不是读 preview，是"声明写得越详细越可能失效"的直接解药。
_INCONCLUSIVE_RECEIPT = "experiment_inconclusive_verdict_declared"
_NO_SEDIMENT_RECEIPT = "experiment_no_sediment_declared"

# 与 tools/sediment.py 的 _MIN_VERDICT_REASON_CHARS / _MIN_NEXT_STEP_CHARS /
# _MIN_REASON_CHARS 一致。工具在发 receipt 前已强制过一遍；这里再校验一次，
# 是为了工具阈值将来漂移时审计不跟着松掉。
_MIN_DECLARED_REASON_CHARS = 20
_MIN_DECLARED_NEXT_STEP_CHARS = 10


def _receipts(state: Any, event_type: str) -> list[dict[str, Any]]:
    return [e for e in _events(state) if e.get("event") == event_type]


def _unreadable_tool_results(state: Any) -> dict[str, int]:
    """哪些工具的结果只在 transcript 里留下了解析不了的字符串。

    ``_tool_result_object`` 返回 None 有两种截然不同的含义：这次调用**没发生**，
    或者它发生了、成功了、但**记录读不出来**。把两者合并成同一个观测，正是让
    一次成功的声明被判成"你没声明过"、并把 agent 推进不可能赢的重试循环的原因。
    审计仍然 fail-closed（读不出就不采信），但必须能说出自己拒的是哪一种。
    """
    counts: dict[str, int] = {}
    for event in _events(state):
        if event.get("event") != "tool_result":
            continue
        preview = event.get("result_preview")
        if (isinstance(preview, str) and preview.strip()
                and _tool_result_object(preview) is None):
            name = str(event.get("name") or "")
            counts[name] = counts.get(name, 0) + 1
    return counts


def _unreadable_note(state: Any, *tools: str) -> str:
    """给失败原因补一句"证据不可读"，点名工具 —— 否则定位要靠逐字节翻 transcript。"""
    counts = _unreadable_tool_results(state)
    hits = [(name, counts[name]) for name in tools if counts.get(name)]
    if not hits:
        return ""
    listed = "、".join(f"{name}×{n}" for name, n in hits)
    return (f"；注意：本 run 有工具结果只在 transcript 里留下截断字符串（{listed}），"
            "审计无法确认其是否成功 —— 这是记录层缺陷，不等于该调用未发生")


def _successful_tool_calls(state: Any) -> list[dict[str, Any]]:
    """返回有对应成功 tool_result 的 tool_call。

    若 transcript 只留下截断字符串而非完整结果，宁可不把调用算作成功，
    避免审计误放行。
    """
    calls: list[dict[str, Any]] = []
    pending: dict[str, list[dict[str, Any]]] = {}
    for event in _events(state):
        if event.get("event") == "tool_call":
            pending.setdefault(str(event.get("name") or ""), []).append(event)
            continue
        if event.get("event") != "tool_result":
            continue
        name = str(event.get("name") or "")
        result = _tool_result_object(event.get("result_preview"))
        queue = pending.get(name) or []
        if queue:
            call = dict(queue.pop(0))
            if result is not None and result.get("status") == "success":
                call["result"] = result
                calls.append(call)
    return calls


def current_run_artifacts(state: Any, artifact_type: str) -> list[dict[str, Any]]:
    """Return artifacts genuinely produced by this run, not project history.

    ``State.list_artifacts`` deliberately supports cross-node/project reads.
    A node-local closure check is an ownership question, so it must filter on
    the immutable provenance stamped by ``State.save_artifact``.
    """
    current_run_id = str(getattr(state, "run_id", "") or "")
    records: list[dict[str, Any]] = []
    for item in state.list_artifacts(artifact_type) or []:
        artifact_id = str(item.get("id") or "")
        record = state.read_artifact(artifact_id) if artifact_id else None
        if str((record or {}).get("produced_by_run_id") or "") == current_run_id:
            records.append(item)
    return records


#: 可被追加式否定的 closure 草稿类型。raw/clean 是同一份执行证据的两半，
#: 只否定一半会让 closure 读到半份证据，故必须成对否定（由工具机械推导，
#: 不要求调用方自己配对）。
_CLOSURE_DRAFT_TYPES = ("experiment_log", "raw_results", "clean_results")


def _clean_results_raw_id(record: Any) -> str:
    """从 clean_results 正文里读出它引用的 raw_results id（读不出返回空）。"""
    try:
        payload = json.loads(str((record or {}).get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    manifest = payload.get("replay_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    return str(
        payload.get("raw_results_artifact_id")
        or manifest.get("raw_results_artifact_id")
        or ""
    ).strip()


def _linked_closure_draft(
    state: Any, artifact_id: str, record: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """返回与该 raw/clean 草稿成对的另一半，以及没能配上时的如实注记。

    配对关系只从 clean_results 自己声明的 ``raw_results_artifact_id`` 机械派生，
    不猜、不按时间就近匹配。experiment_log 没有配对。

    跨 run 的 partner 在**候选阶段**就过滤掉，不留给调用方当拒绝理由：本 run 的
    clean 正文由模型书写，它声明的 raw id 可以指向上一个 run 留下的产物，而那份
    raw 本来就不在本 run 的 closure 有效视图里（``current_run_artifacts`` 按
    ``produced_by_run_id`` 过滤），成对否定既无必要也无法完成。把它当作"配对失败"
    拒掉会造成一处反向激励：正文如实写出处的被罚，写一个不存在的 id 的反而当场
    放行（下面 partner 读不出即返回空配对）。这里返回空配对加一条注记，由调用方
    记进否定记录，账仍然是真的。
    """
    artifact_type = str((record or {}).get("type") or "")
    current_run_id = str(getattr(state, "run_id", "") or "")
    if artifact_type == "clean_results":
        raw_id = _clean_results_raw_id(record)
        if not raw_id:
            return "", None, None
        partner = state.read_artifact(raw_id)
        if not (isinstance(partner, dict)
                and partner.get("type") == "raw_results"):
            return "", None, {
                "declared_raw_results_artifact_id": raw_id,
                "unpaired_reason": "declared_partner_unreadable"}
        partner_run_id = str(partner.get("produced_by_run_id") or "")
        if partner_run_id != current_run_id:
            return "", None, {
                "declared_raw_results_artifact_id": raw_id,
                "unpaired_reason": "declared_partner_from_another_run",
                "partner_produced_by_run_id": partner_run_id or None}
        return raw_id, partner, None
    if artifact_type == "raw_results":
        for item in current_run_artifacts(state, "clean_results"):
            clean_id = str(item.get("id") or "")
            if not clean_id:
                continue
            clean = state.read_artifact(clean_id)
            if (isinstance(clean, dict)
                    and _clean_results_raw_id(clean) == artifact_id):
                return clean_id, clean, None
    return "", None, None


def _current_run_supersessions(state: Any) -> dict[str, dict[str, Any]]:
    """本 run 内的 experiment_log 否定记录：superseded_id → 记录摘要。

    否定通道是**追加式**的：误建的 experiment_log 不可删除（产物不可变），
    但可以由 ``supersede_closure_draft`` 追加一条不可变的
    ``experiment_log_supersession`` 记录把它从 canonical 计数中排除。
    这里只读事实，不做裁决；能否排除由 ``active_experiment_logs`` 判。
    """
    result: dict[str, dict[str, Any]] = {}
    current_run_id = str(getattr(state, "run_id", "") or "")
    for item in current_run_artifacts(state, "experiment_log_supersession"):
        supersession_id = str(item.get("id") or "")
        record = state.read_artifact(supersession_id) if supersession_id else None
        metadata = (record or {}).get("metadata") or {}
        if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
            continue
        try:
            payload = json.loads(str((record or {}).get("content") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        superseded_id = str(payload.get("superseded_id") or "")
        reason = str(payload.get("reason") or "").strip()
        payload_run_id = str(payload.get("run_id") or "")
        metadata_target = str(metadata.get("superseded_id") or "")
        # 旧格式（只否定 experiment_log）没有 artifact_type 字段：默认它，
        # 这样历史记录继续生效，也不会被误用来隐藏 raw/clean。
        artifact_type = str(
            payload.get("artifact_type") or "experiment_log").strip()
        linked_id = str(payload.get("linked_superseded_id") or "").strip()
        linked_type = str(payload.get("linked_artifact_type") or "").strip()
        if (
            not superseded_id
            or not reason
            or payload_run_id != current_run_id
            or metadata_target != superseded_id
            or artifact_type not in _CLOSURE_DRAFT_TYPES
            or str(metadata.get("artifact_type")
                   or "experiment_log").strip() != artifact_type
            or str(metadata.get("linked_superseded_id") or "").strip() != linked_id
            or bool(linked_id) != bool(linked_type)
            or (linked_type and linked_type not in _CLOSURE_DRAFT_TYPES)
        ):
            continue
        result[superseded_id] = {
            "supersession_id": supersession_id, "reason": reason,
            "artifact_type": artifact_type}
        if linked_id:
            result[linked_id] = {
                "supersession_id": supersession_id, "reason": reason,
                "artifact_type": linked_type}
    return result


def active_closure_artifacts(
    state: Any, artifact_type: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (未被否定的本 run 该类产物, 已被否定的 id 列表)。

    这是 closure 有效性的**唯一**视图：contract audit、operation completion 与
    路线封口都读它，避免两个读者对同一事实给出不同答案。
    frozen 产物是已验证证据：即使存在指向它的否定记录（不该有，但审计
    fail-closed），它也留在集合内 —— 否定通道只对未冻结的误建草稿有效。
    否定记录自己声明的 artifact_type 必须与被否定产物的实际类型一致，
    一条 experiment_log 的否定记录不能用来隐藏一份 raw_results。
    """
    if artifact_type not in _CLOSURE_DRAFT_TYPES:
        return current_run_artifacts(state, artifact_type), []
    supersessions = _current_run_supersessions(state)
    active: list[dict[str, Any]] = []
    superseded_ids: list[str] = []
    for item in current_run_artifacts(state, artifact_type):
        artifact_id = str(item.get("id") or "")
        supersession = supersessions.get(artifact_id)
        if supersession and supersession.get("artifact_type") == artifact_type:
            record = state.read_artifact(artifact_id) or {}
            if not ((record.get("metadata") or {}).get("frozen")):
                superseded_ids.append(artifact_id)
                continue
        active.append(item)
    return active, superseded_ids


def active_experiment_logs(state: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """experiment_log 的有效视图（``active_closure_artifacts`` 的既有入口名）。"""
    return active_closure_artifacts(state, "experiment_log")


def _latest_log(state: Any) -> dict[str, Any] | None:
    records, _ = active_experiment_logs(state)
    if not records:
        return None
    artifact_id = records[-1]["id"]
    record = state.read_artifact(artifact_id)
    if record is None:
        return None
    return {**record, "id": artifact_id}


def _is_auto_generated(record: dict[str, Any] | None) -> bool:
    """兜底记录由框架自己写，不是 agent 的科研产出。

    ``secondary_experiment_log_recovery`` 在 agent 没能留下 experiment_log 时
    自动补一份，其中固定含 ``verdict: inconclusive`` 和“未发现 methodological /
    dead_end finding”。若审计照单全收，框架就会用自己写的文本给自己发合格证 ——
    一个零工具调用的 run 也能过门。因此自动记录对 verdict 和 sediment 一律
    fail-closed，与 run_role 无关：它证明的恰恰是本次运行没有正常收尾。
    """
    metadata = (record or {}).get("metadata") or {}
    return bool(metadata.get("auto_generated"))


def _is_formal_run(state: Any) -> bool:
    """Reuse the existing formal-verdict contract; do not create a new stage state."""
    return bool(load_run_contract(state).get("requires_hypothesis_verdict"))


def audit_experiment_log_integrity(state: Any) -> dict[str, Any]:
    """One run has one canonical experiment log; aliases cannot replace frozen evidence.

    被 ``supersede_closure_draft`` 否定过的未冻结草稿不计入 canonical 计数
    （否定是追加的不可变事实，产物本身原样保留）；最终要求不放宽 ——
    过滤后仍必须恰好一个 canonical。
    """
    records, superseded_ids = active_experiment_logs(state)
    ids = [str(record.get("id") or "") for record in records]
    if not records:
        return {"passed": False, "n_logs": 0, "canonical_log_id": None,
                "superseded_ids": superseded_ids,
                "reason": "missing experiment_log"}
    if len(records) != 1:
        return {"passed": False, "n_logs": len(records), "canonical_log_id": ids[0],
                "artifact_ids": ids, "superseded_ids": superseded_ids,
                "reason": (
                    "one run may have only one canonical experiment_log; aliases cannot "
                    "replace frozen evidence。产物不可删除，但误建的多余草稿有出路："
                    "supersede_closure_draft(artifact_id=<误建的草稿 id>, reason=...) "
                    "追加不可变否定记录后即从计数中排除（frozen log 是已验证证据，"
                    "一律不可否定）")}
    return {"passed": True, "n_logs": 1, "canonical_log_id": ids[0],
            "superseded_ids": superseded_ids,
            "reason": "one canonical experiment_log"}


async def _supersede_closure_draft(
    state: Any, artifact_id: str, reason: str = "", **_: Any,
) -> dict[str, Any]:
    """追加式否定本 run 内误建的、未冻结的 experiment_log。

    不删除、不改写任何已有产物：否定本身是一条新的不可变
    ``experiment_log_supersession`` 记录（save_artifact 后立即 freeze），
    审计据此把被否定的草稿从 canonical 计数中排除。
    不变量：frozen log 是已验证证据，一律拒绝否定；否定记录冻结后不可变、
    不可重复；"恰好一个 canonical" 的最终要求不放宽。
    """
    artifact_id = str(artifact_id or "").strip()
    reason = str(reason or "").strip()
    if not reason:
        return {"status": "error", "error": (
            "reason 必填非空：说明这份 experiment_log 为何是误建"
            "（例如与 canonical log 重复的第二份草稿）。")}
    record = state.read_artifact(artifact_id) if artifact_id else None
    artifact_type = str((record or {}).get("type") or "")
    if not isinstance(record, dict) or artifact_type not in _CLOSURE_DRAFT_TYPES:
        return {"status": "error", "error": (
            f"artifact_id={artifact_id!r} 不是可读的 closure 草稿；本工具只否定 "
            f"{list(_CLOSURE_DRAFT_TYPES)} 三类（否定记录本身不可再否定）。")}
    current_run_id = str(getattr(state, "run_id", "") or "")
    if str(record.get("produced_by_run_id") or "") != current_run_id:
        return {"status": "error", "error": (
            f"只能否定本 run 产出的 {artifact_type}；历史 run 的冻结证据不在本工具权限内。")}
    if (record.get("metadata") or {}).get("frozen"):
        return {"status": "error", "error": (
            f"该 {artifact_type} 已冻结：frozen 产物是已验证证据，一律不可否定。"
            "若多余的是另一份未冻结草稿，请否定那一份。")}
    supersessions = _current_run_supersessions(state)
    existing = supersessions.get(artifact_id)
    if existing:
        return {"status": "error",
                "error": f"该 {artifact_type} 已有否定记录；否定记录不可变、不可重复。",
                "supersession_id": existing["supersession_id"]}
    # raw/clean 是同一份执行证据的两半：能一起否定就一条记录同时否定两者，
    # 不存在"写了一半"的中间态。
    #
    # 另一半**不能**随本次一起否定时（已冻结 / 已被否定 / 属于别的 run），
    # 如实记账后只否定本半，不拒绝。#879 复审在这里判错了方向：拒绝造出的
    # 恰恰是它声称要防的半份视图 —— 实测同一份 raw 下两份 clean 草稿，否定
    # 第一份成功（成对带走 raw），否定第二份被拒，终态是 active clean 非空、
    # active raw 为空；放行第二份才把账本收敛回一致。frozen 的那一半原样留在
    # 有效视图里（active_closure_artifacts 对 frozen 无条件保留），本来就藏
    # 不掉，所以"只隐藏未冻结那一半"这个危害在代码上并不成立。
    linked_id, linked_record, unpaired = _linked_closure_draft(
        state, artifact_id, record)
    linked_type = str((linked_record or {}).get("type") or "") if linked_id else ""
    if linked_id:
        if (linked_record.get("metadata") or {}).get("frozen"):
            unpaired = {"linked_artifact_id": linked_id,
                        "linked_artifact_type": linked_type,
                        "unpaired_reason": "linked_half_frozen"}
            linked_id, linked_record, linked_type = "", None, ""
        elif supersessions.get(linked_id):
            unpaired = {
                "linked_artifact_id": linked_id,
                "linked_artifact_type": linked_type,
                "unpaired_reason": "linked_half_already_superseded",
                "linked_supersession_id":
                    supersessions[linked_id]["supersession_id"]}
            linked_id, linked_record, linked_type = "", None, ""
    payload_body = {
        "superseded_id": artifact_id, "artifact_type": artifact_type,
        "reason": reason, "run_id": current_run_id,
    }
    metadata_body: dict[str, Any] = {
        "superseded_id": artifact_id, "artifact_type": artifact_type,
    }
    if linked_id:
        payload_body["linked_superseded_id"] = linked_id
        payload_body["linked_artifact_type"] = linked_type
        metadata_body["linked_superseded_id"] = linked_id
        metadata_body["linked_artifact_type"] = linked_type
    elif unpaired:
        # 只否定了一半：把"另一半是什么、为什么没一起否定"钉进不可变记录。
        # 跨 run 的那一半不写进 linked_superseded_id —— 本 run 的记录不去声明
        # 别的 run 的证据，那才是真正的账本完整性顾虑。
        payload_body["unpaired_half"] = unpaired
        metadata_body["unpaired_half"] = unpaired
    payload = json.dumps(payload_body, ensure_ascii=False, sort_keys=True)
    # artifact 类型保持 experiment_log_supersession 不变：它是已落盘的线上格式，
    # 历史 run 的冻结否定记录按它读取。工具改名只影响模型面词表，不动线上兼容面。
    saved = state.save_artifact(
        "experiment_log_supersession", f"supersede_{artifact_id}", payload,
        metadata=metadata_body)
    supersession_id = str(saved.get("id") or "")
    # 复用唯一的 freeze 机制把否定记录钉成不可变事实；冻不上就不算否定成功。
    from shared.tools.library.artifacts_extra import _freeze_artifact
    frozen = await _freeze_artifact(
        state=state, artifact_id=supersession_id,
        reason=f"immutable negation of mistakenly created {artifact_id}")
    if frozen.get("status") != "success":
        return {"status": "error", "supersession_id": supersession_id,
                "error": "否定记录未能冻结为不可变，本次否定不生效；见 freeze_result。",
                "freeze_result": frozen}
    state.append_transcript(
        "experiment_log_superseded",
        superseded_id=artifact_id, artifact_type=artifact_type,
        linked_superseded_id=linked_id or None,
        unpaired_half=unpaired or None,
        supersession_id=supersession_id, reason=reason)
    result = {"status": "success", "supersession_id": supersession_id,
              "superseded_id": artifact_id, "artifact_type": artifact_type,
              "reason": reason,
              "experiment_log_integrity": audit_experiment_log_integrity(state)}
    if linked_id:
        result["linked_superseded_id"] = linked_id
        result["linked_artifact_type"] = linked_type
    elif unpaired:
        result["unpaired_half"] = unpaired
    return result


register_tool(ToolDefinition(
    name="supersede_closure_draft",
    description=(
        "追加一条不可变否定记录（experiment_log_supersession），把本 run 内误建的、"
        "未冻结的 closure 草稿从有效视图中排除。可否定的类型：experiment_log、"
        "raw_results、clean_results。用于误建第二份 log，或先按 scientific 存了草稿、"
        "后改判 operation 导致 closure 冲突时的恢复通道。"
        "raw_results 与 clean_results 是同一份执行证据的两半：给出其中任意一个 id，"
        "工具会按 clean_results 自己声明的 raw_results_artifact_id 机械找到另一半，"
        "用同一条记录成对否定，不存在只隐藏一半的中间态。"
        "不删除也不改写任何产物；frozen 产物是已验证证据，一律拒绝否定；"
        "否定记录本身冻结后不可变、不可再否定。过滤后仍要求恰好一份 canonical 证据。"),
    parameters_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string",
                            "description": ("要否定的本 run 未冻结 closure 草稿 id："
                                            "experiment_log / raw_results / "
                                            "clean_results 之一；raw 与 clean 给任意"
                                            "一个即可，另一半自动成对否定")},
            "reason": {"type": "string", "description": "为何这份草稿是误建（必填非空）"},
        },
        "required": ["artifact_id", "reason"],
    },
    allowed_node_types=["experiment"], risk_level="low",
), _supersede_closure_draft)


# ── 过渡期别名：旧名 supersede_experiment_log ─────────────────────────────────
#
# 正名是上面的 `supersede_closure_draft` —— 它否定的是**误建的 closure 草稿**，
# 不是 experiment_log 本身；旧名会让模型以为可以否定一份 log。
#
# 旧名与正名挂在同一个实现、同一份 schema 上，是**有界的过渡期兼容**：
#
# - 兼容谁：仓库根 `tests/test_instructed_tools_are_in_whitelist.py` 断言的是旧名，
#   而本节点工具面已是正名；该根测试不在本节点 scope 内（#923）。两名并存让任一
#   版本的断言都能通过。
# - 兼容的是什么：**工具名解析**，不是行为。旧名复用 `_supersede_closure_draft`
#   与正名逐字相同的 `parameters_schema`，语义、返回、事件、错误码完全一致。
# - 风险：工具面多一条（与 #919 收敛工具面方向相反），模型会看到两个同义工具。
#   缓解：旧名的 description 首句标注它是旧名并指向正名；拒绝理由只点正名。
# - 测试：`test_supersede_tool_alias.py`。
# - owner：本节点。
# - 删除条件：**#923 落地**后删除本段与 harness.yaml 里的旧名声明。
_CANONICAL_SUPERSEDE = _REGISTRY.tools["supersede_closure_draft"]
register_tool(ToolDefinition(
    name="supersede_experiment_log",
    description=("【过渡期旧名，正名是 supersede_closure_draft，两者完全等价】"
                 + _CANONICAL_SUPERSEDE.description),
    parameters_schema=_CANONICAL_SUPERSEDE.parameters_schema,
    allowed_node_types=list(_CANONICAL_SUPERSEDE.allowed_node_types),
    risk_level=_CANONICAL_SUPERSEDE.risk_level,
), _supersede_closure_draft)


def _resolve_evidence(state: Any, evidence: Any) -> tuple[int, list[str]]:
    """用 KB 既有的 ``get_kb_record`` 解析引用，不另写第二个解析器。

    返回 (可解析数, 无法解析的 id)。解析不到即视为 phantom —— 一个指不到任何
    KB 记录的 evidence_id，和没有证据是一回事。
    """
    if not isinstance(evidence, list):
        return 0, []
    resolved = 0
    unresolved: list[str] = []
    for raw in evidence:
        item = str(raw).strip()
        if not item:
            continue
        entity = ("chunks" if item.startswith("chunk_")
                  else "claims" if item.startswith("claim_") else None)
        record = None
        if entity is not None:
            try:
                record = state.get_kb_record(entity, item)
            except Exception:
                record = None
        if record:
            resolved += 1
        else:
            unresolved.append(item)
    return resolved, unresolved


_VERDICT_LABEL_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?verdict(?:\*\*)?\s*[:：]\s*"
    r"(?:\*\*)?([a-z_]+)\b(?:\*\*)?",
    re.IGNORECASE | re.MULTILINE,
)


def _verdict_labels(content: str) -> tuple[str, ...]:
    """Return anchored verdict values across supported Markdown forms.

    This is the single syntax parser shared by contract auditing and the
    sediment declaration guard. Semantic evidence requirements deliberately
    remain in their respective audit functions.
    """
    return tuple(match.group(1).lower() for match in _VERDICT_LABEL_RE.finditer(content))


def _has_explicit_inconclusive_reasoning(content: str) -> bool:
    if "inconclusive" not in _verdict_labels(content):
        return False
    has_reason = bool(re.search(r"(?i)(reason|原因|confound|缺少|判不动|无法判)", content))
    has_next = bool(re.search(
        r"(?i)(需要|补充|redirect_upstream|重跑|metric|样本|need|next[_\s]+step|"
        r"formal\s+hypothesis|register(?:ed|ing)?\s+.*claim|claim\s+must\s+be\s+registered)",
        content,
    ))
    return has_reason and has_next


def _has_provisional_execution_assessment(content: str) -> bool:
    """A provisional experiment assessment is evidence handoff, not a claim flip."""
    if "provisional" not in _verdict_labels(content):
        return False
    has_measurement = bool(re.search(r"(?i)(measured|metric|measurement|测量|指标|结果)", content))
    has_comparison = bool(re.search(r"(?i)(threshold|against|compared|comparison|阈值|对比|比较)", content))
    has_handoff = bool(re.search(r"(?i)(analysis|research_state|hypothesis|交给|交回|下一步)", content))
    return has_measurement and has_comparison and has_handoff


def _valid_inconclusive_declarations(state: Any, log_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the node-local frozen-log addendum path; never accept auto records.

    The tool's own receipt is authoritative: it is written verbatim after every
    error return, so its presence *is* the success signal, and its fields are
    never truncated.  The tool-result scan below stays only as a fallback for
    transcripts recorded before the receipt existed (or produced by an external
    runner) — it can add declarations, never remove one.
    """
    if _is_auto_generated(log_record):
        return []
    log_id = str(log_record.get("id") or "")
    valid: list[dict[str, Any]] = [
        {"log_frozen": bool(event.get("log_frozen")),
         "rendered_in_log": bool(event.get("rendered_in_log"))}
        for event in _receipts(state, _INCONCLUSIVE_RECEIPT)
        if str(event.get("experiment_log_id") or "") == log_id
        and len(str(event.get("reason") or "").strip()) >= _MIN_DECLARED_REASON_CHARS
        and len(str(event.get("next_step") or "").strip()) >= _MIN_DECLARED_NEXT_STEP_CHARS
    ]
    if valid:
        return valid
    for call in _successful_tool_calls(state):
        if call.get("name") != "declare_inconclusive_verdict":
            continue
        args, result = call.get("args") or {}, call.get("result") or {}
        if (str(result.get("experiment_log_id") or "") == log_id
                and len(str(args.get("reason") or "").strip()) >= 20
                and len(str(args.get("next_step") or "").strip()) >= 10):
            valid.append({"log_frozen": bool(result.get("log_frozen")),
                          "rendered_in_log": bool(result.get("rendered_in_log"))})
    return valid


def audit_verdict(state: Any) -> dict[str, Any]:
    successful = _successful_tool_calls(state)
    contract = load_run_contract(state)
    formal = bool(contract.get("requires_hypothesis_verdict"))
    if not formal:
        log_record = _latest_log(state)
        auto_record = _is_auto_generated(log_record)
        passed = bool(log_record) and not auto_record
        role_reason = run_role_non_applicability_reason(contract)
        return {
            "passed": passed, "applicable": False,
            "n_updates": 0, "n_valid_updates": 0, "has_inconclusive": False,
            "has_explicit_declaration": False, "late_declaration": False,
            "auto_generated_record": auto_record, "formal_run": formal,
            "unresolved_evidence": [],
            "reason": (
                "all runs require an agent-authored experiment_log"
                if not passed else
                ("secondary/non-formal scientific run has no "
                 "hypothesis-verdict gate")
                if role_reason else
                "non-scientific run has no hypothesis-verdict gate"
            ),
            **({"not_applicable_reason": role_reason} if role_reason else {}),
        }
    updates: list[dict[str, Any]] = []
    unresolved_all: list[str] = []
    for call in successful:
        if call.get("name") not in _VERDICT_TOOLS:
            continue
        args = call.get("args") or {}
        status = str(args.get("new_status") or args.get("status") or "")
        claim_id = str(args.get("claim_id") or "")
        reasoning = str(args.get("reasoning") or "")
        evidence = args.get("evidence_ids")
        resolved, unresolved = _resolve_evidence(state, evidence)
        unresolved_all.extend(unresolved)
        valid = bool(claim_id and status in _VERDICT_STATUSES and len(reasoning) >= 10)
        if status in {"validated", "refuted"}:
            valid = valid and isinstance(evidence, list) and bool(evidence)
            # 正式运行才硬性要求引用能解析：secondary 不进正式分析，收紧只会
            # 逼出形式化引用。无论角色都把 unresolved 记进事件，便于复核。
            if formal:
                valid = valid and resolved > 0 and not unresolved
        updates.append({
            "claim_id": claim_id,
            "new_status": status,
            "reasoning_length": len(reasoning),
            "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
            "evidence_resolved": resolved,
            "evidence_unresolved": unresolved,
            "valid": valid,
        })

    log_record = _latest_log(state) or {}
    auto_record = _is_auto_generated(log_record)
    content = str(log_record.get("content") or "")
    inconclusive = _has_explicit_inconclusive_reasoning(content)
    provisional = _has_provisional_execution_assessment(content)
    declarations = _valid_inconclusive_declarations(state, log_record)
    explicit_declaration = bool(declarations)
    passed = not auto_record and (provisional or inconclusive or explicit_declaration)
    if auto_record:
        reason = ("本 run 只有自动兜底记录，agent 未产出正式 experiment_log："
                  "自动记录不能形成 verdict")
    elif provisional:
        reason = "experiment_log 记录了可复核的 provisional 执行层评估，等待 Analysis 裁决"
    elif inconclusive:
        reason = "experiment_log 明确记录 inconclusive 原因和后续补充"
    elif explicit_declaration:
        reason = "declare_inconclusive_verdict 已留下绑定 experiment_log 的可审计说明"
    else:
        reason = (
            "缺少**执行层评估**（这不是科学裁决 —— validated/refuted 归 Analysis，"
            "本节点只写 provisional 或 inconclusive）。三条合法出路任选其一："
            "① experiment_log 写 `verdict: provisional` + 实测值/阈值对比/交接说明；"
            "② experiment_log 写 `verdict: inconclusive` + 原因 + 下一步；"
            "③ 调用 declare_inconclusive_verdict(reason, next_step)。"
            "环境不可行导致无法执行时，`inconclusive` 正是正确答案，不是伪造。"
            + _unreadable_note(state, "declare_inconclusive_verdict"))
    return {
        "passed": passed,
        "n_updates": len(updates),
        "n_valid_updates": sum(1 for item in updates if item["valid"]),
        "has_provisional": provisional,
        "has_inconclusive": inconclusive,
        "has_explicit_declaration": explicit_declaration,
        "late_declaration": any(item["log_frozen"] for item in declarations),
        "auto_generated_record": auto_record,
        "formal_run": formal,
        "unresolved_evidence": sorted(set(unresolved_all)),
        "reason": reason,
    }


def audit_sediment(state: Any) -> dict[str, Any]:
    successful = _successful_tool_calls(state)
    contract = load_run_contract(state)
    formal = bool(contract.get("requires_hypothesis_verdict"))
    if not formal:
        log_record = _latest_log(state)
        auto_record = _is_auto_generated(log_record)
        passed = bool(log_record) and not auto_record
        role_reason = run_role_non_applicability_reason(contract)
        return {
            "passed": passed, "applicable": False,
            "n_claims": 0, "has_explicit_none": False,
            "has_explicit_declaration": False, "late_declaration": False,
            "auto_generated_record": auto_record, "formal_run": formal,
            "unresolved_evidence": [],
            "reason": (
                "all runs require an agent-authored experiment_log"
                if not passed else
                "secondary/non-formal scientific run has no sediment gate"
                if role_reason else
                "non-scientific run has no sediment gate"
            ),
            **({"not_applicable_reason": role_reason} if role_reason else {}),
        }
    claims: list[dict[str, Any]] = []
    unresolved_all: list[str] = []
    for call in successful:
        if call.get("name") != "create_claim":
            continue
        args = call.get("args") or {}
        claim_type = str(args.get("claim_type") or "")
        if claim_type not in _SEDIMENT_TYPES:
            continue
        resolved, unresolved = _resolve_evidence(state, args.get("sources"))
        unresolved_all.extend(unresolved)
        # 正式运行的沉淀要能追回证据：没有可解析 sources 的 claim 会跨项目复利，
        # 一条无据的 dead_end 会让后来的 run 放弃一条其实可行的路。
        claims.append({
            "claim_type": claim_type,
            "evidence_resolved": resolved,
            "valid": (not formal) or (resolved > 0 and not unresolved),
        })
    claims = [item for item in claims if item["valid"]]

    log_record = _latest_log(state)
    auto_record = _is_auto_generated(log_record)
    explicit_none = not auto_record and bool(re.search(
        r"(?i)(未发现|没有发现|无).*?(methodological|dead.?end|方法学|死路)",
        str((log_record or {}).get("content") or ""),
    ))
    log_id = str((log_record or {}).get("id") or "")
    # 与 verdict 同理：先认工具自己的 receipt（完整、不截断），读不到才回落到
    # 会被 _brief 压坏的 tool_result preview。
    declarations: list[dict[str, Any]] = [] if auto_record else [
        {"reason": str(event.get("reason") or "").strip(),
         "log_frozen": bool(event.get("log_frozen")),
         "rendered_in_log": bool(event.get("rendered_in_log"))}
        for event in _receipts(state, _NO_SEDIMENT_RECEIPT)
        if log_id and str(event.get("experiment_log_id") or "") == log_id
        and len(str(event.get("reason") or "").strip()) >= _MIN_DECLARED_REASON_CHARS
    ]
    for call in [] if declarations else successful:
        if call.get("name") != "declare_no_sediment":
            continue
        args = call.get("args") or {}
        result = call.get("result") or {}
        reason = str(args.get("reason") or "").strip()
        declared_log_id = str(result.get("experiment_log_id") or "")
        if (len(reason) >= 20 and not auto_record
                and log_id and declared_log_id == log_id):
            declarations.append({
                "reason": reason,
                "log_frozen": bool(result.get("log_frozen")),
                "rendered_in_log": bool(result.get("rendered_in_log")),
            })
    explicit_declaration = bool(declarations)
    passed = not auto_record and (bool(claims) or explicit_none or explicit_declaration)
    return {
        "passed": passed,
        "n_claims": len(claims),
        "has_explicit_none": explicit_none,
        "has_explicit_declaration": explicit_declaration,
        "late_declaration": any(item["log_frozen"] for item in declarations),
        "auto_generated_record": auto_record,
        "formal_run": formal,
        "unresolved_evidence": sorted(set(unresolved_all)),
        "reason": (
            "本 run 只有自动兜底记录，agent 未产出正式 experiment_log："
            "自动记录不能构成 sediment 结论"
            if auto_record
            else "成功创建 methodological/dead_end claim"
            if claims
            else "experiment_log 明确说明本次没有可沉淀 finding"
            if explicit_none
            else "declare_no_sediment 已留下可审计的无 finding 声明"
            if explicit_declaration
            else ("没有 sediment claim，也没有明确的无 finding 说明。两条合法出路："
                  "① create_claim(claim_type='methodological' 或 'dead_end', sources=[可解析证据])；"
                  "② 调用 declare_no_sediment(reason) 说明为何本次无可沉淀 finding。")
            + _unreadable_note(state, "declare_no_sediment", "create_claim")
        ),
    }


def audit_execution_record(state: Any) -> dict[str, Any]:
    """Report optional KB experiment-record registration for this run.

    Frozen artifacts are the execution proof. ``create_experiment`` is an
    optional KB index for a reusable experimental action, so an absent or
    failed registration must never recast completed execution as incomplete.
    When a registration is present, still verify that it belongs to this run
    and was made after the canonical log was frozen.
    """
    contract = load_run_contract(state)
    run_role = str(contract.get("run_role") or "secondary")
    integrity = audit_experiment_log_integrity(state)
    canonical_log_id = str(integrity.get("canonical_log_id") or "")
    canonical_log = state.read_artifact(canonical_log_id) if canonical_log_id else None
    log_frozen = bool(((canonical_log or {}).get("metadata") or {}).get("frozen"))
    current_run_id = str(getattr(state, "run_id", "") or "")

    frozen_before_registration = False
    registrations: list[dict[str, Any]] = []
    for call in _successful_tool_calls(state):
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        result = call.get("result") or {}
        if name == "freeze_artifact":
            requested_id = str(args.get("artifact_id") or "")
            returned_id = str(result.get("artifact_id") or requested_id)
            if canonical_log_id and requested_id == canonical_log_id and returned_id == canonical_log_id:
                frozen_before_registration = True
            continue
        if name != _EXECUTION_RECORD_TOOL:
            continue
        experiment_id = str(result.get("id") or "")
        record = state.get_kb_record("experiments", experiment_id) if experiment_id else None
        bound_to_current_run = bool(
            isinstance(record, dict)
            and str(record.get("run_by_run_id") or "") == current_run_id
        )
        registrations.append({
            "experiment_id": experiment_id or None,
            "outcome": str(args.get("outcome") or ""),
            "frozen_log_precedes_registration": frozen_before_registration,
            "record_exists": isinstance(record, dict),
            "bound_to_current_run": bound_to_current_run,
        })

    verified = [item for item in registrations if (
        integrity.get("passed") and log_frozen
        and item["frozen_log_precedes_registration"]
        and item["record_exists"] and item["bound_to_current_run"]
    )]
    auto_record = _is_auto_generated(_latest_log(state))

    if not registrations:
        registration_status, passed, reason = "not_registered", True, (
            "本 run 未登记 KB experiment record；KB 沉淀是可选索引，不影响执行完成。")
    elif verified:
        registration_status, passed, reason = "registered", True, (
            "已登记当前 run 的 experiment record，且登记发生在 canonical log 冻结之后")
    else:
        registration_status, passed, reason = "invalid", False, (
            "KB experiment record 登记未能证明绑定当前 run 的冻结 canonical experiment_log："
            "检查 create_experiment 返回的 experiment id、KB record.run_by_run_id 与冻结/登记顺序。"
            + _unreadable_note(state, _EXECUTION_RECORD_TOOL))

    return {
        "passed": passed,
        "required": False,
        "run_role": run_role,
        "applicable": bool(registrations),
        "registration_status": registration_status,
        "canonical_log_id": canonical_log_id or None,
        "canonical_log_frozen": log_frozen,
        "unreadable_tool_results": _unreadable_tool_results(state),
        "n_registrations": len(registrations),
        "n_registered_after_log_freeze": sum(
            bool(item["frozen_log_precedes_registration"]) for item in registrations),
        "n_bound_to_current_run": sum(bool(item["bound_to_current_run"]) for item in registrations),
        "n_verified_registrations": len(verified),
        "registrations": registrations,
        "auto_generated_record": auto_record,
        "reason": reason,
    }


def audit_citation_binding(state: Any) -> dict[str, Any]:
    """Fail closed when the canonical log cites missing KB claims/chunks.

    Citation validation is emitted when an experiment_log is saved.  Keeping
    it in the same audit as the freeze gate prevents a later Core QC from
    discovering a scientific-capital failure after Experiment has claimed a
    completed workflow.
    """
    record = _latest_log(state)
    if not record:
        return {"passed": False, "applicable": True, "reason": "missing experiment_log"}
    artifact_id = str(record.get("id") or "")
    content = str(record.get("content") or "")
    cited_ids = sorted(set(re.findall(r"\b(?:claim|chunk)_[A-Za-z0-9_-]+\b", content)))
    citations = [
        event for event in _events(state)
        if event.get("event") == "citation_validation"
        and event.get("artifact_id") == artifact_id
        and event.get("artifact_type") == "experiment_log"
    ]
    latest = citations[-1] if citations else {}
    if not cited_ids:
        return {
            "passed": True, "applicable": False, "artifact_id": artifact_id,
            "n_cited": 0, "n_phantom": 0,
            "reason": "canonical experiment_log has no KB claim/chunk citations",
        }
    if not latest:
        return {
            "passed": False, "applicable": True, "artifact_id": artifact_id,
            "n_cited": len(cited_ids), "n_phantom": None,
            "reason": "citation_validation is missing for canonical experiment_log",
        }
    n_phantom = int(latest.get("n_phantom", 0) or 0)
    passed = latest.get("passed") is True and n_phantom == 0
    return {
        "passed": passed, "applicable": True, "artifact_id": artifact_id,
        "n_cited": int(latest.get("n_cited", len(cited_ids)) or 0),
        "n_phantom": n_phantom,
        "phantom_ids": list(latest.get("phantom_ids") or []),
        "reason": ("citation binding verified" if passed else
                   f"canonical experiment_log has {n_phantom} unresolved KB citation(s)"),
    }


def audit_terminal_failure_record(state: Any) -> dict[str, Any]:
    """Validate framework-owned failure evidence without certifying science."""
    record = _latest_log(state) or {}
    metadata = record.get("metadata") or {}
    applicable = bool(metadata.get("terminal_failure_record"))
    if not applicable:
        return {"applicable": False, "passed": True}

    content = str(record.get("content") or "")
    artifact_id = str(record.get("id") or "")
    citations = [
        event for event in _events(state)
        if event.get("event") == "citation_validation"
        and event.get("artifact_id") == artifact_id
        and event.get("artifact_type") == "experiment_log"
    ]
    latest_citation = citations[-1] if citations else {}
    required_markers = (
        "verdict: inconclusive",
        "credibility: invalid",
        "## Methodological / Dead End",
        "returncode:",
        "log_paths:",
        "next_step:",
    )
    missing_markers = [marker for marker in required_markers if marker not in content]
    frozen = bool(metadata.get("frozen"))
    citation_passed = (latest_citation.get("passed") is True
                       and int(latest_citation.get("n_phantom", 1) or 0) == 0)
    passed = frozen and citation_passed and not missing_markers
    return {
        "applicable": True,
        "passed": passed,
        "artifact_id": artifact_id,
        "frozen": frozen,
        "citation_validation_present": bool(latest_citation),
        "citation_validation_passed": citation_passed,
        "n_cited_claims": int(latest_citation.get("n_cited", 0) or 0),
        "n_phantom_claims": int(latest_citation.get("n_phantom", 0) or 0),
        "missing_markers": missing_markers,
        "recommended_action": metadata.get("recommended_action"),
    }




def _clean_results_payload(record: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode clean_results once so every freeze check sees the same payload."""
    content = record.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, ["clean_results content is empty"]
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        try:
            import yaml
            payload = yaml.safe_load(content)
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return None, ["clean_results must be a structured object"]
    return payload, []


def _numeric_result_errors(payload: Any, path: str = "$") -> list[str]:
    """Reject NaN and infinities before numerical evidence becomes immutable."""
    if isinstance(payload, float) and (payload != payload or payload in (float("inf"), float("-inf"))):
        return [f"non-finite numeric value at {path}"]
    if isinstance(payload, dict):
        return [error for key, value in payload.items() for error in _numeric_result_errors(value, f"{path}.{key}")]
    if isinstance(payload, list):
        return [error for index, value in enumerate(payload) for error in _numeric_result_errors(value, f"{path}[{index}]")]
    return []


def _values_at_result_path(payload: Any, path: str) -> list[Any]:
    values = [payload]
    for segment in path.split("."):
        next_values: list[Any] = []
        for value in values:
            if segment == "*" and isinstance(value, list):
                next_values.extend(value)
            elif isinstance(value, dict) and segment in value:
                next_values.append(value[segment])
        values = next_values
    return values


def _prereg_result_invariants(state: Any) -> dict[str, Any]:
    contract = load_run_contract(state)
    artifact_id = str(contract.get("prereg_artifact_id") or "")
    record = state.read_artifact(artifact_id) if artifact_id else None
    metadata = (record or {}).get("metadata") or {}
    execution = metadata.get("execution_contract") if isinstance(metadata, dict) else {}
    invariants = (metadata.get("result_invariants") if isinstance(metadata, dict) else None)
    if invariants is None and isinstance(execution, dict):
        invariants = execution.get("result_invariants")
    return invariants if isinstance(invariants, dict) else {}


def _observed_type(value: Any) -> str:
    """回显模型实际传了什么。"requires X" 对一个已经传了 X 的模型没有信息量。"""
    if value is None:
        return "nothing"
    return {
        bool: "a boolean", int: "a number", float: "a number",
        str: "a string", list: "a list", dict: "an object",
    }.get(type(value), type(value).__name__)


def _source_hashes_skeleton(raw_payload: dict[str, Any] | None) -> dict[str, str]:
    """从已冻结 raw manifest 机械派生一份可直接采用的 source_hashes。

    门本来就要拿这批 sha256 做覆盖检查 —— 手里握着正确答案却只用来打叉，是让
    模型手抄一份机器自己能生成的表。这份映射不含任何科学判断：合法答案集合由
    已冻结 manifest 唯一确定（冻结时 verify_files=True 逐字节重算过），手抄与
    照抄产出同一份内容。真正需要模型判断的是绑哪一份 raw，那由它自己声明的
    raw_results_artifact_id 决定，派生只跟着那个 id 走。

    key 是标签，覆盖检查只看 values；但 manifest 允许同名文件共存（去重按解析
    后的全路径，见 _validate_raw_results_manifest），basename 做 key 会静默吞掉
    一个 hash，派生出的 payload 反被下面的覆盖检查拒掉 —— 报错自相矛盾比不给
    答案更糟。所以重名的那一组整组退回绝对路径。
    """
    files = (raw_payload or {}).get("files")
    if not isinstance(files, list):
        return {}
    entries: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        if path and _RAW_SHA256_RE.fullmatch(digest):
            entries.append((path, digest))
    name_counts: dict[str, int] = {}
    for path, _ in entries:
        name_counts[PurePosixPath(path).name] = name_counts.get(PurePosixPath(path).name, 0) + 1
    skeleton: dict[str, str] = {}
    for path, digest in entries:
        name = PurePosixPath(path).name
        skeleton[path if name_counts.get(name, 0) > 1 else name] = digest
    return skeleton


def _source_hashes_error(observed: str, raw_payload: dict[str, Any] | None) -> str:
    """形状要求同源自契约声明，再补上观测态和（能派生时）可直接采用的 payload。"""
    message = contract_requirement(CLEAN_RESULTS_CONTRACT, "replay_manifest.source_hashes") + observed
    skeleton = _source_hashes_skeleton(raw_payload)
    if skeleton:
        message += "; adopt: " + json.dumps({"source_hashes": skeleton}, sort_keys=True)
    return message


def _replay_manifest_errors(state: Any, payload: dict[str, Any]) -> list[str]:
    """完成态、可供 Analysis 使用的结果必须带可回放链。

    门不放宽：source_hashes 的 values 仍须覆盖冻结 raw 的每一个 sha256。变的是
    拒绝方式 —— 按实际收到的类型分叉，并在 raw 绑定有效时直接给出可采用的
    payload，让模型一次构造对，而不是从一句 "requires source_hashes" 反推形状。
    """
    replay = payload.get("replay_manifest")
    if not isinstance(replay, dict):
        return [contract_requirement(CLEAN_RESULTS_CONTRACT, "replay_manifest")
                + f"; got {_observed_type(replay)}"]

    errors: list[str] = []
    raw_id = replay.get("raw_results_artifact_id")
    raw_payload: dict[str, Any] | None = None
    if not isinstance(raw_id, str) or not raw_id.strip():
        errors.append(
            contract_requirement(CLEAN_RESULTS_CONTRACT, "replay_manifest.raw_results_artifact_id")
            + f"; got {_observed_type(raw_id)}"
        )
    else:
        # 已知限制：这里只验类型与 frozen，不验 produced_by_run_id —— 绑错 run
        # 的 raw 仍能通过（既有缺口，见 audit_result_evidence 另行要求本 run 有
        # 冻结 raw 但不核对 clean 绑的是不是它）。派生使错绑更省力，补 run 归属
        # 校验是独立的一步，不在本次修复范围内。
        raw_record, raw_errors = _frozen_raw_results_record(state, raw_id)
        if raw_errors:
            # 缺陷在那份已冻结的 raw 里，不在这份 clean_results 里。此时不派生：
            # 从残缺 manifest 生成的 skeleton 会把模型引向错的方向。
            errors.extend(raw_errors)
        elif isinstance(raw_record, dict):
            raw_payload, _ = _raw_results_payload(raw_record)

    hashes = replay.get("source_hashes")
    if not isinstance(hashes, dict) or not hashes:
        observed = "an empty object" if isinstance(hashes, dict) else _observed_type(hashes)
        errors.append(_source_hashes_error(f"; got {observed}", raw_payload))
    elif raw_payload is not None:
        declared = {str(item.get("sha256") or "").lower()
                    for item in raw_payload.get("files", [])
                    if isinstance(item, dict)}
        referenced = {str(value).lower() for value in hashes.values()
                      if isinstance(value, str)}
        missing_hashes = sorted(value for value in declared if value and value not in referenced)
        if missing_hashes:
            errors.append(_source_hashes_error(
                "; omits raw_results SHA-256: " + ", ".join(missing_hashes), raw_payload))
    return errors


def _validate_clean_results_payload(state: Any, record: dict[str, Any]) -> list[str]:
    """Validate the minimum machine-readable result contract before freezing."""
    payload, errors = _clean_results_payload(record)
    if payload is None:
        return errors
    errors.extend(_numeric_result_errors(payload))
    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        errors.append(contract_requirement(CLEAN_RESULTS_CONTRACT, "status"))
    not_replayable = payload.get("not_replayable")
    if not_replayable is not None and not isinstance(not_replayable, bool):
        errors.append(contract_requirement(CLEAN_RESULTS_CONTRACT, "not_replayable"))
    if not_replayable is True:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(
                "not_replayable clean_results requires a non-empty reason")
    # operational run 的收尾产物不是科学测量，不收重放清单（那里另有自己的门）。
    # 这个 execution_mode 守卫必须留着：没有它，同一份报错会一边教模型补
    # replay_manifest、一边告诉它这份产物根本不该有科学结论 —— 两个门合成一个
    # 把模型往反方向推的陷阱。
    if (status == "completed" and not_replayable is not True
            and load_run_contract(state).get("execution_mode") != "operational"):
        errors.extend(_replay_manifest_errors(state, payload))
    for path, bounds in _prereg_result_invariants(state).items():
        if not isinstance(bounds, dict):
            errors.append(f"result invariant {path} must be an object")
            continue
        invalid_bounds = [key for key in ("min", "max") if key in bounds and (not isinstance(bounds[key], (int, float)) or isinstance(bounds[key], bool))]
        if invalid_bounds:
            errors.append("result invariant " + str(path) + " has non-numeric bounds")
            continue
        values = _values_at_result_path(payload, str(path))
        if not values:
            errors.append(f"result invariant path has no values: {path}")
            continue
        for value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"result invariant {path} must resolve to numbers")
                continue
            if "min" in bounds and value < bounds["min"]:
                errors.append("result invariant %s below min %s" % (path, bounds["min"]))
            if "max" in bounds and value > bounds["max"]:
                errors.append("result invariant %s above max %s" % (path, bounds["max"]))
    return errors


_RAW_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

def _raw_results_payload(record: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode the raw-results manifest without treating agent text as evidence."""
    try:
        payload = json.loads(str(record.get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return None, ["raw_results must be a JSON manifest"]
    if not isinstance(payload, dict):
        return None, ["raw_results must be a JSON manifest object"]
    return payload, []


def _validate_raw_results_manifest(
    record: dict[str, Any], *, verify_files: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate manifest shape and, at freeze time, the actual retained bytes."""
    payload, errors = _raw_results_payload(record)
    if payload is None:
        return None, errors
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return payload, ["raw_results." + contract_requirement(RAW_RESULTS_CONTRACT, "files")]
    seen_paths: set[str] = set()
    for index, item in enumerate(files):
        prefix = f"files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        path_value = item.get("path")
        sha256 = item.get("sha256")
        role = item.get("role")
        retention = item.get("retention")
        size = item.get("bytes")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append(f"{prefix}.path must be a non-empty string")
            continue
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            errors.append(f"{prefix}.path must be absolute for cross-node audit")
            continue
        canonical_path = str(path.resolve(strict=False))
        if canonical_path in seen_paths:
            errors.append(f"{prefix}.path duplicates another manifest entry")
        seen_paths.add(canonical_path)
        if not isinstance(sha256, str) or not _RAW_SHA256_RE.fullmatch(sha256):
            errors.append(f"{prefix}.sha256 must be a 64-character SHA-256 hex digest")
        if not isinstance(role, str) or not role.strip():
            errors.append(f"{prefix}.role must be a non-empty string")
        if retention not in {"protected", "disposable"}:
            errors.append(f"{prefix}.retention must be protected or disposable")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"{prefix}.bytes must be a non-negative integer")
        if errors and any(error.startswith(prefix) for error in errors):
            continue
        if verify_files:
            if not path.is_file():
                errors.append(f"{prefix}.path is not a readable regular file: {canonical_path}")
                continue
            actual_size = path.stat().st_size
            if actual_size != size:
                errors.append(f"{prefix}.bytes mismatch: declared {size}, actual {actual_size}")
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                errors.append(f"{prefix}.path cannot be read: {type(exc).__name__}")
                continue
            actual_sha256 = digest.hexdigest().lower()
            if actual_sha256 != sha256.lower():
                # 实际摘要就在手里，报出来。节点对自己 run_root 里的产物本就有读权限，
                # 隐瞒它不构成任何防线，只是逼节点另找一条算 hash 的路；真实 E2E
                # （2026-09-02）里 shell 被磁盘 reserve 挡死后，节点为了算这一个
                # sha256 往科学路线 DAG 里插了个 hash_outputs 步骤，污染了路线语义。
                # 上面 bytes 的检查一直是 "declared X, actual Y"，此处对齐。
                errors.append(
                    f"{prefix}.sha256 mismatch for {canonical_path}: "
                    f"declared {sha256.lower()}, actual {actual_sha256}"
                )
    return payload, errors


def _frozen_raw_results_record(state: Any, artifact_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        record = state.read_artifact(artifact_id)
    except Exception:
        record = None
    if not isinstance(record, dict) or record.get("type") != "raw_results":
        return None, ["replay_manifest.raw_results_artifact_id must identify raw_results"]
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata.get("frozen"):
        return None, ["replay_manifest.raw_results_artifact_id must identify frozen raw_results"]
    _, errors = _validate_raw_results_manifest(record, verify_files=False)
    return record, errors







# 数据服务请求有两种 kind，区别只在**科学权威**上：
#
#   formal_input_preparation      —— 由冻结 prereg 背书的正式输入准备。参数来源
#       只能是冻结 prereg，产物可直接喂给 primary simulation。
#   preprocessing_service_request —— 没有绑定冻结 prereg 的 run（operation /
#       diagnostic / toolchain_build）要网格、要输入包时的合法入口。此前 spec
#       门禁无条件要求 source_prereg_artifact_id，这类 run **构造不出**能过门的
#       请求，于是"调 data"这条路对它们等于不存在，剩下的唯一动作就是自己就地
#       造前处理产物 —— 正是边界要禁的行为。堵了洞不给出口就是在制造死锁。
#
# 第二种 kind 不携带科学权威（``scientific_authority: False``）。这个标记是
# 必需的：``preflight.audit_execution_contract`` 用 input_delivery_state 里有没有
# verified 条目来决定要不要跟冻结 prereg 逐键比对参数，若不区分，一个 secondary
# simulation 只要走了这条路就会被要求拿出它根本没有的 expected_params，且无解。
_DATA_REQUEST_KINDS = ("formal_input_preparation", "preprocessing_service_request")
_PREPROCESSING_REQUEST_STAGES = ("diagnostic", "toolchain_build", "operation")

_ACQUISITION_REFERENCE_FIELDS = (
    "source_locator",
    "expected_revision",
    "expected_filename",
    "expected_sha256",
    "license",
)


def _normalize_acquisition_reference(
    asset: dict[str, Any], index: int, errors: list[str],
) -> None:
    """Keep an optional exact external-source contract safe and deterministic.

    Data owns acquisition planning. Experiment only preserves the provenance
    provided by the caller, so planning never has to reconstruct a
    repository/file identity from prose. The block stays optional because a
    requested asset may instead be locally generated or supplied by a prior
    workflow stage.
    """
    reference = asset.get("acquisition_reference")
    if reference is None:
        return
    prefix = f"required_assets[{index}].acquisition_reference"
    if not isinstance(reference, dict):
        errors.append(f"{prefix} must be an object")
        return
    unknown = sorted(set(reference) - set(_ACQUISITION_REFERENCE_FIELDS))
    if unknown:
        errors.append("{} has unsupported fields: {}".format(prefix, ", ".join(unknown)))
        return
    if not reference:
        errors.append(f"{prefix} must not be empty")
        return

    normalized: dict[str, str] = {}
    for field in _ACQUISITION_REFERENCE_FIELDS:
        if field not in reference:
            continue
        value = reference[field]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.{field} must be a non-empty string")
            continue
        value = value.strip()
        if len(value) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
            errors.append(f"{prefix}.{field} has invalid control characters or length")
            continue
        normalized[field] = value

    filename = normalized.get("expected_filename")
    if filename:
        candidate = PurePosixPath(filename)
        if candidate.is_absolute() or ".." in candidate.parts or filename in {".", ".."}:
            errors.append(f"{prefix}.expected_filename must be package-relative")
    digest = normalized.get("expected_sha256")
    if digest:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            errors.append(f"{prefix}.expected_sha256 must be a 64-character hexadecimal digest")
        else:
            normalized["expected_sha256"] = digest.lower()

    # Do not retain malformed partial data in a successful normalized request.
    if not any(error.startswith(prefix) for error in errors):
        asset["acquisition_reference"] = normalized


def _decode_data_request(spec: str) -> dict[str, Any] | None:
    try:
        value = json.loads(spec)
    except (TypeError, json.JSONDecodeError):
        try:
            import yaml
            value = yaml.safe_load(spec)
        except Exception:
            return None
    return value if isinstance(value, dict) else None


def _compose_data_request_payload(request: dict[str, Any]) -> str:
    """把校验过的请求摊成 data 节点认得出的 research plan 形状。

    data 的入口分类器读的是 research plan：它按 objective / stages / software /
    deliverables / conditions 五个信号判 evidence_status，信号不足就只能去做定向
    web search 或直接产 blocked contract。而 experiment 发过去的是一个请求对象
    （required_assets / acceptance），字段名对不上任何一个信号 —— 链路能通靠的是
    ``source_prereg_artifact_id`` 的值里恰好含 ``pre_registration`` 这个子串。

    这里不去迎合对面的正则（那是依赖对方未声明的实现细节，人家改一行就静默
    失效），而是**把请求如实写成一份计划**：本来就有的目标、阶段、软件、交付物
    和验收要求各自成段。信号自然命中，读的人也看得懂。
    """
    kind = request.get("request_kind")
    formal = kind == "formal_input_preparation"
    prereg_id = str(request.get("source_prereg_artifact_id") or "").strip()
    prereg_version = request.get("source_prereg_version")
    prereg_hash = str(request.get("source_prereg_content_hash") or "").strip()
    objective = str(request.get("purpose") or "").strip() or (
        f"按冻结预注册 {prereg_id} 准备下游求解器所需的正式输入" if formal
        else "为本次实验运行准备所需的前处理资产")
    stages = str(request.get("requesting_stage") or "simulation").strip()
    software = str(request.get("target_software") or "").strip() or "（未声明；请按 required files 的格式推断）"

    lines = [
        "# Preprocessing request (research plan) — from the experiment node",
        "",
        "## Research objective / 研究目标",
        objective,
        "",
    ]
    if formal:
        lines += [
            "## Upstream plan / 上游计划",
            f"Frozen pre_registration receipt: id={prereg_id}; version={prereg_version}; content_sha256={prereg_hash}",
            "本请求由 experiment 节点转来；科学参数以该冻结预注册为准，不得改写或新增。",
            "",
        ]
    lines += [
        "## Calculation stages / 计算阶段",
        f"本次请求服务于 {stages} 阶段的运行。",
        "",
        "## Target software / 求解器软件",
        software,
        "",
        "## Required files / 所需前处理资产",
    ]
    for asset in request.get("required_assets") or []:
        if isinstance(asset, dict):
            lines.append(f"- {asset.get('name')}（格式 {asset.get('format')}）：{asset.get('purpose')}")
    acquisition_references = [
        {"asset": asset.get("name"), **asset["acquisition_reference"]}
        for asset in request.get("required_assets") or []
        if isinstance(asset, dict) and isinstance(asset.get("acquisition_reference"), dict)
    ]
    if acquisition_references:
        lines += [
            "",
            "## External acquisition references / 外部获取依据",
            "以下精确来源约束由上游请求提供；仅 Data 可据此规划检索或获取，不得静默替换。",
            "```json",
            json.dumps(acquisition_references, ensure_ascii=False, sort_keys=True),
            "```",
        ]
    lines += [
        "",
        "## Conditions / 工况参数",
        ("科学参数只能来自上述冻结预注册（frozen_prereg_only），不得由本节点或数据节点新拟。"
         if formal else
         "本请求不携带科学参数（not_applicable）：它服务于非科学运行，产物不得用于科学结论。"),
        "",
        "## Acceptance / 验收要求",
        "交付的 dataset 必须给出真实 package path、manifest 与 lineage；experiment 会逐项核对"
        "文件存在性、schema、单位和 manifest lineage，任一不过即不消费。",
    ]
    return "\n".join(lines)


_DATA_DISPATCH_RECEIPT_SCHEMA = "experiment.data_dispatch.v1"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$")


def _canonical_json(value: Any) -> str:
    """Canonical bytes for a request receipt, never a second lifecycle object."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )


def _data_dispatch_material(spec_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Build the exact Data input and digest it before the managed dispatch.

    The receipt belongs to the existing input-delivery ledger.  It binds an
    actual ``run_node(data)`` return to this already validated request, rather
    than trusting a later free-form child id or transcript preview.
    """
    payload = _compose_data_request_payload(request)
    return {
        "schema_version": _DATA_DISPATCH_RECEIPT_SCHEMA,
        "spec_id": spec_id,
        "request_sha256": hashlib.sha256(_canonical_json(request).encode("utf-8")).hexdigest(),
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        # Preserve the established Data-node input shape.  The wrapper, not a
        # new payload protocol, is the authority that binds this exact call.
        #
        # 结构化请求原样带上（main 1197482e）。data 的入口分类器现在**原生认得
        # experiment 的请求词表** —— `request_kind` / `source_prereg_artifact_id` /
        # `required_assets` / `acceptance` 都是它显式读的键。只发下面那份 Markdown
        # 伪计划的后果实测过：`formal_input_preparation` 会被判成
        # authority.kind=user_request / review_profile=request_bound —— **冻结预注册
        # 的科学权威在跨节点这一步丢掉**，而两边都不报错。带上之后是
        # pre_registration / plan_bound。Markdown 那份继续发：它是给人看的，也是
        # data 的 caller_request_text 的来源。
        #
        # 必须补在这里而不是下游的返回值上：payload_sha256 与真正的 run_node 调用
        # 都读这一份，只补返回值等于发出去的仍然只有散文。
        "node_inputs": {"spec": payload, **request},
    }


def _child_artifact_ids(value: Any) -> list[str]:
    """Extract stable artifact identities from the established run_node return."""
    if not isinstance(value, list):
        return []
    result: set[str] = set()
    for item in value:
        candidate = item.get("id") if isinstance(item, dict) else item
        artifact_id = str(candidate or "").strip()
        if artifact_id and _ARTIFACT_ID_RE.fullmatch(artifact_id):
            result.add(artifact_id)
    return sorted(result)


async def _validate_data_request_spec(
    state: Any,
    spec: str,
    supersedes_spec_id: str | None = None,
    supersede_reason: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    request = _decode_data_request(spec)
    errors: list[str] = []
    if request is None:
        errors.append("spec must be a JSON or YAML object")
    else:
        kind = request.get("request_kind")
        if kind not in _DATA_REQUEST_KINDS:
            errors.append("request_kind must be formal_input_preparation or preprocessing_service_request")
        if kind == "preprocessing_service_request":
            # 绑定了冻结 prereg = 本 run 是科学运行；再走无权威 kind 就等于绕开
            # frozen_prereg_only。契约读不出来时按"已绑定"处理（fail-closed）。
            try:
                bound_prereg = str(load_run_contract(state).get("prereg_artifact_id") or "").strip()
            except Exception:
                bound_prereg = "__contract_unreadable__"
            if bound_prereg:
                errors.append(
                    "preprocessing_service_request is not allowed once a frozen pre_registration "
                    "is bound to this run; use formal_input_preparation")
            if request.get("requesting_stage") not in _PREPROCESSING_REQUEST_STAGES:
                errors.append("requesting_stage must be diagnostic, toolchain_build, or operation")
            # 没有冻结 prereg 可读时，data 只能从请求正文推断要造什么；求解器名字
            # 是它推导必需输入文件的起点，缺了就只能去猜或去 web search。
            if not isinstance(request.get("target_software"), str) or not request["target_software"].strip():
                errors.append("target_software is required: name the solver/toolchain that consumes these assets")
            if request.get("scientific_parameters") not in (None, "not_applicable"):
                errors.append("preprocessing_service_request carries no scientific parameters; use not_applicable")
            if not isinstance(request.get("purpose"), str) or not request["purpose"].strip():
                errors.append("purpose is required: state what this run needs the asset for")
        else:
            if not isinstance(request.get("source_prereg_artifact_id"), str) or not request["source_prereg_artifact_id"].strip():
                errors.append("source_prereg_artifact_id is required")
            try:
                bound_contract = load_run_contract(state)
            except Exception:
                bound_contract = {}
            bound_prereg_id = str(bound_contract.get("prereg_artifact_id") or "").strip()
            if (
                bound_prereg_id
                and str(request.get("source_prereg_artifact_id") or "").strip() != bound_prereg_id
            ):
                errors.append("source_prereg_artifact_id must match the run bound frozen pre_registration")
            if bound_prereg_id:
                expected_version = bound_contract.get("prereg_version")
                expected_hash = str(bound_contract.get("prereg_content_hash") or "").lower()
                if (
                    not isinstance(expected_version, int)
                    or isinstance(expected_version, bool)
                    or expected_version < 1
                    or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
                ):
                    errors.append("bound frozen pre_registration receipt is incomplete")
                else:
                    supplied_version = request.get("source_prereg_version")
                    supplied_hash = request.get("source_prereg_content_hash")
                    if supplied_version is None:
                        request["source_prereg_version"] = expected_version
                    elif supplied_version != expected_version:
                        errors.append("source_prereg_version must match the run bound frozen pre_registration")
                    if supplied_hash is None:
                        request["source_prereg_content_hash"] = expected_hash
                    elif str(supplied_hash).lower() != expected_hash:
                        errors.append("source_prereg_content_hash must match the run bound frozen pre_registration")
            else:
                supplied_version = request.get("source_prereg_version")
                supplied_hash = request.get("source_prereg_content_hash")
                if supplied_version is not None and (
                    not isinstance(supplied_version, int)
                    or isinstance(supplied_version, bool)
                    or supplied_version < 1
                ):
                    errors.append("source_prereg_version must be a positive integer")
                if supplied_hash is not None and re.fullmatch(
                    r"[0-9a-f]{64}", str(supplied_hash).lower()
                ) is None:
                    errors.append("source_prereg_content_hash must be a 64-character hexadecimal digest")
            if request.get("scientific_parameters") != "frozen_prereg_only":
                errors.append("scientific_parameters must be frozen_prereg_only")
        assets = request.get("required_assets")
        if not isinstance(assets, list) or not assets:
            errors.append("required_assets must be a non-empty list")
        else:
            for i, asset in enumerate(assets):
                if not isinstance(asset, dict) or any(not isinstance(asset.get(k), str) or not asset[k].strip() for k in ("name", "format", "purpose")):
                    errors.append(f"required_assets[{i}] needs name, format, purpose")
                    continue
                # Asset names identify deliverables in Data's package. They must
                # remain package-relative so Experiment can safely verify them.
                asset_path = Path(asset["name"])
                if asset_path.is_absolute() or ".." in asset_path.parts:
                    errors.append(f"required_assets[{i}].name must be a package-relative file name")
                _normalize_acquisition_reference(asset, i, errors)
        # 判决拆除（ca:1077 删，2026-08-31）：acceptance 四字段逐字抄 true 是纯
        # 仪式 —— 框架不验其真伪，真验收在下游 verify_dataset_consumption。
    # supersede 是本分支独有能力（origin/main 全文无此概念），判决未覆盖，随结构保留。
    supersedes = str(supersedes_spec_id or "").strip()
    reason = str(supersede_reason or "").strip()
    if supersedes and not reason:
        errors.append("supersede_reason is required when supersedes_spec_id is provided")
    if reason and not supersedes:
        errors.append("supersedes_spec_id is required when supersede_reason is provided")
    if errors:
        state.append_transcript("data_request_spec_validation", passed=False, errors=errors)
        return {"status": "error", "errors": errors}
    spec_id = "data_request__" + hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    scientific_authority = request.get("request_kind") == "formal_input_preparation"
    try:
        ledger = load_input_delivery_ledger(state)
    except InputDeliveryLedgerError as exc:
        return {
            "status": "error",
            "error": "input_delivery_ledger_unreadable",
            "detail": str(exc),
        }
    specs = ledger["specs"]
    if supersedes == spec_id:
        return {"status": "error", "error": "data_request_cannot_supersede_itself"}

    existing = specs.get(spec_id)
    if isinstance(existing, dict):
        if existing.get("request") != request:
            return {"status": "error", "error": "data_request_spec_id_collision"}
        if existing.get("lifecycle_status") != "active":
            return {
                "status": "error",
                "error": "data_request_spec_already_superseded",
                "superseded_by": existing.get("superseded_by"),
            }

    old = specs.get(supersedes) if supersedes else None
    already_superseded = bool(
        isinstance(old, dict)
        and old.get("lifecycle_status") == "superseded"
        and old.get("superseded_by") == spec_id
    )
    if supersedes and not already_superseded:
        if not isinstance(old, dict) or old.get("lifecycle_status") != "active":
            return {
                "status": "error",
                "error": "superseded_spec_not_active",
                "supersedes_spec_id": supersedes,
            }

    changed = False
    if not isinstance(existing, dict):
        specs[spec_id] = {
            "request": request,
            "lifecycle_status": "active",
            "superseded_by": None,
            "supersede_reason": None,
            "delivery": {
                "provider": None,
                "verified": False,
                "data_terminally_blocked": False,
                "request_kind": request.get("request_kind"),
                "scientific_authority": scientific_authority,
                "source_prereg_artifact_id": request.get("source_prereg_artifact_id"),
            },
        }
        changed = True
    if supersedes and not already_superseded:
        old["lifecycle_status"] = "superseded"
        old["superseded_by"] = spec_id
        old["supersede_reason"] = reason
        changed = True

    # revision=0 表示当前视图来自旧 hook_state，而不是 durable artifact。
    # 即使逻辑上是幂等重调，也必须借这次合法 mutation 完成一次迁移落盘。
    if changed or ledger.get("revision") == 0:
        try:
            committed = save_input_delivery_ledger(state, ledger)
        except Exception as exc:
            return {
                "status": "error",
                "error": "input_delivery_ledger_persistence_failed",
                "detail": type(exc).__name__,
            }
        ledger_artifact_id = committed["artifact"]["id"]
    else:
        ledger_artifact_id = (
            state.hook_state.get("input_delivery_ledger") or {}
        ).get("artifact_id")
    state.append_transcript("data_request_spec_validation", passed=True, spec_id=spec_id,
                            request_kind=request.get("request_kind"),
                            scientific_authority=scientific_authority,
                            supersedes_spec_id=supersedes or None,
                            input_delivery_ledger_artifact_id=ledger_artifact_id)
    if supersedes:
        state.append_transcript(
            "data_request_spec_superseded",
            superseded_spec_id=supersedes,
            replacement_spec_id=spec_id,
            reason=reason,
            idempotent=already_superseded,
            input_delivery_ledger_artifact_id=ledger_artifact_id,
        )
    dispatch_material = _data_dispatch_material(spec_id, request)
    return {"status": "success", "spec_id": spec_id, "normalized_spec": request,
            "request_kind": request.get("request_kind"),
            "scientific_authority": scientific_authority,
            "supersedes_spec_id": supersedes or None,
            "idempotent": not changed,
            "input_delivery_ledger_artifact_id": ledger_artifact_id,
            "request_sha256": dispatch_material["request_sha256"],
            "payload_sha256": dispatch_material["payload_sha256"],
            "dispatch_node_inputs": dispatch_material["node_inputs"]}


_UNKNOWN_SPEC_ERROR = "unknown spec_id; validate spec first"


def _require_spec(state: Any, spec_id: str, *, spec: bool = True, delivery: bool = False,
                  ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a validated data-request spec and/or its delivery state by id.

    Returns ``(spec, delivery, error)``; ``error`` is the C-class refusal dict when
    a requested record is missing (the id references nothing this run validated).
    判决拆除·第三波（ca:1115/1273/1384/1422 merge，2026-09-02）：同一条件四份抄件
    收成一处，行为不变。
    """
    spec_record = (state.hook_state.get("validated_data_request_specs") or {}).get(spec_id)
    delivery_record = (state.hook_state.get("input_delivery_state") or {}).get(spec_id)
    missing = ((spec and not isinstance(spec_record, dict))
               or (delivery and not isinstance(delivery_record, dict)))
    error = {"status": "error", "error": _UNKNOWN_SPEC_ERROR} if missing else None
    return spec_record, delivery_record, error


async def _verify_dataset_consumption(state: Any, dataset_artifact_id: str, spec_id: str, **_: Any) -> dict[str, Any]:
    try:
        ledger = load_input_delivery_ledger(state)
    except InputDeliveryLedgerError as exc:
        return {"status": "error", "error": "input_delivery_ledger_unreadable", "detail": str(exc)}
    entry = ledger["specs"].get(spec_id)
    if not isinstance(entry, dict):
        return {"status": "error", "error": "unknown spec_id; validate spec first"}
    if entry.get("lifecycle_status") != "active":
        return {
            "status": "error",
            "error": "spec_id_not_active",
            "superseded_by": entry.get("superseded_by"),
        }
    spec = entry.get("request")
    if not isinstance(spec, dict):
        return {"status": "error", "error": "unknown spec_id; validate spec first"}

    # Non-formal Raw Data delivery keeps its established compatibility path.
    # Formal input must first acquire the managed dispatch provenance below;
    # after that, only a dataset named by the direct child receipt is eligible.
    # The same receipt check applies after Core deliberately skipped pause import.
    delivery = entry.get("delivery")
    receipt = delivery.get("data_dispatch_receipt") if isinstance(delivery, dict) else None
    formal_receipt_missing = (
        spec.get("request_kind") == "formal_input_preparation"
        and not isinstance(receipt, dict)
    )
    if isinstance(receipt, dict):
        intent_binding = audit_execution_intent_binding(state, require=True)
        if not intent_binding.get("passed", False):
            return {
                "status": "error",
                "error": (
                    "managed Data dataset consumption requires a current immutable "
                    "upstream-intent binding"
                ),
                "spec_id": spec_id,
                "intent_binding": intent_binding,
            }
        receipt_errors, _ = _managed_data_dispatch_receipt_errors(
            state, spec_id, spec, delivery, require_blocked_report=False,
        )
        if receipt_errors:
            return {
                "status": "error",
                "error": "managed_data_dispatch_receipt_invalid",
                "receipt_errors": receipt_errors,
            }
        child_run_id = str(receipt.get("child_run_id") or "").strip()
        artifact_ids = _child_artifact_ids(receipt.get("child_artifact_ids"))
        if dataset_artifact_id not in artifact_ids:
            return {
                "status": "error",
                "error": "dataset_artifact_id_not_returned_by_managed_data_child",
            }
        record = _read_child_run_artifact(
            state, child_run_id, dataset_artifact_id, require_sibling=True,
        )
        dataset_errors = _data_dataset_errors(record, child_run_id)
        if dataset_errors:
            return {
                "status": "error",
                "error": "managed_data_dataset_invalid",
                "dataset_artifact_id": dataset_artifact_id,
                "dataset_errors": dataset_errors,
            }
    elif not formal_receipt_missing:
        record = state.read_artifact(dataset_artifact_id)
    else:
        # Do not even inspect a visible dataset before formal producer provenance exists.
        record = None
    if spec.get("request_kind") == "formal_input_preparation":
        formal_errors = _formal_request_contract_errors(spec, load_run_contract(state))
        if formal_receipt_missing or formal_errors:
            return {
                "status": "error",
                "error": (
                    "formal input consumption requires a managed Data dispatch receipt"
                    if formal_receipt_missing else
                    "formal input request is not bound to the current frozen preregistration"
                ),
                **(
                    {
                        "error_code": "formal_input_requires_dispatch_receipt",
                        "spec_id": spec_id,
                        "dataset_artifact_id": dataset_artifact_id,
                        "next_step": (
                            f"dispatch_data_request(spec_id={json.dumps(spec_id)}, "
                            'user_note="Request Data to prepare and return the validated '
                            'formal input for this spec.")'
                        ),
                    }
                    if formal_receipt_missing else
                    {"errors": formal_errors}
                ),
            }
    if not isinstance(record, dict) or record.get("type") != "dataset":
        return {"status": "error", "error": "dataset_artifact_id must identify dataset"}
    view = dict(record.get("metadata") or {})
    try:
        content = json.loads(record.get("content") or "{}")
        if isinstance(content, dict):
            for key, value in content.items():
                view.setdefault(key, value)
    except (TypeError, json.JSONDecodeError):
        pass
    aliases_by_field = {"package_path": ("package_path", "package_dir", "data_path", "path"), "manifest": ("manifest", "manifest_path"), "lineage": ("lineage",), "downstream_contract": ("downstream_contract",)}
    missing = [key for key, aliases in aliases_by_field.items() if not any(view.get(a) not in (None, "", [], {}) for a in aliases)]
    package_path = next((view.get(a) for a in aliases_by_field["package_path"] if view.get(a)), None)
    package_dir: Path | None = None
    if not isinstance(package_path, str):
        missing.append("package_path_not_usable")
    else:
        candidate = Path(package_path).expanduser()
        if not candidate.is_dir():
            missing.append("package_path_not_found_or_not_directory")
        else:
            package_dir = candidate.resolve()

    manifest_path = next((view.get(a) for a in aliases_by_field["manifest"] if view.get(a)), None)
    if not isinstance(manifest_path, str) or not Path(manifest_path).expanduser().is_file():
        missing.append("manifest_path_not_found")
    else:
        try:
            manifest = json.loads(Path(manifest_path).expanduser().read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                missing.append("manifest_not_an_object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            missing.append("manifest_not_readable_json")

    contract = view.get("downstream_contract")
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except json.JSONDecodeError:
            pass
    contract_text = json.dumps(contract, ensure_ascii=False, sort_keys=True) if isinstance(contract, (dict, list)) else str(contract or "")
    undeclared = [asset["name"] for asset in spec["required_assets"] if asset["name"] not in contract_text]
    missing_assets: list[str] = []
    if package_dir is not None:
        for asset in spec["required_assets"]:
            relative_name = asset["name"]
            direct = package_dir / relative_name
            # Data may place deliverables in a declared stage subdirectory.
            # Accept that layout only when the requested file is in the package.
            found = direct.is_file() or any(path.is_file() for path in package_dir.rglob(Path(relative_name).name))
            if not found:
                missing_assets.append(relative_name)
    passed = not missing and not undeclared and not missing_assets
    result = {"status": "success" if passed else "error", "passed": passed, "dataset_artifact_id": dataset_artifact_id, "spec_id": spec_id, "missing_dataset_fields": missing, "undeclared_requested_assets": undeclared, "missing_requested_assets": missing_assets}
    if passed:
        delivery = entry["delivery"]
        delivery.update({"provider": "data", "verified": True, "input_package_artifact_id": dataset_artifact_id, "package_dir": str(package_dir), "manifest_path": str(manifest_path)})
        try:
            committed = save_input_delivery_ledger(state, ledger)
        except Exception as exc:
            result.update({
                "status": "error",
                "passed": False,
                "persistence_error": "input_delivery_ledger_persistence_failed",
                "persistence_detail": type(exc).__name__,
            })
        else:
            result["input_delivery_ledger_artifact_id"] = committed["artifact"]["id"]
    state.append_transcript("dataset_consumption_verification", **result)
    return result


register_tool(ToolDefinition(
    name="validate_data_request_spec",
    description=(
        "Mechanically validate a JSON/YAML data-service request spec before calling data. "
        "request_kind=formal_input_preparation covers prereg-backed formal inputs for a "
        "scientific run (needs source_prereg_artifact_id, plus source_prereg_version and "
        "source_prereg_content_hash pinning that exact frozen version — omit them and the "
        "framework fills them from the bound prereg, but a value that disagrees with the "
        "binding is rejected; and scientific_parameters=frozen_prereg_only). request_kind=preprocessing_service_request is the entry point "
        "for a run with no bound frozen pre_registration; for example, operation / diagnostic / "
        "toolchain_build needs requesting_stage and purpose, carries no scientific "
        "authority, and is rejected once a frozen prereg is bound. Both kinds need "
        "required_assets and the four acceptance flags. An asset may optionally carry "
        "acquisition_reference with source_locator, expected_revision, expected_filename, "
        "expected_sha256, and license; this preserves an exact public-source contract for Data "
        "without authorizing Experiment to fetch it. To correct an active request, pass "
        "supersedes_spec_id plus supersede_reason in the same call; the old request becomes "
        "inactive only after the replacement is durably committed. "
        "Whether an experiment-side fallback may replace a Data delivery at all is decided by "
        "the frozen prereg's input_delivery_policy — its mode plus experiment_fallback_permitted; "
        "this call cannot widen it, and using a fallback is recorded as an unmet execution precondition."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "spec": {"type": "string"},
            "supersedes_spec_id": {"type": "string"},
            "supersede_reason": {"type": "string"},
        },
        "required": ["spec"],
    },
    allowed_node_types=["experiment"], risk_level="low",
), _validate_data_request_spec)
register_tool(ToolDefinition(name="verify_dataset_consumption", description="Mechanically verify returned dataset against a validated data request.", parameters_schema={"type": "object", "properties": {"dataset_artifact_id": {"type": "string"}, "spec_id": {"type": "string"}}, "required": ["dataset_artifact_id", "spec_id"]}, allowed_node_types=["experiment"], risk_level="low"), _verify_dataset_consumption)

def _sibling_child_run_root(state: Any, run_id: str) -> Path | None:
    """Return only a direct current-run sibling, never a global run lookup."""
    normalized_run_id = str(run_id or "").strip()
    if _RUN_ID_RE.fullmatch(normalized_run_id) is None:
        return None
    parent = Path(getattr(state, "root", ".")).resolve(strict=False).parent
    candidate = (parent / normalized_run_id).resolve(strict=False)
    if candidate.parent != parent or not candidate.is_dir():
        return None
    return candidate


def _child_run_start_record(state: Any, run_id: str) -> dict[str, Any] | None:
    """Read the immutable parent/node declaration from a direct child run."""
    root = _sibling_child_run_root(state, run_id)
    if root is None:
        return None
    transcript = root / "transcript.jsonl"
    if not transcript.is_file():
        return None
    try:
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            event = json.loads(line)
            if isinstance(event, dict) and event.get("event") == "run_start":
                return event
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _child_run_terminal_summary(state: Any, run_id: str) -> dict[str, Any] | None:
    """Read a completed direct child's durable summary and matching run_end."""
    root = _sibling_child_run_root(state, run_id)
    if root is None:
        return None
    transcript = root / "transcript.jsonl"
    summary = root / "summary.json"
    if not transcript.is_file() or not summary.is_file():
        return None
    terminal_status: str | None = None
    try:
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("event") != "run_end":
                continue
            candidate = str(event.get("status") or "").strip()
            if candidate and candidate != "paused":
                terminal_status = candidate
        if not terminal_status:
            return None
        payload = json.loads(summary.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return None
        if str(payload.get("status") or "").strip() != terminal_status:
            return None
        # This is the durable equivalent of run_node's all_child_artifacts.
        # Without it a stray file in the child directory cannot become a
        # fallback-authorizing receipt after a pause.
        if not isinstance(payload.get("artifacts"), list):
            return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload


def _child_run_terminal_status(state: Any, run_id: str) -> str | None:
    """Return terminal status only when the durable child summary is coherent."""
    summary = _child_run_terminal_summary(state, run_id)
    return str(summary.get("status") or "").strip() if isinstance(summary, dict) else None


def _read_child_run_artifact(
    state: Any,
    run_id: str,
    artifact_id: str,
    *,
    require_sibling: bool = True,
) -> dict[str, Any] | None:
    """Read one direct Data-child artifact without widening cross-run authority.

    Project v2 stores node artifacts in the project worktree rather than the
    child cache directory. The worktree fallback uses the Core-owned `data`
    workspace mapping only, then admits only a record whose producer identity
    is the exact direct Data child. It is not a generic external-artifact lookup.
    """
    normalized_artifact_id = str(artifact_id or "").strip()
    if _ARTIFACT_ID_RE.fullmatch(normalized_artifact_id) is None:
        return None
    normalized_run_id = str(run_id or "").strip()
    run_root = _sibling_child_run_root(state, normalized_run_id)
    if run_root is None and not require_sibling:
        from core.paths import find_run_dir
        run_root = find_run_dir(normalized_run_id)
    if run_root is None:
        return None

    strict_direct_data_child = False
    if require_sibling:
        start = _child_run_start_record(state, normalized_run_id)
        if (
            not isinstance(start, dict)
            or start.get("node_type") != "data"
            or str(start.get("parent_run_id") or "")
            != str(getattr(state, "run_id", "") or "")
        ):
            return None
        strict_direct_data_child = True

    # 记录问账本，不拼路径：工作区绑定时 data 子 run 的产物在工作区账本里
    # （State.read_artifact 工作区在前、run 本地在后）；没绑时在那个子 run 自己的
    # run 本地账本里。
    record: dict[str, Any] | None = None
    if getattr(state, "project_worktree", None):
        record = state.read_artifact(normalized_artifact_id)
    if record is None:
        from core.ledger import RecordStore

        child_store = RecordStore(Path(run_root) / "artifacts", Path(run_root) / "records.jsonl")
        record = child_store.record(normalized_artifact_id)
    if not isinstance(record, dict):
        return None
    if strict_direct_data_child:
        # The project worktree is shared among node runs. Its pathname alone
        # cannot identify a producer; require framework-provided producer facts.
        producer = record.get("produced_by_node_type") or (
            record.get("provenance") or {}
        ).get("by_node_type")
        if (
            producer != "data"
            or str(record.get("produced_by_run_id") or "") != normalized_run_id
        ):
            return None
    record = dict(record)
    record["id"] = normalized_artifact_id
    return record


#: data 交不出货时的终态词表，**分三档**（nodes/data/pipeline_contract.PIPELINE_OUTCOMES
#: 里会落盘的那三个）。分档不是好看：三种情况该做的事完全不同，而老词表
#: `recoverable_blocked` 把它们糊成一坨，收货端只能一律当"data 彻底不行了"。
#:
#: 值 = 给 experiment 的祈使句。措辞从这里取，不在下面另写一句
#: （另写一句就是又开一份会各自演化的抄件）。
DATA_RECOVERABLE_OUTCOMES: dict[str, str] = {
    "needs_input": (
        "补齐 data 点名缺的输入值，用**同一个 spec_id** 重新 "
        "run_node(node_type='data', ...)；不要改写 data 已锁定的请求"
    ),
    "externally_blocked": (
        "补齐 data 点名缺的运行环境/工具（这是 experiment 的职责：data 不再自己"
        "装包或建工具链），然后用**同一个 spec_id** 重新 run_node(node_type='data', ...)"
    ),
}

#: 真·终态：data 自己也没有下一步了。旧词表一并收下 —— 2026-08-31 之前的 run
#: 和存量 artifact 还在用它们，不分档，一律终态。
DATA_TERMINAL_OUTCOMES: frozenset[str] = frozenset({
    "fatal",
    "recoverable_blocked", "blocked", "incomplete",
})


#: 终态 blocker 的"资产级获取尝试证据"验收 schema —— **experiment 是定义方**，
#: 不是 data 词表的抄件（不存在 a3bcd65c 那种漂移问题；data 侧照此供给，见
#: 数据契约 proposal）。一条合格的尝试记录 = 指名来源 + 指名失败。
_ACQUISITION_ATTEMPT_CONTAINERS = ("acquisition_attempts", "attempts",
                                   "attempted_sources", "asset_failures")
_ACQUISITION_SOURCE_FIELDS = ("url", "source", "source_url", "source_locator",
                              "asset", "asset_name")
_ACQUISITION_FAILURE_FIELDS = ("failure_type", "error", "error_type",
                               "evidence", "evidence_path", "status")


def _acquisition_attempt_evidence(payload: Any) -> list[dict[str, Any]]:
    """纯函数：从 blocked report payload 里抽出合格的获取尝试记录。

    空列表 = 该报告没有任何"对具体资产/来源试过并失败"的机械证据 ——
    planner 空转、规划循环耗尽、内部异常都长这样。它们是 Data 的内部失败，
    不构成"数据不可得"，不得一步解锁 experiment fallback（东亚 E2E 病灶）。
    """
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, Any]] = []
    for container in _ACQUISITION_ATTEMPT_CONTAINERS:
        entries = payload.get(container)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source = next((str(entry[f]).strip() for f in _ACQUISITION_SOURCE_FIELDS
                           if str(entry.get(f) or "").strip()), "")
            failure = next((str(entry[f]).strip() for f in _ACQUISITION_FAILURE_FIELDS
                            if str(entry.get(f) or "").strip()), "")
            if source and failure:
                found.append({"container": container, "source": source,
                              "failure": failure, "entry": entry})
    return found


def _data_outcome_detail(report_payload: dict[str, Any]) -> dict[str, Any]:
    """把 data 报告里"缺什么"那部分挑出来 —— 让 experiment 不用自己去翻 JSON。"""
    resume = report_payload.get("resume_contract")
    contract = report_payload.get("input_contract")
    detail = {
        "reason": report_payload.get("reason"),
        "failure_category": report_payload.get("failure_category"),
        "stop_reason": report_payload.get("stop_reason"),
    }
    if isinstance(contract, dict):
        detail["question"] = contract.get("question")
        detail["options"] = contract.get("options")
    if isinstance(resume, dict):
        detail["missing_fields"] = resume.get("missing_fields")
        detail["preserve_request_spec_hash"] = resume.get("preserve_request_spec_hash")
    return {k: v for k, v in detail.items() if v not in (None, "", [], {})}


def _active_data_spec_context(
    state: Any,
    spec_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (ledger, entry, error)，所有 Data 状态 mutation 共用同一 active 门。"""
    try:
        ledger = load_input_delivery_ledger(state)
    except InputDeliveryLedgerError as exc:
        return None, None, {
            "status": "error",
            "error": "input_delivery_ledger_unreadable",
            "detail": str(exc),
        }
    entry = ledger["specs"].get(spec_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
        return None, None, {
            "status": "error",
            "error": "unknown spec_id; validate spec first",
        }
    if entry.get("lifecycle_status") != "active":
        return None, None, {
            "status": "error",
            "error": "spec_id_not_active",
            "superseded_by": entry.get("superseded_by"),
        }
    return ledger, entry, None


def _data_blocked_report_errors(
    report: dict[str, Any] | None,
    child_run_id: str,
) -> list[str]:
    """Validate the terminal blocker shape independent of how its id was found."""
    if not isinstance(report, dict):
        return ["data_blocked_report_unreadable_from_managed_child"]
    errors: list[str] = []
    if report.get("type") != "preprocessing_blocked_report":
        errors.append("data_blocked_report_type_invalid")
    if str(report.get("produced_by_run_id") or "") != child_run_id:
        errors.append("data_blocked_report_run_mismatch")
    producer = report.get("produced_by_node_type") or (
        report.get("provenance") or {}
    ).get("by_node_type")
    if producer != "data":
        errors.append("data_blocked_report_producer_invalid")
    try:
        payload = json.loads(report.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    # 词表跟 DATA_* 常量同源。写死一份抄件正是 a3bcd65c 那次断线的形状：
    # data 换了词，收货端一个字没改，交集为空。
    if not isinstance(payload, dict) or payload.get("status") not in (
        set(DATA_RECOVERABLE_OUTCOMES) | set(DATA_TERMINAL_OUTCOMES)
    ):
        errors.append("data_blocked_report_status_invalid")
    return errors


def _data_dataset_errors(
    record: dict[str, Any] | None,
    child_run_id: str,
) -> list[str]:
    """Validate a managed Data dataset is genuinely produced by that child."""
    if not isinstance(record, dict):
        return ["data_dataset_unreadable_from_managed_child"]
    errors: list[str] = []
    if record.get("type") != "dataset":
        errors.append("data_dataset_type_invalid")
    if str(record.get("produced_by_run_id") or "") != child_run_id:
        errors.append("data_dataset_run_mismatch")
    producer = record.get("produced_by_node_type") or (
        record.get("provenance") or {}
    ).get("by_node_type")
    if producer != "data":
        errors.append("data_dataset_producer_invalid")
    return errors


def _direct_child_terminal_artifact_ids(state: Any, child_run_id: str) -> list[str]:
    """Return the durable equivalent of a normal run_node all_child_artifacts."""
    summary = _child_run_terminal_summary(state, child_run_id)
    return _child_artifact_ids(summary.get("artifacts")) if isinstance(summary, dict) else []


def _direct_child_terminal_blocker_ids(state: Any, child_run_id: str) -> list[str]:
    """Find valid blockers listed in one direct child's terminal summary only."""
    matches: list[str] = []
    for artifact_id in _direct_child_terminal_artifact_ids(state, child_run_id):
        report = _read_child_run_artifact(
            state, child_run_id, artifact_id, require_sibling=True,
        )
        if not _data_blocked_report_errors(report, child_run_id):
            matches.append(artifact_id)
    return matches


def _direct_child_terminal_dataset_ids(state: Any, child_run_id: str) -> list[str]:
    """Find dataset deliverables retained by the same terminal Data child."""
    matches: list[str] = []
    for artifact_id in _direct_child_terminal_artifact_ids(state, child_run_id):
        record = _read_child_run_artifact(
            state, child_run_id, artifact_id, require_sibling=True,
        )
        if not _data_dataset_errors(record, child_run_id):
            matches.append(artifact_id)
    return matches


def _managed_data_dispatch_receipt_errors(
    state: Any,
    spec_id: str,
    request: dict[str, Any],
    delivery: dict[str, Any],
    *,
    data_run_id: str | None = None,
    blocked_report_id: str | None = None,
    require_blocked_report: bool,
) -> tuple[list[str], dict[str, Any] | None]:
    """Validate the receipt made by ``dispatch_data_request``.

    A Data blocked report alone proves only that *some* Data run stopped. This
    routine binds it to the exact validated spec, the exact payload sent by the
    managed wrapper, the direct child run, and either the returned artifact list
    or the durable terminal child evidence after a human-authorized pause.
    """
    errors: list[str] = []
    report: dict[str, Any] | None = None
    receipt = delivery.get("data_dispatch_receipt")
    if not isinstance(receipt, dict):
        return ["managed_data_dispatch_receipt_missing"], None
    expected = _data_dispatch_material(spec_id, request)
    for field in ("schema_version", "spec_id", "request_sha256", "payload_sha256"):
        if receipt.get(field) != expected[field]:
            errors.append("data_dispatch_" + field + "_mismatch")

    has_dispatch_state = "dispatch_state" in receipt
    dispatch_state = receipt.get("dispatch_state", "returned")
    if dispatch_state not in {"returned", "paused_pending_result", "resumed_terminal"}:
        errors.append("data_dispatch_state_invalid")
        dispatch_state = "invalid"

    child_run_id = str(receipt.get("child_run_id") or "").strip()
    if _RUN_ID_RE.fullmatch(child_run_id) is None:
        errors.append("data_dispatch_child_run_id_invalid")
    if receipt.get("child_node_type") != "data":
        errors.append("data_dispatch_child_node_type_invalid")
    raw_status = receipt.get("child_status")
    child_status = raw_status.strip() if isinstance(raw_status, str) else ""
    raw_artifact_ids = receipt.get("child_artifact_ids")
    artifact_ids = _child_artifact_ids(raw_artifact_ids)
    origin = receipt.get("child_artifact_ids_origin")

    if dispatch_state == "paused_pending_result":
        if child_status != "paused":
            errors.append("data_dispatch_paused_status_invalid")
        if raw_artifact_ids != [] or artifact_ids:
            errors.append("data_dispatch_paused_artifacts_invalid")
        if has_dispatch_state and origin != "unavailable_while_paused":
            errors.append("data_dispatch_paused_artifact_origin_invalid")
    else:
        if not child_status or child_status == "paused":
            errors.append("data_dispatch_child_status_invalid")
        if not isinstance(raw_artifact_ids, list) or raw_artifact_ids != artifact_ids:
            errors.append("data_dispatch_child_artifacts_invalid")
        if dispatch_state == "returned" and has_dispatch_state and origin != "run_node_return":
            errors.append("data_dispatch_returned_artifact_origin_invalid")
        if dispatch_state == "resumed_terminal" and origin != "direct_child_after_pause":
            errors.append("data_dispatch_resumed_artifact_origin_invalid")
        if dispatch_state == "resumed_terminal":
            terminal_summary = _child_run_terminal_summary(state, child_run_id)
            if not isinstance(terminal_summary, dict):
                errors.append("data_dispatch_resumed_child_summary_invalid")
            else:
                if str(terminal_summary.get("status") or "").strip() != child_status:
                    errors.append("data_dispatch_resumed_status_summary_mismatch")
                expected_artifact_ids = _child_artifact_ids(
                    terminal_summary.get("artifacts")
                )
                if artifact_ids != expected_artifact_ids:
                    errors.append("data_dispatch_resumed_artifacts_not_terminal_summary")

    start = _child_run_start_record(state, child_run_id)
    if start is None:
        errors.append("data_dispatch_child_run_unreadable")
    else:
        if start.get("node_type") != "data":
            errors.append("data_dispatch_child_run_not_data")
        if str(start.get("parent_run_id") or "") != str(getattr(state, "run_id", "") or ""):
            errors.append("data_dispatch_child_parent_mismatch")

    if not require_blocked_report:
        return errors, None
    if dispatch_state == "paused_pending_result":
        errors.append("data_dispatch_pending_reconciliation")
        return errors, None

    reported_run_id = str(
        data_run_id if data_run_id is not None else delivery.get("data_run_id") or ""
    ).strip()
    report_id = str(
        blocked_report_id if blocked_report_id is not None else delivery.get("blocked_report_id") or ""
    ).strip()
    if reported_run_id != child_run_id:
        errors.append("data_blocked_report_run_not_managed_dispatch")
    if (
        dispatch_state == "resumed_terminal"
        and _direct_child_terminal_blocker_ids(state, child_run_id) != [report_id]
    ):
        errors.append("data_dispatch_paused_blocker_missing_or_ambiguous")
    if _ARTIFACT_ID_RE.fullmatch(report_id) is None:
        errors.append("data_blocked_report_id_invalid")
    elif report_id not in artifact_ids:
        errors.append("data_blocked_report_not_returned_by_dispatch")
    else:
        report = _read_child_run_artifact(
            state, child_run_id, report_id, require_sibling=True,
        )
        errors.extend(_data_blocked_report_errors(report, child_run_id))
    return errors, report


async def _reconcile_data_dispatch(
    state: Any,
    spec_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Recover a paused managed Data receipt from one direct child run.

    Core owns pause/resume, while this Experiment-owned adapter reconstructs
    only the missing terminal child receipt. It records the complete durable
    artifact list, so a successful Data dataset remains consumable and a
    failure can still prove its unique blocker without a second lifecycle.
    """
    requested_spec_id = str(spec_id or "").strip()
    try:
        ledger = load_input_delivery_ledger(state)
    except InputDeliveryLedgerError as exc:
        return {
            "status": "error",
            "error": "input_delivery_ledger_unreadable",
            "detail": str(exc),
        }
    if not requested_spec_id:
        candidates = sorted(
            sid for sid, entry in ledger["specs"].items()
            if isinstance(entry, dict)
            and entry.get("lifecycle_status") == "active"
            and isinstance(entry.get("delivery"), dict)
            and (entry["delivery"].get("data_dispatch_receipt") or {}).get(
                "dispatch_state"
            ) == "paused_pending_result"
        )
        if len(candidates) != 1:
            return {
                "status": "error",
                "error": "spec_id is required unless exactly one managed Data dispatch is paused",
                "pending_spec_ids": candidates,
            }
        requested_spec_id = candidates[0]
    entry = ledger["specs"].get(requested_spec_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
        return {"status": "error", "error": "unknown spec_id; validate spec first"}
    if entry.get("lifecycle_status") != "active":
        return {
            "status": "error",
            "error": "spec_id_not_active",
            "superseded_by": entry.get("superseded_by"),
        }
    delivery = entry.get("delivery")
    if not isinstance(delivery, dict):
        return {"status": "error", "error": "input_delivery_ledger_entry_invalid"}

    intent_binding = audit_execution_intent_binding(state, require=True)
    if not intent_binding.get("passed", False):
        return {
            "status": "error",
            "error": "managed Data reconciliation requires a current immutable upstream-intent binding",
            "spec_id": requested_spec_id,
            "intent_binding": intent_binding,
        }

    receipt = delivery.get("data_dispatch_receipt")
    if not isinstance(receipt, dict):
        return {
            "status": "error",
            "error": "managed_data_dispatch_receipt_missing",
            "spec_id": requested_spec_id,
        }
    dispatch_state = receipt.get("dispatch_state", "returned")
    child_run_id = str(receipt.get("child_run_id") or "").strip()
    if dispatch_state not in {"paused_pending_result", "resumed_terminal"}:
        # P0a v4：派发已返回、账本已提交，但 census 看的 data_request_dispatched
        # 没落下（_dispatch_data_request 返回 dispatch_committed_but_receipt_event_
        # not_durable 时指到这里）——补记那一条，幂等；其余情况维持原拒绝。
        missing_receipt_event = not any(
            event.get("event") == "data_request_dispatched"
            and str(event.get("spec_id") or "") == requested_spec_id
            for event in _events(state)
        )
        if dispatch_state == "returned" and missing_receipt_event:
            state.append_transcript(
                "data_request_dispatched",
                spec_id=requested_spec_id,
                data_dispatch_receipt=receipt,
                input_delivery_ledger_artifact_id=(
                    (getattr(state, "hook_state", {}).get("input_delivery_ledger") or {})
                    .get("artifact_id")
                ),
                recorded_by="reconcile_data_dispatch",
            )
            return {
                "status": "success",
                "spec_id": requested_spec_id,
                "data_run_id": child_run_id,
                "child_status": receipt.get("child_status"),
                "dispatch_state": dispatch_state,
                "receipt_event_recorded": True,
                "already_returned": True,
            }
        return {
            "status": "error",
            "error": "managed Data dispatch is not waiting for pause reconciliation",
            "spec_id": requested_spec_id,
            "dispatch_state": dispatch_state,
        }

    terminal_summary = _child_run_terminal_summary(state, child_run_id)
    if not isinstance(terminal_summary, dict):
        return {
            "status": "error",
            "error": "data_dispatch_paused_child_not_terminal",
            "spec_id": requested_spec_id,
            "data_run_id": child_run_id,
            "next_step": "wait for the same Data child to finish; do not dispatch another child",
        }
    terminal_status = str(terminal_summary.get("status") or "").strip()
    terminal_artifacts = terminal_summary.get("artifacts")
    terminal_artifact_ids = _child_artifact_ids(terminal_artifacts)
    dataset_ids = _direct_child_terminal_dataset_ids(state, child_run_id)
    blocker_ids = _direct_child_terminal_blocker_ids(state, child_run_id)
    if len(dataset_ids) == 1 and not blocker_ids:
        terminal_kind = "dataset"
        terminal_artifact_id = dataset_ids[0]
    elif len(blocker_ids) == 1 and not dataset_ids:
        terminal_kind = "blocked"
        terminal_artifact_id = blocker_ids[0]
    else:
        return {
            "status": "error",
            "error": "data_dispatch_paused_terminal_delivery_missing_or_ambiguous",
            "spec_id": requested_spec_id,
            "data_run_id": child_run_id,
            "dataset_artifact_ids": dataset_ids,
            "blocked_report_ids": blocker_ids,
            "next_step": (
                "resolve Data's terminal delivery ambiguity; do not select an artifact manually "
                "or dispatch a duplicate child"
            ),
        }

    if dispatch_state == "paused_pending_result":
        receipt.update({
            "dispatch_state": "resumed_terminal",
            "child_status": terminal_status,
            "child_artifact_ids": terminal_artifact_ids,
            "child_artifact_ids_origin": "direct_child_after_pause",
        })
        receipt_errors, _ = _managed_data_dispatch_receipt_errors(
            state,
            requested_spec_id,
            entry["request"],
            delivery,
            require_blocked_report=False,
        )
        if receipt_errors:
            return {
                "status": "error",
                "error": "managed_data_dispatch_reconciled_receipt_invalid",
                "spec_id": requested_spec_id,
                "receipt_errors": receipt_errors,
            }
        try:
            committed = save_input_delivery_ledger(state, ledger)
        except Exception as exc:
            return {
                "status": "error",
                "error": "input_delivery_ledger_persistence_failed",
                "detail": type(exc).__name__,
                "spec_id": requested_spec_id,
            }
        ledger_artifact_id = committed["artifact"]["id"]
        already_reconciled = False
    else:
        receipt_errors, _ = _managed_data_dispatch_receipt_errors(
            state,
            requested_spec_id,
            entry["request"],
            delivery,
            require_blocked_report=False,
        )
        if receipt_errors:
            return {
                "status": "error",
                "error": "managed_data_dispatch_reconciled_receipt_invalid",
                "spec_id": requested_spec_id,
                "receipt_errors": receipt_errors,
            }
        ledger_artifact_id = (
            (getattr(state, "hook_state", {}).get("input_delivery_ledger") or {}).get(
                "artifact_id"
            )
        )
        already_reconciled = True

    result = {
        "status": "success",
        "spec_id": requested_spec_id,
        "data_run_id": child_run_id,
        "child_status": terminal_status,
        "terminal_outcome": terminal_kind,
        "all_child_artifacts": terminal_artifacts,
        "input_delivery_ledger_artifact_id": ledger_artifact_id,
    }
    if terminal_kind == "dataset":
        result["dataset_artifact_id"] = terminal_artifact_id
    else:
        result["blocked_report_id"] = terminal_artifact_id
    if already_reconciled:
        result["already_reconciled"] = True
    state.append_transcript("data_dispatch_pause_reconciled", **result)
    return result


async def _dispatch_data_request(
    state: Any,
    spec_id: str,
    user_note: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Synchronously dispatch one validated Data request and persist its receipt.

    This remains a narrow wrapper over the existing run_node path. It neither
    creates another lifecycle nor grants fallback by itself: its duty is to
    bind the exact frozen request to one fresh direct Data child.
    """
    note = str(user_note or "").strip()
    if not note:
        return {
            "status": "error",
            "error": "user_note is required before dispatching Data so the user can see the declared input request",
            "spec_id": spec_id,
        }
    ledger, entry, context_error = _active_data_spec_context(state, spec_id)
    if context_error:
        return context_error
    request = entry["request"]
    delivery = entry["delivery"]
    if delivery.get("verified"):
        return {"status": "error", "error": "validated Data delivery already exists for this spec_id"}
    if delivery.get("data_terminally_blocked") or delivery.get("fallback_authorized"):
        return {
            "status": "error",
            "error": "Data dispatch is closed for this spec_id after a terminal blocker or fallback authorization",
        }
    if isinstance(delivery.get("data_dispatch_receipt"), dict):
        return {
            "status": "error",
            "error": (
                "this spec_id already has a managed Data dispatch; reconcile that direct child "
                "or supersede the spec before requesting Data again"
            ),
            "spec_id": spec_id,
        }

    intent_binding = audit_execution_intent_binding(state, require=True)
    if not intent_binding.get("passed", False):
        return {
            "status": "error",
            "error": "managed Data dispatch requires a current immutable upstream-intent binding",
            "spec_id": spec_id,
            "intent_binding": intent_binding,
        }
    if request.get("request_kind") == "formal_input_preparation":
        acceptance = resolve_run_acceptance(state, bind_if_absent=False)
        accepted_prereg = (
            acceptance["receipt"].get("governing_task_input_binding")
            if acceptance.get("passed", False) else None
        )
        if isinstance(accepted_prereg, dict):
            # A non-null receipt already selected one exact identity.  Verify
            # that exact immutable version/hash through the receipt-authoritative
            # contract; never make it compete again with unrelated catalog growth.
            exact_contract = load_run_contract(state)
            expected_prereg = {
                "artifact_id": (
                    str(exact_contract.get("prereg_artifact_id") or "").strip()
                    or None
                ),
                "version": exact_contract.get("prereg_version"),
                "content_hash": (
                    str(exact_contract.get("prereg_content_hash") or "").strip()
                    or None
                ),
            }
            current_prereg = {
                "passed": True,
                "status": "receipt_bound_exact_check",
                "binding": expected_prereg,
                "binding_source": exact_contract.get("prereg_binding_source"),
                "authorizing": False,
            }
        else:
            # A positive-null receipt has no selected identity.  Its live-right
            # is a non-authorizing observation used only to detect late inputs.
            current_prereg = observe_current_prereg_binding_witness(state)
            expected_prereg = (
                dict(current_prereg["binding"])
                if current_prereg.get("passed")
                and isinstance(current_prereg.get("binding"), dict)
                else {
                    "artifact_id": None,
                    "version": None,
                    "content_hash": None,
                }
            )
        live_contract = {
            "prereg_artifact_id": expected_prereg["artifact_id"],
            "prereg_version": expected_prereg["version"],
            "prereg_content_hash": expected_prereg["content_hash"],
        }
        valid_prereg = (
            isinstance(expected_prereg["artifact_id"], str)
            and bool(expected_prereg["artifact_id"])
            and isinstance(expected_prereg["version"], int)
            and not isinstance(expected_prereg["version"], bool)
            and expected_prereg["version"] >= 1
            and re.fullmatch(r"[0-9a-f]{64}", str(expected_prereg["content_hash"]).lower())
            is not None
        )
        if isinstance(accepted_prereg, dict) and not valid_prereg:
            current_prereg["passed"] = False
            current_prereg["status"] = "receipt_bound_prereg_unavailable"
        gate_a_request = request
        if acceptance.get("passed") and accepted_prereg is None and valid_prereg:
            # A positive-null receipt can never authorize this formal dispatch.
            # Fill an ephemeral comparison copy so Gate A verifies the caller's
            # live artifact identity and Gate B remains the authority decision.
            # Nothing is persisted and the child still cannot launch.
            gate_a_request = dict(request)
            gate_a_request.setdefault(
                "source_prereg_version", expected_prereg["version"],
            )
            gate_a_request.setdefault(
                "source_prereg_content_hash", expected_prereg["content_hash"],
            )
        formal_errors = _formal_request_contract_errors(
            gate_a_request, live_contract,
        )
        if not current_prereg.get("passed"):
            formal_errors.append(
                str(current_prereg.get("status") or "live_prereg_witness_unavailable")
            )
        if not acceptance.get("passed", False) or formal_errors or not valid_prereg:
            return {
                "status": "error",
                "error": "formal Data dispatch requires the current frozen preregistration receipt",
                "spec_id": spec_id,
                "formal_request_errors": formal_errors,
                "run_acceptance": acceptance,
                "current_prereg_witness": current_prereg,
            }
        if accepted_prereg != expected_prereg:
            return {
                "status": "error",
                "error": (
                    "formal Data dispatch requires this exact frozen preregistration receipt "
                    "to have been bound in the write-once run acceptance receipt"
                ),
                "spec_id": spec_id,
                "accepted_prereg_binding": accepted_prereg,
                "current_prereg_binding": expected_prereg,
            }

    material = _data_dispatch_material(spec_id, request)
    # Keep the compatibility admission immediately adjacent to the actual
    # child-run side effect.  Pure request/receipt validation above remains
    # authoritative and can therefore return its more specific correction.
    try:
        try:
            from .execution_action_census import pending_operation_action_block
        except ImportError:
            from tools.execution_action_census import pending_operation_action_block
        pending_block = pending_operation_action_block(
            state,
            {
                "tool": "dispatch_data_request",
                "program": "run_node:data",
                "read_only": False,
                "dry_run": False,
                "observed_effects": ["child_run", "workspace_write"],
            },
            {
                "decision": "route_not_required",
                "policy": "managed_child_dispatch",
                "effective_effects": ["child_run", "workspace_write"],
            },
        )
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "execution_action_census_guard_unavailable",
            "error": "无法核验 pending-assignment 动作边界，Data child 未启动。",
            "detail": type(exc).__name__,
            "spec_id": spec_id,
        }
    if pending_block is not None:
        return {**pending_block, "spec_id": spec_id}
    try:
        from shared.tools.run_node import _run_node_tool
        child_result = await _run_node_tool(
            state=state,
            node_type="data",
            node_inputs=material["node_inputs"],
            background=False,
            user_note=note,
            # A managed receipt must describe this request, never an older
            # interrupted Data child from the same session.
            resume_run_id="fresh",
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": "managed_data_dispatch_failed",
            "detail": type(exc).__name__,
            "spec_id": spec_id,
        }
    if not isinstance(child_result, dict):
        return {"status": "error", "error": "managed_data_dispatch_return_invalid", "spec_id": spec_id}

    child_run_id = str(child_result.get("child_run_id") or "").strip()
    paused = child_result.get("status") == "pause"
    receipt = {
        "schema_version": material["schema_version"],
        "spec_id": spec_id,
        "request_sha256": material["request_sha256"],
        "payload_sha256": material["payload_sha256"],
        "child_run_id": child_run_id,
        "child_node_type": child_result.get("child_node_type"),
        "dispatch_state": "paused_pending_result" if paused else "returned",
        "child_status": "paused" if paused else str(child_result.get("child_status") or "").strip(),
        "child_artifact_ids": [] if paused else _child_artifact_ids(child_result.get("all_child_artifacts")),
        "child_artifact_ids_origin": (
            "unavailable_while_paused" if paused else "run_node_return"
        ),
    }
    delivery["data_dispatch_receipt"] = receipt
    receipt_errors, _ = _managed_data_dispatch_receipt_errors(
        state, spec_id, request, delivery, require_blocked_report=False,
    )
    if receipt_errors:
        return {
            "status": "error",
            "error": "managed_data_dispatch_receipt_invalid",
            "spec_id": spec_id,
            "receipt_errors": receipt_errors,
            "child_result": child_result,
        }
    try:
        committed = save_input_delivery_ledger(state, ledger)
    except Exception as exc:
        return {
            "status": "error",
            "error": "input_delivery_ledger_persistence_failed",
            "detail": type(exc).__name__,
            "spec_id": spec_id,
        }
    result = dict(child_result)
    result.update({
        "spec_id": spec_id,
        "data_dispatch_receipt": receipt,
        "input_delivery_ledger_artifact_id": committed["artifact"]["id"],
    })
    # 这条事件就是 action census 看到的 dispatch 收据（P0a v4：不再另落第二条）。
    # 子 run 已经跑完、账本已经提交；只有这条没落下时，不能返回普通 success
    # （模型会再派一个子 run），也不能吞掉——返回"副作用已发生、账未结算"，
    # 出口是真实工具 reconcile_data_dispatch（它会把缺的这条事件补上）。
    try:
        state.append_transcript(
            "data_request_dispatched",
            spec_id=spec_id,
            data_dispatch_receipt=receipt,
            input_delivery_ledger_artifact_id=committed["artifact"]["id"],
        )
    except Exception as exc:
        return {
            **result,
            "status": "error",
            "error_code": "dispatch_committed_but_receipt_event_not_durable",
            "error_type": type(exc).__name__,
            "side_effect_committed": True,
            "payload_must_not_rerun": True,
            "do_not_retry_payload": True,
            "error": (
                f"managed Data dispatch for {spec_id} already ran and its delivery "
                "ledger is committed, but the census receipt event was not durable. "
                "Do not dispatch again; call reconcile_data_dispatch for this spec_id "
                "to record the missing receipt event."
            ),
            "next_action": {
                "owner": "experiment",
                "action": "reconcile_data_dispatch",
                "arguments": {"spec_id": spec_id},
            },
        }
    return result


async def _record_data_delivery_outcome(
    state: Any,
    spec_id: str,
    status: str = "",
    data_run_id: str | None = None,
    blocked_report_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """记录 Data 交不出货的结果 —— 两道判据合成一道。

    provenance（node20 / 23a0fe78）：报告必须来自本 spec 的受管派发回执，裸
    run_node 结果或任意历史报告都不能授权 fallback。
    分档（main / a3bcd65c）：判据取 data 自己写在 report 里的 status，分三档；
    可恢复档不开 fallback，同一 spec 两个不同 data run 都卡在可恢复档才升终态。
    """
    ledger, entry, context_error = _active_data_spec_context(state, spec_id)
    if context_error:
        return context_error
    delivery = entry["delivery"]

    receipt = delivery.get("data_dispatch_receipt")
    if isinstance(receipt, dict) and receipt.get("dispatch_state") == "paused_pending_result":
        return {
            "status": "error",
            "error": (
                "the managed Data child paused; call reconcile_data_dispatch first so Experiment "
                "can mechanically require one durable direct-child terminal blocker"
            ),
            "spec_id": spec_id,
            "next_step": "reconcile_data_dispatch(spec_id=...) before record_data_delivery_outcome",
        }

    receipt_errors, report = _managed_data_dispatch_receipt_errors(
        state,
        spec_id,
        entry["request"],
        delivery,
        data_run_id=data_run_id,
        blocked_report_id=blocked_report_id,
        require_blocked_report=True,
    )
    if receipt_errors:
        # 词表问题比 provenance 问题更可操作，且它意味着报告本身是读到了的：
        # 优先把 a3bcd65c 那句「合法取值是这些」交到现场，否则未知 status 只会
        # 收到一句不透明的 provenance 拒绝 —— 那正是 a3bcd65c 要治的病复发。
        # 判决拆除 O6（ca:1244 降格，2026-08-31）同样适用于这条早浮现通道：词表外
        # 状态不再是拒绝理由，按终态处理并记 corroborated:false（见下方主路径）。
        # 但只放行「词表不认识」这一种 receipt 错误 —— 真的 provenance 问题
        # （报告不来自本 spec 的受管派发）仍然拒，那是 B 类账本墙不是词表判决。
        status_only = all(
            str(e).startswith("data_blocked_report_status_invalid") for e in receipt_errors)
        if status_only:
            try:
                bad_payload = json.loads((report or {}).get("content") or "{}")
            except (TypeError, json.JSONDecodeError):
                bad_payload = {}
            try:
                state.append_transcript(
                    "data_delivery_status_uncorroborated",
                    blocked_report_id=blocked_report_id,
                    report_status=str((bad_payload or {}).get("status") or "").strip(),
                    known_statuses=sorted(
                        set(DATA_RECOVERABLE_OUTCOMES) | set(DATA_TERMINAL_OUTCOMES)),
                    receipt_errors=receipt_errors)
            except Exception:
                pass
            receipt_errors = []
        elif any(str(e).startswith("data_blocked_report_status_invalid") for e in receipt_errors):
            receipt_errors = [
                e for e in receipt_errors
                if not str(e).startswith("data_blocked_report_status_invalid")
            ]
    if receipt_errors:
        return {
            "status": "error",
            "error": (
                "Data terminal blocker must come from dispatch_data_request for this exact spec; "
                "a raw run_node result or an arbitrary historical Data report cannot authorize fallback"
            ),
            "spec_id": spec_id,
            "data_run_id": data_run_id,
            "blocked_report_id": blocked_report_id,
            "receipt_errors": receipt_errors,
            "next_step": (
                "dispatch_data_request(spec_id=..., user_note=...) first. For an ordinary return, "
                "pass its exact child_run_id and returned report id; after a pause, call "
                "reconcile_data_dispatch first and use its unique blocker result."
            ),
        }

    receipt = delivery["data_dispatch_receipt"]
    managed_run_id = str(receipt.get("child_run_id") or "").strip()
    managed_report_id = str(blocked_report_id or "").strip()
    report_source = "managed_data_child_run"

    # `status` 只是模型的复述，判据取 report 里 data 自己写的那个。
    try:
        report_payload = json.loads((report or {}).get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        report_payload = {}
    report_status = str((report_payload or {}).get("status") or "").strip()
    known = set(DATA_RECOVERABLE_OUTCOMES) | set(DATA_TERMINAL_OUTCOMES)
    status_corroborated = isinstance(report_payload, dict) and report_status in known
    if not status_corroborated:
        # 判决拆除 O6（ca:1244 降格，2026-08-31）：拿兄弟节点报告的措辞当状态
        # 转移许可是死路（词表漂一次现场就焊死）。照记：词表外状态按终态处理，
        # corroborated:false 如实进账。合法取值仍列出，便于上游修词表。
        try:
            state.append_transcript(
                "data_delivery_status_uncorroborated",
                blocked_report_id=managed_report_id,
                report_status=report_status, known_statuses=sorted(known))
        except Exception:
            pass
        report_status = report_status or "unrecorded"
    detail = _data_outcome_detail(report_payload)
    acquisition_evidence = _acquisition_attempt_evidence(report_payload)
    # ── 内容质量门（东亚 E2E 病灶）：终态词 + 零获取证据 = Data 内部失败 ────
    # planner 空转到 max_iterations_reached / 规划循环无进展落成的 fatal（或旧
    # 词表 recoverable_blocked/blocked/incomplete）里没有任何"对具体来源试过并
    # 失败"的记录。这种报告**不是**"数据不可得"，不得一步判终态解锁 fallback，
    # 也不得转述成"用户缺数据"。按可恢复档路由：重派同一 spec；两个不同的
    # data run 都交不出证据才升终态（复用下方既有的机械梯子，不会死锁）。
    internal_terminal = (report_status not in DATA_RECOVERABLE_OUTCOMES
                         and not acquisition_evidence)
    if report_status not in DATA_RECOVERABLE_OUTCOMES and acquisition_evidence:
        # 真终态：把证据摘要带给下游 —— 归因/问人措辞可以点名 URL 和失败类型。
        detail = {**detail, "acquisition_attempts": [
            {"source": item["source"], "failure": item["failure"]}
            for item in acquisition_evidence[:8]
        ]}

    if report_status in DATA_RECOVERABLE_OUTCOMES or internal_terminal:
        # ── 可恢复档：**不开 fallback** ────────────────────────────────────
        # fallback = 允许 experiment 自己造输入、并把整个 run 标成
        # 执行前提见证。为"少一个参数"或"少装一个工具"付这个代价是
        # 荒唐的 —— 那两种情况 data 自己写明了怎么续跑。
        attempts = sorted({*(delivery.get("data_recoverable_runs") or []),
                           managed_run_id} - {""})
        delivery.update({"data_recoverable_runs": attempts,
                         "data_status": report_status,
                         "data_run_id": managed_run_id,
                         "blocked_report_id": managed_report_id,
                         "blocked_report_source": report_source,
                         "data_outcome_detail": detail})
        if len(attempts) < 2:
            result = {"status": "success", "outcome": "recoverable",
                      "spec_id": spec_id, "data_status": report_status,
                      "data_terminally_blocked": False,
                      # ca:1244 降格的记账必须两条路由都送到 —— 走哪一档都不
                      # 影响「这个状态词有没有被佐证」这个事实。
                      "corroborated": status_corroborated,
                      **({} if status_corroborated
                         else {"known_statuses": sorted(known)}),
                      "data_run_id": managed_run_id,
                      "blocked_report_id": managed_report_id,
                      "blocked_report_source": report_source,
                      "detail": detail,
                      # 祈使句 + 点名下一步。只说"可恢复"会被当成"记完了"。
                      "next_step": (
                          DATA_RECOVERABLE_OUTCOMES[report_status]
                          if report_status in DATA_RECOVERABLE_OUTCOMES else
                          ("这是 Data 的内部失败（规划/执行循环未产生任何资产级获取"
                           "尝试记录），不是数据不可得，更不是用户缺数据。用**同一个 "
                           "spec_id** 重新 dispatch_data_request；不要据此请求用户"
                           "手工提供数据或改动数据源。")
                      ),
                      "note": (("这不是终态，fallback 仍然关闭。直接用同一个 spec_id 重新 "
                                "dispatch_data_request（experiment 侧没有要补齐的东西）；"
                                "第二个 data run 仍交不出获取证据即判终态。")
                               if internal_terminal else
                               ("这不是终态，fallback 仍然关闭。补齐后用同一个 spec_id 重新 "
                                "dispatch_data_request；如果第二次仍然卡在可恢复档，"
                                "再调本工具即判终态。"))}
            if internal_terminal:
                result["failure_class"] = "data_internal_failure_without_acquisition_evidence"
            try:
                committed = save_input_delivery_ledger(state, ledger)
            except Exception as exc:
                return {"status": "error",
                        "error": "input_delivery_ledger_persistence_failed",
                        "detail": type(exc).__name__}
            result["input_delivery_ledger_artifact_id"] = committed["artifact"]["id"]
            state.append_transcript("data_delivery_recoverable_outcome", **result)
            return result
        # 解除路径（必须有，否则这道闸就是死锁）：**两个不同的 data run** 都卡在
        # 可恢复档 —— 补过一次还是这样，按终态处理，fallback 打开。
        # 判据用"不同的 run 出现过两次"而不是"模型说它试过了"：前者机械可查。
        escalated_from = (f"{report_status}_without_acquisition_evidence"
                          if internal_terminal else report_status)
        report_status = "fatal"
        detail = {**detail, "escalated_from": escalated_from,
                  "recoverable_attempts": attempts}

    delivery.update({
        "data_terminally_blocked": True,
        "data_status": report_status,
        "data_run_id": managed_run_id,
        "blocked_report_id": managed_report_id,
        "blocked_report_source": report_source,
        "data_outcome_detail": detail,
        "data_dispatch_receipt": receipt,
        # ca:1244 降格的记账：词表外状态照记为终态，但佐证与否如实进账
        "data_status_corroborated": status_corroborated,
    })
    result = {
        "status": "success",
        "outcome": "terminal",
        "spec_id": spec_id,
        "data_status": report_status,
        "data_terminally_blocked": True,
        "data_run_id": managed_run_id,
        "blocked_report_id": managed_report_id,
        "blocked_report_source": report_source,
        "detail": detail,
        "corroborated": status_corroborated,
        # 词表外状态照记，但合法取值仍送到调用方手上（契约必须送到调用方）。
        **({} if status_corroborated else {"known_statuses": sorted(known)}),
    }
    if status and status not in {"blocked", "incomplete", report_status}:
        # 模型复述和 data 自己写的不一致 —— 说出来，别静默以任一方为准。
        result["caller_reported_status_ignored"] = status
    try:
        committed = save_input_delivery_ledger(state, ledger)
    except Exception as exc:
        return {
            "status": "error",
            "error": "input_delivery_ledger_persistence_failed",
            "detail": type(exc).__name__,
        }
    state.append_transcript("data_delivery_terminal_outcome", **result)
    return result


def _fallback_allowed(state: Any) -> tuple[bool, str, dict[str, Any]]:
    """Authorize an Experiment data-delivery backstop only from frozen policy.

    ``integration_e2e_fallback`` remains a compatibility mode with its original
    secondary-only semantics.  A new upstream prereg must explicitly opt into
    the generic ``experiment_data_fallback`` mode before a formal scientific
    run can acquire and verify its own declared input after Data terminates.
    """
    contract = load_run_contract(state)
    intent_binding = audit_execution_intent_binding(state, require=True)
    policy = contract.get("input_delivery_policy")
    if not isinstance(policy, dict) or policy.get("experiment_fallback_permitted") is not True:
        reason = "frozen pre_registration does not permit Experiment data fallback"
        if not intent_binding.get("passed", False):
            detail = str(intent_binding.get("reason") or "").strip()
            if detail:
                reason = f"{reason}; immutable upstream-intent binding is unavailable: {detail}"
        return False, reason, contract
    if not intent_binding.get("passed", False):
        detail = str(intent_binding.get("reason") or "").strip()
        reason = "Experiment data fallback requires a current immutable upstream-intent binding"
        return False, f"{reason}: {detail}" if detail else reason, contract
    mode = str(policy.get("mode") or "").strip()
    if mode == "integration_e2e_fallback":
        if contract.get("run_role") != "secondary":
            return False, "legacy integration_e2e_fallback is restricted to secondary runs", contract
        return True, "legacy secondary integration fallback allowed", contract
    if mode != "experiment_data_fallback":
        return False, "input_delivery_policy.mode must be experiment_data_fallback", contract
    if contract.get("execution_mode") != "scientific" or not contract.get("prereg_artifact_id"):
        return False, "formal Experiment data fallback requires a scientific run bound to a frozen pre_registration", contract
    expected_binding = {
        "artifact_id": str(contract.get("prereg_artifact_id") or "").strip() or None,
        "version": contract.get("prereg_version"),
        "content_hash": str(contract.get("prereg_content_hash") or "").strip() or None,
    }
    if intent_binding.get("prereg_binding") != expected_binding:
        return (
            False,
            "Experiment data fallback requires this exact frozen preregistration receipt to have been bound when the scientific scope was classified",
            contract,
        )
    return True, "authorized formal Experiment data fallback", contract


def _formal_request_contract_errors(
    spec: dict[str, Any], contract: dict[str, Any],
) -> list[str]:
    """Bind any formal input request to the exact frozen prereg receipt for this run."""
    errors: list[str] = []
    if spec.get("request_kind") != "formal_input_preparation":
        errors.append("formal_input_preparation_required")
    bound_prereg_id = str(contract.get("prereg_artifact_id") or "").strip()
    if not bound_prereg_id:
        return errors
    expected_version = contract.get("prereg_version")
    expected_hash = str(contract.get("prereg_content_hash") or "").lower()
    if str(spec.get("source_prereg_artifact_id") or "").strip() != bound_prereg_id:
        errors.append("source_prereg_artifact_id_mismatch")
    if spec.get("source_prereg_version") != expected_version:
        errors.append("source_prereg_version_mismatch")
    if str(spec.get("source_prereg_content_hash") or "").lower() != expected_hash:
        errors.append("source_prereg_content_hash_mismatch")
    return errors


def _fallback_request_contract_errors(spec: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """Add the fallback-only requirement that formal authority is bound and frozen."""
    errors = _formal_request_contract_errors(spec, contract)
    if spec.get("request_kind") != "formal_input_preparation":
        errors = [
            "fallback_requires_formal_input_preparation"
            if item == "formal_input_preparation_required" else item
            for item in errors
        ]
    if not str(contract.get("prereg_artifact_id") or "").strip():
        errors.append("fallback_requires_bound_frozen_prereg")
    return errors


def _fallback_delivery_receipt_errors(
    state: Any,
    spec_id: str,
    spec: dict[str, Any],
    delivery: dict[str, Any],
    contract: dict[str, Any],
) -> list[str]:
    """Recheck the durable authorization receipt before consuming fallback input."""
    errors = _fallback_request_contract_errors(spec, contract)
    # 判决拆除（ca:1285 删）：授权在先不再是校验前置；该事实由调用方以
    # fallback_pre_authorized 记账。data_terminal_blocker / blocked_report /
    # 受管派发回执绑定是 B 类出处墙，保持不动。
    if delivery.get("data_terminally_blocked") is not True:
        errors.append("data_terminal_blocker_missing")
    if not str(delivery.get("blocked_report_id") or "").strip():
        errors.append("data_blocked_report_missing")
    dispatch_errors, _report = _managed_data_dispatch_receipt_errors(
        state,
        spec_id,
        spec,
        delivery,
        require_blocked_report=True,
    )
    errors.extend(dispatch_errors)
    expected_policy = (contract.get("input_delivery_policy") or {}).get("mode")
    for field, expected in (
        ("fallback_contract_prereg_id", contract.get("prereg_artifact_id")),
        ("fallback_contract_prereg_version", contract.get("prereg_version")),
        ("fallback_contract_prereg_content_hash", contract.get("prereg_content_hash")),
        ("fallback_policy_mode", expected_policy),
    ):
        if delivery.get(field) != expected:
            errors.append(field + "_changed")
    return errors


async def _authorize_experiment_fallback(state: Any, spec_id: str, **_: Any) -> dict[str, Any]:
    # 结构取本分支（ledger 持久化 + 受管派发回执绑定），判决语义取 origin/main：
    #  · ca:1269 降格：data 未记 terminal blocker 不再拒绝授权，corroborated:false 进账
    #  · ca:1272 降格（呈裁③定案）：契约不允许 fallback 不再是拒绝理由 ——
    #    前置从「许可」变「效应」，使用 fallback 即记一条执行前提见证
    #    （run 内不可撤销），契约不允许这一事实照记。
    ledger, entry, context_error = _active_data_spec_context(state, spec_id)
    if context_error:
        return context_error
    spec = entry["request"]
    delivery = entry["delivery"]
    corroborated = bool(delivery.get("data_terminally_blocked"))
    if not corroborated:
        try:
            state.append_transcript(
                "experiment_fallback_uncorroborated", spec_id=spec_id,
                reason="no recorded terminal Data blocker for this spec")
        except Exception:
            pass
    dispatch_errors, _report = _managed_data_dispatch_receipt_errors(
        state,
        spec_id,
        spec,
        delivery,
        require_blocked_report=corroborated,
    )
    allowed, reason, contract = _fallback_allowed(state)
    request_errors = _fallback_request_contract_errors(spec, contract)
    try:
        from .run_contract import record_execution_precondition_witness
    except ImportError:
        from tools.run_contract import record_execution_precondition_witness
    record_execution_precondition_witness(
        state, "authorize_experiment_fallback",
        "experiment fallback inputs replace the formal Data delivery")
    delivery.update({
        "fallback_authorized": True,
        "fallback_contract_prereg_id": contract.get("prereg_artifact_id"),
        "fallback_contract_prereg_version": contract.get("prereg_version"),
        "fallback_contract_prereg_content_hash": contract.get("prereg_content_hash"),
        "fallback_policy_mode": (contract.get("input_delivery_policy") or {}).get("mode"),
        "fallback_permitted_by_contract": allowed,
        "fallback_corroborated": corroborated,
        **({"fallback_dispatch_binding_errors": dispatch_errors} if dispatch_errors else {}),
        **({"fallback_request_contract_errors": request_errors} if request_errors else {}),
    })
    result = {
        "status": "success",
        "spec_id": spec_id,
        "provider": "experiment_fallback",
        "execution_precondition_unmet": True,
        "corroborated": corroborated,
        "fallback_permitted_by_contract": allowed,
        "fallback_policy_mode": delivery["fallback_policy_mode"],
        **({} if allowed else {"contract_restriction_recorded": reason}),
        **({"dispatch_binding_errors": dispatch_errors} if dispatch_errors else {}),
        **({"request_contract_errors": request_errors} if request_errors else {}),
    }
    try:
        committed = save_input_delivery_ledger(state, ledger)
    except Exception as exc:
        return {
            "status": "error",
            "error": "input_delivery_ledger_persistence_failed",
            "detail": type(exc).__name__,
        }
    result["input_delivery_ledger_artifact_id"] = committed["artifact"]["id"]
    state.append_transcript("experiment_fallback_authorized", **result)
    return result


def _validate_experiment_fallback_inputs(
    state: Any,
    fallback_artifact_id: str,
    spec_id: str,
    spec: dict[str, Any],
    delivery: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Pure validation used both at verification and immediately before execution."""
    errors = _fallback_delivery_receipt_errors(
        state, spec_id, spec, delivery, contract,
    )
    record = state.read_artifact(fallback_artifact_id)
    if not isinstance(record, dict) or record.get("type") != "experiment_fallback_inputs":
        return [*errors, "fallback_artifact_id_invalid"], {}
    if str(record.get("produced_by_run_id") or "") != str(getattr(state, "run_id", "") or ""):
        return [*errors, "fallback_artifact_not_current_run"], {}
    try:
        content = json.loads(record.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    package_text = str(content.get("package_dir") or "").strip()
    manifest_text = str(content.get("manifest_path") or "").strip()
    package_dir = Path(package_text).expanduser() if package_text else None
    manifest_path = Path(manifest_text).expanduser() if manifest_text else None
    if content.get("generation_mode") != "experiment_fallback_after_data_failure":
        errors.append("generation_mode_invalid")
    if content.get("source_prereg_artifact_id") != spec.get("source_prereg_artifact_id"):
        errors.append("source_prereg_artifact_id_mismatch")
    if content.get("source_prereg_version") != spec.get("source_prereg_version"):
        errors.append("source_prereg_version_mismatch")
    if str(content.get("source_prereg_content_hash") or "").lower() != str(spec.get("source_prereg_content_hash") or "").lower():
        errors.append("source_prereg_content_hash_mismatch")
    report_ids = content.get("data_blocked_report_ids")
    if not isinstance(report_ids, list) or delivery.get("blocked_report_id") not in report_ids:
        errors.append("data_blocked_report_not_linked")
    expected = contract.get("expected_params")
    if not isinstance(expected, dict) or content.get("scientific_params") != expected:
        errors.append("scientific_params_mismatch")
    if package_dir is None or not package_dir.is_dir():
        errors.append("package_path_not_found_or_not_directory")
    manifest: dict[str, Any] = {}
    if manifest_path is None or not manifest_path.is_file():
        errors.append("manifest_path_not_found")
    else:
        try:
            parsed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(parsed_manifest, dict):
                raise ValueError("not_object")
            manifest = parsed_manifest
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            errors.append("manifest_not_readable_json")
    expected_manifest_hash = str(content.get("manifest_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
        errors.append("manifest_sha256_missing_or_invalid")
    elif manifest_path is not None and manifest_path.is_file():
        try:
            manifest_digest = hashlib.sha256()
            with manifest_path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    manifest_digest.update(block)
            if manifest_digest.hexdigest() != expected_manifest_hash:
                errors.append("manifest_sha256_mismatch")
        except OSError:
            errors.append("manifest_unreadable")
    declared_hashes = {
        str(item.get("path")): str(item.get("sha256"))
        for item in manifest.get("files", [])
        if isinstance(item, dict)
    }
    required_assets = spec.get("required_assets")
    if not isinstance(required_assets, list):
        errors.append("required_assets_invalid")
        required_assets = []
    for asset in required_assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            errors.append("required_asset_invalid")
            continue
        asset_name = asset["name"]
        target = package_dir / asset_name if package_dir is not None else None
        if target is None or not target.is_file():
            errors.append("missing_requested_asset:" + asset_name)
            continue
        declared = declared_hashes.get(asset_name)
        try:
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            actual_hash = digest.hexdigest()
        except OSError:
            errors.append("asset_unreadable:" + asset_name)
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", declared or ""):
            errors.append("manifest_hash_missing:" + asset_name)
        elif declared != actual_hash:
            errors.append("manifest_hash_mismatch:" + asset_name)
    return errors, {
        "package_dir": str(package_dir.resolve()) if package_dir is not None and package_dir.is_dir() else None,
        "manifest_path": str(manifest_path.resolve()) if manifest_path is not None and manifest_path.is_file() else None,
    }


async def _verify_experiment_fallback_inputs(state: Any, fallback_artifact_id: str, spec_id: str, **_: Any) -> dict[str, Any]:
    ledger, entry, context_error = _active_data_spec_context(state, spec_id)
    if context_error:
        return context_error
    spec = entry["request"]
    delivery = entry["delivery"]
    # 判决拆除（ca:1285 删，2026-08-31）：「未事先授权不许校验」是顺序仪式 ——
    # ca:1272 降格后授权步骤无独立内容，校验本身照做。`if not allowed` 是同一
    # 判决在 verify 侧的等价复活（authorize 侧已降格，这里再拒等于把降格作废），
    # 一并拆除。两个事实如实进账，成功即记一条执行前提见证（效应跟着
    # 「用了」这个事实走，不跟仪式走 —— 下方既有代码首次真正生效）。
    fallback_pre_authorized = bool(delivery.get("fallback_authorized"))
    allowed, reason, contract = _fallback_allowed(state)
    errors, details = _validate_experiment_fallback_inputs(
        state, fallback_artifact_id, spec_id, spec, delivery, contract,
    )
    passed = not errors
    if passed:
        delivery.update({
            "provider": "experiment_fallback",
            "verified": True,
            "input_package_artifact_id": fallback_artifact_id,
            "package_dir": details["package_dir"],
            "manifest_path": details["manifest_path"],
        })
        # 使用 fallback 即机械降（O1/呈裁③）：即使跳过了 authorize 步骤，
        # 效应也跟着「用了」这个事实走，不跟仪式走。
        try:
            from .run_contract import record_execution_precondition_witness
        except ImportError:
            from tools.run_contract import record_execution_precondition_witness
        record_execution_precondition_witness(
            state, "verify_experiment_fallback_inputs",
            "experiment fallback inputs replace the formal Data delivery")
    result = {
        "status": "success" if passed else "error",
        "passed": passed,
        "spec_id": spec_id,
        "fallback_artifact_id": fallback_artifact_id,
        "errors": errors,
        # 效应而非许可：验收通过即意味着用了 fallback，已记一条执行前提见证
        "execution_precondition_unmet": True,
        "fallback_policy_mode": delivery.get("fallback_policy_mode"),
        "fallback_pre_authorized": fallback_pre_authorized,
        "fallback_permitted_by_contract": allowed,
        **({} if allowed else {"contract_restriction_recorded": reason}),
    }
    if passed:
        try:
            committed = save_input_delivery_ledger(state, ledger)
        except Exception as exc:
            result.update({
                "status": "error",
                "passed": False,
                "persistence_error": "input_delivery_ledger_persistence_failed",
                "persistence_detail": type(exc).__name__,
            })
        else:
            result["input_delivery_ledger_artifact_id"] = committed["artifact"]["id"]
    state.append_transcript("experiment_fallback_inputs_verification", **result)
    return result


def audit_input_delivery_for_execution(
    state: Any,
    input_package_artifact_id: str | None = None,
    input_package_bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify the exact Data packages consumed by one execution.

    A single package id cannot represent two independently validated request
    specs.  Keep the legacy scalar only for the unambiguous one-spec case;
    multi-spec execution must provide the complete ``spec_id -> artifact_id``
    mapping.  The mapping selects no authority: every entry is checked against
    the already verified Data delivery owned by this run.
    """
    try:
        entries = active_input_delivery_entries(state)
    except InputDeliveryLedgerError as exc:
        return {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["input_delivery_ledger_unreadable"],
            "reason": str(exc),
            "spec_ids": [],
        }
    active = sorted(
        (
            (str(spec_id), entry.get("delivery") or {})
            for spec_id, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("delivery"), dict)
        ),
        key=lambda pair: pair[0],
    )
    if not active:
        contract = load_run_contract(state)
        if requires_experiment_fallback_input_gate(contract):
            return {
                "passed": False,
                "applicable": True,
                "blocking_reasons": ["experiment_fallback_input_required"],
                "reason": (
                    "the frozen generic Experiment fallback policy requires a validated, "
                    "Data-blocked, authorized, and verified formal input package before execution"
                ),
                "spec_ids": [],
            }
        return {
            "passed": True,
            "applicable": False,
            "reason": "no formal data request registered",
            "spec_ids": [],
        }

    active_ids = [sid for sid, _ in active]
    unverified = [sid for sid, item in active if not item.get("verified")]
    if unverified:
        return {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["formal_input_delivery_unverified"],
            "spec_ids": unverified,
        }

    bindings: dict[str, str]
    if input_package_bindings is not None:
        if not isinstance(input_package_bindings, dict):
            return {
                "passed": False,
                "applicable": True,
                "blocking_reasons": ["input_package_bindings_invalid"],
                "spec_ids": active_ids,
            }
        bindings = {
            str(spec_id): str(artifact_id)
            for spec_id, artifact_id in input_package_bindings.items()
            if str(spec_id).strip() and str(artifact_id).strip()
        }
        missing = sorted(set(active_ids) - set(bindings))
        extra = sorted(set(bindings) - set(active_ids))
        if missing or extra:
            return {
                "passed": False,
                "applicable": True,
                "blocking_reasons": ["input_package_binding_incomplete"],
                "spec_ids": active_ids,
                "missing_spec_ids": missing,
                "unknown_spec_ids": extra,
            }
    else:
        if len(active) != 1 and input_package_artifact_id:
            return {
                "passed": False,
                "applicable": True,
                "blocking_reasons": ["input_package_binding_ambiguous"],
                "spec_ids": active_ids,
                "hint": "multiple data specs require input_package_bindings",
            }
        if not input_package_artifact_id:
            reason = (
                "input_package_binding_ambiguous"
                if len(active) != 1 else "input_package_artifact_missing"
            )
            return {
                "passed": False,
                "applicable": True,
                "blocking_reasons": [reason],
                "spec_ids": active_ids,
                "hint": (
                    "multiple data specs require input_package_bindings"
                    if len(active) != 1 else None
                ),
            }
        bindings = {active[0][0]: str(input_package_artifact_id)}

    mismatched = [
        sid for sid, item in active
        if item.get("input_package_artifact_id") != bindings.get(sid)
    ]
    if mismatched:
        return {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["input_package_artifact_mismatch"],
            "spec_ids": mismatched,
            "input_package_bindings": bindings,
        }
    formal_request_errors: dict[str, list[str]] = {}
    contract = load_run_contract(state)
    for sid, _ in active:
        entry = entries.get(sid) or {}
        spec = entry.get("request") if isinstance(entry, dict) else None
        if isinstance(spec, dict) and spec.get("request_kind") == "formal_input_preparation":
            errors = _formal_request_contract_errors(spec, contract)
            if errors:
                formal_request_errors[sid] = errors
    if formal_request_errors:
        return {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["formal_input_request_not_bound"],
            "spec_ids": sorted(formal_request_errors),
            "input_package_bindings": bindings,
            "formal_request_errors": formal_request_errors,
        }
    fallback_receipt_errors: dict[str, list[str]] = {}
    fallback_ids = [sid for sid, item in active if item.get("provider") == "experiment_fallback"]
    if fallback_ids:
        allowed, reason, contract = _fallback_allowed(state)
        for sid in fallback_ids:
            entry = entries.get(sid) or {}
            spec = entry.get("request") if isinstance(entry, dict) else None
            delivery = entry.get("delivery") if isinstance(entry, dict) else None
            errors: list[str] = []
            if not allowed:
                errors.append(reason)
            elif not isinstance(spec, dict) or not isinstance(delivery, dict):
                errors.append("fallback_ledger_entry_invalid")
            else:
                artifact_id = str(delivery.get("input_package_artifact_id") or "").strip()
                artifact_errors, _ = _validate_experiment_fallback_inputs(
                    state, artifact_id, sid, spec, delivery, contract,
                )
                errors.extend(artifact_errors)
            if errors:
                fallback_receipt_errors[sid] = errors
    if fallback_receipt_errors:
        return {
            "passed": False,
            "applicable": True,
            "blocking_reasons": ["experiment_fallback_authorization_invalid"],
            "spec_ids": sorted(fallback_receipt_errors),
            "input_package_bindings": bindings,
            "fallback_receipt_errors": fallback_receipt_errors,
        }
    return {
        "passed": True,
        "applicable": True,
        "spec_ids": active_ids,
        "input_package_bindings": bindings,
        "providers": [item.get("provider") for _, item in active],
    }


register_tool(ToolDefinition(
    name="reconcile_data_dispatch",
    description=(
        "After a managed Data child pauses and later resumes, recover its complete direct-child "
        "terminal artifact receipt from the durable input-delivery ledger. A unique dataset can "
        "continue to consumption verification; a unique blocker can be recorded for fallback. "
        "With exactly one pending request, spec_id may be omitted; ambiguity fails closed."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"spec_id": {"type": "string"}},
    },
    allowed_node_types=["experiment"], risk_level="low",
), _reconcile_data_dispatch)
register_tool(ToolDefinition(
    name="dispatch_data_request",
    description=(
        "Synchronously dispatch one validated Data request through the existing run_node path and "
        "persist a hash-bound current-child receipt. Use this, rather than raw run_node(data), for "
        "any formal request that may need an Experiment fallback after a terminal Data blocker."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"spec_id": {"type": "string"}, "user_note": {"type": "string"}},
        "required": ["spec_id", "user_note"],
    },
    allowed_node_types=["experiment"], risk_level="low",
), _dispatch_data_request)
register_tool(ToolDefinition(
    name="record_data_delivery_outcome",
    description=(
        "记录 data 交不出货的结果。**判据取 data 自己写在 blocked report 里的 status**，"
        "分三档：`needs_input`（补输入后用同一个 spec_id 重派）、`externally_blocked`"
        "（experiment 补环境后重派）、`fatal`（终态，之后才谈 fallback）。前两档返回 "
        "outcome=recoverable 且**不**开 fallback；同一个 spec 有两个不同的 data run 都卡在"
        "前两档时自动升为终态。"
    ),
    parameters_schema={"type": "object", "properties": {
        "spec_id": {"type": "string"},
        "data_run_id": {"type": "string", "description": "run_node 返回的 child_run_id；blocked report 不会自动回填到本 run，没有它读不到。"},
        "blocked_report_id": {"type": "string"},
        "status": {"type": "string",
                   "description": "可选，且只是复述；不一致时以 report 里的 status 为准。",
                   "enum": ["blocked", "incomplete", "needs_input", "externally_blocked", "fatal"]},
    }, "required": ["spec_id", "blocked_report_id"]},
    allowed_node_types=["experiment"], risk_level="low"), _record_data_delivery_outcome)
register_tool(ToolDefinition(name="authorize_experiment_fallback", description="Authorize a frozen Experiment data fallback after Data blocks.", parameters_schema={"type": "object", "properties": {"spec_id": {"type": "string"}}, "required": ["spec_id"]}, allowed_node_types=["experiment"], risk_level="low"), _authorize_experiment_fallback)
register_tool(ToolDefinition(name="verify_experiment_fallback_inputs", description="Verify fallback package files, manifest, Data blocker linkage, and frozen scientific parameters.", parameters_schema={"type": "object", "properties": {"fallback_artifact_id": {"type": "string"}, "spec_id": {"type": "string"}}, "required": ["fallback_artifact_id", "spec_id"]}, allowed_node_types=["experiment"], risk_level="low"), _verify_experiment_fallback_inputs)

def audit_result_evidence(state: Any) -> dict[str, Any]:
    """Require the two frozen result artifacts before Experiment closes its evidence contract.

    These scientific readers intentionally retain the direct current-run view:
    Experiment scientific-audit ownership will converge them in a dedicated
    active-view package after the operation closure path is stable.  Delete
    those direct reads only when that package proves both scientific consumers
    use the shared active view.
    """
    errors: list[str] = []
    details: dict[str, Any] = {}
    for artifact_type in ("raw_results", "clean_results"):
        try:
            summaries = current_run_artifacts(state, artifact_type)
            artifact_id = str(summaries[-1]["id"]) if summaries else ""
            loaded = state.read_artifact(artifact_id) if artifact_id else None
            record = ({**loaded, "id": artifact_id} if isinstance(loaded, dict) else loaded)
        except Exception:
            record = None
        metadata = (record or {}).get("metadata") if isinstance(record, dict) else {}
        details[artifact_type] = {"artifact_id": (record or {}).get("id") if isinstance(record, dict) else None,
                                  "frozen": bool(isinstance(metadata, dict) and metadata.get("frozen"))}
        if not isinstance(record, dict):
            errors.append(f"missing {artifact_type}")
            continue
        if not isinstance(metadata, dict) or not metadata.get("frozen"):
            errors.append(f"{artifact_type} is not frozen")
            continue
        if artifact_type == "raw_results":
            # The manifest is only a pointer; re-verify retained bytes at the
            # final evidence closure so a vanished or modified raw file cannot
            # remain reviewable merely because it once froze successfully.
            _, raw_errors = _validate_raw_results_manifest(record, verify_files=True)
            errors.extend(raw_errors)
        else:
            errors.extend(_validate_clean_results_payload(state, record))
    return {"passed": not errors, "reason": "frozen raw_results and clean_results present" if not errors else "; ".join(errors),
            "artifacts": details, "errors": errors}


def audit_scientific_question_closure(state: Any) -> dict[str, Any]:
    """Require every frozen question to be answered by a formal run's real evidence.

    The prereg parser remains the authority for question/closure syntax.  This
    audit only verifies concrete execution facts: a finite, explicitly
    ``measured`` metric must cite frozen raw evidence from the current run;
    an asserted statement must cite a current evidence artifact or this run's
    identity.  Therefore an ``estimated`` value, a proxy receipt, or a prior
    run's artifact may inform diagnosis but cannot close the original task.
    """
    contract = load_run_contract(state)
    if contract.get("execution_mode") != "scientific":
        return {
            "passed": True,
            "applicable": False,
            "status": "not_scientific",
            "reason": "operation receipts never close scientific questions",
            "questions": [],
        }

    intent_binding = audit_execution_intent_binding(state, require=True)
    if not intent_binding.get("passed", False):
        return {
            "passed": False,
            "applicable": True,
            "status": "execution_intent_binding_required",
            "reason": (
                "scientific question closure requires immutable upstream input and "
                "applicable frozen prereg binding before any result can close the requested research question"
            ),
            "questions": [],
            "intent_binding": intent_binding,
        }
    # Every scientific child remains bound to the upstream intent, but a
    # secondary/diagnostic run can contribute one measurement without truthfully
    # closing the whole prereg. Existing formal-verdict contract defines the result authority; do not
    # create a second closure role or lifecycle state here.
    if not bool(contract.get("requires_hypothesis_verdict")):
        return {
            "passed": True,
            "applicable": False,
            "status": "nonterminal_scientific_subrun",
            "reason": (
                "secondary/non-formal scientific run is evidence-bearing but does not "
                "individually close every frozen research question"
            ),
            "questions": [],
            "intent_binding": intent_binding,
            **({"not_applicable_reason": role_reason}
               if (role_reason := run_role_non_applicability_reason(contract))
               else {}),
        }

    prereg = load_bound_frozen_prereg(state)
    if not prereg:
        return {
            "passed": False,
            "applicable": True,
            "status": "missing_bound_prereg",
            "reason": (
                "scientific run has no exactly readable frozen prereg; Experiment must "
                "ask/block instead of declaring a substitute method or question complete"
            ),
            "questions": [],
            "intent_binding": intent_binding,
        }

    from core.prereg_commitments import parse_questions

    questions = parse_questions(str(prereg.get("content") or ""))
    if not questions:
        return {
            "passed": False,
            "applicable": True,
            "status": "no_closure_contract",
            "reason": (
                "the bound frozen prereg has no parseable Research Questions; Experiment "
                "may retain diagnostics but cannot claim the upstream scientific task completed"
            ),
            "pre_registration_id": prereg.get("id"),
            "questions": [],
            "intent_binding": intent_binding,
        }

    closure_items = [
        (question_id, item)
        for question_id, question in questions.items()
        for item in question.closure
    ]
    if not closure_items:
        return {
            "passed": False,
            "applicable": True,
            "status": "no_closure_contract",
            "reason": (
                "the bound frozen questions have no measurable/dischargeable closure items; "
                "ask upstream to amend the prereg instead of treating a proxy as fulfilment"
            ),
            "pre_registration_id": prereg.get("id"),
            "questions": [{"question_id": key, "closed": False, "items": []}
                          for key in sorted(questions)],
            "intent_binding": intent_binding,
        }

    integrity = audit_experiment_log_integrity(state)
    result_evidence = audit_result_evidence(state)
    if not integrity.get("passed") or not result_evidence.get("passed"):
        return {
            "passed": False,
            "applicable": True,
            "status": "missing_current_evidence",
            "reason": "; ".join(filter(None, [
                integrity.get("reason") if not integrity.get("passed") else "",
                result_evidence.get("reason") if not result_evidence.get("passed") else "",
            ])),
            "pre_registration_id": prereg.get("id"),
            "questions": [],
            "experiment_log_id": integrity.get("canonical_log_id"),
            "result_evidence": result_evidence,
            "intent_binding": intent_binding,
        }

    log_id = str(integrity.get("canonical_log_id") or "")
    log = state.read_artifact(log_id) if log_id else None
    metadata = (log or {}).get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata.get("frozen"):
        return {
            "passed": False,
            "applicable": True,
            "status": "missing_current_evidence",
            "reason": "canonical experiment_log must be frozen before scientific question closure",
            "pre_registration_id": prereg.get("id"),
            "questions": [],
            "experiment_log_id": log_id or None,
            "result_evidence": result_evidence,
            "intent_binding": intent_binding,
        }

    frozen_raw_ids: set[str] = set()
    frozen_clean_ids: set[str] = set()
    invalid_raw_evidence: dict[str, list[str]] = {}
    invalid_clean_evidence: dict[str, list[str]] = {}
    for artifact_type, destination in (
        ("raw_results", frozen_raw_ids),
        ("clean_results", frozen_clean_ids),
    ):
        for summary in current_run_artifacts(state, artifact_type):
            artifact_id = str(summary.get("id") or "")
            record = state.read_artifact(artifact_id) if artifact_id else None
            record_metadata = (record or {}).get("metadata") or {}
            if not (isinstance(record_metadata, dict) and record_metadata.get("frozen")):
                continue
            if artifact_type == "raw_results":
                if not isinstance(record, dict):
                    invalid_raw_evidence[artifact_id] = ["raw_results artifact cannot be read"]
                    continue
                _, manifest_errors = _validate_raw_results_manifest(record, verify_files=True)
                if manifest_errors:
                    invalid_raw_evidence[artifact_id] = manifest_errors
                    continue
            elif artifact_type == "clean_results":
                if not isinstance(record, dict):
                    invalid_clean_evidence[artifact_id] = ["clean_results artifact cannot be read"]
                    continue
                clean_errors = _validate_clean_results_payload(state, record)
                if clean_errors:
                    invalid_clean_evidence[artifact_id] = clean_errors
                    continue
            destination.add(artifact_id)

    measured_metrics = metadata.get("measured_metrics")
    measured_metrics = measured_metrics if isinstance(measured_metrics, dict) else {}
    discharges = metadata.get("closure_discharges")
    discharges = discharges if isinstance(discharges, dict) else {}
    evidence_ids = frozen_raw_ids | frozen_clean_ids
    current_run_id = str(getattr(state, "run_id", "") or "")
    rendered_questions: dict[str, dict[str, Any]] = {
        question_id: {"question_id": question_id, "closed": True, "items": []}
        for question_id in sorted(questions)
    }
    unresolved: list[dict[str, Any]] = []

    for question_id, item in closure_items:
        detail: dict[str, Any] = {
            "key": item.key,
            "kind": item.kind,
            "passed": False,
        }
        if item.kind == "numeric":
            metric_key = str(item.metric or item.key)
            measurement = measured_metrics.get(metric_key)
            if not isinstance(measurement, dict):
                detail["reason"] = f"metric {metric_key} is missing from experiment_log.metadata.measured_metrics"
            elif measurement.get("status") != "measured":
                detail["reason"] = f"metric {metric_key} must be status=measured, not estimated/proxy/degraded"
            else:
                value = measurement.get("value")
                raw_id = str(measurement.get("raw_results_artifact_id") or "")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    detail["reason"] = f"metric {metric_key} must have a finite numeric measured value"
                elif raw_id in invalid_raw_evidence:
                    detail["reason"] = (
                        f"metric {metric_key} cites raw_results whose retained bytes no longer verify: "
                        + "; ".join(invalid_raw_evidence[raw_id])
                    )
                elif raw_id not in frozen_raw_ids:
                    detail["reason"] = f"metric {metric_key} must cite a current frozen raw_results artifact"
                else:
                    detail.update({"passed": True, "value": value, "raw_results_artifact_id": raw_id})
        else:
            discharge = discharges.get(item.key)
            if not isinstance(discharge, dict):
                detail["reason"] = f"statement closure {item.key} is missing from experiment_log.metadata.closure_discharges"
            elif discharge.get("status") != "discharged":
                detail["reason"] = f"statement closure {item.key} must be status=discharged"
            else:
                artifact_candidates = [
                    str(discharge.get(key) or "")
                    for key in ("raw_results_artifact_id", "artifact_id", "evidence_artifact_id")
                ]
                evidence = discharge.get("evidence")
                if isinstance(evidence, str):
                    artifact_candidates.append(evidence)
                has_current_evidence = any(candidate in evidence_ids for candidate in artifact_candidates)
                has_current_run = str(discharge.get("run_id") or "") == current_run_id
                invalid_candidates = [
                    candidate for candidate in artifact_candidates
                    if candidate in invalid_raw_evidence or candidate in invalid_clean_evidence
                ]
                if not has_current_evidence and not has_current_run:
                    if invalid_candidates:
                        invalid_reasons = [
                            reason
                            for candidate in invalid_candidates
                            for reason in (
                                invalid_raw_evidence.get(candidate, [])
                                + invalid_clean_evidence.get(candidate, [])
                            )
                        ]
                        detail["reason"] = (
                            f"statement closure {item.key} cites evidence that no longer verifies: "
                            + "; ".join(invalid_reasons)
                        )
                    else:
                        detail["reason"] = f"statement closure {item.key} must cite current frozen evidence or this run_id"
                else:
                    detail.update({"passed": True, "evidence_artifact_ids": [
                        candidate for candidate in artifact_candidates if candidate in evidence_ids
                    ], "run_id": discharge.get("run_id")})
        rendered_questions[question_id]["items"].append(detail)
        if not detail["passed"]:
            rendered_questions[question_id]["closed"] = False
            unresolved.append({"question_id": question_id, **detail})

    passed = not unresolved
    return {
        "passed": passed,
        "applicable": True,
        "status": "closed" if passed else "unresolved",
        "reason": (
            "every frozen scientific closure item is backed by this run's measured evidence"
            if passed else "scientific question closure remains unresolved; do not claim the upstream task complete"
        ),
        "pre_registration_id": prereg.get("id"),
        "prereg_version": contract.get("prereg_version"),
        "prereg_content_hash": contract.get("prereg_content_hash"),
        "experiment_log_id": log_id,
        "raw_results_artifact_ids": sorted(frozen_raw_ids),
        "clean_results_artifact_ids": sorted(frozen_clean_ids),
        "questions": [rendered_questions[key] for key in sorted(rendered_questions)],
        "unresolved_items": unresolved,
        "result_evidence": result_evidence,
        "intent_binding": intent_binding,
    }



def _operation_result_bundle(state: Any) -> dict[str, Any]:
    """Validate the non-scientific evidence triplet owned by the current run."""
    errors: list[str] = []
    details: dict[str, Any] = {}
    records_by_type: dict[str, list[dict[str, Any]]] = {}
    for artifact_type in ("raw_results", "clean_results"):
        loaded: list[dict[str, Any]] = []
        summaries, _ = active_closure_artifacts(state, artifact_type)
        for summary in summaries:
            artifact_id = str(summary.get("id") or "")
            record = state.read_artifact(artifact_id) if artifact_id else None
            if isinstance(record, dict):
                loaded.append({**record, "id": artifact_id})
        records_by_type[artifact_type] = loaded
        details[artifact_type] = {
            "artifact_ids": [str(item.get("id") or "") for item in loaded],
            "count": len(loaded),
        }
        if len(loaded) != 1:
            errors.append(f"operation requires exactly one current-run {artifact_type}")

    raw_record = (records_by_type["raw_results"] or [None])[0]
    clean_record = (records_by_type["clean_results"] or [None])[0]
    raw_id = str((raw_record or {}).get("id") or "")
    receipt_payload: dict[str, Any] | None = None
    raw_metadata: dict[str, Any] = {}
    if isinstance(raw_record, dict):
        raw_metadata = raw_record.get("metadata") or {}
        if not isinstance(raw_metadata, dict) or not raw_metadata.get("frozen"):
            errors.append("operation raw_results is not frozen")
        _, raw_errors = _validate_raw_results_manifest(raw_record, verify_files=True)
        errors.extend(raw_errors)
        raw_payload, raw_payload_errors = _raw_results_payload(raw_record)
        errors.extend(raw_payload_errors)
        receipt_entries = [
            item for item in (raw_payload or {}).get("files", [])
            if isinstance(item, dict) and item.get("role") == "operation_verification_receipt"
        ]
        if len(receipt_entries) != 1:
            errors.append("operation raw_results requires exactly one operation_verification_receipt")
        elif isinstance(receipt_entries[0].get("path"), str):
            try:
                parsed = json.loads(Path(receipt_entries[0]["path"]).read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    receipt_payload = parsed
                else:
                    errors.append("operation verification receipt must be a JSON object")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                errors.append("operation verification receipt is unreadable or invalid JSON")
        if receipt_payload is not None:
            if receipt_payload.get("schema_version") != 2:
                errors.append("operation verification receipt must use closure schema_version=2")
            raw_closure_id = raw_metadata.get("operation_closure_id") if isinstance(raw_metadata, dict) else None
            if not raw_closure_id or raw_metadata.get("operation_closure_owner") != "record_operation_completion":
                errors.append("operation raw_results must be owned by record_operation_completion")
            if receipt_payload.get("closure_id") != raw_closure_id:
                errors.append("operation verification receipt closure_id must bind raw_results ownership")
            if receipt_payload.get("record_kind") != "operation":
                errors.append("operation verification receipt.record_kind must be operation")
            if receipt_payload.get("task_kind") not in {"python_install", "build", "file_delivery", "external_job", "generic"}:
                errors.append("operation verification receipt.task_kind is invalid")
            if not isinstance(receipt_payload.get("objective"), str) or not receipt_payload["objective"].strip():
                errors.append("operation verification receipt.objective must be non-empty")
            # 判决拆除跨批依赖 §3（2026-08-31）：partial = 「宣称 success 但验证
            # 未通过」的机械降格结果，收据词表随之扩展。
            if receipt_payload.get("outcome") not in {"success", "failed", "blocked", "partial"}:
                errors.append("operation verification receipt.outcome is invalid")
            receipt_checks = receipt_payload.get("checks")
            if not isinstance(receipt_checks, list) or not receipt_checks:
                errors.append("operation verification receipt.checks must be a non-empty list")
            else:
                malformed_checks = [
                    str(index) for index, check in enumerate(receipt_checks)
                    if not isinstance(check, dict)
                    or not isinstance(check.get("name"), str) or not check["name"].strip()
                    or not isinstance(check.get("passed"), bool)
                ]
                if malformed_checks:
                    errors.append("operation verification receipt.checks contains malformed entries: " + ", ".join(malformed_checks))
                failed_receipt_checks = [
                    check for check in receipt_checks
                    if isinstance(check, dict) and check.get("passed") is False
                ]
                if receipt_payload.get("outcome") == "success" and failed_receipt_checks:
                    errors.append("successful operation verification receipt cannot contain failed checks")
                if receipt_payload.get("outcome") == "partial" and not failed_receipt_checks:
                    errors.append("partial operation verification receipt requires the failed checks that demoted it")
                # 判决拆除（oc:176 同规则抄件，2026-08-31）：blocked/failed 的
                # 「必须有失败证据 + next_step 字数」子句随工具侧一并删 ——
                # 一个问题一个真相源；收据如实携带 agent 给出的内容。

    if isinstance(clean_record, dict):
        clean_metadata = clean_record.get("metadata") or {}
        if (not isinstance(clean_metadata, dict)
                or clean_metadata.get("operation_closure_owner") != "record_operation_completion"
                or clean_metadata.get("operation_closure_id") != (raw_metadata.get("operation_closure_id") if isinstance(raw_metadata, dict) else None)):
            errors.append("operation clean_results must share record_operation_completion closure ownership with raw_results")
        if not isinstance(clean_metadata, dict) or not clean_metadata.get("frozen"):
            errors.append("operation clean_results is not frozen")
        errors.extend(_validate_clean_results_payload(state, clean_record))
        payload, payload_errors = _clean_results_payload(clean_record)
        errors.extend(payload_errors)
        if payload is not None:
            if payload.get("record_kind") != "operation":
                errors.append("operation clean_results.record_kind must be operation")
            if payload.get("status") not in {"completed", "blocked", "failed", "partial"}:
                errors.append("operation clean_results.status must be completed, partial, blocked, or failed")
            if payload.get("raw_results_artifact_id") != raw_id:
                errors.append("operation clean_results must bind the unique current-run raw_results")
            if receipt_payload is not None:
                expected_status = {"success": "completed", "blocked": "blocked", "failed": "failed",
                                   "partial": "partial"}.get(
                    receipt_payload.get("outcome")
                )
                for field in ("task_kind", "objective", "outcome"):
                    if payload.get(field) != receipt_payload.get(field):
                        errors.append(f"operation clean_results.{field} must match the verification receipt")
                if payload.get("status") != expected_status:
                    errors.append("operation clean_results.status must match the verification receipt outcome")
                if verification := payload.get("verification"):
                    if verification.get("passed") is not (receipt_payload.get("outcome") == "success"):
                        errors.append("operation clean_results.verification.passed must match the verification receipt outcome")
            verification = payload.get("verification")
            if not isinstance(verification, dict) or not isinstance(verification.get("passed"), bool):
                errors.append("operation clean_results.verification.passed must be boolean")
            else:
                checks = verification.get("checks")
                if not isinstance(checks, list) or not checks:
                    errors.append("operation clean_results.verification.checks must be a non-empty list")
                elif receipt_payload is not None and checks != receipt_payload.get("checks"):
                    errors.append("operation clean_results.verification.checks must match the verification receipt")
                if payload.get("status") == "completed" and verification.get("passed") is not True:
                    errors.append("completed operation clean_results requires verification.passed=true")
                if payload.get("status") in {"blocked", "failed"} and verification.get("passed") is not False:
                    errors.append("blocked or failed operation clean_results requires verification.passed=false")
    return {
        "passed": not errors,
        "reason": "frozen current-run operation raw_results and clean_results are bound" if not errors else "; ".join(errors),
        "artifacts": details,
        "errors": errors,
        "closure_id": (raw_metadata.get("operation_closure_id") if isinstance(raw_metadata, dict) else None),
    }


def _operation_clean_results_freeze_errors(state: Any, record: dict[str, Any]) -> list[str]:
    """Reject an operation clean result that is not bound to frozen current-run raw evidence."""
    contract = load_run_contract(state)
    if contract.get("execution_mode") != "operational":
        return []
    payload, errors = _clean_results_payload(record)
    if payload is None:
        return errors
    if payload.get("record_kind") != "operation":
        errors.append("operation clean_results.record_kind must be operation")
    # partial = 「宣称 success 但验证未通过」的机械降格结果（判决拆除跨批依赖 §3）。
    if payload.get("status") not in {"completed", "blocked", "failed", "partial"}:
        errors.append("operation clean_results.status must be completed, partial, blocked, or failed")
    verification = payload.get("verification")
    if not isinstance(verification, dict) or not isinstance(verification.get("passed"), bool):
        errors.append("operation clean_results.verification.passed must be boolean")
    elif not isinstance(verification.get("checks"), list) or not verification.get("checks"):
        errors.append("operation clean_results.verification.checks must be a non-empty list")

    raw_summaries, _ = active_closure_artifacts(state, "raw_results")
    if len(raw_summaries) != 1:
        errors.append("operation clean_results requires exactly one current-run raw_results")
        return errors
    raw_id = str(raw_summaries[0].get("id") or "")
    raw_record = state.read_artifact(raw_id) if raw_id else None
    raw_metadata = (raw_record or {}).get("metadata") or {}
    if not isinstance(raw_metadata, dict) or not raw_metadata.get("frozen"):
        errors.append("operation clean_results requires frozen current-run raw_results")
    elif isinstance(raw_record, dict):
        _, raw_errors = _validate_raw_results_manifest(raw_record, verify_files=True)
        errors.extend(raw_errors)
    if payload.get("raw_results_artifact_id") != raw_id:
        errors.append("operation clean_results must bind the unique current-run raw_results")
    return errors


def _operation_goal_effect_audit(
    state: Any,
    *,
    intent_binding: dict[str, Any],
) -> dict[str, Any]:
    """Check that a new operation receipt cannot stand in for a research goal."""
    artifact_types = ("raw_results", "clean_results", "experiment_log")
    artifacts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    new_receipt_seen = False
    for artifact_type in artifact_types:
        records, _ = active_closure_artifacts(state, artifact_type)
        if len(records) != 1:
            artifacts[artifact_type] = {"count": len(records), "status": "unavailable"}
            continue
        artifact_id = str(records[0].get("id") or "")
        record = state.read_artifact(artifact_id) if artifact_id else None
        metadata = (record or {}).get("metadata") or {}
        if not isinstance(metadata, dict):
            errors.append(f"operation {artifact_type} metadata is invalid")
            artifacts[artifact_type] = {"artifact_id": artifact_id, "status": "invalid"}
            continue
        effect = metadata.get("upstream_goal_effect")
        scientific_contribution = metadata.get("scientific_contribution")
        child_obligation_version = metadata.get(
            "operation_child_obligation_version"
        )
        stored_binding = metadata.get("execution_intent_binding")
        is_new = effect is not None or stored_binding is not None
        new_receipt_seen = new_receipt_seen or is_new
        artifacts[artifact_type] = {
            "artifact_id": artifact_id,
            "upstream_goal_effect": effect,
            "scientific_contribution": scientific_contribution,
            "child_obligation_version": child_obligation_version,
            "binding_status": (stored_binding or {}).get("status")
            if isinstance(stored_binding, dict) else None,
        }
        if not is_new:
            continue
        if effect != "operational_subtask_only":
            errors.append(
                f"operation {artifact_type} must declare upstream_goal_effect=operational_subtask_only"
            )
        if child_obligation_version is not None and (
            child_obligation_version != 1 or scientific_contribution != "none"
        ):
            errors.append(
                f"operation {artifact_type} child obligation must declare "
                "scientific_contribution=none"
            )
        if not isinstance(stored_binding, dict):
            errors.append(f"operation {artifact_type} lacks execution_intent_binding receipt")
            continue
        if stored_binding.get("status") != intent_binding.get("status"):
            errors.append(f"operation {artifact_type} intent binding status differs from current run")
        if intent_binding.get("status") == "bound":
            if stored_binding.get("intent_digest") != intent_binding.get("intent_digest"):
                errors.append(f"operation {artifact_type} intent digest differs from current run")
            if stored_binding.get("prereg_binding") != intent_binding.get("prereg_binding"):
                errors.append(f"operation {artifact_type} prereg binding differs from current run")
    return {
        "passed": not errors,
        "legacy": not new_receipt_seen,
        "artifacts": artifacts,
        "errors": errors,
        "reason": (
            "operation receipt is explicitly limited to its operational subtask"
            if not errors else "; ".join(errors)
        ),
    }


def audit_operation_log_contract(
    state: Any, *, require_frozen: bool = True, expected_artifact_id: str | None = None,
) -> dict[str, Any]:
    """Audit the complete non-scientific evidence triplet from the immutable run contract."""
    contract = load_run_contract(state)
    operational = contract.get("execution_mode") == "operational"
    try:
        intent_binding = audit_execution_intent_binding(state, require=False)
    except Exception as exc:
        intent_binding = {
            "passed": False,
            "status": "binding_audit_error",
            "reason": f"execution intent binding audit failed: {type(exc).__name__}",
        }
    intent_binding_allowed = (
        intent_binding.get("passed", False)
        and intent_binding.get("status") in {"bound", "legacy_unverifiable"}
    )
    records, superseded_ids = active_experiment_logs(state)
    if len(records) != 1:
        return {
            "passed": False, "execution_mode": contract.get("execution_mode"),
            "intent_binding": intent_binding,
            "reason": ("operation requires exactly one experiment_log；误建的多余草稿"
                       "可用 supersede_closure_draft(artifact_id, reason) 追加否定记录"
                       "（frozen log 不可否定）"),
            "log_count": len(records),
            "superseded_ids": superseded_ids,
        }
    artifact_id = str(records[0].get("id") or "")
    if expected_artifact_id and artifact_id != expected_artifact_id:
        return {
            "passed": False, "artifact_id": artifact_id,
            "intent_binding": intent_binding,
            "reason": "only the unique canonical operation experiment_log may be frozen",
        }
    record = state.read_artifact(artifact_id) if artifact_id else None
    metadata = (record or {}).get("metadata") or {}
    content = str((record or {}).get("content") or "")
    frozen = bool(metadata.get("frozen")) if isinstance(metadata, dict) else False
    auto_generated = bool(metadata.get("auto_generated")) if isinstance(metadata, dict) else False
    has_execution = bool(re.search(r"(?i)(?:command|命令|execution|执行)", content))
    has_verification = bool(re.search(r"(?i)(?:verification|验证|check|检查)", content))
    registered_blockers = list(state.hook_state.get("blockers") or []) if hasattr(state, "hook_state") else []
    declared_blocked = (
        str(metadata.get("status") or "").strip().lower() in {"blocked", "incomplete"}
        if isinstance(metadata, dict) else False
    )
    documents_blocker = bool(re.search(r"(?im)^#{1,6}\s*(?:blocker|受阻|阻塞)", content))
    # 判决拆除（oc:178 删的同规则抄件，2026-08-31）：outcome=blocked 的收据本身
    # 就是 blocker 声明 —— 不再要求先在 hook_state 登记 blocker 才许关闭；
    # 登记数照实报告（n_registered_blockers）供 orchestrator 消费。
    blocked_closure = declared_blocked or documents_blocker
    result_bundle = _operation_result_bundle(state)
    goal_effect = _operation_goal_effect_audit(state, intent_binding=intent_binding)
    if (not isinstance(metadata, dict)
            or metadata.get("operation_closure_owner") != "record_operation_completion"
            or metadata.get("operation_closure_id") != result_bundle.get("closure_id")):
        result_bundle.setdefault("errors", []).append("operation experiment_log must share record_operation_completion closure ownership")
        result_bundle["passed"] = False
    try:
        try:
            from .operation_completion import audit_operation_route_alignment
        except ImportError:
            from tools.operation_completion import audit_operation_route_alignment
        route_alignment = audit_operation_route_alignment(state)
    except Exception as exc:
        route_alignment = {
            "passed": False,
            "applicable": True,
            "reason": f"operation execution route audit failed: {type(exc).__name__}: {exc}",
        }
    passed = (
        operational and (frozen or not require_frozen) and not auto_generated
        and has_execution and has_verification and result_bundle.get("passed", False)
        # 本分支新增的三条（merge-base 与 origin/main 都没有，判决从未覆盖）：
        # 它们是布尔条件不是 return-error 站点，不计入拒绝点扫描，不在拆除范围。
        and route_alignment.get("passed", False)
        and intent_binding_allowed
        and goal_effect.get("passed", False)
        # (not declared_blocked or blocked_closure) 随 origin/main 拆除（oc:178
        # 同族：收据 outcome=blocked 本身就是 blocker 声明），不再进 passed。
    )
    detail_errors = list(result_bundle.get("errors") or [])
    if not route_alignment.get("passed"):
        detail_errors.append(str(route_alignment.get("reason") or "execution route audit failed"))
    if not intent_binding_allowed:
        detail_errors.append(str(intent_binding.get("reason") or "execution intent binding audit failed"))
    if not goal_effect.get("passed", False):
        detail_errors.append(str(goal_effect.get("reason") or "operation goal-effect audit failed"))
    if passed and blocked_closure:
        # oc:178 删随 origin/main 合入：措辞不再宣称已拆的 "registered blocker" 要求。
        reason = ("operation closed as blocked with frozen raw evidence and normalized "
                  "verification; the receipt itself is the blocker declaration")
    elif passed and intent_binding.get("status") == "legacy_unverifiable":
        reason = (
            "historical operation evidence is reconciled; its upstream intent is "
            "legacy-unverifiable and never authorizes new execution"
        )
    elif passed:
        reason = "operation evidence triplet is frozen, bound to this run, and verifiable"
    else:
        reason = (
            "operation requires one canonical agent-authored experiment_log with execution and verification, "
            "plus one frozen current-run raw_results and one frozen current-run clean_results bound together"
        )
        if detail_errors:
            reason += "; " + "; ".join(str(error) for error in detail_errors)
    return {
        "passed": passed, "artifact_id": artifact_id, "execution_mode": contract.get("execution_mode"),
        "frozen": frozen, "closure_recorded": frozen, "auto_generated": auto_generated,
        "has_execution": has_execution, "has_verification": has_verification,
        "blocked_closure": blocked_closure, "n_registered_blockers": len(registered_blockers),
        "execution_route": route_alignment,
        "result_evidence": result_bundle,
        "intent_binding": intent_binding,
        "upstream_goal_effect": goal_effect,
        "reason": reason,
    }
def audit_experiment_contract(state: Any) -> dict[str, dict[str, Any]]:
    contract = load_run_contract(state)
    require_intent = (
        contract.get("execution_mode") == "scientific"
        or bool(execution_intent_snapshot(state).get("available"))
    )
    log_integrity = audit_experiment_log_integrity(state)
    if contract.get("execution_mode") != "operational":
        log_integrity = _experiment_log_integrity_with_correction_disclosure(
            state, log_integrity)
    return {
        "verdict": audit_verdict(state),
        "sediment": audit_sediment(state),
        "execution_record": audit_execution_record(state),
        "experiment_log_integrity": log_integrity,
        "citation_binding": audit_citation_binding(state),
        "result_evidence": audit_result_evidence(state),
        "terminal_failure_record": audit_terminal_failure_record(state),
        "execution_intent_binding": audit_execution_intent_binding(
            state, require=require_intent
        ),
        "scientific_question_closure": audit_scientific_question_closure(state),
        "prereg_assignment": audit_prereg_assignment(state),
        "data_provenance": audit_data_provenance(state),
        "job_submission_records_readable": (
            audit_job_submission_records_readable(state)
        ),
    }


# ── 冻结门：experiment 三类证据件的门禁，声明给唯一的 freeze_artifact ───────
# 这里原来是三个各自包一层的 freeze_* 工具（同一个动作三个名字）。门禁逻辑
# 逐字保留，只是从"工具"变成"类型的性质"：freeze_artifact 冻结前自动执行。
from shared.tools.library.artifacts_extra import register_freeze_gate as _register_freeze_gate


def _experiment_log_correction_disclosure_failures(
    state: Any, record: dict[str, Any],
) -> dict[str, str]:
    """Validate a scientific log's projection of the route-owned receipt."""
    try:
        try:
            from .execution_route import route_correction_witness_disclosure
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.execution_route import route_correction_witness_disclosure
        disclosure = route_correction_witness_disclosure(state)
        expected_limitations = disclosure["limitations"]
        expected_check = disclosure["check"]
    except Exception as exc:
        return {
            "route_correction_witness_limitations": (
                "cannot derive the authoritative validated recovery receipt "
                f"projection: {type(exc).__name__}: {exc}"
            )
        }

    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    observed_limitations = metadata.get(
        "route_correction_witness_limitations", [])
    observed_check = metadata.get(
        "route_correction_witness_limitation_check")
    failures: dict[str, str] = {}
    if observed_limitations != expected_limitations:
        failures["route_correction_witness_limitations"] = (
            "metadata.route_correction_witness_limitations must exactly project "
            "the authoritative validated recovery receipt; expected="
            + json.dumps(expected_limitations, ensure_ascii=False, sort_keys=True)
        )
    if observed_check != expected_check:
        failures["route_correction_witness_limitation_check"] = (
            "metadata.route_correction_witness_limitation_check must be the "
            "framework-derived named check; expected="
            + json.dumps(expected_check, ensure_ascii=False, sort_keys=True)
        )
    if expected_check is not None:
        exact_human_projection = json.dumps(
            expected_check,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if exact_human_projection not in str(record.get("content") or ""):
            failures["route_correction_witness_human_disclosure"] = (
                "experiment_log content must visibly include this exact compact "
                "JSON named check: " + exact_human_projection
            )
    return failures


def _experiment_log_integrity_with_correction_disclosure(
    state: Any,
    integrity: dict[str, Any],
) -> dict[str, Any]:
    """Keep the canonical-log audit aligned with the current correction head."""
    if not integrity.get("passed"):
        return integrity
    record = _latest_log(state)
    if not isinstance(record, dict):
        return {
            **integrity,
            "passed": False,
            "reason": "canonical experiment_log is unreadable",
        }
    failures = _experiment_log_correction_disclosure_failures(state, record)
    if not failures:
        return integrity
    return {
        **integrity,
        "passed": False,
        "correction_disclosure_passed": False,
        "correction_disclosure_failures": failures,
        "reason": (
            "canonical experiment_log no longer exactly projects the active "
            "route correction witness limitations"
        ),
    }


def _experiment_log_freeze_gate(state, artifact_id, record):
    contract = load_run_contract(state)
    if contract.get("execution_mode") == "operational":
        operation = audit_operation_log_contract(
            state, require_frozen=False, expected_artifact_id=artifact_id,
        )
        failures = {} if operation.get("passed") else {
            "operation_log_contract": operation.get("reason", "invalid operation receipt"),
        }
        state.append_transcript(
            "experiment_pre_freeze_gate", artifact_id=artifact_id, scope="operation",
            passed=not failures, failed_checks=list(failures), reasons=failures,
        )
        return {
            "failures": failures,
            "hint": "For operation: freeze immutable raw execution evidence first, then a bound clean_results verification with record_kind=operation, then this one canonical experiment_log.",
        }

    # Framework-written terminal records are status pointers, not scientific
    # evidence. They stay freezeable for diagnosis, but never satisfy scientific
    # verdict or sediment audits.  Their caller-writable marker must not bypass
    # the route-owned correction limitation projection.
    md = record.get("metadata") or {}
    if md.get("auto_generated") and md.get("terminal_failure_record"):
        disclosure_failures = _experiment_log_correction_disclosure_failures(
            state, record)
        return {
            "failures": disclosure_failures,
            "hint": (
                "Framework terminal records remain diagnostic pointers, but "
                "must project any authoritative route-correction limitations."
            ),
        }
    audit = audit_experiment_contract(state)
    integrity = audit["experiment_log_integrity"]
    disclosure_failures = integrity.get(
        "correction_disclosure_failures", {})
    # execution_intent_binding 按**成因**分流（2026-09-03）：
    #
    # freeze 是**不可逆的落盘**，而上游意图能否核验取决于调用方派发时给没给
    # node_inputs —— 本 run 改不了别人给自己的输入（墙自己的文案就这么写的）。
    # 把它挂在 freeze 上，等于让一次别人造成的缺失把已经干完的活永久挡在
    # canonical 收据之外；框架层 tests/ 也证实它越界拦住了别的节点：
    # test_sediment_findings_reach_the_kb（「搬运不许影响 freeze」）、
    # test_freeze_actually_hands_back_a_chunk_id（「登记炸了不许把 freeze 判失败」）、
    # test_artifact_policy —— 这些调用根本不属于 experiment 的执行分类范畴。
    #
    # 但**漂移**不是缺席：上游意图在 run 中途被换掉（把声明的原生求解器换成
    # proxy、把绑定的 prereg 换成另一份）之后还想把 log 冻成原任务的证据，
    # 那是账本伪造，出路也在本 run 手里（改回去，或按新目标重新分类）。
    # 所以只对「缺席/不可核验」降格，「变更」保持阻断。
    _DRIFT = {"intent_changed", "prereg_binding_changed", "prereg_binding_invalid"}
    _intent = audit.get("execution_intent_binding") or {}
    _intent_ok = bool(_intent.get("passed", False))
    _intent_drifted = (not _intent_ok) and str(_intent.get("status") or "") in _DRIFT
    failures = {
        name: item.get("reason", "pre-freeze evidence gate failed")
        for name, item in audit.items()
        if name in {"verdict", "sediment", "citation_binding"} and not item.get("passed", False)
    }
    failures.update(disclosure_failures)
    if _intent_drifted:
        failures["execution_intent_binding"] = _intent.get(
            "reason", "upstream execution intent changed after classification")
    elif not _intent_ok:
        try:
            state.append_transcript(
                "experiment_log_frozen_with_unverified_intent",
                artifact_id=artifact_id,
                reason=_intent.get("reason") or "upstream intent binding not verifiable",
                status=_intent.get("status"),
            )
        except Exception:
            pass
    provenance = audit["data_provenance"]
    if not provenance.get("passed", False):
        failures["data_provenance"] = provenance.get(
            "reason", "data provenance audit failed",
        )
    canonical_id = str(integrity.get("canonical_log_id") or "")
    if not integrity.get("passed") or artifact_id != canonical_id:
        failures["experiment_log_integrity"] = integrity.get("reason", "invalid experiment_log")
    state.append_transcript(
        "experiment_pre_freeze_gate", artifact_id=artifact_id, scope="scientific",
        passed=not failures, failed_checks=list(failures), reasons=failures,
    )
    return {
        "failures": failures,
        "hint": (
            "Complete the draft evidence gates, freeze this canonical log, then create "
            "the execution record and finalize external jobs before final closure. "
            "Never create a replacement log."
        ),
    }


def _clean_results_freeze_gate(state, artifact_id, record):
    errors = _validate_clean_results_payload(state, record)
    errors.extend(_operation_clean_results_freeze_errors(state, record))
    return {
        "failures": ({"clean_results_schema": "; ".join(errors)} if errors else {}),
        "errors": errors,
        "hint": "按报错逐字段补齐 clean_results 内容契约后重新 freeze。",
    }


def _raw_results_freeze_gate(state, artifact_id, record):
    _, errors = _validate_raw_results_manifest(record, verify_files=True)
    return {
        "failures": ({"raw_results_manifest": "; ".join(errors)} if errors else {}),
        "errors": errors,
        "hint": "manifest 声明的字节必须与留存文件逐一相符（raw_results.files）。",
    }


# 契约跟着门一起注册：这两个 dict 既是校验器的拒绝措辞（contract_requirement），
# 也是模型在 freeze_artifact 说明里读到的要求 —— 仍然是一份声明两个消费者。
_register_freeze_gate("experiment_log", _experiment_log_freeze_gate)
_register_freeze_gate("clean_results", _clean_results_freeze_gate, CLEAN_RESULTS_CONTRACT)
_register_freeze_gate("raw_results", _raw_results_freeze_gate, RAW_RESULTS_CONTRACT)
