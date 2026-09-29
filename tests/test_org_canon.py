"""正典层：卡片是中间形态，正典是沉淀形态。

核心不变量：吸收 ≠ 删除（账本永远走得回去）；刷新按增量触发不按时间；
分域不猜（猜错的分域比没有分域更糟）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core import org_canon as canon
from core.state import State

_AT = "2026-08-21T00:00:00Z"
_PROV = {"project_id": "p_prev", "source_id": "s", "approved_by": "wangd", "at": _AT}


def _state() -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p_org")


def _org_card(state: State, statement: str, *, domain: str | None = None,
              kind: str = "verified_finding") -> str:
    rec, _ = state.write_kb("claims", {
        "claim_text": statement, "statement": statement,
        "claim_type": "empirical" if kind != "dead_end" else "dead_end",
        "org_kind": kind, "concept_ids": ["c"], "sources": ["doi:10/x"],
        "scope": "org", "promoted_from": dict(_PROV),
        **({"domain": domain} if domain else {}),
        **({"dont_repeat_reason": "试过失败了"} if kind == "dead_end" else {}),
    })
    return rec["id"]


# ── 分域：不猜 ──────────────────────────────────────────────────────────────


def test_undeclared_domain_falls_to_unsorted_not_a_guess():
    """猜错的分域比没有分域更糟 —— 它会让开题注入送错知识，而错误的
    "本组已知"比没有已知更有害（人会照着它走）。"""
    assert canon.domain_of({"statement": "关于高压相变的一条结论"}) == "unsorted"
    assert canon.domain_of({"domain": "mlip/high-pressure"}) == "mlip/high-pressure"


# ── 刷新：按增量触发 ────────────────────────────────────────────────────────


def test_refresh_triggers_on_accumulation_not_on_a_timer():
    """没新东西时重写综述纯烧钱 —— 按增量触发才对。"""
    st = _state()
    for i in range(canon.CANON_REFRESH_THRESHOLD - 1):
        _org_card(st, f"结论 {i}", domain="mlip")
    assert canon.refresh_jobs(st) == []

    _org_card(st, "压垮阈值的那一条", domain="mlip")
    jobs = canon.refresh_jobs(st)
    assert len(jobs) == 1 and jobs[0]["domain"] == "mlip"
    assert len(jobs[0]["absorb"]) == canon.CANON_REFRESH_THRESHOLD


def test_refresh_is_a_proposal_not_an_auto_write():
    """综述是**叙述** —— 机械层给不出脉络与争论，只能指出"该刷了"。"""
    st = _state()
    for i in range(canon.CANON_REFRESH_THRESHOLD):
        _org_card(st, f"结论 {i}", domain="mlip")
    job = canon.refresh_jobs(st)[0]
    assert "instruction" in job and "1–3 页" in job["instruction"]
    assert canon.canon_for_domain(st, "mlip") is None, "提议不该已经写好了"


def test_open_questions_are_listed_for_the_canon():
    st = _state()
    for i in range(canon.CANON_REFRESH_THRESHOLD):
        _org_card(st, f"结论 {i}", domain="mlip")
    q = _org_card(st, "两条结论方向相反，是条件划分不够细吗？",
                  domain="mlip", kind="open_question")
    job = canon.refresh_jobs(st)[0]
    assert q in job["open_questions"], "开放问题要单列一节 —— 那是研究议程"


# ── 写入与吸收 ──────────────────────────────────────────────────────────────


def test_writing_canon_records_absorption_and_bumps_version():
    st = _state()
    ids = [_org_card(st, f"结论 {i}", domain="mlip") for i in range(3)]

    r1 = canon.write_canon(st, domain="mlip", body="# MLIP 综述\n\n第一版…",
                           absorbed_ids=ids[:2], at=_AT)
    assert r1["status"] == "success" and r1["version"] == 1

    r2 = canon.write_canon(st, domain="mlip", body="# MLIP 综述\n\n第二版…",
                           absorbed_ids=[ids[2]], at=_AT)
    assert r2["version"] == 2
    assert r2["absorbed_count"] == 3, "吸收关系累积，不是每次覆盖"


def test_absorbed_cards_are_downranked_not_deleted():
    """吸收 ≠ 删除 —— 账本永远走得回去，正典只是消费面的入口。"""
    st = _state()
    cid = _org_card(st, "被吸收的一条结论", domain="mlip")
    canon.write_canon(st, domain="mlip", body="# 综述\n\n引用了它…",
                      absorbed_ids=[cid], at=_AT)

    assert canon.is_absorbed(st, cid) is True
    assert st.get_kb_record("claims", cid) is not None, "卡片必须还在"


def test_empty_canon_body_is_refused():
    """机械层写不出叙述，这一步必须有内容 —— 空综述是假装做过了。"""
    out = canon.write_canon(_state(), domain="mlip", body="   ",
                            absorbed_ids=[], at=_AT)
    assert out["status"] == "error" and out["code"] == "empty_canon"


def test_oversize_canon_is_flagged_for_splitting():
    """写不下说明该拆子域，不是把综述写成第二个账本。"""
    st = _state()
    out = canon.write_canon(st, domain="mlip",
                            body="x" * (canon.CANON_TARGET_CHARS + 100),
                            absorbed_ids=[], at=_AT)
    assert out["oversize"] is True


# ── 消费：正典优先 ──────────────────────────────────────────────────────────


def test_canon_for_domain_returns_latest_version():
    st = _state()
    canon.write_canon(st, domain="mlip", body="第一版", absorbed_ids=[], at=_AT)
    canon.write_canon(st, domain="mlip", body="第二版", absorbed_ids=[], at=_AT)
    got = canon.canon_for_domain(st, "mlip")
    assert got["version"] == 2 and "第二版" in got["body"]


def test_domains_are_independent():
    st = _state()
    for i in range(canon.CANON_REFRESH_THRESHOLD):
        _org_card(st, f"MLIP 结论 {i}", domain="mlip")
    _org_card(st, "史学方法的一条结论", domain="history/method")

    jobs = {j["domain"] for j in canon.refresh_jobs(st)}
    assert jobs == {"mlip"}, "只有满阈值的域该刷新"


def test_survey_reports_pending_and_absorbed_per_domain():
    st = _state()
    ids = [_org_card(st, f"结论 {i}", domain="mlip") for i in range(4)]
    canon.write_canon(st, domain="mlip", body="# 综述", absorbed_ids=ids[:3], at=_AT)

    ds = next(d for d in canon.survey_domains(st) if d.domain == "mlip")
    assert ds.canon_version == 1
    assert list(ds.pending_ids) == [ids[3]]
    assert len(ds.absorbed_ids) == 3
