"""答复被受理的那一刻就是 pause 的终点 —— 不是 LLM 续跑的那一刻。

## 现场（2026-10-28，qinp，ScienceMate 0.5.7 Windows，连续档）

writing 跑完 → reviewer major_concerns → 决策卡呈递 → `loop_pause`（注册表登记、
pause_pending.json 落盘、平台投成 waiting_human、界面画出「等你回答」）→ **12 ms 后**
连续档替人选了 REVISE → `decision_action_authorized` → 运行时**同步**重跑 writing
（上一轮 68 分钟）。`loop_resume` / 清注册表 / 删磁盘标记三件事都在 `resume_loop`
里，而它要等重跑结束才轮得到。于是这一个多小时里：

    注册表：pause 还在            磁盘：pause_pending.json 还在
    平台：run = waiting_human     界面：卡片挂着，「先回答上面那个问题」
    事实：答复早已受理，writing 正在重跑

用户连打三句「继续」「你让我回答什么呢」「我看你已经在重跑了」都只能进收件箱。

同一个洞在级联路径上修过一次（#1083，父 run 补写 `loop_resume`），顶层路径原样
活着 —— 因为"pause 什么时候结束"此前没有一处说了算。现在 `agent_loop.end_pause`
说了算，`_settle_decision_answer` 在受理那一刻调它。

判据落在**执行授权动作的那段时间里**各投影说什么，不落在最终状态上 —— 最终
状态一直是对的，病在中间那一个小时。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.bootstrap import bootstrap
from core.decision_offer import Choice, Offer
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import (
    PauseEvent,
    PausedRunContext,
    clear_all,
    get_paused_run,
    register_pause,
)
from core.pause_driver import drive_pause_chain
from core.state import State

PRODUCING_RUN = "run_prod_1"
CALL_ID = "call_decision_1"


@pytest.fixture(autouse=True)
def _isolate():
    bootstrap()
    clear_all()
    yield
    clear_all()


class _Stop(Exception):
    """平台的 answer 路径只答一次，第二次问就抛（AdditionalPauseRequired 的形状）。"""


def _offer(*choice_ids: str) -> Offer:
    labels = {
        "proceed": "PROCEED to next stage",
        "revise": "REVISE (re-run source_node with reviewer feedback)",
        "edit": "EDIT manually then proceed (pauses for you to edit artifact / KB)",
        "abort": "ABORT pipeline",
    }
    return Offer(
        decision_id=f"{PRODUCING_RUN}:p1",
        kind="post_node",
        question="Post-node decision for hypothesis",
        choices=tuple(Choice(id=c, label=labels[c]) for c in choice_ids),
        recommended_id="revise" if "revise" in choice_ids else choice_ids[0],
    )


def _paused_orchestrator(offer: Offer) -> tuple[State, PausedRunContext]:
    """一个停在决策卡上的顶层 run：注册表里有它、磁盘上有标记、transcript 有 loop_pause。"""
    st = State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()), project_id=None)
    st.hook_state["pending_post_node_flow"] = [{
        "producing_run_id": PRODUCING_RUN,
        "producing_node": "hypothesis",          # 欠 post-node flow 的节点，REVISE 可执行
        "review_state": "done",
        "decision_options": list(offer.choice_ids()),
        "producing_node_inputs": {"research_question": "q"},
    }]
    payload = offer.to_pause_payload()
    payload["metadata"] = {
        "type": "decision_package",
        "producing_run_id": PRODUCING_RUN,
        "decision_id": offer.decision_id,
        "recommended_feedback": "reviewer: 对照组维度不一致。",
    }
    pause_event = PauseEvent.from_payload(
        payload, pending_tool_call_id=CALL_ID, default_node_type="_orchestrator",
        default_run_id=st.run_id)

    llm = MagicMock()
    llm.chat = AsyncMock(return_value=LLMResponse(
        content="done", tool_calls=[], finish_reason="stop", usage={}))
    msgs = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": CALL_ID, "type": "function",
            "function": {"name": "run_node", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id=CALL_ID, name="run_node",
                   content='{"status":"pause"}'),
    ]
    h = NodeHarness(node_type="_orchestrator", version="0.1", system_prompt="", rules=[],
                    guidelines=[], skills=[], expected_outputs={}, tools=[], max_turns=3,
                    kb_query="_disable")
    ctx = PausedRunContext(
        run_id=st.run_id, state=st, messages=msgs, harness=h, llm=llm,
        pending_tool_call_id=CALL_ID, pause_event=pause_event)
    register_pause(ctx)
    st.append_transcript("loop_pause", turn=1, question=pause_event.question,
                         pending_tool_call_id=CALL_ID)
    (st.root / "pause_pending.json").write_text("{}", encoding="utf-8")
    return st, ctx


def _events(st: State) -> list[str]:
    return [json.loads(line)["event"]
            for line in st.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _answer_once(offer: Offer, choice_id: str):
    """像平台的 answer 路径：只回答一次，再问就抛。"""
    asked: list[PauseEvent] = []

    async def ask(pause_event: PauseEvent):
        asked.append(pause_event)
        if len(asked) > 1:
            raise _Stop
        return {"offer_id": offer.offer_id, "choice_id": choice_id}
    return ask, asked


def _snapshot_during_dispatch(st: State, run_id: str) -> dict:
    """授权动作**正在执行**时，各投影怎么说。"""
    return {
        "registered": get_paused_run(run_id) is not None,
        "marker_on_disk": (st.root / "pause_pending.json").exists(),
        "events": _events(st),
    }


def test_the_card_is_gone_while_the_authorized_action_runs(monkeypatch):
    """REVISE 受理后、重跑 writing/hypothesis 的那段时间里，没有任何投影还说「在等人」。"""
    import shared.tools.run_node as run_node_mod

    offer = _offer("proceed", "revise")
    st, ctx = _paused_orchestrator(offer)
    during: dict = {}

    async def _fake_run_node(state_, target, **kw):
        state_.append_transcript("fake_dispatch_ran", target=target)
        during.update(_snapshot_during_dispatch(st, ctx.run_id))
        return {"status": "success", "run_id": "child-1"}

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _fake_run_node)
    ask, _asked = _answer_once(offer, "revise")

    asyncio.run(drive_pause_chain(ask_fn=ask))

    assert during, "授权动作根本没被执行"
    assert during["registered"] is False, "重跑期间注册表里 pause 还在 —— 平台会继续说在等人"
    assert during["marker_on_disk"] is False, "重跑期间 pause_pending.json 还在"
    assert "loop_resume" in during["events"], (
        "受理答复之后、起重跑之前没写 loop_resume —— 平台据此一直投 waiting_human，"
        f"卡片会挂整个重跑。重跑开始时的事件：{during['events']}")
    ev = during["events"]
    assert ev.index("decision_action_authorized") < ev.index("loop_resume") < ev.index("fake_dispatch_ran")


def test_the_pause_ends_exactly_once_on_the_accepted_path(monkeypatch):
    """`resume_loop` 之后不许再写第二条 loop_resume（平台会把它投成第二次 run.resumed）。"""
    import shared.tools.run_node as run_node_mod

    offer = _offer("proceed", "revise")
    st, _ctx = _paused_orchestrator(offer)

    async def _fake_run_node(state_, target, **kw):
        return {"status": "success"}

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _fake_run_node)
    ask, _ = _answer_once(offer, "revise")
    asyncio.run(drive_pause_chain(ask_fn=ask))

    ev = _events(st)
    assert ev.count("loop_resume") == 1, ev
    assert ev.index("loop_resume") < ev.index("loop_finished"), "续跑完了才记恢复，就是原来的病"
    assert get_paused_run(st.run_id) is None
    assert not (st.root / "pause_pending.json").exists()


def test_a_failed_dispatch_reinstates_the_pause_for_every_projection(monkeypatch):
    """运行时没能执行人的决定 → 这是一次**新的** pause：三处投影都要重新挂上。"""
    import shared.tools.run_node as run_node_mod

    offer = _offer("proceed", "revise")
    st, ctx = _paused_orchestrator(offer)

    async def _rejecting_run_node(state_, target, **kw):
        return {"status": "error", "error": "node_inputs 与输入契约不匹配"}

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _rejecting_run_node)
    ask, asked = _answer_once(offer, "revise")

    with pytest.raises(_Stop):
        asyncio.run(drive_pause_chain(ask_fn=ask))

    # 人会被再问一次，而且问的是带失败原因的那张卡
    assert len(asked) == 2
    assert "没能执行" in asked[1].question
    assert "输入契约" in asked[1].context
    # 注册表 / 磁盘 / transcript 三处都重新说「在等人」
    reinstated = get_paused_run(st.run_id)
    assert reinstated is not None and reinstated.pause_event.question == asked[1].question, (
        "派发失败后注册表里没有 pause —— 平台会把 run 当成已完成，人再也没法重新裁决")
    assert (st.root / "pause_pending.json").exists()
    ev = _events(st)
    assert ev.index("loop_resume") < ev.index("authorized_action_dispatch_failed") < len(ev) - 1
    assert ev[-1] == "loop_pause", f"重新挂起没写 loop_pause：{ev}"


def test_edit_keeps_the_pause_because_that_is_what_edit_means(monkeypatch):
    """EDIT 的语义是"停在这里等你改"——它不是 pause 的终点。"""
    import shared.tools.run_node as run_node_mod

    offer = _offer("proceed", "revise", "edit")
    st, _ctx = _paused_orchestrator(offer)
    called = []

    async def _fake_run_node(state_, target, **kw):
        called.append(target)
        return {"status": "success"}

    monkeypatch.setattr(run_node_mod, "_run_node_tool", _fake_run_node)
    ask, asked = _answer_once(offer, "edit")

    with pytest.raises(_Stop):
        asyncio.run(drive_pause_chain(ask_fn=ask))

    assert not called, "EDIT 不该起任何节点"
    assert "loop_resume" not in _events(st), "EDIT 把 pause 记成了结束"
    assert get_paused_run(st.run_id) is not None
    assert (st.root / "pause_pending.json").exists()
    assert "编辑完成了吗" in asked[1].question


def test_a_plain_pause_still_ends_in_resume_loop():
    """不经过受理路径的 pause（request_human_input）照旧由 resume_loop 记一次终点。"""
    st = State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()), project_id=None)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value=LLMResponse(
        content="done", tool_calls=[], finish_reason="stop", usage={}))
    msgs = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": "q1", "type": "function",
            "function": {"name": "request_human_input", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id="q1", name="request_human_input",
                   content='{"status":"pause"}'),
    ]
    h = NodeHarness(node_type="_orchestrator", version="0.1", system_prompt="", rules=[],
                    guidelines=[], skills=[], expected_outputs={}, tools=[], max_turns=3,
                    kb_query="_disable")
    register_pause(PausedRunContext(
        run_id=st.run_id, state=st, messages=msgs, harness=h, llm=llm,
        pending_tool_call_id="q1",
        pause_event=PauseEvent(question="哪个阈值？", asking_node_type="_orchestrator")))
    (st.root / "pause_pending.json").write_text("{}", encoding="utf-8")

    async def ask(_pe):
        return "0.02"

    asyncio.run(drive_pause_chain(ask_fn=ask))
    ev = _events(st)
    assert ev.count("loop_resume") == 1, ev
    assert get_paused_run(st.run_id) is None
    assert not (st.root / "pause_pending.json").exists()
