"""自产证据必须过得了 org 的河 —— 否则计算类项目的每张卡出生即断链。

## 病灶

原来晋升只有一条证据过河的路：`_write_biblio`，身份是外部锚（DOI/arXiv）。
`_has_external_anchor` 为假就**整条跳过** —— 而计算类项目的证据是自己跑出来的
实验日志，`source` 是 `artifact:...` 不是 DOI。

后果：结论过了河，支撑它的证据没过河。2026-08-21 Ising 闭环回放实测
**3/3 张 org 卡 intact=false**。而 org 卡最该服务的读者恰恰是别的项目 ——
它们连源项目的产物盘面都读不到。

## 修法

两种证据两条路。自产证据的身份不能是 DOI，但可以是
「源项目 + 冻结产物 id + 版本 + 内容哈希」：**正文跨层**（它是内容不是指针），
产物本体留在源项目，锚记成可核对的指针。要复核就拿哈希去那个项目对。
"""
from __future__ import annotations

import pytest

from core import kb_promotion as kp
from core.bootstrap import bootstrap
from core.kb_provenance import kb_provenance
from core.state import State

bootstrap()

_CARD = {
    "domain": "cond-mat.stat-mech",
    "statement": "有限尺度标度外推的加权方案必须在预注册里显式指定",
    "applicability": {"method": "Binder FSS", "regime": "crossing pairs <= 5"},
    "why": "交叉点少时单个高精度点主导截距，其系统误差未计入误差棒",
    "practice": "预注册的 falsifier 里连权重来源一起写",
    "confidence_basis": "单课题实测，同一份数据两种拟合给出 2.8σ 与 1.17σ",
    "evidence": [],
}


def _terminal_project(tmp_path) -> tuple[State, str]:
    st = State.new(node_type="_curator", base_dir=tmp_path / "src",
                   project_id="ising-tc-2026")
    art = st.save_artifact("experiment_log", "IsingLog", "# 实验日志\n\n正文…")
    st.mark_frozen(art["id"])
    paper = st.save_artifact("manuscript", "Paper", "# 终稿")
    st.mark_frozen(paper["id"])
    chunk, _ = st.write_kb("chunks", {
        "text": "不加权拟合给出 1.17σ，加权给出 2.8σ —— 判决对加权方案敏感。",
        "source": f"artifact:{art['id']}",     # 自产证据：不是 DOI
        "origin_artifact_id": art["id"],
        "origin_content_hash": "sha256:deadbeef",
        "scope": "project",
    })
    claim, _ = st.write_kb("claims", {
        "claim_text": "加权与不加权 FSS 外推给出互相矛盾的判决",
        "claim_type": "methodological", "scope": "project",
        "concept_ids": [], "orphan_reason": "本项目未注册受控词条",
        "sources": [chunk["id"]], "confidence": 0.85,
    })
    return st, claim["id"]


def test_self_produced_evidence_crosses_with_the_conclusion(tmp_path):
    st, cid = _terminal_project(tmp_path)
    scan = kp.promotion_scan(st, drafts={cid: _CARD})
    cand = next(c for c in scan["human_batch"] if c.source_id == cid)
    res = kp.promote(st, cand, project_id="ising-tc-2026",
                     approved_by="wangd", at="2026-08-21T00:00:00Z")
    assert res["status"] == "success"
    assert len(res["written"]) >= 2, (
        "只写了结论没写证据 —— 自产证据又被 _has_external_anchor 跳过了")

    org_chunks = [c for c in st.list_kb("chunks") if c.get("scope") == "org"]
    assert org_chunks, "org 层没有证据记录"
    ev = org_chunks[0]
    assert ev["origin_project_id"] == "ising-tc-2026"
    assert ev["origin_content_hash"] == "sha256:deadbeef", (
        "没记内容哈希 —— 那这条锚就不可核对了")
    assert "1.17σ" in ev["text"], "证据**正文**必须跨层：org 卡要自足，不能只给指针"


def test_outsider_project_sees_an_intact_chain(tmp_path):
    """org 卡最该服务的读者是**别的项目** —— 它连源项目的产物盘面都读不到。"""
    st, cid = _terminal_project(tmp_path)
    scan = kp.promotion_scan(st, drafts={cid: _CARD})
    cand = next(c for c in scan["human_batch"] if c.source_id == cid)
    org_id = kp.promote(st, cand, project_id="ising-tc-2026",
                        approved_by="wangd", at="2026-08-21T00:00:00Z")["org_id"]

    outsider = State.new(node_type="writing", base_dir=tmp_path / "other",
                         project_id="xy-model-kt-2026")
    res = kb_provenance(outsider, org_id)
    assert res["intact"] is True, (
        f"别的项目走这条链报断链：{res.get('broken')} —— "
        f"假警报会让人学会忽略真警报")
    crossed = [h for h in res["hops"]
               if h["kind"] == "artifact" and "源项目" in h["summary"]]
    assert crossed, "跨项目证据锚没出现在链上"
    assert crossed[0]["detail"]["origin_project_id"] == "ising-tc-2026"


def test_evidence_without_anchor_or_artifact_is_not_faked(tmp_path):
    """既无外部锚又无来源产物 —— 没有可核对的东西，就不该造一条记录出来。"""
    st = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_noanchor")
    chunk, _ = st.write_kb("chunks", {
        "text": "一段没有任何出处的文字" * 2, "source": "手写笔记",
        "scope": "project",
    })
    rec = kp._write_evidence_record(
        st, st.get_kb_record("chunks", chunk["id"]),
        prov={"project_id": "p_noanchor", "source_id": "claim_x",
              "approved_by": "w", "at": "t"})
    assert rec is None
