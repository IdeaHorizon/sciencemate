"""统一 Proposal Inbox（v2.1 瘦身：合并 KB 和 Skill 的 4 套 proposal 工具）。

3 个工具替代旧的 7 个：
  - propose             写一条 proposal（按 proposal_type 自动路由到 kb_proposals.jsonl 或 skill_proposals.jsonl）
  - list_proposals      浏览（自动合并三层文件）
  - resolve_proposal    accept / reject（skill_candidate 时自动落 SKILL.md）

物理存储：project/kb_proposals.jsonl（target 是 scope=project 的 KB record，
或 profile/project update）、org/kb_proposals.jsonl（target 是 scope=org 的
KB record —— 2026-07 新增，见 _kb_proposal_write_path 的根因说明）、
org/skill_proposals.jsonl（skill 类）。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool


# ── proposal_type 分类 ──────────────────────────────────────────────────────

_KB_PROPOSAL_TYPES = {
    "kb_claim_status_flip",
    "kb_synthesis_candidate",
    "kb_merge_candidate",
    "kb_staleness",
    "kb_contradiction_review",
    "kb_scope_conflict",
    "kb_other",
    # 新发现 → 候选 claim（2026-08-22，wangd 拍板的沉淀管线）。
    #
    # 在此之前这张表**只有关于既有 KB 记录的动作**（翻状态/合并/过期/矛盾）——
    # proposal 通道结构上装不下「这里有条新发现，请立成 claim」这件事。后果实
    # 测过：产出节点把发现写成日志散文（## Sediment 段），curator 三次 dreaming
    # 一条也没搬进 KB，项目 KB 零 claim，论文没东西可引。
    #
    # 分工（第一性原理）：产出节点只负责把发现写清楚（它们不该被要求精通 KB
    # schema —— 契约灌不进去，四次实测全是这么误伤的）；**框架**在 freeze 时把
    # 沉淀段机械搬运成这类 proposal（target = 刚登记的 frozen chunk，证据锚点
    # 天然在）；**curator** 做语义判断：去重、定 claim_type、挂 concept、
    # 决定收不收。生产方零学习成本，KB claim 只有 curator 一个写入口。
    "kb_claim_candidate",
    # PROFILE/PROJECT 更新（也走项目层 inbox —— 提议本就 project-scoped）
    "profile_update",
    "project_update",
    # memory promotion / archive 候选（curator dreaming 写）
    "memory_promote_to_kb",
    "memory_promote_to_profile",
    "memory_promote_to_project",
    "memory_archive_candidate",
}
_SKILL_PROPOSAL_TYPES = {
    "skill_candidate",
    "skill_deprecation_candidate",
}
_ALL_PROPOSAL_TYPES = _KB_PROPOSAL_TYPES | _SKILL_PROPOSAL_TYPES


def _layer_of(proposal_type: str) -> str:
    """proposal 应该落到哪一层：'kb'（项目层）或 'skill'（org 层）。"""
    if proposal_type in _KB_PROPOSAL_TYPES:
        return "kb"
    if proposal_type in _SKILL_PROPOSAL_TYPES:
        return "skill"
    # 未知 → 按前缀推断（forward-compat）
    if proposal_type.startswith("skill"):
        return "skill"
    return "kb"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org_root() -> Path:
    from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）

    org = Path(os.getenv("HARNESS_FRAMEWORK_ORG_HOME", str(_root() / "org")))
    org.mkdir(parents=True, exist_ok=True)
    return org


def _kb_proposals_path(state: State) -> Path:
    """项目层 kb_proposals.jsonl —— 本项目自己的 proposal（含 profile_update /
    project_update，这两类天然项目特定，恒落本项目文件，见 profile_tools.py）。"""
    base = state.project_root if state.project_root else state.root
    base.mkdir(parents=True, exist_ok=True)
    return base / "kb_proposals.jsonl"


def _org_kb_proposals_path() -> Path:
    """org 共享 KB proposal 队列 —— target 是 scope=org 的 KB record 时落这里。"""
    return _org_root() / "kb_proposals.jsonl"


def _skill_proposals_path() -> Path:
    return _org_root() / "skill_proposals.jsonl"


def _kb_proposal_write_path(state: State, target_record: dict | None) -> Path:
    """按被提议 KB record 的真实 scope 路由存储位置（根因修复，2026-07）。

    v10c dogfood 实测：find_synthesis_candidates 等自动扫描工具用
    state.list_kb() 合并读 project+org 两层 KB，扫到的 org 共享 concept/claim
    （可能来自完全不相关的其它项目）之前一律被 propose() 写进"恰好触发这次
    扫描的项目"的本地 kb_proposals.jsonl —— 实测同一项目 47 条 proposal 里
    42 条（89%）target 的其实是别的课题的 org 概念，把本项目的 triage 列表
    淹没；且因为去重只查本项目文件，同一个 org record 会被不同项目反复
    重复提议。现在按 target_record.scope 路由：scope=org → 写共享
    org/kb_proposals.jsonl（所有项目共用同一份，天然去重、天然归口）；
    否则（scope=project，或没有真实 KB target，如 profile/project update）→
    仍写本项目文件。
    """
    if target_record is not None and target_record.get("scope") == "org":
        return _org_kb_proposals_path()
    return _kb_proposals_path(state)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _rewrite_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# propose
# ─────────────────────────────────────────────────────────────────────────────

async def _propose(
    state: State,
    proposal_type: str,
    target_entity: str,
    target_id: str,
    proposed_action: str,
    reasoning: str,
    confidence: float | None = None,
    extra: dict | None = None,
    **_: Any,
) -> dict:
    """统一 propose：按 proposal_type 路由到 KB 或 Skill 层。

    proposal_type 已知值：
      KB 类（→ project/kb_proposals.jsonl）:
        kb_claim_status_flip / kb_synthesis_candidate / kb_merge_candidate /
        kb_staleness / kb_contradiction_review / kb_scope_conflict / kb_other
      Skill 类（→ org/skill_proposals.jsonl）:
        skill_candidate          - 新增 skill 候选；extra 必须含 skill={...}
        skill_deprecation_candidate - 废弃 skill 候选；extra 应含 skill_name
    """
    # 五个必填参数的非空由 parameters_schema 声明（required + minLength:1），
    # 派发口核一次；框架内部直调方（skill_tools / artifacts_extra）传的是
    # 自己拼的非空值。

    # KB 类 proposal：target_id 必须是 <entity_singular>_<hex> 形式
    target_record: dict | None = None
    if _layer_of(proposal_type) == "kb":
        from shared.lib.kb_schema import ENTITIES as _KB_ENT, entity_singular as _es
        if target_entity not in _KB_ENT:
            return {"status": "error",
                    "error": (f"KB proposal target_entity={target_entity!r} 非法。"
                              f"必须 ∈ {_KB_ENT}")}
        expected_prefix = f"{_es(target_entity)}_"
        if not target_id.startswith(expected_prefix):
            return {"status": "error",
                    "error": (f"target_id={target_id!r} 不符合 {target_entity} 的"
                              f"id 格式（应以 {expected_prefix!r} 开头）。"
                              f"通常先 search_kb / get_kb_record 拿到合法 id 再 propose。")}
        # 校验 target_id 在 KB 真存在（防止 propose 一个 typo id）；顺便拿到
        # record 本身用于按 scope 路由存储位置（见 _kb_proposal_write_path）
        target_record = state.get_kb_record(target_entity, target_id)
        if target_record is None:
            return {"status": "error",
                    "error": (f"target_id={target_id!r} 在 KB 中不存在（已 supersede "
                              f"或 typo）。先用 get_kb_record 确认。")}

    write_path = (_skill_proposals_path() if _layer_of(proposal_type) == "skill"
                  else _kb_proposal_write_path(state, target_record))

    # ── v3.4 负结果留痕（E2E#2：343/369 被 rejected、同 target 重提最多 74 次）──
    # 被拒绝的提议此前没有任何记忆 —— curator 每个 cycle 重新"发现"同一提议、
    # 换个措辞再提，user/框架再拒，无限循环。机械规则：
    #   1. 同 (proposal_type, target_id) 已有 **pending** → 去重复用（所有 KB 类型，
    #      不再仅限 status_flip）。
    #   2. 同 (proposal_type, target_id) 有 **rejected 历史** → 照提，但把
    #      prior_rejected_count / last_rejected_at 挂在提案上让人一眼看穿循环
    #      （判决拆除 O3：thrash 只记不拦——「new_evidence 非空才许重提」任意
    #      非空即过，是仪式闸；pending 去重已挡住真正的重复）。
    if _layer_of(proposal_type) == "kb":
        _existing = _read_jsonl(write_path)
        _same = [p for p in _existing
                 if p.get("proposal_type") == proposal_type
                 and p.get("target_id") == target_id]
        # kb_claim_candidate 的身份是 (target, 发现内容)，不是 (target)：一份
        # 冻结日志天然带多条发现、全部锚在同一个 chunk 上。按 target 级去重会
        # 把第 2..N 条发现静默吞掉（实测：2 条只落 1 条）。同文重提仍然去重。
        if proposal_type == "kb_claim_candidate":
            _same = [p for p in _same
                     if p.get("proposed_action") == proposed_action]
        _pending = [p for p in _same if p.get("status") == "pending"]
        if _pending and proposal_type != "kb_claim_status_flip":
            # status_flip 保留下方更精细的同向判定；其余类型 target 级去重即可
            p = _pending[-1]
            return {
                "status": "success", "proposal_id": p["id"], "layer": "kb",
                "deduplicated": True,
                "note": (f"已有 pending proposal {p['id']}（{p.get('at')}）针对同一 "
                          f"target；本次 propose 已忽略以防重复。"),
            }
        _rejected = [p for p in _same if p.get("status") == "rejected"]
        if _rejected:
            extra = dict(extra or {})
            extra["prior_rejected_count"] = len(_rejected)
            extra["last_rejected_at"] = _rejected[-1].get("at")
            state.append_transcript(
                "proposal_repeats_rejected_target",
                proposal_type=proposal_type, target_id=target_id,
                prior_rejected_count=len(_rejected),
                has_new_evidence=bool(str(extra.get("new_evidence") or "").strip()),
            )

    # 同向 flip dedup：同一 claim 已有 pending 的同向 status flip → 直接复用
    if proposal_type == "kb_claim_status_flip":
        to_status = (extra or {}).get("to_status")
        if to_status:
            existing = _read_jsonl(write_path)
            for p in existing:
                if (p.get("status") == "pending"
                    and p.get("proposal_type") == "kb_claim_status_flip"
                    and p.get("target_id") == target_id
                    and (p.get("extra") or {}).get("to_status") == to_status):
                    return {
                        "status": "success",
                        "proposal_id": p["id"],
                        "layer": "kb",
                        "deduplicated": True,
                        "note": (f"已有 pending proposal {p['id']} 提议 {target_id} → "
                                  f"{to_status}（{p.get('at')}）；本次 propose 已忽略以防 thrash"),
                    }

    # skill_candidate 特殊：extra 必须有合法的 skill 数据
    if proposal_type == "skill_candidate":
        skill_data = (extra or {}).get("skill", {})
        required = ("name", "description", "body_markdown")
        missing = [k for k in required if not skill_data.get(k)]
        if missing:
            return {"status": "error",
                    "error": f"skill_candidate proposal 的 extra.skill 缺：{missing}"}
        from core.skill_registry import get_skill
        if get_skill(skill_data["name"]) is not None:
            return {"status": "error",
                    "error": f"skill {skill_data['name']!r} 已存在；想改直接编辑 SKILL.md"}

    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    record = {
        "id": proposal_id,
        "at": _now(),
        "proposed_by_curator_run_id": state.hook_state.get("_current_curator_run_id"),
        "proposed_by_run_id": state.run_id,
        "proposed_by_node_type": state.node_type,
        "proposal_type": proposal_type,
        "target_entity": target_entity,
        "target_id": target_id,
        "proposed_action": proposed_action,
        "reasoning": reasoning,
        "confidence": confidence,
        "status": "pending",
        "extra": extra or {},
    }
    _append_jsonl(write_path, record)
    out = {"status": "success", "proposal_id": proposal_id,
           "layer": _layer_of(proposal_type),
           "routed_to": ("org_kb" if write_path == _org_kb_proposals_path()
                         else ("skill" if _layer_of(proposal_type) == "skill"
                               else "project_kb"))}
    if (extra or {}).get("prior_rejected_count"):
        out["prior_rejected_count"] = extra["prior_rejected_count"]
        out["last_rejected_at"] = extra.get("last_rejected_at")
        out["note"] = (
            f"该 target 的同类提议此前已被拒绝 {extra['prior_rejected_count']} 次"
            f"（最近 {extra.get('last_rejected_at')}）；本次照提，历史已挂在提案上"
            f"供人裁。若这次没有新情况，重提很可能再被拒——请继续其它工作。")
    return out


register_tool(
    ToolDefinition(
        name="propose",
        description=(
            "提交一条 proposal 到统一 inbox。按 proposal_type 自动路由：\n"
            "  KB 类: kb_claim_status_flip / kb_synthesis_candidate / "
            "kb_merge_candidate / kb_staleness / kb_contradiction_review / "
            "kb_scope_conflict / kb_other —— 落项目层还是 org 共享层，由 "
            "target_id 指向的 KB record 自己的 scope 决定（scope=org → 落 org "
            "共享队列，不会塞进本项目的私有 inbox）\n"
            "  Skill 类（org 层）: skill_candidate（extra.skill={name,description,"
            "body_markdown,...}）/ skill_deprecation_candidate\n"
            "reasoning 非空，说清为什么；高风险动作（refute validated claim / merge claims / "
            "新 skill）必走这里，不直接执行。"
            "同 target 有被拒历史时照提，但 prior_rejected_count 会挂在提案上供人裁。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "proposal_type": {"type": "string", "minLength": 1},
                "target_entity": {"type": "string", "minLength": 1,
                                    "description": "KB entity 名或 'skill'"},
                "target_id": {"type": "string", "minLength": 1},
                "proposed_action": {"type": "string", "minLength": 1},
                "reasoning": {"type": "string", "minLength": 1,
                              "description": "非空，说清为什么提这条"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "extra": {"type": "object",
                            "description": "类型相关额外数据（skill_candidate 必含 skill={...}）"},
            },
            "required": ["proposal_type", "target_entity", "target_id",
                          "proposed_action", "reasoning"],
        },
        risk_level="low",
    ),
    _propose,
)


# ─────────────────────────────────────────────────────────────────────────────
# list_proposals
# ─────────────────────────────────────────────────────────────────────────────

def _compact_proposal(p: dict) -> dict:
    """triage 用紧凑投影：只留判断/定位字段，丢掉 extra（可能含大 candidate_source
    _claim_ids 数组）。想看某条全量用 detail='full' 或 status/type filter 收窄。"""
    return {
        "id": p.get("id"),
        "proposal_type": p.get("proposal_type"),
        "target_entity": p.get("target_entity"),
        "target_id": p.get("target_id"),
        "status": p.get("status"),
        "_origin_layer": p.get("_origin_layer"),
        # proposed_action 必须在紧凑投影里：老类型的动作由 proposal_type 隐含，
        # 但 kb_claim_candidate 的**发现原文**就在这个字段 —— triage 靠它判断
        # 收不收，列表里看不见就得每条再读一次全量。有界切片，不整回。
        "proposed_action": (p.get("proposed_action") or "")[:300],
        "reasoning": (p.get("reasoning") or "")[:200],
        "confidence": p.get("confidence"),
        "at": p.get("at"),
    }


async def _list_proposals(
    state: State,
    layer_filter: str | None = None,
    type_filter: str | None = None,
    status: str = "pending",
    limit: int = 50,
    detail: str = "compact",
    **_: Any,
) -> dict:
    """列出 proposals。自动从 KB 层（项目 + org 共享）+ Skill 层（org）合并读取。

    每条 item 附带 `_origin_layer`（'project_kb' / 'org_kb' / 'skill'），方便
    triage 时分清"这是我这个项目自己的事"还是"这是全 org 共享的积压"——
    后者可能是完全不相关课题的产出（见 _kb_proposal_write_path 的根因说明）。

    detail：'compact'（默认，每条只回 triage 关键字段，丢 extra）/ 'full'（回
    全量含 extra）。有界投影 + token 预算——proposal 队列可能积压上百条（含大
    candidate_source_claim_ids 数组），全量一次性回会顶爆 context（v10c 实测
    单次 list_proposals 回 183 条 = 11k tokens）。想看某条全量：detail='full'
    并配 type_filter / status 把结果集缩小。
    """
    kb_props = _read_jsonl(_kb_proposals_path(state))
    for p in kb_props:
        p["_origin_layer"] = "project_kb"
    org_kb_props = _read_jsonl(_org_kb_proposals_path())
    for p in org_kb_props:
        p["_origin_layer"] = "org_kb"
    skill_props = _read_jsonl(_skill_proposals_path())
    for p in skill_props:
        p["_origin_layer"] = "skill"

    all_items: list[dict] = []
    if layer_filter in (None, "kb", "project_kb"):
        all_items.extend(kb_props)
    if layer_filter in (None, "kb", "org_kb"):
        all_items.extend(org_kb_props)
    if layer_filter in (None, "skill"):
        all_items.extend(skill_props)

    if status:
        all_items = [p for p in all_items if p.get("status") == status]
    if type_filter:
        all_items = [p for p in all_items if p.get("proposal_type") == type_filter]

    # 按 at 倒序
    all_items.sort(key=lambda p: p.get("at", ""), reverse=True)
    total_matched = len(all_items)
    windowed = all_items[:limit]

    from shared.lib.kb_result_budget import budget_rows
    if detail == "full":
        rows, truncated = budget_rows(windowed)
    else:
        rows, truncated = budget_rows(windowed, projector=_compact_proposal)

    out = {
        "status": "success",
        "total_matched": total_matched,
        "returned": len(rows),
        "truncated": truncated or total_matched > len(rows),
        "detail": detail,
        "proposals": rows,
        # 兼容旧 caller
        "count": len(rows),
    }
    if out["truncated"]:
        out["hint"] = (
            f"共 {total_matched} 条匹配，只返回 {len(rows)} 条（预算/limit 所限）。"
            f"用 type_filter / status / layer_filter 收窄，逐类清账，别一次性拉全部。"
        )
    return out


register_tool(
    ToolDefinition(
        name="list_proposals",
        # 纯读：结果可用同样参数重调取回。重复调用紧凑化与压缩器都扫这个
        # 声明（core/tool_call_cache.cacheable_tools），不再各写一份名单。
        replayable_read=True,
        description=(
            "列出所有 proposals（默认 status=pending）。"
            "自动合并项目 KB 层 + org 共享 KB 层 + org Skill 层三个文件。"
            "每条附带 _origin_layer 标注来源（project_kb 是本项目自己的事；"
            "org_kb 是全 org 共享积压，可能来自完全不相关的课题）。"
            "可按 layer_filter('kb'=项目+org KB 都要 / 'project_kb' / 'org_kb' / "
            "'skill') 或 type_filter（具体 proposal_type）过滤。\n\n"
            "**有界返回**：默认 detail='compact'（每条只回 triage 关键字段，丢 extra），"
            "受 token 预算硬顶——队列可能积压上百条，别一次拉全量顶爆 context。"
            "想看某条全量：detail='full' 并配 type_filter/status 缩小结果集，逐类清账。\n"
            "返回 `{total_matched, returned, truncated, proposals, hint?}`。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "layer_filter": {"type": "string",
                                  "enum": ["kb", "project_kb", "org_kb", "skill"]},
                "type_filter": {"type": "string"},
                "status": {"type": "string",
                            "enum": ["pending", "accepted", "rejected", "reverted"],
                            "default": "pending"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                "detail": {"type": "string", "enum": ["compact", "full"],
                            "default": "compact",
                            "description": "compact=只回 triage 关键字段（默认，省 context）；"
                                           "full=回全量含 extra（超预算截断）。"},
            },
        },
        risk_level="low",
    ),
    _list_proposals,
)


# ─────────────────────────────────────────────────────────────────────────────
# resolve_proposal
# ─────────────────────────────────────────────────────────────────────────────

def _find_proposal(state: State, proposal_id: str) -> tuple[dict | None, Path | None, list[dict]]:
    """在三层文件里找 proposal（项目 KB / org 共享 KB / org skill）。
    返回 (proposal, path_it_lives_in, all_records_in_that_path)。"""
    for path in (_kb_proposals_path(state), _org_kb_proposals_path(),
                 _skill_proposals_path()):
        records = _read_jsonl(path)
        for p in records:
            if p.get("id") == proposal_id:
                return p, path, records
    return None, None, []


async def _resolve_proposal(
    state: State,
    proposal_id: str,
    decision: str,
    reasoning: str,
    **_: Any,
) -> dict:
    """处理 proposal：accepted / rejected。

    accepted 的副作用：
      - skill_candidate → 自动落 SKILL.md 到 org/skills/<name>/
      - 其它 KB proposal → 仅标记 accepted；具体执行动作由 caller 显式调对应工具

    rejected → 仅标记，不执行任何动作。

    decision 枚举与 reasoning 非空由 parameters_schema 声明、派发口核一次。
    """
    proposal, path, records = _find_proposal(state, proposal_id)
    if proposal is None:
        return {"status": "error", "error": f"proposal {proposal_id!r} 不存在"}
    if proposal.get("status") != "pending":
        return {"status": "error",
                "error": f"proposal 当前 status={proposal.get('status')}, 不能再 resolve"}

    side_effect = None

    # accept profile_update / project_update → 真正写 PROFILE/PROJECT.md
    if (decision == "accepted"
        and proposal.get("proposal_type") in ("profile_update", "project_update")):
        try:
            from shared.tools.library.profile_tools import apply_profile_update
            apply_result = apply_profile_update(state, proposal)
            side_effect = apply_result
        except Exception as e:
            return {"status": "error",
                    "error": f"apply_profile_update 失败：{type(e).__name__}: {e}"}

    # accept skill_candidate → 落 SKILL.md
    elif (decision == "accepted"
        and proposal.get("proposal_type") == "skill_candidate"):
        skill_data = (proposal.get("extra") or {}).get("skill", {})
        skill_name = skill_data.get("name")
        if not skill_name:
            return {"status": "error", "error": "skill_candidate proposal 缺 skill.name"}

        org_skills_dir = _org_root() / "skills"
        if (org_skills_dir / skill_name).exists():
            return {"status": "error",
                    "error": f"org/skills/{skill_name}/ 已存在"}

        # 复用 skill_tools 里的 _write_skill_md helper
        from shared.tools.library.skill_tools import _write_skill_md
        _write_skill_md(
            org_skills_dir, skill_data,
            origin_note=f"accepted from proposal {proposal_id} at {_now()} "
                         f"by run {state.run_id}; reviewer: {reasoning}",
        )

        # 加载进 registry
        from core.skill_loader import load_skill_from_folder
        from core.skill_registry import register_skill
        loaded = load_skill_from_folder(org_skills_dir / skill_name, origin="imported")
        if loaded:
            register_skill(loaded)

        side_effect = {"wrote_skill_md": str(org_skills_dir / skill_name / "SKILL.md")}

    # 更新 proposal status
    for p in records:
        if p.get("id") == proposal_id:
            p["status"] = decision
            p["reviewed_at"] = _now()
            p["reviewer_node"] = state.node_type
            p["reviewer_run_id"] = state.run_id
            p["reviewer_reasoning"] = reasoning
            break
    _rewrite_jsonl(path, records)

    out = {"status": "success", "proposal_id": proposal_id,
           "decision": decision, "proposal_type": proposal.get("proposal_type")}
    if side_effect:
        out["side_effect"] = side_effect
    return out


register_tool(
    ToolDefinition(
        name="resolve_proposal",
        description=(
            "处理 pending proposal：accepted / rejected。"
            "副作用：skill_candidate + accepted → 自动落 SKILL.md 到 org/skills/"
            "<name>/。其它 KB proposal accepted 只标记，**具体执行动作要 caller 显式调"
            "对应工具**（update_claim_status / create_synthesis / 等）。"
            "reasoning 非空，说清为什么这么裁。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accepted", "rejected"]},
                "reasoning": {"type": "string", "minLength": 1,
                              "description": "非空，说清为什么这么裁"},
            },
            "required": ["proposal_id", "decision", "reasoning"],
        },
        risk_level="medium",
    ),
    _resolve_proposal,
)
