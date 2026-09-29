"""Curator 审计基础设施。

四个文件：
  kb_curator_runs.jsonl   每次 curator run 的 summary
  kb_mode3_audits.jsonl    每条 Mode 3 判断的 reasoning
  kb_proposals.jsonl       Mode 3 / Mode 2 提的待审建议（PROPOSE policy 的产出）
  kb_revert_log.jsonl      revert 操作历史

CuratorRun 上下文管理器：在 curator 节点入口 wrap，自动分配 run_id +
finalize 时写 summary。
"""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.state import State, _kb_file_lock


def _audit_dir(state: State) -> Path:
    base = state.project_root if state.project_root else state.root
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path(state: State, name: str) -> Path:
    return _audit_dir(state) / name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, record: dict) -> None:
    with _kb_file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
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


# ─────────────────────────────────────────────────────────────────────────────
# CuratorRun 上下文管理器
# ─────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def curator_run(state: State, mode: str, trigger: str = "manual"):
    """在 curator 节点入口 wrap 一次 run：

        with curator_run(state, mode="mode_1", trigger="literature_done") as crun:
            crun.record_auto_action(...)
            crun.record_proposal(...)
            crun.record_rejected(...)
            ...

    退出时自动写 summary 到 kb_curator_runs.jsonl。
    """
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    started_at = _now()

    bag = _CuratorRunBag(state=state, run_id=run_id, mode=mode, trigger=trigger,
                         started_at=started_at)
    try:
        yield bag
    finally:
        bag.finished_at = _now()
        bag.flush_summary()


class _CuratorRunBag:
    """收集一次 curator run 期间发生的事件，结束时写 summary。"""

    def __init__(self, state: State, run_id: str, mode: str, trigger: str,
                  started_at: str) -> None:
        self.state = state
        self.run_id = run_id
        self.mode = mode
        self.trigger = trigger
        self.started_at = started_at
        self.finished_at: str | None = None
        self.auto_actions: list[dict] = []
        self.proposals: list[dict] = []
        self.rejected_writes: list[dict] = []
        self.llm_calls: int = 0
        self.tokens_used: int = 0

    # ── 记录事件 ────────────────────────────────────────────────────────────

    def record_auto_action(self, action_type: str, target_entity: str,
                            target_id: str, details: dict | None = None,
                            confidence: float | None = None) -> None:
        self.auto_actions.append({
            "at": _now(),
            "action_type": action_type,
            "target_entity": target_entity,
            "target_id": target_id,
            "confidence": confidence,
            "details": details or {},
        })

    def record_proposal(self, proposal_type: str, target_entity: str,
                          target_id: str, proposed_action: str,
                          reasoning: str, confidence: float | None = None,
                          extra: dict | None = None) -> str:
        proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
        record = {
            "id": proposal_id,
            "at": _now(),
            "proposed_by_curator_run_id": self.run_id,
            "proposal_type": proposal_type,
            "target_entity": target_entity,
            "target_id": target_id,
            "proposed_action": proposed_action,
            "reasoning": reasoning,
            "confidence": confidence,
            "status": "pending",
            "extra": extra or {},
        }
        _append_jsonl(_path(self.state, "kb_proposals.jsonl"), record)
        self.proposals.append({"id": proposal_id, "type": proposal_type,
                                "target": f"{target_entity}/{target_id}"})
        return proposal_id

    def record_rejected_write(self, entity: str, reason: str,
                                attempted_text: str = "") -> None:
        self.rejected_writes.append({
            "at": _now(),
            "entity": entity,
            "reason": reason,
            "attempted_text": (attempted_text or "")[:300],
        })

    def record_mode3_audit(self, target_entity: str, target_id: str,
                              checks: dict[str, Any]) -> None:
        """记录 Mode 3 对一条 entity 的 8 项判断 reasoning。

        checks 形如 {"provenance": {...}, "scope": {...}, ...}
        """
        record = {
            "at": _now(),
            "curator_run_id": self.run_id,
            "mode": self.mode,
            "target_entity": target_entity,
            "target_id": target_id,
            "checks": checks,
        }
        _append_jsonl(_path(self.state, "kb_mode3_audits.jsonl"), record)

    def record_llm_call(self, tokens: int) -> None:
        self.llm_calls += 1
        self.tokens_used += tokens

    # ── 退出时写 summary ────────────────────────────────────────────────────

    def flush_summary(self) -> None:
        summary = {
            "run_id": self.run_id,
            "mode": self.mode,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trigger_run_id": self.state.run_id,
            "trigger_node_type": self.state.node_type,
            "project_id": self.state.project_id,
            "n_auto_actions": len(self.auto_actions),
            "n_proposals": len(self.proposals),
            "n_rejected_writes": len(self.rejected_writes),
            "llm_calls": self.llm_calls,
            "tokens_used": self.tokens_used,
            "auto_actions": self.auto_actions[:50],   # 防止过大；> 50 可看 audits
            "proposals_brief": self.proposals,
            "rejected_writes": self.rejected_writes,
        }
        _append_jsonl(_path(self.state, "kb_curator_runs.jsonl"), summary)


# ─────────────────────────────────────────────────────────────────────────────
# 查询 / revert
# ─────────────────────────────────────────────────────────────────────────────

def list_curator_runs(state: State, limit: int = 50) -> list[dict]:
    """最近 limit 个 curator run summary。"""
    runs = _read_jsonl(_path(state, "kb_curator_runs.jsonl"))
    return runs[-limit:]


def get_curator_run(state: State, run_id: str) -> dict | None:
    for r in _read_jsonl(_path(state, "kb_curator_runs.jsonl")):
        if r.get("run_id") == run_id:
            return r
    return None


def list_proposals(state: State, status: str = "pending",
                    limit: int = 100) -> list[dict]:
    out = []
    for p in _read_jsonl(_path(state, "kb_proposals.jsonl")):
        if status and p.get("status") != status:
            continue
        out.append(p)
    return out[-limit:]


def get_mode3_audits_for(state: State, target_id: str) -> list[dict]:
    return [a for a in _read_jsonl(_path(state, "kb_mode3_audits.jsonl"))
            if a.get("target_id") == target_id]


def revert_curator_run(state: State, run_id: str,
                          *, reasoning: str = "") -> dict:
    """撤销某次 curator run 的所有 AUTO 操作：
      - 清空带这个 run_id 的所有 derived 字段
      - canonical record 加 reverted_by_curator_run_id flag 但保留
      - 删除带这个 run_id 的所有 edges
      - proposals 的 status 改成 'reverted'

    NOTE: lifecycle 变更（如 status flip）已写进 review_history，无法物理撤销 ——
    但可以追加一条新的 review_history 说明 "由 revert 触发的回滚"。

    返回 revert 操作的 summary。
    """
    from core.kb_edges import remove_edges_by_curator_run
    from shared.lib.kb_schema import ENTITIES, derived_fields

    # 重写 find_by_curator_run（state v2 方法已删）：扫 4 v3 entity 找匹配 run_id
    matches: list[tuple[str, dict]] = []
    for entity in ENTITIES:
        for r in state.list_kb(entity):
            if (r.get("created_by_curator_run_id") == run_id
                or r.get("derived_by_curator_run_id") == run_id):
                matches.append((entity, r))
    summary = {
        "reverted_run_id": run_id,
        "at": _now(),
        "by_run_id": state.run_id,
        "reasoning": reasoning,
        "n_records_touched": len(matches),
        "n_canonical_flagged": 0,
        "n_derived_cleared": 0,
        "n_edges_removed": 0,
        "n_proposals_reverted": 0,
    }

    now = _now()
    # 1. 处理 entity record
    for entity, rec in matches:
        rec_id = rec["id"]
        # 如果是 curator 创建的 entity（created_by_curator_run_id 命中）→ 标 reverted
        flagged = False
        cleared = False
        if rec.get("created_by_curator_run_id") == run_id:
            # 加 reverted flag（仍保留 canonical record）
            state.patch_derived(entity, rec_id,
                                 {"reverted_by_curator_run_id": run_id},
                                 curator_run_id=None)
            flagged = True
            summary["n_canonical_flagged"] += 1
        # 如果是 curator 修改 derived 字段（derived_by_curator_run_id 命中）→ 清空 derived
        if rec.get("derived_by_curator_run_id") == run_id:
            # 清空所有 derived 字段（重置为 null）
            df_set = derived_fields(entity)
            patch = {f: None for f in df_set if f in rec and f != "embedding_hash"}
            patch["derived_by_curator_run_id"] = None
            patch["derived_at"] = None
            state.patch_derived(entity, rec_id, patch, curator_run_id=None)
            cleared = True
            summary["n_derived_cleared"] += 1

    # 2. 删 edges
    summary["n_edges_removed"] = remove_edges_by_curator_run(state, run_id)

    # 3. proposals 标 reverted
    prop_path = _path(state, "kb_proposals.jsonl")
    with _kb_file_lock(prop_path):
        proposals = _read_jsonl(prop_path)
        n_changed = 0
        for p in proposals:
            if p.get("proposed_by_curator_run_id") == run_id and p.get("status") == "pending":
                p["status"] = "reverted"
                p["reverted_at"] = now
                n_changed += 1
        if n_changed:
            tmp = prop_path.with_suffix(prop_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for p in proposals:
                    f.write(json.dumps(p, ensure_ascii=False) + "\n")
            tmp.replace(prop_path)
        summary["n_proposals_reverted"] = n_changed

    # 4. 写 revert log
    _append_jsonl(_path(state, "kb_revert_log.jsonl"), summary)
    return summary


def update_proposal_status(state: State, proposal_id: str, new_status: str,
                            reviewer_node: str | None = None,
                            reasoning: str = "") -> dict | None:
    """处理一个 proposal：accepted / rejected / superseded。"""
    path = _path(state, "kb_proposals.jsonl")
    now = _now()
    with _kb_file_lock(path):
        proposals = _read_jsonl(path)
        target = None
        for p in proposals:
            if p.get("id") == proposal_id:
                p["status"] = new_status
                p["reviewed_at"] = now
                p["reviewer_node"] = reviewer_node or state.node_type
                p["reviewer_run_id"] = state.run_id
                p["reviewer_reasoning"] = reasoning
                target = p
                break
        if target is None:
            return None
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for p in proposals:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        tmp.replace(path)
        return target
