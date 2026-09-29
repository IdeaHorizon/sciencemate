"""E2E: A-H 全 phase 串起来的集成场景。

模拟：写多条 KB → semantic dedup 命中 merge → context engine 注入含 caveat →
producing artifact 含 disagreement → curator scan → propose → dreaming triggered。

不联网；DummyEmbeddingClient 替代真 sentence-transformers。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import numpy as np
import pytest

from core.bootstrap import bootstrap
from core.embeddings import EmbeddingClient
from core.state import State, _SEMANTIC_INDEX_INITIALIZED


class _ControlEmbed(EmbeddingClient):
    """让测试完全控制 cosine 关系：固定向量映射。"""
    model_id = "test:control-384"
    dim = 384

    def __init__(self, fixed_vec: np.ndarray | None = None):
        v = fixed_vec if fixed_vec is not None else np.zeros(384, dtype=np.float32)
        if fixed_vec is None:
            v[0] = 1.0
        n = np.linalg.norm(v)
        self.fixed = (v / n).astype(np.float32) if n > 0 else v

    def embed(self, texts):
        return np.tile(self.fixed, (len(texts), 1))


@pytest.fixture
def e2e_state(tmp_path, monkeypatch):
    """启用所有 v0.3.2+ phase；DummyClient 替代真 model。"""
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "0")
    _SEMANTIC_INDEX_INITIALIZED.clear()
    client = _ControlEmbed()
    monkeypatch.setattr(
        "core.embeddings.get_default_embedding_client",
        lambda: client,
    )
    bootstrap()
    state = State.new(node_type="literature",
                       base_dir=tmp_path / "runs",
                       project_id="e2e_test")
    yield state, client
    _SEMANTIC_INDEX_INITIALIZED.clear()


@pytest.mark.asyncio
async def test_full_phase_pipeline(e2e_state):
    """完整 flow：write → dedup → context inject → scan → propose → pending mark.

    Steps：
      1. 写第 1 条 empirical claim (A)
      2. 写第 2 条字面不同但 embedding 同的 claim (B) → 应 merge 到 A，bump indep_sources
      3. 写一个 dead_end claim → dreaming pending mark
      4. context engine 注入含 caveat（KB heuristic rule）
      5. producing 节点 artifact 含 disagreement → curator scan → propose
    """
    state, client = e2e_state

    # ── Step 1+2: write + auto-merge ───────────────────────────────────────
    rec_a = {
        "claim_text": "Method M fails on dataset D in OOD",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.1/paperA"],
        "confidence": 0.7,
    }
    final_a, created_a = state.write_kb("claims", rec_a)
    assert created_a is True
    assert final_a.get("independent_source_count") in (None, 1)

    # 字面不同但 embedding 同 → merge
    rec_b = {
        "claim_text": "On D's OOD split, method M shows large failure",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.2/paperB"],   # 独立 source
        "confidence": 0.7,
    }
    final_b, created_b = state.write_kb("claims", rec_b)
    assert created_b is False
    assert final_b["id"] == final_a["id"]
    assert len(final_b["sources"]) == 2
    assert final_b["independent_source_count"] == 2
    # 1 次跨同 project 复现 → replication_count = 1
    assert final_b.get("replication_count") == 1

    # ── Step 3: dead_end → pending dreaming ────────────────────────────────
    rec_dead = {
        "claim_text": "Path X is a dead-end for problem Y",
        "claim_type": "dead_end",
        "dont_repeat_reason": "expressivity bottleneck",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"},
        "sources": ["doi:10.3/paperC"],
        "confidence": 0.8,
    }
    # 给个不同向量避免触发 merge 到 A
    other_v = np.zeros(384, dtype=np.float32)
    other_v[100] = 1.0
    client.fixed = other_v
    final_dead, created_dead = state.write_kb("claims", rec_dead)
    assert created_dead is True
    from core.dreaming_scheduler import read_pending
    pending = read_pending("e2e_test")
    assert pending is not None
    assert any("dead_end" in r for r in pending["reasons"])

    # ── Step 4: context engine 注入含 caveat ──────────────────────────────
    from core.context_engine import build_messages
    from core.loader import load_harness
    h = load_harness("literature")
    msgs = build_messages(h, state, {})
    sys_text = msgs[0].content or ""
    user_text = msgs[1].content or ""
    # KB 启发式 rule 注入 system prompt（Phase E）
    assert "KB 使用启发式" in sys_text
    assert "I disagree with claim_" in sys_text
    # KB 状态注入 user prompt + caveat 标签（Phase D）
    assert "历史记录参考" in user_text or "ground truth" in user_text
    # claim_a / claim_dead 都该在注入（at least 1 命中）
    assert (final_a["id"] in user_text) or (final_dead["id"] in user_text)

    # ── Step 5: artifact 含 disagreement → scan → propose ─────────────────
    art_content = (
        f"Analysis report:\n\n"
        f"Result supports the new direction.\n"
        f"I disagree with {final_a['id']} because scope (this experiment uses "
        f"dataset E, not D)."
    )
    art = state.save_artifact("analysis_report", "test_e2e", art_content)
    from core.tool_registry import execute as execute_tool
    scan_res = await execute_tool(
        "scan_artifact_disagreements", state,
        artifact_ids=[art["id"]], auto_propose=True,
    )
    assert scan_res["status"] == "success"
    assert scan_res["n_findings"] >= 1
    assert scan_res["proposals_created"] >= 1
    # 找到的 claim_id 是 final_a
    assert any(f["claim_id"] == final_a["id"] for f in scan_res["found"])


@pytest.mark.asyncio
async def test_kb_heuristic_rule_appears_for_all_producing_nodes(tmp_path):
    """Phase E：所有 producing 节点 system_prompt 都自动注入 KB rule。"""
    bootstrap()
    from core.context_engine import build_messages
    from core.loader import load_harness

    for node_type in ("literature", "hypothesis", "data", "experiment",
                      "postprocess", "writing",
                      "_curator", "_reviewer"):
        state = State.new(node_type=node_type,
                           base_dir=tmp_path / f"r_{node_type}")
        h = load_harness(node_type)
        msgs = build_messages(h, state, {})
        sys_text = msgs[0].content or ""
        assert "KB 使用启发式" in sys_text, \
            f"节点 {node_type} system prompt 缺 KB 启发式 rule"

    # orchestrator 不该有
    state_orch = State.new(node_type="_orchestrator", base_dir=tmp_path / "r_orch")
    h_orch = load_harness("_orchestrator")
    msgs_orch = build_messages(h_orch, state_orch, {})
    assert "KB 使用启发式" not in (msgs_orch[0].content or "")


def test_project_scope_accepts_single_source_methodological(tmp_path):
    """project 层是零门禁工作记忆 —— 单来源的方法学结论必须写得进去。

    墓碑：原 `test_phase_c_mechanical_threshold_blocks_theoretical_with_one_source`
    守的是 independent_source_count ≥ 2 写入闸。闸已删（P5）：它的报错原文在教模型
    「证据不够请用 claim_type='empirical' 先写」，而 claim_type 现在是晋升分道的
    路由键 —— 闸没挡住坏知识，只是把它换个名字塞进错的道。准入整体移到晋升侧
    （三项机械检查 + 判断类走人工批次）。
    """
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="zero_gate_project")
    written, _ = state.write_kb("claims", {
        "claim_text": "网格加密到 128 以下时有限尺度效应会吃掉临界指数",
        "claim_type": "methodological",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.1/single"],
        "confidence": 0.6,
    })
    assert written["id"].startswith("claim_")
    assert state.get_kb_record("claims", written["id"]) is not None


def test_cross_project_promote_gate_is_gone(tmp_path):
    """墓碑：`promoting_from_project` 闸（replication_count ≥ 3 + confidence ≥ 0.85）。

    删它的判据不是「太严」，是**它从来没响过** —— 全仓零个写入方设过这个触发
    字段。且真接上会永久 fail-closed：P2 晋升器首次晋升写 replication_count = 1。
    复现次数由 dreaming 跨项目归并累加，是**结果**不是准入条件。
    """
    from shared.lib import kb_schema

    src = inspect.getsource(kb_schema)
    assert "HIGH_TIER_CLAIM_TYPES = {" not in src
    assert 'record.get("promoting_from_project") is True' not in src

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="promote_gate_test")
    written, _ = state.write_kb("claims", {
        "claim_text": "低温段的自旋翻转接受率必须按温度重标定",
        "claim_type": "methodological",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "org",
        "promoted_from": {"project_id": "p_prev", "source_id": "claim_deadbeef1234",
                          "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
        "promoting_from_project": True,
        "sources": ["doi:10.1/single"],
        "confidence": 0.6,
        "replication_count": 1,
    })
    assert written["id"].startswith("claim_")
