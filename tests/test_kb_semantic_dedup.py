"""Phase B: state.write_kb semantic dedup + source independence.

用 DummyEmbeddingClient（确定性 hash → 假 384 维向量）取代真 sentence-transformers
模型，让测试快 + 不下载。覆盖：
  - cosine ≥ 0.95 + 新 source 独立 → merge sources + bump independent_source_count
  - 0.80-0.95 → 写新但带 warn_similar dedup_info
  - cosine < 0.80 → 当成真新，不打扰
  - HARNESS_DISABLE_SEMANTIC_DEDUP 跳过整个 path
  - 跨 project / org 查
  - 索引 signature 不匹配 → 自动 rebuild
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from core.bootstrap import bootstrap
from core.embeddings import EmbeddingClient
from core.kb_vector_index import append_one, load_index, rebuild_if_needed
from core.state import State, _SEMANTIC_INDEX_INITIALIZED


# ── 可控 DummyClient：测试场景可设 fixed vectors ────────────────────────────

class _FixedDummyClient(EmbeddingClient):
    """允许测试预先指定文本 → 向量，让 cosine 完全可控。

    text_to_vector: 显式 map；未命中 fallback hash。
    """
    model_id = "dummy:fixed-384"
    dim = 384

    def __init__(self, text_to_vector: dict[str, np.ndarray] | None = None):
        self.text_to_vector = text_to_vector or {}

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            if t in self.text_to_vector:
                v = self.text_to_vector[t]
            else:
                v = np.zeros(self.dim, dtype=np.float32)
                for ch in t:
                    v[ord(ch) % self.dim] += 1.0
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            else:
                v[0] = 1.0
            out[i] = v.astype(np.float32)
        return out


@pytest.fixture
def dedup_state(tmp_path, monkeypatch):
    """启用 semantic dedup + DummyClient 替代真 model。"""
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "0")
    # 清初始化标记，每 test 独立
    _SEMANTIC_INDEX_INITIALIZED.clear()
    # patch 全局 client
    client = _FixedDummyClient()
    monkeypatch.setattr(
        "core.embeddings.get_default_embedding_client",
        lambda: client,
    )
    bootstrap()
    state = State.new(node_type="literature", base_dir=tmp_path / "runs",
                       project_id="test_proj")
    yield state, client
    _SEMANTIC_INDEX_INITIALIZED.clear()


# ── 1. cosine < 0.80 → 真新 ────────────────────────────────────────────────

def test_low_similarity_writes_as_new(dedup_state):
    state, client = dedup_state
    # 写第一条
    r1 = {
        "claim_text": "GAP MAE > 100 on QM9 OOD",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "test stub",
        "scope": "project",
        "sources": ["doi:10.1234/example"],
        "confidence": 0.7,
    }
    final, created = state.write_kb("claims", r1)
    assert created is True
    # 写一条完全不同主题的（hash → 远向量）
    r2 = {
        "claim_text": "Transformer outperforms RNN on long context",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "test stub",
        "scope": "project",
        "sources": ["doi:10.5678/different"],
        "confidence": 0.7,
    }
    final2, created2 = state.write_kb("claims", r2)
    assert created2 is True
    # 两条都在
    all_claims = state.list_kb("claims")
    assert len(all_claims) == 2


# ── 2. cosine ≥ 0.95 + 独立 source → merge ────────────────────────────────

def test_high_similarity_merges_independent_source(dedup_state, monkeypatch):
    state, client = dedup_state
    # 给定一个固定向量，让两条 claim embed 后高度相似
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    # 让 embed_text 都映射到 v1（cosine=1.0）→ 触发 merge
    monkeypatch.setattr(client, "text_to_vector",
                          {})  # placeholder
    # 强 patch embed 让它永远返同向量（模拟 sim=1.0）
    def constant_embed(texts):
        return np.tile(v1, (len(texts), 1))
    monkeypatch.setattr(client, "embed", constant_embed)

    r1 = {
        "claim_text": "GAP MAE high on QM9 OOD",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.1/paperA"],
        "confidence": 0.7,
    }
    final1, created1 = state.write_kb("claims", r1)
    assert created1 is True

    # 写"语义同"但 source 独立（不同 paper）
    r2 = {
        "claim_text": "GAP shows high error on QM9 OOD split",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.2/paperB"],   # 独立 source
        "confidence": 0.7,
    }
    final2, created2 = state.write_kb("claims", r2)
    # 没创建新；merge 到 r1
    assert created2 is False
    assert final2["id"] == final1["id"]
    # sources 合并了
    assert "doi:10.1/paperA" in final2["sources"]
    assert "doi:10.2/paperB" in final2["sources"]
    # independent_source_count bump
    assert final2.get("independent_source_count") == 2
    # 审计 log
    assert final2.get("derived", {}).get("semantic_merge_log")
    # 跨同 project：replication_count++（v1 r1 是 project；r2 也是 project → 1 次复现）
    assert final2.get("replication_count") == 1


# ── 3. 0.80 ≤ cosine < 0.95 → 写新但带 warn_similar ────────────────────────

def test_moderate_similarity_warns_but_writes(dedup_state, monkeypatch):
    state, client = dedup_state
    # 让第二次 embed cos ≈ 0.85
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    v_mid = np.zeros(384, dtype=np.float32)
    v_mid[0] = 0.86
    v_mid[1] = 0.51   # |v_mid| ≈ 1
    v_mid = v_mid / np.linalg.norm(v_mid)

    call_count = [0]
    def staged_embed(texts):
        call_count[0] += 1
        # 第 1 调（写第一条 dedup check）→ v1
        # 第 2 调（写第一条 append_index）→ v1
        # 第 3 调（写第二条 dedup check）→ v_mid（跟 v1 cos≈0.86）
        # 第 4 调（写第二条 append_index）→ v_mid
        if call_count[0] <= 2:
            return np.tile(v1, (len(texts), 1))
        return np.tile(v_mid, (len(texts), 1))
    monkeypatch.setattr(client, "embed", staged_embed)

    r1 = {
        "claim_text": "X works on Y", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project", "sources": ["doi:10.1/src1"], "confidence": 0.7,
    }
    state.write_kb("claims", r1)

    r2 = {
        "claim_text": "X kinda works on Y similar",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project", "sources": ["doi:10.2/src2"], "confidence": 0.7,
    }
    final2, created2 = state.write_kb("claims", r2)
    # 0.80-0.95 → 写新但带 warning
    assert created2 is True
    assert final2["id"] != "src1"  # 不同 id
    info = final2.get("derived", {}).get("dedup_info")
    assert info is not None
    assert info["action"] == "warn_similar"
    assert info["candidates"][0]["cosine"] > 0.80


# ── 4. HARNESS_DISABLE_SEMANTIC_DEDUP=1 → 跳过 ────────────────────────────

def test_disable_env_skips_path(tmp_path, monkeypatch):
    """env=1 时不调 embedding，行为退到 v0.3.1。"""
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")

    # 强制：如果有人调 embedding 就报错
    def boom():
        raise AssertionError("不该调 embedding!")
    monkeypatch.setattr(
        "core.embeddings.get_default_embedding_client", boom,
    )

    state = State.new(node_type="literature", base_dir=tmp_path,
                       project_id="p")
    r = {
        "claim_text": "no dedup test", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project", "sources": ["doi:10.1/s"], "confidence": 0.5,
    }
    final, created = state.write_kb("claims", r)
    assert created is True


# ── 5. Source independence helper ────────────────────────────────────────

def test_compute_new_independent_sources():
    from core.state import State as _S
    res = _S._compute_new_independent_sources(
        ["doi:a", "doi:b"], ["doi:b", "doi:c"],
    )
    assert res == ["doi:c"]   # b 已有；c 新
    res2 = _S._compute_new_independent_sources(
        ["doi:a"], ["doi:a"],
    )
    assert res2 == []
    res3 = _S._compute_new_independent_sources(
        [], ["doi:x"],
    )
    assert res3 == ["doi:x"]


# ── 6. 同 source 不算独立 → 不 bump replication ────────────────────────────

def test_same_source_no_replication_bump(dedup_state, monkeypatch):
    state, client = dedup_state
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    monkeypatch.setattr(
        client, "embed", lambda texts: np.tile(v1, (len(texts), 1)),
    )

    r1 = {
        "claim_text": "X causes Y", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project", "sources": ["doi:same"], "confidence": 0.7,
    }
    final1, _ = state.write_kb("claims", r1)
    assert final1.get("replication_count", 0) == 0

    # 用同 source 再写一次（同语义同 source → 无新增）
    r2 = dict(r1)
    r2["claim_text"] = "X is the cause of Y"  # 字面不同，sim=1
    final2, created2 = state.write_kb("claims", r2)
    assert created2 is False
    assert final2["id"] == final1["id"]
    # source 没新；replication_count 仍 0（无新 indep source）
    assert final2.get("replication_count", 0) == 0
    # sources 不重复
    assert final2["sources"] == ["doi:same"]


# ── 7. 索引初始化 + lazy ──────────────────────────────────────────────────

def test_first_write_triggers_index_rebuild(dedup_state, monkeypatch):
    state, client = dedup_state
    # 之前 _SEMANTIC_INDEX_INITIALIZED 已被 fixture 清；首次 write 触发 rebuild
    rebuild_calls = []
    real_rebuild = rebuild_if_needed
    def tracked_rebuild(scope, project_id=None, client=None):
        rebuild_calls.append((scope, project_id))
        return real_rebuild(scope, project_id, client=client)
    monkeypatch.setattr(
        "core.kb_vector_index.rebuild_if_needed", tracked_rebuild,
    )

    r = {
        "claim_text": "First write", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project", "sources": ["doi:10.1/s"], "confidence": 0.5,
    }
    state.write_kb("claims", r)
    # 应触发 org + project rebuild
    scopes = [c[0] for c in rebuild_calls]
    assert "org" in scopes
    assert "project" in scopes


# ── 8. Concept 也走 semantic dedup ────────────────────────────────────────

def test_high_cosine_but_different_scope_does_NOT_merge(dedup_state, monkeypatch):
    """Critical: 字面同 cosine 高但 scope 不同 → 必须不 merge（防真模型误判）。

    Real model 上 multilingual-e5-small 对"字面同 scope 不同"测出 cosine=0.987，
    所以 _safe_to_auto_merge 必须 fall back 看 scope_dimensions。
    """
    state, client = dedup_state
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    # 让所有 embed 返同向量（模拟字面同 → cosine=1.0）
    monkeypatch.setattr(client, "embed",
                          lambda texts: np.tile(v1, (len(texts), 1)))

    r1 = {
        "claim_text": "Model performs poorly on validation",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.1/a"],
        "scope_dimensions": {"dataset": "QM9", "regime": "OOD"},
        "confidence": 0.6,
    }
    final1, c1 = state.write_kb("claims", r1)
    assert c1 is True

    # 字面同但 dataset 不同 → 必须不 merge
    r2 = {
        "claim_text": "Model performs poorly on validation",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.2/b"],
        "scope_dimensions": {"dataset": "RMD17", "regime": "OOD"},
        "confidence": 0.6,
    }
    final2, c2 = state.write_kb("claims", r2)
    assert c2 is True, "scope 不同时 cosine=1 也不该 merge"
    assert final2["id"] != final1["id"]
    # 应当带 warn_similar dedup_info
    info = (final2.get("derived") or {}).get("dedup_info")
    assert info is not None
    assert info["action"] == "warn_similar"


def test_high_cosine_different_claim_type_does_NOT_merge(dedup_state, monkeypatch):
    """字面同 cosine 高但 claim_type 不同（empirical vs methodological）→ 不 merge。"""
    state, client = dedup_state
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    monkeypatch.setattr(client, "embed",
                          lambda texts: np.tile(v1, (len(texts), 1)))

    r1 = {
        "claim_text": "Method M improves baseline by 10%",
        "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub",
        "scope": "project",
        "sources": ["doi:10.1/a"],
        "confidence": 0.7,
    }
    final1, c1 = state.write_kb("claims", r1)

    r2 = dict(r1)
    r2["claim_type"] = "methodological"
    r2["sources"] = ["doi:10.1/b", "doi:10.2/c"]  # methodological 要 ≥2 sources（Phase C）
    final2, c2 = state.write_kb("claims", r2)
    # claim_type 不同 → 不 merge
    assert c2 is True
    assert final2["id"] != final1["id"]


def test_concepts_also_get_semantic_dedup(dedup_state, monkeypatch):
    state, client = dedup_state
    v1 = np.zeros(384, dtype=np.float32)
    v1[0] = 1.0
    monkeypatch.setattr(client, "embed",
                          lambda texts: np.tile(v1, (len(texts), 1)))

    # concept 1
    c1 = {"canonical_name": "GAP", "concept_type": "method",
          "description": "ML potential"}
    final1, created1 = state.write_kb("concepts", c1)
    # 第二个 concept 描述不同但 embedding 同 → 应 merge
    c2 = {"canonical_name": "GAP method",
          "concept_type": "method",
          "description": "Same method different name"}
    final2, created2 = state.write_kb("concepts", c2)
    # concept dedup: cosine=1, merge_into existing
    assert created2 is False
    # ID 同 final1
    assert final2["id"] == final1["id"]
