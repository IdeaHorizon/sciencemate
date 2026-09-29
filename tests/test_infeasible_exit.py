"""架构修复 D：infeasible 任务的一等公民出口。

实测根因：experiment 收到无法执行的任务（无 benchmark 环境/数据/标注人力）后
**静默替换设计**——造 synthetic ground truth 冒充真实验，还基于它 refuted 了
hypothesis claim（循环证伪进 KB）。harness rules 写了"不得自行降级需求"但零机械
后果。本修复给"不能执行"一条有牙齿的合法出口：
  1. 声明协议（metadata.infeasible / ## Feasibility 段）
  2. decision_package 机械强制 REDIRECT（不看 reviewer 分数）
  3. update_claim_status 禁止 infeasible run 翻 validated/refuted
不联网。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.state import State
from shared.lib.feasibility import (
    artifact_declares_infeasible,
    find_infeasibility_declaration,
)


def _review_provenance(state):
    return forwarded(
        produced("_reviewer", "review-run"),
        via_node_type=state.node_type,
        via_run_id=state.run_id,
    )

bootstrap()


# ══ 1. 声明协议检测 ══════════════════════════════════════════════════════════

def test_metadata_flag_declares():
    rec = {"content": "x", "metadata": {"infeasible": True}}
    assert artifact_declares_infeasible(rec) is True


def test_feasibility_section_declares():
    rec = {"content": "# Log\n\n## Feasibility\nverdict: infeasible\n缺 benchmark 环境，"
                       "已 probe（nvidia-smi 无 GPU）+ 尝试 pip 安装失败。", "metadata": {}}
    assert artifact_declares_infeasible(rec) is True


def test_chinese_section_declares():
    rec = {"content": "## 可行性\n结论：无法执行——缺真实 trace 数据且无法生成。",
           "metadata": {}}
    assert artifact_declares_infeasible(rec) is True


def test_feasible_run_not_flagged():
    """正常 run（Feasibility 段判 feasible / 或压根没这段）不触发。"""
    assert artifact_declares_infeasible(
        {"content": "## Feasibility\nverdict: feasible —— 环境齐全。\n## Results\n...",
         "metadata": {}}) is False
    assert artifact_declares_infeasible(
        {"content": "# Experiment Log\n## Results\n数据正常", "metadata": {}}) is False


def test_infeasible_word_far_from_header_not_flagged():
    """判定词离 Feasibility 段头太远（>600 字符窗口外）不误触发。"""
    rec = {"content": "## Feasibility\nverdict: feasible。" + "填充" * 400
                       + "\n另外附录里讨论了某方法在别处 infeasible 的情况。",
           "metadata": {}}
    assert artifact_declares_infeasible(rec) is False


def test_find_declaration_returns_redirect_target(tmp_path: Path):
    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_d1")
    state.save_artifact("experiment_log", "blocked", "## Feasibility\ninfeasible：缺数据",
                         metadata={"infeasible": True,
                                   "infeasible_reason": "缺真实 trace 数据",
                                   "redirect_target": "experiment"})
    decl = find_infeasibility_declaration(state)
    assert decl is not None
    assert decl["redirect_target"] == "experiment"
    assert "trace" in decl["reason"]


# ══ 2. decision_package 机械强制 REDIRECT ════════════════════════════════════

@pytest.mark.asyncio
async def test_infeasible_forces_redirect_over_proceed(tmp_path: Path):
    """reviewer 说 proceed 也没用——产出声明 infeasible → 强制 REDIRECT。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_d2")
    log = state.save_artifact(
        "experiment_log", "blocked_run",
        "## Feasibility\nverdict: infeasible —— 无 benchmark 环境，probe+安装尝试均失败",
        metadata={"infeasible": True, "infeasible_reason": "无 benchmark 环境",
                  "redirect_target": "hypothesis"})
    critique = {"verdict": "approve", "confidence": 0.9, "concerns": [], "strengths": [],
                "recommended_action": {"action": "proceed", "feedback_to_next_run": ""}}
    art = state.save_artifact(
        "review_critique",
        "c1",
        json.dumps(critique),
        metadata={"produced_by_node_type": "_reviewer"},
        provenance=_review_provenance(state),
    )

    res = await execute_tool(
        "present_decision_package", state,
        source_node_type="experiment", producing_run_id="r_d2",
        artifact_ids_produced=[log["id"]],
        review_critique_artifact_id=art["id"])
    assert res["status"] == "pause"
    md = res["pause_event"]["metadata"]
    assert md["recommended_action"] == "redirect_upstream"
    assert md["recommended_target_node"] == "hypothesis"
    assert "不可执行" in res["pause_event"]["context"]


@pytest.mark.asyncio
async def test_infeasible_fires_even_when_review_failed(tmp_path: Path):
    """review 崩了（fail-closed 本会推 REVISE）+ infeasible → 仍强制 REDIRECT。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_d3")
    log = state.save_artifact("experiment_log", "blocked2", "…",
                               metadata={"infeasible": True,
                                         "redirect_target": "hypothesis"})
    res = await execute_tool(
        "present_decision_package", state,
        source_node_type="experiment", producing_run_id="r_d3",
        artifact_ids_produced=[log["id"]],
        review_failed_reason="reviewer JSON 崩了")
    md = res["pause_event"]["metadata"]
    assert md["recommended_action"] == "redirect_upstream"
    assert md["recommended_target_node"] == "hypothesis"


@pytest.mark.asyncio
async def test_normal_run_unaffected(tmp_path: Path):
    """无 infeasible 声明 → 走 reviewer 推荐（proceed），不被 D 改写（回归保护）。

    v2.1 P3d 起 experiment 的 post_run_flow 是 `review_curate`：一轮正常出结果的
    实验不再停下来问人，裁决顺延给下一个 Analysis。所以这里断言的是"推荐动作
    仍是 proceed、且它被如实记进 flow entry 交给 Analysis"，而不是"弹出
    pause"。infeasible / critical veto 那两条路径照旧 pause（见上面两个用例）——
    正是它们保证了本用例的对照意义。
    """
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_d4")
    log = state.save_artifact("experiment_log", "ok_run",
                               "## Results\n一切正常\n## Verdict\nvalidated")
    critique = {"verdict": "approve", "confidence": 0.9, "concerns": [], "strengths": [],
                "recommended_action": {"action": "proceed", "feedback_to_next_run": ""}}
    art = state.save_artifact(
        "review_critique",
        "c2",
        json.dumps(critique),
        metadata={"produced_by_node_type": "_reviewer"},
        provenance=_review_provenance(state),
    )
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "experiment", "producing_run_id": "r_d4",
        "review_state": "done", "review_critique_artifact_id": art["id"],
        "curator_state": "done", "decision_state": "pending",
    }]
    res = await execute_tool(
        "present_decision_package", state,
        source_node_type="experiment", producing_run_id="r_d4",
        artifact_ids_produced=[log["id"]],
        review_critique_artifact_id=art["id"])
    assert res["status"] == "success" and res["deferred_to"] == "hypothesis"
    assert res["recommended_action"] == "proceed"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["decision_recommended_action"] == "proceed"


# ══ 3. update_claim_status 禁循环 verdict ════════════════════════════════════

async def _mk_open_claim(state: State, claim_type: str = "hypothesis") -> str:
    # hypothesis claim 只有 _curator 采纳时能写（准入原则）。道具借 curator 身份造，
    # 被测的 infeasible 守卫与创建者无关。
    _orig_nt = state.node_type
    if claim_type == "hypothesis":
        state.node_type = "_curator"
    try:
        return await _mk_open_claim_inner(state, claim_type)
    finally:
        state.node_type = _orig_nt


async def _mk_open_claim_inner(state: State, claim_type: str = "hypothesis") -> str:
    """按 KB schema 造一条合法 open claim（concept + sources 满足校验）。"""
    from core.tool_registry import execute as execute_tool

    concept = await execute_tool("create_concept", state,
                                  canonical_name=f"test-concept-{claim_type}",
                                  concept_type="method",
                                  description="seed concept for infeasible-exit tests")
    kwargs = dict(
        claim_text=f"a {claim_type} claim for infeasible guard test",
        claim_type=claim_type, confidence=0.5,
        concept_ids=[concept["id"]],
        sources=["doi:10/a", "doi:10/b"])
    if claim_type == "hypothesis":
        kwargs["falsification_criteria_text"] = (
            "若真实 benchmark trace 中 D0-D4 占比 < 50% 则本假设被证伪")
        kwargs["predicted_outcome"] = "真实 trace 中 D0-D4 占比将 ≥ 50%"
        # 走完整合法链：prereg artifact → freeze → 注册 chunk → 立 hypothesis
        prereg = state.save_artifact("pre_registration", "test_prereg",
                                      """## Research Questions

### Q1: D0-D4 占比是否超过 50%？
- output_kind: 对一条命题的裁决
- proposition: 真实 trace 中 D0-D4 占比超过 50%
```yaml
- metric: d0_d4_ratio
  comparison: ">"
  threshold: 50
```
""")
        await execute_tool("freeze_artifact", state, artifact_id=prereg["id"],
                            reason="test fixture freeze",
                            run_role="primary", analysis_eligible=True,
                            expected_params={"d0_d4_share_pct": 50})
        # 登记已撤出工具面（采纳即登记）：内部函数是 curator 整合的机械步骤
        from shared.tools.library.kb import _kb_register_artifact_as_chunk
        reg = await _kb_register_artifact_as_chunk(state, artifact_id=prereg["id"])
        assert reg.get("status") == "success", f"register chunk failed: {reg}"
        kwargs["prereg_chunk_id"] = reg["chunk_id"] if "chunk_id" in reg else reg["id"]
        # RFC 2026-08-18：hypothesis claim 的身份锚 =(prereg 身份, 问题 id)
        kwargs["hypothesis_id"] = "Q1"
    res = await execute_tool("create_claim", state, **kwargs)
    assert res.get("status") == "success", f"create_claim failed: {res}"
    return res["id"]


@pytest.mark.asyncio
async def test_infeasible_run_cannot_flip_claim(tmp_path: Path):
    """声明 infeasible 的 producing run 翻 validated/refuted → **如实降落 provisional**
    （判决拆除：与 Analysis 背书 / 预注册兑现同一条裁决资格路径，不拒绝）。
    复现实测事故：synthetic 循环证伪把 hypothesis claim refuted 进 KB —— 现在
    refuted 从不入账，authority_note 写明原因；墙若被加回来这条转红。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_d5")
    claim_id = await _mk_open_claim(state, "hypothesis")
    # run 声明 infeasible
    state.save_artifact("experiment_log", "blocked3", "## Feasibility\ninfeasible：缺真实数据",
                         metadata={"infeasible": True})
    res = await execute_tool("update_claim_status", state,
                              claim_id=claim_id, new_status="refuted",
                              reasoning=(
                                  "synthetic 数据显示 H1 不成立（56.5% 未过阈值），"
                                  "据此判 refuted"
                              ))
    assert res["status"] == "success", res
    assert res["landed_status"] == "provisional"
    assert "不可执行" in res["authority_note"] or "循环" in res["authority_note"]
    assert state.get_kb_record("claims", claim_id)["status"] == "provisional"


@pytest.mark.asyncio
async def test_feasible_run_flip_still_works(tmp_path: Path):
    """无 infeasible 声明的正常 run 翻转照常（带证据链，回归保护）。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_d5b")
    claim_id = await _mk_open_claim(state, "methodological")
    chunk, _ = state.write_kb("chunks", {"text": "evidence", "source": "doi:10/e"})
    res = await execute_tool("update_claim_status", state,
                              claim_id=claim_id, new_status="validated",
                              evidence_ids=[chunk["id"]],
                              reasoning="真实执行的实验数据支持该方法学结论：收敛指标达标，"
                                        "多次重复运行结果一致，对照阈值全部通过")
    assert res["status"] == "success", res


@pytest.mark.asyncio
async def test_curator_flip_unaffected_by_guard(tmp_path: Path):
    """_curator（治理路径）不受 D 守卫影响——dreaming 合法操作照常。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_d6")
    claim_id = await _mk_open_claim(state, "methodological")
    # curator state 里即使有 infeasible artifact（理论上不会有）也不拦
    state.save_artifact("note", "n", "x", metadata={"infeasible": True})
    res = await execute_tool("update_claim_status", state,
                              claim_id=claim_id, new_confidence=0.9,
                              reasoning="dreaming 复审提升置信度，证据充分且多源一致")
    assert res["status"] == "success", res


# ── 声明里的目标是模型写的，可以是任何字符串 ────────────────────────────────

def test_a_declared_target_that_cannot_take_the_handoff_falls_back(tmp_path: Path):
    """`redirect_target` 是**模型写在 artifact metadata 里**的值。

    它可以点名一个服务节点（`post_run_flow: none`）—— 而 REDIRECT 给服务节点的
    义务在账本上永远关不掉、也不被空转熔断看见（2026-09-17 实测空转 40 轮）。
    收敛到既有默认值，**不降级成 REVISE**：那会把「任务不可执行、去上游改设计」
    变成「原样重跑一遍」，正是 infeasible 这条路当初要避免的东西。
    """
    from core.loader import node_owes_post_node_flow

    assert node_owes_post_node_flow("data") is False, "前提变了，本条要重写"
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_fb")
    art = state.save_artifact("experiment_log", "blocked3", "…",
                              metadata={"infeasible": True, "redirect_target": "data"})
    decl = find_infeasibility_declaration(state, [art["id"]])
    assert decl is not None
    assert decl["redirect_target"] == "hypothesis"
    assert node_owes_post_node_flow(decl["redirect_target"]), (
        "回落值自己也得接得住 REDIRECT"
    )


def test_a_declared_target_that_can_take_it_is_kept(tmp_path: Path):
    """对照：合法目标一个字不动。"""
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_keep")
    art = state.save_artifact("experiment_log", "blocked4", "…",
                              metadata={"infeasible": True,
                                        "redirect_target": "observation"})
    decl = find_infeasibility_declaration(state, [art["id"]])
    assert decl["redirect_target"] == "observation"
