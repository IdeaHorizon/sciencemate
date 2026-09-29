"""KB Typed Edge 系统：双向存储 + 反驳传播。

存在 `kb_edges.jsonl`。每个逻辑 edge 写两条 record（正向 + 反向），便于按
from 或 to 任一端查询都 O(N) 扫描即可（N = 项目总 edge 数，几千以内）。

Edge schema:
    {
      "from": "<entity>_<hash>",
      "to":   "<entity>_<hash>",
      "edge_type": "depends_on",        # 在 _REVERSE 表里
      "confidence": 0.85,                  # 0-1
      "created_at": ISO,
      "created_by_curator_run_id": "...",
      "created_by_node": "_curator",
      "reasoning": "...",                  # 可选，为什么建这条边
    }
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.state import State, _kb_file_lock


# ── Edge 类型 + 反向名 ──────────────────────────────────────────────────────

_REVERSE: dict[str, str] = {
    # claim ↔ claim
    "depends_on": "dependents",
    "dependents": "depends_on",
    "supports": "supported_by",
    "supported_by": "supports",
    "refutes": "refuted_by",
    "refuted_by": "refutes",
    "implies": "implied_by",
    "implied_by": "implies",
    # claim ↔ concept
    "subject_of": "has_subject",
    "has_subject": "subject_of",
    "mentions": "mentioned_in",
    "mentioned_in": "mentions",
    # experiment ↔ hypothesis
    "tests": "tested_by",
    "tested_by": "tests",
    # experiment ↔ claim
    "produces": "produced_by",
    "produced_by": "produces",
    # hypothesis ↔ claim
    "predicts": "predicted_by",
    "predicted_by": "predicts",
    # generic ↔ concept (hypothesis/question/opportunity/decision/failure/skill)
    "about": "has",
    "has": "about",
    # question ↔ claim
    "answered_by": "answers",
    "answers": "answered_by",
    # opportunity ↔ claim/synthesis
    "spawned_from": "spawned",
    "spawned": "spawned_from",
    # decision ↔ concept
    "selects": "selected_by",
    "selected_by": "selects",
    "rejects": "rejected_by",
    "rejected_by": "rejects",
    # failure ↔ hypothesis/experiment
    "while_doing": "encountered_failure",
    "encountered_failure": "while_doing",
    # skill ↔ concept
    "applies_to": "applies_skills",
    "applies_skills": "applies_to",
    # synthesis ↔ claim
    "emerges_from": "emerges_into",
    "emerges_into": "emerges_from",
    # claim/hypothesis ↔ chunk
    "sources": "source_of",
    "source_of": "sources",
}


def is_valid_edge_type(edge_type: str) -> bool:
    return edge_type in _REVERSE


def reverse_of(edge_type: str) -> str | None:
    return _REVERSE.get(edge_type)


# ── 存储路径 ────────────────────────────────────────────────────────────────

def _edges_path(state: State) -> Path:
    base = state.project_root if state.project_root else state.root
    base.mkdir(parents=True, exist_ok=True)
    return base / "kb_edges.jsonl"


def _read_edges(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_edges(path: Path, edges: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in edges:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ── add_edge：双向写入 ─────────────────────────────────────────────────────

def add_edge(state: State, from_id: str, to_id: str, edge_type: str,
              confidence: float | None = None,
              reasoning: str = "",
              curator_run_id: str | None = None,
              created_by_node: str | None = None) -> tuple[dict, dict]:
    """添加一条 edge（双向存储）。已存在则更新 confidence + reasoning，不重复插入。

    返回 (forward_edge, reverse_edge)。
    """
    if not is_valid_edge_type(edge_type):
        raise ValueError(f"未知 edge_type: {edge_type!r}。合法名见 _REVERSE。")
    if from_id == to_id:
        raise ValueError("from_id 不能等于 to_id（不允许自环）")

    rev_type = _REVERSE[edge_type]
    now = datetime.now(timezone.utc).isoformat()
    path = _edges_path(state)
    created_by_node = created_by_node or state.node_type

    forward = {
        "from": from_id, "to": to_id, "edge_type": edge_type,
        "confidence": confidence,
        "reasoning": reasoning,
        "created_at": now,
        "created_by_curator_run_id": curator_run_id,
        "created_by_node": created_by_node,
    }
    reverse = {
        "from": to_id, "to": from_id, "edge_type": rev_type,
        "confidence": confidence,
        "reasoning": reasoning,
        "created_at": now,
        "created_by_curator_run_id": curator_run_id,
        "created_by_node": created_by_node,
    }

    with _kb_file_lock(path):
        edges = _read_edges(path)
        # 去重：同 (from, to, edge_type) 三元组只留一条；更新 confidence/reasoning
        def _key(e: dict) -> tuple:
            return (e.get("from"), e.get("to"), e.get("edge_type"))
        existing = {_key(e): i for i, e in enumerate(edges)}

        for e in (forward, reverse):
            k = _key(e)
            if k in existing:
                idx = existing[k]
                old = edges[idx]
                edges[idx] = {**old, **{ek: ev for ek, ev in e.items() if ev is not None}}
            else:
                edges.append(e)
                existing[k] = len(edges) - 1

        _write_edges(path, edges)
    return forward, reverse


def list_edges(state: State,
                 from_id: str | None = None,
                 to_id: str | None = None,
                 edge_type: str | None = None) -> list[dict]:
    """按任意组合过滤返回 edge list。"""
    path = _edges_path(state)
    edges = _read_edges(path)
    out = []
    for e in edges:
        if from_id and e.get("from") != from_id:
            continue
        if to_id and e.get("to") != to_id:
            continue
        if edge_type and e.get("edge_type") != edge_type:
            continue
        out.append(e)
    return out


def remove_edge(state: State, from_id: str, to_id: str, edge_type: str) -> int:
    """删除一条 edge（双向都删）。返回删除的 record 数（0 或 2）。"""
    if not is_valid_edge_type(edge_type):
        return 0
    rev_type = _REVERSE[edge_type]
    path = _edges_path(state)
    removed = 0
    with _kb_file_lock(path):
        edges = _read_edges(path)
        kept = []
        for e in edges:
            if (e.get("from") == from_id and e.get("to") == to_id
                and e.get("edge_type") == edge_type):
                removed += 1
                continue
            if (e.get("from") == to_id and e.get("to") == from_id
                and e.get("edge_type") == rev_type):
                removed += 1
                continue
            kept.append(e)
        if removed:
            _write_edges(path, kept)
    return removed


def remove_edges_by_curator_run(state: State, curator_run_id: str) -> int:
    """删除某次 curator run 创建的所有 edge（用于 revert）。返回删除的 record 数。"""
    path = _edges_path(state)
    removed = 0
    with _kb_file_lock(path):
        edges = _read_edges(path)
        kept = []
        for e in edges:
            if e.get("created_by_curator_run_id") == curator_run_id:
                removed += 1
                continue
            kept.append(e)
        if removed:
            _write_edges(path, kept)
    return removed


# ── 反驳传播 BFS ───────────────────────────────────────────────────────────

def propagate_refutation(state: State, refuted_claim_id: str,
                          curator_run_id: str | None = None,
                          max_depth: int = 5) -> list[str]:
    """当 claim X 被标记为 refuted，沿 dependents 边向下游 BFS，把所有
    依赖 X 的 claim 标 status=needs_review。

    返回受影响的 claim_id 列表。
    """
    affected: list[str] = []
    visited: set[str] = {refuted_claim_id}
    queue: list[tuple[str, int]] = [(refuted_claim_id, 0)]

    while queue:
        current_id, depth = queue.pop(0)
        if depth >= max_depth:
            continue

        # 找谁 depends_on current_id（即 current_id.dependents）
        downstream_edges = list_edges(state, from_id=current_id, edge_type="dependents")
        for edge in downstream_edges:
            dep_id = edge.get("to")
            if not dep_id or dep_id in visited:
                continue
            visited.add(dep_id)

            # 标 needs_review（仅当当前是 provisional/validated）
            rec = state.get_kb_record("claims", dep_id)
            if rec is None:
                continue
            cur_status = rec.get("status", "provisional")
            if cur_status not in ("provisional", "validated"):
                continue   # 已经 refuted/superseded 的不动

            try:
                state.update_lifecycle(
                    "claims", dep_id,
                    status_change={"to_status": "needs_review"},
                    reasoning=(
                        f"上游 claim {current_id} 被 refute；按依赖图沿 depends_on "
                        f"传播 needs_review。深度 {depth+1}。"
                    ),
                    curator_run_id=curator_run_id,
                )
                affected.append(dep_id)
                queue.append((dep_id, depth + 1))
            except ValueError:
                # 非法转换（例如已经在 refuted 等终态）—— 跳过
                continue

    return affected
