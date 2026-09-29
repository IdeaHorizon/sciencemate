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
                    artifact_id=a["id"], artifact_type=a["type"], name=a["id"],
                    content="{}", metadata={}, directory=base / "artifacts",
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
        "producing_node": "hypothesis",
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


# ── 2. Move 1d：运行时自己走完两步 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_dispatch_runs_review_and_returns_a_decision_pause(tmp_path, wired):
    """调度器只发起一次 run_node(hypothesis) —— 它下一次醒来该看到决策 pause。"""
    from shared.tools.run_node import _run_node_tool

    state = _state(tmp_path)
    out = await _run_node_tool(state, "hypothesis", node_inputs={"mode": "fresh"},
        user_note="先立研究设计：把问题和闭合条件定下来。")

    assert "_reviewer" in wired, "运行时没有自己派 reviewer —— 手续又回到调度器手上"
    assert out.get("status") == "pause", (
        f"run_node 该返回决策 pause，实际 {out.get('status')!r}"
    )
    assert "decision" in str(out.get("pause_event", {})).lower() or out["pause_event"]


# ── 3. Move 1e：flow 开着时调度器拿不到控制权 ─────────────────────────────

@pytest.mark.asyncio
async def test_the_orchestrator_gets_no_turn_while_the_flow_is_open(tmp_path, wired):
    """一次派发里，调度器唯一的返回点就是决策 pause —— 中间没有它的回合。"""
    from shared.tools.run_node import _run_node_tool

    state = _state(tmp_path)
    out = await _run_node_tool(state, "hypothesis", node_inputs={"mode": "fresh"},
        user_note="先立研究设计：把问题和闭合条件定下来。")

    # 派发序列由运行时决定：先 producing，再 reviewer，然后就 pause 了。
    assert wired == ["hypothesis", "_reviewer"], f"派发序列不对：{wired}"
    assert out["status"] == "pause"


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
