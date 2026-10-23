"""finish：v3 协议唯一的收尾入口（governing document §6）。

模型说 outcome、摘要与实际交付；自然任务还说明验收方法及其对应要求。
方法是模型的专业解释，不能改写原任务；框架做的是：

1. 读冻结的 required obligations（A）与账本（B），为每一项找**正面证据**：
   文件在、非空、且账本里有本 run 的某个 action 的 observation 记录它被写出；
   检查命令由框架自己跑（受沙箱、只读、记账约束）；
2. 找**确定性矛盾**：开放 / unknown 的作业；失败且没有成功重试的作业；当前
   required 检查失败；适用的 fatal 信号；产物身份对不上
   observation；
3. success 谓词 = 每个 required 都有正面证据 ∧ 没有矛盾。谓词不成立而模型要
   success：**一次**返回缺口与可达的诚实 outcome，不冻结；partial / failed /
   blocked / cancelled 永远可达；
4. 冻结 raw → clean → experiment_log 三件（所有 outcome 同一封套），写平台
   闭包元数据；账本记一条 finish。

这里没有 route、receipt、attempt 术语；模型不需要知道账本长什么样。
"""

from __future__ import annotations

import glob as _glob
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.tool_registry import ToolDefinition, register_tool

try:
    from . import capture as _capture
    from . import delivery_transaction as _delivery_transaction
    from . import ledger as _ledger
    from . import obligations as _obligations
    from . import run_bootstrap as _run_bootstrap
    from . import task_verification as _task_verification
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import capture as _capture  # type: ignore
    from tools import delivery_transaction as _delivery_transaction  # type: ignore
    from tools import ledger as _ledger  # type: ignore
    from tools import obligations as _obligations  # type: ignore
    from tools import run_bootstrap as _run_bootstrap  # type: ignore
    from tools import task_verification as _task_verification  # type: ignore

OUTCOMES = ("success", "partial", "failed", "blocked", "cancelled")
_SUCCESS_STATES = {"COMPLETED", "completed", "success", "succeeded", "0"}

CheckRunner = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


# ── 证据 ─────────────────────────────────────────────────────────────────────

def _producer_rows(records: list[dict[str, Any]], path: Path) -> list[dict[str, Any]]:
    """Content provenance projected from before/after observations.

    A same-inode, same-content timestamp change cannot establish a new content
    producer: touch and an identical in-place recomputation look the same.
    Preserve the previous producer through that identity adjustment instead
    of attributing scientific content to the metadata-changing command.
    """
    key = str(path)
    out: list[dict[str, Any]] = []
    for row in _ledger.project_output_observations(records):
        if key in (row.get("unknown") or []):
            out = []
            continue
        if key not in (row.get("created") or []) and key not in (row.get("modified") or []):
            continue
        frozen = ((row.get("identities") or {}).get(key) or {})
        if not frozen.get("after") or frozen.get("before_unknown") or frozen.get("basis") == "hash_unavailable":
            # A failed hash probe is not evidence of a physical write.
            continue
        before, after = frozen.get("before"), frozen.get("after")
        if any(isinstance(ident, dict) and ident.get("hash_error") for ident in (before, after)):
            continue
        same_file_object = (
            isinstance(before, dict) and isinstance(after, dict)
            and before.get("kind") == after.get("kind") == "file"
            and before.get("inode") == after.get("inode")
            and before.get("size") == after.get("size")
        )
        if same_file_object and (before.get("sha256") is None or after.get("sha256") is None):
            # A metadata delta cannot distinguish touch from an in-place,
            # same-size rewrite. Keep B's observation, but do not invent a
            # content producer or carry the old content identity forward.
            continue
        same_content_object = (
            same_file_object
            and before.get("sha256") is not None
            and before.get("sha256") == after.get("sha256")
        )
        same_directory_object = (
            isinstance(before, dict) and isinstance(after, dict)
            and before.get("kind") == after.get("kind") == "dir"
            and before.get("inode") == after.get("inode")
        )
        # Directory metadata is not evidence of a fresh container producer.
        # Members are evaluated separately; touching an empty failed output
        # must preserve its creator just as touching a file preserves content.
        if same_content_object or same_directory_object:
            out = [({**producer, "after": after,
                     "identity_adjustments": [*producer.get("identity_adjustments", []), str(row.get("action_id"))]}
                    if _identity_matches(producer.get("after"), before)[0] else producer)
                   for producer in out]
            continue
        out.append({"action_id": str(row.get("action_id")), "after": frozen.get("after"),
                    "basis": frozen.get("basis"), "before_unknown": bool(frozen.get("before_unknown"))})
    return out


def _producers(records: list[dict[str, Any]], path: Path) -> list[str]:
    return [row["action_id"] for row in _producer_rows(records, path)]


def _identity_matches(frozen: dict[str, Any] | None, now: dict[str, Any]) -> tuple[bool, str]:
    """冻结的 after 身份与当前身份是不是同一个东西。

    挡住的是：action 写出了文件，之后又被别的东西覆盖，finish 只看"这条路径被动过"
    就会把别人的内容当成这次的产物（Codex 21 号 P0-B）。
    """
    if not isinstance(frozen, dict) or not frozen:
        return False, "no_frozen_identity"
    return _capture.same_identity(frozen, now)


def _file_evidence(state: Any, records: list[dict[str, Any]], ob: dict[str, Any], *,
                   allow_input_reference: bool = False,
                   before_check: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        path = _obligations.resolve_path(state, ob["path"])
    except (ValueError, OSError, RuntimeError) as exc:
        return {"passed": False, "gap": "path_unavailable", "evidence": {"path": ob["path"], "error": str(exc)}}
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    rows = _producer_rows(records, path) if exists else []
    evidence = {"path": str(path), "exists": exists, "bytes": size,
                "producers": [r["action_id"] for r in rows]}
    if not exists:
        return {"passed": False, "gap": "missing", "evidence": evidence}
    if ob.get("non_empty", True) and size == 0:
        return {"passed": False, "gap": "empty", "evidence": evidence}
    reference = _obligations.input_reference(state, path) if allow_input_reference else None
    if reference is not None:
        now = _capture.identity(path)
        same, basis = _identity_matches(before_check, now) if before_check is not None else (True, "current")
        stable = now.get("kind") == "file" and same
        incomplete = (now.get("kind") == "error" or (before_check or {}).get("kind") == "error"
                      or basis == "hash_unavailable")
        evidence.update(input_reference=reference, identity_now=now, before_check=before_check,
                        object_kind="existing_input_reference",
                        limitation="identity observed during verification, not a run-start immutability proof")
        return {"passed": stable, "gap": None if stable else (
            "input_observation_incomplete_during_check" if incomplete else "input_changed_during_check"),
                "evidence": evidence}
    if not rows:
        return {"passed": False, "gap": "unattributed", "evidence": evidence}
    now = _capture.identity(path)
    matches = [r for r in rows if _identity_matches(r.get("after"), now)[0]]
    evidence["identity_now"] = {k: now.get(k) for k in ("kind", "size", "sha256", "inode", "mtime_ns")}
    evidence["identity_basis"] = [r.get("basis") for r in rows]
    if not matches:
        evidence["frozen_identities"] = [r.get("after") for r in rows][:3]
        return {"passed": False, "gap": "identity_changed_since_the_action",
                "evidence": evidence}
    if before_check is not None:
        same, basis = _identity_matches(before_check, now)
        if not same:
            evidence.update(before_check=before_check, window_identity_basis=basis)
            gap = ("output_observation_incomplete_during_check" if basis == "hash_unavailable"
                   or before_check.get("kind") == "error" or now.get("kind") == "error"
                   else "output_changed_during_check")
            return {"passed": False, "gap": gap, "evidence": evidence}
    evidence["producers"] = [r["action_id"] for r in matches]
    evidence["identity_adjustments"] = [aid for r in matches for aid in r.get("identity_adjustments", [])]
    return {"passed": True, "evidence": evidence}


def _empty_directory_evidence(state: Any, records: list[dict[str, Any]], path: Path,
                              current: dict[str, Any], *,
                              before_check: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence: dict[str, Any] = {"path": str(path), "identity_now": current,
                                "producers": [], "identity_adjustments": []}
    reference = _obligations.input_reference(state, path)
    if reference is not None:
        evidence["input_reference"] = reference
        stable = before_check is None or _identity_matches(before_check, current)[0]
        return {"passed": stable, "gap": None if stable else "input_changed_during_check", "evidence": evidence}
    rows = [row for row in _producer_rows(records, path)
            if _identity_matches(row.get("after"), current)[0]]
    evidence["producers"] = [row["action_id"] for row in rows]
    evidence["identity_adjustments"] = [aid for row in rows for aid in row.get("identity_adjustments", [])]
    return {"passed": bool(rows), "gap": None if rows else "unattributed_directory", "evidence": evidence}


def _directory_evidence(state: Any, records: list[dict[str, Any]], path: Path, *,
                        before_check: dict[str, Any] | None = None,
                        before_members: dict[str, Any] | None = None) -> dict[str, Any]:
    """A directory is a bounded collection, not a file nor a provenance waiver.

    This is a C view of existing B observations, never another manifest owner.
    Nonempty containers are supported by the actual members' identities and
    lineage. Empty output directories need their own observed creation/change;
    an existing input directory remains an input reference, not a new result.
    """
    current = _capture.identity(path)
    evidence: dict[str, Any] = {
        "path": str(path), "exists": current.get("kind") != "missing",
        "object_kind": "directory", "identity_now": current,
        "producers": [], "identity_adjustments": [], "members": [],
    }
    if current.get("kind") != "dir":
        return {"passed": False, "gap": "object_kind_changed", "evidence": evidence}
    snapshot = _capture.snapshot([], [path], include_directories=True)
    coverage = snapshot.get("root_coverage") or []
    evidence["coverage"] = coverage
    evidence["truncated"] = bool(snapshot.get("truncated"))
    evidence["limitation"] = "bounded member observations; directory identity alone does not prove member contents"
    gaps = []
    producers: set[str] = set()
    adjustments: set[str] = set()
    input_root = _obligations.input_reference(state, path)
    entries = snapshot.get("entries") or {}
    previous_entries = (before_members or {}).get("entries") or {}
    # Only leaf directories are independent empty objects. Intermediate
    # containers derive their delivered contents from their actual members;
    # requiring a fresh producer for every old parent would invent a task.
    parents_with_members = {str(Path(value).parent) for value in entries if Path(value) != path}
    # A subtree omitted by capture cannot be silently claimed as a verified
    # part of a whole-directory delivery. No recursive symlink traversal.
    after_complete = bool(coverage) and not snapshot.get("truncated") and not any(
        row.get("status") != "complete" for row in coverage)
    if not after_complete:
        gaps.append({"path": str(path), "gap": "directory_observation_incomplete"})
    before_complete = bool(before_members and before_members.get("root_coverage")) and not (
        before_members.get("snapshot_error") or before_members.get("truncated") or any(
            row.get("status") != "complete" for row in before_members.get("root_coverage", [])))
    if not before_complete:
        gaps.append({"path": str(path), "gap": "directory_before_observation_incomplete"})
    for value, observed in entries.items():
        member = Path(value)
        kind = observed.get("kind")
        if kind == "dir" and value in parents_with_members:
            continue
        if kind == "dir":
            row = _empty_directory_evidence(state, records, member, observed,
                    before_check=previous_entries.get(value))
        elif kind != "file":
            row = {"passed": False, "gap": "member_kind_unverified",
                   "evidence": {"path": value, "identity_now": observed}}
        else:
            row = _file_evidence(state, records, {"path": value, "non_empty": False},
                                 allow_input_reference=True,
                                 before_check=previous_entries.get(value))
        facts = row["evidence"]
        producers.update(facts.get("producers") or [])
        adjustments.update(facts.get("identity_adjustments") or [])
        evidence["members"].append({"passed": row["passed"], "gap": row.get("gap"), **facts})
        if not row["passed"]:
            gaps.append({"path": value, "gap": row.get("gap")})
    if input_root:
        evidence["input_reference"] = input_root
    # Input/output provenance differs, but a passed check refers to the same
    # observed object in either case. Checking only surviving output members
    # would let a directory lose required contents after the check succeeded.
    # These are existing bounded observations, not an atomic filesystem lease.
    if before_complete and after_complete and set(previous_entries) != set(entries):
        gaps.append({"path": str(path), "gap": "directory_members_changed_during_check"})
    for value in previous_entries.keys() & entries.keys():
        previous, observed = previous_entries[value], entries[value]
        if (value in parents_with_members and previous.get("kind") == observed.get("kind") == "dir"):
            # A nonempty container's mtime is not its members' content.
            same, basis = previous.get("inode") == observed.get("inode"), "directory_inode"
        else:
            same, basis = _identity_matches(previous, observed)
        if not same:
            gap = ("directory_member_observation_incomplete" if basis == "hash_unavailable"
                   or previous.get("kind") == "error" or observed.get("kind") == "error"
                   else "directory_member_changed_during_check")
            gaps.append({"path": value, "gap": gap, "basis": basis})
    evidence["producers"] = sorted(producers)
    evidence["identity_adjustments"] = sorted(adjustments)
    evidence["member_count"] = len(evidence["members"])
    evidence["member_gaps"] = gaps
    return {"passed": not gaps, "gap": "directory_members_unverified" if gaps else None,
            "evidence": evidence}


def _glob_evidence(state: Any, records: list[dict[str, Any]], ob: dict[str, Any]) -> dict[str, Any]:
    try:
        pattern = str(_obligations.resolve_path(state, ob["pattern"]))
    except (ValueError, OSError, RuntimeError) as exc:
        return {"passed": False, "gap": "path_unavailable", "evidence": {"pattern": ob["pattern"], "error": str(exc)}}
    matches = sorted(Path(p) for p in _glob.glob(pattern, recursive=True) if Path(p).is_file())
    candidates: dict[str, dict[str, Any]] = {}
    fatal_actions = {str(row.get("action_id")) for row in records
                     if row.get("kind") == "action_settled" and row.get("fatal_signals")}
    for path in matches:
        candidates[str(path)] = _file_evidence(
            state, records, {"path": str(path), "non_empty": False},
            allow_input_reference=ob.get("source") == "model_interpretation")
    attributed = [p for p, row in candidates.items() if row["passed"] and row["evidence"].get("producers")]
    reference_paths = {p for p, row in candidates.items() if row["passed"] and row["evidence"].get("input_reference")}
    unattributed = [str(p) for p in matches if str(p) not in attributed and str(p) not in reference_paths]
    eligible = [p for p, row in candidates.items() if row["passed"] and not fatal_actions.intersection(
        [*row["evidence"].get("producers", []), *row["evidence"].get("identity_adjustments", [])])]
    # Prefer a settled zero-exit witness when several candidates satisfy the
    # same predicate, without rejecting a valid nonzero-exit candidate when
    # it is needed. This is evidence selection, not another acceptance gate.
    nonzero = {str(row.get("action_id")) for row in records
               if row.get("kind") == "action_settled" and row.get("exit_code") not in (0, None)}
    eligible.sort(key=lambda p: bool(nonzero.intersection(
        [*candidates[p]["evidence"].get("producers", []),
         *candidates[p]["evidence"].get("identity_adjustments", [])])))
    minimum = int(ob.get("min_count") or 1)
    selected = eligible[:minimum]
    passed = len(selected) >= minimum
    # When the predicate cannot be met, retain observed failed candidates as
    # explanatory facts; when it can, unselected matches are not deliverables.
    witness_paths = selected if passed else list(candidates)
    witnesses = [candidates[p]["evidence"] for p in witness_paths]
    evidence = {"pattern": pattern, "matched": len(matches), "attributed": attributed[:50],
                "unattributed": unattributed[:50], "min_count": minimum,
                "selected": selected, "unselected_count": len(matches) - len(selected),
                "producers": sorted({aid for row in witnesses for aid in row.get("producers", [])}),
                "identity_adjustments": sorted({aid for row in witnesses for aid in row.get("identity_adjustments", [])}),
                "input_references": [candidates[p]["evidence"] for p in selected if p in reference_paths],
                "selected_objects": [{"path": p, "before_check": _capture.identity(Path(p))} for p in selected]}
    return {"passed": passed, "gap": None if passed else "missing" if not matches else "unattributed", "evidence": evidence}


def read_only_roots(state: Any, cwd: str | None) -> tuple[list[Path], list[Path]]:
    """检查命令的只读执行根（Codex 21 号 P0-E）。

    ``sandbox_write_targets=[]`` 并不等于只读：``bash_sandbox_roots`` 仍会按 cwd 的
    可写角色加进 writable roots，而 ``_exec_and_log`` 又要求至少有一个可写根。所以
    这里把该 run 的**所有**角色根压成只读，另给一个空的、用完即弃的 scratch 作为唯一
    可写根：命令跑得起来，往交付物或源码写就是 EACCES，写进 scratch 的东西不影响任何
    交付判定，并在证据里如实披露。
    """
    try:
        from .safe_bash import _sandbox_roots_for_payload
    except ImportError:  # pragma: no cover
        from tools.safe_bash import _sandbox_roots_for_payload  # type: ignore
    scratch = Path(state.root) / "ledger" / "check_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        writable, readonly = _sandbox_roots_for_payload(
            state, cwd or str(state.root), sandbox_profile="bash", authorized_targets=None)
    except Exception:
        writable, readonly = [], [Path(state.root)]
    read_only = []
    for root in [*writable, *readonly]:
        root_path = Path(str(root))
        if root_path == scratch or scratch in root_path.parents:
            continue
        if root_path not in read_only:
            read_only.append(root_path)
    return [scratch], read_only


async def _default_check_runner(state: Any, ob: dict[str, Any]) -> dict[str, Any]:
    """检查命令走与 safe_run_bash 同一条沙箱路径，但用只读执行根，并进账本。"""
    try:
        from .safe_bash import _exec_and_log, _bash_static_validity_block  # type: ignore
        from .execution_guard import classify_high_risk
    except ImportError:  # pragma: no cover
        from tools.safe_bash import _exec_and_log, _bash_static_validity_block  # type: ignore
        from tools.execution_guard import classify_high_risk
    # A read-only filesystem does not prevent signals or external control.
    # Reuse execution's safety analysis; do not let verification be an alternate
    # launch/approval channel. Risky work belongs in the normal authorized tool,
    # after which its evidence can be checked here without repeating the effect.
    admission = _bash_static_validity_block(
        state, ob["command"], expected_duration_s=None, check_after_seconds=None)
    risk = classify_high_risk(ob["command"], mode="shell")
    if admission is not None or risk is not None:
        return {"exit_code": None, "executed": False, "stdout_tail": "", "stderr_tail": "",
                "error": "核验命令不满足既有执行边界或包含高风险动作；实际操作请走原授权工具，这里只检查其结果，不重做副作用。",
                "admission": admission, "risk_category": risk}
    cwd = str(_obligations.resolve_path(state, ob.get("cwd") or "."))
    roots = read_only_roots(state, cwd)
    result = await _exec_and_log(state, ob["command"], timeout=int(ob.get("timeout_s") or 120),
                                 cwd=cwd, sandbox_profile="bash", sandbox_roots=roots, _read_only_check=True)
    return {"exit_code": result.get("returncode"),
            "executed": type(result.get("returncode")) is int,
            "stdout_tail": str(result.get("stdout_tail") or result.get("stdout") or "")[-2000:],
            "stderr_tail": str(result.get("stderr_tail") or result.get("stderr") or "")[-2000:],
            "error": result.get("error"),
            "execution_profile": {"writable_roots": [str(p) for p in roots[0]],
                                  "read_only_roots": [str(p) for p in roots[1]][:12],
                                  "note": "检查命令只有一个用完即弃的 scratch 可写；其余全部只读"}}


_check_runner: CheckRunner = _default_check_runner


async def _check_evidence(state: Any, led: _ledger.Ledger, ob: dict[str, Any]) -> dict[str, Any]:
    action_id = f"check-{ob['id']}"
    records = led.records()
    used_ids = {row.get("action_id") for row in records}
    if action_id in used_ids:
        # Rechecking with another method is a new physical execution, never an
        # overwrite of the previous check's result (including failed results).
        action_id = f"{action_id}-{max((row['seq'] for row in records), default=0) + 1}"
        while action_id in used_ids:
            action_id += "_"
    led.append("action_started", action_id=action_id, tool="finish.check", command=ob["command"],
               effect_class="support", namespace=None, work_root=ob.get("cwd"),
               verification_method=ob)
    try:
        run = await _check_runner(state, ob)
    except Exception as exc:  # 检查命令自己炸了：如实记，不冒充通过
        run = {"exit_code": None, "error": f"{type(exc).__name__}: {exc}", "stdout_tail": "", "stderr_tail": ""}
    led.append("action_settled", action_id=action_id, state="settled", exit_code=run.get("exit_code"),
               error=run.get("error"), executed=run.get("executed", type(run.get("exit_code")) is int))
    expected_exit = int(ob.get("expect_exit", 0))
    marker = ob.get("expect_stdout_contains")
    exit_ok = run.get("exit_code") == expected_exit and not run.get("error")
    marker_ok = (marker is None) or (marker in (run.get("stdout_tail") or ""))
    evidence = {"action_id": action_id, "command": ob["command"], "exit_code": run.get("exit_code"), "expected_exit": expected_exit,
                "marker": marker, "marker_found": marker_ok if marker else None,
                "stdout_tail": (run.get("stdout_tail") or "")[-400:], "error": run.get("error"),
                "stderr_tail": (run.get("stderr_tail") or "")[-2000:],
                "executed": run.get("executed", type(run.get("exit_code")) is int),
                "admission": run.get("admission"), "risk_category": run.get("risk_category"),
                "execution_profile": run.get("execution_profile")}
    if exit_ok and marker_ok:
        return {"passed": True, "evidence": evidence}
    return {"passed": False, "gap": "check_failed", "evidence": evidence}


# ── 矛盾 ─────────────────────────────────────────────────────────────────────

def _job_contradictions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = _ledger.project_jobs(records)
    retries: dict[str, list[str]] = {}
    for row in records:
        if row.get("kind") == "intent" and row.get("retry_of"):
            retries.setdefault(str(row["retry_of"]), []).append(str(row.get("nonce")))
    out: list[dict[str, Any]] = []
    for nonce, job in jobs.items():
        if job["state"] in {"intent", "submitted"}:
            out.append({"name": "jobs_open", "job": nonce, "external_id": job.get("external_id"),
                        "reason": "作业尚未终态"})
        elif job["state"] == "unknown":
            out.append({"name": "jobs_unknown", "job": nonce, "reason": job.get("unknown_reason")})
        elif job["state"] == "terminal":
            derived = str((job.get("terminal") or {}).get("derived_state") or "")
            if derived in _SUCCESS_STATES:
                continue
            superseded = any(jobs.get(r, {}).get("state") == "terminal"
                             and str((jobs[r].get("terminal") or {}).get("derived_state") or "") in _SUCCESS_STATES
                             for r in retries.get(nonce, []))
            if not superseded:
                out.append({"name": "job_failed_unsuperseded", "job": nonce, "external_id": job.get("external_id"),
                            "derived_state": derived, "reason": "作业以非成功终态结束且没有成功的重试"})
    return out


def _action_contradictions(
    records: list[dict[str, Any]], evaluation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Keep action outcome, output provenance and task acceptance distinct.

    A nonzero composite command may have produced valid outputs before an
    auxiliary command failed. Its exit remains in B, not a blanket rejection
    of everything it wrote. Required checks determine task acceptance; open
    actions and applicable fatal evidence still contradict completion. Neither
    a passing check nor a later command rewrites the original producer/exit.
    """
    actions = _ledger.project_actions(records)
    classes = {}
    fatal = {}
    for row in records:
        action_id = str(row.get("action_id") or "")
        if row.get("kind") == "action_started":
            classes[action_id] = str(row.get("effect_class") or "experiment")
        if row.get("kind") == "action_settled" and row.get("fatal_signals"):
            fatal[action_id] = list(row.get("fatal_signals") or [])
    evaluation = evaluation or {}
    current_lineage: set[str] = set()
    for check in evaluation.get("checks", []):
        if not check.get("required"):
            continue
        evidence = check.get("evidence") or {}
        if "producers" in evidence:
            producers = set(evidence.get("producers") or [])
            current_lineage.update(producers)
            current_lineage.update(evidence.get("identity_adjustments") or [])

    out: list[dict[str, Any]] = []
    for action in actions:
        aid = action["action_id"]
        if classes.get(aid, "experiment") != "experiment" or action.get("tool") == "finish.check":
            continue
        if action.get("state") == "started":
            out.append({"name": "action_open", "action": aid, "reason": "action 没有结束记录"})
            continue
        # Actual required execution/quality checks are evaluated separately.
        # Do not invent an extra "every producer exited zero" requirement.
        if aid in current_lineage and fatal.get(aid):
            out.append({"name": "fatal_signal",
                        "action": aid, "exit_code": action.get("exit_code"),
                        "fatal_signals": fatal.get(aid) or [],
                        "reason": "当前交付对象仍关联已观察到的 fatal 信号；核对相关结果与任务要求，不能靠无关成功命令消除。"})
    return out


# ── 谓词 ─────────────────────────────────────────────────────────────────────

async def evaluate(state: Any, led: _ledger.Ledger, verification: Any = None,
                   deliverables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    records = led.records()
    obligations = _obligations.frozen(state)
    interpreted = _task_verification.prepare(state, verification)
    if interpreted["checks"]:
        led.append("task_verification_proposed", **interpreted)
    checks: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    # Objects remain visible even when the model changes its checking method.
    # Formal required files are independently evaluated below, never exempted
    # by an input reference or a new interpretation in C.
    objects: dict[str, dict[str, Any]] = {}
    applicable = [*obligations, *interpreted["checks"]]
    object_items = [*(deliverables or []), *[ob for ob in applicable if ob["kind"] == "file" and ob["required"]]]
    # A glob is a candidate/min_count predicate, not a declaration that every
    # matching old file is a required deliverable. Its own evaluator retains
    # selected member identities and lineage; explicit objects remain required.
    for item in object_items:
        if not item.get("path"):
            continue
        try:
            path = _obligations.resolve_path(state, item["path"])
            reference = _obligations.input_reference(state, path)
            objects[str(path)] = {"path": str(path), "input_reference": reference,
                                  "before_check": _capture.identity(path)}
            if objects[str(path)]["before_check"].get("kind") == "dir":
                objects[str(path)]["before_members"] = _capture.snapshot([], [path], include_directories=True)
        except (ValueError, OSError, RuntimeError) as exc:
            objects[str(item["path"])] = {"path": str(item["path"]), "error": str(exc)}
    # Select necessary witnesses before ANY check executes. A check appearing
    # before a glob must not turn its post-check identity into a fake baseline.
    initial_globs = {}
    for ob in applicable:
        if ob["kind"] == "glob":
            row = _glob_evidence(state, records, ob)
            initial_globs[ob["id"]] = row
            if ob["required"]:
                for selected in row["evidence"].get("selected_objects", []):
                    objects.setdefault(selected["path"], selected)
    for ob in applicable:
        kind = ob["kind"]
        if kind == "task":
            # The task text is A, not an executable command. Methods supplied
            # in C must have actual evidence; they do not replace the text or
            # certify arbitrary natural-language semantic completeness.
            if not interpreted["checks"]:
                gaps.append({"obligation": ob["id"], "kind": kind, "gap": "task_verification_missing",
                             "evidence": {"source": ob["source"], "task": ob["text"]},
                             "next_action": "在 finish.verification 引用任务要求并给出可核产物或只读检查；无需重跑已完成载荷、补 artifact 或修改任务。"})
            continue
        if kind == "artifact":
            checks.append({"name": ob["id"], "passed": True, "required": ob["required"],
                           "evidence": {"artifact_type": ob["artifact_type"], "frozen_by": "finish"}})
            continue
        if kind == "file":
            row = _file_evidence(state, records, ob, allow_input_reference=ob.get("source") == "model_interpretation")
        elif kind == "glob":
            row = initial_globs[ob["id"]]
        else:
            row = await _check_evidence(state, led, ob)
        checks.append({"name": ob["id"], "passed": row["passed"], "required": ob["required"],
                       "evidence": row["evidence"]})
        if not row["passed"] and ob["required"]:
            gaps.append({"obligation": ob["id"], "kind": kind, "gap": row.get("gap"), "evidence": row["evidence"]})
    # A check is itself a ledgered support action. Re-read after all checks so
    # contradictions and the delivery snapshot describe one final B view.
    records = led.records()
    for index, obj in enumerate(objects.values()):
        name = f"delivery_object_{index}"
        path = Path(obj["path"])
        if obj.get("error"):
            row = {"passed": False, "gap": "path_unavailable", "evidence": obj}
        elif obj.get("before_check", {}).get("kind") == "dir":
            row = _directory_evidence(state, records, path, before_check=obj.get("before_check"),
                                      before_members=obj.get("before_members"))
        else:
            row = _file_evidence(state, records, {"path": str(path), "non_empty": False},
                                 allow_input_reference=True, before_check=obj.get("before_check"))
        checks.append({"name": name, "required": True, "assessment_kind": "delivery_object",
                       "passed": row["passed"], "evidence": row["evidence"]})
        if not row["passed"]:
            gaps.append({"obligation": name, "kind": "delivery_object", "gap": row.get("gap"), "evidence": row["evidence"]})
    contradictions = _job_contradictions(records) + _action_contradictions(
        records, {"obligations": obligations, "checks": checks, "task_verification": interpreted})
    # Object findings still constrain success, but are not additional task
    # requirements. Do not count the same required file a second time merely
    # because it is also listed as a deliverable.
    required = [c for c in checks if c["required"] and c["evidence"].get("frozen_by") != "finish"
                and c.get("assessment_kind") != "delivery_object"]
    satisfied = [c for c in required if c["passed"]]
    predicate = not gaps and not contradictions
    return {"obligations": obligations, "checks": checks, "gaps": gaps, "contradictions": contradictions,
            "task_verification": interpreted,
            "success_supported": predicate, "required_total": len(required), "required_satisfied": len(satisfied)}


def _failure_context(records: list[dict[str, Any]], evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """Bounded recovery context from B; not a new verdict or retry relation."""
    starts = {row["action_id"]: row for row in records if row.get("kind") == "action_started"}
    paths: dict[str, set[str]] = {}
    for check in evaluation.get("checks", []):
        evidence = check.get("evidence") or {}
        for obj in [evidence, *evidence.get("members", [])]:
            for aid in [*obj.get("producers", []), *obj.get("identity_adjustments", [])]:
                if obj.get("path"):
                    paths.setdefault(aid, set()).add(obj["path"])
    failed = [action for action in _ledger.project_actions(records)
              if action.get("exit_code") not in (0, None) and action.get("tool") != "finish.check"]
    failed.sort(key=lambda action: action["action_id"] not in paths)
    return [{
        "action": action["action_id"], "exit_code": action.get("exit_code"),
        "command": str(starts.get(action["action_id"], {}).get("command") or "")[:800],
        "paths": sorted(paths.get(action["action_id"], []))[:10],
        "meaning": "命令非零退出仍保留；不自动证明这些对象无效，也不代表失败已恢复。按实际任务条件核验。",
    } for action in failed[:8]]


def _reachable_outcomes(evaluation: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    if evaluation["required_satisfied"] > 0:
        out.append({"outcome": "partial", "when": "部分 required 已有正面证据，缺口逐条披露"})
    failure_like = [c for c in evaluation["contradictions"] if c["name"] in {
        "job_failed_unsuperseded", "action_failed_unsuperseded", "fatal_signal"}] \
        or [g for g in evaluation["gaps"] if g.get("gap") == "check_failed"]
    if failure_like:
        out.append({"outcome": "failed", "when": "有失败证据（作业 / action / 检查命令）"})
    out.append({"outcome": "blocked", "when": "外部原因阻塞，给出 blocker 说明"})
    out.append({"outcome": "cancelled", "when": "计划内停止"})
    return out


# ── ExperimentDeliveryV1 cutover ─────────────────────────────────────────────
# The persisted transaction manifest is the sole C authority.


class _FinishAssemblyError(ValueError):
    def __init__(self, code: str, detail: str, **facts: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.facts = facts

    def as_result(self) -> dict[str, Any]:
        return {"status": "error", "error_code": self.code,
                "error": self.detail, **self.facts}


def _read_manifest(state: Any) -> dict[str, Any] | None:
    """Compatibility read view; transaction code remains the sole manifest owner."""
    return _delivery_transaction.read_manifest(state)


def _project_authority(records: list[dict[str, Any]]) -> dict[str, Any]:
    authority = _run_bootstrap.project_run_authority(records)
    if not isinstance(authority, dict):
        raise _FinishAssemblyError(
            "run_authority_unavailable",
            "bootstrap is ready but its canonical run authority cannot be projected",
        )
    if int(authority.get("schema_version") or 0) != 1:
        raise _FinishAssemblyError(
            "run_authority_invalid", "unsupported or missing run authority schema",
            authority_schema_version=authority.get("schema_version"),
        )
    scope = authority.get("scope")
    scope_status = authority.get("scope_status")
    if scope not in {None, "operation", "scientific"}:
        raise _FinishAssemblyError(
            "run_authority_invalid", f"unsupported authority scope: {scope!r}")
    expected_status = "undecided" if scope is None else "resolved"
    if scope_status != expected_status:
        raise _FinishAssemblyError(
            "run_authority_invalid", "scope and scope_status disagree in run authority",
            scope=scope, scope_status=scope_status,
        )
    return authority


def _run_scope(state: Any, led: _ledger.Ledger | None = None) -> str | None:
    """Read-only compatibility view backed only by the canonical bootstrap A."""
    source = led or _ledger.open_ledger(state)
    authority = _run_bootstrap.project_run_authority(source.records())
    return authority.get("scope") if isinstance(authority, dict) else None


def _formal_input_from_authority(authority: dict[str, Any]) -> dict[str, Any]:
    prereg_status = str(authority.get("prereg_status") or "")
    if prereg_status in {"none", "omitted"}:
        return {"status": "none", "binding": None, "error": None}
    if prereg_status == "bound_valid":
        binding = authority.get("prereg")
        if not isinstance(binding, dict):
            raise _FinishAssemblyError(
                "run_authority_invalid", "bound_valid authority has no exact prereg binding")
        exact_binding = {
            "artifact_id": binding.get("artifact_id"),
            "version": binding.get("version"),
            "content_hash": binding.get("content_hash"),
        }
        return {"status": "valid", "binding": exact_binding, "error": None}
    status = {
        "pending": "pending",
        "invalid": "invalid",
        "legacy_unsupported": "unsupported",
        "mixed_conflict": "conflict",
    }.get(prereg_status)
    if status is None:
        raise _FinishAssemblyError(
            "run_authority_invalid", f"unsupported prereg_status: {prereg_status!r}")
    startup = authority.get("startup_blocker")
    startup = startup if isinstance(startup, dict) else {}
    code = str(startup.get("code") or f"formal_input_{status}").strip()
    reason = str(startup.get("reason") or authority.get("reason")
                 or f"formal input is {status}").strip()
    return {"status": status, "binding": None,
            "error": {"code": code, "reason": reason}}


def _authority_findings(
    authority: dict[str, Any], formal_input: dict[str, Any], scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    gaps: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    limitations: list[str] = []
    if scope == "unknown":
        limitations.append(
            "scope remains unknown; completion is evaluated against task evidence, "
            "not an operation/scientific label."
        )
    formal_error = formal_input.get("error")
    if isinstance(formal_error, dict):
        item = {"name": "formal_input_unavailable",
                "status": formal_input.get("status"), "error": formal_error}
        if formal_input.get("status") == "conflict":
            contradictions.append(item)
        else:
            gaps.append(item)
    startup = authority.get("startup_blocker")
    if isinstance(startup, dict) and not isinstance(formal_error, dict):
        item = {"name": "run_authority_blocker", "blocker": dict(startup)}
        if startup.get("code") == "scope_resolution_conflict":
            # A different descriptive label is not a changed task or evidence.
            # Keep the existing interpretation and disclose the disagreement.
            limitations.append(f"scope_resolution_conflict: {startup.get('reason')}")
        elif startup.get("code") == "run_authority_input_changed":
            contradictions.append(item)
        else:
            gaps.append(item)
    return gaps, contradictions, limitations


def _external_job_refs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for nonce, job in sorted(_ledger.project_jobs(records).items()):
        ref = {"nonce": nonce, "scheduler": job.get("scheduler"),
               "state": job.get("state"), "external_id": job.get("external_id")}
        if isinstance(job.get("terminal"), dict):
            ref["terminal"] = dict(job["terminal"])
        if job.get("unknown_reason") is not None:
            ref["unknown_reason"] = job.get("unknown_reason")
        if job.get("absent_reason") is not None:
            ref["absent_reason"] = job.get("absent_reason")
        refs.append(ref)
    return refs


def _open_effects(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    live_jobs = [
        {"nonce": nonce, "state": job.get("state"),
         "scheduler": job.get("scheduler"), "external_id": job.get("external_id"),
         **({"reason": job.get("unknown_reason")} if job.get("state") == "unknown" else {})}
        for nonce, job in sorted(_ledger.project_jobs(records).items())
        if job.get("state") in {"intent", "submitted", "unknown"}
    ]
    effect_classes = {
        str(row.get("action_id") or ""): str(row.get("effect_class") or "experiment")
        for row in records if row.get("kind") == "action_started"
    }
    open_actions = [
        {"action_id": action.get("action_id"), "tool": action.get("tool"),
         "work_root": action.get("work_root")}
        for action in _ledger.project_actions(records)
        if action.get("state") == "started"
        and action.get("tool") != "finish.check"
        and effect_classes.get(str(action.get("action_id") or ""), "experiment") == "experiment"
    ]
    return live_jobs, open_actions


def _live_effect_refusal(
    outcome: str, records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    live_jobs, open_actions = _open_effects(records)
    if not live_jobs and not open_actions:
        return None
    return {
        "status": "live_external_effect",
        "error_code": "terminal_closure_with_live_or_unknown_effect",
        "error": "the run still owns an open or unknown effect; settle, cancel, or formally hand it off before finish",
        "requested_outcome": outcome,
        "exits": [
            *([{"action": "wait_for_external_job",
                "when": "作业应继续运行：等到调度器终态入账后，再调用 finish"},
               {"action": "cancel_job",
                "when": "作业应停止：受管取消确认生效后，再调用 finish"}]
              if live_jobs else []),
            {"action": "report_blocker",
             "when": "当前无法确认或结束开放动作：保存作业身份与恢复条件，不能声称已取消或已交接"},
        ],
        "live_jobs": live_jobs,
        "open_actions": open_actions,
        "retryable": True,
        "owner": "experiment_runtime",
    }


def _parameter_value_conflict(authority: dict[str, Any], context: dict[str, Any]) -> bool:
    """Only an explicit different value can contradict this exact commitment.

    Omitted witness fields do not prove omitted execution parameters, and an
    unregistered field has no promised value to contradict.  Keep those
    comparison failures as limitations, not as proof of a contradictory value.
    """
    comparison = context.get("parameter_comparison") or {}
    return bool(
        authority.get("prereg_status") == "bound_valid"
        and context.get("prereg_binding") == authority.get("prereg")
        and comparison.get("passed") is False
        and comparison.get("mismatched_parameters")
    )


def _parameter_witness_limitations(action_id: str, comparison: dict[str, Any]) -> list[str]:
    """Preserve incomplete comparisons without inventing actual parameter use."""
    limitations = []
    if missing := comparison.get("missing_expected_parameters"):
        limitations.append(
            f"{action_id}: parameter witness omits preregistered fields {missing}; "
            "actual parameter use for those fields was not verified."
        )
    if extra := comparison.get("unexpected_execution_parameters"):
        limitations.append(
            f"{action_id}: parameter witness includes unregistered fields {extra}; "
            "their actual parameter use and scientific effect were not verified. "
            "Unregistered fields alone do not establish a contradictory value."
        )
    return limitations


def _execution_lineage_findings(
    authority: dict[str, Any], records: list[dict[str, Any]], evaluation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Read the current deliverables' B witnesses, never a run-wide params cache.

    A match only compares tool arguments with a frozen commitment. Missing
    observation is disclosed, not invented as a match or made a new form the
    model must fill. Other preparation actions do not decide this delivery.
    """
    producer_ids = {aid for check in evaluation.get("checks", []) if check.get("required")
                    for aid in check.get("evidence", {}).get("producers", [])}
    modifier_ids = {aid for check in evaluation.get("checks", []) if check.get("required")
                    for aid in check.get("evidence", {}).get("identity_adjustments", [])}
    contexts = {str(row.get("action_id")): row.get("execution_context")
                for row in records if row.get("kind") == "action_started"}
    intents = {str(row.get("nonce")): row.get("execution_context")
               for row in records if row.get("kind") == "intent"}
    for row in records:
        if row.get("kind") == "job_terminal" and row.get("output_observation"):
            contexts[str(row["output_observation"])] = intents.get(str(row.get("nonce")))
    lineage, gaps, contradictions, limitations = [], [], [], []
    for row in records:
        if row.get("kind") == "job_terminal":
            observation = row.get("output_diff") or {}
            if observation.get("limitation"):
                limitations.append(f"{row.get('nonce')}: {observation['limitation']}")
    has_parameter_contract = authority.get("prereg_status") == "bound_valid"
    if not producer_ids and (has_parameter_contract or authority.get("scope") == "scientific"):
        limitations.append("No result-producer lineage was recorded for the required checks; actual parameter use was not verified.")
        unresolved = []
        for aid, context in contexts.items():
            if not isinstance(context, dict) or authority.get("prereg_status") != "bound_valid":
                continue
            comparison = context.get("parameter_comparison") or {}
            if context.get("prereg_binding") == authority.get("prereg"):
                limitations.extend(_parameter_witness_limitations(aid, comparison))
            if _parameter_value_conflict(authority, context):
                unresolved.append({"action": aid, "comparison": comparison})
        if unresolved:
            # This is not a claim that all preparation is relevant. There is
            # no B relation establishing relevance or irrelevance here, so a
            # known conflict cannot simply disappear from a success claim.
            gaps.append({"name": "parameter_lineage_unresolved",
                         "candidates": unresolved,
                         "reason": "required checks do not identify the result producer; existing parameter conflicts cannot be resolved by unrelated matches"})
    for aid in sorted(producer_ids | modifier_ids):
        context = contexts.get(aid)
        context = context if isinstance(context, dict) else {}
        comparison = context.get("parameter_comparison") or {"applicable": False, "passed": None}
        source = context.get("parameter_source", "not_observed")
        lineage.append({"action_id": aid, "parameter_source": source,
                        "relationship": "content_producer" if aid in producer_ids else "identity_modifier",
                        "prereg_binding": context.get("prereg_binding"),
                        "parameter_comparison": comparison,
                        "input_verification": context.get("input_verification", {"status": "not_observed"})})
        input_observation = context.get("input_verification") or {}
        if input_observation.get("status") == "structure_checked":
            limitations.append(f"{aid}: selected dataset structure and current artifact identity were inspected; formal dispatch binding, payload hashes and scientific task fitness were not verified.")
        elif (input_observation.get("audit") or {}).get("inspection_kind") == "registered_dataset_current_structure":
            limitations.append(f"{aid}: applicable registered-delivery source/receipt checks and current dataset identity/structure were repeated; package-member content hashes and scientific task fitness were not revalidated.")
        if not has_parameter_contract and authority.get("scope") != "scientific":
            continue
        if source != "tool_argument_witness":
            limitations.append(f"{aid}: no parameter-use witness was observed; output checks do not prove actual parameter use.")
        else:
            limitations.append(f"{aid}: parameters are tool-argument witnesses, not observations of the program's actual parameter use.")
        limitations.extend(_parameter_witness_limitations(aid, comparison))
        if comparison.get("passed") is False and not any(comparison.get(key) for key in (
                "mismatched_parameters", "missing_expected_parameters", "unexpected_execution_parameters")):
            limitations.append(f"{aid}: the recorded parameter claim could not be compared with a complete formal parameter contract.")
        # Only an explicit contradictory claim against this exact A is a
        # contradiction. No comparator, incomplete metadata or absent inputs
        # cannot be upgraded into a supposed observed deviation.
        if _parameter_value_conflict(authority, context):
            contradictions.append({"name": "producer_parameter_claim_conflict", "action": aid,
                                   "source": source, "comparison": comparison,
                                   "relationship": "content_producer" if aid in producer_ids else "identity_modifier",
                                   "reason": "the delivered output's observed lineage contains a parameter claim inconsistent with the bound preregistration"})
    return lineage, gaps, contradictions, limitations


def _execution_status(records: list[dict[str, Any]], evaluation: dict[str, Any] | None = None) -> str:
    attempted_job_events = {
        "submission_attempted", "job_bound", "job_terminal", "job_cancelled",
        "job_reconciled", "job_unknown",
    }
    check_actions = {row.get("action_id") for row in records
                     if row.get("kind") == "action_started" and row.get("tool") == "finish.check"}
    evaluation = evaluation or {}
    methods = [ob for ob in [*evaluation.get("obligations", []),
                            *(evaluation.get("task_verification") or {}).get("checks", [])]
               if ob.get("required") and ob.get("kind") not in {"artifact", "task"}]
    evidence_by_name = {c["name"]: c.get("evidence") or {} for c in evaluation.get("checks", [])}
    check_only_task = bool(methods) and all(
        ob["kind"] == "check"
        or evidence_by_name.get(ob["id"], {}).get("object_kind") == "existing_input_reference"
        or (ob["kind"] == "glob" and evidence_by_name.get(ob["id"], {}).get("input_references")
            and not evidence_by_name.get(ob["id"], {}).get("unattributed"))
        for ob in methods)
    for row in records:
        kind = row.get("kind")
        if kind in attempted_job_events:
            return "attempted"
        # Support writers also perform real work (for example, a requested
        # file or download). The frozen obligations and producer evidence
        # decide whether that work fulfils the task; its tool category does
        # not erase the execution fact.
        if kind == "action_started" and row.get("tool") != "finish.check":
            return "attempted"
        if (check_only_task and kind == "action_settled" and row.get("action_id") in check_actions
                and row.get("executed") is True):
            # A self-test can be the whole task. Count actual check execution,
            # not an admission refusal, an unexecuted intent, or inspection of
            # missing outputs from a task whose actual payload never ran.
            return "attempted"
    return "not_run"


def _effective_blocker(
    outcome: str, blocker: dict[str, Any] | None, authority: dict[str, Any],
) -> dict[str, Any] | None:
    if blocker is not None and not isinstance(blocker, dict):
        raise _FinishAssemblyError("finish_parameters_invalid", "blocker must be an object")
    selected = dict(blocker) if blocker else None
    if outcome == "blocked" and not selected:
        startup = authority.get("startup_blocker")
        if isinstance(startup, dict) and startup:
            selected = {
                "blocker_id": str(startup.get("code") or "run_authority_blocker"),
                "summary": str(startup.get("reason") or authority.get("reason")
                               or "run authority is blocked"),
            }
    if outcome == "blocked" and not selected:
        raise _FinishAssemblyError(
            "finish_parameters_invalid",
            "blocked needs blocker={'blocker_id'|'summary'} describing the external cause",
        )
    if outcome == "success" and selected is not None:
        raise _FinishAssemblyError(
            "finish_parameters_invalid", "success cannot carry a blocker")
    return selected


def _result_status(
    outcome: str, execution_status: str, evaluation: dict[str, Any],
) -> str:
    if outcome == "success":
        return "delivered"
    if execution_status == "attempted" and int(evaluation.get("required_satisfied") or 0) > 0:
        return "partial"
    return "unavailable"


def _delivery_and_evaluation(
    state: Any,
    *,
    outcome: str,
    summary: str,
    limitations: list[str],
    next_step: str,
    blocker: dict[str, Any] | None,
    authority: dict[str, Any],
    records: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = str(authority.get("scope") or "unknown")
    scope_status = "undecided" if scope == "unknown" else "resolved"
    formal_input = _formal_input_from_authority(authority)
    authority_gaps, authority_contradictions, authority_limitations = _authority_findings(
        authority, formal_input, scope)
    lineage, lineage_gaps, lineage_contradictions, lineage_limitations = _execution_lineage_findings(
        authority, records, evaluation)
    merged = {
        **evaluation,
        "execution_lineage": lineage,
        "gaps": [*list(evaluation.get("gaps") or []), *authority_gaps, *lineage_gaps],
        "contradictions": [*list(evaluation.get("contradictions") or []),
                           *authority_contradictions, *lineage_contradictions],
    }
    merged["success_supported"] = bool(
        evaluation.get("success_supported")
        and not authority_gaps and not authority_contradictions and not lineage_gaps and not lineage_contradictions)
    # A removed model interpretation is not a frozen requirement. Preserve
    # failed B facts and disclose the missing relevance proof without making
    # an old mistaken C association a new success gate.
    failed_ids = {str(row.get("action_id")) for row in records
                  if row.get("kind") == "action_settled" and
                  (row.get("exit_code") not in (0, None) or row.get("fatal_signals"))}
    blocked_ids = {row.get("action") for row in merged["contradictions"]}
    unbound_failures = sorted(failed_ids - blocked_ids)
    relevance_limitations = []
    if unbound_failures:
        relevance_limitations.append(
            f"Historical failed actions remain in raw_results ({len(unbound_failures)}; "
            f"examples: {', '.join(unbound_failures[:5])}). Their nonzero exits are not rewritten "
            "by task acceptance; a valid delivered output does not mean its whole producing command succeeded. "
            "A later check alone does not prove those failures unrelated or repaired: arbitrary shell read dependencies "
            "are not observed. Mechanical provenance guarantees cover current explicit "
            "objects and formal conditions; task coverage remains the model's interpretation.")
    execution_status = _execution_status(records, evaluation)
    delivery = {
        "schema": _delivery_transaction.DELIVERY_SCHEMA,
        "delivery_id": _delivery_transaction.delivery_id(state),
        "delivery_owner": "finish",
        "scope": scope,
        "scope_status": scope_status,
        "outcome": outcome,
        "execution_status": execution_status,
        "result_status": _result_status(outcome, execution_status, merged),
        "summary": summary,
        "gaps": merged["gaps"],
        "contradictions": merged["contradictions"],
        "formal_input": formal_input,
        "limitations": list(dict.fromkeys([
            *limitations, *authority_limitations, *lineage_limitations, *relevance_limitations,
            *([_task_verification.LIMITATION]
              if (evaluation.get("task_verification") or {}).get("checks") else []),
        ])),
        "external_job_refs": _external_job_refs(records),
        "next_step": next_step.strip() or None,
        "blocker": blocker,
    }
    return delivery, merged


def _json_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False, default=str) + "\n"


def _triplet_contents(
    state: Any,
    *,
    delivery: dict[str, Any],
    authority: dict[str, Any],
    records: list[dict[str, Any]],
    evaluation: dict[str, Any],
    declared_deliverables: list[dict[str, Any]],
    finish_request: dict[str, Any] | None = None,
) -> dict[str, str]:
    through_seq = max((int(row.get("seq") or 0) for row in records), default=0)
    ledger_pointer = {
        "path": str((_ledger.ledger_dir(state) / "events.jsonl").resolve()),
        "through_seq": through_seq,
    }
    try:
        from .remote_admission import isolation_disclosure
    except ImportError:  # pragma: no cover - standalone node bootstrap
        from tools.remote_admission import isolation_disclosure  # type: ignore
    write_boundary = []
    schedulers = {str(job.get("scheduler") or "local")
                  for job in delivery["external_job_refs"]} or {"local"}
    for scheduler in sorted(schedulers):
        try:
            write_boundary.append(isolation_disclosure(scheduler))
        except Exception as exc:
            write_boundary.append({
                "scheduler": scheduler, "remote_write_isolation": "unknown",
                "enforced": False,
                "notes": [f"isolation disclosure unavailable: {type(exc).__name__}: {exc}"],
            })
    raw = {
        "schema": "ExperimentDeliveryRawV1",
        "delivery": delivery,
        "run_authority": authority,
        "ledger_pointer": ledger_pointer,
        "ledger_records": records,
        "write_boundary": write_boundary,
        "obligations": evaluation.get("obligations") or [],
        "task_verification": evaluation.get("task_verification"),
        "declared_deliverables": declared_deliverables,
        "finish_request": finish_request,
    }
    clean = {
        "schema": "ExperimentDeliveryCleanV1",
        "delivery": delivery,
        "ledger_pointer": ledger_pointer,
        "write_boundary": write_boundary,
        "evaluation": {
            key: evaluation.get(key) for key in (
                "checks", "gaps", "contradictions", "success_supported",
                "required_total", "required_satisfied",
                "execution_lineage",
                "task_verification",
            )
        },
        "jobs": _external_job_refs(records),
        "actions": _ledger.project_actions(records),
    }
    formal = delivery["formal_input"]
    log = "\n".join([
        "# Experiment delivery",
        "",
        f"- delivery_id: `{delivery['delivery_id']}`",
        f"- scope: `{delivery['scope']}` ({delivery['scope_status']})",
        f"- outcome: `{delivery['outcome']}`",
        f"- execution_status: `{delivery['execution_status']}`",
        f"- result_status: `{delivery['result_status']}`",
        f"- formal_input_status: `{formal['status']}`",
        f"- ledger_through_seq: `{through_seq}`",
        "",
        "## Summary",
        "",
        delivery["summary"],
        "",
        "## Formal input and evidence sources",
        "",
        "```json",
        json.dumps(formal, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "Action-scoped parameters, input observations and result-producer comparisons are in raw_results and clean_results.",
        "",
        "## Limitations and next step",
        "",
        *[f"- {item}" for item in delivery["limitations"]],
        "",
        delivery.get("next_step") or "No next step specified.",
        "",
        "## Write boundary disclosure",
        "",
        "```json",
        json.dumps(write_boundary, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Gaps and contradictions",
        "",
        "```json",
        json.dumps({"gaps": delivery["gaps"],
                    "contradictions": delivery["contradictions"]},
                   ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "```",
        "",
    ])
    return {"raw_results": _json_payload(raw),
            "clean_results": _json_payload(clean), "experiment_log": log}


def _adapt_settled_result(
    settled: dict[str, Any], evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(settled)
    delivery = result.get("delivery")
    if isinstance(delivery, dict):
        result.update({
            "outcome": delivery.get("outcome"),
            "declared_outcome": delivery.get("outcome"),
            "gaps": list(delivery.get("gaps") or []),
            "contradictions": list(delivery.get("contradictions") or []),
        })
    if isinstance(evaluation, dict):
        result["required_satisfied"] = (
            f"{int(evaluation.get('required_satisfied') or 0)}/"
            f"{int(evaluation.get('required_total') or 0)}"
        )
    return result


def _owner_ledger(state: Any) -> tuple[_ledger.Ledger | None, dict[str, Any] | None]:
    try:
        led = _ledger.open_ledger(state, write=True)
    except Exception as exc:
        return None, {"status": "error", "error_code": "ledger_unavailable",
                      "error": f"{type(exc).__name__}: {exc}"}
    if led.read_only:
        return None, {"status": "error", "error_code": "not_run_owner",
                      "error": f"finish requires the run owner: {led.reason}"}
    return led, None


def _delivery_changed(
    state: Any, persisted: dict[str, Any], request: dict[str, Any], records: list[dict[str, Any]],
) -> bool:
    """A replay reads old C; a correction evaluates A/B again.

    Reuse the sealed object observations, not every ledger-tail event and not
    a new provenance source. This is bounded cache validation, not proof of
    arbitrary shell dependencies or continuous filesystem immutability.
    """
    raw = json.loads(persisted["artifacts"]["raw_results"]["content"])
    clean = json.loads(persisted["artifacts"]["clean_results"]["content"])
    if _delivery_transaction.canonical_digest(raw.get("finish_request")) != _delivery_transaction.canonical_digest(request):
        return True
    if raw.get("run_authority") != _project_authority(records):
        return True
    if persisted["delivery"]["external_job_refs"] != _external_job_refs(records):
        return True
    seen: set[str] = set()
    for check in (clean.get("evaluation") or {}).get("checks", []):
        evidence = check.get("evidence") or {}
        value = evidence.get("path")
        if not value or value in seen:
            continue
        seen.add(value)
        path = _obligations.resolve_path(state, value)
        now = _capture.identity(path)
        old = evidence.get("identity_now")
        if evidence.get("object_kind") == "directory":
            snapshot = _capture.snapshot([], [path], include_directories=True)
            if snapshot.get("truncated") or any(
                    row.get("status") != "complete" for row in snapshot.get("root_coverage", [])):
                return True
            entries = snapshot.get("entries") or {}
            parents = {str(Path(item).parent) for item in entries if item != value}
            leaves = {key: identity for key, identity in entries.items()
                      if identity.get("kind") != "dir" or key not in parents}
            members = {row["path"]: row for row in evidence.get("members", [])}
            if set(leaves) != set(members):
                return True
            if any(not _identity_matches(members[key].get("identity_now"), identity)[0]
                   for key, identity in leaves.items()):
                return True
            if not isinstance(old, dict) or old.get("inode") != now.get("inode"):
                return True
        elif isinstance(old, dict):
            if not _identity_matches(old, now)[0]:
                return True
        elif evidence.get("exists") is False:
            if now.get("kind") != "missing":
                return True
        else:
            # An incomplete old observation cannot prove an unchanged object.
            return True
    if not seen:
        through = int((raw.get("ledger_pointer") or {}).get("through_seq") or 0)
        # With no file objects, new actual experiment actions may be the
        # result (self-test, measurement). Pure observations are not revisions.
        return any(row.get("kind") == "action_started"
                   and row.get("effect_class") == "experiment"
                   and int(row.get("seq") or 0) > through for row in records)
    return False


def _child_obligation_effect(delivery: dict[str, Any]) -> tuple[str, str]:
    """封存交付的 (scope, outcome) → 这一趟对父任务目标的效应（#1099）。

    只往保守一侧取：交付不成立的什么也没关掉；scientific 只有 success 才算交出
    关闭科学目标的证据，partial 推进了它但不关；operation 与 scope 未定的 run
    再成功也只是运维/构建子任务——纯构建 child 可以 completed，父的科学目标
    不得被节点侧标完成。
    """
    from core.obligation_effect import (
        CONTRIBUTION_EVIDENCE, CONTRIBUTION_NONE,
        EFFECT_FULL, EFFECT_NONE, EFFECT_OPERATIONAL,
    )

    outcome = str(delivery.get("outcome") or "")
    if outcome not in {"success", "partial"}:
        return EFFECT_NONE, CONTRIBUTION_NONE
    if delivery.get("scope") == "scientific":
        return EFFECT_FULL, (CONTRIBUTION_EVIDENCE if outcome == "success" else CONTRIBUTION_NONE)
    return EFFECT_OPERATIONAL, CONTRIBUTION_NONE


def _record_core_delivery(state: Any, result: dict[str, Any]) -> None:
    """从同一封存 C 派生 Core 的任务结局与 child obligation effect。

    任务是否完成（#1086）与对上游科学目标的贡献（#1099）是不同轴。
    fresh、恢复重放和修订都只读实际封存结果，不认本次请求实参。
    """
    if not isinstance(result, dict) or result.get("status") != "success":
        return
    delivery = result.get("delivery")
    if not isinstance(delivery, dict):
        return
    from core.task_outcome import record_task_outcome

    # 此投影参与 Core 的完成判定，不能静默漏记。异常时保留已封存 C，
    # 下次 finish 重放即可补记；不改证据、不重跑载荷、不清除其他 blocker。
    record_task_outcome(
        state, delivery["outcome"], detail=delivery.get("summary", ""),
        reported_by="experiment.finish",
    )
    effect, contribution = _child_obligation_effect(delivery)
    try:
        from core.obligation_effect import record_obligation_effect

        record_obligation_effect(
            state, upstream_goal_effect=effect, scientific_contribution=contribution,
            source_receipt={
                "closure_id": result.get("closure_id"),
                "revision": result.get("revision"),
                "delivery_id": delivery.get("delivery_id"),
                "delivery_digest": result.get("delivery_digest"),
                "outcome": delivery.get("outcome"),
                "scope": delivery.get("scope"),
            },
        )
    except Exception:  # 记账失败不能把已经封存的交付弄丢
        pass


async def _finish(
    state: Any,
    outcome: str,
    summary: str,
    deliverables: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    next_step: str = "",
    blocker: dict[str, Any] | None = None,
    verification: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    result = await _seal_finish(
        state, outcome, summary, deliverables=deliverables,
        limitations=limitations, next_step=next_step, blocker=blocker,
        verification=verification, **kwargs,
    )
    _record_core_delivery(state, result)
    return result


async def _seal_finish(
    state: Any,
    outcome: str,
    summary: str,
    deliverables: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    next_step: str = "",
    blocker: dict[str, Any] | None = None,
    verification: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    # Incomplete transaction recovery owns precedence over new arguments.
    # A sealed C may instead be followed by an independently evaluated revision.
    try:
        persisted = _delivery_transaction.read_manifest(state)
    except _delivery_transaction.DeliveryTransactionError as exc:
        return exc.as_result()
    request = {"outcome": outcome, "summary": summary,
               "deliverables": deliverables or [], "limitations": limitations or [],
               "next_step": next_step, "blocker": blocker, "verification": verification or [],
               **kwargs}
    if persisted is not None:
        led, ledger_error = _owner_ledger(state)
        if ledger_error is not None:
            return ledger_error
        assert led is not None
        try:
            _delivery_transaction.validate_manifest(state, persisted)
            # Preserve the first delivery, but never discard a lifecycle
            # that became open after an interrupted finish.
            live_error = _live_effect_refusal(persisted["delivery"]["outcome"], led.records())
            if live_error is not None:
                return live_error
            if persisted.get("commit") is None:
                settled = _delivery_transaction.settle_delivery(state, led, None)
                return _adapt_settled_result(settled)
            _delivery_transaction._sealed(state, led, persisted)
            if not _delivery_changed(state, persisted, request, led.records()):
                settled = _delivery_transaction.settle_delivery(state, led, None)
                return _adapt_settled_result(settled)
        except _delivery_transaction.DeliveryTransactionError as exc:
            return exc.as_result()
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return {"status": "error", "error_code": "closure_observation_unavailable",
                    "error": f"Cannot compare the sealed delivery with current facts: {exc}",
                    "owner": "experiment", "closure_id": persisted["closure_id"],
                    "side_effects": "none", "retryable_after_change": True,
                    "next_action": "Restore access to the delivery objects and retry finish; "
                                   "do not rerun the workload. If the persisted delivery is damaged, "
                                   "retain it and ask the runtime owner to repair or take over."}

    # Fresh calls are validated only after recovery has been ruled out.
    if kwargs:
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": f"unknown parameters: {sorted(kwargs)}"}
    outcome = str(outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": f"outcome must be one of {OUTCOMES}"}
    if not isinstance(summary, str) or not summary.strip():
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": "summary must be a non-empty string"}
    if deliverables is not None and (
            not isinstance(deliverables, list)
            or any(not isinstance(item, dict) for item in deliverables)):
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": "deliverables must be an array of objects"}
    if limitations is not None and (
            not isinstance(limitations, list)
            or any(not isinstance(item, str) for item in limitations)):
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": "limitations must be an array of strings"}
    if not isinstance(next_step, str):
        return {"status": "error", "error_code": "finish_parameters_invalid",
                "error": "next_step must be a string"}
    phase = _ledger.bootstrap_phase(state)
    if phase != "ready":
        return {"status": "error", "error_code": "bootstrap_incomplete",
                "bootstrap_phase": phase,
                "error": "the v3 run bootstrap has not established canonical A"}
    led, ledger_error = _owner_ledger(state)
    if ledger_error is not None:
        return ledger_error
    assert led is not None

    try:
        initial_records = led.records()
        authority = _project_authority(initial_records)
        live_error = _live_effect_refusal(outcome, initial_records)
        if live_error is not None:
            return live_error
        effective_blocker = _effective_blocker(outcome, blocker, authority)
        try:
            evaluation = await evaluate(state, led, verification, deliverables)
        except _task_verification.VerificationError as exc:
            return {"status": "error", "error_code": "finish_parameters_invalid", "error": str(exc)}
        records = led.records()
        authority = _project_authority(records)
        # A/B and the reported contradictions share the delivery snapshot.
        evaluation = {
            **evaluation,
            "contradictions": _job_contradictions(records) + _action_contradictions(records, evaluation),
        }
        evaluation["success_supported"] = not evaluation["gaps"] and not evaluation["contradictions"]
        live_error = _live_effect_refusal(outcome, records)
        if live_error is not None:
            live_error.update({"gaps": evaluation.get("gaps") or [],
                               "contradictions": evaluation.get("contradictions") or []})
            return live_error
        # The check runner can await while new authority facts are recorded.
        # Assemble A and B from the same final snapshot, including blockers.
        effective_blocker = _effective_blocker(outcome, blocker, authority)
        delivery, evaluation = _delivery_and_evaluation(
            state, outcome=outcome, summary=summary.strip(),
            limitations=list(limitations or []), next_step=next_step,
            blocker=effective_blocker, authority=authority, records=records,
            evaluation=evaluation,
        )
        revision = persisted["revision"] + 1 if persisted is not None else 1
        delivery["delivery_id"] = _delivery_transaction.delivery_id(state, revision)
        if outcome == "success" and not evaluation.get("obligations"):
            return {
                "status": "clarification_required",
                "error_code": "task_acceptance_unavailable",
                "reason": "没有可读取的原任务或正式验收要求；说明缺少的业务输入，不要事后补造成功标准。",
            }
        if outcome == "success" and (
                not evaluation.get("success_supported")
                or delivery.get("execution_status") != "attempted"):
            return {
                "status": "success_unsupported",
                "gaps": evaluation["gaps"],
                "contradictions": evaluation["contradictions"],
                "required_satisfied": (
                    f"{evaluation['required_satisfied']}/{evaluation['required_total']}"),
                "reachable_outcomes": _reachable_outcomes(evaluation),
                "failed_actions": _failure_context(records, evaluation),
                "next_action": (
                    "先处理 gaps 中具体文件或检查命令及其输出，以及 contradictions 中的实际冲突。"
                    "核验方法写错可纠正后再次 finish；正式条件不能删除。"
                    "不要为改变来源而重写正确报告，也不要无变化重复 finish 或重跑已通过的计算。"
                    "只有格式检查不足以证明计算成功；须核对任务要求的阶段状态和实际结果。"
                    "无法补齐时按真实完成程度交付 partial/failed/blocked。"
                ),
            }
        contents = _triplet_contents(
            state, delivery=delivery, authority=authority, records=records,
            evaluation=evaluation, declared_deliverables=list(deliverables or []),
            finish_request=request,
        )
        proposed = _delivery_transaction.build_manifest(
            state, delivery=delivery, contents=contents, previous=persisted)
        settled = _delivery_transaction.settle_delivery(state, led, proposed, previous=persisted)
        return _adapt_settled_result(settled, evaluation)
    except _FinishAssemblyError as exc:
        return exc.as_result()
    except _delivery_transaction.DeliveryTransactionError as exc:
        return exc.as_result()


def _model_finish_receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded receipt for the actual sealed C, not the latest request."""
    delivery = result.get("delivery")
    if result.get("status") != "success" or not isinstance(delivery, dict):
        return result
    summary = str(delivery.get("summary") or "")
    limitations = [str(item) for item in (delivery.get("limitations") or [])]
    next_step = str(delivery.get("next_step") or "")
    blocker = delivery.get("blocker")
    blocker_summary = str(blocker.get("summary") or "") if isinstance(blocker, dict) else ""
    details_truncated = (
        len(summary) > 500 or len(next_step) > 300
        or len(blocker_summary) > 250 or len(limitations) > 4
        or any(len(item) > 220 for item in limitations[:4])
    )
    receipt = {
        "status": "success",
        "outcome": delivery.get("outcome"),
        "closure_id": result.get("closure_id"),
        "revision": result.get("revision"),
        "reused_manifest": bool(result.get("reused_manifest")),
        "delivery_digest": result.get("delivery_digest"),
        "artifact_ids": result.get("artifact_ids"),
        "finish_seq": result.get("finish_seq"),
        "gap_count": len(delivery.get("gaps") or []),
        "contradiction_count": len(delivery.get("contradictions") or []),
        "limitation_count": len(limitations),
        "sealed_summary": summary[:500],
        "sealed_limitations": [item[:220] for item in limitations[:4]],
        "sealed_next_step": next_step[:300],
        "sealed_blocker_summary": blocker_summary[:250],
        "details_truncated": details_truncated,
        "next_action": (
            "交付已冻结；按回执中实际封存的主张向调用方报告并结束。"
            "本次实参与封存内容不同时，以封存内容为准。只有正式任务或输入、"
            "执行事实、交付对象变化，或需纠正交付主张时才再次调用 finish；"
            "如 details_truncated 或需完整证据，按 artifact_ids 读取冻结交付。"
        ),
    }
    if result.get("required_satisfied") is not None:
        receipt["required_satisfied"] = result["required_satisfied"]
    return receipt


async def _finish_for_model(
    state: Any,
    outcome: str,
    summary: str,
    deliverables: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    next_step: str = "",
    blocker: dict[str, Any] | None = None,
    verification: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    result = await _finish(
        state, outcome, summary, deliverables=deliverables,
        limitations=limitations, next_step=next_step, blocker=blocker,
        verification=verification, **kwargs,
    )
    return _model_finish_receipt(result)

register_tool(
    ToolDefinition(
        name="finish",
        description=(
            "结束本 run：说 outcome（success / partial / failed / blocked / cancelled）、一段如实的摘要、"
            "实际交付了什么（可选）、限制（可选）。三件套自动生成并冻结，不逐个 save_artifact / freeze_artifact。"
            "框架核对交付对象身份、来源（本次产物或已知输入参考）、"
            "检查命令是否通过、作业是否都已终态；success 站不住时一次告诉你缺口和可达的诚实 outcome。"
            "自然任务用 verification 说明原文要求与实际核验方法；不必先声明或重做已完成工作。"
            "模型负责解释覆盖，框架验证具体检查，不能把文件存在当作全部语义已满足。"
            "失败命令与任务失败分别判断：保留原退出码，核对适用的计算状态与结果；报告格式通过不能替代计算验收。"
            "诚实的非 success 收尾永远可达。"
            "已有交付后纠正文件或交付说明，仍用 finish；保留旧版本并重新核验，不重跑实验载荷。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                "summary": {"type": "string", "minLength": 1},
                "verification": _task_verification.SCHEMA,
                "deliverables": {"type": "array", "items": {"type": "object", "properties": {
                    "path": {"type": "string"}, "obligation_id": {"type": "string"}, "note": {"type": "string"}},
                    "additionalProperties": False}},
                "limitations": {"type": "array", "items": {"type": "string"}},
                "next_step": {"type": "string"},
                "blocker": {"type": "object", "properties": {"blocker_id": {"type": "string"},
                                                              "summary": {"type": "string"}},
                            "additionalProperties": False},
            },
            "required": ["outcome", "summary"],
            "additionalProperties": False,
        },
        allowed_node_types=["experiment"], risk_level="low",
    ),
    _finish_for_model,
)
