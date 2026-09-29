"""v3.4：E2E#2（验证 run，2026-07-24~27）暴露问题的修复批回归。

覆盖：summarizer 真校准 / rejected proposal 拦截 / memory 复发聚合成 defect /
KB literature_reported 豁免 / prereg 可行性门禁 / 调度器乒乓熔断。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.artifact_provenance import forwarded, produced
from core.bootstrap import bootstrap
from core.ledger import RecordStore
from core.state import State

bootstrap()


def _review_provenance(state):
    return forwarded(
        produced("_reviewer", "review-run"),
        via_node_type=state.node_type,
        via_run_id=state.run_id,
    )


# ── 1. summarizer 真校准 ─────────────────────────────────────────────────────

def test_observed_ratio_only_increases_and_caps():
    import core.summarizer as sm

    class _S:
        hook_state: dict = {}
    s = _S()
    s.hook_state = {}
    sm.note_observed_prompt_tokens(s, local_estimate=100_000, server_prompt_tokens=185_000)
    assert abs(sm.effective_calibration(s) - 1.85) < 0.01, "观测比 1.85 应生效"
    # 更低的观测不回退（context 安全要上界）
    sm.note_observed_prompt_tokens(s, local_estimate=100_000, server_prompt_tokens=120_000)
    assert abs(sm.effective_calibration(s) - 1.85) < 0.01
    # 异常值封顶
    sm.note_observed_prompt_tokens(s, local_estimate=100_000, server_prompt_tokens=999_000)
    assert sm.effective_calibration(s) <= sm._CONTEXT_CALIBRATION_CAP + 1e-9


def test_tool_schema_tokens_counted_in_should_compress():
    import core.summarizer as sm
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMMessage

    msgs = [LLMMessage(role="user", content="x" * 4000)]   # est ≈ 1000
    est = sm.estimate_tokens(msgs)
    harness = NodeHarness(
        node_type="t", max_context_tokens=int(est * 2.8),   # threshold=1.4×est
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5,
                                     strategy="truncate"),
    )

    class _S:
        hook_state: dict = {}
    s = _S()
    s.hook_state = {}
    trig_no, _ = sm.should_compress(harness, msgs, turn=2, state=s)
    assert not trig_no, "无 tool schema 时 eff=1.3×est < 1.4×est 不应触发"
    s.hook_state["_tool_schema_tokens"] = est   # 工具 schema 与 messages 等量
    trig_yes, _ = sm.should_compress(harness, msgs, turn=2, state=s)
    assert trig_yes, "计入 tool schema 后 eff=1.3×2est > 1.4×est 应触发"


# ── 2. rejected proposal 拦截 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejected_proposal_blocks_reproposal(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
    from shared.tools.library.proposals import _propose, _resolve_proposal
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_rej")
    # 造一条真实 claim 当 target
    rec = {"claim_text": "some claim to merge", "claim_type": "empirical",
           "concept_ids": ["c1"], "confidence": 0.6, "sources": [],
           "orphan_reason": "test", "scope": "project"}
    claim, _ = state.write_kb("claims", rec)
    r1 = await _propose(state, proposal_type="kb_merge_candidate",
                        target_entity="claims", target_id=claim["id"],
                        proposed_action="merge", reasoning="looks duplicated enough")
    assert r1["status"] == "success"
    # pending 去重（同 type+target 再提 → dedup 不新增）
    r_dup = await _propose(state, proposal_type="kb_merge_candidate",
                           target_entity="claims", target_id=claim["id"],
                           proposed_action="merge again", reasoning="same thing again")
    assert r_dup.get("deduplicated") is True
    # 拒绝后重提 → 照提，但被拒历史挂在提案上让人裁（判决拆除 O3：thrash 只记不拦；
    # 「new_evidence 非空才许重提」任意非空即过，是仪式闸）。墙若加回来这条转红。
    rr = await _resolve_proposal(state, proposal_id=r1["proposal_id"],
                                 decision="rejected", reasoning="not a dup at all")
    assert rr["status"] == "success"
    r2 = await _propose(state, proposal_type="kb_merge_candidate",
                        target_entity="claims", target_id=claim["id"],
                        proposed_action="merge", reasoning="try again same words")
    assert r2["status"] == "success", r2
    assert r2["prior_rejected_count"] == 1
    from shared.tools.library.proposals import _find_proposal
    stored, _, _ = _find_proposal(state, r2["proposal_id"])
    assert stored["extra"]["prior_rejected_count"] == 1
    assert stored["extra"]["last_rejected_at"]


# ── 3. memory 复发聚合 → defect ──────────────────────────────────────────────


def _mem_state(tmp_path, wt, pid="p_def"):
    from core import memory as M
    from core.state import State

    st = State.new(node_type="literature", base_dir=tmp_path / "runs",
                   project_id=pid, project_worktree=wt)
    M.ensure_skeleton(st)
    return st


def test_near_duplicate_aggregates_and_escalates_to_defect(tmp_path, mem_worktree):
    """复发不是知识，是待修的系统性缺陷 —— 到阈值要停止记录、推动修根因。"""
    from core import memory as M

    st = _mem_state(tmp_path, mem_worktree)
    base = "survey methodology notes 中 author wiring 计数不准确 必须 cross-check transcript"
    last = None
    for i in range(M.DEFECT_RECURRENCE + 1):
        # 每次换措辞（词序/前后缀微调），模拟 reviewer 复述
        text = base if i == 0 else base + f" 第{i}次复发 记录一下"
        last = M.append_manual(st, text=text, section=M.SECTION_PITFALL,
                               nodes=["literature"], run_id=f"r{i}")
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 1
    assert last["created"] is False
    assert last["defect"] is True
    assert "修根因" in last["note"]


def test_unrelated_entries_not_aggregated(tmp_path, mem_worktree):
    """另一半：只测"会合并"会让"把什么都合并"也通过。"""
    from core import memory as M

    st = _mem_state(tmp_path, mem_worktree, "p_unrelated")
    M.append_manual(st, text="biber 缺失时 compile_latex 会误报需要 biber",
                    section=M.SECTION_PITFALL, nodes=["writing"], run_id="r1")
    M.append_manual(st, text="punkt_tab 未安装导致 WebArena evaluator 全部崩溃",
                    section=M.SECTION_PITFALL, nodes=["data"], run_id="r2")
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 2


def test_defects_surface_first_in_the_onboarding_slice(tmp_path, mem_worktree):
    """defect 是系统性缺陷，最该被看见 —— 送达时必须排在前面并带标记。"""
    from core import memory as M
    from core.memory_delivery import onboarding_slice

    st = _mem_state(tmp_path, mem_worktree, "p_defect_slice")
    M.append_manual(st, text="一条普通教训：先看局面再动手",
                    section=M.SECTION_PITFALL, nodes=["literature"], run_id="r0")
    base = "同一个 QC 缺口反复出现 author wiring 覆盖不足"
    for i in range(M.DEFECT_RECURRENCE):
        M.append_manual(st, text=base if i == 0 else base + f" 变体{i}",
                        section=M.SECTION_PITFALL, nodes=["literature"],
                        run_id=f"r{i}")

    md = onboarding_slice(st, "literature", [])
    assert md is not None
    assert "⚠️" in md
    assert md.index("author wiring") < md.index("先看局面再动手")


# ── 4. prereg 可行性门禁 ─────────────────────────────────────────────────────

def _prereg_record(content: str, commitment: dict | None = None) -> dict:
    md = {}
    if commitment is not None:
        md["execution_commitment"] = commitment
    return {"type": "pre_registration", "name": "t", "content": content,
            "metadata": md}


def test_prereg_gate_flags_unavailable_model_and_annotators(monkeypatch):
    monkeypatch.delenv("HARNESS_HUMAN_ANNOTATION_CHANNEL", raising=False)
    from shared.tools.library.artifacts_extra import _prereg_feasibility_violations
    rec = _prereg_record(
        "Primary Model: GPT-4o. Routability re-run with gpt-4o-mini. "
        "Three independent annotators are blind to hypotheses.")
    v = _prereg_feasibility_violations(rec)
    assert any("gpt-4o" in x for x in v)
    assert any("人工标注" in x for x in v)


def test_prereg_gate_passes_with_commitment(monkeypatch):
    monkeypatch.delenv("HARNESS_HUMAN_ANNOTATION_CHANNEL", raising=False)
    from shared.tools.library.artifacts_extra import _prereg_feasibility_violations
    rec = _prereg_record(
        "Primary Model: GPT-4o. Annotators are blind.",
        commitment={"substitutes": {"gpt-4o": "deepseek-v4-pro（平台唯一后端）",
                                      "gpt-4o-mini": "deepseek-v4-flash"},
                    "deferred": ["human annotation（Phase 2）"]})
    assert _prereg_feasibility_violations(rec) == []


def test_prereg_gate_ignores_clean_prereg():
    from shared.tools.library.artifacts_extra import _prereg_feasibility_violations
    rec = _prereg_record("We run the platform primary model on τ-bench, N=100 tasks.")
    assert _prereg_feasibility_violations(rec) == []


# ── 5. 调度器乒乓熔断 ────────────────────────────────────────────────────────

def _write_run(base: Path, name: str, node_type: str, project_id=None):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(
        {"node_type": node_type, "project_id": project_id, "status": "completed"}),
        encoding="utf-8")


def test_system_node_streak_counts_and_breaks(tmp_path):
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    for i in range(10):
        _write_run(tmp_path, f"17000000{i:02d}-aaaaaa", "_curator" if i % 2 else "_reviewer")
    assert chat_mod._system_node_streak(state) == 10
    # 插一个 producing run（时间上最新）→ streak 归零
    _write_run(tmp_path, "1800000000-bbbbbb", "experiment")
    assert chat_mod._system_node_streak(state) == 0


def test_pingpong_aborts_continuous(tmp_path, monkeypatch):
    import chat as chat_mod
    monkeypatch.setattr(chat_mod, "_SYSTEM_NODE_STREAK_ABORT", 20)
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    for i in range(22):
        _write_run(tmp_path, f"17000001{i:02d}-cccccc", "_curator" if i % 2 else "_reviewer")
    prompt, _ = chat_mod._continuous_followup(state, "继续整理 KB", reason="turn_finished")
    assert prompt is None
    assert state.hook_state["continuous_phase"] == "aborted"
    assert "乒乓" in state.hook_state.get("continuous_abort_reason", "") or \
           "producing" in state.hook_state.get("continuous_abort_reason", "")


def test_pingpong_warn_injected_below_abort(tmp_path, monkeypatch):
    import chat as chat_mod
    monkeypatch.setattr(chat_mod, "_SYSTEM_NODE_STREAK_WARN", 8)
    monkeypatch.setattr(chat_mod, "_SYSTEM_NODE_STREAK_ABORT", 20)
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    for i in range(10):
        _write_run(tmp_path, f"17000002{i:02d}-dddddd", "_reviewer" if i % 2 else "_curator")
    prompt, _ = chat_mod._continuous_followup(state, "继续", reason="turn_finished")
    assert prompt is not None
    assert "乒乓警告" in prompt
    assert "禁止" in prompt


# 2026-08-21 删掉 `test_near_duplicate_works_for_chinese_text`。
#
# 它测的是 `MemoryV2.add_candidate` 的中文近似去重 —— 候选队列整体退场
# （判据见 tests/test_memory_rebuild_tombstones.py）。
#
# 继任者更强，不是更弱：
#   tests/test_memory_core.py::test_chinese_near_dup_merges_regardless_of_spacing
#     —— 同一条回归（按空格分段取 bigram 会让跨空格的字对凭空消失）
#   tests/test_memory_core.py::test_genuinely_different_lessons_are_not_merged
#     —— 配了反向用例。只测"会合并"的话，"把什么都合并"也能通过。
#        被删的这条没有这一半。

# ── v3.5：producing 节点重复失败熔断（r3 实测 writing 5×50 turns）────────────

def _write_fail_run(base, name, node_type, failed, project_id=None, status="incomplete"):
    # QC 层删除（2026-08-22）后，失败信号只剩契约层：缺失的必需产出。
    # 熔断器语义不变 —— 变的只是它数什么。测试料同步换成 missing:* 信号。
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": status,
        "missing_required_outputs": failed,
    }), encoding="utf-8")


def test_repeated_producing_failure_detected(tmp_path):
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    common = ["manuscript", "figure"]
    for i in range(4):
        _write_fail_run(tmp_path, f"1790000{i:03d}-aaaaaa", "writing",
                        common + [f"extra_{i}"])
    r = chat_mod._repeated_producing_failure(state)
    assert r is not None
    assert r["node_type"] == "writing" and r["count"] == 4
    assert set(r["common_checks"]) == {f"missing:{c}" for c in common}, \
        "只应保留每次都缺的公共产出"


def test_producing_success_breaks_streak(tmp_path):
    """成功过 = 正常迭代，不是卡死。"""
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    _write_fail_run(tmp_path, "1790001000-aaaaaa", "writing", ["x"])
    _write_fail_run(tmp_path, "1790001001-aaaaaa", "writing", [], status="completed")
    _write_fail_run(tmp_path, "1790001002-aaaaaa", "writing", ["x"])
    assert chat_mod._repeated_producing_failure(state) is None


def test_repeated_producing_failure_aborts_and_warns(tmp_path, monkeypatch):
    import chat as chat_mod
    monkeypatch.setattr(chat_mod, "_PRODUCING_FAIL_STREAK_WARN", 3)
    monkeypatch.setattr(chat_mod, "_PRODUCING_FAIL_STREAK_ABORT", 5)
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    for i in range(3):
        _write_fail_run(tmp_path, f"1790002{i:03d}-bbbbbb", "writing", ["chk_a"])
    prompt, _ = chat_mod._continuous_followup(state, "继续写", reason="turn_finished")
    assert prompt is not None
    # v3.10：措辞由 core/obligations.render 统一产出；断言**语义**不锁字符串
    assert "[repeated_failure]" in prompt
    assert "退回上游" in prompt and "降级产物" in prompt
    assert "禁止" in prompt
    for i in range(3, 6):
        _write_fail_run(tmp_path, f"1790002{i:03d}-bbbbbb", "writing", ["chk_a"])
    prompt2, _ = chat_mod._continuous_followup(state, "继续写", reason="turn_finished")
    assert prompt2 is None
    assert state.hook_state["continuous_phase"] == "aborted"
    assert "上游" in state.hook_state.get("continuous_abort_reason", "")


def test_inflight_child_counts_as_progress(tmp_path):
    """v3.5 回归（E2E-3 实测误杀）：run_node 后台执行时，子节点 transcript 在长
    但 summary.json 要等结束才写 —— 若指纹看不见它，正在算 CI/证伪假设的
    experiment 会被判 livelock 停机。"""
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    fp0 = chat_mod._continuous_progress_fingerprint(state)
    child = tmp_path / "1790900000-child1"
    child.mkdir(parents=True, exist_ok=True)
    t = child / "transcript.jsonl"
    t.write_text('{"event":"turn_start","turn":1}\n', encoding="utf-8")
    fp1 = chat_mod._continuous_progress_fingerprint(state)
    assert fp1 != fp0, "出现进行中子 run 应视为进展"
    # 子节点继续写 → 指纹继续变
    with t.open("a", encoding="utf-8") as f:
        f.write('{"event":"llm_response","turn":22}\n')
    fp2 = chat_mod._continuous_progress_fingerprint(state)
    assert fp2 != fp1, "子节点 transcript 增长应视为进展"
    # 子节点结束（写 summary）后不再计入 inflight，但 summaries 变化仍是进展
    (child / "summary.json").write_text('{"node_type":"experiment","status":"completed"}',
                                         encoding="utf-8")
    fp3 = chat_mod._continuous_progress_fingerprint(state)
    assert fp3 != fp2


def test_repeated_failure_uses_frequency_not_intersection(tmp_path):
    """v3.5.1 回归（E2E-3 实测漏报）：writing 连续 3 次挂 no_unverified_details
    （占位引用造假，拦得对），但最新一次挂 missing:manuscript、中间夹了一次无
    signal 的 blocked —— 用"全程交集"会立刻变空集导致漏报，改用频次判定。"""
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    # 复刻真实序列（目录名倒序=时间倒序，故最新的排最后写但名字最大）
    _write_fail_run(tmp_path, "1790800001-aaaaaa", "writing",
                    ["manuscript_no_unverified_details"])
    _write_fail_run(tmp_path, "1790800002-aaaaaa", "writing",
                    ["manuscript_no_unverified_details"])
    _write_fail_run(tmp_path, "1790800003-aaaaaa", "writing",
                    ["manuscript_no_unverified_details"])
    _write_fail_run(tmp_path, "1790800004-aaaaaa", "writing", [],
                    status="blocked")  # 中途停靠，无 signal → 跳过不断链
    d = tmp_path / "1790800005-aaaaaa"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "writing",
        "project_id": None,
        "status": "incomplete",
        "missing_required_outputs": ["manuscript"],
    }), encoding="utf-8")

    r = chat_mod._repeated_producing_failure(state)
    assert r is not None, "频次判定应识别出重复失败（旧的交集逻辑会漏报）"
    assert r["node_type"] == "writing"
    assert r["count"] == 3, f"应识别出重复 3 次的那个检查，实得 {r}"
    assert r["common_checks"] == ["missing:manuscript_no_unverified_details"]


# ── v3.6 上游返工路由（③① 接线 + ② 申诉权）──────────────────────────────────

def test_upstream_candidates_from_dependency_graph():
    """候选集 = 依赖图 ∩ **跑完能把 flow 关掉的节点**。

    literature 后来改成了服务（`post_run_flow: none`），于是 hypothesis 没有任何
    能接单的上游 —— 退回一个服务节点，那条审查义务永远关不掉，而空转熔断在这一档
    看不见（2026-09-17 实测 40 轮）。"没有合法上游"是**正确答案**，不是缺陷；
    reviewer 那边会如实报"无上游，考虑 revise/abort"，绝不静默降级。
    """
    from core.loader import node_owes_post_node_flow
    from core.upstream_routing import infer_redirect_target, upstream_candidates

    assert node_owes_post_node_flow("literature") is False, "前提变了，本条要重写"
    assert upstream_candidates("hypothesis") == []
    assert infer_redirect_target("hypothesis") is None
    # writing 仍有多个上游 → 有歧义就不猜（宁可要一次澄清）
    assert len(upstream_candidates("writing")) > 1
    assert infer_redirect_target("writing") is None


@pytest.mark.asyncio
async def test_redirect_ambiguous_becomes_retry_reviewer_not_revise(tmp_path):
    """歧义时转 retry_reviewer 要求指名 —— 关键是**绝不退化成 revise**。"""
    from core.tool_registry import execute as execute_tool
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_amb")
    critique = {
        "verdict": "major_concerns", "concerns": [], "strengths": [],
        "recommended_action": {"action": "redirect_upstream",
                                "feedback_to_next_run": "root cause upstream"},
    }
    art = state.save_artifact(
        "review_critique",
        "amb",
        json.dumps(critique),
        metadata={"produced_by_node_type": "_reviewer"},
        provenance=_review_provenance(state),
    )
    res = await execute_tool(
        "present_decision_package", state,
        source_node_type="writing",            # 多个上游 → 歧义
        producing_run_id="r_amb",
        review_critique_artifact_id=art["id"],
    )
    md = res["pause_event"]["metadata"]
    assert md["recommended_action"] != "revise", "歧义不得退化成重跑当前节点"
    assert md["recommended_action"] == "redirect_upstream", "应保留上游诊断"
    assert md.get("recommended_target_node") in (None, "")
    assert md["review_failed"] is True, "缺 target 要被标出来等澄清"


@pytest.mark.asyncio
async def test_request_upstream_rework_records_and_validates(tmp_path):
    from core.tool_registry import execute as execute_tool
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p_appeal")
    # 判决拆除：字数闸删——缺验收标准（空）仍拒，短验收放行原样记录
    bad = await execute_tool(
        "request_upstream_rework", state, upstream_node="literature",
        missing="被引论文没进 KB，共 12 篇", acceptance="  ")
    assert bad["status"] == "error" and "acceptance" in bad["error"]
    # 不能申诉自己
    self_ = await execute_tool(
        "request_upstream_rework", state, upstream_node="writing",
        missing="被引论文没进 KB，共 12 篇",
        acceptance="12 篇均有 chunk 且 author_concept_ids 非空")
    assert self_["status"] == "error"
    # 正常登记
    ok = await execute_tool(
        "request_upstream_rework", state, upstream_node="literature",
        missing="manuscript 引用的 12 篇论文未入 KB，无法生成可追溯 bibliography",
        acceptance="12 篇均有 chunk_id 且 author_concept_ids 非空")
    assert ok["status"] == "success"
    assert state.hook_state["upstream_rework_requests"][0]["upstream_node"] == "literature"


def test_pending_appeals_surface_to_orchestrator(tmp_path):
    """申诉必须能被调度器看见 —— 否则又是"节点知道、没人听见"。"""
    import chat as chat_mod
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    state.hook_state.update(continuous_loop=True, continuous_phase="running")
    d = tmp_path / "1791000000-appeal"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "writing",
        "project_id": None,
        "status": "incomplete",
        "missing_required_outputs": [],
        "upstream_rework_requests": [{
            "requested_by_node": "writing", "upstream_node": "literature",
            "missing": "12 篇被引论文未入 KB", "acceptance": "均有 chunk 且 author 已接线",
        }],
    }), encoding="utf-8")
    assert len(chat_mod._pending_upstream_requests(state)) == 1
    prompt, _ = chat_mod._continuous_followup(state, "继续", reason="turn_finished")
    assert prompt is not None
    assert "未了结的义务" in prompt
    assert "[appeal]" in prompt
    assert "不要忽略这些账去重跑申诉方" in prompt
    assert "literature" in prompt


def test_all_producing_nodes_have_appeal_right():
    """申诉权是基本权利，不该由节点自己选择是否拥有（照 _ALWAYS_ON_HOOKS 先例）。"""
    from core.loader import load_harness
    for n in ("literature", "hypothesis", "data", "experiment", "postprocess", "writing"):
        assert "request_upstream_rework" in load_harness(n).tools, n
    # 系统节点本就有 redirect/决策通道，不注入
    assert "request_upstream_rework" not in load_harness("_curator").tools


# ── v3.7 派发时拦截（轮内循环：轮间熔断够不着的主路径）──────────────────────

def _fail_run(base, name, node_type, missing=None,
              status="incomplete", project_id=None, extra=None):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": status,
        "missing_required_outputs": missing or [],
        **(extra or {}),
    }), encoding="utf-8")


def test_dispatch_detector_counts_and_breaks_on_success(tmp_path):
    from shared.tools.run_node import _repeated_failure_for
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id=None)
    for i in range(3):
        _fail_run(tmp_path, f"1792000{i:03d}-w", "writing",
                  missing=["manuscript"], project_id=None)
    r = _repeated_failure_for(state, "writing")
    assert r and r["count"] == 3 and r["signals"] == ["missing:manuscript"]
    # 该节点成功过 → 断链（正常迭代不算卡死）
    _fail_run(tmp_path, "1792000900-w", "writing", status="completed", project_id=None)
    assert _repeated_failure_for(state, "writing") is None
    # 系统节点不由本机制管
    assert _repeated_failure_for(state, "_curator") is None


@pytest.mark.asyncio
async def test_run_node_blocks_after_repeated_failures(tmp_path):
    """轮内连起同一个失败节点必须被拒 —— 实测 orchestrator 在**一个 turn 内**
    连起 4 个 writing，轮间熔断根本够不着。"""
    from core.tool_registry import execute as execute_tool
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id=None)
    for i in range(4):
        _fail_run(tmp_path, f"1792100{i:03d}-w", "writing",
                  missing=["manuscript_no_unverified_details"], project_id=None)
    res = await execute_tool("run_node", state, node_type="writing", user_note="测试派发",
                              node_inputs={"research_question": "再写一次"})
    assert res["status"] == "error"
    assert "拒绝再次启动" in res["error"]
    assert res["repeated_failure"]["count"] == 4
    assert res["upstream_candidates"], "必须给出可退回的上游候选"


def test_upstream_success_clears_dispatch_block(tmp_path):
    """v3.7.1 死锁回归：上游补齐后必须能重试被拦的节点。

    E2E-3 现场：writing 被拦 → orchestrator 照指令退回上游 → experiment 成功
    跑完 → 但计数只在 writing **自己**成功时才清零，于是 writing 永远起不来。
    orchestrator 原话："框架持续拒绝 writing（计数未重置）"。
    """
    from shared.tools.run_node import _repeated_failure_for
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id=None)
    for i in range(4):
        _fail_run(tmp_path, f"1793000{i:03d}-w", "writing",
                  missing=["manuscript_no_unverified_details"], project_id=None)
    assert _repeated_failure_for(state, "writing")["count"] == 4
    # 系统节点跑完不算"状况改变"
    _fail_run(tmp_path, "1793000500-c", "_curator", status="completed", project_id=None)
    assert _repeated_failure_for(state, "writing") is not None
    # 上游 producing 节点成功 → 状况已变，解除拦截
    _fail_run(tmp_path, "1793000600-e", "experiment", status="completed", project_id=None)
    assert _repeated_failure_for(state, "writing") is None, "上游补齐后必须放行重试"


def test_infra_protocol_failure_not_counted_as_node_stall(tmp_path):
    """v3.7.1：后端协议抽风不算节点卡死。

    E2E-3：postprocess 第 1 turn 收到 7-token 空回复 → dsml_leak，被当成一次
    真失败计入卡死统计，并原样交回 orchestrator（后者据此判 blocked 停机）。
    """
    from shared.tools.run_node import _repeated_failure_for
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id=None)
    for i in range(5):
        _fail_run(tmp_path, f"1794000{i:03d}-p", "postprocess",
                  missing=["clean_results"], project_id=None,
                  extra={"failure_category": "provider_tool_call_protocol_error"})
    assert _repeated_failure_for(state, "postprocess") is None, "基础设施故障不该计入"


def test_infra_retry_predicate():
    """只重试"零产出"的协议故障；跑出东西的失败不重来。"""
    from shared.tools.run_node import _is_retryable_infra_failure
    leak = {"status": "incomplete", "turns": 1, "tool_call_count": 0,
            "failure_category": "provider_tool_call_protocol_error"}
    assert _is_retryable_infra_failure(leak)
    assert not _is_retryable_infra_failure({**leak, "status": "completed"})
    assert not _is_retryable_infra_failure({**leak, "tool_call_count": 12}), \
        "已经调过工具 → 重跑会浪费/覆盖成果"
    assert not _is_retryable_infra_failure({**leak, "turns": 9})
    assert not _is_retryable_infra_failure(
        {**leak, "failure_category": "quality_checks_failed"}), "真失败不能被吞掉"


# test_judge_reasoning_not_truncated_mid_sentence 已删（2026-08-22）：
# 它钉的 _failed_check_reasons 随 QC 层退场；「机械拒绝理由必须完整到达」
# 这条不变量现在由写入面工具直接返回完整报错保证（test_artifact_intake_provenance 等覆盖）。

def _prior_run_with_artifact(base, name, node_type, project_id, *,
                             status="incomplete", missing=None,
                             artifact_id="manuscript__x", artifact_type="manuscript",
                             extra_artifacts=None, evaluated=None):
    # QC 层删除（2026-08-22）：失败只由契约信号表达（missing_required_outputs）。
    # evaluated=False（撞 max_turns 没走到评估）在新语义下用 status="error" 表达
    # —— catch-all 收尸的 run 没有终态判定，不参与平反。
    d = base / name
    arts = [{"id": artifact_id, "type": artifact_type, "name": "x"}]
    arts += list(extra_artifacts or [])
    # 上一次 run 的产物在它的 run 本地账本上（原生文件 + records.jsonl）
    store = RecordStore(d / "artifacts", d / "records.jsonl")
    for a in arts:
        store.save(
            artifact_id=a["id"], artifact_type=a["type"], name=a.get("name") or a["id"],
            content="\\section{Intro} previous draft", metadata={},
            directory=d / "artifacts", created_at="2026-09-12T00:00:00+00:00",
            provenance=produced(node_type, name),
            produced_by_node_type=node_type, produced_by_run_id=name,
            by_node=node_type, by_run=name,
        )
    d.mkdir(parents=True, exist_ok=True)
    if evaluated is False and status == "incomplete":
        status = "error"
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": status,
        "missing_required_outputs": list(missing or []),
        "state_dir": str(d),
        "artifacts": arts,
    }), encoding="utf-8")
    return d


def test_revision_baseline_injected_only_after_failure(tmp_path):
    """v3.7.2：重新调起失败过的节点 → 必须告诉它上一版在哪。

    E2E-3：writing 第 1 次跑出完整 manuscript、只挂 1 条 check；第 2 次拿着精准
    的 revision_instructions 仍从零重做，50 轮撞上限、挂了 5 条。上一版根本没
    交回给它。
    """
    from shared.tools.run_node import _revision_baseline_note
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_rev")
    assert _revision_baseline_note(st, "writing") is None, "没跑过 → 不注入"

    _prior_run_with_artifact(tmp_path, "1796000001-w", "writing", "p_rev",
                             missing=["manuscript_no_unverified_details"])
    b = _revision_baseline_note(st, "writing")
    assert b and b["run_id"] == "1796000001-w"
    assert "manuscript__x" in b["note"], "必须列出上一版的 artifact id"
    assert "read_own_prior_attempt" in b["note"], "必须告诉它用哪个工具读回来"
    assert "从零重做" in b["note"], "必须明说重做是错误默认"

    # 上一次干净成功 → 这是新一轮工作，不是修订
    _prior_run_with_artifact(tmp_path, "1796000002-w", "writing", "p_rev",
                             status="completed", missing=[])
    assert _revision_baseline_note(st, "writing") is None
    # 系统节点不适用
    assert _revision_baseline_note(st, "_curator") is None


def test_revision_baseline_always_carries_failed_checks_key(tmp_path):
    """基线 dict 必须带 `failed_checks` 键 —— 消费方硬取它，缺了就 KeyError。

    E2E v29 实测：writing 拿到 freeze_artifact 工具后一去重调，run_node 在
    `revision_baseline_injected` 事件里读 `_baseline["failed_checks"]`，而构造方
    `_revision_baseline_note` 从没产出这个键 → KeyError → manuscript 永远冻不了。
    构造方漏一个键、消费方硬取一个键，契约两头对不上。这条钉住那个键的存在。
    """
    from shared.tools.run_node import _revision_baseline_note
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_fc")
    _prior_run_with_artifact(tmp_path, "1796000101-w", "writing", "p_fc",
                             missing=["manuscript_no_unverified_details"])
    b = _revision_baseline_note(st, "writing")
    assert b is not None
    assert "failed_checks" in b, "消费方（revision_baseline_injected 事件）硬取这个键"
    assert isinstance(b["failed_checks"], list)


@pytest.mark.asyncio
async def test_read_own_prior_attempt_refuses_other_nodes(tmp_path):
    """窄工具：只能读自己的历史产物，不得成为绕过上游转发门禁的新洞。"""
    from shared.tools.run_node import _read_own_prior_attempt
    _prior_run_with_artifact(tmp_path, "1796100001-w", "writing", "p_iso",
                             missing=["x"])
    _prior_run_with_artifact(tmp_path, "1796100002-e", "experiment", "p_iso",
                             missing=["y"], artifact_id="experiment_log__e",
                             artifact_type="experiment_log")
    st = State.new(node_type="writing", base_dir=tmp_path, project_id="p_iso")

    ok = await _read_own_prior_attempt(st, artifact_id="manuscript__x")
    assert ok["status"] == "success"
    assert ok["from_run_id"] == "1796100001-w"
    assert "previous draft" in json.dumps(ok["artifact"], ensure_ascii=False)

    # 指名别的节点的 run → 拒绝
    bad = await _read_own_prior_attempt(st, artifact_id="experiment_log__e",
                                        run_id="1796100002-e")
    assert bad["status"] == "error" and "只能" in bad["error"]

    # 跨项目也读不到
    st2 = State.new(node_type="writing", base_dir=tmp_path, project_id="other_proj")
    assert (await _read_own_prior_attempt(st2, artifact_id="manuscript__x"))["status"] == "error"


def test_read_own_prior_attempt_is_always_on_for_producing_nodes():
    from core.loader import _ALWAYS_ON_TOOLS, _with_always_on_tools
    assert "read_own_prior_attempt" in _ALWAYS_ON_TOOLS
    assert "read_own_prior_attempt" in _with_always_on_tools(["save_artifact"], "writing")
    assert "read_own_prior_attempt" not in _with_always_on_tools(["save_artifact"], "_curator")


def test_revision_baseline_picks_best_attempt_not_latest(tmp_path, monkeypatch):
    """v3.7.2a：基线要挑走得最远的那次，不是最近那次。

    E2E-3 现场：最近一次 writing 撞 max_turns、连 manuscript 都没产出；再往前
    那次 31 轮跑出完整 manuscript、62 项 preflight 全过、只挂 1 条 check。第一版
    取 prior[0]，等于让它从一个更烂的起点重来 —— 实测节点确实调了
    read_own_prior_attempt，但读回来的清单里根本没有 manuscript。
    """
    import shared.tools.run_node as rn

    class _H:
        required_output_artifact_types = ["manuscript", "writing_preflight_plan"]
    monkeypatch.setattr("core.loader.load_harness", lambda nt, *a, **k: _H())

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_best")
    # 早一次：跑出 manuscript（走得远），挂 1 条
    _prior_run_with_artifact(tmp_path, "1797000001-w", "writing", "p_best",
                             missing=["manuscript_no_unverified_details"],
                             artifact_id="manuscript__good", artifact_type="manuscript")
    # 晚一次：只有 preflight plan，撞 max_turns，什么 check 都没跑
    _prior_run_with_artifact(tmp_path, "1797000002-w", "writing", "p_best",
                             missing=["manuscript"], evaluated=False,
                             artifact_id="writing_preflight_plan__meh",
                             artifact_type="writing_preflight_plan")

    b = rn._revision_baseline_note(st, "writing")
    assert b["run_id"] == "1797000001-w", "必须挑产出更多必需产物的那次"
    assert "manuscript__good" in b["note"]
    assert "不是最近那次" in b["note"], "要显式提醒这不是最近一次，避免它以为清单过时"


def test_revision_baseline_carries_provenance_guardrail(tmp_path):
    """v3.7.2b：基线机制本身在鼓励复用 —— 护栏必须跟基线同时给。

    E2E-3 现场：experiment 拿到的基线是个 completed run，带 clean_results /
    experiment_log / repro_bundle。"别从零开始"对产测量数据的节点很容易被读成
    "沿用上次的结果"，正好撞上"实验复用旧数据冒充新结果"这个老问题。
    """
    from shared.tools.run_node import _revision_baseline_note
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_prov")
    _prior_run_with_artifact(tmp_path, "1798000001-e", "experiment", "p_prov",
                             missing=["experiment_has_repro_bundle"],
                             artifact_id="clean_results__r", artifact_type="clean_results")
    note = _revision_baseline_note(st, "experiment")["note"]
    assert "与上一版不同" in note, "任务变了就不该套用基线"
    assert "沉默复用等于伪造" in note, "数据复用必须显式声明来源"
    assert "run id" in note


@pytest.mark.asyncio
async def test_baseline_note_and_tool_agree_on_which_run(tmp_path, monkeypatch):
    """v3.7.2c：note 列的 artifact id 必须真能被工具默认读到。

    E2E-3 实测：PR#198 只改了 note 的选择（挑走得最远那次），工具默认还读最近
    那次 —— 节点照着 note 里的 id 去读，连报两次"run xxx 里没有 artifact yyy"。
    """
    import shared.tools.run_node as rn

    class _H:
        required_output_artifact_types = ["manuscript"]
    monkeypatch.setattr("core.loader.load_harness", lambda nt, *a, **k: _H())

    # 走得最远的一次（早）：有 manuscript，被评估过
    _prior_run_with_artifact(tmp_path, "1799000001-w", "writing", "p_agree",
                             missing=["manuscript_no_unverified_details"],
                             artifact_id="manuscript__good", artifact_type="manuscript")
    # 最近一次：撞 max_turns，只有别的产物，没被评估过
    _prior_run_with_artifact(tmp_path, "1799000002-w", "writing", "p_agree",
                             missing=[], evaluated=False,
                             artifact_id="writing_preflight_plan__meh",
                             artifact_type="writing_preflight_plan")

    st_o = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_agree")
    note = rn._revision_baseline_note(st_o, "writing")
    assert note["run_id"] == "1799000001-w"

    # 节点照着 note 里的 id、用工具默认（不传 run_id）读 → 必须命中
    st_w = State.new(node_type="writing", base_dir=tmp_path, project_id="p_agree")
    got = await rn._read_own_prior_attempt(st_w, artifact_id="manuscript__good")
    assert got["status"] == "success", got
    assert got["from_run_id"] == note["run_id"], "工具默认必须和 note 指的是同一次"


def test_terminal_gate_reconciles_orphaned_child_run(tmp_path):
    """v3.7.3：子 run 跑完但父进程先没了 → 成功的那次对父隐形，项目永远关不掉。

    E2E-3 现场：writing run 1785222389-e08990 跑满 46 轮、12 项 QC 全绿、PDF
    编译完成，但 orchestrator 在它写下 subagent_call_end 之前被重启，transcript
    里最后记录的仍是它前面那个失败的 run。终态门禁于是永远驳回 "complete"，
    orchestrator 反复抱怨"这个 run 上一轮已经 PROCEED 关闭了，但框架仍然标记它"。
    磁盘上的兄弟 run 才是事实来源。
    """
    import chat

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_orph")
    base = st.root.parent
    # 父只记下了失败那次
    _fail_run(base, "1800000001-w", "writing", missing=["manuscript"],
              project_id="p_orph")
    st.append_transcript("subagent_call_end", child_node_type="writing",
                         child_run_id="1800000001-w", child_status="incomplete")
    assert [r["run_id"] for r in chat._continuous_unresolved_terminal_producers(st)] \
        == ["1800000001-w"]

    # 之后成功的那次只在磁盘上（父没来得及记）
    _fail_run(base, "1800000002-w", "writing", status="completed", project_id="p_orph")
    assert chat._continuous_unresolved_terminal_producers(st) == [], \
        "磁盘上更晚的成功 run 必须被对账进来，否则项目永远关不掉"

    # 别的项目的 run 不参与对账
    _fail_run(base, "1800000003-w", "writing", missing=["manuscript"],
              project_id="other")
    assert chat._continuous_unresolved_terminal_producers(st) == []


# ── v3.8：decision package 的 "null" 字符串死循环 ──────────────────────────

def test_absent_sentinels_normalize_to_none():
    """LLM 把"没有失败原因"写成 null/none/N/A → 到这里是**非空字符串**。

    E2E-4 现场：orchestrator 传 review_failed_reason="null"（JSON 字面量 null 被
    序列化成字符串），`not "null"` 为假 → critique artifact 根本不被读 →
    review_unusable=True → 推荐 RETRY REVIEWER（该选项集不含 PROCEED）→
    auto-approve 照做 → 再审同一个产物。literature 的 survey_report 被连审 4 轮，
    reviewer 每轮 APPROVE(4/5)，orchestrator 自己都写了"四轮一致 proceed"，
    管线就是出不去。一个 truthy 的 "null" 把整条流水线钉死在第一个节点。
    """
    from shared.tools.library.decision_package import _normalize_absent

    for v in (None, "", "  ", "null", "NULL", "None", "n/a", "N/A", "nil",
              "undefined", "false", "NaN"):
        assert _normalize_absent(v) is None, repr(v)
    # 真实的失败原因不能被吞掉
    for v in ("review_critique content 非法 JSON", "artifact not found", "0", "no"):
        assert _normalize_absent(v) == v.strip(), repr(v)


@pytest.mark.asyncio
async def test_string_null_does_not_force_retry_reviewer(tmp_path):
    """端到端：传 "null" 时必须照常读 critique 并给出 PROCEED，而不是 retry。"""
    import json as _json

    from shared.tools.library.decision_package import _present_decision_package

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_null")
    state.save_artifact("survey_report", "s", "survey body", {})
    state.save_artifact("review_critique", "c", _json.dumps({
        "verdict": "approve",
        "recommended_action": {"action": "proceed",
                               "feedback_to_next_run": "可以进入下一阶段"},
        "findings": [],
    }), {"produced_by_node_type": "_reviewer"}, provenance=_review_provenance(state))
    res = await _present_decision_package(
        state,
        source_node_type="literature",
        producing_run_id="run-x",
        artifact_ids=["survey_report__s"],
        review_critique_artifact_id="review_critique__c",
        review_failed_reason="null",          # ← 现场传的就是这个
    )
    ctx = _json.dumps(res, ensure_ascii=False)
    assert "PROCEED" in ctx, "review 是 approve，选项集必须含 PROCEED"
    assert "review 失败/不可用" not in ctx


def test_reviewer_retry_is_capped_per_producer_run(tmp_path, monkeypatch):
    """v3.8：同一个 producer run 的 reviewer 重试必须封顶。

    E2E-4 活锁：decision → retry_reviewer → reviewer APPROVE(4/5) → 又 decision →
    又 retry_reviewer …… 连续 5 轮，literature 的 survey_report 反复重审，流水线
    一步没走。现有熔断全都够不着 —— 整个循环在**一个 orchestrator run 内部的
    pause/resume** 里（run_end=0、continuous_followup_scheduled=0），而乒乓/停滞/
    重复失败熔断都在 _continuous_followup，那是**轮之间**跑的。PR#193 同一条教训。
    """
    import shared.tools.library.decision_package as dp

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_cap")
    meta = {"producing_run_id": "run-A", "producing_node": "literature"}

    def _entry(attempts):
        flows = state.hook_state.setdefault("pending_post_node_flow", [])
        flows.clear()
        flows.append({
            "producing_run_id": "run-A", "producing_node": "literature",
            "review_state": "failed", "decision_state": "awaiting_human",
            "review_attempt_count": attempts,
            "decision_options": list(dp._RETRY_FIRST_ACTIONS
                                     if hasattr(dp, "_RETRY_FIRST_ACTIONS")
                                     else ["retry_reviewer", "revise",
                                           "redirect_upstream", "abort"]),
        })

    # 前几次放行
    for n in range(dp._RETRY_REVIEWER_MAX):
        _entry(n)
        got = dp.record_decision_answer(state, meta, "1")
        assert got["review_state"] == "retry_authorized", f"第 {n+1} 次应放行"

    # 达到上限 → 拒绝，并且给出别的出路
    _entry(dp._RETRY_REVIEWER_MAX)
    got = dp.record_decision_answer(state, meta, "1")
    assert got["review_state"] != "retry_authorized"
    assert got["accepted_action"] is None
    err = got["decision_validation_error"]
    assert "上限" in err
    assert "PROCEED" in err and "REVISE" in err and "REDIRECT" in err
