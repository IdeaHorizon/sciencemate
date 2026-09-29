"""KB 读取工具有界投影 + token 预算回归（2026-07 v10c dogfood 根因）。

根因：curator dreaming 一次 search_kb(claims, limit=200) 返回 159 条全量
claim = 83k tokens 塞进单条 tool result，顶爆 context。第一性原理：KB 无界，
context 有界，读取工具输出必须与 KB 总量无关。

覆盖：
  - summarize_kb_record 各 entity 投影字段正确、不含全文
  - budget_rows 按 token 预算截断、信息保全（total 仍报告）
  - search_kb 默认 summary、不再回全量；full 超预算自动降级
  - search_kb 单次结果 token 有界（哪怕 limit=200 + 大 KB）
  - list_proposals 默认 compact、有界；detail='full' 可要全量
  - get_kb_record 仍全保真（find/read 分离）
"""
from __future__ import annotations

import json

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as execute_tool
from shared.lib.kb_result_budget import (
    DEFAULT_RESULT_TOKEN_BUDGET,
    budget_rows,
    summarize_kb_record,
)


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数：投影 + 预算
# ─────────────────────────────────────────────────────────────────────────────

def test_summarize_claim_drops_full_text_keeps_key_fields():
    rec = {
        "id": "claim_abc", "scope": "org", "status": "validated",
        "claim_type": "empirical", "confidence": 0.9,
        "claim_text": "X" * 5000,
        "sources": ["a", "b", "c"], "concept_ids": ["c1", "c2"],
        "review_history": [{"x": 1}],
    }
    s = summarize_kb_record("claims", rec)
    assert s["id"] == "claim_abc"
    assert s["claim_type"] == "empirical"
    assert s["n_sources"] == 3
    assert s["n_concepts"] == 2
    assert s["n_review_flips"] == 1
    # 全文被截断，不是原样 5000 字符
    assert len(s["claim_text"]) < 5000
    assert s["claim_text"].endswith("…")


def test_summarize_concept_and_chunk():
    c = summarize_kb_record("concepts", {
        "id": "concept_x", "scope": "org", "concept_type": "person",
        "canonical_name": "Jane Doe", "aliases": ["JD"], "usage_count": 4,
    })
    assert c["concept_type"] == "person"
    assert c["n_aliases"] == 1
    ch = summarize_kb_record("chunks", {
        "id": "chunk_y", "scope": "org", "source": "doi:10/x",
        "text": "Z" * 1000, "author_concept_ids": ["p1"],
    })
    assert ch["source"] == "doi:10/x"
    assert len(ch["text"]) < 1000
    assert ch["n_authors"] == 1


def test_budget_rows_truncates_and_flags():
    big_rows = [{"id": i, "blob": "Y" * 4000} for i in range(100)]
    kept, truncated = budget_rows(big_rows, max_tokens=2000)  # 2000 tok ≈ 8000 chars
    assert truncated is True
    assert len(kept) < 100
    # 至少留 1 条（不会因为单条就大到 0 条）
    assert len(kept) >= 1


def test_budget_rows_keeps_all_when_small():
    rows = [{"id": i} for i in range(10)]
    kept, truncated = budget_rows(rows, max_tokens=DEFAULT_RESULT_TOKEN_BUDGET)
    assert truncated is False
    assert len(kept) == 10


# ─────────────────────────────────────────────────────────────────────────────
# search_kb 端到端：有界
# ─────────────────────────────────────────────────────────────────────────────

async def _seed_many_claims(state: State, n: int) -> None:
    c = await execute_tool("create_concept", state, canonical_name="Seed",
                            concept_type="method", description="seed")
    cid = c["id"]
    for i in range(n):
        await execute_tool(
            "create_claim", state,
            claim_text=f"claim {i} " + ("verbose padding text " * 60),  # 每条约 1.3KB
            claim_type="empirical", confidence=0.6,
            concept_ids=[cid], sources=[f"doi:10/{i}"],
        )


@pytest.mark.asyncio
async def test_search_kb_default_summary_is_bounded(tmp_path):
    """核心回归：即便 limit=200 + KB 里 150 条大 claim，单次结果也必须有界
    （远小于把全量 record 倒出来的 80k+）。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_budget")
    await _seed_many_claims(state, 150)

    res = await execute_tool("search_kb", state, entity_type="claims", limit=200)
    assert res["status"] == "success"
    assert res["projection"] == "summary"
    # total_matched 信息保全
    assert res["total_matched"] == 150
    # 单次结果 payload 有界：序列化后远小于全量（全量 ~150×1.3KB ≈ 200KB）
    payload_chars = len(json.dumps(res, ensure_ascii=False))
    assert payload_chars < DEFAULT_RESULT_TOKEN_BUDGET * 4 + 4000, payload_chars
    # summary 行不含全量 claim_text（被截断）
    for row in res["records"]:
        assert len(row.get("claim_text", "")) <= 210


@pytest.mark.asyncio
async def test_search_kb_full_downgrades_when_over_budget(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_dg")
    await _seed_many_claims(state, 150)

    res = await execute_tool("search_kb", state, entity_type="claims",
                              limit=200, projection="full")
    # full 超预算 → 自动降级 summary
    assert res["projection"] == "summary"
    assert res.get("downgraded_from") == "full"
    assert "hint" in res
    payload_chars = len(json.dumps(res, ensure_ascii=False))
    assert payload_chars < DEFAULT_RESULT_TOKEN_BUDGET * 4 + 4000


@pytest.mark.asyncio
async def test_search_kb_full_kept_when_small(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_small")
    await _seed_many_claims(state, 3)
    res = await execute_tool("search_kb", state, entity_type="claims",
                              limit=20, projection="full")
    assert res["projection"] == "full"
    assert res["returned"] == 3
    # full 模式回原始 record（含 claim_text 全文）
    assert any(len(r.get("claim_text", "")) > 210 for r in res["records"])


@pytest.mark.asyncio
async def test_get_kb_record_still_full_fidelity(tmp_path):
    """find/read 分离：summary 拿到 id 后，get_kb_record 仍给全文。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_read")
    await _seed_many_claims(state, 5)
    listed = await execute_tool("search_kb", state, entity_type="claims", limit=5)
    a_id = listed["records"][0]["id"]
    full = await execute_tool("get_kb_record", state, entity="claims", kb_id=a_id)
    assert full["status"] == "success"
    assert len(full["record"]["claim_text"]) > 210  # 全文，没被截


@pytest.mark.asyncio
async def test_search_kb_empty_result_shape(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_empty")
    res = await execute_tool("search_kb", state, entity_type="claims", limit=20)
    assert res["total_matched"] == 0
    assert res["records"] == []
    assert res["count"] == 0  # 兼容旧字段


# ─────────────────────────────────────────────────────────────────────────────
# list_proposals 端到端：有界
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_proposals_compact_is_bounded(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_props")
    c = await execute_tool("create_concept", state, canonical_name="PropSeed",
                            concept_type="method", description="s", scope="project")
    cid = c["id"]
    # 造 60 条 proposal，每条 extra 里塞一个大 candidate 数组
    for i in range(60):
        cl = await execute_tool("create_claim", state,
                                 claim_text=f"c{i}", claim_type="empirical",
                                 confidence=0.6, concept_ids=[cid],
                                 sources=[f"doi:10/{i}"], scope="project")
        await execute_tool(
            "propose", state,
            proposal_type="kb_synthesis_candidate", target_entity="concepts",
            target_id=cid, proposed_action="x",
            reasoning="padding " * 40,
            extra={"candidate_source_claim_ids": [cl["id"]] * 50},
        )

    res = await execute_tool("list_proposals", state, status="pending", limit=500)
    assert res["detail"] == "compact"
    # compact 行不含 extra 的大数组
    for row in res["proposals"]:
        assert "extra" not in row
        assert "_origin_layer" in row
    payload_chars = len(json.dumps(res, ensure_ascii=False))
    assert payload_chars < DEFAULT_RESULT_TOKEN_BUDGET * 4 + 4000, payload_chars
