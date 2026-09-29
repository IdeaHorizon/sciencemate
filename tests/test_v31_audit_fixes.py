"""v3.1（2026-06 架构审计）修复的回归测试。

每个测试对应审计的一条 confirmed 高危 —— 这些行为回退 = 诚信/正确性卖点回退。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State


# ── 高危#1：normalize 保留语义算符，对立 claim 不再同 id ──────────────────────

def test_opposite_claims_get_distinct_ids():
    from shared.lib.kb_schema import compute_kb_id
    a = {"claim_text": "accuracy > 90% on QM9", "claim_type": "empirical",
         "scope_dimensions": {}}
    b = {"claim_text": "accuracy < 90% on QM9", "claim_type": "empirical",
         "scope_dimensions": {}}
    assert compute_kb_id("claims", a) != compute_kb_id("claims", b)


def test_semantic_ops_survive_normalize():
    from shared.lib.kb_schema import normalize
    assert ">" in normalize("x > 90%")
    assert "<" in normalize("x < 90%")
    assert normalize("x>90") == normalize("x > 90")   # 算符独立成 token


def test_id_width_is_12_hex():
    from shared.lib.kb_schema import compute_kb_id
    cid = compute_kb_id("claims", {"claim_text": "t", "claim_type": "empirical"})
    assert len(cid.split("_", 1)[1]) == 12


# ── verdict 翻转：只剩契约检查（判决拆除第三波，kb_schema:465/474 降格）──────

def test_flip_checks_the_contract_not_the_sufficiency():
    """reasoning 非空、evidence_ids 引用成形 = 契约（C）。「翻到 refuted 须 ≥1 证据」
    「hypothesis 不能只引 claim」是充分性判决 —— 删了，evidence_ids=[] 如实进
    review_history，充分性由 kb.update_claim_status 的「降落 provisional」承接。
    墙加回去这条转红。"""
    from shared.lib.kb_schema import validate_status_flip, SchemaValidationError
    rec = {"claim_type": "hypothesis"}
    ok_reason = "对照 falsification_criteria 第 2 条，RMSD 超阈值 3 倍，判定不成立"
    with pytest.raises(SchemaValidationError, match="reasoning"):
        validate_status_flip(rec, "refuted", reasoning="   ", evidence_ids=[])
    with pytest.raises(SchemaValidationError, match="evidence_ids"):
        validate_status_flip(rec, "refuted", reasoning=ok_reason, evidence_ids=["bogus"])
    # 文案与判据同源：不再许诺一个不存在的字数闸
    with pytest.raises(SchemaValidationError) as exc:
        validate_status_flip(rec, "refuted", reasoning="", evidence_ids=[])
    assert "30 字符" not in str(exc.value)
    validate_status_flip(rec, "refuted", reasoning="太短", evidence_ids=[])
    validate_status_flip(rec, "refuted", reasoning=ok_reason,
                         evidence_ids=["claim_abcdef123456"])
    validate_status_flip(rec, "refuted", reasoning=ok_reason,
                         evidence_ids=["chunk_abcdef123456"])


def test_confidence_only_update_no_longer_raises(tmp_path):
    """审计 medium：confidence-only 路径曾 100% 抛"非法 status 转换 None"。"""
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p1")
    rec, _ = state.write_kb("claims", {
        "claim_text": "conf only test claim", "claim_type": "empirical",
        "confidence": 0.5, "concept_ids": [], "orphan_reason": "test",
        "sources": ["doi:10/x"],
    })
    updated = state.update_lifecycle(
        "claims", rec["id"], status_change={"confidence": 0.9},
        reasoning="confidence-only 调整",
    )
    assert updated is not None
    assert updated["confidence"] == 0.9


# ── 高危#8：refute 自动传播 needs_review ─────────────────────────────────────

def test_needs_review_in_state_machine():
    from shared.lib.kb_schema import CLAIM_STATUSES, can_transition_claim_status
    assert "needs_review" in CLAIM_STATUSES
    assert can_transition_claim_status("validated", "needs_review")
    assert can_transition_claim_status("needs_review", "refuted")
    assert not can_transition_claim_status("refuted", "needs_review")


def test_refute_propagates_needs_review(tmp_path):
    from core.kb_edges import add_edge, propagate_refutation
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p2")
    up, _ = state.write_kb("claims", {
        "claim_text": "upstream base claim", "claim_type": "empirical",
        "confidence": 0.6, "concept_ids": [], "orphan_reason": "t",
        "sources": ["doi:10/u"],
    })
    down, _ = state.write_kb("claims", {
        "claim_text": "downstream dependent claim", "claim_type": "empirical",
        "confidence": 0.6, "concept_ids": [], "orphan_reason": "t",
        "sources": ["doi:10/d"],
    })
    # down depends_on up（反向边 up→down 的 dependents 由 add_edge 自动写）
    add_edge(state, from_id=down["id"], to_id=up["id"], edge_type="depends_on")
    affected = propagate_refutation(state, up["id"])
    assert down["id"] in affected
    rec = state.get_kb_record("claims", down["id"])
    assert rec["status"] == "needs_review"
    # derive_status 不把 needs_review 冲回 provisional（merge 场景）
    from shared.lib.kb_schema import derive_status
    assert derive_status(rec) == "needs_review"


# ── 高危#4：prereg 冻结加固 ──────────────────────────────────────────────────

def test_freeze_error_message_does_not_teach_bypass(tmp_path):
    state = State.new(node_type="_curator", base_dir=tmp_path)
    saved = state.save_artifact("pre_registration", "h1", "content")
    state.mark_frozen(saved["id"])     # 冻结 = 账本 freeze 行，不是 metadata 手写
    with pytest.raises(ValueError) as ei:
        state.save_artifact("pre_registration", "h1", "changed!")
    msg = str(ei.value)
    assert "用一个新的 name 或 artifact_type 保存" not in msg
    assert "不要" in msg   # 明确禁止换名重存


@pytest.mark.asyncio
async def test_executor_blocks_unfrozen_prereg(tmp_path):
    """experiment 类节点：pre_registration 输入未 freeze → blocked，不进 loop。"""
    from core.executor import execute_node
    from core.harness import NodeHarness
    harness = NodeHarness(
        node_type="experiment_like",
        system_prompt="test", tools=[],
        required_input_artifact_types=["pre_registration"],
    )

    class _NoLLM:   # 不应被调用
        async def chat(self, *a, **k):
            raise AssertionError("LLM should not be called when blocked")

    summary = await execute_node(
        "experiment_like", state_dir=tmp_path, harness_override=harness,
        llm=_NoLLM(),
        upstream_artifacts=[{
            "type": "pre_registration", "name": "h1",
            "content": "unfrozen prereg", "metadata": {},   # 未 freeze
        }],
    )
    assert summary["status"] == "blocked_missing_inputs"
    assert summary["unfrozen_required_inputs"] == ["pre_registration"]


@pytest.mark.asyncio
async def test_executor_allows_frozen_prereg(tmp_path):
    """回归守卫（2026-07 同事报的 bug）：**已 freeze** 的 pre_registration
    必须放行进 loop。此前 frozen 检查读 list_artifacts()（不含 metadata）→
    frozen=true 被丢 → 每个带预注册输入的节点 100% 误 blocked。

    注意：原 test_executor_blocks_unfrozen_prereg 只测'未冻结→拒'，带着 bug
    也会过（未冻结确实该拒）；缺的就是这条'冻结→放行'，正是它让回归溜过。"""
    from core.executor import execute_node
    from core.harness import NodeHarness
    harness = NodeHarness(
        node_type="experiment_like2",
        system_prompt="test", tools=[],
        required_input_artifact_types=["pre_registration"],
    )

    class _StopLLM:   # 放行后会被调用一次，立刻收尾
        model = "fake"
        async def chat(self, messages, **kw):
            class R:
                content = "prereg 已冻结，正常开跑"
                tool_calls = []
                finish_reason = "stop"
                usage = {}
                reasoning_content = None
            return R()

    summary = await execute_node(
        "experiment_like2", state_dir=tmp_path, harness_override=harness,
        llm=_StopLLM(),
        upstream_artifacts=[{
            "type": "pre_registration", "name": "h1",
            "content": "frozen prereg", "metadata": {"frozen": True},   # 已 freeze
        }],
    )
    assert summary["status"] != "blocked_missing_inputs", (
        f"frozen 的 pre_registration 被误 block 了：{summary}")
    assert not (summary.get("unfrozen_required_inputs") or [])


# ── 高危#5：多 tool_calls 中途 pause → 剩余 call 补配对消息 ──────────────────

@pytest.mark.asyncio
async def test_pause_mid_turn_synthesizes_deferred_tool_messages(tmp_path):
    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.tool_registry import register_tool, ToolDefinition
    from core.llm import LLMMessage

    calls = [
        {"id": "c1", "function": {"name": "t_ok", "arguments": "{}"}},
        {"id": "c2", "function": {"name": "t_pause", "arguments": "{}"}},
        {"id": "c3", "function": {"name": "t_ok", "arguments": "{}"}},
        {"id": "c4", "function": {"name": "t_ok", "arguments": "{}"}},
    ]

    async def _ok(state, **_):
        return {"status": "success"}

    async def _pause(state, **_):
        return {"status": "pause", "pause_event": {"question": "q?"}}

    register_tool(ToolDefinition(name="t_ok", description="d",
                                 parameters_schema={"type": "object", "properties": {}}), _ok)
    register_tool(ToolDefinition(name="t_pause", description="d",
                                 parameters_schema={"type": "object", "properties": {}}), _pause)

    class _LLM:
        model = "fake"
        async def chat(self, messages, **kw):
            class R:
                content = ""
                tool_calls = calls
                finish_reason = "tool_calls"
                usage = {}
                reasoning_content = None
            return R()

    harness = NodeHarness(node_type="t_node", system_prompt="s",
                          tools=["t_ok", "t_pause"], max_turns=3)
    state = State.new(node_type="t_node", base_dir=tmp_path)
    msgs = [LLMMessage(role="system", content="s")]
    result = await run_loop(harness, state, msgs, _LLM())
    assert result.status == "paused"
    # c1 真实执行、c2 pause 占位、c3/c4 合成 deferred —— 全部配对
    tool_msgs = {m.tool_call_id for m in msgs if m.role == "tool"}
    assert tool_msgs == {"c1", "c2", "c3", "c4"}
    deferred = [m for m in msgs if m.role == "tool"
                and m.tool_call_id in ("c3", "c4")]
    assert all("deferred" in (m.content or "") for m in deferred)


# ── token 软预算（v3.2 改：warn-only，不再硬停）─────────────────────────────

@pytest.mark.asyncio
async def test_budget_soft_warn_does_not_hard_stop(tmp_path):
    """v3.2（v10b 实测）：token 预算改 warn-only —— 累计花费不再硬停，
    因为它会误杀"很多便宜轮次"的合法重活（experiment 跑 4 个 LAMMPS）。
    硬边界交给 max_turns。"""
    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    class _LLM:
        model = "fake"
        n = 0
        async def chat(self, messages, **kw):
            class R:
                content = "done"
                tool_calls = []          # 立即收尾（stop），不无限跑
                finish_reason = "stop"
                usage = {"total_tokens": 60_000}
            R.reasoning_content = None
            _LLM.n += 1
            return R()

    harness = NodeHarness(node_type="t_node2", system_prompt="s",
                          tools=[], max_turns=50)
    state = State.new(node_type="t_node2", base_dir=tmp_path)
    state.tokens_limit = 100_000
    state.tokens_used = 500_000    # 远超 1.5×limit —— 旧代码会硬停，新代码只警告
    result = await run_loop(harness, state, [LLMMessage(role="system", content="s")], _LLM())
    assert "预算硬停" not in (result.final_text or ""), "不该再硬停"
    # 软警告被注入过一次
    assert state.hook_state.get("_budget_warned") is True


# ── 高危#9：工具同名冲突默认硬拒（未豁免）────────────────────────────────────

def test_unexempted_tool_collision_raises():
    from core.tool_registry import register_tool, ToolDefinition

    async def _a(state, **_):
        return {"status": "success"}

    # 用一个绝不在豁免表里的名字；注册两次来自"不同文件"难以伪造 ——
    # 直接验证同名+同文件是幂等（不抛）
    name = "v31_test_unique_tool_xyz"
    td = ToolDefinition(name=name, description="d",
                        parameters_schema={"type": "object", "properties": {}})
    register_tool(td, _a)
    register_tool(td, _a)   # 同源文件幂等，不抛

    # 不同源文件的同名注册 → 必须抛（exec 动态造一个"另一个文件"的函数）
    ns: dict = {}
    code = compile(
        "async def _b(state, **kw):\n    return {'status': 'success'}\n",
        str(Path("/tmp/v31_fake_other_module.py")), "exec")
    exec(code, ns)
    with pytest.raises(ValueError, match="同名冲突"):
        register_tool(td, ns["_b"])


# ── citation：引用 refuted claim 也 fail ─────────────────────────────────────

def test_citation_check_flags_refuted_claims(tmp_path):
    from shared.lib.citation_integrity import find_phantom_citations
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p3")
    rec, _ = state.write_kb("claims", {
        "claim_text": "later refuted claim", "claim_type": "empirical",
        "confidence": 0.5, "concept_ids": [], "orphan_reason": "t",
        "sources": ["doi:10/r"],
    })
    chunk, _ = state.write_kb("chunks", {"text": "ev", "source": "doi:10/ev"})
    state.update_lifecycle("claims", rec["id"], status_change={
        "to_status": "refuted", "evidence_ids": [chunk["id"]],
    }, reasoning="被实验证伪：观测值与预测相反，详见证据 chunk")
    res = find_phantom_citations(f"as shown in {rec['id']} ...", state)
    assert rec["id"] in res["invalid_status"]
    assert res["invalid_status"][rec["id"]] == "refuted"


# ═══════════════════════════════════════════════════════════════════════════
# 反向/正向补测（2026-07）：v3.1 的检查点很多只测了"该拒→拒"，没测"该放→放"。
# frozen-prereg 回归就是藏在没被测的正向路径里。下面把正向路径补齐 —— 任何一条
# 挂了 = 又一个"静默拖垮正常运行"的 bug。
# ═══════════════════════════════════════════════════════════════════════════


def test_identical_claims_still_dedup():
    """normalize 保留算符后，**完全相同**的 claim 仍必须同 id（去重没被破坏）。
    只测了'对立→分开'不够 —— 若过度保留导致相同也分开，KB 会静默塞满重复。"""
    from shared.lib.kb_schema import compute_kb_id
    a = {"claim_text": "accuracy > 90% on QM9  (n=3)", "claim_type": "empirical",
         "scope_dimensions": {"dataset": "QM9"}}
    b = {"claim_text": "Accuracy  >  90%  on QM9 (n=3)", "claim_type": "empirical",
         "scope_dimensions": {"dataset": "qm9"}}   # 大小写/空格差异应被归一
    assert compute_kb_id("claims", a) == compute_kb_id("claims", b)


def test_citation_valid_claim_not_flagged(tmp_path):
    """引用 validated/provisional（有效）claim **不该**被判问题。只测了'引 refuted
    →flag'，若正向过度 flag，writing 引任何有效 claim 都会挂 review。"""
    from shared.lib.citation_integrity import find_phantom_citations
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="pc_valid")
    rec, _ = state.write_kb("claims", {
        "claim_text": "valid provisional claim", "claim_type": "empirical",
        "confidence": 0.5, "concept_ids": [], "orphan_reason": "t",
        "sources": ["doi:10/v"],
    })
    res = find_phantom_citations(f"as shown in {rec['id']} the effect holds", state)
    assert rec["id"] in res["real"]
    assert rec["id"] not in res["phantom"]
    assert rec["id"] not in (res.get("invalid_status") or {})


@pytest.mark.asyncio
async def test_budget_under_limit_runs_normally(tmp_path):
    """token 熔断：**未超预算**时绝不能误停。只测了'超→停'，若正向误停，所有
    正常 run 都被砍。"""
    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    class _LLM:
        model = "fake"
        async def chat(self, messages, **kw):
            class R:
                content = "任务完成"
                tool_calls = []
                finish_reason = "stop"
                usage = {"total_tokens": 500}
                reasoning_content = None
            return R()

    harness = NodeHarness(node_type="tb_under", system_prompt="s", tools=[], max_turns=5)
    state = State.new(node_type="tb_under", base_dir=tmp_path)
    state.tokens_limit = 1_000_000     # 宽松预算
    result = await run_loop(harness, state, [LLMMessage(role="system", content="s")], _LLM())
    assert "预算硬停" not in (result.final_text or ""), "未超预算却触发熔断"
    assert result.final_text == "任务完成"


@pytest.mark.asyncio
async def test_create_claim_hypothesis_accepts_frozen_prereg_chunk(tmp_path):
    """create_claim 的 hypothesis 正向路径：prereg chunk 存在且已冻结 → 放行。
    这是和 executor frozen bug 同区域的检查，之前**零测试**。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute as execute_tool
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="pph_ok")
    c = await execute_tool("create_concept", state, canonical_name="OOD generalization gap",
                           concept_type="phenomenon", description="d")
    chunk, _ = state.write_kb("chunks", {
        "text": "pre-registration content", "source": "artifact://prereg_h1",
        "origin_artifact_frozen": True,        # 已冻结
    })
    res = await execute_tool(
        "create_claim", state,
        claim_text="model X degrades > 2x on OOD split",
        claim_type="hypothesis", confidence=0.5,
        concept_ids=[c["id"]], sources=[chunk["id"]],
        falsification_criteria_text="若 MAE ratio < 2x 则 refuted",
        prereg_chunk_id=chunk["id"], hypothesis_id="H1",
        predicted_outcome="MAE ratio > 2x",
    )
    assert res.get("status") == "success", f"冻结的 prereg 被误拒: {res}"


@pytest.mark.asyncio
async def test_create_claim_hypothesis_rejects_unfrozen_or_missing_prereg(tmp_path):
    """负向配套：prereg chunk 不存在 → 拒（引用不存在的 id 是契约违约）；
    存在但未冻结 → 照写，claim 如实标 prereg_frozen=false（判决拆除：顺序闸降格，
    冻结仍欠着——experiment 只认冻结后的预注册）。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute as execute_tool
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="pph_bad")
    c = await execute_tool("create_concept", state, canonical_name="OOD gap 2",
                           concept_type="phenomenon", description="d")
    common = dict(claim_type="hypothesis", confidence=0.5, concept_ids=[c["id"]],
                  falsification_criteria_text="crit", predicted_outcome="pred",
                  hypothesis_id="H1")
    # a) chunk 不存在
    r1 = await execute_tool("create_claim", state, claim_text="h missing chunk",
                            sources=["doi:10/x"], prereg_chunk_id="chunk_deadbeef0000", **common)
    assert r1.get("status") == "error" and "不存在" in r1.get("error", "")
    # b) chunk 存在但未冻结
    ch, _ = state.write_kb("chunks", {"text": "t", "source": "artifact://p2",
                                       "origin_artifact_frozen": False})
    r2 = await execute_tool("create_claim", state, claim_text="h unfrozen chunk",
                            sources=[ch["id"]], prereg_chunk_id=ch["id"], **common)
    assert r2.get("status") == "success", r2
    assert r2["prereg_frozen"] is False
    assert "冻结" in r2["note"]
    assert state.get_kb_record("claims", r2["id"])["prereg_frozen"] is False


def test_registered_custom_loop_resolves(tmp_path):
    """custom loop 正门正向路径：**已登记**的节点必须能 resolve 出 loop 函数。
    只测了'未登记→不生效'，若正向也失效，data 节点根本跑不起来。"""
    from core.custom_loop import resolve_custom_loop
    from shared.lib.exemptions import allowed_custom_loops, reload
    reload()
    registered = allowed_custom_loops()
    if "data" not in registered:
        pytest.skip("data 未登记 custom loop（登记表变了）")
    fn = resolve_custom_loop("data")
    assert fn is not None and callable(fn), "已登记的 data custom loop 没 resolve 出来"


def test_exemption_lookup_positive_and_negative():
    """工具冲突豁免的正向路径：已登记的覆盖 → 返回条目（→ 降级 warn，bootstrap
    才不炸）；未登记 → None（→ register_tool 硬拒）。只测了'未豁免→拒'。"""
    from shared.lib.exemptions import tool_collision_exemption, reload
    reload()
    e = tool_collision_exemption("web_search", "nodes.data.tools.web_search")
    assert e is not None, "已登记的 web_search 覆盖查不到 → bootstrap 会硬拒 → 炸"
    assert tool_collision_exemption("totally_made_up_tool_xyz", "nodes.foo.bar") is None
