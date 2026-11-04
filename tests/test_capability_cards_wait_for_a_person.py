"""能力授权卡：有人的档位留给人；没人的档位替人拒，并如实记成被挡住（#1254，RFC D6）。

#1254（nidy，0.5.6 Windows，自主档）：experiment 申请连 repo.msys2.org，卡片 1.2 秒内被档位
按推荐项（拒绝）替人点掉；调度器把同一张卡转给人，人在旁边连打六次「允许」都落空；子节点在
没有授权的情况下又跑了一个小时，这段时间里转发的那张卡一直说「等你回答」。

三件事：
1. 推荐项「拒绝」是给**没人答**准备的安全默认 —— 只有连续档（真没人）才按它替人拒；
2. 替人拒掉的，记成 `capability_not_granted` blocker（人亲手拒的不记：那是一个决定）；
3. 叶子的问题一有受理的答复，沿途转发这张卡的祖先就不再「等人」；叶子又停下来，再转发上去。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import pause_driver
from core.agent_loop import LoopResult, write_pause_pending
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import PauseEvent, PausedRunContext, clear_all, get_paused_run, register_pause
from core.state import State
from shared.lib import dangerous_commands as dc


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    bootstrap()
    clear_all()
    monkeypatch.setattr(dc, "BYPASS_ENABLED", False)
    yield
    clear_all()


def _grant_card(host: str = "repo.msys2.org", call_id: str = "c1") -> PauseEvent:
    return PauseEvent(
        question=f"允许这次课题连接 {host} 吗？", options=["允许", "拒绝"],
        asking_node_type="experiment", pending_tool_call_id=call_id,
        metadata={"type": "capability_grant", "host": host, "reason": "装 QE",
                  "recommended_option_index": 1},
    )


def _ctx(nt: str, call_id: str, event: PauseEvent, *, parent: State | None = None,
         on_chat=None) -> State:
    st = State.new(node_type=nt, base_dir=Path(tempfile.mkdtemp()), project_id=None)

    async def chat(messages, **kw):
        if on_chat is not None:
            on_chat()
        return LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={})

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=chat)
    msgs = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": call_id, "type": "function",
            "function": {"name": "request_network_access", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id=call_id, name="request_network_access",
                   content='{"status":"pause"}'),
    ]
    h = NodeHarness(node_type=nt, version="0.1", system_prompt="", rules=[], guidelines=[],
                    skills=[], expected_outputs={}, tools=[], max_turns=3, kb_query="_disable")
    event = PauseEvent(**{**event.__dict__, "pending_tool_call_id": call_id,
                          "asking_run_id": st.run_id})
    register_pause(PausedRunContext(
        run_id=st.run_id, state=st, messages=msgs, harness=h, llm=llm,
        pending_tool_call_id=call_id, pause_event=event,
        parent_run_id=parent.run_id if parent else None,
        parent_tool_call_id="p1" if parent else None))
    write_pause_pending(st, event, 1)
    return st


def _events(st: State) -> list[dict]:
    if not st.transcript_path.exists():
        return []
    return [json.loads(line) for line in st.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _named(st: State, name: str) -> list[dict]:
    return [e for e in _events(st) if e.get("event") == name]


# ── 1. 谁替人答 ───────────────────────────────────────────────────────────────


def test_an_attended_session_leaves_the_grant_card_to_its_person() -> None:
    assert pause_driver.must_ask_a_person(_grant_card()) is True


def test_only_continuous_mode_answers_it_for_the_person(monkeypatch) -> None:
    monkeypatch.setattr(dc, "BYPASS_ENABLED", True)
    assert pause_driver.must_ask_a_person(_grant_card()) is False
    answer = pause_driver.auto_approve_answer(_grant_card())
    assert answer == "2" and isinstance(answer, pause_driver.AutomaticAnswer)


def test_the_autonomous_tier_no_longer_refuses_it_in_a_second(monkeypatch) -> None:
    """#1254 的原样：自主档（非连续）在 operation 收尾时消化暂停 —— 授权卡不能在里面。"""
    from core.session_driver import auto_resume_pauses

    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    child = _ctx("experiment", "c1", _grant_card())
    assert asyncio.run(auto_resume_pauses()) is None
    assert get_paused_run(child.run_id) is not None, "卡片还在等人"
    assert not _named(child, "capability_grant_answered")


# ── 2. 替人拒掉的要记下来，人拒的不记 ──────────────────────────────────────────


def _drive(ask) -> None:
    asyncio.run(pause_driver.drive_pause_chain(ask_fn=ask))


def test_a_refusal_nobody_gave_is_recorded_as_a_blocker(monkeypatch) -> None:
    monkeypatch.setattr(dc, "BYPASS_ENABLED", True)
    child = _ctx("experiment", "c1", _grant_card("psl.noaa.gov"))

    async def auto(pe):
        return pause_driver.auto_approve_answer(pe)

    _drive(auto)
    answered = _named(child, "capability_grant_answered")
    assert answered and answered[0]["granted"] is False and answered[0]["answered_by"] == "auto"
    blockers = child.hook_state.get("blockers") or []
    assert [b.get("kind") for b in blockers] == ["capability_not_granted"], blockers
    assert blockers[0]["subject"] == "psl.noaa.gov" and blockers[0]["capability"] == "egress_host"
    reported = _named(child, "blocker_reported")
    assert reported and reported[0].get("kind") == "capability_not_granted", (
        "结构化事实要进 transcript —— 平台与诊断包读的是它")


def test_a_person_saying_no_is_a_decision_not_a_blocker() -> None:
    child = _ctx("experiment", "c1", _grant_card())

    async def person(pe):
        return "拒绝"

    _drive(person)
    answered = _named(child, "capability_grant_answered")
    assert answered[0]["answered_by"] == "person" and answered[0]["granted"] is False
    assert not child.hook_state.get("blockers")


def test_a_gpu_card_refused_for_nobody_names_the_cards(monkeypatch) -> None:
    monkeypatch.setattr(dc, "BYPASS_ENABLED", True)
    card = _grant_card()
    card.metadata = {"type": "capability_grant", "capability": "gpu_runtime", "gpus": 2,
                     "recommended_option_index": 1}
    child = _ctx("experiment", "c1", card)

    async def auto(pe):
        return pause_driver.auto_approve_answer(pe)

    _drive(auto)
    blocker = (child.hook_state.get("blockers") or [{}])[0]
    assert blocker.get("capability") == "gpu_runtime" and "2 张" in blocker.get("subject", "")


# ── 3. 转发的卡跟着叶子的答复走 ───────────────────────────────────────────────


def test_the_relayed_card_comes_down_when_the_leaf_is_answered() -> None:
    """叶子开始续跑的那一刻，父 run 已经不再「等人」：loop_resume 写了、磁盘标记删了。"""
    seen: dict = {}
    root = _ctx("_orchestrator", "p1", _grant_card(call_id="p1"))

    def on_child_running():
        seen.setdefault("root_resumed", bool(_named(root, "loop_resume")))
        seen.setdefault("root_marker", (root.root / "pause_pending.json").exists())

    _ctx("experiment", "c1", _grant_card(), parent=root, on_chat=on_child_running)

    async def person(pe):
        return "允许"

    _drive(person)
    assert seen == {"root_resumed": True, "root_marker": False}, (
        f"叶子在跑，父 run 还说在等人（#1254：卡片挂了一小时）：{seen}")
    resumes = _named(root, "loop_resume")
    assert len(resumes) == 1, f"父 run 的恢复只记一次：{resumes}"


def test_if_the_leaf_asks_again_the_parent_shows_the_new_card(monkeypatch) -> None:
    root = _ctx("_orchestrator", "p1", _grant_card(call_id="p1"))
    child = _ctx("experiment", "c1", _grant_card(), parent=root)
    calls = {"n": 0}
    real_resume = pause_driver.resume_loop

    async def resume(ctx, answer, *, recorded_decision=None):
        if ctx.run_id != child.run_id or calls["n"]:
            return await real_resume(ctx, answer, recorded_decision=recorded_decision)
        calls["n"] += 1
        # 叶子续跑后又停下来问一个新问题（同一个 run，新的 pause）。
        again = PauseEvent(question="还要连 mirrors.tuna.tsinghua.edu.cn 吗？",
                           options=["允许", "拒绝"], asking_node_type="experiment",
                           asking_run_id=child.run_id, pending_tool_call_id="c2",
                           metadata={"type": "capability_grant",
                                     "host": "mirrors.tuna.tsinghua.edu.cn"})
        messages = [*ctx.messages,
                    LLMMessage(role="assistant", content="", tool_calls=[{
                        "id": "c2", "type": "function",
                        "function": {"name": "request_network_access", "arguments": "{}"}}]),
                    LLMMessage(role="tool", tool_call_id="c2", name="request_network_access",
                               content='{"status":"pause"}')]
        register_pause(PausedRunContext(
            run_id=child.run_id, state=ctx.state, messages=messages, harness=ctx.harness,
            llm=ctx.llm, pending_tool_call_id="c2", pause_event=again,
            parent_run_id=root.run_id, parent_tool_call_id="p1"))
        return LoopResult(max_turns=3, final_text="", turns=1, tool_calls=[], messages=ctx.messages,
                          status="paused", terminal_cause="pause_event", pause_event=again)

    monkeypatch.setattr(pause_driver, "resume_loop", resume)
    answers = iter(["允许", "拒绝"])

    async def person(pe):
        return next(answers)

    _drive(person)
    pauses = [e for e in _named(root, "loop_pause") if e.get("reinstated")]
    assert pauses and pauses[0]["reason"] == "child_paused_again"
    assert "mirrors.tuna" in pauses[0]["question"]
    assert pauses[0]["pending_tool_call_id"] == "p1", (
        "父 run 的 pending_tool_call_id 是它那次 run_node 的 id —— 被子 run 的盖掉，级联回填就找不到那条消息")
