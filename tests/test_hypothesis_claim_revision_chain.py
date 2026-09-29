"""hypothesis claim 的结构化身份 + KB 修订链（RFC 2026-08-18，#395-2 的解）。

旧现场（jicq E2E Study3）：冻 prereg v5 带新判据重建 claim →
  · 语义去重 0.95 命中 → 静默 merge 只并 sources，新判据全部丢弃；或
  · v3.3 的 chunk 签名 → 新 claim id，旧 claim 无人标 superseded，新旧并存
两条路殊途同归：validate_hypothesis_outputs 永远失败，节点没有确定性修复路径。

新模型：claim 身份 = (prereg 身份, hypothesis_id)。修订命中同一 id，原地更新、
留 revision_history；判据变了机械把 status 退回 open。chunk 逐版快照自动闭链。

夹具用没绑 worktree 的 State（run 本地账本）：冻结是账本上的一行
（`state.mark_frozen`），修订是带 amendment_reason 的 save → 新版本未冻结，再冻。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import shared.tools.library.kb as kb
from core.state import State


@pytest.fixture(autouse=True)
def _no_embed(monkeypatch):
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")


@pytest.fixture()
def state(tmp_path: Path) -> State:
    root = tmp_path / "run1"
    (root / "artifacts").mkdir(parents=True)
    # 承诺不进 KB（提案期）；hypothesis claim 的修订链发生在 _curator 采纳时。
    return State(run_id="r1", node_type="_curator", root=root)


def _frozen_prereg(state: State, content: str, *, amend: str | None = None) -> str:
    r = state.save_artifact("pre_registration", "study1", content, metadata={},
                            amendment_reason=amend)
    state.mark_frozen(r["id"])
    return r["id"]


def _chunk(state: State, artifact_id: str) -> dict:
    return asyncio.run(kb._kb_register_artifact_as_chunk(state, artifact_id))


def _claim(state: State, text: str, chunk_id: str, threshold: float) -> dict:
    return asyncio.run(kb._create_claim(
        state, text, "hypothesis", prereg_chunk_id=chunk_id, hypothesis_id="H1",
        orphan_reason="test", predicted_outcome="ratio above threshold",
        falsification_criteria_structured={"metric": "ratio", "op": ">",
                                           "threshold": threshold},
        sources=[chunk_id]))


def test_missing_hypothesis_id_is_rejected_with_the_contract(state: State) -> None:
    pid = _frozen_prereg(state, "# v1")
    c = _chunk(state, pid)
    res = asyncio.run(kb._create_claim(
        state, "H1 x", "hypothesis", prereg_chunk_id=c["chunk_id"],
        orphan_reason="t", sources=[c["chunk_id"]]))
    assert res["status"] == "error"
    assert "hypothesis_id" in res["error"] and "H1" in res["error"], \
        "报错必须给出合法取值的来源（预注册里的问题 id）"


def test_a_revision_updates_the_same_claim_in_place(state: State) -> None:
    pid = _frozen_prereg(state, "# v1: threshold 0.5")
    c1 = _chunk(state, pid)
    cl1 = _claim(state, "H1: ratio exceeds 0.5", c1["chunk_id"], 0.5)
    assert cl1["status"] == "success"

    _frozen_prereg(state, "# v2: threshold 0.7", amend="review 改阈值")
    c2 = _chunk(state, pid)
    cl2 = _claim(state, "H1: ratio exceeds 0.7 (revised)", c2["chunk_id"], 0.7)

    assert cl2["id"] == cl1["id"], "同 (prereg 身份, H1) 必须命中同一条 claim"
    final = state.get_kb_record("claims", cl1["id"])
    assert final["falsification_criteria_structured"]["threshold"] == 0.7, \
        "修订的实质（新判据）必须真的落上去 —— 旧行为在这里静默丢弃它"
    history = final["revision_history"]
    assert history and history[0]["previous"][
        "falsification_criteria_structured"]["threshold"] == 0.5, "旧判据进历史"
    assert final["prereg_artifact_id"] == pid, "谱系锚由框架从 chunk 机械补齐"

    claims = [r for r in state.list_kb("claims") if r.get("claim_type") == "hypothesis"]
    assert len(claims) == 1, "修订不许产生并存的重复 claim（#395-2）"


def test_changed_criteria_mechanically_reset_the_verdict(state: State) -> None:
    """旧判据下的裁决对新判据不成立 —— 机械事实，不是判断。"""
    pid = _frozen_prereg(state, "# v1")
    c1 = _chunk(state, pid)
    cl = _claim(state, "H1: ratio exceeds 0.5", c1["chunk_id"], 0.5)
    # 走正规转换把 status 抬到 provisional
    state.update_lifecycle("claims", cl["id"],
                           status_change={"to_status": "provisional"},
                           reasoning="首轮实验方向性支持（测试固定文案）")

    _frozen_prereg(state, "# v2", amend="改判据")
    c2 = _chunk(state, pid)
    _claim(state, "H1: ratio exceeds 0.7", c2["chunk_id"], 0.7)

    final = state.get_kb_record("claims", cl["id"])
    assert final["status"] == "open", "判据变了，provisional 必须机械退回 open"
    assert any("机械重置" in (h.get("reasoning") or "")
               for h in final["review_history"]), "重置要在 review_history 留痕"


def test_old_chunks_are_auto_superseded(state: State) -> None:
    pid = _frozen_prereg(state, "# v1")
    c1 = _chunk(state, pid)
    _frozen_prereg(state, "# v2", amend="改")
    c2 = _chunk(state, pid)

    old = state.get_kb_record("chunks", c1["chunk_id"])
    assert old["superseded_by_chunk_id"] == c2["chunk_id"], \
        "旧版 chunk 由框架自动闭链，模型零参与"
    assert old.get("superseded_at")
    new = state.get_kb_record("chunks", c2["chunk_id"])
    assert new.get("superseded_by_chunk_id") is None
    assert new["origin_artifact_version"] == 2


def test_claim_supersede_requires_and_records_a_successor(state: State) -> None:
    """schema 里声明多年、全仓零写入点的 superseded_by_claim_id 第一次接通。"""
    pid = _frozen_prereg(state, "# v1")
    c1 = _chunk(state, pid)
    cl = _claim(state, "H1: ratio exceeds 0.5", c1["chunk_id"], 0.5)
    successor = asyncio.run(kb._create_claim(
        state, "接任的经验性结论", "empirical", orphan_reason="t",
        sources=[c1["chunk_id"]]))

    no_successor = asyncio.run(kb._update_claim_status(
        state, cl["id"], new_status="superseded",
        reasoning="该假说的判据已由新一版经验性结论覆盖并取代，对照了 v2 的判据与实测结果"))
    assert no_successor["status"] == "error"
    assert "superseded_by_claim_id" in no_successor["error"]

    fake = asyncio.run(kb._update_claim_status(
        state, cl["id"], new_status="superseded",
        superseded_by_claim_id="claim_000000000000",
        reasoning="该假说的判据已由新一版经验性结论覆盖并取代，对照了 v2 的判据与实测结果"))
    assert fake["status"] == "error" and "不存在" in fake["error"]

    ok = asyncio.run(kb._update_claim_status(
        state, cl["id"], new_status="superseded",
        superseded_by_claim_id=successor["id"],
        reasoning="该假说的判据已由新一版经验性结论覆盖并取代，对照了 v2 的判据与实测结果"))
    assert ok["status"] == "success"
    rec = state.get_kb_record("claims", cl["id"])
    assert rec["superseded_by_claim_id"] == successor["id"]
    assert rec["status"] == "superseded"
