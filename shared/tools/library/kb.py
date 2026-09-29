"""KB v3 工具集 —— 4 entity × 10 claim_type 统一 CRUD + 5 切片工具 + 治理。

替代 v2 的 19 个 KB 工具：
  v2 → v3 mapping：
    create_concept / create_chunk / create_experiment             保留（加 scope 参数）
    create_claim（claim_type=... 统一入口）  合并：create_claim 单工具
    create_question / create_opportunity / create_decision
    create_failure
    update_claim_status / update_hypothesis_verdict               合并：update_claim_status
    update_question_status / update_opportunity_status
    search_kb / kb_mode3_candidates                              保留（扩展 filter）
    kb_register_artifact_as_chunk                                已下架为内部函数
                                                                 （freeze_artifact 冻结时自动登记）
    kb_ingest                                                    保留

  新增工具：
    curator_scan(scan_type='org_promotion_candidates')
                                               project claim → org promotion 的唯一入口
                                               （机械车道直落、人批车道进组织的待审，
                                               见 core.kb_promotion.offer_to_the_organisation）
    find_concept_pair_gaps                     机械切片：找 concept_type 笛卡尔积里没 claim 连过的
    find_author_recent_unread                  机械切片：找某 person 最近未 ingest 的 chunk
    find_group_trajectory                      机械切片：列 group 时间轴
    find_high_dispute_claims                   机械切片：找争议区 claim
    find_stale_validated                       机械切片：找过期 validated claim
"""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.kb_schema import (
    ENTITIES, CLAIM_TYPES, CONCEPT_TYPES, SCOPES, CREATED_BY_ROLES,
    SchemaValidationError, smart_default_scope,
    is_external_uri, is_claim_id, now_iso,
    validate_status_flip,
)


# ─────────────────────────────────────────────────────────────────────────────
# create_concept
# ─────────────────────────────────────────────────────────────────────────────

async def _create_concept(
    state: State,
    canonical_name: str,
    concept_type: str,
    description: str,
    aliases: list[str] | None = None,
    attributes: dict | None = None,
    scope: str | None = None,
    created_by_role: str = "agent_auto",
    **_: Any,
) -> dict:
    try:
        rec = {
            "canonical_name": canonical_name,
            "concept_type": concept_type,
            "description": description,
            "aliases": aliases or [],
            "attributes": attributes or {},
            "created_by_role": created_by_role,
            "created_by_run_id": state.run_id,
            "created_by_node_type": state.node_type,
        }
        if scope is not None:
            rec["scope"] = scope
        record, created = state.write_kb("concepts", rec)
        return {
            "status": "success",
            "id": record["id"],
            "scope": record["scope"],
            "created": created,
        }
    except SchemaValidationError as e:
        return {"status": "error", "error": str(e)}


from core.kb_promotion import KNOWLEDGE_CARD_FIELDS as _CARD_FIELDS

register_tool(
    ToolDefinition(
        name="create_concept",
        description=(
            "在 KB 注册一个新的 concept（命名的研究'东西'：method/tool/dataset/...）。\n\n"
            "**Use when**：\n"
            "  - 写 claim 时发现引用的概念 KB 还没注册 → 先 search_kb 找；找不到再 create\n"
            "  - 论文中出现新方法 / 数据集 / 度量 / 应用领域\n"
            "  - 追踪新研究者 / 实验室时（type='person' 或 'group'）\n\n"
            "**Do NOT use when**：\n"
            "  - 想存事实陈述 → 用 create_claim\n"
            "  - 想存实验记录 → 用 create_experiment\n"
            "  - concept 已存在 → search_kb 先确认，内容寻址会自动 merge 同名\n\n"
            "**关键参数**：\n"
            "  - concept_type：必须 ∈ method / tool / dataset / phenomenon / theory / "
            "metric / domain / task / person / group\n"
            "  - canonical_name：normalize 后用作 id 来源；大小写/标点不影响\n"
            "  - attributes：dict，按 type 不同字段不同（如 person 有 affiliations，dataset 有 size/modality）\n"
            "  - scope：通常不传，框架按 concept_type 默认 org（全部 concept 都默认共享）；涉密时可 'project'\n\n"
            "**返回**：`{id, scope, created}`。created=False 表示同名 concept 已存在，aliases/attributes 自动 merge。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "canonical_name": {"type": "string",
                    "description": "主名。例 'QM9' / 'MACE-MP-0' / 'Andrej Karpathy'。"},
                "concept_type": {"type": "string", "enum": list(CONCEPT_TYPES),
                    "description": ("10 个类型之一。method=算法 / tool=软件 / dataset=数据集 / "
                                    "phenomenon=现象 / theory=理论 / metric=度量 / domain=领域 / "
                                    "task=任务形式 / person=研究者 / group=实验室")},
                "description": {"type": "string",
                    "description": "1-2 句说明这是啥。"},
                "aliases": {"type": "array", "items": {"type": "string"},
                    "description": "别名列表（例 ['GAP', 'Gaussian Approximation Potential']）。"},
                "attributes": {"type": "object",
                    "description": ("结构化属性 dict。按 concept_type 不同字段不同："
                                    "method.algorithm_family / tool.version / "
                                    "dataset.size+modality / person.affiliations+primary_areas+reputation_note(≤80字符) / "
                                    "group.group_type+location+key_members")},
                "scope": {"type": "string", "enum": ["project"],
                    "description": "不用传，只能是 'project'。org 层的词条是"
                                   "晋升闭包的依赖项，随被晋升的结论一起带过去，"
                                   "不独立注册。"},
                "created_by_role": {"type": "string", "enum": list(CREATED_BY_ROLES),
                    "default": "agent_auto",
                    "description": "trust calibration 标记。agent 直写=agent_auto；走 propose=agent_proposed_user_accepted；user chat 指令=user_directed"},
            },
            "required": ["canonical_name", "concept_type", "description"],
        },
        risk_level="low",
    ),
    _create_concept,
)


# ─────────────────────────────────────────────────────────────────────────────
# create_claim（统一入口，替代 5 个 v2 工具）
# ─────────────────────────────────────────────────────────────────────────────

async def _create_claim(state: State, claim_text: str, claim_type: str,
                        **fields: Any) -> dict:
    """工具面入口：委托给 `write_claim`，并把「谁在什么身份下写」如实入账。

    - org 没有出生通道：schema 里 scope enum=['project']，传 org 是枚举违约，
      派发口核一次（进 org 只有项目终态 curator 晋升那条路，不走 create_claim）。
    - 提案期假说：断言进 KB 的条件是**被采纳**（docs/v21 §7），承诺的正式落点
      是预注册。但「只许 _curator 写 hypothesis」是角色事前审批（S3）——
      写入照常，claim 上如实记 `adoption: proposed`（created_by_node_type 已
      入账），Analysis 收尾的机械投影（`project_research_state_to_kb`）仍是
      已裁决命题的权威来源。
    """
    if (claim_type == "hypothesis"
            and str(getattr(state, "node_type", "")) != "_curator"
            and fields.get("adoption") is None):
        fields["adoption"] = "proposed"
        state.append_transcript(
            "hypothesis_claim_written_before_adoption",
            node_type=state.node_type, hypothesis_id=fields.get("hypothesis_id"),
        )
    out = await write_claim(state, claim_text, claim_type, **fields)
    if out.get("status") == "success" and fields.get("adoption") == "proposed":
        out["adoption"] = "proposed"
        out["note"] = (
            "这是提案期假说，已如实记 adoption=proposed（不是已采纳的断言）。"
            "承诺的正式落点仍是预注册（未冻结改草稿；已冻结走 save_artifact("
            "amendment_reason=...) 修订链）；裁决落 research_state 后框架在 "
            "Analysis 收尾自动投影成已采纳 claim。")
    return out


async def write_claim(
    state: State,
    claim_text: str,
    claim_type: str,
    concept_ids: list[str] | None = None,
    sources: list[str] | None = None,
    confidence: float = 0.5,
    replication_count: int = 0,
    scope_dimensions: dict | None = None,
    orphan_reason: str | None = None,
    # claim_type-specific
    falsification_criteria_structured: dict | None = None,
    falsification_criteria_text: str | None = None,
    prereg_chunk_id: str | None = None,
    hypothesis_id: str | None = None,
    predicted_outcome: str | None = None,
    synthesis_pattern: str | None = None,
    rationale: str | None = None,
    alternatives_considered: list[str] | None = None,
    dont_repeat_reason: str | None = None,
    next_try: str | None = None,
    scope: str | None = None,
    created_by_role: str = "agent_auto",
    adoption: str | None = None,
    **_: Any,
) -> dict:
    # ── 采纳即登记：sources 里的 artifact id 自动固化为 chunk ───────────────
    #
    # 以前这是模型的一步手工仪式（下架的 kb_register_artifact_as_chunk 工具；
    # 更早的 freeze_and_register 还把它和冻结焊在一起，把登记提前到了冻结时刻，
    # 违反"被采纳才进 KB"）。登记的正确时机是**引用发生时**：写 claim 引用了
    # 某个 artifact → 它被采纳了 → 机械登记。不引用不登记。
    if sources:
        _resolved: list[str] = []
        for _src in sources:
            _sid = str(_src or "")
            if (_sid and not _sid.startswith(("chunk_", "claim_"))
                    and ":" not in _sid
                    and state.read_artifact(_sid) is not None):
                _reg = await _kb_register_artifact_as_chunk(state, artifact_id=_sid)
                if _reg.get("status") == "success" and _reg.get("chunk_id"):
                    _resolved.append(str(_reg["chunk_id"]))
                    continue
            _resolved.append(_sid)
        sources = _resolved

    rec: dict[str, Any] = {
        "claim_text": claim_text,
        "claim_type": claim_type,
        "concept_ids": concept_ids or [],
        "sources": sources or [],
        "confidence": confidence,
        "replication_count": replication_count,
        "scope_dimensions": scope_dimensions or {},
        "created_by_role": created_by_role,
        "created_by_run_id": state.run_id,
        "created_by_node_type": state.node_type,
    }
    # optional fields — 只放进非 None 的
    for k, v in [
        ("orphan_reason", orphan_reason),
        ("falsification_criteria_structured", falsification_criteria_structured),
        ("falsification_criteria_text", falsification_criteria_text),
        ("prereg_chunk_id", prereg_chunk_id),
        ("predicted_outcome", predicted_outcome),
        ("synthesis_pattern", synthesis_pattern),
        ("rationale", rationale),
        ("alternatives_considered", alternatives_considered),
        ("dont_repeat_reason", dont_repeat_reason),
        ("next_try", next_try),
    ]:
        if v is not None:
            rec[k] = v
    if scope is not None:
        rec["scope"] = scope
    if adoption is not None:
        rec["adoption"] = adoption

    if hypothesis_id is not None and str(hypothesis_id).strip():
        rec["hypothesis_id"] = str(hypothesis_id).strip()

    # v3.1（审计 高危#4）：hypothesis 的 prereg_chunk_id 不只验非空 ——
    # 该 chunk 必须真实存在（引用不存在的 id 是契约违约）。来源 artifact 是否
    # 已 freeze 则**如实入账**而不拦：chunk 已带 origin_artifact_frozen /
    # version / content_hash 钉死了当时的事实，claim 标 prereg_frozen 让下游
    # （experiment 启动闸只认冻结的预注册）与终审看得见。
    if claim_type == "hypothesis" and not (hypothesis_id or "").strip():
        # RFC 2026-08-18：hypothesis claim 的身份挂在 (预注册身份, 问题 id) 上。
        # 缺 hypothesis_id 就退回按措辞寻址 —— 修订会产生平行 claim、旧判据
        # 永远清不掉（#395-2）。契约必须送到调用方：合法取值就是预注册里的
        # 问题/假说 id。
        return {
            "status": "error",
            "error": (
                "hypothesis 类 claim 必须带 hypothesis_id（预注册里的问题/假说 "
                "id，如 'H1' / 'Q2'）。它是这条 claim 的身份锚：同一 id 在预注册"
                "修订后重新 create_claim 会**原地更新**同一条 claim（保留修订"
                "历史），而不是造出并存的重复 claim。"
            ),
        }
    if claim_type == "hypothesis" and prereg_chunk_id:
        prereg_chunk = state.get_kb_record("chunks", prereg_chunk_id)
        if prereg_chunk is None:
            return {
                "status": "error",
                "error": (
                    f"prereg_chunk_id={prereg_chunk_id!r} 在 KB 里不存在。"
                    f"正确顺序：save_artifact(artifact_type='pre_registration') → "
                    f"freeze_artifact —— 冻结的返回值里就带 `chunk_id`，"
                    f"拿那个真实 id 再立 hypothesis。不需要再调别的登记工具。"
                ),
            }
        rec["prereg_frozen"] = bool(prereg_chunk.get("origin_artifact_frozen"))
        if not rec["prereg_frozen"]:
            state.append_transcript(
                "hypothesis_claim_on_unfrozen_prereg",
                prereg_chunk_id=prereg_chunk_id, hypothesis_id=hypothesis_id,
                origin_artifact_id=prereg_chunk.get("origin_artifact_id"),
            )
        # 谱系锚由框架从 chunk 机械补齐，不要模型手填（手填会拼错，拼错 = 身份漂移）
        lineage = str(prereg_chunk.get("origin_artifact_id") or "").strip()
        if lineage:
            rec["prereg_artifact_id"] = lineage

    # ── v3.4 类别标注（E2E#2：confidence ceiling 的 15 次触发全是误伤"论文 X
    # 报告了 Y"式文献转述 —— 其 source 就是那篇论文本身，"复现"概念不适用）。
    # 机械判定：sources 非空，且每个 source 要么本身是外部 URI（doi:/arxiv:/http），
    # 要么指向 source 为外部 URI 的 chunk → literature_reported=True。
    # kb_schema 的 evidence ceiling 对该类豁免；org 降级不豁免（文献转述仍默认
    # project —— 跨项目共享的载体是 paper chunk 本身，不是转述 claim）。
    try:
        from shared.lib.kb_schema import is_external_uri as _is_ext
        _srcs = rec.get("sources") or []
        if _srcs:
            _all_lit = True
            for _s in _srcs:
                if _is_ext(_s):
                    continue
                if isinstance(_s, str) and _s.startswith("chunk_"):
                    _ch = state.get_kb_record("chunks", _s)
                    if _ch and _is_ext(_ch.get("source") or ""):
                        continue
                _all_lit = False
                break
            if _all_lit:
                rec["literature_reported"] = True
    except Exception:
        pass   # 类别标注失败不阻断写入（退回默认=经验类，ceiling 从严）

    try:
        record, created = state.write_kb("claims", rec)
        out = {
            "status": "success",
            "id": record["id"],
            "scope": record["scope"],
            "claim_status": record["status"],
            "created": created,
        }
        if "prereg_frozen" in rec:
            out["prereg_frozen"] = rec["prereg_frozen"]
            if not rec["prereg_frozen"]:
                out["note"] = (
                    f"prereg chunk {prereg_chunk_id!r} 的来源预注册**尚未冻结**，"
                    "已如实记 prereg_frozen=false。冻结（freeze_artifact）仍然欠着："
                    "experiment 只认冻结后的预注册，未冻结的判据事后可改、不构成承诺。")
        return out
    except SchemaValidationError as e:
        return {"status": "error", "error": str(e)}


register_tool(
    ToolDefinition(
        name="create_claim",
        description=(
            "在**本项目**的 KB 写一条命题（共 5 种 claim_type）。\n\n"
            "**Use when**：写任何带 truth-value 的研究陈述。\n\n"
            "**Do NOT use when**：\n"
            "  - 想注册新概念名 → create_concept\n"
            "  - 想记实验动作 → create_experiment\n"
            "  - 想存 runtime 观察 → memory_note（category 只有 'pitfall' / 'method' 两个值），"
            "沉淀稳定后再升 claim\n\n"
            "**先 search_kb！**：写前必须 `search_kb(entity_type='claims', query=...)` 看是否有相近的。\n\n"
            "**claim_type 选择**（必填）。只有 5 种 —— 每种都对应一条真实存在的机械"
            "处理路径（晋升分道 / 冻结校验 / 死路红旗），不是给知识贴标签：\n"
            "  - empirical：观测/测量得出的结论（最常用；理论推导也归此，"
            "推导链写进 claim_text）\n"
            "  - methodological：关于'怎么做'的可复用结论（配方、踩过的坑、参数选择理由）\n"
            "  - hypothesis：★仅 _curator 采纳时写入。必须有 prereg_chunk_id + "
            "predicted_outcome —— 承诺本体在冻结的 pre_registration 里\n"
            "  - synthesis：多 claim 聚出的高阶结论（sources 须含 ≥ 2 个 claim_id）\n"
            "  - dead_end：已知不通的路（必填 dont_repeat_reason）。它会被 reviewer "
            "机械比对，防别的项目再走一遍\n\n"
            "**核心字段**：\n"
            "  - confidence：[0,1] 连续不确定性。0.5 中性 / >0.85 强信心 / <0.25 强反对。\n"
            "  - sources：chunk_id / claim_id（synthesis 用） / 外部 URI（doi:/arxiv:/https:/pmid:）\n"
            "  - concept_ids：至少 1 个；无则填 orphan_reason 解释为啥没\n"
            "  - scope_dimensions：dict 描述适用范围（dataset/regime/seed/n 等）\n\n"
            "**scope 不用传，也传不了 org**。项目 KB 是**零门禁工作记忆**："
            "你观察到什么就写什么，不必先凑够证据。组织级知识只有一条出生通道 —— "
            "项目终态时的批量晋升（`curator_scan(scan_type='org_promotion_candidates')`，"
            "curator 专用），在那里做三项机械检查"
            "（终态批次 / 证据已冻结 / 已去项目化）并由人审。写入时不设门槛，"
            "是为了让准入发生在**能看全局的时刻**，而不是在你还不知道这条有没有用的时候。\n\n"
            "**返回**：`{id, scope, claim_status, created}`。created=False 表示内容寻址命中已有 claim，sources 自动 merge。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "claim_text": {"type": "string", "description": "断言全文。带条件 + 数值，例 'GAP on QM9 OOD MAE=156 meV/atom (95% CI 150-162)'"},
                "claim_type": {"type": "string", "enum": list(CLAIM_TYPES),
                    "description": "5 个 type 之一，详见工具 description"},
                "concept_ids": {"type": "array", "items": {"type": "string"},
                    "description": "引用的 concept ids（≥ 1，否则填 orphan_reason）"},
                "sources": {"type": "array", "items": {"type": "string"},
                    "description": "chunk_id / claim_id / 外部 URI 混合 ≥ 1。"
                                   "本 run 的产物：先 freeze_artifact 冻结，返回值里带的 `chunk_id` 填这里"
                                   "（也可直接填**已冻结**产物的 artifact_id，框架会在写入时替你登记成 chunk）"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                    "default": 0.5,
                    "description": "连续不确定性 [0,1]。0.5=中性 / >0.85=强 / <0.25=强反对"},
                "replication_count": {"type": "integer", "default": 0, "minimum": 0,
                    "description": "独立 evidence 支持次数。N≥3 才稳"},
                "scope_dimensions": {"type": "object",
                    "description": "适用范围 dict：dataset / regime / seed / n / metric / condition 等。空 dict 视为'普适'"},
                "orphan_reason": {"type": "string",
                    "description": "若 concept_ids 为空必须填，解释为啥这条 claim 无 concept anchor"},
                "falsification_criteria_structured": {"type": "object",
                    "description": "(hypothesis only) 结构化 falsifier：{metric, op, threshold}。命中可机械判 verdict"},
                "falsification_criteria_text": {"type": "string",
                    "description": "(hypothesis only) 自然语言 falsifier（structured + text 至少一个）"},
                "prereg_chunk_id": {"type": "string",
                    "description": "(hypothesis only) 必填。就是 freeze_artifact 冻结 "
                                   "pre_registration 时返回值里的 `chunk_id`（冻结即登记，"
                                   "不需要再调别的登记工具）"},
                "hypothesis_id": {"type": "string",
                    "description": "(hypothesis only) 必填。预注册里的问题/假说 id（如 'H1'/'Q2'）——"
                                   "这是 claim 的**身份锚**：预注册修订后用同一 id 重新 create_claim "
                                   "会原地更新同一条 claim（带修订历史），不会产生并存的重复 claim"},
                "predicted_outcome": {"type": "string",
                    "description": "(hypothesis only) 必填。明示预测的实验结果范围"},
                "synthesis_pattern": {"type": "string",
                    "description": "(synthesis only) 一句话 patternsumamry"},
                "rationale": {"type": "string",
                    "description": "(methodological only) 为啥这么决策"},
                "alternatives_considered": {"type": "array", "items": {"type": "string"},
                    "description": "(methodological only) 考虑过的其它选项"},
                "dont_repeat_reason": {"type": "string",
                    "description": "(dead_end only) 非空，说清为啥这条路不通"},
                "next_try": {"type": "string",
                    "description": "(dead_end only) 如果要继续这方向，下次该怎么转向"},
                "scope": {"type": "string", "enum": ["project"],
                    "description": "不用传，只能是 'project'。org 层没有出生通道，"
                                   "只接受**晋升**：项目终态时 curator 扫盘出候选、"
                                   "去项目化改写成知识卡、经人批准后带出处写入 org"},
                "created_by_role": {"type": "string", "enum": list(CREATED_BY_ROLES),
                    "default": "agent_auto"},
            },
            "required": ["claim_text", "claim_type"],
        },
        risk_level="medium",
    ),
    _create_claim,
)


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# create_experiment
# ─────────────────────────────────────────────────────────────────────────────

async def _create_experiment(
    state: State,
    experiment_text: str,
    setup_text: str = "",
    setup_structured: dict | None = None,
    run_at: str = "",
    outcome: str = "inconclusive",
    frozen_log_chunk_id: str = "",
    tested_hypothesis_ids: list[str] | None = None,
    wall_time_seconds: float | None = None,
    cost_estimate: float | None = None,
    replication_index: int | None = None,
    scope: str | None = None,
    created_by_role: str = "agent_auto",
    **_: Any,
) -> dict:
    rec: dict[str, Any] = {
        "experiment_text": experiment_text,
        "setup_text": setup_text,
        "setup_structured": setup_structured or {},
        "run_at": run_at or now_iso(),
        "outcome": outcome,
        "frozen_log_chunk_id": frozen_log_chunk_id,
        "tested_hypothesis_ids": tested_hypothesis_ids or [],
        "run_by_run_id": state.run_id,
        "created_by_role": created_by_role,
        "created_by_run_id": state.run_id,
        "created_by_node_type": state.node_type,
    }
    if wall_time_seconds is not None:
        rec["wall_time_seconds"] = wall_time_seconds
    if cost_estimate is not None:
        rec["cost_estimate"] = cost_estimate
    if replication_index is not None:
        rec["replication_index"] = replication_index
    if scope is not None:
        rec["scope"] = scope

    try:
        record, created = state.write_kb("experiments", rec)
        return {"status": "success", "id": record["id"], "scope": record["scope"], "created": created}
    except SchemaValidationError as e:
        return {"status": "error", "error": str(e)}


register_tool(
    ToolDefinition(
        name="create_experiment",
        description=(
            "登记一次实验动作（KB 里 experiment 是时间戳动作记录，不是命题）。\n\n"
            "**Use when**：跑完任何独立实验（仿真 / 训练 / 评测）想留可查可复现记录。\n\n"
            "**Do NOT use when**：\n"
            "  - 想存实验得出的 claim → create_claim（experiment 关联的事实另写 claim）\n"
            "  - 实验还没跑完 → 跑完再登记，否则 setup/outcome/log 都不完整\n\n"
            "**关键字段**：\n"
            "  - setup_structured：dict 含 T/dt/ensemble/binary/version/seed 等参数（复现关键）\n"
            "  - frozen_log_chunk_id：freeze_artifact 冻结 experiment_log 后，返回值里就带 `chunk_id`，填那个\n"
            "  - outcome ∈ {success, refuted, inconclusive, error}\n"
            "  - tested_hypothesis_ids：list of claim_id（hypothesis 是 claim 子类型）\n"
            "  - replication_index：复现某 experiment 时填第 N 次\n\n"
            "**scope**：默认 project（实验天然 project-bound）"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "experiment_text": {"type": "string", "description": "实验描述（'GAP NVT MD on QM9 OOD subset'）"},
                "setup_text": {"type": "string", "description": "setup 自然语言版"},
                "setup_structured": {"type": "object",
                    "description": "结构化参数：T / dt / ensemble / software / version / seed"},
                "run_at": {"type": "string", "description": "ISO 时间。默认 now"},
                "outcome": {"type": "string",
                    "enum": ["success", "refuted", "inconclusive", "error"], "default": "inconclusive"},
                "frozen_log_chunk_id": {"type": "string",
                    "description": "freeze_artifact 冻结 experiment_log artifact 后，返回值里的 `chunk_id`"},
                "tested_hypothesis_ids": {"type": "array", "items": {"type": "string"}},
                "wall_time_seconds": {"type": "number"},
                "cost_estimate": {"type": "number"},
                "replication_index": {"type": "integer", "minimum": 1,
                    "description": "复现第 N 次时填"},
                "scope": {"type": "string", "enum": list(SCOPES)},
                "created_by_role": {"type": "string", "enum": list(CREATED_BY_ROLES),
                    "default": "agent_auto"},
            },
            "required": ["experiment_text"],
        },
        risk_level="low",
    ),
    _create_experiment,
)


# ─────────────────────────────────────────────────────────────────────────────
# update_claim_status（统一替代 v2 的 3 个 update_X_status）
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_FLIP_LIMIT = 3  # 同 claim 单 session 翻到第几次算 thrash 信号（只记不拦）


async def _update_claim_status(
    state: State,
    claim_id: str,
    new_status: str | None = None,
    new_confidence: float | None = None,
    reasoning: str = "",
    evidence_ids: list[str] | None = None,
    hypothesis_id: str | None = None,
    superseded_by_claim_id: str | None = None,
    **_: Any,
) -> dict:
    # reasoning 非空由 parameters_schema 声明（minLength:1），派发口核一次。
    status_change: dict[str, Any] = {}
    if new_status is not None:
        status_change["to_status"] = new_status
    if new_confidence is not None:
        status_change["confidence"] = new_confidence
    if not status_change:
        return {"status": "error", "error": "至少传一个 new_status 或 new_confidence"}

    # RFC 2026-08-18：schema 里的 superseded_by_claim_id 声明了六年没有任何
    # 写入点 —— "claim 被新 claim 取代"只剩两个孤立记录，取代关系永久丢失。
    # 翻 superseded 必须指名接任者；接任者必须真实存在。
    if new_status == "superseded":
        successor = (superseded_by_claim_id or "").strip()
        if not successor:
            return {
                "status": "error",
                "error": (
                    "new_status='superseded' 必须同时传 superseded_by_claim_id="
                    "<接任 claim 的 id>。没有接任者的\"取代\"不成立 —— 如果这条 "
                    "claim 只是错了而没有替代者，用 new_status='refuted'。"
                ),
            }
        if state.get_kb_record("claims", successor) is None:
            return {
                "status": "error",
                "error": (f"superseded_by_claim_id={successor!r} 在 KB 里不存在。"
                          f"先 create_claim 建出接任 claim，再来标旧的 superseded。"),
            }
        status_change["superseded_by_claim_id"] = successor

    # ── 裁决资格（同一条降落路径）────────────────────────────────────────
    #
    # 下面几道检查问的都是同一个问题：**这次翻 validated/refuted 够不够格**
    # （本 run 声明了 infeasible / Analysis 没背书 / 预注册承诺没兑现 / 没有
    # 证据）。资格不够一律不拒绝——**如实降落 provisional**：账永真（S1：不够
    # 格的裁决从不以 validated/refuted 入账）、零死路（S3：补齐资格后可再翻）。
    # 差额原因全文入 transcript 与返回值（authority_note），referee 终审可见。
    _verdict_downgrade_note = None

    def _land_provisional(note: str, **extra: Any) -> None:
        nonlocal _verdict_downgrade_note, new_status
        state.append_transcript(
            "scientific_verdict_downgraded_to_provisional",
            claim_id=claim_id, hypothesis_id=hypothesis_id,
            requested_status=new_status, note=note[:600], **extra)
        _verdict_downgrade_note = note
        new_status = "provisional"
        status_change["to_status"] = "provisional"

    # v3.3（架构修复 D，防循环验证）：本 run 产出里已声明任务不可执行
    # （metadata.infeasible=true 或 ## Feasibility 段判 infeasible）。实测事故：
    # experiment 收到没资源执行的任务，静默换成"按假设设计好 ground truth 的
    # synthetic 数据"跑完，基于它 refuted 了 hypothesis claim —— 循环证伪进 KB。
    # 只约束 producing 节点自己的 run；_curator dreaming 等治理路径不受影响。
    if (new_status in ("validated", "refuted")
            and not state.node_type.startswith("_")):
        from shared.lib.feasibility import find_infeasibility_declaration
        _infeasible = find_infeasibility_declaration(state)
        if _infeasible:
            _land_provisional(
                f"本 run 的产出 {_infeasible['artifact_id']!r} 已声明任务不可执行"
                f"（infeasible）—— 未真正执行的实验不能把 claim 翻成 "
                f"{status_change['to_status']!r}（循环验证）。在 experiment_log "
                f"写清缺什么资源、试过哪些获取手段；框架会 REDIRECT 回上游降级设计。",
                infeasible_artifact_id=_infeasible["artifact_id"])

    # v2.1 裁决权边界：假说成不成立由 Analysis 判，落在 research_state 上。
    # experiment 仍然测量、仍然做执行层自查，但不再自己给自己的实验下科学结论。
    if new_status in ("validated", "refuted"):
        from core.verdict_authority import scientific_verdict_block

        _authority_block = scientific_verdict_block(
            state, hypothesis_id=hypothesis_id, new_status=new_status)
        if _authority_block:
            _land_provisional(_authority_block)

    # v3.8 承诺对象化：翻 validated/refuted = **关闭一条预注册承诺**，必须指名关
    # 的是哪一条，且那条承诺的每个 metric 都得真测过。E2E-3：H3 的判据是合取
    # （cost_reduction_pct AND success_rate_delta_pp），第二项论文自己承认没测
    # （"estimated, not measured"），却照样写成 refuted 进了 KB 和摘要。
    # 一个没被测量的合取项，既不能证实也不能证伪。见 core/prereg_commitments.py。
    if (new_status in ("validated", "refuted")
            and not state.node_type.startswith("_")):
        from core import prereg_commitments as _pc
        _rec_for_gate = state.get_kb_record("claims", claim_id)
        if _rec_for_gate is not None:
            _blk = _pc.status_flip_block(
                state, _rec_for_gate, hypothesis_id, new_status)
            if _blk:
                # 预注册承诺未兑现/未指名/范围外；申报式兑现
                # （estimated+basis+degraded_reason）仍是升格出口。
                _land_provisional(_blk)

    # 证据充分性（原 kb_schema.validate_status_flip 465/474 的两条 raise）：
    # 翻 validated/refuted 没挂任何证据、或 hypothesis 只拿别的 claim 互证 ——
    # 这是裁决资格不够，不是账假；同一条降落路径，authority_note 写明
    # verdict_evidence: none | claims_only。证据 id 的形状/存在性仍是契约（下方）。
    if new_status in ("validated", "refuted"):
        _ev = [e for e in (evidence_ids or []) if isinstance(e, str) and e.strip()]
        _rec_for_ev = state.get_kb_record("claims", claim_id)
        _is_hypo = bool(_rec_for_ev) and _rec_for_ev.get("claim_type") == "hypothesis"
        if not _ev:
            _land_provisional(
                f"verdict_evidence: none —— 翻到 {new_status!r} 没有挂任何 evidence_ids"
                f"（chunk_id / experiment_id / claim_id）。先登记结果证据："
                f"证据类产物 freeze_artifact 一下，返回值里就带 chunk_id；实验动作"
                f"本身用 create_experiment 拿 experiment_id。",
                verdict_evidence="none")
        elif _is_hypo and not any(e.startswith(("chunk_", "experiment_")) for e in _ev):
            _land_provisional(
                "verdict_evidence: claims_only —— hypothesis 的 verdict 只引用了其它 "
                "claim 互证，没有 chunk_id / experiment_id（原始数据/实验记录）。",
                verdict_evidence="claims_only")

    # 契约：claim 存在、状态机转换合法、evidence_ids 形状合法且真实存在。
    if new_status is not None:
        rec = state.get_kb_record("claims", claim_id)
        if rec is None:
            return {"status": "error", "error": f"claim_id={claim_id!r} 不存在"}
        try:
            validate_status_flip(
                rec, new_status,
                reasoning=reasoning, evidence_ids=evidence_ids,
            )
        except SchemaValidationError as e:
            return {"status": "error", "error": str(e)}
        if evidence_ids:
            missing = [e for e in evidence_ids
                       if state.get_kb_record(
                           "chunks" if e.startswith("chunk_")
                           else "experiments" if e.startswith("experiment_")
                           else "claims", e) is None]
            if missing:
                return {
                    "status": "error",
                    "error": (
                        f"evidence_ids 中 {missing!r} 在 KB 里不存在。"
                        f"先登记证据：证据类产物（experiment_log / observation_log / "
                        f"raw_data_dump / tool_output_log / pre_registration）"
                        f"freeze_artifact 一下，返回值里就带 `chunk_id`；"
                        f"实验动作本身用 create_experiment 登记拿 experiment_id。"
                    ),
                }
            status_change["evidence_ids"] = evidence_ids

    # 同 session 反复翻同一 claim 是 thrash **信号**，只记不拦（判决拆除 O3）：
    # 第 4 次翻转可能正是新实验结果，裸计数器分不出来；review_history
    # append-only 已把每次翻转留痕，人和 dreaming 复盘看得见。
    # （confidence-only override 不计入 flip 计数）
    _thrash_signal = False
    if new_status is not None:
        flips = state.hook_state.setdefault("session_claim_flips", {})
        already = int(flips.get(claim_id, 0))
        if already >= _SESSION_FLIP_LIMIT:
            _thrash_signal = True
            state.append_transcript(
                "claim_status_thrash_signal", claim_id=claim_id,
                session_flips_so_far=already, requested_status=new_status)

    try:
        updated = state.update_lifecycle(
            "claims", claim_id,
            status_change=status_change,
            reasoning=reasoning,
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if updated is None:
        return {"status": "error", "error": f"claim_id={claim_id!r} 不存在"}

    if new_status is not None:
        flips = state.hook_state.setdefault("session_claim_flips", {})
        flips[claim_id] = int(flips.get(claim_id, 0)) + 1

    # v3.1（审计 高危#8）：refute 自动沿依赖图传播 needs_review。
    # propagate_refutation 此前是无调用者的死代码 —— 现在 refute 即触发，
    # 下游依赖 claim 全部标 needs_review 等复审，反驳不再原地蒸发。
    propagated: list[str] = []
    if new_status == "refuted":
        try:
            from core.kb_edges import propagate_refutation
            propagated = propagate_refutation(state, claim_id)
        except Exception as e:   # 传播失败不吞掉主操作，但要暴露
            propagated = []
            state.append_transcript(
                "refutation_propagation_error", claim_id=claim_id, error=str(e),
            )

    result = {
        "status": "success",
        "id": claim_id,
        "new_status": updated["status"],
        "new_confidence": updated.get("confidence"),
        "session_flips_so_far": state.hook_state.get(
            "session_claim_flips", {}).get(claim_id, 0),
    }
    if _verdict_downgrade_note:
        result["landed_status"] = updated["status"]
        result["authority_note"] = _verdict_downgrade_note
        result["next_step"] = (
            "本次裁决资格不足，已如实降落 provisional（原因见 authority_note）。"
            "补齐资格（Analysis 背书 / 预注册兑现或申报式降级 / 登记证据）后可再翻。"
        )
    if _thrash_signal:
        result["thrash_signal"] = True
        result["note"] = (
            f"claim {claim_id} 本 session 已翻 {result['session_flips_so_far']} 次"
            f"（已记 thrash 信号）。反复翻转多半不是新证据——若不是，改走 "
            f"propose(proposal_type='kb_claim_status_flip', ...) 让人复盘。")
    if new_status == "refuted":
        result["downstream_marked_needs_review"] = propagated
        if propagated:
            result["note"] = (
                f"{len(propagated)} 条下游依赖 claim 已标 needs_review，"
                f"待 curator / 复审后重新定级。"
            )
    return result


register_tool(
    ToolDefinition(
        name="update_claim_status",
        description=(
            "翻 claim 的 status / 调 confidence。统一替代 v2 的 "
            "update_claim_status / update_hypothesis_verdict / update_question_status / update_opportunity_status。\n\n"
            "**Use when**：\n"
            "  - 新实验 evidence 支持/反驳现有 claim\n"
            "  - hypothesis 实验完成判 verdict（new_status='validated'/'refuted'）\n"
            "  - open question 找到答案 → 改 status='validated'\n"
            "  - claim 被新 claim 取代 → 'superseded'\n\n"
            "**Do NOT use when**：\n"
            "  - 想加新独立 claim → create_claim\n"
            "  - claim 内容错了 → 改 canonical 不允许，要 supersede（create 新 claim + 这里 status='superseded'）\n\n"
            "**裁决资格（不拒，如实降落）**：翻 validated/refuted 时若 Analysis 未背书 / "
            "预注册承诺未兑现 / 本 run 已声明 infeasible / 没挂 evidence_ids"
            "（hypothesis 还须含 chunk/experiment 级证据），一律**降落 provisional**，"
            "返回 landed_status + authority_note，补齐后可再翻。\n"
            "  - refute 会自动沿依赖图把下游 claim 标 needs_review（返回受影响列表）\n"
            "  - 高风险 flip（validated→refuted 等）建议走 propose 而非直调\n"
            "  - 非法 status 转换（如 refuted→validated）是契约违约，会报错\n\n"
            "**关键参数**：\n"
            "  - new_status：6 个之一 {open, provisional, validated, refuted, superseded, needs_review}\n"
            "  - new_confidence：[0,1] 连续 override（status 由 confidence + review_history derived；只传它不需要证据）\n"
            "  - evidence_ids：verdict 的证据链接（chunk_/experiment_/claim_ id）；缺则如实降落 provisional\n"
            "  - reasoning：非空，写清判定依据（进 review_history append-only audit）"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "claim_id": {"type": "string"},
                "new_status": {"type": "string",
                    "enum": ["open", "provisional", "validated", "refuted",
                             "superseded", "needs_review"]},
                "superseded_by_claim_id": {"type": "string",
                    "description": "new_status='superseded' 时必填：接任 claim 的 id。"
                                   "没有接任者的取代不成立（只是错了 → 用 refuted）"},
                "new_confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                    "description": "覆盖 confidence 值；如果只传这个，status 由系统重算"},
                "evidence_ids": {"type": "array", "items": {"type": "string"},
                    "description": "verdict 证据链接（chunk_/experiment_/claim_ id），"
                                   "写进 review_history。翻 validated/refuted 缺它"
                                   "会如实降落 provisional"},
                "reasoning": {"type": "string", "minLength": 1,
                    "description": "非空：写清判定依据（对照了哪条判据、看了哪个结果）"},
                "hypothesis_id": {"type": "string",
                    "description": "本次关闭的**预注册假设编号**（如 'H3'）。项目已冻结"
                                   "预注册时，翻 validated/refuted 必填 —— 框架会机械"
                                   "核对该假设的每个判据 metric 是否都有 measured 记录"
                                   "（合取：任一项没测就既不能证实也不能证伪）。"},
            },
            "required": ["claim_id", "reasoning"],
        },
        risk_level="medium",
    ),
    _update_claim_status,
)


# ─────────────────────────────────────────────────────────────────────────────
# search_kb（扩展 filter）
# ─────────────────────────────────────────────────────────────────────────────

async def _search_kb(
    state: State,
    entity_type: str = "",
    query: str = "",
    limit: int = 20,
    recent_n: int = 5,
    concept_type: str | None = None,
    claim_type: str | None = None,
    status_filter: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    scope: str | None = None,
    concept_ids: list[str] | None = None,
    source_contains: str | None = None,
    include_invalid: bool = False,
    projection: str = "summary",
    **_: Any,
) -> dict:
    # entity_type 不传 = 全景仪表盘（原 kb_overview）。「看大势」和「按条件找」
    # 是同一个动作的两档，不是两个工具 —— 后者只是前者加了 where 子句。
    if not (entity_type or "").strip():
        return await _kb_overview(state, recent_n=recent_n)
    # entity_type / projection 的枚举由 parameters_schema 声明，派发口核一次。
    records = state.list_kb(entity_type, scope_filter=scope)
    q = query.strip().lower() if query else ""

    out: list[dict] = []
    for r in records:
        # 默认隐藏 invalid status
        if not include_invalid:
            if entity_type == "claims" and r.get("status") in ("refuted", "superseded"):
                continue
            if entity_type == "concepts" and r.get("status") in ("merged_into", "deprecated"):
                continue

        # type filter
        if entity_type == "concepts" and concept_type:
            if r.get("concept_type") != concept_type:
                continue
        if entity_type == "claims" and claim_type:
            if r.get("claim_type") != claim_type:
                continue

        # status filter
        if status_filter:
            if r.get("status") != status_filter:
                continue

        # confidence range（only for claims）
        if entity_type == "claims":
            conf = r.get("confidence", 0.5)
            if confidence_min is not None and conf < confidence_min:
                continue
            if confidence_max is not None and conf > confidence_max:
                continue

        # concept_ids overlap（claims / experiments）
        if concept_ids:
            r_concepts = set(r.get("concept_ids") or []) | set(r.get("about_concept_ids") or [])
            if not r_concepts & set(concept_ids):
                continue

        # source_contains（claims）
        if source_contains and entity_type == "claims":
            srcs = r.get("sources") or []
            if not any(source_contains in str(s) for s in srcs):
                continue

        # text query
        if q:
            blob = " ".join(str(r.get(k, "")) for k in
                            ("canonical_name", "description", "claim_text",
                             "experiment_text", "text"))
            if q not in blob.lower():
                continue

        out.append(r)
        if len(out) >= limit:
            break

    # 有界投影 + token 预算：保证单次结果与 KB 总量无关（根因修复 2026-07 —— curator
    # dreaming 一次 limit=200 拉回 159 条全量 claim = 83k tokens 顶爆 context）。
    # summary（默认）只返回摘要行；full 超预算自动降级。想看全文用 get_kb_record。
    from shared.lib.kb_result_budget import budget_kb_search
    result = budget_kb_search(out, entity_type, projection=projection)
    # 兼容旧 caller：保留 count 字段（= returned）。
    result["count"] = result["returned"]
    return result


register_tool(
    ToolDefinition(
        name="search_kb",
        replayable_read=True,
        description=(
            "检索 KB 任意 entity，按 type / status / confidence / scope / concept_ids 过滤。\n\n"
            "**Use when**：\n"
            "  - 写新 claim 前：先 `search_kb(entity_type='claims', query='...')` 看 KB 是否已有相近（**强制工作流**）\n"
            "  - 写新 concept 前：先看 KB 已有命名，防止同义异名分裂\n"
            "  - 分析时找在测 hypothesis：`entity_type='claims', claim_type='hypothesis', status_filter='open'`\n"
            "  - 找争议区 claim：`entity_type='claims', confidence_min=0.4, confidence_max=0.7`\n"
            "  - 找特定 person 的 claim：`entity_type='claims', concept_ids=[person_id]`\n\n"
            "**Do NOT use when**：\n"
            "  - 找本项目沉淀的踩坑 / 做法 → memory_recall\n"
            "  - 找上游节点 artifact → list_artifacts + read_artifact\n"
            "  - 想写新 KB entity → 本工具只读\n\n"
            "**关键参数**：\n"
            "  - entity_type ∈ {concepts, claims, experiments, chunks}（v3 只 4 个）\n"
            "  - claim_type / concept_type：在该 entity 内子过滤\n"
            "  - scope：'project'/'org'/None（None=跨两层 union）\n"
            "  - confidence_min/max：[0,1] 区间过滤（仅 claims）\n"
            "  - status_filter：'open'/'provisional'/'validated' 等\n"
            "  - include_invalid：默认 False（隐藏 refuted/superseded）\n"
            "  - **projection**：'summary'（默认，每条只回 id + 一行摘要 + 关键字段）"
            "/ 'full'（回全量 record）。**先 summary 扫，再对想深读的具体 id 用 "
            "`get_kb_record` 拿全文**——像用搜索引擎先看标题摘要再点开。full 模式"
            "若载荷超单次结果预算会自动降级为 summary，绝不让一次结果爆上下文。\n\n"
            "**返回**：`{total_matched, returned, projection, truncated, records, hint?}`。"
            "total_matched=命中总数（不因预算丢失计数）；truncated=True 表示还有更多，"
            "加 filter 收窄或逐条 get_kb_record。空 records 表示该范围 KB 没有 → 该 create。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "entity_type": {"type": "string",
                    "enum": ["concepts", "claims", "experiments", "chunks"],
                    "description": "不传 = 返回 KB 全景（各 entity 总数 + claim "
                                   "按 type/status 分布 + 各 entity 最近 N 条）"},
                "recent_n": {"type": "integer", "default": 5, "minimum": 1,
                    "maximum": 50,
                    "description": "全景档：每个 entity 列几条最近记录"},
                "query": {"type": "string",
                    "description": "子串匹配（不是语义检索）。用 KB 实际字面词"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "projection": {"type": "string", "enum": ["summary", "full"],
                    "default": "summary",
                    "description": "summary=每条只回摘要行（默认，省 context）；"
                                   "full=回全量 record（超预算自动降级）。深读单条用 get_kb_record。"},
                "concept_type": {"type": "string", "enum": list(CONCEPT_TYPES)},
                "claim_type": {"type": "string", "enum": list(CLAIM_TYPES)},
                "status_filter": {"type": "string"},
                "confidence_min": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                    "description": "(claims only) 最低 confidence"},
                "confidence_max": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                    "description": "(claims only) 最高 confidence"},
                "scope": {"type": "string", "enum": list(SCOPES)},
                "concept_ids": {"type": "array", "items": {"type": "string"},
                    "description": "找 records 引用任一这些 concept_id"},
                "source_contains": {"type": "string",
                    "description": "(claims only) sources 子串过滤如 'arxiv:2401'"},
                "include_invalid": {"type": "boolean", "default": False,
                    "description": "包括 refuted/superseded/deprecated"},
            },
            # entity_type 不再必填：不传 = 全景仪表盘（原 kb_overview）。
            # 「看大势」和「按条件找」是同一个动作的两档，不是两个工具。
        },
        risk_level="low",
    ),
    _search_kb,
)


# ─────────────────────────────────────────────────────────────────────────────
# _kb_register_artifact_as_chunk —— **内部函数，不在模型工具面上**。
# 调用方：_create_claim（引用即登记）与 artifacts_extra._freeze_artifact
# （冻结即登记，chunk_id 随 freeze 返回值交给模型）。
# ─────────────────────────────────────────────────────────────────────────────

async def _kb_register_artifact_as_chunk(
    state: State,
    artifact_id: str,
    require_frozen: bool = True,
    author_concept_ids: list[str] | None = None,
    corresponding_author_concept_id: str | None = None,
    created_by_role: str = "agent_auto",
    **_: Any,
) -> dict:
    rec = state.read_artifact(artifact_id)
    if rec is None:
        return {"status": "error", "error": f"找不到 artifact {artifact_id!r}"}
    is_frozen = bool((rec.get("metadata") or {}).get("frozen"))
    # 判决拆除（verdicts_shared kb:986）：未冻结不再拒登——chunk 记录里
    # origin_artifact_frozen/version/content_hash 三件套已把出处如实钉死（S1
    # 不需要这道拒绝兜底）；且本函数不在模型工具面上，闸对 agent 不可达。
    content = rec.get("content", "")
    if not content:
        return {"status": "error", "error": "artifact 内容为空"}

    from core.ledger import record_version, sha256_text

    chunk_rec_in: dict[str, Any] = {
        "text": content,
        "source": f"artifact:{artifact_id}",
        "origin_run_id": state.run_id,
        "origin_artifact_id": artifact_id,
        "origin_artifact_frozen": is_frozen,
        # RFC 2026-08-18：chunk 是 artifact **某一版**的快照 —— 记版本号 +
        # 内容哈希，下游"哪版 prereg 治理这条 claim"从此机械可答。
        "origin_artifact_version": record_version(rec),
        "origin_content_hash": sha256_text(content),
        "offset": 0,
        "length": len(content),
        "author_concept_ids": author_concept_ids or [],
        "created_by_role": created_by_role,
        "created_by_run_id": state.run_id,
        "created_by_node_type": state.node_type,
    }
    if corresponding_author_concept_id:
        chunk_rec_in["corresponding_author_concept_id"] = corresponding_author_concept_id

    try:
        chunk_rec, created = state.write_kb("chunks", chunk_rec_in)
    except SchemaValidationError as e:
        return {"status": "error", "error": str(e)}

    # 修订链自动闭合：同一 artifact 身份的旧版 chunk 打 superseded_by。
    # 框架做，模型零参与也就漏不掉（护栏要扫盘，不要靠申报）。行永不删 ——
    # 老实验引用的旧 chunk 仍可溯源，只是不再被当成现行承诺。
    superseded: list[str] = []
    if created:
        try:
            superseded = state.supersede_older_chunks(
                artifact_id, new_chunk_id=chunk_rec["id"],
                new_version=chunk_rec_in["origin_artifact_version"])
        except Exception as e:      # 闭链失败不吞主操作，但要暴露
            state.append_transcript(
                "chunk_supersede_error", artifact_id=artifact_id, error=str(e))

    out = {
        "status": "success",
        "chunk_id": chunk_rec["id"],
        "scope": chunk_rec["scope"],
        "created": created,
        "length": len(content),
        "origin_artifact_version": chunk_rec_in["origin_artifact_version"],
    }
    if superseded:
        out["superseded_chunk_ids"] = superseded
    return out




# ─────────────────────────────────────────────────────────────────────────────
# kb_ingest（外部长文本切 chunks）
# ─────────────────────────────────────────────────────────────────────────────

async def _kb_ingest(
    state: State,
    text: str,
    source: str,
    chunk_size: int = 1500,
    author_concept_ids: list[str] | None = None,
    corresponding_author_concept_id: str | None = None,
    created_by_role: str = "agent_auto",
    **_: Any,
) -> dict:
    # text / source 非空由 parameters_schema 声明（minLength:1），派发口核一次。
    if not is_external_uri(source):
        return {"status": "error",
                "error": (f"source={source!r} 必须是外部 URI（doi:/arxiv:/https:/pmid:/...）"
                          f"。run-local artifact 不走本工具：freeze_artifact 冻结它，"
                          f"返回值里就带 `chunk_id`。")}

    chunks_out: list[dict] = []
    for i in range(0, len(text), chunk_size):
        piece = text[i:i + chunk_size]
        chunk_in: dict[str, Any] = {
            "text": piece,
            "source": source,
            "offset": i,
            "length": len(piece),
            "author_concept_ids": author_concept_ids or [],
            "created_by_role": created_by_role,
            "created_by_run_id": state.run_id,
            "created_by_node_type": state.node_type,
        }
        if corresponding_author_concept_id:
            chunk_in["corresponding_author_concept_id"] = corresponding_author_concept_id
        try:
            rec, created = state.write_kb("chunks", chunk_in)
        except SchemaValidationError as e:
            return {"status": "error", "error": str(e), "chunks_so_far": chunks_out}
        chunks_out.append({"id": rec["id"], "offset": i, "length": len(piece),
                            "created": created, "scope": rec["scope"]})

    return {
        "status": "success",
        "source": source,
        "chunks_created": sum(1 for c in chunks_out if c["created"]),
        "chunks_merged": sum(1 for c in chunks_out if not c["created"]),
        "chunks": chunks_out,
    }


register_tool(
    ToolDefinition(
        name="kb_ingest",
        description=(
            "把外部长文本按 chunk_size 切片存进 KB（scope=org，跨项目共享）。\n\n"
            "**Use when**：\n"
            "  - 从论文 / 网页 / DB 拉到全文，想沉淀为可引 chunk_id\n"
            "  - 想给某 person/group 关联文献时（填 author_concept_ids）\n\n"
            "**Do NOT use when**：\n"
            "  - 来源是本 run 的 artifact → save_artifact + freeze_artifact，"
            "冻结时框架自动登记成 chunk，`chunk_id` 随返回值给你\n"
            "  - 文本太短不需要切（< 200 字符） → 同上，走 freeze_artifact 那条路\n\n"
            "**关键参数**：\n"
            "  - source 必须是外部 URI 前缀：doi: / arxiv: / https: / http: / pmid: / isbn:\n"
            "  - chunk_size：默认 1500 字符（不是 token）。代码 / 表格用 800-1000；段落文本 1500-2500\n"
            "  - author_concept_ids：填 person concept_id 让所有 chunks 自动挂作者（重要）\n\n"
            "**返回**：`{chunks_created, chunks_merged, chunks: [{id, offset, length, ...}]}`"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "description": "完整文本"},
                "source": {"type": "string", "minLength": 1,
                    "description": "外部 URI。如 'arxiv:2401.12345' / 'doi:10.1234/abc' / 'https://...'"},
                "chunk_size": {"type": "integer", "default": 1500,
                    "minimum": 200, "maximum": 8000},
                "author_concept_ids": {"type": "array", "items": {"type": "string"},
                    "description": "list of person concept_id"},
                "corresponding_author_concept_id": {"type": "string"},
                "created_by_role": {"type": "string", "enum": list(CREATED_BY_ROLES),
                    "default": "agent_auto"},
            },
            "required": ["text", "source"],
        },
        risk_level="low",
    ),
    _kb_ingest,
)


# ─────────────────────────────────────────────────────────────────────────────
# kb_mode3_candidates —— curator 用，每次写入完后跑
# ─────────────────────────────────────────────────────────────────────────────

async def _kb_mode3_candidates(
    state: State,
    target_entity: str,
    target_id: str,
    limit: int = 8,
    **_: Any,
) -> dict:
    """围绕 target 找候选 related KB —— 同 concept / 同 author / 同 type 的近邻。

    供 curator 写 derived.related_to 或 link 决策用。target_entity 的枚举由
    curator_scan 的 parameters_schema 声明（本函数只经它到达）。
    """
    target = state.get_kb_record(target_entity, target_id)
    if target is None:
        return {"status": "error", "error": f"target_id={target_id!r} 找不到"}

    target_concept_ids = set(target.get("concept_ids") or []) | \
                         set(target.get("about_concept_ids") or []) | \
                         set(target.get("author_concept_ids") or [])

    candidates: list[dict] = []
    for entity in ("claims", "experiments", "chunks"):
        for r in state.list_kb(entity):
            if r.get("id") == target_id:
                continue
            r_concepts = set(r.get("concept_ids") or []) | \
                         set(r.get("about_concept_ids") or []) | \
                         set(r.get("author_concept_ids") or [])
            overlap = target_concept_ids & r_concepts
            if overlap:
                candidates.append({
                    "entity": entity,
                    "id": r["id"],
                    "overlap_concept_ids": sorted(overlap),
                    "score": len(overlap),
                    "text": (r.get("claim_text") or r.get("experiment_text")
                             or r.get("text", ""))[:120],
                })

    candidates.sort(key=lambda c: -c["score"])
    return {"status": "success", "count": len(candidates),
            "candidates": candidates[:limit]}

# ─────────────────────────────────────────────────────────────────────────────
# 5 个机械切片工具（dreaming skill 用）
# ─────────────────────────────────────────────────────────────────────────────

# ── Phase G (v0.3.2+): 找 stale dead_end / refuted claim ────────────────────
# 避免 KB 锁死 —— 旧"X 走不通"在不同 scope / 时代下可能变对了，要定期重审。

async def _find_stale_dead_end_or_refuted(
    state: State,
    days: int = 180,
    limit: int = 20,
    **_: Any,
) -> dict:
    """找超过 days 天没 reviewed 的 dead_end / refuted claim。

    设计意图（Phase G）：dead_end 是 cross-project 资产，会被注入 context 影响所有
    后续节点决策；旧 dead_end 可能在新 scope / 新 model 下不再成立，定期推 LLM
    去 review，避免"5 年前 'A 走不通' 永远锁死 KB"。
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    claims = state.list_kb("claims")
    out: list[dict] = []
    for cl in claims:
        ct = cl.get("claim_type")
        status = cl.get("status")
        # dead_end claim：本身 status 可能 validated（被 cross-project 认证过 dead）
        # 或 open；都算"该被定期质疑"
        is_target = (ct == "dead_end") or (status == "refuted")
        if not is_target:
            continue
        last = cl.get("last_reviewed_at") or cl.get("updated_at") or cl.get("created_at") or ""
        if last and last < cutoff_iso:
            out.append({
                "claim_id": cl["id"],
                "claim_text": (cl.get("claim_text") or "")[:160],
                "claim_type": ct,
                "status": status,
                "scope_dimensions": cl.get("scope_dimensions") or {},
                "dont_repeat_reason": (cl.get("dont_repeat_reason") or "")[:200],
                "last_reviewed_at": last,
                "review_history_length": len(cl.get("review_history") or []),
            })
    out.sort(key=lambda r: r["last_reviewed_at"])
    return {
        "status": "success",
        "count": len(out),
        "cutoff_days": days,
        "stale_claims": out[:limit],
        "note": (
            "这些 dead_end / refuted claim 已超 {} 天没人 review。建议挑高影响的 "
            "→ propose status_review，让上游节点重新评估（scope 可能已变 / 技术已进步）。"
        ).format(days),
    }

# 晋升没有「提议」这个工具，也没有项目里的提议 artifact（2026-09-24 删）。
#
# 唯一入口是项目终态扫盘（curator_scan(scan_type='org_promotion_candidates')，或
# 调度器宣称完成时框架自动跑的同一个函数 `core.kb_promotion.offer_to_the_organisation`）：
# 机械车道直落，人批车道进**组织的**待审，由组织管理员在组织页上裁。
#
# 从前这里写一件 `propose_org_promotion` artifact 进项目，等「user inbox 批准」——
# 而 inbox 只认 jsonl 队列，从来没有人能批到它。「扫盘漏了某条」在这个设计下等价于
# 「那条没有知识卡草稿」，正解是 draft_knowledge_card。


# ─────────────────────────────────────────────────────────────────────────────
# get_kb_record —— 按 id 单条精读
# ─────────────────────────────────────────────────────────────────────────────

async def _get_kb_record(
    state: State,
    entity: str,
    kb_id: str,
    **_: Any,
) -> dict:
    # entity 枚举由 parameters_schema 声明（enum=ENTITIES），派发口核一次。
    rec = state.get_kb_record(entity, kb_id)
    if rec is None:
        return {"status": "error",
                "error": f"找不到 {entity}/{kb_id}（已被 supersede 或不存在）"}
    return {"status": "success", "entity": entity, "record": rec}


register_tool(
    ToolDefinition(
        name="get_kb_record",
        replayable_read=True,
        description=(
            "按 id 精读单条 KB record（含 review_history / derived fields / scope_dimensions）。\n\n"
            "Use when：\n"
            "  - search_kb 拿到 id 后想看完整字段（review_history / sources / aliases / sibling_id 等）\n"
            "  - revert 前确认要撤的 entity\n"
            "  - debug curator 是否给 derived 字段填对\n\n"
            "返回：`{status, entity, record}`。record 是 raw KB JSON（含所有字段）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "entity": {"type": "string",
                    "enum": list(ENTITIES),
                    "description": "concepts / claims / experiments / chunks"},
                "kb_id": {"type": "string",
                    "description": "KB record id（如 'claim_abc123'）"},
            },
            "required": ["entity", "kb_id"],
        },
        risk_level="low",
    ),
    _get_kb_record,
)


# ─────────────────────────────────────────────────────────────────────────────
# kb_overview —— KB dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def _kb_overview(
    state: State,
    recent_n: int = 5,
    **_: Any,
) -> dict:
    from collections import Counter
    from shared.lib.kb_schema import ENTITIES as _ENT, CLAIM_TYPES, CLAIM_STATUSES

    out: dict[str, Any] = {"status": "success",
                              "by_entity": {},
                              "claims": {"by_type": {}, "by_status": {}},
                              "recent": {}}

    for ent in _ENT:
        records = state.list_kb(ent)
        out["by_entity"][ent] = len(records)
        # 按 created_at 倒序取最近 recent_n
        try:
            sorted_recs = sorted(records,
                                    key=lambda r: r.get("created_at", ""),
                                    reverse=True)
        except Exception:
            sorted_recs = records[-recent_n:][::-1]
        recent = []
        for r in sorted_recs[:recent_n]:
            recent.append({
                "id": r.get("id"),
                "scope": r.get("scope"),
                "created_at": r.get("created_at"),
                "summary": (r.get("canonical_name")
                              or r.get("claim_text")
                              or r.get("title")
                              or r.get("text", ""))[:120],
            })
        out["recent"][ent] = recent

    # claims 详细分布
    claims = state.list_kb("claims")
    type_counter = Counter(r.get("claim_type", "?") for r in claims)
    status_counter = Counter(r.get("status", "?") for r in claims)
    out["claims"]["by_type"] = {t: type_counter.get(t, 0) for t in CLAIM_TYPES
                                  if type_counter.get(t, 0) > 0}
    out["claims"]["by_status"] = {s: status_counter.get(s, 0) for s in CLAIM_STATUSES
                                    if status_counter.get(s, 0) > 0}

    # frozen artifacts → pending chunks

    return out


# kb_overview 的工具注册已删（2026-08-21）：它就是 `search_kb` 不传 entity_type
# 的那一档。函数保留作内部实现 —— 删的是**模型面上的第二个名字**，不是能力。


async def _kb_provenance(state: Any, kb_id: str, **_: Any) -> dict:
    from core.kb_provenance import kb_provenance

    return kb_provenance(state, str(kb_id or ""))


register_tool(
    ToolDefinition(
        name="kb_provenance",
        replayable_read=True,
        description=(
            "走一条 KB 条目的完整出处链，返回**骨架卡片**（每跳 id + 一句摘要）。\n\n"
            "一条 claim 是「一句可检索的断言 + 一张通往完整记录的索引卡」——"
            "信任不住在句子里，住在可走查的链里：\n"
            "  org 知识卡 → promoted_from → 项目 claim → sources → chunk\n"
            "              → 冻结产物（信封 + 哈希链）→ 产出它的那次 run\n"
            "              → prereg：当时承诺了什么判据\n\n"
            "**Use when**：\n"
            "  - 要引用一条 org 结论进正文之前，先确认它走得回证据\n"
            "  - reviewer 质疑某条断言的来源\n"
            "  - 发现两条 org 卡矛盾，要看各自的证据强度\n\n"
            "**断链会如实报告**（`intact: false` + `broken` 列表），不静默跳过 —— "
            "一条走不到证据的 org 结论正是最该被发现的：它意味着源项目归档后"
            "这条「真理」已经悬空了。\n\n"
            "**有界**：只给骨架，不展开全文。要全文用 read_artifact / get_kb_record。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "string",
                    "description": "claim_id 或 chunk_id"},
            },
            "required": ["kb_id"],
        },
        risk_level="low",
    ),
    _kb_provenance,
)


# ─────────────────────────────────────────────────────────────────────────────
# org 维护面（P4）：dreaming 巡检 + 正典写入
#
# 这两个是 curator 的 org 维护动作，和项目内的 dreaming 不是一回事：
# 项目 dreaming 管本项目的记忆整理，org dreaming 管**跨项目的知识治理**
# （同一断言的两次独立发现要合、相互矛盾的结论要摆出来、老结论要复查）。
# ─────────────────────────────────────────────────────────────────────────────


async def _org_dreaming(state: Any, **_: Any) -> dict:
    from datetime import datetime, timezone

    from core.org_dreaming import run_dreaming

    return run_dreaming(state, now_iso=datetime.now(timezone.utc).isoformat())


register_tool(
    ToolDefinition(
        name="org_dreaming",
        replayable_read=True,
        description=(
            "跑一轮 org 层知识治理巡检。**有界的 checklist，不是开放式治理** ——"
            "逐项过完即收工。\n\n"
            "五道作业：\n"
            "  1. 同一断言的两次独立发现 → 提议合并（复现记数累加、适用范围取并集）\n"
            "  2. 相互矛盾的结论 → **摆出来，不替你裁决**。矛盾是开放问题，"
            "不是错误；哪条对要靠新证据，不靠谁的 confidence 高\n"
            "  3. 老结论复查（久未被引用 / 领域已变）\n"
            "  4. 范例老化（一直被引的范例会把风格钉死）\n"
            "  5. 该刷新的活综述\n\n"
            "**只提议不直落**：判「是不是同一条断言」「这两条是不是真矛盾」"
            "需要语义判断，机械层给不出。\n\n"
            "Use when：curator 的 org 维护轮次。不要在项目跑到一半时调 ——"
            "org 治理和项目进度无关。"
        ),
        parameters_schema={"type": "object", "properties": {}},
        risk_level="low",
    ),
    _org_dreaming,
)


async def _write_org_canon(state: Any, domain: str, body: str,
                           absorbed_ids: list[str] | None = None, **_: Any) -> dict:
    from datetime import datetime, timezone

    from core.org_canon import write_canon

    return write_canon(state, domain=str(domain or ""), body=str(body or ""),
                       absorbed_ids=list(absorbed_ids or []),
                       at=datetime.now(timezone.utc).isoformat())


register_tool(
    ToolDefinition(
        name="write_org_canon",
        description=(
            "落一版领域**活综述**。org 知识主要通过它被阅读 —— 新项目开题不该读"
            "400 条原子 claim，该读 2 页领域综述、再按需下钻到被引条目。\n\n"
            "这不是新发明，是科学自己组织知识的方式：论文 → 综述 → 教科书。\n\n"
            "**唯一写者是 curator**。多写者的正典不是正典，是又一份会分叉的抄件。\n\n"
            "**目标 1–3 页**。写不下说明该拆子域 —— 不是把综述写成第二个账本。\n\n"
            "`absorbed_ids` 记的是「这一版吸收了哪些卡片」：被吸收的卡片在开题注入面"
            "降权（**不删除**，账本永远走得回去），把注入面的位置让给综述没讲到的"
            "新东西。\n\n"
            "先用 org_dreaming 拿 canon_refresh 作业，它会给出待吸收清单。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "domain": {"type": "string",
                    "description": "领域名。和知识卡上的 domain 字段对齐 —— 对不上就命不中"},
                "body": {"type": "string",
                    "description": "综述正文（markdown）。讲清脉络、争论在哪、共识到哪一步，"
                                   "开放问题单列一节。引用被吸收条目的 id"},
                "absorbed_ids": {"type": "array", "items": {"type": "string"},
                    "description": "这一版吸收了哪些 org 条目"},
            },
            "required": ["domain", "body"],
        },
        risk_level="medium",
    ),
    _write_org_canon,
)


# ─────────────────────────────────────────────────────────────────────────────
# Phase I (KB 复利机械化)：自动化两个最容易"owner 自觉依赖"的提议
#
#   find_org_promotion_candidates   —— 扫"已该升 org 的 project claim"，可 auto-propose
#   find_synthesis_candidates       —— 扫"同主 concept 已 ≥ N 条 claim 但没人写
#                                       synthesis"的洼地，可 auto-propose
#
# 两个工具默认 auto_propose=True；curator Mode 2 dreaming 必跑。设计原则：
#   - 这俩动作单调有界（不会写真 KB，只写 propose），错了由 user 在 inbox 拒
#   - dedup：已有同 target 的 pending propose → 跳过
# ─────────────────────────────────────────────────────────────────────────────

# 注：原来这里有一张"哪些 claim_type 值得自动 promote"的分档表（10 类）。
# 已删 —— 类型收敛到 5 类，且晋升不再按类型阈值筛：判据是**项目到了终态**，
# 由三项机械检查 + 人工批次决定（见 core/kb_promotion.py）。
# `_ELIGIBLE_FOR_ORG_PROMOTION` 已删：哪种 claim 晋升成哪种 org 条目，
# 声明在 core.kb_promotion.KIND_BY_CLAIM_TYPE（一处声明，扫盘与落地同读）。


async def _find_org_promotion_candidates(
    state: State,
    auto_propose: bool = True,
    limit: int = 50,
    drafts: dict | None = None,
    **_: Any,
) -> dict:
    """终态晋升扫盘 —— 判据与落地都在 core.kb_promotion，这里只做工具面适配。

    `auto_propose=True`（默认）：够格的**真的交出去** —— 机械车道直落 org 层，人批
    车道进组织的待审（管理员在组织页上裁，agent 碰不到那个队列）。`False` 只列清单。

    ## 删掉的那套判据

    旧实现的门是 `replication_count ≥ 3 且 confidence ≥ 0.85`，且随时可调。
    在两层重构之后它**不可能被满足**：复现记数只有跨项目 dreaming 归并时才
    累加，而首个做出这条结论的项目永远是 replication=1 —— 于是这道门要么
    永不放行，要么被调参调到形同虚设。它问的也是错的问题（"这条够不够硬"），
    真正该问的是"到终态了吗 / 证据冻了吗 / 写得出去项目化版本吗"。

    新判据（三查 + 双车道）见 core.kb_promotion 与 RFC §15。
    复现记数的正确位置是**晋升之后**：dreaming 归并同一断言时累加，
    作为 org 卡的 confidence_basis，而不是入场券。
    """
    from core import kb_promotion as kp

    out = kp.offer_to_the_organisation(
        state, project_id=str(state.project_id or ""), at=now_iso(),
        send=bool(auto_propose), drafts=drafts or {})
    if not out["terminal"]:
        return {
            "status": "success",
            "terminal": False,
            "reason": out["reason"],
            "candidates": [],
            "landed": [],
            "proposals_created": 0,
            "hint": ("晋升只在项目终态发生 —— 跨项目复利的判据"
                     "（结论站没站住、证据冻没冻、能否泛化）在过程中不存在。"),
        }

    def _view(c: dict) -> dict:
        kind = "chunks" if c["kind"] == kp.KIND_BIBLIO else "claims"
        rec = state.get_kb_record(kind, c["source_id"]) or {}
        return {**{k: c[k] for k in ("source_id", "kind", "lane", "evidence_closure",
                                      "has_draft")},
                "text": (rec.get("claim_text") or rec.get("text") or "")[:200]}

    # 这一次新交出去的；已进组织的、已在待审里的另列（见下），不重复算候选。
    handled = {a["source_id"] for a in out["already"]}
    eligible = [c for c in out["candidates"] if c["eligible"] and c["source_id"] not in handled]
    mechanical = [_view(c) for c in eligible if c["lane"] == kp.LANE_MECHANICAL][:limit]
    human = [_view(c) for c in eligible if c["lane"] == kp.LANE_HUMAN][:limit]
    return {
        "status": "success",
        "terminal": True,
        "mechanical": mechanical,          # 直落车道：无需人批
        "human_batch": human,              # 人批车道：进组织的待审
        "candidates": mechanical + human,
        "landed": out["landed"],           # 这一次直落进 org 的
        "blocked": out["blocked"],
        "skipped_already_pending": sorted(
            a["source_id"] for a in out["already"] if a.get("awaiting_review")),
        "already_in_org": sorted(a["source_id"] for a in out["already"] if a.get("org_id")),
        "proposals_created": len(out["queued"]),
        "note": ("人批车道的候选进的是**组织的待审**，由组织管理员裁；"
                 "这里没有要你 resolve 的东西。"),
    }


def _read_pending_synthesis_proposal_concepts(state: State) -> set[str]:
    """读 kb_proposals.jsonl 找已有 pending kb_synthesis_candidate 的 concept_id。

    项目层 + org 共享层都要查：scope=org 的 concept 的 proposal 现在路由进
    org 共享队列（见 proposals._kb_proposal_write_path），只查本项目文件会
    看不到，导致不同项目对同一个 org concept 反复重复提议（v10c dogfood 实测
    的根因之一）。
    """
    from shared.tools.library.proposals import (
        _kb_proposals_path, _org_kb_proposals_path,
    )
    out: set[str] = set()
    for path in (_kb_proposals_path(state), _org_kb_proposals_path()):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") != "pending":
                continue
            if rec.get("proposal_type") != "kb_synthesis_candidate":
                continue
            # target_id 就是 concept_id（按下面 propose 的约定）
            if rec.get("target_entity") == "concepts" and rec.get("target_id"):
                out.add(rec["target_id"])
    return out


# v0.6 K3 fix: 只有"科学发现性 concept"才适合写 synthesis 整合。
# - phenomenon：可观测行为 → 多 finding 聚合写 mechanism / generalization
# - theory：解释框架 → 多支撑/反驳整合
# - method：算法 → 多次使用经验聚合 best-practice / pitfall
# - task：形式化研究问题 → 不同方法在同 task 上的对比整合
#
# NOT eligible（写 synthesis 没意义）：
# - metric (UPC, MAE 度量本身不需要整合)
# - tool (软件实现不是科学 concept)
# - dataset (数据集本身)
# - domain (太宽泛)
# - person / group (不是科学 concept)
#
# v3 dogfood 实测：不加 filter 时，find_synthesis_candidates 提议 27 条，
# 其中 ~19 条是 metric / parameter / dataset 的"对它写 synthesis 没意义"
# proposals，淹没真正 8 条有价值的 phenomenon / method synthesis candidates。
_ELIGIBLE_CONCEPT_TYPES_FOR_SYNTHESIS = frozenset(
    {"phenomenon", "theory", "method", "task"}
)


async def _find_synthesis_candidates(
    state: State,
    min_claims_per_concept: int = 3,
    eligible_concept_types: list[str] | None = None,
    auto_propose: bool = True,
    limit: int = 20,
    **_: Any,
) -> dict:
    """扫 concept：被 ≥ N 条**非 synthesis** claim anchor 但 KB 还没任何
    synthesis claim 覆盖 → 自动 propose `kb_synthesis_candidate`。

    Dedup：concept 已有 pending kb_synthesis_candidate proposal → 跳过。

    synthesis 覆盖判定：若有 claim_type='synthesis' claim 的 concept_ids
    或 sources 命中该 concept 或其任一 candidate claim_id → 视为已覆盖。

    v0.6 加 concept_type filter：默认只 phenomenon/theory/method/task
    （metric/tool/dataset/domain/person/group 写 synthesis 没意义）。
    """
    eligible = (frozenset(eligible_concept_types)
                if eligible_concept_types
                else _ELIGIBLE_CONCEPT_TYPES_FOR_SYNTHESIS)
    claims = state.list_kb("claims")
    concepts = state.list_kb("concepts")
    concept_by_id = {c["id"]: c for c in concepts}

    # concept_id → [(claim_id, claim_text)]，只算非 synthesis 的 anchor claim
    by_concept: dict[str, list[tuple[str, str]]] = {}
    # 已有 synthesis claim 覆盖了哪些 concept / source claim
    synthesis_covered_concepts: set[str] = set()
    synthesis_source_claims: set[str] = set()

    for cl in claims:
        cid = cl["id"]
        ctype = cl.get("claim_type")
        concept_ids = cl.get("concept_ids") or []
        if ctype == "synthesis":
            for c in concept_ids:
                synthesis_covered_concepts.add(c)
            for src in cl.get("sources") or []:
                if isinstance(src, str) and src.startswith("claim_"):
                    synthesis_source_claims.add(src)
            continue
        # 非 synthesis claim：登记到其 anchor concept
        for c in concept_ids:
            by_concept.setdefault(c, []).append(
                (cid, (cl.get("claim_text") or "")[:160]),
            )

    pending_targets = _read_pending_synthesis_proposal_concepts(state)

    candidates: list[dict] = []
    skipped_by_type: dict[str, int] = {}
    for concept_id, anchors in by_concept.items():
        if len(anchors) < min_claims_per_concept:
            continue
        concept = concept_by_id.get(concept_id) or {}
        # v0.6 filter：只对适合写 synthesis 的 concept_type propose
        ct = concept.get("concept_type")
        if ct not in eligible:
            skipped_by_type[ct or "?"] = skipped_by_type.get(ct or "?", 0) + 1
            continue
        if concept_id in synthesis_covered_concepts:
            continue
        # 若现有 synthesis claim 已 source 引用了这批 claim 的多数 → 也算覆盖
        anchor_ids = {a[0] for a in anchors}
        overlap = anchor_ids & synthesis_source_claims
        if len(overlap) >= max(2, min_claims_per_concept // 2):
            continue
        if concept_id in pending_targets:
            continue
        candidates.append({
            "concept_id": concept_id,
            "concept_canonical_name": concept.get("canonical_name") or "",
            "concept_type": ct,
            "n_anchor_claims": len(anchors),
            "candidate_source_claim_ids": [a[0] for a in anchors[:20]],
            "sample_claim_texts": [a[1] for a in anchors[:5]],
        })

    candidates.sort(key=lambda r: r["n_anchor_claims"], reverse=True)
    candidates = candidates[:limit]

    proposals_created = 0
    propose_errors: list[dict] = []
    if auto_propose and candidates:
        from core.tool_registry import execute as execute_tool
        for cand in candidates:
            reasoning = (
                f"机械扫描：concept {cand['concept_id']} "
                f"({cand['concept_canonical_name']!r}) 被 "
                f"{cand['n_anchor_claims']} 条非 synthesis claim anchor，"
                f"但 KB 还没任何 synthesis claim 覆盖；该写 synthesis 整合"
                f"跨条目 pattern。由 find_synthesis_candidates 自动 propose。"
            )
            res = await execute_tool(
                "propose", state,
                proposal_type="kb_synthesis_candidate",
                target_entity="concepts",
                target_id=cand["concept_id"],
                proposed_action="create_claim(claim_type='synthesis')",
                reasoning=reasoning,
                confidence=0.6,
                extra={
                    "candidate_source_claim_ids":
                        cand["candidate_source_claim_ids"],
                    "n_anchor_claims": cand["n_anchor_claims"],
                    "auto_detected_by": "find_synthesis_candidates",
                },
            )
            if res.get("status") == "success":
                proposals_created += 1
                cand["proposed"] = True
            else:
                propose_errors.append({"concept_id": cand["concept_id"],
                                        "error": res.get("error")})

    return {
        "status": "success",
        "count": len(candidates),
        "candidates": candidates,
        "skipped_already_pending": sorted(pending_targets),
        "skipped_by_concept_type": skipped_by_type,
        "proposals_created": proposals_created,
        "propose_errors": propose_errors,
        "filters": {
            "min_claims_per_concept": min_claims_per_concept,
            "eligible_concept_types": sorted(eligible),
        },
    }

# ─────────────────────────────────────────────────────────────────────────────
# v1.5 refine: curator_scan —— 合并 4 个 curator-scan 工具的统一入口
# ─────────────────────────────────────────────────────────────────────────────

_SCAN_TYPES = (
    "stale_dead_end",                # → _find_stale_dead_end_or_refuted
    "org_promotion_candidates",       # → _find_org_promotion_candidates
    "synthesis_candidates",           # → _find_synthesis_candidates
    "mode3",                          # → _kb_mode3_candidates
)


async def _curator_scan(
    state: State,
    scan_type: str,
    limit: int = 20,
    # stale_dead_end
    days: int = 180,
    # org_promotion_candidates（终态晋升扫盘）
    drafts: dict | None = None,
    # synthesis_candidates
    min_claims_per_concept: int = 3,
    eligible_concept_types: list[str] | None = None,
    # 两个 candidates 工具共享
    auto_propose: bool = True,
    # mode3
    target_entity: str = "",
    target_id: str = "",
    **_: Any,
) -> dict:
    """curator 多种扫描的统一入口。scan_type 决定走哪个内部 scanner。

    scan_type ∈:
      - "stale_dead_end"            扫 dead_end/refuted/superseded claim 是否过期
      - "org_promotion_candidates"  扫 project-scope claim 是否够格升 org
      - "synthesis_candidates"      扫 concept 周围是否有合成机会
      - "mode3"                     Mode 3 review 候选（需 target_entity + target_id）

    各 scan 只用相关参数；其它参数被忽略。scan_type / target_entity 的枚举由
    parameters_schema 声明、派发口核一次。
    """
    if scan_type == "stale_dead_end":
        return await _find_stale_dead_end_or_refuted(
            state, days=days, limit=limit,
        )

    if scan_type == "org_promotion_candidates":
        return await _find_org_promotion_candidates(
            state, auto_propose=auto_propose, limit=limit, drafts=drafts,
        )

    if scan_type == "synthesis_candidates":
        return await _find_synthesis_candidates(
            state,
            min_claims_per_concept=min_claims_per_concept,
            eligible_concept_types=eligible_concept_types,
            auto_propose=auto_propose,
            limit=limit,
        )

    # scan_type == "mode3"
    if not target_entity or not target_id:
        return {
            "status": "error",
            "error": "scan_type='mode3' 需要 target_entity + target_id（如 'claims' / 'claim_abc123'）",
        }
    return await _kb_mode3_candidates(
        state,
        target_entity=target_entity,
        target_id=target_id,
        limit=limit,
    )


_CURATOR_SCAN_DESC = (
    "**curator 专用 KB 扫描统一入口**。1 个工具 4 种 scan_type，把之前的 "
    "8+个 find_* / kb_mode3_candidates 合到这里。\n\n"
    "**scan_type**:\n"
    "  - `stale_dead_end`: 扫 dead_end/refuted/superseded claim 过期（默认 180 天）\n"
    "  - `org_promotion_candidates`: 终态晋升扫盘 —— 判据是三查（项目到终态 / "
    "证据来源已冻结 / 去项目化知识卡写得出来），不是复现数或 confidence 阈值。"
    "auto_propose=True 自动 propose\n"
    "  - `synthesis_candidates`: 扫 concept 周围 ≥ N 条 claim 该不该 synthesize\n"
    "  - `mode3`: Mode 3 review 候选。**需要** target_entity + target_id\n\n"
    "**何时调**: curator 节点 dreaming / Mode 2 / Mode 3；别节点别用。"
)


register_tool(
    ToolDefinition(
        name="curator_scan",
        description=_CURATOR_SCAN_DESC,
        parameters_schema={
            "type": "object",
            "properties": {
                "scan_type": {
                    "type": "string",
                    "enum": list(_SCAN_TYPES),
                    "description": "选哪种 scan",
                },
                "limit": {"type": "integer", "default": 20,
                          "minimum": 1, "maximum": 100},
                "days": {"type": "integer", "default": 180,
                          "description": "stale_dead_end: 过期阈值天数"},
                # 字段清单从常量现算，不手抄 —— 手抄的那份会和 KNOWLEDGE_CARD_FIELDS
                # 各自演化，且分叉时两边都不报错（本次实测：抄件漏了 domain 和
                # evidence，模型照抄件写卡就会被 deprojectified 拦下，而拦它的
                # 字段它压根没见过）。
                "drafts": {"type": "object",
                          "description": (
                              "org_promotion: 去项目化知识卡草稿 "
                              "{claim_id: {" + ", ".join(_CARD_FIELDS) + "}}。"
                              "domain 从注册表选：arXiv 分类（如 cond-mat.stat-mech /"
                              " physics.comp-ph）或 `<骨架父>/<新叶名>` 挂本地叶"
                              "（随人批一起注册）；自由文本会被拒并列出最近匹配。"
                              "why 是机制 —— 缺它的东西是数据不是知识。"
                              "没给草稿的候选仍会列出，但 deprojectified 不通过 ——"
                              "让'还差什么'显式可见")},
                "min_claims_per_concept": {"type": "integer", "default": 3,
                          "description": "synthesis: 触发的最少 claim 数"},
                "eligible_concept_types": {"type": "array", "items": {"type": "string"},
                          "description": "synthesis: concept_type 白名单"},
                "auto_propose": {"type": "boolean", "default": True,
                          "description": "org_promotion/synthesis: 找到候选自动 propose"},
                "target_entity": {"type": "string", "enum": list(ENTITIES),
                          "description": "mode3: 'claims' / 'concepts' / 'experiments' / 'chunks'"},
                "target_id": {"type": "string",
                          "description": "mode3: 目标 entity id"},
            },
            "required": ["scan_type"],
        },
        risk_level="low",
    ),
    _curator_scan,
)


# ═══════════════════════════════════════════════════════════════════════════
# v3.5 承重层（load-bearing capital）—— curator 的组合管理动作
# ═══════════════════════════════════════════════════════════════════════════
#
# 为什么要有：两次 E2E 的 KB 各积累 40-50 条 claim，零复用、零自产发现，
# curator 却花了 5000 万 token 管理它们 —— 治理成本超过被治理知识的价值。
# 根因是"什么都记"。承重层用**预算制造稀缺**：少而硬的决策资本，满额必须先
# 降级一条才能晋升新的。承重层是设计时 briefing 的注入源（见 core/recall）。

async def _draft_knowledge_card(
    state: State,
    claim_id: str,
    domain: str = "",
    statement: str = "",
    applicability: dict | None = None,
    why: str = "",
    practice: str = "",
    confidence_basis: str = "",
    evidence: list[str] | None = None,
    trigger: str = "",
    cost_when_hit: str = "",
    discard: bool = False,
    reason: str = "",
    **_: Any,
) -> dict:
    """给一条 project claim 起草知识卡（或 discard=True 撤掉草稿）。

    起草发生在**上下文热**的时候（项目进行中），不是终态 —— "为什么当时
    这么判"在终态已经凉了，那正是真实数据回放里 132/132 缺 why 的原因之一。
    草稿写在 claim 上，终态扫盘自己捡起来。

    草稿**照落，缺什么标什么**（判决拆除 O8）：够不够格 / 完不完整是晋升人批
    要看的事实，不是起草时替人下的判决 —— 拒掉的草稿连候选都当不成，而标了
    `source_warnings` / `missing_trigger` / `deprojectified:false` 的草稿会在
    终态扫盘的 blocked 清单里把"还差什么"显式列出来。草稿位不设配额：真实约束
    是 briefing 注入窗口，在注入侧按预算截断（core/recall）。
    """
    from shared.lib.kb_schema import card_draft_source_errors, normalize_claim_type
    from core.kb_promotion import KNOWLEDGE_CARD_FIELDS, check_deprojectified
    from core.domain_registry import validate_domain

    rec = state.get_kb_record("claims", claim_id)
    if rec is None:
        return {"status": "error", "error": f"claim {claim_id!r} 不存在"}
    if rec.get("scope") != "project":
        return {"status": "error", "code": "not_a_project_claim",
                "error": "只有 project claim 能起草知识卡。org 条目已经是卡了。"}

    if discard:
        # reason 非空由 parameters_schema 声明（minLength:1）。
        if not rec.get("card_draft"):
            # 撤一个本来就没有的草稿：幂等成功比报错更真——目标状态已达成。
            return {"status": "success", "claim_id": claim_id, "card_draft": None,
                    "already_absent": True,
                    "note": f"claim {claim_id!r} 本来就没有草稿，无需撤。"}
        hist = list(rec.get("card_draft_history") or [])
        hist.append({"action": "discard", "at": now_iso(), "reason": reason.strip()})
        state.patch_derived("claims", claim_id,
                            {"card_draft": None, "card_draft_history": hist},
                            curator_run_id=state.run_id)
        return {"status": "success", "claim_id": claim_id, "card_draft": None,
                "note": "草稿已撤（claim 内容保留，只是不再进 briefing / 终态候选）。"}

    # ── 起草 ──────────────────────────────────────────────────────────────
    draft: dict[str, Any] = {
        "domain": domain, "statement": statement or rec.get("claim_text") or "",
        "applicability": applicability or rec.get("scope_dimensions") or {},
        "why": why, "practice": practice,
        "confidence_basis": confidence_basis, "evidence": evidence or []}
    warnings: list[str] = []

    src_errors = card_draft_source_errors(rec)
    if src_errors:
        draft["source_warnings"] = src_errors
        warnings += src_errors

    if normalize_claim_type(str(rec.get("claim_type") or "")) == "dead_end":
        # 死路卡缺 trigger 就**送不出去**：reviewer 红旗按它匹配计划正文，
        # 没有它这张卡在新项目面前一次都不会响。撞上死路的当时最清楚
        # 什么样的计划会撞上它 —— 所以缺了要显眼地标出来，而不是拒。
        if (trigger or "").strip():
            draft["trigger"] = trigger.strip()
        else:
            draft["missing_trigger"] = True
            warnings.append(
                "dead_end 卡缺 trigger（什么样的计划会撞上这条死路，reviewer 按它"
                "机械比对计划正文，例：'团簇算法 温度点 尺寸 预算 小时'）——"
                "没有它这张卡在新项目面前一次都不会响；也建议补 cost_when_hit。")
    if (cost_when_hit or "").strip():
        draft["cost_when_hit"] = cost_when_hit.strip()

    chk = check_deprojectified(draft)
    draft["deprojectified"] = bool(chk.passed)
    if not chk.passed:
        draft["deprojectified_cause"] = chk.cause or "card_incomplete"
        draft["deprojectified_reasons"] = [chk.reason]
        warnings.append(chk.reason)

    hist = list(rec.get("card_draft_history") or [])
    hist.append({"action": "draft", "at": now_iso(), "domain": domain,
                 "deprojectified": bool(chk.passed)})
    state.patch_derived("claims", claim_id,
                        {"card_draft": draft, "card_draft_history": hist},
                        curator_run_id=state.run_id)
    dv = validate_domain(state, domain)
    out: dict[str, Any] = {
        "status": "success", "claim_id": claim_id, "domain": domain,
        "domain_status": dv.status,
        "deprojectified": bool(chk.passed),
        "note": ("草稿已落在 claim 上。它会进设计期 briefing，并在项目终态"
                 "自动成为晋升候选（不必到时再传一遍）。"
                 + ("　域是新叶，晋升人批时一并注册。" if dv.status == "registrable" else "")),
    }
    if src_errors:
        out["source_warnings"] = src_errors
    if draft.get("missing_trigger"):
        out["missing_trigger"] = True
    if not chk.passed:
        out["code"] = chk.cause or "card_incomplete"
        out["deprojectified_reasons"] = [chk.reason]
        out["required_fields"] = list(KNOWLEDGE_CARD_FIELDS)
        out["domain_suggestions"] = list(dv.suggestions)
    if warnings:
        out["note"] += (
            "\n如实标在卡上、终态晋升前要补齐（否则扫盘会把它列进 blocked）：\n- "
            + "\n- ".join(warnings))
    return out


register_tool(
    ToolDefinition(
        name="draft_knowledge_card",
        description=(
            "给一条 project claim 起草**去项目化知识卡**（discard=True 撤草稿）。\n\n"
            "**为什么在项目进行中起草，而不是终态**：卡片要写 why（机制）和 "
            "practice（据此该怎么做）—— 这两样在你刚做完那个实验时最清楚，"
            "到终态已经凉了。真实数据回放实测：132 条候选 132 条缺 why。\n\n"
            "**草稿的三重身份**（原来是三套机制，现在是一件事）：\n"
            "  1. 它是本项目的**决策依据** —— 注入 hypothesis/experiment 的"
            "设计期 briefing（注入窗口有预算，只放最近的几张，其余只报个数）\n"
            "  2. 它是终态晋升的**候选卡** —— 扫盘自动捡起，不必到时再传一遍\n"
            "  3. 它是「这条到底想清楚没有」的**自检** —— 写不出 why 的，多半还没到火候\n\n"
            "**字段**：domain（从注册表选，见下）/ statement / applicability / "
            "why / practice / confidence_basis / evidence。statement 和 applicability "
            "不传则从 claim 继承。\n\n"
            "**草稿照落，缺什么标什么**：卡不完整 / 域不在注册表 / 正文含项目指代 → "
            "deprojectified=false 并列出原因；文献转述或没关联实验的自产 empirical → "
            "source_warnings；dead_end 缺 trigger → missing_trigger。这些在终态扫盘会"
            "列进 blocked，晋升前要补齐。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "claim_id": {"type": "string"},
                "domain": {"type": "string",
                    "description": "从域注册表选：arXiv 分类（如 cond-mat.stat-mech）"
                                   "或 `<骨架父>/<新叶名>` 挂本地叶。自由文本会被拒"
                                   "并列出最近匹配"},
                "statement": {"type": "string",
                    "description": "去项目化的结论正文。不传则用 claim_text"},
                "applicability": {"type": "object",
                    "description": "什么条件下成立/不成立。项目参数（seed/run_id）"
                                   "必须改写成适用条件，不能原样带走"},
                "why": {"type": "string",
                    "description": "机制或解释。**缺它的东西是数据不是知识**"},
                "practice": {"type": "string",
                    "description": "据此该怎么做 / 别怎么做 —— 这条结论会改变"
                                   "未来的哪类选择"},
                "confidence_basis": {"type": "string",
                    "description": "凭什么信（证据强度、复现情况）"},
                "evidence": {"type": "array", "items": {"type": "string"},
                    "description": "证据链入口（chunk_id / 外部锚）"},
                "trigger": {"type": "string",
                    "description": "(dead_end 卡送出去的前提，缺则标 missing_trigger) "
                                   "什么样的计划会撞上这条死路 —— reviewer 按它机械"
                                   "比对计划正文。写实词，别写整句"},
                "cost_when_hit": {"type": "string",
                    "description": "(dead_end 推荐) 上次撞上的代价，如 '8.85 小时算力'"},
                "discard": {"type": "boolean", "description": "true=撤掉草稿"},
                "reason": {"type": "string", "minLength": 1,
                    "description": "撤草稿时必填：非空，说清为什么撤"},
            },
            "required": ["claim_id"],
        },
        risk_level="low",
    ),
    _draft_knowledge_card,
)


async def _propose_org_correction(
    state: State,
    org_id: str,
    verdict: str,
    reason: str,
    evidence_ids: list[str] | None = None,
    superseded_by: str = "",
    **_: Any,
) -> dict:
    """「本组已知」里的一条错了 → 交给组织的管理员裁（`core.org_corrections`）。

    证据必须是**这个项目里**查得到的东西：组织的知识只被证据改，不被看法改。
    """
    from core import org_corrections
    from core.paths import home

    project_id = str(getattr(state, "project_id", "") or "")
    if not project_id:
        return {"status": "error", "code": "no_project",
                "error": "更正要带着项目里的证据提 —— 这一轮不在任何项目里。"}
    evidence = [str(e).strip() for e in (evidence_ids or []) if str(e).strip()]
    if not evidence:
        return {"status": "error", "code": "evidence_required",
                "error": "evidence_ids 至少一条：组织的知识只被证据改。"
                         "给这个项目里支撑你判断的 claim / chunk / 冻结产物的 id。"}
    missing = [e for e in evidence if not _found_in_this_project(state, e)]
    if missing:
        return {"status": "error", "code": "evidence_not_found",
                "error": f"这个项目里找不到这些证据：{missing}。"
                         "只能引用本项目 KB 里的 claim / chunk，或本项目的产物 id。"}
    done = org_corrections.propose(
        state, org_id, verdict=verdict, reason=reason,
        by=f"agent:{getattr(state, 'node_type', '') or 'unknown'}", at=now_iso(),
        origin=org_corrections.ORIGIN_AGENT, project_id=project_id, evidence=evidence,
        source_home=str(home()), superseded_by=superseded_by)
    if done.get("status") != "success":
        return done
    if done.get("already"):
        return {"status": "success", "already_pending": True,
                "note": "这个项目对这一条已经有一条更正在等管理员裁，不重复提。"}
    return {"status": "success", "proposal_id": done["proposal"]["id"],
            "note": ("已交给组织的管理员裁。裁之前「本组已知」里这一条照旧送达；"
                     "你自己的结论照常按你的证据走，不必等它。")}


def _found_in_this_project(state: State, ref: str) -> bool:
    for entity in ("claims", "chunks"):
        try:
            rec = state.get_kb_record(entity, ref)
        except Exception:
            rec = None
        if rec is not None:
            return rec.get("scope") == "project"
    try:
        return state.read_artifact(ref) is not None
    except Exception:
        return False


register_tool(
    ToolDefinition(
        name="propose_org_correction",
        description=(
            "「本组已知」（开局注入的组织知识，方括号里是 id）里的一条，被你**手里的证据**"
            "表明不成立（verdict=refuted）或已有更好的说法（verdict=superseded）→ 交给"
            "组织的管理员裁。\n\n"
            "组织的知识**不删**，只被推翻或取代；裁定之后它不再送给新项目，组织页上"
            "标着为什么。裁之前它照旧送达 —— 你自己的结论照常按你的证据走，不必等。\n\n"
            "只在有证据时用：`evidence_ids` 必须是本项目 KB 里的 claim / chunk 或本项目"
            "产物的 id。只是「和我的课题不一样」不算错 —— 那是开放问题，记在项目里。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "org_id": {"type": "string", "description": "「本组已知」里那一条的 id"},
                "verdict": {"type": "string", "enum": ["refuted", "superseded"]},
                "reason": {"type": "string", "minLength": 1,
                    "description": "它错在哪 / 更好的说法是什么 —— 管理员读的就是这句"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                    "description": "本项目里支撑这个判断的 claim / chunk / 产物 id"},
                "superseded_by": {"type": "string",
                    "description": "(可选) 取代它的那条组织知识的 id"},
            },
            "required": ["org_id", "verdict", "reason", "evidence_ids"],
        },
        risk_level="low",
    ),
    _propose_org_correction,
)


# ═══════════════════════════════════════════════════════════════════════════
# v3.8 引用反查：KB 里有真标识符，别再拿散文当参考文献
#
# E2E-3 审稿发现：论文 9 条参考文献全是 `@misc` + `eprint = {arXiv preprint}`，
# 无 arXiv ID、无 DOI、无 venue，作者一律 "X, Y. and others" —— 一条都无法核对，
# 呈现效果与编造不可区分。
#
# 但**标识符从来没丢**。排查到最后：literature 节点真做了检索（openalex /
# semantic_scholar / arxiv 共 18 次），`kb_ingest` 也真用了 `arxiv:2604.23577`
# 这样的外部 URI 入库 —— 8 条 chunk 全带真 ID，只不过落在 **org 层**（论文类知识
# 跨项目复用，落 org 是对的）。我第一次排查只看了 project 层的 kb_chunks.jsonl
# 就断言"丢光了"，错了。
#
# 真正的缺口在**取**这一侧：bib 是照着 survey_report 的散文正文生成的
# （bib 条目的 note 白纸黑字写着 `Referenced via survey_report`），而 survey 正文
# 本身不带 ID。KB 里躺着真 ID，没人去查。
#
# 这个工具补的就是"去查"这一步：给一组 claim / chunk id，反查它们的外部标识符。
# 生成 bib 的节点拿到之后能填出正经条目。工具在框架层，节点怎么用是节点的事。

_EXTERNAL_URI_PREFIXES = ("arxiv:", "doi:", "pmid:", "isbn:", "http://", "https://")


def _is_external_uri(s: str) -> bool:
    return isinstance(s, str) and s.strip().lower().startswith(_EXTERNAL_URI_PREFIXES)


def _bibtex_hint(uri: str) -> dict:
    """外部 URI → bibtex 该往哪个字段填。避免每个节点各自猜一遍。"""
    u = uri.strip()
    low = u.lower()
    if low.startswith("arxiv:"):
        return {"bibtex_field": "eprint", "value": u[6:],
                "extra": {"archivePrefix": "arXiv"}}
    if low.startswith("doi:"):
        return {"bibtex_field": "doi", "value": u[4:], "extra": {}}
    if low.startswith(("http://", "https://")):
        return {"bibtex_field": "url", "value": u, "extra": {}}
    if low.startswith("pmid:"):
        return {"bibtex_field": "note", "value": f"PMID: {u[5:]}", "extra": {}}
    return {"bibtex_field": "note", "value": u, "extra": {}}


def _resolve_one(state: State, kb_id: str, _seen: set[str] | None = None) -> dict:
    _seen = _seen if _seen is not None else set()
    if kb_id in _seen:
        return {"id": kb_id, "external": [], "note": "循环引用"}
    _seen.add(kb_id)

    if kb_id.startswith("chunk_"):
        rec = state.get_kb_record("chunks", kb_id)
        if rec is None:
            return {"id": kb_id, "external": [], "note": "chunk 不存在"}
        src = str(rec.get("source") or "")
        if _is_external_uri(src):
            return {"id": kb_id, "kind": "chunk", "external": [src],
                    "text_preview": (rec.get("text") or "")[:160],
                    "scope": rec.get("scope")}
        return {"id": kb_id, "kind": "chunk", "external": [],
                "note": f"source 是内部来源（{src[:60]}）—— 不可外部引用",
                "text_preview": (rec.get("text") or "")[:160]}

    if kb_id.startswith("claim_"):
        rec = state.get_kb_record("claims", kb_id)
        if rec is None:
            return {"id": kb_id, "external": [], "note": "claim 不存在"}
        ext: list[str] = []
        internal: list[str] = []
        for s in (rec.get("sources") or []):
            if _is_external_uri(s):
                ext.append(s)
            elif isinstance(s, str) and s.startswith(("chunk_", "claim_")):
                sub = _resolve_one(state, s, _seen)
                ext.extend(sub.get("external") or [])
                if not sub.get("external"):
                    internal.append(s)
        out = {"id": kb_id, "kind": "claim", "external": sorted(set(ext)),
               "claim_text_preview": (rec.get("claim_text") or "")[:160],
               "scope": rec.get("scope")}
        if internal:
            out["unresolved_sources"] = internal
        return out

    rec = state.get_kb_record("concepts", kb_id)
    if rec is not None:
        ext = [s for s in (rec.get("sources") or []) if _is_external_uri(s)]
        return {"id": kb_id, "kind": "concept", "external": sorted(set(ext)),
                "canonical_name": rec.get("canonical_name")}
    return {"id": kb_id, "external": [], "note": "未知 id 前缀 / 记录不存在"}


async def _resolve_citations(state: State, ids: list[str] | None = None,
                             **_: Any) -> dict:
    """把 KB id 反查成可引用的外部标识符。"""
    # 1–100 个由 parameters_schema 声明（minItems / maxItems：有界读取），派发口核一次。
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]

    resolved, unresolved = [], []
    for kb_id in ids:
        row = _resolve_one(state, kb_id)
        if row.get("external"):
            row["bibtex"] = [_bibtex_hint(u) for u in row["external"]]
            resolved.append(row)
        else:
            unresolved.append(row)
    return {
        "status": "success",
        "resolved": resolved,
        "unresolved": unresolved,
        "note": (
            f"{len(resolved)}/{len(ids)} 条查到了外部标识符。"
            "**没查到的不要编**：宁可在正文里写成"
            "'（内部实验证据，见 experiment_log）'，也不要生成 "
            "`eprint = {arXiv preprint}` 这种占位条目 —— 那和编造的引用"
            "在读者眼里没有区别。"
        ),
    }


register_tool(
    ToolDefinition(
        name="resolve_citations",
        description=(
            "把 KB 记录 id（claim_ / chunk_ / concept_）反查成**可引用的外部标识符**"
            "（arxiv: / doi: / http…），并给出该填进 bibtex 哪个字段。"
            "写参考文献前用它 —— KB 里存着 literature 检索时的真实 ID，"
            "别照着 survey 正文的散文编条目。claim 会沿 sources 递归解析到 chunk。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 100,
                        "description": "要反查的 KB id 列表（1–100 个）。"},
            },
            "required": ["ids"],
        },
        risk_level="low",
    ),
    _resolve_citations,
)


# ══ #748：已裁决的命题由框架投影成 KB claim，不靠模型想起来 ══════════════
#
# 现场（2026-08-30～09-01，项目 46da60b0）：43 次节点 run，`_curator` 0 次；
# research_state 里 4 条带命题的问题已裁决（Q1/Q2/Q3 supported、Q4 withdrawn），
# KB 里 hypothesis claim 0 条。curator 从"必经步"改成"按需调取"是对的（必经时
# 一个 session 跑 5 次、写入 0 次、2.42M tokens），但钟摆停在了另一端：调度器
# 引导里写着"产出里有新的科学命题 → 值得起"，43/43 次它都判"不值得"。
#
# 引导不是机制。「Q1 被支持」是 research_state 里的机械事实；它的 KB 投影
# （一条 hypothesis claim + 一次 validated 翻转）**没有任何一处需要模型判断**：
# 命题文本在冻结预注册里、证据在 research_state 行里、裁决权与承诺兑现两道
# 闸本来就是机械的。所以框架自己做，走的是**同一条**写入路径
# （write_claim / _update_claim_status），账本该怎么严还怎么严 —— 承诺没兑现
# 的照样降落 provisional，一个字不放松。curator 剩下的活是真需要判断的：
# 分歧分诊、合并、晋升。

#: research_state 里的裁决词 → KB claim 目标状态。
#: withdrawn / active / 其它 → 不投影（撤回的命题不是知识；未裁决的不入账）。
_VERDICT_TO_CLAIM_STATUS = {
    "supported": "validated",
    "refuted": "refuted",
    "inconclusive": None,          # 立 claim（open），不翻
}


def _latest_prereg_artifact(state: State) -> tuple[str, dict] | None:
    """本项目当前版的预注册 (artifact_id, record)。与 frozen_questions 同一选法：
    同身份取 version 最大。"""
    best: tuple[int, str, dict] | None = None
    for entry in state.list_artifacts(artifact_type="pre_registration"):
        rec = state.read_artifact(entry["id"])
        if not isinstance(rec, dict):
            continue
        try:
            version = int(rec.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        if best is None or version > best[0]:
            best = (version, entry["id"], rec)
    return (best[1], best[2]) if best else None


def _prereg_chunk_id(state: State, prereg_id: str) -> str | None:
    """冻结预注册登记出来的 chunk（同一 artifact 取最新版）。"""
    best: tuple[int, str] | None = None
    for ch in state.list_kb("chunks"):
        if ch.get("origin_artifact_id") != prereg_id or not ch.get("origin_artifact_frozen"):
            continue
        try:
            version = int(ch.get("origin_artifact_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if best is None or version > best[0]:
            best = (version, str(ch.get("id")))
    return best[1] if best else None


async def _evidence_chunk_ids(state: State, evidence: Any) -> list[str]:
    """research_state 行里的证据 → chunk id。artifact id 现场登记（与 write_claim
    的 sources 转换同一函数）；本来就是 chunk_/experiment_ 的原样保留；认不出的丢。"""
    from shared.lib.kb_schema import is_chunk_id, is_experiment_id

    out: list[str] = []
    for item in evidence if isinstance(evidence, list) else []:
        sid = str(item or "").strip()
        if not sid:
            continue
        # 用 schema 自己的判据，不用前缀长相：artifact id `experiment_log__q1`
        # 也以 "experiment_" 开头，按前缀判会把它当成 KB experiment id 原样放行，
        # 然后在证据校验处被拒（实测第一版就栽在这）。
        if is_chunk_id(sid) or is_experiment_id(sid):
            out.append(sid)
            continue
        if state.read_artifact(sid) is not None:
            reg = await _kb_register_artifact_as_chunk(state, artifact_id=sid)
            if reg.get("status") == "success" and reg.get("chunk_id"):
                out.append(str(reg["chunk_id"]))
    return out


async def project_research_state_to_kb(state: State, research_state: dict) -> list[dict]:
    """把 research_state 里已裁决的命题投影成 KB hypothesis claim。幂等。

    每条带命题的问题 → 一条结构化身份的 claim（hyp|<prereg 身份>|<qid>）：
    不存在就立（open），已存在就不动；裁决是 supported / refuted 就走
    `_update_claim_status` 翻转 —— 裁决权闸（Analysis 自己的 run，放行）、承诺
    兑现闸（未兑现 → 如实降落 provisional）、证据链闸一道不少。

    返回每条问题的处置（也写进 transcript 事件 `kb_claim_projected`），让
    "投影了没有、落成了什么"是可查的事实而不是日志里的一句话。
    """
    from core.prereg_commitments import frozen_questions

    meta = research_state.get("metadata") if isinstance(research_state, dict) else None
    rows = (meta or {}).get("hypotheses") if isinstance(meta, dict) else None
    if not isinstance(rows, list) or not rows:
        return []
    version = (meta or {}).get("version")

    prereg = _latest_prereg_artifact(state)
    if prereg is None:
        state.append_transcript("kb_claim_projection_skipped", reason="no_pre_registration")
        return []
    prereg_id, _prereg_rec = prereg
    prereg_chunk = _prereg_chunk_id(state, prereg_id)
    if prereg_chunk is None:
        reg = await _kb_register_artifact_as_chunk(state, artifact_id=prereg_id)
        prereg_chunk = str(reg.get("chunk_id") or "") or None
    if prereg_chunk is None:
        state.append_transcript("kb_claim_projection_skipped",
                                reason="prereg_not_chunked", prereg_id=prereg_id)
        return []

    questions = frozen_questions(state)
    outcomes: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("id") or "").strip().upper()
        verdict = str(row.get("status") or "").strip().lower()
        q = questions.get(qid)
        outcome: dict[str, Any] = {"hypothesis_id": qid, "verdict": verdict}
        if q is None or not q.is_hypothesis or verdict not in _VERDICT_TO_CLAIM_STATUS:
            # 不带命题的问题没有 claim 可立；withdrawn 等不是裁决。
            outcome["action"] = "skipped"
            outcome["reason"] = ("no_proposition" if q is None or not q.is_hypothesis
                                 else f"verdict_{verdict or 'empty'}_not_projected")
            outcomes.append(outcome)
            state.append_transcript("kb_claim_projected", **outcome)
            continue

        # 已存在就不重写：结构化身份 (预注册身份, qid) 唯一，重写同一条会走
        # merge 路径把 status 按 confidence 重算 —— 一条已 validated 的 claim
        # 会被自己的投影静默打回 provisional（第一版幂等测试抓到的）。
        existing = next(
            (c for c in state.list_kb("claims")
             if c.get("claim_type") == "hypothesis"
             and str(c.get("hypothesis_id") or "").upper() == qid
             and c.get("prereg_artifact_id") == prereg_id),
            None,
        )
        criteria = "\n".join(item.describe() for item in q.closure) or q.proposition
        created = {"status": "success", "id": existing["id"], "created": False} if existing else await write_claim(
            state,
            claim_text=q.proposition,
            claim_type="hypothesis",
            hypothesis_id=qid,
            prereg_chunk_id=prereg_chunk,
            predicted_outcome=q.proposition,
            falsification_criteria_text=criteria,
            sources=await _evidence_chunk_ids(state, row.get("evidence")),
            orphan_reason=(
                f"框架自 research_state v{version} 机械投影；概念锚由 curator 整合时补"
            ),
        )
        if created.get("status") != "success":
            outcome.update(action="failed", error=str(created.get("error"))[:300])
            outcomes.append(outcome)
            state.append_transcript("kb_claim_projected", **outcome)
            continue
        claim_id = created["id"]
        outcome.update(claim_id=claim_id, action="created" if created.get("created") else "existing")
        if existing and existing.get("adoption") == "proposed":
            # 提案期由节点写下的同一条假说，被 Analysis 裁决投影时即**采纳**。
            state.patch_derived("claims", claim_id, {"adoption": "adopted"})
            outcome["adoption"] = "adopted"

        target = _VERDICT_TO_CLAIM_STATUS[verdict]
        current = (state.get_kb_record("claims", claim_id) or {}).get("status")
        if target is None or current == target:
            outcome["landed_status"] = current
            outcomes.append(outcome)
            state.append_transcript("kb_claim_projected", **outcome)
            continue

        evidence_ids = await _evidence_chunk_ids(state, row.get("evidence"))
        note = str(row.get("note") or "").strip()
        flipped = await _update_claim_status(
            state, claim_id=claim_id, new_status=target, hypothesis_id=qid,
            evidence_ids=evidence_ids,
            reasoning=(f"框架自 research_state v{version} 机械投影："
                       f"{qid}={verdict}。{note}"),
        )
        if flipped.get("status") == "success":
            outcome["action"] = "flipped"
            outcome["landed_status"] = flipped.get("new_status")
            if flipped.get("authority_note"):
                outcome["authority_note"] = str(flipped["authority_note"])[:600]
        else:
            outcome.update(action="flip_failed", error=str(flipped.get("error"))[:300],
                           landed_status=current)
        outcomes.append(outcome)
        state.append_transcript("kb_claim_projected", **outcome)
    return outcomes
