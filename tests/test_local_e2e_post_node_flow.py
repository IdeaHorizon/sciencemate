"""本地端到端：一个 producing 节点交付之后，到人点完 PROCEED 为止的**真实控制流**。

## 为什么要有这个

2026-08-19 这一整天改的是控制流：谁执行流程、谁持有控制权、pause 什么时候不
resume。而全部验证都是单测 —— 我在同一天里**两次判错"没有可达窗口"、三次删过头**，
每次都是别的测试红了才发现。单测只验我想到的失败方式。

原始事故（session 976e70e1）：人点 PROCEED，卡片重现，点三次。那条链跨了
run_node → flow 登记 → 提醒/派发 → 决策呈递 → 答复解析 → flow 闭合，六段。
任何一段单测绿，整条仍可能断 —— 事实上它当初就是在**段与段之间**断的。

## 桩在哪、真在哪

只桩掉「子 run 内部怎么跑」（`_execute_with_infra_retry` 返回一份与真实同形的
summary）。其余全是真代码：flow entry 怎么登记、运行时怎么派 reviewer、决策包
怎么构造、答复怎么对着 Offer 解析、flow 怎么闭合。

桩不像就等于没验，所以 summary 的键按 `_finish_child` 实际读的那一份给全。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.state import State


def _summary(run_id: str, node_type: str, state_dir: Path, artifacts: list[dict]) -> dict:
    """与真实子 run 同形的 summary（键取自 _finish_child 实际读的集合）。"""
    return {
        "run_id": run_id,
        "node_type": node_type,
        "status": "completed",
        "turns": 3,
        "state_dir": str(state_dir),
        "artifacts": artifacts,
        "final_text_preview": f"{node_type} 交付完成",
        "started_at": run_id,
        "missing_required_outputs": [],
        "missing_input_artifact_types": [],
        "unverified_artifacts": [],
        "memory_candidates": [],
        "blockers": [],
        "project_workspace": {"paths": [a["id"] for a in artifacts]},
    }


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把子 run 的执行桩掉，其余走真代码。返回一个记录器。"""
    from shared.tools import run_node as rn

    calls: list[str] = []

    async def fake_exec(state, node_type, exec_kwargs):
        calls.append(node_type)
        rid = f"r_{node_type.strip('_')}"
        d = tmp_path / rid
        d.mkdir(parents=True, exist_ok=True)
        arts = ([{"id": "review_critique__auto", "type": "review_critique"}]
                if node_type == "_reviewer"
                else [{"id": "pre_registration__t1", "type": "pre_registration"}])
        # 产物要**真落盘**（子 run 的账本 + 父 state 的 run 本地账本）—— 真实路径上
        # 这是子 run 自己写 + `_finish_child` 的 import 做的两件事。不落，
        # forward_artifact_ids 解析不到，运行时派 reviewer 当场失败。第一版就是
        # 这么假绿的：**桩不像，跑到的就不是真实路径**。
        # 记录 = 原生文件 + records.jsonl 一行；读方（import / forward）只认账本。
        from core.artifact_provenance import produced
        from core.ledger import RecordStore

        for base in (d, Path(state.root)):
            store = RecordStore(base / "artifacts", base / "records.jsonl")
            for a in arts:
                store.save(
                    artifact_id=a["id"], artifact_type=a["type"], name=a["id"].split("__", 1)[-1],
                    content=(json.dumps({
                        "verdict": "approve", "summary": "定义明确，设计可以执行。",
                        "recommended_action": {"action": "proceed"},
                    }, ensure_ascii=False) if node_type == "_reviewer" else "{}"),
                    metadata={}, directory=base / "artifacts",
                    created_at="2026-09-12T00:00:00+00:00",
                    provenance=produced(node_type, rid),
                    produced_by_node_type=node_type, produced_by_run_id=rid,
                    by_node=node_type, by_run=rid,
                )
        return _summary(rid, node_type, d, arts)

    monkeypatch.setattr(rn, "_execute_with_infra_retry", fake_exec)
    return calls


def _state(tmp_path) -> State:
    """真实调度器 state：带上它自己的 callable_nodes（`_orchestrator` 是 `["*"]`）。

    不带就会被 callable_nodes 白名单拦下 —— 那也是真实门禁，只是本测试要验的是
    它之后的东西。这一句就是「测试不绑 Project，边界层就不在场」的具体形态：
    state 造得不像，跑到的就不是真实路径。
    """
    from core.bootstrap import bootstrap
    from core.loader import load_harness

    bootstrap()
    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="p_local")
    st.project_root = tmp_path / "proj"
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    st.hook_state["_callable_nodes"] = list(load_harness("_orchestrator").callable_nodes or [])
    return st


# ── 1. 原始事故：点 PROCEED，卡片会不会重现 ────────────────────────────────

@pytest.mark.asyncio
async def test_answering_proceed_closes_the_flow_instead_of_re_presenting(tmp_path, wired):
    """这是 2026-08-19 那个 session 的原始症状：点了 PROCEED，卡片又回来。

    当时的链条：curator 卡在不可满足的条件 → 菜单撤下 PROCEED → UI 拿到的却是
    另一套选项集 → 人点的文案撞不上合法集 → 静默丢弃 → 原地重呈递。
    """
    from shared.tools.library.decision_package import record_decision_answer

    state = _state(tmp_path)
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "analysis",
        "producing_run_id": "r_hyp",
        "artifact_ids": ["pre_registration__t1"],
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__auto",
        "decision_state": "awaiting_human",
        "decision_options": ["proceed", "revise", "redirect_upstream", "abort", "edit"],
    }]

    entry = record_decision_answer(
        state, {"metadata": {"type": "decision_package", "producing_run_id": "r_hyp"}}, "1")

    assert not entry.get("decision_rejection"), (
        "答复被拒 = 原始事故复现：授权静默消失，卡片会再来一次"
    )
    assert entry["accepted_action"] == "proceed"
    # flow 出列 = 不会再呈递同一张卡
    still_open = [e for e in state.hook_state["pending_post_node_flow"]
                  if e.get("producing_run_id") == "r_hyp"]
    assert not still_open, "PROCEED 之后 flow 仍开着 —— 下一轮会重新呈递"


# ── 2. 自动审查后把结果交回调度器 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_dispatch_returns_review_before_presenting_a_decision(tmp_path, wired):
    from shared.tools.run_node import _run_node_tool

    state = _state(tmp_path)
    out = await _run_node_tool(state, "analysis", node_inputs={
        "mode": "fresh", "research_question": "确定本次研究问题与闭合条件"},
        user_note="先立研究设计：把问题和闭合条件定下来。")

    assert "_reviewer" in wired, "运行时没有自己派 reviewer —— 手续又回到调度器手上"
    assert out["status"] == "success"
    assert "pause_event" not in out
    assert out["post_node_review"]["review_critique_artifact_id"] == "review_critique__auto"
    assert "定义明确" in out["post_node_review"]["critique"]["content"]
    assert out["final_text_preview"] == "analysis 交付完成"


# ── 3. 弹框仍由调度器显式调用 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_orchestrator_can_explain_then_explicitly_present(tmp_path, wired):
    from shared.tools.run_node import _run_node_tool
    from shared.tools.library.decision_package import _present_decision_package

    state = _state(tmp_path)
    out = await _run_node_tool(state, "analysis", node_inputs={
        "mode": "fresh", "research_question": "确定本次研究问题与闭合条件"},
        user_note="先立研究设计：把问题和闭合条件定下来。")

    assert wired == ["analysis", "_reviewer"], f"派发序列不对：{wired}"
    assert out["status"] == "success"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["decision_state"] == "pending"
    presented = await _present_decision_package(
        state, source_node_type="analysis", producing_run_id=out["child_run_id"],
        human_decision_reason="测试中的研究取舍由用户确认。",
    )
    assert presented["status"] == "pause"
    assert entry["decision_state"] == "awaiting_human"
    assert "analysis 交付完成" in presented["pause_event"]["context"]
    assert "pre_registration__t1" in presented["pause_event"]["context"]


@pytest.mark.asyncio
async def test_review_reaches_the_llm_and_its_explanation_precedes_the_pause(tmp_path, wired):
    from unittest.mock import AsyncMock, MagicMock

    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.llm import LLMMessage, LLMResponse
    from core.pause import clear_all

    def reply(content, name, arguments, call_id):
        return LLMResponse(
            content=content, finish_reason="tool_calls", usage={"total_tokens": 10},
            tool_calls=[{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}],
        )

    explanation = "研究计划已经完成。独立审查认为定义明确、设计可执行，我建议接受并继续，下面请你选择。"
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        reply("开始研究设计。", "run_node", {"node_type": "analysis",
              "node_inputs": {"research_question": "形成可检验的研究计划"}}, "produce"),
        reply(explanation, "present_decision_package", {
            "source_node_type": "analysis", "producing_run_id": "r_hyp",
            "human_decision_reason": "需要用户选择下一阶段的研究方向。",
        }, "decide"),
    ])
    harness = NodeHarness(
        node_type="_orchestrator", system_prompt="test", kb_query="_disable",
        tools=["run_node", "present_decision_package"], max_turns=3,
    )
    state = _state(tmp_path)
    clear_all()
    try:
        result = await run_loop(harness, state, [LLMMessage(role="user", content="形成研究计划。")], llm)
        assert result.status == "paused"
        assert llm.chat.call_count == 2
        review_inputs = llm.chat.call_args_list[1].args[0]
        assert any(message.role == "tool" and "定义明确" in (message.content or "") for message in review_inputs)
        events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
        said_at = next(i for i, event in enumerate(events) if event.get("content") == explanation)
        card_at = next(i for i, event in enumerate(events) if event.get("event") == "decision_package_presented")
        assert said_at < card_at
        assert any(message.role == "assistant" and message.content == explanation for message in result.messages)
    finally:
        clear_all()


# ── 4. 写入门：不合规的产物写不出来 ───────────────────────────────────────

def _observation_state(tmp_path) -> State:
    """写 observation_log 的必须是 **observation** state。

    此前这两条用的是 `_state()`（`_orchestrator`），而架构节点写 producing 节点
    专属类型本来就会被产权规则拒 —— 当时看不出来，是因为已删的
    `required_metadata` 排在产权规则**之前**，先一步用另一个理由拒了。
    判据搬层之后顺序变了，这条测试才露出它一直在测错东西：
    **state 造得不像，跑到的就不是真实路径。**
    """
    from core.bootstrap import bootstrap

    bootstrap()          # 门在节点 tools import 时注册 —— 真运行时就是这么起来的
    st = State.new(node_type="observation", base_dir=tmp_path / "runs", project_id="p_local")
    st.project_root = tmp_path / "proj"
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    return st


@pytest.mark.asyncio
async def test_an_incomplete_record_is_written_with_honest_advisories(tmp_path):
    """判据钉在**接线**上（save_artifact 真的跑了类型的写入门），不钉字段名。

    判决拆除批 3w（obs 212/252 降格，D-OB1：save gate 一律不再拒存盘）：
    取样纪律痕迹/findings 缺席照存盘，未过项由这道门写进产物
    metadata.advisories —— 接线的证据从「拒绝发生了」换成「advisory 落账了」。
    仍拦死的是 B：anti-HARKing（exploratory 勾账）在下一条钉。
    """
    from shared.tools.builtin import _save_artifact

    st = _observation_state(tmp_path)
    out = await _save_artifact(
        st, "observation_log", "x",
        content="c", metadata={"mode": "exploratory"})

    assert out["status"] == "success", out.get("error")
    record = st.read_artifact("observation_log__x")
    advisories = record["metadata"].get("advisories") or {}
    assert "sampling_discipline" in advisories
    assert "no_findings" in advisories


@pytest.mark.asyncio
async def test_a_harking_record_still_cannot_be_written(tmp_path):
    """B 保留：exploratory 勾账（closure_discharges）仍在必经之路上被拒
    （判决拆除批 3w：obs 224/233 升 B —— 放行即账假）。"""
    from shared.tools.builtin import _save_artifact

    out = await _save_artifact(
        _observation_state(tmp_path), "observation_log", "x",
        content="c",
        metadata={"mode": "exploratory",
                  "closure_discharges": {"Q1#1": {"status": "discharged"}}})

    assert out["status"] == "error"
    assert "exploratory_cannot_close" in out["failed_checks"]


@pytest.mark.asyncio
async def test_removing_the_gate_lets_the_harking_record_through(tmp_path):
    """变异检验：把门摘掉，上一条必须转绿 —— 不转就说明挡住它的不是这道门。"""
    from shared.tools.builtin import _save_artifact
    from shared.tools.library import artifacts_extra as ax

    state = _observation_state(tmp_path)
    gate = ax.SAVE_GATES.pop("observation_log")
    try:
        out = await _save_artifact(
            state, "observation_log", "x", content="c",
            metadata={"mode": "exploratory",
                      "closure_discharges": {"Q1#1": {"status": "discharged"}}})
        assert out["status"] == "success", out.get("error")
    finally:
        ax.SAVE_GATES["observation_log"] = gate
