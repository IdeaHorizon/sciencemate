"""Experiment sediment closure tools.

These tools keep the experiment-specific closure contract out of the shared KB
schema while preserving a recoverable, auditable explicit-none path after an
experiment log has been frozen.
"""
from __future__ import annotations

from typing import Any

from core.prereg_commitments import frozen_commitments, status_flip_block
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.kb_schema import SchemaValidationError, validate_claim, validate_status_flip

try:
    from .contract_audit import (
        _verdict_labels,
        active_experiment_logs,
        audit_experiment_contract,
        current_run_artifacts,
        job_submission_records_readability_failure,
        terminal_closure_projection,
    )
except ImportError:  # loaded as top-level ``tools.sediment`` by node runtime
    from tools.contract_audit import (
        _verdict_labels,
        active_experiment_logs,
        audit_experiment_contract,
        current_run_artifacts,
        job_submission_records_readability_failure,
        terminal_closure_projection,
    )


_FUTURE_LOG_CHUNK = "chunk_000000000000"
_MIN_REASON_CHARS = 20


def _latest_log(state: Any) -> dict[str, Any] | None:
    # 与 contract_audit 同一份过滤视图：被 supersede_closure_draft 否定的
    # 草稿不算最新 log，声明必须绑定到 canonical 那份。
    records, _ = active_experiment_logs(state)
    if not records:
        return None
    artifact_id = records[-1]["id"]
    record = state.read_artifact(artifact_id)
    if record is None:
        return None
    return {**record, "id": artifact_id}


def _bind_declared_log(state: Any, latest: dict[str, Any], experiment_log_id: str | None,
                       closure_witness: dict[str, Any]) -> dict[str, Any]:
    """Resolve which experiment_log a declaration binds to.

    判决拆除·第三波（sed:161/648 降格，2026-09-02）：以前 `experiment_log_id`
    必须等于「当前最新」，给了真实但非最新的 id 就拒——那是「顺序不对」。现在
    挂到点名的那份 log 上并见证 ``not_latest``；点名的 id 不是本 run 的
    experiment_log 时声明照记，见证 ``experiment_log_not_found``（不新造拒绝）。
    """
    declared = str(experiment_log_id or "").strip()
    latest_id = str(latest.get("id") or "")
    if not declared or declared == latest_id:
        return latest
    if any(str(row.get("id")) == declared for row in current_run_artifacts(state, "experiment_log")):
        named = state.read_artifact(declared)
        if named is not None:
            closure_witness["not_latest"] = {
                "declared_experiment_log_id": declared,
                "latest_experiment_log_id": latest_id or None,
            }
            return {**named, "id": declared}
    closure_witness["experiment_log_not_found"] = {
        "declared_experiment_log_id": declared,
        "latest_experiment_log_id": latest_id or None,
    }
    return {}


def _candidate_record(candidate: dict[str, Any], *, reserve_log_chunk: bool) -> dict[str, Any]:
    """Build the same schema-shaped record that create_claim will persist.

    The future experiment-log chunk is intentionally only syntactic here: its
    existence can be checked only after freeze/register, and is reported as an
    unverified item rather than being silently assumed true.
    """
    record = dict(candidate)
    record.setdefault("claim_text", "")
    record.setdefault("claim_type", "")
    record.setdefault("concept_ids", [])
    record.setdefault("sources", [])
    record.setdefault("confidence", 0.5)
    record.setdefault("replication_count", 0)
    record.setdefault("scope_dimensions", {})
    record.setdefault("created_by_role", "agent_auto")
    sources = list(record.get("sources") or [])
    if reserve_log_chunk and _FUTURE_LOG_CHUNK not in sources:
        sources.append(_FUTURE_LOG_CHUNK)
    record["sources"] = sources
    return record


async def _assess_sediment_candidate(
    state: Any,
    candidate: dict[str, Any],
    reserve_experiment_log_chunk: bool = True,
    **_: Any,
) -> dict:
    """Validate prospective sediment with the exact shared schema validator."""
    # 判决拆除·第三波（sed:70 → schema，2026-09-02）：candidate 的 object 类型在
    # schema 里；非 dict 时 dict() 自己抛，派发口按 schema 做形状诊断。
    record = _candidate_record(
        candidate, reserve_log_chunk=bool(reserve_experiment_log_chunk))
    claim_type = str(record.get("claim_type") or "")
    unverified = (["future_experiment_log_chunk_existence"]
                  if reserve_experiment_log_chunk else [])

    # 节点自己的证据判据（2026-08-21）。
    #
    # 原来这里完全转发给 validate_claim，靠 KB schema 的 HIGH_TIER 闸
    # （independent_source_count ≥ 2）兜住"没有证据的候选"。那道闸已删 ——
    # 它教模型改 claim_type 过门，而类型是晋升分道的路由键。
    #
    # 但更根本的是：借来的闸答的是**另一个问题**。节点想问"这条有没有证据"，
    # 闸答的是"独立来源够不够两个"。两个问题一条规则，删掉闸之后节点就裸了。
    #
    # 正确的判据节点自己有：预留的 log chunk 是**承诺**（freeze/register 之后
    # 才存在），不是证据。只有它一个来源 = 这条 sediment 目前零证据。
    real_sources = [s for s in (record.get("sources") or [])
                    if s != _FUTURE_LOG_CHUNK]
    if not real_sources:
        return {
            "status": "success", "admissible": False,
            "claim_type": claim_type,
            "schema_error": (
                "sediment 候选没有任何真实来源：唯一的 "
                f"{_FUTURE_LOG_CHUNK} 是预留占位（freeze/register 之后才存在），"
                "是承诺不是证据。"),
            "closure_effect": "none",
            "unverified": unverified,
            "guidance": (
                "先把支撑这条结论的东西变成真实 chunk：用 freeze_artifact 冻结实验"
                "日志，返回里带 chunk_id，直接用它（没有单独的登记工具）；"
                "或引用文献锚点。然后再回来 assess。"
                "确实没有可沉淀内容时，在 freeze 前 declare_no_sediment"
                "（它要求 experiment_log 已 save_artifact 保存）。"),
        }

    try:
        validate_claim(record)
    except SchemaValidationError as exc:
        return {
            "status": "success", "admissible": False,
            "claim_type": claim_type, "schema_error": str(exc),
            "closure_effect": "none",
            "unverified": unverified,
            "guidance": (
                "该候选不能作为 sediment claim。empirical 可另存观察但不满足 "
                "experiment sediment closure；只有真实失败经验才可改写 dead_end，"
                # 指路必须把**那条路自己的前置条件**一起说了，否则模型照指引走过去
                # 会因为另一个原因再被拒一次 —— 指了路，却没说路口有闸。
                "否则在 freeze 前 declare_no_sediment（它要求 experiment_log 已 "
                "save_artifact 保存）。"),
        }
    return {
        "status": "success", "admissible": claim_type in {"methodological", "dead_end"},
        "claim_type": claim_type,
        "closure_effect": ("sediment_claim" if claim_type in {"methodological", "dead_end"}
                           else "none"),
        "unverified": unverified,
        "guidance": (
            "schema 准入通过；freeze/register 后仍须用真实 chunk_id 创建 claim。"
            if claim_type in {"methodological", "dead_end"}
            else "该类型可写入 KB，但不构成 experiment sediment closure。"),
    }


async def _declare_no_sediment(
    state: Any,
    reason: str,
    experiment_log_id: str | None = None,
    **_: Any,
) -> dict:
    """Record an explicit, agent-authored no-sediment conclusion.

    Before freeze the declaration is rendered into the log for downstream
    readers.  After freeze it remains an immutable transcript addendum bound
    to the frozen log; the contract audit recognizes that tool result.
    """
    reason = str(reason or "").strip()
    # 判决拆除（sed:151 删，2026-08-31）：字数闸且加在让步出口上（双重错误）；
    # reason 原样如实记录，空就记空。
    closure_witness: dict[str, Any] = {}
    record = _latest_log(state)
    if not record:
        # 判决拆除 O6（sed:156 降格）：没有 experiment_log 不再拒绝声明 ——
        # 声明照记进 transcript，log 缺席如实标注（closure_evidence_weak）。
        closure_witness["experiment_log_missing"] = True
        record = {}
    record = _bind_declared_log(state, record, experiment_log_id, closure_witness)
    artifact_id = str(record.get("id") or "")
    metadata = dict(record.get("metadata") or {})
    if metadata.get("auto_generated"):
        # 判决拆除 O6（sed:167 降格）：auto_generated 兜底 log 可机械标记 ——
        # 声明照记，弱证据如实标注，不再拒绝。
        closure_witness["experiment_log_auto_generated"] = True
    frozen = bool(metadata.get("frozen"))
    rendered_in_log = False
    if record and not frozen:
        content = str(record.get("content") or "").rstrip()
        declaration = (
            "## Sediment Closure\n"
            "本次未发现可进入 KB 的 methodological / dead_end finding，理由："
            f"{reason}\n")
        if declaration not in content:
            content = f"{content}\n\n{declaration}"
            state.save_artifact(
                "experiment_log", str(record.get("name") or "experiment_log"), content,
                metadata=metadata, provenance=record.get("provenance"))
        rendered_in_log = True
    state.append_transcript(
        "experiment_no_sediment_declared",
        experiment_log_id=artifact_id, reason=reason, log_frozen=frozen,
        rendered_in_log=rendered_in_log,
        **({"closure_witness": closure_witness} if closure_witness else {}),
    )
    return {
        "status": "success", "experiment_log_id": artifact_id or None,
        "log_frozen": frozen, "rendered_in_log": rendered_in_log,
        "reason": reason,
        **({"closure_witness": closure_witness} if closure_witness else {}),
    }


async def _preview_experiment_contract(state: Any, **_: Any) -> dict:
    """Read-only preview of closure, provenance, and pending external jobs."""
    audit = audit_experiment_contract(state)
    checks = dict(terminal_closure_projection(audit)["audit_checks"])
    provenance = checks["data_provenance"]
    stale = list(provenance.get("unverified") or [])
    try:
        from tools.resource_manager import (
            SubmissionLedgerError,
            current_run_owed_external_workflows,
        )
    except ImportError:
        from .resource_manager import (
            SubmissionLedgerError,
            current_run_owed_external_workflows,
        )
    ledger_key = "job_submission_records_readable"
    ledger_rejected = not checks[ledger_key].get("passed", False)
    waiting: list[dict[str, Any]] = []
    if not ledger_rejected:
        try:
            waiting = current_run_owed_external_workflows(state)
        except SubmissionLedgerError as exc:
            # The ledger may change between the terminal audit and the open-job
            # projection. Normalize that observed race through the same audit
            # authority instead of recreating a preview-only gate.
            checks[ledger_key] = job_submission_records_readability_failure(exc)
            ledger_rejected = True
    if ledger_rejected:
        waiting = []
    failed = [name for name, item in checks.items() if not item["passed"]]
    has_running = any(item.get("workflow_status") == "awaiting_external_job" for item in waiting)
    waiting_status = "awaiting_external_job" if has_running else "awaiting_analysis"
    overall = "incomplete" if failed else (waiting_status if waiting else "completed")
    return {
        "status": "success", "overall_status": overall, "failed_checks": failed,
        "checks": checks, "open_external_jobs": waiting,
        "experiment_workflow_status": (
            "awaiting_external_job" if ledger_rejected
            else waiting_status if waiting else "completed"
        ),
        "review_eligibility": overall == "completed",
        "unverified": stale,
    }

register_tool(
    ToolDefinition(
        name="assess_sediment_candidate",
        description=(
            "在 freeze experiment_log 前，使用 shared KB 的同一 schema 准入规则预检 "
            "methodological/dead_end sediment 候选。不会写 KB；未来日志 chunk 只作占位，"
            "返回的 unverified 会明确说明。empirical 合法但不计入 sediment closure。"),
        parameters_schema={
            "type": "object",
            "properties": {
                "candidate": {"type": "object", "description": "拟传给 create_claim 的字段"},
                "reserve_experiment_log_chunk": {"type": "boolean", "default": True},
            },
            "required": ["candidate"],
        },
        risk_level="low",
    ),
    _assess_sediment_candidate,
)

register_tool(
    ToolDefinition(
        name="declare_no_sediment",
        description=(
            "显式声明本次没有可进入 KB 的 methodological/dead_end finding。必须给具体理由。"
            # 前置条件写进说明，不要只在被拒时才说：声明是**渲染进 log 正文**的，
            # 所以 log 必须先存在。2026-08-18 实测这条撞了两次（两个 run 各一次），
            # 每次都是一个白烧的来回。
            "⚠️ 前置：必须先 `save_artifact(artifact_type='experiment_log', ...)`；"
            "本工具把声明渲染进那份 log。log 未冻结时写入 log 正文；已冻结时写可审计 "
            "transcript addendum。"),
        parameters_schema={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为何无可复用方法学/死路结论；原样如实记录"},
                "experiment_log_id": {"type": "string", "description": "可选；默认绑定当前最新 experiment_log。点名本 run 早先的 log 会挂到那份并见证 not_latest。"},
            },
            "required": ["reason"],
        },
        risk_level="low",
    ),
    _declare_no_sediment,
)

register_tool(
    ToolDefinition(
        name="preview_experiment_contract",
        description=(
            "最终回答前只读预览 experiment 的 verdict、sediment、execution、data provenance 与未完成 external job。"
            "复用终态 audit/provenance 判据，不另写判据；awaiting_external_job 时不得声称科学实验完成或进入 review。"),
        parameters_schema={"type": "object", "properties": {}},
        risk_level="low",
    ),
    _preview_experiment_contract,
)

# ── Hypothesis verdict closure ──────────────────────────────────────────────
# These tools deliberately live in the experiment node.  They consume existing
# frozen prereg/KB records but do not change shared KB semantics or core policy.

_MIN_VERDICT_REASON_CHARS = 20
_MIN_NEXT_STEP_CHARS = 10


def _latest_prereg(state: Any) -> dict[str, Any] | None:
    """Read the one exact frozen prereg selected by the run contract."""
    try:
        from .run_contract import load_bound_frozen_prereg
    except ImportError:  # pragma: no cover - standalone node bootstrap.
        from tools.run_contract import load_bound_frozen_prereg
    return load_bound_frozen_prereg(state)


def _hypothesis_sections(content: str) -> dict[str, str]:
    """预注册里可被结论关闭的条目段 —— `{Q1: 段落正文}`。

    v0.5：Analysis 的一等公民从"假设"改成"研究问题"，编号可能是 `Q1` 也可能
    是 legacy 的 `H1`。绑定必须跟着泛化，否则新格式协议下 experiment 永远
    resolve 不到任何条目，结论关不上（假设降级之后这里是唯一会断的绑定点）。

    解析权归 core.prereg_commitments 一处 —— 本文件不再自己写正则认标题，
    同一个问题只能有一个真相源。
    """
    from core.prereg_commitments import parse_questions, section_text_for

    return {
        qid: section_text_for(content or "", qid)
        for qid in parse_questions(content or "")
    }


def _claim_hypothesis_id(claim: dict[str, Any], sections: dict[str, str]) -> str | None:
    scope = claim.get("scope_dimensions") or {}
    # v0.5：两个键名都认 —— 新协议写 prereg_question_id，历史 claim 写
    # prereg_hypothesis_id。改名不能让存量 claim 突然绑不上。
    declared = str(
        scope.get("prereg_question_id")
        or scope.get("prereg_hypothesis_id")
        or ""
    ).strip().upper()
    if declared in sections:
        return declared
    criteria = str(claim.get("falsification_criteria_text") or "").strip()
    if criteria:
        matched = [hid for hid, section in sections.items() if criteria in section]
        if len(matched) == 1:
            return matched[0]
    return None


def _closure_item_payload(item: Any) -> dict[str, Any]:
    """Render one frozen closure item without asking the agent to re-parse prereg."""
    payload = {
        "id": item.key, "kind": item.kind, "description": item.describe(),
        "record_in": ("measured_metrics" if item.kind == "numeric"
                      else "closure_discharges"),
    }
    if item.kind == "numeric":
        payload.update({"metric": item.metric, "comparison": item.comparison,
                        "threshold": item.threshold})
    else:
        payload["statement"] = item.statement
    return payload


async def _resolve_prereg_questions(
    state: Any,
    pre_registration_id: str | None = None,
    **_: Any,
) -> dict:
    """Return every frozen research question and its evidence-recording contract.

    Unlike ``resolve_prereg_hypotheses``, this is also valid for exploratory,
    replication, and descriptive questions that deliberately have no hypothesis
    claim.  It is read-only and never turns an absent claim into an inconclusive
    scientific result.
    """
    prereg = _latest_prereg(state)
    if pre_registration_id and (not prereg or pre_registration_id != prereg.get("id")):
        return {"status": "success", "resolution_status": "declared_prereg_mismatch",
                "questions": [], "pre_registration_id": pre_registration_id,
                "guidance": "必须使用本 run contract 绑定的 frozen prereg，不能按调用参数改绑。"}
    if not prereg:
        return {"status": "success", "resolution_status": "missing_pre_registration",
                "questions": [],
                "guidance": "没有可读的 frozen pre_registration；不得把工程步骤包装成科学结论。"}

    from core.prereg_commitments import parse_questions

    questions = parse_questions(str(prereg.get("content") or ""))
    if not questions:
        return {"status": "success", "resolution_status": "no_research_questions",
                "pre_registration_id": prereg["id"], "questions": [],
                "guidance": "冻结 prereg 没有可解析的 Research Questions；请 Analysis 修复协议，experiment 不得猜测闭合条件。"}

    # A chunk is a prereg version snapshot.  The run contract, not the
    # current head or a uniqueness accident, decides which one this run uses.
    all_chunks = [chunk for chunk in state.list_kb("chunks")
                  if chunk.get("origin_artifact_id") == prereg["id"]
                  and chunk.get("origin_artifact_frozen")]
    try:
        from .run_contract import load_run_contract as _load_run_contract
    except ImportError:
        from tools.run_contract import load_run_contract as _load_run_contract
    contract = _load_run_contract(state)
    try:
        bound_version = int(contract.get("prereg_version") or 0) or None
    except (TypeError, ValueError):
        bound_version = None
    bound_hash = str(contract.get("prereg_content_hash") or "").strip()
    if bound_version is not None:
        chunks = [chunk for chunk in all_chunks
                  if int(chunk.get("origin_artifact_version") or 1) == bound_version]
        if bound_hash:
            chunks = [chunk for chunk in chunks
                      if str(chunk.get("origin_content_hash") or "") == bound_hash]
    else:
        chunks = [chunk for chunk in all_chunks
                  if not chunk.get("superseded_by_chunk_id")]
    prereg_chunk = chunks[0] if len(chunks) == 1 else None
    sections = _hypothesis_sections(str(prereg.get("content") or ""))
    candidates = []
    if prereg_chunk:
        candidates = [claim for claim in state.list_kb("claims")
                      if isinstance(claim, dict)
                      and claim.get("claim_type") == "hypothesis"
                      and claim.get("status") == "open"
                      and claim.get("prereg_chunk_id") == prereg_chunk.get("id")]
    claims_by_question: dict[str, list[dict[str, Any]]] = {}
    for claim in candidates:
        qid = _claim_hypothesis_id(claim, sections)
        if qid:
            claims_by_question.setdefault(qid, []).append(claim)

    rendered = []
    for qid in sorted(questions):
        question = questions[qid]
        matches = claims_by_question.get(qid, [])
        claim_resolution = (
            "not_applicable" if not question.is_hypothesis else
            ("bound" if len(matches) == 1 else
             ("ambiguous_claims" if len(matches) > 1 else "no_open_claim"))
        )
        rendered.append({
            "question_id": qid,
            "title": question.title,
            "output_kind": question.output_kind,
            "is_hypothesis": question.is_hypothesis,
            "proposition": question.proposition if question.is_hypothesis else None,
            "closure_items": [_closure_item_payload(item) for item in question.closure],
            "claim_resolution": claim_resolution,
            "claim_ids": [str(item.get("id") or "") for item in matches],
            "claim_id": str(matches[0].get("id") or "") if len(matches) == 1 else None,
        })
    return {
        "status": "success", "resolution_status": "resolved",
        "pre_registration_id": prereg["id"],
        "prereg_chunk_id": prereg_chunk.get("id") if prereg_chunk else None,
        "claim_resolution_available": bool(prereg_chunk),
        "questions": rendered,
        "guidance": (
            "逐项记录已完成或未完成的闭合条件：numeric 写 metadata.measured_metrics；"
            "statement 写 metadata.closure_discharges，status=discharged 时必须填真实 artifact_id 或 run_id 作为 evidence。"
            "无 proposition 的问题不需要 hypothesis claim；只有 proposition 问题的 bound claim 才可进入 assess_verdict_transition。"
        ),
    }


async def _resolve_prereg_hypotheses(
    state: Any,
    pre_registration_id: str | None = None,
    **_: Any,
) -> dict:
    """Resolve open hypothesis claims through frozen artifact provenance, not text search.

    This avoids the fragile/locale-dependent ``search_kb(query=...)`` path.  A
    future hypothesis node records ``scope_dimensions.prereg_hypothesis_id``;
    a legacy exact-criteria fallback is deliberately accepted only when unique.
    """
    prereg = _latest_prereg(state)
    if pre_registration_id and (not prereg or pre_registration_id != prereg.get("id")):
        return {"status": "success", "resolution_status": "declared_prereg_mismatch",
                "hypotheses": [], "pre_registration_id": pre_registration_id,
                "guidance": "verdict resolver must use the prereg selected by this run contract; do not override it per call."}
    if not prereg:
        return {"status": "success", "resolution_status": "missing_pre_registration", "hypotheses": [], "guidance": "没有可读 pre_registration；在 experiment_log 写 verdict: inconclusive，并说明需 hypothesis 节点补建预注册。"}
    if not (prereg.get("metadata") or {}).get("frozen"):
        return {"status": "success", "resolution_status": "unfrozen_pre_registration", "hypotheses": [], "pre_registration_id": prereg["id"], "guidance": "pre_registration 尚未冻结；不得翻 hypothesis status。"}

    # 版本原语（RFC 2026-08-18）：同一 prereg 身份每冻结一版就有一个 chunk，
    # 旧版 chunk 由框架自动打 superseded_by_chunk_id。绑定规则：
    #   contract 声明了绑定版本 → 精确取那一版的 chunk；
    #   否则 → 取未被取代的现行 chunk。
    # 修订史因此不再制造 "ambiguous_prereg_chunks"（#395-3 的 KB 侧病灶）。
    all_chunks = [chunk for chunk in state.list_kb("chunks")
                  if chunk.get("origin_artifact_id") == prereg["id"]
                  and chunk.get("origin_artifact_frozen")]
    bound_version = None
    try:
        from .run_contract import load_run_contract as _lrc
    except ImportError:
        from tools.run_contract import load_run_contract as _lrc
    try:
        bound_version = int(_lrc(state).get("prereg_version") or 0) or None
    except (TypeError, ValueError):
        bound_version = None
    if bound_version is not None:
        chunks = [chunk for chunk in all_chunks
                  if int(chunk.get("origin_artifact_version") or 1) == bound_version]
    else:
        chunks = [chunk for chunk in all_chunks
                  if not chunk.get("superseded_by_chunk_id")]
    if len(chunks) != 1:
        status = "missing_prereg_chunk" if not chunks else "ambiguous_prereg_chunks"
        return {"status": "success", "resolution_status": status, "pre_registration_id": prereg["id"], "chunk_ids": [item.get("id") for item in chunks], "hypotheses": [], "guidance": "没有唯一的冻结 prereg chunk，不能猜测 claim 对应关系；写 inconclusive 或请 hypothesis/_curator 修复登记。"}

    prereg_chunk = chunks[0]
    sections = _hypothesis_sections(str(prereg.get("content") or ""))
    candidates = [claim for claim in state.list_kb("claims")
                  if claim.get("claim_type") == "hypothesis"
                  and claim.get("status") == "open"
                  and claim.get("prereg_chunk_id") == prereg_chunk.get("id")]
    grouped: dict[str, list[dict[str, Any]]] = {}
    unbound: list[str] = []
    for claim in candidates:
        hypothesis_id = _claim_hypothesis_id(claim, sections)
        if hypothesis_id:
            grouped.setdefault(hypothesis_id, []).append(claim)
        else:
            unbound.append(str(claim.get("id") or ""))
    hypotheses: list[dict[str, Any]] = []
    for hypothesis_id in sorted(grouped):
        matches = grouped[hypothesis_id]
        hypotheses.append({
            "hypothesis_id": hypothesis_id,
            "claim_ids": [str(item.get("id") or "") for item in matches],
            "claim_id": str(matches[0].get("id") or "") if len(matches) == 1 else None,
            "resolution": "bound" if len(matches) == 1 else "ambiguous_claims",
            "commitment": frozen_commitments(state).get(hypothesis_id, {}),
        })
    status = "resolved" if hypotheses and not unbound and all(item["resolution"] == "bound" for item in hypotheses) else "partial_or_ambiguous"
    return {
        "status": "success", "resolution_status": status,
        "pre_registration_id": prereg["id"], "prereg_chunk_id": prereg_chunk.get("id"),
        "hypotheses": hypotheses, "unbound_claim_ids": unbound,
        "guidance": (
            "只对 resolution=bound 的条目使用 assess_verdict_transition；任何 ambiguous/unbound 条目都不得猜测翻转。"
            if status == "resolved" else
            "存在未绑定或歧义 claim；不得猜测翻转。对无法确定的结论用 declare_inconclusive_verdict。"),
    }


async def _assess_verdict_transition(
    state: Any,
    claim_id: str,
    hypothesis_id: str | None,
    new_status: str,
    reasoning: str,
    evidence_ids: list[str] | None = None,
    **_: Any,
) -> dict:
    """Preflight the same schema and prereg commitment gates as status update.

    A future frozen-log chunk is represented by a syntactically valid placeholder.
    Its existence is explicitly left unverified until freeze/register; all other
    rejection reasons are checked before the log becomes immutable.
    """
    claim = state.get_kb_record("claims", claim_id)
    if not claim:
        return {"status": "success", "admissible": False, "error": f"claim_id={claim_id!r} 不存在"}
    if claim.get("claim_type") != "hypothesis":
        return {"status": "success", "admissible": False, "error": "claim 不是 hypothesis，不能作为 hypothesis verdict closure"}
    if new_status not in {"validated", "refuted", "provisional"}:
        return {"status": "success", "admissible": False, "error": "new_status 必须是 validated/refuted/provisional"}
    try:
        validate_status_flip(claim, new_status, reasoning=reasoning, evidence_ids=evidence_ids or ([ _FUTURE_LOG_CHUNK ] if new_status in {"validated", "refuted"} else []))
    except SchemaValidationError as exc:
        return {"status": "success", "admissible": False, "error": str(exc), "unverified": []}
    blocker = status_flip_block(state, claim, hypothesis_id, new_status)
    if blocker:
        return {"status": "success", "admissible": False, "error": blocker, "unverified": []}
    return {
        "status": "success", "admissible": True, "claim_id": claim_id,
        "hypothesis_id": hypothesis_id, "new_status": new_status,
        "unverified": (["future_experiment_log_chunk_existence"] if new_status in {"validated", "refuted"} else []),
        "guidance": "freeze_artifact 后用它返回的真实 chunk_id（并可附 experiment_id）调用 update_claim_status；若结果或门禁变化，改用 declare_inconclusive_verdict，不得声称完成。",
    }


async def _declare_inconclusive_verdict(
    state: Any,
    reason: str,
    next_step: str,
    experiment_log_id: str | None = None,
    **_: Any,
) -> dict:
    """Record an explicit agent-authored inconclusive verdict, even after freeze."""
    reason, next_step = str(reason or "").strip(), str(next_step or "").strip()
    # 判决拆除（sed:636 删，2026-08-31）：字数闸加在让步出口上（双重错误）删；
    # reason/next_step 原样如实记录。
    closure_witness: dict[str, Any] = {}
    record = _latest_log(state)
    if not record:
        # 判决拆除 O6（sed:639 降格）：log 缺席不再拒绝声明；照记 + 弱证据标注。
        closure_witness["experiment_log_missing"] = True
        record = {}
    record = _bind_declared_log(state, record, experiment_log_id, closure_witness)
    artifact_id = str(record.get("id") or "")
    metadata = dict(record.get("metadata") or {})
    if metadata.get("auto_generated"):
        # 判决拆除 O6（sed:648 降格）：auto_generated 可机械标记；照记不拒。
        closure_witness["experiment_log_auto_generated"] = True
    content = str(record.get("content") or "")
    existing = _verdict_labels(content)
    if existing and existing[0] not in {"inconclusive"}:
        return {"status": "error", "error": "experiment_log 已声明非 inconclusive verdict；不得用声明工具制造矛盾。"}
    frozen = bool(metadata.get("frozen"))
    rendered = False
    if record and not frozen:
        addition = "## Hypothesis Verdict\nverdict: inconclusive\nreason: " + reason + "\nnext_step: " + next_step + "\n"
        if addition not in content:
            state.save_artifact("experiment_log", str(record.get("name") or "experiment_log"), content.rstrip() + "\n\n" + addition, metadata=metadata, provenance=record.get("provenance"))
        rendered = True
    state.append_transcript("experiment_inconclusive_verdict_declared", experiment_log_id=artifact_id, reason=reason, next_step=next_step, log_frozen=frozen, rendered_in_log=rendered,
                            **({"closure_witness": closure_witness} if closure_witness else {}))
    return {"status": "success", "experiment_log_id": artifact_id or None, "log_frozen": frozen, "rendered_in_log": rendered, "reason": reason, "next_step": next_step,
            **({"closure_witness": closure_witness} if closure_witness else {})}


register_tool(ToolDefinition(
    name="resolve_prereg_questions",
    description="读取 run contract 绑定的 frozen pre_registration，列出全部研究问题及其 numeric/statement 闭合条件和 experiment_log metadata 写法；无 proposition 的问题也适用，不写 KB。",
    parameters_schema={"type": "object", "properties": {"pre_registration_id": {"type": "string"}}},
    risk_level="low",
), _resolve_prereg_questions)

register_tool(ToolDefinition(
    name="resolve_prereg_hypotheses",
    description="通过 frozen pre_registration 的 KB provenance 精确定位本次 open hypothesis claim；不做文本关键词搜索，也不写 KB。遇到歧义会 fail-closed，不猜 claim。",
    parameters_schema={"type": "object", "properties": {"pre_registration_id": {"type": "string"}}},
    risk_level="low",
), _resolve_prereg_hypotheses)

register_tool(ToolDefinition(
    name="assess_verdict_transition",
    description="freeze experiment_log 前预检 hypothesis verdict 的 schema、reasoning、evidence 形状与冻结 prereg commitment。复用 shared validator/commitment gate；未来 log chunk 存在性会明确标为 unverified。",
    parameters_schema={"type": "object", "properties": {"claim_id": {"type": "string"}, "hypothesis_id": {"type": "string"}, "new_status": {"type": "string", "enum": ["validated", "refuted", "provisional"]}, "reasoning": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["claim_id", "new_status", "reasoning"]},
    risk_level="low",
), _assess_verdict_transition)

register_tool(ToolDefinition(
    name="declare_inconclusive_verdict",
    description="显式声明为何本次无法给 hypothesis 结论及下一步。log 未冻结时写进正文；冻结后写绑定该日志的 transcript addendum，供同一终态审计识别。不会翻 KB claim status。",
    parameters_schema={"type": "object", "properties": {"reason": {"type": "string"}, "next_step": {"type": "string"}, "experiment_log_id": {"type": "string"}}, "required": ["reason", "next_step"]},
    risk_level="low",
), _declare_inconclusive_verdict)
