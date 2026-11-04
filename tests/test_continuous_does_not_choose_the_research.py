"""连续档不替用户开题，也不无限替人选「再改一轮」。

## 现场（2026-10-30，node20，ScienceMate 0.5.9 个人版）

新项目 `data_materials`，`python chat.py --project data_materials --continuous`，
开场一句「你好」。调度器自己的推理写着：用户只是打招呼，该先问他想研究什么。
它就用 `request_human_input` 问了「从哪个方向开题？」，给了四个从组织知识库里
凑出来的选项、推荐第 0 个。连续档 = bypass，框架照推荐项代答成
「硬碳/多孔碳负极储钠机理」—— 课题就这么开了。

接下来 hypothesis 被 REVISE 了十轮：每轮 reviewer 给 major_concerns、推荐 REVISE，
连续档 1 秒后照推荐自动选 [2]。prereg v1→v11、plan v5→v14，03:33 到 06:39，
一次实验没跑。reviewer 第一轮就写了「权威用户原始输入只有『你好』两个字」。

## 两件事，一道闸

  1. 项目里还没有研究时，没有东西可续：连续档不续轮；调度器那一问（只能是
     「研究什么」）不代答，真的停下来等人。
  2. 同一份产物连续 REVISE 到上限、reviewer 还推荐再改：这张卡不代答。

两种都由建卡方在 pause metadata 里写 `needs_a_person`（理由），自动作答的
每一条路（CLI 队列、stdin、平台 auto_resume、孤儿 pause）只读它 ——
`pause_driver.must_ask_a_person` 一处判。人能选什么一样不变。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import chat as chat_mod
from core import pause as pause_registry
from core import pause_driver
from core.agent_loop import run_loop
from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import PauseEvent, clear_all, get_paused_run
from core.research_situation import research_under_way
from core.session_driver import apply_autonomy, auto_resume_pauses, next_action
from core.state import State
from shared.tools.library import decision_package as dp

bootstrap(force=True)


def _orchestrator(tmp_path: Path) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                     project_id="p_fresh")


def _write_run(state: State, run_id: str, node_type: str, *, finished: bool = True,
               with_identity: bool = True) -> Path:
    """在调度器的兄弟目录里落一个 run —— 与 run_history 读的是同一份盘。"""
    d = state.root.parent / run_id
    d.mkdir(parents=True, exist_ok=True)
    if finished:
        (d / "summary.json").write_text(json.dumps({
            "run_id": run_id, "node_type": node_type,
            "project_id": state.project_id, "status": "completed",
        }), encoding="utf-8")
    else:
        head = ({"event": "run_start", "node_type": node_type,
                 "project_id": state.project_id} if with_identity else {})
        (d / "transcript.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    return d


def _continuous(state: State) -> None:
    state.hook_state.update(authorized_risk_classes=["*"], continuous_loop=True,
                            continuous_phase="running")
    apply_autonomy(state)


def _events(state: State) -> list[dict]:
    return [json.loads(line) for line in
            state.transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── 判据：研究开始了没有 ─────────────────────────────────────────────────────

def test_an_empty_project_has_no_research_to_continue(tmp_path):
    assert research_under_way(_orchestrator(tmp_path)) is False


def test_dreaming_is_not_research(tmp_path):
    """新项目一启动就在后台 dream（node20 现场「你好」之前就打了「后台整理记忆中」）。
    那不是谁给的目标，不能让连续档以为有东西可续。"""
    state = _orchestrator(tmp_path)
    _write_run(state, "1-curator", "_curator")
    _write_run(state, "2-curator", "_curator", finished=False)
    _write_run(state, "3-unknown", "", finished=False, with_identity=False)
    assert research_under_way(state) is False


@pytest.mark.parametrize("node_type", ["hypothesis", "literature"])
def test_any_node_the_user_asked_for_is_research(tmp_path, node_type):
    """服务节点也算：用户只要一次调研，那也是用户给的活。"""
    state = _orchestrator(tmp_path)
    _write_run(state, "1-node", node_type)
    assert research_under_way(state) is True


def test_a_node_still_running_is_research(tmp_path):
    state = _orchestrator(tmp_path)
    _write_run(state, "1-hyp", "hypothesis", finished=False)
    assert research_under_way(state) is True


def test_an_open_post_node_flow_is_research(tmp_path):
    state = _orchestrator(tmp_path)
    state.hook_state["pending_post_node_flow"] = [{"producing_node": "hypothesis"}]
    assert research_under_way(state) is True


def test_another_projects_runs_do_not_count(tmp_path):
    state = _orchestrator(tmp_path)
    other = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                      project_id="p_other")
    _write_run(other, "1-hyp", "hypothesis")
    assert research_under_way(state) is False


# ── 1a：调度器在「什么都没有」时的那一问，连续档不代答 ─────────────────────────

_ASK = {
    "header": "研究起点",
    "question": "这个项目还是空的（0 份执行记录、0 份产物），你想从哪个方向开题？",
    "options": [
        {"label": "硬碳/多孔碳负极储钠机理", "description": "KB 里这块存量最多"},
        {"label": "碳材料电子结构 DFT", "description": "石墨烯/多孔碳的能带"},
        {"label": "我直接说方向", "description": "按你的原话立题"},
    ],
    "recommended_option_index": 0,
}


def _ask_then_greet() -> MagicMock:
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[{
            "id": "call_ask", "type": "function",
            "function": {"name": "request_human_input",
                         "arguments": json.dumps(_ASK, ensure_ascii=False)}}],
            finish_reason="tool_calls", usage={"total_tokens": 10}),
        LLMResponse(content="好。", tool_calls=[], finish_reason="stop",
                    usage={"total_tokens": 10}),
    ])
    return llm


def _harness() -> NodeHarness:
    return NodeHarness(
        node_type="_orchestrator", version="0.1", system_prompt="test", rules=[],
        guidelines=[], skills=[], expected_outputs={},
        tools=["request_human_input"], max_turns=5, kb_query="_disable")


@pytest.mark.asyncio
async def test_the_question_what_to_research_waits_for_the_user(tmp_path):
    """node20 原样：连续档、新项目、调度器问开题方向 → 停下来，不代答。"""
    clear_all()
    state = _orchestrator(tmp_path)
    _continuous(state)
    messages = [LLMMessage(role="user", content="你好")]

    result = await run_loop(_harness(), state, messages, _ask_then_greet())

    assert result.status == "paused", "研究什么只有用户能定：这一问要真的停下来"
    pause = get_paused_run(state.run_id).pause_event
    assert pause_driver.needs_a_person(pause), "理由要随这一问走到每个前端"
    assert pause_driver.must_ask_a_person(pause) is True
    names = [e.get("event") for e in _events(state)]
    assert "human_input_bypassed" not in names
    (asked,) = [e for e in _events(state) if e.get("event") == "human_input_requested"]
    assert asked.get("needs_a_person")
    clear_all()


@pytest.mark.asyncio
async def test_once_research_is_under_way_the_orchestrator_is_answered_as_before(tmp_path):
    """反向：研究开始之后，调度器的提问照旧按推荐项代答（wangd 2026-08-19 的连续档语义）。"""
    clear_all()
    state = _orchestrator(tmp_path)
    _write_run(state, "1-hyp", "hypothesis")
    _continuous(state)
    messages = [LLMMessage(role="user", content="继续")]

    result = await run_loop(_harness(), state, messages, _ask_then_greet())

    assert result.status == "completed"
    (seen,) = [m for m in messages if m.role == "tool" and m.tool_call_id == "call_ask"]
    assert json.loads(seen.content)["answered_by"] == "unattended_default"


# ── 闸：所有自动作答的路都读同一个标记 ────────────────────────────────────────

def _person_only(kind: str = "structured_question") -> PauseEvent:
    return PauseEvent(question="q", options=["a", "b"], asking_run_id="r-person",
                      metadata={"type": kind, "needs_a_person": "只能由你定"})


def test_continuous_still_auto_answers_ordinary_cards(tmp_path):
    """闸只拦标了的那一问。连续档的其余语义一个字不动。"""
    _continuous(_orchestrator(tmp_path))
    plain = PauseEvent(question="q", metadata={"type": "decision_package"})
    assert pause_driver.must_ask_a_person(plain) is False
    assert pause_driver.must_ask_a_person(_person_only()) is True


def test_the_shared_resolver_hands_it_to_the_person(tmp_path):
    """CLI 队列与 stdin 都经 resolve_pause_answer：标了的那一问走人工，不走推荐项。"""
    _continuous(_orchestrator(tmp_path))
    asked: list = []

    async def person(pe):
        asked.append(pe)
        return "我直接说方向：钙钛矿稳定性"

    answer = asyncio.run(pause_driver.resolve_pause_answer(_person_only(), person))
    assert answer == "我直接说方向：钙钛矿稳定性"
    assert asked, "必须真的问到人"


class _Ctx:
    def __init__(self, pause_event: PauseEvent) -> None:
        self.run_id = pause_event.asking_run_id
        self.pause_event = pause_event
        self.parent_run_id = None


def test_the_platform_leaves_it_on_the_card(tmp_path):
    """平台的 auto_resume_pauses 不接管：卡片留给人。"""
    _continuous(_orchestrator(tmp_path))
    clear_all()
    pause_registry.register_pause(_Ctx(_person_only("decision_package")))
    try:
        assert asyncio.run(auto_resume_pauses()) is None
        assert pause_registry.list_paused(), "卡片还在"
    finally:
        clear_all()


def test_an_orphaned_one_is_left_for_the_person_not_answered(tmp_path):
    """孤儿 pause 的兜底（防死锁）也不替人答这一问；续轮因此停下等人。"""
    state = _orchestrator(tmp_path)
    _write_run(state, "1-hyp", "hypothesis")
    _continuous(state)
    clear_all()
    pause_registry.register_pause(_Ctx(_person_only("decision_package")))
    try:
        handled = asyncio.run(pause_driver.resolve_orphan_pauses(
            state, pause_registry.list_paused()))
        assert handled is False
        assert pause_registry.list_paused(), "不清注册表：它就是在等人"

        action = asyncio.run(next_action(state, "", reason="completed"))
        assert action.kind == "stop" and action.reason == "waiting_for_a_person"
    finally:
        clear_all()


# ── 1b：没有研究可续，就不续轮 ────────────────────────────────────────────────

def test_a_greeting_is_not_followed_by_a_continuous_turn(tmp_path):
    """调度器只回了一句问候：连续档不能再推它一轮「继续用户的原始目标」。"""
    state = _orchestrator(tmp_path)
    _continuous(state)

    prompt, _delay = chat_mod._continuous_followup(
        state, "你好！想研究什么？", reason="completed")

    assert prompt is None
    assert state.hook_state["continuous_phase"] == "running", "不是停机：给了目标照常续"
    assert "continuous_followup_withheld" in [e.get("event") for e in _events(state)]


@pytest.mark.parametrize("trigger", ["session_start", "background_completed"])
def test_the_other_two_kicks_are_withheld_too(tmp_path, trigger):
    """CLI 里不经 next_action 的两处续轮（会话启动、后台 dreaming 结束）同样不续。"""
    state = _orchestrator(tmp_path)
    _write_run(state, "1-curator", "_curator")
    _continuous(state)
    prompt, _ = chat_mod._continuous_followup(
        state, "CONTINUOUS_STATUS: continue", reason=trigger)
    assert prompt is None


def test_next_action_says_why_it_did_not_continue(tmp_path):
    state = _orchestrator(tmp_path)
    _continuous(state)
    action = asyncio.run(next_action(state, "你好！", reason="completed"))
    assert action.kind == "stop" and action.reason == "no_research_yet"
    assert action.extra.get("detail"), "停下来要说得出为什么（平台渲染成会话消息）"


def test_once_a_node_runs_continuous_resumes(tmp_path):
    state = _orchestrator(tmp_path)
    _continuous(state)
    _write_run(state, "1-hyp", "hypothesis", finished=False)
    prompt, _ = chat_mod._continuous_followup(
        state, "CONTINUOUS_STATUS: continue", reason="completed")
    assert prompt is not None


# ── 2：REVISE 链到上限，这张卡要人选 ─────────────────────────────────────────

def _chain(state: State, *runs: str, action: str = "revise") -> None:
    for old, new in zip(runs, runs[1:]):
        state.append_transcript(
            "decision_action_completed", producing_run_id=old,
            authorized_action=action, authorized_target_node="hypothesis",
            replacement_run_id=new, old_flow_closed=True)


def test_the_chain_is_read_from_the_replacement_links(tmp_path):
    state = _orchestrator(tmp_path)
    _chain(state, "v1", "v2", "v3", "v4")
    _chain(state, "x1", "x2")                      # 别的链不串进来
    assert dp._revise_chain(state, "v4") == ["v1", "v2", "v3"]
    assert dp._revise_chain(state, "v2") == ["v1"]
    assert dp._revise_chain(state, "v1") == []
    assert dp._revise_chain(state, "x2") == ["x1"]


def test_a_redirect_breaks_the_chain(tmp_path):
    state = _orchestrator(tmp_path)
    _chain(state, "v1", "v2")
    _chain(state, "v2", "v3", action="redirect_upstream")
    _chain(state, "v3", "v4")
    assert dp._revise_chain(state, "v4") == ["v3"]


def _present(state: State, producing_run_id: str, *, action: str = "revise") -> dict:
    state.save_artifact(
        "review_critique", f"c_{producing_run_id}",
        json.dumps({"verdict": "major_concerns",
                    "recommended_action": {"action": action, "feedback": "网格没覆盖 0.45 nm"},
                    "concerns": [{"severity": "major", "description": "网格没覆盖 0.45 nm"}]},
                   ensure_ascii=False),
        metadata={"produced_by_node_type": "_reviewer",
                  "source_node_type": "hypothesis"},
        provenance=forwarded(produced("_reviewer", f"review-{producing_run_id}"),
                             via_node_type=state.node_type, via_run_id=state.run_id))
    cid = f"review_critique__c_{producing_run_id}"
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": producing_run_id,
        "artifact_ids": ["research_plan__x"], "review_state": "done",
        "review_critique_artifact_id": cid, "curator_state": "done",
        "decision_state": "pending"}]
    return asyncio.run(dp._present_decision_package(
        state, source_node_type="hypothesis", producing_run_id=producing_run_id,
        review_critique_artifact_id=cid))


def test_below_the_cap_continuous_keeps_revising(tmp_path):
    state = _orchestrator(tmp_path)
    _chain(state, "v1", "v2", "v3")                # v3 是第 3 版：改了 2 轮
    out = _present(state, "v3")
    assert out["status"] == "success" and out["decision_required"] is True
    assert not pause_driver.needs_a_person(PauseEvent.from_payload(out["offer"]))


def test_at_the_cap_the_card_waits_for_the_person(tmp_path):
    """node20 那条链在第 4 版（改了 3 轮）就该停下来交人 —— 而不是转到第 11 版。"""
    state = _orchestrator(tmp_path)
    _continuous(state)
    _chain(state, "v1", "v2", "v3", "v4")
    out = _present(state, "v4")

    pause = PauseEvent.from_payload(out["pause_event"])
    assert pause_driver.needs_a_person(pause)
    assert pause_driver.must_ask_a_person(pause) is True, "连续档也不替人选"
    assert "⏸ 这张卡要你本人选" in pause.context, "人要看得见为什么这张没被自动选"
    assert "revise" in pause.metadata["recommended_action"], "推荐不改：人照样可以再改一轮"
    capped = [e for e in _events(state) if e.get("event") == "unattended_revise_capped"]
    assert capped and capped[0]["earlier_runs"] == ["v1", "v2", "v3"]
    presented = [e for e in _events(state) if e.get("event") == "decision_package_presented"]
    assert presented[-1].get("needs_a_person"), "平台建 Decision 读的就是这个事件"


def test_a_long_chain_does_not_stop_a_card_that_says_proceed(tmp_path):
    """封顶只管「再改一轮」。reviewer 说可以往前走了，就照常自动放行。"""
    state = _orchestrator(tmp_path)
    _chain(state, "v1", "v2", "v3", "v4", "v5")
    out = _present(state, "v5", action="proceed")
    assert out["status"] == "success" and out["decision_required"] is True
    assert not pause_driver.needs_a_person(PauseEvent.from_payload(out["offer"]))
