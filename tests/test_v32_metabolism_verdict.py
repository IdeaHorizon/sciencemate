"""v3.2 item#2/#3/#4 + v10 熔断误杀修复的回归测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State


# ── item#4：identity 类 concept 默认 project scope ──────────────────────────

def test_all_concepts_default_project_scope():
    """v3.2 修的是"concept 按类型不对称直通 org"；现在整台启发式都删了。

    那次只把 person/group 拉回 project（它们是 org 污染主源），抽象类仍直通。
    出生一律 project 之后，不对称本身消失 —— 抽象 concept 要跨项目复用，
    走晋升闭包（随它支撑的知识卡一起晋升），不靠出生直落。
    """
    from shared.lib.kb_schema import smart_default_scope
    for ct in ("person", "group", "method", "theory", "metric", "tool",
               "dataset", "phenomenon", "task", "domain"):
        assert smart_default_scope("concepts", {"concept_type": ct}) == "project", ct


# ── item#3：manuscript ↔ KB verdict 一致性 ─────────────────────────────────

def _mk_hyp(state, text, status_override=None):
    rec, _ = state.write_kb("claims", {
        "claim_text": text, "claim_type": "hypothesis", "confidence": 0.5,
        "concept_ids": [], "orphan_reason": "t", "sources": ["doi:10/x"],
        "falsification_criteria_text": "eps<5%",
        "prereg_chunk_id": "chunk_abc123456789",
        "predicted_outcome": "x",
    })
    return rec


def test_verdict_consistency_flags_concluded_on_undecided(tmp_path):
    from shared.lib.citation_integrity import check_verdict_consistency
    st = State.new(node_type="writing", base_dir=tmp_path, project_id="vc1")
    h = _mk_hyp(st, "H1 pressure converges")   # status=open（未定论）
    content = f"We find H1 ({h['id']}) is strongly supported."
    r = check_verdict_consistency(content, st)
    assert not r["passed"]
    assert r["undecided_but_concluded"][0]["id"] == h["id"]


def test_verdict_consistency_flags_direct_contradiction(tmp_path):
    from shared.lib.citation_integrity import check_verdict_consistency
    st = State.new(node_type="writing", base_dir=tmp_path, project_id="vc2")
    h = _mk_hyp(st, "H2 rdf converges")
    # 把它翻成 refuted（带证据）
    chunk, _ = st.write_kb("chunks", {"text": "ev", "source": "doi:10/ev"})
    st.update_lifecycle("claims", h["id"], status_change={
        "to_status": "refuted", "evidence_ids": [chunk["id"]],
    }, reasoning="实测证伪：偏差远超阈值，详见证据 chunk")
    content = f"Our analysis shows H2 ({h['id']}) is clearly supported by the results."
    r = check_verdict_consistency(content, st)
    assert not r["passed"]
    assert r["direct_contradictions"][0]["id"] == h["id"]


def test_verdict_consistency_passes_when_aligned(tmp_path):
    from shared.lib.citation_integrity import check_verdict_consistency
    st = State.new(node_type="writing", base_dir=tmp_path, project_id="vc3")
    h = _mk_hyp(st, "H3 energy converges")
    chunk, _ = st.write_kb("chunks", {"text": "ev", "source": "doi:10/ev"})
    st.update_lifecycle("claims", h["id"], status_change={
        "to_status": "refuted", "evidence_ids": [chunk["id"]],
    }, reasoning="实测证伪：偏差远超阈值，详见证据 chunk")
    content = f"H3 ({h['id']}) was refuted: the deviation exceeded the threshold."
    r = check_verdict_consistency(content, st)
    assert r["passed"]   # 正文 refuted == KB refuted，一致


# ── item#2：dreaming **不许**拦 writing ─────────────────────────────────────

def test_writing_is_never_blocked_by_dreaming(tmp_path, monkeypatch):
    """dreaming 积压再多也不许拦交付。

    这条原本断言的是相反的事（「pending 时 writing 必须被拒」）—— 那道门在
    2026-08-22 删了，所以测试跟着翻转，成为防它被加回来的护栏。

    删门的理由不是它有 bug（虽然它有两个，见 run_node.py 里那段说明），是
    **代价极不对称**：拦错 = 交付归零；放过 = KB 晚点整理。实测那一晚，一个
    已经做完的研究被它钉死 6.5 小时、烧掉全场约一半 token、论文一个字没写出来。
    知识代谢是调度问题，不该拿交付当人质。
    """
    from core.tool_registry import execute
    from core.bootstrap import bootstrap
    bootstrap()
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="dg1")
    st.hook_state["_callable_nodes"] = ["*"]
    # 就算积压拉满
    monkeypatch.setattr(
        "core.dreaming_scheduler.should_run_dreaming",
        lambda pid: (True, ["+30 KB writes", "never run dreaming"]),
    )
    res = asyncio.run(execute("run_node", st, node_type="writing", user_note="测试派发", node_inputs={}))
    # 可以被别的门拦（比如 project_synthesis 还没有），但**不能是 dreaming 拦的**
    assert "dreaming" not in (res.get("error") or "").lower()
    assert not res.get("dreaming_pending_reasons")


def test_writing_not_blocked_when_no_dreaming_pending(tmp_path, monkeypatch):
    from core.tool_registry import execute
    from core.bootstrap import bootstrap
    bootstrap()
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="dg2")
    st.hook_state["_callable_nodes"] = ["*"]
    monkeypatch.setattr(
        "core.dreaming_scheduler.should_run_dreaming",
        lambda pid: (False, []),
    )
    res = asyncio.run(execute("run_node", st, node_type="writing", user_note="测试派发", node_inputs={}))
    # 不该是 dreaming 拦的（会被后面的 writing-gate 拦，但不是 dreaming）
    assert "dreaming" not in (res.get("error") or "").lower()


# ── v10 修复：orchestrator 豁免 per-node token 熔断 ──────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_exempt_from_budget_warn(tmp_path):
    """orchestrator 的 tokens_used 是全项目 rollup，连软预算警告都不该触发；
    producing 节点则会收到软警告（但 v3.2 起不再硬停）。"""
    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    class _LLM:
        model = "fake"; n = 0
        async def chat(self, messages, **kw):
            class R:
                content = "coordinating"; tool_calls = []
                finish_reason = "stop"; usage = {"total_tokens": 50_000}
                reasoning_content = None
            _LLM.n += 1
            return R()

    h = NodeHarness(node_type="_orchestrator", system_prompt="s", tools=[], max_turns=3)
    st = State.new(node_type="_orchestrator", base_dir=tmp_path)
    st.tokens_limit = 100_000
    st.tokens_used = 2_000_000     # rollup 远超预算，但 orchestrator 豁免
    res = await run_loop(h, st, [LLMMessage(role="system", content="s")], _LLM())
    assert "预算硬停" not in (res.final_text or "")
    assert not st.hook_state.get("_budget_warned"), "orchestrator 连软警告都不该有"

    # producing 节点：收软警告，但**不硬停**（照常正常收尾）
    h2 = NodeHarness(node_type="literature", system_prompt="s", tools=[], max_turns=50)
    st2 = State.new(node_type="literature", base_dir=tmp_path)
    st2.tokens_limit = 100_000
    st2.tokens_used = 2_000_000
    res2 = await run_loop(h2, st2, [LLMMessage(role="system", content="s")], _LLM())
    assert "预算硬停" not in (res2.final_text or ""), "v3.2 起 producing 也不硬停"
    assert st2.hook_state.get("_budget_warned") is True, "producing 应收到软警告"
