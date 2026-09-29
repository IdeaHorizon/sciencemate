"""组织的知识会被改正 —— 推翻或取代，**不删**。

## 为什么要能改

组织采纳的结论会在每个新项目开局时被机械送到它面前（`core.org_delivery`「本组已知」）。
一条后来站不住的结论（实验翻了、数据有 bug、适用条件写宽了）不改，就会被一个又一个
新项目当成前提 —— 错误也在复利。只接了「知识进组织」、没接「组织的知识会被改正」，
飞轮转得越快，错得越多。

## 为什么不删

科学不删东西，只用新证据推翻或取代旧结论。删掉一条等于抹掉「这件事组里曾经信过、
后来为什么不信了」—— 那本身就是知识。所以组织对一条知识的裁定是 lifecycle 里的一个
字段（`org_standing`），账本里一条不少：

  推翻（refuted）     新证据表明它不成立
  取代（superseded）  有更好的一条代替它（`superseded_by` 指过去，可以还没有）

它和 `status` 是**两个问题**：`status` 随晋升从项目带过来，说的是项目对原结论的判断
（一条被项目自己翻成 refuted 的结论，晋升上来是「X 不成立」这条知识，组织照样认它）。

不再作数的条目不再送达（开题注入、死路红旗、书目复用都经 `in_force`），不再参与
dreaming 的合并 / 矛盾 / 老化；组织页上照样看得到，标着谁、何时、为什么。
裁定可以撤回（retraction-of-retraction 真实存在）：`reinstate`，同样要写理由。

## 谁裁、更正从哪来

裁的只有组织管理员 —— 和采纳同一个人、同一个位置。更正从三处来，都进**同一个**待审
（`core.kb_promotion` 的队列，`type=correction`）：

  成员             在组织页上对一条知识「提出更正」
  项目里的 agent   `propose_org_correction`：手里的证据表明「本组已知」的某一条错了
  机械比对         项目做完时它的结论与组织的某条相反；采纳一条新知识时它与已有的某条相反
                   （判据 `org_dreaming.at_odds`，与 dreaming 的矛盾呈现是同一处）

矛盾**不自动裁决**：机械比对只把它提出来。认可 = 那一条不再作数；驳回 = 两条都站得住
（通常是适用条件还要分细），理由必填。
"""
from __future__ import annotations

import uuid
from typing import Any

VERDICT_REFUTED = "refuted"
VERDICT_SUPERSEDED = "superseded"
#: 让一条知识不再作数的两种裁定。
RETIRING_VERDICTS = (VERDICT_REFUTED, VERDICT_SUPERSEDED)
#: 撤回裁定（重新作数）在历史里记成这个。
VERDICT_IN_FORCE = "in_force"

#: 待审里这一类的 `type`（晋升那一类没有 `type` 字段 —— 它们先来）。
PROPOSAL_TYPE = "correction"

ORIGIN_MEMBER = "member"
ORIGIN_AGENT = "agent"
ORIGIN_CONTRADICTION = "contradiction"
ORIGINS = (ORIGIN_MEMBER, ORIGIN_AGENT, ORIGIN_CONTRADICTION)


def _error(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "error": message}


# ── 作不作数 —— 一处回答 ────────────────────────────────────────────────────


def standing(record: dict) -> dict | None:
    """组织对这条的裁定 —— 还作数就是 None。"""
    got = record.get("org_standing") if isinstance(record, dict) else None
    if isinstance(got, dict) and got.get("verdict") in RETIRING_VERDICTS:
        return got
    return None


def in_force(record: dict) -> bool:
    """组织还认不认这条。送达、dreaming、组织页读的都是这一个判据。"""
    return standing(record) is None


def _org_entry(state: Any, org_id: str) -> dict | None:
    try:
        rec = state.get_kb_record("claims", org_id)
    except Exception:
        return None
    return rec if isinstance(rec, dict) and rec.get("scope") == "org" else None


def _statement(rec: dict) -> str:
    return str(rec.get("statement") or rec.get("claim_text") or "")


def _the_successor(state: Any, org_id: str, superseded_by: str) -> tuple[str, dict | None]:
    successor = str(superseded_by or "").strip()
    if not successor:
        return "", None
    if successor == org_id:
        return successor, _error("invalid_successor", "一条知识不能被它自己取代")
    rec = _org_entry(state, successor)
    if rec is None:
        return successor, _error("invalid_successor", f"组织里没有 {successor} 这一条")
    if not in_force(rec):
        return successor, _error("invalid_successor", f"{successor} 自己已经不作数了，不能拿它取代别的")
    return successor, None


# ── 管理员的裁定 ─────────────────────────────────────────────────────────────


def retire(state: Any, org_id: str, *, verdict: str, reason: str, by: str, at: str,
           superseded_by: str = "", proposal_id: str = "") -> dict:
    """组织不再认这条：推翻或取代。理由必填 —— 以后读到它的人要知道为什么。"""
    if verdict not in RETIRING_VERDICTS:
        return _error("invalid_verdict", f"裁定只能是 {' / '.join(RETIRING_VERDICTS)}")
    reason = str(reason or "").strip()
    if not reason:
        return _error("reason_required", "要写为什么：以后读到这条的人要知道它为什么不作数了")
    if not str(by or "").strip():
        return _error("invalid_by", "谁裁的要记下来")
    rec = _org_entry(state, org_id)
    if rec is None:
        return _error("not_found", f"组织里没有 {org_id} 这一条")
    if not in_force(rec):
        return _error("already_retired", "这一条已经不作数了")
    successor, refused = _the_successor(state, org_id, superseded_by)
    if refused:
        return refused
    entry: dict[str, Any] = {"verdict": verdict, "reason": reason, "by": by, "at": at}
    if successor:
        entry["superseded_by"] = successor
    if proposal_id:
        entry["proposal_id"] = proposal_id
    fields: dict[str, Any] = {
        "org_standing": entry,
        "org_standing_history": [*(rec.get("org_standing_history") or []), entry],
    }
    if successor:
        # 既有的 lifecycle 字段：检索结果上已经会画「⬆ superseded_by=…」。
        fields["superseded_by_claim_id"] = successor
    updated = state.set_lifecycle_fields("claims", org_id, fields)
    if updated is None:
        return _error("not_found", f"组织里没有 {org_id} 这一条")
    return {"status": "success", "entry": updated}


def reinstate(state: Any, org_id: str, *, reason: str, by: str, at: str) -> dict:
    """撤回裁定，这条重新作数。同样要写理由；历史里两次裁定都留着。"""
    reason = str(reason or "").strip()
    if not reason:
        return _error("reason_required", "要写为什么：它曾经被裁定不作数，现在为什么又作数了")
    if not str(by or "").strip():
        return _error("invalid_by", "谁裁的要记下来")
    rec = _org_entry(state, org_id)
    if rec is None:
        return _error("not_found", f"组织里没有 {org_id} 这一条")
    if in_force(rec):
        return _error("not_retired", "这一条本来就作数")
    entry = {"verdict": VERDICT_IN_FORCE, "reason": reason, "by": by, "at": at}
    fields: dict[str, Any] = {
        "org_standing": None,
        "org_standing_history": [*(rec.get("org_standing_history") or []), entry],
    }
    if rec.get("superseded_by_claim_id"):
        fields["superseded_by_claim_id"] = None
    updated = state.set_lifecycle_fields("claims", org_id, fields)
    if updated is None:
        return _error("not_found", f"组织里没有 {org_id} 这一条")
    return {"status": "success", "entry": updated}


# ── 更正提议（进待审）───────────────────────────────────────────────────────


def propose(state: Any, org_id: str, *, verdict: str, reason: str, by: str, at: str,
            origin: str, project_id: str = "", evidence: tuple[str, ...] | list[str] = (),
            source_home: str = "", superseded_by: str = "", about: str = "") -> dict:
    """提出一条更正，交给管理员裁。**同一个来源对同一条只挂一条在等**。

    `about` 是提出它的那一方看到的另一面（机械比对时是那条相反的结论）—— 管理员
    裁的时候两条要摆在一起看。
    """
    from core import kb_promotion

    if verdict not in RETIRING_VERDICTS:
        return _error("invalid_verdict", f"更正只能提 {' / '.join(RETIRING_VERDICTS)}")
    if origin not in ORIGINS:
        return _error("invalid_origin", f"来源只能是 {' / '.join(ORIGINS)}")
    reason = str(reason or "").strip()
    if not reason:
        return _error("reason_required", "要写为什么：管理员要知道它错在哪")
    if not str(by or "").strip():
        return _error("invalid_by", "谁提的要记下来")
    rec = _org_entry(state, org_id)
    if rec is None:
        return _error("not_found", f"组织里没有 {org_id} 这一条")
    if not in_force(rec):
        return _error("already_retired", "这一条已经不作数了")
    successor, refused = _the_successor(state, org_id, superseded_by)
    if refused:
        return refused
    proposal: dict[str, Any] = {
        "id": f"corr_{uuid.uuid4().hex[:12]}",
        "type": PROPOSAL_TYPE,
        "status": kb_promotion.REVIEW_PENDING,
        "org_id": org_id,
        # 管理员读的是提出时的那一版正文 —— 条目以后被改写，这条更正说的仍是它当时的样子。
        "statement": _statement(rec),
        "verdict": verdict,
        "because": reason,
        "origin": origin,
        "proposed_by": by,
        "project_id": project_id,
        "evidence_closure": [str(e) for e in evidence],
        "source_home": source_home,
        "proposed_at": at,
    }
    if successor:
        proposal["superseded_by"] = successor
    if about:
        proposal["about"] = about

    def same_thing(p: dict) -> bool:
        if not (p.get("type") == PROPOSAL_TYPE and p.get("status") == kb_promotion.REVIEW_PENDING
                and p.get("org_id") == org_id and p.get("origin") == origin):
            return False
        if origin == ORIGIN_CONTRADICTION:
            # 机械比对说的是「这一条和别的相反」—— 项目做完时比出来一次、它的结论被采纳时
            # 又比出来一次，是同一个问题，等一条就够。
            return True
        return p.get("project_id") == project_id and p.get("proposed_by") == by

    queued = kb_promotion.enqueue(proposal, unless=same_thing)
    if queued is None:
        return {"status": "success", "already": True, "proposal": None}
    return {"status": "success", "already": False, "proposal": queued}


def adopt(state: Any, proposal: dict, *, approved_by: str, at: str) -> dict:
    """认可一条更正 = 那一条不再作数。它已经不作数了（管理员先直接裁过）也算认可成了。"""
    rec = _org_entry(state, str(proposal.get("org_id") or ""))
    if rec is None:
        return _error("not_found", f"组织里没有 {proposal.get('org_id')} 这一条")
    if not in_force(rec):
        return {"status": "success", "entry": rec, "already": True}
    return retire(state, str(proposal["org_id"]), verdict=str(proposal.get("verdict") or ""),
                  reason=str(proposal.get("because") or ""), by=approved_by, at=at,
                  superseded_by=str(proposal.get("superseded_by") or ""),
                  proposal_id=str(proposal.get("id") or ""))


# ── 机械比对 ────────────────────────────────────────────────────────────────


def at_odds_with_the_organisation(state: Any, candidate: dict, *,
                                  exclude: tuple[str, ...] = ()) -> list[dict]:
    """组织里还作数、又和这一条方向相反的那些验证结论。有界：只看验证结论，判据保守。"""
    from core.org_dreaming import at_odds

    try:
        rows = state.list_kb("claims") or []
    except Exception:
        return []
    return [rec for rec in rows
            if isinstance(rec, dict) and rec.get("scope") == "org" and in_force(rec)
            and str(rec.get("id")) not in exclude
            and str(rec.get("org_kind") or "") == "verified_finding"
            and at_odds(candidate, rec)]
