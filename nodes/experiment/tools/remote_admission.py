"""远端执行的准入：授权包络、作业归属、边界如实披露（governing §4 I1/I8、§8）。

三件事，都是"当次调用型"的硬保证，不是仪式：

* **授权包络**：用户 / 平台预授权了多少 CPU、GPU、节点、时长、费用。包络之内
  不重复问；**首次真实外部提交**或越出包络时问一次。包络本身是 A 的一部分，
  run 内冻结；模型改不了它。
* **作业归属**：普通 Experiment 只能取消**本 run 拥有**的作业。归属来自账本
  （哪个 nonce 是本 run 写进去的），不是调用方说了算。取消别人的作业需要任务
  明确目标加平台独立的 ownership 授权，不能靠一次高危确认升权。
* **边界披露**：adapter 说 ``remote_write_isolation`` 是 missing，那就必须一路
  披露到收尾，不能在任何一层变成 enforced。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from . import ledger as _ledger
    from . import scheduler_adapters as _adapters
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore
    from tools import scheduler_adapters as _adapters  # type: ignore

_ENVELOPE_FILE = "authorization_envelope.json"

#: 包络里可以约束的维度。没给的维度 = 未授权上限，按 None 处理（不拦，但披露）。
ENVELOPE_DIMENSIONS = ("cpus", "gpus", "nodes", "walltime_minutes", "cost_usd")


def envelope_path(state: Any) -> Path:
    return _ledger.ledger_dir(state) / _ENVELOPE_FILE


def _node_inputs(state: Any) -> dict[str, Any]:
    try:
        payload = state.hook_state.get("node_inputs") or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def freeze_envelope(state: Any) -> dict[str, Any]:
    """从 node_inputs 读出预授权包络并冻结（幂等）。没有就是空包络。"""
    path = envelope_path(state)
    if path.exists():
        return {"status": "already_frozen", **json.loads(path.read_text(encoding="utf-8"))}
    raw = _node_inputs(state).get("authorization_envelope")
    limits: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key in ENVELOPE_DIMENSIONS:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                limits[key] = value
    payload = {"limits": limits, "source": "node_inputs.authorization_envelope" if limits else "absent",
               "version": 1}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _ledger.try_record(state, "envelope_frozen", limits=limits, source=payload["source"])
    return {"status": "frozen", **payload}


def frozen_envelope(state: Any) -> dict[str, Any]:
    path = envelope_path(state)
    if not path.exists():
        return {"limits": {}, "source": "absent", "version": 1}
    return json.loads(path.read_text(encoding="utf-8"))


def _prior_external_submission(state: Any) -> bool:
    try:
        records = _ledger.open_ledger(state).records()
    except Exception:
        return False
    return any(r.get("kind") == "intent" and str(r.get("scheduler") or "").lower() != "local"
               for r in records)


def _prior_authorization(state: Any, dimension_key: str) -> bool:
    try:
        records = _ledger.open_ledger(state).records()
    except Exception:
        return False
    return any(r.get("kind") == "authorization_granted" and r.get("scope") == dimension_key
               for r in records)


def check_submission(state: Any, *, scheduler: str, request: dict[str, Any]) -> dict[str, Any]:
    """提交前的授权判断。

    返回 ``{"decision": "allowed"|"ask_once", "reasons": [...], "envelope": ..., "isolation": ...}``。
    **不自己问人**：Core 目前没有 continuous 档下不可自动代答的 pause（Codex 04 §5），
    所以这里只把"需要问"如实交出去，由调用方用 request_human_input 问一次并记授权。
    """
    envelope = frozen_envelope(state)
    limits = envelope.get("limits") or {}
    caps = _adapters.capabilities_for(scheduler)
    remote = str(scheduler or "").lower() != "local"
    reasons: list[dict[str, Any]] = []
    for key in ENVELOPE_DIMENSIONS:
        want = request.get(key)
        cap = limits.get(key)
        if not isinstance(want, (int, float)) or isinstance(want, bool):
            continue
        if cap is None:
            reasons.append({"scope": key, "why": "no_declared_limit", "requested": want})
            continue
        if want > cap:
            reasons.append({"scope": key, "why": "beyond_envelope", "requested": want, "limit": cap})
    if remote and not _prior_external_submission(state):
        reasons.append({"scope": "first_external_submission", "why": "first_real_external_submission",
                        "scheduler": scheduler})
    # 已经问过并拿到授权的维度不再问。
    reasons = [r for r in reasons if not _prior_authorization(state, str(r["scope"]))]
    return {
        "decision": "ask_once" if reasons else "allowed",
        "reasons": reasons,
        "envelope": envelope,
        "isolation": {"remote_write_isolation": caps.remote_write_isolation,
                      "notes": list(caps.notes)},
    }


def record_authorization(state: Any, *, scope: str, granted_by: str, note: str = "") -> dict[str, Any]:
    """记下一次已获授权的维度；同一维度之后不再问。"""
    row = _ledger.try_record(state, "authorization_granted", scope=scope, granted_by=granted_by, note=note)
    return {"status": "recorded" if row else "ledger_unavailable", "scope": scope}


def owned_jobs(state: Any) -> dict[str, dict[str, Any]]:
    """本 run 拥有的作业：账本里有 intent 的那些（按 nonce）。"""
    try:
        records = _ledger.open_ledger(state).records()
    except Exception:
        return {}
    return _ledger.project_jobs(records)


def cancel_admission(state: Any, *, scheduler: str, job_id: str) -> dict[str, Any]:
    """取消的归属判定：这个作业是不是本 run 从账本里认得的那些之一。

    **当前只作披露，还没有接成 cancel 的门。** 原因是实测的：2026-09-23 把它接
    在 ``resource_manager._cancel_job`` 入口上，7 个既有用例转红——它们的受管作业
    是直接构造提交记录建起来的，账本里没有对应绑定，于是本 run 自己的作业被判成
    "别人的"。在账本成为唯一归属权威之前（S1d 原子切换），拿它当门只会多拒一种
    情况，而不会多挡住一个谎。

    真实的缺口仍然存在并已登记：``_external_job_record`` 按项目级 artifact 查，
    不限本 run，所以"普通 Experiment 只能取消本 run 的作业"这条保证今天并不完整。
    S1d 撤掉旧提交记录这条权威时一并关闭。
    """
    jobs = owned_jobs(state)
    for nonce, job in jobs.items():
        if str(job.get("external_id") or "") == str(job_id) and \
                str(job.get("scheduler") or "").lower() == str(scheduler or "").lower():
            return {"decision": "allowed", "nonce": nonce, "job": job}
    return {
        "decision": "refused",
        "reason": "not_owned_by_this_run",
        "error": (f"{scheduler}/{job_id} 不在本 run 的账本里：本 run 没有提交过它，"
                  "普通 Experiment 不能取消别人的作业。"),
        "exit": ("这不是一次高危确认能解除的：取消别的 run 或别人的作业，需要任务里明确"
                 "点名这个目标，加上平台独立的 ownership / admin 授权。"
                 "若只是想停下本 run 自己的作业，用账本里列出的那些。"),
        "owned": [{"nonce": n, "external_id": j.get("external_id"), "state": j.get("state")}
                  for n, j in jobs.items()],
    }


def isolation_disclosure(scheduler: str) -> dict[str, Any]:
    """收尾与视图要原样带上的边界事实。missing 永远不能在任何一层变成 enforced。"""
    caps = _adapters.capabilities_for(scheduler)
    return {
        "scheduler": caps.scheduler,
        "remote_write_isolation": caps.remote_write_isolation,
        "enforced": caps.remote_write_isolation == _adapters.ISOLATION_ENFORCED,
        "notes": list(caps.notes),
        "means": ("可写根之外物理写不进去" if caps.remote_write_isolation == _adapters.ISOLATION_ENFORCED
                  else "计算节点上没有沙箱：越界写只能事后从快照看出并披露，必要时记 blocked"),
    }
