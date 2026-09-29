"""KB vector index —— .npy + manifest + ids.json 三件套。

物理布局：
  ~/.harness-framework/{org,projects/<id>}/kb_embeddings/
    ├── manifest.json     # {model_id, template_version, dim, entities: {claims: {count, built_at}}}
    ├── claims.vectors.npy     # (N, dim) float32 L2-normalized
    ├── claims.ids.json        # [id1, id2, ...] 行对齐
    ├── concepts.vectors.npy
    └── concepts.ids.json

不进 jsonl：KB record 零侵入。.npy 是 derived state，rebuild_all 永远 reproducible
（jsonl 是 source of truth）。

启动时调 `should_rebuild(scope_root)` 比 manifest signature vs 当前 code signature；
不匹配 → rebuild_all。

并发：shared.lib.filelock 文件锁（跟 KB jsonl 一致）。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

from core.embeddings import EmbeddingClient, get_default_embedding_client
from core.kb_embedding_text import template_signature
from core.paths import org_root as _paths_org_root, project_dir as _paths_project_dir
from shared.lib import filelock

log = logging.getLogger("kb_vector_index")


SUPPORTED_ENTITIES = ("claims", "concepts")


# ── 路径 helper ────────────────────────────────────────────────────────────

def _scope_embeddings_dir(scope: str, project_id: str | None = None) -> Path:
    """`<scope_root>/kb_embeddings/` —— org 或某 project。"""
    if scope == "org":
        base = _paths_org_root()
    elif scope == "project":
        if not project_id:
            raise ValueError("project scope 要 project_id")
        base = _paths_project_dir(project_id)
        if base is None:
            raise ValueError(f"project_id={project_id!r} 路径无效")
    else:
        raise ValueError(f"scope must be 'org' or 'project', got {scope!r}")
    d = base / "kb_embeddings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(scope: str, project_id: str | None = None) -> Path:
    return _scope_embeddings_dir(scope, project_id) / "manifest.json"


def _vectors_path(entity: str, scope: str, project_id: str | None = None) -> Path:
    return _scope_embeddings_dir(scope, project_id) / f"{entity}.vectors.npy"


def _ids_path(entity: str, scope: str, project_id: str | None = None) -> Path:
    return _scope_embeddings_dir(scope, project_id) / f"{entity}.ids.json"


# ── File lock（跟 state.py 一致）──────────────────────────────────────────

@contextlib.contextmanager
def _file_lock(path: Path):
    with filelock.exclusive(path.with_suffix(path.suffix + ".lock")):
        yield


# ── Manifest read / write ──────────────────────────────────────────────────

def read_manifest(scope: str, project_id: str | None = None) -> dict | None:
    p = _manifest_path(scope, project_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("manifest %s read failed: %s", p, e)
        return None


def write_manifest(manifest: dict, scope: str,
                    project_id: str | None = None) -> None:
    p = _manifest_path(scope, project_id)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def expected_signature(client: EmbeddingClient | None = None) -> str:
    """当前 code 的 signature。manifest 不匹配 → rebuild_all。

    格式：<model_id>+<template_signature>
    """
    c = client or get_default_embedding_client()
    return f"{c.model_id}+{template_signature()}"


def should_rebuild(scope: str, project_id: str | None = None,
                    client: EmbeddingClient | None = None) -> bool:
    """启动时调：manifest 缺失或 signature 不匹配。

    True → 调 rebuild_all_for_scope。
    """
    m = read_manifest(scope, project_id)
    if m is None:
        return True
    sig = m.get("signature")
    return sig != expected_signature(client)


# ── Vectors + ids 读写 ────────────────────────────────────────────────────

def load_index(entity: str, scope: str,
                project_id: str | None = None) -> tuple[np.ndarray, list[str]]:
    """读 (vectors, ids)。空索引返 ((0, dim) ndarray, [])。

    dim 来自 manifest（若 vectors.npy 也存在则 prefer 它）。
    """
    if entity not in SUPPORTED_ENTITIES:
        raise ValueError(f"entity={entity!r} 不支持 ({SUPPORTED_ENTITIES})")
    v_path = _vectors_path(entity, scope, project_id)
    i_path = _ids_path(entity, scope, project_id)
    if not v_path.exists() or not i_path.exists():
        m = read_manifest(scope, project_id)
        dim = (m or {}).get("dim", 384)
        return np.zeros((0, dim), dtype=np.float32), []
    vectors = np.load(v_path)
    ids = json.loads(i_path.read_text(encoding="utf-8"))
    if vectors.shape[0] != len(ids):
        log.warning(
            "%s vectors/ids 长度不一致 (%d vs %d) —— 触发 rebuild",
            entity, vectors.shape[0], len(ids),
        )
        # 返空让 caller 决定 rebuild
        m = read_manifest(scope, project_id)
        dim = (m or {}).get("dim", 384)
        return np.zeros((0, dim), dtype=np.float32), []
    return vectors, ids


def _save_index_unlocked(entity: str, vectors: np.ndarray, ids: list[str],
                           scope: str, project_id: str | None = None) -> None:
    """内部：原子写 vectors + ids，**不加锁**（caller 必须已持锁）。"""
    if entity not in SUPPORTED_ENTITIES:
        raise ValueError(f"entity={entity!r} 不支持")
    if vectors.shape[0] != len(ids):
        raise ValueError(
            f"vectors row {vectors.shape[0]} != len(ids) {len(ids)}"
        )
    v_path = _vectors_path(entity, scope, project_id)
    i_path = _ids_path(entity, scope, project_id)
    v_tmp = v_path.with_suffix(v_path.suffix + ".tmp")
    # 用文件 handle 写：避免 np.save 看到 str 路径自动加 .npy 后缀
    with open(v_tmp, "wb") as f:
        np.save(f, vectors.astype(np.float32, copy=False))
    v_tmp.replace(v_path)
    i_tmp = i_path.with_suffix(i_path.suffix + ".tmp")
    i_tmp.write_text(
        json.dumps(ids, ensure_ascii=False),
        encoding="utf-8",
    )
    i_tmp.replace(i_path)


def save_index(entity: str, vectors: np.ndarray, ids: list[str],
                scope: str, project_id: str | None = None) -> None:
    """原子写 vectors + ids（含锁）。无并发 caller 调这个。"""
    with _file_lock(_vectors_path(entity, scope, project_id)):
        _save_index_unlocked(entity, vectors, ids, scope, project_id)


def append_one(entity: str, kb_id: str, vector: np.ndarray,
                scope: str, project_id: str | None = None) -> None:
    """追加一个 entity 到索引。已存在 id → 替换那行。

    向量假定已 L2-normalized（caller 保证）。
    """
    if vector.ndim != 1:
        raise ValueError(f"vector should be 1D, got shape {vector.shape}")
    with _file_lock(_vectors_path(entity, scope, project_id)):
        vectors, ids = load_index(entity, scope, project_id)
        if kb_id in ids:
            idx = ids.index(kb_id)
            vectors[idx] = vector
        else:
            ids.append(kb_id)
            if vectors.shape[0] == 0:
                vectors = vector.reshape(1, -1).astype(np.float32)
            else:
                vectors = np.vstack([vectors, vector.reshape(1, -1)])
        _save_index_unlocked(entity, vectors, ids, scope, project_id)


def append_many(entity: str, items: list[tuple[str, np.ndarray]],
                 scope: str, project_id: str | None = None) -> None:
    """批量 append。比循环 append_one 高效（一次 load + 一次 save）。"""
    if not items:
        return
    with _file_lock(_vectors_path(entity, scope, project_id)):
        vectors, ids = load_index(entity, scope, project_id)
        id_to_idx = {kid: i for i, kid in enumerate(ids)}
        new_vecs: list[np.ndarray] = []
        new_ids: list[str] = []
        for kid, vec in items:
            if vec.ndim != 1:
                raise ValueError(f"vector for {kid} not 1D")
            if kid in id_to_idx:
                vectors[id_to_idx[kid]] = vec.astype(np.float32)
            else:
                new_vecs.append(vec.reshape(1, -1).astype(np.float32))
                new_ids.append(kid)
        if new_vecs:
            new_block = np.vstack(new_vecs)
            if vectors.shape[0] == 0:
                vectors = new_block
            else:
                vectors = np.vstack([vectors, new_block])
            ids = ids + new_ids
        _save_index_unlocked(entity, vectors, ids, scope, project_id)


# ── Query ──────────────────────────────────────────────────────────────────

def query(
    entity: str,
    query_vector: np.ndarray,
    top_k: int,
    *,
    scope: str = "org",
    project_id: str | None = None,
    exclude_ids: Iterable[str] = (),
    min_cosine: float = 0.0,
) -> list[tuple[str, float]]:
    """查 top-K 相似 entity。返 [(kb_id, cosine), ...] 按 cosine 降。

    query_vector 必须已 L2-normalized。
    exclude_ids 过滤掉（写 dedup 时排自己）。
    min_cosine 阈值（小于不返）。
    """
    vectors, ids = load_index(entity, scope, project_id)
    if vectors.shape[0] == 0:
        return []
    if query_vector.ndim != 1 or query_vector.shape[0] != vectors.shape[1]:
        raise ValueError(
            f"query_vector shape {query_vector.shape} 不匹配 index dim {vectors.shape[1]}"
        )
    sims = vectors @ query_vector   # (N,) cosine (假定 normalized)
    exclude = set(exclude_ids)
    # mask
    out: list[tuple[str, float]] = []
    # 用 argpartition 取 top_k 比全 sort 快
    k = min(top_k * 2, sims.shape[0])     # 多取些以扣除 exclude
    if k <= 0:
        return []
    top_idx = np.argpartition(-sims, k - 1)[:k]
    top_idx = top_idx[np.argsort(-sims[top_idx])]
    for i in top_idx:
        kid = ids[int(i)]
        if kid in exclude:
            continue
        s = float(sims[int(i)])
        if s < min_cosine:
            continue
        out.append((kid, s))
        if len(out) >= top_k:
            break
    return out


def query_across_scopes(
    entity: str,
    query_vector: np.ndarray,
    top_k: int,
    *,
    project_id: str | None = None,
    exclude_ids: Iterable[str] = (),
    min_cosine: float = 0.0,
) -> list[tuple[str, float, str]]:
    """跨 org + project 查。返 [(kb_id, cosine, scope), ...]。

    用法：write_kb 时 dedup 想跨 scope 查（因为 jsonl list_kb 也跨 scope 取）。
    """
    out: list[tuple[str, float, str]] = []
    for sc in ("project", "org"):
        if sc == "project" and project_id is None:
            continue
        try:
            hits = query(
                entity, query_vector, top_k,
                scope=sc, project_id=project_id,
                exclude_ids=exclude_ids, min_cosine=min_cosine,
            )
        except Exception as e:
            log.warning("query %s scope=%s failed: %s", entity, sc, e)
            continue
        for kid, sim in hits:
            out.append((kid, sim, sc))
    out.sort(key=lambda x: -x[1])
    return out[:top_k]


# ── Rebuild ────────────────────────────────────────────────────────────────

def rebuild_scope(
    scope: str,
    project_id: str | None = None,
    *,
    client: EmbeddingClient | None = None,
) -> dict:
    """从 jsonl 全量重 embed。manifest 自动刷。

    返：stats dict {entities: {claims: count, concepts: count}}
    """
    from core.kb_embedding_text import (
        claim_to_embed_text, concept_to_embed_text,
    )
    from core.paths import org_root, project_dir

    emb_client = client or get_default_embedding_client()
    sig = expected_signature(emb_client)
    log.info("Rebuilding KB embeddings for scope=%s project_id=%s signature=%s",
              scope, project_id, sig)

    if scope == "org":
        base = org_root()
    else:
        base = project_dir(project_id)
        if base is None:
            raise ValueError(f"project_id={project_id!r} invalid")

    def _read_jsonl_file(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    concepts = _read_jsonl_file(base / "kb_concepts.jsonl")
    claims = _read_jsonl_file(base / "kb_claims.jsonl")

    # concept lookup helper（跨 scope —— 因 concept 默认 org）
    # 重建任一 scope 都需要看 org+project concepts 来解 concept_id → name
    all_concepts: dict[str, dict] = {}
    org_concepts_file = _paths_org_root() / "kb_concepts.jsonl"
    if scope != "org":   # 项目 scope 时也要读 org concepts
        for c_rec in _read_jsonl_file(org_concepts_file):
            all_concepts[c_rec.get("id")] = c_rec
    for c_rec in concepts:
        all_concepts[c_rec.get("id")] = c_rec

    def concept_lookup(cid: str):
        return all_concepts.get(cid)

    claim_by_id = {cl.get("id"): cl for cl in claims}

    def claim_lookup(cid: str):
        return claim_by_id.get(cid)

    stats: dict[str, int] = {}

    # Concepts
    if concepts:
        texts = [concept_to_embed_text(rec) for rec in concepts]
        vecs = emb_client.embed(texts)
        ids = [rec["id"] for rec in concepts]
        save_index("concepts", vecs, ids, scope, project_id)
        stats["concepts"] = len(ids)
    else:
        save_index("concepts", np.zeros((0, emb_client.dim), dtype=np.float32), [],
                    scope, project_id)
        stats["concepts"] = 0

    # Claims
    if claims:
        texts = [
            claim_to_embed_text(
                cl,
                concept_lookup=concept_lookup,
                claim_lookup=claim_lookup,
            )
            for cl in claims
        ]
        vecs = emb_client.embed(texts)
        ids = [cl["id"] for cl in claims]
        save_index("claims", vecs, ids, scope, project_id)
        stats["claims"] = len(ids)
    else:
        save_index("claims", np.zeros((0, emb_client.dim), dtype=np.float32), [],
                    scope, project_id)
        stats["claims"] = 0

    # 写 manifest
    manifest = {
        "model_id": emb_client.model_id,
        "template_signature": template_signature(),
        "signature": sig,
        "dim": emb_client.dim,
        "entities": {
            ent: {"count": stats.get(ent, 0),
                   "built_at": datetime.now(timezone.utc).isoformat()}
            for ent in SUPPORTED_ENTITIES
        },
    }
    write_manifest(manifest, scope, project_id)
    log.info("Rebuild done for scope=%s: %s", scope, stats)
    return stats


def rebuild_if_needed(scope: str, project_id: str | None = None,
                       client: EmbeddingClient | None = None) -> bool:
    """主流程入口：检查 signature，不匹配则 rebuild。

    返 True 表示真做了 rebuild。
    """
    if not should_rebuild(scope, project_id, client):
        return False
    rebuild_scope(scope, project_id, client=client)
    return True
