"""晋升是 org 唯一的入口 —— 三查、双车道、证据闭包原子。

判据全部是**行为**：造盘面 → 扫盘 → 看候选与阻塞理由 → 落地 → 验 org 记录。
不断言实现细节（哪个函数被调用），只断言"在这种盘面下，系统该做什么"。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core import kb_promotion as kp
from core.state import State

_AT = "2026-08-21T00:00:00Z"


def _state(project_id: str = "p_mlip_highp") -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id=project_id)


def _seed_evidence(state: State) -> str:
    ch, _ = state.write_kb("chunks", {
        "text": "MACE-MP0 对 Si 相变的预测偏差随压力单调增大，30 GPa 以上超 0.15 eV/atom",
        "source": "doi:10.1038/s41524-023-01012-9",
    })
    return ch["id"]


def _seed_claim(state: State, chunk_id: str, *, claim_type: str = "empirical") -> str:
    cl, _ = state.write_kb("claims", {
        "claim_text": "本项目 MACE-MP0 基线在 30 GPa 以上对 Si 相变能量偏差 > 0.15 eV/atom",
        "claim_type": claim_type, "concept_ids": ["concept_mlip"],
        "sources": [chunk_id],
        "scope_dimensions": {"model": "MACE-MP0", "seed": 42, "run_id": "r1"},
    })
    return cl["id"]


def _make_terminal(state: State) -> None:
    """造终态：一份已冻结的 manuscript。

    冻结的事实只有账本上的 freeze 行一个出处（`state.mark_frozen`）——
    save 时手写 `metadata.frozen` 会被剥掉，造不出冻结记录。
    """
    saved = state.save_artifact("manuscript", "Paper", "# 高压 MLIP 可靠性\n\n正文…")
    state.mark_frozen(saved["id"])
    assert (state.read_artifact(saved["id"])["metadata"]["frozen"]) is True


def _good_card() -> dict:
    return {
        "domain": "cond-mat.mtrl-sci/mlip-robustness",  # 骨架父 + 本地叶（registrable）
        "statement": "通用 MLIP（foundation-model 类）在 >30 GPa 重构型相变区，"
                     "相对能量误差显著超出常压基准（实测 >0.15 eV/atom）",
        "applicability": {"model_family": "universal MLIP",
                          "regime": "reconstructive transition, P>30GPa",
                          "systems_tested": ["Si"]},
        "why": "训练集以常压结构为主，高压重构相的局域配位环境落在分布外，"
               "描述子外推导致能量面失真",
        "practice": "该压力区间的相变能量不要直接采信通用 MLIP，"
                    "需用 DFT 抽样校正或改用该压力段微调过的势",
        "confidence_basis": "单项目实测（Si），证据链含 2 篇文献锚点",
        "evidence": [],
    }


# ── 三查 ────────────────────────────────────────────────────────────────────


def test_not_terminal_blocks_everything():
    """过程中不许晋升 —— 跨项目复利的判据在终态才具备。"""
    st = _state()
    _seed_claim(st, _seed_evidence(st))

    res = kp.promotion_scan(st)
    assert res["terminal"] is False
    assert res["eligible"] == []
    assert all("terminal_batch" in " ".join(b["blocking"]) for b in res["blocked"])


def test_unfrozen_evidence_blocks_promotion():
    """证据来源产物没冻就不能晋升 —— 它还会变，晋升上去就是断链。

    注意查的是 chunk.origin_artifact_id 那一跳：claim.sources 按 schema 只能
    装 chunk/claim/外部 URI，产物必须先登记成 chunk 才能被引用。
    """
    st = _state()
    _make_terminal(st)
    art = st.save_artifact("clean_results", "Scan", "raw numbers")     # 未冻结
    ch, _ = st.write_kb("chunks", {
        "text": "selection from the unfrozen result table",
        "source": "doi:10.1/internal-evidence",
        "origin_artifact_id": art["id"],
    })
    cl, _ = st.write_kb("claims", {
        "claim_text": "a finding resting on unfrozen evidence",
        "claim_type": "empirical", "concept_ids": ["c"], "sources": [ch["id"]],
    })
    res = kp.promotion_scan(st, drafts={cl["id"]: _good_card()})
    blocked = next(b for b in res["blocked"] if b["source_id"] == cl["id"])
    assert "evidence_frozen" in " ".join(blocked["blocking"])


def test_missing_why_blocks_the_card():
    """why 必填 —— 缺机制的只是数据，不是知识；这一查同时是准入审查。"""
    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    card = _good_card()
    card["why"] = ""

    res = kp.promotion_scan(st, drafts={cid: card})
    blocked = next(b for b in res["blocked"] if b["source_id"] == cid)
    assert "why" in " ".join(blocked["blocking"])


def test_project_deixis_blocks_the_card():
    """正文还写着"本项目"就说明没为新读者改写过。"""
    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    card = _good_card()
    card["statement"] = "本项目发现通用 MLIP 在高压区误差偏大"

    res = kp.promotion_scan(st, drafts={cid: card})
    blocked = next(b for b in res["blocked"] if b["source_id"] == cid)
    assert "项目内指代" in " ".join(blocked["blocking"])


def test_leaked_project_parameters_block_the_card():
    """seed/run_id 是项目性的具体形态 —— 必须改写成适用条件，不能原样带走。"""
    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    card = _good_card()
    card["applicability"] = {"model_family": "universal MLIP", "seed": 42}

    res = kp.promotion_scan(st, drafts={cid: card})
    blocked = next(b for b in res["blocked"] if b["source_id"] == cid)
    assert "seed" in " ".join(blocked["blocking"])


# ── 双车道 ──────────────────────────────────────────────────────────────────


def test_lanes_split_judgment_from_mechanics():
    """书目/死路走机械车道（判据客观）；验证结论/配方要人批。

    分层的理由是量：不分层则组织吞吐下每年数千个批准决策，inbox 必死。
    """
    st = _state()
    _make_terminal(st)
    ch = _seed_evidence(st)
    finding = _seed_claim(st, ch)                              # empirical → 人批
    dead, _ = st.write_kb("claims", {
        "claim_text": "相变点附近用 Berendsen 会产生假中间相",
        "claim_type": "dead_end", "concept_ids": ["c"], "sources": [ch],
        "dont_repeat_reason": "实测两轮复现",
    })

    res = kp.promotion_scan(st, drafts={finding: _good_card()})
    mech_ids = {c.source_id for c in res["mechanical"]}
    human_ids = {c.source_id for c in res["human_batch"]}

    assert dead["id"] in mech_ids, "带证据的死路走机械车道（广播义务优先）"
    assert ch in mech_ids, "被采纳 claim 引用过的书目 chunk 走机械车道"
    assert finding in human_ids, "验证结论要人批"


def test_hypothesis_and_synthesis_never_promote():
    """项目内的思考与记账不是跨项目知识。"""
    st = _state()
    _make_terminal(st)
    ch = _seed_evidence(st)
    other, _ = st.write_kb("chunks", {"text": "second", "source": "doi:10/second"})
    base, _ = st.write_kb("claims", {
        "claim_text": "base empirical for synthesis sources",
        "claim_type": "empirical", "concept_ids": ["c"], "sources": [ch]})
    base2, _ = st.write_kb("claims", {
        "claim_text": "second empirical for synthesis sources",
        "claim_type": "empirical", "concept_ids": ["c"], "sources": [other["id"]]})
    prereg_chunk, _ = st.write_kb("chunks", {
        "text": "frozen prereg selection", "source": "doi:10/prereg"})
    st.write_kb("claims", {
        "claim_text": "a hypothesis", "claim_type": "hypothesis",
        "concept_ids": ["c"], "sources": [ch],
        "prereg_chunk_id": prereg_chunk["id"],
        "predicted_outcome": "预期在 30GPa 以上偏差单调增大",
        "falsification_criteria_text": "若观察到 X 则本条不成立"})
    st.write_kb("claims", {
        "claim_text": "a synthesis", "claim_type": "synthesis",
        "concept_ids": ["c"], "sources": [base["id"], base2["id"]]})
    res = kp.promotion_scan(st)
    promoted_claim_ids = {c.source_id for c in res["candidates"]
                          if c.source_kind == "claims"}
    for rec in st.list_kb("claims"):
        if rec.get("claim_type") in ("hypothesis", "synthesis", "conjecture"):
            assert rec["id"] not in promoted_claim_ids, rec["claim_type"]


def test_unanchored_chunks_are_not_biblio():
    """没有外部锚的 chunk 不是书目 —— 它无法被别的项目核验。"""
    st = _state()
    _make_terminal(st)
    ch, _ = st.write_kb("chunks", {"text": "内部选段", "source": "artifact:local"})
    cl, _ = st.write_kb("claims", {
        "claim_text": "x", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [ch["id"]]})
    res = kp.promotion_scan(st)
    assert ch["id"] not in {c.source_id for c in res["candidates"]}


# ── 证据闭包 ────────────────────────────────────────────────────────────────


def test_evidence_closure_is_transitive():
    st = _state()
    c1, _ = st.write_kb("chunks", {"text": "base", "source": "doi:10/a"})
    c2, _ = st.write_kb("chunks", {"text": "mid", "source": "doi:10/b"})
    inner, _ = st.write_kb("claims", {
        "claim_text": "inner", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [c1["id"]]})
    inner2, _ = st.write_kb("claims", {
        "claim_text": "inner2", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": [c2["id"]]})
    outer, _ = st.write_kb("claims", {
        "claim_text": "outer", "claim_type": "synthesis",
        "concept_ids": ["c"], "sources": [inner["id"], inner2["id"]]})

    closure = set(kp.evidence_closure(st, outer["id"]))
    assert {inner["id"], inner2["id"], c1["id"], c2["id"]} <= closure


def test_promotion_carries_the_whole_closure_atomically():
    """结论落 org 时，支撑它的证据必须已经在 org —— 否则是断链的"真理"。"""
    st = _state()
    _make_terminal(st)
    ch = _seed_evidence(st)
    cid = _seed_claim(st, ch)

    res = kp.promotion_scan(st, drafts={cid: _good_card()})
    cand = next(c for c in res["human_batch"] if c.source_id == cid)
    out = kp.promote(st, cand, approved_by="wangd",
                     project_id="p_mlip_highp", at=_AT)
    assert out["status"] == "success"

    org_claims = [r for r in st.list_kb("claims") if r.get("scope") == "org"]
    org_biblio = [r for r in st.list_kb("chunks") if r.get("scope") == "org"]
    assert len(org_claims) == 1 and len(org_biblio) == 1, "闭包没跟着走"
    assert org_biblio[0]["source"] == "doi:10.1038/s41524-023-01012-9"
    assert org_biblio[0]["group_readings"][0]["project_id"] == "p_mlip_highp"

    card = org_claims[0]
    assert card["promoted_from"]["approved_by"] == "wangd"
    assert card["promoted_from"]["source_id"] == cid
    assert card["why"] and card["applicability"]
    assert card["replication_count"] == 1, "首次晋升 = 一次观察"
    assert card["sources"] == [org_biblio[0]["id"]], \
        "org 卡的 sources 应指向已晋升的 org 书目条目"


def test_original_project_record_stays_put():
    """原条目留在原地 —— 两层各自演化，出处靠回链不靠搬走。"""
    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    res = kp.promotion_scan(st, drafts={cid: _good_card()})
    kp.promote(st, next(c for c in res["human_batch"] if c.source_id == cid),
               approved_by="wangd", project_id="p", at=_AT)

    src = st.get_kb_record("claims", cid)
    assert src is not None and src["scope"] == "project"


def test_ineligible_candidate_cannot_be_promoted():
    """扫盘说不合格，落地就必须拒绝 —— 不能靠调用方自觉。"""
    st = _state()
    cid = _seed_claim(st, _seed_evidence(st))          # 未终态
    res = kp.promotion_scan(st)
    cand = next(c for c in res["candidates"] if c.source_id == cid)
    out = kp.promote(st, cand, approved_by="wangd", project_id="p", at=_AT)
    assert out["status"] == "error"
    assert out["code"] == "candidate_not_eligible"


def test_promoted_records_satisfy_the_provenance_guard():
    """晋升写入必须自然满足 P1 的出处护栏（不是靠豁免）。"""
    from shared.lib.kb_schema import org_provenance_errors

    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    res = kp.promotion_scan(st, drafts={cid: _good_card()})
    kp.promote(st, next(c for c in res["human_batch"] if c.source_id == cid),
               approved_by="wangd", project_id="p", at=_AT)

    for entity in ("claims", "chunks"):
        for rec in st.list_kb(entity):
            if rec.get("scope") == "org":
                assert org_provenance_errors(rec) == [], rec.get("id")


# ── 范例预筛（RFC §19.2 第一关）────────────────────────────────────────────


def _frozen_prereg(state: State) -> str:
    saved = state.save_artifact(
        "pre_registration", "Prereg",
        "## Inquiry Contract\n\n### Q1: …\n- decides_a_proposition: yes\n")
    state.mark_frozen(saved["id"])
    return saved["id"]


def _decided_hypothesis(state: State, *, status: str) -> None:
    """造一条已裁决的假设（validated 或 refuted）。"""
    ch, _ = state.write_kb("chunks", {"text": "prereg sel", "source": "doi:10/pre"})
    cl, _ = state.write_kb("claims", {
        "claim_text": f"a hypothesis judged {status}", "claim_type": "hypothesis",
        "concept_ids": ["c"], "sources": [ch["id"]],
        "prereg_chunk_id": ch["id"], "predicted_outcome": "预期 X",
        "falsification_criteria_text": "若观察到 Y 则不成立",
        "status": status,
    })


def test_exemplars_need_terminal():
    st = _state()
    _frozen_prereg(st)
    assert kp.exemplar_candidates(st) == []


def test_frozen_clean_artifacts_become_candidates():
    st = _state()
    _make_terminal(st)
    aid = _frozen_prereg(st)
    cands = {c["artifact_id"] for c in kp.exemplar_candidates(st)}
    assert aid in cands


# test_artifacts_with_failed_checks_are_not_exemplars 已随 QC 层删除
# （2026-08-22）：没有任何路径再把 failed_quality_checks 写进 artifact
# metadata，「门禁欠账」这个概念随层消失；样板质量由 curator 审。


def test_unfrozen_artifacts_are_not_exemplars():
    """范例是给人照着写的，必须是不会再变的那一版。"""
    st = _state()
    _make_terminal(st)
    st.save_artifact("pre_registration", "Draft", "still editing")
    assert kp.exemplar_candidates(st) == []


def test_honest_refutation_counts_as_a_good_exemplar():
    """把自己的假设如实翻成 refuted 的项目，恰恰最该当范例。

    把"结论好看"当判据，就是在教下一个项目粉饰（E2E v26 那份记录是正面样本）。
    """
    st = _state()
    _make_terminal(st)
    _frozen_prereg(st)
    _decided_hypothesis(st, status="refuted")

    cand = kp.exemplar_candidates(st)[0]
    assert cand["signals"]["includes_honest_refutation"] is True
    assert cand["signals"]["closure_honest"] is True


def test_open_hypotheses_mark_closure_dishonest():
    """还有没裁决的问题 = 闭合不诚实，人批时看得见这个信号。"""
    st = _state()
    _make_terminal(st)
    _frozen_prereg(st)
    _decided_hypothesis(st, status="provisional")

    cand = kp.exemplar_candidates(st)[0]
    assert cand["signals"]["closure_honest"] is False
    assert cand["signals"]["hypotheses_open"] >= 1


def test_mechanical_prescreen_never_decides():
    """机械信号衡量纪律不衡量重要性 —— 只出候选，不打分不排序不定夺。

    选拔的裁判在另外两关：人批（品味）与使用验证（模仿者成绩）。
    writing judge 掷硬币烧掉 143M tokens —— 范例选拔不走模型打分。
    """
    st = _state()
    _make_terminal(st)
    _frozen_prereg(st)
    survey = st.save_artifact("survey_report", "Survey", "## 综述\n…")
    st.mark_frozen(survey["id"])

    cands = kp.exemplar_candidates(st)
    assert len(cands) >= 2
    for c in cands:
        assert c["status"] == "awaiting_human_selection"
        assert c["why_good"] is None, "点评由人写 —— 它必须指向决策而不是格式"
        assert "score" not in c and "rank" not in c


# ── 子因必须机读 ────────────────────────────────────────────────────────────
#
# 一道检查有几种失败方式时，只给一句人话 reason，调用方就会按检查的**名字**
# 去理解失败原因。实测于 2026-08-21 的真实数据回放：脚本读到 deprojectified
# 失败，报成「含项目指代 132/132」，实际全部是卡片字段没填。


def test_deprojectified_distinguishes_its_failure_modes():
    """四种失败方式必须给出四个不同的 cause，不能都靠读 reason 猜。"""
    full = {f: "写满了" for f in kp.KNOWLEDGE_CARD_FIELDS}
    full["applicability"] = {"regime": "低温段"}
    full["domain"] = "cond-mat.stat-mech"       # 域有自己的注册表校验，单测另钉

    assert kp.check_deprojectified(None).cause == "no_draft"

    missing = dict(full, why="")
    assert kp.check_deprojectified(missing).cause == "missing_card_fields"

    no_applic = dict(full, applicability={})
    assert kp.check_deprojectified(no_applic).cause == "missing_applicability"

    deixis = dict(full, statement=f"{next(iter(kp._PROJECT_DEIXIS))} 里成立")
    assert kp.check_deprojectified(deixis).cause == "project_deixis"

    leaked_key = next(iter(kp.PROJECT_SPECIFIC_DIMENSIONS))
    leaked = dict(full, applicability={leaked_key: 42})
    assert kp.check_deprojectified(leaked).cause == "leaked_dimensions"


def test_passing_check_carries_no_cause():
    """通过时不该留个空壳子因，免得调用方拿它当分支条件。"""
    full = {f: "写满了" for f in kp.KNOWLEDGE_CARD_FIELDS}
    full["applicability"] = {"regime": "低温段"}
    full["domain"] = "cond-mat.stat-mech"
    chk = kp.check_deprojectified(full)
    assert chk.passed is True
    assert chk.cause == ""


def test_card_without_domain_cannot_be_promoted():
    """填不进任何领域的卡片 = 一张永远不会被读到的卡片。

    域是**送达的地址**：正典层按域组织活综述，开题注入按域命中。
    `domain_of` 明确不猜 —— 猜错的分域比没有分域更糟。
    2026-08-21 真实数据回放：224 条 org 条目全部无域，正典层于是提出把
    LAMMPS + 元胞自动机 + MLIP 的结论吸收进同一篇 1–3 页综述。
    """
    card = _good_card()
    card.pop("domain")
    chk = kp.check_deprojectified(card)
    assert chk.passed is False
    assert chk.cause == "missing_card_fields"
    assert "domain" in chk.reason


def test_promoted_card_carries_its_domain_to_the_org_record():
    """契约要送到落盘那一层 —— 卡片上填了域，org 记录上必须查得到。"""
    from core import org_canon

    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    card = _good_card()
    scan = kp.promotion_scan(st, drafts={cid: card})
    cand = next(c for c in scan["human_batch"] if c.source_id == cid)
    res = kp.promote(st, cand, project_id="p_si", approved_by="wangd",
                     at="2026-08-21T00:00:00Z")
    assert res["status"] == "success"
    rec = st.get_kb_record("claims", res["org_id"])
    assert rec.get("domain") == card["domain"]
    assert org_canon.domain_of(rec) == card["domain"]
    assert org_canon.domain_of(rec) != org_canon.UNSORTED


def test_unfiled_backlog_is_reported_not_turned_into_a_survey_job():
    """`unsorted` 是"没填域"的桶，不是一个领域 —— 别给它派综述作业。"""
    from core import org_canon

    st = _state()
    for i in range(org_canon.CANON_REFRESH_THRESHOLD + 2):
        st.write_kb("claims", {
            "claim_text": f"一条没有域的历史债 {i}",
            "claim_type": "empirical", "concept_ids": [], "orphan_reason": "stub",
            "scope": "org", "sources": ["doi:10.1/x"], "confidence": 0.7,
            "org_kind": "verified_finding",
            "promoted_from": {"project_id": "p_old", "source_id": f"claim_old{i:08d}",
                              "approved_by": "w", "at": "2026-01-01T00:00:00Z"},
        })
    jobs = org_canon.refresh_jobs(st)
    kinds = {j["kind"] for j in jobs}
    assert "unfiled_backlog" in kinds
    assert "canon_refresh" not in kinds, (
        "给 unsorted 派了综述作业 —— curator 会把一堆互不相干的结论写进同一篇叙述")
    backlog = next(j for j in jobs if j["kind"] == "unfiled_backlog")
    assert "不要" in backlog["instruction"]


def test_local_card_fixture_covers_the_whole_contract():
    """夹具跟不上字段集时，让**这一条**炸，而不是六条看不出所以然的断言。

    改 KNOWLEDGE_CARD_FIELDS 时漏的总是别处手工造这个对象的地方：
    本次一天内撞两次（test_kb_promotion / test_auto_propose_slicers），
    症状都是"候选清单空了"，指向的假原因是扫盘坏了。
    """
    from core.kb_promotion import KNOWLEDGE_CARD_FIELDS

    missing = [f for f in KNOWLEDGE_CARD_FIELDS if f not in _good_card()]
    assert not missing, (
        f"本文件的 _good_card() 夹具缺 {missing} —— 知识卡契约加了字段，"
        f"这里没跟上。补上，别把断言改松。")


def test_free_text_domain_is_refused_with_suggestions():
    """自由文本的域被拒，且报错列最近匹配 —— 这是域注册表的准入面。"""
    card = _good_card() | {"domain": "机器学习势"}
    chk = kp.check_deprojectified(card)
    assert chk.passed is False
    assert chk.cause == "invalid_domain"
    assert "cond-mat" in chk.reason or "physics" in chk.reason, (
        "拒收没给最近匹配 —— 合法取值只在运行时报错 = 逼调用方猜")
