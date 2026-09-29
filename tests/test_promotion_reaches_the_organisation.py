"""晋升真的到了组织 —— 机械车道落进 org 层、人批车道进组织的待审、管理员裁了才落。

`RFC_ORGANISATION_PAGE_20260923` C 批。此前（2026-09-24 读代码核实）`promote()` 在生产
代码里零调用方：扫盘结果里有 `mechanical`，谁也不拿它落地；人批提议写成项目里的一件
artifact，而能「批」的 `resolve_proposal` 只认 jsonl 队列 —— 批不到它。晋升这件事从来
没发生过一次。

这里的判据全部是盘面：交出去之后 org 层里有什么、待审里有什么、裁了之后又有什么。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core import kb_promotion as kp
from core.state import State
from tests.test_kb_promotion import _good_card, _make_terminal, _seed_claim, _seed_evidence

_AT = "2026-09-24T00:00:00Z"
PROJECT = "p_mlip_highp"


def _state(project_id: str = PROJECT) -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id=project_id)


def _org(state: State, entity: str) -> list[dict]:
    return [r for r in state.list_kb(entity) if r.get("scope") == "org"]


def _dead_end(state: State, chunk_id: str) -> str:
    rec, _ = state.write_kb("claims", {
        "claim_text": "相变点附近用 Berendsen 会产生假中间相",
        "claim_type": "dead_end", "concept_ids": ["c"], "sources": [chunk_id],
        "dont_repeat_reason": "实测两轮复现",
    })
    return rec["id"]


def _a_finished_project() -> tuple[State, str, str, str]:
    st = _state()
    _make_terminal(st)
    ch = _seed_evidence(st)
    finding = _seed_claim(st, ch)
    dead = _dead_end(st, ch)
    return st, ch, finding, dead


def _offer(st: State, finding: str, card: dict | None = None, *, at: str = _AT, **kw) -> dict:
    # 草稿平时由 draft_knowledge_card 写在 claim 上；扫盘的 `drafts` 与它同一个入口。
    return kp.offer_to_the_organisation(st, project_id=PROJECT, at=at,
                                        drafts={finding: card or _good_card()}, **kw)


def _offered() -> tuple[State, str, dict]:
    st, _ch, finding, _dead = _a_finished_project()
    return st, finding, _offer(st, finding)


# ── 机械车道：直落 ──────────────────────────────────────────────────────────


def test_the_mechanical_lane_lands_without_anyone_approving():
    st, ch, finding, dead = _a_finished_project()

    out = _offer(st, finding)

    landed = {x["source_id"] for x in out["landed"]}
    assert dead in landed, "死路该直落 —— 它的判据不需要人"
    # 书目随死路的证据闭包先过了河，轮到它自己时已经在那儿 —— 两种说法都是"到了"。
    assert ch in landed | {x["source_id"] for x in out["already"]}
    org_dead = [r for r in _org(st, "claims") if (r.get("promoted_from") or {}).get("source_id") == dead]
    assert len(org_dead) == 1
    assert org_dead[0]["promoted_from"]["approved_by"] == kp.MECHANICAL
    biblio = [r for r in _org(st, "chunks") if r.get("source") == "doi:10.1038/s41524-023-01012-9"]
    assert len(biblio) == 1 and biblio[0]["group_readings"][0]["project_id"] == PROJECT
    assert not [c for c in _org(st, "claims") if c.get("claim_text") == ""], (
        "书目走进了「条目本体」那一段，落出一条没有正文的 org claim")


def test_a_paper_only_a_waiting_finding_cites_still_lands_as_a_reading():
    """书目的判据是"被可晋升的结论引用过"，不等那条结论被批 —— 它自己直落，落成一条书目。"""
    st = _state()
    _make_terminal(st)
    ch = _seed_evidence(st)
    finding = _seed_claim(st, ch)

    out = _offer(st, finding)

    assert ch in {x["source_id"] for x in out["landed"]}
    [reading] = [r for r in _org(st, "chunks") if r.get("source") == "doi:10.1038/s41524-023-01012-9"]
    assert reading["group_readings"][0]["project_id"] == PROJECT
    assert _org(st, "claims") == [], "一段文献落成了一条 org claim（还没有正文）"


def test_offering_twice_lands_nothing_twice():
    """宣称完成可以有好几次（每次调度器说 complete 都跑）—— 第二次什么都不该多。"""
    st, _ch, finding, _dead = _a_finished_project()
    _offer(st, finding)
    claims, chunks, queue = len(_org(st, "claims")), len(_org(st, "chunks")), len(kp.review_queue())

    again = _offer(st, finding, at="2026-09-25T00:00:00Z")

    assert again["landed"] == [] and again["queued"] == []
    assert (len(_org(st, "claims")), len(_org(st, "chunks")), len(kp.review_queue())) == (claims, chunks, queue)


def test_nothing_goes_anywhere_before_the_end():
    st = _state()
    ch = _seed_evidence(st)
    _dead_end(st, ch)

    out = kp.offer_to_the_organisation(st, project_id=PROJECT, at=_AT)

    assert out["terminal"] is False
    assert _org(st, "claims") == [] and kp.review_queue() == []


def test_listing_only_writes_nothing():
    st, _ch, finding, _dead = _a_finished_project()

    out = _offer(st, finding, send=False)

    assert any(c["eligible"] for c in out["candidates"])
    assert _org(st, "claims") == [] and kp.review_queue() == []


# ── 人批车道：进组织的待审，裁了才落 ────────────────────────────────────────


def test_a_finding_waits_for_the_organisation_with_the_card_it_was_offered_with():
    st, finding, out = _offered()

    assert [q["source_id"] for q in out["queued"]] == [finding]
    assert not [r for r in _org(st, "claims") if (r.get("promoted_from") or {}).get("source_id") == finding], (
        "验证结论没等管理员就进了组织")
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]
    assert waiting["status"] == "pending" and waiting["project_id"] == PROJECT
    assert waiting["card"] == _good_card(), "待审里得有管理员要读的那张卡"
    assert waiting["original"].startswith("本项目"), "原文也要在 —— 管理员对照着看改写得对不对"
    assert waiting["source_home"], "采纳时要回到那个项目读原结论和证据"


def test_adopting_lands_the_card_that_was_offered():
    st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]

    # 采纳在另一个进程里、另起的一份 State 上做（组织服务器经桥来问）—— 只凭待审记录
    # 和那个项目的知识，不凭扫盘时手里那份草稿。
    done = kp.adopt(_state(), waiting["id"], approved_by="admin@lab", at=_AT)

    assert done["status"] == "success", done
    [card] = [r for r in _org(st, "claims") if (r.get("promoted_from") or {}).get("source_id") == finding]
    assert card["claim_text"] == _good_card()["statement"]
    assert card["promoted_from"]["approved_by"] == "admin@lab"
    assert card["sources"], "证据闭包没跟着过来"
    [decided] = [p for p in kp.review_queue() if p["id"] == waiting["id"]]
    assert decided["status"] == "adopted" and decided["org_id"] == card["id"]


def test_a_decision_is_made_once():
    _st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]
    kp.adopt(_state(), waiting["id"], approved_by="admin@lab", at=_AT)

    again = kp.adopt(_state(), waiting["id"], approved_by="admin@lab", at=_AT)
    late = kp.decline(waiting["id"], declined_by="admin@lab", reason="重复了", at=_AT)

    assert again["code"] == "already_decided" and late["code"] == "already_decided"


def test_adopting_must_read_from_the_project_it_came_from():
    _st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]

    wrong = kp.adopt(_state("p_someone_else"), waiting["id"], approved_by="admin@lab", at=_AT)

    assert wrong["code"] == "wrong_project"


def test_adopting_what_cannot_be_read_lands_nothing():
    """读不到原结论（项目层不在它该在的地方），就不采纳 —— 从前 `promote` 会拿卡片正文照样写出一条
    org 记录：证据闭包一条没带过去、出处是空的，「组织知道」却说不出凭什么。"""
    import tempfile

    from core.paths import projects_root

    _st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]
    elsewhere = State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()), project_id=PROJECT)
    before = [r["id"] for r in _org(elsewhere, "claims")]
    here = projects_root() / PROJECT
    moved = here.with_name(here.name + ".moved")
    here.rename(moved)
    try:
        got = kp.adopt(elsewhere, waiting["id"], approved_by="admin@lab", at=_AT)
    finally:
        moved.rename(here)

    assert got["code"] == "source_unreadable"
    assert [r["id"] for r in _org(elsewhere, "claims")] == before, "读不到原结论还是落了一条 org 记录"


def test_declining_needs_a_reason_and_lands_nothing():
    st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]

    silent = kp.decline(waiting["id"], declined_by="admin@lab", reason="  ", at=_AT)
    said = kp.decline(waiting["id"], declined_by="admin@lab", reason="适用条件写得太宽", at=_AT)

    assert silent["code"] == "reason_required"
    assert said["status"] == "success" and said["proposal"]["reason"] == "适用条件写得太宽"
    assert not [r for r in _org(st, "claims") if (r.get("promoted_from") or {}).get("source_id") == finding]


def test_a_declined_card_is_not_sent_back_unchanged():
    st, finding, _out = _offered()
    [waiting] = [p for p in kp.review_queue() if p["source_id"] == finding]
    kp.decline(waiting["id"], declined_by="admin@lab", reason="适用条件写得太宽", at=_AT)

    same = _offer(st, finding)
    assert same["queued"] == [], "管理员退回的那张卡，下一次宣称完成又原样送了回去"

    narrower = {**_good_card(), "applicability": {"regime": "P>30GPa", "systems_tested": ["Si"]}}
    revised = _offer(st, finding, narrower)
    assert [q["source_id"] for q in revised["queued"]] == [finding], "照退回理由改过的卡该能再提"


# ── 谁来触发：调度器说「做完了」的那一刻，框架自己跑 ────────────────────────


def _saying(state: State, text: str):
    from core.llm import LLMMessage
    from core.loop_hooks import HookContext

    return HookContext(harness=None, state=state, turn=9, messages=[
        LLMMessage(role="user", content="继续"),
        LLMMessage(role="assistant", content=text),
        LLMMessage(role="system", content="别的 hook 追加的东西"),
    ])


def test_saying_complete_hands_the_finished_project_to_the_organisation():
    """不等 curator 哪天想起来跑 dreaming —— 跑不跑那是模型的事。"""
    from core import loop_hooks_builtin as builtin

    st, _ch, _finding, dead = _a_finished_project()

    said = builtin._offer_to_the_organisation_before_finish(
        _saying(st, "论文已冻结。\n\nCONTINUOUS_STATUS: complete"))

    assert said is None, "它不该让调度器为组织知识再跑一轮"
    assert [r for r in _org(st, "claims") if (r.get("promoted_from") or {}).get("source_id") == dead]


def test_an_ordinary_reply_hands_over_nothing():
    from core import loop_hooks_builtin as builtin

    st, _ch, _finding, _dead = _a_finished_project()

    builtin._offer_to_the_organisation_before_finish(_saying(st, "我先看一下数据。"))

    assert _org(st, "claims") == [] and kp.review_queue() == []


def test_the_orchestrator_carries_it():
    """挂在 `_orchestrator` 上 —— 写了函数没挂上，等于没有。"""
    import yaml

    from core.loop_hooks import get_loop_hook

    spec = yaml.safe_load((Path(__file__).resolve().parents[1]
                           / "nodes" / "_orchestrator" / "harness.yaml").read_text(encoding="utf-8"))
    assert "offer_to_the_organisation" in (spec.get("loop_hooks") or [])
    from core import loop_hooks_builtin  # noqa: F401
    assert get_loop_hook("offer_to_the_organisation") is not None
