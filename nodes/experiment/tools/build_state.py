"""Graph-based build state helpers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
STATE_KEY = "provisioned_build_state"

ORDER = {
    "planned": 0,
    "configured": 1,
    "built": 2,
    # 跨 run 遗留：产物齐全但 mtime 早于本 run 起始。与 built 同级、低于 verified，
    # refresh_blocked_by 的 ORDER < ORDER["verified"] 判定自动拦下游。
    "inherited": 2,
    "verified": 3,
    "run_verified": 4,
    "stale": -1,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_from_dag(dag: dict[str, Any], env_fingerprint: str | None = None) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for nid, node in (dag.get("nodes") or {}).items():
        deps = list(dict.fromkeys(node.get("deps") or []))
        nodes[nid] = {
            "id": nid,
            "state": "planned",
            "deps": deps,
            "blocked_by": deps[:],
            "outputs": list(dict.fromkeys(node.get("outputs") or [])),
            "source": node.get("source"),
            "evidence": [],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "dag_id": dag.get("dag_id"),
        "dag_status": dag.get("status"),
        "env_fingerprint": env_fingerprint,
        "created_at": _now(),
        "updated_at": _now(),
        "nodes": nodes,
    }


def _downstream(nodes: dict[str, Any], node_id: str) -> list[str]:
    out: list[str] = []
    for nid, node in nodes.items():
        if node_id in (node.get("deps") or []):
            out.append(nid)
            out.extend(_downstream(nodes, nid))
    return list(dict.fromkeys(out))


def mark_stale_on_env_change(state: dict[str, Any], new_env_fingerprint: str | None) -> dict[str, Any]:
    if not new_env_fingerprint or state.get("env_fingerprint") == new_env_fingerprint:
        return state
    nodes = state.get("nodes") or {}
    stale_ids: list[str] = []
    for nid, node in nodes.items():
        if node.get("state") in {"built", "inherited", "verified", "run_verified"}:
            stale_ids.append(nid)
    for nid in list(stale_ids):
        stale_ids.extend(_downstream(nodes, nid))
    for nid in dict.fromkeys(stale_ids):
        if nid in nodes:
            nodes[nid]["state"] = "stale"
            nodes[nid]["stale_reason"] = "env_fingerprint_changed"
            nodes[nid]["stale_at"] = _now()
    state["env_fingerprint"] = new_env_fingerprint
    state["updated_at"] = _now()
    return state


def refresh_blocked_by(state: dict[str, Any]) -> dict[str, Any]:
    nodes = state.get("nodes") or {}
    for nid, node in nodes.items():
        blocked = []
        for dep in node.get("deps") or []:
            dep_state = (nodes.get(dep) or {}).get("state", "planned")
            if ORDER.get(dep_state, -1) < ORDER["verified"]:
                blocked.append(dep)
        node["blocked_by"] = blocked
    state["updated_at"] = _now()
    return state


def check_prerequisites(state: dict[str, Any], target: str | None) -> dict[str, Any]:
    refresh_blocked_by(state)
    nodes = state.get("nodes") or {}
    if not target or target not in nodes:
        return {"ok": True, "reason": "target_unmapped", "warning": True}
    blocked = nodes[target].get("blocked_by") or []
    return {"ok": not blocked, "target": target, "blocked_by": blocked}


def resolve_run_started_at(state: Any) -> float:
    """本 run 起始时刻锚（epoch 秒）。0.0 = 锚不可得，调用方保持旧行为并留痕。

    与 core/data_provenance 同源：优先读 executor 写入 hook_state 的
    `_run_started_at`（只读消费，不修改 core）；不可得时回退 run_id 的
    时间戳前缀（State.new 生成 "{epoch}-{hex}"），再回退 state.created_at。
    """
    try:
        from core.data_provenance import run_started_at
        v = float(run_started_at(state))
        if v > 0:
            return v
    except Exception:
        pass
    rid = getattr(state, "run_id", None)
    if rid:
        prefix = str(rid).split("-", 1)[0]
        try:
            v = float(prefix)
            if v > 0:
                return v
        except ValueError:
            pass
    created = getattr(state, "created_at", None)
    if created:
        try:
            if isinstance(created, int | float):
                return float(created) if float(created) > 0 else 0.0
            return datetime.fromisoformat(str(created)).timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    return 0.0


def framework_verify_outputs(outputs: list[str], *,
                             run_started_at: float = 0.0) -> dict[str, Any]:
    records = []
    ok = True
    for out in outputs:
        p = Path(os.path.expanduser(str(out)))
        exists = p.is_file()
        size = p.stat().st_size if exists else 0
        rec = {"path": str(p), "exists": exists, "size_bytes": size}
        if exists and run_started_at > 0:
            # 判据与 core/data_provenance.record_tool_paths 同源：
            # mtime < run_started_at 严格比较，不加容差。
            mtime = p.stat().st_mtime
            rec["mtime"] = mtime
            rec["predates_run"] = mtime < run_started_at
        if not exists or size <= 0:
            ok = False
        records.append(rec)
    return {"ok": ok and bool(outputs), "outputs": records}


def mark_inherited(state: dict[str, Any], node_id: str, verify_result: dict[str, Any],
                   run_started_at: float, turn: int | None = None) -> dict[str, Any]:
    """产物齐全但含跨 run 遗留文件 → inherited（只追加事实，不动磁盘产物）。"""
    records = list(verify_result.get("outputs") or [])
    advance_node(state, node_id, "inherited",
                 {"by": "framework_verify_outputs", "turn": turn,
                  "run_started_at": run_started_at, "outputs": records})
    node = (state.get("nodes") or {}).get(node_id) or {}
    if node.get("state") == "inherited":
        node["inherited_run_started_at"] = run_started_at
        node["inherited_outputs"] = [r for r in records if r.get("predates_run")]
    return state


def normalize_reuse_path(path: Any) -> str:
    """收养判定的路径归一化：expanduser + realpath（beegfs 符号链接场景）。

    declared_paths 与 stale_paths 必须经这同一个函数归一化后再比对。
    """
    return os.path.realpath(os.path.expanduser(str(path)))


def adopt_inherited_nodes(state: dict[str, Any], declared_paths: Any,
                          turn: int | None = None) -> dict[str, Any]:
    """机械收养：产物 metadata 声明的 reused_inputs 精确覆盖某 inherited 节点
    **全部** stale 路径 → 收养为 verified（evidence 留痕）。

    部分覆盖不放行：返回 incomplete 记录（含 uncovered_paths / declared_paths），
    调用方必须把它写成事实事件 —— 绝不静默失败。
    """
    declared = {normalize_reuse_path(p) for p in (declared_paths or [])}
    adopted: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for nid, node in (state.get("nodes") or {}).items():
        if node.get("state") != "inherited":
            continue
        stale_paths = [r.get("path") for r in (node.get("inherited_outputs") or [])
                       if r.get("path")]
        if not stale_paths:
            continue
        normalized = sorted({normalize_reuse_path(p) for p in stale_paths})
        uncovered = sorted(set(normalized) - declared)
        if uncovered:
            incomplete.append({"node": nid, "uncovered_paths": uncovered,
                               "declared_paths": sorted(declared)})
            continue
        advance_node(state, nid, "verified",
                     {"by": "reused_inputs_adoption", "turn": turn,
                      "adopted_paths": normalized})
        adopted.append({"node": nid, "adopted_paths": normalized})
    return {"adopted": adopted, "incomplete": incomplete}


def advance_node(state: dict[str, Any], node_id: str, new_state: str,
                 evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    nodes = state.setdefault("nodes", {})
    node = nodes.setdefault(node_id, {"id": node_id, "deps": [], "outputs": [], "evidence": []})
    old_state = node.get("state", "planned")
    if ORDER.get(new_state, -1) >= ORDER.get(old_state, -1) or old_state == "stale":
        node["state"] = new_state
        if evidence:
            node.setdefault("evidence", []).append(evidence)
        node["updated_at"] = _now()
    refresh_blocked_by(state)
    return state


def load_state_from_hook(hook_state: dict[str, Any]) -> dict[str, Any] | None:
    val = hook_state.get(STATE_KEY)
    return val if isinstance(val, dict) else None


def save_state_to_hook(hook_state: dict[str, Any], state: dict[str, Any]) -> None:
    hook_state[STATE_KEY] = state


def to_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
