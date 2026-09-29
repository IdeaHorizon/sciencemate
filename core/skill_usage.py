"""Skill usage 跟踪（独立于 KB audit）。

`skill_usage.jsonl` 在 org 层（跨项目共享 usage 统计），每条形如：
  {
    skill_name: "openfoam_debug",
    used_at: ISO,
    used_by_run_id: "...",
    used_by_node: "experiment",
    outcome: "success" | "failure" | "partial",
    applied_during: "experiment_<hash>" | null,
    reasoning: "..."
  }

用途：
  - Mode 2 dreaming 用它算 skill 的 success rate / 半年没用过的 skill
  - skill_usage_stats 工具暴露给 user / orchestrator
  - 跟 KB audit 完全分开 —— skill 不是 KB entity
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org_root() -> Path:
    from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）

    org = Path(os.getenv("HARNESS_FRAMEWORK_ORG_HOME", str(_root() / "org")))
    org.mkdir(parents=True, exist_ok=True)
    return org


def _usage_path() -> Path:
    return _org_root() / "skill_usage.jsonl"


def record_usage(
    skill_name: str,
    *,
    used_by_run_id: str,
    used_by_node: str,
    outcome: str = "success",
    applied_during: str | None = None,
    reasoning: str = "",
) -> dict:
    """记录一次 skill 使用。outcome ∈ {success / failure / partial}。"""
    if outcome not in ("success", "failure", "partial"):
        raise ValueError(f"outcome 必须 success/failure/partial，got {outcome!r}")
    record = {
        "skill_name": skill_name,
        "used_at": _now(),
        "used_by_run_id": used_by_run_id,
        "used_by_node": used_by_node,
        "outcome": outcome,
        "applied_during": applied_during,
        "reasoning": reasoning,
    }
    path = _usage_path()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def get_usage(skill_name: str | None = None,
               limit: int | None = None) -> list[dict]:
    """读 usage log。skill_name=None 返回全部。"""
    path = _usage_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if skill_name and r.get("skill_name") != skill_name:
            continue
        out.append(r)
    if limit:
        out = out[-limit:]
    return out


def usage_stats(skill_name: str) -> dict:
    """返回该 skill 的聚合统计：使用次数 / 成功率 / 最近使用时间 / 等。"""
    usages = get_usage(skill_name)
    n = len(usages)
    if n == 0:
        return {"skill_name": skill_name, "usage_count": 0}
    n_success = sum(1 for u in usages if u.get("outcome") == "success")
    n_failure = sum(1 for u in usages if u.get("outcome") == "failure")
    n_partial = sum(1 for u in usages if u.get("outcome") == "partial")
    return {
        "skill_name": skill_name,
        "usage_count": n,
        "success_count": n_success,
        "failure_count": n_failure,
        "partial_count": n_partial,
        "success_rate": n_success / n if n else 0,
        "last_used_at": usages[-1].get("used_at"),
        "last_used_outcome": usages[-1].get("outcome"),
    }


def all_skill_stats() -> dict[str, dict]:
    """所有有过 usage 记录的 skill 的统计。"""
    all_records = get_usage()
    by_skill: dict[str, list[dict]] = {}
    for r in all_records:
        by_skill.setdefault(r.get("skill_name", "?"), []).append(r)
    return {name: usage_stats(name) for name in by_skill}
