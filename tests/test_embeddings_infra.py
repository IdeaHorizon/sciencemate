"""Phase A 测试：embeddings + kb_embedding_text + kb_vector_index。

不强依赖 sentence-transformers 模型（mock 一个轻量 EmbeddingClient 跑测试）。
真实 sentence-transformers 跑作为 opt-in smoke（HARNESS_RUN_REAL_EMBED=1 启用）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from core.embeddings import (
    EmbeddingClient, cosine_matrix, cosine_similarity,
)
from core.kb_embedding_text import (
    CLAIM_EMBED_TEMPLATE_VERSION, claim_to_embed_text, concept_to_embed_text,
    template_signature,
)


# ── Mock EmbeddingClient ────────────────────────────────────────────────────

class _DummyEmbeddingClient(EmbeddingClient):
    """确定性 hash-based 假向量。L2-normalized。dim=8."""
    model_id = "dummy:hash-8"
    dim = 8

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            # 简易 hash 映 8 维：每个字符 ord % 8 加 1
            h = np.zeros(self.dim, dtype=np.float32)
            for ch in t:
                h[ord(ch) % self.dim] += 1.0
            n = np.linalg.norm(h)
            if n > 0:
                h = h / n
            else:
                h[0] = 1.0
            out[i] = h
        return out


# ── Cosine helpers ─────────────────────────────────────────────────────────

def test_cosine_similarity_1d():
    a = np.array([1.0, 0.0])
    b = np.array([0.7071067, 0.7071067])
    s = cosine_similarity(a, b)
    assert abs(s - 0.7071067) < 1e-5


def test_cosine_similarity_matrix():
    a = np.array([1.0, 0.0])
    b = np.array([[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]])
    sims = cosine_similarity(a, b)
    assert sims.shape == (3,)
    assert abs(sims[0] - 1.0) < 1e-5
    assert abs(sims[1]) < 1e-5
    assert abs(sims[2] - 0.7071) < 1e-3


def test_cosine_matrix_batch():
    a = np.array([[1.0, 0.0], [0.0, 1.0]])
    b = np.array([[1.0, 0.0], [0.0, 1.0]])
    m = cosine_matrix(a, b)
    assert m.shape == (2, 2)
    assert abs(m[0, 0] - 1.0) < 1e-5
    assert abs(m[0, 1]) < 1e-5
    assert abs(m[1, 1] - 1.0) < 1e-5


# ── DummyEmbeddingClient ───────────────────────────────────────────────────

def test_dummy_client_basic():
    c = _DummyEmbeddingClient()
    vecs = c.embed(["hello", "world"])
    assert vecs.shape == (2, 8)
    # 归一化
    for row in vecs:
        assert abs(np.linalg.norm(row) - 1.0) < 1e-5


def test_dummy_client_empty():
    c = _DummyEmbeddingClient()
    vecs = c.embed([])
    assert vecs.shape == (0, 8)


def test_dummy_client_same_text_same_vector():
    c = _DummyEmbeddingClient()
    a = c.embed(["foo"])[0]
    b = c.embed(["foo"])[0]
    assert np.allclose(a, b)


# ── Template tests ─────────────────────────────────────────────────────────

def _mock_concept_lookup(cid: str):
    table = {
        "concept_a": {"canonical_name": "GAP", "concept_type": "method"},
        "concept_b": {"canonical_name": "QM9", "concept_type": "dataset"},
        "concept_c": {"canonical_name": "OOD generalization", "concept_type": "phenomenon"},
    }
    return table.get(cid)


def test_empirical_template():
    claim = {
        "claim_text": "GAP MAE > 100 on QM9 OOD",
        "claim_type": "empirical",
        "concept_ids": ["concept_a", "concept_b"],
        "scope_dimensions": {"dataset": "QM9", "regime": "OOD", "metric": "MAE"},
    }
    text = claim_to_embed_text(claim, concept_lookup=_mock_concept_lookup)
    assert "[empirical claim]" in text
    assert "GAP MAE > 100" in text
    assert "GAP (method)" in text
    assert "QM9 (dataset)" in text
    assert "dataset=QM9" in text
    assert "regime=OOD" in text


def test_hypothesis_template_has_falsification():
    claim = {
        "claim_text": "GAP improves with more data",
        "claim_type": "hypothesis",
        "predicted_outcome": "MAE halves at 10x training data",
        "falsification_criteria_structured": {
            "metric": "MAE", "comparison": "<", "threshold": 50, "dataset": "QM9",
        },
        "falsification_criteria_text": "若 10x 数据后 MAE 仍 ≥ 50 则 refuted",
        "concept_ids": ["concept_a"],
    }
    text = claim_to_embed_text(claim, concept_lookup=_mock_concept_lookup)
    assert "[hypothesis claim]" in text
    assert "MAE < 50 on QM9" in text
    assert "MAE halves" in text


def test_dead_end_template_emphasizes_reason():
    claim = {
        "claim_text": "GAP on QM9 OOD fails",
        "claim_type": "dead_end",
        "dont_repeat_reason": "expressiveness 不够 + SOAP 也无改进",
        "concept_ids": ["concept_a", "concept_b"],
        "scope_dimensions": {"dataset": "QM9"},
    }
    text = claim_to_embed_text(claim, concept_lookup=_mock_concept_lookup)
    assert "[dead_end claim]" in text
    assert "why_dont_repeat" in text
    assert "expressiveness 不够" in text


def test_synthesis_template_expands_sources():
    target = {
        "claim_text": "Multiple methods fail on QM9 OOD",
        "claim_type": "synthesis",
        "source_claim_ids": ["claim_x", "claim_y"],
        "concept_ids": ["concept_b"],
    }
    sources_table = {
        "claim_x": {"claim_text": "GAP MAE high on QM9 OOD"},
        "claim_y": {"claim_text": "SOAP+GAP no improvement"},
    }
    text = claim_to_embed_text(
        target, concept_lookup=_mock_concept_lookup,
        claim_lookup=lambda i: sources_table.get(i),
    )
    assert "[synthesis claim]" in text
    assert "GAP MAE high" in text
    assert "SOAP+GAP" in text


def test_methodological_template():
    claim = {
        "claim_text": "Use NHC thermostat for NVT MD",
        "claim_type": "methodological",
        "concept_ids": [],
    }
    text = claim_to_embed_text(claim, concept_lookup=_mock_concept_lookup)
    assert "[methodological claim]" in text
    assert "NHC thermostat" in text


def test_fallback_for_unknown_claim_type():
    claim = {
        "claim_text": "Energy is conserved",
        "claim_type": "theoretical",
        "concept_ids": [],
    }
    text = claim_to_embed_text(claim, concept_lookup=_mock_concept_lookup)
    assert "[theoretical claim]" in text


def test_concept_template_includes_aliases_and_description():
    c = {
        "canonical_name": "GAP",
        "concept_type": "method",
        "aliases": ["Gaussian Approximation Potential"],
        "description": "ML interatomic potential using Gaussian process regression",
    }
    text = concept_to_embed_text(c)
    assert "[concept]" in text
    assert "GAP" in text
    assert "Gaussian Approximation Potential" in text
    assert "Gaussian process regression" in text


def test_template_signature_consistency():
    sig = template_signature()
    assert sig == f"claim_v{CLAIM_EMBED_TEMPLATE_VERSION}+concept_v1"


def test_empirical_with_scope_changes_embedding_signal():
    """字面同 claim 但 scope 不同 → embed text 不同 → 应能区分。"""
    c1 = {
        "claim_text": "MAE > 100", "claim_type": "empirical",
        "concept_ids": [], "scope_dimensions": {"dataset": "QM9"},
    }
    c2 = {
        "claim_text": "MAE > 100", "claim_type": "empirical",
        "concept_ids": [], "scope_dimensions": {"dataset": "RMD17"},
    }
    t1 = claim_to_embed_text(c1, concept_lookup=_mock_concept_lookup)
    t2 = claim_to_embed_text(c2, concept_lookup=_mock_concept_lookup)
    assert t1 != t2
    assert "QM9" in t1
    assert "RMD17" in t2


# ── kb_vector_index tests ──────────────────────────────────────────────────

@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """临时 HARNESS_FRAMEWORK_HOME 隔离测试。"""
    h = tmp_path / "hf_home"
    h.mkdir()
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(h))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(h / "org"))
    (h / "org").mkdir()
    (h / "projects").mkdir()
    yield h


def test_index_load_empty(isolated_home):
    from core.kb_vector_index import load_index
    vecs, ids = load_index("claims", "org")
    assert vecs.shape[0] == 0
    assert ids == []


def test_index_append_one_and_query(isolated_home):
    from core.kb_vector_index import append_one, load_index, query
    v1 = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v3 = np.array([0.7, 0.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.14], dtype=np.float32)
    v3 = v3 / np.linalg.norm(v3)

    append_one("claims", "claim_a", v1, "org")
    append_one("claims", "claim_b", v2, "org")
    append_one("claims", "claim_c", v3, "org")

    vecs, ids = load_index("claims", "org")
    assert vecs.shape == (3, 8)
    assert set(ids) == {"claim_a", "claim_b", "claim_c"}

    # query
    hits = query("claims", v1, top_k=2, scope="org")
    assert hits[0][0] == "claim_a"
    assert hits[0][1] > 0.99


def test_index_append_many(isolated_home):
    from core.kb_vector_index import append_many, load_index
    items = []
    for i in range(5):
        v = np.zeros(8, dtype=np.float32)
        v[i] = 1.0
        items.append((f"claim_{i}", v))
    append_many("claims", items, "org")
    vecs, ids = load_index("claims", "org")
    assert vecs.shape == (5, 8)
    assert ids == [f"claim_{i}" for i in range(5)]


def test_index_replace_existing_id(isolated_home):
    """同 id 第二次 append → 替换那行，不重复。"""
    from core.kb_vector_index import append_one, load_index
    v1 = np.array([1.0, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v2 = np.array([0, 1.0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    append_one("claims", "claim_x", v1, "org")
    append_one("claims", "claim_x", v2, "org")
    vecs, ids = load_index("claims", "org")
    assert ids == ["claim_x"]
    assert np.allclose(vecs[0], v2)


def test_query_exclude_ids(isolated_home):
    from core.kb_vector_index import append_one, query
    v1 = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v2 = np.array([0.99, 0.01, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v2 = v2 / np.linalg.norm(v2)
    append_one("claims", "claim_a", v1, "org")
    append_one("claims", "claim_b", v2, "org")
    hits = query("claims", v1, top_k=5, scope="org", exclude_ids=["claim_a"])
    assert all(h[0] != "claim_a" for h in hits)
    assert hits[0][0] == "claim_b"


def test_query_min_cosine_filter(isolated_home):
    from core.kb_vector_index import append_one, query
    v1 = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v_far = np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    append_one("claims", "near", v1, "org")
    append_one("claims", "far", v_far, "org")
    hits = query("claims", v1, top_k=5, scope="org", min_cosine=0.5)
    assert len(hits) == 1
    assert hits[0][0] == "near"


def test_query_across_scopes(isolated_home):
    from core.kb_vector_index import append_one, query_across_scopes
    v1 = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v2 = np.array([0.95, 0.31, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    v2 = v2 / np.linalg.norm(v2)
    append_one("claims", "org_claim", v1, "org")
    append_one("claims", "proj_claim", v2, "project", project_id="p1")
    hits = query_across_scopes(
        "claims", v1, top_k=5, project_id="p1", min_cosine=0.5,
    )
    assert len(hits) == 2
    # scope 标记
    scopes = {h[2] for h in hits}
    assert "org" in scopes
    assert "project" in scopes


def test_manifest_signature_roundtrip(isolated_home):
    from core.kb_vector_index import (
        expected_signature, read_manifest, should_rebuild,
        write_manifest,
    )
    # 初始 no manifest → should_rebuild=True
    client = _DummyEmbeddingClient()
    assert should_rebuild("org", client=client)
    # 写一个对的 manifest → should_rebuild=False
    write_manifest({
        "signature": expected_signature(client),
        "model_id": client.model_id,
        "dim": client.dim,
    }, "org")
    assert not should_rebuild("org", client=client)
    # 改 signature → should_rebuild=True
    write_manifest({"signature": "wrong+sig"}, "org")
    assert should_rebuild("org", client=client)


def test_rebuild_scope_from_jsonl(isolated_home):
    """jsonl 现有 KB → rebuild_scope 算出 vectors + manifest。"""
    from core.kb_vector_index import (
        load_index, read_manifest, rebuild_scope,
    )
    # 造 jsonl
    org = isolated_home / "org"
    (org / "kb_concepts.jsonl").write_text(
        json.dumps({
            "id": "concept_gap", "canonical_name": "GAP",
            "concept_type": "method", "description": "ML potential",
        }) + "\n", encoding="utf-8",
    )
    (org / "kb_claims.jsonl").write_text(
        json.dumps({
            "id": "claim_1", "claim_text": "GAP MAE > 100 on QM9 OOD",
            "claim_type": "empirical", "concept_ids": ["concept_gap"],
            "scope_dimensions": {"dataset": "QM9"},
        }) + "\n", encoding="utf-8",
    )

    client = _DummyEmbeddingClient()
    stats = rebuild_scope("org", client=client)
    assert stats["claims"] == 1
    assert stats["concepts"] == 1

    vecs, ids = load_index("claims", "org")
    assert ids == ["claim_1"]
    assert vecs.shape == (1, 8)

    m = read_manifest("org")
    assert m["dim"] == 8
    assert "signature" in m


def test_rebuild_if_needed_idempotent(isolated_home):
    from core.kb_vector_index import (
        rebuild_if_needed, expected_signature, read_manifest,
    )
    # 空 KB 也应能 rebuild
    client = _DummyEmbeddingClient()
    assert rebuild_if_needed("org", client=client)        # 首次需要
    assert not rebuild_if_needed("org", client=client)    # 再调不需要


# ── opt-in: 真实 sentence-transformers smoke ──────────────────────────────

@pytest.mark.skipif(
    os.getenv("HARNESS_RUN_REAL_EMBED") != "1",
    reason="set HARNESS_RUN_REAL_EMBED=1 to run real model smoke (会下载 ~120MB)",
)
def test_real_sentence_transformers_smoke():
    """真模型 smoke：跑 sentence-transformers 真 embed 短文本，验证 dim + 归一化。

    set HARNESS_RUN_REAL_EMBED=1 启用。首次下载 ~120MB（之后缓存）。
    """
    from core.embeddings import SentenceTransformersClient
    c = SentenceTransformersClient()
    vecs = c.embed(["hello world", "你好 世界"])
    assert vecs.shape == (2, c.dim)
    # L2 归一化
    for row in vecs:
        assert abs(np.linalg.norm(row) - 1.0) < 1e-3
    # 中英语义相似的句子 cosine 应较高
    en_zh = c.embed(["dog is an animal", "狗是一种动物"])
    sim = float(en_zh[0] @ en_zh[1])
    assert sim > 0.5, f"中英同义 cosine 太低: {sim}"


# ── TOKENIZERS_PARALLELISM（huggingface tokenizers fork 警告刷屏）─────────────
#
# 根因：core/embeddings.py 是仓库里唯一 import sentence_transformers 的地方；
# 该库底层的 tokenizers（Rust 扩展）首次用到会起并行线程池，之后进程只要
# fork/spawn 子进程（run_bash/execute_python/compile_latex 等工具，chat.py
# 一个 session 里几乎必然发生）就会打一遍"detected fork...disabling
# parallelism"警告——不是错误，但重复刷屏。修法是 import 这个模块时就把
# TOKENIZERS_PARALLELISM 设成 false，连触发条件都不成立。


def test_embeddings_module_sets_tokenizers_parallelism_false(monkeypatch):
    """import core.embeddings 必须把 TOKENIZERS_PARALLELISM 设为 false
    （不设 -> 每次 fork 子进程都刷一遍 huggingface tokenizers 警告）。"""
    import importlib
    import core.embeddings as embeddings_module

    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    importlib.reload(embeddings_module)
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "false"


def test_embeddings_module_does_not_override_explicit_user_setting(monkeypatch):
    """用户自己显式设了（比如真想要并行）-> 不覆盖，setdefault 语义。"""
    import importlib
    import core.embeddings as embeddings_module

    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    importlib.reload(embeddings_module)
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "true"
