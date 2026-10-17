"""run 开始时框架建立 A（governing document §2、§3、§6）。

模型不参与这一步：框架从 node_inputs、绑定的 prereg 与上游契约读出目标与约束，
物化可写根，冻结 required obligations，打开账本，并给模型**一页纸**。

scope 的派生（§3）：绑定 prereg 一定 scientific；输入明确点名 operation 的按
operation；两者都没有时**不猜**——scope 留空并在一页纸里如实说明，
不要求为分类补手续；只澄清实际任务、必要输入或验收条件的缺口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.llm import LLMMessage
from core.loop_hooks import HookContext, LoopHook, get_loop_hook, register_loop_hook

try:
    from . import ledger as _ledger
    from . import obligations as _obligations
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore
    from tools import obligations as _obligations  # type: ignore

_BOOTSTRAP_FLAG = "experiment_v3_bootstrap"
_OPERATION_CATEGORIES = ("package_install", "toolchain_build", "environment_probe",
                         "scheduler_probe", "job_observation", "format_validation", "other")
_A_SCHEMA_VERSION = 1
_BINDING_BLOCKERS = {"run_authority_input_changed", "run_authority_unavailable",
                     "task_contract_unavailable", "task_contract_identity_invalid"}


def _node_inputs(state: Any) -> dict[str, Any]:
    try:
        payload = state.hook_state.get("node_inputs") or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _startup_blocker(code: str, reason: str) -> dict[str, Any]:
    return {"status": "blocked", "code": code, "reason": reason}


def _formal_input_authority(prereg_status: str) -> str:
    """A 的纯投影；这里只陈述 formal input authority，不做 action admission。"""
    if prereg_status == "bound_valid":
        return "bound"
    if prereg_status == "none":
        return "unbound"
    return "unavailable"


def derive_scope(state: Any) -> dict[str, Any]:
    """Use the exact selected Core contract when present, not a mutable hint."""
    from .task_input_view import resolve_task_inputs
    view = resolve_task_inputs(state)
    result = _derive_scope_inputs(state, view["inputs"])
    if (view["task_contract"] and not view["error"]
            and view["inputs"].get("prereg_assignment") == {"kind": "pending"}):
        # Core field absence is not the node protocol's explicit scientific
        # pending signal.  Keep scope independent from formal-input readiness.
        scope_inputs = {key: value for key, value in view["inputs"].items()
                        if key != "prereg_assignment"}
        result = _derive_scope_inputs(state, scope_inputs)
        result.update(prereg_status="omitted", prereg_source="task_contract",
                      formal_input_authority="unavailable", startup_blocker=None)
    result["task_contract"] = view["task_contract"]
    result["task_assignment_kind"] = view.get("assignment_kind")
    result["input_witness"] = view["input_witness"]
    result["node_input_digest"] = view.get("node_input_digest")
    if view["error"]:
        result["startup_blocker"] = _startup_blocker(**view["error"])
        result["formal_input_authority"] = "unavailable"
    return result


def _requested_operation_category(inputs: dict[str, Any]) -> str | None:
    """调用方是否明确把本 run 点名为 operation；是则返回 category（缺省 other）。"""
    declared = str(inputs.get("scope") or "").strip().lower()
    category = str(inputs.get("operation_category") or "").strip().lower()
    if declared == "operation" or category in _OPERATION_CATEGORIES:
        return category if category in _OPERATION_CATEGORIES else "other"
    return None


def _derive_scope_inputs(state: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """从 typed node inputs 严格派生一个候选 A；不读取或修改持久状态。

    supplied prereg signal 即使损坏也足以证明这是 scientific 请求，但损坏的
    formal input 不能被消费。因此 invalid / pending / legacy / mixed 返回
    scientific scope 加 typed startup blocker，而不是让 bootstrap 永久卡在
    initializing。省略 prereg 不构成 scientific 或 operation 的证据。
    """
    legacy_keys = [key for key in ("prereg_artifact_id", "prereg_version") if key in inputs]
    typed_present = "prereg_assignment" in inputs
    if legacy_keys:
        status = "mixed_conflict" if typed_present else "legacy_unsupported"
        code = "mixed_prereg_input_unsupported" if typed_present else "legacy_prereg_input_unsupported"
        reason = ("typed prereg_assignment 不能与 legacy prereg_artifact_id / prereg_version 混用"
                  if typed_present else
                  "v3 只接受 typed prereg_assignment；legacy prereg_artifact_id / prereg_version 不受支持")
        return {"scope": "scientific", "category": None, "scope_status": "resolved",
                "scope_basis": "prereg_signal", "prereg_status": status,
                "prereg_source": "mixed" if typed_present else "legacy", "prereg": None,
                "startup_blocker": _startup_blocker(code, reason),
                "formal_input_authority": "unavailable", "reason": reason}

    if typed_present:
        try:
            from .run_contract import _validated_prereg_assignment, _resolve_exact_frozen_prereg
        except ImportError:  # pragma: no cover
            from tools.run_contract import _validated_prereg_assignment, _resolve_exact_frozen_prereg  # type: ignore
        raw_assignment = inputs.get("prereg_assignment")
        assignment, error = _validated_prereg_assignment(
            raw_assignment, require_bound_source=False,
        )
        if isinstance(raw_assignment, dict) and "source" in raw_assignment:
            assignment = None
            error = "caller prereg_assignment must not provide receipt source"
        if error or not assignment:
            reason = str(error or "prereg_assignment is invalid")
            return {"scope": "scientific", "category": None, "scope_status": "resolved",
                    "scope_basis": "prereg_signal", "prereg_status": "invalid",
                    "prereg_source": "typed", "prereg": None,
                    "startup_blocker": _startup_blocker("prereg_assignment_invalid", reason),
                    "formal_input_authority": "unavailable", "reason": reason}
        kind = str(assignment["kind"])
        if kind == "pending":
            reason = "显式 prereg_assignment=pending：scientific formal input 尚未可用"
            return {"scope": "scientific", "category": None, "scope_status": "resolved",
                    "scope_basis": "prereg_signal", "prereg_status": "pending",
                    "prereg_source": "typed", "prereg": None,
                    "startup_blocker": _startup_blocker("prereg_assignment_pending", reason),
                    "formal_input_authority": "unavailable", "reason": reason}
        if kind == "bound":
            version = int(assignment["version"])
            resolved = _resolve_exact_frozen_prereg(
                state, artifact_id=str(assignment["artifact_id"]), version=version,
                expected_content_hash=str(assignment["content_hash"]),
            )
            if not resolved.get("passed"):
                code = f"prereg_{resolved.get('status') or 'invalid'}"
                reason = str(resolved.get("reason") or code)
                return {"scope": "scientific", "category": None, "scope_status": "resolved",
                        "scope_basis": "prereg_signal", "prereg_status": "invalid",
                        "prereg_source": "typed", "prereg": None,
                        "startup_blocker": _startup_blocker(code, reason),
                        "formal_input_authority": "unavailable", "reason": reason}
            binding = {"artifact_id": assignment["artifact_id"], "version": version,
                       "content_hash": resolved["content_hash"]}
            return {"scope": "scientific", "category": None, "scope_status": "resolved",
                    "scope_basis": "valid_bound_prereg", "prereg_status": "bound_valid",
                    "prereg_source": "typed", "prereg": binding, "startup_blocker": None,
                    "formal_input_authority": "bound",
                    "reason": "本 run 绑定了冻结的预注册（id/version/hash 已核对），按 governing §3 一定是 scientific"}
        prereg_status = "none"
        prereg_source = "typed"
    else:
        prereg_status = "omitted"
        prereg_source = "omitted"

    declared = str(inputs.get("scope") or "").strip().lower()
    requested_category = _requested_operation_category(inputs)
    if requested_category is not None:
        scope = "operation"
        resolved_category = requested_category
        reason = "调用方输入明确把本 run 点名为 operation"
        basis = "explicit_scope"
        status = "resolved"
    elif declared == "scientific":
        scope = "scientific"
        resolved_category = None
        reason = "调用方输入明确把本 run 点名为 scientific"
        basis = "explicit_scope"
        status = "resolved"
    else:
        scope = None
        resolved_category = None
        reason = "输入未确定 operation/scientific；不猜，保留 unknown，按任务要求与证据核验，不以分类为执行或收尾前提"
        basis = "unresolved"
        status = "undecided"
    return {"scope": scope, "category": resolved_category, "scope_status": status,
            "scope_basis": basis, "prereg_status": prereg_status,
            "prereg_source": prereg_source, "prereg": None, "startup_blocker": None,
            "formal_input_authority": _formal_input_authority(prereg_status), "reason": reason}


def project_run_authority(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pure reducer for canonical A; it never inspects commands or mutable catalog state."""
    authority: dict[str, Any] | None = None
    for row in records:
        kind = row.get("kind")
        if kind == "run_bootstrap" and authority is None:
            prereg_status = str(row.get("prereg_status") or "omitted")
            authority = {
                "schema_version": int(row.get("a_schema_version") or 0),
                "scope": row.get("scope") if row.get("scope") in {"scientific", "operation"} else None,
                "scope_status": "resolved" if row.get("scope") in {"scientific", "operation"} else "undecided",
                "scope_basis": str(row.get("scope_basis") or "unresolved"),
                "category": row.get("category"), "prereg_status": prereg_status,
                "prereg_source": str(row.get("prereg_source") or "omitted"),
                "prereg": row.get("prereg") if isinstance(row.get("prereg"), dict) else None,
                "startup_blocker": (dict(row["startup_blocker"])
                                    if isinstance(row.get("startup_blocker"), dict) else None),
                "formal_input_authority": _formal_input_authority(prereg_status),
                "reason": str(row.get("reason") or ""),
                "task_contract": row.get("task_contract"),
                "task_assignment_kind": row.get("task_assignment_kind"),
                "input_witness": row.get("input_witness"),
                "node_input_digest": row.get("node_input_digest"),
            }
        elif authority is not None and kind == "run_authority_refreshed":
            refreshed = row.get("authority")
            from .run_contract import _prereg_binding_state
            from .task_input_view import selected_task_binding_status
            if (isinstance(refreshed, dict)
                    and selected_task_binding_status(authority, refreshed) == "selected_task_unchanged"
                    and refreshed.get("scope") == authority["scope"] == "scientific"
                    and refreshed.get("scope_status") == authority["scope_status"] == "resolved"
                    and refreshed.get("schema_version") == authority["schema_version"] == 1
                    and row.get("previous_prereg_status") == authority["prereg_status"] == "invalid"
                    and refreshed.get("prereg_status") == "bound_valid"
                    and _prereg_binding_state(refreshed.get("prereg")) == "valid"
                    and refreshed.get("startup_blocker") is None
                    and refreshed.get("formal_input_authority") == "bound"):
                authority = dict(refreshed)
        elif authority is not None and kind == "scope_resolved":
            proposed = row.get("scope")
            if authority["scope"] is None and proposed in {"scientific", "operation"}:
                authority["scope"] = proposed
                authority["scope_status"] = "resolved"
                authority["scope_basis"] = str(row.get("scope_basis") or "explicit_scope")
                authority["category"] = row.get("category")
                authority["reason"] = str(row.get("reason") or authority["reason"])
        elif authority is not None and kind == "startup_blocker_recorded":
            blocker = row.get("startup_blocker")
            if isinstance(blocker, dict):
                authority["startup_blocker"] = dict(blocker)
            elif ("startup_blocker" in row and blocker is None
                  and (authority.get("startup_blocker") or {}).get("code") in _BINDING_BLOCKERS):
                authority["startup_blocker"] = None
    return authority


def load_run_authority(state: Any) -> dict[str, Any] | None:
    """Read-only canonical A projection for later action-specific admission."""
    try:
        return project_run_authority(_ledger.open_ledger(state).records())
    except Exception:
        return None


def materialize_writable_roots(state: Any) -> dict[str, Any]:
    """把可写的路径角色真正建出来（governing §2：A 建立时物化可写根）。

    实测依据（第四次方法预览，2026-09-23）：一页纸说"你能写这里"，但
    ``build_root`` / ``managed_source_root`` 的目录并不存在，沙箱契约按
    "受权路径角色尚未物化"拒了 3 次执行。v2 里这些目录是 ``declare_execution_route``
    顺带建的；v3 没有那一步，就必须在这里建，否则模型第一条构建命令必然撞墙。

    只建 writable 且非 container_only 的角色；只读角色（baseline / dependency）
    一个都不碰。
    """
    created: list[str] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []
    try:
        try:
            from .path_roles import collect_path_roles
        except ImportError:  # pragma: no cover
            from tools.path_roles import collect_path_roles  # type: ignore
        roles = collect_path_roles(state)
    except Exception as exc:
        return {"status": "error", "created": [], "skipped": [], "failures": [],
                "error": f"{type(exc).__name__}: {exc}"}
    for role in roles:
        if not getattr(role, "writable", False) or getattr(role, "container_only", False):
            continue
        path = Path(str(getattr(role, "path", "") or ""))
        if not str(path):
            continue
        try:
            if path.is_dir():
                skipped.append(f"{role.role}:exists")
                continue
            if path.exists():
                failures.append({"role": str(role.role), "path": str(path),
                                 "error": "path_exists_but_is_not_directory"})
                continue
            path.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                created.append(f"{role.role}={path}")
            else:
                failures.append({"role": str(role.role), "path": str(path),
                                 "error": "directory_not_materialized"})
        except OSError as exc:
            failures.append({"role": str(role.role), "path": str(path),
                             "error": f"{type(exc).__name__}: {exc}"})
    return {"status": "error" if failures else "ready", "created": created,
            "skipped": skipped, "failures": failures,
            **({"error": "one or more writable roots could not be materialized"} if failures else {})}


def _writable_roots(state: Any) -> list[str]:
    try:
        from core.sandbox import write_roots_for
    except Exception:
        return []
    try:
        return [str(p) for p in (write_roots_for(state) or [])]
    except Exception:
        return []


def _scheduler_facts(state: Any) -> str:
    """Reuse resource discovery's candidate view; presence is not submission."""
    try:
        try:
            from . import resource_manager as _rm
        except ImportError:  # pragma: no cover
            from tools import resource_manager as _rm  # type: ignore
        discovered = _rm._discover()
    except Exception as exc:
        return f"调度器探测未完成（{type(exc).__name__}）；提交能力未知，不自动回退 local。"
    clients = [str(name) for name in discovered.get("available_schedulers", []) if name != "local"]
    candidates = discovered.get("submittable_schedulers", [])
    presence = ("检测到 " + "、".join(clients) + " 客户端（不等于授权或真实提交可用）"
                if clients else "未发现可用外部调度器客户端")
    selection = ("当前 submit_job 候选：" + "、".join(candidates)
                 if candidates else "当前没有可用的 submit_job 候选，不能自动回退 local")
    return f"调度器：{presence}；{selection}。候选仍须通过当次授权、资源与启动检查。"


async def _classify_experiment_scope_for_bootstrap(state: Any, scope: dict[str, Any]) -> dict[str, Any]:
    """让框架（不是模型）建立 acceptance receipt：旧 freeze gate 与 intent binding 仍读它。"""
    hook_state = getattr(state, "hook_state", {})
    if not scope.get("scope") or "experiment_execution_scope" in hook_state:
        return {"status": "already_set"}
    try:
        from .run_contract import _classify_experiment_scope
    except ImportError:  # pragma: no cover
        from tools.run_contract import _classify_experiment_scope  # type: ignore
    requested, category = scope["scope"], scope.get("category") or "other"
    if requested == "scientific" and scope.get("scope_basis") == "valid_bound_prereg":
        # #917：调用方明确要 operation、绑定的冻结 prereg 却把 run 定成 scientific。
        # 此前这里只转交生效值 scientific，classify 里那条
        # requested_operation_with_governing_prereg 偏离申报永远不触发——冲突
        # 没有 witness。转交调用方原请求：生效值仍由入口收据的 prereg 绑定定为
        # scientific，classify 照常申报偏离并在分类记录里留下 requested_mode。
        from .task_input_view import resolve_task_inputs
        caller_category = _requested_operation_category(resolve_task_inputs(state)["inputs"])
        if caller_category is not None:
            requested, category = "operation", caller_category
    return await _classify_experiment_scope(
        state, scope=requested, reason=scope["reason"],
        operation_category=category)


async def establish(state: Any) -> dict[str, Any]:
    """幂等：建立 scope（可派生时）、打开账本、冻结 obligations。返回一页纸需要的事实。"""
    hook_state = getattr(state, "hook_state", {})
    candidate = derive_scope(state)
    materialized = materialize_writable_roots(state)
    led = _ledger.open_ledger(state, write=True, bootstrap=True)
    existing = None if led.read_only else project_run_authority(led.records())
    if existing is None:
        scope = dict(candidate)
        if not led.read_only:
            led.append(
                "run_bootstrap", a_schema_version=_A_SCHEMA_VERSION,
                scope=scope["scope"], scope_status=scope["scope_status"],
                scope_basis=scope["scope_basis"], category=scope.get("category"),
                prereg_status=scope["prereg_status"], prereg_source=scope["prereg_source"],
                prereg=scope.get("prereg"), startup_blocker=scope.get("startup_blocker"),
                reason=scope["reason"], writable_roots=_writable_roots(state),
                materialized_roots=materialized.get("created"),
                materialize_status=materialized.get("status"),
                materialize_error=materialized.get("error"),
                task_contract=scope.get("task_contract"), input_witness=scope.get("input_witness"),
                task_assignment_kind=scope.get("task_assignment_kind"),
                node_input_digest=scope.get("node_input_digest"),
            )
    else:
        scope = dict(existing)
        same_prereg = all(
            existing.get(key) == candidate.get(key)
            for key in ("prereg_status", "prereg_source", "prereg")
        )
        blocker = existing.get("startup_blocker")
        from .task_input_view import selected_task_binding_status
        binding_status = selected_task_binding_status(existing, candidate)
        if (binding_status == "selected_task_unchanged"
                and (blocker or {}).get("code") in _BINDING_BLOCKERS):
            blocker = candidate.get("startup_blocker")
        # Re-observe the same immutable assignment, never adopt a new revision.
        if binding_status != "selected_task_unchanged":
            blocker = _startup_blocker(
                binding_status,
                ("当前输入与本 run 原任务不同；请父编排器按新任务 fresh 派发，保留本 run 证据"
                 if binding_status == "run_authority_input_changed" else
                 "本 run 缺少可核验的原始或当前任务输入身份；不能从恢复请求补铸原始 A"),
            )
        elif (existing.get("scope") == candidate.get("scope")
                and candidate.get("prereg_status") == "bound_valid"
                and existing.get("prereg_status") == "invalid"
                and (not same_prereg or blocker != candidate.get("startup_blocker"))):
            scope = {**candidate, "schema_version": _A_SCHEMA_VERSION}
            led.append("run_authority_refreshed", authority=scope,
                       previous_prereg_status=existing.get("prereg_status"))
            blocker = scope.get("startup_blocker")
        elif not same_prereg:
            blocker = _startup_blocker(
                "run_authority_input_changed",
                "bootstrap 已冻结 prereg authority；重试输入不能改写既有 A",
            )
        elif existing.get("scope") is None and candidate.get("scope") in {"scientific", "operation"}:
            led.append("scope_resolved", scope=candidate["scope"],
                       scope_basis=candidate["scope_basis"], category=candidate.get("category"),
                       reason=candidate["reason"])
            scope.update({key: candidate.get(key) for key in
                          ("scope", "scope_status", "scope_basis", "category", "reason")})
        elif (existing.get("scope") in {"scientific", "operation"}
              and candidate.get("scope") in {"scientific", "operation"}
              and existing.get("scope") != candidate.get("scope")):
            blocker = _startup_blocker(
                "scope_resolution_conflict",
                f"scope 已冻结为 {existing.get('scope')}，重试不能改写为 {candidate.get('scope')}",
            )
        if blocker != scope.get("startup_blocker"):
            led.append("startup_blocker_recorded", startup_blocker=blocker)
        scope["startup_blocker"] = blocker
        scope["formal_input_authority"] = _formal_input_authority(str(scope.get("prereg_status") or ""))

    scope_status = "already_set"
    if scope["scope"] and not scope.get("startup_blocker") and "experiment_execution_scope" not in hook_state:
        try:
            result = await _classify_experiment_scope_for_bootstrap(state, scope)
            scope_status = str(result.get("status") or "error")
        except Exception as exc:
            scope_status = f"error: {type(exc).__name__}: {exc}"
    elif scope.get("startup_blocker"):
        scope_status = "blocked"
    elif not scope["scope"]:
        scope_status = "undecided"
    ob = {"status": "skipped"}
    envelope: dict[str, Any] = {"status": "skipped"}
    if not led.read_only:
        try:
            ob = _obligations.freeze(state)
        except Exception as exc:
            ob = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        try:
            try:
                from .remote_admission import freeze_envelope
            except ImportError:  # pragma: no cover
                from tools.remote_admission import freeze_envelope  # type: ignore
            envelope = freeze_envelope(state)
        except Exception as exc:
            envelope = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    ready = False
    if not led.read_only:
        minimal_ok = (str(ob.get("status")) in {"frozen", "already_frozen", "clarification_required"}
                      and str(envelope.get("status")) in {"frozen", "already_frozen"}
                      and materialized.get("status") == "ready")
        if minimal_ok:
            try:
                _ledger.mark_bootstrap_ready(state)
                ready = True
            except Exception as exc:
                ob = {**ob, "ready_error": f"{type(exc).__name__}: {exc}"}
        if ready:
            led.append("bootstrap_ready")
        else:
            # 停在 initializing：writer 会拒绝执行并交 runtime 修（Codex 27 号 P0-1），
            # 不会把它当普通 v3 run，也不会退回旧协议。
            led.append("bootstrap_incomplete", obligations=ob.get("status"),
                       envelope=envelope.get("status"),
                       materialize_status=materialized.get("status"),
                       materialize_error=materialized.get("error"))
    hook_state[_BOOTSTRAP_FLAG] = {"scope": scope["scope"], "scope_status": scope_status,
                                   "obligations": ob.get("status"), "ledger_read_only": led.read_only,
                                   "ready": ready}
    return {"scope": scope, "scope_status": scope_status, "obligations": ob,
            "materialized_roots": materialized, "envelope": envelope,
            "ledger": {"read_only": led.read_only, "epoch": led.epoch, "reason": led.reason}}


def one_page(state: Any, established: dict[str, Any]) -> str:
    inputs = _node_inputs(state)
    source = _obligations.task_source(state)
    goal = (source["text"] if source is not None else str(
        inputs.get("experiment_spec") or inputs.get("experiment_focus")
        or inputs.get("requested_work") or "（调用方没有给任务正文）").strip())
    roots = _writable_roots(state)
    lines = ["# 本 run 的目标与约束（框架从任务输入读出，不需要你确认）", "", "## 目标", goal]
    if not (established.get("scope") or {}).get("scope"):
        lines += ["", "**scope 未定**：任务输入既没绑定预注册也没点名 operation / scientific。框架不猜。"
                  "按任务要求继续执行和核验；分类未定本身不阻止动作或 success 收尾，不必补分类手续。"
                  "只有具体必要输入、方法或验收条件有缺口时才澄清该问题；未满足的条件如实记录，不猜成功。"]
    blocker = (established.get("scope") or {}).get("startup_blocker")
    if isinstance(blocker, dict) and blocker.get("code") == "scope_resolution_conflict":
        lines += ["", "## 分类描述备注",
                  f"- {blocker.get('reason')}。保留原描述与这次差异；分类差异本身不阻止执行或收尾。"]
    elif isinstance(blocker, dict):
        lines += ["", "## 启动输入阻断",
                  f"- `{blocker.get('code')}`：{blocker.get('reason')}。",
                  "- A 已经 durable ready；这个 blocker 只表示 formal scientific input 当前不可消费，"
                  "不会把能力探测、获取输入、诊断、取消或诚实报告全局锁死。"]
    lines += ["", "## 交付物"]
    from .delivery_transaction import MODEL_DELIVERY_CONTRACT
    lines.append("- " + MODEL_DELIVERY_CONTRACT["experiment_v3_delivery"])
    obligations = _obligations.frozen(state)
    deliverables = [ob for ob in obligations if ob["kind"] != "artifact"]
    if deliverables:
        for ob in deliverables:
            if ob["kind"] == "file":
                lines.append(f"- {ob['id']}：文件 `{ob['path']}`" + ("（非空）" if ob.get("non_empty", True) else ""))
            elif ob["kind"] == "glob":
                lines.append(f"- {ob['id']}：至少 {ob.get('min_count', 1)} 个匹配 `{ob['pattern']}` 的文件")
            elif ob["kind"] == "task":
                lines.append("- 原始任务全文已保存；没有把模型解释或现有结果倒推为用户的成功标准。")
                lines.append("- 按原任务执行并核验，调用 `finish` 时用 `verification` 忠实解释任务要求（无需逐字引用）、"
                             "检查方法与真实证据；无需预先声明清单，也无需补内部分类字段。")
                lines.append("- 检查方法是你的专业解释，不是用户追加要求或语义完整性的机械证明；"
                             "只有具体业务要求确实含糊时才澄清那个问题。")
            else:
                marker = f"，标准输出含 `{ob['expect_stdout_contains']}`" if ob.get("expect_stdout_contains") else ""
                lines.append(f"- {ob['id']}：检查命令 `{ob['command']}` 退出码 {ob.get('expect_exit', 0)}{marker}（收尾时由框架自己跑一遍）")
    elif established["obligations"].get("status") == "clarification_required":
        lines.append("- **任务正文与结构化交付要求都缺失。** 请澄清实际要完成的工作及验收要求，"
                     "不要要求用户填写内部清单或分类字段。")
    else:
        lines.append("- （未冻结）")
    lines += ["", "## 你能写哪里（目录已经建好）"]
    role_lines = []
    try:
        try:
            from .path_roles import collect_path_roles
        except ImportError:  # pragma: no cover
            from tools.path_roles import collect_path_roles  # type: ignore
        for role in collect_path_roles(state):
            if getattr(role, "writable", False) and not getattr(role, "container_only", False):
                role_lines.append(f"- {role.role}：`{role.path}`")
            elif not getattr(role, "writable", False):
                role_lines.append(f"- {role.role}：`{role.path}`（只读）")
    except Exception:
        role_lines = []
    lines += role_lines or [f"- {r}" for r in roots] or ["- （框架未报告可写根）"]
    board = str(getattr(state, "scratchpad", "") or "").strip()
    if board:
        # 恢复 / 跨会话：把模型上次的工作计划带回来，醒目标注它是模型自己写的、非权威（Codex 44）
        lines += ["", "## 你上次写的工作计划（你自己的工作假设，不是框架事实；以下面的事实为准）",
                  board[:4000] + ("\n…（截断）" if len(board) > 4000 else "")]
    lines += ["", "## 环境", "- " + _scheduler_facts(state)]
    env = (established.get("envelope") or {}).get("limits") or {}
    if env:
        lines.append("- 预授权包络：" + "、".join(f"{k}≤{v}" for k, v in sorted(env.items()))
                     + "（之内不再问；越出或首次真实外部提交会问一次）")
    else:
        lines.append("- 预授权包络：调用方没给；首次真实外部提交与每个没有上限的维度都会问一次")
    try:
        try:
            from .remote_admission import isolation_disclosure
        except ImportError:  # pragma: no cover
            from tools.remote_admission import isolation_disclosure  # type: ignore
        for scheduler_name in ("slurm", "pbs"):
            caps = isolation_disclosure(scheduler_name)
            if caps["remote_write_isolation"] != "enforced":
                lines.append(f"- {scheduler_name} 作业的写边界：{caps['means']}")
                break
    except Exception:
        pass
    lines += ["", "## 怎么结束",
              "- 干完（或干不下去）就调 `finish`：说 outcome、一段如实的摘要、实际交付了什么。",
              "- 框架自己核对交付物在不在、是不是本 run 写出的、检查命令过不过、作业是不是都终态了。",
              "- success 站不住时它会一次告诉你缺什么、哪些诚实的 outcome 可达；partial / failed / blocked / cancelled 永远可达。"]
    return "\n".join(lines)


def _on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """turn 1 建立 A；没建成（initializing）就在后续每轮重放同一个幂等 establish，直到 ready。

    ready 恢复仍核对同一 A；未变时零字，不重开账本或重复物理动作。
    """
    state = ctx.state
    phase = _ledger.bootstrap_phase(state)
    if phase == "ready":
        from .task_input_view import audit_selected_task_binding
        authority = load_run_authority(state)
        audit = audit_selected_task_binding(state)
        blocker = _startup_blocker(audit["status"], audit["reason"]) if not audit["passed"] else None
        record_drift = bool(not audit["passed"] and authority
                            and authority.get("startup_blocker") != blocker)
        restore = bool(audit["passed"] and authority
                       and (authority.get("startup_blocker") or {}).get("code") in _BINDING_BLOCKERS)
        candidate = (derive_scope(state) if audit["passed"] and authority
                     and (restore or authority.get("prereg_status") == "invalid") else None)
        refresh = bool(candidate and authority and authority.get("prereg_status") == "invalid"
                       and candidate.get("scope") == authority.get("scope")
                       and candidate.get("prereg_status") == "bound_valid")
        if record_drift or restore or refresh:
            writer = _ledger.open_ledger(state, write=True)
            if writer.read_only:
                return [LLMMessage(role="system", content=(
                    f"[只读观察] 当前输入核验：{audit['status']}；存在待更新的输入事实。"
                    f"本会话不是可写 owner（{writer.reason}），未改写任务记录、未完成持久恢复；"
                    "可继续只读观察，由原 owner 或正式接管流程处理。"))]
        if not audit["passed"]:
            if record_drift:
                writer.append("startup_blocker_recorded", startup_blocker=blocker)
                return [LLMMessage(role="system", content=(
                    f"[恢复输入不可核验] {blocker['reason']}。保留原 run 证据，"
                    "请父编排器核对原任务；新任务须 fresh 派发，不要补分类字段。"))]
            return None
        if restore and not refresh:
            # Restore the original formal-input observation, not a forged all-clear.
            writer.append("startup_blocker_recorded", startup_blocker=candidate.get("startup_blocker"))
        if refresh:
            _run_sync(establish(state))
            return [LLMMessage(role="system", content=(
                "[正式输入已就绪] 本 run 原任务指定的同一份输入现已通过身份与冻结核验。"
                "任务未变，已发生动作不会重跑；可以继续消费该输入。"))]
        return None
    if phase == "none" and ctx.turn != 1:
        return None  # 不是本 hook 启动的 run（旧协议 / 未接线），不在中途插手
    first = phase == "none"
    try:
        established = _run_sync(establish(state))
    except Exception as exc:
        return [LLMMessage(role="system", content=f"[启动] 框架建立本 run 的目标与约束时出错：{type(exc).__name__}: {exc}；下一轮会再试同一步。")]
    ready = _ledger.bootstrap_phase(state) == "ready"
    if not ready:
        ob = established.get("obligations") or {}
        env = established.get("envelope") or {}
        roots = established.get("materialized_roots") or {}
        return [LLMMessage(role="system", content=(
            "[启动未完成] 框架还没建立完本 run 的目标与约束（交付物清单："
            f"{ob.get('status')}{'，' + str(ob.get('reason')) if ob.get('reason') else ''}；授权包络：{env.get('status')}"
            f"{'，' + str(env.get('reason')) if env.get('reason') else ''}；可写根：{roots.get('status')}"
            f"{'，' + str(roots.get('error')) if roots.get('error') else ''}）。有副作用的动作会被拒绝；"
            "框架下一轮会再试同一步。若一直不成功，report_blocker 交给人。"))]
    if not first:
        return [LLMMessage(role="system", content="[启动完成] 目标与约束已建立，可以开始。")] + \
               [LLMMessage(role="system", content=one_page(state, established))]
    try:
        text = one_page(state, established)
    except Exception as exc:
        text = f"一页纸生成失败：{type(exc).__name__}: {exc}"
    return [LLMMessage(role="system", content=text)]


def _run_sync(coro):
    """在 hook 的同步上下文里跑一个协程；已有事件循环时用独立线程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _register_once(hook: LoopHook) -> None:
    """同一个模块可能以两个模块名各 import 一次；注册一次即可，不刷覆盖警告。"""
    if get_loop_hook(hook.name) is None:
        register_loop_hook(hook)

experiment_v3_bootstrap = LoopHook(
    name="experiment_v3_bootstrap",
    description="v3 启动：框架读出目标与约束、物化可写根、冻结交付物清单、打开账本，并给模型一页纸（模型不参与）",
    on_turn_start=_on_turn_start,
)
_register_once(experiment_v3_bootstrap)
