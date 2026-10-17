"""共同 action 边界：副作用发生前后唯一的记账入口（governing §5；Codex 21 号 P0-A/B/C）。

**一个入口、一个出口**，所有会产生副作用的写入者都走它：safe_run_bash、
safe_execute_python、safe_write_file、fetch_resource、submit_job。

约定三条，每条都有它挡住的具体失败：

1. **开始要 durable，否则不执行**（fail-closed）。``begin()`` 用会抛的
   ``ledger.record``，不是吞异常的 ``try_record``。账本写不进去就返回拒绝，调用方
   **不得**继续 spawn / submit。挡住的是：intent 没落盘却已经提交，ACK 丢了之后
   既没有 intent 也没有 nonce，只能猜要不要重投。

2. **结束写不进去不许报普通 success**。副作用已经发生了，这时候返回 success 会让
   模型以为可以重跑。``settle()`` 失败时返回
   ``effect_recording_uncertain``：带外部句柄、带"已发生、不得重跑"的事实，
   交给 runtime 去对账或交接。

3. **observation 记身份，不只记路径名**。冻结每个候选路径在 action 前后的身份
   （小文件 sha256，大文件 size+inode+mtime，并记 identity_basis）。finish 拿当前
   身份和冻结的 after 身份比：action 之后被别的东西覆盖过，success 就不可达。
   只记"这条路径被动过"挡不住覆盖。

turn-end 的记账 hook 只作摘要补充，不是 B 的事实来源。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from . import capture as _capture
    from . import ledger as _ledger
    from . import obligations as _obligations
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import capture as _capture  # type: ignore
    from tools import ledger as _ledger  # type: ignore
    from tools import obligations as _obligations  # type: ignore

#: 仅为展示 / 内联摘要上限；不能丢弃已完成快照中的 B 身份。
IDENTITY_ROWS_LIMIT = _capture.OBSERVATION_SUMMARY_LIMIT

EXPERIMENT = "experiment"
SUPPORT = "support"


@dataclass
class ActionHandle:
    action_id: str
    tool: str
    effect_class: str
    before: dict[str, Any]
    candidates: list[Path] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)
    settled: bool = False
    #: False = 这不是 v3 run，边界只是穿过去，不记也不拦。
    recording: bool = True
    observation_boundary: dict[str, Any] = field(default_factory=dict)


def observation_boundary(state: Any, writable: list[Path], readonly: list[Path]) -> dict[str, Any]:
    """Observe business trees intersecting the already approved OS capability.

    This is a read/recording plan only, never an input to spawn permissions.
    Global scratch, caches and framework state are not business output roots.
    """
    from .path_roles import collect_path_roles
    roles = collect_path_roles(state)
    roots: set[Path] = set()
    for role in roles:
        if not role.writable or role.container_only:
            continue
        business = Path(role.path).resolve(strict=False)
        if business == Path(state.root).resolve():
            continue
        for allowed in writable:
            allowed = Path(allowed).resolve(strict=False)
            if business.is_relative_to(allowed):
                roots.add(business)
            elif allowed.is_relative_to(business):
                roots.add(allowed)
    exclusions = {Path(r.path).resolve(strict=False) for r in roles
                  if not r.writable and not r.container_only}
    # Only nested readonly overlays exclude a subtree: the Project ancestor
    # is readonly too, but authorized business roots are rebound inside it.
    exclusions = {r for r in exclusions if any(r == root or r.is_relative_to(root) for root in roots)}
    exclusions.update(Path(role.path).resolve() / ".python-scratch"
                      for role in roles if role.role == "run_root")
    exclusions.update(Path(r).resolve() for r in readonly
                      if any(Path(r).resolve().is_relative_to(root) for root in roots))
    roots = {r for r in roots if not any(r.is_relative_to(x) for x in exclusions)}
    roots = {r for r in roots if not any(r != other and r.is_relative_to(other) for other in roots)}
    return {"roots": sorted(map(str, roots)), "excluded_roots": sorted(map(str, exclusions)),
            "basis": "approved_writable_roots_intersect_business_roles",
            "limitation": "bounded before/after observation; unobserved external concurrent writers are not ruled out"}


@dataclass
class BoundaryRefusal:
    """账本没能 durable 记下这次动作——副作用**不得**发生。"""

    reason: str
    detail: str
    code: str = "action_not_durably_recorded"

    def as_result(self, tool: str) -> dict[str, Any]:
        if self.code == "bootstrap_incomplete":
            return {
                "status": "error",
                "error_code": "bootstrap_incomplete",
                "error": (f"{tool} 未执行：本 run 的 v3 启动只做了一半（账本已开、最低目标与约束"
                          f"没有建立成功：{self.detail}）。这种 run 不能当普通 v3 run 继续，也不能"
                          "退回旧协议——需要 runtime 先修好启动状态。"),
                "detail": self.detail, "payload_spawned": False, "job_submitted": False,
                "next_action": {"owner": "experiment_runtime", "action": "bootstrap_replays_next_turn",
                                "model_callable": False},
            }
        return {
            "status": "error",
            "error_code": "action_not_durably_recorded",
            "error": (f"{tool} 未执行：本次动作没能落进账本（{self.reason}）。"
                      "先记下再动手是为了 ACK 丢失或会话重开时不必靠猜——"
                      "账本写不进去时执行，等于放弃这条保证。"),
            "detail": self.detail,
            "payload_spawned": False,
            "job_submitted": False,
            "next_action": {"owner": "experiment_runtime", "action": "repair_ledger_then_retry",
                            "model_callable": False},
        }


def _bootstrap_phase(state: Any) -> str:
    try:
        return _ledger.bootstrap_phase(state)
    except Exception:
        return "none"


def _is_v3_run(state: Any) -> bool:
    """这个 run 是不是**正式**在 v3 协议下。三处写入口共用同一个纯判定。"""
    return _bootstrap_phase(state) == "ready"


def v3_ready(state: Any) -> bool:
    """给写入者用的公开判定：run 在 v3 纪元上（initializing 或 ready）→ 走边界，不读不写旧路线 authority。

    initializing 也算：这时 begin() 会以 bootstrap_incomplete 拒绝，而不是让旧控制面接管
    （Codex 45 P0-B：启动只做了一半的 run 不能退回旧协议）。
    """
    return _bootstrap_phase(state) in {"initializing", "ready"}


def _obligation_candidates(state: Any) -> list[Path]:
    rows: list[Path] = []
    try:
        for ob in _obligations.frozen(state):
            if ob.get("kind") == "file":
                rows.append(_obligations.resolve_path(state, str(ob["path"])))
    except Exception:
        return []
    return rows


def begin(state: Any, *, tool: str, effect_class: str = EXPERIMENT, command: str | None = None,
          work_root: str | None = None, output_candidates: list[Any] | None = None,
          output_roots: list[Any] | None = None, predecessor: str | None = None,
          correlation_key: str | None = None, execution_params: Any = None,
          input_observation: dict[str, Any] | None = None,
          observation_boundary: dict[str, Any] | None = None) -> ActionHandle | BoundaryRefusal:
    """记下"要动手了"并拍前置快照。账本写不进去就返回拒绝，调用方不许继续。"""
    action_id = f"{tool}-{uuid.uuid4().hex[:16]}"
    candidates = [Path(str(p)) for p in (output_candidates or [])] + _obligation_candidates(state)
    roots = [Path(str(r)) for r in (output_roots or [])]
    if observation_boundary is not None:
        roots = [Path(p) for p in observation_boundary["roots"]]
        candidates = [p for p in candidates if any(p.is_relative_to(r) for r in roots)]
    elif work_root:
        roots.append(Path(str(work_root)))
    def before_snapshot() -> dict[str, Any]:
        try:
            return _capture.snapshot(candidates, roots, include_directories=True,
                                     excluded_roots=(observation_boundary or {}).get("excluded_roots", []))
        except Exception as exc:
            return {"entries": {}, "truncated": True, "snapshot_error": f"{type(exc).__name__}: {exc}"}
    phase = _bootstrap_phase(state)
    if phase == "initializing":
        return BoundaryRefusal(reason="bootstrap_incomplete", code="bootstrap_incomplete",
                               detail="ledger meta exists but bootstrap.ready was never written")
    if phase == "none":
        # 不是 v3 run（旧协议留下的 run，或者框架还没建立 A）：新协议不读写旧控制状态
        # （governing §9 cutover），所以这里不记也不拦。durable-or-refuse 是 v3 自己的
        # 保证，把它加到旧协议 run 上只会把那边的执行全拒掉——2026-09-23 实测 28 条
        # 既有用例因此转红。
        return ActionHandle(action_id=action_id, tool=tool, effect_class=effect_class,
                            before=before_snapshot(), candidates=candidates, roots=roots, recording=False)
    try:
        from .execution_context import capture as capture_context
        _ledger.record(
            state, "action_started", action_id=action_id, tool=tool, effect_class=effect_class,
            command=(str(command)[:500] if command else None), work_root=(str(work_root) if work_root else None),
            predecessor=predecessor, correlation_key=correlation_key,
            capture_record=str(_capture.allocate_record_dir(state, action_id)),
            candidates=[str(p) for p in candidates][:IDENTITY_ROWS_LIMIT],
            observation_boundary=observation_boundary or {"roots": list(map(str, roots)), "excluded_roots": []},
            execution_context=capture_context(
                state, execution_params=execution_params, input_observation=input_observation),
        )
    except _ledger.LedgerError as exc:
        return BoundaryRefusal(reason=type(exc).__name__, detail=str(exc))
    except Exception as exc:  # 账本层之外的意外同样 fail-closed
        return BoundaryRefusal(reason=type(exc).__name__, detail=str(exc))
    return ActionHandle(action_id=action_id, tool=tool, effect_class=effect_class,
                        before=before_snapshot(), candidates=candidates, roots=roots,
                        observation_boundary=observation_boundary or {})


def _identity_rows(before: dict[str, Any], after: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """Freeze every changed identity from the already bounded snapshots."""
    rows: dict[str, Any] = {}
    b = before.get("entries") or {}
    a = after.get("entries") or {}
    for path in list(diff.get("created") or []) + list(diff.get("modified") or []):
        rows[path] = {"before": b.get(path), "after": a.get(path),
                      "basis": (diff.get("identity_basis") or {}).get(path),
                      "before_unknown": (diff.get("identity_basis") or {}).get(path) == "hash_unavailable"}
    return rows


def _disclose_overlapping_actions(state: Any, handle: ActionHandle, diff: dict[str, Any]) -> None:
    """A shared before/after window cannot identify which live action wrote it.

    Reuse existing B events; no lock, new authority or execution prohibition.
    This detects recorded action overlap, not every possible external writer.
    """
    records = _ledger.open_ledger(state).records()
    starts = {r["action_id"]: r for r in records if r.get("kind") == "action_started"}
    ends = {r["action_id"]: r["seq"] for r in records if r.get("kind") == "action_settled"}
    own = starts.get(handle.action_id, {})
    overlapping: dict[str, list[str]] = {}
    for aid, row in starts.items():
        if aid == handle.action_id or ends.get(aid, float("inf")) < own.get("seq", 0):
            continue
        boundary = row.get("observation_boundary") or {}
        roots = boundary.get("roots", [])
        excluded = boundary.get("excluded_roots", [])
        candidates = row.get("candidates", [])
        for key in [*diff.get("created", []), *diff.get("modified", [])]:
            p = Path(key)
            if (key in candidates or any(p.is_relative_to(Path(r)) for r in roots)) and not any(
                    p.is_relative_to(Path(r)) for r in excluded):
                overlapping.setdefault(key, []).append(aid)
    if overlapping:
        diff["created"] = [p for p in diff.get("created", []) if p not in overlapping]
        diff["modified"] = [p for p in diff.get("modified", []) if p not in overlapping]
        unknown = list(dict.fromkeys([*diff.get("unknown", []), *overlapping]))
        diff["unknown"] = unknown
        diff["overlapping_actions"] = overlapping


def settle(state: Any, handle: ActionHandle | None, *, outcome: str, exit_code: Any = None,
           stderr_text: str = "", external_handle: dict[str, Any] | None = None) -> dict[str, Any]:
    """拍后置快照、冻结身份、结算。幂等。

    返回 ``{"status": "recorded"}``，或副作用已发生但账本没记下时的
    ``{"status": "effect_recording_uncertain", ...}``——后者调用方必须原样交出去，
    不能当普通 success，也不能让模型重跑。
    """
    if handle is None or handle.settled or not handle.recording:
        return {"status": "recorded", "noop": True}
    handle.settled = True
    try:
        after = _capture.snapshot(handle.candidates, handle.roots,
                                  include_directories=True,
                                  excluded_roots=handle.observation_boundary.get("excluded_roots", []))
        diff = _capture.diff(handle.before, after, limit=None)
        _disclose_overlapping_actions(state, handle, diff)
    except Exception as exc:
        after, diff = {"entries": {}}, {"created": [], "modified": [], "deleted": [],
                                        "truncated": True, "identity_basis": {},
                                        "snapshot_error": f"{type(exc).__name__}: {exc}"}
    problems: list[str] = []
    try:
        observation = dict(
            created=diff.get("created"), modified=diff.get("modified"), deleted=diff.get("deleted"),
            unknown=diff.get("unknown", []),
            overlapping_actions=diff.get("overlapping_actions", {}),
            truncated=bool(diff.get("truncated")), identity_basis=diff.get("identity_basis"),
            unchanged_count=diff.get("unchanged_count"),
            identities=_identity_rows(handle.before, after, diff),
            observation_boundary=handle.observation_boundary or {
                "roots": list(map(str, handle.roots)), "excluded_roots": []},
            coverage={"before": handle.before.get("root_coverage", []), "after": after.get("root_coverage", [])},
            scan_errors=[x for x in (handle.before.get("snapshot_error"), diff.get("snapshot_error")) if x],
        )
        _ledger.record(state, "observation", action_id=handle.action_id,
                       **_capture.persist_observation(state, handle.action_id, observation))
    except Exception as exc:
        problems.append(f"observation: {type(exc).__name__}: {exc}")
    try:
        _ledger.record(state, "action_settled", action_id=handle.action_id, state=outcome,
                       exit_code=exit_code, stderr_tail=str(stderr_text)[-400:],
                       external_handle=external_handle)
    except Exception as exc:
        problems.append(f"action_settled: {type(exc).__name__}: {exc}")
    if not problems:
        return {"status": "recorded", "action_id": handle.action_id,
                "created": list(diff.get("created") or [])[:IDENTITY_ROWS_LIMIT],
                "modified": list(diff.get("modified") or [])[:IDENTITY_ROWS_LIMIT]}
    return {
        "status": "effect_recording_uncertain",
        "error_code": "effect_recording_uncertain",
        "error": ("副作用已经发生，但账本没能记下它。不要把这次当成功，也不要重跑——"
                  "重跑会把已经发生的事再做一遍。"),
        "side_effect_committed": True,
        "payload_must_not_rerun": True,
        "do_not_retry_payload": True,
        "action_id": handle.action_id,
        "external_handle": external_handle,
        "problems": problems,
        "next_action": {"owner": "experiment_runtime", "action": "reconcile_or_handoff",
                        "model_callable": False},
    }


def current_identity(path: Path) -> dict[str, Any]:
    """finish 复核时用的当前身份，和 observation 冻结的那套同一算法。"""
    return _capture.identity(path)


def record_completed_effect(state: Any, *, tool: str, effect_class: str = SUPPORT,
                            paths: list[Any], command: str | None = None,
                            external_handle: dict[str, Any] | None = None) -> dict[str, Any]:
    """副作用已经发生、且身份此刻可读时的原子记法。

    用于 ``begin`` 插不进去的写入者——比如 ``fetch_resource``：等代码走到可以记账的
    地方，文件已经下完了，这时再拍"前置快照"只会看到文件已存在、判成没变，反而把
    真的 producer 记丢。所以这里直接把当前身份冻结成这次 action 的 after 身份，并
    如实标注 ``before_unknown``：我们没看见它之前的样子，不假装看见了。

    仍然是 durable-or-refuse：记不下就返回 ``effect_recording_uncertain``，
    调用方不得当普通 success。
    """
    action_id = f"{tool}-{uuid.uuid4().hex[:16]}"
    if _bootstrap_phase(state) == "initializing":
        return {"status": "error", "error_code": "bootstrap_incomplete", "side_effect_committed": True,
                "payload_must_not_rerun": True, "action_id": action_id,
                "next_action": {"owner": "experiment_runtime", "action": "bootstrap_replays_next_turn",
                                "model_callable": False}}
    if not _is_v3_run(state):
        # 非 v3 run：穿过去，不记也不改结果（Codex 25 号 P0-1）。把已经成功的下载
        # 改写成 effect_recording_uncertain，或反过来替它开启 v3 纪元，都是错的。
        return {"status": "recorded", "noop": True, "reason": "run is not on the v3 epoch"}
    rows: dict[str, Any] = {}
    produced: list[str] = []
    for item in paths or []:
        path = Path(str(item))
        ident = _capture.identity(path)
        if ident.get("kind") in {"missing", "error"}:
            continue
        rows[str(path)] = {"before": None, "after": ident, "basis": "after_only",
                           "before_unknown": True}
        produced.append(str(path))
    problems: list[str] = []
    for kind, fields in (
        ("action_started", {"tool": tool, "effect_class": effect_class,
                            "command": (str(command)[:500] if command else None),
                            "before_unknown": True}),
        ("observation", {"created": produced, "modified": [], "deleted": [], "truncated": False,
                         "identity_basis": {p: "after_only" for p in produced},
                         "identities": rows}),
        ("action_settled", {"state": "settled", "exit_code": 0, "external_handle": external_handle}),
    ):
        try:
            _ledger.record(state, kind, action_id=action_id, **fields)
        except Exception as exc:
            problems.append(f"{kind}: {type(exc).__name__}: {exc}")
    if problems:
        return {"status": "effect_recording_uncertain", "error_code": "effect_recording_uncertain",
                "side_effect_committed": True, "payload_must_not_rerun": True,
                "do_not_retry_payload": True, "action_id": action_id, "problems": problems,
                "next_action": {"owner": "experiment_runtime", "action": "reconcile_or_handoff",
                                "model_callable": False}}
    return {"status": "recorded", "action_id": action_id, "created": produced}
