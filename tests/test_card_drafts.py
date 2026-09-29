"""知识卡草稿区（原「承重层」，2026-08-21 合并）。

## 合并前是三套机制

  承重层        项目内 12 条决策资本，判据「decision_relevance：会改变哪类选择」
  org 晋升      终态三查 + 人批，判据「知识卡 practice：据此该怎么做」
  org_kind      org 内分道

前两条问的是**同一个问题**，各记一套账。合并后：一条 project claim 带知识卡
草稿 = 它既进设计期 briefing，也是终态晋升候选。带不带草稿就是唯一信号，
没有平行的 bool 标记。

## 顺带修掉的病

起草从「终态一次性传进来」改成「项目进行中写在 claim 上」—— why（机制）和
practice（据此怎么做）在你刚做完那个实验时最清楚，到终态已经凉了。
真实数据回放实测：132 条候选 132 条缺 why。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as execute_tool
from shared.lib.kb_schema import card_draft_source_errors

bootstrap()

_CARD = {
    "domain": "cs.LG",
    "applicability": {"regime": "长链多分支路由", "tested_on": ["tau-bench"]},
    "why": "可验证性高的子任务能早停，省掉长尾的无效展开",
    "practice": "设计路由时优先把可机械验证的分支前置，别把它们排在长链尾部",
    "confidence_basis": "单项目实测，证据链含 1 篇文献锚点",
}


def _state(tmp_path: Path, pid="p_cap") -> State:
    return State.new(node_type="_curator", base_dir=tmp_path, project_id=pid)


def _make_terminal(state: State) -> None:
    """终态：一份已冻结的 manuscript（冻结 = 账本 freeze 行，`mark_frozen`）。"""
    saved = state.save_artifact("manuscript", "Paper", "# 终稿")
    state.mark_frozen(saved["id"])


def _claim(state: State, **over) -> dict:
    rec = {
        "claim_text": "按可验证性路由能在 tau-bench 上省 40% 成本",
        "claim_type": "empirical", "concept_ids": ["c1"],
        "confidence": 0.6, "sources": ["chunk_" + "a" * 12],
        "scope": "project",
        "produced_by_experiment_id": "exp_" + "b" * 12,
    }
    rec.update(over)
    r, _ = state.write_kb("claims", rec)
    return r


# ── 源资格：两条硬规则（第三条已并入卡片契约）──────────────────────────────


def test_literature_reported_cannot_be_drafted():
    errs = card_draft_source_errors({"claim_type": "empirical",
                                     "literature_reported": True})
    assert errs and "文献转述" in errs[0]


def test_self_produced_empirical_needs_experiment_link():
    errs = card_draft_source_errors({"claim_type": "empirical"})
    assert errs and "关联产生它的实验" in errs[0]
    assert not card_draft_source_errors({
        "claim_type": "empirical",
        "produced_by_experiment_id": "exp_" + "c" * 12})


def test_methodological_needs_no_experiment_link():
    """方法配方不是观测，没有实验出处也成立。"""
    assert card_draft_source_errors({"claim_type": "methodological"}) == []


def test_legacy_type_is_normalized_before_the_rule_applies():
    """旧盘上的 theoretical 归一成 empirical —— 规则得看归一后的值。"""
    errs = card_draft_source_errors({"claim_type": "theoretical"})
    assert errs, "归一前是 theoretical 就绕过了实验出处规则"


def test_decision_relevance_rule_is_gone():
    """墓碑：原「decision_relevance ≥30 字符」。

    它问的正是知识卡 practice 的问题（据此该怎么做）。同一个问题两套记账，
    收敛到卡片那一套 —— practice 的完整性由 check_deprojectified 管。
    """
    import inspect

    from shared.lib import kb_schema

    src = inspect.getsource(kb_schema)
    assert "_DECISION_RELEVANCE_MIN" not in src
    assert "load_bearing_admission_errors" not in src


# ── 起草 → briefing → 终态候选，一条草稿三重身份 ────────────────────────────


@pytest.mark.asyncio
async def test_draft_shows_up_in_design_briefing(tmp_path):
    state = _state(tmp_path)
    c = _claim(state)
    res = await execute_tool("draft_knowledge_card", state,
                             claim_id=c["id"], **_CARD)
    assert res["status"] == "success", res

    from core.recall import recall

    out = recall(state, query="路由", include_categories=["load_bearing_capital"])
    text = out.render() if hasattr(out, "render") else str(out)
    assert "据此该怎么做" in text or "路由" in text


@pytest.mark.asyncio
async def test_draft_is_picked_up_by_terminal_scan_without_passing_it_again(tmp_path):
    """草稿写在 claim 上 —— 终态扫盘自己捡，不必到时再传一遍。

    这是合并的实质收益：原来 curator 要在终态一次性把所有卡片当参数传进来，
    漏传一张就静默少一个候选。
    """
    from core import kb_promotion as kp

    state = _state(tmp_path, "p_pickup")
    c = _claim(state)
    await execute_tool("draft_knowledge_card", state, claim_id=c["id"], **_CARD)
    _make_terminal(state)

    scan = kp.promotion_scan(state)          # 注意：没传 drafts=
    assert c["id"] in {x.source_id for x in scan["human_batch"]}, (
        "草稿在盘上，扫盘却没捡起来 —— 起草和晋升又断成两截了")


# ── 草稿照落，缺什么标什么（判决拆除 O8）——墙若加回来这几条转红 ─────────────


@pytest.mark.asyncio
async def test_incomplete_card_lands_with_the_missing_fields_marked(tmp_path):
    from core import kb_promotion as kp

    state = _state(tmp_path, "p_incomplete")
    c = _claim(state)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"],
                             domain="cs.LG", applicability={"regime": "x"},
                             why="", practice="p", confidence_basis="b")
    assert res["status"] == "success", res
    assert res["deprojectified"] is False and res["code"] == "missing_card_fields"
    assert "required_fields" in res and "why" in res["required_fields"]
    draft = state.get_kb_record("claims", c["id"])["card_draft"]
    assert draft["deprojectified"] is False
    assert draft["deprojectified_cause"] == "missing_card_fields"
    # 终态扫盘把「还差什么」列进 blocked，而不是静默漏掉
    _make_terminal(state)
    scan = kp.promotion_scan(state)
    assert c["id"] in {b["source_id"] for b in scan["blocked"]}
    assert c["id"] not in {x.source_id for x in scan["human_batch"]}


@pytest.mark.asyncio
async def test_free_text_domain_lands_with_suggestions(tmp_path):
    state = _state(tmp_path, "p_domain")
    c = _claim(state)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"],
                             **{**_CARD, "domain": "机器学习"})
    assert res["status"] == "success", res
    assert res["deprojectified"] is False and res["code"] == "invalid_domain"
    assert res["domain_suggestions"], "没给最近匹配 —— 逼调用方猜"


@pytest.mark.asyncio
async def test_ineligible_source_lands_with_source_warnings(tmp_path):
    """没关联实验的自产 empirical：够不够格是晋升人批要看的事实，标在卡上。"""
    state = _state(tmp_path, "p_srcwarn")
    c = _claim(state, produced_by_experiment_id=None)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"], **_CARD)
    assert res["status"] == "success", res
    assert res["source_warnings"] and "实验" in res["source_warnings"][0]
    draft = state.get_kb_record("claims", c["id"])["card_draft"]
    assert draft["source_warnings"] == res["source_warnings"]


@pytest.mark.asyncio
async def test_dead_end_without_trigger_lands_marked_missing_trigger(tmp_path):
    state = _state(tmp_path, "p_deadend")
    c = _claim(state, claim_type="dead_end", dont_repeat_reason="团簇算法在高温点不收敛，预算烧尽",
               produced_by_experiment_id=None)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"], **_CARD)
    assert res["status"] == "success", res
    assert res["missing_trigger"] is True
    assert state.get_kb_record("claims", c["id"])["card_draft"]["missing_trigger"] is True


@pytest.mark.asyncio
async def test_no_draft_budget_but_briefing_truncates_by_budget(tmp_path, monkeypatch):
    """起草侧不设配额；真实约束在 briefing 注入窗口：取最近 N 张，多出的只报个数。"""
    from core.recall import recall, render_briefing

    monkeypatch.setenv("HARNESS_CARD_DRAFT_BUDGET", "2")
    state = _state(tmp_path, "p_budget")
    ids = []
    for i in range(3):
        c = _claim(state, claim_text=f"结论 {i} 关于路由代价")
        ids.append(c["id"])
        r = await execute_tool("draft_knowledge_card", state, claim_id=c["id"], **_CARD)
        assert r["status"] == "success", r          # 第三张照落：墙若加回来转红
    assert all(state.get_kb_record("claims", i).get("card_draft") for i in ids)

    out = recall(state, query="路由", include_categories=["load_bearing_capital"])
    assert len(out.load_bearing_capital) == 2
    assert out.card_drafts_not_injected == 1
    assert "另有 1 张草稿未注入" in render_briefing(out, query="路由")


@pytest.mark.asyncio
async def test_discarding_an_absent_draft_is_idempotent(tmp_path):
    state = _state(tmp_path, "p_absent")
    c = _claim(state)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"],
                             discard=True, reason="从来没起草过")
    assert res["status"] == "success" and res["already_absent"] is True


@pytest.mark.asyncio
async def test_discard_keeps_the_claim(tmp_path):
    state = _state(tmp_path, "p_discard")
    c = _claim(state)
    await execute_tool("draft_knowledge_card", state, claim_id=c["id"], **_CARD)
    res = await execute_tool("draft_knowledge_card", state, claim_id=c["id"],
                             discard=True, reason="结论被后续实验推翻了")
    assert res["status"] == "success"
    rec = state.get_kb_record("claims", c["id"])
    assert not rec.get("card_draft")
    assert rec.get("claim_text"), "撤草稿不该动 claim 本身"
    assert rec["card_draft_history"][-1]["action"] == "discard"


@pytest.mark.asyncio
async def test_org_claim_cannot_be_drafted(tmp_path):
    """org 条目已经是卡了 —— 不该再起草一张。"""
    state = _state(tmp_path, "p_orgdraft")
    rec, _ = state.write_kb("claims", {
        "claim_text": "一条已晋升的 org 结论", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub", "scope": "org",
        "sources": ["doi:10.1/x"], "confidence": 0.8,
        "promoted_from": {"project_id": "p0", "source_id": "claim_" + "d" * 12,
                          "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
    })
    res = await execute_tool("draft_knowledge_card", state, claim_id=rec["id"],
                             **_CARD)
    assert res["status"] == "error" and res["code"] == "not_a_project_claim"
