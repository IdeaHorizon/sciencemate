"""dreaming 是科学正典维护，不是记忆压缩。

三条纪律各配测试：合并保证据闭包、矛盾呈现而不抹平、作废不删除。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core import org_dreaming as dr
from core.state import State

_NOW = "2026-08-21T00:00:00Z"


def _state() -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p_org")


def _finding(state: State, statement: str, *, project: str,
             applicability: dict | None = None, sources=None,
             replication_count: int = 1, domain: str | None = None,
             **extra) -> dict:
    rec, _ = state.write_kb("claims", {
        "claim_text": statement, "statement": statement,
        "claim_type": "empirical", "org_kind": "verified_finding",
        "concept_ids": ["c"], "sources": sources or ["doi:10/x"],
        "applicability": applicability or {},
        "replication_count": replication_count,
        "scope": "org",
        "promoted_from": {"project_id": project, "source_id": "src",
                          "approved_by": "wangd", "at": _NOW},
        **({"domain": domain} if domain else {}),
        **extra})
    return rec


# ── 近重复归并：复利最具象的形态 ────────────────────────────────────────────


def test_identical_statements_merge_at_write_and_count_as_replication():
    """**逐字相同**的结论在写入时就合并了（内容寻址）—— 不留给 dreaming。

    关键是合并时别把跨项目复现吞掉：一律 max 会让两个项目独立得出同一条
    结论后 replication_count 仍是 1，而这是 org 卡置信度的唯一支撑，
    也是本平台相对真实实验室的独有能力。
    """
    st = _state()
    a = _finding(st, "逐字相同的一条结论", project="p_si", sources=["doi:10/a"])
    b = _finding(st, "逐字相同的一条结论", project="p_ge", sources=["doi:10/b"])

    assert a["id"] == b["id"], "内容寻址：同文即同条"
    assert b["replication_count"] == 2, "跨项目独立得出 = 真复现"
    assert [h["project_id"] for h in b["promotion_history"]] == ["p_si", "p_ge"]
    assert set(b["sources"]) == {"doi:10/a", "doi:10/b"}, "证据取并集"


def test_two_projects_near_duplicate_proposes_merge():
    """措辞不同的同一断言 —— 这才是 dreaming 要认的那一类。

    第二个项目没有"重新发现"，它加固了一条已有结论，适用范围自动变宽。
    """
    st = _state()
    a = _finding(st, "通用势在高压重构相变区能量误差显著偏大",
                 project="p_si", applicability={"systems_tested": ["Si"]},
                 sources=["doi:10/a"])
    b = _finding(st, "通用势在高压重构相变区的能量误差显著偏大一些",
                 project="p_ge", applicability={"systems_tested": ["Ge"]},
                 sources=["doi:10/b"])

    job = dr.find_near_duplicates(st)
    assert len(job.proposals) == 1
    p = job.proposals[0]
    assert set(p["ids"]) == {a["id"], b["id"]}
    assert p["merged_replication_count"] == 2, "复现记数累加"
    assert set(p["merged_applicability"]["systems_tested"]) == {"Si", "Ge"}, \
        "适用范围自动变宽"
    assert set(p["evidence_union"]) == {"doi:10/a", "doi:10/b"}, \
        "证据取并集 —— 合并不许丢出处"


def test_same_project_duplicates_are_not_compounding():
    """同项目内的重复是晋升时的问题，不是跨项目复利。"""
    st = _state()
    _finding(st, "同一条结论", project="p_one")
    _finding(st, "同一条结论 略有不同措辞", project="p_one")
    assert dr.find_near_duplicates(st).proposals == []


def test_merge_is_proposal_not_auto_applied():
    """判"是不是同一条断言"要语义判断，机械层不该定夺。"""
    st = _state()
    _finding(st, "某条结论 涉及 A B C D 四个要点", project="p1")
    _finding(st, "某条结论 涉及 A B C D 四个要点 略有差异", project="p2")
    job = dr.find_near_duplicates(st)
    assert job.applied == [] and job.proposals


def test_unrelated_findings_do_not_merge():
    st = _state()
    _finding(st, "高压相变区能量误差偏大", project="p1")
    _finding(st, "配体交换速率受溶剂极性控制", project="p2")
    assert dr.find_near_duplicates(st).proposals == []


# ── 矛盾呈现：不裁决 ────────────────────────────────────────────────────────


def test_conflicting_findings_surface_an_open_question():
    """组内矛盾是科研机会，不是数据缺陷 —— KB 自己产出课题。"""
    st = _state()
    a = _finding(st, "该压力区间误差显著超出基准", project="p_mace",
                 applicability={"regime": "P>30GPa"})
    b = _finding(st, "该压力区间误差不显著，未超出基准", project="p_seven",
                 applicability={"regime": "P>30GPa"})

    job = dr.find_contradictions(st)
    kinds = [x["kind"] for x in job.applied]
    assert "contradicts_edge" in kinds
    q = next(x for x in job.applied if x["kind"] == "open_question")
    assert set(q["contradicts"]) == {a["id"], b["id"]}
    assert q["status"] == "open"
    assert not job.proposals, "矛盾不走裁决提议 —— 它是议程，不是待办"


def test_no_contradiction_when_conditions_do_not_overlap():
    """谈的不是同一件事就不算冲突。"""
    st = _state()
    _finding(st, "误差显著超出基准", project="p1",
             applicability={"regime": "P>30GPa"})
    _finding(st, "误差不显著", project="p2",
             applicability={"regime": "P<5GPa"})
    assert dr.find_contradictions(st).applied == []


def test_agreeing_findings_are_not_contradictions():
    st = _state()
    _finding(st, "误差显著超出基准", project="p1", applicability={"r": "x"})
    _finding(st, "误差显著超出基准", project="p2", applicability={"r": "x"})
    assert not any(x["kind"] == "contradicts_edge"
                   for x in dr.find_contradictions(st).applied)


# ── 老化质询：dead_end 永不老化 ─────────────────────────────────────────────


def test_never_injected_card_is_dormant_not_stale():
    """从没被送出去的卡 —— 是它的**域**休眠了，不是这张卡过时了。

    两种"没用上"处置完全不同：送得出去没人用该重写或降级；从没送出去该
    原样留着。合并成一个 stale 会把它们搞混。
    """
    st = _state()
    rec = _finding(st, "一条从没被送到任何新项目面前的结论", project="p1",
                   created_at="2020-01-01T00:00:00Z")
    job = dr.find_stale_suspects(st, now_iso=_NOW)
    hit = next(x for x in job.applied if x["id"] == rec["id"])
    assert hit["kind"] == "dormant_domain"
    assert hit["injected"] == 0
    assert "原样留着" in hit["note"]
    assert st.get_kb_record("claims", rec["id"]) is not None, "标记 ≠ 删除"


def test_injected_but_uncited_card_becomes_stale_suspect():
    """送出去过、没人引用 —— 这才是**卡的问题**，待质询。"""
    from core.org_usage import record_injection

    st = _state()
    rec = _finding(st, "一条送出去过但没人引用的结论", project="p1",
                   created_at="2020-01-01T00:00:00Z")
    for pid in ("p_new_a", "p_new_b"):
        record_injection(st, [rec["id"]], project_id=pid,
                         at="2020-02-01T00:00:00Z")

    job = dr.find_stale_suspects(st, now_iso=_NOW)
    hit = next(x for x in job.applied if x["id"] == rec["id"])
    assert hit["kind"] == "stale_suspect"
    assert hit["injected"] == 2 and hit["cited"] == 0
    assert "没人用" in hit["note"]
    assert "不删除" in hit["note"]
    assert st.get_kb_record("claims", rec["id"]) is not None, "标记 ≠ 删除"


def test_recently_cited_card_does_not_age(  ):
    """判据从真实使用来，不从日历来：被引用过的卡不该按创建时间老化。"""
    st = _state()
    rec = _finding(st, "一条最近被引用的结论", project="p1",
                   created_at="2020-01-01T00:00:00Z")
    st.write_kb("claims", {
        "claim_text": "新项目基于该 org 结论做的观察",
        "claim_type": "empirical", "scope": "project",
        "concept_ids": [], "orphan_reason": "stub",
        "sources": [rec["id"]], "confidence": 0.7,
        "created_at": _NOW,
    })
    job = dr.find_stale_suspects(st, now_iso=_NOW)
    assert rec["id"] not in [x["id"] for x in job.applied], (
        "被引用过还按日历老化 —— last_cited_at 那个没人写的字段又回来了")


def test_dead_ends_never_go_stale():
    """十年没人撞不代表它过时，只代表这十年没人踩坑。"""
    st = _state()
    rec, _ = st.write_kb("claims", {
        "claim_text": "一条很老的死路", "claim_type": "dead_end",
        "org_kind": "dead_end", "concept_ids": ["c"], "sources": ["doi:10/x"],
        "dont_repeat_reason": "试过，失败了",
        "last_cited_at": "2015-01-01T00:00:00Z", "scope": "org",
        "promoted_from": {"project_id": "p0", "source_id": "s",
                          "approved_by": "w", "at": _NOW}})
    job = dr.find_stale_suspects(st, now_iso=_NOW)
    assert rec["id"] not in [x["id"] for x in job.applied]


# ── 血统监控：反僵化最后一道闸 ──────────────────────────────────────────────


def test_ossified_exemplar_surfaces_for_replacement():
    """一份范例被所有人照抄且成绩不再提升 = 从加速器变成了模具。"""
    job = dr.find_ossified_exemplars(
        _state(), usage={"exemplar_prereg_1": [8.0, 8.2, 8.1, 7.9, 7.8, 7.9]})
    assert len(job.applied) == 1
    assert job.applied[0]["kind"] == "open_question"
    assert "板结" in job.applied[0]["question"]


def test_improving_exemplar_is_not_flagged():
    job = dr.find_ossified_exemplars(
        _state(), usage={"e1": [6.0, 6.5, 7.0, 8.0, 8.5, 9.0]})
    assert job.applied == []


def test_thin_usage_is_not_judged():
    """样本太少谈不上板结 —— 别拿三个点判一份范例的死刑。"""
    assert dr.find_ossified_exemplars(_state(), usage={"e1": [7.0, 8.0]}).applied == []


# ── 编排：有界 checklist ────────────────────────────────────────────────────


def test_dreaming_is_a_bounded_checklist():
    """逐项过完即收工 —— 这条纪律是拿 51,937 次 search_kb 换来的。"""
    st = _state()
    for i in range(30):
        _finding(st, f"结论 {i}", project=f"p{i}")
    out = dr.run_dreaming(st, now_iso=_NOW)
    assert out["status"] == "success"
    assert {j["job"] for j in out["jobs"]} == {
        "near_duplicate_merge", "contradiction_surfacing", "stale_suspect",
        "exemplar_lineage", "canon_refresh"}
    assert out["proposal_count"] <= dr.MAX_PROPOSALS_PER_RUN


# ── 分词：不报错、只是永远不干活的那一类 bug ────────────────────────────────


def test_similarity_works_on_chinese():
    """中文不写空格 —— 按非词字符切会把整句当成一个 token。

    第一版就是这么写的：任何两句中文的重叠恒为 0，近重复归并对中文
    **永远不触发**，而本平台的研究记录大量是中文。它不报错，只是不干活。
    """
    near = dr._token_overlap("通用势在高压重构相变区能量误差显著偏大",
                             "通用势在高压重构相变区的能量误差显著偏大一些")
    assert near >= dr.NEAR_DUPLICATE_MIN_OVERLAP

    unrelated = dr._token_overlap("高压相变区能量误差偏大",
                                  "配体交换速率受溶剂极性控制")
    assert unrelated < 0.2, "不相关的句子不该被判成近重复"


def test_similarity_works_on_english_and_mixed():
    assert dr._token_overlap(
        "universal MLIP energy error exceeds baseline above 30GPa",
        "universal MLIP energy error exceeds the baseline above 30GPa",
    ) >= dr.NEAR_DUPLICATE_MIN_OVERLAP
    assert dr._token_overlap("MLIP 在 30GPa 误差偏大",
                             "MLIP 在 30GPa 的误差偏大") > 0.5


def test_wrong_merges_are_costlier_than_missed_ones():
    """阈值保守是刻意的：错合并会把两条不同结论揉成一条，
    而证据链已经并起来了，事后拆不回去。"""
    assert dr.NEAR_DUPLICATE_MIN_OVERLAP >= 0.7
    assert dr._token_overlap("A 方法在低温段稳定", "B 方法在高温段失稳") \
        < dr.NEAR_DUPLICATE_MIN_OVERLAP


# ── 6. 综述刷新：沉淀链的驱动器 ─────────────────────────────────────────────


def test_canon_refresh_job_appears_when_a_domain_accumulates():
    """没有它，几年后就是一堆谁也不敢信的半相关卡片。"""
    from core import org_canon as canon

    st = _state()
    for i in range(canon.CANON_REFRESH_THRESHOLD):
        _finding(st, f"某域结论 {i}", project=f"p{i}", domain="mlip")

    job = dr.find_canon_refreshes(st)
    assert len(job.proposals) == 1
    assert job.proposals[0]["domain"] == "mlip"
    assert "降权不删除" in " ".join(job.notes)


def test_canon_refresh_is_silent_below_threshold():
    st = _state()
    _finding(st, "只有一条", project="p1", domain="mlip")
    assert dr.find_canon_refreshes(st).proposals == []
