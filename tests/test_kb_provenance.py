"""出处走查：信任不住在句子里，住在可走查的链里。

核心不变量：断链**如实报告**，不静默跳过 —— 一条走不到证据的 org 结论
正是最该被发现的东西（源项目归档后它已经悬空了）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core.kb_provenance import kb_provenance
from core.state import State

_AT = "2026-08-21T00:00:00Z"
_PROV = {"project_id": "p_src", "source_id": "claim_src",
         "approved_by": "wangd", "at": _AT}


def _state() -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p")


def _kinds(res: dict) -> list[str]:
    return [h["kind"] for h in res["hops"]]


def test_missing_record_is_an_error_not_an_empty_chain():
    res = kb_provenance(_state(), "claim_doesnotexist")
    assert res["status"] == "error" and res["code"] == "not_found"


def test_chain_reaches_anchor_artifact_and_run():
    """一条完整的链：claim → chunk → 外部锚 + 冻结产物 → 产出它的 run。"""
    st = _state()
    art = st.save_artifact("clean_results", "Scan", "结果表")
    st.mark_frozen(art["id"])
    ch, _ = st.write_kb("chunks", {
        "text": "结果表的关键选段", "source": "doi:10.1/evidence",
        "origin_artifact_id": art["id"]})
    cl, _ = st.write_kb("claims", {
        "claim_text": "一条有完整证据的断言", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [ch["id"]]})

    res = kb_provenance(st, cl["id"])
    assert res["intact"] is True
    kinds = _kinds(res)
    assert "project_claim" in kinds and "chunk" in kinds
    assert "external_anchor" in kinds, "外部锚必须可见 —— 那是核原文的入口"
    assert "artifact" in kinds

    art_hop = next(h for h in res["hops"] if h["kind"] == "artifact")
    assert art_hop["detail"]["frozen"] is True
    assert art_hop["detail"]["content_hash"], "内容哈希是防篡改的凭据"


def test_org_card_walks_back_to_its_source_project_claim():
    st = _state()
    ch, _ = st.write_kb("chunks", {"text": "证据", "source": "doi:10/a"})
    src, _ = st.write_kb("claims", {
        "claim_text": "项目内断言", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [ch["id"]]})
    org, _ = st.write_kb("claims", {
        "claim_text": "去项目化的知识卡", "statement": "去项目化的知识卡",
        "claim_type": "empirical", "org_kind": "verified_finding",
        "concept_ids": ["c"], "sources": [ch["id"]],
        "replication_count": 2, "confidence_basis": "两个项目实测",
        "scope": "org", "promoted_from": {**_PROV, "source_id": src["id"]}})

    res = kb_provenance(st, org["id"])
    kinds = _kinds(res)
    assert kinds[0] == "org_card"
    assert "promoted_from" in kinds
    assert "project_claim" in kinds, "必须走得回源项目条目"

    card = res["hops"][0]
    assert card["detail"]["replication_count"] == 2
    assert card["detail"]["confidence_basis"], "凭什么信 —— 一句话说清"


def test_org_card_without_provenance_is_reported_broken():
    """出处护栏拦的是新写入；老数据走查时必须如实说它断了。"""
    st = _state()
    rec = {"claim_text": "没有出处的 org 条目", "claim_type": "empirical",
           "org_kind": "verified_finding", "concept_ids": ["c"],
           "sources": ["doi:10/x"], "scope": "org", "id": "claim_orphan01"}
    st.write_kb  # 走查器读的是记录，不经写入校验
    from unittest.mock import patch

    with patch.object(State, "get_kb_record", lambda self, e, i: rec if i == "claim_orphan01" else None):
        res = kb_provenance(st, "claim_orphan01")
    assert res["intact"] is False
    assert any(h["kind"] == "promoted_from" and not h["ok"] for h in res["hops"])


def test_broken_artifact_link_is_surfaced():
    """源项目归档后产物没了 —— 这条"真理"已经悬空，必须被看见。"""
    st = _state()
    ch, _ = st.write_kb("chunks", {
        "text": "指向不存在产物的选段", "source": "doi:10/a",
        "origin_artifact_id": "clean_results__vanished"})
    cl, _ = st.write_kb("claims", {
        "claim_text": "断言", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [ch["id"]]})

    res = kb_provenance(st, cl["id"])
    assert res["intact"] is False
    assert "clean_results__vanished" in res["broken"]
    bad = next(h for h in res["hops"] if h["kind"] == "artifact")
    assert bad["ok"] is False and "归档" in bad["summary"]


def test_prereg_hop_answers_what_was_committed():
    st = _state()
    prereg = st.save_artifact("pre_registration", "P", "## 判据…")
    st.mark_frozen(prereg["id"])
    pch, _ = st.write_kb("chunks", {
        "text": "预注册选段", "source": "doi:10/prereg",
        "origin_artifact_id": prereg["id"]})
    ch, _ = st.write_kb("chunks", {"text": "证据", "source": "doi:10/e"})
    cl, _ = st.write_kb("claims", {
        "claim_text": "一条假设", "claim_type": "hypothesis",
        "concept_ids": ["c"], "sources": [ch["id"]],
        "prereg_chunk_id": pch["id"], "predicted_outcome": "预期 X",
        "falsification_criteria_text": "若 Y 则不成立"})

    res = kb_provenance(st, cl["id"])
    hop = next(h for h in res["hops"] if h["kind"] == "prereg")
    assert hop["ok"] is True and "承诺了什么判据" in hop["summary"]


def test_walk_is_bounded_and_cycle_safe():
    """闭包成环或超长时不许把 context 撑爆。"""
    st = _state()
    ch, _ = st.write_kb("chunks", {"text": "base", "source": "doi:10/a"})
    prev = None
    for i in range(60):
        rec, _ = st.write_kb("claims", {
            "claim_text": f"链条 {i}", "claim_type": "empirical",
            "concept_ids": ["c"],
            "sources": [prev] if prev else [ch["id"]]})
        prev = rec["id"]

    res = kb_provenance(st, prev)
    assert len(res["hops"]) <= 45, "骨架卡片必须有界"


def test_output_is_a_skeleton_not_full_text():
    """走查是为了知道能不能走到，不是把整条链灌进 context。"""
    st = _state()
    long_text = "证" * 5000
    ch, _ = st.write_kb("chunks", {"text": long_text, "source": "doi:10/a"})
    cl, _ = st.write_kb("claims", {
        "claim_text": long_text, "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [ch["id"]]})

    res = kb_provenance(st, cl["id"])
    for hop in res["hops"]:
        assert len(hop["summary"]) <= 200
