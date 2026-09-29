"""组织的知识会被改正 —— 推翻或取代，不删（`core/org_corrections`）。

C 批只接了「知识进组织」：采纳过的结论每个新项目开局都会收到，却没有任何一条路说「这条
错了」。一条站不住的结论于是被一个又一个项目当成前提，错误也在复利。

判据全部落在送达面和待审上：裁定之后新项目还收不收得到它、红旗还响不响、账本里它还在
不在、更正从哪几处进来、谁裁。
"""
from __future__ import annotations

import asyncio

from core import kb_promotion as kp
from core import org_corrections as oc
from core import org_delivery as od
from core.state import State
from tests.test_kb_promotion import _good_card, _make_terminal, _seed_claim, _seed_evidence
from tests.test_promotion_reaches_the_organisation import _AT, _a_finished_project, _offer, _state

ADMIN = "admin@lab.test"


def _adopt_the_waiting(st: State, project_id: str) -> str:
    waiting = [p for p in kp.review_queue()
               if p.get("status") == "pending" and p.get("project_id") == project_id
               and p.get("type") != oc.PROPOSAL_TYPE]
    assert len(waiting) == 1, waiting
    done = kp.adopt(st, waiting[0]["id"], approved_by=ADMIN, at=_AT)
    assert done["status"] == "success", done
    return done["proposal"]["org_id"]


def _an_adopted_finding() -> tuple[State, str]:
    st, _ch, finding, _dead = _a_finished_project()
    _offer(st, finding)
    return st, _adopt_the_waiting(st, st.project_id)


def _another_finished_project(project_id: str, card: dict, *, status: str = "") -> tuple[State, str, dict]:
    st = _state(project_id)
    _make_terminal(st)
    finding = _seed_claim(st, _seed_evidence(st))
    if status:
        st.update_lifecycle("claims", finding, status_change={"to_status": status},
                            reasoning="项目自己的判断")
    out = kp.offer_to_the_organisation(st, project_id=project_id, at=_AT, drafts={finding: card})
    return st, finding, out


def _the_opposite() -> dict:
    card = _good_card()
    card["statement"] = ("通用 MLIP（foundation-model 类）在 >30 GPa 重构型相变区，"
                         "相对能量误差未超出常压基准")
    return card


def _something_else() -> dict:
    card = _good_card()
    card["statement"] = "通用 MLIP 在 >30 GPa 区间需要按压力段分别校正参考能量"
    return card


def _corrections(org_id: str = "", status: str = "pending") -> list[dict]:
    return [p for p in kp.review_queue()
            if p.get("type") == oc.PROPOSAL_TYPE and p.get("status") == status
            and (not org_id or p.get("org_id") == org_id)]


def _org_dead_end(st: State) -> dict:
    [dead] = [r for r in st.list_kb("claims")
              if r.get("scope") == "org" and r.get("org_kind") == "dead_end"]
    return dead


# ── 送达面：不作数的不再送 ──────────────────────────────────────────────────


def test_a_refuted_entry_is_no_longer_handed_to_new_projects():
    st, org_id = _an_adopted_finding()
    assert org_id in (od.org_orientation(st) or ""), "采纳了的结论开局就该摆在面前（带 id）"

    done = oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED,
                     reason="复算发现参考能量取错了 —— 误差其实在常压基准以内", by=ADMIN, at=_AT)

    assert done["status"] == "success", done
    assert org_id not in (od.org_orientation(st) or ""), "推翻了的结论还在当「本组已知」送"
    kept = st.get_kb_record("claims", org_id)
    assert kept is not None, "组织的知识不删 —— 推翻了它也还在账本里"
    assert kept["org_standing"]["reason"].startswith("复算发现") and kept["org_standing"]["by"] == ADMIN


def test_a_retired_dead_end_raises_no_red_flag():
    st, _ch, _finding, _dead = _a_finished_project()
    _offer(st, _finding)
    dead = _org_dead_end(st)
    plan = str(dead.get("claim_text"))
    assert od.dead_end_flags(st, plan), "前提：这份计划本来会撞上这条死路"

    oc.retire(st, dead["id"], verdict=oc.VERDICT_REFUTED,
              reason="换了积分器之后这条路走通了", by=ADMIN, at=_AT)

    assert od.dead_end_flags(st, plan) == [], "已经走通的死路还在 reviewer 那里响红旗"


def test_what_the_project_concluded_is_not_the_organisations_verdict():
    """`status` 随晋升从项目带过来，是项目对原结论的判断；组织认不认它是另一件事。

    一条被项目自己翻成 refuted 的结论，晋升上来就是「X 不成立」这条知识 —— 组织照样认。
    拿 `status` 当「作不作数」，这类知识会从开局注入里悄悄消失。
    """
    st, org_id = _an_adopted_finding()
    st.update_lifecycle("claims", org_id, status_change={"to_status": "refuted"},
                        reasoning="随项目带过来的判断")

    assert oc.in_force(st.get_kb_record("claims", org_id))
    assert org_id in (od.org_orientation(st) or "")


# ── 管理员的裁定 ─────────────────────────────────────────────────────────────


def test_a_verdict_needs_a_reason_and_is_made_once():
    st, org_id = _an_adopted_finding()

    assert oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED, reason="  ", by=ADMIN, at=_AT)["code"] == "reason_required"
    assert oc.retire(st, org_id, verdict="deleted", reason="不要了", by=ADMIN, at=_AT)["code"] == "invalid_verdict"
    assert oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED, reason="错了", by=ADMIN, at=_AT)["status"] == "success"
    assert oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED, reason="又错了", by=ADMIN, at=_AT)["code"] == "already_retired"


def test_a_superseded_entry_points_to_what_replaced_it():
    st, old = _an_adopted_finding()
    st2, _f, _out = _another_finished_project("p_newer", _something_else())
    new = _adopt_the_waiting(st2, "p_newer")

    assert oc.retire(st, old, verdict=oc.VERDICT_SUPERSEDED, reason="x", by=ADMIN, at=_AT,
                     superseded_by=old)["code"] == "invalid_successor"
    done = oc.retire(st, old, verdict=oc.VERDICT_SUPERSEDED, reason="新的一条按压力段分开说了",
                     by=ADMIN, at=_AT, superseded_by=new)

    assert done["status"] == "success", done
    assert done["entry"]["superseded_by_claim_id"] == new
    assert done["entry"]["org_standing"]["superseded_by"] == new


def test_a_verdict_can_be_withdrawn_with_a_reason_and_both_stay_on_record():
    st, org_id = _an_adopted_finding()
    oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED, reason="以为参考能量取错了", by=ADMIN, at=_AT)

    assert oc.reinstate(st, org_id, reason="", by=ADMIN, at=_AT)["code"] == "reason_required"
    done = oc.reinstate(st, org_id, reason="复核：参考能量没错，是复算脚本的单位错了", by=ADMIN, at=_AT)

    assert done["status"] == "success", done
    assert oc.in_force(done["entry"]) and org_id in (od.org_orientation(st) or "")
    assert [h["verdict"] for h in done["entry"]["org_standing_history"]] == ["refuted", "in_force"]


# ── 更正从哪来：都进同一个待审，管理员裁 ─────────────────────────────────────


def test_a_members_correction_waits_for_the_admin():
    st, org_id = _an_adopted_finding()

    filed = oc.propose(st, org_id, verdict=oc.VERDICT_REFUTED, reason="我们组复现不出来",
                       by="zhang@lab.test", at=_AT, origin=oc.ORIGIN_MEMBER)

    assert filed["status"] == "success" and not filed["already"]
    assert org_id in (od.org_orientation(st) or ""), "裁之前它照旧作数"
    [waiting] = _corrections(org_id)
    assert kp.adopt(st, waiting["id"], approved_by=ADMIN, at=_AT)["status"] == "success"
    assert org_id not in (od.org_orientation(st) or "")
    assert st.get_kb_record("claims", org_id)["org_standing"]["proposal_id"] == waiting["id"]


def test_a_declined_correction_leaves_the_entry_standing():
    st, org_id = _an_adopted_finding()
    oc.propose(st, org_id, verdict=oc.VERDICT_REFUTED, reason="我觉得不对",
               by="zhang@lab.test", at=_AT, origin=oc.ORIGIN_MEMBER)
    [waiting] = _corrections(org_id)

    assert kp.decline(waiting["id"], declined_by=ADMIN, reason="", at=_AT)["code"] == "reason_required"
    assert kp.decline(waiting["id"], declined_by=ADMIN, reason="没有证据", at=_AT)["status"] == "success"
    assert oc.in_force(st.get_kb_record("claims", org_id))


def test_the_same_correction_does_not_wait_twice():
    st, org_id = _an_adopted_finding()
    first = oc.propose(st, org_id, verdict=oc.VERDICT_REFUTED, reason="复现不出来",
                       by="zhang@lab.test", at=_AT, origin=oc.ORIGIN_MEMBER)
    again = oc.propose(st, org_id, verdict=oc.VERDICT_REFUTED, reason="还是复现不出来",
                       by="zhang@lab.test", at=_AT, origin=oc.ORIGIN_MEMBER)

    assert not first["already"] and again["already"]
    assert len(_corrections(org_id)) == 1


def test_a_retired_entry_takes_no_more_corrections():
    st, org_id = _an_adopted_finding()
    oc.retire(st, org_id, verdict=oc.VERDICT_REFUTED, reason="错了", by=ADMIN, at=_AT)
    assert oc.propose(st, org_id, verdict=oc.VERDICT_REFUTED, reason="错了",
                      by="zhang@lab.test", at=_AT, origin=oc.ORIGIN_MEMBER)["code"] == "already_retired"


# ── 机械比对：结论方向相反就提出来，不自动裁 ────────────────────────────────


def test_a_finished_project_that_says_the_opposite_raises_it():
    _st, org_id = _an_adopted_finding()

    _st2, _f, out = _another_finished_project("p_other", _the_opposite())

    assert [c["org_id"] for c in out["corrections"]] == [org_id], out.get("corrections")
    [raised] = _corrections(org_id)
    assert raised["origin"] == oc.ORIGIN_CONTRADICTION and raised["project_id"] == "p_other"
    assert raised["evidence_closure"], "机械提出的更正要带着那个项目的证据"
    assert "未超出" in raised["about"], "管理员裁的时候两条要摆在一起看"
    assert "未超出" in raised["because"] and "认可" not in raised["because"], (
        "认可之后这句话就是那条裁定的理由 —— 要写给以后读到它的人，不是写给管理员的操作说明")
    assert oc.in_force(_st.get_kb_record("claims", org_id)), "矛盾不自动裁决"


def test_the_same_contradiction_waits_once_even_when_its_finding_is_adopted():
    _st, org_id = _an_adopted_finding()
    st2, _f, _out = _another_finished_project("p_other", _the_opposite())

    _adopt_the_waiting(st2, "p_other")

    assert len(_corrections(org_id)) == 1


def test_adopting_the_opposite_raises_the_older_one():
    """两条都还在等的时候谁也比不出矛盾；后采纳的那一刻才比得出来。"""
    st, _ch, finding, _dead = _a_finished_project()
    _offer(st, finding)
    st2, _f, out = _another_finished_project("p_other", _the_opposite())
    assert out["corrections"] == []
    older = _adopt_the_waiting(st, st.project_id)

    newer = _adopt_the_waiting(st2, "p_other")

    [raised] = _corrections(older)
    assert raised["verdict"] == oc.VERDICT_SUPERSEDED and raised["superseded_by"] == newer
    assert "未超出" in raised["because"] and "认可" not in raised["because"]


def test_a_finding_the_project_refuted_is_not_compared_by_its_words():
    """项目自己翻成 refuted 的结论，正文说的是被否掉的那一面 —— 拿它比方向会比反。"""
    _st, org_id = _an_adopted_finding()
    _st2, _f, out = _another_finished_project("p_other", _the_opposite(), status="refuted")
    assert out["corrections"] == [] and _corrections(org_id) == []


def test_a_retired_entry_takes_no_part_in_the_organisations_upkeep():
    """dreaming 的合并 / 矛盾 / 老化只管还作数的 —— 已推翻的那条不该再和谁凑成一对矛盾。"""
    from core.org_dreaming import find_contradictions

    st, _ch, finding, _dead = _a_finished_project()
    _offer(st, finding)
    st2, _f, _out = _another_finished_project("p_other", _the_opposite())
    older = _adopt_the_waiting(st, st.project_id)
    _adopt_the_waiting(st2, "p_other")
    assert find_contradictions(st).applied, "前提：两条方向相反"

    oc.retire(st, older, verdict=oc.VERDICT_REFUTED, reason="错了", by=ADMIN, at=_AT)

    assert find_contradictions(st).applied == []


# ── 项目里的 agent：手里有证据就能提 ────────────────────────────────────────


def _file(st: State, **kw) -> dict:
    from shared.tools.library import kb as kbtools

    return asyncio.run(kbtools._propose_org_correction(st, **kw))


def test_an_agent_files_a_correction_with_its_evidence():
    _st, org_id = _an_adopted_finding()
    here = _state("p_here")
    evidence = _seed_claim(here, _seed_evidence(here))

    assert _file(here, org_id=org_id, verdict="refuted", reason="复现不出来",
                 evidence_ids=[])["code"] == "evidence_required"
    assert _file(here, org_id=org_id, verdict="refuted", reason="复现不出来",
                 evidence_ids=["claim_nowhere"])["code"] == "evidence_not_found"
    done = _file(here, org_id=org_id, verdict="refuted", reason="复现不出来", evidence_ids=[evidence])

    assert done["status"] == "success", done
    [waiting] = _corrections(org_id)
    assert waiting["origin"] == oc.ORIGIN_AGENT and waiting["project_id"] == "p_here"
    assert waiting["evidence_closure"] == [evidence] and waiting["source_home"]


def test_the_organisations_own_entries_are_not_evidence():
    st, org_id = _an_adopted_finding()
    here = _state("p_here")
    assert _file(here, org_id=org_id, verdict="refuted", reason="拿它自己证它",
                 evidence_ids=[org_id])["code"] == "evidence_not_found"


# ── 送得到：不改同事的节点文件 ──────────────────────────────────────────────


def test_hypothesis_hears_what_the_organisation_knows():
    """钩子早就点名了 hypothesis，却要靠节点自己的 harness.yaml 挂 —— 它从没收到过。"""
    from core.agent_loop import resolve_enabled_hook_names
    from core.loader import load_harness
    from core.loop_hooks import HookContext, get_loop_hook

    harness = load_harness("hypothesis")
    assert "org_orientation" in resolve_enabled_hook_names(harness)

    st, org_id = _an_adopted_finding()
    st.node_type = "hypothesis"
    said = get_loop_hook("org_orientation").on_turn_start(
        HookContext(harness=harness, state=st, messages=[], turn=0))
    assert said and org_id in said[0].content and "propose_org_correction" in said[0].content


def test_every_producing_node_can_file_a_correction():
    from core.loader import load_harness

    # 系统节点不拿始终启用的工具：_curator（知识管家）和 _orchestrator（用户对着说话的那个）显式挂。
    for node in ("hypothesis", "experiment", "_curator", "_orchestrator"):
        assert "propose_org_correction" in load_harness(node).tools, node
