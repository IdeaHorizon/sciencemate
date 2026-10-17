"""受管作业的终态结算：B 的唯一 writer（Codex 45 P0-D）。

status / health / wait / cancel 任何一个入口第一次读到**可信**终态，都调用这里的
``settle_terminal``：它把调度器原文、退出事实、输出身份冻结成一条规范 ``job_terminal``
（按 nonce 幂等），并将提交窗口的输出身份变化放在同一条终态事实内，不另造 action。
``finish`` 只消费这条事实；物理终态**不**替模型选择任务级 outcome。

"可信"的定义按调度器：Slurm 要 sacct 的 allocation 行全部终态；PBS 要 job_state C/E/F；
Kubernetes 要 Complete/Failed 条件；local 要原生 supervisor 写出的退出码。squeue 里不见了
不算——那只说明调度器不再持有它，交 reconcile。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from . import ledger as _ledger
    from . import action_boundary as _boundary
    from . import capture as _capture
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore
    from tools import action_boundary as _boundary  # type: ignore
    from tools import capture as _capture  # type: ignore

_MAX_OBSERVED_FILES = 500
_MAX_RAW_TEXT = 4000


def _derive(scheduler: str, evidence: dict[str, Any] | None) -> tuple[str | None, int | None]:
    """从正向终态证据派生 (derived_state, exit_code)。派生不出来就是 None，不猜。"""
    if not isinstance(evidence, dict):
        return None, None
    kind = str(scheduler or "").lower()
    exit_code = evidence.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = evidence.get("returncode") if isinstance(evidence.get("returncode"), int) else None
    if kind == "slurm":
        state = evidence.get("allocation_state") or evidence.get("state")
        if state is None and evidence.get("completed_cleanly") is True:
            state = "COMPLETED"
        return (str(state).upper() if state else None), exit_code
    if kind == "local":
        status = str(evidence.get("sandbox_status") or "").lower()
        if exit_code is None:
            return (status.upper() or None), None
        return ("COMPLETED" if exit_code == 0 else "FAILED"), exit_code
    if kind == "pbs":
        state = evidence.get("job_state") or evidence.get("state")
        if state is None and exit_code is not None:
            state = "COMPLETED" if exit_code == 0 else "FAILED"
        return (str(state).upper() if state else None), exit_code
    if kind == "kubernetes":
        cond = str(evidence.get("condition") or "").lower()
        return ({"complete": "COMPLETED", "failed": "FAILED"}.get(cond)), exit_code
    return (str(evidence.get("state")).upper() if evidence.get("state") else None), exit_code


def existing_terminal(state: Any, nonce: str) -> dict[str, Any] | None:
    try:
        for row in _ledger.open_ledger(state).records():
            if row.get("kind") == "job_terminal" and str(row.get("nonce") or "") == str(nonce):
                return row
    except Exception:
        return None
    return None


def capture_submission_outputs(state: Any, nonce: str) -> dict[str, Any]:
    """Attach a bounded baseline to the existing pre-launch durable marker.

    This runs after framework script/launch preparation, before the scheduler
    call. An unreadable root is unknown, not an empty directory. Failure to
    observe does not introduce another execution gate.
    """
    try:
        intents = [r for r in _ledger.open_ledger(state).records()
                   if r.get("kind") == "intent" and r.get("nonce") == nonce]
        if len(intents) != 1:
            raise ValueError("no unique submission intent")
        intent = intents[0]
        roots = list(dict.fromkeys([*intent.get("output_roots", []), intent["output_root"]]))
        candidates = [p for p in _boundary._obligation_candidates(state)
                      if any(p.is_relative_to(Path(r)) for r in roots)]
        return {"roots": roots, "candidates": [str(p) for p in candidates],
                "snapshot": _capture.snapshot(candidates, [Path(r) for r in roots],
                                              max_files=_MAX_OBSERVED_FILES)}
    except Exception as exc:
        return {"roots": [], "candidates": [], "snapshot": {
            "entries": {}, "truncated": True,
            "snapshot_error": f"{type(exc).__name__}: {exc}"}}


def _output_diff(state: Any, nonce: str) -> dict[str, Any]:
    markers = [r for r in _ledger.open_ledger(state).records()
               if r.get("kind") == "submission_attempted" and r.get("nonce") == nonce]
    baseline = markers[0].get("output_baseline") if len(markers) == 1 else None
    if not isinstance(baseline, dict):
        return {"created": [], "modified": [], "deleted": [], "unknown": [],
                "identities": {}, "truncated": True, "status": "unavailable",
                "reason": "pre-launch output baseline unavailable"}
    before = baseline.get("snapshot") or {"entries": {}, "truncated": True}
    try:
        after = _capture.snapshot([Path(p) for p in baseline.get("candidates", [])],
                                  [Path(r) for r in baseline.get("roots", [])],
                                  max_files=_MAX_OBSERVED_FILES)
        changes = _capture.diff(before, after, limit=None)
        identities = _boundary._identity_rows(before, after, changes)
        # A failed hash probe is not a changed-content witness. Preserve its
        # uncertainty, but do not promote it to a producer in any consumer.
        uncertain = {p for p, ident in identities.items()
                     if ident.get("before_unknown") or (ident.get("after") or {}).get("hash_error")}
        for field in ("created", "modified"):
            changes[field] = [p for p in changes[field] if p not in uncertain]
        changes["unknown"] = list(dict.fromkeys([*changes.get("unknown", []), *sorted(uncertain)]))
        return {**changes, "identities": {p: v for p, v in identities.items() if p not in uncertain},
                "status": "partial" if changes["truncated"] or changes["unknown"] else "observed",
                "basis": "submission_to_terminal_observation_window",
                "baseline_seq": markers[0].get("seq"),
                "observed_at": after.get("taken_at"),
                "root_coverage": after.get("root_coverage", []),
                "baseline_root_coverage": before.get("root_coverage", []),
                "observation_boundary": {"roots": baseline.get("roots", []), "excluded_roots": []},
                "limitation": (
                    "Outputs describe the first durable settlement observation, not the physical job-exit instant. "
                    "A snapshot lost before persistence cannot be recovered; a later retry observes a later window. "
                    "Window changes do not prove an exclusive physical writer; out-of-band concurrent writes are not distinguished. "
                    "Same-object, same-size changes without content hashes cannot distinguish a timestamp update from recomputation."
                )}
    except Exception as exc:
        return {"created": [], "modified": [], "deleted": [], "unknown": [],
                "identities": {}, "truncated": True, "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}"}


def _trim(raw: Any) -> Any:
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            if isinstance(v, str) and len(v) > _MAX_RAW_TEXT:
                out[k] = v[:_MAX_RAW_TEXT] + "…"
            elif isinstance(v, (bytes, bytearray)):
                out[k] = bytes(v)[:_MAX_RAW_TEXT].decode("utf-8", "replace")
            else:
                out[k] = v
        return out
    return raw


def settle_terminal(state: Any, record: dict[str, Any], *, evidence: dict[str, Any] | None,
                    scheduler_result: dict[str, Any] | None = None, observed_by: str = "") -> dict[str, Any]:
    """幂等：同 nonce 只写一次 job_terminal。返回 {settled, reason, nonce, derived_state, exit_code, record}。"""
    nonce = str((record or {}).get("submission_nonce") or "")
    scheduler = str((record or {}).get("scheduler") or "").lower()
    job_id = str((record or {}).get("job_id") or "")
    if not nonce:
        return {"settled": False, "reason": "no_submission_nonce", "scheduler": scheduler, "job_id": job_id}
    try:
        if _ledger.bootstrap_phase(state) == "none":
            return {"settled": False, "reason": "not_a_v3_run", "nonce": nonce}
    except Exception:
        return {"settled": False, "reason": "ledger_unavailable", "nonce": nonce}
    prior = existing_terminal(state, nonce)
    if prior is not None:
        if prior.get("idempotency_key"):
            try:
                _ledger.open_ledger(state, write=True).append_once(
                    "job_terminal", prior["idempotency_key"], nonce=nonce)
            except Exception as exc:
                return {"settled": False, "reason": "ledger_write_failed", "nonce": nonce,
                        "error": f"{type(exc).__name__}: {exc}"}
        return {"settled": True, "already": True, "nonce": nonce, "derived_state": prior.get("derived_state"),
                "exit_code": prior.get("exit_code"), "record": prior}
    derived_state, exit_code = _derive(scheduler, evidence)
    if not isinstance(evidence, dict) or derived_state is None:
        return {"settled": False, "reason": "no_positive_terminal_evidence", "nonce": nonce}
    # One atomic B record owns both terminal facts and output observations.
    # There is no second pseudo-action with an invented successful exit code.
    observation = _output_diff(state, nonce)
    paths = list(observation.get("created", [])) + list(observation.get("modified", []))
    files = [p for p in paths if ((observation.get("identities") or {}).get(p, {}).get("after") or {}).get("kind") == "file"]
    try:
        stored_observation = _capture.persist_observation(state, f"job-{nonce}", observation)
        row = _ledger.open_ledger(state, write=True).append_once(
            "job_terminal", f"job_terminal:{nonce}", nonce=nonce, scheduler=scheduler, external_id=job_id,
            derived_state=derived_state, exit_code=exit_code, observed_by=observed_by or None,
            raw={"evidence": _trim(evidence), "scheduler_result": _trim((scheduler_result or {}).get("raw") if isinstance(scheduler_result, dict) else None)},
            output_files=files[:50], output_observation=f"job-{nonce}",
            capture_record=str(_capture.allocate_record_dir(state, f"job-{nonce}")),
            output_diff=stored_observation, observation_status=observation.get("status"))
    except Exception as exc:
        return {"settled": False, "reason": "ledger_write_failed", "nonce": nonce,
                "derived_state": derived_state, "exit_code": exit_code,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"settled": True, "already": False, "nonce": nonce, "derived_state": derived_state,
            "exit_code": exit_code, "record": row, "observation": stored_observation}
