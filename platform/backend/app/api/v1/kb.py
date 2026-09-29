"""KB read views over the harness knowledge base.

Concepts, claims, syntheses and curator proposals are all sedimented by agents
into harness JSONL (`kb_concepts.jsonl` / `kb_claims.jsonl` / `kb_proposals.jsonl`).
This router previously read this service's own `kb_*` tables, which on the
deployed instance with 15 real projects held **zero rows** — so every KB page in
the UI rendered an empty list while the real knowledge sat in the harness home.

**Read-only by design.**  The four write endpoints that used to live here
(`claims/{id}/verify`, `syntheses/{id}/review`, `proposals/{id}/approve|reject`)
were removed rather than repointed: they mutated tables nothing read, so they
were affordances that did nothing.  Repointing them at the harness would be
worse — claim status there is governed by mechanical authority rules
(`update_claim_status` requires a research_state backing; proposals are resolved
through the curator flow).  A button in this service writing straight into that
store would route around the very gates that make the KB trustworthy.  Curation
belongs in the research session, not in a side channel.

See `app.services.harness_kb`.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import get_current_user
from app.models.user import User
from app.services import harness_kb

logger = logging.getLogger(__name__)

router = APIRouter()


class ConceptResponse(BaseModel):
    id: str
    canonical_name: str
    concept_type: str
    short_definition: str | None
    living_summary: str | None
    source_count: int
    claim_count: int
    project_usage_count: int
    aliases: list[str] = []
    last_refined_at: datetime | None


class ClaimResponse(BaseModel):
    id: str
    claim_text: str
    confidence: str
    artifact_id: str
    is_verified: bool
    concept_ids: list[str] | None
    conditions: dict | None
    status: str = "active"
    created_at: datetime | None


class SynthesisResponse(BaseModel):
    id: str
    title: str
    content: str
    synthesis_type: str
    status: str
    confidence: str
    source_artifact_ids: list[str]
    key_findings: list[dict] | None
    conflicts_detected: list[dict] | None
    created_at: datetime | None


class ProposalResponse(BaseModel):
    id: str
    proposed_content: str
    proposed_layer: str
    proposed_type: str
    proposed_confidence: str
    proposed_topic: str | None
    reasoning: str | None
    status: str
    conflicts_with: list[str] | None
    supersedes: list[str] | None
    created_at: datetime | None


def _as_concept(record: dict[str, Any]) -> ConceptResponse:
    derived = record.get("derived") or {}
    return ConceptResponse(
        id=str(record.get("id") or ""),
        canonical_name=str(record.get("canonical_name") or ""),
        concept_type=str(record.get("concept_type") or "unknown"),
        short_definition=record.get("description"),
        living_summary=derived.get("living_summary"),
        source_count=len(record.get("sources") or []),
        claim_count=len(record.get("claim_ids") or []),
        project_usage_count=int(derived.get("project_usage_count") or 0),
        aliases=[str(a) for a in (record.get("aliases") or [])],
        last_refined_at=None,
    )


def _as_claim(record: dict[str, Any]) -> ClaimResponse:
    """Map a harness claim.

    `confidence` is a float in the harness and a label here, so it is bucketed
    rather than stringified — showing "0.72" under a column the UI renders as a
    confidence level would read as a precision the label vocabulary cannot carry.
    """
    raw_confidence = record.get("confidence")
    try:
        value = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = "medium"
    else:
        confidence = "high" if value >= 0.75 else "low" if value < 0.4 else "medium"
    status = str(record.get("status") or "active")
    return ClaimResponse(
        id=str(record.get("id") or ""),
        claim_text=str(record.get("claim_text") or ""),
        confidence=confidence,
        artifact_id=str(record.get("origin_artifact_id") or ""),
        # 「已核验」在 harness 里是 claim 生命周期状态，不是一个独立布尔。
        is_verified=status in ("supported", "verified"),
        concept_ids=[str(c) for c in (record.get("concept_ids") or [])] or None,
        conditions=record.get("scope_dimensions") or None,
        status=status,
        created_at=record.get("created_at"),
    )


def _as_synthesis(record: dict[str, Any]) -> SynthesisResponse:
    text = str(record.get("claim_text") or "")
    return SynthesisResponse(
        id=str(record.get("id") or ""),
        # harness 的 synthesis 没有独立标题字段 —— 用正文首行，不编一个。
        title=text.splitlines()[0][:200] if text else "",
        content=text,
        synthesis_type=str(record.get("synthesis_pattern") or "synthesis"),
        status=str(record.get("status") or "active"),
        confidence=_as_claim(record).confidence,
        source_artifact_ids=[str(s) for s in (record.get("sources") or [])],
        key_findings=None,
        conflicts_detected=None,
        created_at=record.get("created_at"),
    )


def _as_proposal(record: dict[str, Any]) -> ProposalResponse:
    return ProposalResponse(
        id=str(record.get("id") or ""),
        proposed_content=str(record.get("content") or record.get("text") or ""),
        proposed_layer=str(record.get("scope") or record.get("layer") or "project"),
        proposed_type=str(record.get("kind") or record.get("type") or "memory"),
        proposed_confidence=str(record.get("confidence") or "medium"),
        proposed_topic=record.get("topic"),
        reasoning=record.get("reasoning") or record.get("rationale"),
        status=str(record.get("status") or "pending"),
        conflicts_with=None,
        supersedes=None,
        created_at=record.get("created_at"),
    )


# ── Concepts ───────────────────────────────────────────────────────────────

@router.get("/concepts", response_model=list[ConceptResponse])
async def list_concepts(
    query: str | None = Query(None),
    concept_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    concepts = await harness_kb.query(user, project_id or "", "concepts", limit=500)
    if query:
        needle = query.lower()
        concepts = [
            c for c in concepts
            if needle in str(c.get("canonical_name") or "").lower()
            or needle in str(c.get("description") or "").lower()
            or any(needle in str(a).lower() for a in (c.get("aliases") or []))
        ]
    if concept_type:
        concepts = [c for c in concepts if c.get("concept_type") == concept_type]
    # 最常被引用的排前面 —— 与原来 `order_by(source_count.desc())` 同口径。
    concepts.sort(key=lambda c: len(c.get("sources") or []), reverse=True)
    return [_as_concept(c) for c in concepts[offset:offset + limit]]


@router.get("/concepts/{concept_id}", response_model=ConceptResponse)
async def get_concept(
    concept_id: str,
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    matches = await harness_kb.query(user, project_id or "", "concepts", limit=500)
    record = next((c for c in matches if c.get("id") == concept_id), None)
    if record is None:
        raise HTTPException(404, "Concept not found")
    return _as_concept(record)


@router.get("/concepts/{concept_id}/claims", response_model=list[ClaimResponse])
async def get_concept_claims(
    concept_id: str,
    limit: int = Query(50, ge=1, le=200),
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    claims = await harness_kb.query(user, project_id or "", "claims", limit=500)
    linked = [c for c in claims if concept_id in (c.get("concept_ids") or [])]
    return [_as_claim(c) for c in linked[:limit]]


# ── Claims ─────────────────────────────────────────────────────────────────

@router.get("/claims", response_model=list[ClaimResponse])
async def list_claims(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    claims = await harness_kb.query(user, project_id or "", "claims", limit=500)
    claims.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    # synthesis 有自己的视图，别在 claim 列表里重复出现。
    claims = [c for c in claims if c.get("claim_type") != "synthesis"]
    if status:
        claims = [c for c in claims if str(c.get("status") or "active") == status]
    return [_as_claim(c) for c in claims[offset:offset + limit]]


# ── Syntheses ──────────────────────────────────────────────────────────────

@router.get("/syntheses", response_model=list[SynthesisResponse])
async def list_syntheses(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """harness 把 synthesis 折进 claims，用 `claim_type` 区分（见 kb_schema）。"""
    everything = await harness_kb.query(user, project_id or "", "claims", limit=500)
    everything.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    items = [c for c in everything if c.get("claim_type") == "synthesis"]
    if status:
        items = [s for s in items if str(s.get("status") or "active") == status]
    return [_as_synthesis(s) for s in items[offset:offset + limit]]


# ── Proposals ──────────────────────────────────────────────────────────────

@router.get("/proposals", response_model=list[ProposalResponse])
async def list_proposals(
    status: str | None = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    project_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    proposals = await harness_kb.query(user, project_id or "", "proposals", limit=limit)
    return [_as_proposal(p) for p in proposals[:limit]]


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    project_id: str = Query(...),
    reasoning: str = Query(..., min_length=5),
    user: User = Depends(get_current_user),
):
    """Accept a curator proposal — through the harness's own `resolve_proposal`.

    Not a status flip: accepting can have side effects the harness owns (a
    skill candidate writes `org/skills/<name>/SKILL.md`), and the reasoning is
    what keeps the queue auditable.  Same path the agent takes.
    """
    return await _resolve(user, project_id, proposal_id, "accepted", reasoning)


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    project_id: str = Query(...),
    reasoning: str = Query(..., min_length=5),
    user: User = Depends(get_current_user),
):
    return await _resolve(user, project_id, proposal_id, "rejected", reasoning)


async def _resolve(user: User, project_id: str, proposal_id: str,
                   decision: str, reasoning: str) -> dict[str, Any]:
    try:
        return await harness_kb.resolve_proposal(
            user, project_id, proposal_id, decision=decision, reasoning=reasoning
        )
    except harness_kb.HarnessKBError as exc:
        # 把 harness 的拒绝原样透出去（"已经处理过了" / "理由太短"），别翻译成
        # 一句笼统的 500 —— 用户要知道的是这条为什么没被接受。
        raise HTTPException(400, str(exc)) from exc


# ── 组织层：全组织共用的那一份知识 ──────────────────────────────────────────
#
# 机制 09-16 就做好了（PR#1042：一台安装一个 org 层，晋升上去的 claim 全组织共读），
# 但**界面上一页都没有** —— 前端零调用 KB 接口，后端所有读都按项目问。于是"组织级
# 知识积累"这件事只有 agent 用得上，人看不见也管不了。
#
# 这三个端点只读组织层（`scope="org"`），和按项目问的那几个并排放着 —— 两个问题
# 两个入口，不靠 project_id 空不空来区分。


@router.get("/organisation/stats")
async def organisation_knowledge_stats(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """组织层里各有多少条。"""
    return await harness_kb.stats(user, "", scope="org")


@router.get("/organisation/claims", response_model=list[ClaimResponse])
async def organisation_claims(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
):
    """晋升到组织层的结论。"""
    records = await harness_kb.query(
        user, "", "claims", search=search or "", limit=limit, offset=offset, scope="org")
    return [_as_claim(r) for r in records]


@router.get("/organisation/concepts", response_model=list[ConceptResponse])
async def organisation_concepts(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
):
    """组织层的概念表。"""
    records = await harness_kb.query(
        user, "", "concepts", search=search or "", limit=limit, offset=offset, scope="org")
    return [_as_concept(r) for r in records]
