"""人工决定必须**驱动执行**，而不只是存进账本（issue #183）。

qinp 2026-07-26 PODsys canonical E2E 实测事故：
  - literature producer completed，其 _reviewer incomplete、未产 review_critique；
  - decision package 用 review-failed 专用选项集（**不含 PROCEED**）：
      [1] RETRY REVIEWER  [2] REVISE  [3] REDIRECT  [4] ABORT  [5] EDIT
  - 人工回 "1"；#155 的 record_decision_answer 正确落库
    accepted_action=retry_reviewer、review_state=retry_authorized；
  - 但 resume 只把**原始文本 "1"** 回填给 orchestrator LLM，glm 按最常见的
    "[1] 继续" 套用，复述成「用户选择 PROCEED」，把绑定 task 标 complete、
    对人谎报 review 已放行，且从未真的起 _reviewer。

即：#155 解决了"框架能否确知人工选了什么"（账本侧），没解决"框架据此驱动
执行"（执行侧）。本测试锁死执行侧的机械保护：resume 回填的权威动作。

判决拆除·第三波：原来还有第二道「task(complete) 一致性 gate」——账本说
retry/revise/redirect 时拒绝把绑定 task 标 complete。task 清单是节点私账，标
complete 不改变任何执行事实；真正的执行闸在 run_node 派发前查
pending_post_node_flow。那道闸只制造第二份判决，已删；本文件锁死它**不回来**。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from shared.tools.library.decision_package import (
    _REVIEW_FAILED_ACTIONS,
    describe_recorded_decision,
    record_decision_answer,
)
from shared.tools.library.tasks import _task


def _make_state(tmp: Path) -> State:
    """带 project_root 的 orchestrator state。project_root 由 project_id 推导，
    这里显式改指 tmp —— 免得测试往真实 ~/.harness-framework 里写 task。"""
    st = State.new(node_type="_orchestrator", base_dir=tmp / "runs",
                   project_id="p1")
    st.project_root = tmp / "proj"
    (tmp / "proj").mkdir(parents=True, exist_ok=True)
    return st


def _review_failed_flow(state: State, *, task_id: str | None,
                        producing_run_id: str = "run-lit-1") -> dict:
    """复刻事故现场：producer 完成、reviewer 失败、等人工决定的 flow entry。

    producing_node 必须是**真会登记 flow entry** 的节点。原来写的是 literature，
    而它是服务节点（`post_run_flow: none`）—— 既不挂 reviewer 也不进账本，这个
    现场生产里造不出来。夹具和生产不同形，藏住的正是这次的缺口。
    """
    entry = {
        "producing_node": "hypothesis",
        "producing_run_id": producing_run_id,
        "task_id": task_id,
        "artifact_ids": ["a1"],
        "review_state": "failed_awaiting_human",
        "review_failed_reason": "reviewer produced no critique",
        "curator_state": "pending",
        "decision_state": "pending",
        "decision_options": list(_REVIEW_FAILED_ACTIONS),
    }
    state.hook_state["pending_post_node_flow"] = [entry]
    return entry


# ── B. 权威回填：resume 交给 LLM 的 payload 含已解析动作 ─────────────────────


def test_describe_recorded_decision_renders_authoritative_action():
    """裸数字 "1" 在 review-failed 集里 = RETRY REVIEWER，渲染必须写明，
    不能让 LLM 靠"最常见选项集"猜。"""
    entry = {"accepted_action": "retry_reviewer",
             "decision_options": list(_REVIEW_FAILED_ACTIONS)}
    text = describe_recorded_decision(entry)
    assert "retry_reviewer" in text
    assert "RETRY REVIEWER" in text
    assert text.startswith("[1]")
    assert "PROCEED" not in text          # 该选项集根本没有 PROCEED


def test_describe_recorded_decision_none_without_action():
    """没有结构化决定时返回 None → resume 照旧只回原文，行为不变。"""
    assert describe_recorded_decision(None) is None
    assert describe_recorded_decision({}) is None


def test_resume_payload_contains_authoritative_decision(tmp_path):
    """B（端到端）：record → describe 的结果进入 resume 回填 payload。"""
    import json

    from core.agent_loop import resume_loop
    from core.llm import LLMMessage
    from core.pause import PausedRunContext, PauseEvent

    state = _make_state(tmp_path)
    _review_failed_flow(state, task_id="T01")
    meta = {"type": "decision_package", "producing_run_id": "run-lit-1"}
    entry = record_decision_answer(state, meta, "1")
    assert entry["accepted_action"] == "retry_reviewer"   # 账本侧（#155）正确

    msgs = [LLMMessage(role="tool", tool_call_id="tc1",
                       name="present_decision_package", content="(paused)")]
    ctx = PausedRunContext(
        run_id=state.run_id, state=state, messages=msgs,
        harness=None, llm=None, pending_tool_call_id="tc1",
        pause_event=PauseEvent(
            question="Post-node decision", asking_node_type="_orchestrator",
            asking_run_id=state.run_id, pending_tool_call_id="tc1",
            metadata=meta),
    )
    captured = {}

    async def _fake_run_loop(harness, st, messages, llm):
        captured["payload"] = json.loads(messages[0].content)
        from core.agent_loop import LoopResult
        return LoopResult(final_text="ok", turns=1, messages=messages)

    import core.agent_loop as al
    orig = al.run_loop
    al.run_loop = _fake_run_loop
    try:
        asyncio.run(resume_loop(
            ctx, "1", recorded_decision=describe_recorded_decision(entry)))
    finally:
        al.run_loop = orig

    payload = captured["payload"]
    assert payload["response"] == "1"                     # 原文仍在（不删能力）
    assert "retry_reviewer" in payload["recorded_decision"]
    assert "权威" in payload["authoritative"]


# ── A + D：分叉被拦 —— 账本说 retry，就不许标 task complete / 谎报 PROCEED ──


def _mk_task(state: State, title="T01 literature") -> str:
    r = asyncio.run(_task(state, action="create", title=title))
    return r["task"]["id"]


def test_task_complete_is_not_gated_by_an_open_decision(tmp_path):
    """判决拆除：账本说 retry_reviewer 时 task(complete) 照记（私账不拦执行）。

    墙若被加回来（拒绝 / blocking_decision 字段），这条转红。执行闸在 run_node
    派发前查 pending_post_node_flow —— 这里顺带锁死 flow entry 不被 complete
    改坏：review_state 仍是 retry_authorized，人工选的动作一字不改。
    """
    state = _make_state(tmp_path)
    tid = _mk_task(state)
    asyncio.run(_task(state, action="start", task_id=tid))
    _review_failed_flow(state, task_id=tid)
    record_decision_answer(state, {"type": "decision_package",
                                   "producing_run_id": "run-lit-1"}, "1")

    res = asyncio.run(_task(state, action="complete", task_id=tid,
                            notes="Reviewer failed, user chose PROCEED"))

    assert res["status"] == "success", res
    assert "blocking_decision" not in res
    assert res["task"]["status"] == "completed"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["review_state"] == "retry_authorized"
    assert entry["accepted_action"] == "retry_reviewer"


def test_the_task_gate_stays_deleted():
    """`blocking_decision_for_task` 不许长回来：它是 run_node 执行闸的第二份判决。"""
    import shared.tools.library.decision_package as dp
    import shared.tools.library.tasks as tasks_mod

    assert not hasattr(dp, "blocking_decision_for_task")
    assert not hasattr(dp, "_ACTIONS_WORK_MUST_CONTINUE")
    assert "blocking_decision" not in Path(tasks_mod.__file__).read_text(encoding="utf-8")


# ── C. 正确路径不回归：PROCEED / 已闭合 flow ──────────────────────────────────


def test_proceed_decision_allows_complete(tmp_path):
    """人工在**有** PROCEED 的正常 package 里选 PROCEED → 必须放行。"""
    state = _make_state(tmp_path)
    tid = _mk_task(state)
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": "run-lit-1",
        "task_id": tid, "review_state": "done", "curator_state": "done",
        "decision_state": "done", "accepted_action": "proceed",
    }]
    res = asyncio.run(_task(state, action="complete", task_id=tid))
    assert res["status"] == "success"
    assert res["task"]["status"] == "completed"


def test_no_flow_no_block(tmp_path):
    """没有任何 post-node flow（如非 producing 相关的杂务 task）→ 不受影响。"""
    state = _make_state(tmp_path)
    tid = _mk_task(state)
    res = asyncio.run(_task(state, action="complete", task_id=tid))
    assert res["status"] == "success"


def test_resolved_flow_does_not_block(tmp_path):
    """retry 成功、review_state 已 done → 不再拦（否则就是我 #151 那种死角）。"""
    state = _make_state(tmp_path)
    tid = _mk_task(state)
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": "run-lit-1",
        "task_id": tid, "review_state": "done",
        "accepted_action": "retry_reviewer",     # 历史动作，但已走完
    }]
    res = asyncio.run(_task(state, action="complete", task_id=tid))
    assert res["status"] == "success"


# ── #761：决策附言是用户的话，必须进权威原文段 ─────────────────────────────


def test_decision_note_lands_in_research_intake_as_user_words(tmp_path):
    """课题负责人在 REVISE 附言里给的「中位数 AMI 提升 ≥0.02」——节点如实引用，
    reviewer 对账时的「用户权威原文」只有开题 brief → 判 phantom。附言要走进
    research_intake（来源标 decision_note），核验面才看得到这条通道。"""
    from core.research_intake import load_intake

    state = _make_state(tmp_path)
    _review_failed_flow(state, task_id=None)
    meta = {"type": "decision_package", "producing_run_id": "run-lit-1"}
    # 裸序号 + 附言（CLI 兼容格式）：[2] REVISE，后面那句就是人的话
    entry = record_decision_answer(state, meta, "2 中位数 AMI 提升 ≥0.02")
    assert entry["accepted_action"] == "revise"
    assert entry["human_note"] == "中位数 AMI 提升 ≥0.02"

    rec = load_intake(state.project_root)
    assert rec is not None, "附言没进 research_intake"
    notes = [a for a in rec.get("amendments", []) if a.get("source") == "decision_note"]
    # 第一条进 intake 的话会成为 original_text（项目此前没有开题原文）——
    # 两种落点都算"进了权威原文"，但来源标注只在 amendments 上有
    assert rec["original_text"] == "中位数 AMI 提升 ≥0.02" or (
        notes and notes[0]["text"] == "中位数 AMI 提升 ≥0.02")
